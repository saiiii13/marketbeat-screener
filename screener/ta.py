"""Technical analysis computed here, from MarketBeat's end-of-day closing prices.

  MACD            Appel - trend direction and momentum turns
  RSI             Wilder - momentum
  Bollinger %B    Bollinger - where price sits inside its own volatility envelope
  ATR             Wilder - the average true daily range, from real highs and lows
  ADX / DI        Wilder - how strong the trend is, and which way it points
  OBV             Granville - whether volume is flowing in with the price or against it
  Realised vol    close-to-close
  Sharpe          return per unit of volatility over the window
  Relative volume does volume back the move
  52-week position where price sits in its published range
  Relative strength against the S&P 500

ATR, ADX and OBV need intraday highs, lows and daily volume. MarketBeat's chart carries
closes only, so they are computed only when a caller passes a frame with real highs, lows
and volume: when closes stand in for them, the true range collapses to the close-to-close
move and understates a stock's daily swing by about a third, so they are skipped, not faked.
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


def has_real_ranges(ohlcv: pd.DataFrame | None) -> bool:
    """True when highs and lows are genuine, not closes standing in for them."""
    if ohlcv is None or ohlcv.empty or not {"High", "Low", "Close"} <= set(ohlcv.columns):
        return False
    tail = ohlcv.tail(60)
    return bool(((tail["High"] - tail["Low"]).abs() > 1e-9).mean() > 0.8)


def wilder(series: pd.Series, n: int = 14) -> pd.Series:
    return series.ewm(alpha=1 / n, adjust=False).mean()


def atr(ohlcv: pd.DataFrame, n: int = 14) -> pd.Series:
    high, low, close = ohlcv["High"], ohlcv["Low"], ohlcv["Close"]
    prev = close.shift(1)
    true_range = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    return wilder(true_range, n)


def adx(ohlcv: pd.DataFrame, n: int = 14) -> tuple[pd.Series, pd.Series, pd.Series]:
    """ADX with its +DI and -DI lines."""
    high, low = ohlcv["High"], ohlcv["Low"]
    up, down = high.diff(), -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    rng = atr(ohlcv, n)
    plus_di = 100 * wilder(plus_dm, n) / rng
    minus_di = 100 * wilder(minus_dm, n) / rng
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return wilder(dx, n), plus_di, minus_di


def obv(ohlcv: pd.DataFrame) -> pd.Series:
    direction = np.sign(ohlcv["Close"].diff()).fillna(0)
    return (direction * ohlcv["Volume"].fillna(0)).cumsum()


def _last(series: pd.Series) -> float | None:
    s = series.dropna()
    return float(s.iloc[-1]) if len(s) else None


def analyse(ticker: str, closes: pd.Series, benchmark: pd.Series | None = None,
            mb_row: dict | None = None, ohlcv: pd.DataFrame | None = None) -> Analysis:
    """Every indicator below is computed from end-of-day prices.

    Pass `ohlcv` (Open/High/Low/Close/Volume) to get ATR, ADX and OBV as well; without it,
    or when its highs and lows are not real, those three are left out.
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

    if has_real_ranges(ohlcv):
        bars = ohlcv.dropna(subset=["High", "Low", "Close"]).astype(float)
        atr_v = _last(atr(bars))
        if atr_v:
            atr_pct = atr_v / price * 100
            signals.append(Signal(
                "ATR (14)", atr_pct,
                f"true daily range {atr_pct:.1f}% of price",
                0.6 if atr_pct < 2.5 else 0.5 if atr_pct < 4 else 0.35,
                f"a 2-ATR stop sits {price - 2 * atr_v:,.2f}"))
        adx_s, plus_di, minus_di = adx(bars)
        adx_v, pdi, mdi = _last(adx_s), _last(plus_di), _last(minus_di)
        if adx_v is not None and pdi is not None and mdi is not None:
            rising = pdi > mdi
            strong = adx_v >= 25
            signals.append(Signal(
                "ADX (14)", adx_v,
                ("strong uptrend" if strong and rising else
                 "strong downtrend" if strong else
                 "weak trend, drifting up" if rising else "weak trend, drifting down"),
                0.85 if strong and rising else 0.15 if strong else 0.55 if rising else 0.4,
                f"+DI {pdi:.0f} against -DI {mdi:.0f}"))
        if "Volume" in bars and bars["Volume"].tail(40).sum() > 0:
            flow = obv(bars)
            if len(flow) > 21:
                obv_up = flow.iloc[-1] > flow.iloc[-21]
                price_up = closes.iloc[-1] > closes.iloc[-21]
                signals.append(Signal(
                    "OBV (20d)", float(flow.iloc[-1] - flow.iloc[-21]),
                    ("volume confirming the rise" if obv_up and price_up else
                     "rising on fading volume" if price_up else
                     "volume accumulating into weakness" if obv_up else
                     "volume confirming the fall"),
                    0.8 if obv_up and price_up else 0.45 if price_up else
                    0.55 if obv_up else 0.2,
                    "on-balance volume over the last month"))

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
