"""Measured odds for a setup, instead of a made-up price.

Builds a lookup table from history: for each combination of trailing momentum, RSI band
and trend state, how often did the stock end the next month higher, and by how much?

Every number here is counted, not modelled. The sample size travels with the figure so a
cell built on forty observations is never mistaken for a reliable one, and the whole table
is rebuilt from current data rather than hard-coded.

The honest headline from the first build: these odds sit near 50%. That is the real
edge available from price-shape signals, and showing it plainly is the point.
"""
import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor

from . import db, yahoo

HORIZONS = {"tomorrow": 1, "next week": 5, "next month": 21}
MIN_PRICE = 5.0
MIN_DOLLAR_VOL = 5_000_000
MIN_CELL = 60           # below this a cell is too thin to quote on its own
SAMPLE_STOCKS = 400


def momentum_band(value: float) -> str:
    if value <= -0.05:
        return "falling"
    if value < 0.05:
        return "flat"
    return "rising"


def rsi_band(value: float) -> str:
    if value < 50:
        return "rsi<50"
    if value <= 70:
        return "rsi50-70"
    return "rsi>70"


def rsi_of(window: pd.Series) -> float:
    delta = window.diff().dropna()
    up = delta.clip(lower=0).mean()
    down = (-delta.clip(upper=0)).mean()
    return 100 - 100 / (1 + up / down) if down else 100.0


def observations(tickers: list[str], workers: int = 8) -> pd.DataFrame:
    """Walk each stock's history, recording the setup and what happened next."""
    def load(ticker):
        try:
            df = yahoo.history(ticker, period="2y")
            return df if len(df) > 300 else None
        except Exception:
            return None

    rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for df in pool.map(load, tickers):
            if df is None:
                continue
            close = df["Close"].astype(float)
            volume = df["Volume"].astype(float)
            longest = max(HORIZONS.values())
            for i in range(63, len(close) - longest, 5):
                price = close.iloc[i]
                dollar_vol = (close.iloc[i - 20:i + 1] * volume.iloc[i - 20:i + 1]).mean()
                if price < MIN_PRICE or dollar_vol < MIN_DOLLAR_VOL:
                    continue
                past = price / close.iloc[i - 21] - 1
                if abs(past) > 1:               # split artefact, not a real move
                    continue
                row = {
                    "momentum": momentum_band(past),
                    "rsi": rsi_band(rsi_of(close.iloc[i - 14:i + 1])),
                    "trend": "above" if price > close.iloc[i - 20:i + 1].mean() else "below",
                }
                keep = True
                for label, days in HORIZONS.items():
                    forward = close.iloc[i + days] / price - 1
                    if abs(forward) > 1:
                        keep = False
                        break
                    row[label] = forward
                if keep:
                    rows.append(row)
    return pd.DataFrame(rows)


def build(sample: int = SAMPLE_STOCKS) -> dict:
    """Rebuild the table and store it. Returns the table."""
    with db.connect() as con:
        tickers = [r["ticker"] for r in con.execute(
            "SELECT ticker FROM universe ORDER BY ticker")][:sample]
    if not tickers:
        print("[calibrate] no universe yet — run the pipeline first")
        return {}

    print(f"[calibrate] measuring {len(tickers)} stocks over two years...")
    data = observations(tickers)
    if data.empty:
        return {}

    table: dict = {"cells": {}, "baseline": {}, "observations": int(len(data))}
    for label in HORIZONS:
        series = data[label]
        table["baseline"][label] = {
            "win_rate": float((series > 0).mean() * 100),
            "mean_pct": float(series.mean() * 100),
            "median_pct": float(series.median() * 100),
            "n": int(len(series)),
        }

    grouped = data.groupby(["momentum", "rsi", "trend"], observed=True)
    for (mom, rsi, trend), group in grouped:
        key = f"{mom}|{rsi}|{trend}"
        cell = {"n": int(len(group))}
        for label in HORIZONS:
            series = group[label]
            cell[label] = {
                "win_rate": float((series > 0).mean() * 100),
                "mean_pct": float(series.mean() * 100),
                "median_pct": float(series.median() * 100),
            }
        table["cells"][key] = cell

    with db.connect() as con:
        db.set_meta(con, "calibration", table)
    print(f"[calibrate] {len(table['cells'])} setups from {len(data):,} observations")
    for label in HORIZONS:
        b = table["baseline"][label]
        print(f"  baseline {label:<11} {b['win_rate']:.1f}% up, avg {b['mean_pct']:+.2f}%")
    return table


def load_table() -> dict:
    with db.connect() as con:
        return db.get_meta(con, "calibration", {}) or {}


def setup_key(p: dict) -> str | None:
    ret_1m, rsi = p.get("ret_1m"), p.get("rsi14")
    if ret_1m is None or rsi is None or p.get("above_ma20") is None:
        return None
    return (f"{momentum_band(ret_1m)}|{rsi_band(rsi)}|"
            f"{'above' if p.get('above_ma20') else 'below'}")


def odds_for(p: dict, table: dict | None = None) -> dict:
    """What historically happened to stocks in this same setup."""
    table = table if table is not None else load_table()
    if not table:
        return {}
    key = setup_key(p)
    if not key:
        return {}
    cell = (table.get("cells") or {}).get(key)
    if not cell:
        return {}
    momentum, rsi, trend = key.split("|")
    out = {
        "setup": f"{momentum} momentum, {rsi}, price {trend} its 20-day",
        "n": cell["n"],
        "reliable": cell["n"] >= MIN_CELL,
        "horizons": {},
    }
    for label in HORIZONS:
        base = (table.get("baseline") or {}).get(label, {})
        out["horizons"][label] = {
            "win_rate": cell[label]["win_rate"],
            "mean_pct": cell[label]["mean_pct"],
            "median_pct": cell[label]["median_pct"],
            "baseline_win_rate": base.get("win_rate"),
            "edge_pts": (cell[label]["win_rate"] - base["win_rate"]) if base else None,
        }
    return out
