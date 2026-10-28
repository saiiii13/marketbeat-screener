"""Go back a year and let the screener pick, month by month, with only what it knew then.

On the first trading day of each of the last twelve months this rebuilds the inputs the
screener would have had on that day - Nasdaq's analyst consensus as it stood that month,
and every price-based measure computed from closes up to that day and not one day after -
then runs the real scoring code (score.final_scores, the same function the live screener
calls) under several rule sets and holds each one's top picks for the following month.

The rule sets differ only in the backtest fixes, so each fix's effect can be seen alone:

  old            the original rules: 20+ buys / <=3 holds / 0 sells, no sector cap
  old, scaled    the same shape at 12 / 2 / 0, because Nasdaq counts fewer analysts than
                 MarketBeat and 20 buys on Nasdaq is a stricter cut
  + sector cap   old rules, at most 3 picks per sector
  + 95% buys     buy share instead of the shape, no cap
  new            95% buy share and the sector cap together

The volatility fix is tested separately: for every stock on every date, which volatility
measure would have put the next week's close inside its 68% and 95% ranges the right share
of the time.

What cannot be replayed, and is held neutral for every stock so it cannot tilt the ranking:
analyst upgrades, price targets, MarketBeat's features, beta, earnings dates and insider
trades. None of those have a history anyone hands out. The ranking each month therefore
runs on the consensus counts, momentum, trend and volatility - the part that can be
reconstructed honestly. Survivorship bias applies as in backtest.py: the stocks are drawn
from today's listings.
"""
import math
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

from . import backtest, db, notes, prices, score
from .enrich import metrics

TOP = 10
HOLD_DAYS = 21
WEEK_DAYS = 5
SAMPLE = 400

STRATEGIES = {
    "old":          {"rule": "shape", "cap": None, "scaled": False},
    "old, scaled":  {"rule": "shape", "cap": None, "scaled": True},
    "+ sector cap": {"rule": "shape", "cap": 3,    "scaled": True},
    "+ 95% buys":   {"rule": "share", "cap": None, "scaled": False},
    "new":          {"rule": "share", "cap": 3,    "scaled": False},
}
VOL_MODES = ["20d", "1y", "max", "blend"]


_SECTORS: dict[str, str] | None = None


def _sector(ticker: str) -> str | None:
    """The sector MarketBeat's overview page gave the stock, as stored by the last run."""
    global _SECTORS
    if _SECTORS is None:
        with db.connect() as con:
            _SECTORS = {t: d.get("sector") for t, d in db.load_json(con, "mb_detail").items()
                        if d.get("sector")}
    return _SECTORS.get(ticker)


def _load(ticker: str) -> dict | None:
    history = backtest.consensus_history(ticker)
    if len(history) < 3:
        return None
    closes = prices.history(ticker)
    if len(closes) < 120:
        return None
    frame = closes.to_frame("Close")
    frame.index = frame.index.normalize()
    return {"ticker": ticker, "history": history, "prices": frame, "sector": _sector(ticker)}


def load(n: int = SAMPLE) -> tuple[list[dict], pd.Series]:
    tickers = backtest.sample(n)
    print(f"[timetravel] loading {len(tickers)} stocks: consensus history, stored MarketBeat "
          f"prices, sector...")
    with ThreadPoolExecutor(max_workers=backtest.WORKERS) as pool:
        stocks = [s for s in pool.map(_load, tickers) if s]
    spy = prices.history("SPY", "NYSEARCA")
    spy.index = spy.index.normalize()
    print(f"[timetravel] {len(stocks)} stocks have enough history to replay")
    return stocks, spy


def rebalance_dates(stocks: list[dict], spy: pd.Series) -> list[pd.Timestamp]:
    """First session on or after each monthly consensus snapshot, with a month ahead of it."""
    stamps = sorted({pd.Timestamp(s["date"]) for st in stocks for s in st["history"]})
    out = []
    for stamp in stamps:
        at = spy.index.searchsorted(stamp)
        if at + HOLD_DAYS < len(spy):
            out.append(spy.index[at])
    return sorted(set(out))


def snapshot(stock: dict, day: pd.Timestamp) -> dict | None:
    """What the screener could have known about one stock on one day."""
    known = [h for h in stock["history"] if pd.Timestamp(h["date"]) <= day]
    if not known:
        return None
    consensus = known[-1]
    if (day - pd.Timestamp(consensus["date"])).days > 40:   # stale: no fresh snapshot
        return None
    prices = stock["prices"]
    at = prices.index.searchsorted(day)
    if at >= len(prices) or prices.index[at] != day or at + HOLD_DAYS >= len(prices):
        return None
    p = metrics(prices.iloc[: at + 1])          # nothing after `day` is visible
    if not p:
        return None
    close = prices["Close"]
    total = consensus["buy"] + consensus["hold"] + consensus["sell"]
    return {
        "p": p,
        "d": {"rating_counts": {"buy": consensus["buy"], "hold": consensus["hold"],
                                "sell": consensus["sell"]},
              "analyst_count": total, "sector": stock["sector"]},
        "b": {"sector": stock["sector"], "price": p["last_close"]},
        "week": float(close.iloc[at + WEEK_DAYS] / close.iloc[at] - 1),
        "month": float(close.iloc[at + HOLD_DAYS] / close.iloc[at] - 1),
    }


COVERAGE_BANDS = [(3, 5), (6, 10), (11, 20), (21, 10_000)]


def coverage_band(analysts: int) -> str:
    for lo, hi in COVERAGE_BANDS:
        if lo <= analysts <= hi:
            return f"{lo}-{hi}" if hi < 10_000 else f"{lo}+"
    return "under 3"


def _pick(snaps: dict[str, dict], strategy: dict) -> list[str]:
    bulk = {t: s["b"] for t, s in snaps.items()}
    prices = {t: s["p"] for t, s in snaps.items()}
    detail = {t: s["d"] for t, s in snaps.items()}
    saved = (score.MIN_BUY_RATINGS, score.MAX_HOLD_RATINGS)
    if strategy["scaled"]:
        score.MIN_BUY_RATINGS, score.MAX_HOLD_RATINGS = 12, 2
    try:
        res = score.final_scores(list(snaps), bulk, prices, detail, {},
                                 rule=strategy["rule"], sector_cap=strategy["cap"])
    finally:
        score.MIN_BUY_RATINGS, score.MAX_HOLD_RATINGS = saved
    if res.empty:
        return []
    return res[res["rank"] > 0].sort_values("rank")["ticker"].head(TOP).tolist()


def replay(stocks: list[dict], spy: pd.Series) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    by_ticker = {s["ticker"]: s for s in stocks}
    months, picks_log, bands = [], [], []
    for day in rebalance_dates(stocks, spy):
        snaps = {t: s for t, s in ((t, snapshot(st, day)) for t, st in by_ticker.items()) if s}
        if len(snaps) < 30:
            continue
        at = spy.index.searchsorted(day)
        row = {"date": day.date(), "stocks": len(snaps),
               "spy_week": float(spy.iloc[at + WEEK_DAYS] / spy.iloc[at] - 1),
               "spy_month": float(spy.iloc[at + HOLD_DAYS] / spy.iloc[at] - 1),
               "avg_week": float(np.mean([s["week"] for s in snaps.values()])),
               "avg_month": float(np.mean([s["month"] for s in snaps.values()]))}

        # The fair comparison for a pick is other stocks with as many analysts watching:
        # thinly covered stocks behave differently whatever their rating.
        band_of = {t: coverage_band(s["d"]["analyst_count"]) for t, s in snaps.items()}
        band_avg = pd.Series({t: s["month"] for t, s in snaps.items()}).groupby(band_of).mean()

        for name, strategy in STRATEGIES.items():
            chosen = _pick(snaps, strategy)
            row[f"{name}|n"] = len(chosen)
            if chosen:
                row[f"{name}|week"] = float(np.mean([snaps[t]["week"] for t in chosen]))
                row[f"{name}|month"] = float(np.mean([snaps[t]["month"] for t in chosen]))
                sectors = pd.Series([snaps[t]["d"]["sector"] or "?" for t in chosen])
                row[f"{name}|top_sector_share"] = float(sectors.value_counts().iloc[0] / len(chosen))
            for t in chosen:
                picks_log.append({"date": day.date(), "strategy": name, "ticker": t,
                                  "sector": snaps[t]["d"]["sector"],
                                  "analysts": snaps[t]["d"]["analyst_count"],
                                  "week": snaps[t]["week"], "month": snaps[t]["month"],
                                  "vs_same_coverage": snaps[t]["month"]
                                  - float(band_avg[band_of[t]])})

        # Range calibration, on every stock: would next week's close sit inside the band?
        for t, s in snaps.items():
            for mode in VOL_MODES:
                vol = score.outlook_vol(s["p"], mode)
                if not vol:
                    continue
                sigma = vol * math.sqrt(WEEK_DAYS / 252)
                move = math.log(1 + s["week"])
                bands.append({"mode": mode, "in68": abs(move) <= sigma,
                              "in95": abs(move) <= 2 * sigma})
        months.append(row)
        print(f"[timetravel] {day.date()}: {len(snaps)} stocks, "
              + ", ".join(f"{n} {row.get(f'{n}|n', 0)}" for n in STRATEGIES))
    return pd.DataFrame(months), pd.DataFrame(picks_log), pd.DataFrame(bands)


def _t(series: pd.Series) -> float:
    s = series.dropna()
    if len(s) < 3 or not s.std(ddof=1):
        return float("nan")
    return float(s.mean() / (s.std(ddof=1) / math.sqrt(len(s))))


def report(months: pd.DataFrame, bands: pd.DataFrame, picks: pd.DataFrame | None = None) -> dict:
    out: dict = {"months": len(months), "first": str(months["date"].iloc[0]),
                 "last": str(months["date"].iloc[-1]), "strategies": {}, "bands": {}}
    spy_growth = float((1 + months["spy_month"]).prod() - 1)
    avg_growth = float((1 + months["avg_month"]).prod() - 1)
    print(f"\n  Replayed {len(months)} months, {out['first']} to {out['last']}, top {TOP} "
          f"picks held one month each time.")
    print(f"  S&P 500 over the period: {spy_growth * 100:+.1f}%    average sampled stock: "
          f"{avg_growth * 100:+.1f}%\n")
    print(f"  {'rules':<14}{'picks/mo':>9}{'1 month':>9}{'vs S&P':>9}{'vs avg':>9}{'t':>6}"
          f"{'won':>7}{'1 week':>9}{'vs avg':>8}{'grew to':>10}{'1 sector':>10}")
    for name in STRATEGIES:
        col = f"{name}|month"
        if col not in months:
            continue
        held = months.dropna(subset=[col])
        m = held[col]
        vs_spy = m - held["spy_month"]
        vs_avg = m - held["avg_month"]
        wk_avg = held[f"{name}|week"] - held["avg_week"]
        growth = float((1 + months[col].fillna(0)).prod() - 1)   # cash in empty months
        row = {"months_held": int(len(held)), "picks_per_month": float(held[f"{name}|n"].mean()),
               "month_pct": float(m.mean() * 100), "vs_spy_pct": float(vs_spy.mean() * 100),
               "vs_avg_pct": float(vs_avg.mean() * 100), "t_vs_avg": _t(vs_avg),
               "months_beat_avg": int((vs_avg > 0).sum()),
               "week_pct": float(held[f"{name}|week"].mean() * 100),
               "week_vs_avg_pct": float(wk_avg.mean() * 100),
               "growth_pct": growth * 100,
               "top_sector_share": float(held[f"{name}|top_sector_share"].mean())}
        out["strategies"][name] = row
        t = row["t_vs_avg"]
        print(f"  {name:<14}{row['picks_per_month']:>9.1f}{row['month_pct']:>+8.2f}%"
              f"{row['vs_spy_pct']:>+8.2f}%{row['vs_avg_pct']:>+8.2f}%"
              f"{(f'{t:>6.1f}' if t == t else '     -')}"
              f"{row['months_beat_avg']:>4}/{row['months_held']:<2}"
              f"{row['week_pct']:>+8.2f}%{row['week_vs_avg_pct']:>+7.2f}%"
              f"{row['growth_pct']:>+9.1f}%{row['top_sector_share']:>9.0%}")
    print("\n  'won' is months the picks beat the average stock. '1 sector' is the average share "
          "of the\n  picks that came from the single most-picked sector.")

    if picks is not None and not picks.empty:
        print("\n  Robustness - is the result a few lucky picks, or a coverage tilt?")
        print(f"  {'rules':<14}{'median pick':>12}{'w/o best 5':>12}{'vs same coverage':>18}{'t':>6}"
              f"{'analysts':>10}")
        for name in STRATEGIES:
            g = picks[picks["strategy"] == name]
            if g.empty:
                continue
            per_month = g.groupby("date")["vs_same_coverage"].mean()
            row = out["strategies"].setdefault(name, {})
            row["median_pick_pct"] = float(g["month"].median() * 100)
            row["without_best5_pct"] = float(g.drop(g.nlargest(5, "month").index)["month"].mean() * 100)
            row["vs_same_coverage_pct"] = float(per_month.mean() * 100)
            row["t_vs_same_coverage"] = _t(per_month)
            row["median_analysts"] = float(g["analysts"].median())
            t = row["t_vs_same_coverage"]
            print(f"  {name:<14}{row['median_pick_pct']:>+11.2f}%{row['without_best5_pct']:>+11.2f}%"
                  f"{row['vs_same_coverage_pct']:>+17.2f}%{(f'{t:>6.1f}' if t == t else '     -')}"
                  f"{row['median_analysts']:>10.0f}")
        print("  'vs same coverage' compares each pick with stocks followed by as many analysts that"
              "\n  month - the test that separates a rating signal from a small-cap tilt.")

    print(f"\n  Weekly price ranges - share of real outcomes inside them (target 68% / 95%):")
    for mode in VOL_MODES:
        g = bands[bands["mode"] == mode]
        if g.empty:
            continue
        r68, r95 = float(g["in68"].mean() * 100), float(g["in95"].mean() * 100)
        out["bands"][mode] = {"in68": r68, "in95": r95, "n": int(len(g))}
        err = abs(r68 - 68.27) + abs(r95 - 95.45)
        print(f"    {mode:<6}  68% band held {r68:5.1f}%   95% band held {r95:5.1f}%   "
              f"(off by {err:4.1f} points, n={len(g):,})")
    best = min(out["bands"], key=lambda m: abs(out["bands"][m]["in68"] - 68.27)
               + abs(out["bands"][m]["in95"] - 95.45))
    out["best_vol_mode"] = best
    print(f"    best calibrated: {best}")
    return out


def run(n: int = SAMPLE) -> dict:
    notes.stage("Time travel — replaying the screener over the past year")
    stocks, spy = load(n)
    if not stocks:
        print("[timetravel] nothing to replay")
        return {}
    months, picks, bands = replay(stocks, spy)
    if months.empty:
        print("[timetravel] no month had enough stocks to replay")
        return {}
    result = report(months, bands, picks)
    result["built"] = db.today()
    with db.connect() as con:
        db.set_meta(con, "timetravel", result)
    months.to_csv(db.DATA_DIR / "timetravel_months.csv", index=False)
    picks.to_csv(db.DATA_DIR / "timetravel_picks.csv", index=False)
    print(f"\n  month-by-month results and every pick saved to {db.DATA_DIR}")
    return result
