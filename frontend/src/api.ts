// Thin fetch wrapper around the Tranding-track REST API.
import type {
  BacktestResult,
  BotStatus,
  Candle,
  AiHealth,
  Alert,
  AutoConfirmation,
  CloseAllResult,
  ConfirmationResolveResult,
  ExchangeAccess,
  ExecutionResult,
  LicenseKeyCreated,
  LicenseKeyRow,
  MarketAnalysis,
  Me,
  NewsItem,
  OrderBook,
  Performance,
  ProposedAction,
  ScaledResult,
  SavedStrategy,
  Settings,
  SignalRow,
  StrategyInfo,
  Ticker,
  Trade,
  TrainingReport,
  UserRow,
} from './types'

const TOKEN_KEY = 'tt_token'

export function getToken(): string | null {
  return typeof localStorage !== 'undefined' ? localStorage.getItem(TOKEN_KEY) : null
}

export function setToken(token: string | null) {
  if (typeof localStorage === 'undefined') return
  if (token) localStorage.setItem(TOKEN_KEY, token)
  else localStorage.removeItem(TOKEN_KEY)
}

// Raised on a 401 so the app can bounce the user back to the login screen.
export class AuthError extends Error {}

// Global handler invoked when an AUTHENTICATED request is rejected with 401
// (expired/invalid session). The app registers this to force a clean logout.
// Login/signup 401s (no token present) intentionally do NOT trigger it.
let authFailureHandler: (() => void) | null = null
export function setAuthFailureHandler(fn: (() => void) | null) {
  authFailureHandler = fn
}
export function notifyAuthFailure() {
  setToken(null)
  authFailureHandler?.()
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken()
  const res = await fetch(path, {
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    ...init,
  })
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail ?? detail
    } catch {
      /* ignore */
    }
    if (res.status === 401) {
      // Only a request that CARRIED a token is a dead session -> force logout.
      // A 401 from login/signup (no token) is just bad credentials.
      if (token) notifyAuthFailure()
      throw new AuthError(detail || 'Unauthorized')
    }
    throw new Error(detail)
  }
  return res.json() as Promise<T>
}

export const api = {
  // ---- auth ----
  // Sign up with the licence key you bought (unless the operator runs in
  // auto-license mode), plus the username + email + password you'll log in with.
  signup: (body: {
    username: string
    email: string
    password: string
    license_key?: string
  }) =>
    req<{ access_token: string }>('/api/auth/signup', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  // Log in with EITHER your username or your email in the single identifier field.
  login: (identifier: string, password: string) =>
    req<{ access_token: string }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ identifier, password }),
    }),
  me: () => req<Me>('/api/auth/me'),
  updateCredentials: (body: {
    binance_api_key?: string
    binance_api_secret?: string
    binance_testnet?: boolean
  }) => req<Me>('/api/credentials', { method: 'PUT', body: JSON.stringify(body) }),

  // ---- admin ----
  adminUsers: () => req<UserRow[]>('/api/admin/users'),
  adminSetLicense: (id: number, status: 'pending' | 'active' | 'revoked') =>
    req<UserRow>(`/api/admin/users/${id}/license`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    }),
  // Extend (or start) a user's time-limited licence by N days and make them live.
  adminAddDays: (id: number, days: number) =>
    req<UserRow>(`/api/admin/users/${id}/add-days`, {
      method: 'POST',
      body: JSON.stringify({ days }),
    }),
  adminDeleteUser: (id: number) =>
    req<{ deleted: number }>(`/api/admin/users/${id}`, { method: 'DELETE' }),
  // Licence keys: admin mints them (optionally with a label + day count), clients
  // redeem them at signup to go live instantly.
  adminLicenseKeys: () => req<LicenseKeyRow[]>('/api/admin/license-keys'),
  adminCreateLicenseKey: (label?: string, durationDays?: number | null) =>
    req<LicenseKeyCreated>('/api/admin/license-keys', {
      method: 'POST',
      body: JSON.stringify({
        label: label ?? null,
        duration_days: durationDays ?? null,
      }),
    }),
  adminRevokeLicenseKey: (id: number) =>
    req<LicenseKeyRow>(`/api/admin/license-keys/${id}/revoke`, { method: 'POST' }),
  // A logged-in pending user activates themselves with a key; returns the fresh
  // Me so the app can flip straight into the dashboard.
  redeemLicenseKey: (key: string) =>
    req<Me>('/api/license/redeem', { method: 'POST', body: JSON.stringify({ key }) }),

  // ---- trading ----
  status: () => req<BotStatus>('/api/status'),
  settings: () => req<Settings>('/api/settings'),
  updateSettings: (patch: Partial<Settings>) =>
    req<Settings>('/api/settings', { method: 'PATCH', body: JSON.stringify(patch) }),
  trades: (status?: string) =>
    req<Trade[]>(`/api/trades${status ? `?status=${status}` : ''}`),
  // Delete ONE closed trade from the journal (never an open/pending position;
  // does not change the wallet — realized P/L was already banked).
  deleteTrade: (id: number) =>
    req<{ deleted: number }>(`/api/trades/${id}`, { method: 'DELETE' }),
  // Clear CLOSED trade history for a book: 'paper' (default), 'live', or 'all'.
  clearTrades: (mode: 'paper' | 'live' | 'all' = 'paper') =>
    req<{ deleted: number }>(`/api/trades?mode=${mode}`, { method: 'DELETE' }),
  // Realized performance analytics computed live from the user's CLOSED trades
  // (win rate, profit factor, drawdown, per-symbol, paper/live splits).
  performance: () => req<Performance>('/api/performance'),
  signals: () => req<SignalRow[]>('/api/signals'),
  // Signal log is read-only history — deleting entries never affects positions.
  deleteSignal: (id: number) =>
    req<{ deleted: number }>(`/api/signals/${id}`, { method: 'DELETE' }),
  clearSignals: () =>
    req<{ deleted: number }>('/api/signals', { method: 'DELETE' }),
  setBot: (state: 'start' | 'stop') =>
    req<{ running: boolean }>(`/api/bot/${state}`, { method: 'POST' }),
  order: (body: {
    action: 'buy' | 'sell' | 'close'
    symbol: string
    amount?: number
    limit_price?: number
    stop_loss?: number
    take_profit?: number
  }) => req<ExecutionResult>('/api/order', { method: 'POST', body: JSON.stringify(body) }),
  // Scaled / DCA entry: split one BUY into N legs (first optionally at market,
  // the rest resting limits stepped step_pct% below). Sized ONCE by the risk
  // manager, then split — scaling never risks more than a single entry would.
  scaledOrder: (body: {
    symbol: string
    amount?: number
    legs: number
    step_pct: number
    first_at_market: boolean
    stop_loss?: number
    take_profit?: number
  }) => req<ScaledResult>('/api/order/scaled', { method: 'POST', body: JSON.stringify(body) }),
  // Close every open position AND cancel every resting order for a symbol — the
  // one-click exit for a multi-leg DCA ladder so it's never left half-managed.
  closeAll: (symbol: string) =>
    req<CloseAllResult>(`/api/positions/${encodeURIComponent(symbol)}/close-all`, {
      method: 'POST',
    }),
  closeTrade: (id: number) =>
    req<ExecutionResult>(`/api/trades/${id}/close`, { method: 'POST' }),
  ohlcv: (symbol: string, timeframe = '1h', limit = 200) =>
    req<Candle[]>(`/api/ohlcv/${encodeURIComponent(symbol)}?timeframe=${timeframe}&limit=${limit}`),
  ticker: (symbol: string) =>
    req<Ticker>(`/api/ticker/${encodeURIComponent(symbol)}`),
  // Live order book: the market's real resting bids (buy side) and asks (sell
  // side). Read-only public depth; on the Binance testnet it's the sandbox's own
  // thin book, not the live market.
  orderbook: (symbol: string, limit = 20) =>
    req<OrderBook>(`/api/orderbook/${encodeURIComponent(symbol)}?limit=${limit}`),
  backtest: (
    symbol: string,
    strategy: string,
    timeframe = '1h',
    feePct?: number,
    slippagePct?: number,
    useSaved = false,
  ) => {
    const params = new URLSearchParams({ symbol, strategy, timeframe })
    if (feePct !== undefined) params.set('fee_pct', String(feePct))
    if (slippagePct !== undefined) params.set('slippage_pct', String(slippagePct))
    if (useSaved) params.set('use_saved', 'true')
    return req<BacktestResult>(`/api/backtest?${params.toString()}`)
  },
  strategies: () => req<StrategyInfo[]>('/api/strategies'),
  // Train and (by default) SAVE the winning config to this account so the bot
  // can trade it. Pass save=false to preview a training run without persisting.
  train: (symbol: string, strategy: string, timeframe = '1h', save = true) =>
    req<TrainingReport>(
      `/api/train?symbol=${encodeURIComponent(symbol)}&strategy=${strategy}&timeframe=${timeframe}&save=${save}`,
      { method: 'POST' },
    ),
  // The strategies this user has trained and saved (one per symbol). These are
  // what the bot trades with when Settings `use_saved_strategy` is on. Empty
  // means nothing trained-and-saved yet — never a stub.
  savedStrategies: () => req<SavedStrategy[]>('/api/strategies/saved'),
  // Forget a saved strategy for a symbol; the bot falls back to the analyzer.
  deleteSavedStrategy: (symbol: string) =>
    req<{ removed: boolean; symbol: string }>(
      `/api/strategies/saved/${encodeURIComponent(symbol)}`,
      { method: 'DELETE' },
    ),
  analyze: (symbol: string, timeframe = '1h', explain = false, assess = false) =>
    req<MarketAnalysis>(
      `/api/analyze/${encodeURIComponent(symbol)}?timeframe=${timeframe}&explain=${explain}&assess=${assess}`,
    ),
  aiAsk: (question: string, symbol?: string, timeframe = '1h') =>
    req<{ answer: string; ai_enabled: boolean }>('/api/ai/ask', {
      method: 'POST',
      body: JSON.stringify({ question, symbol, timeframe }),
    }),
  // Assistant chat: grounded in the user's OWN non-secret bot state, an optional
  // per-symbol analysis, and (opt-in) live public news. The AI advises and can
  // PROPOSE an action (`proposed_action`) — it never executes: the UI shows a
  // Confirm card and only then calls the real endpoint. `history` carries prior
  // turns so the assistant keeps the thread across a multi-step conversation.
  aiChat: (body: {
    question: string
    symbol?: string
    timeframe?: string
    include_news?: boolean
    history?: { role: 'user' | 'assistant'; content: string }[]
    // An optional attached image the AI reads (vision). `data` is raw base64 (no
    // data: prefix); `media_type` is the real MIME (always image/jpeg here — the
    // client re-encodes on attach). The backend validates + size-caps it.
    image?: { data: string; media_type: string }
  }) =>
    req<{
      reply: string
      ai_enabled: boolean
      used_news: boolean
      proposed_action?: ProposedAction | null
    }>('/api/ai/chat', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  // Live, REAL market headlines from the configured public feeds. Returns any
  // real items plus per-feed errors; an empty list means the sources were
  // unreachable, never fabricated news.
  news: (limit = 8) =>
    req<{ items: NewsItem[]; errors: string[] }>(`/api/news?limit=${limit}`),
  exchangeAccess: () => req<ExchangeAccess>('/api/exchange/access'),
  // Real tradable spot pairs from the configured exchange (ccxt load_markets),
  // so the pair selector reflects what ACTUALLY exists on Binance rather than a
  // hardcoded list. Empty list = markets unreachable (honest, never fabricated).
  symbols: (quote = 'USDT') =>
    req<{ symbols: string[]; quote: string }>(`/api/symbols?quote=${quote}`),
  // Clean slate for SIMULATED data: wipe this account's paper trades + signal
  // log and reset the paper wallet. Real (live) trades are never touched.
  resetPaperData: () =>
    req<{ trades_deleted: number; signals_deleted: number; paper_balance: number }>(
      '/api/paper/reset',
      { method: 'POST' },
    ),
  // Real reachability of the app-wide AI provider (no secrets). Lets the
  // assistant show "connected" or the concrete reason it can't answer.
  aiHealth: () => req<AiHealth>('/api/ai/health'),

  // ---- price alerts ----
  // "Notify me when SYMBOL crosses PRICE." The background monitor checks each
  // armed alert against the REAL live price and fires it once — nothing here is
  // fabricated. Newest first.
  listAlerts: () => req<Alert[]>('/api/alerts'),
  createAlert: (body: {
    symbol: string
    condition: 'above' | 'below'
    price: number
    note?: string | null
  }) => req<Alert>('/api/alerts', { method: 'POST', body: JSON.stringify(body) }),
  deleteAlert: (id: number) =>
    req<{ deleted: number }>(`/api/alerts/${id}`, { method: 'DELETE' }),

  // ---- confirm-before-live (autonomous entries awaiting your yes/no) ----
  // The bot may decide a LIVE entry on its own; when the gate is on it QUEUES it
  // and pings you instead of placing. These list/approve/reject those proposals.
  // Approving re-runs the order FRESH (re-priced, re-sized, re-risk-checked) so a
  // stale snapshot never fires; rejecting drops it. Newest first.
  autoConfirmations: () => req<AutoConfirmation[]>('/api/auto/confirmations'),
  approveConfirmation: (id: number) =>
    req<ConfirmationResolveResult>(`/api/auto/confirmations/${id}/approve`, {
      method: 'POST',
    }),
  rejectConfirmation: (id: number) =>
    req<ConfirmationResolveResult>(`/api/auto/confirmations/${id}/reject`, {
      method: 'POST',
    }),
}
