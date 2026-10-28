"""Backtest: replay the buy/hold/sell-ratio strategy over the history stored in the database.

On every rebalance day the strategy sees only what MarketBeat showed on that day: each
stock's buy/hold/sell count as it stood (a stored snapshot if one exists, otherwise rebuilt
from the stored rating actions - see ratings.counts_at) and the closing price. It ranks the
chosen stocks by net buys (mb_ratio.net_buys: buys - 2 x holds - 5 x sells; one listing per
company). Sells are not excluded here: the list's zero-sell rule cost about 11 points a run
on average in 66 backtests (homework.txt part 10). It holds the
top few in whole shares, and checks again after the holding period: stocks still in the top
few are kept, the rest are sold, and each newcomer is bought with an equal share of the
portfolio's value (or whatever cash is free, if less). Everything is valued at MarketBeat's
closes.

It is compared with buy-and-hold - the same stocks bought in equal amounts on the first day
and never touched - and with the S&P 500 (SPY) where its history is stored.

Two limits are reported with every result rather than hidden:
  - MarketBeat's chart serves about a year of closes. The database keeps every close it has
    ever stored, so the possible range grows with each run, but it cannot start earlier than
    the oldest stored day.
  - Rating counts older than the stored daily snapshots are rebuilt, and the rebuild drifts
    the further back it goes (about 2 points of buy share at one month, 4-6 at three months,
    ~10 at a year, against MarketBeat's own published counts).
Survivorship bias also applies: the stocks are ones MarketBeat lists today.

Two data guards, both reported in the notes. A stock whose stored closes move threefold (up
or down) in a single day is left out: in MarketBeat's chart that is almost always a split
the series was never adjusted for - ELOX shows a thousandfold "gain" on a 1-for-1000 reverse
split - and one such stock in an equal-weight basket of 2,000 adds fifty points to it. And
nothing under $1 is bought, by the strategy or by buy-and-hold.
"""
import json
import math
from datetime import date

import pandas as pd

from . import db, ratings
from .mb_ratio import MIN_ANALYSTS, net_buys
from .score import one_per_company

HINDSIGHT = ("These stocks were chosen from today's recommendations - the stocks MarketBeat's "
             "analysts rate best now. The past cannot have known that, so the result flatters the "
             "strategy; test 'every stored stock' for a fair comparison.")
SPLIT_JUMP = 3.0          # a one-day move this many times up or down marks a broken series
MIN_TRADE_PRICE = 1.0
DEFAULT_CASH = 10_000.0
DEFAULT_PICKS = 10
DEFAULT_HOLD_DAYS = 21
BENCHMARK = "SPY"

# Optional price-trend rules on top of the ratio, from the same stored closes MarketBeat's
# chart draws. Each needs the stock's price `days` calendar days before the rebalance day,
# so it cannot act until that much history is stored.
TRENDS = {
    "none": ("net buys only", None),
    "up1m": ("net buys, skipping stocks down over the last month", 30),
    "up3m": ("net buys, skipping stocks down over the last 3 months", 91),
    "blend3m": ("70% net-buys percentile, 30% 3-month price change percentile", 91),
}
DEFAULT_TREND = "none"


def _changes_on(full: pd.DataFrame, day: str, days: int) -> pd.Series:
    """Each stock's percent change over the `days` calendar days up to `day`."""
    when = pd.Timestamp(day)
    now = full.loc[:when]
    then = full.loc[:when - pd.Timedelta(days=days)]
    if now.empty or then.empty:
        return pd.Series(dtype=float)
    return (now.iloc[-1] / then.iloc[-1] - 1) * 100


def stored_tickers(min_days: int = 60) -> list[str]:
    """Stocks with enough stored price history to trade, priced in US dollars."""
    with db.connect() as con:
        return [r[0] for r in con.execute(
            "SELECT h.ticker FROM price_history h LEFT JOIN price_meta m ON m.ticker = h.ticker "
            "WHERE m.currency IS NULL OR m.currency = '$' "
            "GROUP BY h.ticker HAVING COUNT(*)>=? ORDER BY h.ticker", (min_days,))
            if r[0] != BENCHMARK]


def latest_recommendations(n: int = 20) -> list[str]:
    with db.connect() as con:
        last = con.execute("SELECT MAX(run_id) FROM recommendations").fetchone()[0]
        if last is None:
            return []
        return [r[0] for r in con.execute(
            "SELECT ticker FROM recommendations WHERE run_id=? ORDER BY rank LIMIT ?", (last, n))]


def history_range() -> tuple[str | None, str | None]:
    with db.connect() as con:
        r = con.execute("SELECT MIN(date), MAX(date) FROM price_history").fetchone()
    return r[0], r[1]


def _price_frame(tickers: list[str], start: str, end: str) -> pd.DataFrame:
    with db.connect() as con:
        marks = ",".join("?" * len(tickers))
        rows = con.execute(f"SELECT ticker, date, close FROM price_history WHERE ticker IN ({marks}) "
                           f"AND date>=? AND date<=?", [*tickers, start, end]).fetchall()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["ticker", "date", "close"])
    return df.pivot(index="date", columns="ticker", values="close").sort_index()


def _max_drawdown(values: list[float]) -> float:
    peak, worst = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, v / peak - 1)
    return worst * 100


def run(start: str, end: str, tickers: list[str] | None = None, cash: float = DEFAULT_CASH,
        picks: int = DEFAULT_PICKS, hold_days: int = DEFAULT_HOLD_DAYS,
        min_analysts: int = MIN_ANALYSTS, label: str = "", save: bool = True, say=print,
        from_recommendations: bool = False, trend: str = "none") -> dict:
    notes: list[str] = []
    trend_days = TRENDS[trend][1]
    if from_recommendations:
        notes.append(HINDSIGHT)
    initial_cash = cash
    first, last = history_range()
    if first is None:
        return {"error": "No price history stored yet - refresh from MarketBeat first."}
    if start < first:
        notes.append(f"MarketBeat price history in the database starts {first}; "
                     f"the requested start {start} was moved to it.")
        start = first
    if end > last:
        end = last
    if start >= end:
        return {"error": f"Nothing to replay between {start} and {end}."}

    universe = tickers or stored_tickers()
    frame = _price_frame(universe, start, end)
    if frame.empty:
        return {"error": "None of those stocks have stored prices in that range."}
    missing = sorted(set(universe) - set(frame.columns))
    if missing:
        notes.append(f"{len(missing)} of the chosen stocks have no stored prices in the range "
                     f"and were skipped" + (f": {', '.join(missing[:12])}" if len(missing) <= 12 else "."))
    # Trading days: the dates on which most of the chosen stocks traded.
    counts = frame.notna().sum(axis=1)
    days = [d for d in frame.index if counts[d] >= max(1, 0.5 * frame.shape[1])]
    frame = frame.loc[days].ffill()
    ratio = frame / frame.shift(1)
    broken = [t for t in frame.columns
              if ((ratio[t] > SPLIT_JUMP) | (ratio[t] < 1 / SPLIT_JUMP)).any()]
    if broken:
        frame = frame.drop(columns=broken)
        notes.append(f"{len(broken)} stock(s) left out for a one-day price move of {SPLIT_JUMP:.0f}x or "
                     f"more, almost always an unadjusted split in MarketBeat's chart"
                     + (f": {', '.join(broken)}" if len(broken) <= 12 else "."))
    if frame.empty:
        return {"error": "Every chosen stock was left out for broken price history."}
    if len(days) < hold_days + 2:
        return {"error": f"Only {len(days)} trading days stored between {start} and {end} - "
                         f"too few for a {hold_days}-day holding period."}

    with db.connect() as con:
        history = ratings.load(con, list(frame.columns))
        company_of = {r["ticker"]: r["company"] for r in con.execute("SELECT ticker, company FROM universe")}
    full = None
    if trend_days:
        # The trend looks back before the replay's first day, so it reads from the oldest close.
        full = _price_frame(list(frame.columns), first, end).ffill()
        full.index = pd.to_datetime(full.index)
        if pd.Timestamp(days[0]) - pd.Timedelta(days=trend_days) < full.index[0]:
            notes.append(f"The {trend_days}-day price trend needs history from before {first}, so "
                         f"until then stocks without it are skipped.")
    say(f"replaying {len(frame.columns)} stocks over {len(days)} trading days "
        f"({days[0]} to {days[-1]}), rebalancing every {hold_days} days")

    positions: dict[str, dict] = {}       # ticker -> {shares, entry_date, entry_price}
    trades: list[dict] = []
    equity: list[tuple[str, float]] = []
    pick_sources = {"snapshot": 0, "rebuilt": 0}
    rebalances = 0
    idle_cash: list[float] = []

    def close_position(ticker: str, day: str, price: float):
        nonlocal cash
        pos = positions.pop(ticker)
        cash += pos["shares"] * price
        trades.append({"ticker": ticker, "entry_date": pos["entry_date"],
                       "entry_price": pos["entry_price"], "exit_date": day, "exit_price": price,
                       "shares": pos["shares"], "pnl": pos["shares"] * (price - pos["entry_price"]),
                       "return_pct": (price / pos["entry_price"] - 1) * 100})

    for i, day in enumerate(days):
        row = frame.loc[day]
        if i % hold_days == 0 and i < len(days) - 1:
            rebalances += 1
            scored = []
            change = _changes_on(full, day, trend_days) if full is not None else None
            if trend == "blend3m":
                pct = change.rank(pct=True)
            for t in frame.columns:
                price = row.get(t)
                if price is None or pd.isna(price) or price < MIN_TRADE_PRICE:
                    continue
                stock = history.get(t)
                c = ratings.counts_at(stock, day) if stock else None
                if not c or sum(c) < min_analysts:
                    continue
                score = net_buys(*c)
                if score is None:
                    continue
                if change is not None:
                    moved = change.get(t)
                    if moved is None or pd.isna(moved):
                        continue
                    if trend in ("up1m", "up3m") and moved <= 0:
                        continue
                exact = any(0 <= (date.fromisoformat(day) - date.fromisoformat(d)).days
                            <= ratings.EXACT_WITHIN_DAYS for d in stock["snapshots"])
                scored.append((score, sum(c), t, exact))
            if trend == "blend3m" and scored:
                # Net buys has no fixed scale, so it is blended as a percentile too.
                net_pct = pd.Series({t: s for s, _, t, _ in scored}).rank(pct=True)
                scored = [(0.7 * float(net_pct[t]) + 0.3 * float(pct[t]), n, t, e)
                          for _, n, t, e in scored]
            scored.sort(key=lambda x: (-x[0], -x[1], x[2]))
            distinct, _ = one_per_company([t for _, _, t, _ in scored], company_of)
            chosen = distinct[:picks]
            exact_of = {t: exact for _, _, t, exact in scored}
            for t in chosen:
                pick_sources["snapshot" if exact_of[t] else "rebuilt"] += 1
            for t in [t for t in positions if t not in chosen]:
                close_position(t, day, float(row[t]))
            new = [t for t in chosen if t not in positions]
            if new:
                value_now = cash + sum(p["shares"] * float(row[t]) for t, p in positions.items())
                budget = min(cash / len(new), value_now / picks)
                for t in new:
                    price = float(row[t])
                    shares = int(budget // price)
                    if shares <= 0:
                        continue
                    cash -= shares * price
                    positions[t] = {"shares": shares, "entry_date": day, "entry_price": price}
        value = cash + sum(p["shares"] * float(row[t]) for t, p in positions.items())
        equity.append((day, value))
        idle_cash.append(cash / value if value else 0)

    final_day, final_row = days[-1], frame.loc[days[-1]]
    for t in list(positions):
        close_position(t, final_day, float(final_row[t]))

    start_cash = initial_cash
    # Buy-and-hold: the same money split equally across every chosen stock that traded on
    # the first day, bought once (fractional, so a large list is not starved by share prices).
    first_row = frame.loc[days[0]]
    held = [t for t in frame.columns
            if not pd.isna(first_row.get(t)) and first_row[t] >= MIN_TRADE_PRICE]
    stake = start_cash / max(len(held), 1)
    bh_units = {t: stake / float(first_row[t]) for t in held}
    bh_curve = [(d, sum(u * float(frame.loc[d, t]) for t, u in bh_units.items())) for d in days]

    from . import prices as price_store
    spy = price_store.closes(BENCHMARK, days[0], days[-1])
    spy_curve = None
    if len(spy) > 1:
        spy = spy.reindex(pd.to_datetime(days)).ffill().bfill()
        spy_curve = [(d, start_cash * float(v) / float(spy.iloc[0])) for d, v in zip(days, spy)]
    else:
        notes.append("No stored S&P 500 (SPY) history for the range, so no index comparison.")

    wins = [t for t in trades if t["pnl"] > 0]
    values = [v for _, v in equity]
    total = (values[-1] / start_cash - 1) * 100
    bh_total = (bh_curve[-1][1] / start_cash - 1) * 100
    total_picks = sum(pick_sources.values())
    if total_picks and pick_sources["rebuilt"]:
        notes.append(f"{pick_sources['rebuilt'] / total_picks:.0%} of the picks were made on rebuilt "
                     f"rating counts (no stored snapshot that day); see ratings.py for their accuracy.")
    metrics = {
        "start": days[0], "end": days[-1], "trading_days": len(days), "stocks": len(frame.columns),
        "start_cash": round(start_cash, 2), "end_equity": round(values[-1], 2),
        "total_return_pct": round(total, 2),
        "buy_hold_return_pct": round(bh_total, 2),
        "vs_buy_hold_pts": round(total - bh_total, 2),
        "spy_return_pct": round((spy_curve[-1][1] / start_cash - 1) * 100, 2) if spy_curve else None,
        "trades": len(trades),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
        "avg_trade_pct": round(sum(t["return_pct"] for t in trades) / len(trades), 2) if trades else None,
        "best_trade": max(trades, key=lambda t: t["return_pct"])["ticker"] if trades else None,
        "worst_trade": min(trades, key=lambda t: t["return_pct"])["ticker"] if trades else None,
        "max_drawdown_pct": round(_max_drawdown(values), 2),
        "buy_hold_max_drawdown_pct": round(_max_drawdown([v for _, v in bh_curve]), 2),
        "rebalances": rebalances,
        "avg_idle_cash_pct": round(sum(idle_cash) / len(idle_cash) * 100, 1) if idle_cash else 0,
        "picks_on_snapshots_pct": round(pick_sources["snapshot"] / total_picks * 100, 1) if total_picks else 0,
        "notes": notes,
    }
    params = {"start": start, "end": end,
              "tickers": "latest recommendations" if from_recommendations else (tickers or "all stored"),
              "ticker_list": tickers,
              "ticker_count": len(frame.columns), "cash": initial_cash,
              "picks": picks, "hold_days": hold_days, "min_analysts": min_analysts,
              "trend": trend, "rank": "net buys"}
    result = {"params": params, "metrics": metrics, "trades": trades,
              "equity": [{"date": d, "strategy": v, "buy_hold": bh[1],
                          "benchmark": spy_curve[k][1] if spy_curve else None}
                         for k, ((d, v), bh) in enumerate(zip(equity, bh_curve))]}
    if save:
        result["id"] = save_result(result, label)
    return result


def save_result(result: dict, label: str = "") -> int:
    with db.connect() as con:
        cur = con.execute("INSERT INTO backtests(created, label, params, metrics) VALUES (?,?,?,?)",
                          (db.now(), label, json.dumps(result["params"], default=str),
                           json.dumps(result["metrics"], default=str)))
        bid = int(cur.lastrowid)
        con.executemany(
            "INSERT INTO backtest_trades(backtest_id, ticker, entry_date, entry_price, exit_date, "
            "exit_price, shares, pnl, return_pct) VALUES (?,?,?,?,?,?,?,?,?)",
            [(bid, t["ticker"], t["entry_date"], t["entry_price"], t["exit_date"], t["exit_price"],
              t["shares"], t["pnl"], t["return_pct"]) for t in result["trades"]])
        con.executemany(
            "INSERT INTO backtest_equity(backtest_id, date, strategy, buy_hold, benchmark) "
            "VALUES (?,?,?,?,?)",
            [(bid, e["date"], e["strategy"], e["buy_hold"], e["benchmark"]) for e in result["equity"]])
    return bid


def saved(limit: int = 50) -> list[dict]:
    """Earlier backtests, newest first, for comparing runs over time."""
    with db.connect() as con:
        rows = con.execute("SELECT * FROM backtests ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{"id": r["id"], "created": r["created"], "label": r["label"],
             "params": json.loads(r["params"]), "metrics": json.loads(r["metrics"])} for r in rows]


def equity_of(backtest_id: int) -> list[dict]:
    with db.connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT date, strategy, buy_hold, benchmark FROM backtest_equity WHERE backtest_id=? "
            "ORDER BY date", (backtest_id,))]


def show(result: dict):
    if result.get("error"):
        print(f"  {result['error']}")
        return
    m, p = result["metrics"], result["params"]
    print(f"\n  Backtest #{result.get('id', '-')}  {m['start']} to {m['end']}  ·  {m['stocks']} stocks  ·  "
          f"top {p['picks']} held {p['hold_days']} days  ·  ${m['start_cash']:,.0f} start  ·  "
          f"{TRENDS[p.get('trend', 'none')][0]}")
    spy = f"{m['spy_return_pct']:+.2f}%" if m["spy_return_pct"] is not None else "-"
    print(f"  total return     {m['total_return_pct']:+.2f}%   (ends ${m['end_equity']:,.2f})")
    print(f"  buy-and-hold     {m['buy_hold_return_pct']:+.2f}%   strategy vs it {m['vs_buy_hold_pts']:+.2f} pts")
    print(f"  S&P 500 (SPY)    {spy}")
    win = f"{m['win_rate_pct']:.1f}%" if m["win_rate_pct"] is not None else "-"
    print(f"  trades {m['trades']}   win rate {win}   max drawdown {m['max_drawdown_pct']:.2f}% "
          f"(buy-and-hold {m['buy_hold_max_drawdown_pct']:.2f}%)")
    for n in m["notes"]:
        print(f"  note: {n}")
