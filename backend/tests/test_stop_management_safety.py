"""Stop-management + resting-fill safety (audit M4 + M1).

M4 — trailing-stop and profit-lock must NOT leave orphaned or missing exchange
stops. The stop move now runs under the engine lock after re-reading the trade,
so a close racing on another session can't make us place a fresh exchange stop
for a position that no longer exists; and the cancel->replace is crash-proof —
a failed re-place drops to the in-process stop instead of pretending the venue
still holds one, a failed cancel keeps the old stop rather than stacking a
second the venue would reject for the now-reserved base.

M1 — a PAPER resting limit must fill only when the market REALLY trades through
it. Fetching the price with ``fallback=limit`` meant a feed outage returned the
limit itself and "filled" every resting order at its own price on dead data;
now we fetch without a fallback and skip the tick when the feed is down.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.database import Base
from app.engine import TradingEngine
from app.models import Trade, TradeStatus

# <<APPEND-1>>

class FakeConnector:
    """Scriptable connector: prices can be made to fail, and stop create/cancel
    calls are recorded (and can be made to return None or raise) so a test can
    assert exactly what the engine did on the exchange side."""

    has_credentials = True

    def __init__(self, price: float = 100.0):
        self._price = price
        self._price_raises = False
        self._stop_result: dict | None = {"id": "s-new"}
        self._stop_raises = False
        self._cancel_raises = False
        self.stop_orders: list[tuple] = []
        self.canceled: list[tuple] = []

    @property
    def connected(self) -> bool:
        return True

    def reload(self, settings):
        pass

    def set_price(self, price):
        self._price = price

    def fail_price(self, flag: bool = True):
        self._price_raises = flag

    def fetch_price(self, symbol):
        if self._price_raises:
            raise RuntimeError("price feed unreachable")
        return self._price

    def fetch_ohlcv(self, symbol, timeframe, limit):
        return []

    def fetch_position_amounts(self):
        return {}

    def fetch_balance(self, quote="USDT"):
        return 10_000.0

    def normalize_amount(self, symbol, amount, price):
        return amount, None

    def spread_pct(self, symbol):
        return 0.0
# <<APPEND-2>>

    def create_market_order(self, symbol, side, amount):
        return {"id": "m1", "average": self._price, "filled": amount}

    def create_limit_order(self, symbol, side, amount, price):
        return {"id": "l1"}

    def create_stop_loss_order(self, symbol, side, amount, stop_price):
        self.stop_orders.append((symbol, side, amount, stop_price))
        if self._stop_raises:
            raise RuntimeError("stop rejected by venue")
        return self._stop_result

    def cancel_order(self, order_id, symbol):
        if self._cancel_raises:
            raise RuntimeError("cancel failed")
        self.canceled.append((order_id, symbol))

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
    try:
        yield session
    finally:
        session.close()
# <<APPEND-3>>

def _engine(conn, **over) -> TradingEngine:
    base = dict(
        trading_mode="live",
        max_open_positions=5,
        risk_per_trade_pct=1.0,
        daily_loss_limit_pct=50.0,
        default_stop_loss_pct=2.0,
        default_take_profit_pct=4.0,
        paper_starting_balance=10_000.0,
        min_signal_confidence=0.1,
        max_drawdown_pct=90.0,
        max_position_pct=0.0,
        trailing_stop_pct=0.0,
        profit_lock_enabled=False,
        paper_taker_fee_pct=0.0,
    )
    base.update(over)
    return TradingEngine(Settings(**base), conn, user_id=None)


def _open_trade(db, **over) -> Trade:
    t = Trade(
        symbol="BTC/USDT", side="buy", amount=1.0, entry_price=100.0,
        stop_loss=90.0, status=TradeStatus.open.value, mode="live",
        stop_order_id="s0", order_type="market",
    )
    for k, v in over.items():
        setattr(t, k, v)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _pending_buy(db, limit: float = 100.0) -> Trade:
    t = Trade(
        symbol="BTC/USDT", side="buy", amount=1.0, entry_price=limit,
        limit_price=limit, order_type="limit",
        status=TradeStatus.pending.value, mode="paper",
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t
# <<APPEND-4>>

# ---- M4: trailing stop moves the exchange stop safely --------------


def test_trail_raises_and_moves_exchange_stop(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, trailing_stop_pct=5.0)
    t = _open_trade(db, stop_loss=90.0, stop_order_id="s0")
    eng._maybe_trail_stop(db, t, 120.0)  # candidate = 120 * 0.95 = 114
    db.refresh(t)
    assert t.stop_loss == pytest.approx(114.0)
    assert conn.canceled == [("s0", "BTC/USDT")]
    assert conn.stop_orders[-1] == ("BTC/USDT", "sell", 1.0, 114.0)
    assert t.stop_order_id == "s-new"


def test_trail_skips_when_trade_closed_underneath(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, trailing_stop_pct=5.0)
    t = _open_trade(db, stop_loss=90.0, stop_order_id="s0")
    t.status = TradeStatus.closed.value  # a concurrent close, committed
    db.commit()
    eng._maybe_trail_stop(db, t, 120.0)
    db.refresh(t)
    assert conn.stop_orders == []   # no orphaned resting stop placed
    assert conn.canceled == []
    assert t.stop_order_id == "s0"


def test_trail_replace_failure_degrades_to_inprocess(db):
    conn = FakeConnector(price=100.0)
    conn._stop_result = None  # venue can't place the replacement
    eng = _engine(conn, trailing_stop_pct=5.0)
    t = _open_trade(db, stop_loss=90.0, stop_order_id="s0")
    eng._maybe_trail_stop(db, t, 120.0)
    db.refresh(t)
    assert t.stop_loss == pytest.approx(114.0)      # in-process level advanced
    assert conn.canceled == [("s0", "BTC/USDT")]    # old stop cancelled
    assert t.stop_order_id is None                  # exchange stop honestly gone


def test_trail_cancel_failure_keeps_old_stop(db):
    conn = FakeConnector(price=100.0)
    conn._cancel_raises = True
    eng = _engine(conn, trailing_stop_pct=5.0)
    t = _open_trade(db, stop_loss=90.0, stop_order_id="s0")
    eng._maybe_trail_stop(db, t, 120.0)
    db.refresh(t)
    assert conn.stop_orders == []      # don't stack a second stop
    assert t.stop_order_id == "s0"     # keep the (still-live) old stop
    assert t.stop_loss == pytest.approx(114.0)


def test_trail_paper_updates_stop_without_exchange_calls(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, trading_mode="paper", trailing_stop_pct=5.0)
    t = _open_trade(db, stop_loss=90.0, stop_order_id=None, mode="paper")
    eng._maybe_trail_stop(db, t, 120.0)
    db.refresh(t)
    assert t.stop_loss == pytest.approx(114.0)
    assert conn.stop_orders == []
    assert conn.canceled == []
# ---- M4: profit-lock moves the exchange stop safely ----------------


def test_profit_lock_moves_stop(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, profit_lock_enabled=True,
                  profit_lock_trigger_pct=1.0, profit_lock_floor_pct=0.5)
    t = _open_trade(db, entry_price=100.0, stop_loss=90.0, stop_order_id="s0")
    eng._maybe_lock_profit(db, t, 102.0)  # +2% -> lock floor 0.5% -> 100.5
    db.refresh(t)
    assert t.stop_loss == pytest.approx(100.5)
    assert conn.canceled == [("s0", "BTC/USDT")]
    assert conn.stop_orders[-1] == ("BTC/USDT", "sell", 1.0, pytest.approx(100.5))
    assert t.stop_order_id == "s-new"


def test_profit_lock_skips_when_closed_underneath(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, profit_lock_enabled=True,
                  profit_lock_trigger_pct=1.0, profit_lock_floor_pct=0.5)
    t = _open_trade(db, entry_price=100.0, stop_loss=90.0, stop_order_id="s0")
    t.status = TradeStatus.closed.value
    db.commit()
    eng._maybe_lock_profit(db, t, 102.0)
    db.refresh(t)
    assert conn.stop_orders == []
    assert t.stop_order_id == "s0"


# ---- M1: paper resting limit never fills on a feed outage ----------


def test_paper_resting_limit_does_not_fill_on_feed_outage(db):
    conn = FakeConnector(price=100.0)
    conn.fail_price(True)
    eng = _engine(conn, trading_mode="paper")
    t = _pending_buy(db, limit=100.0)
    filled = eng.check_pending_orders(db)
    db.refresh(t)
    assert filled == []
    assert t.status == TradeStatus.pending.value


def test_paper_resting_limit_fills_on_real_cross(db):
    conn = FakeConnector(price=99.0)
    eng = _engine(conn, trading_mode="paper")
    t = _pending_buy(db, limit=100.0)
    filled = eng.check_pending_orders(db)
    db.refresh(t)
    assert len(filled) == 1
    assert t.status == TradeStatus.open.value
    assert t.entry_price == pytest.approx(100.0)


# ---- live-open safety: honest stop + honest balance -----------------


def test_live_buy_warns_when_exchange_stop_cannot_be_placed(db):
    # A live BUY whose exchange-side stop can't rest must STILL open (the in-process
    # monitor guards it while the bot runs), but the bot must loudly flag the
    # missing hard stop rather than leave the operator with silent false safety.
    conn = FakeConnector(price=100.0)
    conn._stop_result = None  # venue won't rest the protective stop
    eng = _engine(conn, trading_mode="live", default_stop_loss_pct=2.0)
    events: list[tuple] = []
    eng._emit = lambda kind, payload: events.append((kind, payload))
    ok, msg, trade = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=1.0,
        stop_loss=98.0, take_profit=None, source="manual",
    )
    assert ok and trade is not None
    assert trade.stop_order_id is None                # honestly: no exchange stop
    assert trade.stop_loss == pytest.approx(98.0)     # in-process stop still armed
    kinds = [k for k, _ in events]
    assert "stop_unprotected" in kinds
    payload = next(p for k, p in events if k == "stop_unprotected")
    assert payload["symbol"] == "BTC/USDT"
    assert payload["stop"] == pytest.approx(98.0)


def test_live_open_refused_clearly_when_balance_unreadable(db):
    # If the live balance can't be read, _equity falls back to 0.0 — which must
    # NOT surface as the misleading "position size is zero". The open is refused
    # with an explicit "balance unavailable" message, and no order is attempted.
    conn = FakeConnector(price=100.0)
    conn.fetch_balance = lambda quote="USDT": None  # feed/exchange can't answer
    eng = _engine(conn, trading_mode="live", default_stop_loss_pct=2.0)
    ok, msg, trade = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=1.0,
        stop_loss=98.0, take_profit=None, source="manual",
    )
    assert not ok and trade is None
    assert "balance unavailable" in msg.lower()
    assert conn.stop_orders == []  # never got as far as placing anything




