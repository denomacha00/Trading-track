"""M8 — risk inputs must fail fast at load, not degrade silently at runtime.

A money bot must never boot with a degenerate risk setting. ``config.Settings``
now rejects a zero/negative stop, a non-positive risk %, an empty position cap,
etc. at construction, so a fat-fingered env var (``DEFAULT_STOP_LOSS_PCT=0``, a
negative ``RISK_PER_TRADE_PCT``) is caught at startup with a clear message rather
than producing silently-wrong sizing and stops on live money. Fields where 0
means "disabled" (trailing stop, exposure/position caps, cooldowns, ATR floor)
must still accept 0 — those cases are asserted here too.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_defaults_construct_cleanly():
    # The shipped defaults must all satisfy the new bounds.
    s = Settings()
    assert s.default_stop_loss_pct > 0
    assert s.risk_per_trade_pct > 0
    assert 0.0 <= s.min_signal_confidence <= 1.0


@pytest.mark.parametrize(
    "field,bad",
    [
        ("default_stop_loss_pct", 0.0),     # 0% stop = stop AT entry -> instant knockout
        ("default_stop_loss_pct", -1.0),
        ("default_take_profit_pct", 0.0),
        ("risk_per_trade_pct", 0.0),
        ("risk_per_trade_pct", -0.5),       # negative risk % inverts sizing
        ("daily_loss_limit_pct", 0.0),
        ("paper_starting_balance", 0.0),    # nothing to size against
        ("paper_starting_balance", -100.0),
        ("max_open_positions", 0),          # bot could never open a trade
        ("ai_timeout_seconds", 0.0),
        ("ai_max_tokens", 0),
        ("monitor_interval_seconds", 0.0),
        ("access_token_ttl_minutes", 0),
        ("reversal_confirm_count", 0),
    ],
)
def test_strictly_positive_fields_reject_non_positive(field, bad):
    with pytest.raises(ValidationError):
        Settings(**{field: bad})


@pytest.mark.parametrize(
    "field,bad",
    [
        ("max_position_pct", -1.0),
        ("max_total_exposure_pct", -1.0),
        ("trailing_stop_pct", -0.1),
        ("max_drawdown_pct", -5.0),
        ("max_spread_pct", -0.01),
        ("reentry_cooldown_minutes", -1.0),
        ("max_consecutive_losses", -1),
        ("atr_stop_mult", -0.5),
        ("paper_taker_fee_pct", -0.1),
        ("strategy_min_win_rate_pct", -1.0),
        ("strategy_max_drawdown_pct", -1.0),
    ],
)
def test_disable_able_fields_reject_negative(field, bad):
    with pytest.raises(ValidationError):
        Settings(**{field: bad})


@pytest.mark.parametrize(
    "field",
    [
        "max_position_pct", "max_total_exposure_pct", "trailing_stop_pct",
        "max_drawdown_pct", "max_spread_pct", "reentry_cooldown_minutes",
        "max_consecutive_losses", "atr_stop_mult", "paper_taker_fee_pct",
        "strategy_min_win_rate_pct", "strategy_max_drawdown_pct",
    ],
)
def test_zero_is_allowed_for_disable_able_fields(field):
    # 0 means "feature off" for these — it must NOT be rejected.
    s = Settings(**{field: 0})
    assert getattr(s, field) == 0


@pytest.mark.parametrize("bad", [-0.01, 1.01, 2.0])
def test_min_signal_confidence_out_of_range_rejected(bad):
    with pytest.raises(ValidationError):
        Settings(min_signal_confidence=bad)


@pytest.mark.parametrize("ok", [0.0, 0.5, 1.0])
def test_min_signal_confidence_in_range_accepted(ok):
    assert Settings(min_signal_confidence=ok).min_signal_confidence == ok


def test_error_message_names_the_offending_field():
    # A beginner reading the boot log should see exactly what to fix.
    with pytest.raises(ValidationError) as exc:
        Settings(default_stop_loss_pct=0.0)
    assert "default_stop_loss_pct" in str(exc.value)
