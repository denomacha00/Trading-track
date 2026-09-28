"""Real fundamental / macro / sentiment data for the AI and the Fundamentals panel.

Honest scope: every number here is fetched LIVE from a public, no-key data source
(plain GETs — no user data leaves the box):
  * Crypto Fear & Greed index ................ api.alternative.me/fng
  * Global crypto market + BTC/ETH dominance .. CoinGecko /global
  * Per-coin mcap / volume / supply / ATH / trailing returns .. CoinGecko /coins/markets
  * Derivatives positioning (funding, open interest, long/short) .. Binance USD-M futures

If a source is unreachable we record it in ``errors`` and leave that section
None -- we NEVER fabricate a value. Results are cached per-source so the AI and
the UI can both read them without hammering the sources.
"""
from __future__ import annotations

import datetime as dt
import json as _json
import logging
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_UA = "Tranding-track/1.0 (+fundamentals)"
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


def _get_json(url: str, *, params: dict | None = None, timeout: float = 8.0) -> Any:
    """GET JSON with a hard size cap and a short timeout. Raises on any failure;
    callers catch and degrade gracefully (they never fabricate on failure)."""
    with httpx.Client(timeout=timeout, headers={"User-Agent": _UA}) as client:
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

    snapshot = {
        "symbol": (symbol or "").upper(),
        "asset": asset,
        "fear_greed": fng,
        "global_market": glob,
        "coin": coin,
        "derivatives": deriv,
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

    if not lines:
        return ""
    header = (
        "Fundamentals / macro / sentiment (REAL -- live public market data, not "
        "fabricated; ANALYSE these and factor them into your read, don't claim you "
        "lack fundamentals):"
    )
    return header + "\n" + "\n".join(lines)





