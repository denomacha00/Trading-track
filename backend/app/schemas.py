"""Pydantic request/response schemas for the API."""
from __future__ import annotations

import datetime as dt
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class TradingViewSignal(BaseModel):
    """Payload sent by a TradingView alert webhook.

    Example alert message (JSON):
        {"secret": "...", "action": "buy", "symbol": "BTC/USDT", "amount": 0.001}
    """

    # Optional now: the unguessable per-user token in the webhook URL is the
    # authenticator. Kept for backward-compatible alert payloads.
    secret: Optional[str] = None
    action: Literal["buy", "sell", "close"]
    symbol: str
    # Optional explicit amount (base currency). If omitted, risk manager sizes it.
    amount: Optional[float] = Field(default=None, gt=0)
    # Optional overrides
    price: Optional[float] = Field(default=None, gt=0)
    limit_price: Optional[float] = Field(default=None, gt=0)  # resting limit entry
    stop_loss: Optional[float] = Field(default=None, gt=0)
    take_profit: Optional[float] = Field(default=None, gt=0)
    note: Optional[str] = None


class ManualOrder(BaseModel):
    action: Literal["buy", "sell", "close"]
    symbol: str
    amount: Optional[float] = Field(default=None, gt=0)
    limit_price: Optional[float] = Field(default=None, gt=0)  # resting limit entry
    stop_loss: Optional[float] = Field(default=None, gt=0)
    take_profit: Optional[float] = Field(default=None, gt=0)


class TradeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    symbol: str
    side: str
    amount: float
    entry_price: float
    exit_price: Optional[float]
    stop_loss: Optional[float]
    take_profit: Optional[float]
    status: str
    order_type: str = "market"
    limit_price: Optional[float] = None
    pnl: float
    fee: float = 0.0  # real exchange fees observed (quote); live only, else 0
    mode: str
    source: str
    note: Optional[str]
    opened_at: dt.datetime
    closed_at: Optional[dt.datetime]


class SignalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    symbol: Optional[str]
    action: Optional[str]
    accepted: int
    message: Optional[str]
    # Real model confidence for analyzer rows (0..1); None when the source didn't
    # provide one (e.g. a TradingView webhook). Never fabricated.
    confidence: Optional[float] = None
    created_at: dt.datetime


class ExecutionResult(BaseModel):
    accepted: bool
    message: str
    trade: Optional[TradeOut] = None


class ScaledOrder(BaseModel):
    """Open a BUY in scaled (DCA) legs: the first optionally at market, the rest
    as resting limits stepped ``step_pct``% apart below it. ``amount`` is the
    TOTAL base quantity across all legs; omit it to let the risk manager size the
    whole entry. ``stop_loss``/``take_profit`` apply to each filled leg."""

    symbol: str
    amount: Optional[float] = Field(default=None, gt=0)  # total base qty; blank = risk-sized
    legs: int = Field(ge=2, le=20)
    step_pct: float = Field(gt=0, le=50)  # price gap between legs, % of price
    first_at_market: bool = True
    stop_loss: Optional[float] = Field(default=None, gt=0)
    take_profit: Optional[float] = Field(default=None, gt=0)


class ScaledResult(BaseModel):
    accepted: bool
    message: str
    legs: list[TradeOut] = []


class CloseAllResult(BaseModel):
    """Result of closing/cancelling every position + resting order for a symbol."""

    closed: int
    realized_pnl: float
    message: str


class TickerOut(BaseModel):
    symbol: str
    last: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    percentage: Optional[float] = None
    # 24h traded volume as the exchange reports it: base_volume in the base asset
    # (e.g. BTC), quote_volume in the quote asset (e.g. USDT). None when the venue
    # omits it — never fabricated.
    base_volume: Optional[float] = None
    quote_volume: Optional[float] = None
    # Which venue actually served this price: the primary exchange, or the
    # public-data fallback when the primary is geo-blocked. Honest source label
    # so the UI never implies a price came from somewhere it didn't.
    source: Optional[str] = None


class OrderBookLevel(BaseModel):
    price: float
    amount: float


class OrderBookOut(BaseModel):
    """A snapshot of the market's real resting orders (depth).

    `bids` are the buy side (highest price first), `asks` the sell side (lowest
    price first). Straight from the exchange (or the public fallback); an empty
    side means the venue returned no depth, never an invented ladder.
    """

    symbol: str
    bids: list[OrderBookLevel]
    asks: list[OrderBookLevel]
    source: Optional[str] = None


class PerfBucket(BaseModel):
    """Realized-P&L stats for a set of closed trades (all figures from real
    trades; ``profit_factor`` is null when there are no losing trades)."""

    closed_trades: int
    wins: int
    losses: int
    breakeven: int
    win_rate_pct: float
    total_pnl: float
    gross_profit: float
    gross_loss: float
    profit_factor: Optional[float] = None
    avg_win: float
    avg_loss: float
    expectancy: float
    largest_win: float
    largest_loss: float
    max_drawdown: float


class PerfSymbol(BaseModel):
    symbol: str
    trades: int
    pnl: float
    wins: int


class PerformanceOut(PerfBucket):
    """Overall stats (inherited) plus paper/live splits and a per-symbol
    breakdown. Paper and live are separate so simulated gains are never counted
    as real money."""

    avg_hold_seconds: Optional[float] = None
    paper: PerfBucket
    live: PerfBucket
    by_symbol: list[PerfSymbol]


class BotStatus(BaseModel):
    running: bool
    trading_mode: str
    testnet: bool
    exchange_connected: bool
    open_positions: int
    balance: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    day_pnl: float
    max_open_positions: int
    # Risk-safeguard + autopilot visibility (all optional so older callers/tests
    # that only assert the core keys keep passing).
    killswitch: bool = False
    max_drawdown_pct: float = 0.0
    peak_equity: float = 0.0
    auto_trade_enabled: bool = False
    consecutive_losses: int = 0
    max_consecutive_losses: int = 0
    entries_paused: bool = False
    entries_pause_reason: Optional[str] = None
    # Per-symbol regime snapshots: {SYMBOL: {regime, detail, protective_hold,
    # entries_paused, verdict, at}}. Empty until the monitor has analysed a symbol.
    regimes: dict[str, Any] = Field(default_factory=dict)


class SettingsOut(BaseModel):
    trading_mode: str
    binance_testnet: bool
    max_open_positions: int
    risk_per_trade_pct: float
    daily_loss_limit_pct: float
    default_stop_loss_pct: float
    default_take_profit_pct: float
    trailing_stop_pct: float
    max_total_exposure_pct: float
    max_position_pct: float = 0.0
    paper_taker_fee_pct: float = 0.0
    min_signal_confidence: float
    auto_trade_enabled: bool
    auto_symbols: str
    auto_timeframe: str
    auto_confirm_timeframe: str
    use_saved_strategy: bool = False
    ai_trade_confirm: bool = False
    ai_pretrade_analysis: bool = False
    ai_monitor_enabled: bool = False
    ai_autopilot_enabled: bool = False
    ai_enabled: bool
    ai_model: str = ""
    ai_style: str = ""
    # Visible pause/resume + saved-strategy validation gate.
    auto_pause_in_bear: bool = True
    require_strategy_validation: bool = True
    strategy_min_return_pct: float = 0.0
    strategy_min_win_rate_pct: float = 0.0
    strategy_min_trades: int = 5
    strategy_max_drawdown_pct: float = 0.0
    # Profit-lock / early profit-take (autopilot).
    profit_lock_enabled: bool = False
    profit_lock_trigger_pct: float = 1.0
    profit_lock_floor_pct: float = 0.3
    take_profit_on_reversal: bool = False
    reversal_confirm_count: int = 2
    # Global background monitor cadence (seconds).
    monitor_interval_seconds: float = 5.0
    notifications_enabled: bool
    api_key_set: bool
    webhook_path: str
    webhook_secret_set: bool


class SettingsUpdate(BaseModel):
    trading_mode: Optional[Literal["paper", "live"]] = None
    max_open_positions: Optional[int] = Field(default=None, ge=1, le=100)
    risk_per_trade_pct: Optional[float] = Field(default=None, gt=0, le=100)
    daily_loss_limit_pct: Optional[float] = Field(default=None, gt=0, le=100)
    default_stop_loss_pct: Optional[float] = Field(default=None, gt=0, le=100)
    default_take_profit_pct: Optional[float] = Field(default=None, gt=0, le=100)
    min_signal_confidence: Optional[float] = Field(default=None, ge=0, le=1)
    auto_trade_enabled: Optional[bool] = None
    auto_symbols: Optional[str] = None
    auto_timeframe: Optional[str] = None
    auto_confirm_timeframe: Optional[str] = None
    use_saved_strategy: Optional[bool] = None
    ai_trade_confirm: Optional[bool] = None
    ai_pretrade_analysis: Optional[bool] = None
    ai_monitor_enabled: Optional[bool] = None
    ai_autopilot_enabled: Optional[bool] = None
    trailing_stop_pct: Optional[float] = Field(default=None, ge=0, le=100)
    max_total_exposure_pct: Optional[float] = Field(default=None, ge=0, le=1000)
    max_position_pct: Optional[float] = Field(default=None, ge=0, le=100)
    paper_taker_fee_pct: Optional[float] = Field(default=None, ge=0, le=5)
    # Visible pause/resume + saved-strategy validation gate.
    auto_pause_in_bear: Optional[bool] = None
    require_strategy_validation: Optional[bool] = None
    strategy_min_return_pct: Optional[float] = Field(default=None, ge=-100, le=1000)
    strategy_min_win_rate_pct: Optional[float] = Field(default=None, ge=0, le=100)
    strategy_min_trades: Optional[int] = Field(default=None, ge=0, le=100000)
    strategy_max_drawdown_pct: Optional[float] = Field(default=None, ge=0, le=100)
    # Profit-lock / early profit-take (autopilot). The engine additionally clamps
    # the effective floor/trigger to clear round-trip fees, so a tiny value here
    # can never "lock" a fee-loss — but keep trigger > floor for the stop to sit
    # below price.
    profit_lock_enabled: Optional[bool] = None
    profit_lock_trigger_pct: Optional[float] = Field(default=None, ge=0, le=100)
    profit_lock_floor_pct: Optional[float] = Field(default=None, ge=0, le=100)
    take_profit_on_reversal: Optional[bool] = None
    reversal_confirm_count: Optional[int] = Field(default=None, ge=1, le=20)
    # Global background monitor cadence (seconds); clamped to [3, 60] server-side.
    monitor_interval_seconds: Optional[float] = Field(default=None, ge=3, le=60)


class AlertCreate(BaseModel):
    """Create a price alert: notify when ``symbol`` crosses ``price``."""

    symbol: str
    condition: Literal["above", "below"]
    price: float = Field(gt=0)
    note: Optional[str] = Field(default=None, max_length=200)


class AlertOut(BaseModel):
    id: int
    symbol: str
    condition: str
    price: float
    note: Optional[str] = None
    status: str
    created_at: Optional[dt.datetime] = None
    triggered_at: Optional[dt.datetime] = None
    triggered_price: Optional[float] = None


# ---- Auth & multi-user ----------------------------------------------


class SignupRequest(BaseModel):
    # A client signs up with the licence key you sold them, plus the username +
    # email + password they'll log in with next time. The key is optional only
    # for the bootstrap admin / auto-license mode (enforced server-side).
    username: str = Field(min_length=3, max_length=64)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=200)
    license_key: Optional[str] = Field(default=None, max_length=200)


class LoginRequest(BaseModel):
    # Accept either the username OR the email in a single field.
    identifier: str = Field(min_length=1, max_length=255)
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: Optional[str] = None
    email: str
    role: str
    license_status: str
    # Effective access (status active AND not past expiry) + how much time is
    # left, so the admin sees at a glance who is truly live and for how long.
    license_active: bool = False
    license_expires_at: Optional[dt.datetime] = None
    license_days_left: Optional[int] = None
    created_at: dt.datetime
    licensed_at: Optional[dt.datetime] = None


class MeOut(BaseModel):
    """Current user + capability flags the UI needs to render correctly."""

    id: int
    username: Optional[str] = None
    email: str
    role: str
    license_status: str
    license_active: bool = False
    license_expires_at: Optional[dt.datetime] = None
    license_days_left: Optional[int] = None
    webhook_path: str
    binance_keys_set: bool
    binance_testnet: bool
    ai_key_set: bool
    ai_model: str = ""
    secrets_storage_enabled: bool


class CredentialsUpdate(BaseModel):
    """Per-user API-key entry. Any field omitted is left unchanged.

    Only exchange keys are per-user. The AI/LLM is an app-wide, operator-provided
    capability, so no AI fields are accepted here.
    """

    binance_api_key: Optional[str] = None
    binance_api_secret: Optional[str] = None
    binance_testnet: Optional[bool] = None


class LicenseUpdate(BaseModel):
    status: Literal["pending", "active", "revoked"]


class AddLicenseDays(BaseModel):
    """Admin action: extend (or start) a user's time-limited licence by N days."""

    days: int = Field(ge=1, le=3650)


# ---- Licence keys (admin generates, users self-redeem) --------------


class LicenseKeyCreate(BaseModel):
    """Admin request to mint a new licence key.

    ``label`` is a free-text reminder (e.g. the client's name). ``duration_days``
    sets how long the licence lasts once redeemed — omit (or null) for a
    lifetime key that never expires.
    """

    label: Optional[str] = Field(default=None, max_length=120)
    duration_days: Optional[int] = Field(default=None, ge=1, le=3650)


class LicenseKeyOut(BaseModel):
    """Admin-facing view of a licence key. NEVER carries the plaintext key —
    that is returned only once, at creation, via :class:`LicenseKeyCreated`."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    key_prefix: str
    label: Optional[str] = None
    duration_days: Optional[int] = None
    status: str
    created_at: dt.datetime
    redeemed_by: Optional[int] = None
    redeemed_at: Optional[dt.datetime] = None


class LicenseKeyCreated(LicenseKeyOut):
    """Returned exactly once when a key is generated — includes the plaintext
    ``key`` so the admin can copy it. It is never stored or returned again."""

    key: str


class RedeemLicenseKey(BaseModel):
    key: str = Field(min_length=8, max_length=200)
