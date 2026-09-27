"""Honest live-fill accounting (audit M2 + M3).

M2 — a LIVE open must record the base quantity we ACTUALLY hold, not the size we
requested. A spot BUY has its taker fee skimmed from the base asset and venues
round to lot size, so ``order['filled']`` (minus any base-asset fee) is what we
can later sell. Booking the request instead over-reports holdings and a later
close hits Binance -2010 (insufficient balance), wedging the trade open.

M3 — LIVE realized P&L must be booked NET of the real fees the venue reported
(``order['fee']``/``order['fees']``), captured on both the entry and exit legs.
We only ever subtract fees we OBSERVED and can value from real data: a quote fee
as-is, a base-asset fee at the actual fill price. A fee paid in a third asset
(e.g. BNB) is left out rather than converted with a price we never saw — we
under-count that rare case instead of inventing a number.
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


class FakeConnector:
    """Fake connector whose market fills are scriptable, so a test can hand back
    an order dict with a specific ``filled`` quantity and ``fee`` shape and assert
    exactly what the engine books."""

    has_credentials = True

    def __init__(self, price: float = 100.0):
        self._price = price
        self._order = None            # what fetch_order returns (resting/stop)
        self._script: list[dict] = []  # scripted create_market_order results
        self.market_orders: list[tuple] = []
        self.stop_orders: list[tuple] = []
        self.canceled: list[tuple] = []

    @property
    def connected(self) -> bool:
        return True

    def reload(self, settings):
        pass

    def set_price(self, price):
        self._price = price

    def set_order(self, order):
        self._order = order

    def script_market(self, *orders):
        self._script = list(orders)

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
        if self._script:
            return self._script.pop(0)
        return {"id": "m1", "average": self._price, "filled": amount}

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
    session._sa_engine_ref = engine
    try:
        yield session
    finally:
        session.close()


def _engine(connector, **over) -> TradingEngine:
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
        max_position_pct=0.0,       # don't clamp the explicit test amounts
        trailing_stop_pct=0.0,
        profit_lock_enabled=False,
        paper_taker_fee_pct=0.0,    # isolate observed live fees from the sim fee
    )
    base.update(over)
    return TradingEngine(Settings(**base), connector, user_id=None)


def _open_live(eng, db, **over):
    args = dict(
        action="buy", symbol="BTC/USDT", amount=1.0,
        stop_loss=None, take_profit=None, source="manual",
    )
    args.update(over)
    return eng.execute_signal(db, **args)


# ---- _fill_details: the honest parser ------------------------------


def test_fill_details_quote_fee():
    filled, fee_q, fee_b = TradingEngine._fill_details(
        {"filled": 2.0, "fee": {"cost": 3.0, "currency": "USDT"}}, "BTC/USDT", 100.0
    )
    assert (filled, fee_q, fee_b) == (2.0, 3.0, 0.0)


def test_fill_details_base_fee_valued_at_fill_price():
    # A base-asset (BTC) fee is valued at the real fill price, not guessed.
    filled, fee_q, fee_b = TradingEngine._fill_details(
        {"filled": 2.0, "fee": {"cost": 0.01, "currency": "BTC"}}, "BTC/USDT", 100.0
    )
    assert filled == 2.0
    assert fee_b == pytest.approx(0.01)
    assert fee_q == pytest.approx(1.0)  # 0.01 BTC * 100 quote/BTC


def test_fill_details_prefers_itemised_fees_list():
    # When both shapes are present, the itemised ``fees`` list wins (the singular
    # ``fee`` is a duplicate total on many venues — using both double-counts).
    filled, fee_q, fee_b = TradingEngine._fill_details(
        {
            "filled": 1.0,
            "fees": [
                {"cost": 1.0, "currency": "USDT"},
                {"cost": 0.001, "currency": "BTC"},
            ],
            "fee": {"cost": 999.0, "currency": "USDT"},
        },
        "BTC/USDT",
        200.0,
    )
    assert filled == 1.0
    assert fee_b == pytest.approx(0.001)
    assert fee_q == pytest.approx(1.0 + 0.001 * 200.0)  # 1.2, NOT 999


def test_fill_details_third_asset_fee_not_converted():
    # A BNB fee can't be valued from data we have -> left out, never fabricated.
    filled, fee_q, fee_b = TradingEngine._fill_details(
        {"filled": 1.0, "fee": {"cost": 0.05, "currency": "BNB"}}, "BTC/USDT", 100.0
    )
    assert (filled, fee_q, fee_b) == (1.0, 0.0, 0.0)


def test_fill_details_handles_missing_order_and_settle_suffix():
    assert TradingEngine._fill_details(None, "BTC/USDT", 100.0) == (0.0, 0.0, 0.0)
    assert TradingEngine._fill_details({}, "BTC/USDT", 100.0) == (0.0, 0.0, 0.0)
    # A ":settle" swap suffix on the quote is stripped so the quote fee matches.
    _, fee_q, _ = TradingEngine._fill_details(
        {"filled": 1.0, "fee": {"cost": 2.0, "currency": "USDT"}},
        "BTC/USDT:USDT", 100.0,
    )
    assert fee_q == pytest.approx(2.0)


# ---- M2: live opens book the REAL filled quantity ------------------


def test_live_market_open_books_net_qty_and_base_fee(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    # Requested 1.0, but the venue filled 1.0 and skimmed a 0.001 BTC taker fee:
    # we actually HOLD 0.999, and the fee is worth 0.1 quote at the fill price.
    conn.script_market(
        {"id": "m1", "average": 100.0, "filled": 1.0,
         "fee": {"cost": 0.001, "currency": "BTC"}}
    )
    ok, msg, trade = _open_live(eng, db, amount=1.0)
    assert ok, msg
    assert trade.amount == pytest.approx(0.999)   # net sellable base, not 1.0
    assert trade.fee == pytest.approx(0.1)         # 0.001 BTC * 100
    # The protective exchange stop is sized to what we actually hold, not 1.0.
    assert conn.stop_orders[-1][2] == pytest.approx(0.999)


def test_live_market_open_books_filled_qty_and_quote_fee(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    # Partial fill: requested 1.0, venue filled only 0.9; quote fee taken as-is.
    conn.script_market(
        {"id": "m1", "average": 100.0, "filled": 0.9,
         "fee": {"cost": 9.0, "currency": "USDT"}}
    )
    ok, msg, trade = _open_live(eng, db, amount=1.0)
    assert ok, msg
    assert trade.amount == pytest.approx(0.9)
    assert trade.fee == pytest.approx(9.0)


def test_live_open_third_asset_fee_records_no_fabricated_fee(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    conn.script_market(
        {"id": "m1", "average": 100.0, "filled": 1.0,
         "fee": {"cost": 0.05, "currency": "BNB"}}
    )
    ok, msg, trade = _open_live(eng, db, amount=1.0)
    assert ok, msg
    assert trade.amount == pytest.approx(1.0)  # BNB fee doesn't reduce base held
    assert trade.fee == pytest.approx(0.0)     # never invented from a price we lack


def test_zero_fill_is_rejected(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    # Fill so tiny the base fee consumes all of it -> nothing held -> refuse,
    # rather than book a phantom position.
    conn.script_market(
        {"id": "m1", "average": 100.0, "filled": 0.0005,
         "fee": {"cost": 0.0005, "currency": "BTC"}}
    )
    ok, msg, trade = _open_live(eng, db, amount=1.0)
    assert not ok
    assert "zero fill" in msg.lower()
    assert db.query(Trade).count() == 0


def test_live_limit_fill_books_net_qty_and_fee(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    t = Trade(
        symbol="BTC/USDT", side="buy", amount=1.0, entry_price=0.0,
        limit_price=100.0, order_type="limit", exchange_order_id="l1",
        status=TradeStatus.pending.value, mode="live",
    )
    db.add(t)
    db.commit()
    # The exchange reports the resting order fully filled: 0.999 BTC net of a
    # 0.001 BTC fee.
    conn.set_order({
        "status": "closed", "filled": 1.0, "average": 100.0,
        "fee": {"cost": 0.001, "currency": "BTC"},
    })
    filled = eng.check_pending_orders(db)
    assert len(filled) == 1
    db.refresh(t)
    assert t.status == TradeStatus.open.value
    assert t.amount == pytest.approx(0.999)
    assert t.fee == pytest.approx(0.1)


# ---- M3: live realized P&L is net of the observed fees -------------


def test_live_realized_pnl_is_net_of_observed_fees(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    # Open (buy) fee 0.1 quote; then close (sell) at 110 with fee 0.11 quote.
    conn.script_market(
        {"id": "m1", "average": 100.0, "filled": 1.0,
         "fee": {"cost": 0.1, "currency": "USDT"}},
        {"id": "m2", "average": 110.0, "filled": 1.0,
         "fee": {"cost": 0.11, "currency": "USDT"}},
    )
    ok, msg, trade = _open_live(eng, db, amount=1.0)
    assert ok, msg
    tid = trade.id
    conn.set_price(110.0)
    ok, msg, closed = eng.close_by_id(db, tid)
    assert ok, msg
    db.refresh(closed)
    assert closed.fee == pytest.approx(0.21)               # both legs summed
    # gross (110-100)*1.0 = 10.0; net of 0.21 fees = 9.79 — no invented number.
    assert closed.pnl == pytest.approx(10.0 - 0.21)


def test_paper_trade_records_zero_observed_fee(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, trading_mode="paper")
    ok, msg, trade = _open_live(eng, db, amount=1.0)
    assert ok, msg
    # Paper never observes a real fee — the field stays 0.0 (paper models cost via
    # the simulated taker fee instead, which is 0 here so PnL is the gross move).
    assert trade.fee == pytest.approx(0.0)
    conn.set_price(110.0)
    ok, msg, closed = eng.close_by_id(db, trade.id)
    assert ok, msg
    db.refresh(closed)
    assert closed.fee == pytest.approx(0.0)
    assert closed.pnl == pytest.approx(10.0)
