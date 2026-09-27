"""Live order-lifecycle safety (audit H5 + H6).

H5 — a live SL/TP close MUST notice when the protective EXCHANGE-side stop has
already fired: the base asset is gone, so firing another market order just loops
on -2010 (insufficient balance) and wedges the trade open forever. We instead
book the close at the stop's REAL fill price.

H6 — resting limit/DCA fills MUST NOT bypass the capital-preservation halts.
A tripped max-drawdown kill-switch cancels resting entries (they must not fill
into the very drawdown that halted us); a daily-loss breach skips the paper fill
but leaves the order resting to be re-evaluated once the (auto-resetting) breaker
clears. A LIVE order the exchange has ALREADY filled is booked as-is either way —
we never pretend reality didn't happen and desync our book from the account.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.database import Base
from app.engine import TradingEngine
from app.models import Trade, TradeStatus


class FakeConnector:
    """Fake connector with a mutable price, a scriptable ``fetch_order`` and call
    recording, so a test can assert exactly which exchange actions fired."""

    has_credentials = True

    def __init__(self, price: float = 100.0):
        self._price = price
        self._order = None  # what fetch_order returns
        self.market_orders: list[tuple] = []
        self.canceled: list[tuple] = []
        self.stop_orders: list[tuple] = []

    @property
    def connected(self) -> bool:
        return True

    def reload(self, settings):
        pass

    def set_price(self, price):
        self._price = price

    def set_order(self, order):
        self._order = order

    def fetch_price(self, symbol):
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

    def create_market_order(self, symbol, side, amount):
        self.market_orders.append((symbol, side, amount))
        return {"id": "m1", "average": self._price}

    def create_limit_order(self, symbol, side, amount, price):
        return {"id": "l1"}

    def create_stop_loss_order(self, symbol, side, amount, stop_price):
        self.stop_orders.append((symbol, side, amount, stop_price))
        return {"id": "s1"}

    def cancel_order(self, order_id, symbol):
        self.canceled.append((order_id, symbol))

    def fetch_order(self, order_id, symbol):
        return self._order


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
        daily_loss_limit_pct=50.0,
        default_stop_loss_pct=2.0,
        default_take_profit_pct=4.0,
        paper_starting_balance=10_000.0,
        min_signal_confidence=0.1,
        max_drawdown_pct=20.0,
        trailing_stop_pct=0.0,       # keep the monitor from moving the stop
        profit_lock_enabled=False,   # ...or ratcheting it into profit
    )
    base.update(over)
    return TradingEngine(Settings(**base), connector, user_id=None)


def _open_live_trade(db, **over) -> Trade:
    fields = dict(
        symbol="BTC/USDT", side="buy", amount=1.0, entry_price=100.0,
        stop_loss=95.0, take_profit=110.0, status=TradeStatus.open.value,
        mode="live",
    )
    fields.update(over)
    t = Trade(**fields)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ---- H5: a fired exchange-side stop is booked, never re-fired -------


def test_close_books_at_fired_exchange_stop_and_skips_market_order(db):
    conn = FakeConnector(price=94.0)
    eng = _engine(conn, trading_mode="live")
    t = _open_live_trade(db, stop_order_id="s1", stop_loss=95.0)
    # The exchange reports the protective stop ALREADY filled at 94.5.
    conn.set_order({"status": "closed", "filled": 1.0, "average": 94.5})
    closed = eng.check_open_positions(db)
    assert len(closed) == 1
    db.refresh(t)
    assert t.status == TradeStatus.closed.value
    assert t.exit_price == pytest.approx(94.5)          # booked at the REAL fill
    assert t.pnl == pytest.approx((94.5 - 100.0) * 1.0)
    assert t.stop_order_id is None
    assert "exchange stop already filled" in (t.note or "")
    # Crucially, NO fresh market order fired — that is the -2010 loop.
    assert conn.market_orders == []


def test_fired_stop_does_not_loop_on_repeated_monitor_passes(db):
    conn = FakeConnector(price=94.0)
    eng = _engine(conn, trading_mode="live")
    _open_live_trade(db, stop_order_id="s1", stop_loss=95.0)
    conn.set_order({"status": "closed", "filled": 1.0, "average": 94.5})
    eng.check_open_positions(db)
    eng.check_open_positions(db)   # a wedged trade would fire an order here
    eng.check_open_positions(db)
    assert conn.market_orders == []
    assert (
        db.query(Trade).filter(Trade.status == TradeStatus.open.value).count() == 0
    )


def test_close_market_closes_when_stop_still_resting(db):
    conn = FakeConnector(price=94.0)
    eng = _engine(conn, trading_mode="live")
    t = _open_live_trade(db, stop_order_id="s1", stop_loss=95.0)
    # Stop has NOT fired (still resting) -> cancel it, then real market close.
    conn.set_order({"status": "open", "filled": 0.0})
    closed = eng.check_open_positions(db)
    assert len(closed) == 1
    db.refresh(t)
    assert t.status == TradeStatus.closed.value
    assert ("s1", "BTC/USDT") in conn.canceled       # resting stop canceled first
    assert len(conn.market_orders) == 1              # then a real market close
    assert t.exit_price == pytest.approx(94.0)       # booked at the market fill
    assert "exchange stop already filled" not in (t.note or "")


# ---- H6: resting fills respect the capital-preservation halts -------


def test_kill_switch_cancels_resting_paper_order(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)  # paper
    ok, msg, trade = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=1.0,
        stop_loss=None, take_profit=None, source="manual", limit_price=90.0,
    )
    assert ok, msg
    assert trade.status == TradeStatus.pending.value
    assert eng.paper_balance == pytest.approx(10_000 - 90.0)
    # The drawdown kill-switch trips before the resting order fills.
    eng._killswitch_tripped = True
    conn.set_price(89.0)  # price crosses the limit -> WOULD fill if not halted
    filled = eng.check_pending_orders(db)
    assert filled == []
    db.refresh(trade)
    assert trade.status == TradeStatus.canceled.value    # canceled, NOT filled
    assert eng.paper_balance == pytest.approx(10_000.0)  # reservation returned


def test_daily_loss_skips_paper_fill_but_leaves_order_resting(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, paper_starting_balance=1_000.0, daily_loss_limit_pct=5.0)
    _, _, trade = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=1.0,
        stop_loss=None, take_profit=None, source="manual", limit_price=90.0,
    )
    assert trade.status == TradeStatus.pending.value
    # A realized loss today breaches the 5%-of-equity daily-loss breaker.
    loss = Trade(
        symbol="ETH/USDT", side="buy", amount=1.0, entry_price=100.0,
        exit_price=40.0, pnl=-60.0, status=TradeStatus.closed.value,
        mode="paper", closed_at=dt.datetime.now(dt.timezone.utc),
    )
    db.add(loss)
    db.commit()
    assert eng._daily_loss_hit(db) is True
    conn.set_price(89.0)  # crosses the limit
    assert eng.check_pending_orders(db) == []            # not filled today
    db.refresh(trade)
    assert trade.status == TradeStatus.pending.value     # LEFT resting, not canceled
    # Breaker clears (loss reversed) -> the SAME resting order now fills.
    loss.pnl = 0.0
    db.commit()
    assert eng._daily_loss_hit(db) is False
    filled = eng.check_pending_orders(db)
    assert len(filled) == 1
    db.refresh(trade)
    assert trade.status == TradeStatus.open.value


def test_kill_switch_cancels_resting_live_order_on_exchange(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, trading_mode="live")
    t = Trade(
        symbol="BTC/USDT", side="buy", amount=1.0, entry_price=0.0,
        limit_price=90.0, order_type="limit", exchange_order_id="l1",
        status=TradeStatus.pending.value, mode="live",
    )
    db.add(t)
    db.commit()
    eng._killswitch_tripped = True
    conn.set_order({"status": "open", "filled": 0.0})   # still resting on venue
    filled = eng.check_pending_orders(db)
    assert filled == []
    db.refresh(t)
    assert t.status == TradeStatus.canceled.value
    assert ("l1", "BTC/USDT") in conn.canceled          # canceled ON the exchange


def test_kill_switch_does_not_unfill_already_filled_live_order(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, trading_mode="live")
    t = Trade(
        symbol="BTC/USDT", side="buy", amount=1.0, entry_price=0.0,
        limit_price=100.0, order_type="limit", exchange_order_id="l1",
        status=TradeStatus.pending.value, mode="live",
    )
    db.add(t)
    db.commit()
    eng._killswitch_tripped = True
    # The exchange already FILLED it before the trip -> book reality; never
    # "cancel" a position we actually hold (that would desync the book).
    conn.set_order({"status": "closed", "filled": 1.0, "average": 100.0})
    filled = eng.check_pending_orders(db)
    assert len(filled) == 1
    db.refresh(t)
    assert t.status == TradeStatus.open.value           # booked, not canceled
    assert ("l1", "BTC/USDT") not in conn.canceled

