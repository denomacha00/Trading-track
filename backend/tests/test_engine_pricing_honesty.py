"""L2 + L3 — engine pricing honesty.

L2: equity / drawdown must NOT fall back to ``entry_price`` on a price-feed
    outage. Fabricating "price == entry" reads as zero unrealized PnL / unchanged
    equity and blinds the drawdown kill-switch and the daily-loss breaker to a
    real loss. The honest answer to "can't price this position" is UNKNOWN —
    degrade safe (refuse new entries, report equity as null/stale), never a fake
    all-clear.

L3: a PAPER stop/target exit must book at its LEVEL, capped on overshoot. The bot
    places real resting orders on live, which fire at their trigger, so a paper
    fill can't pocket the extra distance the ~5s poll happened to sample past the
    level (fabricated profit on a target / a deeper-than-real loss on a stop).

All fake-connector based — no network or credentials.
"""
from __future__ import annotations

import types

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings
from app.database import Base
from app.engine import TradingEngine
from app.models import TradeStatus


class FakeConnector:
    """Paper-capable fake with per-symbol prices and per-symbol feed outages."""

    def __init__(self, price=100.0, balance=10_000.0):
        self._default = price
        self._prices: dict[str, float] = {}
        self._raise_all = False
        self._raise_for: set[str] = set()
        self._balance = balance
        self.market_orders: list[tuple] = []

    @property
    def connected(self) -> bool:
        return True

    def reload(self, settings):
        pass

    def set_price(self, price, symbol=None):
        if symbol is None:
            self._default = price
        else:
            self._prices[symbol] = price

    def outage(self, flag=True, symbol=None):
        if symbol is None:
            self._raise_all = flag
        elif flag:
            self._raise_for.add(symbol)
        else:
            self._raise_for.discard(symbol)

    def fetch_price(self, symbol):
        if self._raise_all or symbol in self._raise_for:
            raise RuntimeError(f"price feed unreachable for {symbol}")
        return self._prices.get(symbol, self._default)

    def fetch_ohlcv(self, symbol, timeframe, limit):
        return []

    def fetch_position_amounts(self):
        return {}

    def fetch_balance(self, quote="USDT"):
        return self._balance

    def normalize_amount(self, symbol, amount, price):
        return amount, None

    def spread_pct(self, symbol):
        return 0.0

    def create_market_order(self, symbol, side, amount):
        self.market_orders.append((symbol, side, amount))
        return {"id": "m1", "average": self.fetch_price(symbol)}

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
    session._sa_engine_ref = engine  # keep the :memory: db alive
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
        max_spread_pct=1.0,
        reentry_cooldown_minutes=15.0,
        max_consecutive_losses=3,
        atr_stop_mult=1.5,
    )
    base.update(over)
    return TradingEngine(Settings(**base), connector)


def _open(eng, db, symbol="BTC/USDT", amount=1.0, stop_loss=None, take_profit=None):
    ok, msg, _ = eng.execute_signal(
        db, action="buy", symbol=symbol, amount=amount,
        stop_loss=stop_loss, take_profit=take_profit, source="manual",
    )
    assert ok, msg


# ===================== L2 — pricing honesty =====================

def test_open_unrealized_raises_on_outage(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    _open(eng, db)
    assert eng._open_unrealized(db) == pytest.approx(0.0)  # priceable now
    # Feed goes dark: the honest answer is UNKNOWN, so it must RAISE — never
    # silently value the position at entry (a fake zero-PnL all-clear).
    conn.outage(True)
    with pytest.raises(Exception):
        eng._open_unrealized(db)


def test_total_equity_raises_on_outage(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    _open(eng, db)
    assert eng._total_equity(db) == pytest.approx(10_000.0)
    conn.outage(True)
    with pytest.raises(Exception):
        eng._total_equity(db)


def test_drawdown_check_degrades_safe_on_outage(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, paper_starting_balance=1_000.0, max_drawdown_pct=20.0)
    _open(eng, db, amount=5.0)   # notional 500; cash 500; equity peak 1000
    assert eng._update_drawdown(db) is False   # healthy: seeds peak, no breach
    assert eng._peak_equity == pytest.approx(1_000.0)
    assert eng._killswitch_tripped is False
    # Outage: must NOT crash, NOT fabricate a breach, NOT move the peak.
    conn.outage(True)
    assert eng._update_drawdown(db) is False
    assert eng._killswitch_tripped is False
    assert eng._peak_equity == pytest.approx(1_000.0)


def test_daily_loss_breaker_degrades_safe_on_outage(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    _open(eng, db)
    assert eng._daily_loss_hit(db) is False
    # Unpriceable book -> can't compute today's loss -> no crash, no fake trip.
    conn.outage(True)
    assert eng._daily_loss_hit(db) is False


def test_new_entry_refused_when_a_holding_is_unpriceable(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    _open(eng, db, symbol="BTC/USDT")
    # BTC feed goes dark; ETH is fine. We can't value the book, so a NEW entry
    # must be refused cleanly (no 500, no fake all-clear) until it recovers.
    conn.outage(True, symbol="BTC/USDT")
    ok, msg, _ = eng.execute_signal(
        db, action="buy", symbol="ETH/USDT", amount=1.0,
        stop_loss=None, take_profit=None, source="manual",
    )
    assert ok is False
    assert "price feed" in msg.lower() or "holding new entries" in msg.lower()


def test_status_reports_stale_not_fake_on_outage(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    _open(eng, db)   # 10_000 start; notional 100 reserved -> cash 9_900
    conn.outage(True)
    st = eng.status(db)
    # Equity/unrealized are UNKNOWN, reported as null (UI shows "-"), never 0.
    assert st["equity"] is None
    assert st["unrealized_pnl"] is None
    assert st["prices_stale"] is True
    # Free cash is still real and honest.
    assert st["balance"] == pytest.approx(9_900.0)


def test_healthy_feed_still_trips_killswitch_and_reports_numeric(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, paper_starting_balance=1_000.0, max_drawdown_pct=20.0)
    _open(eng, db, amount=5.0)   # equity peak 1000
    assert eng._update_drawdown(db) is False
    # Price collapses: equity 500 cash + 100 value = 600 -> 40% below peak.
    conn.set_price(20.0)
    assert eng._update_drawdown(db) is True
    assert eng._killswitch_tripped is True
    st = eng.status(db)
    assert st["prices_stale"] is False
    assert st["equity"] == pytest.approx(600.0)
    assert st["unrealized_pnl"] == pytest.approx(-400.0)


# ===================== L3 — paper exits book at the level =====================

@pytest.mark.parametrize(
    "side,hit,observed,level,expected",
    [
        ("buy", "stop-loss", 90.0, 95.0, 95.0),      # long stop: gap down capped up
        ("buy", "take-profit", 115.0, 110.0, 110.0),  # long target: spike capped down
        ("sell", "stop-loss", 110.0, 105.0, 105.0),   # short stop: gap up capped down
        ("sell", "take-profit", 85.0, 90.0, 90.0),    # short target: dip capped up
        ("buy", "take-profit", 108.0, 110.0, 108.0),  # no overshoot -> real fill kept
        ("buy", "stop-loss", 96.0, 95.0, 96.0),       # no overshoot -> real fill kept
    ],
)
def test_cap_fill_at_level(side, hit, observed, level, expected):
    got = TradingEngine._cap_fill_at_level(side, hit, observed, level)
    assert got == pytest.approx(expected)


@pytest.mark.parametrize("level", [None, 0.0])
def test_cap_fill_passthrough_without_level(level):
    # No level configured -> can't cap; book the observed tick unchanged.
    assert TradingEngine._cap_fill_at_level("buy", "take-profit", 115.0, level) == 115.0


def test_paper_long_take_profit_books_at_target_on_overshoot(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    _open(eng, db, stop_loss=90.0, take_profit=110.0)
    # The ~5s poll samples 115 — past the 110 target. A REAL resting order fills
    # at 110, so paper must book 110, not pocket the extra 5 of fabricated profit.
    conn.set_price(115.0)
    closed = eng.check_open_positions(db)
    assert [c[1] for c in closed] == ["take-profit"]
    trade = closed[0][0]
    assert trade.exit_price == pytest.approx(110.0)
    assert trade.pnl == pytest.approx(10.0)   # (110-100)*1, no fee in paper default


def test_paper_long_stop_books_at_stop_on_overshoot(db):
    conn = FakeConnector(price=100.0)
    eng = _engine(conn)
    _open(eng, db, stop_loss=95.0, take_profit=130.0)
    # Poll samples 90 — past the 95 stop. Real stop fires at 95; a deeper paper
    # fill would fabricate a bigger-than-real loss.
    conn.set_price(90.0)
    closed = eng.check_open_positions(db)
    assert [c[1] for c in closed] == ["stop-loss"]
    trade = closed[0][0]
    assert trade.exit_price == pytest.approx(95.0)
    assert trade.pnl == pytest.approx(-5.0)


def test_paper_exit_fill_none_on_live():
    conn = FakeConnector(price=100.0)
    eng = _engine(conn, trading_mode="live")
    pos = types.SimpleNamespace(side="buy", stop_loss=95.0, take_profit=110.0)
    # Live: the REAL exchange fill governs; the monitor must not synthesize one.
    assert eng._paper_exit_fill(pos, observed=115.0, hit="take-profit") is None
