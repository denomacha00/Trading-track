"""Saved-strategy persistence + the bot trading with a saved strategy.

Covers the "train -> save -> the bot trades it" path and, crucially, that a
saved strategy can never override capital preservation: its BUY is refused in a
bear regime or while the analyzer is protectively standing aside, while its
SELL/exit is always honoured (reducing risk is never blocked).
"""
from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.engine as engine_mod
from app.analysis import MarketAnalyzer
from app.config import Settings
from app.database import Base
from app.engine import TradingEngine
from app.state import load_strategy_configs
from app.strategies import StrategySignal


class _Conn:
    connected = True
    has_credentials = False

    def reload(self, settings):
        pass

    def fetch_ohlcv(self, symbol, timeframe, limit):
        return []

    def fetch_price(self, symbol):
        return 100.0

    def fetch_position_amounts(self):
        return {}

    def fetch_balance(self, quote="USDT"):
        return None


class _StubStrat:
    """A strategy whose signal we control, to isolate the override logic."""

    def __init__(self, action: str):
        self._a = action

    def generate(self, df):
        return StrategySignal(self._a, "stub")


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


def _engine(**over) -> TradingEngine:
    base = dict(
        trading_mode="paper",
        max_open_positions=5,
        default_stop_loss_pct=2.0,
        default_take_profit_pct=4.0,
        paper_starting_balance=10_000.0,
        min_signal_confidence=0.1,
        # These tests isolate the regime/override logic, so keep the separate
        # validation gate OFF here; it has its own dedicated tests below.
        require_strategy_validation=False,
    )
    base.update(over)
    return TradingEngine(Settings(**base), _Conn(), user_id=None)


def _frame(closes, *, high=None, low=None) -> pd.DataFrame:
    n = len(closes)
    highs = high if high is not None else [c * 1.001 for c in closes]
    lows = low if low is not None else [c * 0.999 for c in closes]
    return pd.DataFrame(
        {
            "timestamp": range(n),
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [1.0] * n,
        }
    )


_UP = _frame([100 + i * 0.8 for i in range(200)])       # clean bull regime
_DOWN = _frame([300 - i * 0.8 for i in range(220)])     # deep bear regime


def _stub(monkeypatch, action: str) -> None:
    monkeypatch.setattr(engine_mod, "build_strategy", lambda name, **kw: _StubStrat(action))


# ---- persistence ----------------------------------------------------


def test_set_and_remove_strategy_config_persists(db):
    eng = _engine()
    cfg = {"strategy": "ma_cross", "timeframe": "1h", "params": {"fast": 5, "slow": 20}}
    eng.set_strategy_config(db, "btc/usdt", cfg)
    # In-memory (uppercased key) AND persisted to the KV store.
    assert eng.strategy_configs["BTC/USDT"] == cfg
    assert load_strategy_configs(db, None)["BTC/USDT"]["params"]["fast"] == 5
    assert eng.remove_strategy_config(db, "BTC/USDT") is True
    assert "BTC/USDT" not in eng.strategy_configs
    assert "BTC/USDT" not in load_strategy_configs(db, None)
    assert eng.remove_strategy_config(db, "BTC/USDT") is False  # already gone


def test_restore_state_loads_saved_strategies(db):
    eng = _engine()
    eng.set_strategy_config(db, "ETH/USDT", {"strategy": "rsi", "timeframe": "4h", "params": {}})
    # A fresh engine restoring the same DB must see the saved strategy.
    eng2 = _engine()
    eng2.restore_state(db)
    assert "ETH/USDT" in eng2.strategy_configs


# ---- the bot trading with a saved strategy --------------------------


def test_saved_buy_taken_in_bull_regime(db, monkeypatch):
    eng = _engine(use_saved_strategy=True)
    eng.strategy_configs["BTC/USDT"] = {"strategy": "ma_cross", "params": {}}
    _stub(monkeypatch, "buy")
    analysis = MarketAnalyzer().analyze(_UP, "BTC/USDT")
    assert eng._bear_regime(analysis) is False
    out = eng._apply_saved_strategy("BTC/USDT", _UP, analysis)
    assert out.verdict == "buy"
    assert out.confidence == 1.0


def test_saved_buy_suppressed_in_bear_regime(db, monkeypatch):
    # Capital preservation is non-negotiable: never long a broken market just
    # because a saved strategy fired a buy.
    eng = _engine(use_saved_strategy=True)
    eng.strategy_configs["BTC/USDT"] = {"strategy": "ma_cross", "params": {}}
    _stub(monkeypatch, "buy")
    analysis = MarketAnalyzer().analyze(_DOWN, "BTC/USDT")
    assert eng._bear_regime(analysis) is True
    out = eng._apply_saved_strategy("BTC/USDT", _DOWN, analysis)
    assert out.verdict == "hold"


def test_saved_sell_is_always_honoured(db, monkeypatch):
    # Reducing risk is never blocked, even in a bull regime.
    eng = _engine(use_saved_strategy=True)
    eng.strategy_configs["BTC/USDT"] = {"strategy": "ma_cross", "params": {}}
    _stub(monkeypatch, "sell")
    analysis = MarketAnalyzer().analyze(_UP, "BTC/USDT")
    out = eng._apply_saved_strategy("BTC/USDT", _UP, analysis)
    assert out.verdict == "sell"
    assert out.confidence == 1.0


def test_not_opted_in_keeps_analyzer_verdict(db, monkeypatch):
    eng = _engine(use_saved_strategy=False)  # opt-out
    eng.strategy_configs["BTC/USDT"] = {"strategy": "ma_cross", "params": {}}
    _stub(monkeypatch, "sell")
    analysis = MarketAnalyzer().analyze(_UP, "BTC/USDT")
    before = analysis.verdict
    out = eng._apply_saved_strategy("BTC/USDT", _UP, analysis)
    assert out.verdict == before  # untouched by the saved strategy


def test_no_saved_config_keeps_analyzer_verdict(db, monkeypatch):
    eng = _engine(use_saved_strategy=True)  # opted in, but nothing saved
    _stub(monkeypatch, "sell")
    analysis = MarketAnalyzer().analyze(_UP, "BTC/USDT")
    before = analysis.verdict
    out = eng._apply_saved_strategy("BTC/USDT", _UP, analysis)
    assert out.verdict == before


def test_broken_saved_strategy_falls_back_to_analyzer(db, monkeypatch):
    # A bad/unknown saved strategy must never crash the decision path — it falls
    # back to the analyzer verdict rather than fabricating a trade.
    eng = _engine(use_saved_strategy=True)
    eng.strategy_configs["BTC/USDT"] = {"strategy": "does-not-exist", "params": {}}

    def _boom(name, **kw):
        raise ValueError("unknown strategy")

    monkeypatch.setattr(engine_mod, "build_strategy", _boom)
    analysis = MarketAnalyzer().analyze(_UP, "BTC/USDT")
    before = analysis.verdict
    out = eng._apply_saved_strategy("BTC/USDT", _UP, analysis)
    assert out.verdict == before


# ---- validation gate (the honest stand-in for "make it 95% correct") -----
# We never fabricate an accuracy figure. Instead, a saved strategy only earns
# the right to drive a NEW long once its REAL, out-of-sample backtest metrics
# clear the operator's thresholds. Unproven -> defer to the analyzer.


_GOOD_METRICS = {
    "validation_return_pct": 12.0,   # out-of-sample split present and positive
    "win_rate_pct": 58.0,
    "num_trades": 30,
    "max_drawdown_pct": 9.0,
    "overfit_gap_pct": 4.0,
}


def test_validate_passes_with_real_oos_metrics():
    eng = _engine(
        require_strategy_validation=True,
        strategy_min_return_pct=0.0,
        strategy_min_trades=5,
    )
    ok, reason, detail = eng._validate_saved_strategy(
        {"strategy": "ma_cross", "params": {}, "metrics": _GOOD_METRICS}
    )
    assert ok is True
    assert reason == "validated"
    assert detail["return_basis"] == "out-of-sample"


def test_validate_fails_closed_without_metrics():
    # No metrics at all -> unproven -> refuse (fail CLOSED), never assume good.
    eng = _engine(require_strategy_validation=True)
    ok, reason, detail = eng._validate_saved_strategy({"strategy": "ma_cross"})
    assert ok is False
    assert "no backtest metrics" in reason
    assert detail == {}


def test_validate_fails_on_in_sample_only():
    # A strategy with only an in-sample return (no out-of-sample split) is not
    # trustworthy for real money — the gate demands an out-of-sample number.
    eng = _engine(require_strategy_validation=True, strategy_min_return_pct=0.0)
    ok, reason, _ = eng._validate_saved_strategy(
        {"strategy": "ma_cross", "metrics": {"total_return_pct": 40.0, "num_trades": 20}}
    )
    assert ok is False
    assert "out-of-sample" in reason


def test_validate_fails_when_below_thresholds():
    eng = _engine(
        require_strategy_validation=True,
        strategy_min_return_pct=20.0,   # demand more than the strategy delivered
        strategy_min_win_rate_pct=70.0,
        strategy_min_trades=100,
    )
    ok, reason, _ = eng._validate_saved_strategy(
        {"strategy": "ma_cross", "metrics": _GOOD_METRICS}
    )
    assert ok is False
    assert "return" in reason and "win rate" in reason and "trades" in reason


def test_gate_on_defers_unvalidated_buy_to_analyzer(db, monkeypatch):
    # Opted in + gate ON + no metrics: a saved BUY is NOT taken; we fall back to
    # the deterministic analyzer verdict instead of trading an unproven edge.
    eng = _engine(use_saved_strategy=True, require_strategy_validation=True)
    eng.strategy_configs["BTC/USDT"] = {"strategy": "ma_cross", "params": {}}
    _stub(monkeypatch, "buy")
    analysis = MarketAnalyzer().analyze(_UP, "BTC/USDT")
    before = analysis.verdict
    out = eng._apply_saved_strategy("BTC/USDT", _UP, analysis)
    assert out.verdict == before          # analyzer keeps the wheel
    assert out.confidence != 1.0          # not the forced saved-strategy confidence
    assert "not validated" in out.summary


def test_gate_on_takes_validated_buy(db, monkeypatch):
    # Same setup but WITH passing out-of-sample metrics -> the saved BUY drives.
    eng = _engine(
        use_saved_strategy=True,
        require_strategy_validation=True,
        strategy_min_return_pct=0.0,
        strategy_min_trades=5,
    )
    eng.strategy_configs["BTC/USDT"] = {
        "strategy": "ma_cross", "params": {}, "metrics": _GOOD_METRICS
    }
    _stub(monkeypatch, "buy")
    analysis = MarketAnalyzer().analyze(_UP, "BTC/USDT")
    out = eng._apply_saved_strategy("BTC/USDT", _UP, analysis)
    assert out.verdict == "buy"
    assert out.confidence == 1.0


def test_gate_never_blocks_a_sell(db, monkeypatch):
    # Reducing risk is never gated: an unvalidated saved SELL is still honoured.
    eng = _engine(use_saved_strategy=True, require_strategy_validation=True)
    eng.strategy_configs["BTC/USDT"] = {"strategy": "ma_cross", "params": {}}
    _stub(monkeypatch, "sell")
    analysis = MarketAnalyzer().analyze(_UP, "BTC/USDT")
    out = eng._apply_saved_strategy("BTC/USDT", _UP, analysis)
    assert out.verdict == "sell"
    assert out.confidence == 1.0
