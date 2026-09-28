"""Capital / money manager — disciplined autopilot sizing.

Covers the money manager the operator asked for: a per-run budget deployed a
slice at a time (holding the rest in reserve), outcome-based re-sizing, a profit
reserve held out of the redeployable budget, and a per-trade max-hold time-stop.

Safety invariant under test: the money manager only ever deploys the SAME or
LESS than the risk manager's risk-based size — never more — so every RiskManager
cap still binds and turning it on can't increase risk. Nothing is fabricated:
budgets/deployed/profit are read from real settings and real trades.
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
from app.money_manager import MoneyManager
from app.risk import RiskManager

_NOW = dt.datetime.now(dt.timezone.utc)


class _Conn:
    """Minimal paper-mode connector: a fixed price, a readable balance."""

    has_credentials = True

    def __init__(self, price: float = 100.0):
        self._price = price

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
        return 1_000.0

    def spread_pct(self, symbol):
        return 0.0

    def normalize_amount(self, symbol, amount, price):
        return amount, None

    def create_market_order(self, symbol, side, amount):
        return {"id": "m1", "average": self._price, "filled": amount}

    def create_stop_loss_order(self, symbol, side, amount, stop_price):
        return {"id": "s1"}


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


def _settings(**over) -> Settings:
    base = dict(
        trading_mode="paper",
        paper_starting_balance=1_000.0,
        risk_per_trade_pct=1.0,
        default_stop_loss_pct=2.0,
        max_open_positions=10,
        daily_loss_limit_pct=100.0,
        max_position_pct=0.0,
        max_total_exposure_pct=0.0,
        min_signal_confidence=0.1,
        capital_manager_enabled=True,
        capital_run_budget_quote=0.0,
        capital_per_trade_pct=25.0,
        capital_min_trade_quote=5.0,
        capital_resize_on_outcome=True,
        capital_profit_reserve_pct=0.0,
        capital_max_hold_minutes=0.0,
    )
    base.update(over)
    return Settings(**base)


def _mm(settings) -> MoneyManager:
    return MoneyManager(settings, user_id=None, risk=RiskManager(settings))


def _closed_auto(db, pnl: float, when: dt.datetime, mode: str = "paper") -> Trade:
    t = Trade(
        symbol="BTC/USDT", side="buy", amount=0.1, entry_price=100.0,
        exit_price=100.0 + pnl, status=TradeStatus.closed.value, mode=mode,
        source="auto", pnl=pnl, opened_at=when, closed_at=when,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def _open_auto(db, notional: float, mode: str = "paper", source: str = "auto") -> Trade:
    t = Trade(
        symbol="ETH/USDT", side="buy", amount=notional / 100.0, entry_price=100.0,
        status=TradeStatus.open.value, mode=mode, source=source, opened_at=_NOW,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ---- sizing: partial deploy, reserve, risk-ceiling bound ------------------


def test_disabled_is_passthrough(db):
    mm = _mm(_settings(capital_manager_enabled=False))
    # Disabled -> returns the risk-based size untouched (no budget discipline).
    assert mm.plan_amount(db, equity=1_000.0, price=100.0, risk_based_qty=3.0) == 3.0


def test_no_budget_bounded_by_risk_ceiling(db):
    # run_budget=0 -> free-equity budget. The 25% slice of $1000 is $250 (2.5
    # units), but the risk-based ceiling is only 2.0 units -> the ceiling binds.
    mm = _mm(_settings(capital_run_budget_quote=0.0, capital_per_trade_pct=25.0))
    qty = mm.plan_amount(db, equity=1_000.0, price=100.0, risk_based_qty=2.0)
    assert qty == pytest.approx(2.0)


def test_fixed_budget_partial_deploy(db):
    # "$20 for the run, deploy a quarter" -> $5 = 0.05 units, far under the ceiling.
    mm = _mm(_settings(capital_run_budget_quote=20.0, capital_per_trade_pct=25.0))
    qty = mm.plan_amount(db, equity=1_000.0, price=100.0, risk_based_qty=5.0)
    assert qty == pytest.approx(0.05)  # $5 / $100


def test_deployed_capital_shrinks_the_next_slice(db):
    # $10 already working (open AUTO position); of the $10 left, 25% = $2.50 which
    # is under the $5 floor, so it deploys the $5 minimum (still <= what's free).
    _open_auto(db, notional=10.0)
    mm = _mm(_settings(capital_run_budget_quote=20.0, capital_per_trade_pct=25.0))
    assert mm.deployed_quote(db) == pytest.approx(10.0)
    qty = mm.plan_amount(db, equity=1_000.0, price=100.0, risk_based_qty=5.0)
    assert qty == pytest.approx(0.05)  # floored to the $5 minimum


def test_budget_exhausted_holds(db):
    # Only $2 of the $20 budget is free — below the $5 minimum -> HOLD (0).
    _open_auto(db, notional=18.0)
    mm = _mm(_settings(capital_run_budget_quote=20.0, capital_per_trade_pct=25.0))
    assert mm.plan_amount(db, equity=1_000.0, price=100.0, risk_based_qty=5.0) == 0.0


def test_manual_position_does_not_consume_auto_budget(db):
    # A MANUAL open doesn't count against the autopilot's run budget.
    _open_auto(db, notional=18.0, source="manual")
    mm = _mm(_settings(capital_run_budget_quote=20.0, capital_per_trade_pct=25.0))
    assert mm.deployed_quote(db) == pytest.approx(0.0)


# ---- outcome-based re-sizing ---------------------------------------------


def test_outcome_multiplier_grows_on_wins_shrinks_faster_on_losses(db):
    mm = _mm(_settings())
    assert mm.outcome_multiplier(db) == 1.0  # no history

    # Two most-recent wins -> 1 + 0.15*2 = 1.30.
    _closed_auto(db, pnl=5.0, when=_NOW - dt.timedelta(minutes=20))
    _closed_auto(db, pnl=5.0, when=_NOW - dt.timedelta(minutes=10))
    assert mm.outcome_multiplier(db) == pytest.approx(1.30)

    # A fresh LOSS breaks the win streak and cuts size faster: 1 - 0.25 = 0.75.
    _closed_auto(db, pnl=-5.0, when=_NOW - dt.timedelta(minutes=1))
    assert mm.outcome_multiplier(db) == pytest.approx(0.75)


def test_outcome_multiplier_clamps(db):
    mm = _mm(_settings())
    for i in range(5):  # five straight losses -> floor at 0.5, not 1-1.25
        _closed_auto(db, pnl=-1.0, when=_NOW - dt.timedelta(minutes=5 - i))
    assert mm.outcome_multiplier(db) == pytest.approx(0.5)


# ---- profit reserve -------------------------------------------------------


def test_profit_reserve_held_out_of_budget(db):
    # $100 realised today, hold 50% -> $50 reserved. Of a $100 run budget, only
    # $50 remains deployable.
    _closed_auto(db, pnl=100.0, when=_NOW)
    mm = _mm(_settings(capital_run_budget_quote=100.0, capital_profit_reserve_pct=50.0))
    assert mm.available_budget(db, equity=1_000.0) == pytest.approx(50.0)


# ---- engine integration: auto entry sized by the money manager ------------


def _engine(**over) -> TradingEngine:
    return TradingEngine(_settings(**over), _Conn(price=100.0), user_id=None)


def test_auto_entry_deploys_only_the_budget_slice(db):
    # Autopilot buy with a $20 run budget / 25% slice: the position opens at ~$5
    # notional, not the ~$500 the risk-based sizer alone would have taken.
    eng = _engine(capital_run_budget_quote=20.0, capital_per_trade_pct=25.0)
    ok, msg, trade = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=None,
        stop_loss=98.0, take_profit=None, source="auto",
    )
    assert ok, msg
    assert trade is not None
    notional = trade.amount * trade.entry_price
    assert notional == pytest.approx(5.0, abs=0.5)


def test_auto_entry_holds_when_budget_exhausted(db):
    _open_auto(db, notional=18.0)
    eng = _engine(capital_run_budget_quote=20.0, capital_per_trade_pct=25.0)
    ok, msg, trade = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=None,
        stop_loss=98.0, take_profit=None, source="auto",
    )
    assert not ok
    assert trade is None
    assert "budget" in msg.lower()


def test_manual_entry_ignores_money_manager(db):
    # A deliberate MANUAL order with an explicit size is honoured as-is: the money
    # manager governs the autopilot, not the human.
    eng = _engine(capital_run_budget_quote=20.0)
    ok, msg, trade = eng.execute_signal(
        db, action="buy", symbol="BTC/USDT", amount=1.0,
        stop_loss=98.0, take_profit=None, source="manual",
    )
    assert ok, msg
    assert trade.amount == pytest.approx(1.0)  # $100, well above the $5 slice


# ---- engine integration: per-trade max-hold time-stop ---------------------


def _open_trade(db, **over) -> Trade:
    t = Trade(
        symbol="BTC/USDT", side="buy", amount=0.05, entry_price=100.0,
        stop_loss=90.0, take_profit=200.0, status=TradeStatus.open.value,
        mode="paper", source="auto", opened_at=_NOW - dt.timedelta(minutes=120),
    )
    for k, v in over.items():
        setattr(t, k, v)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


def test_time_stop_closes_aged_auto_trade(db):
    eng = _engine(capital_max_hold_minutes=60.0)
    _open_trade(db)  # opened 120 min ago, max hold 60 -> due
    closed = eng.check_open_positions(db)
    assert [hit for _t, hit in closed] == ["time-stop"]


def test_time_stop_off_by_default(db):
    eng = _engine(capital_max_hold_minutes=0.0)  # default: no time-based exit
    _open_trade(db)
    assert eng.check_open_positions(db) == []


def test_time_stop_never_touches_manual_trades(db):
    eng = _engine(capital_max_hold_minutes=60.0)
    _open_trade(db, source="manual")  # aged, but manual -> never time-stopped
    assert eng.check_open_positions(db) == []
