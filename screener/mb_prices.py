"""Price history from MarketBeat itself.

Each stock page references a per-page chart endpoint:

    /Scripts/charts/PriceSeries.ashx?prefix=<exchange>&symbol=<ticker>&currency=<ccy>&scrp=<token>

The token is minted for that page, so the page must be fetched first. The response is a
JavaScript file containing a CSV:

    Date, SharePrice, Session, InsightPoint, InsightHTML, OneMinuteVolume

It carries about a year of end-of-day closes and event markers (earnings dates, rating
changes). It does NOT carry open/high/low or end-of-day volume, which is why the indicators
built on it are close-only - see ta.py.
"""
import re

import pandas as pd

from .pull_detail import get_html, stock_base_url

SERIES_RE = re.compile(r"""["'](/Scripts/charts/PriceSeries\.ashx[^"']*)["']""", re.I)
PAYLOAD_RE = re.compile(r'priceSeries="(.*?)";', re.S)
CURRENCY_RE = re.compile(r'priceSeriesCurrencySymbol="([^"]*)"')


def series_url(session, ticker: str, universe_row: dict | None) -> str | None:
    """Find the chart endpoint on the stock's own page (the token is page-specific)."""
    html = get_html(session, stock_base_url(ticker, universe_row))
    m = SERIES_RE.search(html)
    if not m:
        return None
    return "https://www.marketbeat.com" + m.group(1).replace("&amp;", "&")


def parse_series(js: str) -> tuple[pd.DataFrame, list[dict], str]:
    m = PAYLOAD_RE.search(js)
    if not m:
        return pd.DataFrame(), [], "$"
    currency = (CURRENCY_RE.search(js).group(1) if CURRENCY_RE.search(js) else "$")
    raw = m.group(1).encode("utf-8").decode("unicode_escape")
    lines = [l for l in raw.split("\n") if l.strip()]
    rows, events = [], []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 3:
            continue
        date, price, session_kind = parts[0], parts[1], parts[2]
        try:
            close = float(price)
        except ValueError:
            continue
        if session_kind.strip().upper() != "EOD":
            continue
        rows.append((date, close))
        marker = parts[3].strip() if len(parts) > 3 else ""
        if marker:
            events.append({"date": date[:10], "kind": marker})
    if not rows:
        return pd.DataFrame(), events, currency
    df = pd.DataFrame(rows, columns=["Date", "Close"])
    df["Date"] = pd.to_datetime(df["Date"])
    return df.set_index("Date").sort_index(), events, currency


def history(session, ticker: str, universe_row: dict | None) -> tuple[pd.DataFrame, list[dict], str]:
    """A year of MarketBeat's own end-of-day closes, plus the events it marks on the chart."""
    url = series_url(session, ticker, universe_row)
    if not url:
        return pd.DataFrame(), [], "$"
    try:
        response = session.get(url, timeout=45)
        if not response.ok:
            return pd.DataFrame(), [], "$"
        return parse_series(response.text)
    except Exception:
        return pd.DataFrame(), [], "$"
