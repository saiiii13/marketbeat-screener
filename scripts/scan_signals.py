"""Scan every MarketBeat signal overlay against plain net buys on the fixed backtest, then
apply the adoption bar from docs/superpowers/plans/2026-10-28-signals-and-scans.md.

Nothing is saved to the backtests table. Results: data/checks/scan_signals.json.

    python scripts/scan_signals.py            # about an hour on 4 processes
"""
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "checks" / "scan_signals.json"
END = "2026-10-23"
WINDOWS = [(s, END) for s in ("2026-01-26", "2026-02-23", "2026-03-23", "2026-04-20", "2026-05-04")]
WINDOWS += [("2026-01-26", "2026-04-24"), ("2026-04-24", "2026-07-24"), ("2026-07-24", "2026-10-23")]
SETUPS = (10, 20)
# The action-based overlays (upgrade_flow, target_raises, target_upside, agreement) cannot be
# backtested before the screener has recorded actions as they happen (replay.overlay_available).
OVERLAYS = ("none", "rating_up")
CHECK_WINDOWS = [("2026-01-26", END), ("2026-03-23", END)]
OFFICIAL_STARTS = ("2026-01-26", "2026-02-23", "2026-03-23", "2026-04-20")


def _official_mode():
    """Rating counts only from MarketBeat's own dated snapshots, no rebuilding."""
    from screener import ratings
    original = ratings.load

    def load(con, tickers=None):
        data = original(con, tickers)
        marks = ",".join("?" * len(tickers))
        official = {}
        for r in con.execute(f"SELECT ticker, asof, buy, hold, sell FROM rating_snapshots "
                             f"WHERE source='official' AND ticker IN ({marks})", list(tickers)):
            official.setdefault(r["ticker"], {})[r["asof"]] = (r["buy"], r["hold"], r["sell"])
        for t, stock in data.items():
            stock["snapshots"] = official.get(t, {})
        return data

    def counts_at(stock, asof):
        snaps = stock.get("snapshots") or {}
        before = [d for d in snaps if date.fromisoformat(d) <= date.fromisoformat(asof)]
        return tuple(snaps[max(before)]) if before else None

    ratings.load, ratings.counts_at = load, counts_at


def job(spec: dict) -> dict:
    from screener import replay
    if spec.get("official"):
        _official_mode()
    kw = dict(picks=spec["picks"], hold_days=21, save=False, say=lambda _: None,
              overlay=spec.get("overlay", "none"), rule=spec.get("rule", "net"),
              invest_all=spec.get("invest_all", False))
    r = replay.run(spec["start"], spec["end"], **kw)
    m = r.get("metrics") or {}
    by = {}
    for t in r.get("trades") or []:
        by[t["ticker"]] = by.get(t["ticker"], 0) + t["pnl"]
    top2 = [t for t, _ in sorted(by.items(), key=lambda kv: -kv[1])[:2]]
    out = {**spec, "ret": m.get("total_return_pct"), "spy": m.get("spy_return_pct"),
           "dd": m.get("max_drawdown_pct"), "win": m.get("win_rate_pct"),
           "idle": m.get("avg_idle_cash_pct"), "top2": top2, "error": r.get("error")}
    if spec.get("drop_top2") and top2:
        rest = [t for t in replay.stored_tickers() if t not in top2]
        r2 = replay.run(spec["start"], spec["end"], tickers=rest, **kw)
        out["ret_without_top2"] = (r2.get("metrics") or {}).get("total_return_pct")
    return out


def specs() -> list[dict]:
    out = [{"part": "grid", "start": s, "end": e, "picks": p, "overlay": o}
           for s, e in WINDOWS for p in SETUPS for o in OVERLAYS]
    out += [{"part": "grid", "start": s, "end": e, "picks": p, "overlay": "none", "rule": "list",
             "invest_all": inv} for s, e in WINDOWS for p in SETUPS for inv in (False, True)]
    out += [{"part": "concentration", "start": s, "end": e, "picks": p, "overlay": o, "drop_top2": True}
            for s, e in CHECK_WINDOWS for p in SETUPS for o in OVERLAYS]
    out += [{"part": "official", "start": s, "end": END, "picks": p, "overlay": o, "official": True}
            for s in OFFICIAL_STARTS for p in SETUPS for o in OVERLAYS]
    return out


def verdict(rows: list[dict]) -> dict:
    """The five-part adoption bar for each overlay against 'none'."""
    def runs(part, overlay, rule="net", invest_all=False):
        return {(r["start"], r["end"], r["picks"]): r for r in rows
                if r["part"] == part and r["overlay"] == overlay and r.get("rule", "net") == rule
                and r.get("invest_all", False) == invest_all and r["ret"] is not None}

    base, out = runs("grid", "none"), {}
    for o in OVERLAYS[1:]:
        grid = runs("grid", o)
        keys = sorted(set(grid) & set(base))
        mean = lambda rs, f: sum(r[f] for r in rs) / len(rs) if rs else 0  # noqa: E731
        wins = sum(grid[k]["ret"] > base[k]["ret"] for k in keys)
        conc_o, conc_b = runs("concentration", o), runs("concentration", "none")
        ck = sorted(set(conc_o) & set(conc_b))
        off_o, off_b = runs("official", o), runs("official", "none")
        ok = sorted(set(off_o) & set(off_b))
        tests = {
            "wins_70pct": wins >= 0.7 * len(keys),
            "mean_plus_2": mean([grid[k] for k in keys], "ret") - mean([base[k] for k in keys], "ret") >= 2,
            "better_without_top2": mean([conc_o[k] for k in ck], "ret_without_top2")
                                   > mean([conc_b[k] for k in ck], "ret_without_top2"),
            "better_official_only": mean([off_o[k] for k in ok], "ret") > mean([off_b[k] for k in ok], "ret"),
            "drawdown_within_3": mean([grid[k] for k in keys], "dd") >= mean([base[k] for k in keys], "dd") - 3,
        }
        out[o] = {"windows": len(keys), "wins": wins,
                  "mean": round(mean([grid[k] for k in keys], "ret"), 2),
                  "base_mean": round(mean([base[k] for k in keys], "ret"), 2),
                  "dd": round(mean([grid[k] for k in keys], "dd"), 2),
                  "base_dd": round(mean([base[k] for k in keys], "dd"), 2),
                  "without_top2": round(mean([conc_o[k] for k in ck], "ret_without_top2"), 2),
                  "base_without_top2": round(mean([conc_b[k] for k in ck], "ret_without_top2"), 2),
                  "official": round(mean([off_o[k] for k in ok], "ret"), 2),
                  "base_official": round(mean([off_b[k] for k in ok], "ret"), 2),
                  "tests": tests, "adopt": all(tests.values())}
    return out


if __name__ == "__main__":
    todo = specs()
    rows = []
    with ProcessPoolExecutor(max_workers=4) as pool:
        for n, row in enumerate(pool.map(job, todo), 1):
            rows.append(row)
            print(f"{n}/{len(todo)} {row['part']:<13} {row['start']}..{row['end']} top{row['picks']} "
                  f"{row.get('rule', 'net')}/{row['overlay']:<13} ret {row['ret']} spy {row['spy']}", flush=True)
            OUT.write_text(json.dumps({"rows": rows}, indent=1))
    result = {"rows": rows, "verdict": verdict(rows)}
    OUT.write_text(json.dumps(result, indent=1))
    for o, v in result["verdict"].items():
        print(o, "ADOPT" if v["adopt"] else "keep as option", v)
