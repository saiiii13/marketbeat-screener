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


def test_split_like_reverse_split():
    assert replay.split_like(pd.Series([10, 10.2, 101, 100, 99, 102, 100, 101.0]))


def test_split_like_forward_split():
    assert replay.split_like(pd.Series([400, 402, 99.8, 100, 101, 99, 100, 102.0]))


def test_real_crash_kept():
    assert not replay.split_like(pd.Series([100, 101, 28, 27, 25, 26, 24, 23.0]))


def test_jump_that_reverses_is_not_a_split():
    assert not replay.split_like(pd.Series([10, 10, 100, 10.5, 10, 10, 10, 10.0]))


def test_stop_never_negative():
    closes = pd.Series([100 * (1.35 if i % 2 else 0.7) ** (i % 2) for i in range(60)], dtype=float)
    sig = {"label": "Hold", "text": "", "buy_share": 0.8, "confidence": 50, "counts": (8, 2, 0)}
    out = planner.sell_guidance({"ticker": "X", "shares": 1, "purchase_price": 100}, 100.0, closes, sig, None)
    assert out["stop_loss"] > 0
