"""Durable halt: the bot's run/stop + drawdown kill-switch + equity peak MUST
survive a restart, redeploy or engine rebuild.

The capital-preservation bug this guards against: runtime state used to live only
in memory, so any rebuild (a redeploy, or the per-user engine being rebuilt when
keys/settings change) would (a) silently resume a bot the operator had stopped,
(b) clear a tripped drawdown kill-switch and auto-resume trading straight back
into the very drawdown that halted it, and (c) reset the equity peak to zero,
blinding the drawdown measure. These tests pin the fixed behaviour: a halt/stop
persists and NEVER auto-resumes; only an explicit human re-arm clears it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.database import Base
from app.engine import TradingEngine
from app.state import load_engine_runtime


class _Conn:
    """Minimal paper-capable fake connector (no network, no credentials)."""

    has_credentials = False

    def __init__(self, price: float = 100.0):
        self._price = price
        self.market_orders: list[tuple] = []

    @property
    def connected(self) -> bool:
        return True

    def reload(self, settings):
        pass

    def set_price(self, price):
        self._price = price

    def fetch_price(self, symbol):
        return self._price

    def fetch_ohlcv(self, symbol, timeframe, limit):
        return []

    def fetch_position_amounts(self):
        return {}

    def fetch_balance(self, quote="USDT"):
        return None

    def normalize_amount(self, symbol, amount, price):
        return amount, None

    def spread_pct(self, symbol):
        return 0.0

    def create_market_order(self, symbol, side, amount):
        self.market_orders.append((symbol, side, amount))
        return {"id": "m1", "average": self._price}

    def create_limit_order(self, symbol, side, amount, price):
        return {"id": "l1"}

    def create_stop_loss_order(self, symbol, side, amount, stop_price):
        return {"id": "s1"}

    def cancel_order(self, order_id, symbol):
        pass

    def fetch_order(self, order_id, symbol):
        return None


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    session._sa_engine_ref = engine  # keep the :memory: db alive for the test
    try:
        yield session
    finally:
        session.close()


def _engine(connector, **over) -> TradingEngine:
    base = dict(
        trading_mode="paper",
        max_open_positions=5,
        risk_per_trade_pct=1.0,
        daily_loss_limit_pct=50.0,   # high, so it never interferes with these tests
        default_stop_loss_pct=2.0,
        default_take_profit_pct=4.0,
        paper_starting_balance=10_000.0,
        min_signal_confidence=0.1,
        max_drawdown_pct=20.0,
        max_spread_pct=1.0,
        reentry_cooldown_minutes=15.0,
        max_consecutive_losses=3,
        atr_stop_mult=1.5,
    )
    base.update(over)
    return TradingEngine(Settings(**base), connector, user_id=None)


# ---- a manual stop persists across a rebuild ------------------------


def test_stopped_bot_stays_stopped_across_rebuild(db):
    conn = _Conn()
    eng = _engine(conn)
    eng.running = False          # operator stops the bot
    eng.persist_runtime(db)

    eng2 = _engine(conn)         # redeploy / engine rebuild
    eng2.restore_state(db)
    assert eng2._runtime_restored is True
    assert eng2.running is False
    # The manager's fresh-engine default must NOT override a persisted stop.
    if not eng2._runtime_restored and not eng2._killswitch_tripped:
        eng2.running = True
    assert eng2.running is False


# ---- a running bot resumes running ----------------------------------


def test_running_bot_resumes_across_rebuild(db):
    conn = _Conn()
    eng = _engine(conn)
    eng.running = True
    eng.persist_runtime(db)

    eng2 = _engine(conn)
    eng2.restore_state(db)
    assert eng2.running is True


# ---- a fresh engine (no history) defaults to running ----------------


def test_fresh_engine_defaults_to_running_only_when_no_persisted_state(db):
    conn = _Conn()
    eng = _engine(conn)
    eng.restore_state(db)        # nothing persisted for this account yet
    assert eng._runtime_restored is False
    assert eng.running is False  # __init__ default until the manager decides
    # Manager rule: a brand-new engine with no history defaults to running.
    if not eng._runtime_restored and not eng._killswitch_tripped:
        eng.running = True
    assert eng.running is True


# ---- a tripped kill-switch survives + never auto-resumes ------------


def test_tripped_killswitch_survives_rebuild_and_requires_rearm(db):
    conn = _Conn()
    eng = _engine(conn)
    eng._peak_equity = 1_000.0
    eng._killswitch_tripped = True
    eng.running = False
    eng.persist_runtime(db)

    # Rebuild: the halt and the drawdown baseline both survive.
    eng2 = _engine(conn)
    eng2.restore_state(db)
    assert eng2._killswitch_tripped is True
    assert eng2.running is False
    assert eng2._peak_equity == pytest.approx(1_000.0)  # baseline NOT reset to 0

    # Even the fresh-engine default must refuse to resume a tripped kill-switch.
    if not eng2._runtime_restored and not eng2._killswitch_tripped:
        eng2.running = True
    assert eng2.running is False

    # Only an explicit human re-arm (start -> reset_killswitch) clears it, durably.
    eng2.reset_killswitch(db)
    eng2.running = True
    eng2.persist_runtime(db)

    eng3 = _engine(conn)
    eng3.restore_state(db)
    assert eng3._killswitch_tripped is False
    assert eng3.running is True
    assert eng3._peak_equity == 0.0  # reseeded fresh from the re-arm


# ---- end-to-end: a real drawdown trip writes durable state ----------


def test_real_drawdown_trip_persists_halt_and_blocks_resume(db):
    conn = _Conn(price=100.0)
    eng = _engine(conn, paper_starting_balance=1_000.0, max_drawdown_pct=20.0)
    ok, msg, _ = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=5.0,
        stop_loss=None, take_profit=None, source="manual",
    )
    assert ok, msg
    # Price collapses well past the 20% drawdown limit -> kill-switch trips.
    conn.set_price(20.0)
    assert eng._update_drawdown(db) is True
    assert eng.running is False

    # The trip was persisted synchronously: a rebuild honours the halt.
    stored = load_engine_runtime(db, None)
    assert stored["killswitch_tripped"] is True
    assert stored["running"] is False

    eng2 = _engine(conn, paper_starting_balance=1_000.0, max_drawdown_pct=20.0)
    eng2.restore_state(db)
    assert eng2._killswitch_tripped is True
    assert eng2.running is False


# ---- the growing equity peak is persisted (throttled) ---------------


def test_equity_peak_is_persisted_as_it_grows(db):
    conn = _Conn(price=100.0)
    eng = _engine(conn, paper_starting_balance=10_000.0, max_drawdown_pct=20.0)
    # First drawdown check seeds + persists the peak from current equity.
    eng._update_drawdown(db)
    stored = load_engine_runtime(db, None)
    assert stored["peak_equity"] == pytest.approx(10_000.0)
    # A rebuild restores that baseline rather than starting from zero.
    eng2 = _engine(conn, paper_starting_balance=10_000.0, max_drawdown_pct=20.0)
    eng2.restore_state(db)
    assert eng2._peak_equity == pytest.approx(10_000.0)


# ---- a paper<->live flip re-arms the baseline (never a false trip) --


def test_mode_flip_rearms_drawdown_baseline(db):
    """Flipping paper->live must NOT carry the paper equity peak into live.

    The peak is tracked per running mode; live measures a real balance, paper a
    simulated wallet. If a ~10k paper peak leaked into a small live balance, the
    next _update_drawdown tick would see a ~100% drawdown and falsely trip the
    kill-switch, blocking the user's real trading. Flipping the mode re-arms the
    baseline (like tapping Start) and persists it.
    """
    conn = _Conn()
    eng = _engine(conn, trading_mode="paper")
    eng._peak_equity = 10_000.0          # a peak accumulated on the paper wallet
    eng._last_persisted_peak = 10_000.0
    eng.persist_runtime(db)

    eng.settings.trading_mode = "live"   # operator flips to live (small real acct)
    eng.apply_settings(eng.settings, db)

    assert eng._active_mode == "live"
    assert eng._peak_equity == 0.0       # re-armed: no stale paper peak
    assert eng._killswitch_tripped is False
    # The re-arm is durable: a rebuild restores the fresh (zero) baseline, not 10k.
    stored = load_engine_runtime(db, None)
    assert stored["peak_equity"] == pytest.approx(0.0)


def test_non_mode_settings_change_keeps_drawdown_peak(db):
    """A settings change that does NOT touch trading_mode must leave the drawdown
    baseline intact — only a real mode flip re-arms it."""
    conn = _Conn()
    eng = _engine(conn, trading_mode="paper")
    eng._peak_equity = 10_000.0

    eng.settings.risk_per_trade_pct = 2.0   # some unrelated tweak
    eng.apply_settings(eng.settings, db)

    assert eng._active_mode == "paper"
    assert eng._peak_equity == pytest.approx(10_000.0)  # untouched
