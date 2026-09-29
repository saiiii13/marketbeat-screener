"""Stage A: sweep the MarketBeat stock screener for every listed stock (10k+).

The screener caps a single view at 1000 rows, so we sort by price ascending and
page through with a moving minimum-price cursor until the table runs dry.
"""
import re
import time

from playwright.sync_api import Page

from . import db, notes
from .browser import Session, check_login
from .config import (
    SCREENER_URL, SCREENER_COLUMNS, SCREENER_UNCHECK, SCREENER_HEADER_MAP, SCREENER_COUNTRIES,
    SCREENER_RESULT_LIMIT, POSTBACK_TARGET, COLUMN_CLOSE_TARGET, UNIVERSE_MIN, UPGRADES_URL,
    SWEEP_MIN_PRICE, SWEEP_MAX_PAGES, SWEEP_EXPECTED_PAGES, SCROLL_PAGES,
)
from .parse import parse_upgrades_page, parse_number, parse_rating

ROW_JS = """
() => {
  const tbl = document.querySelector('table.scroll-table');
  if (!tbl) return {headers: [], rows: []};
  const headers = [...tbl.querySelectorAll('thead th, tr th')].map(th => th.innerText.trim());
  const rows = [];
  for (const tr of tbl.querySelectorAll('tbody tr')) {
    const cells = [...tr.querySelectorAll('td')].map(td => ({
      text: td.innerText.trim(),
      sort: td.getAttribute('data-sort-value'),
      clean: td.getAttribute('data-clean'),
      href: (td.querySelector('a.profile-link') || {}).href || null,
      paywalled: !!td.querySelector('.paywall-blur-text'),
    }));
    if (cells.length) rows.push(cells);
  }
  return {headers, rows};
}
"""

RESULT_LIMIT_JS = """
v => {
  for (const r of document.querySelectorAll('input[name="ctl00$cphPrimaryContent$rblResultLimits"]')) {
    r.checked = (r.value === v);
  }
}
"""

SORT_JS = """
() => {
  const s = document.getElementById('cphPrimaryContent_ddlSortBy');
  if (s) s.value = 'CurrentPrice:0';
}
"""

# No change event: that would start the page's own delayed postback, which races ours.
MIN_PRICE_JS = """
v => {
  const el = document.getElementById('cphPrimaryContent_txtMinimum_CurrentPrice');
  el.value = v;
}
"""

SET_CHECKED_JS = """
([id, c]) => {
  const el = document.getElementById(id);
  if (!el) return false;
  el.checked = c;
  return true;
}
"""

NOT_IN_POSTBACK_JS = (
    "() => !(window.Sys && Sys.WebForms && "
    "Sys.WebForms.PageRequestManager.getInstance().get_isInAsyncPostBack())"
)


def wait_postback(page: Page, timeout: int = 120_000):
    page.wait_for_timeout(400)
    try:
        page.wait_for_function(NOT_IN_POSTBACK_JS, timeout=timeout)
    except Exception:
        pass
    page.wait_for_load_state("networkidle", timeout=timeout)
    page.wait_for_timeout(300)


# ASP.NET's __doPostBack inspects arguments.callee.caller, which throws when called from
# Playwright's strict-mode evaluate; a sloppy-mode Function run from a timer has no caller.
POSTBACK_JS = """t => { new Function('t', "setTimeout(function(){ __doPostBack(t, ''); }, 0);")(t); }"""


def postback(page: Page, target: str = POSTBACK_TARGET):
    page.evaluate(POSTBACK_JS, target)
    wait_postback(page)


def set_checked(page: Page, element_id: str, checked: bool = True) -> bool:
    return page.evaluate(SET_CHECKED_JS, [element_id, checked])


def configure_screener(page: Page):
    page.goto(SCREENER_URL)
    page.wait_for_selector("table.scroll-table", timeout=90_000)

    for idx in SCREENER_COUNTRIES:
        set_checked(page, f"cphPrimaryContent_chkExchangeCountries_Country_{idx}", True)
    postback(page)

    for n in SCREENER_UNCHECK:
        set_checked(page, f"cphPrimaryContent_chkCol_{n}", False)
    missing = [n for n in SCREENER_COLUMNS if not set_checked(page, f"cphPrimaryContent_chkCol_{n}", True)]
    if missing:
        print(f"[bulk] column checkboxes not found (site layout changed?): {missing}")
    postback(page, COLUMN_CLOSE_TARGET)

    page.evaluate(RESULT_LIMIT_JS, str(SCREENER_RESULT_LIMIT))
    page.evaluate(SORT_JS)
    postback(page)

    headers = page.evaluate("() => [...document.querySelectorAll('table.scroll-table th')].map(t => t.innerText.trim())")
    print(f"[bulk] {len(headers)} screener columns: {headers}")
    if len(headers) > 25:
        print("[bulk] WARNING: more than 25 columns selected - MarketBeat ignores filters beyond that")
    if not any(h.lower().startswith("consensus rating") for h in headers):
        print("[bulk] WARNING: Consensus Rating column not visible - column selection may have failed")


def set_min_price(page: Page, value: float | None):
    page.evaluate(MIN_PRICE_JS, "" if value is None else f"{value:.4f}")
    page.evaluate(SORT_JS)
    postback(page)


def page_is_consistent(rows: list[dict], cursor: float | None) -> bool:
    prices = [r["price"] for r in rows if r.get("price") is not None]
    if len(prices) < 2:
        return True
    # currency-converted rows make the displayed order slightly noisy; require it to be mostly ascending
    pairs = list(zip(prices, prices[1:]))
    ascending = sum(1 for a, b in pairs if a <= b) / len(pairs) >= 0.95
    return ascending and (cursor is None or prices[0] >= cursor * 0.9)


def map_headers(headers: list[str]) -> list[str | None]:
    out = []
    for h in headers:
        key = re.sub(r"[™®]|\s+", " ", h).strip().lower()
        field = next((f for prefix, f in SCREENER_HEADER_MAP.items() if key.startswith(prefix)), None)
        out.append(field)
    return out


def parse_row(fields: list[str | None], cells: list[dict]) -> dict | None:
    row: dict = {}
    for field, cell in zip(fields, cells):
        if field is None:
            continue
        if field == "ticker":
            clean = cell.get("clean") or ""
            ticker, _, company = clean.partition("|")
            href = cell.get("href") or ""
            m = re.search(r"/stocks/([^/]+)/([^/]+)/", href)
            row["ticker"] = (ticker or (m.group(2) if m else "")).strip().upper()
            row["company"] = company.strip()
            row["url"] = href
            row["exchange_from_url"] = m.group(1) if m else None
            continue
        if cell.get("paywalled"):
            row[field] = None
            continue
        text = cell["text"]
        if field == "consensus_rating":
            row[field], row["consensus_score"] = parse_rating(text)
        elif field in ("range_50d", "range_52w"):
            nums = [n for n in (parse_number(x) for x in re.split(r"[▼▲\-–]|\s{2,}|\n", text)) if n is not None]
            row[field + "_low"], row[field + "_high"] = (nums[0], nums[-1]) if len(nums) >= 2 else (None, None)
        elif field in ("exchange", "country", "sector", "industry", "next_earnings_date"):
            row[field] = text or None
        else:
            sv = cell.get("sort")
            row[field] = parse_number(sv) if sv not in (None, "") else parse_number(text)
    if not row.get("ticker"):
        return None
    if not row.get("exchange"):
        row["exchange"] = row.get("exchange_from_url")
    return row


def scroll_through(page: Page, step_px: int = 700):
    """Scroll the whole results table into view, the way a person reading it would."""
    height = page.evaluate("() => document.body.scrollHeight")
    position = 0
    while position < height:
        position += step_px
        page.evaluate("y => window.scrollTo(0, y)", position)
        page.wait_for_timeout(90)
        height = page.evaluate("() => document.body.scrollHeight")
    page.evaluate("() => window.scrollTo(0, 0)")
    page.wait_for_timeout(150)


def read_table(page: Page, scroll: bool = False) -> list[dict]:
    if scroll:
        scroll_through(page)
    data = page.evaluate(ROW_JS)
    fields = map_headers(data["headers"])
    rows = [parse_row(fields, cells) for cells in data["rows"]]
    return [r for r in rows if r]


def sweep(page: Page) -> dict[str, dict]:
    t_sweep = time.time()
    all_rows: dict[str, dict] = {}
    cursor: float | None = SWEEP_MIN_PRICE or None
    page_no = 0
    if cursor:
        print(f"[bulk] starting at ${cursor:,.2f} (SWEEP_MIN_PRICE); set it to 0 to include sub-dollar listings")
        notes.bullet(f"Sweeping every listing priced ${cursor:,.2f} and up, 1,000 at a time.")
    while page_no < SWEEP_MAX_PAGES:
        page_no += 1
        for attempt in range(3):
            set_min_price(page, cursor)
            rows = read_table(page, scroll=SCROLL_PAGES)
            if page_is_consistent(rows, cursor):
                break
            print(f"[bulk] page {page_no}: results not sorted/filtered as requested, retrying ({attempt + 1}/3)")
            page.wait_for_timeout(3000)
        new = [r for r in rows if r["ticker"] not in all_rows]
        for r in rows:
            all_rows[r["ticker"]] = r
        prices = [r["price"] for r in rows if r.get("price") is not None]
        print(f"[bulk] page {page_no}: {len(rows)} rows, {len(new)} new, total {len(all_rows)}, cursor={cursor}")
        rated = sum(1 for r in rows if r.get("consensus_rating"))
        eta = notes.eta(page_no, SWEEP_EXPECTED_PAGES, t_sweep)
        notes.line(f"**Page {page_no}** of ~{SWEEP_EXPECTED_PAGES} — read {len(rows)} stocks "
                   f"(${min(prices):,.2f}–${max(prices):,.2f} each), {rated} with analyst coverage. "
                   f"{len(all_rows):,} total" + (f" · {eta}" if eta else "") + f" · {notes.elapsed()} elapsed")
        notes.stocks_read(rows)
        if len(rows) < SCREENER_RESULT_LIMIT or not prices or not new:
            break
        top = sorted(prices)[int(len(prices) * 0.98)]  # near the top of the page, robust to odd rows
        if cursor is not None and top <= cursor:
            top = cursor + 0.0001
        cursor = top
        time.sleep(0.5)
    return all_rows


def pull_upgrades(session: Session) -> list[dict]:
    try:
        return parse_upgrades_page(session.get_html(UPGRADES_URL))
    except Exception as e:
        print(f"[bulk] upgrades page failed: {e}")
        return []


def run(headless: bool = False, window: tuple[int, int, int, int] | None = None) -> int:
    run_date = db.today()
    notes.stage("Stage 1 — reading every stock on MarketBeat")
    with Session(headless=headless, window=window) as s:
        page = s.page()
        check_login(page)
        configure_screener(page)
        rows = sweep(page)
        upgrades = pull_upgrades(s)

    with db.connect() as con:
        db.upsert_universe(con, list(rows.values()))
        for t, r in rows.items():
            db.upsert_json(con, "mb_bulk", t, r, run_date)
        con.executemany(
            "INSERT OR REPLACE INTO upgrades(ticker,date,firm,action,detail) VALUES (?,?,?,?,?)",
            [(u["ticker"], u["date"], u["firm"], u["action"], u["detail"]) for u in upgrades],
        )
        db.set_meta(con, "bulk_run_date", run_date)
        n = con.execute("SELECT COUNT(*) FROM universe").fetchone()[0]
    print(f"[bulk] universe size: {n} (this run: {len(rows)}), upgrades captured: {len(upgrades)}")
    notes.line(f"Finished reading **{len(rows):,} stocks**; captured {len(upgrades)} analyst upgrades from today's list.")
    if n < UNIVERSE_MIN:
        print(f"[bulk] WARNING: universe below {UNIVERSE_MIN}. Check that you are logged in and all countries were selected.")
    return n
