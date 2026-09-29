"""HTML parsers for MarketBeat pages. Text-based where the site uses prose, table-based elsewhere."""
import re
from datetime import date, datetime

from bs4 import BeautifulSoup

RATING_WORDS = ["strong buy", "moderate buy", "buy", "hold", "reduce", "moderate sell", "strong sell", "sell"]

# MarketBeat quotes every listing in its own currency: "$" on US pages, "GBX" (pence) on
# London, "C$" on Toronto, "£"/"€" for market caps. Optional, so US pages are unaffected.
CUR = r"(?:GBX|C\$|A\$|\$|£|€|CHF|SEK|NOK|DKK)?"


def parse_number(text) -> float | None:
    if text is None:
        return None
    s = str(text).strip().replace(",", "").replace("$", "").replace("%", "")
    if s in ("", "-", "N/A", "n/a", "—"):
        return None
    m = re.search(r"[-+]?\d*\.?\d+", s)
    if not m:
        return None
    val = float(m.group(0))
    tail = s[m.end():].strip().lower()
    word = re.match(r"[a-z]+", tail)
    word = word.group(0) if word else ""
    mult = {"trillion": 1e12, "billion": 1e9, "million": 1e6, "thousand": 1e3, "t": 1e12, "b": 1e9, "m": 1e6, "k": 1e3}
    return val * mult.get(word, 1)


def parse_rating(text: str) -> tuple[str | None, float | None]:
    t = (text or "").lower()
    label = next((w for w in RATING_WORDS if w in t), None)
    m = re.search(r"\(([\d.]+)\)", t)
    return (label.title() if label else None, float(m.group(1)) if m else None)


def parse_date(text: str) -> str | None:
    t = (text or "").strip()
    for fmt in ("%m/%d/%Y", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(t.split(" (")[0].strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _text(soup_or_html) -> str:
    soup = soup_or_html if isinstance(soup_or_html, BeautifulSoup) else BeautifulSoup(soup_or_html, "lxml")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    lines = [l.strip() for l in soup.get_text("\n").split("\n")]
    return "\n".join(l for l in lines if l)


def _table_rows(soup: BeautifulSoup, header_prefix: str) -> tuple[list[str], list[list[str]]]:
    for table in soup.find_all("table"):
        heads = [th.get_text(" ", strip=True) for th in table.find_all("th")]
        if heads and heads[0].lower().startswith(header_prefix.lower()):
            rows = []
            for tr in table.find_all("tr"):
                cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                if cells:
                    rows.append(cells)
            return heads, rows
    return [], []


def _clean_paywall(text: str) -> str:
    return re.sub(r"\s*Subscribe to MarketBeat.*$", "", text or "", flags=re.S).strip()


def _grab(txt: str, pattern: str, group: int = 1, flags=re.I) -> str | None:
    m = re.search(pattern, txt, flags)
    return m.group(group) if m else None


def _num(txt: str, pattern: str) -> float | None:
    return parse_number(_grab(txt, pattern))


# ---------------------------------------------------------------- overview page (MarketRank etc.)

def parse_overview(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    txt = _text(soup)
    d: dict = {}

    d["marketrank_percentile"] = _num(txt, r"(\d+)(?:st|nd|rd|th) Percentile Overall Score")
    d["sector_rank"] = _grab(txt, r"ranked (\d+\w* out of \d+ stocks in the [^.]+ sector)")

    def sub(label):
        return _num(txt, re.escape(label) + r"\n([\d.]+) / 5")

    d["mr_analyst"] = sub("Analyst's Opinion")
    d["mr_earnings_valuation"] = sub("Earnings and Valuation")
    d["mr_short_interest"] = sub("Short Interest")
    d["mr_dividend"] = sub("Dividend")
    d["mr_news_social"] = sub("News and Social Media")
    d["mr_ownership"] = sub("Company Ownership")
    d["mr_sustainability"] = sub("Sustainability and ESG")

    m = re.search(r"average rating score is ([\d.]+), and is based on (.*?)\.", txt)
    if m:
        d["consensus_score"] = float(m.group(1))
        counts = {}
        for n, kind in re.findall(r"(\d+) (strong buy|buy|hold|sell|strong sell|reduce) rating", m.group(2), re.I):
            counts[kind.lower().replace(" ", "_")] = int(n)
        d["rating_counts"] = counts
    d["consensus_rating"] = _grab(txt, r"consensus rating of ([A-Za-z ]+?)\.")
    d["research_reports_90d"] = _num(txt, r"subject of (\d+) research reports? in the past 90 days")
    d["proj_earnings_growth_pct"] = _num(txt, r"Proj\. Earnings Growth\n([-\d.]+)%")
    d["pe"] = _num(txt, r"P/E ratio of [\w .&']+? is ([\d.]+)")
    d["peg"] = _num(txt, r"PEG Ratio of ([\d.]+)")
    d["pb"] = _num(txt, r"P/B Ratio of ([\d.]+)")
    d["short_pct_float"] = _num(txt, r"([\d.]+)% of the float of")
    d["short_days_to_cover"] = _num(txt, r'days to cover"\) of ([\d.]+)')
    d["short_change_pct"] = _num(txt, r"Short interest in [\w .&']+? has recently (?:increased|decreased) by ([\d.]+)%")
    if d["short_change_pct"] is not None and re.search(r"has recently decreased", txt):
        d["short_change_pct"] = -d["short_change_pct"]
    d["short_level"] = _grab(txt, r"Short Interest Level\n([A-Za-z ]+)")
    d["news_sentiment"] = _num(txt, r"news sentiment score of ([-\d.]+)")
    d["news_sentiment_sector_avg"] = _num(txt, r"than the ([-\d.]+) average news sentiment")
    d["news_articles_week"] = _num(txt, r"tracked ([\d,]+) news articles")
    d["news_articles_avg_week"] = _num(txt, r"compared to ([\d,]+) articles on an average week")
    d["search_interest_30d"] = _num(txt, r"(?:Only )?([\d,]+) people have searched for")
    d["search_interest_change_pct"] = _num(txt, r"searched for .*?This is an? (?:increase|decrease) of ([-\d.]+)%")
    d["follows_30d"] = _num(txt, r"([\d,]+) people have added [\w .&']+? to their MarketBeat watchlist")
    d["insider_bought_3m"] = _num(txt, r"they have bought \$([\d,.]+) in company stock")
    d["insider_sold_3m"] = _num(txt, r"and sold \$([\d,.]+) in company stock")
    d["insider_own_pct"] = _num(txt, r"([\d.]+)% of the stock of [\w .&']+? is held by insiders")
    d["inst_own_pct"] = _num(txt, r"([\d.]+)% of the stock of [\w .&']+? is held by institutions")
    d["dividend_yield"] = _num(txt, r"dividend yield of ([\d.]+)%")
    d["payout_ratio"] = _num(txt, r"dividend payout ratio of [\w .&']+? is ([\d.]+)%")

    # Key stats block
    # MarketBeat quotes each listing in its own currency: "$338.98" on US pages, "GBX 136.50"
    # on London, "C$14.70" on Toronto, "£135.47 million" for a UK market cap.
    d["currency"] = _grab(txt, r"\n(GBX|C\$|A\$|\$|£|€|CHF|SEK|NOK|DKK)\s?[\d,.]+\n[+-][\d.]+\s\(")
    d["price"] = _num(txt, r"\n(?:GBX|C\$|A\$|\$|£|€|CHF|SEK|NOK|DKK)?\s?([\d,.]+)\n[+-][\d.]+\s\([+-][\d.]+%\)")
    d["volume"] = _num(txt, r"\nVolume\n([\d,.]+ (?:million|billion|thousand)?)")
    d["avg_volume"] = _num(txt, r"Average Volume\n([\d,.]+ (?:million|billion|thousand)?)")
    d["market_cap"] = _num(txt, r"Market Cap(?:italization)?\n" + CUR + r"\s?([\d,.]+ (?:trillion|billion|million)?)")
    d["beta"] = _num(txt, r"\nBeta\n([-\d.]+)")
    d["optionable"] = bool(re.search(r"\nOptionable\nOptionable", txt))
    d["price_target"] = _num(txt, r"\nPrice Target\n" + CUR + r"\s?([\d,.]+)")
    for label, key in (("Today's Range", "range_day"), ("50-Day Range", "range_50d"), ("52-Week Range", "range_52w")):
        m = re.search(re.escape(label) + r"\n" + CUR + r"\s?([\d,.]+)\n.*?\n" + CUR + r"\s?([\d,.]+)", txt)
        if m:
            d[key + "_low"], d[key + "_high"] = parse_number(m.group(1)), parse_number(m.group(2))

    heads = []
    for row in soup.select("div.headline-row"):
        a = row.find("a")
        t = row.find("time")
        if a:
            heads.append({
                "title": a.get_text(" ", strip=True),
                "url": a.get("href"),
                "when": t.get("datetime") if t else None,
            })
    d["headlines"] = heads[:25]
    return d


# ---------------------------------------------------------------- forecast page

def parse_forecast(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    txt = _text(soup)
    d: dict = {}
    d["consensus_target"] = _num(txt, r"(?:Consensus|Average) Price Target\n\$([\d,.]+)")
    d["target_high"] = _num(txt, r"highest price target for [\w.]+ is\n\$([\d,.]+)")
    d["target_low"] = _num(txt, r"lowest price target for [\w.]+ is\n\$([\d,.]+)")
    m = re.search(r"forecasted\n(upside|downside) of ([\d.]+)%", txt, re.I)
    if m:
        d["upside_pct"] = float(m.group(2)) * (1 if m.group(1).lower() == "upside" else -1)
    else:
        d["upside_pct"] = _num(txt, r"([-\d.]+)% (?:Upside|Downside)")
    d["analyst_count"] = _num(txt, r"Based on (\d+) Analyst Ratings") or _num(txt, r"Based on\n(\d+)\nWall Street analysts")

    heads, rows = _table_rows(soup, "Type")
    if heads and len(heads) > 1 and "Current" in heads[1]:
        def snapshot(col):
            counts = {}
            for r in rows:
                key = r[0].strip().lower().replace(" ", "_")
                if len(r) > col and key in ("strong_buy", "buy", "hold", "sell", "strong_sell", "reduce"):
                    n = parse_number(r[col])
                    if n is not None:
                        counts[key] = int(n)
            return counts
        d["rating_counts"] = snapshot(1)
        d["rating_counts_1m_ago"] = snapshot(2)

    hist = []
    heads, rows = _table_rows(soup, "Date")
    for r in rows:
        if len(r) < 6:
            continue
        rating = r[4]
        pt = r[5]
        m_pt = re.findall(r"\$([\d,.]+)", pt)
        hist.append({
            "date": parse_date(r[0]),
            "firm": _clean_paywall(re.sub(r"\s+\d(?:\.\d)? of 5 stars.*", "", r[1])),
            "analyst": _clean_paywall(r[2]),
            "action": r[3].strip(),
            "rating_from": rating.split("➝")[0].strip() if "➝" in rating else None,
            "rating_to": rating.split("➝")[-1].strip() if rating else None,
            "target_from": parse_number(m_pt[0]) if len(m_pt) > 1 else None,
            "target_to": parse_number(m_pt[-1]) if m_pt else None,
        })
    d["ratings_history"] = hist[:60]
    return d


# ---------------------------------------------------------------- earnings page

def parse_earnings(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    txt = _text(soup)
    d: dict = {}
    d["next_earnings_date"] = parse_date(_grab(txt, r"(?:next|upcoming) earnings (?:date|report)[^\n]*?\n?([A-Z][a-z]+ \d{1,2}, \d{4}|\d{1,2}/\d{1,2}/\d{4})") or "")
    heads, rows = _table_rows(soup, "Date")
    hist = []
    for r in rows:
        if len(r) < 5:
            continue
        est, rep = parse_number(r[2]), parse_number(r[3])
        beat = parse_number(r[4])
        hist.append({
            "date": parse_date(r[0]),
            "estimated": "Estimated" in r[0],
            "quarter": r[1],
            "eps_est": est,
            "eps_actual": rep,
            "beat_miss": beat,
            "revenue_est": parse_number(r[6]) if len(r) > 6 else None,
            "revenue_actual": parse_number(r[7]) if len(r) > 7 else None,
        })
    d["earnings_history"] = hist[:12]
    if d["next_earnings_date"] is None:
        nxt = [h["date"] for h in hist if h["estimated"] and h["date"]]
        d["next_earnings_date"] = min(nxt) if nxt else None
    reported = [h for h in hist if not h["estimated"] and h["eps_est"] is not None and h["eps_actual"] is not None][:4]
    d["beat_rate_4q"] = (sum(1 for h in reported if h["eps_actual"] >= h["eps_est"]) / len(reported)) if reported else None
    d["avg_surprise_4q"] = (sum(h["beat_miss"] or 0 for h in reported) / len(reported)) if reported else None
    return d


# ---------------------------------------------------------------- short interest page

def parse_short_interest(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    heads, rows = _table_rows(soup, "Report Date")
    hist = []
    for r in rows[:6]:
        if len(r) < 6:
            continue
        hist.append({
            "date": parse_date(r[0]),
            "shares_short": parse_number(r[1]),
            "dollar_short": parse_number(r[2]),
            "change_pct": parse_number(r[3]),
            "pct_float": parse_number(r[4]),
            "days_to_cover": parse_number(r[5]),
        })
    d = {"short_history": hist}
    if hist:
        d["short_pct_float"] = hist[0]["pct_float"]
        d["short_days_to_cover"] = hist[0]["days_to_cover"]
        d["short_change_pct"] = hist[0]["change_pct"]
    return d


# ---------------------------------------------------------------- insider trades page

def parse_insider(html: str, lookback_days: int = 90) -> dict:
    soup = BeautifulSoup(html, "lxml")
    heads, rows = _table_rows(soup, "Transaction Date")
    trades = []
    cutoff = date.today().toordinal() - lookback_days
    for r in rows:
        if len(r) < 6:
            continue
        dt = parse_date(r[0])
        trades.append({
            "date": dt,
            "insider": r[1],
            "side": r[2].strip().lower(),
            "shares": parse_number(r[3]),
            "price": parse_number(r[4]),
            "value": parse_number(r[5]),
        })
    recent = [t for t in trades if t["date"] and date.fromisoformat(t["date"]).toordinal() >= cutoff]
    buys = [t for t in recent if t["side"].startswith("buy")]
    sells = [t for t in recent if t["side"].startswith("sell")]
    return {
        "insider_trades": trades[:30],
        "insider_buy_count_90d": len(buys),
        "insider_sell_count_90d": len(sells),
        "insider_buy_value_90d": sum(t["value"] or 0 for t in buys),
        "insider_sell_value_90d": sum(t["value"] or 0 for t in sells),
    }


# ---------------------------------------------------------------- institutional ownership page

def parse_institutional(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    txt = _text(soup)
    d: dict = {}
    d["inst_own_pct"] = _num(txt, r"([\d.]+)% of [\w .&']+? (?:stock|shares) (?:is|are) (?:owned|held) by institutional")
    heads, rows = _table_rows(soup, "Reporting Date")
    recs = []
    for r in rows[:40]:
        if len(r) < 6:
            continue
        recs.append({
            "date": parse_date(r[0]),
            "holder": r[1],
            "shares": parse_number(r[2]),
            "value": parse_number(r[3]),
            "change_pct": parse_number(r[5]),
        })
    d["inst_recent"] = recs[:15]
    ups = [x for x in recs if x["change_pct"] is not None and x["change_pct"] > 0]
    downs = [x for x in recs if x["change_pct"] is not None and x["change_pct"] < 0]
    d["inst_increases"] = len(ups)
    d["inst_decreases"] = len(downs)
    d["inst_net_flow_value"] = sum(
        (x["value"] or 0) * x["change_pct"] / (100 + x["change_pct"]) for x in ups + downs if x["change_pct"] > -100
    )
    return d


# ---------------------------------------------------------------- analyst upgrades list

def parse_upgrades_page(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    out = []
    for table in soup.find_all("table"):
        heads = [th.get_text(" ", strip=True) for th in table.find_all("th")]
        if not heads or heads[0] != "Company":
            continue
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 7:
                continue
            clean = str(tds[0].get("data-clean") or "")
            ticker = clean.split("|")[0].strip().upper()
            if not ticker:
                continue
            out.append({
                "ticker": ticker,
                "date": date.today().isoformat(),
                "firm": tds[2].get_text(" ", strip=True).split("Subscribe")[0].strip(),
                "action": tds[1].get_text(" ", strip=True),
                "detail": f"{tds[6].get_text(' ', strip=True)} | {tds[5].get_text(' ', strip=True)}",
            })
    return out


# ---------------------------------------------------------------- financials tab

def parse_financials(html: str) -> dict:
    """Income statement, cash flow and balance sheet, as MarketBeat lays them out:
    metrics down the rows, periods across the columns."""
    soup = BeautifulSoup(html, "lxml")
    d: dict = {}
    # MarketBeat's own row labels, which are more verbose than the usual shorthand.
    wanted = {
        "total revenue": "revenue",
        "total gross profit": "gross_profit",
        "total operating income": "operating_income",
        "consolidated net income / (loss)": "net_income",
        "net income / (loss) attributable to common shareholders": "net_income_common",
        "net cash from operating activities": "operating_cash_flow",
        "purchase of property, plant & equipment": "capex",
        "total assets": "total_assets",
        "total liabilities": "total_liabilities",
        "total equity": "total_equity",
        "total common equity": "total_equity",
        "cash and cash equivalents": "cash",
        "long term debt": "long_term_debt",
        "total debt": "total_debt",
        "diluted earnings per share": "eps_diluted",
    }
    series: dict[str, list] = {}
    periods: list[str] = []
    for table in soup.find_all("table"):
        heads = [th.get_text(" ", strip=True) for th in table.find_all("th")]
        if not heads or heads[0].strip().lower() != "metric":
            continue
        periods = [h for h in heads[1:] if h.strip()]
        for tr in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) < 3:
                continue
            key = wanted.get(cells[0].strip().lower())
            if not key or key in series:
                continue
            values = [parse_number(c) for c in cells[1:] if c.strip()]
            values = [v for v in values if v is not None]
            if values:
                series[key] = values
                d[key] = values[-1]
    d["financial_periods"] = periods[-6:]
    d["financial_series"] = {k: v[-6:] for k, v in series.items()}

    # growth on the two most recent comparable periods
    for key in ("revenue", "net_income", "eps_diluted"):
        values = series.get(key) or []
        if len(values) >= 2 and values[-2]:
            d[key + "_growth_pct"] = (values[-1] - values[-2]) / abs(values[-2]) * 100
    if d.get("revenue") and d.get("net_income"):
        d["net_margin_pct"] = d["net_income"] / d["revenue"] * 100
    if d.get("total_equity") and (d.get("total_debt") or d.get("long_term_debt")):
        debt = d.get("total_debt") or d["long_term_debt"]
        d["debt_to_equity"] = debt / d["total_equity"]
    if d.get("operating_cash_flow") is not None and d.get("capex") is not None:
        d["free_cash_flow"] = d["operating_cash_flow"] - abs(d["capex"])
    return d


# ---------------------------------------------------------------- dividend tab

def parse_dividend(html: str) -> dict:
    txt = _text(BeautifulSoup(html, "lxml"))
    return {
        "dividend_yield": _num(txt, r"Dividend Yield\n([\d.]+)%"),
        "annual_dividend": _num(txt, r"Annual Dividend\n" + CUR + r"\s?([\d.]+)"),
        "dividend_growth_years": _num(txt, r"Track Record\n(\d+) Years"),
        "dividend_growth_5y_pct": _num(txt, r"Dividend Growth\n([\d.]+)%"),
        "dividend_payout_ratio": _num(txt, r"Dividend Payout Ratio\n([\d.]+)%"),
        "next_dividend_date": _grab(txt, r"Dividend Payment\n([A-Z][a-z]+\.? \d{1,2})"),
    }


# ---------------------------------------------------------------- competitors tab

def parse_competitors(html: str) -> dict:
    """MarketBeat compares the stock against its peers on ratings, sentiment and
    valuation. The comparison is the useful part: it says whether this name is the
    best of its group or merely in a strong group."""
    soup = BeautifulSoup(html, "lxml")
    d: dict = {"peers": [], "peer_tables": {}}
    for table in soup.find_all("table"):
        heads = [th.get_text(" ", strip=True) for th in table.find_all("th")]
        if not heads or heads[0].strip().lower() != "company":
            continue
        label = "ratings" if any("Buy Ratings" in h for h in heads) else \
                "sentiment" if any("Positive" in h for h in heads) else \
                "valuation" if any("Revenue" in h or "Earnings" in h for h in heads) else None
        if not label:
            continue
        names = [h for h in heads[1:] if h and not any(
            w in h for w in ("Rating", "Positive", "Negative", "Neutral", "Sentiment",
                             "Revenue", "Ratio", "Income", "Share"))]
        rows = []
        for tr in table.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if cells:
                rows.append(cells)
        d["peer_tables"][label] = {"columns": heads, "rows": rows[:8]}
        for n in names:
            if n not in d["peers"]:
                d["peers"].append(n)
    d["peers"] = d["peers"][:8]
    return d


# ---------------------------------------------------------------- SEC filings tab

def parse_sec_filings(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    _, rows = _table_rows(soup, "Date")
    filings = []
    for r in rows[:25]:
        if len(r) < 3:
            continue
        filings.append({"date": parse_date(r[0].split(" ")[0]),
                        "filer": r[1][:60], "form": r[2][:60]})
    txt = _text(soup)
    return {
        "sec_filings": filings,
        "sec_filing_count": _num(txt, r"submitted ([\d,]+)\+? documents"),
        "sec_recent_forms": sorted({" ".join(f["form"].split(" ")[:2]) for f in filings})[:8],
    }


# ---------------------------------------------------------------- options tab

def parse_options(html: str) -> dict:
    txt = _text(BeautifulSoup(html, "lxml"))
    return {
        "optionable": "Put and Call Options" in txt,
        "option_expiries": re.findall(r"\d{1,2}/\d{1,2}/\d{4}", txt)[:6],
        "unusual_option_activity": "Unusual" in txt and "Option" in txt,
    }


# ---------------------------------------------------------------- news tab

def parse_news(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    txt = _text(soup)
    items = []
    for row in soup.select("div.headline-row"):
        a = row.find("a")
        t = row.find("time")
        if a:
            items.append({"title": a.get_text(" ", strip=True)[:140],
                          "url": a.get("href"), "when": t.get("datetime") if t else None})
    return {
        "news_items": items[:20],
        "news_sentiment": _num(txt, r"news sentiment score of ([-\d.]+)"),
        "news_articles_week": _num(txt, r"tracked ([\d,]+) news articles"),
    }
