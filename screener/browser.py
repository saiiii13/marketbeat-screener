"""Chrome session handling. Only a Chrome window is automated - nothing else on the PC."""
import sys
import time

from playwright.sync_api import sync_playwright, BrowserContext, Page

from .config import PROFILE_DIR, BASE_URL, LOGIN_URL, USER_AGENT

import re

# MarketBeat stamps the user level into a script URL on every page (userlevel=NotLoggedIn / AllAccess / ...)
USERLEVEL_RE = re.compile(r"userlevel=([A-Za-z]+)")


def user_level(html: str) -> str | None:
    m = USERLEVEL_RE.search(html)
    return m.group(1) if m else None


class Session:
    def __init__(self, headless: bool = False, window: tuple[int, int, int, int] | None = None):
        """window = (x, y, width, height) to place the Chrome window, e.g. the right half."""
        self.headless = headless
        self.window = window
        self._pw = None
        self.ctx: BrowserContext | None = None

    def __enter__(self) -> "Session":
        PROFILE_DIR.mkdir(exist_ok=True)
        self._pw = sync_playwright().start()
        kwargs = dict(
            user_data_dir=str(PROFILE_DIR),
            headless=self.headless,
            user_agent=USER_AGENT,
            viewport=None if self.window else {"width": 1400, "height": 950},
            args=["--disable-blink-features=AutomationControlled"]
                 + ([f"--window-position={self.window[0]},{self.window[1]}",
                     f"--window-size={self.window[2]},{self.window[3]}"] if self.window else []),
        )
        try:
            self.ctx = self._pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
        except Exception as e:  # Google Chrome not installed -> bundled Chromium
            print(f"[browser] Google Chrome unavailable ({e.__class__.__name__}); using bundled Chromium")
            self.ctx = self._pw.chromium.launch_persistent_context(**kwargs)
        return self

    def __exit__(self, *exc):
        try:
            self.ctx.close()
        finally:
            self._pw.stop()

    def page(self) -> Page:
        p = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        p.set_default_timeout(60_000)
        return p

    def is_logged_in(self, page: Page) -> bool:
        level = user_level(page.content())
        return level is not None and level.lower() != "notloggedin"

    def get_html(self, url: str, retries: int = 3) -> str:
        """Fetch a page over HTTP using the browser's cookies (no rendering)."""
        last = None
        for attempt in range(retries):
            try:
                r = self.ctx.request.get(url, headers={"User-Agent": USER_AGENT}, timeout=45_000)
                if r.ok:
                    return r.text()
                last = f"HTTP {r.status}"
                if r.status in (404, 410):
                    break
            except Exception as e:
                last = repr(e)
            time.sleep(2 * (attempt + 1))
        raise RuntimeError(f"GET {url} failed: {last}")


def read_scrolled(page, url: str, settle_ms: int = 700, step_px: int = 600) -> str:
    """Open a page in the visible browser and scroll it to the bottom before reading.

    Slower than fetching the HTML directly, and deliberately so: the page is loaded and
    scrolled the way a person would, which also triggers anything MarketBeat defers until
    the content is actually on screen.
    """
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(settle_ms)
    height = page.evaluate("() => document.body.scrollHeight")
    position = 0
    while position < height:
        position += step_px
        page.evaluate("y => window.scrollTo(0, y)", position)
        page.wait_for_timeout(120)
        height = page.evaluate("() => document.body.scrollHeight")
    page.evaluate("() => window.scrollTo(0, 0)")
    page.wait_for_timeout(200)
    return page.content()


def login_flow(timeout_min: int = 15):
    with Session(headless=False) as s:
        page = s.page()
        page.goto(LOGIN_URL)
        if s.is_logged_in(page):
            print("Already logged in to MarketBeat - session is saved in browser_profile/.")
            return
        print("A Chrome window is open. Log in to MarketBeat there.")
        print(f"Waiting up to {timeout_min} minutes for the login to complete...")
        deadline = time.time() + timeout_min * 60
        while time.time() < deadline:
            try:
                if s.is_logged_in(page):
                    print("Login detected. Session saved in browser_profile/. You can close the window.")
                    time.sleep(2)
                    return
            except Exception:
                pass
            time.sleep(2)
        print("Timed out waiting for login.", file=sys.stderr)
        sys.exit(1)


def check_login(page: Page) -> bool:
    page.goto(BASE_URL)
    level = user_level(page.content())
    ok = level is not None and level.lower() != "notloggedin"
    if ok:
        print(f"[browser] logged in to MarketBeat (user level: {level})")
    else:
        print("[browser] Not logged in to MarketBeat - run `python run.py login` first. "
              "Continuing with public data (paywalled columns will be blank).")
    return ok
