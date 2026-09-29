"""Stage D: every MarketBeat feature for each finalist (overview/MarketRank, forecast, earnings,
short interest, insider trades, institutional ownership).

Pages are fetched over HTTP with the cookies from the logged-in Chrome profile (a few threads,
polite delay), parsed, and checkpointed per ticker so the stage is resumable."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import local

import requests

from . import db, notes
from .browser import Session, read_scrolled
from .config import BASE_URL, DETAIL_DELAY_SEC, DETAIL_WORKERS, USER_AGENT, LIVE_TABS
from .parse import (
    parse_overview, parse_forecast, parse_earnings, parse_short_interest, parse_insider,
    parse_institutional, parse_financials, parse_dividend, parse_competitors,
    parse_sec_filings, parse_options, parse_news,
)

# Every tab MarketBeat publishes for a stock, in the order the site lists them.
PAGES = {
    "": parse_overview,
    "forecast": parse_forecast,
    "earnings": parse_earnings,
    "financials": parse_financials,
    "dividend": parse_dividend,
    "insider-trades": parse_insider,
    "institutional-ownership": parse_institutional,
    "short-interest": parse_short_interest,
    "competitors-and-alternatives": parse_competitors,
    "sec-filings": parse_sec_filings,
    "options": parse_options,
    "news": parse_news,
}

_tls = local()


def marketbeat_cookies(sess: Session) -> list[dict]:
    try:
        return [c for c in sess.ctx.cookies() if "marketbeat" in c.get("domain", "")]
    except Exception:
        return []


def browser_cookies() -> list[dict]:
    """Read MarketBeat cookies out of the persistent Chrome profile (opens it briefly, headless)."""
    try:
        with Session(headless=True) as s:
            s.page()
            return marketbeat_cookies(s)
    except Exception as e:
        print(f"[detail] could not read browser cookies ({e}); fetching as a logged-out visitor")
        return []


def make_session(cookies: list[dict]) -> requests.Session:
    sess = requests.Session()
    sess.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"})
    for c in cookies:
        sess.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
    return sess


def get_html(sess: requests.Session, url: str, retries: int = 3) -> str:
    last = None
    for attempt in range(retries):
        try:
            r = sess.get(url, timeout=45)
            if r.ok:
                return r.text
            last = f"HTTP {r.status_code}"
            if r.status_code in (404, 410):
                break
            if r.status_code in (429, 503):
                time.sleep(10 * (attempt + 1))
        except requests.RequestException as e:
            last = repr(e)
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET {url} failed: {last}")


def stock_base_url(ticker: str, universe_row: dict | None) -> str:
    url = (universe_row or {}).get("url") or ""
    if url.startswith("http"):
        return url.rstrip("/") + "/"
    exch = (universe_row or {}).get("exchange") or "NASDAQ"
    return f"{BASE_URL}/stocks/{exch}/{ticker}/"


def fetch_ticker(cookies: list[dict], ticker: str, universe_row: dict | None) -> dict:
    sess = getattr(_tls, "sess", None)
    if sess is None:
        sess = _tls.sess = make_session(cookies)
    base = stock_base_url(ticker, universe_row)
    out: dict = {"source_url": base, "errors": {}}
    for slug, parser in PAGES.items():
        url = base + (slug + "/" if slug else "")
        try:
            parsed = parser(get_html(sess, url))
            for k, v in parsed.items():
                if v is not None or k not in out:
                    out[k] = v
        except Exception as e:
            out["errors"][slug or "overview"] = str(e)[:200]
        time.sleep(DETAIL_DELAY_SEC)
    return out


def run(tickers: list[str], headless: bool = True,
        window: tuple[int, int, int, int] | None = None) -> int:
    """window: place a visible Chrome window there and walk it through each stock's MarketBeat
    page as that stock is read, so the work is watchable. Purely visual - the data still comes
    over HTTP, which is what keeps this stage fast."""
    run_date = db.today()
    with db.connect() as con:
        done = db.done_tickers(con, "mb_detail", run_date)
        uni = {r["ticker"]: dict(r) for r in con.execute("SELECT * FROM universe")}
    todo = [t for t in tickers if t not in done]
    print(f"[detail] {len(tickers)} finalists, {len(done)} already fetched today, {len(todo)} to go")
    if not todo:
        return len(tickers)

    notes.stage("Stage 3 — every MarketBeat page for the finalists")
    notes.line(f"Reading the full MarketBeat profile (ratings, earnings, short interest, "
               f"insiders, institutions, news) for **{len(todo)}** finalists.")
    spotlight = Session(headless=False, window=window) if window else None
    if spotlight:
        spotlight.__enter__()
        page = spotlight.page()
        page.set_default_timeout(15_000)
        cookies = marketbeat_cookies(spotlight)
        print("[detail] follow-along window open; it moves to each stock as it is read")
    else:
        page = None
        cookies = browser_cookies()
    print(f"[detail] using {len(cookies)} MarketBeat cookies from the Chrome profile, {DETAIL_WORKERS} workers")
    t0 = time.time()
    try:
        _detail_loop(todo, cookies, uni, run_date, t0, page)
    finally:
        if spotlight:
            try:
                spotlight.__exit__(None, None, None)
            except Exception:
                pass
    return len(tickers)


def _detail_loop(todo, cookies, uni, run_date, t0, page):
    with ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as pool:
        futures = {pool.submit(fetch_ticker, cookies, t, uni.get(t)): t for t in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            t = futures[fut]
            try:
                data = fut.result()
            except Exception as e:
                data = {"errors": {"all": str(e)[:200]}}
            with db.connect() as con:
                db.upsert_json(con, "mb_detail", t, data, run_date)
            errs = f" errors={list(data['errors'])}" if data.get("errors") else ""
            rate = (time.time() - t0) / i
            print(f"[detail] {i}/{len(todo)} {t}: analysts={data.get('analyst_count')} MR={data.get('marketrank_percentile')} "
                  f"short%={data.get('short_pct_float')}{errs}  (~{rate * (len(todo) - i) / 60:.0f} min left)")
            if page is not None:
                try:  # visual only - never let a slow page load hold up the data
                    page.goto(stock_base_url(t, uni.get(t)), wait_until="commit", timeout=8000)
                except Exception:
                    pass
            if i % 10 == 0 or i == len(todo):
                notes.progress(i, len(todo), "Profiles read", t0)
                notes.bullet(f"Latest: **{t}** — {data.get('consensus_rating') or 'no consensus'}, "
                             f"{int(data['analyst_count']) if data.get('analyst_count') else 0} analysts, "
                             f"MarketRank {data.get('marketrank_percentile') or '–'}")


def run_live(tickers: list[str], window: tuple[int, int, int, int] | None = None,
             deadline: float | None = None, tabs: int = LIVE_TABS) -> int:
    """Read stocks by actually browsing MarketBeat: open each tab in Chrome, scroll it to
    the bottom, parse what the browser is showing.

    Several Chrome tabs work in parallel so an overnight budget covers more stocks, but
    each page is still opened and scrolled rather than fetched behind the scenes.

    deadline: a time.time() value to stop at. Whatever has been read by then is kept and
    the run moves on to scoring, so an overnight run always produces a result.
    """
    run_date = db.today()
    with db.connect() as con:
        uni = {r["ticker"]: dict(r) for r in con.execute("SELECT * FROM universe")}

    notes.stage("Stage 3 — reading every MarketBeat tab, live")
    budget = f", stopping after {(deadline - time.time()) / 3600:.1f}h" if deadline else ""
    notes.line(f"Browsing up to **{len(tickers):,}** stocks across {len(PAGES)} tabs each, "
               f"{tabs} at a time, scrolling every page{budget}.")

    session = Session(headless=False, window=window)
    session.__enter__()
    done = 0
    lock = threading.Lock()
    queue: list[str] = list(tickers)
    t0 = time.time()

    def worker(page):
        nonlocal done
        while True:
            if deadline and time.time() > deadline:
                return
            with lock:
                if not queue:
                    return
                ticker = queue.pop(0)
            base = stock_base_url(ticker, uni.get(ticker))
            data: dict = {"source_url": base, "errors": {}}
            for slug, parser in PAGES.items():
                if deadline and time.time() > deadline:
                    break
                url = base + (slug + "/" if slug else "")
                try:
                    for key, value in parser(read_scrolled(page, url)).items():
                        if value is not None or key not in data:
                            data[key] = value
                except Exception as exc:
                    data["errors"][slug or "overview"] = type(exc).__name__
            with lock:
                with db.connect() as con:
                    db.upsert_json(con, "mb_detail", ticker, data, run_date)
                done += 1
                count = done
            rate = (time.time() - t0) / max(count, 1)
            left = len(queue)
            eta = rate * left / 3600
            print(f"[detail] {count}/{len(tickers)} {ticker}: analysts={data.get('analyst_count')} "
                  f"MR={data.get('marketrank_percentile')}  ({left:,} left, ~{eta:.1f}h)")
            if count % 10 == 0:
                notes.progress(count, len(tickers), "Stocks browsed", t0)
                notes.bullet(f"Latest: **{ticker}** — {data.get('consensus_rating') or 'no consensus'}, "
                             f"{int(data['analyst_count']) if data.get('analyst_count') else 0} analysts")

    try:
        pages = [session.page()]
        for _ in range(max(0, tabs - 1)):
            extra = session.ctx.new_page()
            extra.set_default_timeout(60_000)
            pages.append(extra)
        for p in pages:
            p.set_default_timeout(60_000)
        threads = [threading.Thread(target=worker, args=(p,), daemon=True) for p in pages]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        try:
            session.__exit__(None, None, None)
        except Exception:
            pass

    elapsed = (time.time() - t0) / 3600
    stopped_early = bool(deadline and queue)
    notes.line(f"Browsed **{done:,}** stocks in {elapsed:.1f}h"
               + (f"; stopped on the time budget with {len(queue):,} left" if stopped_early else ""))
    print(f"[detail] browsed {done:,} stocks in {elapsed:.1f}h"
          + (f" (time budget reached, {len(queue):,} unread)" if stopped_early else ""))
    return done
