"""Interactive browsing of whatever is already in data/screener.db.

No network calls and no scraping: this re-slices the data the pipeline has already
collected. Categories differ in what data even exists for them, so each one is scored
with signals that are actually available (see homework.txt, parts 2 and 3):

  stocks  - analyst consensus drives the ranking, as in the main screen
  etfs    - no analyst coverage exists, so price/technical signals only
  penny   - coverage is nearly absent and liquidity is the real risk, so
            price/technical signals with a volume floor and loud warnings
"""
import sqlite3

from . import ansi as a

from . import db
from .config import MIN_DOLLAR_VOL
from .score import clip01, hard_exclusions, momentum_score, price_outlook, rating_counts_of

# NYSEARCA is almost entirely funds. BATS is Cboe's own venue and lists real operating
# companies (CBOE itself), so it is not treated as an ETF signal.
ETF_EXCHANGES = {"NYSEARCA"}
# MarketBeat reports each listing in its own currency - LON in pence, TSE in CAD, ETR in EUR.
# Nothing here converts, so price-band categories are restricted to US listings and everything
# else is labelled with its exchange.
USD_EXCHANGES = {"NASDAQ", "NYSE", "NYSEAMERICAN", "NYSEARCA", "BATS", "OTCMKTS", "AMEX"}
MIN_ANALYSTS_FOR_RATIO = 5
BEARISH_RATINGS = {"sell", "strong sell", "reduce", "moderate sell", "underperform"}
SPIKE_1M = 1.0  # a 100%+ month usually means the move already happened
# Words that appear in a FUND's name. Bare issuer names are deliberately excluded:
# "WisdomTree" alone is the asset manager (WT), not a fund.
FUND_WORDS = (" etf", "ucits", "ishares", "spdr", "proshares", "direxion",
              "index fund", " index etf", " trust series")
# Funds whose price action does not mean what a momentum score assumes it means: leveraged
# and inverse funds decay in chop (homework.txt 2.4), covered-call funds cap their own upside,
# and volatility products bleed on roll. They are labelled, and excluded unless asked for.
LEVERAGED_WORDS = ("ultra", "ultrapro", "2x", "3x", "-1x", "leveraged", "bull ", "bear ",
                   "daily ", "double", "triple")
INVERSE_WORDS = ("short ", "inverse", "-1x", "bear ")
INCOME_FUND_WORDS = ("covered call", "buywrite", "buy-write", "premium income", "option income")
VOLATILITY_WORDS = ("vix", "volatility futures", "short vix")

PENNY_MAX_PRICE = 5.0
PENNY_MIN_DOLLAR_VOL = 250_000

CATEGORIES = {
    "1": ("stocks", "Individual stocks — ranked on analyst consensus, momentum and risk"),
    "2": ("etfs", "ETFs — no analyst data exists, so momentum and liquidity only"),
    "3": ("penny", "Penny stocks (under $5) — speculative, thin, mostly uncovered"),
    "4": ("all", "Everything, ranked together"),
}

def banner() -> str:
    line = "═" * 62
    return (a.c("\n  ╔" + line + "╗", "cyan") + "\n"
            + a.c("  ║", "cyan") + a.c(a.pad("  MarketBeat screener", 62), "bold", "bright_cyan")
            + a.c("║", "cyan") + "\n"
            + a.c("  ║", "cyan")
            + a.c(a.pad("  Browsing collected data — nothing is fetched or scraped.", 62), "grey")
            + a.c("║", "cyan") + "\n"
            + a.c("  ╚" + line + "╝", "cyan"))


def load(con: sqlite3.Connection) -> tuple[dict, dict, dict]:
    return (db.load_json(con, "mb_bulk"), db.load_json(con, "prices"), db.load_json(con, "mb_detail"))


def structural_flags(row: dict) -> list[str]:
    """Reasons a fund's momentum should not be read as 'this is doing well'."""
    name = " " + (row.get("company") or "").lower() + " "
    flags = []
    if any(w in name for w in LEVERAGED_WORDS):
        flags.append("leveraged")
    if any(w in name for w in INVERSE_WORDS):
        flags.append("inverse")
    if any(w in name for w in INCOME_FUND_WORDS):
        flags.append("covered-call")
    if any(w in name for w in VOLATILITY_WORDS):
        flags.append("volatility")
    return flags


def is_etf(row: dict) -> bool:
    if (row.get("exchange") or "").upper() in ETF_EXCHANGES:
        return True
    name = " " + (row.get("company") or "").lower() + " "
    return any(w in name for w in FUND_WORDS)


def category_rows(category: str, bulk: dict, prices: dict,
                  price_min: float = 0.0, price_max: float | None = None) -> list[str]:
    out = []
    for t, b in bulk.items():
        price = (prices.get(t) or {}).get("last_close") or b.get("price")
        if price is None:
            continue
        etf = is_etf(b)
        usd = (b.get("exchange") or "").upper() in USD_EXCHANGES
        if category == "all":
            if price < price_min or (price_max is not None and price > price_max):
                continue
            out.append(t)
            continue
        if category == "etfs" and not etf:
            continue
        # Price thresholds only mean something in one currency.
        if category == "penny" and (etf or not usd or price >= PENNY_MAX_PRICE):
            continue
        if category == "stocks" and (etf or (usd and price < PENNY_MAX_PRICE)):
            continue
        if price < price_min or (price_max is not None and price > price_max):
            continue
        out.append(t)
    return out


def technical_score(p: dict, ret_rank: float | None) -> float:
    """Used where no analyst data exists. Momentum, trend and a volatility penalty."""
    mom = momentum_score(p, ret_rank) or 0.0
    vol = p.get("vol_20d_annualized")
    penalty = 0.0 if vol is None else (0.20 if vol > 0.45 else 0.10 if vol > 0.35 else 0.0)
    liq = clip01((p.get("avg_dollar_vol_20d") or 0) / (20 * MIN_DOLLAR_VOL)) or 0.0
    return max(0.0, 0.75 * mom + 0.25 * liq - penalty)


def rank(category: str, tickers: list[str], bulk: dict, prices: dict, detail: dict, limit: int,
         include_structural: bool = False) -> list[dict]:
    priced = [t for t in tickers if (prices.get(t) or {}).get("ret_1m") is not None]
    ordered = sorted(priced, key=lambda t: prices[t]["ret_1m"])
    rank_of = {t: (i + 1) / max(len(ordered), 1) for i, t in enumerate(ordered)}

    rows = []
    for t in priced:
        b, p, d = bulk.get(t, {}), prices.get(t, {}), detail.get(t, {})
        dv = p.get("avg_dollar_vol_20d") or 0
        floor = PENNY_MIN_DOLLAR_VOL if category == "penny" else MIN_DOLLAR_VOL
        if dv < floor:
            continue
        if not include_structural and structural_flags(b):
            continue
        counts = rating_counts_of(d)
        analyst_based = (category in ("stocks", "all") and counts is not None
                         and sum(counts) >= MIN_ANALYSTS_FOR_RATIO)
        if analyst_based:
            buy, hold, sell = counts
            total = buy + hold + sell
            score = (0.45 * (momentum_score(p, rank_of.get(t)) or 0)
                     + 0.35 * (buy / total if total else 0)
                     + 0.20 * (clip01(((d.get("upside_pct") or b.get("upside_pct") or 0)) / 40) or 0))
            basis = "analyst + momentum"
        else:
            score = technical_score(p, rank_of.get(t))
            basis = "momentum only"
            # Momentum scoring must not ignore an analyst body that is actively negative.
            rating = (d.get("consensus_rating") or b.get("consensus_rating") or "").lower()
            if rating in BEARISH_RATINGS:
                score *= 0.5
                basis = "momentum, penalised for bearish consensus"
        rows.append({
            "ticker": t,
            "company": (b.get("company") or "")[:30],
            "exchange": b.get("exchange"),
            "price": p.get("last_close") or b.get("price"),
            "ret_1m": p.get("ret_1m"),
            "rsi": p.get("rsi14"),
            "vol": p.get("vol_20d_annualized"),
            "dollar_vol": dv,
            "rating": d.get("consensus_rating") or b.get("consensus_rating"),
            "counts": counts,
            "upside": d.get("upside_pct") if d.get("upside_pct") is not None else b.get("upside_pct"),
            "score": round(100 * score, 1),
            "basis": basis,
            "warnings": hard_exclusions({**b, **p, **d}),
            "structural": structural_flags(b),
            "outlook": price_outlook(d, p, b),
            "spiked": (p.get("ret_1m") or 0) >= SPIKE_1M,
            "bearish": (d.get("consensus_rating") or b.get("consensus_rating") or "").lower() in BEARISH_RATINGS,
        })
    rows.sort(key=lambda r: -r["score"])
    return rows[:limit]


def show(category: str, rows: list[dict], price_note: str = ""):
    if not rows:
        print(a.c("\n  Nothing matches that combination.", "yellow"))
        print(a.c("  Try a wider price range, or run `python run.py all` to collect more data.\n", "grey"))
        return

    width = 108
    title = f" Top {len(rows)} — {category}{(' · ' + price_note) if price_note else ''} "
    print()
    print("  " + a.c("┌" + "─" * width + "┐", "cyan"))
    print("  " + a.c("│", "cyan") + a.c(a.pad(title, width, "^"), "bold", "bright_cyan") + a.c("│", "cyan"))
    print("  " + a.c("├" + "─" * width + "┤", "cyan"))

    head = (f" {'#':>3}  {'TICKER':<8}{'COMPANY':<26}{'PRICE':>10}{'1-MO':>8}{'RSI':>5}"
            f"{'$VOL/DAY':>10}  {'BUY/HOLD/SELL':<15}{'SCORE':>16}")
    print("  " + a.c("│", "cyan") + a.c(a.pad(head, width), "bold", "grey") + a.c("│", "cyan"))
    print("  " + a.c("├" + "─" * width + "┤", "cyan"))

    for i, r in enumerate(rows, 1):
        counts = (f"{r['counts'][0]}B/{r['counts'][1]}H/{r['counts'][2]}S" if r["counts"]
                  else (r["rating"] or "no coverage"))
        counts_style = ("bright_green" if r["counts"] and r["counts"][2] == 0 and r["counts"][0] >= 5
                        else "red" if r["bearish"] else "grey")
        dv = r["dollar_vol"]
        dv_s = (f"${dv / 1e9:.1f}B" if dv >= 1e9 else f"${dv / 1e6:.0f}M" if dv >= 1e6
                else f"${dv / 1e3:.0f}K")
        ret, rsi = r["ret_1m"], r["rsi"]
        rsi_style = ("bright_green" if rsi is not None and 50 <= rsi <= 70
                     else "yellow" if rsi is not None and rsi > 70 else "grey")

        cells = (
            a.c(f" {i:>3}", "grey") + "  "
            + a.c(a.pad(r["ticker"], 8), "bold", "bright_cyan")
            + a.c(a.pad(r["company"][:25], 26), "white")
            + a.pad(a.c(f"${r['price']:,.2f}" if r["price"] is not None else "–", "white"), 10, ">")
            + a.pad(a.c(f"{ret * 100:+.1f}%" if ret is not None else "–", a.money_move(ret)), 8, ">")
            + a.pad(a.c(f"{rsi:.0f}" if rsi is not None else "–", rsi_style), 5, ">")
            + a.pad(a.c(dv_s, "grey"), 10, ">") + "  "
            + a.pad(a.c(counts, counts_style), 15)
            + " " + a.bar(r["score"] / 100, 9, a.heat(r["score"]))
            + a.pad(a.c(f"{r['score']:.1f}", a.heat(r["score"])), 6, ">")
        )
        print("  " + a.c("│", "cyan") + a.pad(cells, width) + a.c("│", "cyan"))

        notes = []
        if r["structural"]:
            notes.append("/".join(r["structural"]) + " fund — a market bet, not a business")
        if r["spiked"]:
            notes.append("already spiked this month")
        if r["bearish"]:
            notes.append("analysts rate it Sell")
        if (r["exchange"] or "").upper() not in USD_EXCHANGES:
            notes.append(f"{r['exchange']} listing — local currency, not USD")
        if notes:
            print("  " + a.c("│", "cyan")
                  + a.pad(a.c("       " + " · ".join(notes), "yellow"), width)
                  + a.c("│", "cyan"))

    print("  " + a.c("└" + "─" * width + "┘", "cyan"))

    bases = {r["basis"] for r in rows}
    print(a.c(f"  Ranked on {', '.join(sorted(bases))}; analyst scoring needs "
              f">= {MIN_ANALYSTS_FOR_RATIO} ratings.", "grey"))
    if category == "etfs":
        print(a.c("  ETFs carry no analyst ratings — momentum and liquidity only.", "grey"))
    if category == "penny":
        print(a.c("  Speculative: wide spreads, dilution risk, most of this universe is uncovered.", "grey"))
    if not any(r["structural"] for r in rows):
        print(a.c("  Leveraged, inverse, covered-call and volatility funds excluded "
                  "(--include-structural to show them).", "grey"))
    print(a.c("  Informational only — not investment advice.\n", "grey"))


def _ask(question: str, default: str = "") -> str | None:
    """None means the user wants out."""
    suffix = a.c(f" [{default}]", "grey") if default else ""
    try:
        answer = input(a.c("  ➜ ", "bright_cyan") + question + suffix + a.c(": ", "grey")).strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if answer.lower() in ("q", "quit", "exit"):
        return None
    return answer or default


def _step(number: int, question: str):
    print()
    print("  " + a.c(f" {number} ", "bold", "on_blue") + "  " + a.c(question, "bold", "white"))
    print()


def prompt_category() -> str | None:
    _step(1, "What kind of stocks?")
    colours = {"1": "bright_green", "2": "bright_cyan", "3": "bright_yellow", "4": "bright_magenta"}
    for key, (name, label) in CATEGORIES.items():
        head, _, tail = label.partition(" — ")
        print("     " + a.c(key + ".", "bold", colours[key]) + " "
              + a.c(a.pad(head, 27), "bold", colours[key]) + a.c(tail, "grey"))
    print()
    while True:
        choice = _ask("Choice [1-4, q to quit]")
        if choice is None:
            return None
        if choice in CATEGORIES:
            return CATEGORIES[choice][0]
        if choice.lower() in {name for name, _ in CATEGORIES.values()}:
            return choice.lower()
        print(a.c("     Pick 1, 2, 3, 4, or q.", "red"))


def prompt_count(default: int = 20) -> int | None:
    _step(2, "How many stocks should it list?")
    while True:
        answer = _ask(f"Number 1-100", str(default))
        if answer is None:
            return None
        try:
            n = int(answer)
        except ValueError:
            print(a.c("     Enter a whole number.", "red"))
            continue
        if 1 <= n <= 100:
            return n
        print(a.c("     Pick a number between 1 and 100.", "red"))


def prompt_price_band() -> tuple[float, float | None] | None:
    """Share price range, in the listing's own currency."""
    _step(3, "What should the shares cost?")
    presets = {"1": (0.0, None, "Any price", ""),
               "2": (0.0, 5.0, "Under $5", "penny stocks"),
               "3": (5.0, 75.0, "$5 to $75", ""),
               "4": (75.0, 500.0, "$75 to $500", ""),
               "5": (500.0, None, "Over $500", "")}
    for key, (_, _, label, note) in presets.items():
        print("     " + a.c(key + ".", "bold", "bright_cyan") + " "
              + a.c(a.pad(label, 16), "white") + a.c(note, "grey"))
    print("     " + a.c("6.", "bold", "bright_cyan") + " "
          + a.c(a.pad("Custom range", 16), "white") + a.c("you choose the minimum and maximum", "grey"))
    print()
    while True:
        choice = _ask("Choice [1-6]", "1")
        if choice is None:
            return None
        if choice in presets:
            lo, hi, _, _ = presets[choice]
            return (lo, hi)
        if choice == "6":
            lo = _ask("Minimum $ per share", "0")
            if lo is None:
                return None
            hi = _ask("Maximum $ per share (blank for no cap)")
            if hi is None:
                return None
            try:
                return (float(lo or 0), float(hi) if hi else None)
            except ValueError:
                print(a.c("     Enter numbers, e.g. 10 and 30.", "red"))
                continue
        print(a.c("     Pick 1-6.", "red"))


def prompt_verify() -> bool:
    _step(4, "Watch it check these on MarketBeat?")
    print("     " + a.c("Opens Chrome and re-reads each page live, with notes, so you can see", "grey"))
    print("     " + a.c("the numbers being confirmed rather than taking them on trust.", "grey"))
    print()
    answer = _ask("Watch the live check? [y/N]", "n")
    return bool(answer) and answer.strip().lower().startswith("y")


def fetch_missing_prices(category: str, tickers: list[str], bulk: dict, prices: dict, cap: int = 400):
    """Price the busiest names in a category that the pipeline never fetched.

    The main pipeline only prices analyst-backed candidates, so ETFs and penny stocks are
    mostly absent. This tops them up on demand, busiest first, bounded so it stays quick.
    """
    from . import enrich
    missing = [t for t in tickers if t not in prices or prices[t].get("missing")]
    if not missing:
        return prices
    missing.sort(key=lambda t: -((bulk.get(t) or {}).get("avg_volume") or 0)
                 * ((bulk.get(t) or {}).get("price") or 0))
    batch = missing[:cap]
    print(f"  {len(missing):,} {category} have no price history; fetching the busiest {len(batch)}...")
    enrich.run(batch)
    with db.connect() as con:
        return db.load_json(con, "prices")


def run(limit: int = 20, category: str | None = None, once: bool = False, fetch: bool = False,
        price_min: float = 0.0, price_max: float | None = None, include_structural: bool = False):
    with db.connect() as con:
        bulk, prices, detail = load(con)
    if not bulk:
        print("No data yet — run `python run.py all` first.")
        return
    print(f"\n  Loaded {len(bulk):,} stocks, {len(prices):,} with price history, "
          f"{len(detail):,} with full MarketBeat profiles.")
    interactive = category is None
    while True:
        if interactive:
            print(banner())
            cat = prompt_category()
            if cat is None:
                print("\n  Bye.\n")
                return
            count = prompt_count(limit)
            if count is None:
                print("\n  Bye.\n")
                return
            band = prompt_price_band()
            if band is None:
                print("\n  Bye.\n")
                return
            limit, price_min, price_max = count, band[0], band[1]
            band_text = ("any price" if price_max is None and price_min == 0
                         else f"over ${price_min:,.0f}" if price_max is None
                         else f"under ${price_max:,.0f}" if price_min == 0
                         else f"${price_min:,.0f}-${price_max:,.0f}")
            print(f"\n  Looking for the top {limit} {cat}, {band_text}.")
            watch = prompt_verify()
        else:
            cat, watch, band_text = category, False, ""
        tickers = category_rows(cat, bulk, prices, price_min, price_max)
        print(f"\n  {len(tickers):,} {cat} match; ranking the ones with enough data...")
        if fetch:
            prices = fetch_missing_prices(cat, tickers, bulk, prices)
        ranked = rank(cat, tickers, bulk, prices, detail, limit, include_structural)
        if len(ranked) < limit and not fetch:
            have = sum(1 for t in tickers if t in prices and not prices[t].get("missing"))
            print(f"  (only {have:,} of these have price history — the main pipeline only prices\n"
                  f"   analyst-backed candidates. Re-run with --fetch to price more.)")
        show(cat, ranked, price_note=band_text if interactive else "")
        if interactive and watch and ranked:
            from . import verify
            verify.run(ranked, limit=min(len(ranked), 10))
        if once or not interactive:
            return
