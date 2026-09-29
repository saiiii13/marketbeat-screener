from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "out"
PROFILE_DIR = ROOT / "browser_profile"
DB_PATH = DATA_DIR / "screener.db"
TEMPLATE_DIR = ROOT / "screener" / "templates"

BASE_URL = "https://www.marketbeat.com"
LOGIN_URL = f"{BASE_URL}/login/"
SCREENER_URL = f"{BASE_URL}/stock-screener/"
UPGRADES_URL = f"{BASE_URL}/ratings/upgrades/"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Screener column checkbox ids -> our field names (cphPrimaryContent_chkCol_<n>).
# The screener silently ignores filters once more than ~25 columns are selected, so keep
# this list short and uncheck the defaults we don't need (SCREENER_UNCHECK).
SCREENER_COLUMNS = {
    1: "exchange",
    3: "country",
    93: "sector",
    19: "price",
    76: "price_change_pct",
    57: "news_sentiment",
    52: "marketrank",
    51: "market_cap",
    42: "range_50d",
    43: "range_52w",
    11: "avg_volume",
    88: "relative_volume",
    15: "consensus_target",
    16: "upside_pct",
    17: "consensus_rating",
    59: "next_earnings_date",
    22: "eps_growth_cy",
    62: "eps_growth_ny",
    67: "peg",
    69: "pe_forward",
    49: "insider_own_pct",
    50: "inst_own_pct",
    12: "beta",
    91: "rsi",
}
SCREENER_UNCHECK = [25, 68]  # Annual Dividend Yield, P/E (trailing)
# Column header text (lower-cased, prefix match) -> field name, used to map the table
SCREENER_HEADER_MAP = {
    "company": "ticker",
    "exchange": "exchange",
    "country": "country",
    "sector": "sector",
    "industry": "industry",
    "current price": "price",
    "price change": "price_change_pct",
    "news sentiment": "news_sentiment",
    "marketrank": "marketrank",
    "market cap": "market_cap",
    "50-day range": "range_50d",
    "52-week range": "range_52w",
    "average daily volume": "avg_volume",
    "relative volume": "relative_volume",
    "consensus price target": "consensus_target",
    "upside/downside": "upside_pct",
    "consensus rating": "consensus_rating",
    "next earnings date": "next_earnings_date",
    "current year eps growth": "eps_growth_cy",
    "next year eps growth": "eps_growth_ny",
    "p/e growth": "peg",
    "p/e ratio (forward)": "pe_forward",
    "insider ownership": "insider_own_pct",
    "institutional ownership": "inst_own_pct",
    "beta": "beta",
    "rsi": "rsi",
}
# Country checkboxes: index -> label. 1 = USA (All Exchanges) already covers 0.
SCREENER_COUNTRIES = {1: "USA (All Exchanges)", 2: "Canada", 3: "United Kingdom", 4: "Europe"}
SCREENER_RESULT_LIMIT = 1000
POSTBACK_TARGET = "ctl00$cphPrimaryContent$btnApplyDelayPostbackFilters"
COLUMN_CLOSE_TARGET = "ctl00$cphPrimaryContent$btnColumnSelectorClose"

UNIVERSE_MIN = 10_000

# The sweep runs cheapest-first. Most of MarketBeat's listings are sub-dollar OTC shells with no
# analyst coverage, which the filters drop anyway, so start above them and spend the pages on
# stocks that can actually be screened. Set to 0.0 to sweep literally everything.
SWEEP_MIN_PRICE = 1.0
SWEEP_MAX_PAGES = 40

# Stage B pre-filter
BULLISH_RATINGS = {"strong buy", "buy", "moderate buy"}
MIN_ANALYSTS = 3

# Stage C
YF_BATCH = 250
PRELIM_FINALISTS = 250

# Detail pages fetched per finalist
DETAIL_PAGES = ["", "forecast", "earnings", "short-interest", "insider-trades", "institutional-ownership", "news"]
DETAIL_DELAY_SEC = 0.5
DETAIL_WORKERS = 4

# Analyst-consensus requirements on the finalists (counts from each stock's forecast page).
# The user's brief: lots of buys, very few holds, no sells.
MIN_BUY_RATINGS = 20
MAX_HOLD_RATINGS = 3
MAX_SELL_RATINGS = 0

# Price preference. Not a hard cut - stocks below this are ranked down, not removed.
FOCUS_MIN_PRICE = 75.0
FOCUS_PRICE_WEIGHT = 0.25

# Hard exclusions
MAX_VOL_ANNUAL = 0.60  # growth names run hot; 45% would drop e.g. NVDA. Penalised in the risk score above 25%.
MIN_DOLLAR_VOL = 1_000_000
MAX_DRAWDOWN_FROM_52W = -0.25

# Score weights
WEIGHTS = {
    "recent_upgrade": 0.20,
    "buy_ratio": 0.15,
    "momentum": 0.20,
    "target_upside": 0.10,
    "mb_features": 0.20,
    "risk": 0.15,
}

# yfinance throttling: a collapsing batch hit rate means Yahoo is refusing, not that the symbols
# are bad, so pause and retry those tickers on a later pass instead of recording them as missing.
THROTTLE_HIT_RATE = 0.70
THROTTLE_SLEEP_SEC = 60
YF_MAX_PASSES = 3

# Only used to show an ETA during the sweep; a full run has taken 19 pages.
SWEEP_EXPECTED_PAGES = 19


# Browse MarketBeat the slow way: scroll each page before reading it, and read every
# finalist through the visible browser rather than fetching its HTML directly.
SCROLL_PAGES = True
LIVE_DETAIL = True
# Start every run from an empty store, so nothing is carried over between runs.
WIPE_ON_RUN = True

# How many Chrome tabs browse in parallel during the live read. Each still opens and
# scrolls its pages; more tabs simply means more of them happen at once.
LIVE_TABS = 4
# Default overnight budget in hours for the whole run, or None for no limit.
RUN_HOURS = None
