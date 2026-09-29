"""Browsing MarketBeat concurrently, the honest way.

Playwright's synchronous API is single-threaded: driving several pages from Python threads
fails immediately (every navigation raises), which looks like enormous speed until the
errors are read. The async API is the supported way to have several pages in flight at
once, so that is what this module uses.

Each page is still opened in a real Chrome window and scrolled to the bottom before being
parsed. Concurrency only means several of them happen at the same time.
"""
import asyncio
import time

from playwright.async_api import async_playwright

from . import db, notes
from .config import PROFILE_DIR, USER_AGENT

SCROLL_STEP = 900
SCROLL_PAUSE_MS = 70
SETTLE_MS = 400
NAV_TIMEOUT_MS = 45_000
# A MarketBeat stock page is a few screens tall; 60 steps of 900px covers ~54,000px, far
# more than any of them. Anything still scrolling past that is a page that grows as it is
# read, not a page being read.
MAX_SCROLL_STEPS = 60
MAX_PAGE_SECONDS = 20
# When MarketBeat starts refusing, back off instead of hammering it.
ERROR_WINDOW = 20          # judge the most recent N page attempts
ERROR_RATE_TRIGGER = 0.5   # this share failing means we are being blocked
BACKOFF_START_SEC = 30
BACKOFF_MAX_SEC = 600


async def read_scrolled(page, url: str, deadline: float | None = None) -> str:
    """Open a page and scroll it, with hard limits on how long that may take.

    Pages that append content as you scroll (news feeds, filing lists) grow faster than
    the scroll advances, so a plain `while position < height` loop never terminates. Three
    guards prevent that: a step cap, a wall-clock cap per page, and the run's own deadline
    checked on every iteration rather than only between pages.
    """
    await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    await page.wait_for_timeout(SETTLE_MS)
    height = await page.evaluate("() => document.body.scrollHeight")
    position = 0
    started = time.time()
    for _ in range(MAX_SCROLL_STEPS):
        if position >= height:
            break
        if time.time() - started > MAX_PAGE_SECONDS:
            break
        if deadline and time.time() > deadline:
            break
        position += SCROLL_STEP
        await page.evaluate("y => window.scrollTo(0, y)", position)
        await page.wait_for_timeout(SCROLL_PAUSE_MS)
        height = await page.evaluate("() => document.body.scrollHeight")
    await page.evaluate("() => window.scrollTo(0, 0)")
    return await page.content()


def _health_check(state: dict, seconds_left: float, deadline: float | None,
                  count: int, total: int):
    """Say loudly when the run will not finish in the time it has.

    The previous run spent fifteen hours quietly failing because nothing compared its own
    estimate against its budget. A warning in the log and the notes makes that visible
    without anyone having to watch it.
    """
    if not deadline:
        return
    budget_left = deadline - time.time()
    if budget_left <= 0 or seconds_left <= budget_left:
        state["warned"] = False
        return
    if state.get("warned"):
        return
    state["warned"] = True
    reach = int(count + (budget_left / max(seconds_left, 1)) * (total - count))
    msg = (f"at this pace only about {reach:,} of {total:,} stocks will be read before the "
           f"{budget_left / 3600:.1f}h left runs out (needs {seconds_left / 3600:.1f}h)")
    print(f"[detail] WARNING: {msg}")
    notes.line(f"**Pace warning** — {msg}")


def _record(state: dict, ok: bool):
    """Keep a rolling record of recent page attempts, and open a backoff when the
    failure rate says MarketBeat has stopped serving us."""
    recent = state["recent"]
    recent.append(ok)
    if len(recent) > ERROR_WINDOW:
        recent.pop(0)
    if ok:
        state["backoff"] = 0
        return
    if len(recent) < ERROR_WINDOW:
        return
    failing = recent.count(False) / len(recent)
    if failing >= ERROR_RATE_TRIGGER and time.time() >= state["resume_at"]:
        state["backoff"] = min(max(state["backoff"] * 2, BACKOFF_START_SEC), BACKOFF_MAX_SEC)
        state["resume_at"] = time.time() + state["backoff"]
        msg = (f"{failing:.0%} of the last {ERROR_WINDOW} pages failed — "
               f"pausing {state['backoff']:.0f}s before trying again")
        print(f"[detail] {msg}")
        notes.line(f"MarketBeat is refusing pages: {msg}")


async def _wait_out_backoff(state: dict, deadline: float | None):
    while time.time() < state["resume_at"]:
        if deadline and time.time() > deadline:
            return
        await asyncio.sleep(min(2.0, state["resume_at"] - time.time()))


async def _worker(page, queue: asyncio.Queue, pages_map: dict, uni: dict, run_date: str,
                  state: dict, total: int, deadline: float | None):
    from .pull_detail import stock_base_url

    while True:
        if deadline and time.time() > deadline:
            return
        try:
            ticker = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        base = stock_base_url(ticker, uni.get(ticker))
        data: dict = {"source_url": base, "errors": {}}
        for slug, parser in pages_map.items():
            if deadline and time.time() > deadline:
                break
            await _wait_out_backoff(state, deadline)
            url = base + (slug + "/" if slug else "")
            try:
                html = await read_scrolled(page, url, deadline)
                for key, value in parser(html).items():
                    if value is not None or key not in data:
                        data[key] = value
                _record(state, ok=True)
            except Exception as exc:
                data["errors"][slug or "overview"] = type(exc).__name__
                _record(state, ok=False)

        with db.connect() as con:
            db.upsert_json(con, "mb_detail", ticker, data, run_date)
        state["done"] += 1
        count = state["done"]
        rate = (time.time() - state["t0"]) / count
        left = queue.qsize()
        bad = len(data["errors"])
        print(f"[detail] {count}/{total} {ticker}: analysts={data.get('analyst_count')} "
              f"MR={data.get('marketrank_percentile')}"
              + (f" errors={bad}" if bad else "")
              + f"  ({left:,} left, ~{rate * left / 3600:.1f}h)")
        if count % 10 == 0:
            notes.progress(count, total, "Stocks browsed", state["t0"])
            notes.bullet(f"Latest: **{ticker}** — {data.get('consensus_rating') or 'no consensus'}, "
                         f"{int(data['analyst_count']) if data.get('analyst_count') else 0} analysts")
            _health_check(state, rate * left, deadline, count, total)


async def _run(tickers, pages_map, uni, run_date, tabs, deadline, window):
    queue: asyncio.Queue = asyncio.Queue()
    for t in tickers:
        queue.put_nowait(t)
    state = {"done": 0, "t0": time.time(), "recent": [], "backoff": 0, "resume_at": 0.0}

    async with async_playwright() as pw:
        args = ["--disable-blink-features=AutomationControlled"]
        if window:
            args += [f"--window-position={window[0]},{window[1]}",
                     f"--window-size={window[2]},{window[3]}"]
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=False, channel="chrome",
            user_agent=USER_AGENT, args=args,
            viewport=None if window else {"width": 1400, "height": 950})
        try:
            pages = ctx.pages[:1] or [await ctx.new_page()]
            while len(pages) < tabs:
                pages.append(await ctx.new_page())
            for p in pages:
                p.set_default_timeout(60_000)
            await asyncio.gather(*[
                _worker(p, queue, pages_map, uni, run_date, state, len(tickers), deadline)
                for p in pages])
        finally:
            await ctx.close()
    return state, queue


def run_live(tickers: list[str], window=None, deadline: float | None = None, tabs: int = 4) -> int:
    from .pull_detail import PAGES

    run_date = db.today()
    with db.connect() as con:
        uni = {r["ticker"]: dict(r) for r in con.execute("SELECT * FROM universe")}

    notes.stage("Stage 3 — reading every MarketBeat tab, live")
    budget = f", stopping after {(deadline - time.time()) / 3600:.1f}h" if deadline else ""
    notes.line(f"Browsing up to **{len(tickers):,}** stocks across {len(PAGES)} tabs each, "
               f"{tabs} pages at a time, scrolling every one{budget}.")

    state, queue = asyncio.run(_run(tickers, PAGES, uni, run_date, tabs, deadline, window))
    elapsed = (time.time() - state["t0"]) / 3600
    unread = queue.qsize()
    notes.line(f"Browsed **{state['done']:,}** stocks in {elapsed:.1f}h"
               + (f"; stopped on the time budget with {unread:,} left" if unread else ""))
    print(f"[detail] browsed {state['done']:,} stocks in {elapsed:.1f}h"
          + (f" (time budget reached, {unread:,} unread)" if unread else ""))
    return state["done"]
