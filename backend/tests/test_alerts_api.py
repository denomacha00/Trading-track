"""Price-alert HTTP CRUD + the drag-to-move PATCH (re-arm on move).

No network: alerts are pure DB rows here. The firing loop that checks them
against the LIVE price is exercised elsewhere; this pins the endpoint contract,
especially that MOVING an alert (new price/condition) re-arms a fired one so the
repositioned level can fire again — while a note-only edit does not.
"""

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.config import get_settings
from app.database import SessionLocal
from app.models import PriceAlert


@pytest.fixture(scope="module")
def client():
    from app.database import init_db

    s = get_settings()
    s.secret_key = "unit-test-secret-key"
    s.auto_license_new_users = True
    s.rate_limit_enabled = False
    init_db()
    with TestClient(app) as c:
        r = c.post("/api/auth/signup", json={
            "username": "alertuser", "email": "alerts@example.com",
            "password": "supersecret123"})
        if r.status_code == 409:
            r = c.post("/api/auth/login", json={
                "identifier": "alerts@example.com", "password": "supersecret123"})
        c.headers.update({"Authorization": f"Bearer {r.json()['access_token']}"})
        yield c


def _make_alert(client, *, condition="above", price=65000.0) -> int:
    r = client.post("/api/alerts", json={
        "symbol": "btc/usdt", "condition": condition, "price": price})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "armed"
    assert body["symbol"] == "BTC/USDT"  # normalized
    return body["id"]


def _force_triggered(alert_id: int) -> None:
    """Simulate a fired alert directly in the DB (no live price needed)."""
    db = SessionLocal()
    try:
        a = db.get(PriceAlert, alert_id)
        assert a is not None
        a.status = "triggered"
        a.triggered_at = dt.datetime.now(dt.timezone.utc)
        a.triggered_price = a.price
        db.commit()
    finally:
        db.close()


def test_move_updates_price_and_stays_armed(client):
    aid = _make_alert(client, price=65000.0)
    r = client.patch(f"/api/alerts/{aid}", json={"price": 61000.0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["price"] == 61000.0
    assert body["condition"] == "above"  # unchanged when not sent
    assert body["status"] == "armed"


def test_move_rearms_a_triggered_alert(client):
    aid = _make_alert(client, price=70000.0)
    _force_triggered(aid)
    # sanity: it now reads as fired
    listed = {a["id"]: a for a in client.get("/api/alerts").json()}
    assert listed[aid]["status"] == "triggered"
    # move it → back to armed, trigger stamps cleared
    r = client.patch(f"/api/alerts/{aid}", json={"price": 68000.0, "condition": "below"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["price"] == 68000.0
    assert body["condition"] == "below"
    assert body["status"] == "armed"
    assert body["triggered_at"] is None
    assert body["triggered_price"] is None


def test_note_only_edit_does_not_rearm(client):
    aid = _make_alert(client, price=50000.0)
    _force_triggered(aid)
    r = client.patch(f"/api/alerts/{aid}", json={"note": "watch this level"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["note"] == "watch this level"
    assert body["status"] == "triggered"  # a note is not a move


def test_patch_rejects_nonpositive_price(client):
    aid = _make_alert(client, price=42000.0)
    r = client.patch(f"/api/alerts/{aid}", json={"price": 0})
    assert r.status_code == 422  # price must be > 0


def test_patch_missing_alert_404(client):
    r = client.patch("/api/alerts/99999999", json={"price": 100.0})
    assert r.status_code == 404


def test_cannot_move_another_users_alert(client):
    aid = _make_alert(client, price=30000.0)
    other = TestClient(app)
    r = other.post("/api/auth/signup", json={
        "username": "alertuser2", "email": "alerts2@example.com",
        "password": "supersecret123"})
    if r.status_code == 409:
        r = other.post("/api/auth/login", json={
            "identifier": "alerts2@example.com", "password": "supersecret123"})
    other.headers.update({"Authorization": f"Bearer {r.json()['access_token']}"})
    resp = other.patch(f"/api/alerts/{aid}", json={"price": 31000.0})
    assert resp.status_code == 404  # not visible to another user
