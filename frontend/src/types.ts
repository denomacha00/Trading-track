// Shared types mirroring the backend schemas.

import type { IndicatorPrefs } from './indicators'
import type { IctOverlayPrefs } from './ictOverlays'

// Per-symbol market-regime snapshot the bot publishes so the UI can SHOW it
// standing aside in a bad market and re-engaging in a good one (the visible
// pause/resume). Pure observation of the analyzer's own regime factor — nothing
// fabricated. Empty until the monitor has analysed that symbol at least once.
export interface RegimeSnapshot {
  regime: 'bull' | 'bear' | 'neutral'
  detail: string
  protective_hold: boolean
  entries_paused: boolean
  verdict: 'buy' | 'sell' | 'hold'
  at: string
}

export interface BotStatus {
  running: boolean
  trading_mode: string
  testnet: boolean
  exchange_connected: boolean
  open_positions: number
  balance: number
  // NULL when the price feed can't value an open position right now (the UI shows
  // "-" rather than a fabricated break-even). Free cash (`balance`) stays honest.
  equity: number | null
  realized_pnl: number
  unrealized_pnl: number | null
  day_pnl: number
  max_open_positions: number
  // True when ≥1 open position can't be priced, so equity/unrealized read stale.
  prices_stale?: boolean
  // Risk-safeguard + autopilot visibility. All optional so older payloads
  // (and tests that assert only the core keys) keep parsing.
  killswitch?: boolean
  max_drawdown_pct?: number
  peak_equity?: number
  auto_trade_enabled?: boolean
  consecutive_losses?: number
  max_consecutive_losses?: number
  // True when NEW entries are halted (kill-switch, consecutive-loss breaker, or
  // the bot is stopped); `entries_pause_reason` is the plain-language why.
  entries_paused?: boolean
  entries_pause_reason?: string | null
  // Confirm-before-live gate + how many autonomous LIVE entries are waiting for
  // the operator's yes/no right now. Optional so older payloads still parse.
  auto_live_confirm?: boolean
  pending_confirmations?: number
  // Per-symbol regime snapshots, keyed by SYMBOL. Empty until analysed.
  regimes?: Record<string, RegimeSnapshot>
}

// A LIVE entry the bot proposed on its own and is holding for the operator's
// yes/no (mirrors the backend AutoConfirmationOut). `amount`/`ref_price` are the
// snapshot AT PROPOSAL TIME — shown for context; the real fill is re-priced and
// re-sized when approved. Nothing here is fabricated.
export interface AutoConfirmation {
  id: number
  symbol: string
  side: string
  amount: number
  ref_price: number
  stop_loss: number | null
  take_profit: number | null
  confidence: number | null
  timeframe: string | null
  note: string | null
  status: string
  created_at: string | null
  expires_at: string | null
}

// Outcome of approving/rejecting a queued live entry.
export interface ConfirmationResolveResult {
  ok: boolean
  message: string
  trade_id: number | null
}

export interface Trade {
  id: number
  symbol: string
  side: string
  amount: number
  entry_price: number
  exit_price: number | null
  stop_loss: number | null
  take_profit: number | null
  status: string
  order_type?: string
  limit_price?: number | null
  pnl: number
  // Real exchange fee booked against this trade (live only; 0 in paper). Never
  // fabricated — absent/older rows may omit it.
  fee?: number
  mode: string
  source: string
  note: string | null
  opened_at: string
  closed_at: string | null
}

export interface SignalRow {
  id: number
  source: string
  symbol: string | null
  action: string | null
  accepted: number
  message: string | null
  created_at: string
  // Real model confidence (0..1) for analyzer rows; null when the source
  // (e.g. a raw TradingView alert) didn't carry one. Never fabricated.
  confidence: number | null
}

// One real, live headline from a configured public trading/market RSS feed.
// Fetched server-side; never fabricated (an empty list means the sources were
// unreachable, not that nothing is happening).
export interface NewsItem {
  title: string
  link: string
  source: string
  published: string
}

// A single turn in the AI assistant conversation (browser-local history).
export interface ChatTurn {
  role: 'you' | 'ai'
  text: string
  // Whether live news was attached to this question (shown as a small note).
  usedNews?: boolean
}

// An action the assistant PROPOSES the user take. The AI never executes anything:
// the backend validates its suggestion against a strict allowlist and returns one
// of these, the UI shows a Confirm/Cancel card, and only on Confirm does the app
// call the normal authenticated endpoint. `reason` is the AI's one-line rationale.
//
// `auto` is set by the SERVER (never the model): true only when the operator has
// turned autopilot on AND the action is in the safe subset (settings, bot,
// alert, or a PAPER order). When true the UI applies it immediately via the same
// authenticated endpoint a manual Confirm would call — so the outcome line is
// always the real one, never the AI claiming success. LIVE orders and paper<->live
// switches are never auto (auto=false) and always need a manual Confirm tap.
export type ProposedAction =
  | {
      type: 'order'
      side: 'buy' | 'sell' | 'close'
      symbol: string
      // null => let the risk manager size it (the safe default). A number is base units.
      amount: number | null
      limit_price?: number
      stop_loss?: number
      take_profit?: number
      reason?: string | null
      auto?: boolean
    }
  | { type: 'settings'; changes: Partial<Settings>; reason?: string | null; auto?: boolean }
  | { type: 'bot'; state: 'start' | 'stop'; reason?: string | null; auto?: boolean }
  | {
      type: 'train'
      symbol: string
      strategy: string
      timeframe: string
      reason?: string | null
      auto?: boolean
    }
  | {
      type: 'alert'
      symbol: string
      condition: 'above' | 'below'
      price: number
      note?: string | null
      reason?: string | null
      auto?: boolean
    }
  // VIEW-ONLY: change what's on the chart (moves no money, touches no account
  // state). Every field is optional — only what changes is sent. `undo:true` is a
  // standalone request that steps the view back one change via the app's own
  // history stack. Applied by the app locally, never by a network endpoint.
  | {
      type: 'chart'
      symbol?: string
      timeframe?: string
      indicators?: Partial<IndicatorPrefs>
      // Which ICT / smart-money overlays to show (true) or hide (false). Same
      // view-only nature as `indicators`: it only changes what's drawn, never
      // money or account state. Only the keys that change are sent.
      ict?: Partial<IctOverlayPrefs>
      clear_drawings?: boolean
      undo?: boolean
      reason?: string | null
      auto?: boolean
    }

export interface Settings {
  trading_mode: string
  binance_testnet: boolean
  max_open_positions: number
  risk_per_trade_pct: number
  daily_loss_limit_pct: number
  default_stop_loss_pct: number
  default_take_profit_pct: number
  trailing_stop_pct: number
  max_total_exposure_pct: number
  // Concentration cap: no single position may exceed this % of TOTAL equity.
  // Clamps the auto-sizer (the main beginner blow-up guard); 0 = off.
  max_position_pct: number
  // Paper-only modeled taker fee charged on BOTH legs of a simulated round trip
  // so paper P&L reflects the real cost of trading. 0 = fee-free (default).
  // LIVE P&L is never adjusted by this — real fills already include real fees.
  paper_taker_fee_pct: number
  min_signal_confidence: number
  auto_trade_enabled: boolean
  auto_symbols: string
  auto_timeframe: string
  auto_confirm_timeframe: string
  // Confirm-before-LIVE: when on, an autonomous (bot-decided) BUY on a REAL-money
  // account is QUEUED and you're pinged to approve it — it is NOT placed until you
  // say yes ("it can trade real market but it will confirm when given permission").
  // Never gates paper, exits/closes, or your own manual orders. Turn OFF to let it
  // trade alone. On by default. `auto_confirm_ttl_minutes` is how long a queued
  // proposal stays approvable before it expires (so a late yes can't fire into a
  // moved market).
  auto_live_confirm: boolean
  auto_confirm_ttl_minutes: number
  // When on, the bot trades a symbol with the strategy you trained and SAVED for
  // it (instead of the built-in analyzer brain). Capital-preservation gates still
  // override its BUY in a bear regime; its SELL/exit is always honoured.
  use_saved_strategy: boolean
  // When on AND autonomous trading is on, the AI reviews each deterministic
  // entry and may VETO it (it can never invent or force a trade). Off by default.
  ai_trade_confirm: boolean
  // When on, a background monitor watches your OPEN positions + day P&L on REAL
  // live prices and speaks up (in the assistant) about the single most material
  // risk — a stop about to hit, a position deep red, nearing your loss limit.
  // Opt-in and OFF by default; it never trades, only calls things out.
  ai_monitor_enabled: boolean
  // When on, the SAFE subset of assistant actions (settings, bot start/stop,
  // price alerts, and PAPER orders) is APPLIED automatically the moment the AI
  // proposes it — no manual Confirm tap. LIVE (real-money) orders and switching
  // paper<->live are NEVER autopiloted; those always need an explicit confirm.
  // Opt-in and OFF by default.
  ai_autopilot_enabled: boolean
  // When on AND autonomous trading is on, the AI writes a short, grounded
  // rationale BEFORE each autonomous entry (explaining the analyzer's own
  // decision to a non-expert). It can never invent a number or override a risk
  // gate — a failure just falls back to the plain summary. Off by default.
  ai_pretrade_analysis: boolean
  ai_enabled: boolean
  ai_model?: string
  ai_style?: string
  // ICT / smart-money read. When on, every Analyze also computes a REAL ICT read
  // (market structure, liquidity, order blocks, FVGs, premium/discount) on the
  // same closed bars, and the chart can draw it. It is an analytical LENS only —
  // it never sizes, places, or vetoes a trade. On by default.
  ict_enabled: boolean
  // When on (and ict_enabled), the ICT read also VOTES in the deterministic
  // brain as weighted confluence (structure / premium-discount+OTE / fresh
  // sweep) alongside the classic signals — never overriding a risk veto, never
  // inventing a level, computed on closed bars so it can't repaint. Off = ICT is
  // a pure lens (drawn / narrated only). On by default.
  ict_confluence: boolean
  // ---- Capital / money manager (disciplined autopilot sizing) ----
  // When on, autonomous BUYS are sized by the money manager (on TOP of the risk
  // manager, never above it): a per-run budget is deployed a slice at a time with
  // the rest held in reserve, the slice re-sizes on recent outcomes, a cut of the
  // day's profit is held back, and an optional max-hold time-stop closes an aged
  // auto trade. It can only ever deploy the SAME or LESS than the risk-based size,
  // so turning it on is always at least as safe as off. On by default; with the
  // budget at 0 it just tracks the risk-based size (trimming after a losing run).
  capital_manager_enabled: boolean
  // Max quote (e.g. USDT) the autopilot may put to work this run. 0 = no fixed
  // budget: it sizes from free equity instead (still risk-capped).
  capital_run_budget_quote: number
  // Slice of the still-free budget to deploy on ONE entry (holds the rest back).
  capital_per_trade_pct: number
  // Never place an auto entry smaller than this (quote); below it the bot HOLDS
  // rather than send a dust order.
  capital_min_trade_quote: number
  // Grow the slice a little after a winning run, cut it (faster) after losses.
  capital_resize_on_outcome: boolean
  // Close an AUTO position once it has been open this many minutes (the "trade for
  // 4 min or 1 hr" time-box). 0 = off (positions run to stop/target only). Never
  // touches your manual trades. Protective SL/TP still win a tie.
  capital_max_hold_minutes: number
  // Hold this % of the day's REALISED profit OUT of the redeployable budget so
  // booked gains aren't immediately re-risked. 0 = off. (The bot can't move money
  // off your exchange — withdrawals are always yours to make there.)
  capital_profit_reserve_pct: number
  // ---- Autopilot safety / pause-resume (safe defaults for non-traders) ----
  // Stand aside for NEW longs while price is in a bear regime; resume in a bull.
  // On by default — capital preservation is the safe stance.
  auto_pause_in_bear: boolean
  // Require a saved strategy to pass a REAL out-of-sample backtest (the
  // thresholds below) before it may drive autonomous BUYS. On by default; a
  // SELL/exit is never gated. This is the honest stand-in for "95% correct":
  // we never fabricate accuracy, we refuse to auto-trade an unproven edge.
  require_strategy_validation: boolean
  strategy_min_return_pct: number
  strategy_min_win_rate_pct: number
  strategy_min_trades: number
  strategy_max_drawdown_pct: number
  // ---- Profit-lock / early profit-take (autopilot) ----
  // Ratchet a winning long's stop up into profit once it's up enough. The engine
  // AUTOMATICALLY clamps the effective trigger/floor to clear round-trip fees, so
  // a tiny value here can never lock in a fee-loss (you don't have to do the fee
  // math — keep trigger > floor so the locked stop sits below price).
  profit_lock_enabled: boolean
  profit_lock_trigger_pct: number
  profit_lock_floor_pct: number
  // Actively bank a NET-positive winner when the read turns bearish and STAYS
  // bearish for `reversal_confirm_count` reads (anti-whipsaw). OFF => the trade
  // runs to its stop/target ("it must finish"). Only ever sells a real winner.
  take_profit_on_reversal: boolean
  reversal_confirm_count: number
  // Background monitor cadence (seconds); server clamps to [3, 60]. This is a
  // GLOBAL setting (one shared monitor loop), not per-symbol.
  monitor_interval_seconds: number
  notifications_enabled: boolean
  api_key_set: boolean
  webhook_path: string
  webhook_secret_set: boolean
}

export interface ExecutionResult {
  accepted: boolean
  message: string
  trade: Trade | null
}

// Result of a scaled (DCA) entry: the legs that were actually placed. A market
// leg comes back `open`; resting limit legs come back `pending`. On a partial
// placement `accepted` is still true and `message` says which legs are live.
export interface ScaledResult {
  accepted: boolean
  message: string
  legs: Trade[]
}

// Result of closing every position + cancelling every resting order for one
// symbol (the one-click exit for a multi-leg DCA ladder).
export interface CloseAllResult {
  closed: number
  realized_pnl: number
  message: string
}

export interface ExchangeAccess {
  ok: boolean
  can_read_public: boolean
  can_read_account: boolean
  can_trade: boolean
  testnet: boolean
  // Which ccxt exchange this account talks to (e.g. "binance", "binanceus").
  exchange?: string
  detail: string
}

// Live reachability of the app-wide AI provider (never carries the key).
// Powers the assistant's connection dashboard so "AI not working" shows a
// concrete reason instead of failing silently.
export interface AiHealth {
  enabled: boolean
  ok: boolean
  model: string
  base_url: string
  style: string
  detail: string
}

export interface Candle {
  time: number
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface Ticker {
  symbol: string
  last: number
  bid: number | null
  ask: number | null
  // 24h price change percent (may be null if the exchange omits it).
  percentage: number | null
  // 24h traded volume: base_volume in the base asset (e.g. BTC), quote_volume in
  // the quote asset (e.g. USDT). null when the venue omits it — never faked.
  base_volume?: number | null
  quote_volume?: number | null
  // Which venue actually served this price: the primary exchange, or the
  // public-data fallback when the primary is geo-blocked. Honest source label
  // so the UI never implies a price came from somewhere it didn't.
  source?: string | null
}

// One price level of the live order book (a real resting order aggregate).
export interface OrderBookLevel {
  price: number
  amount: number
}

// A live order-book snapshot: the market's real resting liquidity. `bids` are
// the buy side (highest first), `asks` the sell side (lowest first). An empty
// side means the venue returned no depth — never an invented ladder. On the
// Binance testnet this is the sandbox's own thin book, not the live market.
export interface OrderBook {
  symbol: string
  bids: OrderBookLevel[]
  asks: OrderBookLevel[]
  source?: string | null
}

// Realized-P&L stats for a set of CLOSED trades. Every figure is derived from
// trades that actually executed; `profit_factor` is null (never a fake
// "infinity") when there are no losing trades. Mirrors the backend PerfBucket.
export interface PerfBucket {
  closed_trades: number
  wins: number
  losses: number
  breakeven: number
  win_rate_pct: number
  total_pnl: number
  gross_profit: number
  gross_loss: number
  profit_factor: number | null
  avg_win: number
  avg_loss: number
  expectancy: number
  largest_win: number
  largest_loss: number
  max_drawdown: number
}

export interface PerfSymbol {
  symbol: string
  trades: number
  pnl: number
  wins: number
}

// One realized-P&L event: a single closed trade's booked result at its close
// time. The cumulative equity line is built on the CLIENT per mode — paper and
// live are never summed (simulated and real money must not mix). `t` is the ISO
// close time, or null when the record has no close timestamp. Mirrors the
// backend EquityPoint.
export interface EquityPoint {
  t: string | null
  pnl: number
  symbol: string
  mode: string
}

// Overall realized performance plus paper/live splits and a per-symbol
// breakdown. Paper and live are separate so simulated gains are never counted
// as real money. Mirrors the backend PerformanceOut.
export interface Performance extends PerfBucket {
  avg_hold_seconds: number | null
  paper: PerfBucket
  live: PerfBucket
  by_symbol: PerfSymbol[]
  // Realized-P&L events in close order (one per closed trade). Empty until the
  // first position closes — never back-filled or invented.
  equity_curve: EquityPoint[]
}

// Real, explainable analytics for a backtest run — every figure derived from the
// run's own trades/equity curve, nothing invented. `profit_factor` is null when
// there were no losing trades (shown as "-", never a fake ratio) and
// `avg_hold_seconds`/`buy_hold_return_pct` are null when they can't be measured.
export interface BacktestAnalytics {
  wins: number
  losses: number
  breakeven: number
  gross_profit: number
  gross_loss: number
  profit_factor: number | null
  avg_win: number
  avg_loss: number
  avg_trade_pnl: number
  expectancy_pct: number
  avg_trade_return_pct: number
  largest_win: number
  largest_loss: number
  avg_bars_held: number
  avg_hold_seconds: number | null
  fees_pct_of_start: number
  buy_hold_return_pct: number | null
  vs_buy_hold_pct: number | null
  beat_buy_hold: boolean
  profitable: boolean
  // True when the history lacked separate open/high/low, so intrabar stop/target
  // fills were approximated on the close (exit timing is then only approximate).
  ohlc_synthetic?: boolean
  bars: number
  explanation: string
}

export interface BacktestResult {
  symbol: string
  strategy: string
  timeframe: string
  starting_balance: number
  ending_balance: number
  total_return_pct: number
  num_trades: number
  // null when the run took zero trades (no rate to report) — the UI shows "-".
  win_rate_pct: number | null
  max_drawdown_pct: number
  total_fees?: number
  // Exit rules the backtest applied (defaulted from live settings) so results
  // reflect how the bot actually trades, not idealised buy-and-hold.
  stop_loss_pct?: number
  take_profit_pct?: number
  trailing_stop_pct?: number
  // True when this run replayed the saved (trained) strategy for the symbol
  // rather than the raw picker selection — so the UI can label it honestly.
  used_saved?: boolean
  equity_curve: number[]
  // True when the history lacked separate open/high/low columns (intrabar exits
  // approximated on the close). Optional so an older backend still parses.
  ohlc_synthetic?: boolean
  // Deeper real analytics + a plain-language read of what happened. Optional so
  // an older backend without them still parses.
  analytics?: BacktestAnalytics
  explanation?: string
}

export interface StrategyInfo {
  name: string
  params: Record<string, (number | string)[]>
}

export interface TrainingCandidate {
  params: Record<string, number | string>
  total_return_pct: number
  win_rate_pct: number
  max_drawdown_pct: number
  num_trades: number
  score: number
  validation_return_pct?: number | null
  validation_num_trades?: number | null
  overfit_gap_pct?: number | null
}

export interface TrainingReport {
  symbol: string
  strategy: string
  timeframe: string
  candles: number
  tested: number
  best: TrainingCandidate | null
  leaderboard: TrainingCandidate[]
  train_fraction: number
  warning?: string | null
  // True when the winning config was persisted to this account (so the bot can
  // trade it). False when nothing beat the baseline or save was disabled.
  saved?: boolean
}

// A strategy the user trained and SAVED for a symbol — the persisted config the
// bot trades with when `use_saved_strategy` is on. `metrics` are the real,
// measured results from the training run that produced it (never fabricated);
// older saves may omit some fields. This is the answer to "where did the
// strategies I trained go" — they live on the account, keyed by symbol.
// The verdict of the validation gate for a saved strategy, computed from its
// REAL measured metrics (never fabricated). `will_auto_trade` is the bottom line
// the UI shows: whether this strategy's BUY will actually be trusted right now.
export interface SavedStrategyValidation {
  ok: boolean
  reason: string
  gate_enabled: boolean
  use_saved_strategy: boolean
  will_auto_trade: boolean
  // Present only when the strategy carries metrics (absent when it has none yet).
  return_pct?: number
  return_basis?: 'out-of-sample' | 'in-sample only'
  win_rate_pct?: number
  num_trades?: number
  max_drawdown_pct?: number
  overfit_gap_pct?: number | null
}

export interface SavedStrategy {
  symbol: string
  strategy: string
  timeframe?: string
  params: Record<string, number | string>
  metrics?: {
    total_return_pct?: number
    win_rate_pct?: number
    max_drawdown_pct?: number
    num_trades?: number
    score?: number
    validation_return_pct?: number | null
    overfit_gap_pct?: number | null
  }
  trained_at?: string
  // Whether this strategy has earned the right to drive autonomous BUYS.
  validation?: SavedStrategyValidation
}

export interface AnalysisFactor {
  name: string
  signal: 'buy' | 'sell' | 'hold'
  weight: number
  detail: string
}

export interface MarketAnalysis {
  symbol: string
  verdict: 'buy' | 'sell' | 'hold'
  confidence: number
  score: number
  price: number
  summary: string
  factors: AnalysisFactor[]
  narration?: string
  assessment?: string
  ai_enabled?: boolean
  // A REAL, computed ICT / smart-money read on the same closed bars (present when
  // ICT is enabled and there were enough bars). `null` when the engine declined
  // (thin data) or the read errored; `ict_enabled` tells the UI which state it's
  // in. Never fabricated — every level here is computed from real candles.
  ict?: IctAnalysis | null
  ict_enabled?: boolean
}

// ---- ICT / smart-money read (mirrors backend app/ict.py `as_dict()`) --------
// Everything here is COMPUTED from real closed candles — no fabricated levels.
// `time` is unix SECONDS (for the chart) or null when the frame carried no
// timestamps. Prices are absolute.

export interface IctSwing {
  index: number
  time: number | null
  price: number
  kind: 'high' | 'low'
}

export interface IctStructureEvent {
  index: number // bar whose CLOSE broke the level
  time: number | null
  kind: 'BOS' | 'CHoCH' // CHoCH + displacement => an MSS
  direction: 'bull' | 'bear'
  level: number // the swing price that was broken
  from_index: number
  displacement: boolean
}

export interface IctSweep {
  index: number
  time: number | null
  side: 'buy-side' | 'sell-side' // which liquidity pool was swept
  level: number // the swept swing level
  extreme: number // the wick extreme that ran the stops
  reaction: 'bull' | 'bear' // implied follow-through
}

export interface IctZone {
  kind: 'bullish' | 'bearish'
  top: number
  bottom: number
  index: number // origin bar (left edge)
  time: number | null
  mitigated: boolean // price has since traded back into it
  subtype: 'order-block' | 'breaker' | 'rejection' | 'fvg' | 'bpr' | 'volume-imbalance'
  inverted: boolean // (FVG) a close ran fully through it, so its role flipped
  void: boolean // (FVG) oversized gap = liquidity void / inefficiency
  ce: number // consequent encroachment = the zone's 50% (a key ICT level)
}

export interface IctLiquidityPool {
  kind: 'buy-side' | 'sell-side'
  price: number
  index: number
  time: number | null
  equal: boolean // part of an EQH/EQL cluster (a stronger pool)
  swept: boolean // price has since run through it
}

export interface IctDealingRange {
  high: number
  low: number
  equilibrium: number // the 50% line
  position_pct: number // 0 at the low, 1 at the high
  zone: 'premium' | 'discount' | 'equilibrium'
  ote_discount: [number, number] // long "optimal trade entry" band (price)
  ote_premium: [number, number] // short OTE band (price)
  in_ote: boolean // price is inside the side-appropriate OTE band
}

export interface IctKeyLevels {
  pdh?: number // previous day high
  pdl?: number // previous day low
  pwh?: number // previous week high
  pwl?: number // previous week low
}

export interface IctDrawOnLiquidity {
  above: IctLiquidityPool | null // nearest UNSWEPT buy-side pool above price
  below: IctLiquidityPool | null // nearest UNSWEPT sell-side pool below price
}

export interface IctAnalysis {
  symbol: string
  price: number
  trend: 'bull' | 'bear' | 'none'
  bias: 'bullish' | 'bearish' | 'neutral'
  summary: string
  swings: IctSwing[]
  events: IctStructureEvent[]
  sweeps: IctSweep[]
  order_blocks: IctZone[]
  fvgs: IctZone[]
  breakers: IctZone[]
  rejection_blocks: IctZone[]
  bpr: IctZone[]
  volume_imbalances: IctZone[]
  liquidity: IctLiquidityPool[]
  draw_on_liquidity: IctDrawOnLiquidity | null
  key_levels: IctKeyLevels | null
  dealing_range: IctDealingRange | null
}

// A user-defined price alert: "notify me when SYMBOL crosses PRICE". The
// background monitor checks each armed alert against the REAL live price and
// fires it once (status flips 'armed' -> 'triggered'), recording the real
// trigger time + price. Nothing here is fabricated — an alert fires only on a
// genuine crossing; if the live price can't be read it stays armed.
export interface Alert {
  id: number
  symbol: string
  condition: 'above' | 'below'
  price: number
  note: string | null
  status: 'armed' | 'triggered'
  created_at: string | null
  triggered_at: string | null
  triggered_price: number | null
}

export type WsMessage =
  | { event: 'status'; data: BotStatus; user_id?: number }
  | { event: 'trade_opened'; data: { id: number; symbol: string; side: string } }
  | { event: 'trade_closed'; data: { id: number; symbol: string; pnl: number } }
  | { event: 'order_pending'; data: { id: number; symbol: string; side: string; limit_price: number } }
  | { event: 'order_canceled'; data: { id: number; symbol: string } }
  | { event: 'stop_trailed'; data: { id: number; symbol: string; stop_loss: number } }
  // The autopilot ratcheted a winning long's stop up into profit (fee-aware): a
  // pullback now banks the gain instead of giving it back. `locked_pct` is the
  // profit floor above entry that the raised stop now guarantees.
  | { event: 'profit_locked'; data: { id: number; symbol: string; stop_loss: number; locked_pct: number } }
  // A grounded, plain-language rationale the AI wrote BEFORE an autonomous entry
  // (it explains the analyzer's own decision; it never invents a number or forces
  // the trade). `confidence` is the analyzer's real 0..1 score for that entry.
  | { event: 'pretrade_analysis'; data: { symbol: string; text: string; confidence: number } }
  | { event: 'reconcile_closed'; data: { symbol: string; db_amount: number; exchange_amount: number; pnl: number } }
  | { event: 'reconcile_adjusted'; data: { symbol: string; db_amount: number; exchange_amount: number } }
  // The bot decided a LIVE entry on its own and is holding it for your yes/no
  // (confirm-before-live gate). `data` carries the proposal snapshot so the UI can
  // show it immediately; the real fill is re-priced/re-sized on approval. Routed
  // per-user by `user_id`.
  | {
      event: 'auto_confirm_pending'
      user_id?: number
      data: {
        id: number
        symbol: string
        side: string
        amount: number
        ref_price: number
        stop_loss: number | null
        confidence: number | null
        timeframe: string | null
        note: string | null
        expires_at: string
      }
    }
  // A queued live entry was resolved (approved / rejected / expired), so the UI
  // should refresh its pending list. `ok`/`trade_id`/`message` are present on an
  // approval outcome. Routed per-user by `user_id`.
  | {
      event: 'auto_confirm_resolved'
      user_id?: number
      data: {
        id: number
        symbol: string
        status: 'approved' | 'rejected' | 'expired'
        ok?: boolean
        trade_id?: number | null
        message?: string
      }
    }
  | {
      event: 'signal'
      data: {
        id?: number
        source: string
        action: string
        symbol: string
        accepted: boolean
        message: string
        confidence?: number
        // Present on autonomous analyzer verdicts: the timeframe the decision was
        // made on, and the REAL factors that drove it (name, direction, weight) —
        // sent verbatim from the analyzer so the UI can show WHY it decided and
        // light up the matching chart indicators. Never fabricated; absent on raw
        // external (e.g. TradingView) alerts.
        timeframe?: string
        factors?: { name: string; signal: 'buy' | 'sell' | 'hold'; weight: number }[]
      }
    }
  // A proactive assistant call-out pushed from the server: a fired price alert
  // ('alert') or a live risk read on an open position ('monitor'). `text` is the
  // real, already-true deterministic line (a monitor line may be rephrased by the
  // LLM but its numbers are never changed). Routed per-user by `user_id`.
  | {
      event: 'assistant'
      user_id?: number
      data: {
        kind: 'alert' | 'monitor'
        event: string
        symbol: string | null
        text: string
        level: 'info' | 'warn'
      }
    }

export interface Me {
  id: number
  username: string | null
  email: string
  role: 'admin' | 'user'
  license_status: 'pending' | 'active' | 'revoked'
  // Effective access: licensed AND not past expiry. This — not license_status
  // alone — is what the UI should gate trading on.
  license_active: boolean
  license_expires_at: string | null
  license_days_left: number | null
  webhook_path: string
  binance_keys_set: boolean
  binance_testnet: boolean
  ai_key_set: boolean
  ai_model: string
  secrets_storage_enabled: boolean
}

export interface UserRow {
  id: number
  username: string | null
  email: string
  role: 'admin' | 'user'
  license_status: 'pending' | 'active' | 'revoked'
  license_active: boolean
  license_expires_at: string | null
  license_days_left: number | null
  created_at: string
  licensed_at: string | null
}

// A licence key row as the admin sees it. NEVER carries the plaintext key —
// that is shown only once, at creation, via LicenseKeyCreated.
export interface LicenseKeyRow {
  id: number
  key_prefix: string
  label: string | null
  // Days of access this key grants when redeemed; null = lifetime.
  duration_days: number | null
  status: 'unused' | 'redeemed' | 'revoked'
  created_at: string
  redeemed_by: number | null
  redeemed_at: string | null
}

// Returned exactly once when an admin generates a key: `key` is the full
// plaintext to copy now (never stored server-side, never returned again).
export interface LicenseKeyCreated extends LicenseKeyRow {
  key: string
}
