"""The stocks with the best buy/hold/sell ratio on MarketBeat.

MarketBeat's own research lists (upgrades, top rated, breakouts, sectors and the rest in
discover.SOURCES) supply the candidates, because a free account cannot run MarketBeat's full
screener. Each candidate's forecast page then gives the analyst count, the buy/hold/sell
split, the consensus target and the upside - all read by pipeline.py and stored.

Ranking is by net buys: buys, minus HOLD_WEIGHT for every hold and NET_SELL_WEIGHT for
every sell. The most analysts saying Buy with the fewest holds comes first - NVDA's 54/1/0
(52) and AMZN's 57/3/0 (51) above V's 37/0/0 (37) - and a stock with any sell is left off
the list (config.MAX_LIST_SELLS). The planner's confidence still uses the buy share's
Wilson lower bound (ratio_score), with sells counted SELL_WEIGHT times.
"""
import math

from . import db
from .config import LIST_MAX_HOLDS, LIST_MIN_BUYS, MAX_LIST_SELLS
from .ratings import counts_of

MIN_ANALYSTS = 5
SELL_WEIGHT = 2
HOLD_WEIGHT = 2
NET_SELL_WEIGHT = 5
Z = 1.96
# The list, the planner and the backtest size positions in dollars, so listings priced in
# another currency (London in pence, Toronto in Canadian dollars) are left out of them.
US_EXCHANGES = {"NASDAQ", "NYSE", "NYSEAMERICAN", "NYSEARCA", "BATS", "OTCMKTS"}


def in_dollars(d: dict) -> bool:
    currency = d.get("currency")
    if currency:
        return currency == "$"
    return (d.get("exchange") or "NASDAQ") in US_EXCHANGES


def ratio_score(buy: int, hold: int, sell: int) -> float:
    """Wilson lower bound of the buy share, with each sell counted SELL_WEIGHT times."""
    n = buy + hold + SELL_WEIGHT * sell
    if n == 0:
        return 0.0
    p = buy / n
    centre = p + Z * Z / (2 * n)
    spread = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n))
    return (centre - spread) / (1 + Z * Z / n)


def net_buys(buy: int, hold: int, sell: int) -> int:
    return buy - HOLD_WEIGHT * hold - NET_SELL_WEIGHT * sell


def list_rule(buy: int, hold: int, sell: int) -> bool:
    """Whether a stock belongs on the menu list: no sells, few holds, many buys."""
    return sell <= MAX_LIST_SELLS and hold <= LIST_MAX_HOLDS and buy >= LIST_MIN_BUYS


# Ranking rules shared by the menu list (LIST_RULE) and the backtest (replay.run(rule=...)).
# Each scores (buy, hold, sell); None leaves the stock out.
RULES = {
    "list": (lambda b, h, s: net_buys(b, h, s) if list_rule(b, h, s) else None,
             "net buys, list stocks only (35+ buys, under 5 holds, no sells)"),
    "net": (net_buys, "net buys (buys - 2 x holds - 5 x sells)"),
    "net0": (lambda b, h, s: net_buys(b, h, s) if s == 0 else None, "net buys, no sells"),
    "ratio": (lambda b, h, s: ratio_score(b, h, s), "buy share (the old ranking)"),
}
LIST_RULE = "list"


def sort_key(score: float, hold: int, ticker: str) -> tuple:
    """Highest score first; on a tie, fewer holds; then the ticker."""
    return (-score, hold, ticker)


def row_for(ticker: str, d: dict, p: dict | None = None) -> dict | None:
    counts = counts_of(d.get("rating_counts"))
    if not counts or not in_dollars(d):
        return None
    buy, hold, sell = counts
    total = buy + hold + sell
    if not total:
        return None
    exchange = d.get("exchange") or "NASDAQ"
    return {"ticker": ticker, "exchange": exchange, "company": d.get("company") or "",
            "sector": d.get("sector"),
            "url": d.get("source_url") or f"https://www.marketbeat.com/stocks/{exchange}/{ticker}/",
            "buy": buy, "hold": hold, "sell": sell, "analysts": total, "buy_share": buy / total,
            "score": net_buys(buy, hold, sell),
            "target": d.get("consensus_target"), "upside": d.get("upside_pct"),
            "price": (p or {}).get("last_close") or d.get("price"),
            "lists": d.get("sources") or []}


def rank(rows: list[dict], min_analysts: int = MIN_ANALYSTS) -> list[dict]:
    """Best first by RULES[LIST_RULE], one listing per company (GOOGL and GOOG are both
    Alphabet); stocks the rule leaves out are dropped."""
    from .score import one_per_company
    score = RULES[LIST_RULE][0]
    kept = []
    for r in rows:
        s = score(r["buy"], r["hold"], r["sell"]) if r["analysts"] >= min_analysts else None
        if s is not None:
            kept.append({**r, "score": s})
    kept.sort(key=lambda r: sort_key(r["score"], r["hold"], r["ticker"]))
    distinct, _ = one_per_company([r["ticker"] for r in kept],
                                  {r["ticker"]: r.get("company") for r in kept})
    keep = set(distinct)
    kept = [r for r in kept if r["ticker"] in keep]
    for i, r in enumerate(kept, 1):
        r["rank"] = i
    return kept


def store_ranking(detail: dict, price_rows: dict, run_id: int | None = None) -> dict:
    """Rank everything read today and save it as this run's recommendations."""
    rows = [r for t, d in detail.items() if (r := row_for(t, d, price_rows.get(t)))]
    ranked = rank(rows)
    run_date = db.today()
    result = {"built": run_date, "candidates": len(detail), "read": len(rows), "rows": ranked}
    with db.connect() as con:
        if run_id is None:
            run_id = db.start_run(con, "ranking")
            db.finish_run(con, run_id, {"ranked": len(ranked)})
        con.execute("DELETE FROM recommendations WHERE run_id=?", (run_id,))
        con.executemany(
            "INSERT INTO recommendations(run_id, run_date, ticker, rank, score, buy, hold, sell, "
            "buy_share, price, target, upside_pct, exchange) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, run_date, r["ticker"], r["rank"], r["score"], r["buy"], r["hold"], r["sell"],
              r["buy_share"], r["price"], r["target"], r["upside"], r["exchange"]) for r in ranked])
        db.set_meta(con, "mb_ratio", result)
    return result


def run(say=print) -> dict:
    """Read MarketBeat afresh (pipeline.refresh) and return the ranking."""
    from . import pipeline
    pipeline.refresh(say)
    return load()


def load() -> dict | None:
    """The latest ranking: today's if a run has stored one, else the newest saved run."""
    with db.connect() as con:
        meta = db.get_meta(con, "mb_ratio")
        if meta:
            return meta
        last = con.execute("SELECT MAX(run_id) FROM recommendations").fetchone()[0]
        if last is None:
            return None
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM recommendations WHERE run_id=? ORDER BY rank", (last,))]
    out = []
    for r in rows:
        total = r["buy"] + r["hold"] + r["sell"]
        out.append({"ticker": r["ticker"], "exchange": r["exchange"],
                    "url": f"https://www.marketbeat.com/stocks/{r['exchange'] or 'NASDAQ'}/{r['ticker']}/",
                    "buy": r["buy"], "hold": r["hold"], "sell": r["sell"], "analysts": total,
                    "buy_share": r["buy_share"], "score": r["score"], "target": r["target"],
                    "upside": r["upside_pct"], "price": r["price"], "rank": r["rank"]})
    return {"built": rows[0]["run_date"] if rows else None, "candidates": len(out),
            "read": len(out), "rows": out}


def show(result: dict, top: int = 20):
    print(f"\n  MarketBeat — most buys, fewest holds, no sells  (built {result['built']}, "
          f"{result['read']:,} stocks read)")
    from . import prices
    rows = result["rows"][:top]
    chart, facts = prices.chart_changes([r["ticker"] for r in rows], result["built"])
    print(f"  {'#':>3} {'stock':<7}{'buy':>5}{'hold':>6}{'sell':>6}{'net':>6}{'buy %':>8}{'1 week':>9}"
          f"{'1 month':>9}{'1 year':>9}{'price':>10}{'target':>10}{'upside':>9}")
    for r in rows:
        live = (facts.get(r["ticker"]) or {}).get("price") or r["price"]
        price = f"{live:.2f}" if live else "-"
        target = f"{r['target']:.2f}" if r["target"] else "-"
        upside = f"{(r['target'] / live - 1) * 100:+.1f}%" if r["target"] and live else "-"
        ch = chart.get(r["ticker"]) or {}
        moves = "".join(f"{ch[k]:>+8.1f}%" if ch.get(k) is not None else f"{'-':>9}"
                        for k in ("1w", "1m", "1y"))
        net = net_buys(r["buy"], r["hold"], r["sell"])
        print(f"  {r['rank']:>3} {r['ticker']:<7}{r['buy']:>5}{r['hold']:>6}{r['sell']:>6}{net:>6}"
              f"{r['buy_share']:>7.0%}{moves}{price:>10}{target:>10}{upside:>9}")
