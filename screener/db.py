import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime

from .config import DB_PATH, DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS universe (
    ticker TEXT PRIMARY KEY,
    company TEXT,
    exchange TEXT,
    country TEXT,
    url TEXT,
    updated TEXT
);
CREATE TABLE IF NOT EXISTS mb_bulk (
    ticker TEXT PRIMARY KEY,
    run_date TEXT,
    data TEXT
);
CREATE TABLE IF NOT EXISTS upgrades (
    ticker TEXT,
    date TEXT,
    firm TEXT,
    action TEXT,
    detail TEXT,
    PRIMARY KEY (ticker, date, firm)
);
CREATE TABLE IF NOT EXISTS prices (
    ticker TEXT PRIMARY KEY,
    run_date TEXT,
    data TEXT
);
CREATE TABLE IF NOT EXISTS mb_detail (
    ticker TEXT PRIMARY KEY,
    run_date TEXT,
    data TEXT
);
CREATE TABLE IF NOT EXISTS verify (
    ticker TEXT PRIMARY KEY,
    run_date TEXT,
    data TEXT
);
CREATE TABLE IF NOT EXISTS scores (
    run_date TEXT,
    ticker TEXT,
    rank INTEGER,
    score REAL,
    data TEXT,
    PRIMARY KEY (run_date, ticker)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Everything below is history: kept across runs and never wiped, so each run adds to it.

-- MarketBeat end-of-day closes, one row per stock per trading day.
CREATE TABLE IF NOT EXISTS price_history (
    ticker TEXT,
    date TEXT,
    close REAL,
    PRIMARY KEY (ticker, date)
);
-- When each stock's prices were last fetched, and how far they reach.
CREATE TABLE IF NOT EXISTS price_meta (
    ticker TEXT PRIMARY KEY,
    exchange TEXT,
    currency TEXT,
    first_date TEXT,
    last_date TEXT,
    fetched_at TEXT
);
-- MarketBeat's buy/hold/sell count as of a date. source 'daily' is what a run read that
-- day; 'official' is one of the forecast page's own 1-month/3-month/1-year-ago columns.
CREATE TABLE IF NOT EXISTS rating_snapshots (
    ticker TEXT,
    asof TEXT,
    source TEXT,
    buy INTEGER,
    hold INTEGER,
    sell INTEGER,
    analysts INTEGER,
    consensus_rating TEXT,
    target REAL,
    upside_pct REAL,
    price REAL,
    PRIMARY KEY (ticker, asof, source)
);
-- Every rating action MarketBeat lists on a stock's forecast page.
CREATE TABLE IF NOT EXISTS rating_actions (
    ticker TEXT,
    date TEXT,
    firm TEXT,
    action TEXT,
    rating_from TEXT,
    rating_to TEXT,
    target_from REAL,
    target_to REAL,
    first_seen TEXT,
    PRIMARY KEY (ticker, date, firm, action)
);
-- One row per pipeline run, and the ranked recommendations it produced.
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT,
    started TEXT,
    finished TEXT,
    params TEXT,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS recommendations (
    run_id INTEGER,
    run_date TEXT,
    ticker TEXT,
    rank INTEGER,
    score REAL,
    buy INTEGER,
    hold INTEGER,
    sell INTEGER,
    buy_share REAL,
    price REAL,
    target REAL,
    upside_pct REAL,
    exchange TEXT,
    PRIMARY KEY (run_id, ticker)
);
-- Backtests: the settings and headline numbers, every simulated trade, and the equity curve.
CREATE TABLE IF NOT EXISTS backtests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT,
    label TEXT,
    params TEXT,
    metrics TEXT
);
CREATE TABLE IF NOT EXISTS backtest_trades (
    backtest_id INTEGER,
    ticker TEXT,
    entry_date TEXT,
    entry_price REAL,
    exit_date TEXT,
    exit_price REAL,
    shares INTEGER,
    pnl REAL,
    return_pct REAL
);
CREATE TABLE IF NOT EXISTS backtest_equity (
    backtest_id INTEGER,
    date TEXT,
    strategy REAL,
    buy_hold REAL,
    benchmark REAL,
    PRIMARY KEY (backtest_id, date)
);
-- The Portfolio Planner: positions the user already holds, and every plan it produced.
CREATE TABLE IF NOT EXISTS holdings (
    ticker TEXT PRIMARY KEY,
    shares REAL,
    purchase_price REAL,
    purchase_date TEXT,
    updated TEXT,
    sell_limit REAL
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT,
    cash REAL,
    mode TEXT,
    rows TEXT
);
"""

# Tables a fresh run may clear. Everything else is history and survives every wipe.
WORKING_TABLES = ["universe", "mb_bulk", "upgrades", "prices", "mb_detail", "verify", "scores",
                  "meta"]

# Columns added after a table first shipped; CREATE TABLE IF NOT EXISTS leaves older
# databases without them.
ADDED_COLUMNS = [("holdings", "sell_limit", "REAL")]


def today() -> str:
    return date.today().isoformat()


@contextmanager
def connect():
    DATA_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for table, column, kind in ADDED_COLUMNS:
        if column not in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def wipe():
    """Clear the working tables so a run starts from today's data only.

    Price history, rating history, runs, recommendations, backtests, holdings and plans are
    kept: they are what later runs and the backtest build on.
    """
    with connect() as con:
        for table in WORKING_TABLES:
            con.execute(f"DELETE FROM {table}")


def upsert_json(con, table: str, ticker: str, data: dict, run_date: str | None = None):
    con.execute(
        f"INSERT INTO {table}(ticker, run_date, data) VALUES (?,?,?) "
        f"ON CONFLICT(ticker) DO UPDATE SET run_date=excluded.run_date, data=excluded.data",
        (ticker, run_date or today(), json.dumps(data, default=str)),
    )


def load_json(con, table: str, run_date: str | None = None) -> dict[str, dict]:
    q = f"SELECT ticker, data FROM {table}"
    args: tuple = ()
    if run_date:
        q += " WHERE run_date=?"
        args = (run_date,)
    return {r["ticker"]: json.loads(r["data"]) for r in con.execute(q, args)}


def done_tickers(con, table: str, run_date: str) -> set[str]:
    return {r["ticker"] for r in con.execute(f"SELECT ticker FROM {table} WHERE run_date=?", (run_date,))}


def upsert_universe(con, rows: list[dict]):
    con.executemany(
        "INSERT INTO universe(ticker, company, exchange, country, url, updated) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(ticker) DO UPDATE SET company=excluded.company, exchange=excluded.exchange, "
        "country=excluded.country, url=excluded.url, updated=excluded.updated",
        [(r["ticker"], r.get("company"), r.get("exchange"), r.get("country"), r.get("url"), today()) for r in rows],
    )


def set_meta(con, key: str, value):
    con.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, default=str)))


def get_meta(con, key: str, default=None):
    r = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r["value"]) if r else default


# ---------------------------------------------------------------- history helpers

def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def save_prices(con, ticker: str, closes: dict[str, float]):
    con.executemany(
        "INSERT INTO price_history(ticker, date, close) VALUES (?,?,?) "
        "ON CONFLICT(ticker, date) DO UPDATE SET close=excluded.close",
        [(ticker, d, float(c)) for d, c in closes.items()],
    )


def load_closes(con, ticker: str, start: str | None = None,
                end: str | None = None) -> list[tuple[str, float]]:
    q, args = "SELECT date, close FROM price_history WHERE ticker=?", [ticker]
    if start:
        q += " AND date>=?"
        args.append(start)
    if end:
        q += " AND date<=?"
        args.append(end)
    return [(r["date"], r["close"]) for r in con.execute(q + " ORDER BY date", args)]


def price_meta(con, ticker: str) -> dict | None:
    r = con.execute("SELECT * FROM price_meta WHERE ticker=?", (ticker,)).fetchone()
    return dict(r) if r else None


def set_price_meta(con, ticker: str, exchange: str | None, currency: str | None):
    first, last = con.execute("SELECT MIN(date), MAX(date) FROM price_history WHERE ticker=?",
                              (ticker,)).fetchone()
    con.execute(
        "INSERT INTO price_meta(ticker, exchange, currency, first_date, last_date, fetched_at) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET "
        "exchange=COALESCE(excluded.exchange, exchange), "
        "currency=COALESCE(excluded.currency, currency), first_date=excluded.first_date, "
        "last_date=excluded.last_date, fetched_at=excluded.fetched_at",
        (ticker, exchange, currency, first, last, now()),
    )


def save_snapshot(con, ticker: str, asof: str, source: str, counts: tuple[int, int, int],
                  consensus_rating: str | None = None, target: float | None = None,
                  upside_pct: float | None = None, price: float | None = None):
    buy, hold, sell = counts
    con.execute(
        "INSERT INTO rating_snapshots(ticker, asof, source, buy, hold, sell, analysts, "
        "consensus_rating, target, upside_pct, price) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(ticker, asof, source) DO UPDATE SET buy=excluded.buy, hold=excluded.hold, "
        "sell=excluded.sell, analysts=excluded.analysts, "
        "consensus_rating=excluded.consensus_rating, target=excluded.target, "
        "upside_pct=excluded.upside_pct, price=excluded.price",
        (ticker, asof, source, buy, hold, sell, buy + hold + sell, consensus_rating, target,
         upside_pct, price),
    )


def save_actions(con, ticker: str, actions: list[dict]):
    con.executemany(
        "INSERT INTO rating_actions(ticker, date, firm, action, rating_from, rating_to, "
        "target_from, target_to, first_seen) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(ticker, date, firm, action) DO UPDATE SET rating_from=excluded.rating_from, "
        "rating_to=excluded.rating_to, target_from=excluded.target_from, "
        "target_to=excluded.target_to",
        [(ticker, a["date"], a["firm"], a.get("action") or "", a.get("rating_from"),
          a.get("rating_to"), a.get("target_from"), a.get("target_to"), today())
         for a in actions if a.get("date") and a.get("firm")],
    )


def start_run(con, kind: str, params: dict | None = None) -> int:
    cur = con.execute("INSERT INTO runs(kind, started, params) VALUES (?,?,?)",
                      (kind, now(), json.dumps(params or {}, default=str)))
    return int(cur.lastrowid)


def finish_run(con, run_id: int, summary: dict):
    con.execute("UPDATE runs SET finished=?, summary=? WHERE id=?",
                (now(), json.dumps(summary, default=str), run_id))
