"""Parser regression checks against saved MarketBeat pages: python -m pytest tests/ (or python tests/test_parse.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from screener import parse  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return (FIX / name).read_text(encoding="utf-8", errors="ignore")


def test_overview():
    d = parse.parse_overview(load("aapl_overview.html"))
    assert d["marketrank_percentile"] == 83
    assert d["rating_counts"] == {"strong_buy": 1, "buy": 26, "hold": 13, "sell": 2}
    assert d["short_pct_float"] == 0.96 and d["news_sentiment"] == 1.06
    assert d["insider_sold_3m"] > 2_000_000 and d["price"] == 338.98
    assert d["market_cap"] == 4.95e12 and d["headlines"]


def test_forecast():
    d = parse.parse_forecast(load("aapl_forecast.html"))
    assert d["analyst_count"] == 42 and d["consensus_target"] == 340.14
    assert d["target_high"] == 400 and d["target_low"] == 245
    assert d["rating_counts"]["buy"] == 26 and d["rating_counts_1m_ago"]["buy"] == 25
    assert d["ratings_history"][0]["firm"] == "Weiss Ratings"


def test_earnings_short_insider_institutional():
    e = parse.parse_earnings(load("aapl_earnings.html"))
    assert e["next_earnings_date"] == "2026-10-29" and e["beat_rate_4q"] == 1.0
    s = parse.parse_short_interest(load("aapl_short-interest.html"))
    assert s["short_days_to_cover"] == 3.53
    i = parse.parse_insider(load("aapl_insider-trades.html"))
    assert i["insider_sell_count_90d"] >= 10
    n = parse.parse_institutional(load("aapl_institutional-ownership.html"))
    assert n["inst_own_pct"] == 67.73 and n["inst_recent"]


def test_upgrades_and_numbers():
    assert len(parse.parse_upgrades_page(load("upgrades.html"))) >= 10
    assert parse.parse_number("26 Buy rating(s)") == 26
    assert parse.parse_number("$4.95 trillion") == 4.95e12
    assert parse.parse_number("$59.48M") == 59_480_000


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
