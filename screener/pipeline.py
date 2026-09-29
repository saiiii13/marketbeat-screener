"""The Yahoo-backed pipeline: universe, discovery, detail, prices.

Replaces the MarketBeat stages. The database layout is unchanged, so scoring, the
dashboard, explore, simulate and the menu all keep working on the rows written here.
"""
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import db, notes, yahoo

DETAIL_WORKERS = 6
PRICE_WORKERS = 8


def sweep(min_volume: int = 0, cap: int | None = None, world: bool = False,
          regions: list[str] | None = None) -> int:
    """Read the market from Yahoo: US by default, every region when asked."""
    run_date = db.today()
    notes.stage("Stage 1 — reading the market from Yahoo Finance")
    t0 = time.time()
    if world or regions:
        rows = yahoo.world(regions=regions, cap=cap)
    else:
        rows = yahoo.universe(min_volume=min_volume, cap=cap)
    with db.connect() as con:
        db.upsert_universe(con, rows)
        for r in rows:
            db.upsert_json(con, "mb_bulk", r["ticker"], r, run_date)
        total = con.execute("SELECT COUNT(*) FROM universe").fetchone()[0]
    notes.line(f"Read **{len(rows):,} stocks** from Yahoo's screener in "
               f"{time.time() - t0:.0f}s.")
    print(f"[sweep] {len(rows):,} stocks ({total:,} in the store)")
    return len(rows)


def discover(top_n: int | None = None) -> list[str]:
    """Let Yahoo's curated screens choose the candidates."""
    notes.stage("Stage 2 — letting Yahoo's screens find the candidates")
    hits = yahoo.discover()
    if not hits:
        return []
    ordered = sorted(hits.values(),
                     key=lambda r: (-r["net_weight"], -len(r["sources"]), r["ticker"]))
    picked = [r["ticker"] for r in ordered][:top_n or len(ordered)]

    run_date = db.today()
    with db.connect() as con:
        db.upsert_universe(con, list(hits.values()))
        for t, row in hits.items():
            db.upsert_json(con, "mb_bulk", t, row, run_date)
        db.set_meta(con, "discovered",
                    {k: {"sources": v["sources"], "weight": v["weight"],
                         "warnings": v["warnings"]} for k, v in hits.items()})
        db.set_meta(con, "finalists", picked)
        db.set_meta(con, "prefiltered", len(hits))

    multi = sum(1 for r in hits.values() if len(r["sources"]) > 1)
    notes.line(f"Yahoo's screens surfaced **{len(hits):,} stocks**; {multi:,} appear on "
               f"more than one. Taking the top {len(picked):,}.")
    print(f"[discover] {len(hits):,} unique, {multi:,} on multiple screens, "
          f"{len(picked):,} queued")
    return picked


def details(tickers: list[str], deadline: float | None = None) -> int:
    """Analyst consensus, ownership and short interest for each candidate."""
    run_date = db.today()
    with db.connect() as con:
        already = db.done_tickers(con, "mb_detail", run_date)
    todo = [t for t in tickers if t not in already]
    notes.stage("Stage 3 — analyst data for every candidate")
    notes.line(f"Fetching **{len(todo):,}** stocks from Yahoo "
               f"({len(already):,} already done today).")
    if not todo:
        return 0

    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        futures = {pool.submit(yahoo.detail, t): t for t in todo}
        for fut in as_completed(futures):
            ticker = futures[fut]
            if deadline and time.time() > deadline:
                for f in futures:
                    f.cancel()
                break
            try:
                data = fut.result()
            except Exception as exc:
                data = {"errors": {"detail": type(exc).__name__}}
            with db.connect() as con:
                db.upsert_json(con, "mb_detail", ticker, data, run_date)
            done += 1
            if done % 25 == 0 or done == len(todo):
                rate = (time.time() - t0) / done
                left = len(todo) - done
                print(f"[detail] {done}/{len(todo)} {ticker}: "
                      f"{data.get('consensus_rating')} "
                      f"({data.get('analyst_count')} analysts)  "
                      f"~{rate * left / 60:.0f} min left")
                notes.progress(done, len(todo), "Stocks analysed", t0)
    notes.line(f"Analysed **{done:,}** stocks in {(time.time() - t0) / 60:.1f} min.")
    return done


def prices(tickers: list[str], deadline: float | None = None) -> dict:
    """A year of daily OHLCV per stock, and the metrics the score needs."""
    from .enrich import metrics
    run_date = db.today()
    with db.connect() as con:
        cached = {t: d for t, d in db.load_json(con, "prices", run_date).items()
                  if not d.get("missing")}
    todo = [t for t in tickers if t not in cached]
    notes.stage("Stage 4 — price history")
    notes.line(f"Downloading **{len(todo):,}** price histories from Yahoo.")

    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=PRICE_WORKERS) as pool:
        futures = {pool.submit(yahoo.history, t): t for t in todo}
        for fut in as_completed(futures):
            ticker = futures[fut]
            if deadline and time.time() > deadline:
                break
            try:
                df = fut.result()
                row = metrics(df) if df is not None and not df.empty else None
            except Exception:
                row = None
            with db.connect() as con:
                db.upsert_json(con, "prices", ticker, row or {"missing": True}, run_date)
            done += 1
            if done % 50 == 0 or done == len(todo):
                rate = (time.time() - t0) / done
                print(f"[prices] {done}/{len(todo)}  ~{rate * (len(todo) - done) / 60:.0f} min left")
                notes.progress(done, len(todo), "Prices fetched", t0)

    with db.connect() as con:
        have = db.load_json(con, "prices", run_date)
    priced = {t: have[t] for t in tickers if t in have and not have[t].get("missing")}
    notes.line(f"**{len(priced):,}** of {len(tickers):,} have usable price history.")
    print(f"[prices] {len(priced):,}/{len(tickers):,} priced in {(time.time() - t0) / 60:.1f} min")
    return priced
