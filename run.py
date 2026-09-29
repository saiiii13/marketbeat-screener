"""MarketBeat short-term growth screener.

  python run.py login          open Chrome once and log in to MarketBeat (session is saved)
  python run.py pull-bulk      read the market from Yahoo -> data/screener.db
  python run.py enrich         yfinance price history for the pre-filtered candidates
  python run.py pull-detail    analyst data for every candidate
  python run.py analyze        score, rank, write out/dashboard_<date>.html and open it
  python run.py calibrate      rebuild the measured odds table from history
  python run.py discover       let Yahoo's curated screens pick the candidates
  python run.py menu           graphical menu screen: pick, then see the ranked list
  python run.py explore        interactive: screen ETFs, penny stocks, or individual stocks
  python run.py simulate       show how restrictive each filter threshold is
  python run.py finish         stages 2-4 when the sweep has already run today
  python run.py all            everything above in order (resumable; --fresh re-pulls today)
"""
import argparse
import json
import sys
import time
import webbrowser

from screener import ansi, db, notes  # ansi first: it puts the console into UTF-8
from screener.config import PRELIM_FINALISTS, UNIVERSE_MIN, WIPE_ON_RUN, RUN_HOURS


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
    n = pipeline.sweep(cap=args.universe_cap, world=args.world)
    if n < UNIVERSE_MIN:
        print(f"Universe is {n} (< {UNIVERSE_MIN}).")


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
    """Let Yahoo's curated screens surface the candidates."""
    from screener import pipeline
    return pipeline.discover(args.finalists)


def cmd_enrich(args):
    from screener import pipeline
    _, _, _, _, discovered, _ = load_state()
    if not discovered:
        discovered = cmd_discover(args)
    priced = pipeline.prices(discovered, deadline=getattr(args, "deadline", None))
    finalists = [t for t in discovered if t in priced] or discovered
    with db.connect() as con:
        db.set_meta(con, "finalists", finalists)
        db.set_meta(con, "with_prices", len(priced))
    print(f"[enrich] {len(finalists):,} candidates carried forward")


def cmd_pull_detail(args):
    from screener import pipeline
    _, _, _, _, finalists, _ = load_state()
    if not finalists:
        sys.exit("No candidates yet - run `python run.py discover` first.")
    pipeline.details(finalists, deadline=getattr(args, "deadline", None))


def cmd_calibrate(args):
    """Measure how setups like these actually performed, so the odds shown are counted."""
    from screener import calibrate
    calibrate.build(sample=args.calibrate_sample)


def cmd_analyze(args):
    from screener.score import final_scores
    from screener.report import render
    bulk, prices, detail, ups, finalists, universe_n = load_state()
    if not finalists:
        sys.exit("No finalists yet - run `python run.py enrich` first.")
    have_detail = [t for t in finalists if t in detail]
    if len(have_detail) < len(finalists):
        print(f"[analyze] {len(finalists) - len(have_detail)} finalists have no MarketBeat detail yet (run pull-detail); scoring with what exists")
    df = final_scores(finalists, bulk, prices, detail, ups)
    if df.empty:
        sys.exit("Nothing to score.")
    run_date = db.today()
    with db.connect() as con:
        con.execute("DELETE FROM scores WHERE run_date=?", (run_date,))
        for _, r in df.iterrows():
            con.execute("INSERT INTO scores(run_date,ticker,rank,score,data) VALUES (?,?,?,?,?)",
                        (run_date, r["ticker"], int(r["rank"]), float(r["score"]),
                         json.dumps({"components": r["components"], "flags": r["flags"], "excluded": r["excluded"]}, default=str)))
        stats = {
            "universe": universe_n,
            "prefiltered": db.get_meta(con, "prefiltered", 0),
            "with_prices": db.get_meta(con, "with_prices", 0),
            "finalists": len(finalists),
            "ranked": int((df["excluded"] == "").sum()),
        }
    path = render(df, stats, args.top, run_date)
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
    """Everything after the screener sweep - use when pull-bulk has already run today."""
    t0 = time.time()
    path = notes.start("MarketBeat screener — live run notes (resumed)")
    notes.bullet("Resuming after the screener sweep; stock list already collected.")
    notes.open_in_editor(path)
    cmd_enrich(args)
    cmd_pull_detail(args)
    if not args.no_calibrate:
        cmd_calibrate(args)
    cmd_analyze(args)
    notes.close(f"Total time: {(time.time() - t0) / 60:.1f} min")
    print(f"Total time: {(time.time() - t0) / 60:.1f} min")


def cmd_all(args):
    t0 = time.time()
    if WIPE_ON_RUN and not args.keep:
        db.wipe()
        print("[run] previous data wiped — this run starts from nothing")
    notes.start("Stock screener — live run notes")
    notes.bullet("Data comes from Yahoo Finance. This pane updates as each stage works.")
    if args.split:
        notes.open_split_left()
    else:
        notes.open_in_editor(str(notes.NOTES_PATH))
    cmd_pull_bulk(args)
    cmd_discover(args)
    cmd_enrich(args)
    cmd_pull_detail(args)
    if not args.no_calibrate:
        cmd_calibrate(args)
    cmd_analyze(args)
    notes.close(f"Total time: {(time.time() - t0) / 60:.1f} min")
    print(f"Total time: {(time.time() - t0) / 60:.1f} min")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["login", "pull-bulk", "enrich", "pull-detail", "analyze", "finish", "explore", "simulate", "menu", "discover", "calibrate", "all"])
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
    ap.add_argument("--no-calibrate", action="store_true",
                    help="skip rebuilding the historical odds table")
    ap.add_argument("--world", action="store_true",
                    help="sweep every region Yahoo covers, not just the US")
    ap.add_argument("--universe-cap", type=int, default=None,
                    help="stop the market sweep after this many stocks")
    ap.add_argument("--hours", type=float, default=RUN_HOURS,
                    help="stop the deep read after this many hours and score what was collected")
    ap.add_argument("--finalists", type=int, default=None,
                    help="how many stocks get the full 12-tab read (default 250)")
    ap.add_argument("--keep", action="store_true",
                    help="keep data from earlier runs instead of starting from an empty store")
    ap.add_argument("--fetch", action="store_true",
                    help="explore: also fetch prices for names in the category that were never priced")
    ap.add_argument("--split", action="store_true",
                    help="show live notes in a window on the left half of the screen")
    args = ap.parse_args()
    # A wall-clock deadline, so an overnight run always finishes with a result.
    args.deadline = (time.time() + args.hours * 3600) if args.hours else None
    {
        "login": cmd_login, "pull-bulk": cmd_pull_bulk, "enrich": cmd_enrich,
        "pull-detail": cmd_pull_detail, "analyze": cmd_analyze, "finish": cmd_finish, "explore": cmd_explore, "simulate": cmd_simulate, "menu": cmd_menu,
        "discover": cmd_discover, "calibrate": cmd_calibrate, "all": cmd_all,
    }[args.command](args)


if __name__ == "__main__":
    main()
