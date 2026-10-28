"""Does the analyst consensus actually predict the next month?

The screener's whole thesis is that a stock with a big stack of buy ratings and almost no
sells is more likely to rise over the following month. The verification stage leans on the
same idea: MarketBeat's job is to say "will it go up?" from its buy/hold/sell ratio. This
module tests that claim against what really happened.

Nasdaq's analyst endpoint returns, for each stock, the consensus as it stood at the start
of each of the last twelve months - buys, holds, sells - which is the one piece of history
a backtest of this needs, and MarketBeat's free pages only show four dated snapshots of
it. Each monthly snapshot is paired with what the stock did over the next month, from
MarketBeat's closing prices stored in the database (see prices.py).

Three things keep the result honest:

  - Returns are also measured as EXCESS over the average of every sampled stock in the same
    month. In a rising market nearly everything wins, and a raw win rate would credit the
    consensus with the market's work.
  - Significance is judged across months, not across stock-months. Stocks move together;
    two thousand observations from eleven months is closer to eleven independent draws
    than to two thousand.
  - The sample is random among liquid listings, not alphabetical and not today's winners.

What it cannot fix: the sample is drawn from stocks listed today, so anything delisted
during the year is missing (survivorship bias, which flatters every strategy), and twelve
months is one market regime, not a law of nature.
"""
import math
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import db, nasdaq, notes, prices

SAMPLE = 400
SEED = 7
MIN_PRICE = 5.0
MIN_MARKET_CAP = 2e9
HORIZONS = {"1 week": 5, "1 month": 21}
WORKERS = 8

# Buy-share buckets, chosen to straddle the verification stage's own lines (60% / 80%).
SHARE_BUCKETS = [(0.0, 0.6, "under 60% buy"), (0.6, 0.8, "60-80% buy"),
                 (0.8, 0.95, "80-95% buy"), (0.95, 1.01, "95%+ buy")]


def sample(n: int = SAMPLE, seed: int = SEED) -> list[str]:
    """A seeded random draw of liquid US listings from the stored universe.

    The seed fixes the draw for a given universe, not forever: a fresh pipeline run
    refreshes the universe, so the same seed then draws different stocks. That is useful -
    it is how the time-travel result in homework.txt 7.4b was replicated on new stocks -
    but it means two runs either side of a refresh are two samples, not one.
    """
    with db.connect() as con:
        bulk = db.load_json(con, "mb_bulk")
    liquid = sorted(t for t, d in bulk.items()
                    if (d.get("price") or 0) >= MIN_PRICE
                    and (d.get("market_cap") or 0) >= MIN_MARKET_CAP
                    and "." not in t and "-" not in t)
    random.Random(seed).shuffle(liquid)
    return liquid[:n]


def consensus_history(ticker: str) -> list[dict]:
    """Monthly consensus snapshots for one stock, oldest first."""
    client = nasdaq.Nasdaq()
    data = client.get(f"analyst/{ticker.upper()}/targetprice") or {}
    out = []
    for point in data.get("historicalConsensus") or []:
        z = point.get("z") or {}
        stamp = point.get("x")
        if stamp is None:
            continue
        buy, hold, sell = (int(z.get(k) or 0) for k in ("buy", "hold", "sell"))
        if buy + hold + sell == 0:
            continue
        out.append({"date": datetime.fromtimestamp(stamp, tz=timezone.utc).date(),
                    "buy": buy, "hold": hold, "sell": sell,
                    "consensus": z.get("consensus")})
    return sorted(out, key=lambda r: r["date"])


def _one(ticker: str) -> list[dict]:
    history = consensus_history(ticker)
    if len(history) < 2:
        return []
    closes = prices.history(ticker)
    if closes.empty:
        return []
    index = closes.index.normalize()

    rows = []
    previous = None
    for snap in history:
        # The first session on or after the snapshot date: the consensus was known by then.
        at = index.searchsorted(pd.Timestamp(snap["date"]))
        if at >= len(closes):
            previous = snap
            continue
        row = {"ticker": ticker, "date": snap["date"], "month": snap["date"].strftime("%Y-%m"),
               "buy": snap["buy"], "hold": snap["hold"], "sell": snap["sell"],
               "price": float(closes.iloc[at])}
        total = snap["buy"] + snap["hold"] + snap["sell"]
        row["analysts"] = total
        row["buy_share"] = snap["buy"] / total
        row["sell_share"] = snap["sell"] / total
        if previous:
            row["buy_change"] = snap["buy"] - previous["buy"]
        complete = True
        for label, days in HORIZONS.items():
            if at + days < len(closes):
                row[label] = float(closes.iloc[at + days] / closes.iloc[at] - 1)
            else:
                complete = False
        if complete:
            rows.append(row)
        previous = snap
    return rows


def observations(tickers: list[str]) -> pd.DataFrame:
    rows: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for got in pool.map(_one, tickers):
            rows.extend(got)
            done += 1
            if done % 50 == 0 or done == len(tickers):
                print(f"[backtest] {done}/{len(tickers)} stocks, {len(rows):,} stock-months")
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # Trim the handful of splits, mergers and data faults that would otherwise dominate.
    for label in HORIZONS:
        df = df[df[label].abs() < 1.0]
    for label in HORIZONS:
        df[f"{label} excess"] = df[label] - df.groupby("month")[label].transform("mean")
    return df.reset_index(drop=True)


def _summary(series: pd.Series, excess: pd.Series) -> dict:
    return {"n": int(len(series)),
            "win_rate": float((series > 0).mean() * 100),
            "mean_pct": float(series.mean() * 100),
            "median_pct": float(series.median() * 100),
            "excess_pct": float(excess.mean() * 100),
            "beat_avg_rate": float((excess > 0).mean() * 100)}


def _monthly_t(df: pd.DataFrame, mask: pd.Series, label: str) -> tuple[float, float, int]:
    """Mean monthly excess return of the group, and its t-statistic across months."""
    per_month = df[mask].groupby("month")[f"{label} excess"].mean()
    if len(per_month) < 3:
        return float("nan"), float("nan"), len(per_month)
    mean, sd = per_month.mean(), per_month.std(ddof=1)
    t = mean / (sd / math.sqrt(len(per_month))) if sd else float("nan")
    return float(mean * 100), float(t), int(len(per_month))


def _ic(df: pd.DataFrame, signal: str, label: str) -> tuple[float, float, int]:
    """Rank correlation of a signal with the forward return, averaged over months."""
    ics = []
    for _, group in df.groupby("month"):
        g = group[[signal, label]].dropna()
        if len(g) >= 20 and g[signal].nunique() > 1:
            ics.append(g[signal].rank().corr(g[label].rank()))
    if len(ics) < 3:
        return float("nan"), float("nan"), len(ics)
    arr = np.array(ics)
    t = arr.mean() / (arr.std(ddof=1) / math.sqrt(len(arr))) if arr.std(ddof=1) else float("nan")
    return float(arr.mean()), float(t), len(arr)


def analyse(df: pd.DataFrame) -> dict:
    out: dict = {"observations": int(len(df)), "stocks": int(df["ticker"].nunique()),
                 "months": sorted(df["month"].unique().tolist()), "horizons": {}}
    groups = {}
    for lo, hi, name in SHARE_BUCKETS:
        groups[name] = (df["buy_share"] >= lo) & (df["buy_share"] < hi)
    groups["buys rising"] = df["buy_change"] > 0
    groups["buys flat"] = df["buy_change"] == 0
    groups["buys falling"] = df["buy_change"] < 0
    groups["any sell rating"] = df["sell"] > 0
    groups["zero sells"] = df["sell"] == 0
    # The verification stage's own lines, applied to Nasdaq's counts.
    groups["passes the check"] = (df["buy_share"] >= 0.8) & ~(
        ((df["sell"] >= 2) & (df["sell_share"] > 0.10)) | (df["sell_share"] >= 0.25))
    groups["fails the check"] = (df["buy_share"] < 0.6) | (
        (df["sell"] >= 2) & (df["sell_share"] > 0.10)) | (df["sell_share"] >= 0.25)
    # The screener's brief, as near as Nasdaq's smaller counts allow.
    groups["brief: 20+ buys, <=3 holds, 0 sells"] = (
        (df["buy"] >= 20) & (df["hold"] <= 3) & (df["sell"] == 0))

    for label in HORIZONS:
        h = {"baseline": _summary(df[label], df[f"{label} excess"]), "groups": {}}
        for name, mask in groups.items():
            if mask.sum() == 0:
                continue
            row = _summary(df.loc[mask, label], df.loc[mask, f"{label} excess"])
            row["monthly_excess_pct"], row["t_stat"], row["months"] = _monthly_t(df, mask, label)
            h["groups"][name] = row
        h["ic_buy_share"] = _ic(df, "buy_share", label)
        h["ic_buy_change"] = _ic(df, "buy_change", label)
        out["horizons"][label] = h
    return out


def print_report(result: dict):
    print(f"\n  {result['observations']:,} stock-months from {result['stocks']} stocks, "
          f"{result['months'][0]} to {result['months'][-1]}")
    for label, h in result["horizons"].items():
        b = h["baseline"]
        print(f"\n  ── next {label} ─────────────────────────────────────────────────────")
        print(f"  baseline: {b['win_rate']:.1f}% rose, mean {b['mean_pct']:+.2f}%  "
              f"(n={b['n']:,})")
        print(f"  {'group':<36}{'n':>6}{'rose':>8}{'mean':>9}{'vs avg':>9}{'t':>7}")
        for name, g in h["groups"].items():
            t = g["t_stat"]
            print(f"  {name:<36}{g['n']:>6}{g['win_rate']:>7.1f}%{g['mean_pct']:>+8.2f}%"
                  f"{g['excess_pct']:>+8.2f}%{(f'{t:>7.1f}' if t == t else '      -')}")
        ic, t, n = h["ic_buy_share"]
        print(f"  rank correlation, buy share -> return:   {ic:+.3f}  (t={t:.1f}, {n} months)")
        ic, t, n = h["ic_buy_change"]
        print(f"  rank correlation, buy change -> return:  {ic:+.3f}  (t={t:.1f}, {n} months)")
    print("\n  |t| under about 2 means the difference could easily be chance.")


def run(n: int = SAMPLE, seed: int = SEED) -> dict:
    tickers = sample(n, seed)
    if not tickers:
        print("[backtest] no universe yet - run `python run.py pull-bulk` first")
        return {}
    print(f"[backtest] {len(tickers)} random liquid stocks (seed {seed}): reading a year of "
          f"Nasdaq consensus history and MarketBeat prices...")
    notes.stage("Backtest — does the consensus predict the next month?")
    df = observations(tickers)
    if df.empty:
        print("[backtest] no usable history came back")
        return {}
    result = analyse(df)
    result.update({"seed": seed, "sample": len(tickers), "built": db.today()})
    with db.connect() as con:
        db.set_meta(con, "backtest", result)
    print_report(result)
    df.to_csv(db.DATA_DIR / "backtest_observations.csv", index=False)
    print(f"\n  every observation saved to {db.DATA_DIR / 'backtest_observations.csv'}")
    return result
