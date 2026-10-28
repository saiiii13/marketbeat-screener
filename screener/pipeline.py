"""The MarketBeat pipeline: candidates, analyst data, prices, scores.

MarketBeat is the only source. Its research lists (upgrades, top rated, breakouts, sectors -
see discover.SOURCES) supply the candidates. For each candidate, the overview page gives the
company profile and the link to its price chart, and the forecast page gives the buy/hold/
sell count, the price target and the rating history.

Everything read is written to data/screener.db. The working tables (mb_bulk, mb_detail,
prices, scores) describe today; the history tables (price_history, rating_snapshots,
rating_actions, runs, recommendations) are only ever added to, so later runs reuse what is
stored and the backtest has more to work with each day.
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import db, discover as mb_lists, notes, prices, ratings
from .config import BASE_URL, WIPE_ON_RUN
from .parse import parse_forecast, parse_overview
from .pull_detail import get_html

DETAIL_WORKERS = 4
PAUSE_SEC = 0.3
BENCHMARK = "SPY"
# MarketBeat redirects a wrong exchange to the right page (NASDAQ/CHG -> LON/CHG); the page's
# canonical link says where it really lives.
CANONICAL_RE = re.compile(r'<link[^>]+rel="canonical"[^>]+href="https://www\.marketbeat\.com/stocks/'
                          r'([A-Z]+)/([^/"]+)/"')
FALLBACK_EXCHANGES = ["NASDAQ", "NYSE", "NYSEAMERICAN", "NYSEARCA", "OTCMKTS", "BATS"]
# Overview fields carried onto the stock's bulk row, where the ranking screens look first.
BULK_FIELDS = ["company", "sector", "price", "market_cap", "avg_volume", "beta", "currency",
               "is_fund", "consensus_rating"]


def sweep(cap: int | None = None) -> int:
    """Stage 1: collect the candidates from MarketBeat's own research lists."""
    hits = mb_lists.run(prices.shared_session())
    rows = sorted(hits.values(), key=lambda r: (-r["weight"], r["ticker"]))[:cap or len(hits)]
    run_date = db.today()
    with db.connect() as con:
        held = [r["ticker"] for r in con.execute("SELECT ticker FROM holdings")]
        for r in rows:
            db.upsert_json(con, "mb_bulk", r["ticker"], {
                "ticker": r["ticker"], "exchange": r["exchange"],
                "url": f"{BASE_URL}/stocks/{r['exchange']}/{r['ticker']}/",
                "sources": r["sources"], "list_weight": r["weight"],
                "list_warnings": r.get("warnings", [])}, run_date)
        # Stocks the user holds are always read, so the planner's sell advice is current.
        for t in held:
            if t not in hits:
                ex = prices.exchange_of(t)
                db.upsert_json(con, "mb_bulk", t, {"ticker": t, "exchange": ex, "sources": ["held"],
                                                   "list_weight": 0.0}, run_date)
                db.upsert_universe(con, [{"ticker": t, "exchange": ex,
                                          "url": f"{BASE_URL}/stocks/{ex or 'NASDAQ'}/{t}/"}])
        total = con.execute("SELECT COUNT(*) FROM mb_bulk").fetchone()[0]
    print(f"[sweep] {len(rows):,} candidates from MarketBeat's lists ({total:,} in the store)")
    return total


def discover(top_n: int | None = None) -> list[str]:
    """Stage 2: order the candidates by how many MarketBeat lists flagged them."""
    with db.connect() as con:
        bulk = db.load_json(con, "mb_bulk", db.today())
    if not bulk:
        sweep()
        with db.connect() as con:
            bulk = db.load_json(con, "mb_bulk", db.today())
    notes.stage("Stage 2 — MarketBeat's lists choose the candidates")
    ordered = sorted(bulk.values(), key=lambda r: (-(r.get("list_weight") or 0)
                                                   + 0.5 * len(r.get("list_warnings") or []),
                                                   r["ticker"]))
    picked = [r["ticker"] for r in ordered][:top_n or len(ordered)]
    with db.connect() as con:
        db.set_meta(con, "finalists", picked)
        db.set_meta(con, "prefiltered", len(bulk))
    notes.line(f"**{len(picked):,}** candidates go forward.")
    print(f"[discover] {len(picked):,} candidates queued")
    return picked


def _page(session, ticker: str, exchange: str | None) -> tuple[str, str]:
    """The stock's overview page, trying other exchanges if the stored one is wrong."""
    tried = []
    for ex in [exchange] + [e for e in FALLBACK_EXCHANGES if e != exchange]:
        if not ex:
            continue
        url = f"{BASE_URL}/stocks/{ex}/{ticker}/"
        tried.append(ex)
        try:
            return get_html(session, url, retries=2), ex
        except RuntimeError as exc:
            if "404" not in str(exc) and "410" not in str(exc):
                raise
    raise RuntimeError(f"no MarketBeat page for {ticker} (tried {', '.join(tried)})")


def read_stock(session, ticker: str, exchange: str | None, refetch_prices: bool = False) -> dict:
    """Overview, price chart (if the stored history is behind, or always with refetch_prices)
    and forecast."""
    html, exchange = _page(session, ticker, exchange)
    canonical = CANONICAL_RE.search(html)
    if canonical and canonical.group(2).upper() == ticker.upper():
        exchange = canonical.group(1)
    base = f"{BASE_URL}/stocks/{exchange}/{ticker}/"
    out: dict = {"ticker": ticker, "exchange": exchange, "source_url": base, "errors": {}}
    for k, v in parse_overview(html).items():
        if v is not None:
            out[k] = v
    if refetch_prices or not prices.is_fresh(ticker):
        try:
            df, currency = prices.chart_from_page(session, html)
            closes = prices.clean(df)
            if closes:
                prices.store(ticker, closes, exchange, currency)
            out["price_days_fetched"] = len(closes)
        except Exception as exc:
            out["errors"]["chart"] = type(exc).__name__
    try:
        forecast = parse_forecast(get_html(session, base + "forecast/"))
        for k, v in forecast.items():
            if v is not None:
                out[k] = v
    except Exception as exc:
        out["errors"]["forecast"] = str(exc)[:200]
    return out


def save_stock(con, data: dict, run_date: str):
    ticker = data["ticker"]
    ratings.record(con, ticker, data, run_date, data.get("price"))
    stored = {k: v for k, v in data.items() if k not in ("ratings_history", "official_snapshots")}
    stored["ratings_history"] = (data.get("ratings_history") or [])[:60]
    db.upsert_json(con, "mb_detail", ticker, stored, run_date)
    bulk = db.load_json(con, "mb_bulk").get(ticker) or {"ticker": ticker}
    bulk.update({k: data[k] for k in BULK_FIELDS if data.get(k) is not None})
    bulk.update({"exchange": data["exchange"], "url": data["source_url"],
                 "consensus_target": data.get("consensus_target"),
                 "upside_pct": data.get("upside_pct"), "analyst_count": data.get("analyst_count")})
    db.upsert_json(con, "mb_bulk", ticker, bulk, run_date)
    db.upsert_universe(con, [{"ticker": ticker, "company": data.get("company"),
                              "exchange": data["exchange"], "url": data["source_url"]}])


def details(tickers: list[str], deadline: float | None = None, say=print,
            fresh: bool = WIPE_ON_RUN) -> int:
    """Stage 3: read every candidate on MarketBeat. A fresh run reads every stock and its price
    chart again; otherwise stocks already read today are skipped."""
    run_date = db.today()
    with db.connect() as con:
        already = set() if fresh else db.done_tickers(con, "mb_detail", run_date)
        exchange = {r["ticker"]: r["exchange"] for r in con.execute("SELECT ticker, exchange FROM universe")}
    todo = [t for t in tickers if t not in already]
    notes.stage("Stage 3 — each candidate's MarketBeat overview, chart and forecast")
    notes.line(f"Reading **{len(todo):,}** stocks on MarketBeat ({len(already):,} already "
               f"read today).")
    say(f"reading {len(todo):,} stocks on MarketBeat ({len(already):,} already read today)")
    if not todo:
        return 0

    session = prices.shared_session()

    def one(ticker):
        if deadline and time.time() > deadline:
            return ticker, None
        try:
            data = read_stock(session, ticker, exchange.get(ticker), refetch_prices=fresh)
        except Exception as exc:
            data = {"ticker": ticker, "error": str(exc)[:200]}
        time.sleep(PAUSE_SEC)
        return ticker, data

    t0 = time.time()
    done = failed = 0
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        futures = [pool.submit(one, t) for t in todo]
        for fut in as_completed(futures):
            ticker, data = fut.result()
            if data is None:
                continue
            done += 1
            if data.get("error"):
                failed += 1
            else:
                with db.connect() as con:
                    save_stock(con, data, run_date)
            if done % 100 == 0 or done == len(todo):
                rate = (time.time() - t0) / done
                say(f"MarketBeat pages: {done:,}/{len(todo):,}  ~{rate * (len(todo) - done) / 60:.0f} min left")
                notes.progress(done, len(todo), "Stocks read", t0)
    notes.line(f"Read **{done - failed:,}** stocks in {(time.time() - t0) / 60:.1f} min"
               + (f"; {failed} had no readable page." if failed else "."))
    return done - failed


def prices_stage(tickers: list[str], deadline: float | None = None) -> dict:
    """Stage 4: price metrics from the stored history (fetching only what is stale)."""
    from . import enrich
    priced = enrich.run(tickers)
    with db.connect() as con:
        db.set_meta(con, "with_prices", len(priced))
    return priced


def analyze(top: int = 20, run_id: int | None = None):
    """Score the finalists, store the ranking and write the dashboard.

    Shared by `python run.py analyze` and the menu's refresh. Also stores the
    buy/hold/sell-ratio ranking as this run's recommendations.
    """
    import json
    from . import mb_ratio
    from .report import render
    from .score import final_scores
    with db.connect() as con:
        bulk = db.load_json(con, "mb_bulk")
        price_rows = db.load_json(con, "prices")
        detail = db.load_json(con, "mb_detail")
        ups: dict[str, list[dict]] = {}
        for r in con.execute("SELECT * FROM upgrades"):
            ups.setdefault(r["ticker"], []).append(dict(r))
        finalists = db.get_meta(con, "finalists", [])
        universe_n = con.execute("SELECT COUNT(*) FROM universe").fetchone()[0]
    if not finalists:
        return None, None
    finalists = [t for t in finalists if t in detail]
    mb_ratio.store_ranking(detail, price_rows, run_id)
    df = final_scores(finalists, bulk, price_rows, detail, ups)
    if df.empty:
        return df, None
    run_date = db.today()
    with db.connect() as con:
        con.execute("DELETE FROM scores WHERE run_date=?", (run_date,))
        for _, r in df.iterrows():
            con.execute("INSERT INTO scores(run_date,ticker,rank,score,data) VALUES (?,?,?,?,?)",
                        (run_date, r["ticker"], int(r["rank"]), float(r["score"]),
                         json.dumps({"components": r["components"], "flags": r["flags"],
                                     "excluded": r["excluded"]}, default=str)))
        stats = {
            "universe": universe_n,
            "prefiltered": db.get_meta(con, "prefiltered", 0),
            "with_prices": db.get_meta(con, "with_prices", 0),
            "finalists": len(finalists),
            "ranked": int((df["excluded"] == "").sum()),
        }
        verdicts = db.load_json(con, "verify", run_date)
    path = render(df, stats, top, run_date, verdicts)
    return df, path


def clear_old_days():
    """Drop working rows from earlier days; rows read today are kept so a second run the same
    day resumes instead of starting over. History tables are never touched."""
    today = db.today()
    with db.connect() as con:
        for table in ("mb_bulk", "prices", "mb_detail", "verify"):
            con.execute(f"DELETE FROM {table} WHERE run_date<>?", (today,))
        con.execute("DELETE FROM universe WHERE updated<>?", (today,))


def refresh(say=print, deadline: float | None = None, calibrate_sample: int = 200,
            fresh: bool = WIPE_ON_RUN) -> dict:
    """The whole MarketBeat run: lists, pages, prices, odds, scores, recommendations. A fresh
    run starts again from nothing; otherwise a second run the same day resumes."""
    from . import calibrate
    t0 = time.time()
    with db.connect() as con:
        run_id = db.start_run(con, "refresh")
    if fresh:
        db.wipe()
        say("starting again: every stock and price is read from MarketBeat afresh")
    else:
        clear_old_days()
    say("reading MarketBeat's research lists for candidates...")
    sweep()
    picked = discover()
    with db.connect() as con:
        extra = [r["ticker"] for r in con.execute(
            "SELECT DISTINCT ticker FROM recommendations WHERE run_id=(SELECT MAX(run_id) FROM recommendations)")]
    extra = [t for t in extra if t not in set(picked)]
    say(f"{len(picked):,} candidates from MarketBeat's lists"
        + (f", plus {len(extra)} from the last recommendations" if extra else ""))
    read = details(picked + extra, deadline=deadline, say=say, fresh=fresh)
    with db.connect() as con:
        have = list(db.load_json(con, "mb_detail", db.today()))
        db.set_meta(con, "finalists", have)
    # The S&P 500 fund, for the backtest's and the detail view's market comparison.
    prices.ensure([(BENCHMARK, "NYSEARCA")], say=say, force=fresh)
    say("computing price measures from the stored history...")
    priced = prices_stage(have, deadline=deadline)
    say(f"{len(priced):,} stocks priced")
    say("measuring historical odds from the stored prices...")
    calibrate.build(sample=calibrate_sample)
    say("scoring, ranking and writing the dashboard...")
    df, path = analyze(run_id=run_id)
    ranked = 0 if df is None or df.empty else int((df["rank"] > 0).sum())
    cov = prices.coverage()
    summary = {"candidates": len(picked) + len(extra), "read_today": read, "priced": len(priced),
               "scored": ranked, "dashboard": path, "minutes": round((time.time() - t0) / 60, 1),
               "price_history": cov}
    with db.connect() as con:
        db.finish_run(con, run_id, summary)
    say(f"done in {summary['minutes']} min — {ranked} clear every filter; price history now "
        f"{cov['stocks']:,} stocks, {cov['first']} to {cov['last']}")
    return summary
