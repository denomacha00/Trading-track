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
    from app.ict import analyze_ict

    az = MarketAnalyzer()  # confluence on by default
    closed = _candles(150)
    # The honest, closed-bar decision — with the SAME closed-bar ICT read that
    # analyze_live votes on (price=None, so the zone is a function of closed bars).
    ict_ref = analyze_ict(closed, price=None)
    ref = az.analyze(closed, ict=ict_ref)
    forming_px = float(closed["close"].iloc[-1]) * 1.5
    full = _with_forming(closed, forming_px)

    live, closed_out, live_ict = az.analyze_live(full)

    # Decision is computed on the CLOSED frame — identical to analysing without
    # the forming bar at all (ICT included).
    assert live.verdict == ref.verdict
    assert live.score == pytest.approx(ref.score)
    assert live.confidence == pytest.approx(ref.confidence)
    assert live.atr == pytest.approx(ref.atr)
    # The ICT read was computed and returned for the display lens (no repaint).
    assert live_ict is not None
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
    """An extreme forming spike/crash must NOT change the decision — only price.

    Holds WITH ICT confluence on: analyze_live measures the premium/discount zone
    against the last closed price (price=None), so no forming tick can flip a vote.
    """
    from app.ict import analyze_ict

    az = MarketAnalyzer()
    closed = _candles(150)
    ref = az.analyze(closed, ict=analyze_ict(closed, price=None))
    base = float(closed["close"].iloc[-1])

    for forming_px in (base * 3.0, base * 0.3, base, base * 1.002):
        live, _, _ = az.analyze_live(_with_forming(closed, forming_px))
        assert live.verdict == ref.verdict, forming_px
        assert live.score == pytest.approx(ref.score), forming_px
        assert live.price == pytest.approx(forming_px), forming_px


def test_analyze_live_tolerates_tiny_frame():
    az = MarketAnalyzer()
    one = _candles(1)
    live, closed_out, _ict = az.analyze_live(one)
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
    from app.ict import analyze_ict

    az = MarketAnalyzer(min_confidence=0.1)  # confluence on, matching the engine
    closed = _candles(150)
    # Reference includes the SAME closed-bar ICT read the engine's analyze_live
    # feeds the verdict, so this stays an apples-to-apples check of the drop.
    ict_ref = analyze_ict(closed, "BTC/USDT", price=None)
    ref = az.analyze(closed, "BTC/USDT", ict=ict_ref)
    base = float(closed["close"].iloc[-1])

    # A wild forming spike would flip the verdict (shock guard) if it weren't
    # dropped; analyze_symbol must ignore it for the decision but report it.
    full = _with_forming(closed, base * 4.0)
    eng = _engine(_CandleConn(full.values.tolist()))
    got = eng.analyze_symbol("BTC/USDT")
    assert got.verdict == ref.verdict
    assert got.score == pytest.approx(ref.score)
    assert got.price == pytest.approx(base * 4.0)  # live price kept for display


# ---- ICT confluence: real levels VOTE, and never repaint ---------------------


def test_ict_factors_map_structure_zone_and_sweep():
    """_ict_factors turns a computed ICT read into weighted votes — it reads the
    IctAnalysis straight (no invention) and maps direction/zone/reaction to signs."""
    from types import SimpleNamespace

    az = MarketAnalyzer()
    ict = SimpleNamespace(
        events=[SimpleNamespace(index=140, kind="CHoCH", direction="bull",
                                level=155.0, displacement=True)],
        trend="bull",
        dealing_range=SimpleNamespace(zone="discount", in_ote=True),
        sweeps=[SimpleNamespace(index=149, side="sell-side", level=150.0,
                                reaction="bull")],
    )
    facs = {f.name: f for f in az._ict_factors(ict, n_bars=150)}
    assert facs["ict-structure"].signal == "buy"     # bullish break
    assert facs["ict-structure"].weight == 0.35       # displacement CHoCH => MSS
    assert facs["ict-zone"].signal == "buy"           # discount favours longs
    assert facs["ict-zone"].weight == 0.22            # inside the OTE band
    assert facs["ict-sweep"].signal == "buy"          # fresh sell-side sweep, bull snap

    # A bearish read flips every sign — nothing is hard-coded to "buy".
    ict.events[0].direction = "bear"
    ict.dealing_range.zone = "premium"
    ict.sweeps[0].reaction = "bear"
    facs = {f.name: f for f in az._ict_factors(ict, n_bars=150)}
    assert facs["ict-structure"].signal == "sell"
    assert facs["ict-zone"].signal == "sell"
    assert facs["ict-sweep"].signal == "sell"

    # A stale structure break (>15 bars back) and a stale sweep (>5 bars) fade out.
    ict.events[0].index = 100          # 49 bars back -> half weight
    ict.sweeps[0].index = 100          # not fresh -> no sweep vote at all
    facs = {f.name: f for f in az._ict_factors(ict, n_bars=150)}
    assert facs["ict-structure"].weight == pytest.approx(0.35 * 0.5)
    assert "ict-sweep" not in facs


def test_ict_confluence_contributes_and_toggles_off():
    """With confluence on, a real ICT read votes as ict-* factors; off, it's silent
    (proving the wiring is live, not dead code)."""
    from app.ict import analyze_ict

    closed = _candles(150)
    ict = analyze_ict(closed, "BTC/USDT", price=None)

    on = MarketAnalyzer(ict_confluence=True).analyze(closed, "BTC/USDT", ict=ict)
    off = MarketAnalyzer(ict_confluence=False).analyze(closed, "BTC/USDT", ict=ict)

    assert any(f.name.startswith("ict-") for f in on.factors), (
        "ICT confluence produced no votes on a trending frame"
    )
    # Disabled: not a single ict-* factor leaks into the verdict.
    assert not any(f.name.startswith("ict-") for f in off.factors)
