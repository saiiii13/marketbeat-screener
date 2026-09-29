"""Find stocks using MarketBeat's own research tools instead of picking them ourselves.

MarketBeat publishes curated lists - breakouts, unusual volume, fresh upgrades, insider
buying, institutional accumulation, golden crosses. Those lists are the site doing the
searching, and they surface obscure names a size-weighted score never would.

Each stock keeps a record of which lists it came from, so a candidate can be judged by
how many independent MarketBeat tools flagged it rather than by how famous it is.
"""
import collections
import re
import time

from bs4 import BeautifulSoup

from . import db, notes
from .pull_detail import get_html

BASE = "https://www.marketbeat.com"

# Every MarketBeat tool that surfaces stocks worth looking at, taken from their sitemap.
# (path, label, weight). Weight is how much the list says about a near-term move; the
# broad "what kind of stock is this" lists carry a low weight because they describe a
# category rather than a signal, but they still widen the net.
SOURCES = [
    # --- analyst activity
    ("/ratings/upgrades/", "analyst upgrades", 1.0),
    ("/ratings/initiations/", "new coverage started", 0.8),
    ("/ratings/pricetargetchanges/", "price target raised", 0.7),
    ("/ratings/stock-picks-from-top-rated-analysts/", "top-rated analysts' picks", 1.0),
    ("/stocks/most-upgraded/", "most upgraded", 1.0),
    ("/stocks/top-rated/", "top rated overall", 0.9),
    ("/stocks/top-marketrank/", "top MarketRank", 0.9),
    ("/stocks/top-rated-small-cap-stocks/", "top rated small-cap", 0.9),
    ("/stocks/top-rated-tech-stocks/", "top rated tech", 0.7),

    # --- price action and trend
    ("/market-data/breakout-stocks/", "breaking out", 0.9),
    ("/market-data/uptrend-stocks/", "in an uptrend", 0.8),
    ("/stocks/golden-cross-stocks/", "golden cross", 0.9),
    ("/market-data/gap-up-stocks/", "gapped up", 0.7),
    ("/market-data/two-day-gainers/", "2-day gainer", 0.4),
    ("/market-data/three-day-gainers/", "3-day gainer", 0.5),
    ("/market-data/four-day-gainers/", "4-day gainer", 0.5),
    ("/market-data/seven-day-gainers/", "7-day gainer", 0.6),
    ("/market-data/thirty-day-gainers/", "30-day gainer", 0.7),
    ("/market-data/ninety-day-gainers/", "90-day gainer", 0.6),
    ("/market-data/oversold-stocks-rsi/", "oversold on RSI", 0.6),

    # --- volume and flow
    ("/market-data/unusual-volume-stocks/", "unusual volume", 0.8),
    ("/market-data/most-active-stocks/", "most active", 0.4),
    ("/market-data/unusual-call-options-volume/", "unusual call buying", 0.8),
    ("/stocks/institutional-buying-tracker/", "institutions buying", 0.8),
    ("/insider-trades/insider-buying-stocks/", "insiders buying", 0.9),
    ("/congress-stock-trades/most-purchased-stocks/", "bought by Congress", 0.6),

    # --- short interest
    ("/short-interest/short-interest-decreases/", "shorts covering", 0.7),
    ("/short-interest/short-interest-increases/", "short interest rising", 0.3),

    # --- valuation and growth
    ("/market-data/low-pe-growth-stocks/", "growth at a low multiple", 0.6),
    ("/market-data/high-pe-growth-stocks/", "growth at a high multiple", 0.4),
    ("/market-data/low-pe-stocks/", "low P/E", 0.5),
    ("/market-data/low-priced-stocks/stocks-on-sale/", "on sale vs its own range", 0.6),

    # --- dividends and buybacks
    ("/dividends/top-rated-dividend-stocks/", "top rated dividend payer", 0.6),
    ("/dividends/best-dividend-stocks/", "best dividend stocks", 0.5),
    ("/dividends/cheap-dividend-stocks/", "cheap dividend stocks", 0.5),
    ("/stock-buybacks/", "buying back stock", 0.7),

    # --- attention
    ("/stocks/trending/", "trending on MarketBeat", 0.6),
    ("/stocks/trending/media-sentiment/", "high media sentiment", 0.7),
    ("/stocks/trending/media/", "heavy media coverage", 0.4),

    # --- corners of the market a size-weighted score would never reach
    ("/types-of-stock/micro-cap-stocks/", "micro-cap", 0.3),
    ("/types-of-stock/small-cap-stocks/", "small-cap", 0.3),
    ("/types-of-stock/penny-stocks/top-penny-stocks-today/", "top penny stock today", 0.5),
    ("/types-of-stock/biotech-stocks/", "biotech", 0.2),
    ("/types-of-stock/5g-stocks/", "5G", 0.2),
    ("/types-of-stock/gold-stocks/", "gold", 0.2),
    ("/types-of-stock/oil-stocks/", "oil", 0.2),
    ("/types-of-stock/water-stocks/", "water", 0.2),
    ("/types-of-stock/travel-stocks/", "travel", 0.2),
    ("/types-of-stock/marijuana-stocks/", "cannabis", 0.2),
    ("/types-of-stock/lumber-stocks/", "lumber", 0.2),
    ("/types-of-stock/special-purpose-acquisition-companies-spacs/", "SPAC", 0.2),
    ("/types-of-stock/warren-buffett-stocks/", "Buffett holding", 0.4),
    ("/types-of-stock/blue-chip-stocks/", "blue chip", 0.3),

    # --- sector leaders, so no sector is missed entirely
    ("/stocks/sectors/technology/", "technology sector", 0.2),
    ("/stocks/sectors/healthcare/", "healthcare sector", 0.2),
    ("/stocks/sectors/finance/", "finance sector", 0.2),
    ("/stocks/sectors/energy/", "energy sector", 0.2),
    ("/stocks/sectors/industrials/", "industrials sector", 0.2),
    ("/stocks/sectors/consumer-discretionary/", "consumer discretionary sector", 0.2),
    ("/stocks/sectors/consumer-staples/", "consumer staples sector", 0.2),
    ("/stocks/sectors/materials/", "materials sector", 0.2),
    ("/stocks/sectors/real-estate/", "real estate sector", 0.2),
    ("/stocks/sectors/utilities/", "utilities sector", 0.2),
    ("/stocks/sectors/communication-services/", "communication services sector", 0.2),

    # --- events
    ("/ipos/quiet-period-expirations/", "IPO quiet period ending", 0.5),
    ("/ipos/lockup-expirations/", "IPO lockup expiring", 0.3),
]

# Lists that say a stock is in trouble. Collected too, so a name flagged by both a
# bullish and a bearish tool can be recognised rather than silently promoted.
BEARISH_SOURCES = [
    ("/ratings/downgrades/", "analyst downgrades"),
    ("/stocks/most-downgraded/", "most downgraded"),
    ("/stocks/lowest-rated/", "lowest rated"),
    ("/stocks/death-cross-stocks/", "death cross"),
    ("/market-data/gap-down-stocks/", "gapped down"),
    ("/market-data/overbought-stocks-rsi/", "overbought on RSI"),
    ("/stocks/institutional-selling-tracker/", "institutions selling"),
    ("/insider-trades/insider-selling-stocks/", "insiders selling"),
    ("/short-interest/stocks-to-short/", "flagged as a short"),
    ("/market-data/trading-halts/", "trading halted"),
]

STOCK_LINK = re.compile(r"/stocks/([A-Z]+)/([A-Z0-9.\-]+)/")


def tickers_on(html: str) -> list[tuple[str, str]]:
    """Every stock the page links to, as (ticker, exchange)."""
    soup = BeautifulSoup(html, "lxml")
    found: list[tuple[str, str]] = []
    seen = set()
    for a in soup.select("a.profile-link, table a[href*='/stocks/']"):
        m = STOCK_LINK.search(str(a.get("href") or ""))
        if not m:
            continue
        exchange, ticker = m.group(1), m.group(2).upper()
        if ticker in seen:
            continue
        seen.add(ticker)
        found.append((ticker, exchange))
    return found


def run(session, limit_per_list: int = 200) -> dict[str, dict]:
    """Walk MarketBeat's research tools and collect everything they flag."""
    notes.stage("Stage 0 — letting MarketBeat's own tools find the stocks")
    hits: dict[str, dict] = {}
    t0 = time.time()

    for path, label, weight in SOURCES:
        try:
            found = tickers_on(get_html(session, BASE + path))[:limit_per_list]
        except Exception as exc:
            print(f"[discover] {label}: failed ({type(exc).__name__})")
            notes.line(f"**{label}** — could not read this list ({type(exc).__name__})")
            continue
        for ticker, exchange in found:
            row = hits.setdefault(ticker, {"ticker": ticker, "exchange": exchange,
                                           "sources": [], "weight": 0.0})
            if label not in row["sources"]:
                row["sources"].append(label)
                row["weight"] += weight
        print(f"[discover] {label}: {len(found)} stocks ({len(hits):,} unique so far)")
        notes.line(f"**{label}** — {len(found)} stocks, {len(hits):,} unique so far")
        time.sleep(0.6)

    for path, label in BEARISH_SOURCES:
        try:
            found = tickers_on(get_html(session, BASE + path))[:limit_per_list]
        except Exception as exc:
            print(f"[discover] {label}: failed ({type(exc).__name__})")
            continue
        flagged = 0
        for ticker, _ in found:
            row = hits.get(ticker)
            if row is not None:
                row.setdefault("warnings", [])
                if label not in row["warnings"]:
                    row["warnings"].append(label)
                    flagged += 1
        print(f"[discover] {label}: {len(found)} stocks, {flagged} of ours flagged")
        time.sleep(0.6)

    multi = sum(1 for r in hits.values() if len(r["sources"]) > 1)
    notes.line(f"MarketBeat's tools surfaced **{len(hits):,} stocks**; "
               f"{multi:,} appear on more than one list.")
    print(f"[discover] {len(hits):,} unique stocks from {len(SOURCES)} MarketBeat tools "
          f"in {time.time() - t0:.0f}s; {multi:,} on multiple lists")

    with db.connect() as con:
        db.set_meta(con, "discovered",
                    {k: {"sources": v["sources"], "weight": v["weight"],
                         "warnings": v.get("warnings", [])} for k, v in hits.items()})
        db.upsert_universe(con, [{"ticker": r["ticker"], "exchange": r["exchange"],
                                  "company": "", "url": f"{BASE}/stocks/{r['exchange']}/{r['ticker']}/"}
                                 for r in hits.values()])
    return hits


def ranked(hits: dict[str, dict], top_n: int) -> list[str]:
    """Order by how many independent MarketBeat tools flagged the stock."""
    for r in hits.values():
        r["net_weight"] = r["weight"] - 0.5 * len(r.get("warnings", []))
    rows = sorted(hits.values(),
                  key=lambda r: (-r["net_weight"], -len(r["sources"]), r["ticker"]))
    counts = collections.Counter(len(r["sources"]) for r in rows[:top_n])
    print("[discover] finalists by number of lists: " +
          ", ".join(f"{k} list(s): {v}" for k, v in sorted(counts.items(), reverse=True)))
    return [r["ticker"] for r in rows[:top_n]]
