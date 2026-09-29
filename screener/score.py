"""Pre-filter, hard exclusions and the weighted 1-month growth score.

Rule thresholds (see README for sources): analyst-upgrade drift is front-loaded in the first
few weeks; RSI 50-70 is the momentum sweet spot; short interest 10-20% of float is the squeeze
zone, >30% is crowded/risky.
"""
import math
import re
from datetime import date, datetime

import numpy as np
import pandas as pd

from .config import (
    BULLISH_RATINGS, MIN_ANALYSTS, MAX_VOL_ANNUAL, MIN_DOLLAR_VOL, MAX_DRAWDOWN_FROM_52W, WEIGHTS,
    MIN_BUY_RATINGS, MAX_HOLD_RATINGS, MAX_SELL_RATINGS, FOCUS_MIN_PRICE, FOCUS_PRICE_WEIGHT,
)

BULLISH_TO = ("buy", "outperform", "overweight", "strong-buy", "strong buy", "positive", "accumulate", "market outperform")
UPGRADE_ACTIONS = ("upgrade", "upgraded", "initiated", "initiat", "reiterat", "boost", "raise", "set target", "lower")


def clip01(x) -> float | None:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return None
    return float(min(1.0, max(0.0, x)))


def days_ago(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return (date.today() - datetime.fromisoformat(iso).date()).days
    except ValueError:
        return None


def mean_available(*vals) -> float | None:
    xs = [v for v in vals if v is not None and not (isinstance(v, float) and np.isnan(v))]
    return float(np.mean(xs)) if xs else None


# ---------------------------------------------------------------- Stage B

def prefilter(bulk: dict[str, dict], upgraded: set[str]) -> list[str]:
    have_ratings = sum(1 for r in bulk.values() if r.get("consensus_rating")) > 0
    keep = []
    for t, r in bulk.items():
        rating = (r.get("consensus_rating") or "").lower()
        if have_ratings:
            ok = rating in BULLISH_RATINGS
        else:  # logged-out fallback: paywalled column is blank, use the target upside instead
            ok = (r.get("upside_pct") or 0) > 5 and r.get("consensus_target") is not None
        if ok or t in upgraded:
            keep.append(t)
    if not have_ratings:
        print("[score] consensus ratings were blank (not logged in?) - pre-filtered on price-target upside instead")
    return keep


# ---------------------------------------------------------------- Stage C preliminary ranking

def rating_counts_of(row: dict) -> tuple[int, int, int] | None:
    counts = row.get("rating_counts") or {}
    if not counts:
        return None
    buy = counts.get("strong_buy", 0) + counts.get("buy", 0)
    hold = counts.get("hold", 0)
    sell = counts.get("sell", 0) + counts.get("strong_sell", 0) + counts.get("reduce", 0)
    return buy, hold, sell


def consensus_shape_exclusions(row: dict) -> list[str]:
    """The brief: a big stack of buys, almost no holds, no sells."""
    counts = rating_counts_of(row)
    if counts is None:
        return ["no analyst rating breakdown"]
    buy, hold, sell = counts
    out = []
    if buy < MIN_BUY_RATINGS:
        out.append(f"only {buy} buy ratings (need {MIN_BUY_RATINGS}+)")
    if hold > MAX_HOLD_RATINGS:
        out.append(f"{hold} hold ratings (max {MAX_HOLD_RATINGS})")
    if sell > MAX_SELL_RATINGS:
        out.append(f"{sell} sell ratings (max {MAX_SELL_RATINGS})")
    return out


def hard_exclusions(row: dict) -> list[str]:
    reasons = []
    v = row.get("vol_20d_annualized")
    if v is not None and v > MAX_VOL_ANNUAL:
        reasons.append(f"volatility {v:.0%} > {MAX_VOL_ANNUAL:.0%}")
    dv = row.get("avg_dollar_vol_20d")
    if dv is not None and dv < MIN_DOLLAR_VOL:
        reasons.append("thin volume")
    dd = row.get("pct_from_52w_high")
    if dd is not None and dd < MAX_DRAWDOWN_FROM_52W:
        reasons.append(f"{dd:.0%} from 52w high")
    ac = row.get("analyst_count")
    if ac is not None and ac < MIN_ANALYSTS:
        reasons.append(f"only {int(ac)} analysts")
    reasons += consensus_shape_exclusions(row) if row.get("rating_counts") else []
    return reasons


def momentum_score(p: dict, ret_1m_pct: float | None) -> float | None:
    if not p:
        return None
    rsi = p.get("rsi14")
    rsi_s = None if rsi is None else (1.0 if 50 <= rsi <= 70 else 0.5 if (40 <= rsi < 50 or 70 < rsi <= 75) else 0.0)
    parts = [
        (0.40, ret_1m_pct),
        (0.15, None if p.get("above_ma20") is None else float(p["above_ma20"])),
        (0.15, None if p.get("ma20_gt_ma50") is None else float(p["ma20_gt_ma50"])),
        (0.20, rsi_s),
        (0.10, None if p.get("breakout_20d") is None else float(p["breakout_20d"])),
    ]
    used = [(w, v) for w, v in parts if v is not None]
    if not used:
        return None
    return float(sum(w * v for w, v in used) / sum(w for w, _ in used))


def preliminary_rank(bulk: dict[str, dict], prices: dict[str, dict], top_n: int) -> list[str]:
    rows = []
    for t, p in prices.items():
        b = bulk.get(t, {})
        merged = {**b, **p}
        if hard_exclusions(merged):
            continue
        rows.append({"ticker": t, "ret_1m": p.get("ret_1m"), "upside": b.get("upside_pct"),
                     "cscore": b.get("consensus_score"), "p": p})
    if not rows:
        return []
    df = pd.DataFrame(rows)
    df["ret_pct"] = df["ret_1m"].rank(pct=True)
    df["mom"] = [momentum_score(p, r) for p, r in zip(df["p"], df["ret_pct"])]
    df["ups"] = df["upside"].apply(lambda x: clip01((x or 0) / 40))
    df["cs"] = df["cscore"].apply(lambda x: clip01((x - 1) / 3) if x else None)
    # Prefer the price band the user cares about, and prefer widely-covered names, since the
    # 20-buy/0-sell requirement downstream can only be met by heavily-followed stocks.
    df["price"] = [(p or {}).get("last_close") for p in df["p"]]
    df["focus"] = [1.0 if (pr or 0) >= FOCUS_MIN_PRICE else 0.0 for pr in df["price"]]
    df["cap"] = [clip01(np.log10(max((bulk.get(t, {}) or {}).get("market_cap") or 1, 1) / 1e8) / 3)
                 for t in df["ticker"]]
    df["prelim"] = (0.40 * df["mom"].fillna(0)
                    + 0.20 * df["ups"].fillna(0)
                    + 0.15 * df["cs"].fillna(0.5)
                    + 0.25 * df["cap"].fillna(0)
                    + FOCUS_PRICE_WEIGHT * df["focus"])
    return df.sort_values("prelim", ascending=False)["ticker"].head(top_n).tolist()


# ---------------------------------------------------------------- Stage D final score

def recent_upgrade(detail: dict, upgrades_rows: list[dict]) -> tuple[float, str | None]:
    best, note = 0.0, None
    events = []
    for h in detail.get("ratings_history") or []:
        d = days_ago(h.get("date"))
        if d is None or d > 30:
            continue
        action = (h.get("action") or "").lower()
        to = (h.get("rating_to") or "").lower()
        tf, tt = h.get("target_from"), h.get("target_to")
        bullish_to = any(b in to for b in BULLISH_TO) or bool(re.match(r"(buy|strong buy|outperform)\s*\((a|b)", to))
        if "upgrade" in action and bullish_to:
            events.append((1.0, d, f"{h.get('firm')} upgrade to {h.get('rating_to')}"))
        elif "upgrade" in action and "sell" not in to and "hold" not in to and "neutral" not in to and "(d" not in to and "(c" not in to:
            events.append((0.5, d, f"{h.get('firm')} upgrade to {h.get('rating_to')}"))
        elif "initiat" in action and bullish_to:
            events.append((0.8, d, f"{h.get('firm')} initiated at {h.get('rating_to')}"))
        elif tf and tt and tt > tf:
            events.append((0.6, d, f"{h.get('firm')} raised target ${tf:g}→${tt:g}"))
        elif "reiterat" in action and any(b in to for b in BULLISH_TO):
            events.append((0.3, d, f"{h.get('firm')} reiterated {h.get('rating_to')}"))
    for u in upgrades_rows:
        d = days_ago(u.get("date"))
        if d is not None and d <= 30 and "upgrade" in (u.get("action") or "").lower():
            events.append((1.0, d, f"{u.get('firm')} upgraded ({u.get('detail')})"))
    for strength, d, text in events:
        s = strength * (1.0 if d <= 14 else 0.5)
        if s > best:
            best, note = s, f"{text} ({d}d ago)"
    return best, note


def buy_ratio(detail: dict, bulk: dict) -> float | None:
    counts = detail.get("rating_counts") or {}
    total = sum(counts.values())
    if total:
        return (counts.get("strong_buy", 0) + counts.get("buy", 0)) / total
    cs = detail.get("consensus_score") or bulk.get("consensus_score")
    return clip01((cs - 1) / 3) if cs else None


def rating_drift(detail: dict) -> float | None:
    now, ago = detail.get("rating_counts") or {}, detail.get("rating_counts_1m_ago") or {}
    if not now or not ago:
        return None
    b_now = now.get("strong_buy", 0) + now.get("buy", 0)
    b_ago = ago.get("strong_buy", 0) + ago.get("buy", 0)
    return 1.0 if b_now > b_ago else 0.5 if b_now == b_ago else 0.0


def mb_feature_score(d: dict, p: dict) -> tuple[float | None, dict]:
    parts: dict = {}
    mr = d.get("marketrank_percentile")
    parts["marketrank"] = None if mr is None else clip01(mr / 100)

    ns = d.get("news_sentiment")
    buzz = None
    if d.get("news_articles_week") and d.get("news_articles_avg_week"):
        buzz = clip01(np.log1p(d["news_articles_week"] / max(d["news_articles_avg_week"], 1)) / np.log1p(5))
    parts["news"] = mean_available(None if ns is None else clip01((ns + 2) / 4), buzz)

    bought = d.get("insider_buy_value_90d") if d.get("insider_buy_value_90d") is not None else d.get("insider_bought_3m")
    sold = d.get("insider_sell_value_90d") if d.get("insider_sell_value_90d") is not None else d.get("insider_sold_3m")
    if bought is not None and sold is not None:
        parts["insider"] = 0.5 if (bought + sold) == 0 else bought / (bought + sold)
    else:
        parts["insider"] = None

    inc, dec = d.get("inst_increases"), d.get("inst_decreases")
    inst_flow = None if inc is None or dec is None or (inc + dec) == 0 else inc / (inc + dec)
    own = d.get("inst_own_pct")
    parts["institutional"] = mean_available(inst_flow, None if own is None else clip01(own / 80))

    beat = d.get("beat_rate_4q")
    growth = d.get("proj_earnings_growth_pct")
    parts["earnings"] = mean_available(beat, None if growth is None else clip01(growth / 50))

    sp = d.get("short_pct_float")
    if sp is None:
        parts["short_squeeze"] = None
    else:
        base = 1.0 if 10 <= sp <= 20 else 0.8 if 5 <= sp < 10 else 0.6 if 20 < sp <= 30 else 0.4 if sp < 5 else 0.2
        if (p.get("ret_1m") or 0) > 0 and 10 <= sp <= 30:
            base = min(1.0, base + 0.2)
        parts["short_squeeze"] = base

    peg = d.get("peg")
    parts["valuation"] = None if peg is None else (1.0 if peg <= 2 else 0.5 if peg <= 3 else 0.25)
    parts["rating_drift"] = rating_drift(d)
    return mean_available(*parts.values()), parts


def risk_score(d: dict, p: dict, b: dict) -> tuple[float, list[str]]:
    pen, flags = 0.0, []
    v = p.get("vol_20d_annualized")
    if v is not None:
        if v > 0.45:
            pen += 0.20
        elif v > 0.35:
            pen += 0.10
        elif v > 0.25:
            pen += 0.05
        if v > 0.35:
            flags.append("Volatile")
    beta = d.get("beta") if d.get("beta") is not None else b.get("beta")
    if beta is not None and beta > 1.3:
        pen += 0.15
    ne = days_ago(d.get("next_earnings_date")) if d.get("next_earnings_date") else None
    if ne is not None and -30 <= ne <= 0:
        pen += 0.05
        flags.append("Earnings in window")
    rsi = p.get("rsi14")
    if rsi is not None and rsi > 75:
        flags.append("Overbought")
    if (p.get("avg_dollar_vol_20d") or 0) < 5 * MIN_DOLLAR_VOL:
        flags.append("Thin volume")
    sp = d.get("short_pct_float")
    if sp is not None and sp > 30:
        pen += 0.10
        flags.append("Heavy short interest")
    bought = d.get("insider_buy_value_90d") or d.get("insider_bought_3m") or 0
    sold = d.get("insider_sell_value_90d") or d.get("insider_sold_3m") or 0
    if sold > 0 and sold > 3 * bought:
        flags.append("Insider selling")
    dd = p.get("pct_from_52w_high")
    if dd is not None and dd < -0.15:
        pen += 0.05
    return max(0.0, 1.0 - pen), flags


def buy_reasons(d: dict, p: dict, b: dict, upgrade_note: str | None) -> list[str]:
    """Plain-language case for the stock, built only from what was actually pulled."""
    out: list[str] = []
    counts = rating_counts_of(d)
    if counts:
        buy, hold, sell = counts
        total = buy + hold + sell
        out.append(f"{buy} of {total} analysts rate it Buy or better"
                   + (", none say Sell" if sell == 0 else f", {sell} say Sell")
                   + (f", {hold} on the fence" if hold else ""))
    if upgrade_note:
        out.append(upgrade_note[0].upper() + upgrade_note[1:])
    up = d.get("upside_pct") if d.get("upside_pct") is not None else b.get("upside_pct")
    tgt = d.get("consensus_target") or b.get("consensus_target")
    if up and tgt and 0 < up < 500:
        out.append(f"Trading {up:.0f}% below the ${tgt:,.2f} consensus target"
                   + (f" (range ${d['target_low']:,.0f}–${d['target_high']:,.0f})"
                      if d.get("target_low") and d.get("target_high") else ""))
    r1m = p.get("ret_1m")
    if r1m is not None and r1m > 0:
        trend = []
        if p.get("above_ma20"):
            trend.append("above its 20-day average")
        if p.get("ma20_gt_ma50"):
            trend.append("20-day above 50-day")
        out.append(f"Up {r1m * 100:.1f}% over the past month"
                   + (", " + " and ".join(trend) if trend else ""))
    if p.get("breakout_20d"):
        out.append("Just broke above its 20-day high")
    rsi = p.get("rsi14")
    if rsi is not None and 50 <= rsi <= 70:
        out.append(f"RSI {rsi:.0f} — momentum without being overbought")
    mr = d.get("marketrank_percentile")
    if mr is not None and mr >= 60:
        out.append(f"MarketRank in the {mr:.0f}th percentile of all stocks MarketBeat covers")
    ns, avg = d.get("news_sentiment"), d.get("news_sentiment_sector_avg")
    if ns is not None and ns > 0:
        out.append(f"News sentiment {ns:+.2f}" + (f" vs {avg:+.2f} for its sector" if avg is not None else ""))
    beat = d.get("beat_rate_4q")
    if beat is not None and beat >= 0.75:
        out.append(f"Beat EPS estimates in {beat * 100:.0f}% of the last four quarters")
    growth = d.get("proj_earnings_growth_pct")
    if growth is not None and growth > 5:
        out.append(f"Earnings projected to grow {growth:.1f}% next year")
    bought = d.get("insider_buy_value_90d") or d.get("insider_bought_3m") or 0
    sold = d.get("insider_sell_value_90d") or d.get("insider_sold_3m") or 0
    if bought > sold and bought > 0:
        out.append(f"Insiders bought ${bought:,.0f} and sold ${sold:,.0f} in the last 90 days")
    inc, dec = d.get("inst_increases"), d.get("inst_decreases")
    if inc and dec is not None and inc > dec:
        out.append(f"Institutions added in {inc} recent filings vs {dec} trims")
    own = d.get("inst_own_pct")
    if own is not None and own >= 70:
        out.append(f"{own:.0f}% institutionally held")
    sp = d.get("short_pct_float")
    if sp is not None and 10 <= sp <= 30:
        out.append(f"{sp:.1f}% of float sold short — squeeze potential if it runs")

    # How the business itself is doing, not just what the market thinks of it.
    margin = d.get("profit_margin")
    if margin is not None and margin > 10:
        out.append(f"Keeps {margin:.0f} cents of every revenue dollar as profit")
    rev = d.get("revenue_growth_pct")
    if rev is not None and rev > 5:
        out.append(f"Revenue growing {rev:.0f}% year on year")
    eps_growth = d.get("earnings_growth_pct")
    if eps_growth is not None and eps_growth > 10:
        out.append(f"Earnings growing {eps_growth:.0f}% year on year")
    fcf = d.get("free_cash_flow")
    if fcf is not None and fcf > 0:
        out.append(f"Generates {_money(fcf, d.get('financial_currency'))} of free cash flow")
    peg = d.get("peg")
    if peg is not None and 0 < peg <= 1.5:
        out.append(f"PEG {peg:.2f} — growth is not fully priced in")
    d_e = d.get("debt_to_equity")
    if d_e is not None and d_e < 50:
        out.append(f"Low debt: {d_e:.0f}% of equity")
    yield_pct = d.get("dividend_yield")
    if yield_pct is not None and yield_pct >= 2:
        out.append(f"Pays a {yield_pct:.1f}% dividend while you wait")
    return out


def _money(value: float, currency: str | None = None) -> str:
    """Format a cash figure, naming the currency unless it is US dollars."""
    symbol = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}.get((currency or "USD").upper())
    prefix = symbol or ((currency or "").upper() + " ")
    for unit, size in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(value) >= size:
            return f"{prefix}{value / size:.1f}{unit}"
    return f"{prefix}{value:,.0f}"


def price_outlook(d: dict, p: dict, b: dict) -> dict:
    """Where the price could be tomorrow, next week and next month - with the evidence.

    There is no point forecast here, because a single future price would be invented. What
    there is:

      * a range for each horizon, from this stock's own realised volatility, scaled by the
        square root of time. Zero drift is assumed, since direction is the unknowable part.
      * the analyst consensus target, which is a real forecast by named people - but on a
        roughly 12-month horizon, not a one-day one.
      * the evidence behind each horizon: what is scheduled, where the trend sits, how the
        volatility compares to the move being hoped for.
    """
    price = p.get("last_close") or d.get("price") or b.get("price")
    vol_annual = p.get("vol_20d_annualized")
    if not price:
        return {}

    out: dict = {"price": price, "horizons": []}
    target = d.get("consensus_target") or b.get("consensus_target")
    if target:
        out["analyst_target"] = target
        out["analyst_upside_pct"] = (target - price) / price * 100
        out["analyst_low"] = d.get("target_low")
        out["analyst_high"] = d.get("target_high")
        out["analyst_count"] = d.get("analyst_count")

    # What historically happened to stocks in this same shape. Measured, not modelled.
    try:
        from .calibrate import odds_for
        odds = odds_for(p)
        if odds:
            out["setup_odds"] = odds
    except Exception:
        pass

    if not vol_annual:
        return out

    earnings_in = days_ago(d.get("next_earnings_date"))
    days_to_earnings = -earnings_in if earnings_in is not None and earnings_in < 0 else None
    rsi = p.get("rsi14")
    ret_1m, ret_5d = p.get("ret_1m"), p.get("ret_5d")

    for label, days in (("tomorrow", 1), ("next week", 5), ("next month", 21)):
        sigma = vol_annual * np.sqrt(days / 252)
        row = {
            "label": label,
            "days": days,
            "typical_move_pct": sigma * 100,
            "low_68": price * np.exp(-sigma),
            "high_68": price * np.exp(sigma),
            "low_95": price * np.exp(-2 * sigma),
            "high_95": price * np.exp(2 * sigma),
            "evidence": [],
        }
        ev = row["evidence"]
        ev.append(f"20-day realised volatility is {vol_annual * 100:.0f}% a year, which over "
                  f"{days} trading day{'s' if days > 1 else ''} is a typical move of "
                  f"±{sigma * 100:.1f}%")
        if days_to_earnings is not None and days_to_earnings <= days:
            ev.append(f"earnings land in {days_to_earnings} days, inside this window — the "
                      f"range above does not price in an earnings gap")
        if ret_5d is not None and days <= 5:
            ev.append(f"it moved {ret_5d * 100:+.1f}% over the last five sessions")
        if ret_1m is not None and days > 5:
            ev.append(f"it moved {ret_1m * 100:+.1f}% over the last month")
        if p.get("above_ma20") and p.get("ma20_gt_ma50"):
            ev.append("trend is up: price above the 20-day, 20-day above the 50-day")
        elif p.get("above_ma20") is False:
            ev.append("price is below its 20-day average, so the trend is not supporting it")
        if rsi is not None and rsi > 70:
            ev.append(f"RSI {rsi:.0f} is stretched, which often precedes a pause")
        cell = (out.get("setup_odds") or {}).get("horizons", {}).get(label)
        if cell:
            row["historical_win_rate"] = cell["win_rate"]
            row["historical_mean_pct"] = cell["mean_pct"]
            edge = cell.get("edge_pts")
            sample = out["setup_odds"]["n"]
            ev.append(f"stocks in this same setup finished higher {cell['win_rate']:.0f}% of "
                      f"the time over this horizon (average {cell['mean_pct']:+.1f}%), against "
                      f"{cell['baseline_win_rate']:.0f}% for any stock — "
                      f"{'+' if (edge or 0) >= 0 else ''}{edge:.0f} points, from {sample:,} "
                      f"past cases")
        if target and target > price:
            needed = np.log(target / price) / sigma
            odds = float(100 * (1 - _normal_cdf(needed)))
            row["odds_reach_target_pct"] = odds
            ev.append(f"reaching the ${target:,.2f} analyst target would take a "
                      f"{(target / price - 1) * 100:.0f}% move — about {needed:.1f} standard "
                      f"deviations, so roughly a {odds:.0f}% chance in this window")
        out["horizons"].append(row)
    return out


def _normal_cdf(z: float) -> float:
    """Standard normal CDF via the error function - no scipy dependency."""
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def risk_notes(d: dict, p: dict, flags: list[str]) -> list[str]:
    """The other side of the trade, so the case is not one-sided."""
    out = []
    v = p.get("vol_20d_annualized")
    if v is not None and v > 0.35:
        out.append(f"Volatile: {v * 100:.0f}% annualized swings")
    ne = d.get("next_earnings_date")
    if ne and "Earnings in window" in flags:
        out.append(f"Earnings due {ne} — a binary event inside your holding period")
    rsi = p.get("rsi14")
    if rsi is not None and rsi > 75:
        out.append(f"RSI {rsi:.0f} is overbought; may pull back first")
    if "Thin volume" in flags:
        dv = p.get("avg_dollar_vol_20d")
        out.append(f"Thin trading (${dv:,.0f}/day) — harder to exit quickly" if dv else "Thin trading")
    bought = d.get("insider_buy_value_90d") or d.get("insider_bought_3m") or 0
    sold = d.get("insider_sell_value_90d") or d.get("insider_sold_3m") or 0
    if sold > 0 and sold > 3 * bought:
        out.append(f"Insiders sold ${sold:,.0f} against ${bought:,.0f} bought in 90 days")
    sp = d.get("short_pct_float")
    if sp is not None and sp > 30:
        out.append(f"Heavily shorted: {sp:.1f}% of float")
    dd = p.get("pct_from_52w_high")
    if dd is not None and dd < -0.15:
        out.append(f"{dd * 100:.0f}% below its 52-week high")

    margin = d.get("profit_margin")
    if margin is not None and margin < 0:
        out.append(f"Loss-making: {margin:.0f}% net margin")
    rev = d.get("revenue_growth_pct")
    if rev is not None and rev < 0:
        out.append(f"Revenue shrinking {abs(rev):.0f}% year on year")
    d_e = d.get("debt_to_equity")
    if d_e is not None and d_e > 200:
        out.append(f"Heavily indebted: debt is {d_e:.0f}% of equity")
    peg = d.get("peg")
    if peg is not None and peg > 3:
        out.append(f"PEG {peg:.1f} — paying a lot for the growth")
    beta = d.get("beta")
    if beta is not None and beta > 1.5:
        out.append(f"Beta {beta:.1f} — amplifies market moves in both directions")
    return out


def final_scores(finalists: list[str], bulk: dict, prices: dict, detail: dict, upgrades: dict[str, list[dict]]) -> pd.DataFrame:
    rows = []
    for t in finalists:
        b, p, d = bulk.get(t, {}), prices.get(t, {}), detail.get(t, {})
        merged = {**b, **p, **d}
        excl = hard_exclusions(merged)
        rows.append({"ticker": t, "b": b, "p": p, "d": d, "excluded": "; ".join(excl), "ret_1m": p.get("ret_1m")})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["ret_pct"] = df["ret_1m"].rank(pct=True)

    out = []
    for _, r in df.iterrows():
        b, p, d = r["b"], r["p"], r["d"]
        up_s, up_note = recent_upgrade(d, upgrades.get(r["ticker"], []))
        comp = {
            "recent_upgrade": up_s,
            "buy_ratio": buy_ratio(d, b),
            "momentum": momentum_score(p, r["ret_pct"]),
            "target_upside": clip01(((d.get("upside_pct") if d.get("upside_pct") is not None else b.get("upside_pct")) or 0) / 40),
        }
        comp["mb_features"], mb_parts = mb_feature_score(d, p)
        comp["risk"], flags = risk_score(d, p, b)
        score = sum(WEIGHTS[k] * (comp[k] if comp[k] is not None else 0.5 * (k != "recent_upgrade")) for k in WEIGHTS)
        contrib = {k: WEIGHTS[k] * (comp[k] or 0) for k in WEIGHTS if k != "risk"}
        why = [k for k, _ in sorted(contrib.items(), key=lambda kv: -kv[1])[:3]]
        out.append({
            "ticker": r["ticker"],
            "buy_reasons": buy_reasons(d, p, b, up_note),
            "outlook": price_outlook(d, p, b),
            "risk_notes": risk_notes(d, p, flags),
            "company": b.get("company") or "",
            "exchange": b.get("exchange"),
            "sector": b.get("sector"),
            "price": p.get("last_close") or d.get("price") or b.get("price"),
            "score": round(100 * score, 1),
            "excluded": r["excluded"],
            "flags": flags,
            "why": why,
            "upgrade_note": up_note,
            "components": comp,
            "mb_parts": mb_parts,
            "consensus_rating": d.get("consensus_rating") or b.get("consensus_rating"),
            "rating_counts": d.get("rating_counts") or {},
            "analyst_count": d.get("analyst_count"),
            "consensus_target": d.get("consensus_target") or b.get("consensus_target"),
            "upside_pct": d.get("upside_pct") if d.get("upside_pct") is not None else b.get("upside_pct"),
            "ret_1m": p.get("ret_1m"),
            "ret_5d": p.get("ret_5d"),
            "rsi14": p.get("rsi14"),
            "vol_20d_annualized": p.get("vol_20d_annualized"),
            "avg_dollar_vol_20d": p.get("avg_dollar_vol_20d"),
            "pct_from_52w_high": p.get("pct_from_52w_high"),
            "closes_30d": p.get("closes_30d") or [],
            "detail": d,
            "bulk": b,
        })
    res = pd.DataFrame(out)
    res = res.sort_values(["excluded", "score"], ascending=[True, False]).reset_index(drop=True)
    res["rank"] = np.where(res["excluded"] == "", np.arange(1, len(res) + 1), 0)
    return res
