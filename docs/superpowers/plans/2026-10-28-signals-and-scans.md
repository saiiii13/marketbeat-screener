# Shared Signals and Rigorous Scans Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the list and the backtest rank stocks with one shared, point-in-time rule. Add the extra MarketBeat signals the stored history can support, and adopt any of them only if it passes a fixed bar set before the scans run.

**Architecture:** A new `screener/signals.py` computes every per-stock signal for a given date. It uses only data dated on or before that date (rating snapshots, rating actions, price closes). `mb_ratio` (the live list) and `replay` (the backtest) both call it, so they cannot drift apart again. A scan script runs the backtest grid with checks and writes the result into homework.txt part 11.

**Tech Stack:** Python 3.12, SQLite, pandas, pygame-ce, pytest 9 (already installed).

**Spec:**
- The user's request (2026-10-28): "heighten ours in structure, and do more rigorous scans using other elements marketbeat offers".
- The code review of 968aab9, Important issues 2, 5 and 6.

## Global Constraints

- **MarketBeat only.** Signals come from MarketBeat data already stored in `data/screener.db`. No new sources.
- **No look-ahead.** A signal for day D reads nothing dated after D.
- **Menu reachability.** Every new option is reachable from the pygame menu (`python run.py menu`).
- **Credentials.** Never written to code, config or files.
- **Existing interfaces stay working.** `mb_ratio.net_buys`, `mb_ratio.ratio_score`, `replay.run(..., trend=...)`, saved backtests #1-#22, `planner.save_holding`.
- **Restore point.** Git tag `restore-2026-10-28` and `data/backups/restore_2026-10-28/screener.db` are the rollback. Tag `restore-2026-10-28-signals` after the last task.

## Adoption Bar (fixed before any scan runs)

A signal becomes the default only if, against plain net buys on the fixed backtest, **all five** hold:
1. It beats net buys in at least 70% of the scan windows.
2. Its mean return is at least 2 points higher.
3. It is still better with each run's two best stocks removed.
4. It is still better on official-snapshot-only counts.
5. Its average max drawdown is no more than 3 points worse.

A signal that fails stays a backtest option only, and the report says which test it failed.

## Review Focus

- **Stock with no rating actions in the last 12 months.** Every signal returns `None`. The stock is skipped by overlays that need that signal, and it is never a crash.
- **Firm with a target but no rating.** It counts toward target signals, not toward counts.
- **Day before the oldest stored action.** Signals return `None`, not 0.
- **Second purchase of a ticker already held.** Shares and total paid add up. Editing replaces.
- **Real 70% crash vs 1-for-3 reverse split.** The crash stays in the backtest. The split, a ratio near a whole factor that persists for 5 days, is left out.

---

### Task 1: Test harness and `signals.py`

**Files:**
- Create: `screener/signals.py`, `tests/conftest.py`, `tests/test_signals.py`

**Interfaces:**
- Produces:
  - `signals.at(stock: dict, prices: pd.Series | None, day: str) -> dict`, with keys:
    - `counts` (tuple or None)
    - `net` (int or None)
    - `net_change_91d` (int or None)
    - `upgrades_60d`, `downgrades_60d` (int)
    - `target_raises_30d`, `target_cuts_30d` (int)
    - `target_mean`, `target_low`, `target_high` (float or None; each firm's latest target_to within 365 days)
    - `upside` (float or None, vs the close on or before `day`)
    - `dispersion` (float or None: `(high - low) / mean`)
  - `stock` is one value of `ratings.load()`.
- Consumes: `ratings.counts_at`, `mb_ratio.net_buys`.

- [ ] **Step 1: Write failing tests** in `tests/test_signals.py`, using a hand-built `stock` fixture of 3 firms:
  - `test_net_and_counts`: counts (2,1,0) give net 0.
  - `test_upgrade_flow_window`: an upgrade 61 days before `day` is not counted, one 59 days before is.
  - `test_targets_latest_per_firm`: two targets from one firm (only the newer counts) plus one older than 365 days (ignored) give the expected mean, low and high.
  - `test_upside_uses_close_on_or_before_day`: uses the prices Series fixture.
  - `test_no_lookahead`: removing every action and snapshot dated after `day` leaves `at(...)` equal.
  - `test_empty_stock`: all values are None or 0, with no exception.
- [ ] **Step 2: Run** `python -m pytest tests -q`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement `signals.at`.** Action kinds come from the stored `action` column: "Upgrade", "Downgrade", "Boost Target", "Lower Target". `net_change_91d` = `net(day) - net(day - 91 days)`.
- [ ] **Step 4: Run** `python -m pytest tests -q`. Expected: PASS.
- [ ] **Step 5: Run the look-ahead check on real data.** Run `python data/checks/lookahead_snapshots.py`, extended to call `signals.at`. Expected: "0 changed".
- [ ] **Step 6: Commit** with the message "signals: point-in-time MarketBeat signals shared by list and backtest".

### Task 2: One ranking rule for the list and the backtest

**Files:**
- Modify: `screener/mb_ratio.py` (`rank`), `screener/replay.py` (`run`, scoring loop near line 205), `screener/gui.py` (backtest step 4 text, result header)
- Test: `tests/test_rule.py`

**Interfaces:**
- Produces:
  - `mb_ratio.RULES = {"net0": ..., "net": ..., "ratio": ...}`, mapping name to `(score_fn(counts) -> float | None, label: str)`.
  - `score_fn` returns None to exclude a stock (net0 excludes any sell).
  - `mb_ratio.sort_key(row) -> tuple`: score descending, holds ascending, ticker.
  - `replay.run(..., rule: str = "net")`, stored in `params["rule"]`.
- Consumes: Task 1 is not needed.

- [ ] **Step 1: Write failing tests.**
  - `test_net0_excludes_sells`
  - `test_sort_key_ties_on_holds`: two rows with equal score, and the one with fewer holds comes first.
  - `test_replay_and_list_same_order`: the same rows through `mb_ratio.rank` and through replay's scorer give the same order.
- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement.**
  - `rank` uses `RULES[config.LIST_RULE]`. Add `LIST_RULE = "net0"` to config.py.
  - replay uses `RULES[rule]` and `sort_key`.
  - The backtest's default stays `net`, the tested best. The menu's step 4 adds a rule row (net / net0 / ratio), and the result header shows the rule's label.
- [ ] **Step 4: Run the tests.** Expected: PASS. Also run `python run.py replay --start 2026-07-24 --end 2026-10-23 --picks 10`. Expected: completes, and the header names the rule.
- [ ] **Step 5: Commit.**

### Task 3: Signal overlays in the backtest

**Files:**
- Modify: `screener/replay.py` (replace `TRENDS` usage with `OVERLAYS`; keep the `trend=` keyword as an alias), `screener/gui.py` (the PRICE-CHART RULE row becomes EXTRA RULE)
- Test: `tests/test_overlays.py`

**Interfaces:**
- Consumes: `signals.at`, `mb_ratio.RULES`, `mb_ratio.sort_key`.
- Produces: `replay.OVERLAYS: dict[str, (label, kind, fn)]`, where kind is "filter" or "blend".
  - **Kept from TRENDS:** none, up1m, up3m, blend3m.
  - **New:**
    - `rating_up`: filter, `net_change_91d > 0`
    - `upgrade_flow`: filter, `upgrades_60d > downgrades_60d`
    - `target_raises`: filter, `target_raises_30d > target_cuts_30d`
    - `target_upside`: blend, 70% rule percentile + 30% upside percentile
    - `agreement`: filter, `dispersion <= 0.5`
  - `replay.run(..., overlay: str = "none")`, stored in `params["overlay"]`. `trend=X` maps to `overlay=X`.

- [ ] **Step 1: Write failing tests.**
  - `test_filter_overlay_drops_stock`
  - `test_blend_overlay_uses_percentiles`
  - `test_trend_alias_still_works`
  - `test_old_saved_params_display`: `params` without "overlay" or "rule" still render via `.get`.
- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement.** The replay loop calls `signals.at(history[t], frame[t], day)` once per stock per rebalance.
- [ ] **Step 4: Run the tests.** Expected: PASS. Run `python run.py replay --start 2026-07-24 --end 2026-10-23 --picks 10 --trend up3m`. Expected: same return as before the change, ±0.01.
- [ ] **Step 5: Commit.**

### Task 4: Review fixes (holdings lots, split guard, stop clamp)

**Files:**
- Modify:
  - `screener/planner.py` (`save_holding`, new `add_lot`, stop at line ~159)
  - `screener/gui.py` (the add path calls `add_lot`, the edit path calls `save_holding`)
  - `run.py` (`--hold-add` calls `add_lot`)
  - `screener/replay.py` (split guard, lines ~150-159)
- Test: `tests/test_review_fixes.py`

**Interfaces:**
- Produces:
  - `planner.add_lot(ticker: str, shares: float, total_paid: float)`: adds to an existing holding (shares sum, total sums, average price recomputed).
  - `replay.split_like(ratio_series: pd.Series) -> bool`: a one-day ratio within 5% of a whole factor in {2,3,4,5,8,10,15,20,25,50,100} (or its inverse) that holds for the next 5 closes.

- [ ] **Step 1: Write failing tests.**
  - `test_add_lot_sums`: 2 shares/$1,000 plus 1 share/$600 give 3 shares at $533.33.
  - `test_split_like_reverse_split`: 1:10.
  - `test_real_crash_kept`: −72% with no recovery to a factor.
  - `test_stop_never_negative`: sigma 0.15.
- [ ] **Step 2: Run the tests.** Expected: FAIL.
- [ ] **Step 3: Implement.** Clamp the stop to `>= 0.01`.
- [ ] **Step 4: Run the tests.** Expected: PASS. Run `python run.py replay` and check the split-exclusion note count drops below 54.
- [ ] **Step 5: Commit.**

### Task 5: Rigorous scan and verdict

**Files:**
- Create: `scripts/scan_signals.py`
- Modify: `homework.txt` (new PART 11 before the final `* * *`), memory file `marketbeat-screener-backtest-findings.md`

**Interfaces:**
- Consumes: `replay.run(rule=..., overlay=..., tickers=..., save=False)`; official-only monkeypatch as in `data/checks/net_checks.py`.

- [ ] **Step 1: Write `scripts/scan_signals.py`.**
  - Grid:
    - windows: rolling starts 2026-01-26, 02-23, 03-23, 04-20, 05-04 to 2026-10-23, plus 3 three-month blocks
    - setups: top10 and top20, 21-day hold
    - overlays: none, rating_up, upgrade_flow, target_raises, target_upside, agreement
  - Then, for each overlay: rerun with its top-2 stocks removed, and rerun on official-only counts (4 starts × 2 setups).
  - Write `data/checks/scan_signals.json` and print a table of the five bar tests per overlay.
- [ ] **Step 2: Run it in the background.** Expected: every row has a return, no errors.
- [ ] **Step 3: Apply the Adoption Bar mechanically.** A passing overlay becomes `replay.DEFAULT_OVERLAY` and is shown in the list. A failing one stays an option.
- [ ] **Step 4: Write homework part 11:** the table, the bar results and the decision. Update the memory file.
- [ ] **Step 5: Commit, tag `restore-2026-10-28-signals`, push.**

### Task 6: Show the new signals in the menu list

**Files:**
- Modify: `screener/gui.py` (RatioScene facts line), `screener/mb_ratio.py` (`show`)

- [ ] **Step 1: Add the signals to the selected stock's facts line.** For the selected stock, include "rating trend 3 mo ±N · upgrades/downgrades 60 d U/D · target raises/cuts 30 d R/C · analysts' range $low–$high", from `signals.at(stock, closes, today)`.
- [ ] **Step 2: Verify.** Launch the menu, then ENTER: the facts line shows the new parts for NVDA. Run `python run.py mb`, then `show()`: no crash.
- [ ] **Step 3: Commit, then push the branch.**
