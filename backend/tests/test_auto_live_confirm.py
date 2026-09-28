"""Confirm-before-LIVE gate for autonomous entries.

The user wants a bot that "can trade real market but it will confirm when given
permission" — watch the market all night on its own, but ask before spending
REAL money on an entry IT decided. So when ``auto_live_confirm`` is on:

  • a LIVE, source="auto" BUY is QUEUED (an AutoConfirmation row) and the
    operator is pinged — it is NOT placed on the exchange;
  • approving re-runs the order fresh (re-priced/re-sized/re-checked);
  • rejecting drops it; a stale (expired) proposal can't be approved into a
    moved market; and the ~5s monitor loop can't stack duplicate proposals.

The gate NEVER touches paper (simulated), exits/closes (a protective exit must
never wait on a human) or deliberate MANUAL orders; turning it OFF restores
full "trade alone" autonomy.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.database import Base
from app.engine import TradingEngine
from app.models import AutoConfirmation, Trade, TradeStatus


class FakeConnector:
    """Scriptable connector: a live market buy fills, a protective stop rests,
    and every exchange call is recorded so a test can assert what was placed."""

    has_credentials = True

    def __init__(self, price: float = 100.0):
        self._price = price
        self.market_orders: list[tuple] = []
        self.stop_orders: list[tuple] = []

    @property
    def connected(self) -> bool:
        return True

    def reload(self, settings):
        pass
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
        return {"id": "m1", "average": self._price, "filled": amount}

    def create_limit_order(self, symbol, side, amount, price):
        return {"id": "l1"}

    def create_stop_loss_order(self, symbol, side, amount, stop_price):
        self.stop_orders.append((symbol, side, amount, stop_price))
        return {"id": "s-new"}

    def cancel_order(self, order_id, symbol):
        pass

    def fetch_order(self, order_id, symbol):
        return None
# <<APPEND2>>

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
        max_position_pct=0.0,       # concentration cap off: don't clamp auto-size
        trailing_stop_pct=0.0,
        profit_lock_enabled=False,
        paper_taker_fee_pct=0.0,
        reentry_cooldown_minutes=0.0,
    )
    base.update(over)
    eng = TradingEngine(Settings(**base), conn, user_id=None)
    eng._notified = []
    eng._notify = lambda text: eng._notified.append(text)
    eng._events = []
    eng._emit = lambda kind, payload: eng._events.append((kind, payload))
    return eng


def _auto_buy(eng, db):
    """Fire one autonomous LIVE buy through the single order path."""
    return eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=None,
        stop_loss=98.0, take_profit=None, source="auto",
        confidence=0.72, timeframe="1h", note="auto: strong uptrend",
    )


def _pending(db) -> list[AutoConfirmation]:
    return list(db.scalars(
        select(AutoConfirmation).where(AutoConfirmation.status == "pending")
    ).all())
# <<APPEND3>>

# ---- gate ON: queue, don't place ------------------------------------


def test_live_auto_buy_is_queued_not_placed(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)  # auto_live_confirm defaults ON
    ok, msg, trade = _auto_buy(eng, db)
    assert not ok and trade is None
    assert conn.market_orders == []          # nothing hit the exchange
    rows = _pending(db)
    assert len(rows) == 1
    row = rows[0]
    assert row.symbol == "BTC/USDT" and row.side == "buy"
    assert row.stop_loss == pytest.approx(98.0)
    assert row.confidence == pytest.approx(0.72)
    assert row.timeframe == "1h"
    assert row.expires_at is not None
    kinds = [k for k, _ in eng._events]
    assert "auto_confirm_pending" in kinds
    assert any("BUY" in n for n in eng._notified)


def test_gate_off_places_live_auto_buy_immediately(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, auto_live_confirm=False)
    ok, msg, trade = _auto_buy(eng, db)
    assert ok and trade is not None
    assert trade.status == TradeStatus.open.value
    assert len(conn.market_orders) == 1      # placed for real
    assert _pending(db) == []                # nothing queued


def test_live_tradingview_buy_is_also_gated(db):
    # A TradingView webhook BUY is an UNATTENDED, machine-decided live entry just
    # like the autopilot's own — the confirm-before-live gate covers it too, so a
    # stray/compromised alert can't spend REAL money without the operator's yes.
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)  # gate ON, live
    ok, msg, trade = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=None,
        stop_loss=98.0, take_profit=None, source="tradingview",
    )
    assert not ok and trade is None
    assert conn.market_orders == []          # nothing hit the exchange
    rows = _pending(db)
    assert len(rows) == 1 and rows[0].side == "buy"


def test_gate_off_places_live_tradingview_buy(db):
    # Turning the gate off restores hands-off webhook execution.
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, auto_live_confirm=False)
    ok, msg, trade = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=None,
        stop_loss=98.0, take_profit=None, source="tradingview",
    )
    assert ok and trade is not None
    assert len(conn.market_orders) == 1
    assert _pending(db) == []


def test_paper_auto_buy_not_gated(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, trading_mode="paper")  # gate is live-only
    ok, msg, trade = _auto_buy(eng, db)
    assert ok and trade is not None
    assert _pending(db) == []


def test_manual_live_buy_not_gated(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    ok, msg, trade = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=1.0,
        stop_loss=98.0, take_profit=None, source="manual",
    )
    assert ok and trade is not None
    assert len(conn.market_orders) == 1
    assert _pending(db) == []
# <<APPEND4>>

def test_auto_close_not_gated(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    # An open long that the brain now wants to exit.
    t = Trade(
        symbol="BTC/USDT", side="buy", amount=1.0, entry_price=100.0,
        stop_loss=98.0, status=TradeStatus.open.value, mode="live",
        source="auto", order_type="market",
    )
    db.add(t)
    db.commit()
    ok, msg, trade = eng.execute_signal(
        db, action="close", symbol="BTC/USDT", amount=None,
        stop_loss=None, take_profit=None, source="auto",
    )
    assert ok
    assert _pending(db) == []                # an exit is never held for approval


# ---- dedupe + expiry ------------------------------------------------


def test_repeat_ticks_do_not_stack_confirmations(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    _auto_buy(eng, db)
    _auto_buy(eng, db)   # the ~5s loop firing again on the same standing setup
    _auto_buy(eng, db)
    assert len(_pending(db)) == 1            # exactly one proposal, not three


# ---- approve / reject -----------------------------------------------


def test_approve_places_the_trade(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    _auto_buy(eng, db)
    row = _pending(db)[0]
    ok, msg, trade = eng.resolve_auto_confirmation(db, row.id, approve=True)
    assert ok and trade is not None
    assert trade.status == TradeStatus.open.value
    assert len(conn.market_orders) == 1      # placed on approval
    db.refresh(row)
    assert row.status == "approved"
    assert row.trade_id == trade.id
# <<APPEND5>>

def test_reject_places_nothing(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    _auto_buy(eng, db)
    row = _pending(db)[0]
    ok, msg, trade = eng.resolve_auto_confirmation(db, row.id, approve=False)
    assert ok and trade is None
    assert conn.market_orders == []          # nothing placed on a reject
    db.refresh(row)
    assert row.status == "rejected"
    assert row.trade_id is None
    assert _pending(db) == []


def test_expired_confirmation_cannot_be_approved(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    _auto_buy(eng, db)
    row = _pending(db)[0]
    # Force the freshness window into the past: a late "yes" must not fire into
    # a market that has since moved.
    row.expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1)
    db.commit()
    ok, msg, trade = eng.resolve_auto_confirmation(db, row.id, approve=True)
    assert not ok and trade is None
    assert conn.market_orders == []          # expired → nothing placed
    assert "expired" in msg.lower()
    db.refresh(row)
    assert row.status == "expired"


# ---- status surface -------------------------------------------------


def test_status_reports_gate_and_pending_count(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    _auto_buy(eng, db)
    st = eng.status(db)
    assert st["auto_live_confirm"] is True
    assert st["pending_confirmations"] == 1
