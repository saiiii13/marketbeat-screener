import json
import sqlite3
from contextlib import contextmanager
from datetime import date

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
"""


def today() -> str:
    return date.today().isoformat()


@contextmanager
def connect():
    DATA_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    try:
        yield con
        con.commit()
    finally:
        con.close()


def wipe():
    """Delete the whole store so a run starts with nothing remembered."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    for leftover in DATA_DIR.glob("screener.db-*"):  # sqlite wal/shm files
        leftover.unlink()


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
