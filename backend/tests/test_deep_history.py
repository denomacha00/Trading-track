"""Deep-history OHLCV stitching (TradingView-paid "extended history" parity).

The exchange caps one klines call at ~1000 bars. `BinanceConnector.fetch_ohlcv`
stitches several capped calls by stepping `since` forward so the chart can load
a year+ of real bars. These tests pin the honest contract: the newest N bars,
strictly ascending, de-duplicated, never padded past what the venue actually
has, and no infinite loop when a venue stops advancing.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.exchange import BinanceConnector, _MAX_SINGLE_CALL

NOW_MS = 1_700_000_000_000
TF_MS = 3_600_000  # 1h


@pytest.fixture(autouse=True)
def _fixed_clock_no_sleep(monkeypatch):
    # Deterministic `now` so the `since` walk is exact, and never really sleep.
    monkeypatch.setattr("app.exchange.time.time", lambda: NOW_MS / 1000)
    monkeypatch.setattr("app.exchange.time.sleep", lambda *_a, **_k: None)


class FakeClient:
    """A venue with a finite 1h history ending at NOW_MS, capped per call."""

    def __init__(self, total: int):
        # Bar k (0 = newest) has ts = NOW_MS - k*TF_MS. Stored oldest-first.
        self.series = [
            [NOW_MS - k * TF_MS, 100.0, 101.0, 99.0, 100.5, 1.0]
            for k in range(total - 1, -1, -1)
        ]
        self.calls: list[int | None] = []  # the `since` of each call

    def fetch_ohlcv(self, symbol, timeframe="1h", since=None, limit=500):
        self.calls.append(since)
        if since is None:
            return [list(r) for r in self.series[-limit:]]
        rows = [r for r in self.series if r[0] >= since]
        return [list(r) for r in rows[:limit]]


def _conn(client) -> BinanceConnector:
    conn = BinanceConnector(Settings(binance_api_key="k", binance_api_secret="s"))
    conn._client = client
    return conn


def _assert_strictly_ascending_unique(bars):
    times = [b[0] for b in bars]
    assert times == sorted(times)
    assert len(set(times)) == len(times)


def test_shallow_request_is_a_single_call():
    client = FakeClient(total=3000)
    out = _conn(client).fetch_ohlcv("BTC/USDT", "1h", 500)
    assert len(out) == 500
    assert client.calls == [None]  # no pagination, no `since`
    assert out[-1][0] == NOW_MS  # newest bar is "now"


def test_deep_request_stitches_pages_to_exact_depth():
    client = FakeClient(total=6000)
    out = _conn(client).fetch_ohlcv("BTC/USDT", "1h", 2500)
    assert len(out) == 2500
    _assert_strictly_ascending_unique(out)
    # The newest 2500 real bars: last is NOW, first is 2499 bars back.
    assert out[-1][0] == NOW_MS
    assert out[0][0] == NOW_MS - 2499 * TF_MS
    # 2501 candidate bars (>= since) fetched in 1000-bar pages => 3 calls.
    assert len(client.calls) == 3
    assert client.calls[0] is not None  # deep path always passes `since`


def test_never_pads_beyond_what_the_venue_has():
    client = FakeClient(total=1200)  # fewer bars than requested
    out = _conn(client).fetch_ohlcv("BTC/USDT", "1h", 5000)
    assert len(out) == 1200  # honest: all real bars, not padded to 5000
    _assert_strictly_ascending_unique(out)
    assert out[-1][0] == NOW_MS


def test_unknown_timeframe_falls_back_to_one_recent_page():
    client = FakeClient(total=3000)
    out = _conn(client).fetch_ohlcv("BTC/USDT", "7m", 2000)  # 7m not in the map
    assert len(out) == _MAX_SINGLE_CALL  # single capped call, no guessed spacing
    assert client.calls == [None]


class StuckClient(FakeClient):
    """Pathological venue that ignores `since` and always returns the same page."""

    def fetch_ohlcv(self, symbol, timeframe="1h", since=None, limit=500):
        self.calls.append(since)
        return [list(r) for r in self.series[-limit:]]


def test_no_progress_terminates_without_hanging():
    client = StuckClient(total=2000)
    out = _conn(client).fetch_ohlcv("BTC/USDT", "1h", 5000)
    # Stops as soon as a page fails to advance the timestamp — never loops.
    assert len(out) == _MAX_SINGLE_CALL
    _assert_strictly_ascending_unique(out)
    assert len(client.calls) == 2  # one real page, one that trips the guard


# --- Lazy "load older on pan" window (end_ms) ------------------------------
# Given an `end_ms` boundary, fetch_ohlcv returns the bars ending STRICTLY
# before it — the chart's infinite-scroll chunk that abuts the already-loaded
# bars without overlap. Real bars only, capped at a single call.


def test_end_ms_returns_the_window_before_the_boundary():
    client = FakeClient(total=6000)
    end_ms = NOW_MS - 1000 * TF_MS
    out = _conn(client).fetch_ohlcv("BTC/USDT", "1h", 500, end_ms=end_ms)
    assert len(out) == 500
    _assert_strictly_ascending_unique(out)
    assert all(r[0] < end_ms for r in out)  # strictly before the boundary
    assert out[-1][0] == end_ms - TF_MS  # newest returned bar abuts the boundary
    assert out[0][0] == end_ms - 500 * TF_MS


def test_end_ms_excludes_the_boundary_bar():
    client = FakeClient(total=2000)
    out = _conn(client).fetch_ohlcv("BTC/USDT", "1h", 100, end_ms=NOW_MS)
    assert len(out) == 100
    assert out[-1][0] == NOW_MS - TF_MS  # the bar AT the boundary (NOW) is excluded


def test_end_ms_unknown_timeframe_returns_empty_not_wrong_bars():
    client = FakeClient(total=3000)
    out = _conn(client).fetch_ohlcv("BTC/USDT", "7m", 500, end_ms=NOW_MS)
    assert out == []  # can't place an older window without a known spacing
    assert client.calls == []  # never even queried the venue


def test_end_ms_caps_at_a_single_call():
    client = FakeClient(total=6000)
    end_ms = NOW_MS - 100 * TF_MS
    out = _conn(client).fetch_ohlcv("BTC/USDT", "1h", 5000, end_ms=end_ms)
    assert len(out) == _MAX_SINGLE_CALL  # 1000, not 5000 — a scroll chunk, not deep history
    _assert_strictly_ascending_unique(out)
    assert all(r[0] < end_ms for r in out)


