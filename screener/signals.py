"""Every per-stock MarketBeat signal on a given day, from stored history only.

The menu list and the backtest both read signals from here, so they cannot drift apart. A
signal for day D uses rating snapshots, rating actions and closes dated on or before D and
nothing later (tests/test_signals.py::test_no_lookahead).

  counts, net        buy/hold/sell on D (ratings.counts_at) and net buys (mb_ratio.net_buys)
  net_change_91d     net buys on D minus net buys 91 days earlier
  upgrades_60d       rating upgrades / downgrades in the 60 days up to D
  target_raises_30d  price-target raises / cuts in the 30 days up to D
  target_*           each firm's latest price target in the year up to D: mean, low, high
  n_targets          how many firms' targets that is
Flow and target signals only count actions the screener had recorded by D (first_seen),
because MarketBeat keeps just each firm's latest action; they are None before that.
  upside             target_mean against the close on or before D
  dispersion         (high - low) / mean: how far apart the analysts are
"""
from datetime import date, timedelta

import pandas as pd

from . import ratings
from .mb_ratio import net_buys

FLOW_DAYS = 60
REVISION_DAYS = 30
TARGET_DAYS = 365
MOMENTUM_DAYS = 91


def describe(s: dict) -> str:
    """One line for the menu: the signals a person can read at a glance."""
    trend = s["net_change_91d"]
    parts = [f"rating trend 3 mo {trend:+d}" if trend is not None else "rating trend 3 mo -"]
    if s["upgrades_60d"] is None:
        return "  ·  ".join(parts + ["no analyst actions recorded yet"])
    parts += [f"upgrades/downgrades 60 days {s['upgrades_60d']}/{s['downgrades_60d']}",
              f"target raises/cuts 30 days {s['target_raises_30d']}/{s['target_cuts_30d']}"]
    if s["target_low"] is not None:
        parts.append(f"analysts' targets ${s['target_low']:,.0f}–${s['target_high']:,.0f}")
    return "  ·  ".join(parts)


def _days_before(day: str, n: int) -> str:
    return (date.fromisoformat(day) - timedelta(days=n)).isoformat()


def _close_on(closes: pd.Series | None, day: str) -> float | None:
    if closes is None or closes.empty:
        return None
    key = pd.Timestamp(day) if isinstance(closes.index, pd.DatetimeIndex) else day
    past = closes[closes.index <= key].dropna()
    return float(past.iloc[-1]) if len(past) else None


def _net_on(stock: dict, day: str) -> int | None:
    c = ratings.counts_at(stock, day)
    return net_buys(*c) if c else None


def at(stock: dict, closes: pd.Series | None, day: str) -> dict:
    actions = [a for a in stock.get("actions") or [] if a.get("date") and a["date"] <= day]
    snaps = {d: v for d, v in (stock.get("snapshots") or {}).items() if d <= day}
    past = {"actions": actions, "snapshots": snaps}

    counts = ratings.counts_at(past, day)
    net = net_buys(*counts) if counts else None
    before = _net_on(past, _days_before(day, MOMENTUM_DAYS))
    flow_from, revision_from = _days_before(day, FLOW_DAYS), _days_before(day, REVISION_DAYS)
    target_from = _days_before(day, TARGET_DAYS)

    out = {"counts": counts, "net": net,
           "net_change_91d": net - before if net is not None and before is not None else None,
           "upgrades_60d": None, "downgrades_60d": None, "target_raises_30d": None,
           "target_cuts_30d": None, "n_targets": 0}
    # MarketBeat lists only each firm's latest action, so a stored action dated before D may
    # have replaced others that are gone. Flow and target signals therefore use only actions
    # the screener had recorded by D (first_seen); before that there is nothing to count.
    seen = [a for a in actions if (a.get("first_seen") or a["date"]) <= day]
    if seen:
        out.update(upgrades_60d=0, downgrades_60d=0, target_raises_30d=0, target_cuts_30d=0)
    latest: dict[str, tuple[str, float]] = {}
    for a in seen:
        kind = (a.get("action") or "").strip().lower()
        if a["date"] >= flow_from:
            if kind == "upgrade":
                out["upgrades_60d"] += 1
            elif kind == "downgrade":
                out["downgrades_60d"] += 1
        if a["date"] >= revision_from:
            if kind == "boost target":
                out["target_raises_30d"] += 1
            elif kind == "lower target":
                out["target_cuts_30d"] += 1
        firm, target = a.get("firm"), a.get("target_to")
        if firm and target and a["date"] >= target_from:
            if firm not in latest or a["date"] >= latest[firm][0]:
                latest[firm] = (a["date"], float(target))

    targets = [t for _, t in latest.values()]
    mean = sum(targets) / len(targets) if targets else None
    price = _close_on(closes, day)
    out.update(n_targets=len(targets), target_mean=mean, target_low=min(targets) if targets else None,
               target_high=max(targets) if targets else None,
               upside=mean / price - 1 if mean and price else None,
               dispersion=(max(targets) - min(targets)) / mean if mean else None)
    return out
