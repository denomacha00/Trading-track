"""Read-only performance analytics computed from REAL closed trades.

Pure functions over stored ``Trade`` records — no network, no fabrication. Every
number here is derived from trades the user actually executed. Paper and live
results are reported SEPARATELY so simulated gains are never mistaken for real
money (this is a real-money app; that distinction matters).
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Iterable


def _pnl(t: Any) -> float:
    try:
        return float(getattr(t, "pnl", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _max_drawdown(pnls: list[float]) -> float:
    """Largest peak-to-trough drop of the cumulative realized-P&L curve (>= 0).

    ``pnls`` must already be in trade-close order. This is realized drawdown on
    booked P&L, not mark-to-market on open positions.
    """
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def _summarize(trades: list[Any]) -> dict[str, Any]:
    """Core stats for a set of closed trades (already close-ordered)."""
    n = len(trades)
    pnls = [_pnl(t) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)  # a positive magnitude
    total_pnl = sum(pnls)
    # profit_factor is undefined with no losing trades — report None (honest)
    # rather than a fake "infinity" that reads like a real number.
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None
    return {
        "closed_trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": n - len(wins) - len(losses),
        "win_rate_pct": round(len(wins) / n * 100.0, 2) if n else 0.0,
        "total_pnl": round(total_pnl, 8),
        "gross_profit": round(gross_profit, 8),
        "gross_loss": round(gross_loss, 8),
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "avg_win": round(gross_profit / len(wins), 8) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 8) if losses else 0.0,
        "expectancy": round(total_pnl / n, 8) if n else 0.0,
        "largest_win": round(max(wins), 8) if wins else 0.0,
        "largest_loss": round(min(losses), 8) if losses else 0.0,
        "max_drawdown": round(_max_drawdown(pnls), 8),
    }


def _sort_key(t: Any):
    return getattr(t, "closed_at", None) or getattr(t, "opened_at", None)


def compute_performance(trades: Iterable[Any]) -> dict[str, Any]:
    """Compute performance analytics from a user's trades.

    Only CLOSED trades count (an open position has no realized result). Results
    are ordered by close time so the drawdown curve is meaningful, then split
    into overall / paper / live buckets plus a per-symbol breakdown. Robust to
    missing/odd fields: anything unparseable contributes 0, never an error.
    """
    closed = [t for t in trades if str(getattr(t, "status", "")).lower() == "closed"]
    try:
        closed.sort(key=_sort_key)
    except TypeError:
        # Heterogeneous or naive-vs-aware timestamps — keep insertion order
        # rather than crash; the summary is still correct, only the drawdown
        # ordering is best-effort.
        pass

    by_symbol: dict[str, dict[str, Any]] = {}
    holds: list[float] = []
    curve: list[dict[str, Any]] = []
    for t in closed:
        sym = str(getattr(t, "symbol", "") or "?")
        b = by_symbol.setdefault(sym, {"symbol": sym, "trades": 0, "pnl": 0.0, "wins": 0})
        b["trades"] += 1
        p = _pnl(t)
        b["pnl"] = round(b["pnl"] + p, 8)
        if p > 0:
            b["wins"] += 1
        opened = getattr(t, "opened_at", None)
        closed_at = getattr(t, "closed_at", None)
        if isinstance(opened, dt.datetime) and isinstance(closed_at, dt.datetime):
            try:
                holds.append((closed_at - opened).total_seconds())
            except (TypeError, ValueError):
                pass
        # One realized-P&L event per closed trade, in close order. We deliberately
        # do NOT pre-sum a cumulative here: paper and live must never be added into
        # one curve (simulated vs real money), so the running total is built on the
        # client per selected mode. ``t`` is null when the close time is missing.
        curve.append(
            {
                "t": closed_at.isoformat() if isinstance(closed_at, dt.datetime) else None,
                "pnl": round(p, 8),
                "symbol": sym,
                "mode": str(getattr(t, "mode", "") or "paper").lower(),
            }
        )

    overall = _summarize(closed)
    paper = _summarize([t for t in closed if str(getattr(t, "mode", "")).lower() == "paper"])
    live = _summarize([t for t in closed if str(getattr(t, "mode", "")).lower() == "live"])
    symbols = sorted(by_symbol.values(), key=lambda d: d["pnl"], reverse=True)
    return {
        **overall,
        "avg_hold_seconds": round(sum(holds) / len(holds), 1) if holds else None,
        "paper": paper,
        "live": live,
        "by_symbol": symbols,
        "equity_curve": curve,
    }
