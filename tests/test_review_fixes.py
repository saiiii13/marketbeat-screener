import pandas as pd
import pytest

from screener import db, planner, replay


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    return tmp_path / "test.db"


def test_add_lot_sums(temp_db):
    planner.add_lot("MSFT", 2, 1000)
    planner.add_lot("msft", 1, 600)
    (h,) = planner.holdings()
    assert h["ticker"] == "MSFT" and h["shares"] == 3
    assert h["shares"] * h["purchase_price"] == pytest.approx(1600)


def test_add_lot_keeps_sell_limit(temp_db):
    planner.add_lot("MU", 1, 900)
    planner.set_sell_limit("MU", 1097)
    planner.add_lot("MU", 0.306, 300)
    (h,) = planner.holdings()
    assert h["sell_limit"] == 1097 and h["shares"] == pytest.approx(1.306)


def test_save_holding_still_replaces(temp_db):
    planner.add_lot("V", 2, 800)
    planner.save_holding("V", 1, 400)
    (h,) = planner.holdings()
    assert h["shares"] == 1 and h["purchase_price"] == 400


def adjusted(values):
    out, splits, ticks = replay.adjust_splits(pd.Series(values, dtype=float))
    return list(out.round(4)), splits, ticks


def test_reverse_split_adjusts_earlier_closes():
    out, splits, _ = adjusted([10, 10.2, 101, 100, 99, 102, 100, 101])
    assert splits == 1 and out[:2] == [100, 102] and out[2:] == [101, 100, 99, 102, 100, 101]


def test_reverse_split_closing_at_2_9x_is_caught():
    out, splits, _ = adjusted([10, 10, 29, 29.5, 30, 29, 29.8, 30])
    assert splits == 1 and out[0] == 30


def test_forward_split_adjusts():
    out, splits, _ = adjusted([400, 402, 99.8, 100, 101, 99, 100, 102])
    assert splits == 1 and out[0] == 100 and out[1] == 100.5


def test_split_with_drift_afterwards_is_caught():
    # ELOX-style: 1-for-1000 reverse split, then the price keeps sliding 40-60%.
    out, splits, _ = adjusted([0.01, 0.0101, 10.1, 8, 6, 5.5, 5, 4.6])
    assert splits == 1 and out[0] == 10


def test_large_odd_factor_is_caught():
    out, splits, _ = adjusted([1.05, 1.04, 39.49, 39.6, 39.2, 39.8, 39.5, 39.4])   # INHD, ~40x
    assert splits == 1


def test_real_crash_kept():
    out, splits, ticks = adjusted([100, 101, 28, 27, 25, 26, 24, 23])
    assert splits == 0 and ticks == 0 and out[2] == 28


def test_one_day_bad_tick_is_repaired():
    out, splits, ticks = adjusted([11.0, 11.1, 0.57, 13.83, 13.9, 14, 13.8, 13.7])   # ALIT
    assert ticks == 1 and splits == 0 and out[2] == 11.1


def test_stop_never_negative():
    closes = pd.Series([100 * (1.35 if i % 2 else 0.7) ** (i % 2) for i in range(60)], dtype=float)
    sig = {"label": "Hold", "text": "", "buy_share": 0.8, "confidence": 50, "counts": (8, 2, 0)}
    out = planner.sell_guidance({"ticker": "X", "shares": 1, "purchase_price": 100}, 100.0, closes, sig, None)
    assert out["stop_loss"] > 0
