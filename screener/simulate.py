"""How many stocks survive each choice of filter, and which ones sit on the boundary.

Read-only against data/screener.db. This is a sensitivity sweep, not a backtest: it shows
how restrictive each threshold is today, so a filter can be chosen deliberately rather than
by guessing. It says nothing about whether the survivors go on to outperform.

Important: rating counts only exist for stocks whose MarketBeat forecast page was fetched
(the finalists), so every count below is a lower bound on the full 18k universe.
"""
from . import db
from .config import (
    MIN_BUY_RATINGS, MAX_HOLD_RATINGS, MAX_SELL_RATINGS, FOCUS_MIN_PRICE,
    MAX_VOL_ANNUAL, MIN_DOLLAR_VOL,
)
from .score import rating_counts_of

BUY_STEPS = [5, 10, 15, 20, 25, 30]
HOLD_STEPS = [0, 1, 2, 3, 5, 10]
SELL_STEPS = [0, 1, 2, 5]
PRICE_STEPS = [0, 5, 25, 75, 150]


def collect() -> list[dict]:
    with db.connect() as con:
        bulk, prices, detail = (db.load_json(con, "mb_bulk"), db.load_json(con, "prices"),
                                db.load_json(con, "mb_detail"))
    rows = []
    for t, d in detail.items():
        counts = rating_counts_of(d)
        if counts is None:
            continue
        p, b = prices.get(t, {}), bulk.get(t, {})
        rows.append({
            "ticker": t,
            "company": (b.get("company") or "")[:26],
            "buy": counts[0], "hold": counts[1], "sell": counts[2],
            "price": p.get("last_close") or d.get("price") or b.get("price"),
            "ret_1m": p.get("ret_1m"),
            "vol": p.get("vol_20d_annualized"),
            "dollar_vol": p.get("avg_dollar_vol_20d"),
        })
    return rows


def survives(r: dict, buy: int, hold: int, sell: int, price: float) -> bool:
    return (r["buy"] >= buy and r["hold"] <= hold and r["sell"] <= sell
            and (r["price"] or 0) >= price)


def grid(rows: list[dict], sell: int, price: float):
    print(f"\n  Stocks surviving, with sell <= {sell} and price >= ${price:,.0f}")
    print("  (rows: minimum buy ratings   columns: maximum hold ratings)\n")
    print("        " + "".join(f"{('H<=%d' % h):>8}" for h in HOLD_STEPS))
    for b in BUY_STEPS:
        cells = "".join(f"{sum(1 for r in rows if survives(r, b, h, sell, price)):>8}"
                        for h in HOLD_STEPS)
        marker = " <- current" if b == MIN_BUY_RATINGS else ""
        print(f"  B>={b:<3}" + cells + marker)
    print("           " + "".join(("  ^^^^  " if h == MAX_HOLD_RATINGS else "        ")
                                  for h in HOLD_STEPS))


def one_axis(rows: list[dict], label: str, values, fn):
    print(f"\n  {label}")
    for v in values:
        keep = [r for r in rows if fn(r, v)]
        bar = "#" * min(60, len(keep))
        print(f"    {str(v):>8}  {len(keep):>4}  {bar}")


def boundary(rows: list[dict], sell: int, price: float):
    """Names that qualify now, and the near-misses one rating away."""
    passing = [r for r in rows if survives(r, MIN_BUY_RATINGS, MAX_HOLD_RATINGS, sell, price)]
    near = [r for r in rows
            if not survives(r, MIN_BUY_RATINGS, MAX_HOLD_RATINGS, sell, price)
            and survives(r, max(MIN_BUY_RATINGS - 5, 0), MAX_HOLD_RATINGS + 2, sell + 1, price)]
    print(f"\n  Passing the current filter ({MIN_BUY_RATINGS}+ buy, <={MAX_HOLD_RATINGS} hold, "
          f"<={MAX_SELL_RATINGS} sell, >=${FOCUS_MIN_PRICE:,.0f}):")
    for r in sorted(passing, key=lambda r: -r["buy"]):
        print(f"    {r['ticker']:<7}{r['company']:<28}{r['buy']:>3}B {r['hold']:>2}H {r['sell']:>2}S"
              f"   ${r['price'] or 0:>9,.2f}")
    if not passing:
        print("    (none)")
    print(f"\n  Near misses — would qualify if the filter loosened slightly:")
    for r in sorted(near, key=lambda r: -r["buy"])[:15]:
        why = []
        if r["buy"] < MIN_BUY_RATINGS:
            why.append(f"{r['buy']} buys")
        if r["hold"] > MAX_HOLD_RATINGS:
            why.append(f"{r['hold']} holds")
        if r["sell"] > MAX_SELL_RATINGS:
            why.append(f"{r['sell']} sells")
        print(f"    {r['ticker']:<7}{r['company']:<28}{r['buy']:>3}B {r['hold']:>2}H {r['sell']:>2}S"
              f"   ${r['price'] or 0:>9,.2f}   blocked by: {', '.join(why)}")
    if not near:
        print("    (none)")


def run():
    rows = collect()
    if not rows:
        print("No rating data yet — run `python run.py all` first.")
        return
    print("=" * 78)
    print("  FILTER SENSITIVITY — how restrictive each threshold is")
    print("=" * 78)
    print(f"\n  Based on {len(rows)} stocks whose full MarketBeat forecast page was fetched.")
    print("  Counts are lower bounds: stocks never fetched cannot appear here.")

    grid(rows, sell=MAX_SELL_RATINGS, price=FOCUS_MIN_PRICE)
    grid(rows, sell=MAX_SELL_RATINGS, price=0)

    one_axis(rows, "Allowing more sell ratings (buy >= %d, hold <= %d, any price)"
             % (MIN_BUY_RATINGS, MAX_HOLD_RATINGS), SELL_STEPS,
             lambda r, v: survives(r, MIN_BUY_RATINGS, MAX_HOLD_RATINGS, v, 0))
    one_axis(rows, "Raising the price floor (current rating filter)", PRICE_STEPS,
             lambda r, v: survives(r, MIN_BUY_RATINGS, MAX_HOLD_RATINGS, MAX_SELL_RATINGS, v))
    one_axis(rows, "Volatility ceiling, annualized (rating filter off)",
             [0.25, 0.35, 0.45, 0.60, 1.00],
             lambda r, v: (r["vol"] or 0) <= v)
    one_axis(rows, "Liquidity floor, average daily dollar volume (rating filter off)",
             [250_000, 1_000_000, 5_000_000, 25_000_000, 100_000_000],
             lambda r, v: (r["dollar_vol"] or 0) >= v)

    boundary(rows, sell=MAX_SELL_RATINGS, price=FOCUS_MIN_PRICE)

    print(f"\n  Current settings: buy>={MIN_BUY_RATINGS}, hold<={MAX_HOLD_RATINGS}, "
          f"sell<={MAX_SELL_RATINGS}, price>=${FOCUS_MIN_PRICE:,.0f}, "
          f"vol<={MAX_VOL_ANNUAL:.0%}, $vol>=${MIN_DOLLAR_VOL:,.0f}")
    print("  Change them in screener/config.py.\n")
