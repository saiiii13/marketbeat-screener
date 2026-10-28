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
# The ratio every listed stock must show, in the menu list and on MarketBeat's own counts:
# at least this share of analysts saying buy, and no sells. AMD at 34 buy / 8 hold / 1 sell
# (79%) is the kind of "Moderate Buy" this keeps out.
MIN_LIST_BUY_SHARE = 0.90
MAX_LIST_SELLS = 0

# Fixes from the 2026-10-15 backtest. Each is a switch so the time-travel test
# (python run.py timetravel) can replay the old rules against the new with the same code.
#
# CONSENSUS_RULE "shape" is the brief above (20+ buys, <=3 holds, 0 sells). "share" asks
# instead for MIN_BUY_SHARE of the analysts to say buy, however many there are - the only
# consensus signal the backtest found any edge in.
# Kept on "shape". The time-travel replay made "share" look spectacular (+102% over the
# year against the S&P's +11.9%), but the 95% rule mostly selects thinly covered smaller
# stocks, and those beat the market that year whatever their rating - helped by the sample
# being drawn from today's $2B+ listings, which keeps the small caps that soared and drops
# the ones that collapsed. Against peers with the same number of analysts, 95%-buy stocks
# won at 3-10 analysts and lost at 11-20, none of it significant. A second replay on a
# different sample reversed the ranking (old +73.7%, 95% rule +24.3%): no rule's edge
# survives a change of sample. See homework.txt 7.4b.
CONSENSUS_RULE = "shape"
MIN_BUY_SHARE = 0.95
# No more than this many picks from one sector. On 2026-10-06 five of the top eight were
# chip and AI-hardware names, and they fell together.
MAX_PER_SECTOR = 3
# Volatility behind the tomorrow / next week / next month ranges. "blend" is the
# root-mean-square of 20-day and one-year volatility, "max" the larger of the two, "1y" the
# year alone. Measured by `run.py timetravel` over 5,737 stock-weeks (2025-10 to 2026-09),
# share of next-week closes inside each band, against targets of 68.3% and 95.4%:
#     20d    65.0% / 90.6%     (the original - too narrow, especially in the tails)
#     1y     68.9% / 93.8%     <- closest on both
#     max    72.3% / 95.1%
#     blend  67.5% / 93.3%
OUTLOOK_VOL = "1y"

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

# Only used to show an ETA during the sweep; a full run has taken 19 pages.
SWEEP_EXPECTED_PAGES = 19


# Browse MarketBeat the slow way: scroll each page before reading it, and read every
# finalist through the visible browser rather than fetching its HTML directly.
SCROLL_PAGES = True
LIVE_DETAIL = True
# Start every run from an empty store, so nothing is carried over between runs: every stock
# and its price chart is read from MarketBeat again, even if a run already read it today.
# Price and rating history, backtests and holdings are still kept. False makes a second run
# the same day resume from what is saved instead.
WIPE_ON_RUN = True

# How many Chrome tabs browse in parallel during the live read. Each still opens and
# scrolls its pages; more tabs simply means more of them happen at once.
LIVE_TABS = 4
# Default overnight budget in hours for the whole run, or None for no limit.
RUN_HOURS = None
