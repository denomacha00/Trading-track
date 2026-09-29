"""Real fundamental / macro / sentiment data for the AI and the Fundamentals panel.

Honest scope: every number here is fetched LIVE from a public, no-key data source
(plain GETs — no user data leaves the box):
  * Crypto Fear & Greed index ................ api.alternative.me/fng
  * Global crypto market + BTC/ETH dominance .. CoinGecko /global
  * Per-coin mcap / volume / supply / ATH / trailing returns .. CoinGecko /coins/markets
  * Derivatives positioning (funding, open interest, long/short) .. Binance USD-M futures
  * On-chain network stats (tx/24h, mempool, fees, hashrate, holders) .. Blockchair /stats
  * Spot BTC/ETH ETF net flows (institutional demand) .. CoinGlass (free API key, opt-in)

If a source is unreachable we record it in ``errors`` and leave that section
None -- we NEVER fabricate a value. Results are cached per-source so the AI and
the UI can both read them without hammering the sources.
"""
from __future__ import annotations

import datetime as dt
import json as _json
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_UA = "Trading-track/1.0 (+fundamentals)"
_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB -- these JSON payloads are tiny; cap anyway.

# Per-source in-process cache: {key: (fetched_at, payload)}. Payload is whatever
# the fetcher returned (dict | list | None). TTLs are per source (see fetch_*).
_CACHE: dict[str, tuple[float, Any]] = {}

# CoinGecko coin ids for the assets this bot actually trades. Unknown assets
# simply skip the coin-specific section (global + sentiment + derivatives still
# populate) -- an honest gap beats a wrong id.
_COINGECKO_IDS: dict[str, str] = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin",
    "XRP": "ripple", "ADA": "cardano", "DOGE": "dogecoin", "AVAX": "avalanche-2",
    "DOT": "polkadot", "MATIC": "matic-network", "POL": "matic-network",
    "LINK": "chainlink", "LTC": "litecoin", "TRX": "tron", "SHIB": "shiba-inu",
    "UNI": "uniswap", "ATOM": "cosmos", "XLM": "stellar", "NEAR": "near",
    "APT": "aptos", "ARB": "arbitrum", "OP": "optimism", "FIL": "filecoin",
    "ETC": "ethereum-classic", "HBAR": "hedera-hashgraph", "ICP": "internet-computer",
    "SUI": "sui", "INJ": "injective-protocol", "SEI": "sei-network",
    "TON": "the-open-network", "PEPE": "pepe", "WLD": "worldcoin-wld", "AAVE": "aave",
    "MKR": "maker", "RNDR": "render-token", "TIA": "celestia", "BCH": "bitcoin-cash",
    "USDT": "tether", "USDC": "usd-coin",
}
_STABLES = {"USDT", "USDC", "USD", "DAI", "TUSD", "FDUSD", "BUSD"}
def _base_asset(symbol: str) -> str:
    """'BTC/USDT' -> 'BTC'; 'BTCUSDT' -> 'BTC' (best-effort)."""
    s = (symbol or "").upper().strip()
    if "/" in s:
        return s.split("/", 1)[0]
    for q in ("USDT", "USDC", "USD", "BUSD", "FDUSD"):
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s


def _futures_symbol(symbol: str) -> str:
    """Binance USD-M perpetual symbol, e.g. 'BTC/USDT' -> 'BTCUSDT'. Non-USDT
    quotes map to the USDT perp -- that's where the derivatives liquidity is."""
    return f"{_base_asset(symbol)}USDT"


def _num(v: Any) -> float | None:
    """float() or None -- never NaN/inf, so a bad field becomes an honest gap."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _clean(s: Any) -> str:
    """Flatten a third-party string and defang action markers before it reaches
    the model (these come from public APIs, but treat them as data all the same)."""
    t = str(s or "").replace("\n", " ").replace("\r", " ")
    return t.replace("[[", "").replace("]]", "").strip()


def _get_json(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: float = 8.0,
) -> Any:
    """GET JSON with a hard size cap and a short timeout. Raises on any failure;
    callers catch and degrade gracefully (they never fabricate on failure)."""
    hdrs = {"User-Agent": _UA}
    if headers:
        hdrs.update(headers)
    with httpx.Client(timeout=timeout, headers=hdrs) as client:
        with client.stream("GET", url, params=params, follow_redirects=True) as resp:
            resp.raise_for_status()
            total = 0
            chunks: list[bytes] = []
            for chunk in resp.iter_bytes():
                total += len(chunk)
                if total > _MAX_BYTES:
                    raise ValueError("fundamentals response exceeded size cap")
                chunks.append(chunk)
    return _json.loads(b"".join(chunks) or b"null")


def _cached(key: str, ttl: float, fetch):
    """Return a fresh cached payload, else call ``fetch`` and cache it. A fetch
    failure re-raises (the caller records the error and leaves the section None)."""
    now = time.time()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    payload = fetch()
    _CACHE[key] = (now, payload)
    return payload
def _fear_greed() -> dict | None:
    """Crypto Fear & Greed index (0=extreme fear .. 100=extreme greed) + yesterday."""
    data = _get_json(
        "https://api.alternative.me/fng/", params={"limit": 2, "format": "json"}
    )
    rows = (data or {}).get("data") or []
    if not rows:
        return None
    val = _num(rows[0].get("value"))
    if val is None:
        return None
    prev = _num(rows[1].get("value")) if len(rows) > 1 else None
    return {
        "value": int(val),
        "classification": _clean(rows[0].get("value_classification")) or None,
        "prev": int(prev) if prev is not None else None,
        "delta": int(val - prev) if prev is not None else None,
    }


def _global_market() -> dict | None:
    """Whole-crypto market: total mcap, 24h volume, 24h change, BTC/ETH dominance."""
    data = (_get_json("https://api.coingecko.com/api/v3/global") or {}).get("data") or {}
    if not data:
        return None
    dom = data.get("market_cap_percentage") or {}
    return {
        "total_market_cap_usd": _num((data.get("total_market_cap") or {}).get("usd")),
        "total_volume_usd": _num((data.get("total_volume") or {}).get("usd")),
        "btc_dominance_pct": _num(dom.get("btc")),
        "eth_dominance_pct": _num(dom.get("eth")),
        "market_cap_change_24h_pct": _num(
            data.get("market_cap_change_percentage_24h_usd")
        ),
    }
def _coin_market(coin_id: str) -> dict | None:
    """Per-coin fundamentals: mcap, 24h volume, supply, distance from ATH, and
    trailing returns over 24h / 7d / 30d / 1y."""
    rows = _get_json(
        "https://api.coingecko.com/api/v3/coins/markets",
        params={
            "vs_currency": "usd",
            "ids": coin_id,
            "price_change_percentage": "24h,7d,30d,1y",
            "sparkline": "false",
        },
    )
    if not isinstance(rows, list) or not rows:
        return None
    r = rows[0]
    return {
        "name": _clean(r.get("name")) or coin_id,
        "market_cap_usd": _num(r.get("market_cap")),
        "market_cap_rank": _num(r.get("market_cap_rank")),
        "volume_24h_usd": _num(r.get("total_volume")),
        "circulating_supply": _num(r.get("circulating_supply")),
        "max_supply": _num(r.get("max_supply")),
        "ath_usd": _num(r.get("ath")),
        "ath_change_pct": _num(r.get("ath_change_percentage")),
        "change_24h_pct": _num(r.get("price_change_percentage_24h_in_currency")),
        "change_7d_pct": _num(r.get("price_change_percentage_7d_in_currency")),
        "change_30d_pct": _num(r.get("price_change_percentage_30d_in_currency")),
        "change_1y_pct": _num(r.get("price_change_percentage_1y_in_currency")),
    }


def _derivatives(fut_symbol: str) -> dict | None:
    """Binance USD-M futures positioning. Each leg is best-effort: a geo-block or
    outage on one leaves the others intact. None only if every leg failed."""
    out: dict[str, Any] = {}
    try:
        pi = _get_json(
            "https://fapi.binance.com/fapi/v1/premiumIndex", params={"symbol": fut_symbol}
        )
        if isinstance(pi, dict):
            fr = _num(pi.get("lastFundingRate"))
            out["funding_rate_pct"] = fr * 100 if fr is not None else None
            out["mark_price"] = _num(pi.get("markPrice"))
            nft = _num(pi.get("nextFundingTime"))
            out["next_funding_time"] = (
                dt.datetime.fromtimestamp(nft / 1000, dt.timezone.utc).isoformat()
                if nft else None
            )
    except Exception as exc:
        logger.info("funding fetch failed %s: %s", fut_symbol, exc)
    try:
        oi = _get_json(
            "https://fapi.binance.com/fapi/v1/openInterest", params={"symbol": fut_symbol}
        )
        base_oi = _num((oi or {}).get("openInterest"))
        out["open_interest_base"] = base_oi
        mp = out.get("mark_price")
        out["open_interest_usd"] = base_oi * mp if (base_oi is not None and mp) else None
    except Exception as exc:
        logger.info("open-interest fetch failed %s: %s", fut_symbol, exc)
    try:
        ls = _get_json(
            "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
            params={"symbol": fut_symbol, "period": "1h", "limit": 1},
        )
        if isinstance(ls, list) and ls:
            out["long_short_ratio"] = _num(ls[0].get("longShortRatio"))
    except Exception as exc:
        logger.info("long/short fetch failed %s: %s", fut_symbol, exc)
    if not any(v is not None for v in out.values()):
        raise RuntimeError("no derivatives data (all legs failed)")
    return out


# Blockchair chain slugs for the assets we can read REAL on-chain stats for.
# Only chains Blockchair's keyless /stats endpoint actually serves; any other
# asset simply has no on-chain section (an honest gap, never faked).
_BLOCKCHAIR_CHAINS: dict[str, str] = {
    "BTC": "bitcoin", "ETH": "ethereum", "LTC": "litecoin",
    "BCH": "bitcoin-cash", "DOGE": "dogecoin", "DASH": "dash",
    "ZEC": "zcash", "XLM": "stellar", "ADA": "cardano",
    "XRP": "ripple", "XMR": "monero", "BSV": "bitcoin-sv",
    "GRS": "groestlcoin",
}


def _on_chain(chain: str) -> dict | None:
    """REAL on-chain network stats from Blockchair (keyless): 24h transactions,
    mempool backlog, average/median fee (USD), PoW hashrate + difficulty, holding
    addresses (adoption), reachable node count (decentralisation), block height,
    and the largest transfer in the last 24h (whale flow). Every field is
    best-effort -- a missing one is None (an honest gap), never fabricated. A
    non-positive hashrate (PoS chains report 0) is dropped rather than shown as a
    misleading zero."""
    data = (_get_json(f"https://api.blockchair.com/{chain}/stats") or {}).get("data") or {}
    if not data:
        return None
    hashrate = _num(data.get("hashrate_24h"))
    if hashrate is not None and hashrate <= 0:
        hashrate = None  # PoS / not applicable -> honest gap, not a fake 0
    largest = data.get("largest_transaction_24h") or {}
    return {
        "chain": chain,
        "tx_count_24h": _num(data.get("transactions_24h")),
        "mempool_tx": _num(data.get("mempool_transactions")),
        "avg_fee_usd_24h": _num(data.get("average_transaction_fee_usd_24h")),
        "median_fee_usd_24h": _num(data.get("median_transaction_fee_usd_24h")),
        "hashrate_24h": hashrate,
        "difficulty": _num(data.get("difficulty")),
        "holding_addresses": _num(data.get("hodling_addresses")),
        "nodes": _num(data.get("nodes")),
        "block_height": _num(data.get("best_block_height") or data.get("blocks")),
        "largest_tx_usd_24h": _num(largest.get("value_usd")),
        "dominance_pct": _num(data.get("market_dominance_percentage")),
    }


# --- Spot crypto ETF net flows (institutional demand) --------------------------
# REAL daily net creation/redemption flows for US-listed spot BTC / ETH ETFs
# (IBIT, FBTC, GBTC, ETHA ...). There is NO reliable keyless public feed for this
# (Farside is Cloudflare-blocked to servers; CoinGlass / SoSoValue / NewHedge all
# gate behind a token), so this section is OPT-IN: the operator drops a free
# NewHedge api token in ETF_FLOW_API_TOKEN (get one at https://newhedge.io -- free,
# non-commercial) and we pull the real series. With no token the section is an
# HONEST GAP (None + an errors note saying how to switch it on) -- a flow number
# is NEVER fabricated. ETF_FLOW_BASE_URL lets an operator point at a different
# provider that speaks the same [[ts_ms, usd], ...] shape.
# --- Spot crypto ETF net flows (institutional demand) --------------------------
# REAL daily net creation/redemption flows for US-listed spot BTC / ETH ETFs
# (IBIT, FBTC, GBTC, ETHA ...). There is NO reliable keyless public feed for this
# (Farside is Cloudflare-blocked to servers; every tracker gates flows behind a
# key). So this is opt-in: drop a CoinGlass API key -- their FREE "Hobbyist" plan
# already includes ETF flow-history -- in COINGLASS_API_KEY (the older
# ETF_FLOW_API_TOKEN name is still accepted) and we pull the real series: the daily
# total AND the per-fund breakdown, straight from CoinGlass. With no key the
# section is an HONEST GAP (None + an errors note saying how to switch it on) -- a
# flow number is NEVER fabricated. Get a key at https://www.coinglass.com/signup .
# ETF_FLOW_BASE_URL can override the API host (e.g. a proxy) but the path/shape are
# CoinGlass's `/api/etf/{asset}/flow-history`.
_ETF_FLOW_BASE = (os.getenv("ETF_FLOW_BASE_URL") or "https://open-api-v4.coinglass.com").rstrip("/")
_ETF_FLOW_TOKEN = (
    os.getenv("COINGLASS_API_KEY") or os.getenv("ETF_FLOW_API_TOKEN") or ""
).strip()

# Only assets with a live US spot ETF appear -- any other asset has no ETF section
# (an honest gap, no error). The value is CoinGlass's asset path segment.
_ETF_FLOW_ASSETS: dict[str, str] = {"BTC": "bitcoin", "ETH": "ethereum"}


def _etf_series(asset_path: str) -> list[dict[str, Any]]:
    """CoinGlass ETF flow-history for one asset -> daily records, oldest->newest.

    Each record: ``{"ts": <ms>, "flow": <usd total>, "funds": {TICKER: usd, ...}}``.
    The one call carries both the headline net flow and the per-fund legs, so there
    are no fragile per-fund round-trips. Raises on transport failure; the caller
    degrades to an honest gap (never a fabricated number).
    """
    url = f"{_ETF_FLOW_BASE}/api/etf/{asset_path}/flow-history"
    data = _get_json(url, headers={"CG-API-KEY": _ETF_FLOW_TOKEN})
    # CoinGlass answers HTTP 200 even when the key is missing/invalid/expired, the
    # plan doesn't cover the endpoint, or a rate limit is hit -- it signals that in
    # a non-"0" ``code`` plus a human ``msg`` with a null ``data``. Turn that into a
    # real error so the operator SEES exactly why (never a silent empty gap that
    # reads like a quiet no-flow day, and never a fabricated number).
    if isinstance(data, dict):
        code = data.get("code")
        if code is not None and str(code) != "0":
            raise RuntimeError(
                f"CoinGlass rejected the ETF flow-history request: "
                f"{data.get('msg') or code} (check COINGLASS_API_KEY / plan)"
            )
    rows = data.get("data") if isinstance(data, dict) else data
    out: list[dict[str, Any]] = []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            ts, flow = _num(row.get("timestamp")), _num(row.get("flow_usd"))
            if ts is None or flow is None:
                continue
            funds: dict[str, float] = {}
            legs = row.get("etf_flows")
            if isinstance(legs, list):
                for leg in legs:
                    if not isinstance(leg, dict):
                        continue
                    tkr, val = leg.get("etf_ticker"), _num(leg.get("flow_usd"))
                    if tkr and val is not None:
                        funds[str(tkr).upper()] = val
            out.append({"ts": int(ts), "flow": flow, "funds": funds})
    out.sort(key=lambda r: r["ts"])
    return out


def _etf_flows(asset: str) -> dict | None:
    """REAL US spot-ETF net flows for BTC / ETH. Latest-day net flow, prior day,
    day-over-day delta, 5-day net, an inflow/outflow streak, and the latest-day
    per-fund breakdown (IBIT / FBTC / GBTC ...). None when the provider gives
    nothing -- never fabricated. Raises if no key is configured (caller degrades)."""
    if not _ETF_FLOW_TOKEN:
        raise RuntimeError("ETF flow key not configured (COINGLASS_API_KEY)")
    asset_path = _ETF_FLOW_ASSETS.get(asset)
    if not asset_path:
        return None  # no US spot ETF for this asset -> honest gap, no error
    series = _etf_series(asset_path)
    if not series:
        return None
    latest = series[-1]
    net = latest["flow"]
    prev = series[-2]["flow"] if len(series) > 1 else None
    last5 = [r["flow"] for r in series[-5:]]
    sign = 1 if net > 0 else -1 if net < 0 else 0
    streak = 0
    if sign:
        for r in reversed(series):
            if (r["flow"] > 0 and sign > 0) or (r["flow"] < 0 and sign < 0):
                streak += 1
            else:
                break
    # Latest-day per-fund, biggest absolute mover first (tidy summary + panel).
    funds = {
        t: v
        for t, v in sorted(
            (latest.get("funds") or {}).items(), key=lambda kv: abs(kv[1]), reverse=True
        )
    }
    return {
        "asset": asset,
        "as_of_ms": latest["ts"],
        "as_of_date": dt.datetime.fromtimestamp(
            latest["ts"] / 1000, dt.timezone.utc
        ).strftime("%Y-%m-%d"),
        "net_flow_usd": net,
        "prev_net_flow_usd": prev,
        "delta_usd": (net - prev) if prev is not None else None,
        "sum_5d_usd": sum(last5) if last5 else None,
        "streak_days": streak,
        "streak_dir": "inflow" if sign > 0 else "outflow" if sign < 0 else "flat",
        "funds": funds or None,
        "source": "CoinGlass",
    }


def fetch_fundamentals(symbol: str, *, ttl_scale: float = 1.0) -> tuple[dict, list[str]]:
    """Return (snapshot, errors) of REAL fundamentals for ``symbol``.

    Sections are independent: a dead source becomes a None section plus an
    ``errors`` entry, never a fabricated number. Cached per source so repeated
    reads (AI chat + the panel) don't hammer the APIs. ``ttl_scale`` lets a
    background warmer stretch the cache windows.
    """
    asset = _base_asset(symbol)
    errors: list[str] = []

    def _try(key: str, ttl: float, fetch, label: str):
        try:
            return _cached(key, ttl * ttl_scale, fetch)
        except Exception as exc:
            logger.warning("fundamentals %s failed: %s", label, exc)
            errors.append(f"{label} unavailable")
            return None

    fng = _try("fng", 600.0, _fear_greed, "Fear & Greed")
    glob = _try("global", 300.0, _global_market, "global market")

    coin = None
    coin_id = _COINGECKO_IDS.get(asset)
    if coin_id:
        coin = _try(
            f"coin:{coin_id}", 300.0, lambda: _coin_market(coin_id), f"{asset} market data"
        )

    deriv = None
    if asset not in _STABLES:
        fut = _futures_symbol(symbol)
        deriv = _try(
            f"deriv:{fut}", 150.0, lambda: _derivatives(fut), f"{asset} derivatives"
        )

    onchain = None
    chain = _BLOCKCHAIR_CHAINS.get(asset)
    if chain:
        onchain = _try(
            f"onchain:{chain}", 600.0, lambda: _on_chain(chain), f"{asset} on-chain"
        )

    etf = None
    if asset in _ETF_FLOW_ASSETS:
        if _ETF_FLOW_TOKEN:
            # Flows update roughly once a business day -> a long cache is plenty.
            etf = _try(
                f"etf:{asset}", 1800.0, lambda: _etf_flows(asset), f"{asset} ETF flows"
            )
        else:
            # Not a failure -- an opt-in feature that's simply off. Tell the operator
            # exactly how to switch it on; never fake a flow to fill the gap.
            errors.append(
                "ETF flows off -- set COINGLASS_API_KEY (free key from "
                "coinglass.com/signup) to pull real spot-ETF net flows"
            )

    snapshot = {
        "symbol": (symbol or "").upper(),
        "asset": asset,
        "fear_greed": fng,
        "global_market": glob,
        "coin": coin,
        "derivatives": deriv,
        "on_chain": onchain,
        "etf_flows": etf,
        "as_of": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    return snapshot, errors
def _fmt_usd(v: float | None) -> str:
    if v is None:
        return "?"
    a = abs(v)
    for unit, div in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if a >= div:
            return f"${v / div:.2f}{unit}"
    return f"${v:,.0f}"


def _fmt_pct(v: float | None, *, sign: bool = True) -> str:
    if v is None:
        return "?"
    return f"{v:+.2f}%" if sign else f"{v:.2f}%"


def _fmt_count(v: float | None) -> str:
    """Large integer counts (transactions, addresses, nodes) -> 1.23M / 4.5K."""
    if v is None:
        return "?"
    a = abs(v)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if a >= div:
            return f"{v / div:.2f}{unit}"
    return f"{v:,.0f}"


def _fmt_hashrate(v: float | None) -> str:
    """Raw hashes/second -> EH/s / PH/s / TH/s ... (honest '?' when unknown)."""
    if v is None:
        return "?"
    for unit, div in (("EH/s", 1e18), ("PH/s", 1e15), ("TH/s", 1e12),
                      ("GH/s", 1e9), ("MH/s", 1e6)):
        if v >= div:
            return f"{v / div:.2f} {unit}"
    return f"{v:.0f} H/s"


def summarize_fundamentals(snapshot: dict | None) -> str:
    """Compact, HONEST one-block rendering for the AI (and UI tooltips). Empty
    string when there's nothing real to show, so the caller simply omits it."""
    if not snapshot:
        return ""
    lines: list[str] = []

    fng = snapshot.get("fear_greed")
    if fng:
        delta = fng.get("delta")
        trend = f", {delta:+d} vs yesterday" if delta is not None else ""
        cls = fng.get("classification") or ""
        lines.append(
            f"- Sentiment (Fear & Greed): {fng.get('value')}/100 {cls}{trend}".rstrip()
        )

    g = snapshot.get("global_market")
    if g:
        lines.append(
            "- Global crypto: total mcap "
            f"{_fmt_usd(g.get('total_market_cap_usd'))} "
            f"({_fmt_pct(g.get('market_cap_change_24h_pct'))} 24h), "
            f"24h vol {_fmt_usd(g.get('total_volume_usd'))}, "
            f"BTC dominance {_fmt_pct(g.get('btc_dominance_pct'), sign=False)}, "
            f"ETH {_fmt_pct(g.get('eth_dominance_pct'), sign=False)}"
        )

    c = snapshot.get("coin")
    if c:
        supply = ""
        cs, ms = c.get("circulating_supply"), c.get("max_supply")
        if cs is not None and ms:
            supply = f", circ {cs / ms * 100:.0f}% of max supply"
        lines.append(
            f"- {c.get('name')} fundamentals: mcap {_fmt_usd(c.get('market_cap_usd'))}, "
            f"24h vol {_fmt_usd(c.get('volume_24h_usd'))}, "
            f"{_fmt_pct(c.get('ath_change_pct'))} from ATH{supply}"
        )
        lines.append(
            "- Trailing returns: "
            f"24h {_fmt_pct(c.get('change_24h_pct'))}, "
            f"7d {_fmt_pct(c.get('change_7d_pct'))}, "
            f"30d {_fmt_pct(c.get('change_30d_pct'))}, "
            f"1y {_fmt_pct(c.get('change_1y_pct'))}"
        )

    dv = snapshot.get("derivatives")
    if dv:
        fr = dv.get("funding_rate_pct")
        bias = ""
        if fr is not None:
            bias = (
                " (longs pay shorts -- crowded long)" if fr > 0.02
                else " (shorts pay longs -- crowded short)" if fr < -0.02
                else " (near flat)"
            )
        seg = f"- Derivatives: funding {_fmt_pct(fr)}{bias}"
        if dv.get("open_interest_usd"):
            seg += f", open interest {_fmt_usd(dv.get('open_interest_usd'))}"
        if dv.get("long_short_ratio") is not None:
            seg += f", long/short {dv.get('long_short_ratio'):.2f}"
        lines.append(seg)

    oc = snapshot.get("on_chain")
    if oc:
        parts: list[str] = []
        if oc.get("tx_count_24h") is not None:
            parts.append(f"{_fmt_count(oc['tx_count_24h'])} tx/24h")
        if oc.get("mempool_tx") is not None:
            parts.append(f"{_fmt_count(oc['mempool_tx'])} in mempool")
        if oc.get("avg_fee_usd_24h") is not None:
            parts.append(f"avg fee ${oc['avg_fee_usd_24h']:,.2f}")
        if oc.get("hashrate_24h") is not None:
            parts.append(f"hashrate {_fmt_hashrate(oc['hashrate_24h'])}")
        if oc.get("holding_addresses") is not None:
            parts.append(f"{_fmt_count(oc['holding_addresses'])} holding addresses")
        if oc.get("largest_tx_usd_24h") is not None:
            parts.append(f"largest 24h transfer {_fmt_usd(oc['largest_tx_usd_24h'])}")
        if oc.get("nodes") is not None:
            parts.append(f"{_fmt_count(oc['nodes'])} reachable nodes")
        if parts:
            lines.append(f"- On-chain ({oc.get('chain')}): " + ", ".join(parts))

    etf = snapshot.get("etf_flows")
    if etf:
        net = etf.get("net_flow_usd")
        dirw = "inflow" if (net or 0) > 0 else "outflow" if (net or 0) < 0 else "flat"
        net_s = _fmt_usd(abs(net)) if net is not None else "?"
        seg = (
            f"- Spot ETF flows ({etf.get('asset')}, {etf.get('as_of_date')}): "
            f"net {net_s} {dirw}"
        )
        d = etf.get("delta_usd")
        if d is not None:
            seg += f" ({'+' if d >= 0 else '-'}{_fmt_usd(abs(d))} vs prior day)"
        s5 = etf.get("sum_5d_usd")
        if s5 is not None:
            seg += f", 5-day {_fmt_usd(abs(s5))} {'in' if s5 >= 0 else 'out'}"
        st = etf.get("streak_days")
        if st and st > 1:
            seg += f", {st}-day {etf.get('streak_dir')} streak"
        funds = etf.get("funds") or {}
        if funds:
            top = ", ".join(
                f"{k} {'+' if v >= 0 else '-'}{_fmt_usd(abs(v))}" for k, v in funds.items()
            )
            seg += f" [{top}]"
        seg += f" (src {etf.get('source')})"
        lines.append(seg)
    elif snapshot.get("asset") in _ETF_FLOW_ASSETS:
        # BTC/ETH have real US spot ETFs but there's no flow figure in THIS pull
        # (either the feed is off, or the ETF-flow source returned nothing this
        # time). Say so plainly so the model neither invents a number nor
        # mis-sources it: ETF flows come from the dedicated ETF-flow feed, NEVER
        # from news headlines.
        lines.append(
            f"- Spot ETF flows ({snapshot.get('asset')}): not available in this "
            "snapshot (comes from the dedicated ETF-flow feed, NOT from news "
            "headlines; a missing figure is never fabricated)."
        )

    if not lines:
        return ""
    header = (
        "Fundamentals / macro / sentiment / on-chain (REAL -- live public market "
        "and blockchain data, not fabricated; ANALYSE these and factor them into "
        "your read, don't claim you lack fundamentals):"
    )
    return header + "\n" + "\n".join(lines)





