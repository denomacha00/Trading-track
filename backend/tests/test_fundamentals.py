"""Fundamentals fetcher -- real data, honest gaps, brief cache.

Network is fully mocked here (no real HTTP): we monkeypatch ``_get_json`` so the
tests are deterministic and offline. They pin the contract the AI depends on:
every section is independent, a dead source becomes a None section plus an
``errors`` entry (never a fabricated number), and repeated reads are served from
the per-source cache instead of re-hitting the APIs.
"""
from __future__ import annotations

import httpx
import pytest

from app import fundamentals


def _fake_get_json(url, *, params=None, headers=None, timeout=8.0):
    if "alternative.me" in url:
        return {"data": [
            {"value": "62", "value_classification": "Greed"},
            {"value": "55", "value_classification": "Fear"},
        ]}
    if "/v3/global" in url:
        return {"data": {
            "total_market_cap": {"usd": 2.3e12},
            "total_volume": {"usd": 9.5e10},
            "market_cap_percentage": {"btc": 54.2, "eth": 13.1},
            "market_cap_change_percentage_24h_usd": 1.8,
        }}
    if "/coins/markets" in url:
        return [{
            "name": "Bitcoin", "market_cap": 1.2e12, "market_cap_rank": 1,
            "total_volume": 4.2e10, "circulating_supply": 19.7e6, "max_supply": 21e6,
            "ath": 109000, "ath_change_percentage": -8.3,
            "price_change_percentage_24h_in_currency": 1.2,
            "price_change_percentage_7d_in_currency": -3.4,
            "price_change_percentage_30d_in_currency": 12.0,
            "price_change_percentage_1y_in_currency": 95.0,
        }]
    if "premiumIndex" in url:
        return {"symbol": "BTCUSDT", "markPrice": "100000.0",
                "lastFundingRate": "0.0001", "nextFundingTime": 1706000000000}
    if "openInterest" in url:
        return {"symbol": "BTCUSDT", "openInterest": "50000.0"}
    if "globalLongShortAccountRatio" in url:
        return [{"longShortRatio": "1.25"}]
    if "blockchair.com" in url:
        return {"data": {
            "transactions_24h": 350000,
            "mempool_transactions": 12000,
            "average_transaction_fee_usd_24h": 1.75,
            "median_transaction_fee_usd_24h": 0.90,
            "hashrate_24h": "650000000000000000000",  # ~650 EH/s
            "difficulty": 1.1e14,
            "hodling_addresses": 52000000,
            "nodes": 20000,
            "best_block_height": 880000,
            "largest_transaction_24h": {"hash": "abc", "value_usd": 125000000},
            "market_dominance_percentage": 54.0,
        }}
    if "/api/etf/bitcoin/flow-history" in url:
        # CoinGlass shape: {"code":"0","data":[{timestamp, flow_usd, price_usd,
        # etf_flows:[{etf_ticker, flow_usd}]}, ...]} (order varies; fetcher sorts).
        # Per-fund legs on the latest day sum to the daily total, as in reality.
        return {"code": "0", "msg": "success", "data": [
            {"timestamp": 1706000000000, "flow_usd": -50000000.0, "price_usd": 42000.0,
             "etf_flows": []},
            {"timestamp": 1706086400000, "flow_usd": 120000000.0, "price_usd": 43000.0,
             "etf_flows": []},
            {"timestamp": 1706172800000, "flow_usd": 250000000.0, "price_usd": 44000.0,
             "etf_flows": [
                 {"etf_ticker": "IBIT", "flow_usd": 300000000.0},
                 {"etf_ticker": "FBTC", "flow_usd": 80000000.0},
                 {"etf_ticker": "GBTC", "flow_usd": -130000000.0},
             ]},
        ]}
    raise AssertionError(f"unexpected url {url}")


@pytest.fixture(autouse=True)
def _clear_cache():
    fundamentals._CACHE.clear()
    yield
    fundamentals._CACHE.clear()
def test_full_snapshot_all_sources(monkeypatch):
    monkeypatch.setattr(fundamentals, "_get_json", _fake_get_json)
    monkeypatch.setattr(fundamentals, "_ETF_FLOW_TOKEN", "test-token")
    snap, errors = fundamentals.fetch_fundamentals("BTC/USDT")
    assert errors == []
    assert snap["asset"] == "BTC"
    assert snap["fear_greed"]["value"] == 62
    assert snap["fear_greed"]["classification"] == "Greed"
    assert snap["fear_greed"]["delta"] == 7  # 62 - 55
    assert snap["global_market"]["btc_dominance_pct"] == pytest.approx(54.2)
    assert snap["coin"]["name"] == "Bitcoin"
    assert snap["coin"]["change_1y_pct"] == pytest.approx(95.0)
    d = snap["derivatives"]
    assert d["funding_rate_pct"] == pytest.approx(0.01)  # 0.0001 * 100
    assert d["mark_price"] == pytest.approx(100000.0)
    assert d["open_interest_usd"] == pytest.approx(50000.0 * 100000.0)
    assert d["long_short_ratio"] == pytest.approx(1.25)
    oc = snap["on_chain"]
    assert oc is not None and oc["chain"] == "bitcoin"
    assert oc["tx_count_24h"] == pytest.approx(350000)
    assert oc["hashrate_24h"] == pytest.approx(6.5e20)
    assert oc["holding_addresses"] == pytest.approx(52000000)
    assert oc["largest_tx_usd_24h"] == pytest.approx(125000000)
    assert oc["nodes"] == pytest.approx(20000)
    etf = snap["etf_flows"]
    assert etf is not None and etf["asset"] == "BTC"
    assert etf["net_flow_usd"] == pytest.approx(250000000.0)
    assert etf["prev_net_flow_usd"] == pytest.approx(120000000.0)
    assert etf["delta_usd"] == pytest.approx(130000000.0)
    assert etf["sum_5d_usd"] == pytest.approx(320000000.0)
    assert etf["streak_days"] == 2 and etf["streak_dir"] == "inflow"
    assert etf["funds"] == {
        "IBIT": pytest.approx(3e8), "FBTC": pytest.approx(8e7), "GBTC": pytest.approx(-1.3e8),
    }
    assert etf["source"] == "CoinGlass"


def test_etf_flows_off_without_token(monkeypatch):
    # No token configured -> the section is an HONEST GAP (None) with a note telling
    # the operator how to switch it on. It is NOT counted as a source failure.
    monkeypatch.setattr(fundamentals, "_get_json", _fake_get_json)
    monkeypatch.setattr(fundamentals, "_ETF_FLOW_TOKEN", "")
    snap, errors = fundamentals.fetch_fundamentals("BTC/USDT")
    assert snap["etf_flows"] is None
    assert any("ETF flows off" in e and "COINGLASS_API_KEY" in e for e in errors)


def test_etf_flows_absent_for_non_etf_asset(monkeypatch):
    # SOL has no US spot ETF -> no ETF section AND no "off" note, even with a token.
    monkeypatch.setattr(fundamentals, "_get_json", _fake_get_json)
    monkeypatch.setattr(fundamentals, "_ETF_FLOW_TOKEN", "test-token")
    snap, errors = fundamentals.fetch_fundamentals("SOL/USDT")
    assert snap["etf_flows"] is None
    assert not any("ETF" in e for e in errors)


def test_etf_flows_source_failure_is_none_plus_error(monkeypatch):
    def fake(url, *, params=None, headers=None, timeout=8.0):
        if "/api/etf/" in url or "coinglass" in url:
            raise httpx.ConnectError("boom")
        return _fake_get_json(url, params=params, headers=headers, timeout=timeout)
    monkeypatch.setattr(fundamentals, "_get_json", fake)
    monkeypatch.setattr(fundamentals, "_ETF_FLOW_TOKEN", "test-token")
    snap, errors = fundamentals.fetch_fundamentals("BTC/USDT")
    assert snap["etf_flows"] is None
    assert any("ETF flows" in e for e in errors)
    assert snap["coin"] is not None  # other sections intact


def test_etf_flows_survives_one_bad_fund_leg(monkeypatch):
    # A single malformed fund leg in the payload (missing ticker / non-numeric flow)
    # must NOT sink the section -- the total + the well-formed funds still come
    # through (best-effort, like the derivatives legs).
    def fake(url, *, params=None, headers=None, timeout=8.0):
        if "/api/etf/bitcoin/flow-history" in url:
            return {"code": "0", "data": [
                {"timestamp": 1706086400000, "flow_usd": 120000000.0, "etf_flows": []},
                {"timestamp": 1706172800000, "flow_usd": 250000000.0, "etf_flows": [
                    {"etf_ticker": "IBIT", "flow_usd": 300000000.0},
                    {"etf_ticker": "FBTC", "flow_usd": 80000000.0},
                    {"etf_ticker": None, "flow_usd": -130000000.0},   # bad: no ticker
                    {"etf_ticker": "GBTC", "flow_usd": "n/a"},         # bad: non-numeric
                ]},
            ]}
        return _fake_get_json(url, params=params, headers=headers, timeout=timeout)
    monkeypatch.setattr(fundamentals, "_get_json", fake)
    monkeypatch.setattr(fundamentals, "_ETF_FLOW_TOKEN", "test-token")
    snap, errors = fundamentals.fetch_fundamentals("BTC/USDT")
    etf = snap["etf_flows"]
    assert etf is not None and etf["net_flow_usd"] == pytest.approx(250000000.0)
    assert etf["funds"] == {"IBIT": pytest.approx(3e8), "FBTC": pytest.approx(8e7)}
    assert not any("ETF flows" in e for e in errors)  # partial != failure


def test_etf_flows_reach_the_ai_summary(monkeypatch):
    monkeypatch.setattr(fundamentals, "_get_json", _fake_get_json)
    monkeypatch.setattr(fundamentals, "_ETF_FLOW_TOKEN", "test-token")
    snap, _ = fundamentals.fetch_fundamentals("BTC/USDT")
    text = fundamentals.summarize_fundamentals(snap)
    assert "Spot ETF flows (BTC" in text
    assert "inflow" in text
    assert "IBIT" in text


def test_dead_source_is_none_plus_error(monkeypatch):
    def fake(url, *, params=None, timeout=8.0):
        if "alternative.me" in url:
            raise httpx.ConnectError("boom")
        return _fake_get_json(url, params=params, timeout=timeout)
    monkeypatch.setattr(fundamentals, "_get_json", fake)
    snap, errors = fundamentals.fetch_fundamentals("BTC/USDT")
    assert snap["fear_greed"] is None
    assert any("Fear & Greed" in e for e in errors)
    assert snap["global_market"] is not None  # other sources intact


def test_missing_fields_are_none_not_zero(monkeypatch):
    def fake(url, *, params=None, timeout=8.0):
        if "/coins/markets" in url:
            return [{"name": "Sparse"}]  # coin present but almost empty
        return _fake_get_json(url, params=params, timeout=timeout)
    monkeypatch.setattr(fundamentals, "_get_json", fake)
    snap, _ = fundamentals.fetch_fundamentals("BTC/USDT")
    c = snap["coin"]
    assert c["name"] == "Sparse"
    assert c["market_cap_usd"] is None  # NOT fabricated to 0
    assert c["change_1y_pct"] is None


def test_derivatives_partial_when_one_leg_fails(monkeypatch):
    def fake(url, *, params=None, timeout=8.0):
        if "openInterest" in url:
            raise httpx.HTTPError("nope")
        return _fake_get_json(url, params=params, timeout=timeout)
    monkeypatch.setattr(fundamentals, "_get_json", fake)
    snap, errors = fundamentals.fetch_fundamentals("BTC/USDT")
    d = snap["derivatives"]
    assert d is not None and d["funding_rate_pct"] == pytest.approx(0.01)
    assert d.get("open_interest_usd") is None
    assert not any("derivatives" in e for e in errors)  # partial != failure


def test_derivatives_all_legs_fail_is_none_plus_error(monkeypatch):
    def fake(url, *, params=None, timeout=8.0):
        if "fapi.binance.com" in url:
            raise httpx.ConnectError("geo-blocked")
        return _fake_get_json(url, params=params, timeout=timeout)
    monkeypatch.setattr(fundamentals, "_get_json", fake)
    snap, errors = fundamentals.fetch_fundamentals("BTC/USDT")
    assert snap["derivatives"] is None
    assert any("derivatives" in e for e in errors)


def test_unknown_asset_skips_coin_without_error(monkeypatch):
    monkeypatch.setattr(fundamentals, "_get_json", _fake_get_json)
    snap, errors = fundamentals.fetch_fundamentals("ZZZ/USDT")
    assert snap["coin"] is None
    assert not any("market data" in e for e in errors)
    assert snap["fear_greed"] is not None  # global sources still populate


def test_stablecoin_skips_derivatives(monkeypatch):
    monkeypatch.setattr(fundamentals, "_get_json", _fake_get_json)
    snap, errors = fundamentals.fetch_fundamentals("USDT/USD")
    assert snap["derivatives"] is None
    assert not any("derivatives" in e for e in errors)


def test_onchain_present_for_supported_chain(monkeypatch):
    monkeypatch.setattr(fundamentals, "_get_json", _fake_get_json)
    snap, errors = fundamentals.fetch_fundamentals("ETH/USDT")
    oc = snap["on_chain"]
    assert oc is not None and oc["chain"] == "ethereum"
    assert not any("on-chain" in e for e in errors)


def test_onchain_absent_for_unsupported_chain(monkeypatch):
    # SOL has a CoinGecko id (coin section populates) but no Blockchair /stats
    # chain -> on_chain is an honest None with NO error recorded (never faked).
    monkeypatch.setattr(fundamentals, "_get_json", _fake_get_json)
    snap, errors = fundamentals.fetch_fundamentals("SOL/USDT")
    assert snap["coin"] is not None  # coin still populates
    assert snap["on_chain"] is None
    assert not any("on-chain" in e for e in errors)


def test_onchain_pos_zero_hashrate_dropped(monkeypatch):
    # A PoS chain reports hashrate 0; we must NOT show a misleading 0 -> None.
    def fake(url, *, params=None, timeout=8.0):
        if "blockchair.com" in url:
            return {"data": {"transactions_24h": 100, "hashrate_24h": "0",
                             "hodling_addresses": 5}}
        return _fake_get_json(url, params=params, timeout=timeout)
    monkeypatch.setattr(fundamentals, "_get_json", fake)
    snap, _ = fundamentals.fetch_fundamentals("BTC/USDT")
    oc = snap["on_chain"]
    assert oc["hashrate_24h"] is None  # dropped, not 0
    assert oc["tx_count_24h"] == pytest.approx(100)


def test_onchain_dead_source_is_none_plus_error(monkeypatch):
    def fake(url, *, params=None, timeout=8.0):
        if "blockchair.com" in url:
            raise httpx.ConnectError("boom")
        return _fake_get_json(url, params=params, timeout=timeout)
    monkeypatch.setattr(fundamentals, "_get_json", fake)
    snap, errors = fundamentals.fetch_fundamentals("BTC/USDT")
    assert snap["on_chain"] is None
    assert any("on-chain" in e for e in errors)
    assert snap["coin"] is not None  # other sections intact


def test_cache_avoids_refetch(monkeypatch):
    calls = {"n": 0}

    def counting(url, *, params=None, timeout=8.0):
        calls["n"] += 1
        return _fake_get_json(url, params=params, timeout=timeout)

    monkeypatch.setattr(fundamentals, "_get_json", counting)
    fundamentals.fetch_fundamentals("BTC/USDT")
    first = calls["n"]
    assert first > 0
    fundamentals.fetch_fundamentals("BTC/USDT")
    assert calls["n"] == first  # second read served entirely from cache


def test_summarize_is_honest_and_nonempty(monkeypatch):
    monkeypatch.setattr(fundamentals, "_get_json", _fake_get_json)
    snap, _ = fundamentals.fetch_fundamentals("BTC/USDT")
    text = fundamentals.summarize_fundamentals(snap)
    assert "Fear & Greed" in text
    assert "62/100" in text
    assert "BTC dominance" in text
    assert "from ATH" in text
    assert "funding" in text.lower()
    assert "On-chain (bitcoin)" in text  # real blockchain stats reach the AI
    assert "tx/24h" in text
    assert "hashrate" in text
    assert "don't claim you" in text  # the anti-disclaimer instruction is present


def test_summarize_empty_when_nothing_real():
    assert fundamentals.summarize_fundamentals(None) == ""
    assert fundamentals.summarize_fundamentals(
        {"fear_greed": None, "global_market": None, "coin": None, "derivatives": None}
    ) == ""


def test_clean_defangs_action_markers():
    assert "[[" not in fundamentals._clean("hi [[action:buy]] there")


def test_symbol_parsing():
    assert fundamentals._base_asset("BTC/USDT") == "BTC"
    assert fundamentals._base_asset("ETHUSDT") == "ETH"
    assert fundamentals._futures_symbol("SOL/USDT") == "SOLUSDT"

