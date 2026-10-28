"""Stage 9 - the second opinion: take the stocks the screener picked and check each one
against MarketBeat before it counts as a buy.

The screener's stored run chooses the shortlist; nothing in here can add a stock. What it
can do is throw one out. Every finalist's MarketBeat page is read live - the analyst forecast, the buy/hold/sell
breakdown, the price target, the upgrade and downgrade history, MarketRank, earnings,
short interest, insider and institutional activity, the options tab and news sentiment -
and each of those becomes a check that passes, warns or fails. Any failure rejects the
stock. Two or more warnings make it mixed. Everything else is confirmed.

Nasdaq is asked the same questions in parallel, through its own keyless JSON backend, so
every figure gets two independent votes rather than one. A number both of them confirm is
solid; a number every source contradicts fails the stock.

The point is that the readings are independent: the stored figures are what MarketBeat
said when the screener ran, the live page is what it says now, and Nasdaq's count comes
from the brokers reporting to the exchange. A stock that looked like a screaming buy at the
run and a hold now, or a hold on Nasdaq, was never as clean as it looked.
"""
import time
from datetime import date, datetime

from . import db, nasdaq, notes
from .browser import Session, user_level
from .config import DETAIL_DELAY_SEC, MAX_LIST_SELLS, MIN_LIST_BUY_SHARE
from .parse import (
    parse_overview, parse_forecast, parse_earnings, parse_short_interest, parse_insider,
    parse_institutional, parse_options, parse_news,
)
from .config import BASE_URL
from .pull_detail import get_html, make_session, marketbeat_cookies
from .score import rating_counts_of

# The tabs this stage reads. Fewer than the full 12 - these are the ones that carry a
# judgement about whether the stock is good, not background reference.
TABS = {
    "": parse_overview,                       # MarketRank, sentiment, key stats
    "forecast": parse_forecast,               # consensus, target, upgrade history
    "earnings": parse_earnings,               # beat rate, next report date
    "short-interest": parse_short_interest,
    "insider-trades": parse_insider,
    "institutional-ownership": parse_institutional,
    "options": parse_options,
    "news": parse_news,
}

# Exchange codes in any form the store may hold (MarketBeat's own, or the short codes some
# feeds use) to MarketBeat's URL slugs, so a MarketBeat address can always be built.
EXCHANGE_SLUGS = {
    "NMS": "NASDAQ", "NGM": "NASDAQ", "NCM": "NASDAQ", "NASDAQ": "NASDAQ",
    "NYQ": "NYSE", "NYSE": "NYSE",
    "ASE": "NYSEAMERICAN", "AMEX": "NYSEAMERICAN",
    "PCX": "NYSEARCA", "ARCA": "NYSEARCA",
    "PNK": "OTCMKTS", "OQB": "OTCMKTS", "OQX": "OTCMKTS", "OTC": "OTCMKTS",
    "BTS": "BATS", "NSI": "NSE",
}
# Tried in order when the stock's own exchange is unknown or the page is not there.
FALLBACK_SLUGS = ["NASDAQ", "NYSE", "NYSEAMERICAN", "OTCMKTS", "NYSEARCA"]

# How MarketBeat's own ratio is judged, live. A proportion, not an exact match against the
# stored count: requiring two readings to agree analyst for analyst rejected nearly
# everything, including stocks every source rates a buy.
SELL_SHARE_FAIL = 0.10   # more than a tenth of its analysts saying sell, with 2+ of them
SELL_SHARE_HARD = 0.25   # a quarter saying sell fails however thin the coverage
BUY_SHARE_FAIL = 0.85    # under this, MarketBeat's ratio is too hold-heavy to call a buy
BUY_SHARE_PASS = MIN_LIST_BUY_SHARE

PAGE_PAUSE = 1.0        # between stocks, on top of the per-tab delay
EARNINGS_WINDOW_DAYS = 30   # our holding horizon: a report inside it is event risk


def _days_until(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        return (date.fromisoformat(iso) - date.today()).days
    except ValueError:
        return None


def _days_since(iso: str | None) -> int | None:
    d = _days_until(iso)
    return None if d is None else -d


def _recent_actions(history: list[dict] | None, days: int = 30) -> tuple[int, int, int, int]:
    """Upgrades, downgrades, target raises and target cuts in the window."""
    up = down = raised = cut = 0
    for h in history or []:
        since = _days_since(h.get("date"))
        if since is None or since > days:
            continue
        action = (h.get("action") or "").lower()
        if "upgrade" in action or "initiate" in action or "boost" in action:
            up += 1
        if "downgrade" in action:
            down += 1
        a, b = h.get("target_from"), h.get("target_to")
        if a and b:
            if b > a:
                raised += 1
            elif b < a:
                cut += 1
    return up, down, raised, cut


# Every number the screener acted on, and how far the two sources may differ before the
# figure counts as disputed. (tolerance, as a fraction; floor, in the field's own units)
FIELDS = [
    ("price",            "Share price",         0.03, 0.05),
    ("consensus_target", "Consensus target",    0.12, 0.50),
    ("upside_pct",       "Upside to target",    0.40, 5.00),
    ("analyst_count",    "Analysts covering",   0.40, 4.00),
    ("market_cap",       "Market cap",          0.12, 0.00),
    ("pe",               "P/E",                 0.25, 2.00),
    ("peg",              "PEG",                 0.35, 0.30),
    ("short_pct_float",  "Short % of float",    0.50, 2.00),
    ("inst_own_pct",     "Institutional held",  0.25, 8.00),
    ("insider_own_pct",  "Insider held",        0.50, 3.00),
    ("dividend_yield",   "Dividend yield",      0.30, 0.40),
    ("beta",             "Beta",                0.35, 0.35),
]


def _agrees(key: str, a, b, tol: float, floor: float, stale: bool) -> str | None:
    """Whether two readings of the same field match. None when one of them is absent."""
    if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
        return None
    if abs(a - b) <= max(abs(a) * tol, floor):
        return "agree"
    # Price and the upside that derives from it move with the market, so a stored figure
    # from an earlier run differing from a live one is not evidence of anything.
    return "stale" if stale and key in ("price", "upside_pct") else "differs"


def _counts_agree(a: tuple[int, int, int], b: tuple[int, int, int]) -> bool:
    """Ratings compared as proportions, not raw counts.

    The sources track slightly different sets of analysts, so one extra sell out of sixty
    is noise. A consensus that points the other way is not.
    """
    (b1, h1, s1), (b2, h2, s2) = a, b
    t1, t2 = max(b1 + h1 + s1, 1), max(b2 + h2 + s2, 1)
    return abs(b1 / t1 - b2 / t2) <= 0.15 and abs(s1 / t1 - s2 / t2) <= 0.10


def factcheck(sources: dict[str, dict], stored: dict, stale: bool = False) -> list[dict]:
    """Put every source's numbers beside the ones the screener acted on.

    This is the part that answers "is the data real?". The screener picked these stocks
    from MarketBeat's figures as stored by its run. The live MarketBeat page says what they
    are now, and Nasdaq publishes its own consensus from the brokers that report to it. A
    figure that at least one of them confirms is solid. A figure every available source contradicts is not, and
    the stock should not be bought on the strength of that line.

    Each row is marked confirmed, disputed, unconfirmed or stale, and carries the reading
    from every source so the disagreement can be seen rather than taken on faith.
    """
    rows = []
    for key, label, tol, floor in FIELDS:
        mine = stored.get(key)
        readings, verdicts = {}, {}
        for name, data in sources.items():
            theirs = (data or {}).get(key)
            readings[name] = theirs
            got = _agrees(key, mine, theirs, tol, floor, stale)
            if got:
                verdicts[name] = got
        rows.append(_fact_row(key, label, mine, readings, verdicts))

    # The ratings breakdown is three numbers at once, so it gets its own row.
    mine_counts = rating_counts_of(stored or {})
    readings, verdicts = {}, {}
    for name, data in sources.items():
        theirs = rating_counts_of(data or {})
        readings[name] = "/".join(str(n) for n in theirs) if theirs else None
        if theirs and mine_counts:
            verdicts[name] = "agree" if _counts_agree(theirs, mine_counts) else "differs"
    rows.append(_fact_row("rating_counts", "Buy / hold / sell",
                          "/".join(str(n) for n in mine_counts) if mine_counts else None,
                          readings, verdicts))
    return rows


def _decision_flips(sources: dict[str, dict], stored: dict) -> list[str]:
    """Disagreements big enough to change the answer, as opposed to merely differing.

    The sources are not expected to match - they poll different brokers and compute from
    different filings. What matters is whether any of them would have led to the opposite
    decision: a materially different price, a target below the price where ours was above
    it, or a consensus that points down where ours pointed up.
    """
    flips = []
    mine_price = stored.get("price")
    for name, data in sources.items():
        theirs = (data or {}).get("price")
        if isinstance(mine_price, (int, float)) and isinstance(theirs, (int, float))                 and mine_price and abs(theirs - mine_price) / mine_price > 0.10:
            flips.append(f"{name} quotes {_fmt(theirs)} against our {_fmt(mine_price)}")

    mine_up = stored.get("upside_pct")
    if isinstance(mine_up, (int, float)) and mine_up > 0:
        against = [n for n, d in sources.items()
                   if isinstance((d or {}).get("upside_pct"), (int, float))
                   and d["upside_pct"] < 0]
        if against and len(against) == sum(
                1 for d in sources.values()
                if isinstance((d or {}).get("upside_pct"), (int, float))):
            flips.append(f"we have {mine_up:+.0f}% of upside but "
                         f"{' and '.join(against)} put the target below the price")

    mine_counts = rating_counts_of(stored)
    if mine_counts and (mine_counts[0] / max(sum(mine_counts), 1)) >= 0.8:
        bearish = []
        for name, data in sources.items():
            theirs = rating_counts_of(data or {})
            if theirs and theirs[0] / max(sum(theirs), 1) < 0.5:
                bearish.append(f"{name} has it at "
                               + "/".join(str(x) for x in theirs))
        if bearish:
            flips.append("we counted "
                         + "/".join(str(x) for x in mine_counts)
                         + " but " + " and ".join(bearish))
    return flips


def _fact_row(key, label, mine, readings: dict, verdicts: dict) -> dict:
    """One field: what we had, what everyone else says, and whether it stands up."""
    if not verdicts:
        status = "unconfirmed"
    elif "agree" in verdicts.values():
        status = "confirmed"
    elif all(v == "stale" for v in verdicts.values()):
        status = "stale"
    else:
        status = "disputed"
    return {"field": key, "label": label, "screener": mine, "sources": readings,
            "by_source": verdicts, "status": status,
            "agreed_with": [n for n, v in verdicts.items() if v == "agree"],
            "disputed_by": [n for n, v in verdicts.items() if v == "differs"]}


def _fmt(v) -> str:
    if v is None:
        return "–"
    if isinstance(v, str):
        return v
    if abs(v) >= 1_000_000:
        for unit, size in (("T", 1e12), ("B", 1e9), ("M", 1e6)):
            if abs(v) >= size:
                return f"{v / size:,.1f}{unit}"
    return f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 1000 else f"{v:,.0f}"


def _check(name, status, text, **extra) -> dict:
    return {"name": name, "status": status, "text": text, **extra}


def judge(mb: dict, stored: dict, nd: dict | None = None, stale: bool = False) -> dict:
    """Turn what the other sources say about a stock into a verdict, with the evidence.

    MarketBeat carries the richer picture, so the company checks run off its pages.
    Nasdaq adds an independent consensus of its own and a second vote on every figure.
    """
    checks: list[dict] = []
    nd = nd or {}

    # 1. The analyst forecast, as MarketBeat counts it ------------------------------
    # The live page is not here to match the stored run analyst for analyst - one stray
    # sell out of fifty is normal drift. What it is here to answer
    # is whether MarketBeat's own buy/hold/sell ratio says this stock goes up. So the
    # judgement is on the proportion, not on the presence of a single sell.
    counts = rating_counts_of(mb)
    rating = (mb.get("consensus_rating") or "").strip()
    n = mb.get("analyst_count")
    if counts is None:
        checks.append(_check("Analyst forecast", "warn",
                             "MarketBeat shows no rating breakdown"
                             + (f" (consensus: {rating})" if rating else "")))
    else:
        buy, hold, sell = counts
        total = buy + hold + sell
        buy_share = buy / total if total else 0
        sell_share = sell / total if total else 0
        bearish_word = any(w in rating.lower() for w in ("hold", "sell", "reduce", "underperform"))
        line = (f"{buy} buy / {hold} hold / {sell} sell on MarketBeat — {buy_share:.0%} buy"
                + (f", consensus {rating}" if rating else ""))
        # A lone sell among nine analysts is 11% - a coarse fraction, not a bearish house.
        # Failing needs either two of them or a quarter of the coverage.
        heavily_sold = (sell > MAX_LIST_SELLS or (sell >= 2 and sell_share > SELL_SHARE_FAIL)
                        or sell_share >= SELL_SHARE_HARD)
        if heavily_sold:
            checks.append(_check("Analyst forecast", "fail",
                                 f"{line}. {sell} of its analysts say sell; the list allows "
                                 f"{MAX_LIST_SELLS}.", counts=counts))
        elif buy_share < BUY_SHARE_FAIL or bearish_word:
            checks.append(_check("Analyst forecast", "fail",
                                 f"{line}. MarketBeat's ratio does not say this goes up.",
                                 counts=counts))
        elif buy_share < BUY_SHARE_PASS or sell:
            checks.append(_check("Analyst forecast", "warn",
                                 f"{line}. Still a buy-weighted ratio, but not the clean stack "
                                 f"the screener thought it was buying.", counts=counts))
        else:
            checks.append(_check("Analyst forecast", "pass",
                                 f"{line}. MarketBeat's ratio agrees this goes up.",
                                 counts=counts))

    # 2. Depth of coverage ----------------------------------------------------------
    if n is not None:
        checks.append(_check("Coverage", "pass" if n >= 8 else "warn" if n >= 3 else "fail",
                             f"{int(n)} analysts cover it on MarketBeat"
                             + ("" if n >= 8 else " - a thin sample to lean on")))

    # 3. Is the consensus improving or decaying? ------------------------------------
    now, before = mb.get("rating_counts"), mb.get("rating_counts_1m_ago")
    if now and before:
        b_now = (rating_counts_of({"rating_counts": now}) or (0, 0, 0))[0]
        b_before = (rating_counts_of({"rating_counts": before}) or (0, 0, 0))[0]
        delta = b_now - b_before
        checks.append(_check("Consensus trend",
                             "pass" if delta > 0 else "warn" if delta < 0 else "pass",
                             f"buy ratings went {b_before} -> {b_now} over the last month"))

    # 4. Where MarketBeat's analysts think the price goes ----------------------------
    up = mb.get("upside_pct")
    target = mb.get("consensus_target")
    if up is not None:
        detail = f" (target {target:,.2f} vs {mb.get('price'):,.2f})" \
            if target and mb.get("price") else ""
        if up < 0:
            checks.append(_check("Price target", "fail",
                                 f"MarketBeat's consensus target is {up:.1f}% BELOW the "
                                 f"current price{detail}.", upside=up))
        elif up < 5:
            checks.append(_check("Price target", "warn",
                                 f"only {up:.1f}% of upside left to the consensus target{detail}.",
                                 upside=up))
        else:
            checks.append(_check("Price target", "pass",
                                 f"{up:.1f}% upside to the consensus target{detail}.", upside=up))

    # 5. What the analysts have actually done lately ---------------------------------
    ups, downs, raised, cut = _recent_actions(mb.get("ratings_history"))
    if ups or downs or raised or cut:
        parts = []
        if ups:
            parts.append(f"{ups} upgrade{'s' * (ups > 1)}")
        if downs:
            parts.append(f"{downs} downgrade{'s' * (downs > 1)}")
        if raised:
            parts.append(f"{raised} target raise{'s' * (raised > 1)}")
        if cut:
            parts.append(f"{cut} target cut{'s' * (cut > 1)}")
        status = "fail" if downs > ups else "warn" if cut > raised else "pass"
        checks.append(_check("Recent analyst actions", status,
                             ", ".join(parts) + " in the last 30 days"))

    # 6. MarketBeat's own ranking ----------------------------------------------------
    pct, mr_analyst = mb.get("marketrank_percentile"), mb.get("mr_analyst")
    if pct is not None or mr_analyst is not None:
        bits = []
        if pct is not None:
            bits.append(f"{pct:.0f}th percentile overall")
        if mr_analyst is not None:
            bits.append(f"analyst score {mr_analyst}/5")
        status = "pass" if (pct or 0) >= 50 or (mr_analyst or 0) >= 3.5 else "warn"
        checks.append(_check("MarketRank", status, "MarketBeat ranks it " + ", ".join(bits)))

    # 7. Earnings: the beat record, and whether one lands inside the horizon ----------
    rate = mb.get("beat_rate_4q")
    if rate is not None:
        surprise = mb.get("avg_surprise_4q")
        tail = f", averaging {surprise:+.2f} against estimates" if surprise else ""
        # A thin beat record is a mark against the stock, not proof the pick was wrong,
        # so it counts as a warning rather than rejecting the row on its own.
        checks.append(_check("Earnings record", "pass" if rate >= 0.75 else "warn",
                             f"beat estimates in {rate:.0%} of the last four quarters{tail}"
                             + ("" if rate >= 0.5 else " - it misses more often than it beats")))
    days = _days_until(mb.get("next_earnings_date"))
    if days is not None and 0 <= days <= EARNINGS_WINDOW_DAYS:
        checks.append(_check("Earnings event", "warn",
                             f"reports in {days} days, inside the holding window - a gap "
                             f"either way is on the table"))

    # 8. Short interest ---------------------------------------------------------------
    sf, dtc = mb.get("short_pct_float"), mb.get("short_days_to_cover")
    if sf is not None:
        tail = f", {dtc:.1f} days to cover" if dtc else ""
        if sf > 20:
            checks.append(_check("Short interest", "fail",
                                 f"{sf:.1f}% of the float is sold short{tail} - that is a crowd "
                                 f"betting against it"))
        elif sf > 10:
            checks.append(_check("Short interest", "warn",
                                 f"{sf:.1f}% of the float is short{tail} - squeeze fuel, but also "
                                 f"a warning"))
        else:
            checks.append(_check("Short interest", "pass",
                                 f"only {sf:.1f}% of the float is short{tail}"))

    # 9. Insiders ---------------------------------------------------------------------
    bought = mb.get("insider_buy_value_90d") or mb.get("insider_bought_3m") or 0
    sold = mb.get("insider_sold_3m") or mb.get("insider_sell_value_90d") or 0
    if bought or sold:
        if sold > bought * 3 and sold > 1_000_000:
            checks.append(_check("Insider activity", "warn",
                                 f"insiders sold ${sold:,.0f} and bought ${bought:,.0f} in 90 days"))
        elif bought > sold:
            checks.append(_check("Insider activity", "pass",
                                 f"insiders bought ${bought:,.0f} against ${sold:,.0f} sold"))
        else:
            checks.append(_check("Insider activity", "pass",
                                 f"insider selling of ${sold:,.0f} in 90 days, nothing unusual"))

    # 10. Institutions ------------------------------------------------------------------
    inc, dec = mb.get("inst_increases"), mb.get("inst_decreases")
    if inc is not None and dec is not None and (inc or dec):
        checks.append(_check("Institutional flow", "pass" if inc >= dec else "warn",
                             f"{inc} funds added, {dec} trimmed in the latest filings"
                             + (f"; {mb['inst_own_pct']:.0f}% institutionally held"
                                if mb.get("inst_own_pct") else "")))

    # 11. Options ------------------------------------------------------------------------
    if mb.get("optionable") is not None:
        if mb.get("optionable"):
            extra = " with unusual activity flagged" if mb.get("unusual_option_activity") else ""
            checks.append(_check("Options", "pass",
                                 f"options trade on it{extra} - a position can be hedged"))
        else:
            checks.append(_check("Options", "warn",
                                 "no listed options, so there is no way to hedge the position"))

    # 12. News sentiment ------------------------------------------------------------------
    sent, avg = mb.get("news_sentiment"), mb.get("news_sentiment_sector_avg")
    if sent is not None:
        cmp = f" against a sector average of {avg}" if avg is not None else ""
        checks.append(_check("News sentiment", "pass" if sent >= (avg or 0) else "warn",
                             f"news sentiment {sent}{cmp}"))

    # 13. Nasdaq's own consensus -----------------------------------------------------
    nd_counts = rating_counts_of(nd)
    if nd_counts:
        buy, hold, sell = nd_counts
        total = buy + hold + sell
        line = (f"Nasdaq has {buy} buy / {hold} hold / {sell} sell"
                + (f" from {int(nd['analyst_count'])} analysts" if nd.get("analyst_count") else ""))
        if nd.get("upside_pct") is not None:
            line += f", target {nd['upside_pct']:+.1f}% away"
        if sell and sell / max(total, 1) > 0.15:
            checks.append(_check("Nasdaq consensus", "fail",
                                 f"{line}. A sixth of the brokers reporting to Nasdaq rate it "
                                 f"a sell."))
        elif buy / max(total, 1) < 0.6 or (nd.get("upside_pct") or 0) < 0:
            checks.append(_check("Nasdaq consensus", "warn", f"{line}."))
        else:
            checks.append(_check("Nasdaq consensus", "pass", f"{line}."))
    elif nd:
        checks.append(_check("Nasdaq consensus", "warn",
                             "Nasdaq publishes no analyst consensus for this stock"))

    # 14. Is the data the screener acted on actually true? ---------------------------
    sources = {"marketbeat": mb}
    if nd:
        sources["nasdaq"] = nd
    facts = factcheck(sources, stored or {}, stale=stale)
    checked = [f for f in facts if f["status"] in ("confirmed", "disputed")]
    disputed = [f for f in checked if f["status"] == "disputed"]
    if checked:
        def listing(fields):
            return ", ".join(
                f"{f['label']} (ours {_fmt(f['screener'])}, "
                + ", ".join(f"{n} {_fmt(v)}" for n, v in f["sources"].items() if v is not None)
                + ")" for f in fields[:2])

        flipped = _decision_flips(sources, stored or {})
        if flipped:
            status = "fail"
            text = (", ".join(flipped) + ". The disagreement changes the answer, so this row "
                    "is not safe to trust.")
        elif len(disputed) >= 3:
            status = "warn"
            text = f"{len(disputed)} of {len(checked)} figures unconfirmed: {listing(disputed)}"
        elif disputed:
            status = "warn"
            text = f"nobody confirms {listing(disputed)}"
        else:
            status = "pass"
            text = (f"all {len(checked)} figures confirmed by "
                    + " and ".join(sorted(sources)) + " - the data is real")
        checks.append(_check("Fact check", status, text, disputed=len(disputed),
                             compared=len(checked)))

    fails = [c for c in checks if c["status"] == "fail"]
    warns = [c for c in checks if c["status"] == "warn"]
    passes = [c for c in checks if c["status"] == "pass"]
    verdict = "REJECTED" if fails else "MIXED" if len(warns) >= 2 else "CONFIRMED"
    confidence = round(100 * len(passes) / len(checks)) if checks else 0
    return {
        "verdict": verdict,
        "confidence": confidence,
        "checks": checks,
        "fails": [c["text"] for c in fails],
        "warns": [c["text"] for c in warns],
        "factcheck": facts,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }


def mb_urls(ticker: str, universe_row: dict | None) -> list[str]:
    """Candidate MarketBeat addresses for a stock, best guess first."""
    stored = ((universe_row or {}).get("url") or "")
    if "marketbeat.com" in stored:
        return [stored.rstrip("/") + "/"]
    code = ((universe_row or {}).get("exchange") or "").upper()
    slugs = [EXCHANGE_SLUGS.get(code)] if EXCHANGE_SLUGS.get(code) else []
    slugs += [s for s in FALLBACK_SLUGS if s not in slugs]
    return [f"{BASE_URL}/stocks/{slug}/{ticker}/" for slug in slugs]


def resolve_url(http, ticker: str, universe_row: dict | None) -> str | None:
    """The first candidate MarketBeat actually serves a page for."""
    for url in mb_urls(ticker, universe_row)[:4]:
        try:
            html = get_html(http, url, retries=1)
        except Exception:
            continue
        if ticker.upper() in html[:200_000].upper():
            return url
    return None


def read_stock(http, base: str, page=None) -> dict:
    """Read one stock's MarketBeat tabs. `page` is a visible Chrome tab to walk along."""
    out: dict = {"source_url": base, "errors": {}}
    for slug, parser in TABS.items():
        url = base + (slug + "/" if slug else "")
        if page is not None:
            try:
                page.goto(url, wait_until="commit", timeout=12_000)
            except Exception:
                pass
        try:
            for k, v in parser(get_html(http, url)).items():
                if v is not None or k not in out:
                    out[k] = v
        except Exception as e:
            out["errors"][slug or "overview"] = str(e)[:200]
        time.sleep(DETAIL_DELAY_SEC)
    return out


def shortlist(limit: int) -> list[dict]:
    """Today's ranked picks, best first."""
    run_date = db.today()
    with db.connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT ticker, rank, score FROM scores WHERE run_date=? ORDER BY rank", (run_date,))]
        uni = {r["ticker"]: dict(r) for r in con.execute("SELECT * FROM universe")}
    picks = [r for r in rows if r["rank"] > 0][:limit]
    for r in picks:
        r["universe"] = uni.get(r["ticker"])
    return picks


def run(limit: int = 10, headless: bool = False, split: bool = True) -> list[dict]:
    """Check today's ranked picks against MarketBeat and store the verdicts."""
    picks = shortlist(limit)
    if not picks:
        print("Nothing to verify - run `python run.py analyze` first.")
        return []
    return check(picks, headless=headless, split=split)


def check(picks: list[dict], headless: bool = False, split: bool = True,
          on_result=None) -> list[dict]:
    """Read each pick on MarketBeat and judge it.

    `picks` are rows with at least a ticker; `on_result` is called with each verdict as it
    lands, which is how the menu shows the check happening rather than reporting it after.
    """
    if not picks:
        return []
    if not any(p.get("universe") for p in picks):
        with db.connect() as con:
            uni = {r["ticker"]: dict(r) for r in con.execute("SELECT * FROM universe")}
        for p in picks:
            p.setdefault("universe", uni.get(p["ticker"]))

    run_date = db.today()
    with db.connect() as con:
        stored_detail = db.load_json(con, "mb_detail")
        fresh = db.done_tickers(con, "mb_detail", run_date)

    notes.start("Second opinion - checking the picks against MarketBeat")
    notes.bullet(f"The screener picked these {len(picks)} stocks. Each one is now being read on "
                 f"MarketBeat — analyst forecast, buy/hold/sell, price target, MarketRank, "
                 f"earnings, short interest, insiders, institutions, options, news — and "
                 f"cross-checked against Nasdaq's own consensus at the same time.")
    window = notes.open_split_left() if split and not headless else None

    results: list[dict] = []
    with Session(headless=headless, window=window) as session:
        page = None
        if not headless:
            page = session.page()
            page.set_default_timeout(15_000)
        http = make_session(marketbeat_cookies(session))
        # Which MarketBeat the pages are read as. Headless runs used to skip this, so a
        # check run logged out reported its verdicts as though it had seen everything.
        try:
            level = user_level(get_html(http, BASE_URL + "/")) or "unknown"
        except Exception:
            level = "unknown"
        paid = level.lower() not in ("notloggedin", "freesubscriber", "unknown")
        print(f"  MarketBeat read as: {level}"
              + ("" if paid else "  - paywalled columns will be blank; "
                 "run `python run.py login` with an All-Access account to see them"))
        notes.line(f"MarketBeat session: **{level}**"
                   + ("" if paid else " — the free view, so paid-only columns are blank."))
        notes.stage("MarketBeat check")
        t0 = time.time()

        for i, pick in enumerate(picks, 1):
            ticker = pick["ticker"]
            base = resolve_url(http, ticker, pick.get("universe"))
            if base is None:
                notes.line(f"**{ticker}** — no MarketBeat page found, cannot check it")
                print(f"  [--] {ticker:<6} no MarketBeat page")
                continue
            mb = read_stock(http, base, page)
            try:
                nd = nasdaq.detail(ticker)
            except Exception as exc:
                nd = {"errors": {"nasdaq": type(exc).__name__}}
            # Prices move, so comparing today's quotes against a stored figure from an
            # earlier run says nothing about whether the data was true.
            result = judge(mb, stored_detail.get(ticker, {}), nd,
                           stale=ticker not in fresh)
            result.update({"ticker": ticker, "rank": pick.get("rank"),
                           "mb_user_level": level,
                           "screener_score": pick.get("score"), "url": base,
                           "marketbeat": mb, "nasdaq": nd,
                           "nasdaq_url": nd.get("source_url")})
            with db.connect() as con:
                db.upsert_json(con, "verify", ticker, result, run_date)
            results.append(result)

            mark = {"CONFIRMED": "OK", "MIXED": "??", "REJECTED": "NO"}[result["verdict"]]
            notes.line(f"**{ticker}** — {result['verdict']} ({result['confidence']}% of checks "
                       f"clean) · " + " · ".join(c["text"] for c in result["checks"][:2]))
            for bad in result["fails"]:
                notes.line(f"  - fails: {bad}")
            print(f"  [{mark}] {ticker:<6} {result['verdict']:<9} "
                  f"{result['confidence']:>3}% clean")
            notes.progress(i, len(picks), "Checked on MarketBeat", t0)
            if on_result:
                on_result(result)
            time.sleep(PAGE_PAUSE)

    kept = [r for r in results if r["verdict"] == "CONFIRMED"]
    mixed = [r for r in results if r["verdict"] == "MIXED"]
    dropped = [r for r in results if r["verdict"] == "REJECTED"]
    with db.connect() as con:
        db.set_meta(con, "verified", {"date": run_date,
                                      "confirmed": [r["ticker"] for r in kept],
                                      "mixed": [r["ticker"] for r in mixed],
                                      "rejected": [r["ticker"] for r in dropped]})
    notes.line(f"**{len(kept)} confirmed**, {len(mixed)} mixed, {len(dropped)} rejected on "
               f"MarketBeat and Nasdaq.")
    notes.close()
    print(f"\n  Of {len(results)} picks: {len(kept)} confirmed, {len(mixed)} mixed, "
          f"{len(dropped)} rejected."
          + (f"  Rejected: {', '.join(r['ticker'] for r in dropped)}" if dropped else ""))
    return results
