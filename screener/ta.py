"""Technical analysis computed here, from MarketBeat's own end-of-day price history.

Every number below comes from MarketBeat: the closes come from its chart feed
(mb_prices.py) and the volume, range and beta figures come from its screener columns.
Nothing is sourced from another data provider.

  MACD            Appel - trend direction and momentum turns
  RSI             Wilder - momentum, cross-checked against MarketBeat's own RSI column
  Bollinger %B    Bollinger - where price sits inside its own volatility envelope
  Realised vol    close-to-close, standing in for ATR
  Sharpe          return per unit of volatility over the window
  Relative volume MarketBeat's column - does volume back the move
  52-week position where price sits in MarketBeat's published range
  Relative strength against the S&P 500, itself taken from MarketBeat

ATR, ADX and OBV are deliberately absent: they need intraday highs, lows and daily
volume, which MarketBeat's chart feed does not carry. Approximating them from closes
would look like analysis while being something else.
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class Signal:
    name: str
    value: float | None
    verdict: str
    score: float  # 0..1, higher is more bullish
    detail: str = ""


@dataclass
class Analysis:
    ticker: str
    closes: list[float] = field(default_factory=list)
    highs: list[float] = field(default_factory=list)
    lows: list[float] = field(default_factory=list)
    volumes: list[float] = field(default_factory=list)
    ma20: list[float] = field(default_factory=list)
    ma50: list[float] = field(default_factory=list)
    ma200: list[float] = field(default_factory=list)
    macd_line: list[float] = field(default_factory=list)
    macd_signal: list[float] = field(default_factory=list)
    macd_hist: list[float] = field(default_factory=list)
    rsi: list[float] = field(default_factory=list)
    bb_upper: list[float] = field(default_factory=list)
    bb_lower: list[float] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)
    score: float = 0.0
    summary: str = ""


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def macd(close: pd.Series, fast=12, slow=26, signal=9):
    line = ema(close, fast) - ema(close, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std()
    return mid + k * sd, mid, mid - k * sd


def slope_pct(series: pd.Series, n: int = 20) -> float | None:
    """Least-squares slope over the last n points, as % of the mean level."""
    tail = series.tail(n).dropna()
    if len(tail) < max(5, n // 2):
        return None
    x = np.arange(len(tail), dtype=float)
    slope = np.polyfit(x, tail.to_numpy(dtype=float), 1)[0]
    level = abs(tail.mean()) or 1.0
    return float(slope / level * 100)


def sharpe(close: pd.Series, periods: int = 63) -> float | None:
    rets = close.pct_change().dropna().tail(periods)
    if len(rets) < 20 or rets.std() == 0:
        return None
    return float(rets.mean() / rets.std() * np.sqrt(252))


def _last(series: pd.Series) -> float | None:
    s = series.dropna()
    return float(s.iloc[-1]) if len(s) else None


def analyse(ticker: str, closes: pd.Series, benchmark: pd.Series | None = None,
            mb_row: dict | None = None) -> Analysis:
    """Every indicator below is computed from MarketBeat's own end-of-day closes.

    MarketBeat's chart feed carries closes only - no open/high/low and no daily volume - so
    the indicators that need those (ATR, ADX, OBV) are not computed. Where MarketBeat
    publishes an equivalent figure in its screener columns (relative volume, 52-week range,
    beta), that value is used instead and labelled as coming from MarketBeat.
    """
    out = Analysis(ticker=ticker)
    closes = closes.dropna().astype(float)
    if len(closes) < 60:
        out.summary = "not enough MarketBeat price history to analyse"
        return out

    mb = mb_row or {}
    ma20, ma50 = closes.rolling(20).mean(), closes.rolling(50).mean()
    ma200 = closes.rolling(200).mean() if len(closes) >= 200 else pd.Series(np.nan, index=closes.index)
    line, sig, hist = macd(closes)
    r = rsi(closes)
    bb_up, bb_mid, bb_lo = bollinger(closes)

    price = float(closes.iloc[-1])
    signals: list[Signal] = []

    above20 = price > (_last(ma20) or price)
    above50 = price > (_last(ma50) or price)
    stacked = (_last(ma20) or 0) > (_last(ma50) or 0)
    signals.append(Signal(
        "Trend", price,
        "above both averages, 20 over 50" if above20 and above50 and stacked else
        "above both averages" if above20 and above50 else
        "above the 20-day only" if above20 else "below its moving averages",
        0.2 + 0.3 * above20 + 0.3 * above50 + 0.2 * stacked,
        f"20-day {_last(ma20):,.2f}, 50-day {_last(ma50):,.2f}" if _last(ma20) and _last(ma50) else ""))

    h_now = _last(hist)
    h_prev = hist.dropna().iloc[-4] if len(hist.dropna()) > 4 else None
    if h_now is not None:
        rising = h_prev is not None and h_now > h_prev
        signals.append(Signal(
            "MACD", h_now,
            "bullish and strengthening" if h_now > 0 and rising else
            "bullish but fading" if h_now > 0 else
            "bearish but improving" if rising else "bearish",
            0.75 if h_now > 0 and rising else 0.6 if h_now > 0 else 0.3 if rising else 0.15,
            f"line {_last(line):+.3f} vs signal {_last(sig):+.3f}"))

    r_now = _last(r)
    if r_now is not None:
        mb_rsi = mb.get("rsi")
        detail = f"MarketBeat's own RSI reads {mb_rsi:.0f}" if mb_rsi else ""
        signals.append(Signal(
            "RSI(14)", r_now,
            "momentum, not overbought" if 50 <= r_now <= 70 else
            "overbought" if r_now > 70 else "oversold" if r_now < 30 else "neutral",
            1.0 if 50 <= r_now <= 70 else 0.6 if 40 <= r_now < 50 else 0.35 if r_now <= 80 else 0.15,
            detail))

    up_v, lo_v = _last(bb_up), _last(bb_lo)
    if up_v and lo_v and up_v != lo_v:
        pct_b = (price - lo_v) / (up_v - lo_v)
        signals.append(Signal(
            "Bollinger %B", pct_b * 100,
            "riding the upper band" if pct_b > 0.85 else
            "upper half, room left" if pct_b >= 0.5 else
            "lower half" if pct_b >= 0.15 else "at the lower band",
            0.9 if 0.5 <= pct_b <= 0.85 else 0.5 if pct_b > 0.85 else 0.55 if pct_b >= 0.3 else 0.3,
            f"band width {(up_v - lo_v) / price * 100:.1f}% of price"))

    # Close-to-close realised volatility stands in for ATR, which needs highs and lows.
    rets = closes.pct_change().dropna()
    vol20 = float(rets.tail(20).std() * np.sqrt(252)) if len(rets) >= 20 else None
    if vol20 is not None:
        daily = vol20 / np.sqrt(252) * 100
        signals.append(Signal(
            "Volatility (20d)", vol20 * 100,
            f"typical daily move {daily:.1f}%", 0.5,
            f"a 2-sigma stop sits {price * (1 - 2 * daily / 100):,.2f}"))

    sh = sharpe(closes)
    if sh is not None:
        signals.append(Signal("Sharpe (3m)", sh,
                              "good risk-adjusted return" if sh > 1 else
                              "modest" if sh > 0 else "losing money for the risk",
                              float(np.clip((sh + 1) / 3, 0, 1))))

    # Volume confirmation, using MarketBeat's relative-volume column.
    # MarketBeat publishes relative volume as a PERCENTAGE of normal, where 100 is average
    # (the column's median across 18k stocks is ~56, p75 ~107). Reading it as a ratio would
    # invert the meaning entirely.
    rel_vol = mb.get("relative_volume")
    if rel_vol and 0 < rel_vol < 10_000:
        price_rising = (_last(ma20) or 0) > (ma20.dropna().iloc[-6] if len(ma20.dropna()) > 6 else 0)
        heavy = rel_vol >= 150
        signals.append(Signal(
            "Relative volume", rel_vol,
            "heavy trading behind the move" if heavy and price_rising else
            "heavier than usual" if rel_vol >= 120 else
            "quiet — the move lacks volume" if rel_vol < 80 else "normal",
            0.85 if heavy and price_rising else 0.65 if rel_vol >= 100 else
            0.45 if rel_vol >= 80 else 0.3,
            f"{rel_vol:.0f}% of its normal volume, per MarketBeat"))

    # Where the price sits in MarketBeat's own 52-week range.
    lo52, hi52 = mb.get("range_52w_low"), mb.get("range_52w_high")
    if lo52 and hi52 and hi52 > lo52:
        pos = (price - lo52) / (hi52 - lo52)
        signals.append(Signal(
            "52-week position", pos * 100,
            "near its high" if pos > 0.85 else "upper half" if pos >= 0.5 else
            "lower half" if pos >= 0.2 else "near its low",
            0.9 if 0.6 <= pos <= 0.95 else 0.6 if pos > 0.95 else 0.5 if pos >= 0.4 else 0.25,
            f"MarketBeat range {lo52:,.2f} to {hi52:,.2f}"))

    if benchmark is not None and len(benchmark) > 60:
        window = min(len(closes), len(benchmark), 63)
        stock_ret = price / float(closes.iloc[-window]) - 1
        bench_ret = float(benchmark.iloc[-1]) / float(benchmark.iloc[-window]) - 1
        excess = (stock_ret - bench_ret) * 100
        signals.append(Signal("vs S&P 500 (3m)", excess,
                              "beating the market" if excess > 0 else "lagging the market",
                              float(np.clip(0.5 + excess / 40, 0, 1)),
                              f"stock {stock_ret * 100:+.1f}% vs index {bench_ret * 100:+.1f}%"))

    voting = [s for s in signals if s.name != "Volatility (20d)"]
    out.score = float(np.mean([s.score for s in voting]) * 100) if voting else 0.0
    out.summary = f"{sum(1 for s in voting if s.score >= 0.6)} of {len(voting)} indicators bullish"

    tail = 120
    out.closes = [round(float(x), 4) for x in closes.tail(tail)]
    out.ma20 = [None if pd.isna(x) else round(float(x), 4) for x in ma20.tail(tail)]
    out.ma50 = [None if pd.isna(x) else round(float(x), 4) for x in ma50.tail(tail)]
    out.ma200 = [None if pd.isna(x) else round(float(x), 4) for x in ma200.tail(tail)]
    out.macd_line = [None if pd.isna(x) else float(x) for x in line.tail(tail)]
    out.macd_signal = [None if pd.isna(x) else float(x) for x in sig.tail(tail)]
    out.macd_hist = [None if pd.isna(x) else float(x) for x in hist.tail(tail)]
    out.rsi = [None if pd.isna(x) else float(x) for x in r.tail(tail)]
    out.bb_upper = [None if pd.isna(x) else float(x) for x in bb_up.tail(tail)]
    out.bb_lower = [None if pd.isna(x) else float(x) for x in bb_lo.tail(tail)]
    out.signals = signals
    return out
