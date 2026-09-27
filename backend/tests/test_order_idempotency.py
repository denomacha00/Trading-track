"""M7 — order placement must be idempotent under retry.

A market/limit CREATE is not safely retryable on its own: a RequestTimeout can
arrive AFTER Binance accepted and executed the order (the request landed, the
response was lost). A blind retry — what plain ``_with_retry`` around a create
would do — then places a SECOND order and doubles the position. For a bot meant
to PREVENT account blow-ups that is the worst possible failure.

BinanceConnector now attaches a clientOrderId and reconciles by it: after a
transient error it checks whether the order already landed before retrying, and
Binance's duplicate-id rejection is the backstop. These tests script a ccxt-like
client per attempt to prove a retry can never double-fill.
"""
from __future__ import annotations

import ccxt
import pytest

from app.config import Settings
from app.exchange import BinanceConnector


class IdempotencyClient:
    """A ccxt-like client scripted per-attempt to model lost responses.

    ``create_order`` pops the next behavior from ``script``:
      "ok"           -> record the order in the exchange book and return it
      "land_timeout" -> record the order (it LANDED) then raise RequestTimeout
                        (models the request reaching Binance but the reply lost)
      "timeout"      -> raise RequestTimeout WITHOUT recording (never landed)
      "dup"          -> raise Binance's duplicate-clientOrderId rejection
      "insufficient" -> raise InsufficientFunds (a non-retryable error)
    ``fetch_order(None, symbol, {"clientOrderId": coid})`` returns the recorded
    order for that id (or None), and can be made to fail the first N times.
    """

    def __init__(self, script):
        self.script = list(script)
        self.create_calls: list[tuple] = []   # (coid, type_, side) per attempt
        self.fetch_calls: list[str] = []       # coid per reconcile
        self._book: dict[str, dict] = {}       # coid -> order the exchange holds
        self.fetch_fail_first = 0              # make reconcile fail N times

    def price_to_precision(self, symbol, price):
        return round(float(price), 2)

    def create_order(self, symbol, type_, side, amount, price=None, params=None):
        params = params or {}
        coid = params.get("clientOrderId")
        self.create_calls.append((coid, type_, side))
        behavior = self.script.pop(0) if self.script else "ok"
        order = {
            "id": f"ord-{len(self.create_calls)}", "clientOrderId": coid,
            "symbol": symbol, "type": type_, "side": side, "amount": amount,
            "price": price, "status": "closed",
        }
        if behavior == "ok":
            self._book[coid] = order
            return order
        if behavior == "land_timeout":
            self._book[coid] = order                      # it executed...
            raise ccxt.RequestTimeout("response lost")    # ...but the reply was lost
        if behavior == "timeout":
            raise ccxt.RequestTimeout("never reached exchange")
        if behavior == "dup":
            raise ccxt.InvalidOrder(
                'binance {"code":-2010,"msg":"Duplicate order sent."}'
            )
        if behavior == "insufficient":
            raise ccxt.InsufficientFunds("account has insufficient balance")
        raise AssertionError(f"unknown scripted behavior {behavior!r}")

    def fetch_order(self, order_id, symbol, params=None):
        coid = (params or {}).get("clientOrderId")
        self.fetch_calls.append(coid)
        if self.fetch_fail_first > 0:
            self.fetch_fail_first -= 1
            raise ccxt.RequestTimeout("fetch timed out")
        return self._book.get(coid)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Don't actually sleep on the backoff between retries.
    monkeypatch.setattr("app.exchange.time.sleep", lambda *_a, **_k: None)


def _conn(script) -> BinanceConnector:
    conn = BinanceConnector(Settings(binance_api_key="k", binance_api_secret="s"))
    conn._client = IdempotencyClient(script)
    return conn


def test_market_order_happy_path_tags_client_id():
    conn = _conn(["ok"])
    order = conn.create_market_order("BTC/USDT", "buy", 1.0)
    assert order["id"] == "ord-1"
    assert len(conn._client.create_calls) == 1        # placed exactly once
    coid, type_, side = conn._client.create_calls[0]
    assert type_ == "market" and side == "buy"
    assert coid and coid.startswith("tt-")            # idempotency key attached
    assert conn._client.fetch_calls == []             # no reconcile needed


def test_timeout_after_landing_reconciles_without_second_order():
    # The crux of M7: the order LANDED but the response was lost. The connector
    # must find it by client id and return it — NOT place a second order.
    conn = _conn(["land_timeout"])
    order = conn.create_market_order("BTC/USDT", "buy", 1.0)
    assert order["id"] == "ord-1"                     # the order that landed
    assert len(conn._client.create_calls) == 1        # NO second placement
    assert conn._client.fetch_calls == [conn._client.create_calls[0][0]]


def test_timeout_not_landed_retries_with_same_id_then_succeeds():
    conn = _conn(["timeout", "ok"])
    order = conn.create_market_order("BTC/USDT", "buy", 1.0)
    assert order["id"] == "ord-2"
    assert len(conn._client.create_calls) == 2        # retried once
    first_id = conn._client.create_calls[0][0]
    second_id = conn._client.create_calls[1][0]
    assert first_id == second_id                      # SAME idempotency key reused
    assert conn._client.fetch_calls == [first_id]     # reconciled (found nothing) once


def test_duplicate_rejection_returns_the_already_placed_order():
    # Order landed but the reply was lost AND the first reconcile also failed, so
    # we retried; Binance then rejects the reused id as a duplicate. We must
    # fetch by that id and return the real order — never surface a spurious error.
    conn = _conn(["land_timeout", "dup"])
    conn._client.fetch_fail_first = 1                 # first reconcile times out
    order = conn.create_market_order("BTC/USDT", "buy", 1.0)
    assert order["id"] == "ord-1"                     # the originally-landed order
    assert len(conn._client.create_calls) == 2
    assert len(conn._client.fetch_calls) == 2         # failed once, then found it


def test_insufficient_funds_raises_immediately_no_reconcile():
    conn = _conn(["insufficient"])
    with pytest.raises(ccxt.InsufficientFunds):
        conn.create_market_order("BTC/USDT", "buy", 1.0)
    assert len(conn._client.create_calls) == 1        # not retried
    assert conn._client.fetch_calls == []             # no reconcile for a hard error


def test_limit_order_is_idempotent_too():
    conn = _conn(["land_timeout"])
    order = conn.create_limit_order("BTC/USDT", "buy", 1.0, 100.0)
    assert order["id"] == "ord-1"
    assert len(conn._client.create_calls) == 1        # no double placement
    assert conn._client.create_calls[0][1] == "limit"


def test_exhausted_timeouts_raise_honestly_without_double_fill():
    conn = _conn(["timeout", "timeout", "timeout"])
    with pytest.raises(ccxt.RequestTimeout):
        conn.create_market_order("BTC/USDT", "buy", 1.0)
    assert len(conn._client.create_calls) == 3        # used the full budget
    ids = {c[0] for c in conn._client.create_calls}
    assert len(ids) == 1                              # every attempt shared one id
    assert len(conn._client.fetch_calls) == 3         # reconciled before each retry
