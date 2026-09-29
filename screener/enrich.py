"""Stage C: price/technical metrics from yfinance for the pre-filtered survivors (batched, resumable).
MarketBeat decides *which* stocks are candidates; this only supplies timing/risk numbers."""
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import local

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from . import db, notes
from .config import YF_BATCH, THROTTLE_HIT_RATE, THROTTLE_SLEEP_SEC, YF_MAX_PASSES

logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # delisted-ticker noise

# MarketBeat exchange code -> Yahoo suffix
SUFFIX = {
    "TSE": ".TO", "TSX": ".TO", "CVE": ".V", "TSXV": ".V", "CSE": ".CN", "CNSX": ".CN",
    "LON": ".L", "LSE": ".L", "AIM": ".L",
    "ETR": ".DE", "FRA": ".F", "XETRA": ".DE", "EPA": ".PA", "AMS": ".AS", "EBR": ".BR", "BIT": ".MI",
    "BME": ".MC", "SWX": ".SW", "VTX": ".SW", "STO": ".ST", "CPH": ".CO", "HEL": ".HE", "OSL": ".OL",
    "VIE": ".VI", "WSE": ".WA", "LIS": ".LS", "ISE": ".IR", "ATH": ".AT", "PRA": ".PR", "BUD": ".BD",
}


def yahoo_symbol(ticker: str, exchange: str | None) -> str:
    sym = ticker.replace(".", "-")
    suf = SUFFIX.get((exchange or "").upper(), "")
    return sym + suf


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


def metrics(df: pd.DataFrame) -> dict | None:
    df = df.dropna(subset=["Close"])
    if len(df) < 30:
        return None
    close = df["Close"].astype(float)
    vol = df["Volume"].fillna(0).astype(float)
    last = float(close.iloc[-1])

    def ret(days):
        return float(last / close.iloc[-1 - days] - 1) if len(close) > days else None

    ma20 = close.rolling(20).mean().iloc[-1]
    ma50 = close.rolling(50).mean().iloc[-1] if len(close) >= 50 else np.nan
    logret = np.log(close).diff().dropna()
    vol20 = float(logret.tail(20).std() * math.sqrt(252)) if len(logret) >= 20 else None
    dollar = float((close * vol).tail(20).mean())
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
        "avg_dollar_vol_20d": dollar,
        "pct_from_52w_high": float(last / hi_52w - 1) if hi_52w else None,
        "pct_from_52w_low": float(last / lo_52w - 1) if lo_52w else None,
        "breakout_20d": bool(last > hi_20d) if hi_20d else None,
        "rel_volume_5d": float(vol.tail(5).mean() / vol.tail(60).mean()) if vol.tail(60).mean() > 0 else None,
        "closes_30d": [round(float(x), 4) for x in close.tail(30).tolist()],
        "bars": int(len(close)),
    }


CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
CHART_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept": "application/json",
}
_chart_tls = local()


def _chart_session() -> requests.Session:
    sess = getattr(_chart_tls, "sess", None)
    if sess is None:
        sess = _chart_tls.sess = requests.Session()
        sess.headers.update(CHART_HEADERS)
    return sess


def fetch_chart(symbol: str, retries: int = 2) -> pd.DataFrame | None:
    """One year of daily bars straight from Yahoo's public chart endpoint.

    yfinance's cookie/crumb handshake gets rate-limited hard on large runs; this plain
    JSON endpoint does not, so it is the primary path.
    """
    for attempt in range(retries):
        try:
            r = _chart_session().get(CHART_URL.format(sym=symbol),
                                     params={"range": "1y", "interval": "1d"}, timeout=25)
            if r.status_code in (404, 422):
                return None
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            res = (r.json().get("chart") or {}).get("result")
            if not res:
                return None
            block = res[0]
            stamps = block.get("timestamp") or []
            quote = ((block.get("indicators") or {}).get("quote") or [{}])[0]
            closes = quote.get("close") or []
            volumes = quote.get("volume") or []
            if not stamps or not closes:
                return None
            adj = ((block.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose")
            # High/Low are needed for ATR and ADX; without them those indicators degrade
            # to close-only and understate real ranges.
            def column(key, fallback):
                values = quote.get(key)
                return values if values and len(values) == len(stamps) else fallback
            df = pd.DataFrame(
                {"Close": adj if adj and len(adj) == len(stamps) else closes,
                 "Open": column("open", closes),
                 "High": column("high", closes),
                 "Low": column("low", closes),
                 "Volume": column("volume", [0] * len(stamps))},
                index=pd.to_datetime(pd.Series(stamps), unit="s"),
            )
            return df.dropna(subset=["Close"])
        except Exception:
            time.sleep(2 * (attempt + 1))
    return None


def download_charts(symbols: list[str], workers: int = 8) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_chart, s): s for s in symbols}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                df = fut.result()
            except Exception:
                df = None
            if df is not None and not df.empty:
                out[sym] = df
    return out


def download_batch(symbols: list[str], retries: int = 3) -> dict[str, pd.DataFrame]:
    for attempt in range(retries):
        try:
            data = yf.download(symbols, period="1y", interval="1d", group_by="ticker", threads=True,
                               auto_adjust=True, progress=False)
            out = {}
            if not isinstance(data.columns, pd.MultiIndex):
                out[symbols[0]] = data
            else:
                for s in symbols:
                    if s in data.columns.get_level_values(0):
                        out[s] = data[s]
                    elif s in data.columns.get_level_values(1):
                        out[s] = data.xs(s, axis=1, level=1)
            return out
        except Exception as e:
            print(f"[enrich] batch failed ({e}); retry {attempt + 1}/{retries}")
            time.sleep(5 * (attempt + 1))
    return {}


def run(tickers: list[str], fresh: bool = False, _pass: int = 0) -> dict[str, dict]:
    run_date = db.today()
    with db.connect() as con:
        # A ticker only counts as done if it actually has price history. Symbols that came back
        # empty (usually Yahoo throttling) are retried rather than cached as permanently missing.
        done = set()
        if not fresh:
            for t, d in db.load_json(con, "prices", run_date).items():
                if not d.get("missing"):
                    done.add(t)
        uni = {r["ticker"]: dict(r) for r in con.execute("SELECT ticker, exchange FROM universe")}
    todo = [t for t in tickers if t not in done]
    print(f"[enrich] {len(tickers)} tickers, {len(done)} cached today, {len(todo)} to fetch")
    notes.stage("Stage 2 — price history for the analyst-backed candidates")
    notes.line(f"Downloading a year of daily prices for **{len(todo):,}** stocks "
               f"({len(done):,} already cached today).")

    t_stage = time.time()
    for i in range(0, len(todo), YF_BATCH):
        chunk = todo[i:i + YF_BATCH]
        sym_map = {yahoo_symbol(t, uni.get(t, {}).get("exchange")): t for t in chunk}
        frames = download_charts(list(sym_map))
        # Anything the chart endpoint could not serve gets one try through yfinance.
        missing = [s for s in sym_map if s not in frames]
        if missing and len(missing) < len(sym_map):
            for s in missing[:40]:
                try:
                    df = yf.download(s, period="1y", interval="1d", threads=False,
                                     auto_adjust=True, progress=False)
                    if isinstance(df.columns, pd.MultiIndex):
                        df = df.droplevel(1, axis=1) if s in df.columns.get_level_values(1) else df[s]
                    if not df.empty:
                        frames[s] = df
                except Exception:
                    pass
        rows = {}
        for sym, t in sym_map.items():
            df = frames.get(sym)
            m = metrics(df) if df is not None and not df.empty else None
            rows[t] = m or {"missing": True}
            if m:
                rows[t]["yahoo_symbol"] = sym
        with db.connect() as con:
            for t, m in rows.items():
                db.upsert_json(con, "prices", t, m, run_date)
        ok = sum(1 for m in rows.values() if not m.get("missing"))
        print(f"[enrich] batch {i // YF_BATCH + 1}: {ok}/{len(chunk)} with price history")
        # A collapsing hit rate means Yahoo is throttling, not that the symbols are bad.
        if ok / max(len(chunk), 1) < THROTTLE_HIT_RATE:
            print(f"[enrich] hit rate {ok / len(chunk):.0%} - backing off {THROTTLE_SLEEP_SEC}s")
            notes.line(f"Yahoo started throttling (only {ok}/{len(chunk)} priced); "
                       f"pausing {THROTTLE_SLEEP_SEC}s, these get retried.")
            time.sleep(THROTTLE_SLEEP_SEC)
        movers = sorted(((t, m) for t, m in rows.items() if m.get("ret_1m") is not None),
                        key=lambda x: -x[1]["ret_1m"])[:8]
        notes.progress(min(i + len(chunk), len(todo)), len(todo), "Priced", t_stage)
        if movers:
            notes.bullet("Biggest 1-month movers in this batch: "
                         + ", ".join(f"{t} {m['ret_1m'] * 100:+.0f}%" for t, m in movers))
        time.sleep(0.5)

    with db.connect() as con:
        allp = db.load_json(con, "prices", run_date)
    missing = [t for t in tickers if allp.get(t, {}).get("missing")]
    if missing and _pass < YF_MAX_PASSES:
        print(f"[enrich] {len(missing)} still unpriced; retrying them after a pause (pass {_pass + 1}/{YF_MAX_PASSES})")
        notes.line(f"Retrying **{len(missing)}** stocks Yahoo refused on this pass.")
        time.sleep(THROTTLE_SLEEP_SEC)
        return run(tickers, fresh=False, _pass=_pass + 1)
    priced = {t: allp[t] for t in tickers if t in allp and not allp[t].get("missing")}
    print(f"[enrich] {len(priced)}/{len(tickers)} candidates have price history"
          + (f"; {len(missing)} unavailable after {YF_MAX_PASSES} passes" if missing else ""))
    notes.line(f"**{len(priced):,}** of {len(tickers):,} candidates have usable price history.")
    return priced
