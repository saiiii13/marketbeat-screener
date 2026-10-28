import pytest

from screener import replay


def sig(**kw):
    base = {"n_targets": 0, "net_change_91d": None, "upgrades_60d": 0, "downgrades_60d": 0, "target_raises_30d": 0,
            "target_cuts_30d": 0, "upside": None, "dispersion": None}
    return {**base, **kw}


SCORED = [(50, 1, "AAA", True), (40, 2, "BBB", False), (30, 0, "CCC", False)]


def test_none_overlay_keeps_everything():
    assert replay.apply_overlay("none", SCORED, {}) == SCORED


def test_filter_overlay_drops_stock():
    sigs = {"AAA": sig(net_change_91d=3), "BBB": sig(net_change_91d=-1), "CCC": sig()}
    kept = replay.apply_overlay("rating_up", SCORED, sigs)
    assert [t for _, _, t, _ in kept] == ["AAA"]


def test_flow_and_revision_filters():
    sigs = {"AAA": sig(upgrades_60d=1, downgrades_60d=2, target_raises_30d=3),
            "BBB": sig(upgrades_60d=2, target_cuts_30d=1), "CCC": sig()}
    assert [t for _, _, t, _ in replay.apply_overlay("upgrade_flow", SCORED, sigs)] == ["BBB"]
    assert [t for _, _, t, _ in replay.apply_overlay("target_raises", SCORED, sigs)] == ["AAA"]


def test_agreement_filter():
    sigs = {"AAA": sig(dispersion=0.8, n_targets=5), "BBB": sig(dispersion=0.3, n_targets=5),
            "CCC": sig(dispersion=0.0, n_targets=1)}
    assert [t for _, _, t, _ in replay.apply_overlay("agreement", SCORED, sigs)] == ["BBB"]


def test_blend_overlay_uses_percentiles():
    # BBB has the most upside, AAA the best rule score; CCC has no target and drops out.
    sigs = {"AAA": sig(upside=0.05), "BBB": sig(upside=0.60), "CCC": sig()}
    out = {t: s for s, _, t, _ in replay.apply_overlay("target_upside", SCORED, sigs)}
    assert set(out) == {"AAA", "BBB"}
    assert out["AAA"] == pytest.approx(0.7 * 1.0 + 0.3 * 0.5)
    assert out["BBB"] == pytest.approx(0.7 * 0.5 + 0.3 * 1.0)


def test_old_saved_params_describe():
    assert replay.describe({"trend": "none"}) == "net buys only"
    assert "fewer than" not in replay.describe({"trend": "up3m", "overlay": "rating_up", "rule": "net"})
    assert "analysts turning positive" in replay.describe({"overlay": "rating_up", "rule": "net"})


def test_action_overlays_need_recorded_history():
    assert replay.overlay_available("none", "2026-01-26", "2026-10-25")
    assert replay.overlay_available("rating_up", "2026-01-26", "2026-10-25")
    for o in ("upgrade_flow", "target_raises", "target_upside", "agreement"):
        assert not replay.overlay_available(o, "2026-01-26", "2026-10-25")
        assert replay.overlay_available(o, "2027-01-05", "2026-10-25")


def test_invest_all_says_trades_are_per_period():
    assert "trades counted per period" in replay.describe({"rule": "list", "invest_all": True})
