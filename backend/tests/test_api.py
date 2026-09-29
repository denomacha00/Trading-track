"""API smoke tests using FastAPI's TestClient (no network, no live server).

Multi-user mode: every protected endpoint requires a Bearer token, so the
fixtures sign up an admin (auto-licensed) and attach the token to the client.
Exchange-dependent endpoints (ticker/ohlcv/backtest/train) are not exercised
here because they require Binance connectivity; those are covered by unit tests
on the underlying modules.
"""
from __future__ import annotations

# DB isolation + paper mode live in tests/conftest.py, which pytest imports
# before any test module — the only point early enough to redirect DATABASE_URL
# before app.database binds its engine. (Doing it here, after `from app.main
# import app` below, would be too late: whichever module imports the app first
# wins, so the suite would silently run against the real tranding_track.db.)

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


def test_upstream_exchange_error_is_sanitized(client, monkeypatch):
    # A raw ccxt/exchange exception must NOT leak to the client (it can carry
    # request URLs, exchange class names, account/permission hints). The client
    # gets a stable generic message; the real cause only goes to the server log.
    from app.exchange import BinanceConnector

    sentinel = "https://internal.exchange/api?apiKey=LEAKED-SECRET-TOKEN"

    def _boom(self, *a, **k):
        raise RuntimeError(sentinel)

    monkeypatch.setattr(BinanceConnector, "fetch_ohlcv", _boom, raising=True)
    r = client.get("/api/ohlcv/BTC/USDT?timeframe=1h&limit=50")
    assert r.status_code == 502
    assert r.json()["detail"] == "OHLCV unavailable"
    # The raw exception text (and its secret) must be absent from the response.
    assert "LEAKED-SECRET-TOKEN" not in r.text
    assert sentinel not in r.text


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


def test_webhook_duplicate_key_ignored(client):
    # M13 — an alert carrying an explicit idempotency token is executed ONCE; a
    # duplicate delivery of the same token is ignored, never re-executed. Uses a
    # close on a symbol with no open position: a deterministic, network-free path.
    path = _webhook_path(client)
    body = b'{"nonce":"evt-dup-1","action":"close","symbol":"ZZZ/USDT"}'
    r1 = client.post(path, content=body)
    assert r1.status_code == 200
    assert r1.json()["accepted"] is False
    assert "no open position" in r1.json()["message"].lower()
    # Same token again -> already claimed -> reported as a duplicate, not re-run.
    r2 = client.post(path, content=body)
    assert r2.status_code == 200
    assert r2.json()["accepted"] is False
    assert "duplicate" in r2.json()["message"].lower()


def test_webhook_keyless_is_not_deduped(client):
    # A keyless payload has nothing to identify a repeat by, so BOTH deliveries
    # reach execution — we never fabricate a key and silently drop real alerts.
    path = _webhook_path(client)
    body = b'{"action":"close","symbol":"ZZZ/USDT"}'
    for _ in range(2):
        r = client.post(path, content=body)
        assert r.status_code == 200
        assert r.json()["accepted"] is False
        assert "no open position" in r.json()["message"].lower()


def test_webhook_buy_blocked_when_bot_stopped(client):
    # The STOP switch halts NEW entries — the autonomous monitor gates new-entry
    # analysis on the bot being "running", and a webhook BUY is an unattended new
    # entry, so it must not open a position while the bot is stopped. An exit
    # (close) must still fire. Runs before the rotate test (token still valid).
    path = _webhook_path(client)
    assert client.post("/api/bot/stop").json()["running"] is False
    r = client.post(
        path, content=b'{"action":"buy","symbol":"ZZZ/USDT","amount":0.01}'
    )
    assert r.status_code == 409
    assert "stop" in r.json()["detail"].lower()
    # A close on the same (positionless) symbol is still accepted while stopped.
    rc = client.post(path, content=b'{"action":"close","symbol":"ZZZ/USDT"}')
    assert rc.status_code == 200


def test_webhook_rotate_invalidates_old_token(client):
    # Rotating mints a fresh token and kills the old URL immediately. Run LAST
    # among webhook tests since it changes the admin's token (others re-read it).
    old_path = _webhook_path(client)
    r = client.post("/api/webhook/rotate")
    assert r.status_code == 200
    new_path = r.json()["webhook_path"]
    assert new_path.startswith("/api/webhook/tradingview/")
    assert new_path != old_path
    # The old URL is dead the instant rotation returns.
    dead = client.post(old_path, content=b'{"action":"close","symbol":"ZZZ/USDT"}')
    assert dead.status_code == 404
    # The new URL routes to the handler.
    live = client.post(new_path, content=b'{"action":"close","symbol":"ZZZ/USDT"}')
    assert live.status_code == 200
    assert live.json()["accepted"] is False


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
        # Clear BOTH providers. `available` is True if EITHER the primary or the
        # optional fallback key is set, so disabling AI for this test means
        # clearing both — otherwise a real AI_FALLBACK_API_KEY in the environment
        # keeps the assistant live and this "no key" path is never exercised.
        engine.ai._settings.ai_api_key = ""
        engine.ai._settings.ai_fallback_api_key = ""
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


def test_ict_enabled_exposed_and_toggles(client):
    # ICT lens is exposed, defaults ON (informational, no money behaviour), and
    # can be turned off — the round-trip proves it persists.
    body = client.get("/api/settings").json()
    assert body["ict_enabled"] is True
    off = client.patch("/api/settings", json={"ict_enabled": False})
    assert off.status_code == 200
    assert off.json()["ict_enabled"] is False
    assert client.get("/api/settings").json()["ict_enabled"] is False


def test_ict_confluence_exposed_and_toggles(client):
    # ICT confluence (letting the smart-money read VOTE in the brain) is exposed,
    # defaults ON, and round-trips through the API allowlist like the lens toggle.
    body = client.get("/api/settings").json()
    assert body["ict_confluence"] is True
    off = client.patch("/api/settings", json={"ict_confluence": False})
    assert off.status_code == 200
    assert off.json()["ict_confluence"] is False
    assert client.get("/api/settings").json()["ict_confluence"] is False


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
    # The realized-P&L equity curve starts empty — never back-filled or invented.
    assert p["equity_curve"] == []


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


def test_normalize_order_ignores_ai_authored_money_math():
    from app.main import _normalize_proposed_action

    # SAFETY (M9): even if the model emits an amount / stop / TP / limit price,
    # the normaliser drops them all. The risk manager owns sizing and the stop,
    # so a hallucinated size can't bypass the per-position concentration cap and
    # a bogus stop can't set the trade's real risk. The AI proposes only the
    # direction and symbol; an operator wanting exact numbers uses the order form.
    a = _normalize_proposed_action(
        {
            "type": "order", "side": "buy", "symbol": "BTC/USDT",
            "amount": 999.0, "stop_loss": 1.0, "take_profit": 5.0,
            "limit_price": 60000,
        },
        None,
    )
    assert a["amount"] is None       # risk manager sizes it (+ concentration cap)
    assert "stop_loss" not in a      # deterministic _auto_stop applies instead
    assert "take_profit" not in a    # deterministic _auto_take applies instead
    assert "limit_price" not in a    # entry price is not taken from the model
    assert a["side"] == "buy" and a["symbol"] == "BTC/USDT"


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


def test_normalize_chart_view_action():
    # The chart action is VIEW-ONLY (it changes what the operator is looking at and
    # moves no money). Only real, allowlisted fields survive; bad ones are dropped.
    from app.main import _normalize_proposed_action

    a = _normalize_proposed_action(
        {
            "type": "chart",
            "symbol": "eth/usdt",  # upper-cased
            "timeframe": "4H",  # lower-cased, in the allowed set
            "indicators": {
                "rsi": "true",  # coerced to bool
                "macd": True,
                "ema9": False,  # explicit hide survives
                "bogus": True,  # unknown key -> dropped
            },
            "clear_drawings": "yes",  # coerced to bool
            "reason": "show momentum",
        },
        None,
    )
    assert a == {
        "type": "chart",
        "symbol": "ETH/USDT",
        "timeframe": "4h",
        "indicators": {"rsi": True, "macd": True, "ema9": False},
        "clear_drawings": True,
        "reason": "show momentum",
        "auto": False,  # no engine/autopilot off => confirm-gated
    }


def test_normalize_chart_undo_is_standalone():
    # "undo when asked" — a pure undo needs no other field and short-circuits; the
    # frontend maps it to its own view-history stack.
    from app.main import _normalize_proposed_action

    a = _normalize_proposed_action({"type": "chart", "undo": True}, None)
    assert a == {"type": "chart", "reason": None, "auto": False, "undo": True}


def test_normalize_chart_rejects_empty_and_bad_fields():
    # An empty tag (nothing actionable) yields None so no do-nothing card renders;
    # a bad timeframe / non-pair symbol / all-unknown indicators are dropped, and if
    # nothing real is left the whole proposal is refused.
    from app.main import _normalize_proposed_action

    assert _normalize_proposed_action({"type": "chart"}, None) is None
    assert _normalize_proposed_action({"type": "chart", "timeframe": "3h"}, None) is None  # not in the picker
    assert _normalize_proposed_action({"type": "chart", "symbol": "BTC"}, None) is None  # no pair
    assert _normalize_proposed_action({"type": "chart", "indicators": {"nope": True}}, None) is None
    # A single valid field is enough to render a card.
    assert _normalize_proposed_action({"type": "chart", "timeframe": "1h"}, None)["timeframe"] == "1h"


def test_normalize_chart_ict_overlays():
    # The chart action can ALSO toggle the ICT / smart-money overlays. Same rules as
    # indicators: only allowlisted keys survive, values are coerced to real bools,
    # and an ICT-only patch is enough on its own to render a card. Toggling an
    # overlay only VIEWS a computed level — it never fabricates one or moves money.
    from app.main import _normalize_proposed_action

    a = _normalize_proposed_action(
        {
            "type": "chart",
            "ict": {
                "orderBlocks": "true",  # coerced to bool
                "fvg": True,
                "dealingRange": False,  # explicit hide survives
                "swings": 1,  # truthy number -> True
                "bogus": True,  # unknown key -> dropped
            },
            "reason": "mark the smart-money zones",
        },
        None,
    )
    assert a == {
        "type": "chart",
        "ict": {"orderBlocks": True, "fvg": True, "dealingRange": False, "swings": True},
        "reason": "mark the smart-money zones",
        "auto": False,
    }
    # An ICT object with only unknown keys leaves nothing actionable -> refused.
    assert _normalize_proposed_action({"type": "chart", "ict": {"nope": True}}, None) is None
    # ICT overlays combine with indicators/symbol in one view-only proposal.
    combo = _normalize_proposed_action(
        {"type": "chart", "indicators": {"rsi": True}, "ict": {"liquidity": True}},
        None,
    )
    assert combo["indicators"] == {"rsi": True}
    assert combo["ict"] == {"liquidity": True}


def test_normalize_chart_params_clamped():
    # The chart action can ALSO tune indicator LENGTHS/multiples. Only allowlisted
    # param keys survive; look-backs are whole bars clamped to [1, 1000]; the two
    # multipliers (bbMult/kcMult) stay fractional in [0.1, 10]; anything unknown,
    # non-finite or boolean is DROPPED (never coerced to a default). Setting a param
    # only changes what an indicator draws — it moves no money, so it's autopilot-safe.
    from app.main import _normalize_proposed_action

    a = _normalize_proposed_action(
        {
            "type": "chart",
            "params": {
                "rsiPeriod": 21,        # kept as-is
                "emaFast": 0,           # clamped up to the min look-back (1)
                "macdSlow": 9999,       # clamped down to the max (1000)
                "bbMult": 2.5,          # multiplier kept fractional
                "kcMult": 99,           # multiplier clamped to 10
                "stochK": 14.7,         # look-back rounded to a whole bar (15)
                "atrPeriod": "abc",     # non-numeric -> dropped
                "rsiPeriod2": 5,        # unknown key -> dropped
                "donchian": True,       # boolean -> dropped (not a length)
            },
            "reason": "make the RSI 21",
        },
        None,
    )
    assert a == {
        "type": "chart",
        "params": {
            "rsiPeriod": 21,
            "emaFast": 1,
            "macdSlow": 1000,
            "bbMult": 2.5,
            "kcMult": 10.0,
            "stochK": 15,
        },
        "reason": "make the RSI 21",
        "auto": False,
    }
    # A params object with only bad/unknown keys leaves nothing actionable -> refused.
    assert _normalize_proposed_action({"type": "chart", "params": {"nope": 5}}, None) is None
    assert _normalize_proposed_action({"type": "chart", "params": {"rsiPeriod": "x"}}, None) is None
    # Params combine with an indicator toggle in one view-only proposal.
    combo = _normalize_proposed_action(
        {"type": "chart", "indicators": {"rsi": True}, "params": {"rsiPeriod": 30}},
        None,
    )
    assert combo["indicators"] == {"rsi": True}
    assert combo["params"] == {"rsiPeriod": 30}


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
    # A chart (view-only) change is always in the safe subset.
    assert _normalize_proposed_action({"type": "chart", "timeframe": "1h"}, eng)["auto"] is True

    # Autopilot ON + LIVE: every real-money action is confirm-gated (never auto).
    # A live order, a live risk-/autonomy-setting change and STARTING live trading
    # all wait for a human Confirm; only capital-neutral actions (stop the bot,
    # view-only chart) stay auto. See test_ai_action_live_guard.py for the full
    # live matrix.
    eng_live = _AutopilotEngine(autopilot=True, live=True)
    assert _normalize_proposed_action(order, eng_live)["auto"] is False
    assert _normalize_proposed_action({"type": "bot", "state": "start"}, eng_live)["auto"] is False
    assert _normalize_proposed_action({"type": "bot", "state": "stop"}, eng_live)["auto"] is True
    assert _normalize_proposed_action({"type": "settings", "changes": {"risk_per_trade_pct": 5.0}}, eng_live)["auto"] is False
    assert _normalize_proposed_action({"type": "chart", "timeframe": "1h"}, eng_live)["auto"] is True

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


def test_ai_chat_returns_validated_settings_action(client, monkeypatch):
    # Regression for the "says yes, does nothing" bug: a `settings` proposal nests
    # a "changes":{...} object. The old flat-only tag matcher couldn't span the
    # inner braces, so it dropped the whole proposal AND left the raw tag in the
    # reply — no Confirm card rendered, so there was nothing to tap and the change
    # never applied. The endpoint must now strip the tag and return the validated
    # action; and when the operator confirms, PATCH /api/settings must APPLY it.
    engine, _ = _admin_engine()

    def fake_chat(question, **kwargs):
        return (
            "I'll cap your total open exposure at 20% as a guardrail.\n"
            '[[action:{"type":"settings","changes":{"max_total_exposure_pct":20.0},'
            '"reason":"cap exposure during the proving run"}]]'
        )

    monkeypatch.setattr(engine.ai, "chat", fake_chat)
    r = client.post("/api/ai/chat", json={"question": "cap my exposure at 20%"})
    assert r.status_code == 200
    body = r.json()
    assert "[[action" not in body["reply"]  # tag hidden, not leaked into the bubble
    action = body["proposed_action"]
    assert action is not None  # a Confirm card WILL render now (the fix)
    assert action["type"] == "settings"
    assert action["changes"] == {"max_total_exposure_pct": 20.0}

    # Confirm path: the frontend PATCHes exactly action["changes"] — it must land.
    original = client.get("/api/settings").json()["max_total_exposure_pct"]
    try:
        r2 = client.patch("/api/settings", json=action["changes"])
        assert r2.status_code == 200
        assert r2.json()["max_total_exposure_pct"] == 20.0
    finally:
        client.patch("/api/settings", json={"max_total_exposure_pct": original})


def test_ai_chat_proposes_every_action_type(client, monkeypatch):
    # "Can it act on ALL it's told?" — every documented action shape must survive
    # the real endpoint as a non-None, correctly-typed proposal, never silently
    # dropped. This guards the whole tag protocol on the actual wire format (the
    # gap that let the nested `settings` shape slip through unnoticed).
    engine, _ = _admin_engine()
    cases = [
        ('[[action:{"type":"order","side":"buy","symbol":"BTC/USDT","amount":null}]]', "order"),
        ('[[action:{"type":"settings","changes":{"max_total_exposure_pct":20.0}}]]', "settings"),
        ('[[action:{"type":"bot","state":"start"}]]', "bot"),
        ('[[action:{"type":"alert","symbol":"BTC/USDT","condition":"above","price":65000}]]', "alert"),
        ('[[action:{"type":"train","symbol":"BTC/USDT","strategy":"ma_cross","timeframe":"1h"}]]', "train"),
        ('[[action:{"type":"chart","symbol":"BTC/USDT","indicators":{"rsi":true}}]]', "chart"),
        ('[[action:{"type":"chart","undo":true}]]', "chart"),
    ]
    for tag, expected_type in cases:
        monkeypatch.setattr(engine.ai, "chat", lambda q, _t=tag, **k: f"On it.\n{_t}")
        r = client.post("/api/ai/chat", json={"question": "do it"})
        assert r.status_code == 200
        body = r.json()
        assert "[[action" not in body["reply"], f"{expected_type}: raw tag leaked into reply"
        assert body["proposed_action"] is not None, f"{expected_type}: proposal was dropped"
        assert body["proposed_action"]["type"] == expected_type


def test_ai_chat_no_action_when_none_proposed(client, monkeypatch):
    engine, _ = _admin_engine()
    monkeypatch.setattr(engine.ai, "chat", lambda q, **k: "Momentum looks weak; I'd wait.")
    r = client.post("/api/ai/chat", json={"question": "should i buy?"})
    assert r.status_code == 200
    assert r.json()["proposed_action"] is None


def test_ai_chat_passes_valid_image_through_to_model(client, monkeypatch):
    # An attached photo (vision) must reach the model as a clean {data, media_type}
    # dict — validated, not silently dropped. "aGVsbG8=" is base64("hello").
    engine, _ = _admin_engine()
    seen: dict[str, object] = {}

    def fake_chat(question, **kwargs):
        seen["image"] = kwargs.get("image")
        return "I can see the chart you attached."

    monkeypatch.setattr(engine.ai, "chat", fake_chat)
    r = client.post(
        "/api/ai/chat",
        json={
            "question": "what do you see?",
            "image": {"data": "aGVsbG8=", "media_type": "image/png"},
        },
    )
    assert r.status_code == 200
    assert seen["image"] == {"data": "aGVsbG8=", "media_type": "image/png"}


def test_ai_chat_rejects_bad_image_shape(client, monkeypatch):
    # Bad shapes are a clean 400 (never a 500 or a silent drop): non-object,
    # disallowed media type, missing data, and non-base64 data.
    engine, _ = _admin_engine()
    monkeypatch.setattr(engine.ai, "chat", lambda q, **k: "unused")
    base = {"question": "look"}
    assert client.post("/api/ai/chat", json={**base, "image": "notanobject"}).status_code == 400
    assert client.post(
        "/api/ai/chat", json={**base, "image": {"data": "aGVsbG8=", "media_type": "image/tiff"}}
    ).status_code == 400
    assert client.post(
        "/api/ai/chat", json={**base, "image": {"media_type": "image/png"}}
    ).status_code == 400
    assert client.post(
        "/api/ai/chat", json={**base, "image": {"data": "@@not base64@@", "media_type": "image/png"}}
    ).status_code == 400


def test_ai_chat_rejects_oversize_image(client, monkeypatch):
    # Both size guards return 413: the pre-decode string-length bound AND the
    # decoded-bytes bound. Shrink the cap so the test stays fast (no 6 MB alloc).
    from app import main as main_mod

    engine, _ = _admin_engine()
    monkeypatch.setattr(engine.ai, "chat", lambda q, **k: "unused")
    monkeypatch.setattr(main_mod, "_CHAT_IMAGE_MAX_BYTES", 4)
    # String longer than cap*2 (=8) short-circuits before decode.
    r_str = client.post(
        "/api/ai/chat",
        json={"question": "x", "image": {"data": "aGVsbG8gd29ybGQ=", "media_type": "image/png"}},
    )
    assert r_str.status_code == 413
    # "aGVsbG8=" is 8 chars (<= cap*2) but decodes to 5 bytes (> cap) => decoded guard.
    r_dec = client.post(
        "/api/ai/chat",
        json={"question": "x", "image": {"data": "aGVsbG8=", "media_type": "image/png"}},
    )
    assert r_dec.status_code == 413


def test_ai_chat_news_grounded_action_is_never_auto(client, monkeypatch):
    # M11: news is untrusted third-party text (a prompt-injection surface). Even
    # with autopilot ON on a paper account — where a paper order would normally
    # auto-apply — an action from a NEWS-grounded reply must be downgraded to a
    # human Confirm. A hostile headline must not be able to move money silently.
    engine, _ = _admin_engine()
    assert engine.settings.is_live is False  # premise: paper account
    monkeypatch.setattr(engine.settings, "ai_autopilot_enabled", True)

    def fake_chat(question, **kwargs):
        return 'On it.\n[[action:{"type":"order","side":"buy","symbol":"BTC/USDT","amount":null}]]'

    monkeypatch.setattr(engine.ai, "chat", fake_chat)

    import app.main as main_mod
    monkeypatch.setattr(
        main_mod, "fetch_market_news",
        lambda feeds, limit=8: ([{"title": "BTC rips higher", "source": "feed"}], []),
    )

    # Baseline: WITHOUT news, autopilot + paper => a paper order auto-applies.
    # News now defaults ON, so opt OUT explicitly to isolate the no-news path.
    r0 = client.post("/api/ai/chat", json={"question": "buy", "include_news": False})
    assert r0.status_code == 200
    assert r0.json()["proposed_action"]["auto"] is True

    # WITH news: the SAME proposal is forced confirm-gated by the injection guard.
    r1 = client.post("/api/ai/chat", json={"question": "buy", "include_news": True})
    assert r1.status_code == 200
    body = r1.json()
    assert body["used_news"] is True
    act = body["proposed_action"]
    assert act is not None
    assert act["auto"] is False
    assert act.get("injection_guard") is True



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


def _mk_trade(uid, *, status, mode="paper", symbol="BTC/USDT", pnl=0.0):
    """Insert a trade row directly and return its id."""
    from app.database import SessionLocal
    from app.models import Trade, TradeStatus
    from datetime import datetime, timezone
    db = SessionLocal()
    try:
        closed = status == TradeStatus.closed.value
        t = Trade(
            user_id=uid, symbol=symbol, side="buy", amount=0.1,
            entry_price=100.0, exit_price=110.0 if closed else None,
            status=status, mode=mode, pnl=pnl if closed else None,
            closed_at=datetime.now(timezone.utc) if closed else None,
        )
        db.add(t)
        db.commit()
        return t.id
    finally:
        db.close()


def _purge_trades(uid):
    from app.database import SessionLocal
    from app.models import Trade
    from sqlalchemy import select
    db = SessionLocal()
    try:
        for t in db.scalars(select(Trade).where(Trade.user_id == uid)).all():
            db.delete(t)
        db.commit()
    finally:
        db.close()


def test_delete_closed_trade_removes_it_from_journal(client):
    from app.models import TradeStatus
    email = "del-closed@example.com"
    c = _sub_client(email)
    _, uid = _engine_by_email(email)
    tid = _mk_trade(uid, status=TradeStatus.closed.value, pnl=5.0)
    try:
        r = c.delete(f"/api/trades/{tid}")
        assert r.status_code == 200 and r.json() == {"deleted": 1}
        # Gone from the caller's journal.
        remaining = c.get("/api/trades").json()
        assert all(t["id"] != tid for t in remaining)
    finally:
        _purge_trades(uid)


def test_cannot_delete_open_or_pending_trade(client):
    from app.models import TradeStatus
    email = "del-open@example.com"
    c = _sub_client(email)
    _, uid = _engine_by_email(email)
    open_id = _mk_trade(uid, status=TradeStatus.open.value)
    pend_id = _mk_trade(uid, status=TradeStatus.pending.value)
    try:
        # A live/open position is not history — it must never be deletable.
        assert c.delete(f"/api/trades/{open_id}").status_code == 409
        assert c.delete(f"/api/trades/{pend_id}").status_code == 409
        ids = {t["id"] for t in c.get("/api/trades").json()}
        assert open_id in ids and pend_id in ids  # both survived
    finally:
        _purge_trades(uid)


def test_delete_trade_is_ownership_scoped(client):
    from app.models import TradeStatus
    owner_email, other_email = "del-owner@example.com", "del-other@example.com"
    owner = _sub_client(owner_email)
    other = _sub_client(other_email)
    _, owner_uid = _engine_by_email(owner_email)
    tid = _mk_trade(owner_uid, status=TradeStatus.closed.value)
    try:
        # A different user cannot delete someone else's record (404, not 403,
        # so its existence isn't even disclosed).
        assert other.delete(f"/api/trades/{tid}").status_code == 404
        assert owner.delete(f"/api/trades/{tid}").status_code == 200
    finally:
        _purge_trades(owner_uid)


def test_clear_trades_closed_only_and_mode_filter(client):
    from app.models import TradeStatus
    email = "clear-trades@example.com"
    c = _sub_client(email)
    _, uid = _engine_by_email(email)
    closed_paper = _mk_trade(uid, status=TradeStatus.closed.value, mode="paper")
    closed_live = _mk_trade(uid, status=TradeStatus.closed.value, mode="live")
    open_paper = _mk_trade(uid, status=TradeStatus.open.value, mode="paper")
    try:
        # Default mode=paper clears only the CLOSED paper row.
        r = c.delete("/api/trades")
        assert r.status_code == 200 and r.json() == {"deleted": 1}
        ids = {t["id"] for t in c.get("/api/trades").json()}
        assert closed_paper not in ids           # cleared
        assert closed_live in ids                # other book untouched
        assert open_paper in ids                 # open position never cleared
        # mode=all clears the remaining CLOSED row but still spares the open one.
        r = c.delete("/api/trades", params={"mode": "all"})
        assert r.status_code == 200 and r.json() == {"deleted": 1}
        ids = {t["id"] for t in c.get("/api/trades").json()}
        assert closed_live not in ids and open_paper in ids
    finally:
        _purge_trades(uid)


def test_clear_trades_rejects_bad_mode(client):
    email = "clear-badmode@example.com"
    c = _sub_client(email)
    assert c.delete("/api/trades", params={"mode": "bogus"}).status_code == 400


def test_delete_and_clear_signals(client):
    from app.database import SessionLocal
    from app.models import SignalLog
    from sqlalchemy import select
    owner_email, other_email = "sig-owner@example.com", "sig-other@example.com"
    c = _sub_client(owner_email)
    other = _sub_client(other_email)
    _, uid = _engine_by_email(owner_email)
    db = SessionLocal()
    try:
        for _ in range(3):
            db.add(SignalLog(user_id=uid, source="analyzer", symbol="BTC/USDT",
                             action="buy", raw="{}", accepted=1))
        db.commit()
        rows = db.scalars(select(SignalLog).where(SignalLog.user_id == uid)).all()
        first_id = rows[0].id
        # Ownership isolation on the per-row delete.
        assert other.delete(f"/api/signals/{first_id}").status_code == 404
        assert c.delete(f"/api/signals/{first_id}").status_code == 200
        # Bulk clear removes the rest for this user only.
        r = c.delete("/api/signals")
        assert r.status_code == 200 and r.json()["deleted"] == 2
        assert c.get("/api/signals").json() == []
    finally:
        for s in db.scalars(select(SignalLog).where(SignalLog.user_id == uid)).all():
            db.delete(s)
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




