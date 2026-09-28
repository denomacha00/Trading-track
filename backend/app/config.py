"""Application configuration loaded from environment / .env file."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Trading mode: "paper" (simulated) or "live" (real orders)
    trading_mode: str = Field(default="paper")

    # Binance
    binance_api_key: str = Field(default="")
    binance_api_secret: str = Field(default="")
    binance_testnet: bool = Field(default=True)

    # Exchange selection & networking. Default is Binance global — nothing
    # changes unless you set these. Binance legally geo-blocks some server
    # regions (HTTP 451): from a blocked region NO Binance call works (not even
    # public market data), which looks like "everything fails". Two real levers:
    #   * exchange_id="binanceus" if your server is in the US (ccxt `binanceus`;
    #     note: US spot markets, no testnet), or another ccxt exchange id.
    #   * exchange_http_proxy=<url> to route requests through an HTTP/HTTPS proxy
    #     that sits in a Binance-supported region.
    # Empty proxy = direct connection. This is honest infra config: it does not
    # fake data, it changes where/what the app actually talks to.
    exchange_id: str = Field(default="binance")
    exchange_http_proxy: str = Field(default="")
    # PUBLIC market-data fallback. If the primary exchange is unreachable from
    # this server's region (the classic Binance HTTP 451 geo-block), read prices
    # and candles from THIS venue instead, so the dashboard, analyzer and paper
    # trading keep working with REAL market data. It powers read-only market data
    # ONLY — live orders and balances always go to the real exchange above, never
    # here. A ccxt exchange id with matching USDT symbols (e.g. "kucoin", "okx",
    # "bybit"); empty disables it. Only activates when the primary actually fails,
    # so a healthy Binance deployment is completely unaffected.
    market_data_fallback_id: str = Field(default="kucoin")

    # TradingView webhook. NOTE: a webhook alert authenticates by the unguessable
    # per-user token embedded in its URL (User.webhook_token) — that token IS the
    # shared secret, rotatable via POST /api/webhook/rotate. There is deliberately
    # no global signing secret (TradingView can't HMAC-sign its payloads anyway).
    # Webhook REPLAY protection (opt-in). When > 0, an inbound alert whose payload
    # carries a send-time (TradingView's {{timenow}} placeholder, mapped to a
    # "timenow"/"timestamp" field) is REJECTED if that time is more than this many
    # seconds away from now — so a captured webhook URL+body can't be replayed
    # later to fire a stale trade. 0 (default) disables it: a normal deployment is
    # completely unaffected, because clock skew or a missing timestamp would
    # otherwise cause false rejects. Idempotency keys (see WebhookDelivery) already
    # stop a replay of a KEYED payload regardless of this setting; this closes the
    # gap for keyless payloads. Turn it on only once your alerts send a timestamp.
    webhook_max_age_seconds: float = Field(default=0.0)

    # Optional AI/LLM commentary. Works with EITHER an OpenAI-compatible
    # chat-completions API OR an Anthropic-native messages API. Leave
    # ai_api_key empty to disable — the bot works fully without it.
    ai_api_key: str = Field(default="")
    ai_base_url: str = Field(default="https://api.openai.com/v1")
    ai_model: str = Field(default="gpt-4o-mini")
    ai_timeout_seconds: float = Field(default=45.0)
    # Fast-failover connect timeout (seconds). A DOWN/unreachable primary provider
    # (the classic "justworker is down" case) is abandoned after this many seconds
    # so the SAME request fails over to the secondary provider — instead of the
    # client hanging for the full ai_timeout_seconds. Caps CONNECT time only, so a
    # healthy provider (connects in <1s) is never affected and a slow-but-alive
    # response still gets the full read budget. Set to 10s so justworker (primary)
    # gets a bit more grace to answer when it's slow-but-alive before we hand the
    # request to the fallback; the trade-off is failover on a truly-dead primary
    # takes up to ~10s instead of ~5s. Lower it for snappier failover.
    ai_connect_timeout_seconds: float = Field(default=10.0)
    # Provider API style: "auto" (infer from model/base_url), "openai", or
    # "anthropic". Auto picks Anthropic when the model looks like a Claude model
    # or the base URL is anthropic-flavoured; otherwise OpenAI chat-completions.
    ai_api_style: str = Field(default="auto")
    # Token budget for AI replies. Higher = more room to reason/research.
    ai_max_tokens: int = Field(default=1024)

    # OPTIONAL SECONDARY (FALLBACK) AI provider — a backup so the assistant keeps
    # working mid-trade if the PRIMARY provider goes down. Mirrors the public
    # market-data fallback above: when a PRIMARY request fails for any reason
    # (provider down/5xx, rate-limited 429, edge/WAF block, timeout, unreachable),
    # the SAME request is transparently retried ONCE against this provider so chat,
    # analysis and the trade-review veto don't go dark. Leave ai_fallback_api_key
    # EMPTY to disable — behaviour is then byte-for-byte what it is today. It is
    # only ever a backstop: a healthy primary is never sent here, this provider can
    # use a totally different vendor/style, and (like the primary) it can only
    # narrate/answer/veto — it never places a trade. Same key handling as the
    # primary (whitespace/quote/NAME= cleaning) so a pasted Railway value works.
    ai_fallback_api_key: str = Field(default="")
    # This bot's configured fallback is glm-5.3-flash on the hcnsec gateway
    # (OpenAI-compatible). These are non-secret defaults, so on Railway you only
    # need to set AI_FALLBACK_API_KEY to switch the fallback ON — base_url/model/
    # style are already correct. Override any of them via env if you swap vendors.
    ai_fallback_base_url: str = Field(default="https://api.hcnsec.cn/v1")
    ai_fallback_model: str = Field(default="glm-5.3-flash")
    ai_fallback_api_style: str = Field(default="openai")

    # Risk management
    max_open_positions: int = Field(default=5)
    risk_per_trade_pct: float = Field(default=1.0)
    daily_loss_limit_pct: float = Field(default=5.0)
    default_stop_loss_pct: float = Field(default=2.0)
    default_take_profit_pct: float = Field(default=4.0)
    # CONCENTRATION CAP — the single most important beginner guardrail against
    # blowing up on one bad coin. No single position's notional may exceed this
    # % of TOTAL account equity. It clamps the AUTO-sizer (an explicit operator/
    # webhook amount is the caller's own deliberate choice and isn't shrunk here,
    # only bounded by free cash + the exposure cap). Why it matters: at the
    # default 1% risk / 2% stop the risk formula alone would put ~50% of equity in
    # ONE trade, and a very tight stop drives that toward 100% — so without this
    # cap "risk 1%" quietly becomes "half the account on one ticker". 25% means a
    # beginner is diversified across at least ~4 positions by construction. Caps
    # only ever REDUCE size. 0 disables (power users who size manually).
    max_position_pct: float = Field(default=25.0)
    # Cap on TOTAL open notional across ALL positions, as a % of TOTAL equity.
    # Portfolio-level leverage control layered on top of per-trade sizing and the
    # concentration cap: it stops many "small" positions from stacking into an
    # oversized, correlated book. On spot the free-cash-per-order check already
    # bounds you at ~100% invested, so this mainly bites with margin; it's opt-in
    # (0 disables) and the concentration cap above is the primary beginner
    # protection. Measured against total equity so the cap means the same thing
    # however much cash is already deployed.
    max_total_exposure_pct: float = Field(default=0.0)
    # Trailing stop (percent). 0 disables. When > 0, an open long's stop-loss is
    # ratcheted up as price makes new highs, locking in gains while letting
    # winners run. Never loosened.
    trailing_stop_pct: float = Field(default=0.0)

    # ---- PROFIT-LOCK (breakeven+ and early profit-take) --------------
    # Bank a real gain instead of giving it back. When enabled, once an open long
    # is in profit by more than the round-trip fee buffer, its stop is ratcheted up
    # to entry + profit_lock_floor_pct so the trade can no longer turn into a loss
    # ("breakeven-plus"). This is a ONE-WAY ratchet layered on top of the ATR/fixed
    # stop and the trailing stop — whichever protects the most is used, never less.
    # It NEVER fabricates a number: the lock level is entry price × (1 + floor%).
    profit_lock_enabled: bool = Field(default=False)
    # Arm the lock once unrealized gain reaches this % of entry. Must be comfortably
    # above a round trip's fees so locking secures a REAL net gain, not a fee loss.
    profit_lock_trigger_pct: float = Field(default=1.0)
    # The locked-in floor: stop is raised to entry × (1 + this %). Kept just above
    # a round-trip taker fee (~0.2%) so a triggered lock is net-positive after fees.
    profit_lock_floor_pct: float = Field(default=0.3)
    # Early profit-take on a reversal: when in profit beyond the fee buffer AND the
    # brain/saved strategy turns bearish (a "red flag"), close and bank the gain
    # rather than waiting for the full take-profit. Off by default; the trailing
    # stop and confident-sell exit still work without it.
    take_profit_on_reversal: bool = Field(default=False)
    # Anti-whipsaw: how many consecutive bearish reads confirm a "red flag" before
    # the early exit fires. 1 = act on the first bearish tick (jumpy); 2 (default)
    # waits for the reversal to persist so a single noisy tick can't bump you out of
    # a still-good trade. Only matters when take_profit_on_reversal is on.
    reversal_confirm_count: int = Field(default=2)

    # Account-level max-drawdown KILL-SWITCH (% below the peak total equity seen
    # while running). If equity falls this far from its peak, the engine HALTS:
    # autonomous trading stops and ALL new entries are blocked (open positions
    # keep their stops) until the operator restarts the bot. This is the
    # catastrophe backstop sitting ABOVE the daily-loss breaker, so one very bad
    # run can never quietly drain the account. 0 disables.
    max_drawdown_pct: float = Field(default=25.0)
    # Pre-trade liquidity guard for LIVE market ENTRIES: if the current bid/ask
    # spread is wider than this % of mid price, skip the entry (a wide spread
    # means a thin/volatile book and a bad fill). Applies to opening market
    # orders only — an exit is NEVER blocked. 0 disables.
    max_spread_pct: float = Field(default=1.0)
    # Anti-whipsaw: after a LOSING autonomous exit on a symbol, wait this many
    # minutes before the bot may re-enter that same symbol. Stops the bot from
    # repeatedly buying back into a chop and bleeding fees + losses. Only affects
    # autonomous re-entries; a manual trade is never cooldown-blocked. 0 disables.
    reentry_cooldown_minutes: float = Field(default=15.0)
    # Consecutive-loss circuit breaker: after this many losing CLOSED trades in a
    # row, the bot stops opening NEW autonomous positions until a win breaks the
    # streak (manual trades still work). Caps damage from a losing regime. 0
    # disables.
    max_consecutive_losses: int = Field(default=3)
    # ATR-based stop FLOOR for autonomous entries. The auto stop-loss is placed at
    # least atr_stop_mult × ATR away from entry, so a fixed default_stop_loss_pct
    # can't sit inside normal market noise and get knocked out immediately. Sizing
    # uses the actual (wider) stop, so the position shrinks to keep risk constant.
    # 0 disables (use the fixed % stop only).
    atr_stop_mult: float = Field(default=1.5)

    # Autonomous trading: only act on analysis at/above this confidence (0..1).
    min_signal_confidence: float = Field(default=0.5)
    # When true, the bot analyses `auto_symbols` on each monitor tick and trades
    # confident signals itself (paper or live per trading_mode). Default off.
    auto_trade_enabled: bool = Field(default=False)
    auto_symbols: str = Field(default="BTC/USDT")
    auto_timeframe: str = Field(default="1h")
    # Multi-timeframe confirmation for autonomous trading. When set to a higher
    # timeframe (e.g. "4h"), a buy is only taken if that higher timeframe does
    # NOT read as a sell, and a confident-sell exit is only taken if the higher
    # timeframe is not a buy. Empty = single-timeframe (disabled).
    auto_confirm_timeframe: str = Field(default="")

    # Confirm-before-LIVE gate for autonomous entries (beginner-safe, ON by
    # default). When true, an entry the BOT itself decided to open (source
    # "auto") on a REAL-money account is not placed straight away: it is queued
    # as a pending AutoConfirmation and the operator is pinged (WebSocket +
    # Telegram) to approve or reject it. This is the "it will confirm when given
    # permission" behaviour — the bot watches the market autonomously but asks
    # before spending real money. Turn OFF to let it "trade all night alone"
    # (full autonomous live execution). Never applies to paper (simulated, $0
    # risk), to exits/closes (a protective exit must never wait on a human), or
    # to deliberate MANUAL orders. Approvals are freshness-checked: a queued
    # entry expires once the market has moved on, so a stale "yes" can't fire
    # into a changed book — the bot simply re-proposes if the setup still holds.
    auto_live_confirm: bool = Field(default=True)
    # How long a queued live-entry confirmation stays valid before it is treated
    # as stale and expired (the market has moved). Clamped sane; used by the
    # monitor loop and the approve endpoint's freshness check.
    auto_confirm_ttl_minutes: float = Field(default=10.0)

    # Stand aside in a bad market, step back in when it recovers. When true
    # (default) an autonomous/saved-strategy BUY is paused while price is in a
    # bear regime (under a falling long-term trend); entries resume automatically
    # once the regime turns neutral/bull. This is the visible "stop when bad,
    # trade when good" behaviour. Protective volatility/shock stand-asides are a
    # separate hard safety and always apply regardless of this flag.
    auto_pause_in_bear: bool = Field(default=True)

    # Trade with a SAVED, trained strategy instead of the built-in analyzer. When
    # true, for any symbol that has a trained strategy saved to the account the
    # autonomous/observe path uses that strategy's signal as the verdict. Off by
    # default (paper-test a strategy before letting it drive real orders); the
    # capital-preservation gates and risk manager still apply.
    use_saved_strategy: bool = Field(default=False)

    # ---- Saved-strategy VALIDATION GATE ------------------------------
    # A saved strategy may only DRIVE autonomous BUYS once it has proven itself
    # on a real backtest. This is the honest answer to "make the strategy correct
    # and profitable": there is no magic 95%-accuracy number, but we CAN refuse to
    # trade a strategy that hasn't demonstrated a positive, repeatable edge on
    # out-of-sample data. When on (default) and use_saved_strategy is enabled, a
    # strategy whose saved metrics fail the thresholds below is NOT trusted for new
    # entries — the deterministic analyzer decides instead. A strategy SELL/exit is
    # never gated (reducing risk is always allowed). Purely a safety gate over REAL
    # measured metrics; it never invents a win-rate.
    require_strategy_validation: bool = Field(default=True)
    # Minimum OUT-OF-SAMPLE return (%) the saved strategy must have scored on its
    # validation split. Default 0 => it must be net profitable out of sample.
    strategy_min_return_pct: float = Field(default=0.0)
    # Minimum win rate (%) on the backtest. 0 disables (win rate alone is a weak,
    # easily-gamed metric, so it is OFF by default; return + trade count matter more).
    strategy_min_win_rate_pct: float = Field(default=0.0)
    # Minimum number of closed trades in the backtest, so a lucky 1-2 trade fluke
    # can't pass as "validated".
    strategy_min_trades: int = Field(default=5)
    # Maximum backtest drawdown (%) allowed. 0 disables the drawdown ceiling.
    strategy_max_drawdown_pct: float = Field(default=0.0)

    # AI trade review (permission gate). When true AND an AI key is configured
    # AND autonomous trading is on, the AI layer reviews each ENTRY the
    # deterministic brain proposes and may VETO it (risk-first). It can only
    # BLOCK new risk — never invent a trade, never block an exit — and if the AI
    # is unavailable it falls back to the deterministic decision (never
    # fabricates a veto/approval). Off by default; paper-test before enabling live.
    ai_trade_confirm: bool = Field(default=False)

    # AI PRE-TRADE ANALYSIS (explanatory, opt-in). When true AND an AI key is
    # configured, before each autonomous ENTRY the AI writes a short, grounded
    # rationale for the setup (from the real factors + risk rules) so you can see
    # WHY the bot is buying. It is explanatory only: the deterministic brain still
    # decides and the risk gates still bind — the AI here never authors or forces a
    # trade (vetoing is the separate ai_trade_confirm gate). Fails safe: if the AI
    # is unavailable the entry proceeds on the deterministic decision with no note.
    ai_pretrade_analysis: bool = Field(default=False)

    # Live "loud monitor" (opt-in, OFF by default). When true AND an AI key is
    # configured, the background monitor watches this user's OPEN positions and
    # day P&L and, when something MATERIAL happens (a trade turns red, price nears
    # a stop or take-profit, the daily-loss limit is approached), pushes ONE short
    # spoken-style line to the app in real time — "live and loud", like a partner
    # calling it out. It never trades and never invents a number; on any failure
    # it stays silent. Off by default; the user turns it on when they want the
    # running commentary.
    ai_monitor_enabled: bool = Field(default=False)

    # ICT / smart-money analysis (informational; ON by default). When true, every
    # deterministic analysis ALSO computes a real ICT read on the SAME closed bars
    # — market structure (BOS/CHoCH/MSS), liquidity sweeps, order blocks, FVGs,
    # breaker/rejection blocks, BPR, volume imbalances, EQH/EQL liquidity pools,
    # draw-on-liquidity, premium/discount dealing range + OTE, and PDH/PDL/PWH/PWL.
    # It is surfaced to the API (`/api/analyze` -> `ict`), drawn on the chart, and
    # handed to the AI so it reads ICT on the REAL data instead of refusing. Turn
    # it off to hide the ICT layer everywhere (and, since confluence needs the read,
    # to switch ICT voting off with it).
    ict_enabled: bool = Field(default=True)

    # ICT CONFLUENCE: let the smart-money read actually VOTE in the deterministic
    # brain instead of only being drawn/narrated. When on (and ict_enabled), the
    # analyzer adds weighted factors for market structure (BOS/CHoCH/MSS), premium/
    # discount + OTE, and a fresh liquidity sweep — they vote ALONGSIDE the classic
    # indicators (regime/trend/RSI/MACD), never overriding the capital-preservation
    # vetoes and never inventing a level (every factor is a real computed ICT event
    # on CLOSED bars). Because the higher-timeframe confirm re-runs the same brain,
    # this gives an honest HTF->LTF confluence with no extra config. Off = ICT is a
    # pure lens again (drawn/narrated only). Requires ict_enabled.
    ict_confluence: bool = Field(default=True)

    # ---- FUNDAMENTALS / MACRO / SENTIMENT FEED (real external data) --------
    # When on, the AI assistant and the Fundamentals panel pull LIVE public data:
    # crypto Fear & Greed, global market cap + BTC/ETH dominance, per-coin
    # mcap/volume/supply/ATH/returns, and derivatives funding/OI/long-short -- so
    # the assistant can genuinely ANALYSE fundamentals instead of disclaiming that
    # it only has technicals. No API key, no user data leaves the box; a dead
    # source is reported honestly and left null, never fabricated. Off = the AI
    # sees technical/structural context only.
    fundamentals_enabled: bool = Field(default=True)

    # ---- CAPITAL / MONEY MANAGER (autonomous sizing discipline) ----------
    # A beginner-safe money manager that sits ON TOP of the risk manager and
    # governs how the AUTOPILOT deploys capital. It can only ever deploy the SAME
    # or LESS than the risk manager's risk-based size (never more), so turning it
    # on is always at least as safe as leaving it off. See app/money_manager.py.
    # Master switch. ON by default: with no run budget set (below = 0) it behaves
    # like the plain risk-based sizer, only trimming size after a losing streak —
    # strictly safer. Set a run budget to unlock partial-deploy / reserve.
    capital_manager_enabled: bool = Field(default=True)
    # Total quote (e.g. USDT) the autopilot may put to work THIS run. 0 = no
    # explicit budget: deploy from free cash as usual (risk-capped). Set e.g. 20
    # to say "trade with $20 tonight" — the bot then feeds it in a slice at a time
    # and always holds the rest in reserve.
    capital_run_budget_quote: float = Field(default=0.0)
    # Fraction of what's STILL FREE this run to commit to a single trade (partial
    # deploy). 25% => first trade uses a quarter, holding three-quarters back; the
    # next uses a quarter of what's left, and so on — capital is fed in, never
    # dumped. Bounded above by the risk-based size, so it only ever reserves more.
    capital_per_trade_pct: float = Field(default=25.0)
    # Don't place an autopilot trade smaller than this in quote terms (avoids dust
    # orders the exchange would reject). If less than this remains free, the bot
    # HOLDS rather than open a sub-minimum position.
    capital_min_trade_quote: float = Field(default=5.0)
    # Grow the per-trade slice a little after a winning streak and cut it (faster)
    # after losses, within hard [0.5x, 1.5x] bounds. Discipline: a cold run trades
    # smaller automatically. Uses REAL closed-trade P&L only.
    capital_resize_on_outcome: bool = Field(default=True)
    # Per-trade max hold (minutes) for AUTOPILOT trades: a position still open
    # after this long is closed at market ("time-stop"), freeing capital rather
    # than letting a trade drift for hours. 0 = off (no time-based exit). Applies
    # only to source="auto" trades; manual trades are never time-stopped.
    capital_max_hold_minutes: float = Field(default=0.0)
    # Hold this % of the day's realised profit OUT of the redeployable budget, so
    # booked gains aren't immediately re-risked. 0 = off. (The bot can't move money
    # off the exchange — trade-only keys — so it reserves profit and reminds you to
    # withdraw it yourself.)
    capital_profit_reserve_pct: float = Field(default=0.0)
    # EXACT stake per autopilot trade in quote terms (e.g. 10 = "trade with $10 each
    # time"). 0 = off -> size by the percentage/risk sizer above. When set >0 the
    # autopilot deploys this much per entry instead of a % of free cash, skipping the
    # percentage AND the win/loss resize (a fixed stake stays fixed). It is still
    # capped by free budget and by every RiskManager limit, so it can only ever
    # deploy this much OR LESS — never more. If free budget can't cover it, the bot
    # deploys what's free (down to the dust floor) or holds; it never fabricates cash.
    capital_fixed_trade_quote: float = Field(default=0.0)

    # Background MONITOR cadence (seconds between ticks): how often the engine
    # re-checks open positions (stops/targets/profit-lock), evaluates alerts, and
    # runs autonomous analysis. Lower = more responsive but more exchange calls;
    # a floor is enforced at read time so it can't be set low enough to hit rate
    # limits. Clamped to [3, 60] by the loop. Default 5s.
    monitor_interval_seconds: float = Field(default=5.0)

    # Assistant AUTOPILOT (opt-in, OFF by default). When true, the safe subset of
    # actions the assistant proposes in chat is APPLIED automatically the moment it
    # proposes them — settings within the allowlist, bot start/stop, price alerts,
    # and PAPER orders — instead of waiting for a manual Confirm tap. This is what
    # lets a hands-off user say "set me up safely and start" and have it actually
    # happen. LIVE (real-money) orders and switching paper<->live are NEVER
    # autopiloted: those always require an explicit human confirmation, no matter
    # this flag. Off by default; the operator turns it on when they want the
    # assistant to act for them.
    ai_autopilot_enabled: bool = Field(default=False)

    # Live market-news sources for the AI assistant + News panel. Comma-separated
    # public RSS/Atom feed URLs (crypto/markets). Real headlines only — if a feed
    # is unreachable it's reported as unavailable, never faked. No user data is
    # sent to fetch these (plain GETs to public feeds).
    news_feeds: str = Field(
        default=(
            "https://www.coindesk.com/arc/outboundfeeds/rss/,"
            "https://cointelegraph.com/rss"
        )
    )

    # Paper trading
    paper_starting_balance: float = Field(default=10_000.0)
    # Simulated taker fee (percent per fill) applied to PAPER trades so the
    # simulated wallet reflects the REAL cost of trading — a round trip pays this
    # on entry AND exit (Binance spot taker is ~0.1%). This is honest simulation,
    # not a fabricated live figure: LIVE P&L is never adjusted by this number
    # (the exchange charges its own real fees on the user's account). 0 = model
    # no fee (the historical default), so existing paper track records are
    # unchanged unless the operator opts in.
    paper_taker_fee_pct: float = Field(default=0.0)

    # Server
    cors_origins: str = Field(default="http://localhost:5173")
    database_url: str = Field(default="sqlite:///./tranding_track.db")

    # Logging: "text" (human) or "json" (structured, one JSON object per line —
    # good for shipping to a log aggregator). Level is the root log level.
    log_format: str = Field(default="text")
    log_level: str = Field(default="INFO")

    # API auth is per-user and JWT-based: every mutating/control endpoint depends
    # on get_current_user / require_licensed_user / require_admin (a signed HS256
    # access token), and each webhook authenticates by its per-user URL token.
    # There is deliberately no global X-API-Key knob — a single shared key would
    # add nothing over the per-user tokens and, left as a dead default, would
    # falsely imply the API is locked down when it is the JWT layer doing that.

    # ---- Multi-user auth & licensing --------------------------------
    # Master secret used to (a) sign JWT access tokens and (b) derive the
    # Fernet key that encrypts each user's Binance/AI API keys at rest. MUST be
    # set in any real deployment. If empty, login still works but per-user key
    # storage is disabled (fail-safe) because we refuse to store secrets we
    # cannot encrypt.
    secret_key: str = Field(default="")
    # Access-token lifetime (minutes).
    access_token_ttl_minutes: int = Field(default=60 * 24 * 7)
    # The email that is auto-promoted to admin on signup/startup. The admin
    # grants licenses to other users. If empty, the very first registered user
    # becomes the admin.
    admin_email: str = Field(default="")
    # When true, new signups start with an ACTIVE license (open access). When
    # false (default), new users are PENDING until the admin grants a license —
    # this is the "users need a licence from me" gate.
    auto_license_new_users: bool = Field(default=False)

    # Telegram notifications (optional). Set both to receive trade/alert pings.
    telegram_bot_token: str = Field(default="")
    telegram_chat_id: str = Field(default="")

    # Abuse protection: in-process rate limiting on auth + webhook endpoints.
    # Enabled by default; set RATE_LIMIT_ENABLED=false only for tests/local dev.
    rate_limit_enabled: bool = Field(default=True)

    @field_validator(
        "ai_api_key", "ai_base_url", "ai_model", "ai_api_style",
        "ai_fallback_api_key", "ai_fallback_base_url", "ai_fallback_model",
        "ai_fallback_api_style", mode="before"
    )
    @classmethod
    def _clean_ai_env(cls, v: object) -> object:
        """Strip whitespace and one layer of matching surrounding quotes.

        On hosts like Railway these AI_* values are real OS environment variables,
        so quotes or stray spaces pasted around a value are sent to the provider
        VERBATIM — a key wrapped in quotes is rejected as invalid (HTTP 401) even
        though the key itself is correct. Cleaning here fixes the most common
        "I pasted my real key but it says the key is wrong" case, without ever
        logging or altering the actual credential characters.
        """
        if isinstance(v, str):
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1].strip()
            # Railway paste error: the whole "NAME=value" line pasted into the
            # value box (e.g. AI_BASE_URL=https://…), so the value carries its own
            # variable name. Drop a leading ENV-NAME= echo so we don't send it to
            # the provider (which turns a URL into an unparseable protocol error).
            head = v.split("=", 1)[0] if "=" in v else ""
            if (
                head
                and head == head.upper()
                and not head[0].isdigit()
                and all(c.isalnum() or c == "_" for c in head)
            ):
                v = v.split("=", 1)[1].strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1].strip()
        return v

    @model_validator(mode="after")
    def _validate_risk_bounds(self) -> "Settings":
        """Reject a misconfigured risk input at load instead of trading on it.

        A money bot must never START with a degenerate risk setting. We fail
        fast with a clear message so a fat-fingered env var (DEFAULT_STOP_LOSS_PCT=0,
        a negative RISK_PER_TRADE_PCT) is caught at boot, not discovered live —
        a 0% stop places the stop AT entry and knocks the trade out on the first
        adverse tick; a negative risk % inverts sizing. Fields where 0 means
        "disabled" (trailing stop, caps, cooldowns, ATR floor) only reject negatives.
        """
        # Strictly positive: zero or below is never a valid trading input.
        positive = (
            "risk_per_trade_pct", "default_stop_loss_pct", "default_take_profit_pct",
            "daily_loss_limit_pct", "paper_starting_balance", "max_open_positions",
            "ai_timeout_seconds", "ai_connect_timeout_seconds", "ai_max_tokens",
            "access_token_ttl_minutes", "monitor_interval_seconds",
            "reversal_confirm_count", "auto_confirm_ttl_minutes",
        )
        for name in positive:
            val = getattr(self, name)
            if val is None or val <= 0:
                raise ValueError(
                    f"{name} must be greater than 0 (got {val!r}); it controls "
                    "position sizing or safety timing and cannot be zero/negative."
                )
        # 0 legitimately means "disabled" here; only a negative value is invalid.
        non_negative = (
            "max_position_pct", "max_total_exposure_pct", "trailing_stop_pct",
            "max_drawdown_pct", "max_spread_pct", "reentry_cooldown_minutes",
            "max_consecutive_losses", "atr_stop_mult", "profit_lock_trigger_pct",
            "profit_lock_floor_pct", "paper_taker_fee_pct", "strategy_min_win_rate_pct",
            "strategy_min_trades", "strategy_max_drawdown_pct", "webhook_max_age_seconds",
        )
        for name in non_negative:
            val = getattr(self, name)
            if val is not None and val < 0:
                raise ValueError(
                    f"{name} must be 0 or greater (got {val!r}); use 0 to disable it."
                )
        if not 0.0 <= self.min_signal_confidence <= 1.0:
            raise ValueError(
                "min_signal_confidence must be between 0 and 1 (got "
                f"{self.min_signal_confidence!r})."
            )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def auto_symbol_list(self) -> list[str]:
        return [s.strip().upper() for s in self.auto_symbols.split(",") if s.strip()]

    @property
    def news_feed_list(self) -> list[str]:
        return [u.strip() for u in self.news_feeds.split(",") if u.strip()]

    @property
    def is_live(self) -> bool:
        return self.trading_mode.lower() == "live"


@lru_cache
def get_settings() -> Settings:
    return Settings()
