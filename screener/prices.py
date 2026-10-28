"""MarketBeat price history, kept in the database.

Every close MarketBeat's chart serves is written to price_history, one row per stock per
trading day. Readers ask this module, which answers from the database and only goes to
MarketBeat when a stock's stored history is missing or behind the last completed session.
History therefore grows with every run: MarketBeat's chart reaches back about a year, and
anything older that was stored by an earlier run stays.

The chart carries closes only - no open, high, low or daily volume - which is why the
indicators built on it are close-only (see ta.py) and liquidity comes from the overview
page's average volume instead.
"""
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import pandas as pd

from . import db, mb_prices
from .config import BASE_URL
from .pull_detail import browser_cookies, get_html, make_session

WORKERS = 4
PAUSE_SEC = 0.3
REFETCH_AFTER_HOURS = 6     # a stock still behind after a fetch is not asked again for this long
CLOSE_HOUR = 16             # US market close, local (Eastern) time
SETTLE_MINUTES = 30         # MarketBeat's end-of-day row lands a little after the close

# NYSE full-day closures. MarketBeat's chart repeats the previous close on these days, which
# would read as a day of zero movement.
HOLIDAYS = {
    "2024-01-01", "2024-01-15", "2024-02-19", "2024-03-29", "2024-05-27", "2024-06-19",
    "2024-07-04", "2024-09-02", "2024-11-28", "2024-12-25",
    "2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25",
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}


def _eastern_now() -> datetime:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).replace(tzinfo=None)
    except Exception:
        return datetime.now()


def is_session(day: date) -> bool:
    return day.weekday() < 5 and day.isoformat() not in HOLIDAYS


def last_session(now: datetime | None = None) -> str:
    """The most recent trading day whose close is already final."""
    now = now or _eastern_now()
    day = now.date()
    settled = now.hour * 60 + now.minute >= CLOSE_HOUR * 60 + SETTLE_MINUTES
    if not (is_session(day) and settled):
        day -= timedelta(days=1)
        while not is_session(day):
            day -= timedelta(days=1)
    return day.isoformat()


def clean(df: pd.DataFrame, now: datetime | None = None) -> dict[str, float]:
    """MarketBeat's end-of-day rows -> {date: close} for real, finished sessions only."""
    if df is None or df.empty:
        return {}
    final = last_session(now)
    out = {}
    for stamp, close in df["Close"].items():
        day = stamp.date()
        if is_session(day) and day.isoformat() <= final and close and close > 0:
            out[day.isoformat()] = float(close)
    return out


def _rescale_for_split(con, ticker: str, fresh: dict[str, float]):
    """A split makes MarketBeat restate the whole year it serves. Stored days older than
    that year still carry the old basis, so they are rescaled by the same factor."""
    if not fresh:
        return
    first_new = min(fresh)
    stored = dict(db.load_closes(con, ticker))
    overlap = [d for d in fresh if d in stored and stored[d]]
    if len(overlap) < 5:
        return
    ratios = pd.Series([fresh[d] / stored[d] for d in overlap])
    factor = float(ratios.median())
    if abs(factor - 1) < 0.02 or ratios.std() > 0.01 * factor:
        return
    con.execute("UPDATE price_history SET close = close * ? WHERE ticker=? AND date<?",
                (factor, ticker, first_new))


def store(ticker: str, closes: dict[str, float], exchange: str | None, currency: str | None):
    with db.connect() as con:
        _rescale_for_split(con, ticker, closes)
        db.save_prices(con, ticker, closes)
        db.set_price_meta(con, ticker, exchange, currency)


def is_fresh(ticker: str, con=None) -> bool:
    def check(c):
        meta = db.price_meta(c, ticker)
        if not meta or not meta.get("last_date"):
            return False
        if meta["last_date"] >= last_session():
            return True
        fetched = meta.get("fetched_at")
        if fetched:
            age = datetime.now() - datetime.fromisoformat(fetched)
            return age < timedelta(hours=REFETCH_AFTER_HOURS)
        return False
    if con is not None:
        return check(con)
    with db.connect() as c:
        return check(c)


def chart_from_page(session, html: str) -> tuple[pd.DataFrame, str]:
    """Follow the chart link on a stock's own page (the token is minted per page)."""
    m = mb_prices.SERIES_RE.search(html)
    if not m:
        return pd.DataFrame(), "$"
    url = BASE_URL + m.group(1).replace("&amp;", "&")
    response = session.get(url, timeout=45)
    if not response.ok:
        return pd.DataFrame(), "$"
    df, _, currency = mb_prices.parse_series(response.text)
    return df, currency


def fetch(session, ticker: str, exchange: str | None, html: str | None = None) -> int:
    """Read one stock's chart from MarketBeat and store it. Returns the days stored."""
    exchange = exchange or "NASDAQ"
    if html is None:
        html = get_html(session, f"{BASE_URL}/stocks/{exchange}/{ticker}/")
    df, currency = chart_from_page(session, html)
    closes = clean(df)
    if closes:
        store(ticker, closes, exchange, currency)
    else:
        with db.connect() as con:      # remember the attempt so it is not retried at once
            db.set_price_meta(con, ticker, exchange, currency)
    return len(closes)


def closes(ticker: str, start: str | None = None, end: str | None = None) -> pd.Series:
    """Stored closes as a date-indexed Series (database only, no network)."""
    with db.connect() as con:
        rows = db.load_closes(con, ticker, start, end)
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series([c for _, c in rows], index=pd.to_datetime([d for d, _ in rows]), name="Close")
    return s


CHANGE_PERIODS = {"1w": 7, "1m": 30, "1y": 365}     # calendar days, like MarketBeat's chart

# What the menu shows beside the ratio, as read from each stock's MarketBeat overview page.
FACT_FIELDS = ["price", "sector", "pe", "peg", "proj_earnings_growth_pct", "mr_earnings_valuation",
               "short_pct_float", "short_level", "insider_bought_3m", "insider_sold_3m",
               "news_sentiment", "news_sentiment_sector_avg", "inst_own_pct", "dividend_yield"]


def facts(run_date: str) -> dict[str, dict]:
    """FACT_FIELDS for every stock a run read on `run_date` (database only)."""
    cols = ", ".join(f"json_extract(data, '$.{f}')" for f in FACT_FIELDS)
    with db.connect() as con:
        return {r[0]: dict(zip(FACT_FIELDS, r[1:])) for r in con.execute(
            f"SELECT ticker, {cols} FROM mb_detail WHERE run_date=?", (run_date,))}


def chart_changes(tickers: list[str], run_date: str | None = None) -> tuple[dict, dict]:
    """1-week, 1-month and 1-year changes ending at the price MarketBeat showed on `run_date`
    (its latest stored close where that run did not read the stock), and the facts."""
    run_date = run_date or db.today()
    known = facts(run_date)
    now = {t: f["price"] for t, f in known.items() if f.get("price")}
    return changes(tickers, now, run_date), known


def changes(tickers: list[str], now: dict[str, float] | None = None,
            asof: str | None = None) -> dict[str, dict[str, float | None]]:
    """Percent change over a week, a month and a year for each stock, from stored closes
    (database only). `now` gives today's price where known, dated `asof`; otherwise each
    stock's latest stored close is the end point."""
    from bisect import bisect_right
    from datetime import date as _date
    now = now or {}
    series: dict[str, list[tuple[str, float]]] = {}
    with db.connect() as con:
        for i in range(0, len(tickers), 900):
            chunk = tickers[i:i + 900]
            for r in con.execute(f"SELECT ticker, date, close FROM price_history WHERE ticker IN "
                                 f"({','.join('?' * len(chunk))}) ORDER BY ticker, date", chunk):
                series.setdefault(r["ticker"], []).append((r["date"], r["close"]))
    out = {}
    for t in tickers:
        rows = series.get(t)
        if not rows:
            out[t] = {k: None for k in CHANGE_PERIODS}
            continue
        dates = [d for d, _ in rows]
        if now.get(t) and asof:
            end_price, end_date = now[t], _date.fromisoformat(asof)
        else:
            end_price, end_date = rows[-1][1], _date.fromisoformat(dates[-1])
        out[t] = {}
        for key, days in CHANGE_PERIODS.items():
            then = (end_date - timedelta(days=days)).isoformat()
            k = bisect_right(dates, then) - 1
            start = rows[k][1] if k >= 0 else None
            out[t][key] = round((end_price / start - 1) * 100, 2) if start else None
    return out


def exchange_of(ticker: str) -> str | None:
    with db.connect() as con:
        meta = db.price_meta(con, ticker)
        if meta and meta.get("exchange"):
            return meta["exchange"]
        r = con.execute("SELECT exchange FROM universe WHERE ticker=?", (ticker,)).fetchone()
        if r and r["exchange"]:
            return r["exchange"]
        r = con.execute("SELECT exchange FROM recommendations WHERE ticker=? AND exchange IS NOT NULL "
                        "ORDER BY run_id DESC LIMIT 1", (ticker,)).fetchone()
    return r["exchange"] if r else None


_session = None


def shared_session():
    global _session
    if _session is None:
        _session = make_session(browser_cookies())
    return _session


def history(ticker: str, exchange: str | None = None, session=None) -> pd.Series:
    """Closes for one stock: from the database, topped up from MarketBeat if behind."""
    if not is_fresh(ticker):
        try:
            fetch(session or shared_session(), ticker, exchange or exchange_of(ticker))
        except Exception:
            pass        # whatever is stored still answers
    return closes(ticker)


def ensure(pairs: list[tuple[str, str | None]], say=print, deadline: float | None = None,
           session=None, force: bool = False) -> dict:
    """Bring every stock's stored history up to the last session. Only stale ones are fetched,
    unless `force` fetches them all."""
    with db.connect() as con:
        todo = [(t, ex) for t, ex in pairs if force or not is_fresh(t, con)]
    stats = {"asked": len(pairs), "fetched": 0, "failed": 0, "cached": len(pairs) - len(todo)}
    if not todo:
        return stats
    say(f"price history: {stats['cached']:,} already current in the database, "
        f"fetching {len(todo):,} from MarketBeat")
    session = session or shared_session()

    def one(pair):
        if deadline and time.time() > deadline:
            return None
        try:
            n = fetch(session, pair[0], pair[1])
        except Exception:
            n = 0
        time.sleep(PAUSE_SEC)
        return n

    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for n in pool.map(one, todo):
            done += 1
            if n:
                stats["fetched"] += 1
            elif n == 0:
                stats["failed"] += 1
            if done % 100 == 0 or done == len(todo):
                say(f"prices: {done:,}/{len(todo):,}")
    return stats


def coverage() -> dict:
    """How much history the database holds - the backtest's raw material."""
    with db.connect() as con:
        r = con.execute("SELECT COUNT(DISTINCT ticker), MIN(date), MAX(date), COUNT(*) "
                        "FROM price_history").fetchone()
    return {"stocks": r[0] or 0, "first": r[1], "last": r[2], "rows": r[3] or 0}
