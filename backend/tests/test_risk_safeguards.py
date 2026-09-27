"""Tests for the added risk safeguards (capital-preservation controls):

- max-drawdown KILL-SWITCH halts new entries (open positions keep their stops)
- LIVE spread/liquidity guard on market entries (never blocks exits)
- anti-whipsaw re-entry cooldown after a losing exit
- consecutive-loss circuit breaker
- ATR-floored autonomous stop
- a STOPPED bot still manages open positions (SL/TP + drawdown)
- a price-feed outage during monitoring is SURFACED, not silently skipped

All fake-connector based — no network or credentials required.
"""
from __future__ import annotations

import datetime as dt
import types

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.database import Base
from app.engine import TradingEngine, _utcnow
from app.models import Trade, TradeStatus


def _uptrend(n: int = 200) -> list[list[float]]:
    # timestamp, open, high, low, close, volume — smooth rising series -> buy.
    return [[i, 100 + i * 0.8, (100 + i * 0.8) * 1.001,
             (100 + i * 0.8) * 0.999, 100 + i * 0.8, 1.0] for i in range(n)]


class FakeConnector:
    """Paper+live capable fake: fixed price, configurable spread, candles."""

    def __init__(self, price=100.0, has_credentials=False, spread=0.0,
                 ohlcv=None, price_raises=False, balance=10_000.0):
        self._price = price
        self.has_credentials = has_credentials
        self._spread = spread
        self._ohlcv = ohlcv or {}
        self._price_raises = price_raises
        self._balance = balance
        self.market_orders: list[tuple] = []

    @property
    def connected(self) -> bool:
        return True

    def reload(self, settings):
        pass

    def set_price(self, price):
        self._price = price

    def set_price_raises(self, flag: bool):
        self._price_raises = flag

    def fetch_price(self, symbol):
        if self._price_raises:
            raise RuntimeError("price feed unreachable")
        return self._price

    def fetch_ohlcv(self, symbol, timeframe, limit):
        return self._ohlcv.get(timeframe, [])

    def fetch_position_amounts(self):
        return {}

    def fetch_balance(self, quote="USDT"):
        return self._balance

    def normalize_amount(self, symbol, amount, price):
        return amount, None

    def spread_pct(self, symbol):
        return self._spread

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
    session._sa_engine_ref = engine  # keep the engine (and :memory: db) alive
    try:
        yield session
    finally:
        session.close()


def _engine(connector, **over) -> TradingEngine:
    base = dict(
        trading_mode="paper",
        max_open_positions=5,
        risk_per_trade_pct=1.0,
        daily_loss_limit_pct=50.0,
        default_stop_loss_pct=2.0,
        default_take_profit_pct=4.0,
        paper_starting_balance=10_000.0,
        min_signal_confidence=0.1,
        # Safeguards default-on; individual tests override as needed.
        max_drawdown_pct=20.0,
        max_spread_pct=1.0,
        reentry_cooldown_minutes=15.0,
        max_consecutive_losses=3,
        atr_stop_mult=1.5,
    )
    base.update(over)
    return TradingEngine(Settings(**base), connector)


# ---- max-drawdown kill-switch ---------------------------------------

def test_killswitch_trips_on_drawdown_and_blocks_new_entries(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, paper_starting_balance=1_000.0, max_drawdown_pct=20.0)
    # Open a long: 5 @ 100 (notional 500). Cash 500 + position 500 = equity 1000.
    ok, msg, _ = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=5.0,
        stop_loss=None, take_profit=None, source="manual",
    )
    assert ok, msg
    assert eng._peak_equity == pytest.approx(1_000.0)
    # Price collapses to 20: position now worth 100, equity = 500 + 100 = 600
    # -> 40% below the 1000 peak, well past the 20% limit.
    conn.set_price(20.0)
    assert eng._update_drawdown(db) is True
    assert eng._killswitch_tripped is True
    assert eng.running is False  # autonomous trading halted
    # A NEW entry is now refused (open positions keep their stops).
    ok2, msg2, _ = eng.execute_signal(
        db, action="buy", symbol="ETH/USDT", amount=1.0,
        stop_loss=None, take_profit=None, source="manual",
    )
    assert ok2 is False
    assert "kill-switch" in msg2.lower()


def test_killswitch_never_blocks_an_exit(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, paper_starting_balance=1_000.0, max_drawdown_pct=20.0)
    eng.execute_signal(db, action="buy", symbol="BTC/USDT", amount=5.0,
                       stop_loss=None, take_profit=None, source="manual")
    conn.set_price(20.0)
    eng._update_drawdown(db)
    assert eng._killswitch_tripped is True
    # Closing the position must still work even with the kill-switch tripped.
    ok, msg, _ = eng.execute_signal(
        db, action="close", symbol="BTC/USDT", amount=None,
        stop_loss=None, take_profit=None, source="manual",
    )
    assert ok, msg


def test_reset_killswitch_reseeds_peak(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, paper_starting_balance=1_000.0, max_drawdown_pct=20.0)
    eng._peak_equity = 5_000.0
    eng._killswitch_tripped = True
    eng.reset_killswitch()
    assert eng._killswitch_tripped is False
    assert eng._peak_equity == 0.0


# ---- live spread / liquidity guard ----------------------------------

def test_live_entry_blocked_when_spread_too_wide(db):
    # Spread 2% > max 1% -> the live market ENTRY is refused before ordering.
    conn = FakeConnector(price=100.0, has_credentials=True, spread=2.0)
    eng = _engine(conn, trading_mode="live", max_spread_pct=1.0)
    ok, msg, _ = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=1.0,
        stop_loss=None, take_profit=None, source="manual",
    )
    assert ok is False
    assert "spread" in msg.lower()
    assert conn.market_orders == []  # no order was attempted


def test_live_entry_allowed_when_spread_tight(db):
    conn = FakeConnector(price=100.0, has_credentials=True, spread=0.05)
    eng = _engine(conn, trading_mode="live", max_spread_pct=1.0)
    ok, msg, _ = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=1.0,
        stop_loss=None, take_profit=None, source="manual",
    )
    assert ok, msg
    assert len(conn.market_orders) == 1


def test_spread_guard_does_not_block_when_unmeasurable(db):
    # spread_pct returns None (no bid/ask) -> we must NOT fabricate a refusal.
    conn = FakeConnector(price=100.0, has_credentials=True, spread=None)
    eng = _engine(conn, trading_mode="live", max_spread_pct=1.0)
    ok, msg, _ = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=1.0,
        stop_loss=None, take_profit=None, source="manual",
    )
    assert ok, msg
    assert len(conn.market_orders) == 1


# ---- ATR-floored autonomous stop ------------------------------------

def test_atr_floored_stop_widens_when_atr_large(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, default_stop_loss_pct=2.0, atr_stop_mult=1.5)
    # ATR 5 -> ATR stop = 100 - 1.5*5 = 92.5, wider than the 98.0 fixed stop.
    analysis = types.SimpleNamespace(price=100.0, atr=5.0)
    assert eng._atr_floored_stop(analysis) == pytest.approx(92.5)


def test_atr_floored_stop_uses_pct_when_atr_small_or_off(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, default_stop_loss_pct=2.0, atr_stop_mult=1.5)
    # Tiny ATR -> ATR stop above the fixed stop -> the fixed 98.0 stop wins.
    assert eng._atr_floored_stop(
        types.SimpleNamespace(price=100.0, atr=0.1)
    ) == pytest.approx(98.0)
    # Disabled (mult 0) -> fixed stop.
    eng2 = _engine(conn, default_stop_loss_pct=2.0, atr_stop_mult=0.0)
    assert eng2._atr_floored_stop(
        types.SimpleNamespace(price=100.0, atr=5.0)
    ) == pytest.approx(98.0)


# ---- shared helpers for the autonomous-control tests ----------------

def _buy_analysis(price: float = 100.0, atr: float = 1.0):
    """Minimal analysis object accepted by _decide_and_act (a buy verdict)."""
    return types.SimpleNamespace(
        verdict="buy", confidence=0.9, summary="unit-test buy",
        price=price, atr=atr,
    )


def _add_closed(db, symbol: str, pnl: float, closed_at) -> Trade:
    """Insert a CLOSED trade with a given realized pnl and close time."""
    t = Trade(
        symbol=symbol, side="buy", amount=1.0, entry_price=100.0,
        exit_price=100.0 + pnl, pnl=pnl,
        status=TradeStatus.closed.value, closed_at=closed_at,
    )
    db.add(t)
    db.commit()
    return t


# ---- anti-whipsaw re-entry cooldown ---------------------------------

def test_reentry_cooldown_starts_after_loss_and_expires(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, reentry_cooldown_minutes=15.0)
    assert eng._reentry_cooldown_remaining("BTC/USDT") == 0.0  # no prior loss
    eng._last_loss_exit["BTC/USDT"] = _utcnow()
    assert eng._reentry_cooldown_remaining("BTC/USDT") > 0
    # A loss older than the window no longer blocks re-entry.
    eng._last_loss_exit["BTC/USDT"] = _utcnow() - dt.timedelta(minutes=20)
    assert eng._reentry_cooldown_remaining("BTC/USDT") == 0.0


def test_reentry_cooldown_blocks_autonomous_buy(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, reentry_cooldown_minutes=15.0)
    eng._last_loss_exit["BTC/USDT"] = _utcnow()  # just took a loss here
    ok, msg = eng._decide_and_act(db, "BTC/USDT", "1h", _buy_analysis())
    assert ok is False
    assert "cooldown" in msg.lower()


# ---- consecutive-loss circuit breaker -------------------------------

def test_consecutive_losses_counts_and_win_breaks_streak(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    base = _utcnow()
    _add_closed(db, "BTC/USDT", -5.0, base - dt.timedelta(minutes=3))
    _add_closed(db, "BTC/USDT", -5.0, base - dt.timedelta(minutes=2))
    _add_closed(db, "BTC/USDT", -5.0, base - dt.timedelta(minutes=1))
    assert eng._consecutive_losses(db) == 3
    # A newer WIN resets the streak (counted from the most recent close).
    _add_closed(db, "BTC/USDT", 8.0, base)
    assert eng._consecutive_losses(db) == 0


def test_consecutive_loss_breaker_blocks_autonomous_buy(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, max_consecutive_losses=2)
    base = _utcnow()
    _add_closed(db, "ETH/USDT", -5.0, base - dt.timedelta(minutes=2))
    _add_closed(db, "ETH/USDT", -5.0, base - dt.timedelta(minutes=1))
    assert eng._consecutive_losses(db) == 2
    ok, msg = eng._decide_and_act(db, "SOL/USDT", "1h", _buy_analysis())
    assert ok is False
    assert "circuit breaker" in msg.lower()


# ---- unattended (webhook) entries get the same discretionary gates --
# The autonomous loop already applies these; the audit found the webhook path
# bypassed them. execute_signal now enforces the re-entry cooldown + streak
# breaker (and, for a webhook, a FRESH bear-regime pause) on UNATTENDED sources
# ("auto"/"tradingview"), while a deliberate MANUAL order stays exempt — it is a
# human acting by hand and remains bounded by the hard risk gates.

def test_webhook_buy_respects_reentry_cooldown(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, reentry_cooldown_minutes=15.0)
    eng._last_loss_exit["BTC/USDT"] = _utcnow()  # just took a loss here
    ok, msg, trade = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=1.0,
        stop_loss=None, take_profit=None, source="tradingview",
    )
    assert ok is False
    assert trade is None
    assert "cooldown" in msg.lower()


def test_webhook_buy_respects_consecutive_loss_breaker(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, max_consecutive_losses=2)
    base = _utcnow()
    _add_closed(db, "ETH/USDT", -5.0, base - dt.timedelta(minutes=2))
    _add_closed(db, "ETH/USDT", -5.0, base - dt.timedelta(minutes=1))
    ok, msg, trade = eng.execute_signal(
        db, action="buy", symbol="SOL/USDT", amount=1.0,
        stop_loss=None, take_profit=None, source="tradingview",
    )
    assert ok is False
    assert trade is None
    assert "circuit breaker" in msg.lower()
def test_webhook_buy_paused_in_fresh_bear_regime(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, auto_pause_in_bear=True)
    eng._last_regime["BTC/USDT"] = {
        "regime": "bear", "detail": "price below a falling EMA200",
        "protective_hold": False, "entries_paused": True,
        "verdict": "sell", "at": _utcnow().isoformat(),
    }
    ok, msg, trade = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=1.0,
        stop_loss=None, take_profit=None, source="tradingview",
    )
    assert ok is False
    assert trade is None
    assert "regime" in msg.lower()


def test_webhook_buy_ignores_stale_bear_regime(db):
    # A regime read older than the freshness window means the bot isn't watching
    # this symbol now — never block a webhook long on a stale reading (that would
    # be acting on data we no longer have).
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, auto_pause_in_bear=True)
    eng._last_regime["BTC/USDT"] = {
        "regime": "bear", "detail": "old read",
        "protective_hold": False, "entries_paused": True,
        "verdict": "sell",
        "at": (_utcnow() - dt.timedelta(minutes=20)).isoformat(),
    }
    ok, msg, trade = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=1.0,
        stop_loss=None, take_profit=None, source="tradingview",
    )
    assert ok, msg
    assert trade is not None
    assert trade.status == TradeStatus.open.value
def test_manual_buy_is_exempt_from_discretionary_gates(db):
    # A deliberate human order is NOT blocked by the cooldown / streak / regime
    # pauses (it stays bounded by the hard risk gates). Stack ALL three against
    # it and confirm the manual buy still opens.
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, reentry_cooldown_minutes=15.0,
                  max_consecutive_losses=2, auto_pause_in_bear=True)
    base = _utcnow()
    _add_closed(db, "BTC/USDT", -5.0, base - dt.timedelta(minutes=2))
    _add_closed(db, "BTC/USDT", -5.0, base - dt.timedelta(minutes=1))
    eng._last_loss_exit["BTC/USDT"] = _utcnow()
    eng._last_regime["BTC/USDT"] = {
        "regime": "bear", "detail": "", "protective_hold": False,
        "entries_paused": True, "verdict": "sell", "at": _utcnow().isoformat(),
    }
    ok, msg, trade = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=1.0,
        stop_loss=None, take_profit=None, source="manual",
    )
    assert ok, msg
    assert trade is not None
    assert trade.status == TradeStatus.open.value

# ---- stopped bot still manages open positions (#3) ------------------

def test_stopped_bot_still_runs_stop_loss(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, default_stop_loss_pct=2.0)
    ok, msg, trade = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=1.0,
        stop_loss=None, take_profit=None, source="manual",
    )
    assert ok, msg
    assert trade.stop_loss == pytest.approx(98.0)
    eng.running = False  # operator stopped the bot
    conn.set_price(97.0)  # below the stop
    closed = eng.check_open_positions(db)
    assert len(closed) == 1
    assert closed[0][1] == "stop-loss"
    db.refresh(trade)
    assert trade.status == TradeStatus.closed.value
    assert eng.running is False  # managing positions never restarts the bot


# ---- price-feed outage is surfaced, not silently skipped (#8) -------

def test_price_feed_outage_is_surfaced_and_recovers(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, default_stop_loss_pct=2.0)
    ok, msg, trade = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=1.0,
        stop_loss=None, take_profit=None, source="manual",
    )
    assert ok, msg
    # Feed goes down: must NOT be silently treated as "price == entry, no trigger".
    conn.set_price_raises(True)
    eng.check_open_positions(db)
    assert "BTC/USDT" in eng._monitor_degraded
    db.refresh(trade)
    assert trade.status == TradeStatus.open.value  # still open, protection paused
    # Feed recovers: the degraded flag clears and the position is managed again.
    conn.set_price_raises(False)
    eng.check_open_positions(db)
    assert "BTC/USDT" not in eng._monitor_degraded
    db.refresh(trade)
    assert trade.status == TradeStatus.open.value


