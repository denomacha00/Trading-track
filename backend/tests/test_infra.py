"""Unit tests for previously-untested-but-wired infrastructure.

Covers the security primitives (password/JWT/Fernet/licence-key hashing), the
optional Telegram notifier, structured JSON logging, and the additive-migration
path. All pure/in-process — no network, no live server, no real DB.
"""
from __future__ import annotations

import json
import logging

import ccxt
import pytest

from app.config import Settings
from app.logging_config import JsonFormatter, configure_logging
from app.notifier import Notifier
from app.security import (
    create_access_token,
    decode_access_token,
    decrypt_secret,
    encrypt_secret,
    hash_license_key,
    hash_password,
    new_license_key,
    new_webhook_token,
    secrets_enabled,
    verify_password,
)

KEY = "unit-test-secret-key-abc123"


# ---- password hashing (PBKDF2) ---------------------------------------


def test_password_hash_roundtrip_and_rejects_wrong():
    h = hash_password("correct horse battery")
    assert h.startswith("pbkdf2_sha256$")
    assert verify_password("correct horse battery", h) is True
    assert verify_password("wrong-password", h) is False


def test_password_hash_is_salted_unique():
    assert hash_password("samepassword") != hash_password("samepassword")


def test_verify_password_malformed_returns_false():
    assert verify_password("x", "not-a-valid-hash") is False


def test_hash_password_rejects_short():
    with pytest.raises(ValueError):
        hash_password("short")


# ---- JWT (HS256) ------------------------------------------------------


def test_jwt_roundtrip_carries_claims():
    tok = create_access_token(KEY, user_id=7, email="a@b.co", role="user", ttl_minutes=60)
    payload = decode_access_token(KEY, tok)
    assert payload is not None
    assert payload["sub"] == "7"
    assert payload["email"] == "a@b.co"
    assert payload["role"] == "user"


def test_jwt_rejects_tampered_or_wrong_secret():
    tok = create_access_token(KEY, user_id=1, email="a@b.co", role="user", ttl_minutes=60)
    assert decode_access_token(KEY, tok + "x") is None       # tampered signature
    assert decode_access_token("other-secret", tok) is None  # wrong signing key
    assert decode_access_token(KEY, "a.b") is None            # malformed


def test_jwt_rejects_expired():
    tok = create_access_token(KEY, user_id=1, email="a@b.co", role="user", ttl_minutes=-1)
    assert decode_access_token(KEY, tok) is None


def test_jwt_requires_secret():
    with pytest.raises(ValueError):
        create_access_token("", user_id=1, email="a@b.co", role="user", ttl_minutes=60)
    assert decode_access_token("", "anything") is None


# ---- Fernet secret encryption (key derived from SECRET_KEY) ----------


def test_encrypt_decrypt_roundtrip():
    ct = encrypt_secret(KEY, "binance-api-secret")
    assert ct != "binance-api-secret"
    assert decrypt_secret(KEY, ct) == "binance-api-secret"


def test_decrypt_wrong_key_or_garbage_returns_none():
    ct = encrypt_secret(KEY, "s3cr3t")
    assert decrypt_secret("another-secret-key", ct) is None  # wrong key -> None, no raise
    assert decrypt_secret(KEY, "not-a-token") is None
    assert decrypt_secret(KEY, "") is None


def test_secrets_enabled_reflects_key_presence():
    assert secrets_enabled(KEY) is True
    assert secrets_enabled("") is False


# ---- licence keys & webhook tokens -----------------------------------


def test_license_key_prefix_and_hash_is_stable_and_unique():
    k = new_license_key()
    assert k.startswith("TT-")
    assert hash_license_key(k) == hash_license_key(f"  {k}  ")  # trims whitespace
    assert hash_license_key(k) != hash_license_key(new_license_key())


def test_webhook_tokens_are_unique():
    assert new_webhook_token() != new_webhook_token()


# ---- Telegram notifier (optional; never breaks the trading path) -----


def _settings(**over) -> Settings:
    s = Settings()
    for k, v in over.items():
        setattr(s, k, v)
    return s


def test_notifier_disabled_without_config():
    n = Notifier(_settings(telegram_bot_token="", telegram_chat_id=""))
    assert n.enabled is False
    assert n.send("hello") is False  # disabled -> no network attempted


def test_notifier_enabled_when_fully_configured():
    n = Notifier(_settings(telegram_bot_token="123:abc", telegram_chat_id="42"))
    assert n.enabled is True


def test_notifier_send_never_raises_on_failure(monkeypatch):
    class _BoomClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr("app.notifier.httpx.Client", _BoomClient)
    n = Notifier(_settings(telegram_bot_token="123:abc", telegram_chat_id="42"))
    assert n.send("will fail") is False  # swallowed -> False, never raises


# ---- structured JSON logging -----------------------------------------


@pytest.fixture
def _restore_root_logging():
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        yield
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
        for h in saved_handlers:
            root.addHandler(h)
        root.setLevel(saved_level)


def test_json_formatter_emits_valid_json_with_fields():
    rec = logging.LogRecord("app.x", logging.INFO, __file__, 10, "hello %s", ("world",), None)
    obj = json.loads(JsonFormatter().format(rec))
    assert obj["level"] == "INFO"
    assert obj["logger"] == "app.x"
    assert obj["message"] == "hello world"
    assert "time" in obj


def test_json_formatter_includes_extras_and_exception():
    import sys

    try:
        raise ValueError("boom")
    except ValueError:
        rec = logging.LogRecord("app.y", logging.ERROR, __file__, 20, "failed", (), sys.exc_info())
    rec.order_id = "OID-7"  # a structured extra=
    obj = json.loads(JsonFormatter().format(rec))
    assert obj["order_id"] == "OID-7"
    assert "ValueError: boom" in obj["exception"]


def test_configure_logging_switches_and_is_idempotent(_restore_root_logging):
    configure_logging("json", "DEBUG")
    root = logging.getLogger()
    assert root.level == logging.DEBUG
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonFormatter)
    # Re-configure: still exactly one handler (replaced, not stacked).
    configure_logging("text", "INFO")
    assert root.level == logging.INFO
    assert len(root.handlers) == 1
    assert not isinstance(root.handlers[0].formatter, JsonFormatter)


# ---- additive migrations (add missing columns to an existing DB) -----


def test_additive_migrations_add_missing_columns(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, inspect, text

    import app.database as db

    eng = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}", future=True)
    # A pre-v2 database: old tables missing the newer columns.
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR(255))"))
        conn.execute(text("CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol VARCHAR(20))"))
    monkeypatch.setattr(db, "engine", eng)

    db._apply_additive_migrations()

    insp = inspect(eng)
    user_cols = {c["name"] for c in insp.get_columns("users")}
    trade_cols = {c["name"] for c in insp.get_columns("trades")}
    assert {"username", "license_expires_at"} <= user_cols
    assert {"user_id", "order_type", "limit_price", "stop_order_id"} <= trade_cols
    assert "ix_users_username" in {i["name"] for i in insp.get_indexes("users")}

    # Idempotent: a second run is a no-op (no duplicate-column crash).
    db._apply_additive_migrations()


# ---- exchange retry: permanent geo-block (HTTP 451) fails fast -------


def test_geo_block_detected_by_message():
    from app.exchange import _is_geo_block

    assert _is_geo_block(Exception("binance GET ... 451 restricted location")) is True
    assert _is_geo_block(Exception("... 'b. Eligibility' in terms")) is True
    assert _is_geo_block(Exception("plain request timeout")) is False


def test_with_retry_does_not_retry_geo_block():
    import app.exchange as ex

    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise ccxt.ExchangeNotAvailable("binance GET ... 451 restricted location")

    with pytest.raises(ccxt.ExchangeNotAvailable):
        ex._with_retry(boom, attempts=3, base_delay=0.0)
    assert calls["n"] == 1  # a permanent block is tried once, not 3x


def test_with_retry_retries_true_transient_error():
    import app.exchange as ex

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        raise ccxt.NetworkError("temporary blip")

    with pytest.raises(ccxt.NetworkError):
        ex._with_retry(flaky, attempts=3, base_delay=0.0)
    assert calls["n"] == 3  # a real transient error uses the full retry budget


# ---- public market-data fallback (works despite a geo-blocked primary) ----


class _FakeExchange:
    """Minimal stand-in for a ccxt client — no network, records call counts."""

    def __init__(self, *, ticker=None, ohlcv=None, raises: Exception | None = None):
        self._ticker = ticker
        self._ohlcv = ohlcv
        self._raises = raises
        self.ticker_calls = 0
        self.ohlcv_calls = 0

    def fetch_ticker(self, symbol):
        self.ticker_calls += 1
        if self._raises is not None:
            raise self._raises
        return self._ticker

    def fetch_ohlcv(self, symbol, timeframe="1h", limit=200):
        self.ohlcv_calls += 1
        if self._raises is not None:
            raise self._raises
        return self._ohlcv


def _connector(**over):
    """A BinanceConnector with no real fallback wired (we inject fakes)."""
    from app.exchange import BinanceConnector

    over.setdefault("market_data_fallback_id", "")
    return BinanceConnector(_settings(**over))


def test_build_data_fallback_selects_configured_venue():
    conn = _connector(market_data_fallback_id="kucoin", exchange_id="binance")
    assert conn._fallback_id == "kucoin"
    assert conn._fallback_client is not None  # constructed, keyless, offline


def test_build_data_fallback_disabled_when_empty_or_same_as_primary():
    assert _connector(market_data_fallback_id="")._fallback_client is None
    same = _connector(market_data_fallback_id="binance", exchange_id="binance")
    assert same._fallback_client is None


def test_market_data_falls_back_on_geo_block():
    conn = _connector()
    conn._exchange_id = "binance"
    conn._client = _FakeExchange(raises=ccxt.ExchangeNotAvailable("451 restricted location"))
    conn._fallback_client = _FakeExchange(
        ticker={"last": 123.0, "bid": 122.0, "ask": 124.0}, ohlcv=[[0, 1, 2, 0.5, 1.5, 10]]
    )
    conn._fallback_id = "kucoin"

    t = conn.fetch_ticker("BTC/USDT")
    assert t["last"] == 123.0
    assert conn.last_data_source == "kucoin"  # honestly labelled as the fallback

    ohlcv = conn.fetch_ohlcv("BTC/USDT")
    assert ohlcv[0][4] == 1.5
    assert conn.last_data_source == "kucoin"


def test_market_data_uses_primary_when_healthy():
    conn = _connector()
    conn._exchange_id = "binance"
    conn._client = _FakeExchange(ticker={"last": 99.0})
    fallback = _FakeExchange(ticker={"last": 1.0})
    conn._fallback_client = fallback
    conn._fallback_id = "kucoin"

    t = conn.fetch_ticker("BTC/USDT")
    assert t["last"] == 99.0
    assert conn.last_data_source == "binance"
    assert fallback.ticker_calls == 0  # a healthy primary never touches the fallback


def test_market_data_bad_symbol_is_not_masked_by_fallback():
    conn = _connector()
    conn._client = _FakeExchange(raises=ccxt.BadSymbol("no such market"))
    fallback = _FakeExchange(ticker={"last": 1.0})
    conn._fallback_client = fallback
    conn._fallback_id = "kucoin"

    with pytest.raises(ccxt.BadSymbol):
        conn.fetch_ticker("NOPE/USDT")
    assert fallback.ticker_calls == 0  # a genuine data error surfaces, not hidden


def test_geo_block_without_fallback_reraises_honestly():
    conn = _connector()
    conn._client = _FakeExchange(raises=ccxt.ExchangeNotAvailable("451 restricted location"))
    conn._fallback_client = None
    with pytest.raises(ccxt.ExchangeNotAvailable):
        conn.fetch_ticker("BTC/USDT")  # no fallback -> the real 451 still surfaces


# ---- performance analytics (computed from REAL closed trades) --------

import datetime as _dt  # noqa: E402
import types as _types  # noqa: E402

_PERF_T0 = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
_perf_seq = {"i": 0}


def _trade(pnl, *, status="closed", mode="paper", symbol="BTC/USDT", opened=None, closed=None):
    """A minimal stand-in for a Trade row (no DB)."""
    _perf_seq["i"] += 1
    if opened is None:
        opened = _PERF_T0
    if closed is None and status == "closed":
        # monotonically increasing close time -> deterministic close ordering
        closed = _PERF_T0 + _dt.timedelta(minutes=_perf_seq["i"])
    return _types.SimpleNamespace(
        pnl=pnl, status=status, mode=mode, symbol=symbol,
        entry_price=100.0, amount=1.0, side="buy",
        opened_at=opened, closed_at=closed,
    )


def test_performance_empty_is_all_zero_not_fabricated():
    from app.performance import compute_performance

    p = compute_performance([])
    assert p["closed_trades"] == 0
    assert p["win_rate_pct"] == 0.0
    assert p["total_pnl"] == 0.0
    assert p["profit_factor"] is None  # undefined -> null, never a fake number
    assert p["by_symbol"] == []
    assert p["avg_hold_seconds"] is None


def test_performance_ignores_open_trades():
    from app.performance import compute_performance

    p = compute_performance([_trade(50.0, status="open"), _trade(20.0)])
    assert p["closed_trades"] == 1  # the still-open position has no realized result
    assert p["total_pnl"] == 20.0


def test_performance_core_metrics_and_drawdown():
    from app.performance import compute_performance

    # close order +100, -50, -30, +20 -> equity 100, 50, 20, 40 (peak 100)
    p = compute_performance([_trade(100.0), _trade(-50.0), _trade(-30.0), _trade(20.0)])
    assert p["closed_trades"] == 4
    assert p["wins"] == 2 and p["losses"] == 2
    assert p["win_rate_pct"] == 50.0
    assert p["total_pnl"] == 40.0
    assert p["gross_profit"] == 120.0 and p["gross_loss"] == 80.0
    assert p["profit_factor"] == 1.5
    assert p["expectancy"] == 10.0
    assert p["largest_win"] == 100.0 and p["largest_loss"] == -50.0
    assert p["max_drawdown"] == 80.0  # peak 100 down to trough 20


def test_performance_profit_factor_none_when_no_losses():
    from app.performance import compute_performance

    p = compute_performance([_trade(10.0), _trade(5.0)])
    assert p["losses"] == 0
    assert p["profit_factor"] is None
    assert p["win_rate_pct"] == 100.0


def test_performance_splits_paper_and_live():
    from app.performance import compute_performance

    p = compute_performance([
        _trade(10.0, mode="paper"),
        _trade(-4.0, mode="live"),
        _trade(6.0, mode="live"),
    ])
    assert p["paper"]["closed_trades"] == 1 and p["paper"]["total_pnl"] == 10.0
    assert p["live"]["closed_trades"] == 2 and p["live"]["total_pnl"] == 2.0
    assert p["total_pnl"] == 12.0  # overall combines both, honestly


def test_performance_by_symbol_breakdown_sorted():
    from app.performance import compute_performance

    p = compute_performance([
        _trade(5.0, symbol="ETH/USDT"),
        _trade(-2.0, symbol="ETH/USDT"),
        _trade(30.0, symbol="BTC/USDT"),
    ])
    syms = p["by_symbol"]
    assert syms[0]["symbol"] == "BTC/USDT" and syms[0]["pnl"] == 30.0  # best first
    eth = next(s for s in syms if s["symbol"] == "ETH/USDT")
    assert eth["trades"] == 2 and eth["pnl"] == 3.0 and eth["wins"] == 1


def test_performance_avg_hold_seconds_from_timestamps():
    from app.performance import compute_performance

    o = _dt.datetime(2026, 1, 1, 12, 0, tzinfo=_dt.timezone.utc)
    p = compute_performance([_trade(1.0, opened=o, closed=o + _dt.timedelta(hours=2))])
    assert p["avg_hold_seconds"] == 7200.0


def test_performance_equity_curve_lists_closed_events_in_close_order():
    from app.performance import compute_performance

    p = compute_performance([
        _trade(10.0, mode="paper", symbol="BTC/USDT"),
        _trade(-4.0, mode="live", symbol="ETH/USDT"),
        _trade(6.0, mode="paper", symbol="BTC/USDT"),
    ])
    curve = p["equity_curve"]
    assert len(curve) == 3  # one realized event per closed trade
    # Each point carries the raw booked pnl + its mode/symbol — no pre-summed total,
    # because paper and live must never be added into one running equity line.
    assert [pt["pnl"] for pt in curve] == [10.0, -4.0, 6.0]
    assert [pt["mode"] for pt in curve] == ["paper", "live", "paper"]
    assert [pt["symbol"] for pt in curve] == ["BTC/USDT", "ETH/USDT", "BTC/USDT"]
    assert all(pt["t"] is not None for pt in curve)  # real close timestamps
    # The cumulative line is a CLIENT concern, accumulated per mode so simulated
    # and real money stay separate.
    paper_cum, run = [], 0.0
    for pt in curve:
        if pt["mode"] == "paper":
            run += pt["pnl"]
            paper_cum.append(round(run, 8))
    assert paper_cum == [10.0, 16.0]


def test_performance_equity_curve_empty_and_ignores_open():
    from app.performance import compute_performance

    assert compute_performance([])["equity_curve"] == []  # never fabricated
    p = compute_performance([_trade(5.0, status="open"), _trade(8.0)])
    assert len(p["equity_curve"]) == 1  # the still-open position has no realized event
    assert p["equity_curve"][0]["pnl"] == 8.0


# ---- scaled / DCA entries (offline paper engine, price pinned) -------
#
# These exercise the REAL execute_scaled_entry / close_symbol code paths on a
# paper TradingEngine backed by a throwaway SQLite DB, with the price feed
# stubbed so nothing touches the network. Paper mode places NO live order.


class _FakeConn:
    """Minimal offline connector: a fixed spot price, no network, no keys.

    Enough surface for the paper execution path (market/limit/stop/cancel are
    no-op stubs). Mirrors the fake used by the other engine tests.
    """

    has_credentials = False
    connected = True

    def __init__(self, price: float = 100.0):
        self._price = price

    def reload(self, settings):
        pass

    def fetch_price(self, symbol):
        return self._price

    def fetch_ohlcv(self, symbol, timeframe, limit):
        return []

    def fetch_position_amounts(self):
        return {}

    def fetch_balance(self, quote="USDT"):
        return None

    def normalize_amount(self, symbol, amount, price):
        return amount, None

    def create_market_order(self, symbol, side, amount):
        return {"id": "m1", "average": self._price}

    def create_limit_order(self, symbol, side, amount, price):
        return {"id": "l1"}

    def create_stop_loss_order(self, symbol, side, amount, stop_price):
        return None

    def cancel_order(self, order_id, symbol):
        pass

    def fetch_order(self, order_id, symbol):
        return None


@pytest.fixture
def _paper_engine():
    """A paper ``TradingEngine`` on an in-memory SQLite DB, price fixed at 100.

    Yields ``(engine, db)``. Fully offline: a fake connector serves a constant
    price and never touches the network, and paper mode means no exchange order
    is ever placed. The drawdown kill-switch and exposure cap are disabled so
    the DCA logic under test is what decides, not another safeguard.
    """
    from sqlalchemy import create_engine as _create_engine
    from sqlalchemy.orm import sessionmaker

    import app.models  # noqa: F401  (registers ORM tables on Base.metadata)
    from app.database import Base
    from app.engine import TradingEngine

    db_engine = _create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(db_engine)
    db = sessionmaker(bind=db_engine)()

    settings = _settings(
        trading_mode="paper",         # is_live -> False; never a live order
        paper_starting_balance=10_000.0,
        max_open_positions=10,
        risk_per_trade_pct=1.0,
        daily_loss_limit_pct=50.0,    # daily-loss breaker out of the way
        max_drawdown_pct=0.0,         # kill-switch off for deterministic tests
        max_total_exposure_pct=0.0,   # exposure cap off
        default_stop_loss_pct=2.0,
        default_take_profit_pct=4.0,
        min_signal_confidence=0.1,
    )
    engine = TradingEngine(settings, _FakeConn(100.0), user_id=None)
    engine.paper_balance = 10_000.0
    try:
        yield engine, db
    finally:
        db.close()


def _trades(db):
    """Every Trade row in the test DB (any status)."""
    from sqlalchemy import select as _sel

    from app.models import Trade

    return list(db.scalars(_sel(Trade)).all())


def test_scaled_entry_creates_market_plus_resting_legs(_paper_engine):
    from app.models import TradeStatus

    engine, db = _paper_engine
    ok, msg, legs = engine.execute_scaled_entry(
        db, symbol="BTC/USDT", amount=3.0, legs=3, step_pct=1.0,
        first_at_market=True, stop_loss=95.0, take_profit=110.0,
        source="manual", note="scaled entry",
    )
    assert ok is True, msg
    assert len(legs) == 3
    # 1 leg at market (open now) + 2 resting limit legs (pending).
    opens = [t for t in legs if t.status == TradeStatus.open.value]
    pend = [t for t in legs if t.status == TradeStatus.pending.value]
    assert len(opens) == 1 and len(pend) == 2
    # Total base qty (3.0) split equally across the 3 legs.
    assert all(abs(t.amount - 1.0) < 1e-9 for t in legs)
    # Ladder DOWN from the current price, 1% apart: 100, 99, 98.
    assert sorted(t.entry_price for t in legs) == pytest.approx([98.0, 99.0, 100.0])
    # The provided SL/TP protect EVERY leg (the open one now; resting on fill).
    assert all(abs(t.stop_loss - 95.0) < 1e-9 for t in legs)
    assert all(abs(t.take_profit - 110.0) < 1e-9 for t in legs)
    # Paper wallet reserved exactly the sum of leg notionals: 100+99+98 = 297.
    assert engine.paper_balance == pytest.approx(10_000.0 - 297.0)


def test_scaled_entry_all_resting_when_not_first_at_market(_paper_engine):
    from app.models import TradeStatus

    engine, db = _paper_engine
    ok, msg, legs = engine.execute_scaled_entry(
        db, symbol="ETH/USDT", amount=2.0, legs=2, step_pct=1.0,
        first_at_market=False, stop_loss=None, take_profit=None,
        source="manual", note=None,
    )
    assert ok is True, msg
    assert len(legs) == 2
    assert all(t.status == TradeStatus.pending.value for t in legs)
    # Both rest as limits: first at the current price, the next 1% below.
    assert sorted(t.limit_price for t in legs) == pytest.approx([99.0, 100.0])
    # Resting legs with no explicit stop stay unset until they FILL (when
    # _fill_pending assigns the auto SL/TP) — nothing fake is written early.
    assert all(t.stop_loss is None and t.take_profit is None for t in legs)
    assert engine.paper_balance == pytest.approx(10_000.0 - 199.0)


def test_scaled_entry_rejects_when_not_enough_slots(_paper_engine):
    engine, db = _paper_engine
    engine.settings.max_open_positions = 2  # only 2 free, entry needs 3
    ok, msg, legs = engine.execute_scaled_entry(
        db, symbol="BTC/USDT", amount=3.0, legs=3, step_pct=1.0,
        first_at_market=True, stop_loss=None, take_profit=None,
        source="manual", note=None,
    )
    assert ok is False
    assert "slots" in msg.lower() or "free" in msg.lower()
    assert legs == []
    assert _trades(db) == []                       # nothing placed on rejection
    assert engine.paper_balance == pytest.approx(10_000.0)  # wallet untouched


def test_scaled_entry_rejects_existing_position(_paper_engine):
    engine, db = _paper_engine
    ok, _msg, _legs = engine.execute_scaled_entry(
        db, symbol="BTC/USDT", amount=2.0, legs=2, step_pct=1.0,
        first_at_market=True, stop_loss=None, take_profit=None,
        source="manual", note=None,
    )
    assert ok is True
    # A second scale-in on the same symbol must be refused, not stacked.
    ok2, msg2, legs2 = engine.execute_scaled_entry(
        db, symbol="BTC/USDT", amount=2.0, legs=2, step_pct=1.0,
        first_at_market=True, stop_loss=None, take_profit=None,
        source="manual", note=None,
    )
    assert ok2 is False
    assert "already" in msg2.lower()
    assert legs2 == []


def test_scaled_entry_blocked_by_killswitch(_paper_engine):
    engine, db = _paper_engine
    # Arm the REAL drawdown kill-switch (needs a positive budget to be active).
    engine.settings.max_drawdown_pct = 25.0
    engine._killswitch_tripped = True
    ok, msg, legs = engine.execute_scaled_entry(
        db, symbol="BTC/USDT", amount=3.0, legs=3, step_pct=1.0,
        first_at_market=True, stop_loss=None, take_profit=None,
        source="manual", note=None,
    )
    assert ok is False
    assert "kill-switch" in msg.lower()
    assert legs == [] and _trades(db) == []


def test_scaled_entry_rejects_below_two_legs(_paper_engine):
    engine, db = _paper_engine
    ok, msg, legs = engine.execute_scaled_entry(
        db, symbol="BTC/USDT", amount=1.0, legs=1, step_pct=1.0,
        first_at_market=True, stop_loss=None, take_profit=None,
        source="manual", note=None,
    )
    assert ok is False
    assert "2 legs" in msg
    assert legs == [] and _trades(db) == []


def test_scaled_entry_risk_sizes_when_amount_blank(_paper_engine):
    engine, db = _paper_engine
    # amount=None -> the risk manager sizes the WHOLE entry, then it's split.
    ok, msg, legs = engine.execute_scaled_entry(
        db, symbol="BTC/USDT", amount=None, legs=2, step_pct=1.0,
        first_at_market=True, stop_loss=98.0, take_profit=None,
        source="manual", note=None,
    )
    assert ok is True, msg
    assert len(legs) == 2
    # Equal slices of the risk-sized total, each a positive quantity.
    assert legs[0].amount == pytest.approx(legs[1].amount)
    assert all(t.amount > 0 for t in legs)
    # Total notional stays within available equity (sizing respects the budget).
    total_notional = sum(t.amount * t.entry_price for t in legs)
    assert total_notional <= 10_000.0 + 1e-6


def test_close_symbol_closes_and_cancels_all_legs(_paper_engine):
    from app.models import TradeStatus

    engine, db = _paper_engine
    ok, _msg, legs = engine.execute_scaled_entry(
        db, symbol="BTC/USDT", amount=3.0, legs=3, step_pct=1.0,
        first_at_market=True, stop_loss=None, take_profit=None,
        source="manual", note=None,
    )
    assert ok is True and len(legs) == 3

    affected, realized, msgs = engine.close_symbol(db, "BTC/USDT")
    assert affected == 3
    assert len(msgs) == 3
    # Closed at the same price it opened (100) and cancelled the rest -> ~0 PnL.
    assert realized == pytest.approx(0.0)
    # No open or resting leg remains for the symbol.
    remaining = [
        t for t in _trades(db)
        if t.status in (TradeStatus.open.value, TradeStatus.pending.value)
    ]
    assert remaining == []
    # Every reservation is returned: the paper wallet is whole again.
    assert engine.paper_balance == pytest.approx(10_000.0)
