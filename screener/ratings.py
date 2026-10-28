"""MarketBeat's analyst consensus as it stood on a past date.

MarketBeat's free pages show today's buy/hold/sell count, plus three official snapshots on
the forecast page (1 month, 3 months and 1 year ago). Between those, the count has to be
rebuilt from the page's rating-action table: each brokerage's latest rating on or before the
date, if that rating is less than twelve months old (MarketBeat's own window).

The table is not a complete log - for a heavily covered stock it holds the most recent
fifty or so actions, so older ratings have often scrolled off. A later action still says
what the firm thought before it: an upgrade or downgrade names the old rating, and a target
change or reiteration leaves the rating where it was. Initiating coverage means the firm had
no rating before. That backward step is what makes the rebuilt counts usable months back.

Snapshots taken by the pipeline itself (rating_snapshots, one per stock per day) always win
over a rebuild, so the history gets more exact the longer the screener runs.
"""
import re
from datetime import date, timedelta

WINDOW_DAYS = 365

STRONG_BUY = {"strong-buy", "strong buy", "conviction-buy", "conviction buy", "top pick"}
BUY = {"buy", "outperform", "overweight", "market outperform", "moderate buy", "positive",
       "sector outperform", "speculative buy", "accumulate", "add", "outperformer",
       "moderate outperform", "long-term buy", "industry outperform"}
HOLD = {"hold", "neutral", "market perform", "equal weight", "equal-weight", "sector perform",
        "peer perform", "sector weight", "in-line", "inline", "cautious", "mixed", "perform",
        "fair value", "sector neutral", "market weight", "industry perform", "moderate hold",
        "sector performer", "average", "core holding"}
SELL = {"sell", "underperform", "strong sell", "strong-sell", "underweight", "moderate sell",
        "negative", "reduce", "sector underperform", "market underperform", "industry underperform"}
RATING_CHANGES = {"upgrade", "downgrade"}
INITIATIONS = {"initiated coverage", "initiated", "reinstated", "assumed", "resumed"}


def bucket(label: str | None) -> str | None:
    """MarketBeat's rating wording -> buy / hold / sell. Weiss grades like "Buy (B-)" keep
    their word and drop the grade."""
    if not label:
        return None
    word = re.sub(r"\s*\([a-e][+-]?\)", "", label.lower()).strip()
    if word in STRONG_BUY or word in BUY:
        return "buy"
    if word in HOLD:
        return "hold"
    if word in SELL:
        return "sell"
    return None


def rating_before(action: dict) -> tuple[str | None, bool]:
    """What a firm's rating was just before this action, and whether it had one at all."""
    kind = (action.get("action") or "").strip().lower()
    if kind in INITIATIONS:
        return None, False
    if kind in RATING_CHANGES:
        return action.get("rating_from"), bool(action.get("rating_from"))
    return action.get("rating_to"), bool(action.get("rating_to"))


def counts_on(actions: list[dict], asof: str, window_days: int = WINDOW_DAYS) -> dict:
    """buy / hold / sell as of `asof` (ISO date), rebuilt from rating actions.

    `actions` are rows with date, firm, action, rating_from, rating_to - any order.
    """
    by_firm: dict[str, list[dict]] = {}
    for a in actions:
        if a.get("date") and a.get("firm"):
            by_firm.setdefault(a["firm"], []).append(a)
    floor = (date.fromisoformat(asof) - timedelta(days=window_days)).isoformat()
    out = {"buy": 0, "hold": 0, "sell": 0, "inferred": 0}
    for firm_actions in by_firm.values():
        firm_actions.sort(key=lambda a: a["date"])
        before = [a for a in firm_actions if a["date"] <= asof and a.get("rating_to")]
        if before:
            last = before[-1]
            if last["date"] > floor:
                b = bucket(last["rating_to"])
                if b:
                    out[b] += 1
            continue
        after = [a for a in firm_actions if a["date"] > asof]
        if not after:
            continue
        label, had = rating_before(after[0])
        b = bucket(label) if had else None
        if b:
            out[b] += 1
            out["inferred"] += 1
    return out


def snapshot_dates(heads: list[str]) -> list[str]:
    """The as-of dates in the forecast page's "Ratings Over Time" header: each column is
    labelled with its twelve-month window, which ends on the snapshot date."""
    out = []
    for h in heads[1:]:
        m = re.search(r"\d{1,2}/\d{1,2}/\d{2,4}\s+to\s+(\d{1,2})/(\d{1,2})/(\d{2,4})", h)
        if not m:
            out.append(None)
            continue
        month, day, year = (int(g) for g in m.groups())
        out.append(date(year + 2000 if year < 100 else year, month, day).isoformat())
    return out


# ---------------------------------------------------------------- storage and lookup

def counts_of(rating_counts: dict | None) -> tuple[int, int, int] | None:
    """MarketBeat's strong-buy/buy/hold/sell row -> (buy, hold, sell)."""
    if not rating_counts:
        return None
    c = rating_counts
    return (c.get("strong_buy", 0) + c.get("buy", 0), c.get("hold", 0),
            c.get("sell", 0) + c.get("strong_sell", 0) + c.get("reduce", 0))


def record(con, ticker: str, forecast: dict, asof: str, price: float | None = None):
    """Store what one forecast page said: today's count, MarketBeat's own dated snapshots,
    and every rating action it lists."""
    from . import db
    today = counts_of(forecast.get("rating_counts"))
    if today:
        db.save_snapshot(con, ticker, asof, "daily", today, forecast.get("consensus_rating"),
                         forecast.get("consensus_target"), forecast.get("upside_pct"), price)
    for snap in forecast.get("official_snapshots") or []:
        counts = counts_of(snap.get("counts"))
        if counts and snap.get("asof"):
            db.save_snapshot(con, ticker, snap["asof"], "official", counts, snap.get("rating"),
                             snap.get("target"))
    db.save_actions(con, ticker, forecast.get("ratings_history") or [])


def load(con, tickers: list[str] | None = None) -> dict[str, dict]:
    """Rating actions and snapshots per stock, for rebuilding counts at any date."""
    where, args = "", []
    if tickers is not None:
        marks = ",".join("?" * len(tickers))
        where, args = f" WHERE ticker IN ({marks})", list(tickers)
    out: dict[str, dict] = {}
    for r in con.execute("SELECT * FROM rating_actions" + where, args):
        out.setdefault(r["ticker"], {"actions": [], "snapshots": {}})["actions"].append(dict(r))
    for r in con.execute("SELECT * FROM rating_snapshots" + where, args):
        snaps = out.setdefault(r["ticker"], {"actions": [], "snapshots": {}})["snapshots"]
        # A run's own reading and MarketBeat's official column can share a date; they are
        # the same number, and the run's is preferred.
        if r["asof"] not in snaps or r["source"] == "daily":
            snaps[r["asof"]] = (r["buy"], r["hold"], r["sell"])
    return out


EXACT_WITHIN_DAYS = 3


def counts_at(stock: dict, asof: str) -> tuple[int, int, int] | None:
    """The buy/hold/sell count on `asof`.

    A stored snapshot within a few days before the date is used as it stands. Otherwise the
    count is rebuilt from the rating actions and then corrected by how far the rebuild was
    off at the nearest snapshots on either side, blended by distance in time - so it lands
    exactly on every snapshot and drifts least between them.
    """
    snaps = stock.get("snapshots") or {}
    target = date.fromisoformat(asof)
    near = [d for d in snaps if 0 <= (target - date.fromisoformat(d)).days <= EXACT_WITHIN_DAYS]
    if near:
        return tuple(snaps[max(near)])
    actions = stock.get("actions") or []
    if not actions and not snaps:
        return None
    rebuilt = counts_on(actions, asof)
    base = [rebuilt["buy"], rebuilt["hold"], rebuilt["sell"]]
    errors = []
    for d, official in snaps.items():
        r = counts_on(actions, d)
        errors.append((d, [official[0] - r["buy"], official[1] - r["hold"], official[2] - r["sell"]]))
    errors.sort()
    before = [e for e in errors if e[0] <= asof]
    after = [e for e in errors if e[0] > asof]
    if before and after:
        (d0, e0), (d1, e1) = before[-1], after[0]
        t0, t1 = date.fromisoformat(d0).toordinal(), date.fromisoformat(d1).toordinal()
        w = (target.toordinal() - t0) / max(t1 - t0, 1)
        err = [e0[i] * (1 - w) + e1[i] * w for i in range(3)]
    elif before:
        err = before[-1][1]
    elif after:
        err = after[0][1]
    else:
        err = [0, 0, 0]
    out = tuple(max(0, int(round(base[i] + err[i]))) for i in range(3))
    return out if sum(out) else None
