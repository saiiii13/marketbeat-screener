"""Self-contained HTML dashboard (inline CSS/SVG, no external requests)."""
import json
from datetime import datetime

import pandas as pd
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .config import OUT_DIR, TEMPLATE_DIR, WEIGHTS

COMPONENT_LABELS = {
    "recent_upgrade": "fresh analyst upgrade",
    "buy_ratio": "buy ratio",
    "momentum": "price momentum",
    "target_upside": "target upside",
    "mb_features": "MarketBeat fundamentals",
    "risk": "low risk",
}


def sparkline(closes: list[float], w: int = 120, h: int = 32) -> dict:
    if not closes or len(closes) < 2:
        return {"path": "", "points": [], "up": True}
    lo, hi = min(closes), max(closes)
    span = (hi - lo) or 1.0
    pts = []
    for i, c in enumerate(closes):
        x = 2 + i * (w - 4) / (len(closes) - 1)
        y = 2 + (h - 4) * (1 - (c - lo) / span)
        pts.append((round(x, 1), round(y, 1)))
    path = "M" + " L".join(f"{x},{y}" for x, y in pts)
    return {"path": path, "points": pts, "up": closes[-1] >= closes[0], "w": w, "h": h}


def as_int(x):
    """pandas hands back NaN where a value is absent; int(NaN) raises."""
    return None if x is None or pd.isna(x) else int(x)


def pct(x, digits=1):
    return None if x is None or pd.isna(x) else f"{x * 100:+.{digits}f}%"


def fmt_money(x):
    if x is None or pd.isna(x):
        return "–"
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            return f"${x / div:.1f}{unit}"
    return f"${x:,.0f}"


def build_rows(df: pd.DataFrame, top_n: int, verdicts: dict | None = None) -> list[dict]:
    verdicts = verdicts or {}
    rows = []
    ranked = df[df["excluded"] == ""].head(top_n)
    for _, r in ranked.iterrows():
        d = r["detail"] or {}
        rc = r["rating_counts"] or {}
        total = sum(rc.values()) or 0
        comp = r["components"]
        v = verdicts.get(r["ticker"]) or {}
        rows.append({
            "verdict": v.get("verdict"),
            "verdict_note": (v.get("fails") or v.get("warns") or ["clean on every check"])[0]
                            if v else None,
            "rank": as_int(r["rank"]) or 0,
            "ticker": r["ticker"],
            "company": r["company"],
            "exchange": r["exchange"],
            "sector": r["sector"],
            "price": None if r["price"] is None else f"${r['price']:,.2f}",
            "price_raw": r["price"],
            "score": r["score"],
            "flags": r["flags"],
            "why": ", ".join(COMPONENT_LABELS.get(k, k) for k in r["why"]),
            "upgrade_note": r["upgrade_note"],
            "buy_reasons": r["buy_reasons"],
            "risk_notes": r["risk_notes"],
            "rating": r["consensus_rating"] or "–",
            "analysts": as_int(r["analyst_count"]),
            "buy": rc.get("strong_buy", 0) + rc.get("buy", 0),
            "hold": rc.get("hold", 0),
            "sell": rc.get("sell", 0) + rc.get("strong_sell", 0) + rc.get("reduce", 0),
            "buy_w": (rc.get("strong_buy", 0) + rc.get("buy", 0)) / total * 100 if total else 0,
            "hold_w": rc.get("hold", 0) / total * 100 if total else 0,
            "sell_w": (rc.get("sell", 0) + rc.get("strong_sell", 0) + rc.get("reduce", 0)) / total * 100 if total else 0,
            "target": None if r["consensus_target"] is None else f"${r['consensus_target']:,.2f}",
            "upside": None if r["upside_pct"] is None else f"{r['upside_pct']:+.1f}%",
            "upside_pos": (r["upside_pct"] or 0) >= 0,
            "ret_1m": pct(r["ret_1m"]),
            "ret_1m_pos": (r["ret_1m"] or 0) >= 0,
            "ret_5d": pct(r["ret_5d"]),
            "rsi": None if r["rsi14"] is None else f"{r['rsi14']:.0f}",
            "vol": None if r["vol_20d_annualized"] is None else f"{r['vol_20d_annualized'] * 100:.0f}%",
            "dollar_vol": fmt_money(r["avg_dollar_vol_20d"]),
            "from_high": pct(r["pct_from_52w_high"]),
            "spark": sparkline(r["closes_30d"], w=100, h=28),
            "closes": r["closes_30d"],
            "components": {k: (None if v is None else round(v * 100)) for k, v in comp.items()},
            "weights": {k: round(v * 100) for k, v in WEIGHTS.items()},
            "mb_parts": {k: (None if v is None else round(v * 100)) for k, v in (r["mb_parts"] or {}).items()},
            "detail": {
                "marketrank": d.get("marketrank_percentile"),
                "sector_rank": d.get("sector_rank"),
                "mr": {k: d.get(f"mr_{k}") for k in ("analyst", "earnings_valuation", "short_interest", "dividend", "news_social", "ownership")},
                "target_low": d.get("target_low"), "target_high": d.get("target_high"),
                "reports_90d": d.get("research_reports_90d"),
                "history": (d.get("ratings_history") or [])[:10],
                "forecast": {
                    "target": d.get("consensus_target"),
                    "low": d.get("target_low"), "high": d.get("target_high"),
                    "upside": d.get("upside_pct"),
                    "counts_now": d.get("rating_counts") or {},
                    "counts_1m": d.get("rating_counts_1m_ago") or {},
                    "score": d.get("consensus_score"),
                },
                "next_earnings": d.get("next_earnings_date"),
                "beat_rate": None if d.get("beat_rate_4q") is None else round(float(d["beat_rate_4q"]) * 100),
                "avg_surprise": d.get("avg_surprise_4q"),
                "proj_growth": d.get("proj_earnings_growth_pct"),
                "earnings": [e for e in (d.get("earnings_history") or []) if not e.get("estimated")][:4],
                "pe": d.get("pe"), "peg": d.get("peg"), "pb": d.get("pb"),
                "short_pct": d.get("short_pct_float"), "short_dtc": d.get("short_days_to_cover"),
                "short_change": d.get("short_change_pct"), "short_level": d.get("short_level"),
                "insider_buy": fmt_money(d.get("insider_buy_value_90d") if d.get("insider_buy_value_90d") is not None else d.get("insider_bought_3m")),
                "insider_sell": fmt_money(d.get("insider_sell_value_90d") if d.get("insider_sell_value_90d") is not None else d.get("insider_sold_3m")),
                "insider_own": d.get("insider_own_pct"),
                "inst_own": d.get("inst_own_pct"), "inst_inc": d.get("inst_increases"), "inst_dec": d.get("inst_decreases"),
                "news_sentiment": d.get("news_sentiment"), "news_week": d.get("news_articles_week"),
                "news_avg": d.get("news_articles_avg_week"), "follows": d.get("follows_30d"),
                "search_change": d.get("search_interest_change_pct"),
                "dividend_yield": d.get("dividend_yield"),
                "market_cap": fmt_money(d.get("market_cap") or (r["bulk"] or {}).get("market_cap")),
                "beta": d.get("beta") if d.get("beta") is not None else (r["bulk"] or {}).get("beta"),
                "headlines": (d.get("headlines") or [])[:6],
                "url": d.get("source_url"),
            },
        })
    return rows


def render(df: pd.DataFrame, stats: dict, top_n: int, run_date: str,
           verdicts: dict | None = None) -> str:
    # The template is named dashboard.html.j2, so a rule keyed on ".html" never matched and
    # scraped headlines and company names went into the page unescaped. Match ".j2" too.
    env = Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)),
                      autoescape=select_autoescape(["html", "j2"]))
    tpl = env.get_template("dashboard.html.j2")
    rows = build_rows(df, top_n, verdicts)
    excluded = df[df["excluded"] != ""][["ticker", "company", "excluded"]].head(40).to_dict("records")
    html = tpl.render(
        rows=rows, stats=stats, run_date=run_date, top_n=top_n, excluded=excluded,
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
        weights={k: round(v * 100) for k, v in WEIGHTS.items()},
        labels=COMPONENT_LABELS,
        rows_json=json.dumps([{"t": r["ticker"], "c": r["closes"]} for r in rows]),
    )
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"dashboard_{run_date}.html"
    path.write_text(html, encoding="utf-8")
    (OUT_DIR / "dashboard_latest.html").write_text(html, encoding="utf-8")
    return str(path)
