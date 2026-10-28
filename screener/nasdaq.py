"""Nasdaq's own data, as a third opinion.

nasdaq.com serves its stock pages from a JSON backend at api.nasdaq.com that needs no key
and no login. It carries the analyst consensus with a twelve-month history, short interest
from the semi-monthly reports, institutional position counts, insider trade counts and the
earnings surprise table - the same ground the MarketBeat tabs cover, from a different
compiler, for nothing.

Two honest caveats. It is not a documented public API: it is the site's own backend, so it
can change without notice and there is no rate-limit contract. And coverage is patchy -
NVDA, SOFI and TSM return a consensus; ET and BRK.B return an empty one. A stock with no
Nasdaq data is reported as unknown, never as a failure.

Keys in the returned dict match the names the rest of the project uses, so the same
fact-check and scoring code reads a Nasdaq row and a MarketBeat row alike.
"""
import re
import time
from concurrent.futures import ThreadPoolExecutor

import requests

BASE = "https://api.nasdaq.com/api"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 25
WORKERS = 6          # one per endpoint: a stock takes about as long as its slowest call


def _num(text) -> float | None:
    """A number out of whatever Nasdaq put in the cell: '$239.42', '79.53%', '1,234'."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    cleaned = re.sub(r"[^\d.\-]", "", str(text).replace(",", ""))
    try:
        return float(cleaned)
    except ValueError:
        return None


def _rows(block, key_field: str) -> dict[str, dict]:
    """A Nasdaq label/value table turned into a lookup by its first column."""
    return {str(r.get(key_field, "")).strip(): r for r in ((block or {}).get("rows") or [])}


class Nasdaq:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    def get(self, path: str, params: dict | None = None) -> dict | None:
        for attempt in range(3):
            try:
                r = self.session.get(f"{BASE}/{path}", params=params, timeout=TIMEOUT)
                if r.ok:
                    return (r.json() or {}).get("data")
                if r.status_code in (429, 503):
                    time.sleep(3 * (attempt + 1))
                    continue
                return None
            except (requests.RequestException, ValueError):
                time.sleep(2 * (attempt + 1))
        return None

    # ---------------------------------------------------------------- one stock

    def detail(self, symbol: str) -> dict:
        """Everything Nasdaq publishes about a stock that bears on whether it is a buy."""
        sym = symbol.upper()
        calls = {
            "info": ("quote/{s}/info", {"assetclass": "stocks"}),
            "target": ("analyst/{s}/targetprice", None),
            "ratings": ("analyst/{s}/ratings", None),
            "short": ("quote/{s}/short-interest", {"assetClass": "stocks"}),
            "inst": ("company/{s}/institutional-holdings", {"limit": 1, "type": "TOTAL"}),
            "insider": ("company/{s}/insider-trades", {"limit": 1, "type": "ALL"}),
            "surprise": ("company/{s}/earnings-surprise", None),
        }
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {name: pool.submit(self.get, path.format(s=sym), params)
                       for name, (path, params) in calls.items()}
            raw = {name: f.result() for name, f in futures.items()}

        out: dict = {"source_url": f"https://www.nasdaq.com/market-activity/stocks/{sym.lower()}",
                     "source": "nasdaq", "errors": {}}
        for name, data in raw.items():
            if data is None:
                out["errors"][name] = "unavailable"
        out.update(self._quote(raw.get("info")))
        out.update(self._consensus(raw.get("target"), raw.get("ratings")))
        out.update(self._institutional(raw.get("inst")))
        # Shares outstanding comes from the ownership endpoint (in millions), so the short
        # percentage can only be worked out once that has been merged in.
        outstanding = out.get("shares_outstanding")
        if not outstanding and out.get("shares_outstanding_millions"):
            outstanding = out["shares_outstanding_millions"] * 1_000_000
        out.update(self._short(raw.get("short"), outstanding))
        out.update(self._insider(raw.get("insider")))
        out.update(self._earnings(raw.get("surprise")))
        if out.get("consensus_target") and out.get("price"):
            out["upside_pct"] = (out["consensus_target"] - out["price"]) / out["price"] * 100
        out["covered"] = bool(out.get("rating_counts") or out.get("consensus_target"))
        # Nasdaq publishes short interest for its own listings only; for a NYSE stock the
        # table is simply empty. That is a coverage gap, not a failed call, so it is noted
        # rather than counted as an error - MarketBeat carries the figure.
        if "short_days_to_cover" not in out and out.get("nasdaq_listed") is False:
            out["errors"].pop("short", None)
            out["short_note"] = "Nasdaq publishes short interest for Nasdaq-listed stocks only"
        return out

    # ---------------------------------------------------------------- per endpoint

    @staticmethod
    def _quote(data: dict | None) -> dict:
        if not data:
            return {}
        primary = data.get("primaryData") or {}
        stats = data.get("keyStats") or {}
        return {
            "company": data.get("companyName") or "",
            "exchange": data.get("exchange") or "",
            "price": _num(primary.get("lastSalePrice")),
            "volume": _num(primary.get("volume")),
            "shares_outstanding": _num((stats.get("SharesOutstanding") or {}).get("value")),
            "nasdaq_listed": data.get("isNasdaqListed"),
        }

    @staticmethod
    def _consensus(target: dict | None, ratings: dict | None) -> dict:
        out: dict = {}
        overview = (target or {}).get("consensusOverview") or {}
        if overview:
            out["consensus_target"] = _num(overview.get("priceTarget"))
            out["target_low"] = _num(overview.get("lowPriceTarget"))
            out["target_high"] = _num(overview.get("highPriceTarget"))
            # Nasdaq publishes the three buckets only - no strong buy / strong sell split.
            out["rating_counts"] = {"buy": int(overview.get("buy") or 0),
                                    "hold": int(overview.get("hold") or 0),
                                    "sell": int(overview.get("sell") or 0)}

        history = [h.get("z") or {} for h in ((target or {}).get("historicalConsensus") or [])]
        if len(history) >= 2:
            month_ago = history[-2]
            out["rating_counts_1m_ago"] = {"buy": int(month_ago.get("buy") or 0),
                                           "hold": int(month_ago.get("hold") or 0),
                                           "sell": int(month_ago.get("sell") or 0)}
            out["consensus_history"] = [
                {"date": h.get("date"), "consensus": h.get("consensus"),
                 "buy": h.get("buy"), "hold": h.get("hold"), "sell": h.get("sell")}
                for h in history[-12:]]

        if ratings:
            out["consensus_rating"] = ratings.get("meanRatingType") or None
            counted = re.search(r"Based on (\d+)", ratings.get("ratingsSummary") or "")
            out["analyst_count"] = float(counted.group(1)) if counted else None
            out["broker_count"] = len(ratings.get("brokerNames") or []) or None
            out["upgrades_downgrades"] = ratings.get("upgradesDowngrades") or []
        return out

    @staticmethod
    def _short(data: dict | None, shares_outstanding: float | None) -> dict:
        rows = ((data or {}).get("shortInterestTable") or {}).get("rows") or []
        if not rows:
            return {}
        now = rows[0]
        out = {
            "short_report_date": now.get("settlementDate"),
            "shares_short": _num(now.get("interest")),
            "short_days_to_cover": _num(now.get("daysToCover")),
            "short_history": [
                {"date": r.get("settlementDate"), "shares_short": _num(r.get("interest")),
                 "days_to_cover": _num(r.get("daysToCover"))} for r in rows[:6]],
        }
        if len(rows) > 1:
            prev = _num(rows[1].get("interest"))
            if prev:
                out["short_change_pct"] = (out["shares_short"] - prev) / prev * 100
        # Nasdaq gives shares short but not the percentage, so it is computed here. Shares
        # outstanding is not float, so this runs slightly low against a float-based figure.
        if out.get("shares_short") and shares_outstanding:
            out["short_pct_float"] = out["shares_short"] / shares_outstanding * 100
        return out

    @staticmethod
    def _institutional(data: dict | None) -> dict:
        if not data:
            return {}
        summary = data.get("ownershipSummary") or {}
        active = _rows(data.get("activePositions"), "positions")
        out = {"inst_own_pct": _num((summary.get("SharesOutstandingPCT") or {}).get("value"))}
        increased = _num((active.get("Increased Positions") or {}).get("holders"))
        decreased = _num((active.get("Decreased Positions") or {}).get("holders"))
        if increased is not None:
            out["inst_increases"] = int(increased)
        if decreased is not None:
            out["inst_decreases"] = int(decreased)
        total = _num((summary.get("ShareoutstandingTotal") or {}).get("value"))
        if total:
            out["shares_outstanding_millions"] = total
        return out

    @staticmethod
    def _insider(data: dict | None) -> dict:
        trades = _rows((data or {}).get("numberOfTrades"), "insiderTrade")
        shares = _rows((data or {}).get("numberOfSharesTraded"), "insiderTrade")
        if not trades and not shares:
            return {}
        out = {}
        buys = _num((trades.get("Number of Open Market Buys") or {}).get("months3"))
        sells = _num((trades.get("Number of Sells") or {}).get("months3"))
        if buys is not None:
            out["insider_buy_count_90d"] = int(buys)
        if sells is not None:
            out["insider_sell_count_90d"] = int(sells)
        out["insider_shares_bought_90d"] = _num((shares.get("Number of Shares Bought") or {}).get("months3"))
        out["insider_shares_sold_90d"] = _num((shares.get("Number of Shares Sold") or {}).get("months3"))
        return out

    @staticmethod
    def _earnings(data: dict | None) -> dict:
        rows = ((data or {}).get("earningsSurpriseTable") or {}).get("rows") or []
        quarters = [r for r in rows if _num(r.get("percentageSurprise")) is not None][:4]
        if not quarters:
            return {}
        surprises = [s for s in (_num(r["percentageSurprise"]) for r in quarters)
                     if s is not None]
        return {
            "beat_rate_4q": sum(1 for s in surprises if s >= 0) / len(surprises),
            "avg_surprise_pct_4q": sum(surprises) / len(surprises),
            "earnings_history": [
                {"quarter": r.get("fiscalQtrEnd"), "date": r.get("dateReported"),
                 "eps_actual": _num(r.get("eps")), "eps_est": _num(r.get("consensusForecast")),
                 "surprise_pct": _num(r.get("percentageSurprise"))} for r in quarters],
        }


_client: Nasdaq | None = None


def detail(symbol: str) -> dict:
    """One stock, through a shared session."""
    global _client
    if _client is None:
        _client = Nasdaq()
    return _client.detail(symbol)
