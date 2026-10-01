"""Tests for the AI provider layer (app/ai.py).

No real network calls: httpx is monkeypatched so we verify the RIGHT endpoint,
headers and body shape are used for each provider style, and that responses are
parsed correctly. These are the parts that actually broke in practice (OpenAI vs
Anthropic-native messages API).
"""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.ai import (
    AICommentator,
    _FALLBACK_READ_TIMEOUT,
    _IDENTITY,
    _SYSTEM_ANALYST,
    _SYSTEM_ASSISTANT,
    _describe_ai_error,
    _extract_anthropic_text,
    _extract_openai_text,
    _looks_anthropic,
    _parse_model_list,
    _sanitize_history,
    _strip_reasoning,
)
from app.analysis import Factor, MarketAnalysis


def _settings(**over):
    base = dict(
        ai_api_key="sk-test",
        ai_base_url="https://api.example.com/v1",
        ai_model="gpt-4o-mini",
        ai_api_style="auto",
        ai_timeout_seconds=30.0,
        ai_max_tokens=256,
        # Fallback provider OFF by default, so these settings behave exactly like
        # a single-provider setup unless a test opts a fallback in.
        ai_fallback_api_key="",
        ai_fallback_base_url="https://api.example.com/v1",
        ai_fallback_model="gpt-4o-mini",
        ai_fallback_api_style="auto",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _analysis():
    return MarketAnalysis(
        symbol="BTC/USDT",
        verdict="buy",
        confidence=0.72,
        score=0.55,
        price=84536.0,
        factors=[Factor("trend", "buy", 0.30, "EMA 9>21>50")],
        summary="deterministic fallback summary",
    )


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Captures the single POST and returns a canned payload."""

    captured: dict = {}

    def __init__(self, payload, timeout=None):
        self._payload = payload
        _FakeClient.captured = {"timeout": timeout}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        _FakeClient.captured.update({"url": url, "headers": headers, "body": json})
        return _FakeResp(self._payload)


def _patch_httpx(monkeypatch, payload):
    def factory(timeout=None):
        return _FakeClient(payload, timeout=timeout)

    import app.ai as ai_mod

    monkeypatch.setattr(ai_mod.httpx, "Client", factory)


# ---- style inference -------------------------------------------------

def test_looks_anthropic_by_model():
    assert _looks_anthropic("claude-opus-4-8", "https://api.example.com/v1")
    assert not _looks_anthropic("gpt-4o-mini", "https://api.openai.com/v1")


def test_style_auto_picks_anthropic_for_claude():
    ai = AICommentator(_settings(ai_model="claude-opus-4-8"))
    assert ai._style() == "anthropic"


def test_style_auto_picks_openai_for_gpt():
    ai = AICommentator(_settings(ai_model="gpt-4o-mini"))
    assert ai._style() == "openai"


def test_style_explicit_override_wins():
    ai = AICommentator(_settings(ai_model="claude-opus-4-8", ai_api_style="openai"))
    assert ai._style() == "openai"


# ---- OpenAI path -----------------------------------------------------

def test_openai_hits_chat_completions(monkeypatch):
    _patch_httpx(monkeypatch, {"choices": [{"message": {"content": "hi there"}}]})
    ai = AICommentator(_settings(ai_model="gpt-4o-mini"))
    out = ai.ask("trend?")
    assert out == "hi there"
    cap = _FakeClient.captured
    assert cap["url"].endswith("/chat/completions")
    assert cap["headers"]["Authorization"] == "Bearer sk-test"
    assert cap["body"]["model"] == "gpt-4o-mini"
    assert cap["body"]["messages"][0]["role"] == "system"


def test_connect_timeout_is_bounded_for_fast_failover(monkeypatch):
    # A DOWN primary must be abandoned on a short CONNECT budget so the request
    # fails over fast, instead of the client hanging for the full read timeout.
    _patch_httpx(monkeypatch, {"choices": [{"message": {"content": "hi"}}]})
    ai = AICommentator(_settings(ai_model="gpt-4o-mini", ai_timeout_seconds=30.0))
    ai.ask("x")
    t = _FakeClient.captured["timeout"]
    assert isinstance(t, httpx.Timeout)
    assert t.connect == 5.0   # fast connect cap (default)
    assert t.read == 30.0     # full read budget preserved for a slow-but-alive reply


# ---- Anthropic path --------------------------------------------------

def test_anthropic_hits_messages_endpoint(monkeypatch):
    _patch_httpx(monkeypatch, {"content": [{"type": "text", "text": "hello world"}]})
    ai = AICommentator(_settings(ai_model="claude-opus-4-8"))
    out = ai.ask("trend?")
    assert out == "hello world"
    cap = _FakeClient.captured
    assert cap["url"].endswith("/messages")
    assert cap["headers"]["x-api-key"] == "sk-test"
    assert cap["headers"]["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in cap["headers"]
    assert cap["body"]["system"]  # system prompt goes in its own field
    assert cap["body"]["max_tokens"] == 256


def test_extract_anthropic_text_joins_blocks():
    data = {"content": [
        {"type": "text", "text": "part one "},
        {"type": "text", "text": "part two"},
        {"type": "tool_use", "id": "x"},
    ]}
    assert _extract_anthropic_text(data) == "part one part two"


def test_extract_anthropic_text_empty_returns_none():
    assert _extract_anthropic_text({"content": []}) is None
    assert _extract_anthropic_text({}) is None


def test_extract_openai_text_reads_content():
    assert _extract_openai_text({"choices": [{"message": {"content": "  hi there  "}}]}) == "hi there"


def test_extract_openai_text_null_content_returns_none():
    # A provider (a free gateway under load, or a reasoning model) can answer
    # HTTP 200 with content=null. That must be treated as "no usable text"
    # (None) — NOT crash with AttributeError on None.strip() — so the caller can
    # cleanly fail over to the next provider instead of the whole request dying.
    assert _extract_openai_text({"choices": [{"message": {"content": None}}]}) is None
    assert _extract_openai_text({"choices": [{"message": {}}]}) is None
    assert _extract_openai_text({"choices": [{}]}) is None
    assert _extract_openai_text({"choices": []}) is None
    assert _extract_openai_text({}) is None


def test_extract_openai_text_does_not_leak_reasoning_scratchpad():
    # If a reasoning model returns only its raw chain-of-thought in a side field
    # and a null content, we return None (no answer) rather than surfacing the
    # scratchpad — the operator complained about exactly that leak with "auto".
    data = {"choices": [{"message": {"content": None, "reasoning_content": "let me think step 1..."}}]}
    assert _extract_openai_text(data) is None


# ---- reasoning-scratchpad stripping (clean replies from a leaky fallback) ---
# A free gateway's "auto" route can land on a model that dumps its chain-of-
# thought into the reply. _strip_reasoning keeps that scratchpad from ever
# reaching the operator, without butchering a genuine answer.


def test_strip_reasoning_removes_think_block():
    assert _strip_reasoning("<think>plan the trade</think>Buy looks weak here.") == "Buy looks weak here."
    assert _strip_reasoning("<Thinking>\nstep 1\n</Thinking>\n\nHold for now.") == "Hold for now."


def test_strip_reasoning_trims_preamble_to_final_answer():
    leaked = (
        "Here's a thinking process:\n"
        "1. Analyze user input: they want risk at 0.5%.\n"
        "2. Decide it's sensible.\n"
        "Final answer: 0.5% per trade is a solid beginner setting."
    )
    assert _strip_reasoning(leaked) == "0.5% per trade is a solid beginner setting."


def test_strip_reasoning_preserves_action_tag_from_trimmed_part():
    # The proposed action may sit in the reasoning body; it must survive the trim
    # so the Confirm card still fires even when the answer prose came after it.
    leaked = (
        "Here's my thinking: they asked to cap exposure. "
        '[[action:{"type":"settings","changes":{"max_total_exposure_pct":20.0}}]] '
        "Final answer: capping you at 20%."
    )
    out = _strip_reasoning(leaked)
    assert out.startswith("capping you at 20%")
    assert '[[action:{"type":"settings"' in out


def test_strip_reasoning_leaves_clean_answers_untouched():
    clean = "Momentum's fading on BTC — I'd wait for a close above 85k before adding."
    assert _strip_reasoning(clean) == clean


def test_strip_reasoning_conservative_without_final_marker():
    # Opens like reasoning but has no clear "final answer" boundary: leave it
    # ALONE rather than risk deleting the real reply. Better messy than empty.
    text = "Let me think about this. The trend is up and volume confirms it, so I'd stay long."
    assert _strip_reasoning(text) == text


def test_strip_reasoning_empty_and_none():
    assert _strip_reasoning("") is None


# ---- identity / white-label (owner = Denis Macharia, never the AI provider) ---
# The deployed bot presents as built/owned by Denis Macharia. The assistant may
# admit it's an AI but must never name the underlying model/vendor, and shares
# the owner's contact only when asked. These lock the _IDENTITY clause into both
# system prompts AND verify it is actually sent on the wire.


def test_identity_clause_is_in_both_system_prompts():
    for prompt in (_SYSTEM_ANALYST, _SYSTEM_ASSISTANT):
        assert _IDENTITY in prompt
        assert "Denis Macharia" in prompt
        assert "+254703285246" in prompt
    # The rule that powers the white-label: never name the model/provider.
    low = _IDENTITY.lower()
    assert "never" in low and "provider" in low


def test_identity_sent_on_wire_via_ask(monkeypatch):
    # ask() uses the ANALYST system prompt; the identity clause must reach the
    # provider so "who built you?" can be answered as Denis Macharia.
    _patch_httpx(monkeypatch, {"choices": [{"message": {"content": "ok"}}]})
    ai = AICommentator(_settings(ai_model="gpt-4o-mini"))
    ai.ask("who built you?")
    system_msg = _FakeClient.captured["body"]["messages"][0]
    assert system_msg["role"] == "system"
    assert "Denis Macharia" in system_msg["content"]
    assert "+254703285246" in system_msg["content"]


def test_identity_sent_on_wire_via_chat(monkeypatch):
    # chat() uses the ASSISTANT system prompt; same guarantee on the chat path.
    _patch_httpx(monkeypatch, {"choices": [{"message": {"content": "ok"}}]})
    ai = AICommentator(_settings(ai_model="gpt-4o-mini"))
    ai.chat("who owns this app?")
    system_msg = _FakeClient.captured["body"]["messages"][0]
    assert system_msg["role"] == "system"
    assert "Denis Macharia" in system_msg["content"]
    assert _strip_reasoning(None) is None
    assert _strip_reasoning("<think>only scratchpad, no answer</think>") is None


# ---- fallbacks -------------------------------------------------------

def test_no_key_means_unavailable_and_fallback():
    ai = AICommentator(_settings(ai_api_key=""))
    assert ai.available is False
    a = _analysis()
    assert ai.narrate(a) == a.summary
    assert ai.assess(a) == a.summary
    assert "not configured" in ai.ask("x").lower()


def test_narrate_falls_back_when_request_fails(monkeypatch):
    def boom(timeout=None):
        class C:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def post(self, *a, **k):
                raise RuntimeError("network down")
        return C()

    import app.ai as ai_mod
    monkeypatch.setattr(ai_mod.httpx, "Client", boom)
    ai = AICommentator(_settings())
    a = _analysis()
    assert ai.narrate(a) == a.summary


def test_assess_uses_full_token_budget(monkeypatch):
    _patch_httpx(monkeypatch, {"choices": [{"message": {"content": "assessment text"}}]})
    ai = AICommentator(_settings(ai_model="gpt-4o-mini", ai_max_tokens=999))
    out = ai.assess(_analysis())
    assert out == "assessment text"
    assert _FakeClient.captured["body"]["max_tokens"] == 999


# ---- conversation memory (history threading) -------------------------

def test_sanitize_history_maps_roles_and_drops_junk():
    out = _sanitize_history([
        {"role": "you", "text": "hi"},          # UI shape -> user
        {"role": "ai", "content": "hello"},      # UI shape -> assistant
        {"role": "user", "content": "  "},       # empty -> dropped
        {"role": "system", "content": "nope"},   # unknown role -> dropped
        "garbage",                                # non-dict -> dropped
        {"role": "assistant", "content": "sure"},
    ])
    assert out == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "assistant", "content": "sure"},
    ]


def test_sanitize_history_bounds_turns_and_length():
    many = [{"role": "user" if i % 2 == 0 else "ai", "content": f"m{i}"} for i in range(40)]
    out = _sanitize_history(many, max_turns=6)
    assert len(out) == 6
    assert out[-1]["content"] == "m39"  # keeps the most recent turns
    long = _sanitize_history([{"role": "user", "content": "x" * 9000}], max_chars=100)
    assert len(long[0]["content"]) == 100


def test_sanitize_history_drops_leading_assistant_and_bad_input():
    # First turn must be a user turn for the Anthropic messages API.
    out = _sanitize_history([
        {"role": "ai", "content": "orphan reply"},
        {"role": "you", "content": "real question"},
    ])
    assert out[0]["role"] == "user"
    assert _sanitize_history(None) == []
    assert _sanitize_history("nope") == []


def test_chat_threads_history_openai(monkeypatch):
    _patch_httpx(monkeypatch, {"choices": [{"message": {"content": "answer"}}]})
    ai = AICommentator(_settings(ai_model="gpt-4o-mini"))
    out = ai.chat("and now?", history=[
        {"role": "you", "text": "what is my balance?"},
        {"role": "ai", "text": "you have 100 USDT"},
    ])
    assert out == "answer"
    msgs = _FakeClient.captured["body"]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[1]["content"] == "what is my balance?"
    assert msgs[2]["content"] == "you have 100 USDT"
    assert "and now?" in msgs[-1]["content"]  # live question carries the grounding


def test_chat_threads_history_anthropic_alternates(monkeypatch):
    _patch_httpx(monkeypatch, {"content": [{"type": "text", "text": "ok"}]})
    ai = AICommentator(_settings(ai_model="claude-opus-4-8"))
    ai.chat("continue", history=[
        {"role": "you", "text": "first"},
        {"role": "ai", "text": "second"},
    ])
    body = _FakeClient.captured["body"]
    assert body["system"]  # system stays in its own field, not in messages
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user"]  # strictly alternating


def test_chat_coalesces_adjacent_same_role(monkeypatch):
    # Two trailing user turns (or a user turn right before the live question)
    # must be merged, or the Anthropic API 400s on non-alternating roles.
    _patch_httpx(monkeypatch, {"content": [{"type": "text", "text": "ok"}]})
    ai = AICommentator(_settings(ai_model="claude-opus-4-8"))
    ai.chat("live question", history=[
        {"role": "you", "text": "a"},
        {"role": "you", "text": "b"},
    ])
    msgs = _FakeClient.captured["body"]["messages"]
    assert [m["role"] for m in msgs] == ["user"]
    assert "a" in msgs[0]["content"] and "b" in msgs[0]["content"]
    assert "live question" in msgs[0]["content"]


def test_chat_no_history_is_single_user_turn(monkeypatch):
    _patch_httpx(monkeypatch, {"choices": [{"message": {"content": "hey"}}]})
    ai = AICommentator(_settings(ai_model="gpt-4o-mini"))
    ai.chat("hello")
    roles = [m["role"] for m in _FakeClient.captured["body"]["messages"]]
    assert roles == ["system", "user"]


def test_chat_attaches_image_openai_style(monkeypatch):
    # Vision on the OpenAI path: the live user turn becomes multimodal content with
    # an image_url data-URL; earlier turns and the system prompt are untouched.
    _patch_httpx(monkeypatch, {"choices": [{"message": {"content": "a green candle"}}]})
    ai = AICommentator(_settings(ai_model="gpt-4o-mini"))
    ai.chat("what do you see?", image={"data": "aGVsbG8=", "media_type": "image/png"})
    last = _FakeClient.captured["body"]["messages"][-1]
    assert last["role"] == "user"
    assert isinstance(last["content"], list)
    text_block, img_block = last["content"]
    assert text_block["type"] == "text" and "what do you see?" in text_block["text"]
    assert img_block == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,aGVsbG8="},
    }


def test_chat_attaches_image_anthropic_style(monkeypatch):
    # Same attach on the Anthropic path uses the native image/source-base64 block.
    _patch_httpx(monkeypatch, {"content": [{"type": "text", "text": "ok"}]})
    ai = AICommentator(_settings(ai_model="claude-opus-4-8"))
    ai.chat("read this", image={"data": "aGVsbG8=", "media_type": "image/jpeg"})
    last = _FakeClient.captured["body"]["messages"][-1]
    assert last["role"] == "user"
    text_block, img_block = last["content"]
    assert text_block["type"] == "text" and "read this" in text_block["text"]
    assert img_block == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": "aGVsbG8="},
    }


# ---- action tag parsing (the assistant's "hands") --------------------------
# The assistant proposes an action by emitting a hidden [[action:{json}]] tag;
# strip_action_tag pulls it out. It must be defensive: a garbled tag must never
# raise and must never leak the raw tag into the user-visible reply.

from app.ai import strip_action_tag


def test_strip_action_tag_none_when_absent():
    text = "Here's my read on BTC — momentum is fading."
    clean, obj = strip_action_tag(text)
    assert clean == text
    assert obj is None


def test_strip_action_tag_extracts_and_hides():
    reply = (
        "I'll place a market buy on BTC, auto-sized by your risk manager.\n"
        '[[action:{"type":"order","side":"buy","symbol":"BTC/USDT","amount":null}]]'
    )
    clean, obj = strip_action_tag(reply)
    assert "[[action" not in clean  # never shown to the user
    assert clean.startswith("I'll place a market buy")
    assert obj == {"type": "order", "side": "buy", "symbol": "BTC/USDT", "amount": None}


def test_strip_action_tag_malformed_json_is_safe():
    reply = "Sure. [[action:{not valid json}]]"
    clean, obj = strip_action_tag(reply)
    assert obj is None  # no crash, no proposal
    assert "[[action" not in clean


def test_strip_action_tag_empty_input():
    assert strip_action_tag("") == ("", None)


def test_strip_action_tag_extracts_nested_settings():
    # Regression: a `settings` action nests a "changes":{...} object. The old
    # flat-only matcher (\{[^{}]*\}) couldn't span the inner braces, so it dropped
    # the whole proposal AND leaked the raw tag into the visible reply — the
    # Confirm card never rendered and the change never applied ("you did nothing").
    reply = (
        "I'll cap your total open exposure at 20% as a guardrail.\n"
        '[[action:{"type":"settings","changes":{"max_total_exposure_pct":20.0},'
        '"reason":"Cap total exposure during the proving run"}]]'
    )
    clean, obj = strip_action_tag(reply)
    assert "[[action" not in clean  # hidden from the user, never shown raw
    assert clean.startswith("I'll cap your total open exposure")
    assert obj == {
        "type": "settings",
        "changes": {"max_total_exposure_pct": 20.0},
        "reason": "Cap total exposure during the proving run",
    }


def test_strip_action_tag_reason_with_braces_and_brackets():
    # The free-text reason may itself contain braces/brackets; string-aware
    # scanning must not end the object early on them.
    reply = (
        "Tightening your stop.\n"
        '[[action:{"type":"settings","changes":{"default_stop_loss_pct":2.0},'
        '"reason":"was {loose} [per plan]"}]]'
    )
    clean, obj = strip_action_tag(reply)
    assert "[[action" not in clean
    assert obj["changes"] == {"default_stop_loss_pct": 2.0}
    assert obj["reason"] == "was {loose} [per plan]"


def test_strip_action_tag_tolerates_stray_trailing_brace():
    # Real production leak: the fallback (reasoning) model emitted an EXTRA `}`
    # before `]]` — `[[action:{...}}]]`. The strict close-match failed, so the raw
    # tag stayed in the reply AND the action was dropped (user saw the tag
    # "instead of doing"). We must skip the stray brace/whitespace, strip the tag,
    # and still parse the FIRST balanced object as the action.
    reply = (
        "I'm closing the long now (autopilot's on, so it applies).\n\n"
        '[[action:{"type":"order","side":"close","symbol":"BTC/USDT","amount":null,'
        '"reason":"Bear MSS + buy-side sweep, exit before the 81874 stop"}}]]'
    )
    clean, obj = strip_action_tag(reply)
    assert "[[action" not in clean  # tag no longer leaks into the visible reply
    assert clean.startswith("I'm closing the long now")
    assert obj == {
        "type": "order",
        "side": "close",
        "symbol": "BTC/USDT",
        "amount": None,
        "reason": "Bear MSS + buy-side sweep, exit before the 81874 stop",
    }


# ---- secondary (fallback) AI provider --------------------------------------
# A backup provider that transparently picks up when the PRIMARY is down, so the
# assistant keeps working mid-trade. Only ever a backstop: a healthy primary is
# never sent to it, and it can still only answer — never place a trade.


class _RecordingClient:
    """A fake httpx.Client that plays a queued SEQUENCE of behaviours.

    Every ``with httpx.Client(...)`` in ai.py makes a fresh instance, so the
    queue + call log are class-level (shared) to span the primary AND fallback
    attempts within one request. Each queued behaviour is ("ok", payload) or
    ("raise", exception).
    """

    calls: list = []
    behaviors: list = []

    def __init__(self, timeout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None):
        _RecordingClient.calls.append({"url": url, "headers": headers, "body": json})
        assert _RecordingClient.behaviors, "more provider calls than behaviours queued"
        kind, val = _RecordingClient.behaviors.pop(0)
        if kind == "raise":
            raise val
        return _FakeResp(val)


def _install_sequence(monkeypatch, behaviors):
    _RecordingClient.calls = []
    _RecordingClient.behaviors = list(behaviors)
    import app.ai as ai_mod

    monkeypatch.setattr(ai_mod.httpx, "Client", lambda timeout=None: _RecordingClient())


_OPENAI_OK = ("ok", {"choices": [{"message": {"content": "primary answer"}}]})
_ANTHROPIC_FB_OK = ("ok", {"content": [{"type": "text", "text": "fallback answer"}]})


def _dual_settings(**over):
    """Primary = OpenAI-style, Fallback = Anthropic-style — deliberately different
    vendors so a test also proves each provider is routed with its OWN style/key."""
    return _settings(
        ai_api_key="sk-primary",
        ai_base_url="https://primary.example.com/v1",
        ai_model="gpt-4o-mini",
        ai_fallback_api_key="sk-ant-fallback",
        ai_fallback_base_url="https://fallback.example.com",
        ai_fallback_model="claude-opus-4-8",
        ai_fallback_api_style="auto",
        **over,
    )


def test_available_true_with_only_fallback():
    ai = AICommentator(_settings(ai_api_key="", ai_fallback_api_key="sk-fb"))
    assert ai.available is True
    provs = ai._providers()
    assert [p["label"] for p in provs] == ["fallback"]


def test_parse_model_list_orders_and_dedupes():
    assert _parse_model_list("a") == ["a"]
    assert _parse_model_list("a, b ,c") == ["a", "b", "c"]  # order preserved
    assert _parse_model_list("a\nb\na") == ["a", "b"]  # dupes dropped, order kept
    assert _parse_model_list("  ") == []
    assert _parse_model_list(None) == []  # type: ignore[arg-type]


def test_fallback_model_list_expands_into_ordered_providers():
    # A comma-separated ai_fallback_model becomes ONE fallback provider per model,
    # tried in the listed order (fastest-verified first) — "auto pick a working
    # model" with no code change. Each fallback carries the fast-fail read cap.
    ai = AICommentator(
        _settings(
            ai_api_key="sk-primary",
            ai_fallback_api_key="sk-fb",
            ai_fallback_base_url="https://fb.example.com/v1",
            ai_fallback_model="sensenova-6.8-flash-lite, glm-5.3, deepseek-v4",
            ai_fallback_api_style="openai",
        )
    )
    provs = ai._providers()
    assert [p["label"] for p in provs] == ["primary", "fallback", "fallback-2", "fallback-3"]
    assert [p["model"] for p in provs] == [
        "gpt-4o-mini",
        "sensenova-6.8-flash-lite",
        "glm-5.3",
        "deepseek-v4",
    ]
    # Primary carries no read cap; every fallback carries the fast-fail cap.
    assert "read_timeout" not in provs[0]
    for fb in provs[1:]:
        assert fb["read_timeout"] == str(_FALLBACK_READ_TIMEOUT)
        assert fb["key"] == "sk-fb"
        assert fb["base_url"] == "https://fb.example.com/v1"


def test_extract_openai_text_reads_list_content_and_skips_reasoning():
    # Some OpenAI-compatible gateways return content as a LIST of blocks (like
    # Anthropic). Join the real answer text; never surface a reasoning block.
    data = {
        "choices": [
            {
                "message": {
                    "content": [
                        {"type": "reasoning", "text": "let me think... hidden"},
                        {"type": "text", "text": "Hello "},
                        {"type": "text", "text": "world"},
                    ]
                }
            }
        ]
    }
    assert _extract_openai_text(data) == "Hello world"
    # A list with no usable text parts is honest "no usable text" (None).
    assert _extract_openai_text({"choices": [{"message": {"content": [{"type": "reasoning", "text": "x"}]}}]}) is None
    assert _extract_openai_text({"choices": [{"message": {"content": []}}]}) is None


def test_fallback_picks_up_when_primary_fails(monkeypatch):
    # Primary raises (provider down); fallback answers. The SAME request must be
    # retried against the fallback, routed with the fallback's own key + style.
    _install_sequence(monkeypatch, [("raise", httpx.ConnectError("primary down")), _ANTHROPIC_FB_OK])
    ai = AICommentator(_dual_settings())
    out = ai.chat("how is my bot doing?")
    assert out == "fallback answer"
    assert ai._last_provider == "fallback"
    calls = _RecordingClient.calls
    assert len(calls) == 2
    # First attempt: primary, OpenAI-style, primary key.
    assert calls[0]["url"] == "https://primary.example.com/v1/chat/completions"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-primary"
    # Second attempt: fallback, Anthropic-style (Claude model), fallback key.
    assert calls[1]["url"] == "https://fallback.example.com/messages"
    assert calls[1]["headers"]["x-api-key"] == "sk-ant-fallback"
    assert "Authorization" not in calls[1]["headers"]


def test_null_content_from_primary_fails_over(monkeypatch):
    # The dominant real-world flake on a free gateway: HTTP 200 but content=null.
    # It must be treated as "no usable text" and fail over to the fallback, not
    # crash the request — so the assistant keeps answering when it matters.
    _install_sequence(monkeypatch, [
        ("ok", {"choices": [{"message": {"content": None}}]}),
        _ANTHROPIC_FB_OK,
    ])
    ai = AICommentator(_dual_settings())
    out = ai.chat("how is my bot doing?")
    assert out == "fallback answer"
    assert ai._last_provider == "fallback"
    assert len(_RecordingClient.calls) == 2


def test_primary_ok_means_fallback_is_never_called(monkeypatch):
    # A healthy primary must NOT leak the request to the fallback provider.
    _install_sequence(monkeypatch, [_OPENAI_OK, _ANTHROPIC_FB_OK])
    ai = AICommentator(_dual_settings())
    out = ai.chat("hi")
    assert out == "primary answer"
    assert ai._last_provider == "primary"
    assert len(_RecordingClient.calls) == 1  # fallback untouched


def test_both_providers_down_reports_both_reasons(monkeypatch):
    _install_sequence(
        monkeypatch,
        [("raise", httpx.ConnectError("primary down")), ("raise", httpx.TimeoutException("fb slow"))],
    )
    ai = AICommentator(_dual_settings())
    msg = ai.ask("trend?")  # returns "AI request failed: <reason>." on total failure
    assert len(_RecordingClient.calls) == 2
    assert "primary" in ai._last_error and "fallback" in ai._last_error
    assert "AI request failed" in msg


def test_transient_protocol_error_retries_same_provider(monkeypatch):
    # A flaky gateway drops the connection mid-response (RemoteProtocolError).
    # The SAME provider must be retried ONCE and succeed — not fall through to a
    # slower fallback over a transient hiccup. (This was the user's live symptom.)
    _install_sequence(monkeypatch, [
        ("raise", httpx.RemoteProtocolError("server disconnected")),
        _OPENAI_OK,  # retry of the SAME primary succeeds
    ])
    ai = AICommentator(_dual_settings())
    out = ai.chat("how's my bot?")
    assert out == "primary answer"
    assert ai._last_provider == "primary"
    assert len(_RecordingClient.calls) == 2  # both calls were the primary; fallback untouched
    assert _RecordingClient.calls[0]["url"] == "https://primary.example.com/v1/chat/completions"
    assert _RecordingClient.calls[1]["url"] == "https://primary.example.com/v1/chat/completions"


def test_read_timeout_does_not_retry_and_fails_over(monkeypatch):
    # A ReadTimeout means the MODEL is slow — retrying it just burns another
    # budget. It must fail over to the fallback immediately (no primary retry).
    _install_sequence(monkeypatch, [
        ("raise", httpx.ReadTimeout("model slow")),
        _ANTHROPIC_FB_OK,
    ])
    ai = AICommentator(_dual_settings())
    out = ai.chat("how's my bot?")
    assert out == "fallback answer"
    assert ai._last_provider == "fallback"
    assert len(_RecordingClient.calls) == 2  # 1 primary (no retry) + 1 fallback
    assert _RecordingClient.calls[0]["url"] == "https://primary.example.com/v1/chat/completions"
    assert _RecordingClient.calls[1]["url"].endswith("/messages")  # the fallback


def test_transient_retry_gives_up_after_one_retry(monkeypatch):
    # Two protocol drops in a row on the primary → one retry, then fail over.
    _install_sequence(monkeypatch, [
        ("raise", httpx.RemoteProtocolError("drop 1")),
        ("raise", httpx.RemoteProtocolError("drop 2")),
        _ANTHROPIC_FB_OK,
    ])
    ai = AICommentator(_dual_settings())
    out = ai.chat("how's my bot?")
    assert out == "fallback answer"
    assert len(_RecordingClient.calls) == 3  # primary x2 (retry), then fallback


def test_describe_error_remote_protocol_is_clear():
    msg = _describe_ai_error(httpx.RemoteProtocolError("server disconnected"))
    assert "dropped the connection" in msg
    assert "retried once" in msg


def test_health_probes_each_provider(monkeypatch):
    _install_sequence(monkeypatch, [_OPENAI_OK, _ANTHROPIC_FB_OK])
    ai = AICommentator(_dual_settings())
    h = ai.health()
    assert h["ok"] is True
    labels = [p["label"] for p in h["providers"]]
    assert labels == ["primary", "fallback"]
    assert all(p["ok"] for p in h["providers"])


def test_health_ok_if_only_fallback_answers(monkeypatch):
    # Primary probe fails, fallback probe works -> overall ok (assistant survives).
    _install_sequence(monkeypatch, [("raise", httpx.ConnectError("down")), _ANTHROPIC_FB_OK])
    ai = AICommentator(_dual_settings())
    h = ai.health()
    assert h["ok"] is True
    assert h["providers"][0]["ok"] is False
    assert h["providers"][1]["ok"] is True


def test_single_provider_behaviour_unchanged(monkeypatch):
    # With no fallback configured, exactly one attempt is made (byte-for-byte the
    # old single-provider path).
    _install_sequence(monkeypatch, [_OPENAI_OK])
    ai = AICommentator(_settings())  # fallback key empty by default
    out = ai.ask("x?")
    assert out == "primary answer"
    assert len(_RecordingClient.calls) == 1
    assert ai._last_provider == "primary"


# --- ICT read is fed to the AI (the "stop refusing ICT" wiring) -------------

def _real_ict():
    """A real computed ICT read on a hand-built frame (no network, no fake)."""
    from app.ict import analyze_ict
    from tests.test_ict import _triangle_frame

    return analyze_ict(_triangle_frame(120), symbol="BTC/USDT")


def test_ict_block_is_empty_without_a_read():
    # No read -> no block, so the model is never told structure exists when it
    # doesn't (honesty: absence is absence).
    assert AICommentator._ict_block(None) == ""


def test_ict_block_renders_only_real_computed_levels():
    ict = _real_ict()
    block = AICommentator._ict_block(ict)
    assert block.startswith("ICT / smart-money read (REAL")
    assert "Bias:" in block and "structure trend:" in block
    # Any dealing-range edge it prints must be the REAL computed swing level,
    # never a rounded/invented one.
    d = ict.as_dict()
    dr = d.get("dealing_range")
    if dr:
        assert f"{float(dr['high']):g}" in block
        assert f"{float(dr['low']):g}" in block


def test_ask_feeds_the_ict_read_and_capability_note(monkeypatch):
    ai = AICommentator(_settings())
    captured: dict = {}

    def fake_post(system, prompt, **kw):
        captured["system"], captured["prompt"] = system, prompt
        return "ok"

    monkeypatch.setattr(ai, "_post", fake_post)
    ai.ask("give me the ICT read", _analysis(), ict=_real_ict())
    # The real read reaches the model...
    assert "ICT / smart-money read (REAL" in captured["prompt"]
    # ...and the system prompt tells it to USE it rather than refuse.
    assert "ICT / SMART-MONEY" in captured["system"]


def test_chat_feeds_the_ict_read_and_capability_note(monkeypatch):
    ai = AICommentator(_settings())
    captured: dict = {}

    def fake_post(system, prompt, **kw):
        captured["system"], captured["prompt"] = system, prompt
        return "ok"

    monkeypatch.setattr(ai, "_post", fake_post)
    ai.chat("how does ICT read here?", analysis=_analysis(), ict=_real_ict())
    assert "ICT / smart-money read (REAL" in captured["prompt"]
    assert "ICT / SMART-MONEY" in captured["system"]


def test_ask_without_ict_has_no_ict_block(monkeypatch):
    ai = AICommentator(_settings())
    captured: dict = {}

    def fake_post(system, prompt, **kw):
        captured["prompt"] = prompt
        return "ok"

    monkeypatch.setattr(ai, "_post", fake_post)
    ai.ask("plain question", _analysis())  # no ict passed
    assert "ICT / smart-money read (REAL" not in captured["prompt"]

