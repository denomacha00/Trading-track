"""Autopilot profit-taking + visible regime/pause, all fee-aware and honest.

Covers the hands-off features a non-trader relies on:
- profit-lock: ratchet a winner's stop into the green, but NEVER bank a gain
  thinner than round-trip fees would erase (fees are the automatic safety floor);
- reversal exit: an opt-in ON/OFF toggle that only sells a genuine NET winner and
  only after the read stays bearish for ``reversal_confirm_count`` reads
  (anti-whipsaw) — OFF means the trade runs to its stop/target ("must finish");
- regime snapshot: status() shows the bot standing aside in a bear and re-engaging
  in a bull. Nothing here fabricates a price, a fee, or an accuracy figure.
"""
from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.analysis import MarketAnalyzer
from app.config import Settings
from app.database import Base
from app.engine import TradingEngine
from app.models import Trade, TradeStatus


class FakeConnector:
    connected = True
    has_credentials = False

    def __init__(self, price: float = 100.0):
        self._price = price

    def reload(self, settings):
        pass

    def fetch_price(self, symbol):
        return self._price

    def fetch_ohlcv(self, symbol, timeframe, limit):
        return []

    def fetch_position_amounts(self):
        return {}

    def fetch_balance(self, quote="USDT"):
        return None

    def create_stop_loss_order(self, symbol, side, amount, stop_price):
        return None

    def cancel_order(self, order_id, symbol):
        pass


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


def _engine(connector=None, **over) -> TradingEngine:
    base = dict(
        trading_mode="paper",
        max_open_positions=5,
        default_stop_loss_pct=2.0,
        default_take_profit_pct=4.0,
        paper_starting_balance=10_000.0,
        min_signal_confidence=0.1,
    )
    base.update(over)
    return TradingEngine(Settings(**base), connector or FakeConnector(), user_id=None)


def _frame(closes) -> pd.DataFrame:
    n = len(closes)
    return pd.DataFrame(
        {
            "timestamp": range(n),
            "open": closes,
            "high": [c * 1.001 for c in closes],
            "low": [c * 0.999 for c in closes],
            "close": closes,
            "volume": [1.0] * n,
        }
    )


_UP = _frame([100 + i * 0.8 for i in range(200)])     # clean bull
_DOWN = _frame([300 - i * 0.8 for i in range(220)])   # deep bear


def _open_buy(db, entry=100.0, amount=1.0, stop=None) -> Trade:
    t = Trade(symbol="BTC/USDT", side="buy", amount=amount, entry_price=entry,
              stop_loss=stop, status=TradeStatus.open.value, mode="paper")
    db.add(t)
    db.commit()
    return t


# ---- profit-lock (ratchet the stop into profit, fee-aware) ----------------


def test_profit_lock_disabled_does_nothing(db):
    eng = _engine(profit_lock_enabled=False)
    t = _open_buy(db, entry=100.0, stop=98.0)
    eng._maybe_lock_profit(db, t, price=110.0)  # big winner, but feature OFF
    assert t.stop_loss == 98.0  # untouched


def test_profit_lock_arms_and_raises_stop_into_green(db):
    eng = _engine(
        profit_lock_enabled=True,
        profit_lock_trigger_pct=1.0,
        profit_lock_floor_pct=0.3,
        paper_taker_fee_pct=0.0,  # -> round-trip fee floor falls back to ~0.2%
    )
    t = _open_buy(db, entry=100.0, stop=98.0)
    # Up only 0.5% (< 1% trigger): not armed yet, stop stays.
    eng._maybe_lock_profit(db, t, price=100.5)
    assert t.stop_loss == 98.0
    # Up 2% (>= trigger): lock a 0.3% floor above entry -> stop = 100.30.
    eng._maybe_lock_profit(db, t, price=102.0)
    assert t.stop_loss == pytest.approx(100.30)
    assert t.stop_loss > 100.0  # genuinely in profit, above entry


def test_profit_lock_never_banks_a_fee_loss(db):
    # The naive "bank every tiny gain" trap: a floor thinner than round-trip fees
    # would lock in a NET LOSS. The engine clamps the floor UP to the fee cost, so
    # a 0.01% target can never place a losing stop.
    eng = _engine(
        profit_lock_enabled=True,
        profit_lock_trigger_pct=0.01,   # absurdly tiny on purpose
        profit_lock_floor_pct=0.01,
        paper_taker_fee_pct=0.1,        # 0.1%/side -> 0.2% round trip floor
    )
    t = _open_buy(db, entry=100.0, stop=98.0)
    # Up 0.1% only: below the 0.2% fee floor -> refuse to arm (would be a net loss).
    eng._maybe_lock_profit(db, t, price=100.1)
    assert t.stop_loss == 98.0
    # Up 0.5%: now clears fees; floor clamps to 0.2% -> stop = 100.20 (net-positive).
    eng._maybe_lock_profit(db, t, price=100.5)
    assert t.stop_loss == pytest.approx(100.20)


# ---- dollar-target arming + profit-activated trailing ("let it ride") ------


def test_profit_lock_arms_on_dollar_target(db):
    # The non-trader knob: "bank me when I'm up $1". entry 100 x amount 1 => $1 of
    # unrealized profit at price 101. Below that it must NOT arm; at/above it locks
    # a net-positive floor (0.3%) above entry.
    eng = _engine(
        profit_lock_enabled=True,
        profit_lock_trigger_usd=1.0,
        profit_lock_floor_pct=0.3,
        profit_lock_trail_pct=0.0,   # static floor, no ride
        paper_taker_fee_pct=0.0,
    )
    t = _open_buy(db, entry=100.0, amount=1.0, stop=98.0)
    eng._maybe_lock_profit(db, t, price=100.5)   # only +$0.50 -> not armed
    assert t.stop_loss == 98.0
    eng._maybe_lock_profit(db, t, price=101.0)   # +$1.00 -> arm, lock 100.30
    assert t.stop_loss == pytest.approx(100.30)


def test_profit_lock_dollar_target_respects_fee_floor(db):
    # A $0.01 target on a $100 position is thinner than round-trip fees: the lock
    # must wait until the trade actually clears fees, never arming on a fee-loss.
    eng = _engine(
        profit_lock_enabled=True,
        profit_lock_trigger_usd=0.01,   # absurdly tiny on purpose
        profit_lock_floor_pct=0.3,
        paper_taker_fee_pct=0.1,        # 0.2% round trip => $0.20 floor on notional 100
    )
    t = _open_buy(db, entry=100.0, amount=1.0, stop=98.0)
    eng._maybe_lock_profit(db, t, price=100.1)   # +$0.10 < $0.20 fee floor -> no arm
    assert t.stop_loss == 98.0
    eng._maybe_lock_profit(db, t, price=100.5)   # +$0.50 clears fees -> arm at 100.30
    assert t.stop_loss == pytest.approx(100.30)


def test_profit_lock_trails_up_and_holds_on_pullback(db):
    # "The money is still coming so go on — but the moment it drops a bit, stop."
    # Once armed at the $ target the stop trails 0.5% under price and ratchets UP as
    # price climbs; a pullback never LOWERS it (raise-only), so the monitor's stop
    # check banks the ridden-up gain instead of giving it back.
    eng = _engine(
        profit_lock_enabled=True,
        profit_lock_trigger_usd=1.0,
        profit_lock_floor_pct=0.3,
        profit_lock_trail_pct=0.5,      # ride, bank on a 0.5% pullback
        paper_taker_fee_pct=0.0,
    )
    t = _open_buy(db, entry=100.0, amount=1.0, stop=98.0)
    eng._maybe_lock_profit(db, t, price=101.0)   # arm: max(100.30, 101*0.995=100.495)
    assert t.stop_loss == pytest.approx(100.495)
    eng._maybe_lock_profit(db, t, price=110.0)   # ride up: 110*0.995 = 109.45
    assert t.stop_loss == pytest.approx(109.45)
    eng._maybe_lock_profit(db, t, price=108.0)   # pullback: raise-only, stop unchanged
    assert t.stop_loss == pytest.approx(109.45)


def test_profit_lock_dollar_target_ignores_shorts(db):
    # Spot bot: profit-lock is long-only. A short is left untouched.
    eng = _engine(profit_lock_enabled=True, profit_lock_trigger_usd=1.0)
    t = Trade(symbol="BTC/USDT", side="sell", amount=1.0, entry_price=100.0,
              stop_loss=102.0, status=TradeStatus.open.value, mode="paper")
    db.add(t)
    db.commit()
    eng._maybe_lock_profit(db, t, price=90.0)   # deep in profit for a short
    assert t.stop_loss == 102.0                 # untouched


# ---- reversal exit (opt-in ON/OFF, confirmed, net-winner only) ------------


def _force_verdict(eng, verdict: str) -> None:
    eng.analyze_symbol = lambda *a, **k: SimpleNamespace(verdict=verdict)


def test_reversal_exit_off_never_fires(db):
    # OFF -> "it must finish": run to the stop/target, never an early reversal exit.
    eng = _engine(take_profit_on_reversal=False)
    _force_verdict(eng, "sell")
    t = _open_buy(db, entry=100.0)
    assert eng._should_take_profit_on_reversal(db, t, price=200.0) is False


def test_reversal_exit_requires_confirmation_count(db):
    # ON with confirm=2: one bearish read is NOT enough (anti-whipsaw); the second
    # consecutive bearish read on a net winner triggers the bank-the-profit exit.
    eng = _engine(
        take_profit_on_reversal=True,
        reversal_confirm_count=2,
        paper_taker_fee_pct=0.0,
    )
    _force_verdict(eng, "sell")
    t = _open_buy(db, entry=100.0)              # net winner at price 110
    assert eng._should_take_profit_on_reversal(db, t, price=110.0) is False  # 1st flag
    assert eng._should_take_profit_on_reversal(db, t, price=110.0) is True   # 2nd flag


def test_reversal_exit_only_sells_a_net_winner(db):
    # A position that is not net-positive after fees is left for the stop, never
    # realized early — even on a bearish read. The streak also resets.
    eng = _engine(
        take_profit_on_reversal=True,
        reversal_confirm_count=1,
        paper_taker_fee_pct=0.1,   # 0.2% round trip
    )
    _force_verdict(eng, "sell")
    t = _open_buy(db, entry=100.0)
    # Up only 0.1% (0.1 gain) vs 0.2 fee cost -> net negative -> do not exit.
    assert eng._should_take_profit_on_reversal(db, t, price=100.1) is False
    assert t.id not in eng._reversal_flags


def test_reversal_bullish_read_resets_streak(db):
    # A bearish flag followed by a non-sell read clears the streak, so it takes a
    # fresh run of confirmations to trigger — a single blip can't accumulate.
    eng = _engine(
        take_profit_on_reversal=True,
        reversal_confirm_count=2,
        paper_taker_fee_pct=0.0,
    )
    t = _open_buy(db, entry=100.0)
    _force_verdict(eng, "sell")
    assert eng._should_take_profit_on_reversal(db, t, price=110.0) is False  # flag 1
    _force_verdict(eng, "hold")
    assert eng._should_take_profit_on_reversal(db, t, price=110.0) is False  # reset
    assert t.id not in eng._reversal_flags
    _force_verdict(eng, "sell")
    assert eng._should_take_profit_on_reversal(db, t, price=110.0) is False  # flag 1 again


# ---- visible regime / pause-resume ---------------------------------------


def test_record_regime_pauses_in_bear_resumes_in_bull(db):
    eng = _engine(auto_pause_in_bear=True)
    eng._record_regime("BTC/USDT", MarketAnalyzer().analyze(_DOWN, "BTC/USDT"))
    snap = eng._last_regime["BTC/USDT"]
    assert snap["regime"] == "bear"
    assert snap["entries_paused"] is True   # standing aside in a bad market

    eng._record_regime("BTC/USDT", MarketAnalyzer().analyze(_UP, "BTC/USDT"))
    snap = eng._last_regime["BTC/USDT"]
    assert snap["regime"] == "bull"
    assert snap["entries_paused"] is False  # re-engaged in a good market


def test_status_exposes_regime_and_pause_fields(db):
    eng = _engine(auto_trade_enabled=True, max_consecutive_losses=3)
    eng._record_regime("BTC/USDT", MarketAnalyzer().analyze(_UP, "BTC/USDT"))
    st = eng.status(db)
    for key in (
        "regimes", "entries_paused", "entries_pause_reason",
        "auto_trade_enabled", "consecutive_losses", "max_consecutive_losses",
    ):
        assert key in st
    assert st["auto_trade_enabled"] is True
    assert st["max_consecutive_losses"] == 3
    assert "BTC/USDT" in st["regimes"]
    assert st["regimes"]["BTC/USDT"]["regime"] == "bull"
