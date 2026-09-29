# MarketBeat short-term growth screener

Standalone Python program that screens **every stock MarketBeat lists (10,000+)** using your
MarketBeat account, pulls **every MarketBeat feature** for the finalists, and ranks the ones
most likely to grow a lot over the next **month or less**. Output is a local HTML dashboard.

Stock *selection* uses MarketBeat data only. Free price history from Yahoo (yfinance) is used
purely for timing/risk numbers (momentum, RSI, volatility, liquidity).

> Informational only, not investment advice. One-month trades are high risk. Verify numbers on
> MarketBeat before acting.

## Setup (once)

```
pip install -r requirements.txt
python run.py login
```

`login` opens a Google Chrome window (its own automation profile in `browser_profile/`, separate
from your everyday Chrome). Log in to MarketBeat there; the script notices and saves the session.
The program only ever drives that Chrome window — it does not touch the mouse, keyboard or any
other app.

## Run

```
python run.py all            # ~15-20 min; resumable if interrupted
python run.py analyze        # re-score / re-render from what is already in data/screener.db
python run.py all --top 30   # more rows in the dashboard
```

Stages (each can be run on its own):

| Command | What it does |
|---|---|
| `pull-bulk` | Sweeps the MarketBeat stock screener (USA all exchanges + Canada + UK + Europe) 1000 rows at a time, sorted by price with a moving price cursor, until every stock is captured. Stores consensus rating, price target, upside, MarketRank, news sentiment, EPS growth, PEG, ownership, beta, RSI, volume, ranges. Also grabs today's analyst-upgrade list. |
| `enrich` | Pre-filters to stocks with a Buy / Moderate Buy / Strong Buy consensus (or a fresh upgrade), downloads 1 year of daily prices for them in batches, computes momentum/RSI/volatility/dollar-volume, and keeps the top 250 by a preliminary score. |
| `pull-detail` | For each finalist fetches the MarketBeat overview (MarketRank™ sub-scores, news sentiment, insider $ bought/sold, ownership %), forecast (rating counts now vs 1 month ago, target range, full upgrade/downgrade history), earnings (beat/miss history, next date, projected growth), short interest (history, % float, days to cover), insider trades (90-day buys/sells) and institutional ownership (recent adds/trims). Uses the logged-in Chrome session's cookies over HTTP (4 workers, ~30 stocks/min). |
| `analyze` | Applies hard filters, computes the score, writes `out/dashboard_<date>.html` (and `dashboard_latest.html`), prints the top 10 and opens the dashboard. |

Everything is checkpointed per ticker per day in `data/screener.db` (SQLite), so Ctrl-C and
re-run continues where it left off. `--fresh` re-downloads today's price data.

## Scoring

Hard filters (a finalist is dropped if any fails): fewer than 3 analysts · average daily dollar
volume < $1M · 20-day annualized volatility > 60% · more than 25% below the 52-week high.

Score (0–100):

| Weight | Component | Built from |
|---|---|---|
| 20% | Fresh analyst upgrade | Upgrade / bullish initiation ≤14 days = 1.0, 15–30 days = 0.5; target raise = 0.6; bullish reiteration = 0.3 |
| 15% | Buy ratio | (strong buy + buy) / all ratings, from the forecast page |
| 20% | Price momentum | 1-month return percentile (40%), above 20-day MA (15%), 20-day MA above 50-day (15%), RSI 50–70 (20%), 20-day breakout (10%) |
| 10% | Price-target upside | consensus target upside, capped at 40% |
| 20% | MarketBeat features | equal-weighted: MarketRank percentile · news sentiment + buzz · insider buy/sell $ ratio · institutional adds vs trims + ownership · earnings beat rate + projected EPS growth · short-interest squeeze zone (10–20% of float best, >30% penalised) · PEG sanity · buy-count drift vs one month ago |
| 15% | Risk | 1 − penalties: volatility 25–35% −0.05, 35–45% −0.10, >45% −0.20; beta > 1.3 −0.15; earnings inside the window −0.05; short interest > 30% −0.10; >15% below 52-week high −0.05 |

Flags shown on the dashboard: Volatile (>35%), Earnings in window (≤30 days), Overbought (RSI > 75),
Thin volume (< $5M/day), Heavy short interest (> 30%), Insider selling (sold > 3× bought).

Threshold sources: analyst-upgrade drift is front-loaded in the first few weeks
([Talval](https://talval.com/learn/do-analyst-upgrades-beat-the-market),
[AAII](https://www.aaii.com/investor-update/article/Changes-in-Analystsrsquo-Recommendations-Wonrsquot-Help-You));
RSI 50–70 as the momentum sweet spot
([Quantified Strategies](https://quantifiedstrategies.substack.com/p/rsi-based-range-momentum-system),
[ScreenerHub](https://screenerhub.app/strategies/find-momentum-stocks-using-trend-strength));
short interest 10–20% of float notable, >20% squeeze territory, >30% crowded
([S3 Partners](https://www.s3partners.com/blogs/short-interest-as-a-percentage-of-float),
[TradeZero](https://tradezero.com/en-us/blog/short-float-explained-how-to-see-squeezes-early)).
All numbers live in `screener/config.py` and `screener/score.py`.

## Layout

```
run.py                     CLI
screener/config.py         URLs, screener column ids, thresholds, weights
screener/browser.py        Chrome session (persistent profile), login flow, cookie-backed fetch
screener/pull_bulk.py      Stage A: screener sweep (Playwright)
screener/parse.py          HTML parsers for every MarketBeat page type
screener/enrich.py         Stage C: yfinance price metrics
screener/pull_detail.py    Stage D: per-finalist MarketBeat pages
screener/score.py          pre-filter, hard filters, weighted score
screener/report.py + templates/dashboard.html.j2   HTML dashboard
data/screener.db           all pulled data (gitignored)
out/                       dashboards (gitignored)
```

## Notes

- Not logged in? The sweep still runs but MarketRank and Consensus Rating are paywalled
  (blank), so the pre-filter falls back to price-target upside. Log in for the real thing.
- MarketBeat is an ASP.NET WebForms site; if they change the screener's column ids or the
  page wording, update `SCREENER_COLUMNS` in `config.py` or the regexes in `parse.py`.
- The screener silently ignores every filter once more than 25 columns are selected, which is
  why `SCREENER_COLUMNS` is capped and two default columns are unchecked.
- Parser regression tests: `python tests/test_parse.py` (saved MarketBeat pages in `tests/fixtures/`).
