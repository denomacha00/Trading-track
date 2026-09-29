"""Binance exchange connector built on CCXT.

Provides a thin, safe wrapper that supports both a real Binance account and the
Binance spot testnet. All network calls are guarded so the rest of the app can
run even when no API keys are configured (useful for paper trading and the UI).
"""
from __future__ import annotations

import logging
import math
import time
import uuid
from typing import Any, Callable, Optional, TypeVar

import ccxt

from app.config import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Transient network/exchange errors worth retrying (vs. auth/insufficient-funds).
_RETRYABLE = (
    ccxt.NetworkError,
    ccxt.ExchangeNotAvailable,
    ccxt.RequestTimeout,
    ccxt.DDoSProtection,
)


def _is_geo_block(exc: Exception) -> bool:
    """True if the error is a Binance regional block (HTTP 451).

    A 451 "restricted location" is permanent — it depends on where the server
    runs, not on transient network weather — so retrying it only wastes time and
    floods the logs. Detect it so callers fail fast instead of looping.
    """
    msg = str(exc)
    low = msg.lower()
    return "451" in msg or "restricted location" in low or "eligibility" in low


def _with_retry(fn: Callable[[], T], *, attempts: int = 3, base_delay: float = 0.5) -> T:
    """Call fn, retrying transient network errors with exponential backoff.

    Non-retryable errors (auth, insufficient funds, bad symbol) are raised
    immediately — retrying those just wastes time and hammers the exchange. A
    regional geo-block (HTTP 451) is likewise permanent, so it fails fast too.
    """
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except _RETRYABLE as exc:
            last_exc = exc
            if _is_geo_block(exc):
                # Permanent regional block — retrying cannot help. Fail fast.
                raise
            if i == attempts - 1:
                break
            delay = base_delay * (2 ** i)
            logger.warning(
                "transient exchange error (attempt %d/%d): %s — retrying in %.1fs",
                i + 1, attempts, exc, delay,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


# Most venues (Binance included) cap a single klines/OHLCV call at ~1000 bars.
# To show a YEAR+ of history (TradingView-paid parity) we stitch several capped
# calls together by walking `since` forward — see BinanceConnector.fetch_ohlcv.
_MAX_SINGLE_CALL = 1000

# Real bar durations, in milliseconds, for the timeframes the app offers. Used
# only to step `since` when stitching deep history. A timeframe that isn't
# listed falls back to a single (most-recent) call rather than guessing a
# duration — we never invent a bar spacing.
_TIMEFRAME_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
}


def rank_movers(
    tickers: dict[str, Any],
    quote: str = "USDT",
    *,
    top: int = 10,
    min_quote_volume: float = 0.0,
) -> dict[str, list[dict[str, Any]]]:
    """Rank live 24h tickers into top gainers / losers / most-active pairs.

    Honest "market movers" — the real version of TradingView's paid
    screener/"Editor's Picks": computed straight from the venue's OWN 24h
    tickers, never an invented editorial list. A ticker is ranked ONLY when it
    exposes a real 24h percentage, a real last price AND a real quote volume;
    entries missing any of these — or carrying a non-finite value — are skipped
    rather than guessed. Only spot pairs quoted in `quote` are considered (a
    symbol containing ':' is a derivatives contract and is ignored).
    `min_quote_volume` drops illiquid pairs whose percentage move is just noise
    (a +900% print on $50/day volume is not a real "gainer"). Returns three
    lists, each ranked and capped at `top`; each row is honest, straight from
    the ticker: {symbol, last, percentage, quote_volume}.
    """
    q = (quote or "USDT").upper()
    floor = max(0.0, float(min_quote_volume))
    rows: list[dict[str, Any]] = []
    for sym, t in (tickers or {}).items():
        if not isinstance(sym, str) or not isinstance(t, dict):
            continue
        if ":" in sym or "/" not in sym:
            continue  # derivatives contract or malformed key — not a spot pair
        base, _, qc = sym.partition("/")
        if not base or qc.upper() != q:
            continue
        try:
            pct = float(t.get("percentage"))
            last = float(t.get("last", t.get("close")))
            qv = float(t.get("quoteVolume"))
        except (TypeError, ValueError):
            continue  # a field the venue didn't report — honest skip, never faked
        if not (math.isfinite(pct) and math.isfinite(last) and math.isfinite(qv)):
            continue
        if qv < floor:
            continue
        rows.append(
            {"symbol": sym, "last": last, "percentage": pct, "quote_volume": qv}
        )
    n = max(0, int(top))
    gainers = sorted(rows, key=lambda r: r["percentage"], reverse=True)[:n]
    losers = sorted(rows, key=lambda r: r["percentage"])[:n]
    most_active = sorted(rows, key=lambda r: r["quote_volume"], reverse=True)[:n]
    return {"gainers": gainers, "losers": losers, "most_active": most_active}


class BinanceConnector:
    """Wraps ccxt.binance with testnet support and defensive error handling."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Optional[ccxt.Exchange] = None
        self._markets: Optional[dict[str, Any]] = None
        self._exchange_id: str = "binance"
        # Optional secondary client used for PUBLIC market data ONLY when the
        # primary exchange is unreachable (see _build_data_fallback). Orders and
        # account reads never touch this — live execution always uses the primary.
        self._fallback_client: Optional[ccxt.Exchange] = None
        self._fallback_id: str = ""
        # Which venue actually served the most recent public read — an honest
        # label for the UI/logs: the primary exchange id, or the fallback id.
        self.last_data_source: str = "binance"
        # Cached trade-access probe (set by check_trading_access). Lets the
        # engine's read-only guard block live ORDER placement without a network
        # round-trip per order. None = not probed yet (unknown).
        self._can_trade: Optional[bool] = None
        self._can_read_account: Optional[bool] = None
        self._connect()

    def _connect(self) -> None:
        try:
            exchange_id = (
                getattr(self._settings, "exchange_id", "") or "binance"
            ).strip().lower()
            exchange_cls = getattr(ccxt, exchange_id, None)
            if exchange_cls is None:
                logger.warning(
                    "unknown exchange id %r; falling back to binance", exchange_id
                )
                exchange_cls = ccxt.binance
                exchange_id = "binance"
            self._exchange_id = exchange_id
            client = exchange_cls(
                {
                    "apiKey": self._settings.binance_api_key or None,
                    "secret": self._settings.binance_api_secret or None,
                    "enableRateLimit": True,
                    "options": {
                        "defaultType": "spot",
                        # Auto-sync our request timestamp to the exchange clock so
                        # a skewed local/host clock doesn't trigger Binance -1021
                        # "Timestamp outside recvWindow" rejections on private calls.
                        "adjustForTimeDifference": True,
                        "recvWindow": 10000,
                    },
                }
            )
            # Optional egress proxy: lets a server in a Binance-blocked region
            # route requests through a proxy in an allowed region. Empty (the
            # default) = direct connection, so existing setups are unchanged.
            proxy = (getattr(self._settings, "exchange_http_proxy", "") or "").strip()
            if proxy:
                try:
                    client.httpProxy = proxy
                    client.httpsProxy = proxy
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning("could not set exchange proxy: %s", exc)
            if self._settings.binance_testnet:
                # ccxt unified sandbox switch -> exchange testnet endpoints. Not
                # every exchange exposes a sandbox (e.g. binanceus); don't crash
                # the whole connector if it isn't supported.
                try:
                    client.set_sandbox_mode(True)
                except Exception as exc:
                    logger.warning(
                        "%s has no testnet/sandbox; using live endpoints: %s",
                        exchange_id,
                        exc,
                    )
            self._client = client
            self.last_data_source = self._exchange_id
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Failed to initialise exchange client: %s", exc)
            self._client = None
        # Build the optional PUBLIC market-data fallback regardless of whether the
        # primary initialised, so prices/candles still work when the primary is
        # unreachable (e.g. the Binance HTTP 451 geo-block). See below.
        self._build_data_fallback()

    def _build_data_fallback(self) -> None:
        """Build an optional keyless client for PUBLIC market data ONLY.

        When the primary exchange is unreachable from this server's region (the
        classic Binance HTTP 451 geo-block), prices and candles are read from
        this venue so the dashboard, analyzer and paper trading keep working with
        REAL market data. It is NEVER used for orders, balances or reconciliation
        — those always go to the primary (the user's real exchange keys). Uses a
        direct connection (no proxy): the fallback exists precisely for the case
        where the primary can't be reached directly. Empty id = disabled.
        """
        self._fallback_client = None
        self._fallback_id = ""
        fb = (
            getattr(self._settings, "market_data_fallback_id", "") or ""
        ).strip().lower()
        if not fb or fb == self._exchange_id:
            return
        fb_cls = getattr(ccxt, fb, None)
        if fb_cls is None:
            logger.warning("unknown market_data_fallback_id %r; fallback disabled", fb)
            return
        try:
            self._fallback_client = fb_cls(
                {"enableRateLimit": True, "options": {"defaultType": "spot"}}
            )
            self._fallback_id = fb
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("could not init market-data fallback %s: %s", fb, exc)

    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def has_credentials(self) -> bool:
        return bool(
            self._settings.binance_api_key and self._settings.binance_api_secret
        )

    @property
    def can_trade(self) -> Optional[bool]:
        """Cached result of the last trade-access probe: True if the key can
        place orders, False if it can only read (or nothing), None if not yet
        probed. Used by the engine to block live order placement for a
        read-only key without pretending the order went through."""
        return self._can_trade

    @property
    def can_read_account(self) -> Optional[bool]:
        """Cached: True if the key can read the real account balance."""
        return self._can_read_account

    def reload(self, settings: Settings) -> None:
        """Recreate the client, e.g. after settings change."""
        self._settings = settings
        self._markets: dict[str, Any] | None = None
        # Endpoint or credentials may have changed — invalidate the cached
        # trade-access so the next probe re-evaluates against the new key.
        self._can_trade = None
        self._can_read_account = None
        self._connect()

    # ---- markets / precision ----------------------------------------

    def _load_markets(self) -> dict[str, Any]:
        """Load and cache market metadata (precision, limits). Best-effort."""
        if self._markets is not None:
            return self._markets
        if not self._client:
            return {}
        try:
            self._markets = self._client.load_markets()
        except Exception as exc:
            logger.warning("load_markets failed: %s", exc)
            self._markets = {}
        return self._markets

    def list_symbols(self, quote: str = "USDT", limit: int = 300) -> list[str]:
        """Real, tradable spot symbols from the exchange (e.g. 'BTC/USDT').

        Sourced from ccxt ``load_markets()`` — the venue's ACTUAL listings, never
        a hardcoded guess. Filtered to active spot pairs with the given quote so
        the UI only offers pairs that really exist on Binance. A handful of
        majors are hoisted to the top for convenience; the rest follow
        alphabetically. Returns [] if the market list can't be loaded (honest
        empty, never fabricated)."""
        markets = self._load_markets()
        if not markets:
            return []
        q = (quote or "USDT").upper()
        out: list[str] = []
        for sym, m in markets.items():
            try:
                is_spot = bool(m.get("spot", m.get("type") == "spot"))
                if is_spot and m.get("active", True) and (m.get("quote") or "").upper() == q:
                    out.append(sym)
            except Exception:
                continue
        out.sort()
        majors = [f"BTC/{q}", f"ETH/{q}", f"BNB/{q}", f"SOL/{q}", f"XRP/{q}"]
        front = [s for s in majors if s in out]
        rest = [s for s in out if s not in front]
        ordered = front + rest
        return ordered[:limit] if limit else ordered

    def normalize_amount(
        self, symbol: str, amount: float, price: float
    ) -> tuple[float, str | None]:
        """Round `amount` to the exchange's lot precision and validate limits.

        Returns (adjusted_amount, error). If error is not None the order should
        NOT be placed. This prevents Binance rejecting orders that violate
        lot-size / min-notional / min-amount rules — a common live-trading bug.
        """
        if amount <= 0:
            return 0.0, "amount must be positive"
        markets = self._load_markets()
        market = markets.get(symbol)
        if not market or not self._client:
            # No metadata available (e.g. offline/paper): pass through unchanged.
            return amount, None
        try:
            adjusted = float(self._client.amount_to_precision(symbol, amount))
        except Exception:
            adjusted = amount
        limits = market.get("limits", {}) or {}
        amt_limits = limits.get("amount", {}) or {}
        cost_limits = limits.get("cost", {}) or {}
        min_amt = amt_limits.get("min")
        if min_amt is not None and adjusted < float(min_amt):
            return adjusted, (
                f"amount {adjusted} below exchange minimum {min_amt} for {symbol}"
            )
        min_cost = cost_limits.get("min")
        if min_cost is not None and adjusted * price < float(min_cost):
            return adjusted, (
                f"notional {adjusted * price:.2f} below exchange minimum "
                f"cost {min_cost} for {symbol}"
            )
        if adjusted <= 0:
            return 0.0, "amount rounded to zero at exchange precision"
        return adjusted, None

    # ---- Market data -------------------------------------------------

    def _fetch_public(self, call: Callable[[ccxt.Exchange], T]) -> T:
        """Run a READ-ONLY public market-data call, falling back to the secondary
        venue when the primary is unreachable.

        `call` receives the ccxt client to use. The fallback is engaged ONLY when
        the primary raises a regional geo-block (HTTP 451) or a transient/network
        error — never for a genuine data error like a bad symbol, which would be
        masked. When there is no fallback the original exception propagates, so an
        honest 451 still surfaces. Trading/account calls never use this path.
        """
        primary = self._client
        if primary is not None:
            try:
                result = _with_retry(lambda: call(primary))
                self.last_data_source = self._exchange_id
                return result
            except Exception as exc:
                can_fallback = self._fallback_client is not None and (
                    _is_geo_block(exc) or isinstance(exc, _RETRYABLE)
                )
                if not can_fallback:
                    raise
                logger.warning(
                    "%s market data unavailable (%s); serving from fallback %s",
                    self._exchange_id,
                    exc,
                    self._fallback_id,
                )
                result = _with_retry(lambda: call(self._fallback_client))
                self.last_data_source = self._fallback_id
                return result
        # No primary client at all — use the fallback if we have one.
        if self._fallback_client is None:
            raise RuntimeError("Exchange client not available")
        result = _with_retry(lambda: call(self._fallback_client))
        self.last_data_source = self._fallback_id
        return result

    def fetch_ticker(self, symbol: str) -> dict[str, Any]:
        return self._fetch_public(lambda c: c.fetch_ticker(symbol))

    def market_movers(
        self,
        quote: str = "USDT",
        *,
        top: int = 10,
        min_quote_volume: float = 0.0,
    ) -> dict[str, list[dict[str, Any]]]:
        """Live top gainers / losers / most-active spot pairs for `quote`.

        Honest market movers computed from the venue's real 24h tickers (ccxt
        ``fetch_tickers`` — one public call for the whole market) via the same
        geo-block-aware fallback path as the other market reads, so it keeps
        working when the primary is geo-blocked. Never an invented list — the
        ranking/filtering rules live in :func:`rank_movers`.
        """
        tickers = self._fetch_public(lambda c: c.fetch_tickers())
        return rank_movers(
            tickers or {}, quote, top=top, min_quote_volume=min_quote_volume
        )

    def fetch_price(self, symbol: str) -> float:
        ticker = self.fetch_ticker(symbol)
        last = ticker.get("last") or ticker.get("close")
        if last is None:
            raise RuntimeError(f"No price available for {symbol}")
        return float(last)

    def spread_pct(self, symbol: str) -> Optional[float]:
        """Current bid/ask spread as a % of mid price, or None if unknowable.

        Used as a pre-trade liquidity guard: a wide spread means a thin/volatile
        book and a poor fill. Returns None when the exchange doesn't expose both a
        bid and an ask (we refuse to *fabricate* a spread and block on it — the
        caller then proceeds rather than inventing a reason to skip).
        """
        if self._client is None and self._fallback_client is None:
            return None
        try:
            ticker = self._fetch_public(lambda c: c.fetch_ticker(symbol))
        except Exception as exc:
            logger.warning("spread probe failed for %s: %s", symbol, exc)
            return None
        bid = ticker.get("bid")
        ask = ticker.get("ask")
        try:
            bid_f = float(bid) if bid is not None else 0.0
            ask_f = float(ask) if ask is not None else 0.0
        except (TypeError, ValueError):
            return None
        if bid_f <= 0 or ask_f <= 0 or ask_f < bid_f:
            return None
        mid = (bid_f + ask_f) / 2.0
        if mid <= 0:
            return None
        return (ask_f - bid_f) / mid * 100.0

    def fetch_ohlcv(
        self, symbol: str, timeframe: str = "1h", limit: int = 200
    ) -> list[list[float]]:
        """Recent candles for `symbol`, newest last.

        For `limit` <= 1000 this is one public call. For deeper requests (the
        chart's "year+ history" depth picker) we STITCH several capped calls by
        stepping `since` forward, so the user can scroll back far further than a
        single exchange call allows — TradingView-paid history parity. Every row
        returned is real exchange data: gaps are left as gaps (never padded) and
        we stop as soon as the venue stops handing back new, forward-moving bars.
        """
        n = max(1, int(limit))
        if n <= _MAX_SINGLE_CALL:
            return self._fetch_public(
                lambda c: c.fetch_ohlcv(symbol, timeframe=timeframe, limit=n)
            )
        tf_ms = _TIMEFRAME_MS.get(timeframe)
        if not tf_ms:
            # Unknown bar size — don't guess a spacing; serve the most recent page.
            return self._fetch_public(
                lambda c: c.fetch_ohlcv(
                    symbol, timeframe=timeframe, limit=_MAX_SINGLE_CALL
                )
            )
        # Page forward from ~`n` bars ago in <=1000-bar chunks and concatenate.
        since = int(time.time() * 1000) - n * tf_ms
        out: list[list[float]] = []
        while len(out) < n:
            page = self._fetch_public(
                lambda c, s=since: c.fetch_ohlcv(
                    symbol, timeframe=timeframe, since=s, limit=_MAX_SINGLE_CALL
                )
            )
            if not page:
                break
            for row in page:
                # Guard against a venue re-returning the boundary bar.
                if not out or row[0] > out[-1][0]:
                    out.append(row)
            last_ts = page[-1][0]
            if last_ts <= since:  # no forward progress -> stop (never loop forever)
                break
            since = last_ts + tf_ms
            if len(page) < _MAX_SINGLE_CALL:  # venue returned its final page
                break
        return out[-n:] if len(out) > n else out

    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict[str, Any]:
        """Live order book: the market's real resting bids and asks.

        Read-only public depth via the same geo-block-aware fallback path as the
        other market reads, so it keeps working when the primary is geo-blocked.
        This is the market's ACTUAL resting liquidity (buy side = bids, sell side
        = asks) — NOT the user's own orders — and it is never fabricated: an
        unreachable venue raises (surfaced as a 502) rather than returning an
        invented book.
        """
        return self._fetch_public(lambda c: c.fetch_order_book(symbol, limit))

    # ---- Account -----------------------------------------------------

    def fetch_balance(self, quote: str = "USDT") -> Optional[float]:
        """Return free balance of the quote currency, or None if unavailable."""
        if not self._client or not self.has_credentials:
            return None
        try:
            balance = self._client.fetch_balance()
            free = balance.get("free", {})
            return float(free.get(quote, 0.0))
        except Exception as exc:
            logger.warning("fetch_balance failed: %s", exc)
            return None

    def fetch_position_amounts(self) -> dict[str, float]:
        """Return total held amount per base asset (for reconciliation).

        e.g. {"BTC": 0.5, "ETH": 2.0}. Empty dict if unavailable.
        """
        if not self._client or not self.has_credentials:
            return {}
        balance = self._client.fetch_balance()
        total = balance.get("total", {}) or {}
        return {asset: float(amt) for asset, amt in total.items() if amt}

    # ---- Orders ------------------------------------------------------

    @staticmethod
    def _new_client_order_id(prefix: str = "tt") -> str:
        """A fresh idempotency key for ONE logical order.

        Binance echoes ``newClientOrderId`` back and rejects a second order that
        reuses it, so retrying a create with the SAME id can never double-fill —
        the first attempt owns the id. Kept within Binance's 36-char
        clientOrderId limit and its allowed charset (alphanumerics + ``-``).
        """
        return f"{prefix}-{uuid.uuid4().hex[:24]}"

    @staticmethod
    def _is_duplicate_order(exc: Exception) -> bool:
        """True for Binance's duplicate-clientOrderId rejection ("Duplicate order
        sent.", code -2010). It means a PRIOR attempt of the same logical order
        already landed, so we must reconcile to that order rather than place a
        second one.
        """
        return "duplicate order" in str(exc).lower()

    def _fetch_by_client_id(
        self, symbol: str, client_order_id: str
    ) -> Optional[dict[str, Any]]:
        """Best-effort fetch of an order by its clientOrderId. Returns the order
        if the exchange has it, else None. NEVER raises: a failed reconcile must
        not itself become a reason to risk a second fill.
        """
        try:
            return self._client.fetch_order(
                None, symbol, {"clientOrderId": client_order_id}
            )
        except Exception as exc:
            logger.warning("reconcile fetch for %s failed: %s", client_order_id, exc)
            return None

    def _create_order_idempotent(
        self,
        symbol: str,
        type_: str,
        side: str,
        amount: float,
        price: float | None = None,
        *,
        attempts: int = 3,
        base_delay: float = 0.5,
    ) -> dict[str, Any]:
        """Place an order with retry that CANNOT double-fill.

        A market/limit CREATE is not safely retryable on its own: a
        RequestTimeout can arrive AFTER Binance accepted and executed the order
        (the request landed, the response was lost). A blind retry — what plain
        ``_with_retry`` around a create would do — then places a SECOND order and
        doubles the position. For a bot meant to PREVENT account blow-ups that is
        exactly the wrong failure.

        So we attach a client order id and make the retry idempotent:
          • the id is generated ONCE and reused on every attempt;
          • after a transient error, before retrying we FETCH the order by that
            id; if it exists the create already succeeded → return it, no 2nd
            order;
          • if a retry still races and Binance rejects the id as a DUPLICATE, we
            fetch by the id and return the real (already-placed) order.
        Non-transient errors (insufficient funds, bad symbol, auth) raise at once.
        """
        coid = self._new_client_order_id()
        params = {"clientOrderId": coid}
        last_exc: Exception | None = None
        for i in range(attempts):
            try:
                return self._client.create_order(
                    symbol, type_, side, amount, price, params
                )
            except Exception as exc:
                if self._is_duplicate_order(exc):
                    # A prior attempt of THIS logical order already landed.
                    existing = self._fetch_by_client_id(symbol, coid)
                    if existing:
                        logger.warning(
                            "order %s: duplicate rejected, prior attempt landed "
                            "— using it, not placing again", coid,
                        )
                        return existing
                    raise
                if not isinstance(exc, _RETRYABLE) or _is_geo_block(exc):
                    raise
                last_exc = exc
                # Transient error: the create MAY have executed before the
                # response was lost. Reconcile by client id BEFORE risking a
                # second placement.
                existing = self._fetch_by_client_id(symbol, coid)
                if existing:
                    logger.warning(
                        "order %s landed despite a transient error — using it, "
                        "not retrying", coid,
                    )
                    return existing
                if i == attempts - 1:
                    break
                delay = base_delay * (2 ** i)
                logger.warning(
                    "transient error placing order %s (attempt %d/%d): %s — "
                    "retrying in %.1fs", coid, i + 1, attempts, exc, delay,
                )
                time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    def create_market_order(
        self, symbol: str, side: str, amount: float
    ) -> dict[str, Any]:
        """Place a REAL market order. Only called in live mode."""
        if not self._client:
            raise RuntimeError("Exchange client not available")
        if not self.has_credentials:
            raise RuntimeError("Binance API credentials are not configured")
        return self._create_order_idempotent(symbol, "market", side, amount)

    def create_limit_order(
        self, symbol: str, side: str, amount: float, price: float
    ) -> dict[str, Any]:
        """Place a REAL resting limit order. Only called in live mode.

        The order sits on the book until price crosses it (or it is cancelled).
        Caller polls fetch_order() to detect the fill.
        """
        if not self._client:
            raise RuntimeError("Exchange client not available")
        if not self.has_credentials:
            raise RuntimeError("Binance API credentials are not configured")
        try:
            limit = float(self._client.price_to_precision(symbol, price))
        except Exception:
            limit = price
        return self._create_order_idempotent(symbol, "limit", side, amount, limit)

    def fetch_order(self, order_id: str, symbol: str) -> Optional[dict[str, Any]]:
        """Fetch a single order's current state (status, filled, average price).

        Best-effort: returns None if unavailable so the caller can retry next
        tick rather than crashing the monitor loop.
        """
        if not self._client or not self.has_credentials or not order_id:
            return None
        try:
            return _with_retry(lambda: self._client.fetch_order(order_id, symbol))
        except Exception as exc:
            logger.warning("fetch_order %s failed: %s", order_id, exc)
            return None

    def create_stop_loss_order(
        self, symbol: str, side: str, amount: float, stop_price: float
    ) -> Optional[dict[str, Any]]:
        """Place a REAL exchange-side stop order to protect a position.

        This is the safety net for when the bot process is down: the exchange
        itself triggers the exit. Best-effort — returns None (and logs) if the
        exchange/market doesn't support it, so the in-process SL/TP monitor stays
        the fallback rather than the app crashing.
        """
        if not self._client or not self.has_credentials:
            return None
        try:
            stop = float(self._client.price_to_precision(symbol, stop_price))
        except Exception:
            stop = stop_price
        # Binance spot STOP_LOSS_LIMIT needs a limit price. Set the limit slightly
        # THROUGH the stop (below it for a protective sell) so it still fills in a
        # fast drop instead of resting unfilled at exactly the stop.
        limit_price = stop * (0.995 if side == "sell" else 1.005)
        try:
            limit_price = float(self._client.price_to_precision(symbol, limit_price))
        except Exception:
            pass
        try:
            params = {"stopPrice": stop}
            return self._client.create_order(
                symbol, "stop_loss_limit", side, amount, limit_price, params
            )
        except Exception as exc:
            logger.warning("stop-loss order not placed for %s: %s", symbol, exc)
            return None

    def cancel_order(self, order_id: str, symbol: str) -> None:
        """Best-effort cancel of a resting order (e.g. a stop when we exit)."""
        if not self._client or not self.has_credentials or not order_id:
            return
        try:
            self._client.cancel_order(order_id, symbol)
        except Exception as exc:
            logger.warning("cancel_order %s failed: %s", order_id, exc)

    # ---- health / permissions ---------------------------------------

    def check_trading_access(self) -> dict[str, Any]:
        """Probe trade access and CACHE the result on the connector, so the
        engine's read-only guard can cheaply block live order placement for a
        key that can read the account but not trade."""
        result = self._probe_trading_access()
        self._can_trade = bool(result.get("can_trade"))
        self._can_read_account = bool(result.get("can_read_account"))
        return result

    def _probe_trading_access(self) -> dict[str, Any]:
        """Probe whether the configured key can actually TRADE, not just read.

        Returns a dict: {ok, can_read_public, can_read_account, can_trade,
        testnet, detail}. This makes the common -2015 failure (key with no Spot
        trading permission, wrong site, or IP restriction) obvious at startup
        instead of only when the first order silently fails.

        It never places an order: it reads public data, then reads the private
        account balance (which requires a valid, permissioned key). Trading
        permission is inferred from the account's reported permissions when the
        exchange exposes them, otherwise from a successful private read.
        """
        result: dict[str, Any] = {
            "ok": False,
            "can_read_public": False,
            "can_read_account": False,
            "can_trade": False,
            "testnet": self._settings.binance_testnet,
            "exchange": self._exchange_id,
            "data_source": self.last_data_source,
            "fallback": self._fallback_id or None,
            "detail": "",
        }
        if not self._client:
            result["detail"] = "exchange client not available"
            return result
        # 1) public read — run this BEFORE the credentials check so a regional
        #    geo-block (HTTP 451) is surfaced clearly even for paper users with no
        #    keys, who would otherwise only see opaque 502s on market data.
        try:
            self._client.fetch_time()
            result["can_read_public"] = True
        except Exception as exc:
            msg = str(exc)
            low = msg.lower()
            if "451" in msg or "restricted location" in low or "eligibility" in low:
                if self._fallback_client is not None:
                    result["detail"] = (
                        f"{self._exchange_id} is geo-blocking this server's region "
                        "(HTTP 451) — it refuses requests based on where the server "
                        "runs, and no API key can undo it. Public market data "
                        "(prices, candles, paper trading) is being served from "
                        f"{self._fallback_id} instead, so the dashboard keeps "
                        "working. LIVE trading still needs the real exchange "
                        "reachable: set EXCHANGE_HTTP_PROXY to route through an "
                        "allowed region, deploy in a supported region, or (US only) "
                        "set EXCHANGE_ID=binanceus."
                    )
                else:
                    result["detail"] = (
                        f"{self._exchange_id} is geo-blocking this server's region "
                        "(HTTP 451). This is the exchange refusing the request based "
                        "on where the server runs — not a bug, and no API key can "
                        "undo it. Nothing works (not even public prices) until you "
                        "either deploy in a supported region, set EXCHANGE_HTTP_PROXY "
                        "to route through an allowed region, (US only) set "
                        "EXCHANGE_ID=binanceus, or set MARKET_DATA_FALLBACK_ID to a "
                        "reachable venue for read-only market data."
                    )
            else:
                result["detail"] = f"public data unreachable: {exc}"
            return result
        if not self.has_credentials:
            result["detail"] = "no API credentials configured (paper mode is fine)"
            return result
        # 2) private account read (this is what fails with -2015)
        try:
            balance = self._client.fetch_balance()
            result["can_read_account"] = True
        except Exception as exc:
            msg = str(exc)
            if "-2015" in msg or "Invalid API-key" in msg:
                result["detail"] = (
                    "account access denied (-2015): the key is invalid for this "
                    "endpoint, lacks permission, or is IP-restricted. For testnet, "
                    "create the key at testnet.binance.vision with Spot trading "
                    "enabled and no/matching IP restriction."
                )
            else:
                result["detail"] = f"account read failed: {msg}"
            return result
        # 3) determine TRADE permission — whether THIS key can place spot orders.
        #    The most reliable signal is the key's OWN restrictions, not just the
        #    account's: a read-only key on a spot-eligible account still cannot
        #    trade. Read that where available; otherwise fall back to the
        #    account's reported permissions.
        key_spot = self._key_spot_trading_enabled()
        if key_spot is not None:
            result["can_trade"] = key_spot
            result["ok"] = key_spot
            result["detail"] = (
                "trading enabled (this API key has Spot & Margin trading)"
                if key_spot
                else (
                    "key can read your account but this API key does NOT have "
                    "Spot trading enabled — you can see real balances and market "
                    "data, but to place orders turn on 'Enable Spot & Margin "
                    "Trading' for the key in Binance API Management (and clear any "
                    "IP restriction that blocks this server)."
                )
            )
            return result
        # Fall back to the account's reported permissions when the key-level
        # signal isn't available (testnet has no sapi, non-binance venues differ,
        # or the endpoint erroring).
        perms = self._account_permissions(balance)
        if perms is None:
            # Exchange didn't expose permissions; a successful private read means
            # the key works, so treat trading as available (best-effort).
            result["can_trade"] = True
            result["ok"] = True
            result["detail"] = "account readable; trading permission not reported (assumed enabled)"
            return result
        # Binance reports spot-trade authorization either as "SPOT" or as a
        # trade-group tag "TRD_GRP_NNN" (002, 003, 004 … 061 …). Per Binance, a
        # TRD_GRP entry means the account IS authorized to trade that group's
        # symbols — it's a spot permission, just region/eligibility-scoped. New
        # groups are minted over time, so match the whole TRD_GRP_* family rather
        # than a hardcoded few; otherwise a genuinely tradeable account (e.g.
        # TRD_GRP_061) is wrongly told Spot trading is off and blocked from going
        # live. MARGIN / LEVERAGED are named separately and are NOT spot.
        def _is_spot_perm(p: str) -> bool:
            pl = p.lower()
            return "spot" in pl or pl.startswith("trd_grp_")

        if any(_is_spot_perm(p) for p in perms):
            result["can_trade"] = True
            result["ok"] = True
            result["detail"] = f"trading enabled (permissions: {', '.join(perms)})"
        else:
            result["detail"] = (
                f"key can read the account but Spot trading is NOT enabled "
                f"(permissions: {', '.join(perms) or 'none'}). Enable Spot trading "
                f"on the API key to place orders."
            )
        return result

    def _account_permissions(self, balance: dict[str, Any]) -> Optional[list[str]]:
        """Best-effort extraction of the account's trading permissions."""
        try:
            info = (balance or {}).get("info", {}) or {}
            perms = info.get("permissions")
            if isinstance(perms, list) and perms:
                return [str(p) for p in perms]
            # Some responses expose canTrade instead of a permissions list.
            if "canTrade" in info:
                return ["spot"] if info.get("canTrade") else []
        except Exception:
            pass
        return None

    def _key_spot_trading_enabled(self) -> Optional[bool]:
        """Whether THIS API key may place spot orders, read from the key's own
        restrictions (GET /sapi/v1/account/apiRestrictions ->
        ``enableSpotAndMarginTrading``).

        This reflects the KEY, not just the account: a read-only key on a
        spot-eligible account (e.g. permissions ``TRD_GRP_061``) returns False
        here, so we never tell the user they can trade when their key can't.
        Returns None when the signal isn't available — testnet has no sapi,
        non-binance venues differ, or the endpoint errors — so the caller falls
        back to account-permission inference. Never places an order.
        """
        if self._exchange_id != "binance" or self._settings.binance_testnet:
            return None
        fn = getattr(self._client, "sapiGetAccountApiRestrictions", None)
        if not callable(fn):
            return None
        try:
            res = fn()
        except Exception as exc:  # endpoint disabled / region-blocked / etc.
            logger.info("apiRestrictions probe unavailable: %s", exc)
            return None
        if not isinstance(res, dict) or "enableSpotAndMarginTrading" not in res:
            return None
        return bool(res.get("enableSpotAndMarginTrading"))
