# How the screener works

Every figure comes from MarketBeat, read through the logged-in Chrome profile's cookies. Every
run writes to `data/screener.db`: the working tables hold today's run, and the history tables
(prices, ratings, runs, recommendations, backtests, holdings, plans) are only ever added to,
so each run builds on the last.

```
python run.py mb                     ~15-20 min; every run reads MarketBeat from scratch
python run.py menu                   the same, and everything else, from the keyboard
```

---

## The flow

```
  ┌──────────────────────────────────────────────────────────────────────┐
  │  0. TIDY            working rows are all dropped, today's included,   │
  │                     so every run reads every stock and price again.   │
  │                     History tables are never touched.                 │
  └───────────────────────────────┬──────────────────────────────────────┘
                                  ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  1. LISTS           ~70 of MarketBeat's research lists: upgrades,     │
  │                     top rated, MarketRank, breakouts, golden crosses, │
  │                     unusual volume, insider buying, every sector...   │
  │                     plus your holdings and the last recommendations   │
  │                     ─────────────────────────────────────────────     │
  │                     2,041 from the lists + 326 carried over           │
  └───────────────────────────────┬──────────────────────────────────────┘
                                  ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  2. PAGES           per stock, 4 at a time:                           │
  │                       overview  company, sector, price, volume,       │
  │                                 market cap, MarketRank, sentiment     │
  │                       chart     a year of daily closes - fetched only │
  │                                 when the stored history is behind     │
  │                       forecast  buy/hold/sell count, target, upside,  │
  │                                 MarketBeat's own counts 1 month,      │
  │                                 3 months and 1 year ago, and every    │
  │                                 analyst action it lists               │
  └───────────────────────────────┬──────────────────────────────────────┘
                                  ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  3. STORE           price_history     one row per stock per day       │
  │                     rating_snapshots  today's count + the 3 official  │
  │                     rating_actions    every upgrade, downgrade,       │
  │                                       initiation and target change    │
  └───────────────────────────────┬──────────────────────────────────────┘
                                  ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  4. PRICE MEASURES  1-month return, RSI, trend, volatility, 52-week   │
  │                     position - from the stored closes                 │
  └───────────────────────────────┬──────────────────────────────────────┘
                                  ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  5. CALIBRATE       measured odds per momentum/RSI/trend setup,       │
  │                     from the stored closes                            │
  └───────────────────────────────┬──────────────────────────────────────┘
                                  ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  6. RANK            the ratio list: buy share's lower 95% bound,      │
  │                     sells counted twice, 5+ analysts                  │
  │                     -> saved as this run's recommendations            │
  │                     the dashboard: filters + weighted score           │
  │                     -> out/dashboard_<date>.html                      │
  └───────────────────────────────┬──────────────────────────────────────┘
                                  ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  7. SECOND OPINION  (verify) the top picks re-read live in Chrome,    │
  │                     and Nasdaq's public consensus asked the same      │
  │                     questions - CONFIRMED / MIXED / REJECTED          │
  └──────────────────────────────────────────────────────────────────────┘
```

Stages 0–6 run from the menu with **R**. Every command that writes the working tables takes a
lock first (`data/run.lock`); a second writer is refused with the name and PID of the one
already running, and a lock left by a crashed process is taken over.

## Built on top

| Menu | Command | What it uses |
|---|---|---|
| **B** Backtest | `run.py replay` | `price_history`, `rating_snapshots`, `rating_actions`; writes `backtests`, `backtest_trades`, `backtest_equity` |
| **P** Portfolio planner | `run.py plan` | today's reading (live if not read today), `price_history`, `recommendations`, `holdings`; writes `plans`, `holdings` |
| H on a backtest | `run.py backtests` | every saved backtest, newest first |

---

# The three MarketBeat pages

All three are ordinary pages, fetched over HTTP with the Chrome profile's cookies
(`pull_detail.make_session(browser_cookies())`). A free account sees everything used here.

## 1. Overview — `/stocks/{EXCHANGE}/{TICKER}/`

```python
from screener.parse import parse_overview
d = parse_overview(html)
d["company"], d["sector"], d["price"], d["avg_volume"], d["market_cap"]
# ('Visa', 'Finance', 408.02, 5950000.0, 728080000000.0)
```

The page also carries the link to its price chart, with a token minted for that page view —
so the chart can only be fetched after the overview.

## 2. Chart — `/Scripts/charts/PriceSeries.ashx?prefix=…&symbol=…&scrp=<token>`

A JavaScript file wrapping a CSV: `Date, SharePrice, Session, InsightPoint, InsightHTML,
OneMinuteVolume`. The `EOD` rows are a year of daily closes; the rest are intraday points.

**The traps.** EOD rows include weekends and market holidays, each repeating the previous
close — `prices.clean()` keeps real sessions only. There are no opens, highs, lows or daily
volume, so ATR/ADX/OBV are skipped (not faked) and liquidity uses the overview page's
average volume. And a split makes MarketBeat restate the whole year it serves, so
`prices.store()` rescales any older stored days by the same factor.

## 3. Forecast — `/stocks/{EXCHANGE}/{TICKER}/forecast/`

```python
from screener.parse import parse_forecast
f = parse_forecast(html)
f["rating_counts"]          # {'strong_buy': 1, 'buy': 25, 'hold': 15, 'sell': 2}
f["official_snapshots"]     # the "Ratings Over Time" table: now, 1 month, 3 months, 1 year ago
f["ratings_history"]        # every action listed: date, firm, action, rating from -> to, target
```

**The trap.** Each column of "Ratings Over Time" is labelled with a twelve-month window,
e.g. `9/25/25 to 9/25/26`; the snapshot date is the *end* of the window. Reading the start
put every snapshot a year early.

---

# Past ratings: what the backtest sees

MarketBeat's free pages publish four dated counts per stock. Every other date is rebuilt by
`ratings.counts_at()`:

1. **A stored snapshot** within three days before the date is used as it stands. Every
   daily run adds one per stock, so recent history becomes exact.
2. Otherwise each firm's latest rating on or before the date, if under twelve months old.
3. The action table holds only the most recent ~50 actions, so for older dates a firm's
   rating is read **backwards** from its next action: an upgrade or downgrade names the old
   rating, a target change or reiteration keeps it, an initiation means there was none.
4. The rebuild is then **anchored** to the official snapshots: its error at the snapshots
   either side of the date is interpolated and removed.

Against MarketBeat's own published counts (random stocks from the list: 40 for the first
column, 80 for the others), share of rebuilt buy shares within 5 points:

| As of | Steps 2 only | + step 3 | + step 4 (that snapshot hidden) |
|---|---|---|---|
| 1 month ago | 50% | 74% | 83% |
| 3 months ago | 38% | 58% | 71% |
| 1 year ago | 11% | 42% | — |

Step 4 removes the bias the rebuild has at the snapshot dates on either side. It never
supplies a rating that had not been issued by the date.

---

## Logging in

```
set MARKETBEAT_EMAIL=you@example.com
set MARKETBEAT_PASSWORD=...
python run.py login
```

With both variables set, `login` fills MarketBeat's form itself, headless; without them it
opens Chrome for a login by hand — and keeps the window open if the saved session is not an
All-Access one, so another account can be signed in. The password is used once and never
written to disk; what persists is the session cookie in `browser_profile/`.

## Nasdaq, in the second opinion only

`verify` also asks `api.nasdaq.com` (the backend of nasdaq.com's stock pages, keyless) for its
consensus, short interest, institutional positions, insider counts and earnings surprises,
so each figure the screener acted on gets an independent vote. Nasdaq never chooses a stock
and none of its figures enter the ranking, the backtest or the planner.

## Rate limits

MarketBeat publishes none. Four concurrent workers with a 0.3s pause each, and a retry with
a growing wait on 429/503, have read 2,000+ stocks a run without refusals.

---

*Informational only — not investment advice.*
