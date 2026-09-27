"""H3 — live signals decide on CLOSED bars only (no repaint / no look-ahead).

A live OHLCV feed's most recent candle is still FORMING: its OHLC keeps moving
until the period closes. If the analyzer read that bar, the verdict could flip as
the candle fills in (repaint) and would differ from the backtest that validated
the strategy — live and backtest MUST agree. ``MarketAnalyzer.analyze_live``
therefore decides on the last CLOSED bar and only keeps the forming candle's
close as the display ``price``.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.analysis import MarketAnalyzer

_HOUR_MS = 3_600_000


def _candles(n: int = 150, start: float = 100.0, step: float = 0.4) -> pd.DataFrame:
    """A clean, deterministic uptrend with a mild wiggle (well-defined indicators)."""
    rows = []
    for i in range(n):
        close = start + step * i + 0.2 * ((i % 7) - 3)
        o = close - 0.2
        h = max(o, close) + 0.5
        lo = min(o, close) - 0.5
        rows.append([i * _HOUR_MS, o, h, lo, close, 1000.0])
    return pd.DataFrame(
        rows, columns=["timestamp", "open", "high", "low", "close", "volume"]
    )


def _with_forming(df: pd.DataFrame, close: float) -> pd.DataFrame:
    """Append one still-FORMING bar closing at ``close`` (its range tracks close)."""
    last_ts = int(df["timestamp"].iloc[-1]) + _HOUR_MS
    o = float(df["close"].iloc[-1])
    h = max(o, close) + 0.5
    lo = min(o, close) - 0.5
    row = pd.DataFrame(
        [[last_ts, o, h, lo, close, 1000.0]], columns=list(df.columns)
    )
    return pd.concat([df, row], ignore_index=True)


def test_analyze_live_decides_on_closed_bar_and_keeps_live_price():
    az = MarketAnalyzer()
    closed = _candles(150)
    ref = az.analyze(closed)  # the honest, closed-bar decision
    forming_px = float(closed["close"].iloc[-1]) * 1.5
    full = _with_forming(closed, forming_px)

    live, closed_out = az.analyze_live(full)

    # Decision is computed on the CLOSED frame — identical to analysing without
    # the forming bar at all.
    assert live.verdict == ref.verdict
    assert live.score == pytest.approx(ref.score)
    assert live.confidence == pytest.approx(ref.confidence)
    assert live.atr == pytest.approx(ref.atr)
    # ...but the CURRENT (forming) price is kept for display — NOT the stale
    # closed-bar close the verdict was actually computed on.
    assert live.price == pytest.approx(forming_px)
    assert ref.price != pytest.approx(forming_px)
    # The forming bar was dropped from the frame handed to a saved strategy.
    assert len(closed_out) == len(closed)
    assert float(closed_out["close"].iloc[-1]) == pytest.approx(
        float(closed["close"].iloc[-1])
    )


def test_forming_bar_cannot_repaint_the_verdict():
    """An extreme forming spike/crash must NOT change the decision — only price."""
    az = MarketAnalyzer()
    closed = _candles(150)
    ref = az.analyze(closed)
    base = float(closed["close"].iloc[-1])

    for forming_px in (base * 3.0, base * 0.3, base, base * 1.002):
        live, _ = az.analyze_live(_with_forming(closed, forming_px))
        assert live.verdict == ref.verdict, forming_px
        assert live.score == pytest.approx(ref.score), forming_px
        assert live.price == pytest.approx(forming_px), forming_px


def test_analyze_live_tolerates_tiny_frame():
    az = MarketAnalyzer()
    one = _candles(1)
    live, closed_out = az.analyze_live(one)
    assert live.verdict == "hold"        # not enough data -> safe hold
    assert len(closed_out) == 1          # nothing to drop; frame kept intact


# ---- engine wiring: analyze_symbol must route through analyze_live ----------


class _CandleConn:
    has_credentials = True

    def __init__(self, rows: list[list[float]]):
        self._rows = rows

    @property
    def connected(self) -> bool:
        return True

    def reload(self, settings):
        pass

    def fetch_ohlcv(self, symbol, timeframe, limit):
        return self._rows


def _engine(conn):
    from app.config import Settings
    from app.engine import TradingEngine

    return TradingEngine(
        Settings(trading_mode="paper", min_signal_confidence=0.1),
        conn,
        user_id=None,
    )


def test_engine_analyze_symbol_drops_forming_bar():
    az = MarketAnalyzer(min_confidence=0.1)
    closed = _candles(150)
    ref = az.analyze(closed, "BTC/USDT")
    base = float(closed["close"].iloc[-1])

    # A wild forming spike would flip the verdict (shock guard) if it weren't
    # dropped; analyze_symbol must ignore it for the decision but report it.
    full = _with_forming(closed, base * 4.0)
    eng = _engine(_CandleConn(full.values.tolist()))
    got = eng.analyze_symbol("BTC/USDT")
    assert got.verdict == ref.verdict
    assert got.score == pytest.approx(ref.score)
    assert got.price == pytest.approx(base * 4.0)  # live price kept for display
