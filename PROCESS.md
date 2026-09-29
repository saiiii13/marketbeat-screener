# How the screener works

Every figure comes from Yahoo Finance. A run wipes the database first, so nothing is
carried over from the last one.

```
python run.py all                            ~2 minutes
```

---

## The flow

```
  ┌──────────────────────────────────────────────────────────────────────┐
  │  0. WIPE            data/screener.db deleted — the run starts blank   │
  └───────────────────────────────┬──────────────────────────────────────┘
                                  ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  1. SWEEP           Yahoo screener, POST /v1/finance/screener         │
  │                     one query per exchange, 250 rows a page           │
  │                     ─────────────────────────────────────────────     │
  │                     22,709 US stocks   (216,813 with --world)         │
  └───────────────────────────────┬──────────────────────────────────────┘
                                  ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  2. DISCOVER        8 curated Yahoo screens decide what is worth      │
  │                     a look: day gainers, most actives, small-cap      │
  │                     gainers, aggressive small caps, growth tech,      │
  │                     undervalued growth, undervalued large caps,       │
  │                     heavily shorted.  Day losers subtract.            │
  │                     Ranked by how many screens agree, not by size.    │
  │                     ─────────────────────────────────────────────     │
  │                     1,042 candidates   (217 on two or more screens)   │
  └───────────────────────────────┬──────────────────────────────────────┘
                                  ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  3. PRICE           GET /v8/finance/chart/{symbol}?range=1y           │
  │                     a year of daily OHLCV, 8 at a time                │
  │                     → 1-month return, RSI, volatility, dollar volume  │
  │                     ─────────────────────────────────────────────     │
  │                     1,036 priced                                      │
  └───────────────────────────────┬──────────────────────────────────────┘
                                  ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  4. ANALYST DATA    GET /v10/finance/quoteSummary/{symbol}            │
  │                     11 modules, 6 at a time                           │
  │                     → buy/hold/sell counts, targets, upgrades,        │
  │                       ownership, short interest, fundamentals         │
  └───────────────────────────────┬──────────────────────────────────────┘
                                  ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  5. CALIBRATE       walk 300 stocks' history, record what happened    │
  │                     next for each (momentum, RSI, trend) setup        │
  │                     ─────────────────────────────────────────────     │
  │                     16 setups from 5,333 observations                 │
  │                     baseline: 49.6% of stocks rose over a month       │
  └───────────────────────────────┬──────────────────────────────────────┘
                                  ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  6. FILTER          ≥20 buys · ≤3 holds · 0 sells · ≥3 analysts       │
  │                     ≥$1M daily volume · ≤60% volatility              │
  │                     within 25% of the 52-week high                    │
  │                     ─────────────────────────────────────────────     │
  │                     12 survive                                        │
  └───────────────────────────────┬──────────────────────────────────────┘
                                  ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  7. SCORE           20% fresh upgrade · 15% buy ratio · 20% momentum  │
  │                     10% target upside · 20% fundamentals · 15% risk   │
  │                     plus reasons to buy, risks, and the outlook       │
  └───────────────────────────────┬──────────────────────────────────────┘
                                  ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  8. OUTPUT          out/dashboard_<date>.html                         │
  │                     python run.py menu     graphical browser          │
  │                     python run.py explore  terminal browser           │
  └──────────────────────────────────────────────────────────────────────┘
```

Stages 1–7 also run from inside the menu: press **R** on the title screen.

## Each stage on its own

| Command | Does |
|---|---|
| `python run.py pull-bulk` | stage 1, add `--world` for all 44 regions |
| `python run.py discover` | stage 2 |
| `python run.py enrich` | stage 3 |
| `python run.py pull-detail` | stage 4 |
| `python run.py calibrate` | stage 5 |
| `python run.py analyze` | stages 6–8 |
| `python run.py menu` | graphical browser |
| `python run.py explore` | terminal browser |
| `python run.py simulate` | how restrictive each filter threshold is |

---

# The three Yahoo endpoints

No key is needed. Two of the three require a crumb: Yahoo sets a cookie on `fc.yahoo.com`,
then issues a token tied to it.

```python
import requests

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                                      "Chrome/128.0.0.0 Safari/537.36"})
session.get("https://fc.yahoo.com", timeout=20)

# The crumb endpoint serves text/plain and answers 406 to an Accept: application/json
# header, so ask for anything.
crumb = session.get("https://query1.finance.yahoo.com/v1/test/getcrumb",
                    headers={"Accept": "*/*"}, timeout=20).text.strip()
```

Implemented in `screener/yahoo.py`, `Yahoo._authorise`.

---

## 1. Screener — which stocks exist

`POST https://query2.finance.yahoo.com/v1/finance/screener`

Used in stage 1 to list the market, and in stage 2 via the `predefined/saved` variant for
the curated screens.

```python
body = {
    "size": 250, "offset": 0,
    "sortField": "dayvolume", "sortType": "desc",
    "quoteType": "equity",
    "query": {"operator": "and", "operands": [
        {"operator": "eq", "operands": ["exchange", "NMS"]},        # Nasdaq Global Select
        {"operator": "gt", "operands": ["dayvolume", 50000]},
    ]},
    "userId": "", "userIdType": "guid",
}
response = session.post("https://query2.finance.yahoo.com/v1/finance/screener",
                        params={"crumb": crumb}, json=body, timeout=30)
result = response.json()["finance"]["result"][0]
print(result["total"])                       # 1249  — matches, not returned
for quote in result["quotes"][:3]:
    print(quote["symbol"], quote["regularMarketPrice"], quote["marketCap"])
```

```
1249
NVDA 227.21 5486440087552
INTC 115.93 612818157568
GRAB 3.12 12764861440
```

**The trap.** `total` reports every match, but pagination stops serving rows at about
10,000 — silently. Asking for all US equities in one query returns 10,000 of 20,207 and
looks complete. `screener/yahoo.py` therefore queries one exchange at a time, and
`world()` slices each region by price band, splitting any band still over the ceiling by
volume.

Curated screens use the same endpoint under `/predefined/saved?scrIds=day_gainers`.
Working screen ids: `day_gainers`, `day_losers`, `most_actives`, `small_cap_gainers`,
`aggressive_small_caps`, `growth_technology_stocks`, `undervalued_growth_stocks`,
`undervalued_large_caps`, `most_shorted_stocks`.

---

## 2. quoteSummary — analyst consensus and fundamentals

`GET https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}`

Stage 4. This is what the buy/hold/sell filter runs on.

```python
modules = ("financialData,recommendationTrend,upgradeDowngradeHistory,"
           "defaultKeyStatistics,earningsTrend,institutionOwnership,"
           "insiderTransactions,majorHoldersBreakdown,summaryDetail,price,calendarEvents")
response = session.get("https://query2.finance.yahoo.com/v10/finance/quoteSummary/ET",
                       params={"modules": modules, "crumb": crumb}, timeout=30)
data = response.json()["quoteSummary"]["result"][0]

trend = data["recommendationTrend"]["trend"][0]      # [0] is now, [1] a month ago
financial = data["financialData"]
print(trend["strongBuy"], trend["buy"], trend["hold"], trend["sell"])
print(financial["recommendationKey"],
      financial["numberOfAnalystOpinions"]["raw"],
      financial["targetMeanPrice"]["raw"])
```

```
5 15 3 0
strong_buy 23 24.7
```

That is Energy Transfer as the pipeline sees it: 20 buys, 3 holds, no sells — which is why
it clears the ≥20 buy / 0 sell rule.

**Two traps.** Numbers arrive wrapped as `{"raw": 24.7, "fmt": "24.70"}`, so a bare
`financial["targetMeanPrice"]` is a dict, not a number — `screener/yahoo.py` has `raw()`
for this. And `financialData.financialCurrency` is the *reporting* currency, which often
differs from the trading currency: TSM trades in USD and reports in TWD, so its free cash
flow of 730,826,014,720 is New Taiwan dollars, about US$23B rather than US$730B.

---

## 3. Chart — price history

`GET https://query1.finance.yahoo.com/v8/finance/chart/{symbol}`

Stage 3, and again whenever the menu draws a chart. No crumb required.

```python
response = session.get("https://query1.finance.yahoo.com/v8/finance/chart/NVDA",
                       params={"range": "1y", "interval": "1d"}, timeout=30)
result = response.json()["chart"]["result"][0]

quote = result["indicators"]["quote"][0]
closes = result["indicators"]["adjclose"][0]["adjclose"]   # split-adjusted
print(len(result["timestamp"]), "sessions")
print("last close", closes[-1])
print("high/low   ", quote["high"][-1], quote["low"][-1])
```

```
251 sessions
last close 227.21
high/low    232.82 227.02
```

**The trap.** Prefer `adjclose` over `quote["close"]`: the raw series is not
split-adjusted, and a split shows up as a fake ±50% move that wrecks every indicator.
`high` and `low` matter too — ATR and ADX need real intraday ranges, and substituting
closes understates a stock's true daily swing by roughly a third.

---

## Rate limits

No published limit, but sustained use gets 429s. `screener/yahoo.py` pauses 0.12s between
calls, retries 429 with a growing wait, and re-authorises once on a 401, since crumbs
expire. Eight concurrent chart requests and six concurrent quoteSummary requests have run
clean; well above that starts drawing refusals.

---

*Informational only — not investment advice.*
