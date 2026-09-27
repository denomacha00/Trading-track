"""Persisted runtime state (KV store) so the bot survives restarts.

Multi-user: every piece of runtime state is scoped by ``user_id`` so one user's
settings/wallet never leak into another's. Keys look like
``settings_overrides:{user_id}`` and ``paper_balance:{user_id}``. A ``user_id``
of ``None`` maps to the legacy global keys (used by single-tenant tests and any
pre-multiuser data).

What lives here and why:
- ``settings_overrides``: fields changed via the settings endpoint. Without this,
  a restart would silently reset trading mode, auto-trade on/off, risk params.
- ``paper_balance``: the simulated wallet, so paper P&L survives restarts.

Everything is stored as JSON text in the ``kv_store`` table.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import KeyValue

SETTINGS_KEY = "settings_overrides"
PAPER_BALANCE_KEY = "paper_balance"
STRATEGY_KEY = "strategy_config"
# The background monitor loop is a single server-wide task shared by all users,
# so its tick cadence is a GLOBAL value (not per-user): stored unscoped and read
# live by monitor_loop each iteration. Clamped to a safe range at read time.
MONITOR_INTERVAL_KEY = "monitor_interval_seconds"
MONITOR_INTERVAL_MIN = 3.0
MONITOR_INTERVAL_MAX = 60.0


def _scoped(base: str, user_id: Optional[int]) -> str:
    return base if user_id is None else f"{base}:{user_id}"


def kv_get(db: Session, key: str, default: Any = None) -> Any:
    row = db.get(KeyValue, key)
    if row is None:
        return default
    try:
        return json.loads(row.value)
    except (ValueError, TypeError):
        return default


def kv_set(db: Session, key: str, value: Any) -> None:
    payload = json.dumps(value)
    row = db.get(KeyValue, key)
    if row is None:
        db.add(KeyValue(key=key, value=payload))
    else:
        row.value = payload
    db.commit()


def load_settings_overrides(db: Session, user_id: Optional[int] = None) -> dict[str, Any]:
    data = kv_get(db, _scoped(SETTINGS_KEY, user_id), {})
    return data if isinstance(data, dict) else {}


def save_settings_overrides(
    db: Session, overrides: dict[str, Any], user_id: Optional[int] = None
) -> None:
    kv_set(db, _scoped(SETTINGS_KEY, user_id), overrides)


def load_paper_balance(
    db: Session, default: float, user_id: Optional[int] = None
) -> float:
    val = kv_get(db, _scoped(PAPER_BALANCE_KEY, user_id), None)
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def save_paper_balance(
    db: Session, balance: float, user_id: Optional[int] = None
) -> None:
    kv_set(db, _scoped(PAPER_BALANCE_KEY, user_id), float(balance))


def load_strategy_configs(db: Session, user_id: Optional[int] = None) -> dict[str, Any]:
    """The user's saved, trained strategies keyed by uppercase SYMBOL.

    Each value is a dict: ``{strategy, timeframe, params, metrics, trained_at}``.
    This is how a strategy tuned in training becomes something the live bot can
    actually trade with (see TradingEngine.analyze_symbol) — it is not thrown
    away when the training request returns.
    """
    data = kv_get(db, _scoped(STRATEGY_KEY, user_id), {})
    return data if isinstance(data, dict) else {}


def save_strategy_configs(
    db: Session, configs: dict[str, Any], user_id: Optional[int] = None
) -> None:
    kv_set(db, _scoped(STRATEGY_KEY, user_id), configs)


def clamp_monitor_interval(seconds: float) -> float:
    """Clamp a requested monitor cadence into the safe [MIN, MAX] range."""
    try:
        s = float(seconds)
    except (ValueError, TypeError):
        return MONITOR_INTERVAL_MIN
    return max(MONITOR_INTERVAL_MIN, min(MONITOR_INTERVAL_MAX, s))


def load_monitor_interval(db: Session, default: float = 5.0) -> float:
    """Global monitor tick cadence (seconds), clamped to the safe range."""
    val = kv_get(db, MONITOR_INTERVAL_KEY, None)
    return clamp_monitor_interval(val if val is not None else default)


def save_monitor_interval(db: Session, seconds: float) -> float:
    """Persist the global monitor cadence (clamped). Returns the stored value."""
    clamped = clamp_monitor_interval(seconds)
    kv_set(db, MONITOR_INTERVAL_KEY, clamped)
    return clamped


def purge_user_state(db: Session, user_id: int) -> int:
    """Delete every KV row scoped to ``user_id`` (settings, wallet, strategies).

    Called when an account is deleted so no orphaned runtime state — including a
    simulated wallet balance — outlives the user it belonged to. Returns the
    number of rows removed. Never touches the legacy global (``user_id=None``)
    keys, which have no ``:{id}`` suffix.
    """
    removed = 0
    for base in (SETTINGS_KEY, PAPER_BALANCE_KEY, STRATEGY_KEY):
        row = db.get(KeyValue, _scoped(base, user_id))
        if row is not None:
            db.delete(row)
            removed += 1
    db.commit()
    return removed
