# MarketBeat short-term growth screener

Standalone Python program that reads MarketBeat through your logged-in account, ranks the
stocks with the cleanest analyst buy/hold/sell ratios, backtests that strategy on stored
history, and plans a portfolio from it. **MarketBeat is the only data source** — candidates,
analyst counts, price targets, rating history and daily prices all come from it — and every
run is saved to a local SQLite database that later runs build on.

> Informational only, not investment advice. Every rating, size, entry, target and stop the
> program shows is a model-generated signal. One-month trades are high risk.

## Setup (once)

```
pip install -r requirements.txt
python run.py login
```

`login` opens a Google Chrome window (its own automation profile in `browser_profile/`, separate
from your everyday Chrome). Log in to MarketBeat there; the script notices and saves the session.
The program only ever drives that Chrome window — it does not touch the mouse, keyboard or any
other app. A free MarketBeat account is enough for everything below.

## The menu

```
python run.py menu
```

| Key | Option |
|---|---|
| ENTER | **Most buys, fewest holds, no sells** — stocks with more than 35 buys, fewer than 5 holds and no sells (`LIST_MIN_BUYS`, `LIST_MAX_HOLDS`, `MAX_LIST_SELLS` in config.py), ranked by net buys (buys − 2 × holds), so NVDA's 54/1/0 comes before V's 37/0/0; with its price chart's 1-week, 1-month and 1-year change beside it. The line under the list shows more from the selected stock's MarketBeat page: sector, P/E and PEG, projected earnings growth, short interest, insider buying and selling, news mood and institutional ownership. R reads MarketBeat again. |
| Y | **Stock screens** — stocks, ETFs, penny stocks or everything, by count and price band, with a detail view per stock. The list, the detail view, the planner's pick list and My portfolio all show the same 1-week / 1-month / 1-year chart change and MarketBeat facts. |
| B | **Backtest** — choose the stocks (latest top 20, every stored stock, or your own), the period, the starting cash, how many to hold and how often to re-check, and (T) an optional price-chart rule: skip stocks down over the last month or 3 months, or blend the 3-month change into the score. Stocks are picked by net buys (buys − 2 × holds − 5 × sells, sells allowed); no chart rule is the default (homework.txt parts 9 and 10 have why). Reports total return, win rate, max drawdown, trades and buy-and-hold / S&P 500 comparisons. Every run is saved; H lists them for comparison. |
| P | **Portfolio planner** — enter your cash, then choose tickers from earlier recommendations or let it pick. For each stock: Buy/Hold/Sell with the ratio and a confidence score, the dollar amount and whole shares that fit your cash (Buy-rated only, at most 40% in one stock), and whether to buy now or wait for a limit price. Stocks you already own are saved once; each plan shows their gain/loss and whether to sell now or hold, with a target sell price and a stop-loss. The rules are at the top of `screener/planner.py`. |
| M | **My portfolio** — the stocks you own: shares, what you paid for them in all, today's price and value, gain/loss, and whether to sell now or hold. Each one has a sell limit: the suggested one (the analysts' target, or a month's typical move up) or your own, set with L and cleared with X. Reaching your sell limit, a Sell rating or the stop-loss turns the action to Sell now. A adds a stock, E edits it, D deletes it. |
| R | **Refresh** — read MarketBeat again from scratch (about 15–20 minutes every time). |

## Terminal commands

```
python run.py mb                              # the whole MarketBeat run, then the top ratios
python run.py replay --start 2026-04-01 --end 2026-10-23 --cash 10000 --picks 10 --hold 21
python run.py replay --tickers V,MSFT,NVDA    # or --tickers recs for the latest top --top
python run.py replay --trend up3m             # with a price-chart rule: up1m, up3m or blend3m
python run.py backtests                       # every saved backtest, newest first
python run.py plan --cash 5000                # program picks --picks-plan stocks
python run.py plan --cash 5000 --tickers V,GOOGL
python run.py plan --hold-add MSFT,10,4800    # save a holding: ticker, shares, total paid
python run.py plan --sell-limit MSFT,600      # your own sell limit (0 goes back to the suggested one)
```

The pipeline stages can also run one at a time:

| Command | What it does |
|---|---|
| `pull-bulk` | Collects candidates from about 70 of MarketBeat's research lists (upgrades, top rated, breakouts, sectors…), plus any stock you hold. |
| `discover` | Orders the candidates by how many lists flagged them. |
| `pull-detail` | Reads each candidate's overview page (company, sector, MarketRank, volume, market cap…), its price chart when the stored history is behind, and its forecast page (buy/hold/sell count, target, MarketBeat's own 1-month/3-month/1-year-ago counts, every rating action). |
| `enrich` | Price measures (momentum, RSI, volatility, 52-week position) from the stored history. |
| `analyze` | Scores, ranks, stores this run's recommendations, writes `out/dashboard_<date>.html`. |
| `verify` | Re-checks the top picks live on MarketBeat (in Chrome) and on Nasdaq's public consensus. |
| `calibrate` | Rebuilds the measured-odds table from stored prices. |
| `all` | Every stage in order, then `verify`. |
| `backtest`, `timetravel` | Research tools from the earlier rule studies (homework.txt part 7). |

## The database

Everything lives in `data/screener.db`, created automatically on first run. The schema is in
`screener/db.py`.

- **History tables, never cleared:** `price_history` (MarketBeat closes, one row per stock per
  trading day), `price_meta`, `rating_snapshots` (each run's buy/hold/sell counts, plus
  MarketBeat's dated 1-month/3-month/1-year-ago counts), `rating_actions` (every analyst action
  MarketBeat lists), `runs` and `recommendations`, `backtests` with `backtest_trades` and
  `backtest_equity`, `holdings` and `plans`.
- **Working tables, today's run only:** `mb_bulk`, `mb_detail`, `prices`, `scores`, `verify`,
  `universe`, `meta`.

Every run starts again: the working tables are cleared and every stock, its forecast and its
price chart are read from MarketBeat afresh, even if a run already read them today. What it
reads is added to the history tables, so the backtest's history still grows. Set
`WIPE_ON_RUN = False` in `screener/config.py` to make a second run the same day resume from
what is saved instead.

## What the backtest can and cannot see

- **Prices:** MarketBeat's chart serves about one year of daily closes. The database keeps
  every close it has stored, so the backtest range grows by a day with every daily run. It
  cannot start before the oldest stored day.
- **Ratings:** MarketBeat's free pages show today's count and three dated snapshots. Other
  dates are rebuilt from the stored rating actions and anchored to those snapshots. Measured
  against MarketBeat's own published counts, the rebuilt buy share is within 5 points for
  about 83% of stocks one month back and 71% three months back. The error is about 10 points
  at a year back. Days a run stored itself are exact.
- **Broken price series:** a stock whose stored closes move threefold in one day is left out
  of a backtest, which says so. In MarketBeat's chart that is almost always a split it never
  adjusted for: ELOX shows a thousandfold "gain" on a 1-for-1000 reverse split. Nothing under
  $1 is bought.
- **Survivorship:** the stocks are ones MarketBeat lists today. Choosing "latest top 20"
  adds hindsight on top, and the result says so.
- **Foreign listings:** stocks priced in pence or Canadian dollars are left out of the ratio
  list, the planner and the backtest, which all work in US dollars.

## Scoring (the dashboard and stock screens)

Hard filters: fewer than 3 analysts · average daily dollar volume < $1M · 20-day annualized
volatility > 60% · more than 25% below the 52-week high · the consensus shape in
`config.CONSENSUS_RULE`.

| Weight | Component | Built from |
|---|---|---|
| 20% | Fresh analyst upgrade | Upgrade / bullish initiation ≤14 days = 1.0, 15–30 days = 0.5 |
| 15% | Buy ratio | (strong buy + buy) / all ratings |
| 20% | Price momentum | 1-month return percentile, 20/50-day trend, RSI 50–70, 20-day breakout |
| 10% | Price-target upside | consensus target upside, capped at 40% |
| 20% | MarketBeat features | MarketRank, news sentiment, insider and institutional flows, earnings, short interest, PEG |
| 15% | Risk | volatility, beta, earnings inside the window, short interest, distance from the high |

The list (ENTER) leaves out any stock with a Sell rating and ranks the rest by net buys:
buys − 2 × holds (− 5 × sells where sells are allowed), so 54/1/0 (52) ranks above 37/0/0 (37)
and 6/0/0 (6). The weights are at the top of `screener/mb_ratio.py`; all other thresholds live
in `screener/config.py`.

## Layout

```
run.py                     CLI
screener/db.py             the database schema and history helpers
screener/pipeline.py       the MarketBeat pipeline (lists -> pages -> prices -> scores)
screener/prices.py         MarketBeat price history, cached in the database
screener/ratings.py        point-in-time buy/hold/sell counts from stored rating history
screener/mb_ratio.py       the buy/hold/sell-ratio ranking and its recommendations
screener/replay.py         the backtest
screener/planner.py        the portfolio planner's rules
screener/gui.py            the menu
screener/parse.py          HTML parsers for every MarketBeat page type
screener/discover.py       MarketBeat's research lists
screener/verify.py         live second opinion (MarketBeat + Nasdaq)
screener/score.py          filters and the weighted score
screener/report.py + templates/dashboard.html.j2   HTML dashboard
```
