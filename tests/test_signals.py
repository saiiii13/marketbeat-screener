import pandas as pd
import pytest

from screener import signals

DAY = "2026-06-30"


def action(date, firm, kind, rating_to=None, rating_from=None, target_to=None, target_from=None):
    return {"date": date, "firm": firm, "action": kind, "rating_from": rating_from,
            "rating_to": rating_to, "target_from": target_from, "target_to": target_to}


@pytest.fixture
def stock():
    return {"snapshots": {"2026-06-29": (2, 1, 0)},
            "actions": [
                action("2026-05-01", "Alpha", "Upgrade", "Buy", "Hold", 120.0, 100.0),
                action("2026-06-20", "Alpha", "Boost Target", "Buy", None, 150.0, 120.0),
                action("2026-04-25", "Beta", "Upgrade", "Buy", "Hold", 90.0),
                action("2026-06-10", "Gamma", "Lower Target", "Hold", None, 80.0, 95.0),
                action("2025-05-01", "Delta", "Set Target", "Buy", None, 500.0),
            ]}


@pytest.fixture
def closes():
    return pd.Series([100.0, 110.0, 125.0],
                     index=pd.to_datetime(["2026-06-26", "2026-06-29", "2026-07-01"]))


def test_net_and_counts(stock, closes):
    s = signals.at(stock, closes, DAY)
    assert s["counts"] == (2, 1, 0)
    assert s["net"] == 0


def test_upgrade_flow_window(stock, closes):
    s = signals.at(stock, closes, DAY)
    # Alpha's upgrade is 60 days before, Beta's 66: only Alpha's counts.
    assert s["upgrades_60d"] == 1
    assert s["downgrades_60d"] == 0


def test_target_revisions_30d(stock, closes):
    s = signals.at(stock, closes, DAY)
    assert s["target_raises_30d"] == 1
    assert s["target_cuts_30d"] == 1


def test_targets_latest_per_firm(stock, closes):
    s = signals.at(stock, closes, DAY)
    # Alpha 150 (newer than its 120), Beta 90, Gamma 80; Delta's is over a year old.
    assert s["target_low"] == 80.0
    assert s["target_high"] == 150.0
    assert s["target_mean"] == pytest.approx(320 / 3)
    assert s["dispersion"] == pytest.approx((150 - 80) / (320 / 3))


def test_upside_uses_close_on_or_before_day(stock, closes):
    s = signals.at(stock, closes, DAY)
    assert s["upside"] == pytest.approx((320 / 3) / 110.0 - 1)


def test_no_lookahead(stock, closes):
    later = {"snapshots": {**stock["snapshots"], "2026-07-15": (9, 0, 0)},
             "actions": stock["actions"] + [action("2026-07-02", "Alpha", "Downgrade", "Sell", "Buy", 10.0, 150.0)]}
    assert signals.at(later, closes, DAY) == signals.at(stock, closes, DAY)


def test_empty_stock():
    s = signals.at({"snapshots": {}, "actions": []}, None, DAY)
    assert s["counts"] is None and s["net"] is None and s["net_change_91d"] is None
    assert s["upgrades_60d"] is None and s["target_raises_30d"] is None
    assert s["target_mean"] is None and s["upside"] is None and s["dispersion"] is None


def test_describe_line(stock, closes):
    line = signals.describe(signals.at(stock, closes, DAY))
    assert "upgrades/downgrades 60 days 1/0" in line
    assert "target raises/cuts 30 days 1/1" in line
    assert "analysts' targets $80–$150" in line


def test_describe_empty():
    assert signals.describe(signals.at({"snapshots": {}, "actions": []}, None, DAY)) == \
        "rating trend 3 mo -  ·  no analyst actions recorded yet"


def test_actions_recorded_later_are_not_used_for_flow_or_targets(stock, closes):
    # MarketBeat shows only each firm's latest action, so an action is known on D only if the
    # screener had recorded it by D (first_seen); Alpha's raise was only seen after DAY.
    seen = {"snapshots": stock["snapshots"],
            "actions": [dict(a, first_seen="2026-06-01") for a in stock["actions"]]}
    seen["actions"][1]["first_seen"] = "2026-07-10"
    s = signals.at(seen, closes, DAY)
    assert s["target_raises_30d"] == 0 and s["target_cuts_30d"] == 1
    assert s["target_high"] == 120.0       # Alpha's older target is all that was known


def test_no_recorded_actions_means_none_not_zero(stock, closes):
    later = {"snapshots": stock["snapshots"],
             "actions": [dict(a, first_seen="2026-10-25") for a in stock["actions"]]}
    s = signals.at(later, closes, DAY)
    assert s["counts"] == (2, 1, 0)        # counts still come from the snapshot
    assert s["upgrades_60d"] is None and s["target_mean"] is None and s["n_targets"] == 0


def test_n_targets(stock, closes):
    assert signals.at(stock, closes, DAY)["n_targets"] == 3
