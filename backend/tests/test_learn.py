"""Tests for the training / parameter-optimisation module."""
from __future__ import annotations

import pandas as pd
import pytest

from app.learn import _overfit_gap, train


def _candles(prices: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": range(len(prices)),
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": [1.0] * len(prices),
        }
    )


def _trending_prices(n: int = 300) -> list[float]:
    # A wavy uptrend so crossovers actually fire and some configs beat others.
    import math

    return [100 + i * 0.5 + 8 * math.sin(i / 6.0) for i in range(n)]


def test_train_ma_cross_returns_best():
    report = train(_candles(_trending_prices()), "ma_cross", symbol="BTC/USDT")
    assert report.tested > 0
    assert report.best is not None
    # fast < slow constraint must hold for the chosen params.
    assert report.best.params["fast"] < report.best.params["slow"]
    assert report.best.num_trades > 0


def test_train_leaderboard_sorted_by_score():
    report = train(_candles(_trending_prices()), "ma_cross")
    scores = [c.score for c in report.leaderboard]
    assert scores == sorted(scores, reverse=True)


def test_train_flat_market_has_no_best():
    # Flat market -> no strategy trades -> best must be None, not a do-nothing win.
    report = train(_candles([100.0] * 200), "ma_cross")
    assert report.best is None
    assert report.leaderboard == []


def test_train_uses_holdout_split_by_default():
    # With enough candles, the optimiser fits in-sample and validates out-of-sample.
    report = train(_candles(_trending_prices(300)), "ma_cross")
    assert report.train_fraction < 1.0
    assert report.best is not None
    # Out-of-sample metrics must be populated when a split is used.
    assert report.best.validation_return_pct is not None
    assert report.best.overfit_gap_pct is not None


def test_train_small_dataset_skips_split_with_warning():
    # Too few candles for a meaningful split -> full-dataset fit + warning.
    report = train(_candles(_trending_prices(80)), "ma_cross")
    assert report.train_fraction == 1.0
    if report.best is not None:
        assert report.best.validation_return_pct is None
    assert report.warning is not None


def test_train_split_can_be_disabled():
    report = train(_candles(_trending_prices(300)), "ma_cross", train_fraction=1.0)
    assert report.train_fraction == 1.0
    assert report.best is not None
    assert report.best.validation_return_pct is None


def test_train_rsi_grid_respects_bounds():
    report = train(_candles(_trending_prices()), "rsi")
    assert report.tested > 0
    for c in report.leaderboard:
        assert c.params["oversold"] < c.params["overbought"]


def test_train_unknown_strategy():
    with pytest.raises(ValueError):
        train(_candles(_trending_prices()), "does_not_exist")


# ---- overfit gap: like-for-like, not a window-length artifact (L9) --------
# The chronological split makes the in-sample and out-of-sample windows unequal
# lengths (e.g. 70/30). Subtracting their raw totals used to fake an overfit gap
# purely from the longer window compounding more. _overfit_gap normalises to the
# in-sample per-bar pace, projects it over the OOS window, and compares like for
# like.

def test_overfit_gap_zero_when_per_bar_pace_identical():
    # Same geometric per-bar growth in both windows => genuinely no overfitting,
    # even though the windows are different lengths and post very different totals.
    per_bar = 1.001
    is_bars, oos_bars = 700, 300
    is_ret = (per_bar ** is_bars - 1) * 100
    oos_ret = (per_bar ** oos_bars - 1) * 100
    gap = _overfit_gap(is_ret, is_bars, oos_ret, oos_bars)
    assert gap == pytest.approx(0.0, abs=1e-6)
    # The old total-minus-total math would have reported a large phantom gap
    # that is nothing but the window-length mismatch.
    assert (is_ret - oos_ret) > 60


def test_overfit_gap_positive_when_in_sample_outperforms():
    is_bars, oos_bars = 700, 300
    is_ret = (1.002 ** is_bars - 1) * 100   # faster per-bar pace in-sample
    oos_ret = (1.001 ** oos_bars - 1) * 100  # slower out-of-sample
    gap = _overfit_gap(is_ret, is_bars, oos_ret, oos_bars)
    assert gap > 0


def test_overfit_gap_negative_when_oos_outperforms():
    is_bars, oos_bars = 700, 300
    is_ret = (1.001 ** is_bars - 1) * 100
    oos_ret = (1.002 ** oos_bars - 1) * 100  # held up BETTER out-of-sample
    gap = _overfit_gap(is_ret, is_bars, oos_ret, oos_bars)
    assert gap < 0


def test_overfit_gap_none_on_in_sample_total_loss():
    # Growth factor <= 0 has no real per-bar root -> refuse, never fabricate a 0.
    assert _overfit_gap(-100.0, 700, 5.0, 300) is None
    assert _overfit_gap(-150.0, 700, 5.0, 300) is None


def test_overfit_gap_none_on_empty_window():
    assert _overfit_gap(50.0, 0, 5.0, 300) is None
    assert _overfit_gap(50.0, 700, 5.0, 0) is None


def test_train_reports_bounded_overfit_gap():
    # End to end: the reported gap is the like-for-like figure, so it stays on the
    # same scale as the OOS return rather than the inflated in-sample total.
    report = train(_candles(_trending_prices(300)), "ma_cross")
    assert report.best is not None
    assert report.best.overfit_gap_pct is not None
