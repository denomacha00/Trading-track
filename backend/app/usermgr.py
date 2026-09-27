"""Per-user trading engines (multi-tenant orchestration).

Each licensed user gets their OWN :class:`TradingEngine` instance, built from a
copy of the global env settings with that user's decrypted Binance keys and
their persisted per-user settings overrides layered on top. The AI/LLM key is
app-wide (operator env), never per-user. Engines are cached in memory and
hydrated lazily on first use, and rebuilt when a user changes their keys or
settings.

Security: a user's exchange secrets are only ever held decrypted inside their
own engine's Settings object in memory; they are stored on disk encrypted
(Fernet, keyed off SECRET_KEY) and never logged.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.engine import TradingEngine
from app.exchange import BinanceConnector
from app.models import LicenseStatus, User
from app.security import decrypt_secret

logger = logging.getLogger(__name__)


def build_settings_for_user(user: User) -> Settings:
    """Return a Settings copy carrying THIS user's decrypted Binance keys.

    Only the EXCHANGE keys are per-user — each user trades their own Binance
    account. The AI/LLM is APP-WIDE and "inbuilt": every user shares the single
    operator-provided key from the global env Settings (``AI_API_KEY`` etc.), so
    no user ever enters an AI key. We therefore leave ``ai_*`` at the global
    values and never layer per-user AI config on top.
    """
    base = get_settings()
    data = base.model_dump()
    sk = base.secret_key
    # SAFETY: a brand-new user always starts in PAPER, regardless of the
    # operator's global TRADING_MODE. Going live is an explicit, per-user opt-in
    # (persisted via update_settings and re-applied in restore_state), so nobody
    # ever trades real funds by merely being added to a live-configured server.
    data["trading_mode"] = "paper"
    data["binance_api_key"] = decrypt_secret(sk, user.binance_api_key_enc or "") or ""
    data["binance_api_secret"] = (
        decrypt_secret(sk, user.binance_api_secret_enc or "") or ""
    )
    data["binance_testnet"] = bool(user.binance_testnet)
    return Settings(**data)


class EngineManager:
    """Owns the per-user engine cache and shared broadcaster wiring."""

    def __init__(self) -> None:
        self._engines: dict[int, TradingEngine] = {}
        self._lock = threading.Lock()
        self._broadcaster: Optional[Any] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def attach_broadcaster(self, broadcaster: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._broadcaster = broadcaster
        self._loop = loop
        with self._lock:
            for eng in self._engines.values():
                eng.attach_broadcaster(broadcaster, loop)

    def _build(self, db: Session, user: User) -> TradingEngine:
        settings = build_settings_for_user(user)
        eng = TradingEngine(settings, BinanceConnector(settings), user_id=user.id)
        if self._broadcaster and self._loop:
            eng.attach_broadcaster(self._broadcaster, self._loop)
        eng.restore_state(db)
        # Preserve the operator's explicit run/stop across restarts, and NEVER
        # auto-resume a bot that a drawdown kill-switch halted. Only a FRESH engine
        # (no persisted runtime yet) defaults to running; a persisted stop or a
        # tripped kill-switch always wins. restore_state has already applied any
        # persisted running flag and forced running=False on an active trip.
        if not eng._runtime_restored and not eng._killswitch_tripped:
            eng.running = True
        # Persist the (possibly defaulted) state so a later rebuild is durable.
        eng.persist_runtime(db)
        return eng

    def get(self, db: Session, user: User) -> TradingEngine:
        with self._lock:
            eng = self._engines.get(user.id)
            if eng is None:
                eng = self._build(db, user)
                self._engines[user.id] = eng
            return eng

    def refresh(self, db: Session, user: User) -> TradingEngine:
        """Rebuild a user's engine after their keys/settings changed."""
        with self._lock:
            self._engines.pop(user.id, None)
            eng = self._build(db, user)
            self._engines[user.id] = eng
            return eng

    def drop(self, user_id: int) -> None:
        with self._lock:
            self._engines.pop(user_id, None)

    def active_users(self, db: Session) -> list[User]:
        stmt = select(User).where(User.license_status == LicenseStatus.active.value)
        # Exclude time-limited licences that have passed their expiry so an
        # unpaid/expired client's bot stops auto-trading (its open positions are
        # still managed elsewhere via check_open_positions on the stored engine).
        return [u for u in db.scalars(stmt).all() if not u.license_expired]

    def engines_for_active_users(self, db: Session) -> list[tuple[User, TradingEngine]]:
        """Return (user, engine) for every currently-licensed user."""
        out: list[tuple[User, TradingEngine]] = []
        for user in self.active_users(db):
            try:
                out.append((user, self.get(db, user)))
            except Exception as exc:  # keep one bad user from stalling the loop
                logger.warning("failed to build engine for user %s: %s", user.id, exc)
        return out


_manager: Optional[EngineManager] = None


def get_manager() -> EngineManager:
    global _manager
    if _manager is None:
        _manager = EngineManager()
    return _manager
