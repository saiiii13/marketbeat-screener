"""Watch the screener check its own results against MarketBeat, live.

Nothing here changes the ranking. It re-reads each stock's MarketBeat page right now and
compares the live numbers against what is stored, so the data can be seen being confirmed
rather than taken on trust - and so stale rows show up instead of hiding.
"""
import time

from . import db, notes
from .browser import Session
from .parse import parse_overview, parse_forecast
from .pull_detail import get_html, make_session, marketbeat_cookies, stock_base_url
from .score import rating_counts_of

PAGE_PAUSE = 2.5  # long enough to actually read the page as it goes by


def _pct_diff(live, stored) -> float | None:
    if live is None or stored is None or not stored:
        return None
    return (live - stored) / abs(stored) * 100


def verdict(live_price, stored_price, live_counts, stored_counts) -> tuple[str, str]:
    """A short judgement plus the style to print it in."""
    drift = _pct_diff(live_price, stored_price)
    if live_price is None:
        return "could not read the live page", "yellow"
    if live_counts and stored_counts and live_counts != stored_counts:
        return "ratings changed since the run", "yellow"
    if drift is not None and abs(drift) > 2:
        return f"price moved {drift:+.1f}% since the run", "yellow"
    return "confirmed", "green"


def run(rows: list[dict], limit: int = 10, split: bool = True) -> list[dict]:
    """Walk a visible Chrome window through each stock while re-reading its data."""
    targets = rows[:limit]
    if not targets:
        return []

    with db.connect() as con:
        uni = {r["ticker"]: dict(r) for r in con.execute("SELECT * FROM universe")}
        stored_detail = db.load_json(con, "mb_detail")

    path = notes.start("Verifying screener results against MarketBeat")
    notes.bullet(f"Re-reading {len(targets)} stocks live. The browser on the right is opening "
                 f"each page as it is checked.")
    window = notes.open_split_left() if split else None

    results = []
    session = Session(headless=False, window=window)
    session.__enter__()
    try:
        page = session.page()
        page.set_default_timeout(20_000)
        http = make_session(marketbeat_cookies(session))
        notes.stage("Live check")
        t0 = time.time()

        for i, row in enumerate(targets, 1):
            ticker = row["ticker"]
            base = stock_base_url(ticker, uni.get(ticker))
            try:
                page.goto(base, wait_until="commit", timeout=15_000)
            except Exception:
                pass

            live_price = live_counts = None
            try:
                overview = parse_overview(get_html(http, base))
                live_price = overview.get("price")
                forecast = parse_forecast(get_html(http, base + "forecast/"))
                live_counts = rating_counts_of(forecast)
            except Exception as exc:
                notes.line(f"**{ticker}** — could not re-read the page ({type(exc).__name__})")

            stored_price = row.get("price")
            stored_counts = rating_counts_of(stored_detail.get(ticker, {}))
            text, _ = verdict(live_price, stored_price, live_counts, stored_counts)

            live_s = f"${live_price:,.2f}" if live_price is not None else "–"
            stored_s = f"${stored_price:,.2f}" if stored_price is not None else "–"
            counts_s = (f"{live_counts[0]}B/{live_counts[1]}H/{live_counts[2]}S"
                        if live_counts else "no breakdown")
            notes.line(f"**{ticker}** — stored {stored_s}, live {live_s} · {counts_s} · {text}")
            notes.progress(i, len(targets), "Checked", t0)

            results.append({"ticker": ticker, "stored_price": stored_price,
                            "live_price": live_price, "live_counts": live_counts,
                            "verdict": text})
            time.sleep(PAGE_PAUSE)

        confirmed = sum(1 for r in results if r["verdict"] == "confirmed")
        notes.line(f"Done: **{confirmed} of {len(results)}** matched the stored data exactly.")
        # The browser stays open so the pages can be looked at; closing on its own would
        # pull the evidence off the screen the moment it appeared.
        print(f"\n  Checked {len(results)} stocks, {confirmed} matched exactly.")
        try:
            input("  Browser is still open. Press Enter when you are done looking: ")
        except (EOFError, KeyboardInterrupt):
            pass
    finally:
        try:
            session.__exit__(None, None, None)
        except Exception:
            pass
        notes.close()
    print(f"\n  Live check finished — notes saved to {path}")
    return results
