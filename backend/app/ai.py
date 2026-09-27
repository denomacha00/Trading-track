"""Optional AI/LLM narration + reasoning layer.

Honest scope: the LLM does NOT decide trades and cannot guarantee profit. The
deterministic MarketAnalyzer (app/analysis.py) makes the call; this module turns
that structured analysis into a plain-English explanation, can answer free-form
questions, and can produce a deeper research-style assessment (risks, scenarios,
position-sizing sanity checks) so the operator loses less to avoidable mistakes.
It is entirely optional — if no API key is configured, `available` is False and
callers fall back to the built-in deterministic summary.

Provider is pluggable. It speaks EITHER:
  * an OpenAI-compatible chat-completions API (POST {base}/chat/completions), or
  * an Anthropic-native messages API (POST {base}/messages with x-api-key +
    anthropic-version headers) — what Claude Code / Claude models use.
The style is chosen from `ai_api_style` ("auto"/"openai"/"anthropic"); "auto"
infers Anthropic when the model looks like Claude or the base URL is
anthropic-flavoured. No key or network call happens unless a key is configured.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

import httpx

from app.analysis import MarketAnalysis
from app.config import Settings

logger = logging.getLogger(__name__)

_SYSTEM_ANALYST = (
    "You are a rigorous, risk-first crypto trading analyst embedded in the "
    "Tranding-track bot. A deterministic engine already produced the numeric "
    "signal; your job is to reason about it like a careful desk analyst so the "
    "operator avoids costly mistakes. Always weigh downside first, flag when a "
    "setup is low-quality or conflicted, and NEVER promise profit or certainty. "
    "Be concrete and concise."
)

_SYSTEM_ASSISTANT = (
    "You are the operator's trading partner inside Tranding-track — talk like a real "
    "person sitting next to them at the desk, not a chatbot. Speak in the first "
    "person, plainly, with a real opinion. Drop the robotic filler: no 'How may I "
    "assist you today', no 'Certainly!', no 'As an AI'. Get to the point like a sharp "
    "friend who trades for a living, and have a spine — if they're about to do "
    "something risky or sloppy, tell them straight ('I wouldn't do that, here's "
    "why'); when a plan is solid, say so plainly. Be concise, but sound human, not "
    "clipped or scripted.\n"
    "You're talking about THEIR OWN account and you're grounded in a live, NON-secret "
    "snapshot of it — use their real numbers and state, and NEVER invent one. If you "
    "don't have a real figure, say so and ask rather than guessing. You never promise "
    "profit or certainty, you call out weak or conflicted setups, and you never "
    "forget this is real money — your first job is to help them keep it.\n"
    "You're also the app's guide: when they ask how something works or how to do it "
    "(add exchange keys, go live, run a backtest, connect TradingView, read a "
    "signal), walk them through it from the APP GUIDE — patiently, even for a total "
    "beginner — and never invent a feature that isn't in it.\n"
    "And you don't just talk, you can DO things for them: place or close an order, "
    "change a setting, start or stop the bot, set a price alert, or train a strategy. "
    "When they ask, first say honestly whether it's a good idea, then act via the "
    "ACTION PROTOCOL. Be RUTHLESSLY TRUTHFUL about what has and hasn't happened: you "
    "PROPOSE an action, and it only takes effect once it is actually applied — either "
    "the operator taps Confirm, or (if they've turned autopilot on) the app applies "
    "the safe ones for you. Until that happens NOTHING has changed, so NEVER say "
    "'done', 'I've set/changed/placed/started it', or anything past-tense for an "
    "action you've only proposed. Say what you're ABOUT to do and that it needs their "
    "confirmation — or, when autopilot is on, that you're applying it now. The app "
    "itself reports the real outcome; don't announce a success you can't actually "
    "see. Real-money (live) orders and switching paper->live ALWAYS need their "
    "explicit confirmation. If a request is unsafe, say so plainly instead of going "
    "along with it. Never ask for or repeat secrets or API keys."
)

# What the assistant knows about the product itself, so "how does this work?" and
# "how do I…" questions get accurate, specific answers instead of generic ones.
# Kept factual to the real features; do not describe anything the app can't do.
_APP_GUIDE = (
    "APP GUIDE — how Tranding-track works (answer how-to questions from this; don't "
    "invent features):\n"
    "• Purpose: a multi-user crypto trading bot. Each user has their own isolated "
    "account, settings, trades and (optional) exchange keys — no user sees another's "
    "data.\n"
    "• Access: sign up with email + password, then activate an ACTIVE licence. A "
    "pending user redeems a licence key (Login screen or the in-app banner); admins "
    "mint keys and manage users in the Admin panel.\n"
    "• Trading modes: 'paper' simulates orders with no real money (safe default); "
    "'live' places REAL orders. Change it in Settings → Trading mode; always prove a "
    "strategy in paper/testnet first.\n"
    "• Exchange keys: Settings → 'Your Binance API keys'. Paste TRADE-ONLY keys "
    "(withdrawals OFF); they're encrypted at rest and never shown again. Tick 'Use "
    "Binance testnet' for fake-money testing. Saving immediately runs a live "
    "connection test and reports whether it truly connected.\n"
    "• Connection status: the Settings access-card and its 'Test connection' button "
    "show — live — whether the app can read public data, read the account, and "
    "trade. HTTP 451 means the exchange is geo-blocking the SERVER's region (not a "
    "key problem): the operator must deploy in a supported region, set an "
    "EXCHANGE_HTTP_PROXY, or (US) EXCHANGE_ID=binanceus.\n"
    "• Manual trading (Trades tab / trade panel): buy, sell or close a symbol; "
    "stop-loss / take-profit default from Settings. Open positions and history show "
    "in Trades.\n"
    "• Risk rules (Settings): risk per trade %, daily loss limit %, default "
    "stop-loss / take-profit %, trailing stop %, max open positions, max total "
    "exposure %, min signal confidence. The bot enforces these — advise within them.\n"
    "• Autonomous trading: 'Enable autonomous trading' lets the bot act on its "
    "analyzer for the chosen auto symbols/timeframe. Optional 'AI trade review' lets "
    "the AI VETO a risky entry — it can never invent, size or force a trade.\n"
    "• Tools: Analyze (deterministic buy/sell/hold verdict + factors, with optional "
    "AI narration/assessment), Backtest (test a strategy on history), Train (search "
    "strategy parameters on historical data).\n"
    "• Signals tab: a timeline of every analyzer/webhook signal, whether it was "
    "accepted, and its confidence.\n"
    "• TradingView: each user has a private webhook URL (Settings). Point a "
    "TradingView alert at it with a JSON message to trade from alerts — there is no "
    "TradingView API key; the URL itself is the credential, keep it private.\n"
    "• News: the assistant can attach REAL public market headlines on request; an "
    "empty list means the feeds were unreachable, never fabricated.\n"
    "• The AI assistant (you): built into the app and funded by the operator — users "
    "never enter an AI key. You can guide a beginner through setting up and running "
    "the bot, and you can DO things for them (place or close an order, change a risk "
    "setting, start/stop the bot, set a price alert, train a strategy). Normally each "
    "one appears as a Confirm card the operator taps; if they turn on Assistant "
    "autopilot (Settings), the safe ones — settings, bot start/stop, alerts and PAPER "
    "orders — are applied automatically, while live orders and paper<->live switches "
    "still need a manual confirm.\n"
    "• Privacy & security: keys/secrets are encrypted at rest and isolated per user. "
    "You receive only a NON-secret snapshot of the asking user's OWN account — never "
    "keys, passwords, the webhook token, or any other user's data."
)

# The assistant has "hands": when the user asks to be shown or taken somewhere, it
# can emit a navigation action the app executes (the frontend turns it into a
# button that switches to that screen). This never changes data — it only moves
# the user around the UI — so it's safe to act on without a confirmation step.
_NAV_ACTIONS = (
    "NAVIGATION: if the user asks to be shown or taken to part of the app (e.g. "
    "\"show me my trades\", \"take me to settings\", \"where do I add my keys\", "
    "\"open the assistant\"), answer briefly THEN append on its own final line a tag "
    "of the form [[goto:<dest>]] where <dest> is exactly one of: trades, signals, "
    "assistant, analyze, train, backtest, settings, admin. Use settings for adding "
    "exchange keys, testing the connection, or changing risk/mode. Only ever emit "
    "one tag, only when the user actually wants to go somewhere, and never invent a "
    "destination outside that list. The tag is machine-read and hidden from the "
    "user, so keep your sentence self-contained."
)

# The assistant can also PROPOSE a real action. It never executes anything: it
# emits one machine-read tag describing the action, the backend validates it
# against a strict allowlist and hands the frontend a proposal, and the app shows
# the operator a Confirm/Cancel card. Nothing touches money or settings until the
# operator clicks Confirm. The JSON must be a FLAT object (no nested braces).
_ACTION_GUIDE = (
    "ACTION PROTOCOL — how you actually DO things:\n"
    "When the operator asks you to place/close a trade, change a setting, start or "
    "stop the bot, set a price alert, or train a strategy, first say (briefly) what "
    "you'll do and why, THEN append on its own final line ONE tag of the form "
    "[[action:{...}]] whose body is a single flat JSON object. Emit at most one "
    "action tag (and don't also emit a goto tag). Only propose an action the user "
    "actually asked for or clearly agreed to. Use REAL values from the context; if "
    "you don't have a real number, ask instead of guessing.\n"
    "WHAT HAPPENS TO YOUR TAG — and how to talk about it truthfully: if the operator "
    "has AUTOPILOT ON (the account snapshot tells you), the app APPLIES the safe "
    "actions for them right away — settings, bot start/stop, price alerts and PAPER "
    "orders — and shows the real outcome. If autopilot is OFF, the tag becomes a "
    "Confirm card and NOTHING happens until they tap it. A LIVE (real-money) order "
    "and switching paper<->live ALWAYS wait for their explicit confirmation, autopilot "
    "or not. So describe the action in the present/future — 'I'm setting your stop to "
    "2%…', 'approve this and it's live', 'this needs your Confirm tap' — and NEVER "
    "claim it's already done; the app reports the true result, not you.\n"
    "Shapes:\n"
    '• Place an order: {"type":"order","side":"buy|sell|close","symbol":"BTC/USDT",'
    '"amount":null,"reason":"one short line"}. Leave "amount" null to let the risk '
    "manager size it safely, and OMIT stop_loss/take_profit so the bot applies the "
    "operator's own default risk rules — that is the safe default for a beginner. "
    'Only include "amount" (base units), "limit_price", "stop_loss" or '
    '"take_profit" (absolute prices) when the operator gave specific numbers. '
    '"close" exits the open position for that symbol.\n'
    '• Change settings: {"type":"settings","changes":{"default_stop_loss_pct":2.0},'
    '"reason":"..."}. Allowed keys ONLY: risk_per_trade_pct, daily_loss_limit_pct, '
    "default_stop_loss_pct, default_take_profit_pct, trailing_stop_pct, "
    "max_total_exposure_pct, max_open_positions, min_signal_confidence, "
    "paper_taker_fee_pct, auto_trade_enabled, auto_symbols, auto_timeframe, "
    "auto_confirm_timeframe, use_saved_strategy, ai_trade_confirm, ai_monitor_enabled. "
    "You CANNOT switch "
    "between paper and live here — going live is a deliberate human step, so guide "
    "them to Settings → Trading mode for that.\n"
    '• Start/stop the bot: {"type":"bot","state":"start|stop","reason":"..."}.\n'
    '• Set a price alert: {"type":"alert","symbol":"BTC/USDT","condition":"above|'
    'below","price":65000,"reason":"..."}. It fires once when the REAL live price '
    "crosses that level and just tells them — it never trades.\n"
    '• Train + save a strategy: {"type":"train","symbol":"BTC/USDT",'
    '"strategy":"ma_cross","timeframe":"1h","reason":"..."}. Training measures real '
    "results on history and only saves if it genuinely beats the baseline.\n"
    "The tag is hidden from the user, so keep your sentence before it self-contained."
)

# How to help a NON-trader trade safely. The whole point of the assistant is that
# someone who knows nothing about trading can lean on it and not get hurt — so
# when they ask to be "set up", to "trade safely", or to have the bot trade for
# them, it should walk them through a conservative setup and PROPOSE it (never
# force it). Everything here rides the existing confirm-gated actions — it adds
# no new execution power, only better judgement about what to suggest.
_SAFE_STARTER = (
    "HELPING A BEGINNER TRADE SAFELY — this matters most:\n"
    "If the user is new, unsure, or asks you to set things up / trade for them "
    "safely, don't dump jargon — take charge gently and keep them out of trouble. "
    "Concretely:\n"
    "• Start in PAPER mode. Never nudge someone toward live money until they've "
    "seen the bot work in paper first; going live is their deliberate step in "
    "Settings → Trading mode (you can't flip it). Say this plainly.\n"
    "• Offer a conservative 'safe starter' risk setup and PROPOSE it as ONE "
    "settings action they confirm — explaining each number in plain words, not "
    "just pasting values. A sensible low-risk starting point (adapt to what their "
    "context shows, don't parrot blindly): risk_per_trade_pct ~0.5 (risk only a "
    "tiny slice of the account per trade), default_stop_loss_pct ~2 and "
    "default_take_profit_pct ~4 (cut losses fast, let winners run about 2:1), "
    "daily_loss_limit_pct ~2 (the bot stands down for the day after a 2% dent), "
    "max_open_positions ~2 and max_total_exposure_pct ~20 (never all-in). These "
    "are suggestions they approve, not promises — smaller risk means smaller "
    "swings, never guaranteed profit.\n"
    "• Prove it before trusting it: for the bot to act on its own, recommend they "
    "Train a strategy on a symbol (real backtested metrics) and, once it beats the "
    "baseline, turn on use_saved_strategy and enable autonomous trading for just "
    "that symbol — one small step at a time, paper first.\n"
    "• A trade at a time: if they ask you to just 'buy some BTC safely', propose a "
    "market order with amount null (the risk manager sizes it) and no custom "
    "stop/target so their default risk rules apply — then tell them what will "
    "happen before they confirm.\n"
    "• Be honest about a tiny balance: exchanges enforce a MINIMUM order size "
    "(Binance spot is roughly 5–10 USDT of notional). If someone says 'I have $2, "
    "trade it for me', tell them the plain truth — a LIVE order that small is "
    "rejected by the exchange, so that money is for learning in PAPER, or they top "
    "it up before going live. Never pretend a sub-minimum live order will fill.\n"
    "• Setting them up AND running it hands-off: if autopilot is on you can actually "
    "do it — propose the safe-starter settings and a paper start, and the app "
    "applies them and reports back (say 'I'm applying this now', not 'done', and let "
    "the app confirm). If autopilot is off, either they tap Confirm on each card or "
    "they switch on Assistant autopilot in Settings so you can do it for them. Always "
    "paper-first, always their call to go live, never a promise of profit.\n"
    "• Always ground every figure in their real account context; if you don't have "
    "a number, ask. You never place anything without their confirmation, and you "
    "never pretend a result you don't have."
)


# The action tag wraps ONE JSON object: [[action:{...}]]. That object routinely
# nests — a `settings` action carries a `"changes":{...}` dict (see _ACTION_GUIDE)
# — and its free-text `reason` may itself contain braces or brackets. A flat
# `\{[^{}]*\}` class (or a naive non-greedy `.*?`) truncates or misses those, which
# silently drops every settings proposal and leaks the raw tag into the reply. So
# we locate the `[[action:` prefix, then walk the JSON with a string-aware brace
# counter to find its true end. Case-insensitive, whitespace-lax.
_ACTION_OPEN_RE = re.compile(r"\[\[\s*action\s*:\s*", re.IGNORECASE)
_ACTION_CLOSE_RE = re.compile(r"\s*\]\]")


def _scan_json_object(text: str, start: int) -> int:
    """Index just past the ``{...}`` object that begins at ``text[start]``.

    String-aware brace matching (handles nesting to any depth, and braces/brackets
    inside JSON string values) so we find the object's real end. Returns -1 if
    ``start`` isn't an opening brace or the object never closes.
    """
    if start >= len(text) or text[start] != "{":
        return -1
    depth = 0
    in_str = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


def strip_action_tag(text: str) -> tuple[str, Optional[dict]]:
    """Pull a trailing ``[[action:{json}]]`` proposal out of an assistant reply.

    Returns ``(clean_text, raw_obj_or_None)``: the reply with the machine-only tag
    removed (it's never shown to the user) and the parsed JSON object, or None if
    there's no tag or the JSON is malformed. Pure and defensive — never raises, so
    a garbled tag simply yields no proposed action. Validation/allowlisting of the
    object happens in the API layer, which knows the real settings/strategies.
    """
    if not text:
        return text, None
    m = _ACTION_OPEN_RE.search(text)
    if not m:
        return text, None
    obj_start = m.end()
    obj_end = _scan_json_object(text, obj_start)
    if obj_end < 0:
        return text, None
    close = _ACTION_CLOSE_RE.match(text, obj_end)
    if not close:
        return text, None
    clean = (text[: m.start()] + text[close.end() :]).strip()
    try:
        obj = json.loads(text[obj_start:obj_end])
    except (ValueError, TypeError):
        return clean, None
    return clean, (obj if isinstance(obj, dict) else None)


def _looks_anthropic(model: str, base_url: str) -> bool:
    m = (model or "").lower()
    b = (base_url or "").lower()
    return "claude" in m or "anthropic" in b


def _sanitize_history(
    history: Any, *, max_turns: int = 12, max_chars: int = 4000
) -> list[dict[str, str]]:
    """Normalise PRIOR conversation turns into clean provider messages.

    This is what lets the assistant actually follow a multi-turn task instead of
    treating every question as brand new. Defensive on purpose — the turns come
    from the browser, so we:
      * accept either {role, content} or the UI's {role:'you'/'ai', text} shape,
      * map roles to the strict {user, assistant} the APIs expect,
      * coerce to strings, drop empties, and clip any oversized turn,
      * keep only the most recent ``max_turns`` (bounds tokens + the shared bill),
      * drop leading assistant turns so the first message is a user turn
        (required by the Anthropic messages API).
    Adjacent same-role turns are coalesced later, once the live question is
    appended, in ``_post``. Never raises — a bad history just yields no memory.
    """
    if not isinstance(history, (list, tuple)):
        return []
    out: list[dict[str, str]] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        raw_role = str(item.get("role", "")).strip().lower()
        if raw_role in ("assistant", "ai", "bot"):
            role = "assistant"
        elif raw_role in ("user", "you", "human"):
            role = "user"
        else:
            continue
        content = item.get("content")
        if content is None:
            content = item.get("text")
        content = str(content or "").strip()
        if not content:
            continue
        out.append({"role": role, "content": content[:max_chars]})
    if len(out) > max_turns:
        out = out[-max_turns:]
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _key_family(key: str) -> str:
    """Best-effort provider guess from a key's PUBLIC prefix (never the secret).

    Anthropic keys start ``sk-ant-``, OpenAI ``sk-``/``sk-proj-``. Knowing the
    family lets the dashboard flag the classic mismatch — an OpenAI key sent
    Anthropic-style (or vice versa) — which returns 401 even though the key is
    real. Only the well-known prefix scheme is used; no key characters leak.
    """
    k = (key or "").strip()
    if not k:
        return "none"
    if k.startswith("sk-ant-"):
        return "anthropic (sk-ant-…)"
    if k.startswith(("sk-proj-", "sk-")):
        return "openai (sk-…)"
    if k.startswith("gsk_"):
        return "groq (gsk_…)"
    return "unrecognized prefix"


class AICommentator:
    """Wraps an OpenAI- or Anthropic-style chat endpoint to narrate + reason."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        # Human-readable reason the last provider call failed (no secrets). Lets
        # the UI/assistant say WHY the AI didn't answer instead of a vague retry.
        self._last_error: str = ""
        # Which provider actually answered the last successful call ("primary" or
        # "fallback"), or "" if none did. Diagnostics only — never a secret.
        self._last_provider: str = ""

    def reload(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def available(self) -> bool:
        # AI is usable if EITHER the primary or the fallback provider has a key.
        return bool(
            self._settings.ai_api_key
            or getattr(self._settings, "ai_fallback_api_key", "")
        )

    @staticmethod
    def _resolve_style(style: str, model: str, base_url: str) -> str:
        """Resolve a provider's send style: explicit wins, else infer from model/url."""
        style = (style or "auto").lower()
        if style in ("openai", "anthropic"):
            return style
        return "anthropic" if _looks_anthropic(model, base_url) else "openai"

    def _style(self) -> str:
        # The PRIMARY provider's resolved style (kept for callers/UI that read it).
        return self._resolve_style(
            self._settings.ai_api_style,
            self._settings.ai_model,
            self._settings.ai_base_url,
        )

    def _providers(self) -> list[dict[str, str]]:
        """Ordered providers to try: PRIMARY first, then the optional FALLBACK.

        Each entry is a non-secret-labelled provider config (its key is included
        only to send the request, never logged). The fallback is present ONLY when
        its own key is set, so with no fallback configured this is a one-element
        list and behaviour is identical to the original single-provider path.
        """
        s = self._settings
        out: list[dict[str, str]] = []
        if s.ai_api_key:
            out.append(
                {
                    "label": "primary",
                    "key": s.ai_api_key,
                    "base_url": s.ai_base_url,
                    "model": s.ai_model,
                    "style": self._resolve_style(s.ai_api_style, s.ai_model, s.ai_base_url),
                }
            )
        fb_key = getattr(s, "ai_fallback_api_key", "")
        if fb_key:
            fb_base = getattr(s, "ai_fallback_base_url", "") or s.ai_base_url
            fb_model = getattr(s, "ai_fallback_model", "") or s.ai_model
            fb_style = getattr(s, "ai_fallback_api_style", "auto")
            out.append(
                {
                    "label": "fallback",
                    "key": fb_key,
                    "base_url": fb_base,
                    "model": fb_model,
                    "style": self._resolve_style(fb_style, fb_model, fb_base),
                }
            )
        return out

    def _post_once(
        self,
        prov: dict[str, str],
        system: str,
        messages: list[dict[str, str]],
        max_tokens: int,
    ) -> Optional[str]:
        """One real request to a SINGLE provider.

        Raises on any transport/HTTP error (the caller records it and may fall
        back to the next provider); returns the reply text, or None if the
        provider answered but with no usable content.
        """
        base = (prov["base_url"] or "").rstrip("/")
        # Fast failover: cap CONNECT time so a down/unreachable provider is dropped
        # quickly and the SAME request can retry the next provider — while a
        # healthy-but-slow response still gets the full read budget.
        read_to = self._settings.ai_timeout_seconds
        connect_to = getattr(self._settings, "ai_connect_timeout_seconds", 5.0) or 5.0
        connect_to = min(connect_to, read_to)
        timeout = httpx.Timeout(connect=connect_to, read=read_to, write=read_to, pool=connect_to)
        with httpx.Client(timeout=timeout) as client:
            if prov["style"] == "anthropic":
                resp = client.post(
                    base + "/messages",
                    headers={
                        "x-api-key": prov["key"],
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": prov["model"],
                        "max_tokens": max_tokens,
                        "system": system,
                        "messages": messages,
                    },
                )
                resp.raise_for_status()
                return _extract_anthropic_text(resp.json())
            # OpenAI-compatible chat completions
            resp = client.post(
                base + "/chat/completions",
                headers={
                    "Authorization": f"Bearer {prov['key']}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": prov["model"],
                    "messages": [
                        {"role": "system", "content": system},
                        *messages,
                    ],
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()

    def _post(
        self,
        system: str,
        user: str,
        max_tokens: int | None = None,
        history: list[dict[str, str]] | None = None,
    ) -> Optional[str]:
        """Send a conversation and return the text reply, or None on failure.

        Tries the PRIMARY provider first; if it fails (or answers with nothing)
        AND a FALLBACK provider is configured, the SAME request is retried once
        against the fallback so the assistant keeps working while the primary is
        down. ``history`` (already-sanitised prior turns) is prepended before the
        live ``user`` turn so the assistant has real conversational memory.
        Adjacent same-role turns are coalesced so the list strictly alternates —
        the Anthropic messages API rejects two same-role turns in a row.
        """
        providers = self._providers()
        if not providers:
            self._last_error = "no AI key configured"
            self._last_provider = ""
            return None
        max_tokens = max_tokens or self._settings.ai_max_tokens
        # Build the alternating message list ONCE and reuse it for every provider.
        messages: list[dict[str, str]] = []
        for turn in (history or []) + [{"role": "user", "content": user}]:
            if messages and messages[-1]["role"] == turn["role"]:
                messages[-1]["content"] += "\n\n" + turn["content"]
            else:
                messages.append({"role": turn["role"], "content": turn["content"]})
        errors: list[str] = []
        for prov in providers:
            try:
                text = self._post_once(prov, system, messages, max_tokens)
                if text:
                    self._last_error = ""
                    self._last_provider = prov["label"]
                    if prov["label"] != "primary":
                        # A trade might be riding on this — make the failover visible
                        # in the logs (never the key or payload).
                        logger.info(
                            "AI answered via the %s provider after the primary failed",
                            prov["label"],
                        )
                    return text
                errors.append(f"{prov['label']}: provider answered with no usable text")
            except Exception as exc:  # noqa: BLE001 — categorised, secret-free below
                reason = _describe_ai_error(exc)
                errors.append(f"{prov['label']}: {reason}")
                logger.warning("AI request via %s provider failed: %s", prov["label"], reason)
        # Every configured provider failed. Compose a combined, secret-free reason.
        self._last_error = "; ".join(errors) if errors else "AI request failed"
        self._last_provider = ""
        return None

    # ---- public API --------------------------------------------------

    def narrate(self, analysis: MarketAnalysis) -> str:
        """Return an LLM explanation of the analysis, or the deterministic summary."""
        if not self.available:
            return analysis.summary
        prompt = (
            "Explain in 2-4 sentences what the market is doing and why the verdict "
            "makes sense. Emphasise capital preservation.\n\n"
            + self._analysis_block(analysis)
        )
        return self._post(_SYSTEM_ANALYST, prompt, max_tokens=350) or analysis.summary

    def narrate_event(self, fact: str, context: str = "") -> Optional[str]:
        """Voice ONE live-monitor event like a partner calling it out.

        ``fact`` is a deterministic, already-true sentence built from the
        account's REAL numbers. We only ask the model to say it naturally in one
        short line — it must NOT add, drop or change any number or claim. Returns
        None on any failure so the caller falls back to ``fact`` verbatim (the
        event is real either way; the model only changes the wording, never the
        facts).
        """
        if not self.available:
            return None
        prompt = (
            "You're watching the operator's live trades and calling out ONE thing "
            "that just happened, out loud, like a sharp trading partner leaning "
            "over — one short sentence, plain and human, urgent only if it truly "
            "matters. Say EXACTLY this fact and nothing more: do not add, drop or "
            "change any number, symbol or claim, and never invent detail.\n"
            f"FACT: {fact}"
            + (f"\nTONE-ONLY CONTEXT (never quote it): {context}" if context else "")
        )
        line = self._post(_SYSTEM_ASSISTANT, prompt, max_tokens=120)
        if not line:
            return None
        # Strip any stray protocol tag so the spoken line is clean prose only.
        line = re.sub(r"\[\[[^\]]*\]\]", "", line).strip()
        return line or None

    def assess(self, analysis: MarketAnalysis) -> str:
        """Deeper research-style assessment: quality, risks, scenarios, sizing.

        This is the "think harder" mode — it does not change the deterministic
        decision, it stress-tests it so the operator loses less to bad entries.
        """
        if not self.available:
            return analysis.summary
        prompt = (
            "Produce a structured risk assessment of this setup. Use short labelled "
            "sections:\n"
            "1. Signal quality (is the confluence real or conflicted?)\n"
            "2. Key risks & what would invalidate the thesis\n"
            "3. Bull vs bear scenario (roughly what price action confirms each)\n"
            "4. Position-sizing / stop sanity check (protect capital first)\n"
            "5. One-line verdict: act or wait, and why.\n"
            "Be honest when the edge is weak. Never promise profit.\n\n"
            + self._analysis_block(analysis)
            + "\n\n"
            + self._risk_block()
        )
        return self._post(_SYSTEM_ANALYST, prompt) or analysis.summary

    def ask(self, question: str, analysis: MarketAnalysis | None = None) -> str:
        """Answer a free-form question, optionally grounded in current analysis."""
        if not self.available:
            return (
                "AI commentary is not configured. Set AI_API_KEY (and optionally "
                "AI_BASE_URL / AI_MODEL / AI_API_STYLE) to enable natural-language "
                "market Q&A."
            )
        context = ""
        if analysis is not None:
            context = "\n\nCurrent analysis JSON:\n" + json.dumps(analysis.as_dict())
        reply = self._post(_SYSTEM_ANALYST, question + context)
        if reply is not None:
            return reply
        return f"AI request failed: {self._last_error}."

    def chat(
        self,
        question: str,
        *,
        analysis: "MarketAnalysis | None" = None,
        bot_context: str | None = None,
        news: list[dict] | None = None,
        history: Any = None,
    ) -> str:
        """Assistant answer grounded in the user's OWN bot state + optional news.

        Privacy: the caller assembles ``bot_context`` from non-secret data only
        (never exchange keys/passwords). Headlines are public. Everything is sent
        to the USER'S OWN configured AI provider. If AI isn't configured we say so
        plainly rather than pretending to answer.

        ``history`` is the PRIOR conversation (browser-local turns) so the
        assistant can follow a multi-turn task. Only the live question carries the
        fresh grounding blocks below — prior turns are sent as plain Q&A so we
        don't re-send (or leak) stale account context on every turn.
        """
        if not self.available:
            return (
                "AI assistant is not configured. Add your AI key in Settings "
                "(AI_API_KEY / AI_BASE_URL / AI_MODEL) to chat with the bot, ask "
                "for a read on the market, or get a second opinion on a decision."
            )
        blocks: list[str] = []
        if bot_context:
            blocks.append("Live bot context (the user's own account):\n" + bot_context)
        if analysis is not None:
            blocks.append(self._analysis_block(analysis))
        blocks.append(self._risk_block())
        if news:
            headlines = "\n".join(
                f"- {n.get('title')} ({n.get('source')})"
                for n in news
                if n.get("title")
            )
            if headlines:
                blocks.append(
                    "Recent REAL market headlines (public feeds; use only if "
                    "relevant, and don't overstate their certainty):\n" + headlines
                )
        prompt = (
            question
            + "\n\n---\n"
            + "\n\n".join(blocks)
            + "\n\n---\nAnswer helpfully and concretely for THIS bot and account. "
            "If the operator is asking you to actually do something (place or close "
            "a trade, change a setting, start/stop the bot, train a strategy), "
            "analyse whether it's sound and then PROPOSE it with the action "
            "protocol so they can confirm — you never execute it yourself, and the "
            "operator stays in control of every order. Weigh downside first and "
            "never promise profit."
        )
        # System prompt = who you are + how the app works + how to navigate it +
        # how to propose real actions + how to keep a beginner safe, so the
        # assistant can explain the product, drive the UI, and act on the account
        # — always behind a confirmation, always steering to the safe side.
        system = (
            _SYSTEM_ASSISTANT
            + "\n\n"
            + _APP_GUIDE
            + "\n\n"
            + _NAV_ACTIONS
            + "\n\n"
            + _ACTION_GUIDE
            + "\n\n"
            + _SAFE_STARTER
        )
        reply = self._post(system, prompt, history=_sanitize_history(history))
        if reply is not None:
            return reply
        return (
            "I couldn't reach the AI provider right now — "
            f"{self._last_error}. Your bot and its data are unaffected; this only "
            "means the chat/LLM couldn't answer. The operator can check the "
            "AI_API_KEY / AI_BASE_URL / AI_MODEL / AI_API_STYLE settings."
        )

    def _diagnose(self, prov: dict[str, str]) -> str:
        """Secret-free misconfig hint for a provider that failed to answer.

        The classic smoking gun is a key/style mismatch (an Anthropic sk-ant-… key
        sent OpenAI-style, or a Claude model sent openai-style), which returns 401
        with a perfectly real key. Never reveals key characters — only its public
        prefix FAMILY vs the send STYLE.
        """
        style = prov["style"]
        family = _key_family(prov["key"])
        looks_anth = _looks_anthropic(prov["model"], prov["base_url"])
        if family.startswith("anthropic") and style != "anthropic":
            return (
                " — this key is an Anthropic key (sk-ant-…) but is being sent "
                f"{style}-style; set its style to anthropic"
            )
        if style == "openai" and looks_anth:
            return (
                " — a Claude model is being sent openai-style; gateways that serve "
                "Claude usually need the anthropic style"
            )
        if family == "unrecognized prefix":
            return " — the key's format isn't a known OpenAI/Anthropic prefix"
        return ""

    def _probe_one(self, prov: dict[str, str]) -> dict[str, Any]:
        """Make ONE tiny real request to a provider and report non-secret status."""
        st: dict[str, Any] = {
            "label": prov["label"],
            "ok": False,
            "model": prov["model"],
            "base_url": prov["base_url"],
            "style": prov["style"],
            "detail": "",
        }
        try:
            text = self._post_once(
                prov,
                _SYSTEM_ASSISTANT,
                [{"role": "user", "content": "Reply with exactly: ok"}],
                5,
            )
            if text:
                st["ok"] = True
                st["detail"] = (
                    f"reachable and the key works ({prov['style']} style, "
                    f"model {prov['model']})"
                )
            else:
                st["detail"] = "the provider answered but returned no usable text"
        except Exception as exc:  # noqa: BLE001 — categorised, secret-free
            st["detail"] = _describe_ai_error(exc) + self._diagnose(prov)
        return st

    def health(self) -> dict[str, Any]:
        """Live check that a configured AI provider actually answers.

        Probes EACH configured provider (primary, then fallback if set) with one
        tiny real request — no fabrication — so "the AI isn't working" becomes a
        concrete per-provider reason. Top-level ``ok`` is True if ANY provider
        answers (that's all it takes to keep the assistant alive), and the
        per-provider results are returned under ``providers``. No API key is ever
        included.
        """
        status: dict[str, Any] = {
            "enabled": self.available,
            "ok": False,
            "model": self._settings.ai_model,
            "base_url": self._settings.ai_base_url,
            "style": self._style(),
            "detail": "",
            "providers": [],
        }
        providers = self._providers()
        if not providers:
            status["detail"] = (
                "No AI key configured. The operator sets AI_API_KEY (and, for a "
                "backup, AI_FALLBACK_API_KEY / AI_FALLBACK_BASE_URL / "
                "AI_FALLBACK_MODEL / AI_FALLBACK_API_STYLE) on the server."
            )
            return status
        probes = [self._probe_one(p) for p in providers]
        status["providers"] = probes
        status["ok"] = any(p["ok"] for p in probes)
        status["detail"] = " | ".join(
            f"{p['label']} {'OK' if p['ok'] else 'FAIL'}: {p['detail']}" for p in probes
        )
        return status

    def confirm_trade(self, analysis: MarketAnalysis) -> tuple[bool, str]:
        """Risk-first AI review of a proposed ENTRY. Returns (proceed, reason).

        The deterministic brain already decided to act; the AI may only VETO
        (block new risk) or approve — it cannot invent a trade or block an exit.
        If the AI is unavailable or its reply can't be parsed we PROCEED with the
        deterministic decision (never fabricate a veto or an approval).
        """
        if not self.available:
            return True, "AI review unavailable; deterministic decision stands"
        prompt = (
            "The deterministic engine wants to OPEN this position. Review it "
            "risk-first and decide whether to APPROVE or VETO the entry. Veto only "
            "when the setup is low-quality, conflicted, or the risk clearly "
            "outweighs the edge. Reply with STRICT JSON and nothing else: "
            '{"decision": "approve" | "veto", "reason": "<one concise sentence>"}\n\n'
            + self._analysis_block(analysis)
            + "\n\n"
            + self._risk_block()
        )
        reply = self._post(_SYSTEM_ANALYST, prompt, max_tokens=200)
        if not reply:
            return True, "AI review failed; deterministic decision stands"
        try:
            data = json.loads(_extract_json(reply))
            decision = str(data.get("decision", "")).strip().lower()
            reason = str(data.get("reason", "")).strip() or "no reason given"
        except Exception:
            return True, "AI review inconclusive; deterministic decision stands"
        if decision == "veto":
            return False, f"AI veto: {reason}"
        return True, f"AI approved: {reason}"

    @staticmethod
    def _analysis_block(analysis: MarketAnalysis) -> str:
        factors = "\n".join(
            f"- {f.name}: {f.signal} (weight {f.weight}; {f.detail})"
            for f in analysis.factors
        )
        return (
            f"Symbol: {analysis.symbol}\nPrice: {analysis.price}\n"
            f"Verdict: {analysis.verdict} (confidence {analysis.confidence:.0%}, "
            f"score {analysis.score:+.2f})\nFactors:\n{factors}"
        )

    def _risk_block(self) -> str:
        """The bot's own risk rules, so sizing/stop advice is grounded in the
        real numbers this account trades with rather than generic guesses."""
        s = self._settings
        return (
            "Bot risk configuration (these ARE the rules that will execute — "
            "advise within them, do not tell the user to override them):\n"
            f"- Mode: {getattr(s, 'trading_mode', 'paper')}\n"
            f"- Risk per trade: {getattr(s, 'risk_per_trade_pct', 0)}% of equity\n"
            f"- Default stop-loss: {getattr(s, 'default_stop_loss_pct', 0)}%; "
            f"take-profit: {getattr(s, 'default_take_profit_pct', 0)}%; "
            f"trailing stop: {getattr(s, 'trailing_stop_pct', 0)}%\n"
            f"- Max open positions: {getattr(s, 'max_open_positions', 0)}; "
            f"daily loss limit: {getattr(s, 'daily_loss_limit_pct', 0)}% of equity\n"
            f"- Max total exposure: {getattr(s, 'max_total_exposure_pct', 0)}% "
            "(0 = uncapped)"
        )


def _is_edge_block(resp: httpx.Response) -> bool:
    """True when a response looks like a CDN/WAF (e.g. Cloudflare) block or
    challenge page rather than a genuine API reply — i.e. the request never
    reached the provider's API. Detected from the ``server`` header, a
    Cloudflare ray id, or an HTML challenge body ("Attention Required",
    "Just a moment"). Kept defensive: diagnostics must never raise."""
    try:
        if "cloudflare" in resp.headers.get("server", "").lower():
            return True
        if resp.headers.get("cf-ray") or resp.headers.get("cf-mitigated"):
            return True
        if "text/html" in resp.headers.get("content-type", "").lower():
            body = resp.text[:2000].lower()
            markers = ("attention required", "just a moment", "cloudflare",
                       "cf-browser-verification", "enable javascript")
            if any(m in body for m in markers):
                return True
    except Exception:  # noqa: BLE001 — never let error-describing itself fail
        return False
    return False


def _describe_ai_error(exc: Exception) -> str:
    """Turn a provider exception into a short, secret-free reason string.

    Never includes the API key or full request/response body — only the failure
    category, so it is safe to show the user and log.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        if _is_edge_block(exc.response):
            return (
                f"the provider's edge/firewall (e.g. Cloudflare) blocked the "
                f"request (HTTP {code}) — this is NOT a key problem; the request "
                "never reached the AI API. The gateway is refusing server-to-server "
                "calls. Use a provider that allows API access from servers, or ask "
                "the gateway operator to allowlist your server."
            )
        if code in (401, 403):
            return (
                f"the AI provider rejected the key (HTTP {code}) — check AI_API_KEY "
                "and that AI_API_STYLE matches the provider"
            )
        if code == 404:
            return (
                "the AI model or endpoint was not found (HTTP 404) — check "
                "AI_MODEL and AI_BASE_URL"
            )
        if code == 429:
            return "the AI provider is rate-limiting (HTTP 429) — wait and retry"
        if 500 <= code < 600:
            return f"the AI provider had a server error (HTTP {code}) — retry shortly"
        return f"the AI provider returned HTTP {code}"
    if isinstance(exc, (httpx.UnsupportedProtocol, httpx.InvalidURL)):
        return (
            "AI_BASE_URL is malformed — it must be just the URL starting with "
            "https:// (no 'AI_BASE_URL=' prefix, quotes, or spaces around it)"
        )
    if isinstance(exc, httpx.TimeoutException):
        return "the AI provider timed out — it may be slow or unreachable"
    if isinstance(exc, httpx.ConnectError):
        return "could not reach the AI provider — check the network or AI_BASE_URL"
    if isinstance(exc, (KeyError, IndexError, ValueError)):
        return "the AI provider returned an unexpected response format"
    return f"AI request error ({type(exc).__name__})"


def _extract_json(text: str) -> str:
    """Return the first {...} JSON-object substring (models sometimes wrap JSON)."""
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start : end + 1]
    return text


def _extract_anthropic_text(data: dict[str, Any]) -> Optional[str]:
    """Pull the text out of an Anthropic messages response."""
    content = data.get("content")
    if isinstance(content, list):
        parts = [
            blk.get("text", "")
            for blk in content
            if isinstance(blk, dict) and blk.get("type") == "text"
        ]
        text = "".join(parts).strip()
        return text or None
    if isinstance(content, str):
        return content.strip() or None
    return None
