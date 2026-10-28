"""Portfolio Planner: turn an amount of cash and a list of stocks into a sized plan, and say
what to do with stocks already held.

Every figure here is a model-generated signal built from MarketBeat data - the analysts'
buy/hold/sell count, their consensus price target and the stored closing prices. It is not
financial advice, and every screen and printout says so.

The rules, kept deliberately simple so each output can be explained in one line:

  rating      Buy when at least 90% of analysts say buy and none say sell (the same bar as the
              menu's list - a 34/8/1 "Moderate Buy" is not enough); Sell when a quarter or
              more say sell, or fewer than 40% say buy; Hold otherwise. The
              confidence beside it is the ratio score (the lower edge of the buy share's 95%
              interval, mb_ratio.ratio_score), so six analysts count for less than forty.
  sizing      Only Buy-rated stocks get money, in proportion to confidence, no more than
              40% of the cash in one stock, in whole shares, never more than the cash. With
              fewer than three Buy-rated stocks the cap leaves some cash uninvested, on
              purpose: one or two stocks are not a portfolio.
  entry       Buy now at today's price, unless the stock is stretched - more than 5% above
              its 20-day average or RSI over 70 - in which case wait for a limit at the
              20-day average or one week's typical move lower, whichever is higher.
  holdings    Gain/loss against the purchase price. A trailing stop sits two months' typical
              moves below the higher of the purchase price and the last month's high close;
              the target is the analysts' consensus, or one month's typical move up when the
              consensus is already below the price. That target is the suggested sell limit.
              A sell limit you set yourself replaces it. Sell now when the rating is Sell, the
              price has reached the sell limit, or the stop has broken; otherwise hold.
"""
import json
import math

import pandas as pd

from . import db, prices
from .enrich import rsi as rsi_of
from .config import MAX_LIST_SELLS, MIN_LIST_BUY_SHARE
from .mb_ratio import ratio_score
from .ratings import counts_of

DISCLAIMER = "Model-generated signals from MarketBeat data - not financial advice."
SELL_SHARE_SELL = 0.25
BUY_SHARE_SELL = 0.40
MAX_WEIGHT = 0.40
STRETCHED_ABOVE_MA20 = 0.05
RSI_HOT = 70
STOP_MONTHS = 2.0
PICK_CANDIDATES = 3          # program-pick mode reads up to this many candidates per slot


# ---------------------------------------------------------------- signals

def signal(counts: tuple[int, int, int] | None) -> dict:
    if not counts or not sum(counts):
        return {"label": "Hold", "buy_share": None, "confidence": None,
                "text": "no analyst ratings on MarketBeat", "counts": counts}
    buy, hold, sell = counts
    total = buy + hold + sell
    share, sell_share = buy / total, sell / total
    if sell_share >= SELL_SHARE_SELL or share < BUY_SHARE_SELL:
        label = "Sell"
    elif share >= MIN_LIST_BUY_SHARE and sell <= MAX_LIST_SELLS:
        label = "Buy"
    else:
        label = "Hold"
    return {"label": label, "buy_share": share, "confidence": ratio_score(buy, hold, sell) * 100,
            "counts": counts, "text": f"{buy} buy / {hold} hold / {sell} sell ({share:.0%} buy)"}


def _daily_sigma(closes: pd.Series) -> float | None:
    rets = closes.pct_change().dropna().tail(252)
    return float(rets.std()) if len(rets) >= 20 else None


def entry_advice(closes: pd.Series, price: float) -> dict:
    """Buy now, or wait for a limit price, and why."""
    if len(closes) < 21 or not price:
        return {"action": "Buy now", "limit": None, "reason": "not enough price history to judge"}
    ma20 = float(closes.tail(20).mean())
    rsi = rsi_of(closes)
    sigma = _daily_sigma(closes) or 0.0
    above = price / ma20 - 1
    if above > STRETCHED_ABOVE_MA20 or (rsi is not None and rsi > RSI_HOT):
        limit = round(min(price, max(ma20, price * (1 - sigma * math.sqrt(5)))), 2)
        why = (f"{above:+.0%} vs its 20-day average" if above > STRETCHED_ABOVE_MA20
               else f"RSI {rsi:.0f}, overbought")
        return {"action": "Wait for limit", "limit": limit,
                "reason": f"{why} - limit at ${limit:,.2f} ({limit / price - 1:+.1%})"}
    return {"action": "Buy now", "limit": None,
            "reason": f"{above:+.1%} vs its 20-day average" + (f", RSI {rsi:.0f}" if rsi else "")}


def allocate(cash: float, rows: list[dict]) -> float:
    """Whole-share amounts for the Buy-rated rows, in proportion to confidence. Returns the
    cash left over. Each row gains `shares`, `amount` and `entry_price`."""
    for r in rows:
        r["shares"], r["amount"] = 0, 0.0
        r["entry_price"] = r["entry"]["limit"] or r["price"]
    buys = [r for r in rows if r["signal"]["label"] == "Buy" and r.get("entry_price")]
    if not buys or cash <= 0:
        return cash
    raw = {r["ticker"]: max(r["signal"]["confidence"] or 0, 1.0) for r in buys}
    # Shares of the cash: in proportion to confidence, capped at MAX_WEIGHT, with what a
    # capped stock gives up handed to the uncapped ones. Whatever no stock can take stays cash.
    weights = {t: w / sum(raw.values()) for t, w in raw.items()}
    for _ in range(len(buys)):
        over = [t for t, w in weights.items() if w > MAX_WEIGHT + 1e-9]
        if not over:
            break
        spare = sum(weights[t] - MAX_WEIGHT for t in over)
        for t in over:
            weights[t] = MAX_WEIGHT
        free = [t for t, w in weights.items() if w < MAX_WEIGHT - 1e-9]
        if not free:
            break
        free_total = sum(raw[t] for t in free)
        for t in free:
            weights[t] += spare * raw[t] / free_total
    total = 1.0
    left = cash
    for r in buys:
        target = cash * weights[r["ticker"]] / total
        r["shares"] = int(target // r["entry_price"])
        r["amount"] = r["shares"] * r["entry_price"]
        left -= r["amount"]
    # Spend what rounding left behind, one share at a time, on the stock furthest below its
    # share of the cash - never going over the cash, and never more than half a share past
    # a stock's own share.
    while True:
        gaps = sorted(((cash * weights[r["ticker"]] / total - r["amount"], r) for r in buys
                       if r["entry_price"] <= left
                       and cash * weights[r["ticker"]] / total - r["amount"] >= r["entry_price"] / 2),
                      key=lambda g: -g[0])
        if not gaps:
            break
        r = gaps[0][1]
        r["shares"] += 1
        r["amount"] += r["entry_price"]
        left -= r["entry_price"]
    for r in buys:
        r["amount"] = round(r["amount"], 2)
    return round(left, 2)


def sell_guidance(holding: dict, price: float | None, closes: pd.Series, sig: dict,
                  target: float | None) -> dict:
    shares, paid = holding["shares"], holding["purchase_price"]
    own = holding.get("sell_limit")
    out = {"ticker": holding["ticker"], "shares": shares, "purchase_price": paid, "price": price,
           "signal": sig, "sell_limit": own, "cost": round(shares * paid, 2)}
    if not price:
        out.update(action="Hold", reason="no current price from MarketBeat")
        return out
    out["value"] = round(shares * price, 2)
    out["gain"] = round((price - paid) * shares, 2)
    out["gain_pct"] = round((price / paid - 1) * 100, 2) if paid else None
    sigma = _daily_sigma(closes) or 0.02
    month = sigma * math.sqrt(21)
    recent_high = float(closes.tail(21).max()) if len(closes) else price
    stop = max(0.01, round(max(paid, recent_high) * (1 - STOP_MONTHS * month), 2))
    target_sell = round(target, 2) if target and target > price else round(price * (1 + month), 2)
    out.update(stop_loss=stop, target_sell=target_sell)
    limit_text = f"your ${own:,.2f} sell limit" if own else f"a ${target_sell:,.2f} sell limit"
    if sig["label"] == "Sell":
        out.update(action="Sell now", target_sell=round(price, 2),
                   reason=f"MarketBeat's ratio has turned to Sell - {sig['text']}")
    elif own and price >= own:
        out.update(action="Sell now", target_sell=round(price, 2),
                   reason=f"reached your ${own:,.2f} sell limit - sell at today's price")
    elif not own and target and price >= target:
        out.update(action="Sell now", target_sell=round(price, 2),
                   reason=f"already at or above the analysts' ${target:,.2f} target - sell at today's price")
    elif price <= stop:
        out.update(action="Sell now", target_sell=round(price, 2),
                   reason=f"broke the ${stop:,.2f} stop - sell at today's price")
    elif sig["label"] == "Hold":
        out.update(action="Hold", reason=f"ratio is mixed ({sig['text']}); place {limit_text}, "
                                         f"and sell if it closes below ${stop:,.2f}")
    else:
        out.update(action="Hold", reason=f"still Buy-rated ({sig['text']}); place {limit_text}, "
                                         f"stop ${stop:,.2f}")
    return out


# ---------------------------------------------------------------- data

def current(ticker: str, exchange: str | None = None) -> dict:
    """Today's MarketBeat reading for one stock: read live unless a run already read it today."""
    from . import pipeline
    with db.connect() as con:
        stored = db.load_json(con, "mb_detail", db.today()).get(ticker)
    if stored and stored.get("rating_counts"):
        return stored
    data = pipeline.read_stock(prices.shared_session(), ticker, exchange or prices.exchange_of(ticker))
    with db.connect() as con:
        pipeline.save_stock(con, data, db.today())
    return data


def recommendation_choices(n: int = 40) -> list[dict]:
    """The latest saved recommendations, for picking tickers by hand."""
    with db.connect() as con:
        last = con.execute("SELECT MAX(run_id) FROM recommendations").fetchone()[0]
        if last is None:
            return []
        return [dict(r) for r in con.execute(
            "SELECT * FROM recommendations WHERE run_id=? ORDER BY rank LIMIT ?", (last, n))]


def holdings() -> list[dict]:
    with db.connect() as con:
        return [dict(r) for r in con.execute("SELECT * FROM holdings ORDER BY ticker")]


def save_holding(ticker: str, shares: float, purchase_price: float, purchase_date: str | None = None):
    with db.connect() as con:
        con.execute("INSERT INTO holdings(ticker, shares, purchase_price, purchase_date, updated) "
                    "VALUES (?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET shares=excluded.shares, "
                    "purchase_price=excluded.purchase_price, purchase_date=excluded.purchase_date, "
                    "updated=excluded.updated",
                    (ticker.upper(), shares, purchase_price, purchase_date, db.now()))


def add_lot(ticker: str, shares: float, total_paid: float, purchase_date: str | None = None):
    """Another purchase of a stock: added to what is already held (shares and total paid sum,
    the average price follows; the sell limit stays). save_holding replaces instead."""
    with db.connect() as con:
        old = con.execute("SELECT shares, purchase_price FROM holdings WHERE ticker=?",
                          (ticker.upper(),)).fetchone()
    if old:
        shares, total_paid = old[0] + shares, old[0] * old[1] + total_paid
    save_holding(ticker, shares, total_paid / shares, purchase_date)


def delete_holding(ticker: str):
    with db.connect() as con:
        con.execute("DELETE FROM holdings WHERE ticker=?", (ticker.upper(),))


def set_sell_limit(ticker: str, price: float | None):
    """Your own sell limit for a holding; None goes back to the suggested one."""
    with db.connect() as con:
        con.execute("UPDATE holdings SET sell_limit=?, updated=? WHERE ticker=?",
                    (price, db.now(), ticker.upper()))


def review(say=print) -> list[dict]:
    """Every saved holding, read live on MarketBeat, with its sell guidance."""
    guidance = []
    for h in holdings():
        say(f"checking your {h['ticker']} on MarketBeat...")
        try:
            data = current(h["ticker"])
            closes = prices.closes(h["ticker"])
            price = data.get("price") or (float(closes.iloc[-1]) if len(closes) else None)
            g = sell_guidance(h, price, closes, signal(counts_of(data.get("rating_counts"))),
                              data.get("consensus_target"))
            g["url"] = data.get("source_url")
            guidance.append(g)
        except Exception as exc:
            guidance.append({"ticker": h["ticker"], "shares": h["shares"],
                             "purchase_price": h["purchase_price"], "sell_limit": h.get("sell_limit"),
                             "cost": round(h["shares"] * h["purchase_price"], 2), "action": "Hold",
                             "reason": f"could not read MarketBeat ({type(exc).__name__})",
                             "signal": signal(None)})
    return guidance


def _row(ticker: str, data: dict) -> dict:
    from .mb_ratio import in_dollars
    if not in_dollars(data):
        raise ValueError(f"priced in {data.get('currency')} on {data.get('exchange')}, not US dollars")
    closes = prices.closes(ticker)
    price = data.get("price") or (float(closes.iloc[-1]) if len(closes) else None)
    sig = signal(counts_of(data.get("rating_counts")))
    return {"ticker": ticker, "company": data.get("company") or "", "price": price,
            "target": data.get("consensus_target"), "upside": data.get("upside_pct"),
            "signal": sig, "entry": entry_advice(closes, price), "closes": closes,
            "url": data.get("source_url")}


def build(cash: float, tickers: list[str] | None = None, picks: int = 5, say=print) -> dict:
    """The whole plan. `tickers` None means the program picks `picks` Buy-rated stocks from the
    latest recommendations, checking each one's rating live before using it."""
    rows, skipped = [], []
    if tickers:
        mode = "own"
        for t in tickers:
            say(f"reading {t} on MarketBeat...")
            try:
                rows.append(_row(t, current(t)))
            except Exception as exc:
                skipped.append(f"{t}: {exc}")
    else:
        mode = "picked"
        for rec in recommendation_choices(picks * PICK_CANDIDATES):
            if len(rows) >= picks:
                break
            say(f"checking {rec['ticker']} on MarketBeat...")
            try:
                row = _row(rec["ticker"], current(rec["ticker"], rec.get("exchange")))
            except Exception as exc:
                skipped.append(f"{rec['ticker']}: {exc}")
                continue
            if row["signal"]["label"] == "Buy":
                rows.append(row)
            else:
                skipped.append(f"{rec['ticker']}: now {row['signal']['label']} ({row['signal']['text']})")
    left = allocate(cash, rows)
    guidance = review(say)
    plan ={"cash": cash, "mode": mode, "rows": rows, "left": left, "holdings": guidance,
            "skipped": skipped, "disclaimer": DISCLAIMER}
    with db.connect() as con:
        cur = con.execute("INSERT INTO plans(created, cash, mode, rows) VALUES (?,?,?,?)",
                          (db.now(), cash, mode, json.dumps(
                              {"buy": [{k: v for k, v in r.items() if k != "closes"} for r in rows],
                               "left": left, "holdings": guidance, "skipped": skipped},
                              default=str)))
        plan["id"] = cur.lastrowid
    return plan
