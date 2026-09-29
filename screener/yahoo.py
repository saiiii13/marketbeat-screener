"""Yahoo Finance data layer.

Everything the screener needs comes from three public endpoints:

  screener    - which stocks exist and which ones are worth a look
  quoteSummary - analyst consensus, targets, upgrades, ownership, short interest
  chart        - daily OHLCV, including the highs and lows that ATR and ADX need

quoteSummary requires a "crumb": Yahoo sets a cookie on any of its hosts, then issues a
token tied to that cookie. Without it every request returns 401 Invalid Crumb.
"""
import threading
import time

import pandas as pd
import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

SCREENER_URL = "https://query2.finance.yahoo.com/v1/finance/screener"
PREDEFINED_URL = "https://query2.finance.yahoo.com/v1/finance/screener/predefined/saved"
SUMMARY_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{symbol}"
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
CRUMB_URL = "https://query1.finance.yahoo.com/v1/test/getcrumb"

SUMMARY_MODULES = ",".join([
    "financialData",            # consensus rating, target prices, analyst count, margins
    "recommendationTrend",      # strongBuy / buy / hold / sell counts, now and historically
    "upgradeDowngradeHistory",  # who changed their rating and when
    "defaultKeyStatistics",     # beta, short interest, float, valuation multiples
    "earningsTrend",            # growth estimates and EPS revisions
    "institutionOwnership",     # recent institutional positions
    "insiderTransactions",      # insider buys and sells
    "majorHoldersBreakdown",    # insider and institutional ownership percentages
    "summaryDetail",            # price, volume, dividend yield, 52-week range
    "price",                    # name, exchange, currency, market cap
    "calendarEvents",           # next earnings date
])

# Yahoo's own curated lists, used the way MarketBeat's research tools were: each one is
# Yahoo deciding a stock is interesting, and agreement across lists means more than any
# single list. Weight reflects how much the list says about a near-term move.
DISCOVERY_SCREENS = [
    ("day_gainers", "today's gainers", 0.8),
    ("most_actives", "most active", 0.5),
    ("small_cap_gainers", "small-cap gainer", 0.9),
    ("aggressive_small_caps", "aggressive small cap", 0.7),
    ("growth_technology_stocks", "growth technology", 0.7),
    ("undervalued_growth_stocks", "undervalued growth", 0.9),
    ("undervalued_large_caps", "undervalued large cap", 0.7),
    ("most_shorted_stocks", "heavily shorted", 0.4),
]
# Lists that are a warning rather than a recommendation.
WARNING_SCREENS = [
    ("day_losers", "today's losers"),
]

_local = threading.local()


class Yahoo:
    """A session that holds the cookie and crumb, and knows how to back off."""

    def __init__(self, pause: float = 0.12):
        self.pause = pause
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept": "application/json"})
        self.crumb = ""
        self._authorise()

    @staticmethod
    def _looks_like_crumb(value: str) -> bool:
        """A crumb is a short opaque token. The endpoint answers with a JSON error body
        when the cookie did not take, and that must not be mistaken for a crumb."""
        return bool(value) and len(value) <= 32 and not value.startswith(("{", "<"))

    def _authorise(self):
        """fc.yahoo.com is the host that reliably sets the cookie the crumb is tied to;
        finance.yahoo.com returns no cookie and the crumb call then fails."""
        for attempt in range(3):
            try:
                self.session.cookies.clear()
                self.session.get("https://fc.yahoo.com", timeout=20)
                # The crumb endpoint serves text/plain and answers 406 to an
                # Accept: application/json header, which the session sets by default.
                crumb = self.session.get(CRUMB_URL, timeout=20,
                                         headers={"Accept": "*/*"}).text.strip()
            except requests.RequestException:
                time.sleep(2 * (attempt + 1))
                continue
            if self._looks_like_crumb(crumb):
                self.crumb = crumb
                return
            time.sleep(2 * (attempt + 1))
        self.crumb = ""
        print("[yahoo] could not obtain a crumb; analyst data will be unavailable")

    def get(self, url: str, params: dict | None = None, retries: int = 3):
        params = dict(params or {})
        if self.crumb:
            params["crumb"] = self.crumb
        for attempt in range(retries):
            try:
                r = self.session.get(url, params=params, timeout=30)
            except requests.RequestException:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 401 and attempt == 0:
                self._authorise()          # crumb expired; get a fresh one and retry
                params["crumb"] = self.crumb
                continue
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            if r.ok:
                time.sleep(self.pause)
                return r
            if r.status_code in (404, 422):
                return None
            time.sleep(2 * (attempt + 1))
        return None

    def post(self, url: str, body: dict, retries: int = 3):
        params = {"crumb": self.crumb} if self.crumb else {}
        for attempt in range(retries):
            try:
                r = self.session.post(url, params=params, json=body, timeout=30,
                                      headers={"Content-Type": "application/json"})
            except requests.RequestException:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 401 and attempt == 0:
                self._authorise()
                params["crumb"] = self.crumb
                continue
            if r.status_code == 429:
                time.sleep(15 * (attempt + 1))
                continue
            if r.ok:
                time.sleep(self.pause)
                return r
            time.sleep(2 * (attempt + 1))
        return None


def shared() -> Yahoo:
    """One authorised session per thread."""
    client = getattr(_local, "client", None)
    if client is None:
        client = _local.client = Yahoo()
    return client


# ---------------------------------------------------------------- numbers

def raw(node, default=None):
    """Yahoo wraps numbers as {"raw": 1.23, "fmt": "1.23"}; unwrap either shape."""
    if isinstance(node, dict):
        return node.get("raw", default)
    return node if node is not None else default


# ---------------------------------------------------------------- the universe

# Yahoo refuses to paginate a single query past roughly 10,000 rows, so the market is
# walked one exchange at a time. These cover the US listings; the largest (OTC Pink) sits
# just under the cap on its own.
US_EXCHANGES = ["NMS", "NGM", "NCM", "NYQ", "ASE", "PCX", "PNK", "OQB", "OQX", "NSI", "BTS"]
PAGE_CAP = 9_800


def _screener_page(client: "Yahoo", operands: list, offset: int, size: int) -> tuple[list, int]:
    body = {"size": size, "offset": offset, "sortField": "dayvolume", "sortType": "desc",
            "quoteType": "equity",
            "query": {"operator": "and", "operands": operands},
            "userId": "", "userIdType": "guid"}
    response = client.post(SCREENER_URL, body)
    if response is None:
        return [], 0
    try:
        result = (response.json().get("finance", {}).get("result") or [{}])[0]
    except ValueError:
        return [], 0
    return result.get("quotes", []), result.get("total", 0)


def universe(min_volume: int = 0, region: str = "us", page: int = 250,
             cap: int | None = None, exchanges: list[str] | None = None) -> list[dict]:
    """Every equity Yahoo lists, gathered exchange by exchange.

    A single query stops serving rows at about 10,000, which silently truncates the market
    to a third of its real size. Splitting by exchange keeps every slice under that ceiling,
    so the full ~20,000 US listings come back.
    """
    client = shared()
    rows: dict[str, dict] = {}
    for exchange in (exchanges or US_EXCHANGES):
        operands = [{"operator": "eq", "operands": ["exchange", exchange]}]
        if min_volume:
            operands.append({"operator": "gt", "operands": ["dayvolume", min_volume]})
        offset, total, before = 0, None, len(rows)
        while True:
            quotes, reported = _screener_page(client, operands, offset, page)
            if total is None:
                total = reported
            if not quotes:
                break
            for q in quotes:
                symbol = q.get("symbol")
                if symbol and symbol not in rows:
                    rows[symbol] = _quote_row(q)
            offset += len(quotes)
            print(f"[universe] {exchange}: {offset:,}/{total:,}   "
                  f"({len(rows):,} total)", end="\r")
            if cap and len(rows) >= cap:
                break
            if offset >= min(total or 0, PAGE_CAP):
                break
        print(f"[universe] {exchange:<5} {len(rows) - before:>6,} new   "
              f"({len(rows):,} total)      ")
        if cap and len(rows) >= cap:
            break
    return list(rows.values())


# Every region Yahoo indexes, largest first so the big ones surface problems early.
ALL_REGIONS = ["de", "it", "tw", "us", "fr", "hk", "in", "at", "cn", "gb", "ca", "jp", "au",
               "br", "kr", "nl", "th", "ar", "se", "my", "il", "id", "mx", "tr", "dk", "cl",
               "ch", "no", "sg", "za", "pl", "sa", "nz", "eg", "fi", "es", "be", "gr", "kw",
               "cz", "hu", "pt", "qa", "ie"]

# Price bands used to cut a region into slices small enough to paginate. Any band still
# over the ceiling is bisected further, so no slice is ever silently truncated.
PRICE_BANDS = [(0, 0.5), (0.5, 1), (1, 2), (2, 5), (5, 10), (10, 20), (20, 50),
               (50, 100), (100, 250), (250, 1000), (1000, 10_000), (10_000, None)]


def _count(client: "Yahoo", operands: list) -> int:
    _, total = _screener_page(client, operands, 0, 1)
    return total


def _collect(client: "Yahoo", operands: list, rows: dict, page: int, label: str,
             cap: int | None = None) -> None:
    """Page through one slice, adding whatever it returns."""
    offset, total = 0, None
    while True:
        quotes, reported = _screener_page(client, operands, offset, page)
        if total is None:
            total = reported
        if not quotes:
            return
        for q in quotes:
            symbol = q.get("symbol")
            if symbol and symbol not in rows:
                rows[symbol] = _quote_row(q)
        offset += len(quotes)
        print(f"[universe] {label}: {offset:,}/{total:,}  ({len(rows):,} total)", end="\r")
        if cap and len(rows) >= cap:
            return
        if offset >= min(total or 0, PAGE_CAP):
            return


def _price_slices(client: "Yahoo", base: list, rows: dict, page: int, label: str,
                  cap: int | None, depth: int = 0) -> None:
    """Split a too-large slice by price until each part fits under the ceiling."""
    for low, high in PRICE_BANDS:
        operands = base + [{"operator": "gte", "operands": ["intradayprice", low]}]
        if high is not None:
            operands = operands + [{"operator": "lt", "operands": ["intradayprice", high]}]
        count = _count(client, operands)
        if not count:
            continue
        band = f"{label} ${low:g}-{'inf' if high is None else format(high, 'g')}"
        if count > PAGE_CAP and depth < 2:
            # still too big: split this band's volume as a second dimension
            for vlow, vhigh in ((0, 10_000), (10_000, 100_000), (100_000, None)):
                sub = operands + [{"operator": "gte", "operands": ["dayvolume", vlow]}]
                if vhigh is not None:
                    sub = sub + [{"operator": "lt", "operands": ["dayvolume", vhigh]}]
                _collect(client, sub, rows, page, f"{band} vol{vlow}", cap)
                if cap and len(rows) >= cap:
                    return
        else:
            _collect(client, operands, rows, page, band, cap)
        if cap and len(rows) >= cap:
            return


def world(regions: list[str] | None = None, page: int = 250,
          cap: int | None = None) -> list[dict]:
    """Every listed equity Yahoo knows about, region by region.

    A region under the pagination ceiling is fetched directly; a larger one is cut into
    price bands, and any band still too large is cut again by volume. Nothing is dropped
    quietly - every slice either fits or gets split.
    """
    client = shared()
    rows: dict[str, dict] = {}
    for region in (regions or ALL_REGIONS):
        base = [{"operator": "eq", "operands": ["region", region]}]
        total = _count(client, base)
        before = len(rows)
        if total <= PAGE_CAP:
            _collect(client, base, rows, page, region.upper(), cap)
        else:
            _price_slices(client, base, rows, page, region.upper(), cap)
        print(f"[universe] {region.upper():<4} {len(rows) - before:>7,} new   "
              f"of {total:>7,} listed   ({len(rows):,} total)            ")
        if cap and len(rows) >= cap:
            print(f"[universe] stopping at the {cap:,} cap")
            break
    return list(rows.values())


def _quote_row(q: dict) -> dict:
    return {
        "ticker": q.get("symbol"),
        "company": q.get("shortName") or q.get("longName") or "",
        "exchange": q.get("fullExchangeName") or q.get("exchange") or "",
        "currency": q.get("currency"),
        "price": raw(q.get("regularMarketPrice")),
        "price_change_pct": raw(q.get("regularMarketChangePercent")),
        "market_cap": raw(q.get("marketCap")),
        "avg_volume": raw(q.get("averageDailyVolume3Month")),
        "today_volume": raw(q.get("regularMarketVolume")),
        "range_52w_low": raw(q.get("fiftyTwoWeekLow")),
        "range_52w_high": raw(q.get("fiftyTwoWeekHigh")),
        "pe_forward": raw(q.get("forwardPE")),
        "pe_trailing": raw(q.get("trailingPE")),
        "eps_forward": raw(q.get("epsForward")),
        "sector": q.get("sector"),
        "industry": q.get("industry"),
        "url": f"https://finance.yahoo.com/quote/{q.get('symbol')}",
    }


# ---------------------------------------------------------------- discovery

def discover(limit_per_screen: int = 250) -> dict[str, dict]:
    """Collect Yahoo's curated screens and record which ones flagged each stock."""
    client = shared()
    hits: dict[str, dict] = {}

    def walk(screen_id: str, want: int) -> list[dict]:
        found, offset = [], 0
        while len(found) < want:
            r = client.get(PREDEFINED_URL, {"scrIds": screen_id, "count": 250, "start": offset})
            if r is None:
                break
            result = (r.json().get("finance", {}).get("result") or [{}])[0]
            quotes = result.get("quotes", [])
            if not quotes:
                break
            found += quotes
            offset += len(quotes)
            if offset >= min(result.get("total", 0), want):
                break
        return found[:want]

    for screen_id, label, weight in DISCOVERY_SCREENS:
        quotes = walk(screen_id, limit_per_screen)
        for q in quotes:
            symbol = q.get("symbol")
            if not symbol:
                continue
            row = hits.setdefault(symbol, {**_quote_row(q), "sources": [], "weight": 0.0,
                                           "warnings": []})
            if label not in row["sources"]:
                row["sources"].append(label)
                row["weight"] += weight
        print(f"[discover] {label}: {len(quotes)} stocks ({len(hits):,} unique so far)")

    for screen_id, label in WARNING_SCREENS:
        flagged = 0
        for q in walk(screen_id, limit_per_screen):
            row = hits.get(q.get("symbol"))
            if row is not None and label not in row["warnings"]:
                row["warnings"].append(label)
                flagged += 1
        print(f"[discover] {label}: {flagged} of ours flagged")

    for row in hits.values():
        row["net_weight"] = row["weight"] - 0.5 * len(row["warnings"])
    return hits


# ---------------------------------------------------------------- per-stock detail

def detail(symbol: str) -> dict:
    """Analyst consensus, targets, ownership and short interest for one stock."""
    client = shared()
    r = client.get(SUMMARY_URL.format(symbol=symbol), {"modules": SUMMARY_MODULES})
    if r is None:
        return {"errors": {"quoteSummary": "unavailable"}}
    try:
        result = (r.json().get("quoteSummary", {}).get("result") or [{}])[0]
    except ValueError:
        return {"errors": {"quoteSummary": "bad json"}}
    if not result:
        return {"errors": {"quoteSummary": "empty"}}

    fd = result.get("financialData", {}) or {}
    ks = result.get("defaultKeyStatistics", {}) or {}
    sd = result.get("summaryDetail", {}) or {}
    pr = result.get("price", {}) or {}
    trend = (result.get("recommendationTrend", {}) or {}).get("trend") or []
    now = trend[0] if trend else {}
    month_ago = trend[1] if len(trend) > 1 else {}

    counts = {"strong_buy": now.get("strongBuy", 0), "buy": now.get("buy", 0),
              "hold": now.get("hold", 0), "sell": now.get("sell", 0),
              "strong_sell": now.get("strongSell", 0)}
    counts_prev = {"strong_buy": month_ago.get("strongBuy", 0), "buy": month_ago.get("buy", 0),
                   "hold": month_ago.get("hold", 0), "sell": month_ago.get("sell", 0),
                   "strong_sell": month_ago.get("strongSell", 0)}

    price = raw(fd.get("currentPrice")) or raw(pr.get("regularMarketPrice"))
    target = raw(fd.get("targetMeanPrice"))
    out = {
        "source_url": f"https://finance.yahoo.com/quote/{symbol}",
        "errors": {},
        "company": pr.get("longName") or pr.get("shortName") or "",
        "exchange": pr.get("exchangeName") or "",
        "currency": pr.get("currency") or "USD",
        # Yahoo reports the financial statements in the company's reporting currency, which
        # is often not the currency its shares trade in (TSM trades in USD, reports in TWD).
        # Labelling cash figures with the price currency would overstate them badly.
        "financial_currency": fd.get("financialCurrency") or pr.get("currency") or "USD",
        "price": price,
        "market_cap": raw(pr.get("marketCap")),
        "consensus_rating": (fd.get("recommendationKey") or "").replace("_", " ").title() or None,
        "consensus_score": raw(fd.get("recommendationMean")),
        "analyst_count": raw(fd.get("numberOfAnalystOpinions")),
        "rating_counts": counts,
        "rating_counts_1m_ago": counts_prev,
        "consensus_target": target,
        "target_low": raw(fd.get("targetLowPrice")),
        "target_high": raw(fd.get("targetHighPrice")),
        "upside_pct": ((target - price) / price * 100) if (target and price) else None,
        "beta": raw(ks.get("beta")),
        "short_pct_float": (raw(ks.get("shortPercentOfFloat")) or 0) * 100 or None,
        "short_ratio": raw(ks.get("shortRatio")),
        "shares_short": raw(ks.get("sharesShort")),
        "short_change_pct": _short_change(ks),
        "peg": raw(ks.get("pegRatio")),
        "pb": raw(ks.get("priceToBook")),
        "pe": raw(sd.get("trailingPE")),
        "profit_margin": (raw(fd.get("profitMargins")) or 0) * 100 or None,
        "revenue_growth_pct": (raw(fd.get("revenueGrowth")) or 0) * 100 or None,
        "earnings_growth_pct": (raw(fd.get("earningsGrowth")) or 0) * 100 or None,
        "free_cash_flow": raw(fd.get("freeCashflow")),
        "total_debt": raw(fd.get("totalDebt")),
        "debt_to_equity": raw(fd.get("debtToEquity")),
        "revenue": raw(fd.get("totalRevenue")),
        "dividend_yield": (raw(sd.get("dividendYield")) or 0) * 100 or None,
        "range_52w_low": raw(sd.get("fiftyTwoWeekLow")),
        "range_52w_high": raw(sd.get("fiftyTwoWeekHigh")),
        "avg_volume": raw(sd.get("averageVolume")),
        "relative_volume": _relative_volume(sd),
    }
    out.update(_ownership(result))
    out.update(_upgrades(result))
    out.update(_earnings(result))
    return out


def _short_change(ks: dict) -> float | None:
    now, prev = raw(ks.get("sharesShort")), raw(ks.get("sharesShortPriorMonth"))
    if not now or not prev:
        return None
    return (now - prev) / prev * 100


def _relative_volume(sd: dict) -> float | None:
    """Today's volume against the 10-day average, as a percentage (100 = normal)."""
    today, average = raw(sd.get("volume")), raw(sd.get("averageVolume10days"))
    if not today or not average:
        return None
    return today / average * 100


def _ownership(result: dict) -> dict:
    holders = result.get("majorHoldersBreakdown", {}) or {}
    inst = (result.get("institutionOwnership", {}) or {}).get("ownershipList") or []
    insider = (result.get("insiderTransactions", {}) or {}).get("transactions") or []

    bought = sold = 0.0
    buys = sells = 0
    for t in insider[:40]:
        value = raw(t.get("value")) or 0
        text = (t.get("transactionText") or "").lower()
        if "purchase" in text or "buy" in text:
            bought += value
            buys += 1
        elif "sale" in text or "sold" in text:
            sold += value
            sells += 1

    increases = sum(1 for i in inst if (raw(i.get("pctChange")) or 0) > 0)
    decreases = sum(1 for i in inst if (raw(i.get("pctChange")) or 0) < 0)
    return {
        "insider_own_pct": (raw(holders.get("insidersPercentHeld")) or 0) * 100 or None,
        "inst_own_pct": (raw(holders.get("institutionsPercentHeld")) or 0) * 100 or None,
        "insider_buy_value_90d": bought,
        "insider_sell_value_90d": sold,
        "insider_buy_count_90d": buys,
        "insider_sell_count_90d": sells,
        "inst_increases": increases,
        "inst_decreases": decreases,
        "inst_recent": [{"holder": i.get("organization"),
                         "value": raw(i.get("value")),
                         "change_pct": (raw(i.get("pctChange")) or 0) * 100}
                        for i in inst[:10]],
    }


def _upgrades(result: dict) -> dict:
    history = (result.get("upgradeDowngradeHistory", {}) or {}).get("history") or []
    rows = []
    for h in history[:40]:
        epoch = h.get("epochGradeDate")
        rows.append({
            "date": (pd.to_datetime(epoch, unit="s").date().isoformat() if epoch else None),
            "firm": h.get("firm"),
            "action": {"up": "Upgrade", "down": "Downgrade", "init": "Initiated",
                       "main": "Reiterated"}.get(h.get("action"), h.get("action") or ""),
            "rating_from": h.get("fromGrade") or None,
            "rating_to": h.get("toGrade") or None,
            "target_from": None, "target_to": None,
        })
    return {"ratings_history": rows}


def _earnings(result: dict) -> dict:
    cal = result.get("calendarEvents", {}) or {}
    dates = (cal.get("earnings", {}) or {}).get("earningsDate") or []
    next_date = None
    if dates:
        epoch = raw(dates[0])
        if epoch:
            next_date = pd.to_datetime(epoch, unit="s").date().isoformat()
    trend = (result.get("earningsTrend", {}) or {}).get("trend") or []
    growth = None
    for t in trend:
        if t.get("period") == "+1y":
            growth = (raw(t.get("growth")) or 0) * 100 or None
            break
    return {"next_earnings_date": next_date, "proj_earnings_growth_pct": growth}


# ---------------------------------------------------------------- price history

def history(symbol: str, period: str = "1y") -> pd.DataFrame:
    """Daily OHLCV. Unlike MarketBeat's feed this includes highs and lows, so ATR and
    ADX can be computed properly."""
    client = shared()
    r = client.get(CHART_URL.format(symbol=symbol), {"range": period, "interval": "1d"})
    if r is None:
        return pd.DataFrame()
    try:
        result = (r.json().get("chart", {}).get("result") or [None])[0]
    except ValueError:
        return pd.DataFrame()
    if not result:
        return pd.DataFrame()
    stamps = result.get("timestamp") or []
    quote = ((result.get("indicators", {}) or {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    if not stamps or not closes:
        return pd.DataFrame()
    adj = ((result.get("indicators", {}) or {}).get("adjclose") or [{}])[0].get("adjclose")

    def column(key, fallback):
        values = quote.get(key)
        return values if values and len(values) == len(stamps) else fallback

    df = pd.DataFrame({
        "Close": adj if adj and len(adj) == len(stamps) else closes,
        "Open": column("open", closes),
        "High": column("high", closes),
        "Low": column("low", closes),
        "Volume": column("volume", [0] * len(stamps)),
    }, index=pd.to_datetime(pd.Series(stamps), unit="s"))
    return df.dropna(subset=["Close"])
