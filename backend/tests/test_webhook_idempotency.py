"""M13 — a webhook is an at-least-once channel: prove the endpoint's building
blocks make it safe to deliver the same alert twice.

Two defences are pinned as PURE functions here (the endpoint wiring is covered in
test_api.py):
  1. ``_webhook_dedup_key`` — extract an EXPLICIT idempotency token, and pointedly
     refuse to treat an ambiguous/static ``id`` as one (which would silently halt
     trading after the first alert).
  2. ``_webhook_replay_stale`` — opt-in rejection of a stale/future send-time so a
     captured URL+body can't be replayed later.
"""
from __future__ import annotations

import datetime as dt

from app.config import get_settings
from app.main import (
    _coerce_webhook_timestamp,
    _webhook_dedup_key,
    _webhook_replay_stale,
)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


# ---- idempotency-key extraction -------------------------------------

def test_dedup_key_reads_explicit_snake_and_camel():
    assert _webhook_dedup_key({"idempotency_key": "abc", "action": "buy"}) == "abc"
    assert _webhook_dedup_key({"clientOrderId": "ord-1"}) == "ord-1"
    assert _webhook_dedup_key({"nonce": "n-9"}) == "n-9"
    assert _webhook_dedup_key({"eventId": "e1"}) == "e1"
    assert _webhook_dedup_key({"uuid": "u-2"}) == "u-2"


def test_dedup_key_priority_prefers_idempotency_key():
    # Most-specific wins so behaviour is deterministic when several are present.
    assert _webhook_dedup_key({"nonce": "n", "idempotency_key": "K"}) == "K"


def test_dedup_key_ignores_ambiguous_static_identifiers():
    # THE safety property: a generic/static id must NOT be taken as a dedup token,
    # or every alert after the first would be silently ignored and trading halts.
    assert _webhook_dedup_key({"id": "strategy-7", "action": "buy"}) is None
    assert _webhook_dedup_key({"order_id": "42"}) is None
    assert _webhook_dedup_key({"alert_id": "rule-1"}) is None


def test_dedup_key_coerces_number_and_bounds_length():
    assert _webhook_dedup_key({"nonce": 12345}) == "12345"
    assert len(_webhook_dedup_key({"nonce": "x" * 5000})) == 200


def test_dedup_key_skips_bool_null_blank_and_nondict():
    assert _webhook_dedup_key({"nonce": True}) is None
    assert _webhook_dedup_key({"nonce": None}) is None
    assert _webhook_dedup_key({"nonce": "   "}) is None
    assert _webhook_dedup_key({}) is None
    assert _webhook_dedup_key(["not", "a", "dict"]) is None
    assert _webhook_dedup_key(None) is None


# ---- timestamp parsing ----------------------------------------------

def test_coerce_timestamp_epoch_seconds():
    now = _utcnow()
    out = _coerce_webhook_timestamp(now.timestamp())
    assert abs((out - now).total_seconds()) < 1


def test_coerce_timestamp_epoch_millis():
    now = _utcnow()
    out = _coerce_webhook_timestamp(int(now.timestamp() * 1000))
    assert abs((out - now).total_seconds()) < 1


def test_coerce_timestamp_numeric_string():
    now = _utcnow()
    out = _coerce_webhook_timestamp(str(int(now.timestamp())))
    assert abs((out - now).total_seconds()) < 2


def test_coerce_timestamp_iso_with_z_is_utc():
    out = _coerce_webhook_timestamp("2026-09-27T12:00:00Z")
    assert out == dt.datetime(2026, 9, 27, 12, 0, 0, tzinfo=dt.timezone.utc)


def test_coerce_timestamp_naive_iso_assumed_utc():
    out = _coerce_webhook_timestamp("2026-09-27T12:00:00")
    assert out == dt.datetime(2026, 9, 27, 12, 0, 0, tzinfo=dt.timezone.utc)


def test_coerce_timestamp_rejects_garbage_bool_and_prehistoric():
    assert _coerce_webhook_timestamp("not-a-time") is None
    assert _coerce_webhook_timestamp(True) is None
    assert _coerce_webhook_timestamp(None) is None
    assert _coerce_webhook_timestamp(100) is None  # 1970 — implausible send time


# ---- replay staleness (opt-in) --------------------------------------

def test_replay_disabled_by_default(monkeypatch):
    monkeypatch.setattr(get_settings(), "webhook_max_age_seconds", 0.0)
    # Even a wildly old timestamp is fine when the guard is off.
    assert _webhook_replay_stale({"timenow": "2001-01-01T00:00:00Z"}) is None


def test_replay_fresh_timestamp_passes(monkeypatch):
    monkeypatch.setattr(get_settings(), "webhook_max_age_seconds", 300.0)
    assert _webhook_replay_stale({"timenow": _utcnow().timestamp()}) is None


def test_replay_stale_timestamp_rejected(monkeypatch):
    monkeypatch.setattr(get_settings(), "webhook_max_age_seconds", 300.0)
    old = (_utcnow() - dt.timedelta(hours=1)).timestamp()
    reason = _webhook_replay_stale({"timenow": old})
    assert reason is not None and "stale" in reason


def test_replay_future_timestamp_rejected(monkeypatch):
    monkeypatch.setattr(get_settings(), "webhook_max_age_seconds", 300.0)
    future = (_utcnow() + dt.timedelta(hours=1)).timestamp()
    reason = _webhook_replay_stale({"timenow": future})
    assert reason is not None and "future" in reason


def test_replay_missing_timestamp_rejected_when_enabled(monkeypatch):
    monkeypatch.setattr(get_settings(), "webhook_max_age_seconds", 300.0)
    reason = _webhook_replay_stale({"action": "buy", "symbol": "BTC/USDT"})
    assert reason is not None and "timestamp" in reason

