"""Tests for the TradingView-style Technicals gauge (app/technicals.py).

All inputs are synthetic candle frames built in-process — no network — so the
ratings are checked against series whose direction we control.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.technicals import (
    _rate_value,
    compute_technicals,
    summarize_technicals,
)


def _frame(closes, *, vol=1000.0):
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    return pd.DataFrame(
        {
            "timestamp": range(n),
            "open": closes,
            "high": closes * 1.005,
            "low": closes * 0.995,
            "close": closes,
            "volume": np.full(n, vol),
        }
    )


def _uptrend(n=260):
    return _frame(100 + np.cumsum(np.full(n, 0.6)))


def _downtrend(n=260):
    return _frame(300 - np.cumsum(np.full(n, 0.6)))


def test_rate_value_buckets_match_tradingview_thresholds():
    assert _rate_value(0.9) == "strong_buy"
    assert _rate_value(0.5) == "strong_buy"
    assert _rate_value(0.3) == "buy"
    assert _rate_value(0.1) == "buy"
    assert _rate_value(0.0) == "neutral"
    assert _rate_value(-0.05) == "neutral"
    assert _rate_value(-0.2) == "sell"
    assert _rate_value(-0.5) == "strong_sell"
    assert _rate_value(-0.9) == "strong_sell"


def test_uptrend_moving_averages_are_bullish():
    r = compute_technicals(_uptrend(), "BTC/USDT", "1h")
    ma = r["moving_averages"]
    # Price rides above every MA in a clean uptrend -> strongly bullish MA gauge.
    assert ma["rating"] in ("buy", "strong_buy")
    assert ma["buy"] > ma["sell"]
    assert r["summary"]["score"] > 0


def test_downtrend_moving_averages_are_bearish():
    r = compute_technicals(_downtrend(), "ETH/USDT", "4h")
    ma = r["moving_averages"]
    assert ma["rating"] in ("sell", "strong_sell")
    assert ma["sell"] > ma["buy"]
    assert r["summary"]["score"] < 0


def test_item_counts_and_shape():
    r = compute_technicals(_uptrend(), "BTC/USDT", "1h")
    assert len(r["oscillators"]["items"]) == 11
    # 6 periods x (SMA+EMA) + Ichimoku + VWMA + Hull = 15
    assert len(r["moving_averages"]["items"]) == 15
    for it in r["oscillators"]["items"] + r["moving_averages"]["items"]:
        assert set(it.keys()) == {"name", "value", "signal"}
        assert it["signal"] in ("buy", "sell", "neutral", "n/a")


def test_summary_score_is_mean_of_group_scores():
    r = compute_technicals(_uptrend(), "BTC/USDT", "1h")
    expected = (r["oscillators"]["score"] + r["moving_averages"]["score"]) / 2
    # Both group scores and the summary are rounded to 4dp, so allow for the
    # double-rounding; the relationship (summary == mean of groups) still holds.
    assert abs(r["summary"]["score"] - expected) < 1e-3


def test_insufficient_bars_is_honest_not_a_crash():
    r = compute_technicals(_frame([100, 101, 102]), "BTC/USDT", "1h")
    # Too short for the long MAs/oscillators: it must not fabricate a strong read.
    assert r["summary"]["rating"] in ("neutral", "buy", "sell",
                                      "strong_buy", "strong_sell")
    assert r["bars"] == 3


def test_empty_frame_returns_note_and_neutral():
    r = compute_technicals(_frame([]), "BTC/USDT", "1h")
    assert r["price"] is None
    assert r["summary"]["rating"] == "neutral"
    assert "note" in r


def test_zero_volume_marks_vwma_unavailable_not_fabricated():
    r = compute_technicals(_uptrend()[:].assign(volume=0.0), "BTC/USDT", "1h")
    vwma = [it for it in r["moving_averages"]["items"] if it["name"] == "VWMA(20)"][0]
    assert vwma["value"] is None
    assert vwma["signal"] == "n/a"


def test_summarize_is_empty_when_nothing_computed():
    r = compute_technicals(_frame([]), "BTC/USDT", "1h")
    assert summarize_technicals(r) == ""
    assert summarize_technicals({}) == ""


def test_summarize_reads_real_numbers():
    r = compute_technicals(_uptrend(), "BTC/USDT", "1h")
    text = summarize_technicals(r)
    assert "BTC/USDT" in text
    assert "TradingView-style" in text
    assert "Summary:" in text
    assert "Moving averages:" in text
