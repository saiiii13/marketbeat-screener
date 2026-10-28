from screener import mb_ratio, replay


def row(ticker, buy, hold, sell):
    return {"ticker": ticker, "company": ticker, "buy": buy, "hold": hold, "sell": sell,
            "analysts": buy + hold + sell, "buy_share": buy / (buy + hold + sell),
            "score": mb_ratio.net_buys(buy, hold, sell)}


def test_list_rule_keeps_only_heavily_covered_clean_stocks():
    score = mb_ratio.RULES["list"][0]
    assert score(54, 1, 0) == 52
    assert score(35, 0, 0) is None      # needs more than 35 buys
    assert score(40, 5, 0) is None      # needs fewer than 5 holds
    assert score(40, 1, 1) is None      # needs no sells


def test_net0_excludes_sells():
    assert mb_ratio.RULES["net0"][0](30, 2, 1) is None
    assert mb_ratio.RULES["net0"][0](30, 2, 0) == 26
    assert mb_ratio.RULES["net"][0](30, 2, 1) == 21


def test_sort_key_ties_on_holds():
    items = [(30, 3, "AAA"), (30, 1, "ZZZ"), (31, 9, "MMM")]
    assert [t for _, _, t in sorted(items, key=lambda x: mb_ratio.sort_key(*x))] == ["MMM", "ZZZ", "AAA"]


def test_replay_and_list_same_order():
    rows = [row("V", 37, 0, 0), row("NVDA", 54, 1, 0), row("AMZN", 57, 3, 0), row("GOOGL", 52, 4, 0),
            row("WMT", 36, 3, 0)]
    listed = [r["ticker"] for r in mb_ratio.rank(rows)]
    scored = [(mb_ratio.RULES["list"][0](r["buy"], r["hold"], r["sell"]), r["hold"], r["ticker"]) for r in rows]
    assert [t for _, _, t in replay.order(scored)] == listed == ["NVDA", "AMZN", "GOOGL", "V", "WMT"]


def test_budget_invest_all_splits_value_over_chosen():
    # Three qualifying stocks, room for ten: invest_all puts a third of the value in each.
    assert replay.budget(cash=10_000, value=10_000, new=3, picks=10, chosen=3, invest_all=True) == 10_000 / 3
    assert replay.budget(cash=10_000, value=10_000, new=3, picks=10, chosen=3, invest_all=False) == 1_000
