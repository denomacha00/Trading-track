"""API smoke tests using FastAPI's TestClient (no network, no live server).

Multi-user mode: every protected endpoint requires a Bearer token, so the
fixtures sign up an admin (auto-licensed) and attach the token to the client.
Exchange-dependent endpoints (ticker/ohlcv/backtest/train) are not exercised
here because they require Binance connectivity; those are covered by unit tests
on the underlying modules.
"""
from __future__ import annotations

import atexit
import os
import tempfile

os.environ.setdefault("TRADING_MODE", "paper")

# Run the suite against a private, throwaway SQLite file — never the developer's
# persistent tranding_track.db. A stale dev DB (e.g. a legacy admin row created
# before the `username` column existed) otherwise poisons fixtures. This MUST be
# set before importing app.database, which binds its engine at import time.
_TEST_DB = os.path.join(tempfile.gettempdir(), f"tt_test_{os.getpid()}.db")
for _p in (_TEST_DB, _TEST_DB + "-wal", _TEST_DB + "-shm"):
    try:
        os.remove(_p)
    except OSError:
        pass
os.environ["DATABASE_URL"] = "sqlite:///" + _TEST_DB.replace("\\", "/")


@atexit.register
def _cleanup_test_db() -> None:
    for _p in (_TEST_DB, _TEST_DB + "-wal", _TEST_DB + "-shm"):
        try:
            os.remove(_p)
        except OSError:
            pass


import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "supersecret123"


@pytest.fixture(scope="module")
def client():
    from app.database import init_db

    # Enable multi-user auth on the cached settings singleton. Env overrides may
    # not take effect (config is import-cached), so we set attributes directly.
    s = get_settings()
    s.secret_key = "unit-test-secret-key"
    s.auto_license_new_users = True  # signups start licensed for these tests
    s.admin_email = ADMIN_EMAIL
    s.rate_limit_enabled = False  # don't throttle the many signups in this suite

    init_db()  # ensure tables exist regardless of lifespan ordering
    with TestClient(app) as c:
        # First user matching admin_email becomes the licensed admin.
        r = c.post(
            "/api/auth/signup",
            json={
                "username": "admin",
                "email": ADMIN_EMAIL,
                "password": ADMIN_PASSWORD,
            },
        )
        if r.status_code == 409:
            r = c.post(
                "/api/auth/login",
                json={"identifier": ADMIN_EMAIL, "password": ADMIN_PASSWORD},
            )
        token = r.json()["access_token"]
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c


def _webhook_path(client) -> str:
    return client.get("/api/auth/me").json()["webhook_path"]


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_requires_auth(client):
    # A bare request with no bearer token must be rejected.
    r = TestClient(app).get("/api/status")
    assert r.status_code == 401


def test_me_shape(client):
    body = client.get("/api/auth/me").json()
    assert body["email"] == ADMIN_EMAIL
    assert body["username"] == "admin"
    assert body["role"] == "admin"
    assert body["license_status"] == "active"
    assert body["license_active"] is True
    assert body["webhook_path"].startswith("/api/webhook/tradingview/")


def test_login_bad_password(client):
    r = client.post(
        "/api/auth/login", json={"identifier": ADMIN_EMAIL, "password": "wrong"}
    )
    assert r.status_code == 401


def test_status_shape(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["trading_mode"] == "paper"
    assert "equity" in body and "open_positions" in body


def test_exchange_access_shape(client):
    r = client.get("/api/exchange/access")
    assert r.status_code == 200
    body = r.json()
    for key in ("ok", "can_read_public", "can_read_account", "can_trade", "detail"):
        assert key in body
    assert body["ok"] is False
    assert isinstance(body["detail"], str) and body["detail"]


def test_webhook_unknown_token(client):
    r = client.post(
        "/api/webhook/tradingview/nope-not-a-real-token",
        content=b'{"action":"buy","symbol":"BTC/USDT","amount":0.01}',
    )
    assert r.status_code == 404


def test_webhook_rejects_malformed(client):
    path = _webhook_path(client)
    r = client.post(path, content=b"not json")
    assert r.status_code == 400


def test_bot_start_stop(client):
    assert client.post("/api/bot/stop").json()["running"] is False
    assert client.post("/api/bot/start").json()["running"] is True
    assert client.post("/api/bot/invalid").status_code == 400


def test_settings_roundtrip(client):
    r = client.patch(
        "/api/settings", json={"max_open_positions": 7, "risk_per_trade_pct": 2.5}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["max_open_positions"] == 7
    assert body["risk_per_trade_pct"] == 2.5
    assert body["webhook_path"].startswith("/api/webhook/tradingview/")


def test_settings_validation(client):
    r = client.patch("/api/settings", json={"risk_per_trade_pct": 999})
    assert r.status_code == 422


def test_strategies_listed(client):
    r = client.get("/api/strategies")
    assert r.status_code == 200
    names = {s["name"] for s in r.json()}
    assert {"ma_cross", "rsi"}.issubset(names)


def test_ai_ask_requires_question(client):
    r = client.post("/api/ai/ask", json={})
    assert r.status_code == 400


def test_ai_ask_without_key_falls_back(client):
    # Force the current user's engine AI off so the fallback path is exercised
    # regardless of any real key in the environment.
    from app.usermgr import get_manager
    from app.database import SessionLocal
    from app.models import User
    from sqlalchemy import select

    db = SessionLocal()
    try:
        user = db.scalars(select(User).where(User.email == ADMIN_EMAIL)).first()
        engine = get_manager().get(db, user)
        engine.ai._settings.ai_api_key = ""
    finally:
        db.close()
    r = client.post("/api/ai/ask", json={"question": "what is the trend?"})
    assert r.status_code == 200
    body = r.json()
    assert body["ai_enabled"] is False
    assert "not configured" in body["answer"].lower()


def test_settings_expose_autonomous_fields(client):
    body = client.get("/api/settings").json()
    for key in ("auto_trade_enabled", "auto_symbols", "auto_timeframe",
                "min_signal_confidence", "ai_enabled"):
        assert key in body


def test_settings_update_autonomous(client):
    r = client.patch(
        "/api/settings",
        json={"auto_trade_enabled": True, "auto_symbols": "BTC/USDT,ETH/USDT",
              "min_signal_confidence": 0.6},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["auto_trade_enabled"] is True
    assert body["min_signal_confidence"] == 0.6
    assert client.patch("/api/settings", json={"min_signal_confidence": 5}).status_code == 422


def test_settings_expose_new_fields(client):
    body = client.get("/api/settings").json()
    for key in ("trailing_stop_pct", "notifications_enabled", "api_key_set"):
        assert key in body


def test_trailing_stop_validation(client):
    assert client.patch("/api/settings", json={"trailing_stop_pct": 1.5}).status_code == 200
    assert client.patch("/api/settings", json={"trailing_stop_pct": 500}).status_code == 422


def test_trades_empty_initially(client):
    r = client.get("/api/trades")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_performance_endpoint_shape_no_trades(client):
    r = client.get("/api/performance")
    assert r.status_code == 200
    p = r.json()
    # No trades yet: honest zeros, undefined profit factor is null (not faked).
    assert p["closed_trades"] == 0
    assert p["total_pnl"] == 0.0
    assert p["profit_factor"] is None
    assert p["by_symbol"] == []
    # Paper/live are always split so simulated gains never look like real money.
    assert p["paper"]["closed_trades"] == 0
    assert p["live"]["closed_trades"] == 0


# ---- multi-user, licensing & admin ---------------------------------


def _signup(client, email, password="password123", username=None, license_key=None):
    # Derive a valid, unique username from the email local-part unless one is
    # given (e.g. "trader1@example.com" -> "trader1").
    username = username or email.split("@", 1)[0]
    payload = {"username": username, "email": email, "password": password}
    if license_key is not None:
        payload["license_key"] = license_key
    return client.post("/api/auth/signup", json=payload)


def test_admin_can_list_and_license_users(client):
    # Create a second user (tolerate re-runs against the persistent dev DB),
    # then flip their licence via the admin endpoint.
    r = _signup(client, "trader1@example.com")
    assert r.status_code in (200, 409)
    users = client.get("/api/admin/users").json()
    target = next(u for u in users if u["email"] == "trader1@example.com")

    revoke = client.patch(
        f"/api/admin/users/{target['id']}/license", json={"status": "revoked"}
    )
    assert revoke.status_code == 200
    assert revoke.json()["license_status"] == "revoked"

    grant = client.patch(
        f"/api/admin/users/{target['id']}/license", json={"status": "active"}
    )
    assert grant.status_code == 200
    assert grant.json()["license_status"] == "active"
    assert grant.json()["licensed_at"] is not None
    # A manual admin grant is a lifetime licence (no expiry).
    assert grant.json()["license_expires_at"] is None
    assert grant.json()["license_active"] is True


def test_non_admin_forbidden_from_admin_routes(client):
    _signup(client, "trader2@example.com", "password123")
    login = client.post(
        "/api/auth/login",
        json={"identifier": "trader2@example.com", "password": "password123"},
    )
    token = login.json()["access_token"]
    r = TestClient(app).get(
        "/api/admin/users", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 403


def test_login_by_username_or_email(client):
    # Sign up a user, then confirm login works with BOTH the username and email.
    _signup(client, "byname@example.com", "password123", username="by_name")
    for ident in ("by_name", "BYNAME@EXAMPLE.COM"):  # username + case-insensitive email
        r = client.post(
            "/api/auth/login", json={"identifier": ident, "password": "password123"}
        )
        assert r.status_code == 200, ident
        assert r.json()["access_token"]


def test_signup_requires_license_key_when_not_auto(client):
    # With auto-licensing OFF and no key, a normal signup is rejected — a client
    # must redeem the key they bought to create a live account.
    s = get_settings()
    s.auto_license_new_users = False
    try:
        r = _signup(client, "nokey@example.com", "password123")
    finally:
        s.auto_license_new_users = True
    assert r.status_code == 400
    assert "licence key" in r.json()["detail"].lower()


def test_signup_with_license_key_activates_and_sets_days(client):
    # Admin mints a 30-day key; a client redeems it at signup and is live at once.
    import uuid

    tag = uuid.uuid4().hex[:8]  # unique labels so the row is easy to find
    key = client.post(
        "/api/admin/license-keys", json={"label": "Client A", "duration_days": 30}
    ).json()["key"]
    s = get_settings()
    s.auto_license_new_users = False
    try:
        r = _signup(
            client,
            f"keyed-{tag}@example.com",
            "password123",
            username=f"keyed_{tag}",
            license_key=key,
        )
        assert r.status_code == 200
        token = r.json()["access_token"]
        me = TestClient(app).get(
            "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
        ).json()
        assert me["license_status"] == "active"
        assert me["license_active"] is True
        assert me["license_days_left"] in (29, 30)  # ceil of ~30 days
        # The same key cannot be reused by another client. This MUST run while
        # auto_license is still off, otherwise the key would be bypassed, not
        # consumed — hence it lives inside the try, before finally restores it.
        again = _signup(
            client,
            f"keyed2-{tag}@example.com",
            "password123",
            username=f"keyed2_{tag}",
            license_key=key,
        )
        assert again.status_code == 400
    finally:
        s.auto_license_new_users = True


def test_admin_add_days_extends_licence(client):
    r = _signup(client, "adddays@example.com", "password123")
    assert r.status_code in (200, 409)
    users = client.get("/api/admin/users").json()
    target = next(u for u in users if u["email"] == "adddays@example.com")
    add = client.post(
        f"/api/admin/users/{target['id']}/add-days", json={"days": 7}
    )
    assert add.status_code == 200
    body = add.json()
    assert body["license_status"] == "active"
    assert body["license_active"] is True
    assert body["license_days_left"] in (6, 7)


def test_pending_user_cannot_trade(client):
    # A user whose licence has EXPIRED must not be able to place orders.
    from app.models import User, _utcnow
    import datetime as _dt
    from app.database import SessionLocal

    _signup(client, "expired@example.com", "password123")
    # Force their licence to be active-but-expired directly in the DB.
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.email == "expired@example.com").first()
        u.license_status = "active"
        u.license_expires_at = _utcnow() - _dt.timedelta(days=1)
        db.commit()
    finally:
        db.close()
    login = client.post(
        "/api/auth/login",
        json={"identifier": "expired@example.com", "password": "password123"},
    )
    token = login.json()["access_token"]
    hdr = {"Authorization": f"Bearer {token}"}
    tc = TestClient(app)
    me = tc.get("/api/auth/me", headers=hdr).json()
    assert me["license_status"] == "active" and me["license_active"] is False
    r = tc.post(
        "/api/order",
        headers=hdr,
        json={"action": "buy", "symbol": "BTC/USDT", "amount": 0.01},
    )
    assert r.status_code == 403


def test_trades_isolated_per_user(client):
    # Admin places a paper order; a fresh user must not see it.
    order = client.post(
        "/api/order",
        json={"action": "buy", "symbol": "BTC/USDT", "amount": 0.01},
    )
    assert order.status_code == 200
    _signup(client, "trader3@example.com", "password123")
    login = client.post(
        "/api/auth/login",
        json={"identifier": "trader3@example.com", "password": "password123"},
    )
    token = login.json()["access_token"]
    other = TestClient(app).get(
        "/api/trades", headers={"Authorization": f"Bearer {token}"}
    )
    assert other.status_code == 200
    assert other.json() == []


def test_duplicate_signup_rejected(client):
    _signup(client, "dup-check@example.com")
    assert _signup(client, "dup-check@example.com").status_code == 409


def test_duplicate_username_rejected(client):
    r1 = _signup(client, "sameuser1@example.com", username="taken_name")
    assert r1.status_code in (200, 409)
    r2 = _signup(client, "sameuser2@example.com", username="taken_name")
    assert r2.status_code == 409


def test_signup_weak_password_rejected(client):
    r = client.post(
        "/api/auth/signup",
        json={"username": "weakling", "email": "weak@example.com", "password": "short"},
    )
    assert r.status_code == 422


# ---- Order book (real resting liquidity, never fabricated) -----------

def test_orderbook_shape_and_filters_bad_levels(client, monkeypatch):
    """The endpoint returns the venue's real depth and drops any non-positive
    price/amount — it must never invent a ladder to fill gaps."""
    import app.exchange as exch

    def fake_ob(self, symbol, limit=20):
        return {
            "bids": [[100.0, 2.0], [99.5, 1.0], [0, 5.0], [98.0, 0]],
            "asks": [[101.0, 1.5], [-1, 2.0], [102.0, 3.0], ["x", 1]],
        }

    monkeypatch.setattr(exch.BinanceConnector, "fetch_order_book", fake_ob)
    r = client.get("/api/orderbook/btc%2Fusdt?limit=20")
    assert r.status_code == 200
    body = r.json()
    assert body["symbol"] == "BTC/USDT"
    # Only levels with price > 0 AND amount > 0 survive.
    assert body["bids"] == [
        {"price": 100.0, "amount": 2.0},
        {"price": 99.5, "amount": 1.0},
    ]
    assert body["asks"] == [
        {"price": 101.0, "amount": 1.5},
        {"price": 102.0, "amount": 3.0},
    ]
    assert "source" in body  # honest venue label (may be null)


def test_orderbook_failure_is_502_not_faked(client, monkeypatch):
    """When the venue can't be reached the endpoint 502s honestly rather than
    returning an empty or invented book."""
    import app.exchange as exch

    def boom(self, symbol, limit=20):
        raise RuntimeError("exchange down")

    monkeypatch.setattr(exch.BinanceConnector, "fetch_order_book", boom)
    r = client.get("/api/orderbook/BTC/USDT")
    assert r.status_code == 502
    assert "unavailable" in r.json()["detail"].lower()


# ---- saved strategies + live-key safety gate ------------------------


def test_saved_strategies_start_empty(client):
    r = client.get("/api/strategies/saved")
    assert r.status_code == 200
    assert r.json() == []


def test_delete_missing_saved_strategy_is_404(client):
    r = client.delete("/api/strategies/saved/BTC/USDT")
    assert r.status_code == 404


def test_switch_to_live_without_keys_is_refused(client):
    # A money-safety gate: you can't flip to LIVE with no permissioned key, or
    # the bot would "trade live" while every order silently fails. The test
    # admin has no Binance keys, so this must be refused and stay on paper.
    r = client.patch("/api/settings", json={"trading_mode": "live"})
    assert r.status_code == 400
    assert "live" in r.json()["detail"].lower()
    # Mode is unchanged — still paper.
    assert client.get("/api/settings").json()["trading_mode"] == "paper"


# ---- AI assistant "hands": proposed-action validation + wiring -------------
# The assistant SUGGESTS an action; _normalize_proposed_action turns its raw tag
# into a safe, allowlisted proposal (or None). It never executes — that only
# happens when the operator confirms and the frontend calls the real endpoint.


def test_normalize_order_defaults_amount_to_none():
    from app.main import _normalize_proposed_action

    a = _normalize_proposed_action(
        {"type": "order", "side": "buy", "symbol": "btc/usdt", "reason": "momentum"},
        None,
    )
    assert a == {
        "type": "order",
        "side": "buy",
        "symbol": "BTC/USDT",  # normalised upper-case
        "reason": "momentum",
        "amount": None,  # null => risk manager sizes it
        "auto": False,  # no engine/autopilot off => confirm-gated
    }


def test_normalize_order_rejects_bad_side_and_symbol():
    from app.main import _normalize_proposed_action

    assert _normalize_proposed_action({"type": "order", "side": "yolo", "symbol": "BTC/USDT"}, None) is None
    assert _normalize_proposed_action({"type": "order", "side": "buy", "symbol": "BTC"}, None) is None


def test_normalize_order_keeps_explicit_prices():
    from app.main import _normalize_proposed_action

    a = _normalize_proposed_action(
        {"type": "order", "side": "buy", "symbol": "BTC/USDT", "amount": 0.01, "stop_loss": 60000},
        None,
    )
    assert a["amount"] == 0.01
    assert a["stop_loss"] == 60000.0
    assert "take_profit" not in a  # omitted stays omitted (engine applies defaults)


def test_normalize_settings_allowlist_drops_trading_mode():
    from app.main import _normalize_proposed_action

    a = _normalize_proposed_action(
        {
            "type": "settings",
            "changes": {
                "default_stop_loss_pct": "2.5",  # coerced to float
                "max_open_positions": "3",  # coerced to int
                "use_saved_strategy": "true",  # coerced to bool
                "trading_mode": "live",  # NOT allowlisted -> dropped
                "bogus_field": 1,  # unknown -> dropped
            },
        },
        None,
    )
    assert a["type"] == "settings"
    assert a["changes"] == {
        "default_stop_loss_pct": 2.5,
        "max_open_positions": 3,
        "use_saved_strategy": True,
    }
    assert "trading_mode" not in a["changes"]


def test_normalize_settings_all_unknown_is_none():
    from app.main import _normalize_proposed_action

    assert _normalize_proposed_action({"type": "settings", "changes": {"trading_mode": "live"}}, None) is None


def test_normalize_bot_and_train():
    from app.main import _normalize_proposed_action

    assert _normalize_proposed_action({"type": "bot", "state": "start"}, None)["state"] == "start"
    assert _normalize_proposed_action({"type": "bot", "state": "explode"}, None) is None
    # Unknown strategy is refused; a real one from the registry is accepted.
    assert _normalize_proposed_action({"type": "train", "symbol": "BTC/USDT", "strategy": "made_up"}, None) is None
    t = _normalize_proposed_action(
        {"type": "train", "symbol": "eth/usdt", "strategy": "ma_cross", "timeframe": "4h"}, None
    )
    assert t == {
        "type": "train",
        "symbol": "ETH/USDT",
        "strategy": "ma_cross",
        "timeframe": "4h",
        "reason": None,
        "auto": False,  # no engine/autopilot off => confirm-gated
    }


def test_normalize_garbage_is_none():
    from app.main import _normalize_proposed_action

    assert _normalize_proposed_action(None, None) is None
    assert _normalize_proposed_action({"type": "nonsense"}, None) is None
    assert _normalize_proposed_action("not a dict", None) is None


def test_normalize_alert():
    from app.main import _normalize_proposed_action

    a = _normalize_proposed_action(
        {"type": "alert", "symbol": "btc/usdt", "condition": "Above", "price": "65000", "note": "watch"},
        None,
    )
    assert a == {
        "type": "alert",
        "symbol": "BTC/USDT",  # upper-cased
        "condition": "above",  # lower-cased
        "price": 65000.0,  # coerced to float
        "note": "watch",
        "reason": None,
        "auto": False,
    }
    # A bad condition, non-positive price, or symbol without a pair is refused.
    assert _normalize_proposed_action({"type": "alert", "symbol": "BTC/USDT", "condition": "sideways", "price": 1}, None) is None
    assert _normalize_proposed_action({"type": "alert", "symbol": "BTC/USDT", "condition": "above", "price": 0}, None) is None
    assert _normalize_proposed_action({"type": "alert", "symbol": "BTC", "condition": "above", "price": 1}, None) is None


class _AutopilotEngine:
    """Minimal engine stub exposing just the settings the normalizer reads."""

    class _S:
        def __init__(self, autopilot, live):
            self.ai_autopilot_enabled = autopilot
            self.is_live = live

    def __init__(self, autopilot=False, live=False):
        self.settings = self._S(autopilot, live)


def test_autopilot_flag_gates_live_orders_but_not_paper():
    from app.main import _normalize_proposed_action

    order = {"type": "order", "side": "buy", "symbol": "BTC/USDT"}

    # Autopilot ON + paper: a paper order, a settings change, a bot toggle and an
    # alert are all auto-applied (the safe subset).
    eng = _AutopilotEngine(autopilot=True, live=False)
    assert _normalize_proposed_action(order, eng)["auto"] is True
    assert _normalize_proposed_action({"type": "settings", "changes": {"max_open_positions": 3}}, eng)["auto"] is True
    assert _normalize_proposed_action({"type": "bot", "state": "start"}, eng)["auto"] is True
    assert _normalize_proposed_action({"type": "alert", "symbol": "BTC/USDT", "condition": "above", "price": 65000}, eng)["auto"] is True

    # Autopilot ON + LIVE: a real-money order is NEVER auto (always confirm-gated),
    # even though non-money actions still are.
    eng_live = _AutopilotEngine(autopilot=True, live=True)
    assert _normalize_proposed_action(order, eng_live)["auto"] is False
    assert _normalize_proposed_action({"type": "bot", "state": "start"}, eng_live)["auto"] is True

    # Autopilot OFF: nothing is auto — every action waits for a manual Confirm.
    eng_off = _AutopilotEngine(autopilot=False, live=False)
    assert _normalize_proposed_action(order, eng_off)["auto"] is False
    assert _normalize_proposed_action({"type": "settings", "changes": {"max_open_positions": 3}}, eng_off)["auto"] is False


def _admin_engine():
    """The admin user's cached engine — the same instance the endpoints use."""
    from app.usermgr import get_manager
    from app.database import SessionLocal
    from app.models import User
    from sqlalchemy import select

    db = SessionLocal()
    user = db.scalars(select(User).where(User.email == ADMIN_EMAIL)).first()
    engine = get_manager().get(db, user)
    uid = user.id  # read before close() so the detached instance isn't refreshed
    db.close()
    return engine, uid


def test_ai_chat_returns_validated_proposed_action(client, monkeypatch):
    # Monkeypatch the model so it "proposes" an order via the tag protocol; the
    # endpoint must strip the tag from the reply and return the validated action.
    engine, _ = _admin_engine()

    def fake_chat(question, **kwargs):
        return (
            "I'll place a market buy on BTC, sized by your risk manager.\n"
            '[[action:{"type":"order","side":"buy","symbol":"BTC/USDT","amount":null,"reason":"trend up"}]]'
        )

    monkeypatch.setattr(engine.ai, "chat", fake_chat)
    r = client.post("/api/ai/chat", json={"question": "buy me some btc safely"})
    assert r.status_code == 200
    body = r.json()
    assert "[[action" not in body["reply"]  # tag hidden from the user
    assert body["proposed_action"] == {
        "type": "order",
        "side": "buy",
        "symbol": "BTC/USDT",
        "reason": "trend up",
        "amount": None,
        "auto": False,  # paper account, autopilot off => confirm-gated
    }


def test_ai_chat_no_action_when_none_proposed(client, monkeypatch):
    engine, _ = _admin_engine()
    monkeypatch.setattr(engine.ai, "chat", lambda q, **k: "Momentum looks weak; I'd wait.")
    r = client.post("/api/ai/chat", json={"question": "should i buy?"})
    assert r.status_code == 200
    assert r.json()["proposed_action"] is None


def test_ai_chat_context_has_trading_hours(client, monkeypatch):
    # The failing example from the field: "how many hours was my trading for". The
    # grounding context must carry real durations derived from trade timestamps.
    import datetime as dt
    from app.database import SessionLocal
    from app.models import Trade, _utcnow

    engine, uid = _admin_engine()
    db = SessionLocal()
    tr = Trade(
        user_id=uid,
        symbol="BTC/USDT",
        side="buy",
        amount=0.01,
        entry_price=60000.0,
        status="open",
        pnl=12.5,
        mode="paper",
        opened_at=_utcnow() - dt.timedelta(hours=5),
    )
    db.add(tr)
    db.commit()
    trade_id = tr.id
    db.close()

    captured = {}

    def fake_chat(question, **kwargs):
        captured["ctx"] = kwargs.get("bot_context") or ""
        return "ok"

    monkeypatch.setattr(engine.ai, "chat", fake_chat)
    try:
        r = client.post("/api/ai/chat", json={"question": "how long have i been trading?"})
        assert r.status_code == 200
        ctx = captured["ctx"]
        assert "Trading activity" in ctx
        assert "hours" in ctx
        assert "BTC/USDT" in ctx
        # The open position reports its running P&L direction (real, not faked).
        assert "up" in ctx
    finally:
        db = SessionLocal()
        obj = db.get(Trade, trade_id)
        if obj:
            db.delete(obj)
            db.commit()
        db.close()


# ---- read-only live data mode / real symbols / paper reset -----------------
# Product rule: LIVE means REAL data. A read-only key (reads the account but
# Spot trading not enabled) must still flip to live and show real balances and
# market data, while ORDER placement is refused with an honest message — never a
# fake fill, never a silent -2015.

def _sub_client(email, password="password123"):
    """A TestClient authenticated as a freshly-signed-up (auto-licensed) user."""
    c = TestClient(app)
    r = c.post("/api/auth/signup", json={
        "username": email.split("@", 1)[0], "email": email, "password": password,
    })
    if r.status_code == 409:
        r = c.post("/api/auth/login", json={"identifier": email, "password": password})
    c.headers.update({"Authorization": f"Bearer {r.json()['access_token']}"})
    return c


def _engine_by_email(email):
    from app.usermgr import get_manager
    from app.database import SessionLocal
    from app.models import User
    from sqlalchemy import select
    db = SessionLocal()
    try:
        user = db.scalars(select(User).where(User.email == email)).first()
        return get_manager().get(db, user), user.id
    finally:
        db.close()

# __APPEND_MARKER__

def test_live_switch_allows_readonly_key_and_blocks_orders(client):
    email = "ro-live@example.com"
    c = _sub_client(email)
    assert c.put("/api/credentials", json={
        "binance_api_key": "k" * 12, "binance_api_secret": "s" * 12,
        "binance_testnet": False,
    }).status_code == 200
    eng, _ = _engine_by_email(email)
    # Account READABLE, trading NOT enabled — the real read-only situation.
    eng.connector._probe_trading_access = lambda: {
        "ok": True, "can_read_public": True, "can_read_account": True,
        "can_trade": False, "detail": "read-only key",
    }
    r = c.patch("/api/settings", json={"trading_mode": "live"})
    assert r.status_code == 200, r.text
    assert r.json()["trading_mode"] == "live"  # live allowed with a read-only key
    # Real data is live; placing an order is refused honestly (200 + accepted False).
    r = c.post("/api/order", json={
        "action": "buy", "symbol": "BTC/USDT", "amount": 0.001,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is False
    assert "read-only" in body["message"].lower()
    assert eng.connector.can_trade is False  # probe result cached on the connector


def test_live_switch_refused_when_account_unreadable(client):
    email = "noread-live@example.com"
    c = _sub_client(email)
    assert c.put("/api/credentials", json={
        "binance_api_key": "k" * 12, "binance_api_secret": "s" * 12,
        "binance_testnet": False,
    }).status_code == 200
    eng, _ = _engine_by_email(email)
    eng.connector._probe_trading_access = lambda: {
        "ok": False, "can_read_public": True, "can_read_account": False,
        "can_trade": False, "detail": "key can't read account",
    }
    r = c.patch("/api/settings", json={"trading_mode": "live"})
    assert r.status_code == 400  # no real data to show -> refuse the switch
    assert "can't read" in r.json()["detail"].lower()

# __APPEND_MARKER2__

def test_reset_paper_data_preserves_live_trades(client):
    from app.database import SessionLocal
    from app.models import Trade, SignalLog, TradeStatus
    from sqlalchemy import select
    email = "reset-paper@example.com"
    _sub_client(email)
    eng, uid = _engine_by_email(email)
    db = SessionLocal()
    try:
        db.add(Trade(user_id=uid, symbol="BTC/USDT", side="buy", amount=0.1,
                     entry_price=100.0, status=TradeStatus.open.value, mode="paper"))
        db.add(Trade(user_id=uid, symbol="ETH/USDT", side="buy", amount=1.0,
                     entry_price=50.0, status=TradeStatus.open.value, mode="live"))
        db.add(SignalLog(user_id=uid, source="analyzer", symbol="BTC/USDT",
                         action="buy", raw="{}", accepted=1))
        db.commit()
        eng.paper_balance = 12345.0
        out = eng.reset_paper_data(db)
        assert out["trades_deleted"] == 1
        assert out["signals_deleted"] == 1
        assert out["paper_balance"] == eng.settings.paper_starting_balance
        paper = db.scalars(select(Trade).where(
            Trade.user_id == uid, Trade.mode == "paper")).all()
        live = db.scalars(select(Trade).where(
            Trade.user_id == uid, Trade.mode == "live")).all()
        assert len(paper) == 0 and len(live) == 1  # real trades untouched
    finally:
        for t in db.scalars(select(Trade).where(Trade.user_id == uid)).all():
            db.delete(t)
        db.commit()
        db.close()

# __APPEND_MARKER3__

def test_connector_list_symbols_filters_and_hoists():
    from app.exchange import BinanceConnector
    from app.config import Settings
    conn = BinanceConnector(Settings())
    conn._markets = {
        "AAVE/USDT": {"spot": True, "active": True, "quote": "USDT"},
        "BTC/USDT": {"spot": True, "active": True, "quote": "USDT"},
        "ETH/USDT": {"spot": True, "active": True, "quote": "USDT"},
        "DOGE/BTC": {"spot": True, "active": True, "quote": "BTC"},    # wrong quote
        "OLD/USDT": {"spot": True, "active": False, "quote": "USDT"},  # inactive
        "PERP/USDT": {"spot": False, "active": True, "quote": "USDT"}, # not spot
    }
    syms = conn.list_symbols(quote="USDT")
    assert syms[:2] == ["BTC/USDT", "ETH/USDT"]  # majors hoisted, in order
    assert "AAVE/USDT" in syms                    # other real USDT spot pair kept
    assert "DOGE/BTC" not in syms                 # wrong quote filtered out
    assert "OLD/USDT" not in syms                 # inactive filtered out
    assert "PERP/USDT" not in syms                # non-spot filtered out


def test_symbols_endpoint_shape(client):
    r = client.get("/api/symbols")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body["symbols"], list)  # real list (may be empty offline)
    assert body["quote"] == "USDT"


def test_paper_reset_endpoint_shape(client):
    r = client.post("/api/paper/reset")
    assert r.status_code == 200
    for k in ("trades_deleted", "signals_deleted", "paper_balance"):
        assert k in r.json()


# ---- Security hardening (redaction, limit clamps, account teardown) ----

def test_redact_raw_masks_nested_secrets():
    # A secret buried inside nested dicts/lists must be masked too, not just at
    # the top level — otherwise it would rest in the signal log in plaintext.
    import json
    from app.main import _redact_raw
    body = json.dumps({
        "action": "buy",
        "meta": {"apiKey": "AKIA-real-key", "deep": {"token": "t0ken"}},
        "legs": [{"password": "hunter2"}, {"symbol": "BTC/USDT"}],
    })
    redacted = _redact_raw(body)
    out = json.loads(redacted)
    assert out["action"] == "buy"                              # non-secret kept
    assert out["meta"]["apiKey"] == "***redacted***"           # nested dict
    assert out["meta"]["deep"]["token"] == "***redacted***"    # deeply nested
    assert out["legs"][0]["password"] == "***redacted***"      # inside a list
    assert out["legs"][1]["symbol"] == "BTC/USDT"              # non-secret in list
    assert "AKIA-real-key" not in redacted and "hunter2" not in redacted

# __SEC_APPEND__

def test_negative_limit_does_not_dump_all_rows(client):
    # A negative ?limit becomes SQL "LIMIT -1", which SQLite treats as "no limit"
    # (ALL rows). The list endpoints clamp with max(1, ...) so a hostile client
    # can't page an entire table in one request.
    from app.database import SessionLocal
    from app.models import Trade, TradeStatus
    from sqlalchemy import select, delete as sql_delete
    c = _sub_client("neg-limit@example.com")
    _, uid = _engine_by_email("neg-limit@example.com")
    db = SessionLocal()
    try:
        for sym in ("BTC/USDT", "ETH/USDT", "SOL/USDT"):
            db.add(Trade(user_id=uid, symbol=sym, side="buy", amount=0.1,
                         entry_price=100.0, status=TradeStatus.open.value,
                         mode="paper"))
        db.commit()
        clamped = c.get("/api/trades?limit=-1").json()
        assert len(clamped) == 1              # clamped to 1, NOT all 3 rows
        assert len(c.get("/api/signals?limit=-1").json()) <= 1
    finally:
        db.execute(sql_delete(Trade).where(Trade.user_id == uid))
        db.commit()
        db.close()

# __SEC_APPEND2__

def test_purge_user_state_removes_only_scoped_kv():
    # Deleting an account must clear its scoped KV (settings, wallet, strategies)
    # and leave the legacy global (user_id=None) state untouched.
    from app.database import SessionLocal
    from app import state
    db = SessionLocal()
    uid = 999321  # an id no signed-up user in this suite owns
    try:
        state.save_settings_overrides(db, {"trading_mode": "paper"}, uid)
        state.save_paper_balance(db, 4242.0, uid)
        state.save_strategy_configs(db, {"BTC/USDT": {"strategy": "sma"}}, uid)
        state.save_paper_balance(db, 111.0, None)  # global — must survive
        assert state.purge_user_state(db, uid) == 3
        assert state.load_settings_overrides(db, uid) == {}
        assert state.load_strategy_configs(db, uid) == {}
        assert state.load_paper_balance(db, -1.0, uid) == -1.0    # gone -> default
        assert state.load_paper_balance(db, -1.0, None) == 111.0  # global intact
    finally:
        state.purge_user_state(db, uid)
        db.close()

# __SEC_APPEND3__

def test_admin_delete_user_cascades_data(client):
    # Deleting a user must remove their trades/signals/alerts + KV state (no FK
    # cascade exists on these tables) and free any licence key they redeemed, so
    # nothing orphaned outlives the account or leaks to a re-used id.
    from app.database import SessionLocal
    from app.models import Trade, SignalLog, PriceAlert, TradeStatus
    from app import state
    from sqlalchemy import select
    email = "cascade-del@example.com"
    _sub_client(email)
    _, uid = _engine_by_email(email)
    db = SessionLocal()
    try:
        db.add(Trade(user_id=uid, symbol="BTC/USDT", side="buy", amount=0.1,
                     entry_price=100.0, status=TradeStatus.open.value, mode="paper"))
        db.add(SignalLog(user_id=uid, source="manual", symbol="BTC/USDT",
                         action="buy", raw="{}", accepted=1))
        db.add(PriceAlert(user_id=uid, symbol="BTC/USDT", condition="above",
                          price=1.0, status="armed"))
        db.commit()
        state.save_paper_balance(db, 5000.0, uid)
    finally:
        db.close()
    r = client.delete(f"/api/admin/users/{uid}")  # module client is the admin
    assert r.status_code == 200 and r.json()["deleted"] == uid
    db = SessionLocal()
    try:
        assert db.scalars(select(Trade).where(Trade.user_id == uid)).all() == []
        assert db.scalars(select(SignalLog).where(SignalLog.user_id == uid)).all() == []
        assert db.scalars(select(PriceAlert).where(PriceAlert.user_id == uid)).all() == []
        assert state.load_paper_balance(db, -1.0, uid) == -1.0  # KV purged
    finally:
        db.close()
    assert all(u["id"] != uid for u in client.get("/api/admin/users").json())




