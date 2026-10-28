"""Stage C: price/technical metrics for the candidates, from MarketBeat closes in the database.
MarketBeat decides *which* stocks are candidates; this only supplies timing/risk numbers."""
import math

import numpy as np
import pandas as pd

from . import db, notes, prices


def rsi(series: pd.Series, n: int = 14) -> float | None:
    if len(series) < n + 1:
        return None
    delta = series.diff().dropna()
    up = delta.clip(lower=0).rolling(n).mean()
    down = (-delta.clip(upper=0)).rolling(n).mean()
    last_up, last_down = up.iloc[-1], down.iloc[-1]
    if pd.isna(last_up) or pd.isna(last_down):
        return None
    if last_down == 0:
        return 100.0
    rs = last_up / last_down
    return float(100 - 100 / (1 + rs))


def metrics(df: pd.DataFrame, avg_volume: float | None = None) -> dict | None:
    """Everything the score reads about price, from a frame with a Close column.

    MarketBeat's chart has no daily volume, so dollar volume is the overview page's average
    daily volume times the latest close, passed in as `avg_volume`. A frame that does carry
    a Volume column (an older store) uses it instead.
    """
    df = df.dropna(subset=["Close"])
    if len(df) < 30:
        return None
    close = df["Close"].astype(float)
    last = float(close.iloc[-1])

    def ret(days):
        return float(last / close.iloc[-1 - days] - 1) if len(close) > days else None

    ma20 = close.rolling(20).mean().iloc[-1]
    ma50 = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else np.nan
    logret = np.log(close).diff().dropna()
    vol20 = float(logret.tail(20).std() * math.sqrt(252)) if len(logret) >= 20 else None
    vol1y = float(logret.tail(252).std() * math.sqrt(252)) if len(logret) >= 120 else None
    if "Volume" in df and df["Volume"].tail(20).sum() > 0:
        volume = df["Volume"].fillna(0).astype(float)
        dollar = float((close * volume).tail(20).mean())
        rel_volume = (float(volume.tail(5).mean() / volume.tail(60).mean())
                      if volume.tail(60).mean() > 0 else None)
    else:
        dollar = float(avg_volume * last) if avg_volume else None
        rel_volume = None
    hi_52w = float(close.tail(252).max())
    lo_52w = float(close.tail(252).min())
    hi_20d = float(close.tail(21).iloc[:-1].max()) if len(close) > 21 else None
    return {
        "last_close": last,
        "last_date": str(df.index[-1].date()),
        "ret_5d": ret(5),
        "ret_1m": ret(21),
        "ret_3m": ret(63),
        "above_ma20": bool(last > ma20) if not pd.isna(ma20) else None,
        "ma20_gt_ma50": bool(ma20 > ma50) if not (pd.isna(ma20) or pd.isna(ma50)) else None,
        "ma20_slope_5d": float(ma20 / close.rolling(20).mean().iloc[-6] - 1) if len(close) >= 26 else None,
        "rsi14": rsi(close),
        "vol_20d_annualized": vol20,
        "vol_1y_annualized": vol1y,
        "avg_dollar_vol_20d": dollar,
        "pct_from_52w_high": float(last / hi_52w - 1) if hi_52w else None,
        "pct_from_52w_low": float(last / lo_52w - 1) if lo_52w else None,
        "breakout_20d": bool(last > hi_20d) if hi_20d else None,
        "rel_volume_5d": rel_volume,
        "closes_30d": [round(float(x), 4) for x in close.tail(30).tolist()],
        "bars": int(len(close)),
    }


def run(tickers: list[str], fresh: bool = False) -> dict[str, dict]:
    """Price metrics for these stocks. Stored history is used as it stands; only stocks
    whose history is missing or behind are fetched from MarketBeat."""
    run_date = db.today()
    with db.connect() as con:
        uni = {r["ticker"]: r["exchange"] for r in con.execute("SELECT ticker, exchange FROM universe")}
        bulk = db.load_json(con, "mb_bulk")
    notes.stage("Stage 2 — price history for the candidates")
    stats = prices.ensure([(t, uni.get(t)) for t in tickers], say=print)
    notes.line(f"Price history: **{stats['cached']:,}** already in the database, "
               f"**{stats['fetched']:,}** fetched from MarketBeat.")
    rows = {}
    for t in tickers:
        series = prices.closes(t)
        m = metrics(series.to_frame("Close"), (bulk.get(t) or {}).get("avg_volume")) if len(series) else None
        rows[t] = m or {"missing": True}
    with db.connect() as con:
        for t, m in rows.items():
            db.upsert_json(con, "prices", t, m, run_date)
    priced = {t: m for t, m in rows.items() if not m.get("missing")}
    print(f"[enrich] {len(priced)}/{len(tickers)} candidates have price history")
    notes.line(f"**{len(priced):,}** of {len(tickers):,} candidates have usable price history.")
    return priced
