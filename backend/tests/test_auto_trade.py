"""Tests for autonomous trading, including multi-timeframe confirmation.

Use a fake connector so no network or credentials are needed: it returns a
different synthetic candle series per timeframe, letting us drive the analyzer
to a buy on one timeframe and a sell on another and assert the engine refuses
to trade against the higher timeframe.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.engine import TradingEngine
from app.models import SignalLog, Trade, TradeStatus


def _uptrend(n: int = 200) -> list[list[float]]:
    # timestamp, open, high, low, close, volume
    return [[i, 100 + i * 0.8, (100 + i * 0.8) * 1.001,
             (100 + i * 0.8) * 0.999, 100 + i * 0.8, 1.0] for i in range(n)]


def _downtrend(n: int = 200) -> list[list[float]]:
    return [[i, 300 - i * 0.8, (300 - i * 0.8) * 1.001,
             (300 - i * 0.8) * 0.999, 300 - i * 0.8, 1.0] for i in range(n)]


class FakeConnector:
    """Returns candles keyed by timeframe; supports paper execution only."""

    def __init__(self, by_timeframe: dict[str, list[list[float]]]):
        self._by_tf = by_timeframe
        self.has_credentials = False

    def reload(self, settings):  # noqa: D401 - matches connector interface
        pass

    def fetch_ohlcv(self, symbol, timeframe, limit):
        return self._by_tf[timeframe]

    def fetch_price(self, symbol):
        return float(self._by_tf["1h"][-1][4])


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


def test_confirm_timeframe_blocks_buy_when_higher_tf_bearish(db):
    # 1h says buy (uptrend) but 4h says sell (downtrend) -> buy must be blocked.
    conn = FakeConnector({"1h": _uptrend(), "4h": _downtrend()})
    eng = _engine(conn, auto_confirm_timeframe="4h")
    ok, msg = eng.auto_trade_symbol(db, "BTC/USDT", "1h")
    assert ok is False
    assert "blocked" in msg.lower()
    # No trade was opened.
    assert db.query(Trade).count() == 0


def test_confirm_timeframe_allows_buy_when_higher_tf_agrees(db):
    # Both timeframes bullish -> buy proceeds and a paper trade opens.
    conn = FakeConnector({"1h": _uptrend(), "4h": _uptrend()})
    eng = _engine(conn, auto_confirm_timeframe="4h")
    ok, msg = eng.auto_trade_symbol(db, "BTC/USDT", "1h")
    assert ok is True, msg
    open_trades = db.query(Trade).filter(
        Trade.status == TradeStatus.open.value
    ).all()
    assert len(open_trades) == 1
    assert open_trades[0].side == "buy"


def test_no_confirm_timeframe_uses_single_timeframe(db):
    # Disabled (blank) -> the higher timeframe is never consulted; 1h buy stands.
    conn = FakeConnector({"1h": _uptrend()})
    eng = _engine(conn, auto_confirm_timeframe="")
    ok, msg = eng.auto_trade_symbol(db, "BTC/USDT", "1h")
    assert ok is True, msg
    assert db.query(Trade).count() == 1


def test_autonomous_verdict_is_logged_and_deduped(db):
    # The brain's real verdict is persisted as an analyzer signal row so the
    # Signals tab shows an honest timeline. A confident buy that acts is logged
    # once; a repeat tick with the SAME verdict must NOT write a duplicate row.
    conn = FakeConnector({"1h": _uptrend()})
    eng = _engine(conn, auto_confirm_timeframe="")
    ok, msg = eng.auto_trade_symbol(db, "BTC/USDT", "1h")
    assert ok is True, msg
    rows = db.query(SignalLog).filter(SignalLog.source == "analyzer").all()
    assert len(rows) == 1
    assert rows[0].action == "buy"
    assert rows[0].accepted == 1
    assert rows[0].symbol == "BTC/USDT"
    assert rows[0].raw  # the real analysis snapshot, not empty/fabricated
    # Second tick: verdict is still "buy" (now already long) -> de-duped.
    eng.auto_trade_symbol(db, "BTC/USDT", "1h")
    assert db.query(SignalLog).filter(SignalLog.source == "analyzer").count() == 1


def test_hold_verdict_is_logged_but_not_acted(db):
    # Too few bars -> the analyzer holds. A hold is still recorded (accepted=0)
    # so the timeline reflects that the bot looked and chose to stand aside.
    flat = [[i, 100.0, 100.1, 99.9, 100.0, 1.0] for i in range(10)]
    conn = FakeConnector({"1h": flat})
    eng = _engine(conn, auto_confirm_timeframe="")
    ok, _ = eng.auto_trade_symbol(db, "BTC/USDT", "1h")
    assert ok is False
    rows = db.query(SignalLog).filter(SignalLog.source == "analyzer").all()
    assert len(rows) == 1
    assert rows[0].action == "hold"
    assert rows[0].accepted == 0
    assert db.query(Trade).count() == 0


def test_observe_symbol_logs_without_trading(db):
    # Autonomous execution off: observe_symbol must record the brain's live read
    # (accepted=0) but NEVER place a trade, and de-dupe on an unchanged verdict.
    conn = FakeConnector({"1h": _uptrend()})
    eng = _engine(conn, auto_confirm_timeframe="")
    ok, msg = eng.observe_symbol(db, "BTC/USDT", "1h")
    assert ok is True, msg
    rows = db.query(SignalLog).filter(SignalLog.source == "analyzer").all()
    assert len(rows) == 1
    assert rows[0].action == "buy"
    assert rows[0].accepted == 0  # observed, not acted
    assert rows[0].confidence is not None and 0.0 <= rows[0].confidence <= 1.0
    assert db.query(Trade).count() == 0  # nothing was traded
    # Same verdict again -> de-duped, still no trade.
    eng.observe_symbol(db, "BTC/USDT", "1h")
    assert db.query(SignalLog).filter(SignalLog.source == "analyzer").count() == 1
    assert db.query(Trade).count() == 0


def test_ai_veto_blocks_autonomous_buy(db):
    # With ai_trade_confirm on, a vetoing AI blocks the ENTRY; the vetoed verdict
    # is still logged (accepted=0) and no trade opens. AI can only block new risk.
    conn = FakeConnector({"1h": _uptrend()})
    eng = _engine(conn, auto_confirm_timeframe="", ai_trade_confirm=True)

    class _VetoAI:
        available = True

        def confirm_trade(self, analysis):
            return False, "AI veto: setup too thin"

    eng.ai = _VetoAI()
    ok, msg = eng.auto_trade_symbol(db, "BTC/USDT", "1h")
    assert ok is False
    assert "veto" in msg.lower()
    assert db.query(Trade).count() == 0
    row = db.query(SignalLog).filter(SignalLog.source == "analyzer").one()
    assert row.action == "buy"
    assert row.accepted == 0


def test_ai_approve_allows_autonomous_buy(db):
    # An approving AI leaves the deterministic buy intact -> the paper trade opens.
    conn = FakeConnector({"1h": _uptrend()})
    eng = _engine(conn, auto_confirm_timeframe="", ai_trade_confirm=True)

    class _OkAI:
        available = True

        def confirm_trade(self, analysis):
            return True, "AI approved: clean trend"

    eng.ai = _OkAI()
    ok, msg = eng.auto_trade_symbol(db, "BTC/USDT", "1h")
    assert ok is True, msg
    open_trades = db.query(Trade).filter(
        Trade.status == TradeStatus.open.value
    ).all()
    assert len(open_trades) == 1
    assert open_trades[0].side == "buy"


# Every analyzer factor name the engine can emit. The live 'signal' event must
# only ever carry names from THIS set — proof the "watch the bot think" chart is
# fed the analyzer's real reasoning, never an invented factor.
_KNOWN_FACTORS = {
    "regime", "trend", "rsi", "macd", "momentum", "volatility", "shock", "volume",
}


def test_autonomous_signal_emit_carries_timeframe_and_real_factors(db):
    # The live 'signal' event that drives the "watch the bot think" chart must
    # carry the decision's real timeframe and its REAL analyzer factors (each with
    # name/signal/weight) — sent verbatim so the UI can show the true "why" and
    # light up the matching indicators. Nothing here is fabricated.
    conn = FakeConnector({"1h": _uptrend(), "15m": _uptrend()})
    eng = _engine(conn, auto_confirm_timeframe="")
    captured: list[tuple[str, dict]] = []
    # Capture what the engine would broadcast (no real socket/loop in tests).
    eng._emit = lambda event, payload: captured.append((event, payload))  # type: ignore[method-assign]
    # Use 15m (not the "1h" default) so a correct timeframe can only come from the
    # argument being threaded through, never a hardcoded fallback.
    ok, msg = eng.auto_trade_symbol(db, "BTC/USDT", "15m")
    assert ok is True, msg
    signal_emits = [p for (e, p) in captured if e == "signal"]
    assert len(signal_emits) == 1
    payload = signal_emits[0]
    assert payload["symbol"] == "BTC/USDT"
    assert payload["action"] == "buy"
    assert payload["timeframe"] == "15m"
    factors = payload["factors"]
    assert isinstance(factors, list) and factors  # real reasoning, not empty
    names = {f["name"] for f in factors}
    assert names and names.issubset(_KNOWN_FACTORS)  # only real analyzer factors
    for f in factors:
        assert set(f) == {"name", "signal", "weight"}
        assert f["signal"] in ("buy", "sell", "hold")
        assert isinstance(f["weight"], (int, float))


def test_observe_emit_carries_factors_without_acting(db):
    # Observe mode (autonomous execution off) still emits the enriched signal so
    # the chart can visualise the brain's read — but accepted is False (no trade).
    conn = FakeConnector({"1h": _uptrend()})
    eng = _engine(conn, auto_confirm_timeframe="")
    captured: list[tuple[str, dict]] = []
    eng._emit = lambda event, payload: captured.append((event, payload))  # type: ignore[method-assign]
    ok, _ = eng.observe_symbol(db, "BTC/USDT", "1h")
    assert ok is True
    payload = next(p for (e, p) in captured if e == "signal")
    assert payload["accepted"] is False
    assert payload["timeframe"] == "1h"
    assert {f["name"] for f in payload["factors"]}.issubset(_KNOWN_FACTORS)
