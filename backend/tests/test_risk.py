"""Tests for the risk manager using an in-memory SQLite database."""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.models import Trade, TradeStatus
from app.risk import RiskManager


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
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
        max_open_positions=3,
        risk_per_trade_pct=1.0,
        daily_loss_limit_pct=5.0,
        default_stop_loss_pct=2.0,
        default_take_profit_pct=4.0,
        paper_starting_balance=10_000.0,
    )
    base.update(over)
    return Settings(**base)


def test_position_sizing_respects_risk_pct():
    rm = RiskManager(_settings(risk_per_trade_pct=1.0, default_stop_loss_pct=2.0))
    # risk = 1% of 10000 = 100; stop distance 2% -> notional = 100/0.02 = 5000.
    qty = rm.size_position(equity=10_000, price=100)
    assert qty == pytest.approx(50.0)  # 5000 notional / 100 price


def test_sizing_capped_at_equity():
    # Tiny stop distance would blow notional past equity; must cap at equity.
    rm = RiskManager(_settings(risk_per_trade_pct=50.0, default_stop_loss_pct=0.1))
    qty = rm.size_position(equity=10_000, price=100)
    assert qty * 100 <= 10_000 + 1e-6


def test_sizing_uses_actual_stop_distance():
    # A wider stop than the default must yield a SMALLER position so the amount
    # actually risked stays at risk_per_trade_pct rather than ballooning.
    rm = RiskManager(_settings(risk_per_trade_pct=1.0, default_stop_loss_pct=2.0))
    default_qty = rm.size_position(equity=10_000, price=100)  # 2% stop
    wide_qty = rm.size_position(equity=10_000, price=100, stop_fraction=0.10)  # 10% stop
    assert wide_qty < default_qty
    # risk = 1% of 10000 = 100; with a 10% stop -> notional 1000 -> qty 10.
    assert wide_qty == pytest.approx(10.0)


def test_check_sizes_against_explicit_stop(db):
    # With an explicit stop_price and no requested amount, sizing must use the
    # real stop distance (entry 100 -> stop 90 = 10%), not the default 2%.
    rm = RiskManager(_settings(risk_per_trade_pct=1.0, default_stop_loss_pct=2.0))
    decision = rm.check(
        db, equity=10_000, price=100, requested_amount=None,
        is_opening=True, stop_price=90.0,
    )
    assert decision.allowed
    assert decision.amount == pytest.approx(10.0)  # risk 100 / 0.10 / price 100


def test_max_open_positions_blocks(db):
    rm = RiskManager(_settings(max_open_positions=1))
    db.add(Trade(symbol="BTC/USDT", side="buy", amount=1, entry_price=100,
                 status=TradeStatus.open.value))
    db.commit()
    decision = rm.check(db, equity=10_000, price=100, requested_amount=1, is_opening=True)
    assert not decision.allowed
    assert "Max open positions" in decision.reason


def test_daily_loss_limit_blocks(db):
    rm = RiskManager(_settings(daily_loss_limit_pct=5.0))
    # A closed losing trade today that exceeds 5% of 10000 = -500.
    db.add(Trade(symbol="ETH/USDT", side="buy", amount=1, entry_price=100,
                 exit_price=40, pnl=-600, status=TradeStatus.closed.value,
                 closed_at=dt.datetime.now(dt.timezone.utc)))
    db.commit()
    decision = rm.check(db, equity=10_000, price=100, requested_amount=1, is_opening=True)
    assert not decision.allowed
    assert "loss limit" in decision.reason.lower()


def test_notional_exceeds_equity_blocks(db):
    rm = RiskManager(_settings())
    decision = rm.check(db, equity=100, price=100, requested_amount=5, is_opening=True)
    assert not decision.allowed


def test_valid_trade_allowed(db):
    rm = RiskManager(_settings())
    decision = rm.check(db, equity=10_000, price=100, requested_amount=1, is_opening=True)
    assert decision.allowed
    assert decision.amount == 1


def test_total_exposure_cap_blocks(db):
    # Cap total open notional at 50% of equity. One open position of 3000 exists;
    # a new 3000 order would push total to 6000 > 5000 cap. (Concentration cap
    # off so this isolates the exposure cap; the explicit 3000 amount is honoured.)
    rm = RiskManager(_settings(max_total_exposure_pct=50.0, max_position_pct=0.0))
    db.add(Trade(symbol="BTC/USDT", side="buy", amount=30, entry_price=100,
                 status=TradeStatus.open.value))
    db.commit()
    decision = rm.check(db, equity=10_000, price=100, requested_amount=30, is_opening=True)
    assert not decision.allowed
    assert "exposure" in decision.reason.lower()


def test_total_exposure_cap_allows_within_limit(db):
    rm = RiskManager(_settings(max_total_exposure_pct=50.0, max_position_pct=0.0))
    db.add(Trade(symbol="BTC/USDT", side="buy", amount=10, entry_price=100,
                 status=TradeStatus.open.value))
    db.commit()
    # existing 1000 + new 1000 = 2000 <= 5000 cap.
    decision = rm.check(db, equity=10_000, price=100, requested_amount=10, is_opening=True)
    assert decision.allowed


def test_total_exposure_cap_disabled_by_default(db):
    # Exposure cap defaults to 0 (off). Concentration cap off too so this test
    # isolates "no exposure cap" (see the concentration-cap tests below).
    rm = RiskManager(_settings(max_position_pct=0.0))
    db.add(Trade(symbol="BTC/USDT", side="buy", amount=90, entry_price=100,
                 status=TradeStatus.open.value))
    db.commit()
    decision = rm.check(db, equity=10_000, price=100, requested_amount=5, is_opening=True)
    assert decision.allowed  # no exposure cap enforced


def test_exposure_cap_uses_total_equity_not_free_cash(db):
    # M5: the exposure cap is measured against TOTAL equity, not the shrinking
    # free-cash figure. Free cash is only 3000 but total equity is 10000; with a
    # 50% cap the ceiling is 5000 (of total), not 1500 (of free cash). An existing
    # 2000 position + a new 1000 order = 3000 <= 5000 -> allowed. Were the basis
    # free cash, 3000 > 1500 would (wrongly) block a perfectly safe trade.
    rm = RiskManager(_settings(max_total_exposure_pct=50.0, max_position_pct=0.0))
    db.add(Trade(symbol="BTC/USDT", side="buy", amount=20, entry_price=100,
                 status=TradeStatus.open.value))
    db.commit()
    decision = rm.check(
        db, equity=3_000, price=100, requested_amount=10,
        is_opening=True, equity_for_limits=10_000,
    )
    assert decision.allowed


# ---- per-position concentration cap (M6) ------------------------------------


def test_concentration_cap_clamps_auto_size(db):
    # Defaults would auto-size 50% of equity into ONE trade (risk 1% / stop 2% ->
    # 5000 notional). The 25% concentration cap must shrink it to 2500 (qty 25).
    rm = RiskManager(_settings(max_position_pct=25.0))
    decision = rm.check(
        db, equity=10_000, price=100, requested_amount=None,
        is_opening=True, equity_for_limits=10_000,
    )
    assert decision.allowed
    assert decision.amount == pytest.approx(25.0)  # 2500 notional / 100 price


def test_concentration_cap_uses_total_equity_basis(db):
    # The cap is a % of TOTAL equity, not free cash. Free cash 4000 auto-sizes to
    # 2000 notional (risk 1% of 4000 = 40, / 2% stop); the 25% cap on TOTAL equity
    # (10000) is 2500, so 2000 is under it and NOT clamped (amount 20). Were the
    # basis free cash (4000 -> cap 1000), it would wrongly clamp to qty 10.
    rm = RiskManager(_settings(max_position_pct=25.0))
    decision = rm.check(
        db, equity=4_000, price=100, requested_amount=None,
        is_opening=True, equity_for_limits=10_000,
    )
    assert decision.allowed
    assert decision.amount == pytest.approx(20.0)


def test_concentration_cap_does_not_resize_explicit_amount(db):
    # An EXPLICIT amount is the caller's deliberate choice: 40 @ 100 = 4000 (40%
    # of equity) exceeds the 25% concentration cap but is honoured, not silently
    # shrunk (still bounded by free cash + the exposure cap).
    rm = RiskManager(_settings(max_position_pct=25.0))
    decision = rm.check(
        db, equity=10_000, price=100, requested_amount=40,
        is_opening=True, equity_for_limits=10_000,
    )
    assert decision.allowed
    assert decision.amount == pytest.approx(40.0)


def test_concentration_cap_rejects_unattended_explicit_over_cap(db):
    # An UNATTENDED explicit size (autonomous decision / TradingView alert) that
    # breaches the 25% per-position cap is REFUSED — we neither trade a size the
    # feed didn't ask for nor let it quietly breach the blow-up guard. (A MANUAL
    # explicit over-cap amount is still honoured — see the test above.)
    rm = RiskManager(_settings(max_position_pct=25.0))
    decision = rm.check(
        db, equity=10_000, price=100, requested_amount=40,
        is_opening=True, equity_for_limits=10_000, unattended=True,
    )
    assert not decision.allowed
    assert "cap" in decision.reason.lower()


def test_concentration_cap_allows_unattended_within_cap(db):
    # The unattended guard only bites when the size actually breaches the cap:
    # 20 @ 100 = 2000 (20% < 25%) from a webhook is allowed and honoured as-is.
    rm = RiskManager(_settings(max_position_pct=25.0))
    decision = rm.check(
        db, equity=10_000, price=100, requested_amount=20,
        is_opening=True, equity_for_limits=10_000, unattended=True,
    )
    assert decision.allowed
    assert decision.amount == pytest.approx(20.0)


def test_concentration_cap_disabled_allows_full_auto_size(db):
    # With the cap off (0), the auto-sizer's full 50%-of-equity position stands.
    rm = RiskManager(_settings(max_position_pct=0.0))
    decision = rm.check(
        db, equity=10_000, price=100, requested_amount=None,
        is_opening=True, equity_for_limits=10_000,
    )
    assert decision.allowed
    assert decision.amount == pytest.approx(50.0)  # 5000 notional / 100 price


def test_daily_loss_limit_includes_open_drawdown(db):
    # No CLOSED losses today, but current OPEN drawdown of -600 already exceeds
    # the 5% (=$500) daily limit, so new entries must be blocked before any
    # stop fires — the breaker measures total current risk, not just realized.
    rm = RiskManager(_settings(daily_loss_limit_pct=5.0))
    decision = rm.check(
        db, equity=10_000, price=100, requested_amount=1,
        is_opening=True, day_unrealized=-600.0,
    )
    assert not decision.allowed
    assert "loss limit" in decision.reason.lower()


def test_daily_loss_limit_allows_small_open_drawdown(db):
    # A small open drawdown (-100) stays under the $500 limit -> still allowed.
    rm = RiskManager(_settings(daily_loss_limit_pct=5.0))
    decision = rm.check(
        db, equity=10_000, price=100, requested_amount=1,
        is_opening=True, day_unrealized=-100.0,
    )
    assert decision.allowed
