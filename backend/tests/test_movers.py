"""Honest market movers (TradingView-paid screener / "Editor's Picks" parity).

The rankings are computed straight from the venue's OWN 24h tickers — never an
invented editorial list. These tests pin that honest contract on the pure
`rank_movers` core: only tickers with a real 24h %, last price AND quote volume
are ranked; missing/non-finite fields are skipped (never faked); a liquidity
floor keeps a big-% print on near-zero volume out of the "gainers"; only the
target quote's spot pairs count (no derivatives). One API test pins the wiring.
"""
from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.exchange import rank_movers
from app.main import app


def _t(pct, qv, last=100.0, **extra):
    """A minimal ccxt-style ticker with unified field names."""
    d = {"percentage": pct, "quoteVolume": qv, "last": last}
    d.update(extra)
    return d


def test_ranks_gainers_losers_and_most_active():
    tickers = {
        "BTC/USDT": _t(2.0, 5_000_000_000.0, last=65000.0),
        "ETH/USDT": _t(-3.5, 2_000_000_000.0, last=3200.0),
        "SOL/USDT": _t(11.0, 800_000_000.0, last=150.0),
        "XRP/USDT": _t(-1.0, 300_000_000.0, last=0.5),
    }
    out = rank_movers(tickers, "USDT", top=10)
    # Gainers: highest % first.
    assert [r["symbol"] for r in out["gainers"]] == [
        "SOL/USDT", "BTC/USDT", "XRP/USDT", "ETH/USDT",
    ]
    # Losers: lowest % first (bottom performers, honest label — not "all red").
    assert [r["symbol"] for r in out["losers"]] == [
        "ETH/USDT", "XRP/USDT", "BTC/USDT", "SOL/USDT",
    ]
    # Most active: highest quote volume first.
    assert [r["symbol"] for r in out["most_active"]] == [
        "BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT",
    ]
    # Rows carry honest fields straight from the ticker.
    top_gain = out["gainers"][0]
    assert top_gain == {
        "symbol": "SOL/USDT", "last": 150.0,
        "percentage": 11.0, "quote_volume": 800_000_000.0,
    }


def test_skips_tickers_missing_any_field():
    tickers = {
        "BTC/USDT": _t(2.0, 1_000_000_000.0),          # complete -> kept
        "AAA/USDT": _t(None, 1_000_000_000.0),          # no % -> skipped
        "BBB/USDT": {"percentage": 5.0, "last": 1.0},   # no quoteVolume -> skipped
        "CCC/USDT": {"percentage": 5.0, "quoteVolume": 9e9},  # no last/close -> skipped
    }
    out = rank_movers(tickers, "USDT", top=10)
    syms = {r["symbol"] for r in out["gainers"]}
    assert syms == {"BTC/USDT"}  # only the honest, fully-reported one survives


def test_uses_close_when_last_absent():
    tickers = {"BTC/USDT": {"percentage": 1.0, "quoteVolume": 9e9, "close": 64000.0}}
    out = rank_movers(tickers, "USDT", top=5)
    assert out["gainers"][0]["last"] == 64000.0


def test_skips_non_finite_values():
    tickers = {
        "BTC/USDT": _t(2.0, 1_000_000_000.0),
        "NAN/USDT": _t(float("nan"), 1_000_000_000.0),
        "INF/USDT": _t(float("inf"), 1_000_000_000.0),
        "BADV/USDT": _t(3.0, float("inf")),
    }
    out = rank_movers(tickers, "USDT", top=10)
    assert {r["symbol"] for r in out["gainers"]} == {"BTC/USDT"}


def test_min_quote_volume_floor_drops_illiquid_noise():
    tickers = {
        "PUMP/USDT": _t(900.0, 50_000.0, last=0.001),   # +900% on $50k/24h = noise
        "BTC/USDT": _t(1.5, 5_000_000_000.0, last=65000.0),
    }
    # With a floor, the illiquid pump is excluded and BTC leads honestly.
    floored = rank_movers(tickers, "USDT", top=10, min_quote_volume=1_000_000.0)
    assert [r["symbol"] for r in floored["gainers"]] == ["BTC/USDT"]
    # With no floor it IS a real (if thin) mover and is not hidden.
    unfloored = rank_movers(tickers, "USDT", top=10, min_quote_volume=0.0)
    assert unfloored["gainers"][0]["symbol"] == "PUMP/USDT"


def test_only_target_quote_and_no_derivatives():
    tickers = {
        "BTC/USDT": _t(2.0, 1_000_000_000.0),
        "ETH/BTC": _t(50.0, 1_000_000_000.0),            # wrong quote -> excluded
        "BTC/USDT:USDT": _t(99.0, 1_000_000_000.0),      # perp contract -> excluded
        "WEIRDKEY": _t(80.0, 1_000_000_000.0),           # no '/' -> excluded
    }
    out = rank_movers(tickers, "USDT", top=10)
    assert {r["symbol"] for r in out["gainers"]} == {"BTC/USDT"}


def test_quote_is_case_insensitive():
    tickers = {"BTC/USDT": _t(2.0, 1_000_000_000.0)}
    out = rank_movers(tickers, "usdt", top=5)
    assert out["gainers"][0]["symbol"] == "BTC/USDT"


def test_top_caps_each_list():
    tickers = {
        f"C{i:02d}/USDT": _t(float(i), float(i) * 1_000_000.0 + 1_000_000.0)
        for i in range(20)
    }
    out = rank_movers(tickers, "USDT", top=3)
    assert len(out["gainers"]) == 3
    assert len(out["losers"]) == 3
    assert len(out["most_active"]) == 3
    # Highest three % in gainers; lowest three % in losers.
    assert [r["percentage"] for r in out["gainers"]] == [19.0, 18.0, 17.0]
    assert [r["percentage"] for r in out["losers"]] == [0.0, 1.0, 2.0]


def test_empty_and_malformed_input():
    assert rank_movers({}, "USDT") == {"gainers": [], "losers": [], "most_active": []}
    junk = {"BTC/USDT": None, 123: {"percentage": 1.0}, "X/USDT": "nope"}
    out = rank_movers(junk, "USDT")
    assert out == {"gainers": [], "losers": [], "most_active": []}


# ---- API wiring -----------------------------------------------------------


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
            "username": "moveruser", "email": "movers@example.com",
            "password": "supersecret123"})
        if r.status_code == 409:
            r = c.post("/api/auth/login", json={
                "identifier": "movers@example.com", "password": "supersecret123"})
        c.headers.update({"Authorization": f"Bearer {r.json()['access_token']}"})
        yield c


def test_movers_endpoint_wiring_and_clamp(client, monkeypatch):
    """The endpoint clamps `top` to <=50, forwards the volume floor, and returns
    the ranked shape with an honest source label."""
    seen: dict = {}

    def fake(self, quote="USDT", *, top=10, min_quote_volume=0.0):
        seen["quote"] = quote
        seen["top"] = top
        seen["floor"] = min_quote_volume
        self.last_data_source = "binance"
        return {
            "gainers": [{"symbol": "BTC/USDT", "last": 65000.0,
                         "percentage": 3.0, "quote_volume": 9e9}],
            "losers": [{"symbol": "ETH/USDT", "last": 3200.0,
                        "percentage": -2.0, "quote_volume": 4e9}],
            "most_active": [{"symbol": "BTC/USDT", "last": 65000.0,
                             "percentage": 3.0, "quote_volume": 9e9}],
        }

    from app.exchange import BinanceConnector
    monkeypatch.setattr(BinanceConnector, "market_movers", fake)

    r = client.get("/api/movers", params={"top": 999, "min_quote_volume": 2_000_000})
    assert r.status_code == 200, r.text
    body = r.json()
    assert seen["top"] == 50  # clamped from 999
    assert seen["floor"] == 2_000_000.0
    assert body["quote"] == "USDT"
    assert body["source"] == "binance"
    assert body["gainers"][0]["symbol"] == "BTC/USDT"
    assert body["losers"][0]["percentage"] == -2.0
    assert math.isclose(body["most_active"][0]["quote_volume"], 9e9)
