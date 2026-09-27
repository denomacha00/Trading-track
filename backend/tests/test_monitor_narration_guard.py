"""M10 — the loud monitor may reword an event, never restate its numbers.

``narrate_event`` hands the LLM a deterministic, already-true sentence built from
the account's REAL numbers and asks only for nicer wording. The prompt forbids
changing any figure, but a money bot must not TRUST that — it must VERIFY. These
tests pin the numeric-honesty guard: a rephrase that invents, alters, rounds or
wholesale-drops a figure is rejected so the caller falls back to the exact
deterministic text; a faithful rephrase (commas/currency/percent reframed, sign
re-voiced, scientific notation spelled out) is allowed through.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.ai import (
    AICommentator,
    _narration_numbers,
    _narration_preserves_numbers,
)


def _settings(**over):
    base = dict(
        ai_api_key="sk-test",
        ai_base_url="https://api.example.com/v1",
        ai_model="gpt-4o-mini",
        ai_api_style="auto",
        ai_timeout_seconds=30.0,
        ai_max_tokens=256,
        ai_fallback_api_key="",
        ai_fallback_base_url="https://api.example.com/v1",
        ai_fallback_model="gpt-4o-mini",
        ai_fallback_api_style="auto",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _patch_llm_reply(monkeypatch, content: str):
    """Make every AI POST return exactly ``content`` as the model's message."""
    import app.ai as ai_mod

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": content}}]}

    class _Client:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **k):
            return _Resp()

    monkeypatch.setattr(ai_mod.httpx, "Client", _Client)


# ---- number extraction ----------------------------------------------

def test_extract_handles_commas_currency_percent_and_scientific():
    nums = _narration_numbers("down $1,234.50 (-2.5%) at 6.5e-05, ~200.00")
    assert 1234.5 in nums
    assert 2.5 in nums          # abs of -2.5
    assert 200.0 in nums
    assert any(abs(n - 6.5e-05) < 1e-12 for n in nums)


def test_extract_empty_and_no_numbers():
    assert _narration_numbers("") == []
    assert _narration_numbers("no digits here at all") == []


# ---- the guard: faithful rephrases pass ------------------------------

def test_guard_allows_reworded_same_numbers():
    fact = "BTC/USDT is only 0.85% from your stop at 60000 — 60510 now; it may close out shortly."
    line = "Heads up — BTC/USDT is barely 0.85% off your stop (60,000); it's trading at 60,510 right now."
    assert _narration_preserves_numbers(fact, line) is True


def test_guard_allows_resigned_percent_wording():
    # Deterministic text mixes sign conventions: abs $ amount, signed %.
    fact = "Your ETH/USDT trade is underwater — down 45.20 (-1.5%)."
    line = "Ouch — your ETH/USDT position is down $45.20, about 1.5% in the red."
    assert _narration_preserves_numbers(fact, line) is True


def test_guard_allows_trailing_zero_and_decimal_equivalents():
    fact = "down 145.30 of ~200.00"
    line = "down 145.3 of roughly 200"
    assert _narration_preserves_numbers(fact, line) is True


def test_guard_passes_when_fact_has_no_numbers():
    assert _narration_preserves_numbers("all quiet on your positions", "Nothing moving right now.") is True


# ---- the guard: dishonest rephrases fail -----------------------------

def test_guard_rejects_altered_magnitude():
    fact = "Your ETH/USDT trade is underwater — down 45.20 (-1.5%)."
    line = "Your ETH/USDT trade is underwater — down 452.00 (-15%)."  # 10x worse than reality
    assert _narration_preserves_numbers(fact, line) is False


def test_guard_rejects_invented_number():
    fact = "BTC/USDT is almost at your take-profit 65000 — 64800 now."
    line = "BTC/USDT is almost at your take-profit 65000 — 64800 now, up 3 days straight."
    assert _narration_preserves_numbers(fact, line) is False


def test_guard_rejects_rounded_price():
    # Rounding a money figure IS an alteration on a serious account.
    fact = "1INCH/USDT is only 0.85% from your stop at 0.4523 — 0.4561 now."
    line = "1INCH/USDT is 0.85% from your stop around 0.45 — 0.46 now."
    assert _narration_preserves_numbers(fact, line) is False


def test_guard_rejects_dropping_every_number():
    fact = "You're at 72% of today's loss limit — down 145.30 of ~200.00. Time to ease off."
    line = "You're getting close to today's loss limit — time to ease off."
    assert _narration_preserves_numbers(fact, line) is False


# ---- end-to-end through narrate_event --------------------------------

def test_narrate_event_returns_faithful_rephrase(monkeypatch):
    fact = "BTC/USDT is only 0.85% from your stop at 60000 — 60510 now; it may close out shortly."
    _patch_llm_reply(monkeypatch, "Careful — BTC/USDT is just 0.85% off your stop at 60,000; 60,510 right now.")
    ai = AICommentator(_settings())
    out = ai.narrate_event(fact)
    assert out is not None
    assert "0.85" in out


def test_narrate_event_falls_back_on_fabricated_number(monkeypatch):
    fact = "Your ETH/USDT trade is underwater — down 45.20 (-1.5%)."
    # Model hallucinates a 10x-worse loss — must be refused (None -> caller uses fact).
    _patch_llm_reply(monkeypatch, "Brutal — your ETH/USDT position just cratered, down 452.00.")
    ai = AICommentator(_settings())
    assert ai.narrate_event(fact) is None


def test_narrate_event_falls_back_when_numbers_all_dropped(monkeypatch):
    fact = "You're at 72% of today's loss limit — down 145.30 of ~200.00. Time to ease off."
    _patch_llm_reply(monkeypatch, "You're nearly at today's loss limit — time to ease off.")
    ai = AICommentator(_settings())
    assert ai.narrate_event(fact) is None


def test_narrate_event_unavailable_returns_none():
    ai = AICommentator(_settings(ai_api_key=""))
    assert ai.available is False
    assert ai.narrate_event("BTC/USDT down 45.20") is None
