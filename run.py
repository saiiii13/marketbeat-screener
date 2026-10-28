"""MarketBeat short-term growth screener. Every figure comes from MarketBeat and is kept in
data/screener.db, so each run builds on the history the earlier ones stored.

  python run.py login          open Chrome once and log in to MarketBeat (session is saved)
  python run.py pull-bulk      collect candidates from MarketBeat's research lists
  python run.py discover       order the candidates by how many lists flagged them
  python run.py pull-detail    each candidate's MarketBeat overview, chart and forecast
  python run.py enrich         price measures from the stored history (fetches only new days)
  python run.py analyze        score, rank, write out/dashboard_<date>.html and open it
  python run.py verify         second opinion: re-check the picks live on MarketBeat and Nasdaq
  python run.py calibrate      rebuild the measured odds table from stored prices
  python run.py mb             the whole MarketBeat run, then the best buy/hold/sell ratios
  python run.py replay         backtest the ratio strategy on stored history (--start --end
                               --tickers --cash --picks --hold)
  python run.py backtests      list every saved backtest, to compare runs over time
  python run.py plan           portfolio planner (--cash, then --tickers or --picks)
  python run.py backtest       research: does Nasdaq's monthly consensus predict the next month?
  python run.py timetravel     research: replay the old screener rules month by month
  python run.py menu           graphical menu screen: everything above, by keyboard
  python run.py explore        interactive: screen ETFs, penny stocks, or individual stocks
  python run.py simulate       show how restrictive each filter threshold is
  python run.py finish         stages after pull-detail, when the reading has already run today
  python run.py all            everything above in order (resumable within a day)
"""
import argparse
import json
import sys
import time
import webbrowser

from screener import ansi, db, notes  # ansi first: it puts the console into UTF-8
from screener.config import WIPE_ON_RUN, RUN_HOURS


def load_state():
    with db.connect() as con:
        bulk = db.load_json(con, "mb_bulk")
        prices = db.load_json(con, "prices")
        detail = db.load_json(con, "mb_detail")
        ups: dict[str, list[dict]] = {}
        for r in con.execute("SELECT * FROM upgrades"):
            ups.setdefault(r["ticker"], []).append(dict(r))
        finalists = db.get_meta(con, "finalists", [])
        universe_n = con.execute("SELECT COUNT(*) FROM universe").fetchone()[0]
    return bulk, prices, detail, ups, finalists, universe_n


def cmd_login(args):
    from screener.browser import login_flow
    login_flow()


def cmd_pull_bulk(args):
    from screener import pipeline
    pipeline.sweep(cap=args.universe_cap)


def candidates(bulk, ups) -> list[str]:
    from screener.score import prefilter
    if not bulk:
        sys.exit("No MarketBeat bulk data yet - run `python run.py pull-bulk` first.")
    keep = prefilter(bulk, set(ups))
    print(f"[prefilter] {len(keep)} of {len(bulk)} stocks pass the analyst pre-filter")
    notes.line(f"**{len(keep):,} of {len(bulk):,}** stocks have a Buy-side analyst consensus "
               f"or a fresh upgrade — those go forward.")
    return keep


def cmd_discover(args):
    """Order MarketBeat's candidates by how many of its lists flagged them."""
    from screener import pipeline
    return pipeline.discover(args.finalists)


def cmd_pull_detail(args):
    from screener import pipeline
    _, _, _, _, finalists, _ = load_state()
    if not finalists:
        finalists = cmd_discover(args)
    pipeline.details(finalists, deadline=getattr(args, "deadline", None))
    _, _, detail, _, _, _ = load_state()
    with db.connect() as con:
        db.set_meta(con, "finalists", [t for t in finalists if t in detail])


def cmd_enrich(args):
    from screener import pipeline
    _, _, _, _, finalists, _ = load_state()
    if not finalists:
        sys.exit("No candidates yet - run `python run.py pull-detail` first.")
    priced = pipeline.prices_stage(finalists, deadline=getattr(args, "deadline", None))
    print(f"[enrich] {len(priced):,} candidates priced from the stored history")


def cmd_calibrate(args):
    """Measure how setups like these actually performed, so the odds shown are counted."""
    from screener import calibrate
    calibrate.build(sample=args.calibrate_sample)


def cmd_backtest(args):
    """Research: test Nasdaq's monthly consensus against a year of real outcomes."""
    from screener import backtest
    backtest.run(n=args.backtest_sample, seed=args.seed)


def cmd_timetravel(args):
    """Replay the past year with the old rules and the fixed ones."""
    from screener import timetravel
    timetravel.run(n=args.backtest_sample)


def cmd_analyze(args):
    from screener import pipeline
    _, _, detail, _, finalists, _ = load_state()
    if not finalists:
        sys.exit("No finalists yet - run `python run.py enrich` first.")
    have_detail = [t for t in finalists if t in detail]
    if len(have_detail) < len(finalists):
        print(f"[analyze] {len(finalists) - len(have_detail)} finalists have no analyst detail yet (run pull-detail); scoring with what exists")
    df, path = pipeline.analyze(top=args.top)
    if df is None or df.empty:
        sys.exit("Nothing to score.")
    run_date = db.today()
    ranked = df[df["excluded"] == ""].head(10)
    notes.stage("Stage 4 — final ranking")
    notes.table(
        ["#", "Ticker", "Company", "Score", "Upside", "1-mo", "Flags"],
        [[int(r["rank"]), r["ticker"], (r["company"] or "")[:30], r["score"],
          f"{r['upside_pct']:+.1f}%" if r["upside_pct"] is not None else "",
          f"{r['ret_1m'] * 100:+.1f}%" if r["ret_1m"] is not None else "",
          ", ".join(r["flags"])] for _, r in ranked.iterrows()],
    )
    notes.line(f"Dashboard written to `{path}`")
    print(f"\nTop {len(ranked)} for a ~1-month horizon ({run_date}):")
    for _, r in ranked.iterrows():
        flags = f"  [{', '.join(r['flags'])}]" if r["flags"] else ""
        print(f"  {int(r['rank']):>2}. {r['ticker']:<7} {r['score']:>5.1f}  {r['company'][:32]:<32} "
              f"upside {r['upside_pct'] if r['upside_pct'] is not None else '–'}%{flags}")
    print(f"\nDashboard: {path}")
    if not args.no_open:
        webbrowser.open(path)


def cmd_verify(args):
    """The second opinion: read each pick's MarketBeat page and judge it again."""
    from screener import pipeline, verify
    if verify.run(limit=args.verify_top, headless=args.headless, split=args.split):
        _, path = pipeline.analyze(top=args.top)      # redraw with the verdicts on it
        if path:
            print(f"  Dashboard updated with the verdicts: {path}")


def cmd_mb(args):
    """The whole MarketBeat run, then its candidates ranked by buy/hold/sell ratio."""
    from screener import mb_ratio
    mb_ratio.show(mb_ratio.run(), args.top)


def cmd_replay(args):
    """Backtest the ratio strategy against the history stored in the database."""
    from screener import replay
    first, last = replay.history_range()
    start = args.start or first
    end = args.end or last
    if not start:
        sys.exit("No price history stored yet - run `python run.py mb` first.")
    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None
    if args.tickers == "recs":
        tickers = replay.latest_recommendations(args.top)
    result = replay.run(start, end, tickers, args.cash, args.picks, args.hold, label=args.label or "",
                        from_recommendations=args.tickers == "recs",
                        trend=args.trend or replay.DEFAULT_TREND)
    replay.show(result)


def cmd_backtests(args):
    """Every saved backtest, newest first."""
    from screener import replay
    rows = replay.saved(args.top)
    if not rows:
        print("No backtests saved yet - run `python run.py replay`.")
        return
    print(f"\n  {'#':>4}  {'run at':<17}{'period':<25}{'stocks':>7}{'cash':>10}{'top/days':>10}"
          f"{'return':>9}{'buy&hold':>10}{'win':>6}{'max dd':>8}  label")
    for r in rows:
        m, p = r["metrics"], r["params"]
        win = f"{m['win_rate_pct']:.0f}%" if m["win_rate_pct"] is not None else "-"
        print(f"  {r['id']:>4}  {r['created'][:16].replace('T', ' '):<17}{m['start'] + ' - ' + m['end']:<25}"
              f"{m['stocks']:>7}{m['start_cash']:>10,.0f}{str(p['picks']) + '/' + str(p['hold_days']):>10}"
              f"{m['total_return_pct']:>+8.2f}%{m['buy_hold_return_pct']:>+9.2f}%{win:>6}"
              f"{m['max_drawdown_pct']:>7.1f}%  {r['label'] or ''}")


def cmd_plan(args):
    """Portfolio planner from the terminal: what to buy with the cash, and what to do with
    the stocks already held (add them in the menu, or with --hold-add)."""
    from screener import planner
    if args.hold_add:
        ticker, shares, total = args.hold_add.split(",")
        planner.save_holding(ticker.strip(), float(shares), float(total) / float(shares))
        print(f"Saved holding {ticker.strip().upper()}: {float(shares):g} shares for ${float(total):,.2f} "
              f"(${float(total) / float(shares):,.2f} a share)")
    if args.sell_limit:
        ticker, price = args.sell_limit.split(",")
        planner.set_sell_limit(ticker.strip(), float(price) if float(price) > 0 else None)
        print(f"Sell limit for {ticker.strip().upper()}: "
              f"{'$' + format(float(price), ',.2f') if float(price) > 0 else 'back to the suggested one'}")
    if (args.hold_add or args.sell_limit) and not args.cash:
        return
    if not args.cash:
        sys.exit("Give the cash to invest with --cash, e.g. --cash 5000")
    tickers = [t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None
    plan = planner.build(args.cash, tickers, args.picks_plan)
    print(f"\n  {planner.DISCLAIMER}")
    print(f"\n  ${plan['cash']:,.2f} to invest - "
          f"{'your tickers' if plan['mode'] == 'own' else 'picked by the program'}")
    print(f"  {'stock':<7}{'rating':<7}{'buy/hold/sell':<20}{'conf':>5}{'price':>11}  {'entry':<18}"
          f"{'shares':>7}{'amount':>12}")
    for r in plan["rows"]:
        s, e = r["signal"], r["entry"]
        entry = "buy now" if not e["limit"] else f"limit ${e['limit']:,.2f}"
        conf = f"{s['confidence']:.0f}" if s["confidence"] is not None else "-"
        price = f"${r['price']:,.2f}" if r["price"] else "-"
        c = s.get("counts")
        ratio = f"{c[0]}/{c[1]}/{c[2]} ({s['buy_share']:.0%} buy)" if c else "no ratings"
        print(f"  {r['ticker']:<7}{s['label']:<7}{ratio:<20}{conf:>5}{price:>11}  {entry:<18}"
              f"{r.get('shares', 0):>7}{r.get('amount', 0):>12,.2f}")
        print(f"         {e['reason']}")
    spent = sum(r.get("amount", 0) for r in plan["rows"])
    print(f"  invested ${spent:,.2f}, left as cash ${plan['left']:,.2f}")
    for line in plan["skipped"]:
        print(f"  skipped {line}")
    if plan["holdings"]:
        print(f"\n  {'held':<7}{'shares':>8}{'paid':>11}{'price':>11}{'gain/loss':>20}  {'action':<10}"
              f"{'sell limit':>12}{'stop-loss':>11}")
        for h in plan["holdings"]:
            gain = (f"${h['gain']:,.2f} ({h['gain_pct']:+.1f}%)" if h.get("gain") is not None else "-")
            price = f"${h['price']:,.2f}" if h.get("price") else "-"
            own = h.get("sell_limit") and h["action"] != "Sell now"
            limit = h["sell_limit"] if own else h.get("target_sell")
            tgt = f"{'*' if own else ''}${limit:,.2f}" if limit else "-"
            stop = f"${h['stop_loss']:,.2f}" if h.get("stop_loss") else "-"
            print(f"  {h['ticker']:<7}{h['shares']:>8g}{h['purchase_price']:>11,.2f}{price:>11}{gain:>20}  "
                  f"{h['action']:<10}{tgt:>12}{stop:>11}")
            print(f"         {h.get('reason', '')}")


def cmd_menu(args):
    from screener import gui
    gui.run()


def cmd_simulate(args):
    from screener import simulate
    simulate.run()


def cmd_explore(args):
    from screener import explore
    explore.run(limit=args.top, category=getattr(args, "category", None), fetch=args.fetch,
                price_min=args.min_price, price_max=args.max_price,
                include_structural=args.include_structural)


def cmd_finish(args):
    """Everything after the MarketBeat reading - use when pull-detail has already run today."""
    t0 = time.time()
    path = notes.start("MarketBeat screener — live run notes (resumed)")
    notes.bullet("Resuming after the MarketBeat reading; stock list already collected.")
    notes.open_in_editor(path)
    cmd_pull_detail(args)
    cmd_enrich(args)
    if not args.no_calibrate:
        cmd_calibrate(args)
    cmd_analyze(args)
    notes.close(f"Total time: {(time.time() - t0) / 60:.1f} min")
    print(f"Total time: {(time.time() - t0) / 60:.1f} min")


def cmd_all(args):
    t0 = time.time()
    if WIPE_ON_RUN and not args.keep:
        db.wipe()
        print("[run] working tables cleared — price and rating history are kept")
    notes.start("Stock screener — live run notes")
    notes.bullet("Data comes from MarketBeat. This pane updates as each stage works.")
    if args.split:
        notes.open_split_left()
    else:
        notes.open_in_editor(str(notes.NOTES_PATH))
    cmd_pull_bulk(args)
    cmd_discover(args)
    cmd_pull_detail(args)
    cmd_enrich(args)
    if not args.no_calibrate:
        cmd_calibrate(args)
    cmd_analyze(args)
    if not args.no_verify:
        cmd_verify(args)
    notes.close(f"Total time: {(time.time() - t0) / 60:.1f} min")
    print(f"Total time: {(time.time() - t0) / 60:.1f} min")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["login", "pull-bulk", "enrich", "pull-detail", "analyze", "verify", "finish", "explore", "simulate", "menu", "discover", "calibrate", "backtest", "timetravel", "mb", "replay", "backtests", "plan", "all"])
    ap.add_argument("--top", type=int, default=20, help="rows in the dashboard (default 20)")
    ap.add_argument("--fresh", action="store_true", help="ignore today's cached price data")
    ap.add_argument("--no-open", action="store_true", help="don't open the dashboard in the browser")
    ap.add_argument("--category", choices=["stocks", "etfs", "penny", "all"],
                    help="explore: skip the prompt and show this category directly")
    ap.add_argument("--min-price", type=float, default=0.0,
                    help="explore: lowest share price to include")
    ap.add_argument("--max-price", type=float, default=None,
                    help="explore: highest share price to include")
    ap.add_argument("--include-structural", action="store_true",
                    help="explore: also show leveraged, inverse, covered-call and volatility funds")
    ap.add_argument("--calibrate-sample", type=int, default=300,
                    help="how many stocks to measure when building the odds table")
    ap.add_argument("--backtest-sample", type=int, default=400,
                    help="backtest: how many random liquid stocks to test")
    ap.add_argument("--seed", type=int, default=7,
                    help="backtest: random seed for the sample, so a run can be repeated")
    ap.add_argument("--no-calibrate", action="store_true",
                    help="skip rebuilding the historical odds table")
    ap.add_argument("--universe-cap", type=int, default=None,
                    help="keep only this many candidates from MarketBeat's lists")
    ap.add_argument("--hours", type=float, default=RUN_HOURS,
                    help="stop the deep read after this many hours and score what was collected")
    ap.add_argument("--finalists", type=int, default=None,
                    help="how many stocks get the full 12-tab read (default 250)")
    ap.add_argument("--keep", action="store_true",
                    help="keep data from earlier runs instead of starting from an empty store")
    ap.add_argument("--fetch", action="store_true",
                    help="explore: also fetch prices for names in the category that were never priced")
    ap.add_argument("--verify-top", type=int, default=10,
                    help="verify: how many of the ranked picks to check on MarketBeat")
    ap.add_argument("--headless", action="store_true",
                    help="verify: read MarketBeat without showing the Chrome window")
    ap.add_argument("--no-verify", action="store_true",
                    help="skip the MarketBeat second opinion at the end of a full run")
    ap.add_argument("--split", action="store_true",
                    help="show live notes in a window on the left half of the screen")
    ap.add_argument("--start", help="replay: first day (YYYY-MM-DD); default the oldest stored")
    ap.add_argument("--end", help="replay: last day (YYYY-MM-DD); default the newest stored")
    ap.add_argument("--tickers", help="replay/plan: comma-separated tickers; replay also takes "
                                      "'recs' for the latest recommendations (--top of them)")
    ap.add_argument("--cash", type=float, default=None,
                    help="replay: starting cash (default 10000); plan: cash to invest")
    ap.add_argument("--picks", type=int, default=10, help="replay: stocks held at once")
    ap.add_argument("--hold", type=int, default=21, help="replay: trading days between re-checks")
    ap.add_argument("--label", help="replay: a name to store with the result")
    ap.add_argument("--trend", choices=["none", "up1m", "up3m", "blend3m"],
                    help="replay: price-chart rule on top of the ratio (default: none)")
    ap.add_argument("--picks-plan", type=int, default=5,
                    help="plan: how many stocks the program picks when --tickers is not given")
    ap.add_argument("--hold-add", help="plan: save a holding as TICKER,SHARES,TOTAL_PAID")
    ap.add_argument("--sell-limit", help="plan: your own sell limit as TICKER,PRICE (0 clears it)")
    args = ap.parse_args()
    if args.command == "replay" and args.cash is None:
        args.cash = 10_000.0
    # A wall-clock deadline, so an overnight run always finishes with a result.
    args.deadline = (time.time() + args.hours * 3600) if args.hours else None
    command = {
        "login": cmd_login, "pull-bulk": cmd_pull_bulk, "enrich": cmd_enrich,
        "pull-detail": cmd_pull_detail, "analyze": cmd_analyze, "verify": cmd_verify, "finish": cmd_finish, "explore": cmd_explore, "simulate": cmd_simulate, "menu": cmd_menu,
        "discover": cmd_discover, "calibrate": cmd_calibrate, "backtest": cmd_backtest, "timetravel": cmd_timetravel, "mb": cmd_mb,
        "replay": cmd_replay, "backtests": cmd_backtests, "plan": cmd_plan, "all": cmd_all,
    }[args.command]
    if args.command not in WRITERS:
        command(args)
        return
    # Writers take the store lock first, so a menu refresh and a terminal run can never
    # wipe each other's work. The menu takes the same lock for its own refresh.
    from screener.runlock import Busy, RunLock
    try:
        with RunLock(f"`python run.py {args.command}`"):
            command(args)
    except Busy as busy:
        sys.exit(f"[run] {busy}")


# Commands that write to data/screener.db. `menu` takes the lock itself, only while refreshing.
WRITERS = {"pull-bulk", "enrich", "pull-detail", "analyze", "verify", "finish", "discover",
           "calibrate", "mb", "all"}
# replay, backtests and plan only add rows to history tables that no run clears, so they do
# not need the lock and can run beside a refresh.


if __name__ == "__main__":
    main()
