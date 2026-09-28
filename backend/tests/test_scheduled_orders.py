"""Tests for scheduled / timed orders ("buy at 20:00").

Engine-level tests drive the fire logic deterministically with a FakeConnector
and an in-memory DB (no network, no clock monkeypatching — rows carry explicit
past/future ``scheduled_for`` instants). Endpoint tests exercise the CRUD via
TestClient. The contract pinned here: a due armed order fires ONCE through the
same execute_signal chain a manual order uses; a refused run is an honest
``error`` (never a fabricated fill); future/cancelled/fired rows never fire.
"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.database import Base
from app.engine import TradingEngine
from app.main import app
from app.models import ScheduledOrder, Trade, TradeStatus


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class FakeConnector:
    """Paper connector with a mutable spot price (mirrors test_limit_orders)."""

    def __init__(self, price: float = 100.0):
        self._price = price
        self.has_credentials = False

    def reload(self, settings):
        pass

    def set_price(self, price: float):
        self._price = price

    def fetch_price(self, symbol):
        return self._price

    def fetch_ohlcv(self, symbol, timeframe, limit):
        return []

    def fetch_position_amounts(self):
        return {}

    def normalize_amount(self, symbol, amount, price):
        return amount, None

    def create_market_order(self, symbol, side, amount):
        return {"id": "m1", "average": self._price}

    def create_limit_order(self, symbol, side, amount, price):
        return {"id": "l1"}

    def create_stop_loss_order(self, symbol, side, amount, stop_price):
        return None

    def cancel_order(self, order_id, symbol):
        pass

    def fetch_order(self, order_id, symbol):
        return None


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
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
    )
    base.update(over)
    return TradingEngine(Settings(**base), connector)


def _schedule(db, *, action="buy", symbol="BTC/USDT", amount=1.0,
              limit_price=None, when_offset_s=-1, status="armed"):
    """Insert a ScheduledOrder due in the past (offset < 0) or future (> 0)."""
    so = ScheduledOrder(
        symbol=symbol, action=action, amount=amount, limit_price=limit_price,
        scheduled_for=_utcnow() + dt.timedelta(seconds=when_offset_s),
        status=status,
    )
    db.add(so)
    db.commit()
    db.refresh(so)
    return so


# ---- Engine-level: the fire logic, driven deterministically ---------------

def test_due_order_fires_once_and_links_a_real_trade(db):
    eng = _engine(FakeConnector(price=100.0))
    so = _schedule(db)  # armed BUY BTC/USDT, due 1s ago
    events = eng.check_scheduled_orders(db)
    db.refresh(so)
    assert so.status == "fired"
    assert so.result_trade_id is not None
    assert so.error is None
    assert so.fired_at is not None
    tr = db.get(Trade, so.result_trade_id)
    assert tr is not None and tr.symbol == "BTC/USDT"
    assert tr.status == TradeStatus.open.value  # market fill in paper
    assert len(events) == 1
    assert events[0]["kind"] == "scheduled" and events[0]["event"] == "fired"


def test_future_order_stays_armed(db):
    eng = _engine(FakeConnector())
    so = _schedule(db, when_offset_s=3600)  # due in an hour
    events = eng.check_scheduled_orders(db)
    db.refresh(so)
    assert events == []
    assert so.status == "armed"
    assert so.fired_at is None
    assert db.scalars(select(Trade)).all() == []


def test_canceled_order_never_fires(db):
    eng = _engine(FakeConnector())
    so = _schedule(db, status="canceled")  # past due, but user cancelled it
    events = eng.check_scheduled_orders(db)
    db.refresh(so)
    assert events == []
    assert so.status == "canceled"
    assert db.scalars(select(Trade)).all() == []


def test_fired_order_is_not_refired_on_the_next_sweep(db):
    eng = _engine(FakeConnector(price=100.0))
    so = _schedule(db)
    eng.check_scheduled_orders(db)
    db.refresh(so)
    assert so.status == "fired"
    first_trade_id = so.result_trade_id
    # A second sweep must be a no-op — the row is no longer "armed".
    events = eng.check_scheduled_orders(db)
    db.refresh(so)
    assert events == []
    assert so.result_trade_id == first_trade_id
    assert len(db.scalars(select(Trade)).all()) == 1


def test_already_in_position_becomes_honest_error_not_a_second_trade(db):
    eng = _engine(FakeConnector(price=100.0))
    ok, _msg, tr = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=1.0,
        stop_loss=None, take_profit=None, source="manual",
    )
    assert ok and tr is not None
    so = _schedule(db, symbol="BTC/USDT", action="buy")  # duplicate, comes due
    events = eng.check_scheduled_orders(db)
    db.refresh(so)
    assert so.status == "error"
    assert so.result_trade_id is None
    assert "Already in" in (so.error or "")
    open_trades = [t for t in db.scalars(select(Trade)).all()
                   if t.status == TradeStatus.open.value]
    assert len(open_trades) == 1  # no SECOND position opened
    assert events and events[0]["event"] == "error" and events[0]["level"] == "warn"


def test_limit_scheduled_order_rests_as_pending(db):
    eng = _engine(FakeConnector(price=100.0))
    # A buy limit BELOW market rests instead of filling now.
    so = _schedule(db, action="buy", limit_price=90.0)
    eng.check_scheduled_orders(db)
    db.refresh(so)
    assert so.status == "fired"
    tr = db.get(Trade, so.result_trade_id)
    assert tr is not None and tr.status == TradeStatus.pending.value


def test_sell_scheduled_order_fires_in_paper(db):
    # Paper simulates sell-to-open, so a scheduled SELL fires and books a trade.
    eng = _engine(FakeConnector(price=100.0))
    so = _schedule(db, action="sell", symbol="ETH/USDT", amount=1.0)
    eng.check_scheduled_orders(db)
    db.refresh(so)
    assert so.status == "fired"
    assert so.result_trade_id is not None


def test_engine_fires_only_its_own_users_orders(db):
    # A per-user engine must ignore another user's armed order entirely.
    eng = _engine(FakeConnector(price=100.0))
    eng.user_id = 1
    mine = ScheduledOrder(
        symbol="BTC/USDT", action="buy", amount=1.0, status="armed", user_id=1,
        scheduled_for=_utcnow() - dt.timedelta(seconds=1),
    )
    theirs = ScheduledOrder(
        symbol="BTC/USDT", action="buy", amount=1.0, status="armed", user_id=2,
        scheduled_for=_utcnow() - dt.timedelta(seconds=1),
    )
    db.add_all([mine, theirs])
    db.commit()
    db.refresh(mine)
    db.refresh(theirs)
    eng.check_scheduled_orders(db)
    db.refresh(mine)
    db.refresh(theirs)
    assert mine.status == "fired"
    assert theirs.status == "armed"  # untouched — different owner


# ---- Endpoint CRUD via TestClient (auth'd, no network) --------------------

@pytest.fixture(scope="module")
def client():
    from app.database import init_db

    s = get_settings()
    s.secret_key = "unit-test-secret-key"
    s.auto_license_new_users = True  # signups start licensed
    s.rate_limit_enabled = False
    init_db()
    with TestClient(app) as c:
        r = c.post("/api/auth/signup", json={
            "username": "scheduser", "email": "sched@example.com",
            "password": "supersecret123"})
        if r.status_code == 409:
            r = c.post("/api/auth/login", json={
                "identifier": "sched@example.com", "password": "supersecret123"})
        c.headers.update({"Authorization": f"Bearer {r.json()['access_token']}"})
        yield c


def _future_iso(secs: int = 3600) -> str:
    return (_utcnow() + dt.timedelta(seconds=secs)).isoformat()


def test_create_future_scheduled_order_ok(client):
    r = client.post("/api/orders/scheduled", json={
        "action": "buy", "symbol": "btc/usdt", "amount": 0.01,
        "scheduled_for": _future_iso()})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "armed"
    assert body["symbol"] == "BTC/USDT"  # normalized upper
    assert body["action"] == "buy"
    assert body["id"] > 0


def test_create_past_scheduled_order_rejected(client):
    r = client.post("/api/orders/scheduled", json={
        "action": "buy", "symbol": "BTC/USDT", "amount": 0.01,
        "scheduled_for": (_utcnow() - dt.timedelta(hours=1)).isoformat()})
    assert r.status_code == 400
    assert "future" in r.json()["detail"].lower()


def test_create_bad_symbol_rejected(client):
    r = client.post("/api/orders/scheduled", json={
        "action": "buy", "symbol": "BTCUSDT", "amount": 0.01,
        "scheduled_for": _future_iso()})
    assert r.status_code == 400
    assert "BASE/QUOTE" in r.json()["detail"]


def test_list_then_cancel_scheduled_order(client):
    oid = client.post("/api/orders/scheduled", json={
        "action": "sell", "symbol": "ETH/USDT", "amount": 0.5,
        "scheduled_for": _future_iso()}).json()["id"]
    listing = client.get("/api/orders/scheduled").json()
    assert any(o["id"] == oid for o in listing)
    d = client.delete(f"/api/orders/scheduled/{oid}")
    assert d.status_code == 200
    assert d.json() == {"canceled": oid, "status": "canceled"}
    after = client.get("/api/orders/scheduled").json()
    assert any(o["id"] == oid and o["status"] == "canceled" for o in after)


def test_cancel_missing_scheduled_order_404(client):
    r = client.delete("/api/orders/scheduled/99999999")
    assert r.status_code == 404
