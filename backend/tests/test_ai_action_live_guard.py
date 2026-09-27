"""H4 — the AI must never AUTO-APPLY risk changes or start live trading.

The assistant can PROPOSE an action; ``_normalize_proposed_action`` decides
whether autopilot may apply it silently (``auto=True``) or it must wait for a
human Confirm tap (``auto=False``). On real money the bar is high:
  * a change to any risk-/autonomy-governing setting is NEVER auto-applied live,
  * STARTING the bot is never auto-applied live (stopping always may),
  * a live ORDER is never auto-applied (pre-existing rule, guarded here too).
Paper is a safe sandbox, so autopilot may still auto-apply there.
"""
from __future__ import annotations

import types

from app.main import _normalize_proposed_action


def _eng(autopilot: bool, live: bool):
    return types.SimpleNamespace(
        settings=types.SimpleNamespace(
            ai_autopilot_enabled=autopilot, is_live=live
        )
    )


def _settings(changes: dict) -> dict:
    return {"type": "settings", "changes": changes, "reason": "test"}


# ---- risk-loosening settings ------------------------------------------------


def test_live_risk_setting_change_requires_confirm_even_on_autopilot():
    out = _normalize_proposed_action(
        _settings({"risk_per_trade_pct": 5.0}), _eng(autopilot=True, live=True)
    )
    assert out is not None
    assert out["auto"] is False  # real money -> human must confirm


def test_live_enabling_autonomy_requires_confirm():
    out = _normalize_proposed_action(
        _settings({"auto_trade_enabled": True}), _eng(autopilot=True, live=True)
    )
    assert out["auto"] is False


def test_live_disabling_validation_gate_requires_confirm():
    out = _normalize_proposed_action(
        _settings({"require_strategy_validation": False}),
        _eng(autopilot=True, live=True),
    )
    assert out["auto"] is False


def test_paper_risk_setting_change_may_autopilot():
    out = _normalize_proposed_action(
        _settings({"risk_per_trade_pct": 5.0}), _eng(autopilot=True, live=False)
    )
    assert out["auto"] is True  # paper is a safe sandbox


def test_live_non_risk_setting_may_autopilot():
    # auto_timeframe steers analysis, not capital preservation -> still auto.
    out = _normalize_proposed_action(
        _settings({"auto_timeframe": "4h"}), _eng(autopilot=True, live=True)
    )
    assert out["auto"] is True


def test_autopilot_off_never_auto_applies_settings():
    out = _normalize_proposed_action(
        _settings({"auto_timeframe": "4h"}), _eng(autopilot=False, live=False)
    )
    assert out["auto"] is False


# ---- bot start/stop ---------------------------------------------------------


def test_live_bot_start_requires_confirm():
    out = _normalize_proposed_action(
        {"type": "bot", "state": "start"}, _eng(autopilot=True, live=True)
    )
    assert out["auto"] is False


def test_live_bot_stop_always_safe_to_autopilot():
    out = _normalize_proposed_action(
        {"type": "bot", "state": "stop"}, _eng(autopilot=True, live=True)
    )
    assert out["auto"] is True  # stopping only reduces activity


def test_paper_bot_start_may_autopilot():
    out = _normalize_proposed_action(
        {"type": "bot", "state": "start"}, _eng(autopilot=True, live=False)
    )
    assert out["auto"] is True


# ---- live order (pre-existing rule, regression-guarded) ---------------------


def test_live_order_never_auto_applied():
    out = _normalize_proposed_action(
        {"type": "order", "side": "buy", "symbol": "BTC/USDT"},
        _eng(autopilot=True, live=True),
    )
    assert out["auto"] is False


def test_paper_order_may_autopilot():
    out = _normalize_proposed_action(
        {"type": "order", "side": "buy", "symbol": "BTC/USDT"},
        _eng(autopilot=True, live=False),
    )
    assert out["auto"] is True
