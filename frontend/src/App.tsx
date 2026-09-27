import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type KeyboardEvent as ReactKeyboardEvent, type SetStateAction } from 'react'
import { api, setToken, getToken, setAuthFailureHandler } from './api'
import { PriceChart, TF_SECONDS } from './PriceChart'
import { TradingViewChart } from './TradingViewChart'
import { DEFAULT_INDICATORS, type IndicatorPrefs } from './indicators'
import { tradesToMarkers } from './chartMarkers'
import { loadTurns, saveTurns } from './chatHistory'
import { useBinanceStream } from './useBinanceStream'
import { Login, LicenseGate } from './Login'
import { Admin } from './Admin'
import { useSocket } from './useSocket'
import { useTheme, type Theme } from './theme'
import { ThemeToggle } from './ThemeToggle'
import type { AiHealth, Alert, BotStatus, BacktestResult, Candle, ChatTurn, ExchangeAccess, MarketAnalysis, Me, NewsItem, OrderBook as OrderBookData, Performance, PerfBucket, ProposedAction, SavedStrategy, Settings, SignalRow, StrategyInfo, Ticker, Trade, TrainingReport } from './types'

const SYMBOLS = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT']
const TIMEFRAMES = ['1m', '5m', '15m', '1h', '4h', '1d']

// WS events that mean the trades table on screen is now stale and must be
// refetched: a position opened/closed, a resting order placed/cancelled, the
// exchange reconciled a position (closed or resized it out from under us), or a
// trailing stop moved. Missing any of these would leave the UI showing a trade
// that no longer matches reality — unacceptable for a live-money view.
const TRADE_EVENTS: ReadonlySet<string> = new Set([
  'trade_opened',
  'trade_closed',
  'order_pending',
  'order_canceled',
  'reconcile_closed',
  'reconcile_adjusted',
  'stop_trailed',
])

// Which chart overlays correspond to each REAL analyzer factor, for the
// "watch the bot think" view. When a factor drove a decision we light up exactly
// the indicators that show it — trend → the moving averages, regime → the
// 200 SMA, rsi/macd → their oscillators, volatility → Bollinger Bands, volume →
// the volume panel. `momentum` and `shock` have no dedicated overlay (they're
// read from price/volatility that other rows already draw), so they map to
// nothing rather than lighting up something misleading. Honest by construction:
// we never show an indicator for a factor the analyzer didn't actually report.
const FACTOR_INDICATORS: Record<string, (keyof IndicatorPrefs)[]> = {
  trend: ['ema9', 'ema21', 'sma50'],
  regime: ['sma200'],
  rsi: ['rsi'],
  macd: ['macd'],
  volatility: ['bb'],
  volume: ['volume'],
}


function fmt(n: number | null | undefined, dp = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return '-'
  return n.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp })
}

type Toast = { kind: 'ok' | 'error'; text: string } | null
// A persisted copy of a toast, kept in the header's notifications feed so bot
// pings/alerts aren't lost the instant the transient toast auto-dismisses.
type Notif = { id: number; kind: 'ok' | 'error'; text: string; ts: number }

export default function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [checking, setChecking] = useState(true)
  const [theme, toggleTheme] = useTheme()

  const loadMe = useCallback(async () => {
    if (!getToken()) {
      setMe(null)
      setChecking(false)
      return
    }
    try {
      setMe(await api.me())
    } catch {
      setToken(null)
      setMe(null)
    } finally {
      setChecking(false)
    }
  }, [])

  useEffect(() => {
    loadMe()
  }, [loadMe])

  const logout = useCallback(() => {
    setToken(null)
    setMe(null)
  }, [])

  // Force a clean logout when any authenticated request (REST or WebSocket)
  // reports the session is dead (401 / ws 4401), instead of leaving the user
  // on a broken dashboard that silently fails every call.
  useEffect(() => {
    setAuthFailureHandler(() => setMe(null))
    return () => setAuthFailureHandler(null)
  }, [])

  if (checking) {
    return <div className="auth-wrap"><div className="auth-card">Loading…</div></div>
  }
  if (!me) {
    return <Login onAuthed={() => { setChecking(true); loadMe() }} theme={theme} onToggleTheme={toggleTheme} />
  }
  // Gate on EFFECTIVE access (active AND not expired), not status alone, so an
  // expired time-limited licence is stopped at the door just like a pending one.
  if (!me.license_active) {
    const gate: 'pending' | 'revoked' | 'expired' =
      me.license_status === 'revoked'
        ? 'revoked'
        : me.license_status === 'active'
          ? 'expired' // status active but past its expiry
          : 'pending'
    return (
      <LicenseGate
        status={gate}
        email={me.email}
        onLogout={logout}
        onRedeemed={setMe}
        theme={theme}
        onToggleTheme={toggleTheme}
      />
    )
  }
  return <Dashboard me={me} onLogout={logout} onMeChanged={setMe} theme={theme} onToggleTheme={toggleTheme} />
}

type TabKey = 'trades' | 'performance' | 'signals' | 'assistant' | 'news' | 'analyze' | 'train' | 'backtest' | 'settings' | 'admin'

// Left-drawer navigation. `admin: true` items only render for admins. The same
// keys drive the in-panel tab strip, so the two stay in sync off one `tab`.
const NAV: { key: TabKey; label: string; icon: string; admin?: boolean }[] = [
  { key: 'trades', label: 'Trades', icon: '📈' },
  { key: 'performance', label: 'Performance', icon: '🏆' },
  { key: 'signals', label: 'Signals', icon: '📡' },
  { key: 'assistant', label: 'AI Assistant', icon: '🤖' },
  { key: 'news', label: 'News', icon: '📰' },
  { key: 'analyze', label: 'Analyze', icon: '🔍' },
  { key: 'train', label: 'Train', icon: '🧠' },
  { key: 'backtest', label: 'Backtest', icon: '↺' },
  { key: 'settings', label: 'Settings', icon: '⚙' },
  { key: 'admin', label: 'Admin', icon: '🛡', admin: true },
]

// Human labels for a nav destination, used when the AI asks to "take me to X".
const NAV_LABEL: Record<TabKey, string> = {
  trades: 'Trades',
  performance: 'Performance',
  signals: 'Signals',
  assistant: 'AI Assistant',
  news: 'News',
  analyze: 'Analyze',
  train: 'Train',
  backtest: 'Backtest',
  settings: 'Settings',
  admin: 'Admin',
}

// Map the words the assistant might emit in [[goto:<dest>]] to a real tab. We
// accept synonyms so "keys", "connect", "credentials" etc. all land on Settings.
const NAV_ALIAS: Record<string, TabKey> = {
  trades: 'trades', trade: 'trades', positions: 'trades', dashboard: 'trades', home: 'trades',
  performance: 'performance', perf: 'performance', stats: 'performance', results: 'performance',
  pnl: 'performance', analytics: 'performance',
  signals: 'signals', signal: 'signals',
  assistant: 'assistant', ai: 'assistant', chat: 'assistant',
  news: 'news', headlines: 'news', feed: 'news', feeds: 'news',
  analyze: 'analyze', analysis: 'analyze', analyse: 'analyze',
  train: 'train', training: 'train',
  backtest: 'backtest', backtesting: 'backtest',
  settings: 'settings', setting: 'settings', credentials: 'settings', keys: 'settings',
  connect: 'settings', connection: 'settings', binance: 'settings', account: 'settings', risk: 'settings',
  admin: 'admin', users: 'admin',
}

// Pull a trailing [[goto:<dest>]] action out of an AI reply: returns the reply
// with the tag stripped (it's machine-only, never shown) plus the resolved tab.
function parseNavAction(text: string): { text: string; dest: TabKey | null } {
  const m = text.match(/\[\[\s*goto\s*:\s*([a-zA-Z]+)\s*\]\]/i)
  if (!m) return { text, dest: null }
  const dest = NAV_ALIAS[m[1].toLowerCase()] ?? null
  return { text: text.replace(m[0], '').trim(), dest }
}

// One message in the assistant transcript. An AI reply may carry a hidden
// [[goto:…]] navigation target or a validated proposal (shown as a Confirm/Cancel
// card; `actionState` tracks its lifecycle so it can't run twice). `live` marks a
// proactive call-out the monitor/alerts pushed in — not a reply to something you
// typed. Lifted to module scope + the Dashboard so pushed call-outs land here even
// when the Assistant tab isn't mounted.
type ChatMsg = ChatTurn & {
  nav?: { dest: TabKey; label: string }
  action?: ProposedAction
  actionState?: 'pending' | 'running' | 'done' | 'dismissed'
  live?: boolean
  // When this turn was created (unix ms). Used to age the persisted transcript
  // out after 24h (see chatHistory). Stamped at creation; absent on old data.
  ts?: number
  // Stable id for an action-bearing reply (see _actionTurnSeq). Only set when the
  // turn carries a proposal the autopilot may auto-run.
  id?: number
}

// Monotonic id stamped on assistant reply turns that carry a proposed action, so
// the autopilot effect can start each auto-action EXACTLY once (matching by id is
// stable across the appends/awaits that a manual index can't survive).
let _actionTurnSeq = 0

function Dashboard({
  me,
  onLogout,
  onMeChanged,
  theme,
  onToggleTheme,
}: {
  me: Me
  onLogout: () => void
  onMeChanged: (m: Me) => void
  theme: Theme
  onToggleTheme: () => void
}) {
  const [status, setStatus] = useState<BotStatus | null>(null)
  const [settings, setSettings] = useState<Settings | null>(null)
  const [settingsError, setSettingsError] = useState(false)
  const [trades, setTrades] = useState<Trade[]>([])
  const [signals, setSignals] = useState<SignalRow[]>([])
  const [candles, setCandles] = useState<Candle[]>([])
  const [ticker, setTicker] = useState<Ticker | null>(null)
  const [tickerStale, setTickerStale] = useState(false)
  const [symbol, setSymbol] = useState(SYMBOLS[0])
  const [timeframe, setTimeframe] = useState('1h')
  // Real tradable pairs pulled from the exchange (see effect below). Seeded with
  // the majors as a fallback so the picker is never empty if markets are briefly
  // unreachable; replaced with the live Binance listing once it loads.
  const [symbolList, setSymbolList] = useState<string[]>(SYMBOLS)
  // Which chart the user is looking at: our own real-data candle chart (with the
  // bot's trades/alerts marked) or the embedded full TradingView chart. Remembered
  // between visits.
  const [chartView, setChartView] = useState<'bot' | 'tv'>(
    () => (localStorage.getItem('tt.chartView') === 'tv' ? 'tv' : 'bot'),
  )
  // Which price-overlay indicators are switched on, loaded from localStorage so
  // the choice sticks (like a saved TradingView layout). All real math on the
  // bot chart's own candles.
  const [indicators, setIndicators] = useState<IndicatorPrefs>(() => {
    try {
      const raw = localStorage.getItem('tt.indicators')
      if (raw) return { ...DEFAULT_INDICATORS, ...(JSON.parse(raw) as Partial<IndicatorPrefs>) }
    } catch {
      /* ignore bad/absent stored prefs */
    }
    return DEFAULT_INDICATORS
  })
  // Trade arrows (buy/sell markers on the exact bars where your OWN trades opened
  // and closed) are OFF by default — they can crowd the chart — and shown on
  // demand via the "Trades" toggle. Remembered like the other chart prefs.
  const [showTradeMarkers, setShowTradeMarkers] = useState<boolean>(
    () => localStorage.getItem('tt.showTradeMarkers') === '1',
  )
  // Bumped to tell PriceChart to wipe every hand-drawn line (the assistant's
  // "clear the drawings" command). A counter, not a boolean, so each request is a
  // distinct edge PriceChart can react to.
  const [chartClearSignal, setChartClearSignal] = useState(0)
  // View-history for the assistant's chart commands, so "undo" steps the chart
  // back one change. Each entry is the view as it was BEFORE a change we applied.
  const chartUndoRef = useRef<{ symbol: string; timeframe: string; indicators: IndicatorPrefs }[]>([])
  // "Watch the bot think": when ON, each autonomous verdict briefly drives the
  // chart to the symbol it just decided on and lights up the indicators for the
  // REAL factors behind that call — so you can SEE why it acted — then it
  // auto-reverts to your own view a few seconds later. Opt-in and OFF by default
  // (it temporarily moves your chart), and it only ever changes what you're
  // LOOKING AT — it places no orders and changes no settings.
  const [watchThinking, setWatchThinking] = useState<boolean>(
    () => localStorage.getItem('tt.watchThinking') === '1',
  )
  // The decision currently on screen (for the "thinking" banner), or null when the
  // overlay is idle. Every figure here is the analyzer's real output, not invented.
  const [thinkingInfo, setThinkingInfo] = useState<{
    symbol: string
    timeframe: string
    verdict: string
    confidence: number | null
    acted: boolean
    factors: { name: string; signal: 'buy' | 'sell' | 'hold'; weight: number }[]
  } | null>(null)
  // The operator's OWN view, snapshotted when a thinking overlay first takes over,
  // so it can be restored exactly when the overlay ends. Kept separate from the
  // assistant's undo stack — this is transient and never surfaced as "undo".
  const thinkingSnapRef = useRef<{ symbol: string; timeframe: string; indicators: IndicatorPrefs } | null>(null)
  const thinkingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    localStorage.setItem('tt.watchThinking', watchThinking ? '1' : '0')
  }, [watchThinking])
  useEffect(() => {
    localStorage.setItem('tt.chartView', chartView)
  }, [chartView])
  useEffect(() => {
    localStorage.setItem('tt.indicators', JSON.stringify(indicators))
  }, [indicators])
  useEffect(() => {
    localStorage.setItem('tt.showTradeMarkers', showTradeMarkers ? '1' : '0')
  }, [showTradeMarkers])
  // Apply a VIEW-ONLY chart command from the assistant (switch symbol/timeframe,
  // toggle indicators, clear drawings — or step back one change on `undo`). It only
  // ever changes what the operator is LOOKING AT; it moves no money and calls no
  // endpoint. Returns a short, TRUE summary of what actually changed, which the
  // assistant shows as the outcome line — so it's never the AI claiming something
  // it didn't really do. Defined as a plain closure so it always reads the freshest
  // chart state for its undo snapshot.
  const applyChartControl = (c: {
    symbol?: string
    timeframe?: string
    indicators?: Partial<IndicatorPrefs>
    clear_drawings?: boolean
    undo?: boolean
  }): string => {
    if (c.undo) {
      const prev = chartUndoRef.current.pop()
      if (!prev) return 'Nothing to undo on the chart.'
      setSymbol(prev.symbol)
      setTimeframe(prev.timeframe)
      setIndicators(prev.indicators)
      if (chartView !== 'bot') setChartView('bot')
      const on = Object.entries(prev.indicators).filter(([, v]) => v).map(([k]) => k).join(', ')
      return `Reverted the chart to ${prev.symbol} · ${prev.timeframe}${on ? ` · ${on}` : ''}.`
    }
    const parts: string[] = []
    const viewChanges =
      (!!c.symbol && c.symbol !== symbol) ||
      (!!c.timeframe && c.timeframe !== timeframe) ||
      (!!c.indicators && Object.keys(c.indicators).length > 0)
    // Snapshot the CURRENT view before a view change so `undo` can restore it.
    // Clearing drawings is destructive and NOT snapshotted — undo can't un-delete
    // drawings (and the assistant is told to say so).
    if (viewChanges) {
      chartUndoRef.current.push({ symbol, timeframe, indicators })
      if (chartUndoRef.current.length > 25) chartUndoRef.current.shift()
    }
    if (c.symbol && c.symbol !== symbol) {
      setSymbol(c.symbol)
      parts.push(`symbol → ${c.symbol}`)
    }
    if (c.timeframe && c.timeframe !== timeframe) {
      setTimeframe(c.timeframe)
      parts.push(`timeframe → ${c.timeframe}`)
    }
    if (c.indicators && Object.keys(c.indicators).length > 0) {
      const inds = c.indicators
      setIndicators((cur) => ({ ...cur, ...inds }))
      const shown = Object.entries(inds).filter(([, v]) => v).map(([k]) => k)
      const hidden = Object.entries(inds).filter(([, v]) => v === false).map(([k]) => k)
      if (shown.length) parts.push(`show ${shown.join(', ')}`)
      if (hidden.length) parts.push(`hide ${hidden.join(', ')}`)
    }
    if (c.clear_drawings) {
      setChartClearSignal((n) => n + 1)
      parts.push('cleared all drawings')
    }
    if (!parts.length) return 'The chart already matched that — nothing to change.'
    // The TradingView embed can't be driven, so make sure the operator is on our
    // own real-data chart where these changes are actually visible.
    if (chartView !== 'bot') setChartView('bot')
    return `Chart updated: ${parts.join(' · ')}.`
  }
  // Restore the operator's own view after a "thinking" overlay, and clear the
  // banner + timer. Safe to call when nothing is active (no-op).
  const revertThinking = () => {
    if (thinkingTimerRef.current) {
      clearTimeout(thinkingTimerRef.current)
      thinkingTimerRef.current = null
    }
    const snap = thinkingSnapRef.current
    thinkingSnapRef.current = null
    setThinkingInfo(null)
    if (snap) {
      setSymbol(snap.symbol)
      setTimeframe(snap.timeframe)
      setIndicators(snap.indicators)
    }
  }
  // Briefly visualise ONE real autonomous decision on the chart: switch to the
  // symbol/timeframe it was made on and turn on the indicators for the factors
  // that actually drove it, so the operator can see WHY. The very first decision
  // in a burst snapshots the operator's own view; each new decision re-arms a
  // timer that reverts to that snapshot once the bot goes quiet. Only what the
  // operator LOOKS AT changes — no order, no setting. Nothing shown is invented:
  // the symbol, timeframe and factors are exactly what the analyzer emitted.
  const visualizeDecision = (d: {
    symbol: string
    timeframe?: string
    confidence?: number
    accepted: boolean
    action: string
    factors?: { name: string; signal: 'buy' | 'sell' | 'hold'; weight: number }[]
  }) => {
    const factors = d.factors ?? []
    // Map the real factors to their chart indicators (deduped). Factors with no
    // dedicated overlay (momentum/shock) simply contribute nothing.
    const wanted: Partial<IndicatorPrefs> = {}
    for (const f of factors) {
      for (const key of FACTOR_INDICATORS[f.name] ?? []) wanted[key] = true
    }
    // Snapshot the operator's own view ONCE, before the first override of a burst.
    if (!thinkingSnapRef.current) {
      thinkingSnapRef.current = { symbol, timeframe, indicators }
    }
    if (d.symbol && d.symbol !== symbol) setSymbol(d.symbol)
    if (d.timeframe && d.timeframe !== timeframe) setTimeframe(d.timeframe)
    if (Object.keys(wanted).length) setIndicators((cur) => ({ ...cur, ...wanted }))
    if (chartView !== 'bot') setChartView('bot')
    setThinkingInfo({
      symbol: d.symbol,
      timeframe: d.timeframe ?? timeframe,
      verdict: d.action,
      confidence: typeof d.confidence === 'number' ? d.confidence : null,
      acted: d.accepted,
      factors,
    })
    // Re-arm the auto-revert: the overlay clears a few seconds after the LAST
    // decision, so a run of quick verdicts stays up, then tidies itself away.
    if (thinkingTimerRef.current) clearTimeout(thinkingTimerRef.current)
    thinkingTimerRef.current = setTimeout(revertThinking, 7000)
  }
  // If "watch the bot think" is switched off — or the component unmounts — while
  // an overlay is showing, put the operator's own view back at once instead of
  // leaving the chart on the bot's last pick (the pending timer is cleared too).
  useEffect(() => {
    if (!watchThinking) revertThinking()
    return () => {
      if (thinkingTimerRef.current) clearTimeout(thinkingTimerRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watchThinking])
  // Wall-clock of the last status we received (WS push or poll), so the bot
  // activity strip can show an honest "updated Ns ago" heartbeat.
  const [statusTs, setStatusTs] = useState(0)
  const [amount, setAmount] = useState('')
  const [limitPrice, setLimitPrice] = useState('')
  const [stopLoss, setStopLoss] = useState('')
  const [takeProfit, setTakeProfit] = useState('')
  const [placing, setPlacing] = useState(false)
  // Scaled / DCA entry controls (buy-only; splits one entry into laddered legs).
  const [scaleIn, setScaleIn] = useState(false)
  const [legs, setLegs] = useState('3')
  const [stepPct, setStepPct] = useState('1')
  const [firstAtMarket, setFirstAtMarket] = useState(true)
  const [scaling, setScaling] = useState(false)
  const [closingAll, setClosingAll] = useState(false)
  const [closing, setClosing] = useState<number | null>(null)
  const [toast, setToast] = useState<Toast>(null)
  const [tab, setTab] = useState<TabKey>('trades')
  const [access, setAccess] = useState<ExchangeAccess | null>(null)
  // Real-time market data: when the account is on REAL Binance.com (not testnet,
  // not a public fallback venue), stream price / candles / depth straight from
  // Binance's PUBLIC websocket in the browser so the chart, price and order book
  // move the SAME as a TradingView chart — no REST polling lag. It's public,
  // read-only, key-less data; REST stays the seed (history) and the fallback
  // whenever the stream isn't live, so nothing on screen is ever fabricated.
  const liveBinance = !!access && !access.testnet && (access.exchange ?? 'binance') === 'binance'
  const stream = useBinanceStream(symbol, timeframe, liveBinance)
  const [menuOpen, setMenuOpen] = useState(false)
  // Global connection status (shown in the slim bar under the header on every
  // tab): the built-in AI provider's real reachability + the exchange access.
  const [aiHealth, setAiHealth] = useState<AiHealth | null>(null)
  const [aiHealthLoading, setAiHealthLoading] = useState(false)
  const [testingAccess, setTestingAccess] = useState(false)
  // Header notifications feed: a capped, persistent log of recent alerts/pings
  // behind the 🔔 bell, with an unread count.
  const [notifs, setNotifs] = useState<Notif[]>([])
  const [notifUnread, setNotifUnread] = useState(0)
  // The AI-assistant transcript, LIFTED here (out of AssistantPanel) so proactive
  // monitor/alert call-outs pushed over the socket land in it even when the
  // Assistant tab isn't mounted — and so voice can read them regardless of tab.
  // Restored from this browser's 24h history (scoped to this user) so the thread
  // survives a refresh/quit and the assistant can recall earlier turns.
  const [turns, setTurns] = useState<ChatMsg[]>(() => loadTurns<ChatMsg>(me.id))
  // Persist the transcript back for 24h whenever it changes (see chatHistory):
  // real messages only, scoped to this user, proposed-action cards neutralised on
  // reload so a stale order card can never be one-click executed later.
  useEffect(() => {
    saveTurns(me.id, turns)
  }, [turns, me.id])
  // Read-aloud (Web Speech) is OFF by default; the user turns it on in the
  // assistant. Lifted so a pushed alert can be spoken from any tab when it's on.
  const [readAloud, setReadAloud] = useState(false)
  // The user's price alerts (armed + recently triggered): real rows from the
  // backend, driving the chart markers and the alerts panel. Never fabricated.
  const [alerts, setAlerts] = useState<Alert[]>([])

  const showToast = useCallback((kind: 'ok' | 'error', text: string) => {
    setToast({ kind, text })
    // Mirror every toast into the persistent notifications feed so it survives
    // the 4s auto-dismiss. Newest first; capped so the log can't grow unbounded.
    setNotifs((n) =>
      [{ id: Date.now() + Math.random(), kind, text, ts: Date.now() }, ...n].slice(0, 50),
    )
    setNotifUnread((u) => Math.min(u + 1, 999))
    setTimeout(() => setToast(null), 4000)
  }, [])

  // Read a line aloud via the browser's Web Speech API — ONLY when the user has
  // turned voice on (OFF by default) and the browser supports it. Shared by the
  // assistant's typed replies and the proactive monitor/alert call-outs.
  const ttsSupported = typeof window !== 'undefined' && 'speechSynthesis' in window
  const speak = useCallback(
    (text: string) => {
      if (!readAloud || !ttsSupported || !text) return
      try {
        window.speechSynthesis.cancel()
        window.speechSynthesis.speak(new SpeechSynthesisUtterance(text))
      } catch {
        /* ignore */
      }
    },
    [readAloud, ttsSupported],
  )

  // Close the nav drawer on Escape so it behaves like a normal modal drawer.
  useEffect(() => {
    if (!menuOpen) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [menuOpen])

  const refreshTrades = useCallback(async () => {
    try {
      setTrades(await api.trades())
    } catch (e) {
      /* ignore */
    }
  }, [])

  const refreshSignals = useCallback(async () => {
    try {
      setSignals(await api.signals())
    } catch (e) {
      /* ignore */
    }
  }, [])

  // The user's price alerts — refetched after one fires (its status flips to
  // "triggered"), after an add/delete, and on a slow poll as a safety net.
  const refreshAlerts = useCallback(async () => {
    try {
      setAlerts(await api.listAlerts())
    } catch {
      /* ignore */
    }
  }, [])

  // Settings load is its own callback so the Settings panel can retry it after
  // a failed fetch instead of being stuck on "Loading…" forever (the fetch
  // failing is distinct from it still being in flight).
  const loadSettings = useCallback(async () => {
    try {
      setSettings(await api.settings())
      setSettingsError(false)
    } catch (e) {
      setSettingsError(true)
    }
  }, [])

  // Apply a fresh status and stamp when it arrived, so the UI can show a
  // truthful "last updated" heartbeat rather than implying constant liveness.
  const applyStatus = useCallback((s: BotStatus) => {
    setStatus(s)
    setStatusTs(Date.now())
  }, [])

  const { connected } = useSocket({
    onStatus: applyStatus,
    onEvent: (m) => {
      if (TRADE_EVENTS.has(m.event)) {
        refreshTrades()
      }
      if (m.event === 'signal') {
        refreshSignals()
        // The built-in analyzer logs its OWN verdict changes here too. A
        // hold/blocked verdict isn't a failure, so don't flash a red error
        // toast for it — only surface a toast when the bot actually acted.
        // External (TradingView) signals keep the ok/error toast so a
        // rejected alert is still visible.
        if (m.data.source === 'analyzer') {
          if (m.data.accepted) showToast('ok', m.data.message)
          // When "watch the bot think" is on, briefly paint this real decision on
          // the chart (its symbol + the indicators for the factors that drove it),
          // then auto-revert. Only ever changes the view — never money or settings.
          if (watchThinking) visualizeDecision(m.data)
        } else {
          showToast(m.data.accepted ? 'ok' : 'error', m.data.message)
        }
      }
      if (m.event === 'assistant') {
        const d = m.data
        // A proactive call-out (fired price alert, or a live risk read on an open
        // position). Append it to the shared transcript (capped) so it's there
        // whatever tab is open; toast by level; and speak it if the user turned
        // voice on. A fired alert also flipped its row to "triggered" — refetch so
        // the list + chart marker update.
        setTurns((t) => [...t, { role: 'ai', text: d.text, live: true, ts: Date.now() } as ChatMsg].slice(-200))
        showToast(d.level === 'warn' ? 'error' : 'ok', d.text)
        speak(d.text)
        if (d.kind === 'alert') refreshAlerts()
      }
    },
  })

  // Re-run the real exchange connection probe (used on mount, after saving
  // API keys, and by the "Test connection" button) so the connection status
  // shown is always the true, current result — never a stale/blank guess.
  const refreshAccess = useCallback(async (): Promise<ExchangeAccess | null> => {
    try {
      const a = await api.exchangeAccess()
      setAccess(a)
      return a
    } catch {
      return null
    }
  }, [])

  // Real AI-provider reachability probe (one honest ping, no secrets) so the
  // global status bar can say "connected" or the concrete reason it can't answer.
  const loadAiHealth = useCallback(async () => {
    setAiHealthLoading(true)
    try {
      setAiHealth(await api.aiHealth())
    } catch {
      setAiHealth(null)
    } finally {
      setAiHealthLoading(false)
    }
  }, [])

  // Re-run the exchange probe on demand from the status bar's "Test" button.
  const testAccess = useCallback(async () => {
    setTestingAccess(true)
    try {
      const a = await refreshAccess()
      if (!a) showToast('error', 'Could not reach the connection check.')
    } finally {
      setTestingAccess(false)
    }
  }, [refreshAccess, showToast])

  // Wipe SIMULATED (paper) data for a clean slate. The backend never touches
  // real (live) trades. Refreshes the tables and wallet so the reset shows
  // immediately. Guarded by a confirm — it can't be undone.
  const resetPaper = useCallback(async () => {
    const ok = window.confirm(
      'Reset ALL paper (simulated) data?\n\n' +
        'This deletes your paper trades and signal history and resets the paper ' +
        'wallet to its starting balance. Your real (live) trades are NOT touched. ' +
        'This cannot be undone.',
    )
    if (!ok) return
    try {
      const res = await api.resetPaperData()
      await Promise.all([refreshTrades(), refreshSignals()])
      api.status().then(applyStatus).catch(() => {})
      showToast(
        'ok',
        `Paper data cleared — wallet reset to ${res.paper_balance.toLocaleString()} USDT`,
      )
    } catch (e) {
      showToast('error', e instanceof Error ? e.message : 'Could not reset paper data')
    }
  }, [refreshTrades, refreshSignals, applyStatus, showToast])

  // Initial load.
  useEffect(() => {
    api.status().then(applyStatus).catch(() => {})
    loadSettings()
    refreshAccess()
    loadAiHealth()
    refreshTrades()
    refreshSignals()
    refreshAlerts()
  }, [loadSettings, refreshAccess, loadAiHealth, refreshTrades, refreshSignals, refreshAlerts, applyStatus])

  // Pull the REAL tradable pairs from the exchange once, so the symbol picker
  // reflects what actually exists on Binance instead of a hardcoded guess. If
  // the venue is briefly unreachable it returns [] and we keep the majors
  // fallback — never a fabricated list.
  useEffect(() => {
    api
      .symbols()
      .then((r) => {
        if (r.symbols && r.symbols.length) setSymbolList(r.symbols)
      })
      .catch(() => {})
  }, [])

  // Poll the trades table on a slow cadence as a safety net. Trade changes are
  // normally pushed over the WebSocket (see onEvent), but if the socket drops
  // and reconnects, any events during the gap are missed; this also keeps an
  // open position's unrealized PnL from going stale between pushes. Cheap GET.
  useEffect(() => {
    const id = setInterval(refreshTrades, 15000)
    return () => clearInterval(id)
  }, [refreshTrades])

  // Same safety net for the signal log: verdict changes are pushed over the
  // WebSocket, but poll on a slow cadence so anything missed during a socket
  // gap still appears (and the tab is populated even if a push was dropped).
  useEffect(() => {
    const id = setInterval(refreshSignals, 20000)
    return () => clearInterval(id)
  }, [refreshSignals])

  // Slow safety-net poll for alerts (they also refresh on fire / add / delete),
  // so a change made on another device still shows up here.
  useEffect(() => {
    const id = setInterval(refreshAlerts, 30000)
    return () => clearInterval(id)
  }, [refreshAlerts])

  // Real horizontal levels to MARK on the chart for the CURRENT symbol: each
  // ARMED price alert, plus every OPEN position's entry / stop-loss / take-profit.
  // Every value is a genuine number from the user's own data — never decorative.
  const chartPriceLines = useMemo(() => {
    const sym = symbol.toUpperCase()
    const lines: { price: number; color?: string; title?: string }[] = []
    for (const a of alerts) {
      if (a.status !== 'armed' || a.symbol.toUpperCase() !== sym) continue
      lines.push({ price: a.price, color: '#f0a020', title: `Alert ${a.condition} ${fmt(a.price)}` })
    }
    for (const t of trades) {
      if (t.status !== 'open' || t.symbol.toUpperCase() !== sym) continue
      if (t.entry_price) lines.push({ price: t.entry_price, color: '#3b82f6', title: `Entry ${fmt(t.entry_price)}` })
      if (t.stop_loss) lines.push({ price: t.stop_loss, color: '#ea3943', title: `SL ${fmt(t.stop_loss)}` })
      if (t.take_profit) lines.push({ price: t.take_profit, color: '#16c784', title: `TP ${fmt(t.take_profit)}` })
    }
    return lines
  }, [alerts, trades, symbol])

  // Buy/sell arrows on the exact bars where THIS symbol's trades opened/closed —
  // real history only (see tradesToMarkers), snapped to the candle timeframe.
  const chartMarkers = useMemo(
    () => tradesToMarkers(trades, symbol, TF_SECONDS[timeframe] ?? 0),
    [trades, symbol, timeframe],
  )

  // Load candles when symbol/timeframe changes, and poll periodically. The
  // poll is fairly frequent so a new closed bar shows up quickly; the live
  // ticker (below) keeps the forming bar moving in between reloads.
  useEffect(() => {
    let alive = true
    setCandles([]) // drop the previous market's bars immediately on a switch
    const load = (isInitial: boolean) =>
      api
        .ohlcv(symbol, timeframe, 200)
        .then((c) => alive && setCandles(c))
        .catch(() => {
          // Only blank the chart if the very first fetch for this market
          // fails (genuine "no data"); on background polls keep the last good
          // bars rather than wiping the chart over a transient hiccup.
          if (alive && isInitial) setCandles([])
        })
    load(true)
    const id = setInterval(() => load(false), 10000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [symbol, timeframe])

  // Live price feed: poll the ticker fast so the chart's newest bar and the
  // header last-price move in near-real-time, like an exchange chart. Reset on
  // symbol change so a stale price from the previous market never lingers.
  useEffect(() => {
    let alive = true
    setTicker(null)
    setTickerStale(false)
    let lastOk = Date.now()
    const load = () =>
      api
        .ticker(symbol)
        .then((t) => {
          if (!alive) return
          lastOk = Date.now()
          setTicker(t)
          setTickerStale(false)
        })
        .catch(() => {
          // Keep the last price on screen, but once the feed has been down for
          // several polls flag it stale so a frozen quote is never presented as
          // live — "if it's offline, show it offline".
          if (alive && Date.now() - lastOk > 12000) setTickerStale(true)
        })
    load()
    const id = setInterval(load, 3000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [symbol])

  const openTrades = useMemo(
    () => trades.filter((t) => t.status === 'open' || t.status === 'pending'),
    [trades],
  )

  const doOrder = async (action: 'buy' | 'sell') => {
    if (placing) return // guard against double-submit / duplicate orders
    const amt = amount ? Number(amount) : undefined
    const lim = limitPrice ? Number(limitPrice) : undefined
    const sl = stopLoss ? Number(stopLoss) : undefined
    const tp = takeProfit ? Number(takeProfit) : undefined
    // Client-side numeric validation: any provided value must be > 0.
    const fields: [string, number | undefined][] = [
      ['Amount', amt],
      ['Limit price', lim],
      ['Stop loss', sl],
      ['Take profit', tp],
    ]
    for (const [label, v] of fields) {
      if (v !== undefined && (!Number.isFinite(v) || v <= 0)) {
        showToast('error', `${label} must be a positive number.`)
        return
      }
    }
    // Confirm before spending REAL money. Confirm unless we positively KNOW the
    // account is in paper mode: if status hasn't loaded yet (mode unknown) we
    // must not silently wave through what could be a live, real-funds order.
    if (status?.trading_mode !== 'paper') {
      const knownLive = status?.trading_mode === 'live'
      const parts = [
        `${action.toUpperCase()} ${symbol}`,
        amt ? `amount ${amt}` : 'auto-sized by risk',
        lim ? `limit ${lim}` : 'market',
      ]
      if (sl) parts.push(`stop-loss ${sl}`)
      if (tp) parts.push(`take-profit ${tp}`)
      const header = knownLive
        ? 'LIVE ORDER — this uses real funds on your Binance account.'
        : 'Trading mode not confirmed yet — this MAY place a REAL order on your Binance account.'
      const ok = window.confirm(`${header}\n\n${parts.join('  ·  ')}\n\nPlace this order?`)
      if (!ok) return
    }
    setPlacing(true)
    try {
      const res = await api.order({
        action,
        symbol,
        amount: amt,
        limit_price: lim,
        stop_loss: sl,
        take_profit: tp,
      })
      showToast(res.accepted ? 'ok' : 'error', res.message)
      refreshTrades()
    } catch (e) {
      showToast('error', (e as Error).message)
    } finally {
      setPlacing(false)
    }
  }

  const closeTrade = async (id: number) => {
    if (closing !== null) return // one close at a time; avoid double-close
    // Same conservative gate as doOrder: confirm unless we KNOW it's paper.
    if (status?.trading_mode !== 'paper') {
      const msg =
        status?.trading_mode === 'live'
          ? 'Close this LIVE position at market now?'
          : 'Trading mode not confirmed yet — this may close a REAL position at market. Continue?'
      if (!window.confirm(msg)) return
    }
    setClosing(id)
    try {
      const res = await api.closeTrade(id)
      showToast(res.accepted ? 'ok' : 'error', res.message)
      refreshTrades()
    } catch (e) {
      showToast('error', (e as Error).message)
    } finally {
      setClosing(null)
    }
  }

  const doScaledOrder = async () => {
    if (scaling) return // guard against double-submit
    const amt = amount ? Number(amount) : undefined
    const sl = stopLoss ? Number(stopLoss) : undefined
    const tp = takeProfit ? Number(takeProfit) : undefined
    const nLegs = Number(legs)
    const step = Number(stepPct)
    if (!Number.isInteger(nLegs) || nLegs < 2 || nLegs > 20) {
      showToast('error', 'Legs must be a whole number between 2 and 20.')
      return
    }
    if (!Number.isFinite(step) || step <= 0 || step > 50) {
      showToast('error', 'Step % must be greater than 0 and at most 50.')
      return
    }
    const nums: [string, number | undefined][] = [
      ['Amount', amt],
      ['Stop loss', sl],
      ['Take profit', tp],
    ]
    for (const [label, v] of nums) {
      if (v !== undefined && (!Number.isFinite(v) || v <= 0)) {
        showToast('error', `${label} must be a positive number.`)
        return
      }
    }
    // Same real-money confirm gate as doOrder: confirm unless we KNOW it's paper.
    if (status?.trading_mode !== 'paper') {
      const knownLive = status?.trading_mode === 'live'
      const parts = [
        `Scaled BUY ${symbol}`,
        `${nLegs} legs, ${step}% apart`,
        firstAtMarket ? 'first leg at market' : 'all resting limits',
        amt ? `total amount ${amt}` : 'auto-sized by risk',
      ]
      if (sl) parts.push(`stop-loss ${sl}`)
      if (tp) parts.push(`take-profit ${tp}`)
      const header = knownLive
        ? 'LIVE SCALED ORDER — this uses real funds on your Binance account.'
        : 'Trading mode not confirmed yet — this MAY place REAL orders on your Binance account.'
      if (!window.confirm(`${header}\n\n${parts.join('  ·  ')}\n\nPlace this scaled entry?`)) return
    }
    setScaling(true)
    try {
      const res = await api.scaledOrder({
        symbol,
        amount: amt,
        legs: nLegs,
        step_pct: step,
        first_at_market: firstAtMarket,
        stop_loss: sl,
        take_profit: tp,
      })
      showToast(res.accepted ? 'ok' : 'error', res.message)
      refreshTrades()
    } catch (e) {
      showToast('error', (e as Error).message)
    } finally {
      setScaling(false)
    }
  }

  const doCloseAll = async () => {
    if (closingAll) return // one bulk-close at a time
    // Same conservative gate as closeTrade: confirm unless we KNOW it's paper.
    if (status?.trading_mode !== 'paper') {
      const msg =
        status?.trading_mode === 'live'
          ? `Close ALL LIVE ${symbol} positions and cancel any resting orders at market now?`
          : `Trading mode not confirmed yet — this may close REAL ${symbol} positions at market. Continue?`
      if (!window.confirm(msg)) return
    }
    setClosingAll(true)
    try {
      const res = await api.closeAll(symbol)
      showToast(res.closed > 0 ? 'ok' : 'error', res.message)
      refreshTrades()
    } catch (e) {
      showToast('error', (e as Error).message)
    } finally {
      setClosingAll(false)
    }
  }

  const toggleBot = async () => {
    if (!status) return
    try {
      const res = await api.setBot(status.running ? 'stop' : 'start')
      // Functional update: a fresh status may have arrived over the WS while
      // the request was in flight, so merge onto the latest, not the closure's
      // snapshot, and only touch `running`.
      setStatus((prev) => (prev ? { ...prev, running: res.running } : prev))
    } catch (e) {
      showToast('error', (e as Error).message)
    }
  }

  const pnlClass = (n: number) => (n > 0 ? 'pos' : n < 0 ? 'neg' : '')

  // Live price for the header readout + the chart's forming bar. Prefer the
  // real-time stream when it's live (sub-second, exchange-grade); otherwise the
  // polled ticker; and fall back to the newest candle close until either
  // arrives. Never a fabricated number — only real feeds, in order of freshness.
  const streamingLive = liveBinance && stream.streaming
  const livePrice =
    (streamingLive && stream.price != null && stream.price > 0 ? stream.price : null) ??
    ticker?.last ??
    (candles.length ? candles[candles.length - 1].close : null)
  const chgPct = ticker?.percentage ?? null

  return (
    <div className="app">
      {/* Slide-in navigation drawer + click-away backdrop. The hamburger in the
         topbar toggles `menuOpen`; picking an item sets the tab and closes it. */}
      <div
        className={`drawer-backdrop ${menuOpen ? 'show' : ''}`}
        onClick={() => setMenuOpen(false)}
      />
      <aside className={`drawer ${menuOpen ? 'open' : ''}`} aria-hidden={!menuOpen}>
        <div className="drawer-head">
          <div className="brand" style={{ fontSize: 16 }}>
            <span className="dot" />
            Tranding-track
          </div>
          <button
            className="drawer-close"
            onClick={() => setMenuOpen(false)}
            aria-label="Close menu"
            type="button"
          >
            ✕
          </button>
        </div>
        <nav className="drawer-nav">
          {NAV.filter((n) => !n.admin || me.role === 'admin').map((n) => (
            <button
              key={n.key}
              className={`drawer-item ${tab === n.key ? 'active' : ''}`}
              onClick={() => {
                setTab(n.key)
                setMenuOpen(false)
              }}
              type="button"
            >
              <span className="drawer-ico">{n.icon}</span>
              <span>{n.label}</span>
            </button>
          ))}
        </nav>
      </aside>

      <header className="topbar">
        <button
          className={`hamburger ${menuOpen ? 'open' : ''}`}
          onClick={() => setMenuOpen((v) => !v)}
          aria-label="Toggle menu"
          aria-expanded={menuOpen}
          type="button"
        >
          <span />
          <span />
          <span />
        </button>
        <div className="brand">
          <span className="dot" />
          Tranding-track
        </div>
        {status && (
          <>
            <span className={`badge ${status.trading_mode === 'live' ? 'live' : 'paper'}`}>
              {status.trading_mode}
            </span>
            {status.testnet && <span className="badge">testnet</span>}
            <span className={`badge ${status.running ? 'on' : 'off'}`}>
              {status.running ? 'running' : 'stopped'}
            </span>
          </>
        )}
        <div className="spacer" />
        <span className="hint">{me.email}</span>
        {me.role === 'admin' && <span className="badge">admin</span>}
        <span className="hint">{connected ? 'live' : 'reconnecting…'}</span>
        <span className={`ws-dot ${connected ? 'connected' : ''}`} />
        <NotificationsBell
          items={notifs}
          unread={notifUnread}
          onOpen={() => setNotifUnread(0)}
          onClear={() => {
            setNotifs([])
            setNotifUnread(0)
          }}
        />
        <ThemeToggle theme={theme} onToggle={onToggleTheme} />
        <button className="btn primary" onClick={toggleBot}>
          {status?.running ? 'Stop bot' : 'Start bot'}
        </button>
        <button className="btn" onClick={onLogout}>
          Sign out
        </button>
      </header>

      {/* Slim, always-on status bar: the AI provider + exchange connection live
          here so they show on every tab, not buried inside the chat panel. */}
      <ConnectionBar
        aiHealth={aiHealth}
        aiHealthLoading={aiHealthLoading}
        onRecheckAi={loadAiHealth}
        access={access}
        testing={testingAccess}
        onTest={testAccess}
        onFix={() => setTab('settings')}
      />

      {access && status?.trading_mode === 'live' && !access.ok && (
        <div className="alert-banner">
          <span className="alert-icon">⚠️</span>
          <div>
            <b>Live trading is not ready.</b> {access.detail}
            {' '}Orders will be rejected until the exchange key can trade. Paper mode is unaffected.
          </div>
        </div>
      )}

      <div className="body">
        <div className="col">
          <StatsRow status={status} />

          <BotPulse
            status={status}
            statusTs={statusTs}
            signals={signals}
            settings={settings}
            connected={connected}
          />

          <AutonomyToggle
            settings={settings}
            status={status}
            onSaved={(s) => setSettings(s)}
            onError={(m) => showToast('error', m)}
          />

          <section className="panel">
            <div className="panel-head">
              <div className="price-ticker">
                <span>Price</span>
                {livePrice != null && (
                  <>
                    <span className="last" style={tickerStale ? { opacity: 0.5 } : undefined}>
                      {fmt(livePrice, livePrice < 10 ? 4 : 2)}
                    </span>
                    {chgPct != null && !tickerStale && (
                      <span className={`chg ${pnlClass(chgPct)}`}>
                        {chgPct > 0 ? '+' : ''}
                        {fmt(chgPct, 2)}%
                      </span>
                    )}
                    {tickerStale && (
                      <span
                        className="hint"
                        title="Live price feed interrupted — showing the last known price, not a current quote"
                      >
                        ⚠ stale
                      </span>
                    )}
                    {/* Honest source label: when the primary exchange is
                        geo-blocked, market data is served from the configured
                        public fallback venue. Show it so the price is never
                        implied to come from somewhere it didn't. */}
                    {ticker?.source &&
                      access?.exchange &&
                      ticker.source !== access.exchange && (
                        <span
                          className="hint"
                          title={`${access.exchange} market data is unavailable here (e.g. geo-blocked); this price is served from the public fallback ${ticker.source}. Orders and balances still use ${access.exchange}.`}
                        >
                          via {ticker.source}
                        </span>
                      )}
                  </>
                )}
              </div>
              <div className="row" style={{ alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                <SymbolPicker value={symbol} symbols={symbolList} onChange={setSymbol} />
                <select
                  className="select"
                  value={timeframe}
                  onChange={(e) => setTimeframe(e.target.value)}
                >
                  {TIMEFRAMES.map((t) => (
                    <option key={t}>{t}</option>
                  ))}
                </select>
                {chartView === 'bot' && (
                  <IndicatorsMenu value={indicators} onChange={setIndicators} />
                )}
                {chartView === 'bot' && (
                  <div className="chart-view-toggle" role="group" aria-label="Trade arrows">
                    <button
                      type="button"
                      className={`cvt-btn${showTradeMarkers ? ' active' : ''}`}
                      aria-pressed={showTradeMarkers}
                      onClick={() => setShowTradeMarkers((v) => !v)}
                      title={
                        showTradeMarkers
                          ? 'Hide the buy/sell trade arrows on the chart'
                          : 'Show buy/sell arrows on the bars where your own trades opened and closed'
                      }
                    >
                      Trades
                    </button>
                  </div>
                )}
                {chartView === 'bot' && (
                  <div className="chart-view-toggle" role="group" aria-label="Watch the bot think">
                    <button
                      type="button"
                      className={`cvt-btn${watchThinking ? ' active' : ''}`}
                      aria-pressed={watchThinking}
                      onClick={() => setWatchThinking((v) => !v)}
                      title={
                        watchThinking
                          ? 'Stop auto-showing the bot’s live decisions on the chart'
                          : 'When the bot makes an autonomous call, briefly jump the chart to that symbol and light up the indicators behind it, then revert. View only — moves no money.'
                      }
                    >
                      🧠 Watch
                    </button>
                  </div>
                )}
                <div className="chart-view-toggle" role="tablist" aria-label="Chart view">
                  <button
                    type="button"
                    className={`cvt-btn${chartView === 'bot' ? ' active' : ''}`}
                    onClick={() => setChartView('bot')}
                    title="Your bot's chart on real data — your trades, stops and alerts marked"
                  >
                    Bot chart
                  </button>
                  <button
                    type="button"
                    className={`cvt-btn${chartView === 'tv' ? ' active' : ''}`}
                    onClick={() => setChartView('tv')}
                    title="The full TradingView chart: every drawing tool and indicator (live market data)"
                  >
                    TradingView
                  </button>
                </div>
              </div>
            </div>
            <div className="panel-body">
              <MarketStats
                ticker={ticker}
                symbol={symbol}
                stale={tickerStale && !streamingLive}
                live={streamingLive}
              />
              {chartView === 'bot' && thinkingInfo && (
                <div className="think-banner" role="status" aria-live="polite">
                  <span className="tb-live">
                    <span className="tb-dot" aria-hidden="true" />
                    Bot decided
                  </span>
                  <span>
                    <b>{thinkingInfo.symbol}</b> · {thinkingInfo.timeframe} →{' '}
                    <b className={`tb-verdict ${thinkingInfo.verdict}`}>
                      {thinkingInfo.verdict.toUpperCase()}
                    </b>
                    {thinkingInfo.confidence !== null &&
                      ` · ${Math.round(thinkingInfo.confidence * 100)}% confident`}
                  </span>
                  {thinkingInfo.factors.length > 0 && (
                    <span className="think-factors">
                      {thinkingInfo.factors
                        .filter((f) => f.signal !== 'hold' || f.weight !== 0)
                        .slice(0, 7)
                        .map((f) => (
                          <span key={f.name} className={`think-chip ${f.signal}`}>
                            {f.name}
                            {f.signal === 'buy' ? ' ▲' : f.signal === 'sell' ? ' ▼' : ''}
                          </span>
                        ))}
                    </span>
                  )}
                  <span className="think-note">
                    {thinkingInfo.acted ? 'acted' : 'no trade'} · view only, auto-reverts
                  </span>
                </div>
              )}
              {chartView === 'tv' ? (
                <TradingViewChart symbol={symbol} timeframe={timeframe} theme={theme} />
              ) : candles.length ? (
                <PriceChart
                  candles={candles}
                  theme={theme}
                  last={livePrice}
                  liveBar={streamingLive ? stream.candle : null}
                  fitKey={`${symbol}:${timeframe}`}
                  symbol={symbol}
                  timeframe={timeframe}
                  priceLines={chartPriceLines}
                  indicators={indicators}
                  markers={showTradeMarkers ? chartMarkers : []}
                  clearSignal={chartClearSignal}
                />
              ) : (
                <div className="empty">
                  No candle data. Check the backend / Binance connection.
                </div>
              )}
            </div>
          </section>

          <OrderBook
            symbol={symbol}
            exchange={access?.exchange}
            liveBook={streamingLive ? stream.book : null}
            streaming={streamingLive}
          />

          <AlertsPanel
            symbol={symbol}
            alerts={alerts}
            lastPrice={livePrice ?? ticker?.last ?? null}
            onChanged={refreshAlerts}
            onError={(m) => showToast('error', m)}
          />

          <section className="panel">
            <div className="panel-head">Manual order</div>
            <div className="panel-body">
              <div className="row">
                <div className="field">
                  <label>Symbol</label>
                  <input className="input" value={symbol} readOnly />
                </div>
                <div className="field">
                  <label>{scaleIn ? 'Total amount across all legs (blank = auto)' : 'Amount (blank = auto-size by risk)'}</label>
                  <input
                    className="input"
                    placeholder="auto"
                    value={amount}
                    onChange={(e) => setAmount(e.target.value)}
                    inputMode="decimal"
                  />
                </div>
                <div className="field">
                  <label>{scaleIn ? 'Limit price (set by ladder)' : 'Limit price (blank = market)'}</label>
                  <input
                    className="input"
                    placeholder={scaleIn ? 'stepped per leg' : 'market'}
                    value={scaleIn ? '' : limitPrice}
                    onChange={(e) => setLimitPrice(e.target.value)}
                    inputMode="decimal"
                    disabled={scaleIn}
                  />
                </div>
                <div className="field">
                  <label>Stop-loss price (optional)</label>
                  <input
                    className="input"
                    placeholder="auto"
                    value={stopLoss}
                    onChange={(e) => setStopLoss(e.target.value)}
                    inputMode="decimal"
                  />
                </div>
                <div className="field">
                  <label>Take-profit price (optional)</label>
                  <input
                    className="input"
                    placeholder="auto"
                    value={takeProfit}
                    onChange={(e) => setTakeProfit(e.target.value)}
                    inputMode="decimal"
                  />
                </div>
                {scaleIn && (
                  <>
                    <div className="field">
                      <label>Legs (2–20)</label>
                      <input
                        className="input"
                        placeholder="3"
                        value={legs}
                        onChange={(e) => setLegs(e.target.value)}
                        inputMode="numeric"
                      />
                    </div>
                    <div className="field">
                      <label>Step % between legs</label>
                      <input
                        className="input"
                        placeholder="1"
                        value={stepPct}
                        onChange={(e) => setStepPct(e.target.value)}
                        inputMode="decimal"
                      />
                    </div>
                  </>
                )}
                {scaleIn ? (
                  <button className="btn buy" onClick={doScaledOrder} disabled={scaling}>
                    {scaling ? 'Placing…' : 'Scaled buy'}
                  </button>
                ) : (
                  <>
                    <button className="btn buy" onClick={() => doOrder('buy')} disabled={placing}>
                      {placing ? 'Placing…' : 'Buy'}
                    </button>
                    <button className="btn sell" onClick={() => doOrder('sell')} disabled={placing}>
                      {placing ? 'Placing…' : 'Sell'}
                    </button>
                  </>
                )}
                <button className="btn ghost" onClick={doCloseAll} disabled={closingAll}>
                  {closingAll ? 'Closing…' : `Close all ${symbol}`}
                </button>
              </div>
              <div className="row" style={{ marginTop: 8 }}>
                <label className="check">
                  <input
                    type="checkbox"
                    checked={scaleIn}
                    onChange={(e) => setScaleIn(e.target.checked)}
                  />
                  Scale in (DCA) — split one buy into a ladder of legs
                </label>
                {scaleIn && (
                  <label className="check">
                    <input
                      type="checkbox"
                      checked={firstAtMarket}
                      onChange={(e) => setFirstAtMarket(e.target.checked)}
                    />
                    First leg at market (rest are resting limits)
                  </label>
                )}
              </div>
              <p className="hint" style={{ marginTop: 10 }}>
                Orders respect your risk settings. In <b>paper</b> mode nothing hits the exchange;
                in <b>live</b> mode you'll be asked to confirm before real funds are used. Set a
                <b> limit price</b> to rest the order until the market reaches it (a buy fills at or
                below it, a sell at or above it); leave it blank for an immediate market order. A
                blank <b>stop-loss</b>/<b>take-profit</b> uses your configured default percentages.
                <br />
                <b>Scale in (DCA)</b> splits a single buy into a ladder of legs stepped below the
                current price. The <b>total</b> is sized once by your risk manager, then divided
                equally — so a ladder never risks more than one entry. The first leg can fill at
                market; the rest rest as limit orders and each filled leg gets its own
                stop-loss/take-profit. <b>Close all {symbol}</b> exits every open position and
                cancels every resting leg for the symbol in one click.
              </p>
            </div>
          </section>
        </div>

        <div className="col">
          <section className="panel">
            <div className="panel-head">
              <div className="tabs">
                <span
                  className={`tab ${tab === 'trades' ? 'active' : ''}`}
                  onClick={() => setTab('trades')}
                >
                  Trades
                </span>
                <span
                  className={`tab ${tab === 'performance' ? 'active' : ''}`}
                  onClick={() => setTab('performance')}
                >
                  Performance
                </span>
                <span
                  className={`tab ${tab === 'signals' ? 'active' : ''}`}
                  onClick={() => setTab('signals')}
                >
                  Signals
                </span>
                <span
                  className={`tab ${tab === 'assistant' ? 'active' : ''}`}
                  onClick={() => setTab('assistant')}
                >
                  AI Assistant
                </span>
                <span
                  className={`tab ${tab === 'news' ? 'active' : ''}`}
                  onClick={() => setTab('news')}
                >
                  News
                </span>
                <span
                  className={`tab ${tab === 'analyze' ? 'active' : ''}`}
                  onClick={() => setTab('analyze')}
                >
                  Analyze
                </span>
                <span
                  className={`tab ${tab === 'train' ? 'active' : ''}`}
                  onClick={() => setTab('train')}
                >
                  Train
                </span>
                <span
                  className={`tab ${tab === 'backtest' ? 'active' : ''}`}
                  onClick={() => setTab('backtest')}
                >
                  Backtest
                </span>
                <span
                  className={`tab ${tab === 'settings' ? 'active' : ''}`}
                  onClick={() => setTab('settings')}
                >
                  Settings
                </span>
                {me.role === 'admin' && (
                  <span
                    className={`tab ${tab === 'admin' ? 'active' : ''}`}
                    onClick={() => setTab('admin')}
                  >
                    Admin
                  </span>
                )}
              </div>
              {/* On phones the tab strip is hidden (the hamburger drawer is the
                 nav); show just the current view's name so context isn't lost. */}
              <div className="tab-current">{NAV_LABEL[tab]}</div>
            </div>
            <div className="panel-body">
              {tab === 'trades' && (
                <TradesTable
                  trades={trades}
                  openTrades={openTrades}
                  onClose={closeTrade}
                  pnlClass={pnlClass}
                  closingId={closing}
                />
              )}
              {tab === 'signals' && <SignalsTable signals={signals} />}
              {tab === 'performance' && (
                <PerformancePanel onError={(m) => showToast('error', m)} />
              )}
              {tab === 'assistant' && (
                <AssistantPanel
                  symbol={symbol}
                  timeframe={timeframe}
                  tradingMode={status?.trading_mode}
                  turns={turns}
                  setTurns={setTurns}
                  readAloud={readAloud}
                  onReadAloudChange={setReadAloud}
                  ttsSupported={ttsSupported}
                  speak={speak}
                  onNavigate={setTab}
                  onChartControl={applyChartControl}
                  onError={(m) => showToast('error', m)}
                />
              )}
              {tab === 'news' && <NewsPanel onError={(m) => showToast('error', m)} />}
              {tab === 'analyze' && (
                <AnalyzePanel
                  symbol={symbol}
                  timeframe={timeframe}
                  onError={(m) => showToast('error', m)}
                />
              )}
              {tab === 'train' && (
                <TrainPanel symbol={symbol} timeframe={timeframe} onError={(m) => showToast('error', m)} />
              )}
              {tab === 'backtest' && (
                <BacktestPanel symbol={symbol} timeframe={timeframe} onError={(m) => showToast('error', m)} />
              )}
              {tab === 'settings' && (
                <SettingsPanel
                  settings={settings}
                  loadError={settingsError}
                  onReload={loadSettings}
                  access={access}
                  onRefreshAccess={refreshAccess}
                  me={me}
                  onSaved={(s) => {
                    setSettings(s)
                    showToast('ok', 'Settings saved')
                  }}
                  onMeChanged={onMeChanged}
                  onError={(msg) => showToast('error', msg)}
                  onResetPaper={resetPaper}
                />
              )}
              {tab === 'admin' && me.role === 'admin' && (
                <Admin onError={(msg) => showToast('error', msg)} />
              )}
            </div>
          </section>
        </div>
      </div>

      {toast && <div className={`toast ${toast.kind}`}>{toast.text}</div>}
    </div>
  )
}

// Human-friendly "x minutes ago" for the notifications feed. Recomputed on each
// render (no ticking timer) — good enough for a dropdown.
function relativeTime(ts: number): string {
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000))
  if (s < 60) return 'just now'
  const m = Math.round(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.round(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.round(h / 24)}d ago`
}

// Slim, always-visible status strip under the header: the built-in AI provider's
// real reachability and the exchange connection, each with a concrete state and
// a one-click action. Lifted out of the AI chat panel so it shows on every tab.
function ConnectionBar({
  aiHealth,
  aiHealthLoading,
  onRecheckAi,
  access,
  testing,
  onTest,
  onFix,
}: {
  aiHealth: AiHealth | null
  aiHealthLoading: boolean
  onRecheckAi: () => void
  access: ExchangeAccess | null
  testing: boolean
  onTest: () => void
  onFix: () => void
}) {
  return (
    <div className="conn-bar">
      <div
        className={`conn-pill ${aiHealth ? (aiHealth.ok ? 'ok' : 'bad') : 'muted'}`}
        title={aiHealth?.detail || ''}
      >
        <span className="conn-dot" />
        <span className="conn-label">Assistant AI</span>
        <span className="conn-state">
          {aiHealthLoading
            ? 'checking…'
            : aiHealth
              ? aiHealth.ok
                ? `connected (${aiHealth.model || 'model'})`
                : `not working — ${aiHealth.detail}`
              : 'status unknown'}
        </span>
        <button
          type="button"
          className="btn ghost sm"
          onClick={onRecheckAi}
          disabled={aiHealthLoading}
          title="Re-check the AI provider"
        >
          {aiHealthLoading ? '…' : '↻'}
        </button>
      </div>
      <div
        className={`conn-pill ${access ? (access.ok ? 'ok' : 'bad') : 'muted'}`}
        title={access?.detail || ''}
      >
        <span className="conn-dot" />
        <span className="conn-label">Exchange</span>
        <span className="conn-state">
          {access
            ? access.can_trade
              ? `trade-ready on ${access.exchange ?? 'exchange'} (${access.testnet ? 'testnet' : 'live'})`
              : access.can_read_account
                ? 'connected, read-only (can’t trade yet)'
                : access.can_read_public
                  ? 'public data only — not signed in'
                  : 'not connected'
            : 'not tested'}
        </span>
        <button
          type="button"
          className="btn ghost sm"
          onClick={onTest}
          disabled={testing}
          title="Test the exchange connection now"
        >
          {testing ? '…' : 'Test'}
        </button>
        {access && !access.can_trade && (
          <button
            type="button"
            className="btn sm"
            onClick={onFix}
            title="Open Settings to add keys / fix the connection"
          >
            Fix in Settings →
          </button>
        )}
      </div>
    </div>
  )
}

// Header notifications: a 🔔 with an unread badge and a dropdown feed of recent
// alerts/pings (fed from every showToast). Opening it clears the unread count;
// transient toasts still flash for live events.
function NotificationsBell({
  items,
  unread,
  onOpen,
  onClear,
}: {
  items: Notif[]
  unread: number
  onOpen: () => void
  onClear: () => void
}) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement | null>(null)

  // Close on outside-click / Escape, like a normal popover menu.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open])

  const toggle = () => {
    setOpen((v) => {
      if (!v) onOpen() // opening marks everything read
      return !v
    })
  }

  return (
    <div className="notif-wrap" ref={wrapRef}>
      <button
        type="button"
        className="notif-bell"
        onClick={toggle}
        aria-label={`Notifications${unread ? ` (${unread} unread)` : ''}`}
        aria-expanded={open}
        title="Notifications"
      >
        🔔
        {unread > 0 && <span className="notif-badge">{unread > 99 ? '99+' : unread}</span>}
      </button>
      {open && (
        <div className="notif-dropdown" role="menu">
          <div className="notif-head">
            <span>Notifications</span>
            {items.length > 0 && (
              <button type="button" className="btn ghost sm" onClick={onClear}>
                Clear
              </button>
            )}
          </div>
          {items.length === 0 ? (
            <div className="notif-empty">
              No notifications yet. Bot pings and alerts will show up here.
            </div>
          ) : (
            <div className="notif-list">
              {items.map((n) => (
                <div key={n.id} className={`notif-item ${n.kind}`}>
                  <span className="n-dot" />
                  <div className="notif-body">
                    <div>{n.text}</div>
                    <div className="notif-time">{relativeTime(n.ts)}</div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// Compact number (1.23K / 4.56M / 7.89B) for volumes and order sizes.
function compact(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return '—'
  const a = Math.abs(n)
  if (a >= 1e9) return (n / 1e9).toFixed(2) + 'B'
  if (a >= 1e6) return (n / 1e6).toFixed(2) + 'M'
  if (a >= 1e3) return (n / 1e3).toFixed(2) + 'K'
  return n.toFixed(a > 0 && a < 1 ? 4 : 2)
}

// Price with a sensible number of decimals for its magnitude.
function fmtPx(v: number): string {
  const dp = v < 1 ? 6 : v < 10 ? 4 : 2
  return v.toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp })
}

// Short "time since" for the activity heartbeat. Truthful, not decorative.
function ago(ts: number): string {
  if (!ts) return 'never'
  const s = Math.max(0, Math.floor((Date.now() - ts) / 1000))
  if (s < 3) return 'just now'
  if (s < 60) return `${s}s ago`
  if (s < 3600) return `${Math.floor(s / 60)}m ago`
  return `${Math.floor(s / 3600)}h ago`
}

// Live bid/ask + 24h volume strip above the chart. Every figure is only shown
// when the venue actually reported it — no fabricated depth or volume.
function MarketStats({
  ticker,
  symbol,
  stale,
  live,
}: {
  ticker: Ticker | null
  symbol: string
  stale: boolean
  live?: boolean
}) {
  const [base, quote] = symbol.split('/')
  const bid = ticker?.bid ?? null
  const ask = ticker?.ask ?? null
  const spread = bid != null && ask != null && ask > 0 ? ask - bid : null
  const spreadPct = spread != null && ask ? (spread / ask) * 100 : null
  return (
    <div className={`market-stats ${stale ? 'stale' : ''}`}>
      {live && (
        <div className="ms-item">
          <span className="ms-k">Feed</span>
          <span className="ms-v live-pill" title="Streaming live from Binance's public websocket — sub-second, same as an exchange chart">
            ● LIVE
          </span>
        </div>
      )}
      <div className="ms-item">
        <span className="ms-k">Bid</span>
        <span className="ms-v buy" title="Highest resting buy order — you sell into this">
          {bid != null ? fmtPx(bid) : '—'}
        </span>
      </div>
      <div className="ms-item">
        <span className="ms-k">Ask</span>
        <span className="ms-v sell" title="Lowest resting sell order — you buy at this">
          {ask != null ? fmtPx(ask) : '—'}
        </span>
      </div>
      <div className="ms-item">
        <span className="ms-k">Spread</span>
        <span className="ms-v">
          {spread != null ? `${fmtPx(spread)}${spreadPct != null ? ` (${spreadPct.toFixed(3)}%)` : ''}` : '—'}
        </span>
      </div>
      <div className="ms-item">
        <span className="ms-k">24h Vol</span>
        <span className="ms-v" title="24h traded volume reported by the venue">
          {ticker?.base_volume != null ? `${compact(ticker.base_volume)} ${base}` : '—'}
          {ticker?.quote_volume != null ? ` · ${compact(ticker.quote_volume)} ${quote}` : ''}
        </span>
      </div>
    </div>
  )
}
// Running cumulative QUOTE VALUE (price × amount) of the order book from the top
// outward, so each level shows the total resting liquidity in the quote currency
// (USDT for a USDT pair — i.e. dollars) up to and including it. Quote value reads
// far easier than tiny base-coin amounts, and it's a straight multiply of the
// venue's own real price and size — nothing invented.
function cumulativeValue(levels: { price: number; amount: number }[]): number[] {
  const out: number[] = []
  let run = 0
  for (const l of levels) {
    run += Number.isFinite(l.price) && Number.isFinite(l.amount) ? l.price * l.amount : 0
    out.push(run)
  }
  return out
}

// Market picker: click to open a searchable dropdown of the REAL live Binance
// markets (from /api/symbols) and pick one — no need to type a pair by hand.
// Typing in the search box only FILTERS that live list; if you search something
// not yet in it (a brand-new listing), pressing Enter still accepts it as a pair
// (adding a /USDT quote when none is given) so you're never blocked. Nothing here
// is fabricated — the list is exactly what the exchange reports.
// The price-overlay indicators offered on the bot chart, with the exact line
// colour each draws in (kept in sync with PriceChart) so the menu swatch matches
// the chart. All are real math on the chart's own candles.
const INDICATOR_DEFS: { key: keyof IndicatorPrefs; label: string; color: string }[] = [
  { key: 'ema9', label: 'EMA 9', color: '#f0b90b' },
  { key: 'ema21', label: 'EMA 21', color: '#3b82f6' },
  { key: 'sma50', label: 'SMA 50', color: '#a855f7' },
  { key: 'sma200', label: 'SMA 200', color: '#9aa7b8' },
  { key: 'bb', label: 'Bollinger Bands (20, 2)', color: 'rgba(120,144,180,0.95)' },
  { key: 'vwap', label: 'VWAP (loaded range)', color: '#e6c200' },
]

// Oscillators that draw in their OWN pane under price (their y-scale isn't the
// price), so they're offered as a separate group. Same rule: real math on the
// chart's candles — RSI(14) and MACD(12,26,9), nothing fabricated.
const OSCILLATOR_DEFS: { key: keyof IndicatorPrefs; label: string; color: string }[] = [
  { key: 'rsi', label: 'RSI (14)', color: '#d1a1ff' },
  { key: 'macd', label: 'MACD (12, 26, 9)', color: '#3b82f6' },
]

// Volume visualisations. `volume` is the bar histogram along the bottom (on by
// default); `volumeProfile` is the horizontal VPVR histogram up the right edge
// showing how much real volume traded at each price. Both are summed from the
// chart's own candles — nothing fabricated.
const VOLUME_DEFS: { key: keyof IndicatorPrefs; label: string; color: string }[] = [
  { key: 'volume', label: 'Volume (bars)', color: '#5b8def' },
  { key: 'volumeProfile', label: 'Volume Profile (VPVR)', color: '#f0b90b' },
]

// TradingView-style "Indicators" dropdown for the bot chart: tick the moving
// averages / bands / VWAP to overlay. The choice is saved (localStorage) by the
// parent, so it persists like a saved layout. Every overlay is computed from the
// real candles — nothing here fabricates a line.
function IndicatorsMenu({
  value,
  onChange,
}: {
  value: IndicatorPrefs
  onChange: (v: IndicatorPrefs) => void
}) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onDoc)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])
  const count =
    INDICATOR_DEFS.filter((d) => value[d.key]).length +
    OSCILLATOR_DEFS.filter((d) => value[d.key]).length +
    VOLUME_DEFS.filter((d) => value[d.key]).length
  return (
    <div className="ind-menu" ref={ref}>
      <button
        type="button"
        className="ind-btn"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="true"
        aria-expanded={open}
        title="Add real indicators to the bot chart"
      >
        Indicators{count ? ` (${count})` : ''}
        <span className="ind-caret">▾</span>
      </button>
      {open && (
        <div className="ind-panel">
          {INDICATOR_DEFS.map((d) => (
            <label key={d.key} className="ind-row">
              <input
                type="checkbox"
                checked={value[d.key]}
                onChange={(e) => onChange({ ...value, [d.key]: e.target.checked })}
              />
              <span className="ind-swatch" style={{ background: d.color }} />
              <span className="ind-label">{d.label}</span>
            </label>
          ))}
          <div className="ind-group">Oscillators · own pane</div>
          {OSCILLATOR_DEFS.map((d) => (
            <label key={d.key} className="ind-row">
              <input
                type="checkbox"
                checked={value[d.key]}
                onChange={(e) => onChange({ ...value, [d.key]: e.target.checked })}
              />
              <span className="ind-swatch" style={{ background: d.color }} />
              <span className="ind-label">{d.label}</span>
            </label>
          ))}
          <div className="ind-group">Volume</div>
          {VOLUME_DEFS.map((d) => (
            <label key={d.key} className="ind-row">
              <input
                type="checkbox"
                checked={value[d.key]}
                onChange={(e) => onChange({ ...value, [d.key]: e.target.checked })}
              />
              <span className="ind-swatch" style={{ background: d.color }} />
              <span className="ind-label">{d.label}</span>
            </label>
          ))}
          {count > 0 && (
            <button
              type="button"
              className="ind-clear"
              onClick={() => onChange({ ...DEFAULT_INDICATORS })}
            >
              Clear all
            </button>
          )}
        </div>
      )}
    </div>
  )
}

function SymbolPicker({
  value,
  symbols,
  onChange,
}: {
  value: string
  symbols: string[]
  onChange: (s: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const wrapRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  // Close when clicking outside the menu.
  useEffect(() => {
    if (!open) return
    const onDoc = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [open])

  // Fresh, focused filter each time it opens so you can type-to-narrow instantly
  // — but never have to; the full market list is right there to click.
  useEffect(() => {
    if (open) {
      setQuery('')
      const t = setTimeout(() => inputRef.current?.focus(), 0)
      return () => clearTimeout(t)
    }
  }, [open])

  const q = query.trim().toUpperCase()
  const matches = useMemo(
    () => (q ? symbols.filter((s) => s.toUpperCase().includes(q)) : symbols),
    [q, symbols],
  )
  const CAP = 200
  const shown = matches.slice(0, CAP)

  const pick = (s: string) => {
    onChange(s)
    setOpen(false)
  }
  const onKeyDown = (e: ReactKeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      if (matches.length) {
        pick(matches[0])
        return
      }
      let s = q
      if (s) {
        if (!s.includes('/')) s = `${s}/USDT`
        pick(s)
      }
    } else if (e.key === 'Escape') {
      setOpen(false)
    }
  }

  return (
    <div className="sym-picker" ref={wrapRef}>
      <button
        type="button"
        className="sym-picker-btn"
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        title="Choose a market"
      >
        <span className="sym-picker-val">{value}</span>
        <span className="sym-picker-caret">▾</span>
      </button>
      {open && (
        <div className="sym-picker-menu" role="listbox">
          <input
            ref={inputRef}
            className="input sym-picker-search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={`Search ${symbols.length} live markets…`}
            spellCheck={false}
            autoComplete="off"
            aria-label="Search markets"
          />
          <div className="sym-picker-list">
            {shown.length === 0 ? (
              <div className="sym-picker-empty">
                No market matches “{query}”. Press Enter to use it as a pair.
              </div>
            ) : (
              shown.map((s) => (
                <button
                  type="button"
                  key={s}
                  className={`sym-picker-item ${s === value ? 'active' : ''}`}
                  role="option"
                  aria-selected={s === value}
                  onClick={() => pick(s)}
                >
                  {s}
                </button>
              ))
            )}
          </div>
          {matches.length > CAP && (
            <div className="sym-picker-more">
              {matches.length - CAP} more — keep typing to narrow.
            </div>
          )}
        </div>
      )}
    </div>
  )
}
// Live order-book depth: the market's REAL resting bids (buy side) and asks
// (sell side). When a real-time depth stream is available it's used directly
// (100ms, exchange-grade); otherwise REST is polled every 2.5s. Each side shows
// price, the level's VALUE in the quote currency (price × amount — USDT/dollars
// for a USDT pair, far easier to read than fractional coin amounts), and a
// running Total (cumulative quote value from the top of book outward), with
// depth bars scaled to that cumulative value. Empty sides mean the venue
// returned no depth — never invented.
function OrderBook({
  symbol,
  exchange,
  liveBook,
  streaming,
}: {
  symbol: string
  exchange?: string
  liveBook?: OrderBookData | null
  streaming?: boolean
}) {
  const [book, setBook] = useState<OrderBookData | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    // While the live websocket is feeding depth, don't also poll REST — the
    // stream is fresher (100ms) and polling would only fight it.
    if (streaming) {
      setErr(null)
      return
    }
    let alive = true
    setBook(null)
    setErr(null)
    const load = () =>
      api
        .orderbook(symbol, 20)
        .then((b) => {
          if (alive) {
            setBook(b)
            setErr(null)
          }
        })
        .catch((e) => {
          if (alive) setErr(e?.message || 'unavailable')
        })
    load()
    const id = setInterval(load, 2500)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [symbol, streaming])

  // Prefer the live streamed book when streaming; otherwise the polled book.
  const active = streaming ? liveBook ?? null : book
  const LEVELS = 12
  const asks = (active?.asks ?? []).slice(0, LEVELS)
  const bids = (active?.bids ?? []).slice(0, LEVELS)
  // Sizes shown as quote VALUE (price × amount) — USDT/dollars for a USDT pair.
  const quote = symbol.split('/')[1] || ''
  const askCum = cumulativeValue(asks)
  const bidCum = cumulativeValue(bids)
  const maxCum = Math.max(
    1e-9,
    askCum[askCum.length - 1] ?? 0,
    bidCum[bidCum.length - 1] ?? 0,
  )
  const bestAsk = active?.asks?.[0]?.price
  const bestBid = active?.bids?.[0]?.price
  const spread = bestAsk != null && bestBid != null ? bestAsk - bestBid : null
  const spreadPct = spread != null && bestAsk ? (spread / bestAsk) * 100 : null
  const viaFallback = Boolean(active?.source && exchange && active.source !== exchange)
  return (
    <section className="panel orderbook">
      <div className="panel-head">
        <span>Order book</span>
        <div className="row" style={{ alignItems: 'center', gap: 8 }}>
          {streaming && (
            <span className="live-pill" title="Streaming live depth at 100ms from Binance's public websocket">
              ● LIVE
            </span>
          )}
          {viaFallback && (
            <span className="hint" title={`Depth served from the public fallback ${active?.source}, not ${exchange}.`}>
              via {active?.source}
            </span>
          )}
          {spread != null && (
            <span className="hint">
              Spread {fmtPx(spread)}
              {spreadPct != null ? ` (${spreadPct.toFixed(3)}%)` : ''}
            </span>
          )}
        </div>
      </div>
      <div className="panel-body">
        {!active ? (
          streaming ? (
            <div className="empty">Connecting live order book…</div>
          ) : err ? (
            <div className="empty">Order book unavailable: {err}</div>
          ) : (
            <div className="empty">Loading order book…</div>
          )
        ) : asks.length === 0 && bids.length === 0 ? (
          <div className="empty">No resting orders returned{active.source ? ` by ${active.source}` : ''}.</div>
        ) : (
          <div className="ob">
            <div className="ob-headrow">
              <span>Price</span>
              <span>Amount{quote ? ` (${quote})` : ''}</span>
              <span>Total{quote ? ` (${quote})` : ''}</span>
            </div>
            <div className="ob-side">
              {[...asks].reverse().map((lvl, i) => {
                const orig = asks.length - 1 - i
                const cum = askCum[orig] ?? lvl.price * lvl.amount
                return (
                  <div className="ob-row ask" key={`a${i}`} title={`${compact(lvl.amount)} ${quote ? `${symbol.split('/')[0]} ` : ''}resting at ${fmtPx(lvl.price)} · cumulative ${compact(cum)} ${quote}`}>
                    <div className="ob-depth" style={{ width: `${(cum / maxCum) * 100}%` }} />
                    <span className="ob-price sell">{fmtPx(lvl.price)}</span>
                    <span className="ob-amt">{compact(lvl.price * lvl.amount)}</span>
                    <span className="ob-total">{compact(cum)}</span>
                  </div>
                )
              })}
            </div>
            <div className="ob-spread">
              {bestBid != null && bestAsk != null ? `${fmtPx(bestBid)} — ${fmtPx(bestAsk)}` : '—'}
            </div>
            <div className="ob-side">
              {bids.map((lvl, i) => {
                const cum = bidCum[i] ?? lvl.price * lvl.amount
                return (
                  <div className="ob-row bid" key={`b${i}`} title={`${compact(lvl.amount)} ${quote ? `${symbol.split('/')[0]} ` : ''}resting at ${fmtPx(lvl.price)} · cumulative ${compact(cum)} ${quote}`}>
                    <div className="ob-depth" style={{ width: `${(cum / maxCum) * 100}%` }} />
                    <span className="ob-price buy">{fmtPx(lvl.price)}</span>
                    <span className="ob-amt">{compact(lvl.price * lvl.amount)}</span>
                    <span className="ob-total">{compact(cum)}</span>
                  </div>
                )
              })}
            </div>
          </div>
        )}
      </div>
    </section>
  )
}

// Price alerts for the current symbol: "tell me when SYMBOL crosses PRICE".
// Real, one-shot, server-side alerts the background monitor checks against the
// live price — never fabricated; a fired one flips to "triggered" with the real
// time + price it crossed at. Armed alerts also draw as dashed lines on the
// chart above (see chartPriceLines). Add / arm / delete here.
function AlertsPanel({
  symbol,
  alerts,
  lastPrice,
  onChanged,
  onError,
}: {
  symbol: string
  alerts: Alert[]
  lastPrice?: number | null
  onChanged: () => void
  onError: (msg: string) => void
}) {
  const [condition, setCondition] = useState<'above' | 'below'>('above')
  const [price, setPrice] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)

  const sym = symbol.toUpperCase()
  const mine = alerts.filter((a) => a.symbol.toUpperCase() === sym)
  const others = alerts.length - mine.length

  const add = async () => {
    const p = Number(price)
    if (!price.trim() || !Number.isFinite(p) || p <= 0) {
      onError('Enter a valid alert price above 0.')
      return
    }
    setBusy(true)
    try {
      await api.createAlert({ symbol, condition, price: p, note: note.trim() || null })
      setPrice('')
      setNote('')
      onChanged()
    } catch (e) {
      onError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }
  const remove = async (id: number) => {
    try {
      await api.deleteAlert(id)
      onChanged()
    } catch (e) {
      onError((e as Error).message)
    }
  }

  return (
    <section className="panel alerts">
      <div className="panel-head">
        <span>Price alerts</span>
        {lastPrice != null && <span className="hint">Last {fmtPx(lastPrice)}</span>}
      </div>
      <div className="panel-body">
        <div className="row">
          <div className="field">
            <label>When {sym}</label>
            <select
              className="select"
              value={condition}
              onChange={(e) => setCondition(e.target.value as 'above' | 'below')}
            >
              <option value="above">rises above</option>
              <option value="below">falls below</option>
            </select>
          </div>
          <div className="field">
            <label>Price</label>
            <input
              className="input"
              placeholder={lastPrice != null ? fmtPx(lastPrice) : 'price'}
              value={price}
              onChange={(e) => setPrice(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && add()}
              inputMode="decimal"
            />
          </div>
        </div>
        <div className="field">
          <label>Note (optional)</label>
          <input
            className="input"
            placeholder="e.g. take profit / add here"
            value={note}
            maxLength={200}
            onChange={(e) => setNote(e.target.value)}
          />
        </div>
        <button type="button" className="btn" onClick={add} disabled={busy}>
          {busy ? 'Adding…' : 'Add alert'}
        </button>

        {mine.length === 0 ? (
          <div className="empty">No alerts for {sym} yet.</div>
        ) : (
          <ul className="alert-list">
            {mine.map((a) => (
              <li key={a.id} className={`alert-row ${a.status}`}>
                <div className="alert-main">
                  <span className="alert-cond">
                    {a.condition === 'above' ? '▲' : '▼'} {a.condition} {fmtPx(a.price)}
                  </span>
                  {a.note && <span className="alert-note">{a.note}</span>}
                </div>
                <div className="alert-side">
                  {a.status === 'triggered' ? (
                    <span className="alert-fired" title={a.triggered_at ?? ''}>
                      fired{a.triggered_price != null ? ` @ ${fmtPx(a.triggered_price)}` : ''}
                    </span>
                  ) : (
                    <span className="alert-armed">● armed</span>
                  )}
                  <button
                    type="button"
                    className="btn ghost sm"
                    onClick={() => remove(a.id)}
                    title="Delete this alert"
                  >
                    ✕
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
        {others > 0 && (
          <div className="hint">
            {others} alert{others > 1 ? 's' : ''} on other symbols (switch pair to see them).
          </div>
        )}
      </div>
    </section>
  )
}

// Bot activity heartbeat: an honest, at-a-glance answer to "is the bot actually
// doing anything?". Shows the live monitor pulse, mode, open positions, the
// most recent logged signal, an "updated Ns ago" stamp, and a plain-English
// line on what "running" means right now (watching vs. autonomously trading).
function BotPulse({
  status,
  statusTs,
  signals,
  settings,
  connected,
}: {
  status: BotStatus | null
  statusTs: number
  signals: SignalRow[]
  settings: Settings | null
  connected: boolean
}) {
  // Re-render every second so the "updated Ns ago" heartbeat stays honest.
  const [, force] = useState(0)
  useEffect(() => {
    const id = setInterval(() => force((n) => n + 1), 1000)
    return () => clearInterval(id)
  }, [])

  const running = Boolean(status?.running)
  const auto = Boolean(settings?.auto_trade_enabled)
  const latest = signals.length
    ? [...signals].sort((a, b) => +new Date(b.created_at) - +new Date(a.created_at))[0]
    : null
  const autoSymbols = settings?.auto_symbols?.trim() || 'your auto symbols'

  let explain: string
  if (!running) {
    explain =
      'Monitor stopped — no new signals. Stop-loss / take-profit and resting-order checks still run to protect any open trades.'
  } else if (auto) {
    explain = `Autonomous trading is ON: a confident signal on ${autoSymbols} can place a REAL order. It never invents trades.`
  } else {
    explain = `Watching ${autoSymbols} and logging verdicts to Signals — it does NOT place orders unless you send a manual order or a TradingView alert fires.`
  }
  return (
    <section className="bot-pulse">
      <div className="bp-main">
        <span className={`bp-dot ${running ? 'live' : 'off'}`} />
        <span className="bp-state">{running ? 'Monitoring market' : 'Bot stopped'}</span>
        <span className={`badge ${status?.trading_mode === 'live' ? 'live' : 'paper'}`}>
          {status?.trading_mode ?? '—'}
        </span>
        {status?.testnet && <span className="badge">testnet</span>}
        <span className={`badge ${auto ? 'on' : 'off'}`}>auto {auto ? 'on' : 'off'}</span>
        <span className="bp-sep" />
        <span className="bp-meta">Open positions: {status?.open_positions ?? 0}</span>
        <span className="bp-sep" />
        <span className="bp-meta" title="Time since the last status update from the backend">
          {connected ? 'updated ' : 'stale — reconnecting, last '}
          {ago(statusTs)}
        </span>
      </div>
      <div className="bp-signal">
        {latest ? (
          <>
            <span className="bp-k">Latest signal</span>
            <span className={`badge ${latest.accepted ? 'on' : 'off'}`}>
              {latest.accepted ? 'acted' : 'logged'}
            </span>
            <span className="bp-sig">
              {latest.source}: {latest.action ?? 'hold'} {latest.symbol ?? ''}
              {latest.confidence != null ? ` · ${(latest.confidence * 100).toFixed(0)}%` : ''}
            </span>
            <span className="hint">{ago(+new Date(latest.created_at))}</span>
          </>
        ) : (
          <span className="hint">No signals logged yet — none will appear until the bot is running.</span>
        )}
      </div>
      <div className="bp-explain">{explain}</div>
    </section>
  )
}

function StatsRow({ status }: { status: BotStatus | null }) {
  const cls = (n: number) => (n > 0 ? 'pos' : n < 0 ? 'neg' : '')
  return (
    <section className="stats">
      <div className="stat">
        <div className="label">Equity</div>
        <div className="value">${fmt(status?.equity)}</div>
      </div>
      <div className="stat">
        <div className="label">Balance</div>
        <div className="value">${fmt(status?.balance)}</div>
      </div>
      <div className="stat">
        <div className="label">Unrealized PnL</div>
        <div className={`value ${cls(status?.unrealized_pnl ?? 0)}`}>
          ${fmt(status?.unrealized_pnl)}
        </div>
      </div>
      <div className="stat">
        <div className="label">Realized PnL</div>
        <div className={`value ${cls(status?.realized_pnl ?? 0)}`}>
          ${fmt(status?.realized_pnl)}
        </div>
      </div>
      <div className="stat">
        <div className="label">Today's PnL</div>
        <div className={`value ${cls(status?.day_pnl ?? 0)}`}>
          ${fmt(status?.day_pnl)}
        </div>
      </div>
      <div className="stat">
        <div className="label">Open / Max</div>
        <div className="value">
          {status?.open_positions ?? 0} / {status?.max_open_positions ?? 0}
        </div>
      </div>
    </section>
  )
}

function TradesTable({
  trades,
  openTrades,
  onClose,
  pnlClass,
  closingId,
}: {
  trades: Trade[]
  openTrades: Trade[]
  onClose: (id: number) => void
  pnlClass: (n: number) => string
  closingId?: number | null
}) {
  if (!trades.length) return <div className="empty">No trades yet.</div>
  const openIds = new Set(openTrades.map((t) => t.id))
  return (
    <table>
      <thead>
        <tr>
          <th>Symbol</th>
          <th>Side</th>
          <th className="mono">Entry</th>
          <th className="mono">PnL</th>
          <th>Status</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {trades.map((t) => (
          <tr key={t.id}>
            <td>{t.symbol}</td>
            <td>
              <span className={`tag ${t.side}`}>{t.side}</span>
            </td>
            <td className="mono">
              {t.status === 'pending' && t.limit_price
                ? `${fmt(t.limit_price)} (limit)`
                : fmt(t.entry_price)}
            </td>
            <td className={`mono ${pnlClass(t.pnl)}`}>{fmt(t.pnl)}</td>
            <td>
              <span className={`tag ${t.status}`}>{t.status}</span>
            </td>
            <td>
              {openIds.has(t.id) && (
                <button
                  className="btn"
                  onClick={() => onClose(t.id)}
                  disabled={closingId !== null && closingId !== undefined}
                >
                  {closingId === t.id
                    ? 'Working…'
                    : t.status === 'pending'
                      ? 'Cancel'
                      : 'Close'}
                </button>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

// Map a REAL analyzer verdict + confidence to a TradingView-style rating label.
// Never invents a rating: a hold, or a row with no confidence, reads "Neutral".
function ratingFor(
  action: string | null,
  confidence: number | null,
): { label: string; cls: string } {
  if (action === 'buy')
    return confidence != null && confidence >= 0.66
      ? { label: 'Strong Buy', cls: 'rate-strong-buy' }
      : { label: 'Buy', cls: 'rate-buy' }
  if (action === 'sell')
    return confidence != null && confidence >= 0.66
      ? { label: 'Strong Sell', cls: 'rate-strong-sell' }
      : { label: 'Sell', cls: 'rate-sell' }
  return { label: 'Neutral', cls: 'rate-neutral' }
}

type SigSource = 'all' | 'analyzer' | 'tradingview'
type SigAction = 'all' | 'buy' | 'sell' | 'hold'

function SignalsTable({ signals }: { signals: SignalRow[] }) {
  const [srcFilter, setSrcFilter] = useState<SigSource>('all')
  const [actFilter, setActFilter] = useState<SigAction>('all')

  // Per-symbol rating strip from the LATEST analyzer verdict per symbol. Signals
  // arrive newest-first, so the first analyzer row seen for a symbol is current.
  // Built only from real logged verdicts — a symbol with none is never shown.
  const ratings = useMemo(() => {
    const seen = new Map<string, SignalRow>()
    for (const s of signals) {
      if (s.source !== 'analyzer' || !s.symbol) continue
      if (!seen.has(s.symbol)) seen.set(s.symbol, s)
    }
    return Array.from(seen.values())
  }, [signals])

  const filtered = useMemo(
    () =>
      signals.filter((s) => {
        if (srcFilter !== 'all' && s.source !== srcFilter) return false
        if (actFilter !== 'all' && (s.action ?? 'hold') !== actFilter) return false
        return true
      }),
    [signals, srcFilter, actFilter],
  )

  const fmtTime = (iso: string) => {
    const d = new Date(iso)
    return Number.isNaN(d.getTime()) ? '-' : d.toLocaleString()
  }
  const actionClass = (a: string | null) =>
    a === 'buy' ? 'sig-buy' : a === 'sell' ? 'sig-sell' : 'sig-hold'

  return (
    <div className="signals-wrap">
      {ratings.length > 0 && (
        <div className="rating-strip">
          {ratings.map((r) => {
            const rt = ratingFor(r.action, r.confidence)
            const pct = r.confidence != null ? Math.round(r.confidence * 100) : null
            return (
              <div key={r.symbol} className="rating-card" title={r.message ?? undefined}>
                <div className="rating-sym">{r.symbol}</div>
                <div className={`rating-badge ${rt.cls}`}>{rt.label}</div>
                <div className="conf-bar">
                  <span
                    className={`conf-fill ${rt.cls}`}
                    style={{ width: pct != null ? `${pct}%` : '0%' }}
                  />
                </div>
                <div className="muted rating-conf">
                  {pct != null ? `confidence ${pct}%` : 'no confidence'}
                </div>
              </div>
            )
          })}
        </div>
      )}

      <div className="sig-filters">
        <div className="seg">
          {(['all', 'analyzer', 'tradingview'] as SigSource[]).map((v) => (
            <button
              key={v}
              type="button"
              className={`seg-btn ${srcFilter === v ? 'active' : ''}`}
              onClick={() => setSrcFilter(v)}
            >
              {v === 'all' ? 'All sources' : v === 'analyzer' ? 'Brain' : 'TradingView'}
            </button>
          ))}
        </div>
        <div className="seg">
          {(['all', 'buy', 'sell', 'hold'] as SigAction[]).map((v) => (
            <button
              key={v}
              type="button"
              className={`seg-btn ${actFilter === v ? 'active' : ''}`}
              onClick={() => setActFilter(v)}
            >
              {v === 'all' ? 'All' : v.toUpperCase()}
            </button>
          ))}
        </div>
        <span className="spacer" />
        <span className="hint">{filtered.length} shown</span>
      </div>

      {!signals.length ? (
        <div className="empty">
          No signals yet. The built-in analyzer records its own live verdicts here
          every few seconds while the bot is running — even with autonomous trading
          off — alongside any TradingView alerts you wire up in Settings.
        </div>
      ) : !filtered.length ? (
        <div className="empty">No signals match the current filters.</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Source</th>
              <th>Action</th>
              <th>Symbol</th>
              <th>Confidence</th>
              <th>Acted</th>
              <th>Detail</th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((s) => {
              const pct = s.confidence != null ? Math.round(s.confidence * 100) : null
              return (
                <tr key={s.id}>
                  <td className="muted">{fmtTime(s.created_at)}</td>
                  <td>
                    <span className={`src-badge src-${s.source}`}>
                      {s.source === 'analyzer' ? 'Brain' : s.source}
                    </span>
                  </td>
                  <td>
                    <span className={actionClass(s.action)}>
                      {(s.action ?? '-').toUpperCase()}
                    </span>
                  </td>
                  <td>{s.symbol ?? '-'}</td>
                  <td>
                    {pct != null ? (
                      <div className="conf-cell">
                        <div className="conf-bar sm">
                          <span
                            className={`conf-fill ${actionClass(s.action)}`}
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                        <span className="mono muted">{pct}%</span>
                      </div>
                    ) : (
                      <span className="muted">—</span>
                    )}
                  </td>
                  <td>{s.accepted ? '✅' : '—'}</td>
                  <td className="muted sig-detail">{s.message ?? '-'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </div>
  )
}

// Turn a duration in seconds into a compact human label (e.g. "2h 15m").
function fmtHold(seconds: number | null): string {
  if (seconds == null || !Number.isFinite(seconds)) return '—'
  const s = Math.round(seconds)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  const remM = m % 60
  if (h < 24) return remM ? `${h}h ${remM}m` : `${h}h`
  const d = Math.floor(h / 24)
  const remH = h % 24
  return remH ? `${d}d ${remH}h` : `${d}d`
}

// Realized performance analytics for the current user, computed live by the
// backend from CLOSED trades only. Read-only: this panel never places or
// changes an order. Paper and live are shown separately so simulated gains are
// never mistaken for real money, and undefined metrics stay "—" (never faked).
function PerformancePanel({ onError }: { onError: (msg: string) => void }) {
  const [perf, setPerf] = useState<Performance | null>(null)
  const [loading, setLoading] = useState(true)
  const [failed, setFailed] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setFailed(false)
    try {
      setPerf(await api.performance())
    } catch (e) {
      setFailed(true)
      onError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [onError])

  useEffect(() => {
    load()
  }, [load])

  const cls = (n: number) => (n > 0 ? 'pos' : n < 0 ? 'neg' : '')
  const money = (n: number) => `${n < 0 ? '-' : ''}$${fmt(Math.abs(n))}`
  const pf = (v: number | null) => (v == null ? '—' : fmt(v))

  if (!perf) {
    if (loading) return <div className="empty">Loading…</div>
    return (
      <div className="empty">
        {failed ? "Couldn't load your performance." : 'No performance data.'}
        <button className="btn" style={{ marginLeft: 10 }} onClick={load}>
          Retry
        </button>
      </div>
    )
  }

  if (perf.closed_trades === 0) {
    return (
      <div className="empty">
        No closed trades yet. Your realized performance — win rate, profit
        factor, expectancy and drawdown — appears here as soon as positions
        close. Paper and live results are tracked separately, so simulated gains
        are never counted as real money.
      </div>
    )
  }

  // One comparison row for a paper/live bucket. Drawdown is shown as a negative
  // magnitude so a bigger drop reads as more red, consistent with PnL.
  const bucketRow = (label: string, b: PerfBucket) => (
    <tr>
      <td>{label}</td>
      <td className="mono">{b.closed_trades}</td>
      <td className="mono">{fmt(b.win_rate_pct)}%</td>
      <td className={`mono ${cls(b.total_pnl)}`}>{money(b.total_pnl)}</td>
      <td className="mono">{pf(b.profit_factor)}</td>
      <td className="mono neg">{b.max_drawdown ? money(-b.max_drawdown) : money(0)}</td>
    </tr>
  )

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div className="row" style={{ alignItems: 'center' }}>
        <p className="hint" style={{ flex: 1, margin: 0 }}>
          Realized results from your <b>closed</b> trades — computed live, never
          fabricated. Open positions aren't counted (no realized result yet), and
          an undefined metric shows “—”, not a fake number.
        </p>
        <button className="btn" onClick={load} disabled={loading}>
          {loading ? '…' : '↻ Refresh'}
        </button>
      </div>

      <div className="stats cols-3">
        <div className="stat">
          <div className="label">Closed trades</div>
          <div className="value">{perf.closed_trades}</div>
        </div>
        <div className="stat">
          <div className="label">Win rate</div>
          <div className="value">{fmt(perf.win_rate_pct)}%</div>
        </div>
        <div className="stat">
          <div className="label">Total realized PnL</div>
          <div className={`value ${cls(perf.total_pnl)}`}>{money(perf.total_pnl)}</div>
        </div>
        <div className="stat">
          <div className="label">Profit factor</div>
          <div
            className="value"
            title={perf.profit_factor == null ? 'Undefined — no losing trades yet' : undefined}
          >
            {pf(perf.profit_factor)}
          </div>
        </div>
        <div className="stat">
          <div className="label">Expectancy / trade</div>
          <div className={`value ${cls(perf.expectancy)}`}>{money(perf.expectancy)}</div>
        </div>
        <div className="stat">
          <div className="label">Max drawdown</div>
          <div className="value neg">{perf.max_drawdown ? money(-perf.max_drawdown) : money(0)}</div>
        </div>
      </div>

      <div className="row" style={{ flexWrap: 'wrap', gap: 20 }}>
        <span className="hint">Wins <b className="pos">{perf.wins}</b></span>
        <span className="hint">Losses <b className="neg">{perf.losses}</b></span>
        <span className="hint">Breakeven <b>{perf.breakeven}</b></span>
        <span className="hint">Avg win <b className="pos">{money(perf.avg_win)}</b></span>
        <span className="hint">Avg loss <b className="neg">{money(perf.avg_loss)}</b></span>
        <span className="hint">Largest win <b className="pos">{money(perf.largest_win)}</b></span>
        <span className="hint">Largest loss <b className="neg">{money(perf.largest_loss)}</b></span>
        <span className="hint">Avg hold <b>{fmtHold(perf.avg_hold_seconds)}</b></span>
      </div>

      <div>
        <div className="panel-head" style={{ paddingLeft: 0, borderBottom: 'none' }}>
          Paper vs live
        </div>
        <table>
          <thead>
            <tr>
              <th>Account</th>
              <th className="mono">Closed</th>
              <th className="mono">Win rate</th>
              <th className="mono">Total PnL</th>
              <th className="mono">Profit factor</th>
              <th className="mono">Max DD</th>
            </tr>
          </thead>
          <tbody>
            {bucketRow('📝 Paper (simulated)', perf.paper)}
            {bucketRow('💵 Live (real money)', perf.live)}
          </tbody>
        </table>
        <p className="hint" style={{ marginTop: 6 }}>
          Paper and live are kept strictly separate — simulated gains are never
          added to your real-money results.
        </p>
      </div>

      {perf.by_symbol.length > 0 && (
        <div>
          <div className="panel-head" style={{ paddingLeft: 0, borderBottom: 'none' }}>
            By symbol (best first)
          </div>
          <table>
            <thead>
              <tr>
                <th>Symbol</th>
                <th className="mono">Trades</th>
                <th className="mono">Wins</th>
                <th className="mono">Realized PnL</th>
              </tr>
            </thead>
            <tbody>
              {perf.by_symbol.map((s) => (
                <tr key={s.symbol}>
                  <td>{s.symbol}</td>
                  <td className="mono">{s.trades}</td>
                  <td className="mono">{s.wins}</td>
                  <td className={`mono ${cls(s.pnl)}`}>{money(s.pnl)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

function AutonomyToggle({
  settings,
  status,
  onSaved,
  onError,
}: {
  settings: Settings | null
  status: BotStatus | null
  onSaved: (s: Settings) => void
  onError: (msg: string) => void
}) {
  const [saving, setSaving] = useState(false)
  if (!settings) return null

  const auto = settings.auto_trade_enabled
  const symbols = settings.auto_symbols?.trim() ? settings.auto_symbols : '—'

  const setAuto = async (next: boolean) => {
    if (saving) return
    // Enabling autonomous execution outside paper risks REAL funds with no
    // per-order click -> require an explicit, honest confirmation first.
    if (next && status && status.trading_mode !== 'paper') {
      const warn =
        status.trading_mode === 'live'
          ? 'Enable AUTONOMOUS LIVE trading? The bot will place REAL orders on its own, within your risk limits, with no manual click per trade.'
          : 'Trading mode is not confirmed as paper — enabling autonomous trading may place REAL orders on its own. Continue?'
      if (!window.confirm(warn)) return
    }
    setSaving(true)
    try {
      onSaved(await api.updateSettings({ auto_trade_enabled: next }))
    } catch (e) {
      onError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="panel autonomy">
      <div className="panel-head">
        <span>Autonomy</span>
        <span className={`mode-pill ${auto ? 'on' : ''}`}>
          {auto ? '🤖 Auto' : '✋ Manual'}
        </span>
      </div>
      <div className="panel-body">
        <div className="switch-row">
          <div>
            <div className="switch-title">
              Autonomous trading is {auto ? 'ON' : 'OFF'}
            </div>
            <div className="hint">
              {auto
                ? `The bot analyses ${symbols} on ${settings.auto_timeframe} and places orders itself, within your risk limits.`
                : `Manual mode: the bot still analyses ${symbols} and logs live verdicts to Signals, but never places an order on its own.`}
            </div>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={auto}
            className={`toggle ${auto ? 'on' : ''}`}
            onClick={() => setAuto(!auto)}
            disabled={saving}
            title={auto ? 'Switch to Manual' : 'Switch to Auto'}
          >
            <span className="knob" />
          </button>
        </div>
        {settings.ai_trade_confirm && (
          <div className="hint" style={{ marginTop: 8 }}>
            🧠 AI trade review is on — it may VETO an autonomous entry it judges too
            risky (it can never invent or force a trade).
          </div>
        )}
      </div>
    </section>
  )
}

// Browser-native voice input via the Web Speech API. Not every browser ships it,
// and some (e.g. Chrome) transcribe audio via a cloud service — so it's opt-in
// and the UI says so honestly. `any` is used only for these vendor-typed events.
type SpeechRec = {
  lang: string
  interimResults: boolean
  continuous: boolean
  onresult: ((e: any) => void) | null
  onerror: (() => void) | null
  onend: (() => void) | null
  start: () => void
  stop: () => void
}
function getSpeechRecognition(): (new () => SpeechRec) | null {
  const w = window as any
  return w.SpeechRecognition || w.webkitSpeechRecognition || null
}

function AssistantPanel({
  symbol,
  timeframe,
  tradingMode,
  turns,
  setTurns,
  readAloud,
  onReadAloudChange,
  ttsSupported,
  speak,
  onNavigate,
  onChartControl,
  onError,
}: {
  symbol: string
  timeframe: string
  tradingMode?: string
  // Transcript + read-aloud + speak are owned by the Dashboard, so proactive
  // monitor/alert call-outs land in the transcript (and can be spoken) even when
  // this tab is closed. This panel reads and appends, but doesn't own them.
  turns: ChatMsg[]
  setTurns: Dispatch<SetStateAction<ChatMsg[]>>
  readAloud: boolean
  onReadAloudChange: (on: boolean) => void
  ttsSupported: boolean
  speak: (text: string) => void
  onNavigate: (dest: TabKey) => void
  // Apply a VIEW-ONLY chart command (owned by the Dashboard so it drives the real
  // chart). Returns a TRUE one-line summary of what actually changed, used as the
  // action's outcome — never the AI claiming a change it didn't make.
  onChartControl: (c: {
    symbol?: string
    timeframe?: string
    indicators?: Partial<IndicatorPrefs>
    clear_drawings?: boolean
    undo?: boolean
  }) => string
  onError: (msg: string) => void
}) {
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [useSymbol, setUseSymbol] = useState(true)
  const [useNews, setUseNews] = useState(false)
  const [listening, setListening] = useState(false)
  const recRef = useRef<SpeechRec | null>(null)
  const listRef = useRef<HTMLDivElement | null>(null)
  // Ids of auto (autopilot) actions we've already kicked off, so the effect that
  // watches `turns` starts each one exactly once even as it re-runs on every append.
  const autoStartedRef = useRef<Set<number>>(new Set())

  const speechSupported = typeof window !== 'undefined' && getSpeechRecognition() != null

  // Keep the transcript pinned to the newest turn.
  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight })
  }, [turns, busy])

  // Stop the mic if the user leaves the tab. Read-aloud is owned by the Dashboard
  // now, so we don't cancel speech here — a pushed call-out being read shouldn't
  // be cut off just because you switched tabs.
  useEffect(
    () => () => {
      recRef.current?.stop()
    },
    [],
  )

  const send = async (q: string) => {
    const question = q.trim()
    if (!question || busy) return
    // Prior turns become the conversation history the assistant reads, so it can
    // follow a multi-step task instead of answering each question cold. Captured
    // BEFORE we append this question (setTurns is async), so it's exactly the
    // context that preceded it. Drop the synthetic "(request failed…)" bubbles —
    // those are our own error notices, not real assistant replies.
    const history = turns
      .filter((m) => m.text && !(m.role === 'ai' && m.text.startsWith('(request failed:')))
      .slice(-20)
      .map((m) => ({
        role: m.role === 'ai' ? ('assistant' as const) : ('user' as const),
        content: m.text,
      }))
    setTurns((t) => [...t, { role: 'you', text: question, ts: Date.now() }])
    setInput('')
    setBusy(true)
    try {
      const res = await api.aiChat({
        question,
        symbol: useSymbol ? symbol : undefined,
        timeframe: useSymbol ? timeframe : undefined,
        include_news: useNews,
        history,
      })
      // Extract any hidden navigation action; the spoken/shown text is the reply
      // with the tag removed, and a button lets the user actually go there. A
      // validated proposed_action (order/settings/bot/train/alert) rides along.
      // The SERVER decides `auto`: when true (autopilot on + a safe, non-live
      // action) we start it immediately as 'running' so no Confirm card ever
      // shows and the outcome line is the real endpoint result — never the AI
      // claiming it's "done". Otherwise it's 'pending' and waits for a tap.
      const { text, dest } = parseNavAction(res.reply)
      const action = res.proposed_action ?? undefined
      const auto = !!action && (action as ProposedAction).auto === true
      setTurns((t) => [
        ...t,
        {
          role: 'ai',
          text,
          usedNews: res.used_news,
          nav: dest ? { dest, label: NAV_LABEL[dest] } : undefined,
          action,
          actionState: action ? (auto ? 'running' : 'pending') : undefined,
          id: action ? ++_actionTurnSeq : undefined,
          ts: Date.now(),
        },
      ])
      speak(text)
    } catch (e) {
      const msg = (e as Error).message
      onError(msg)
      setTurns((t) => [...t, { role: 'ai', text: `(request failed: ${msg})`, ts: Date.now() }])
    } finally {
      setBusy(false)
    }
  }

  // Mark a turn's action with a new lifecycle state (so its card can't be re-run
  // and shows the outcome). Keyed by the turn's STABLE id, not its array index:
  // a live monitor call-out on the shared transcript can slice leading turns off
  // (see the Dashboard socket handler's `.slice(-200)`) between an action
  // starting and its await resolving, which would shift every index.
  const setActionState = (id: number, s: 'pending' | 'running' | 'done' | 'dismissed') =>
    setTurns((t) => t.map((m) => (m.id === id ? { ...m, actionState: s } : m)))
  const pushResult = (text: string) => setTurns((t) => [...t, { role: 'ai', text, ts: Date.now() }])
  const dismissAction = (id: number) => setActionState(id, 'dismissed')

  // Turn a proposed action into a human-readable card: a title, plain-English
  // detail lines, and whether it's a real-money danger (a live order).
  const describeAction = (
    a: ProposedAction,
  ): { title: string; lines: string[]; danger: boolean } => {
    if (a.type === 'order') {
      const live = (tradingMode || '').toLowerCase() === 'live'
      const lines = [
        `${a.side.toUpperCase()} ${a.symbol}`,
        a.amount != null ? `Amount: ${a.amount}` : 'Amount: auto-sized by your risk manager',
      ]
      if (a.limit_price != null) lines.push(`Limit price: ${a.limit_price}`)
      if (a.stop_loss != null) lines.push(`Stop loss: ${a.stop_loss}`)
      if (a.take_profit != null) lines.push(`Take profit: ${a.take_profit}`)
      if (a.stop_loss == null && a.take_profit == null && a.side !== 'close')
        lines.push('Stop / target: your Settings defaults')
      lines.push(live ? '⚠️ LIVE mode — a REAL order with real money.' : 'Paper mode — simulated, no real money.')
      return { title: a.side === 'close' ? 'Close position' : 'Place order', lines, danger: live }
    }
    if (a.type === 'settings')
      return { title: 'Change settings', lines: Object.entries(a.changes).map(([k, v]) => `${k} → ${v}`), danger: false }
    if (a.type === 'bot')
      return { title: a.state === 'start' ? 'Start the bot' : 'Stop the bot', lines: [a.state === 'start' ? 'Begin trading / monitoring per your settings.' : 'Halt autonomous trading.'], danger: false }
    if (a.type === 'alert')
      return { title: 'Set a price alert', lines: [`${a.symbol} ${a.condition} ${a.price}`, ...(a.note ? [`Note: ${a.note}`] : []), 'Notifies you on a REAL price cross — it never trades.'], danger: false }
    if (a.type === 'chart') {
      if (a.undo) return { title: 'Undo chart change', lines: ['Step the chart back to the previous view.', 'View only — shows things, moves no money.'], danger: false }
      const lines: string[] = []
      if (a.symbol) lines.push(`Symbol → ${a.symbol}`)
      if (a.timeframe) lines.push(`Timeframe → ${a.timeframe}`)
      if (a.indicators && Object.keys(a.indicators).length) {
        const shown = Object.entries(a.indicators).filter(([, v]) => v).map(([k]) => k)
        const hidden = Object.entries(a.indicators).filter(([, v]) => v === false).map(([k]) => k)
        if (shown.length) lines.push(`Show: ${shown.join(', ')}`)
        if (hidden.length) lines.push(`Hide: ${hidden.join(', ')}`)
      }
      if (a.clear_drawings) lines.push('Clear all drawings (can’t be undone).')
      lines.push('View only — shows things, moves no money.')
      return { title: 'Update the chart', lines, danger: false }
    }
    return { title: 'Train a strategy', lines: [`${a.strategy} on ${a.symbol} ${a.timeframe}`, 'Measures real results on history; saves only if it beats the baseline.'], danger: false }
  }

  // Execute a proposed action AFTER the user confirms it, by calling the SAME
  // authenticated endpoints the manual controls use — the AI has no private path
  // to money or settings. Report the REAL result (or the real error); leave the
  // card re-confirmable if the server rejected it.
  const runAction = async (id: number, action: ProposedAction) => {
    setActionState(id, 'running')
    try {
      if (action.type === 'order') {
        const res = await api.order({
          action: action.side,
          symbol: action.symbol,
          ...(action.amount != null ? { amount: action.amount } : {}),
          ...(action.limit_price != null ? { limit_price: action.limit_price } : {}),
          ...(action.stop_loss != null ? { stop_loss: action.stop_loss } : {}),
          ...(action.take_profit != null ? { take_profit: action.take_profit } : {}),
        })
        const tr = res.trade
        setActionState(id, res.accepted ? 'done' : 'pending')
        pushResult(
          (res.accepted ? '✅ ' : '⚠️ ') +
            res.message +
            (res.accepted && tr ? ` (trade #${tr.id}: ${tr.side} ${tr.amount} ${tr.symbol} @ ${tr.entry_price}, ${tr.mode})` : ''),
        )
        if (!res.accepted) onError(res.message)
      } else if (action.type === 'settings') {
        const s = await api.updateSettings(action.changes)
        setActionState(id, 'done')
        pushResult('✅ Settings updated: ' + Object.keys(action.changes).map((k) => `${k}=${(s as any)[k]}`).join(', '))
      } else if (action.type === 'bot') {
        const res = await api.setBot(action.state)
        setActionState(id, 'done')
        pushResult(`✅ Bot ${res.running ? 'started' : 'stopped'}.`)
      } else if (action.type === 'alert') {
        const al = await api.createAlert({
          symbol: action.symbol,
          condition: action.condition,
          price: action.price,
          ...(action.note ? { note: action.note } : {}),
        })
        setActionState(id, 'done')
        pushResult(`✅ Alert armed: ${al.symbol} ${al.condition} ${al.price}${al.note ? ` (${al.note})` : ''}. You'll be notified on a real cross.`)
      } else if (action.type === 'train') {
        const rep = await api.train(action.symbol, action.strategy, action.timeframe, true)
        setActionState(id, 'done')
        pushResult(
          rep.best
            ? `✅ Trained ${rep.strategy} on ${rep.symbol} ${rep.timeframe}: return ${rep.best.total_return_pct.toFixed(2)}%, win ${rep.best.win_rate_pct.toFixed(1)}%, ${rep.best.num_trades} trades — ${rep.saved ? 'saved to your account.' : 'not saved (did not beat the baseline).'}${rep.warning ? ` Note: ${rep.warning}` : ''}`
            : `ℹ️ Training ran but found no config beating the baseline${rep.warning ? ` — ${rep.warning}` : ''}.`,
        )
      } else if (action.type === 'chart') {
        // VIEW-ONLY and local: no endpoint, no money. Apply it to the real chart
        // and report exactly what changed (the summary comes from the applier, so
        // the outcome line is the truth of what happened, not the AI's claim).
        const summary = onChartControl({
          symbol: action.symbol,
          timeframe: action.timeframe,
          indicators: action.indicators,
          clear_drawings: action.clear_drawings,
          undo: action.undo,
        })
        setActionState(id, 'done')
        pushResult('✅ ' + summary)
      }
    } catch (e) {
      const msg = (e as Error).message
      onError(msg)
      setActionState(id, 'pending')
      pushResult(`⚠️ Couldn't complete that action: ${msg}`)
    }
  }

  // AUTOPILOT: when the server flagged a proposal `auto` (autopilot on + a safe,
  // non-live action), the reply turn is appended already 'running'. This effect
  // picks it up on the next commit and calls the SAME authenticated endpoint a
  // manual Confirm would — so nothing bypasses auth, and the outcome line is the
  // real endpoint result, never the AI asserting success. Matched by stable id
  // (not index) and guarded by autoStartedRef so it fires exactly once; a live
  // order or paper<->live switch is never `auto`, so it still waits for a tap.
  useEffect(() => {
    const m = turns.find(
      (m) =>
        m.id != null &&
        m.action &&
        (m.action as ProposedAction).auto === true &&
        m.actionState === 'running' &&
        !autoStartedRef.current.has(m.id),
    )
    if (!m || m.id == null) return
    autoStartedRef.current.add(m.id)
    void runAction(m.id, m.action as ProposedAction)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turns])

  const toggleMic = () => {
    const Rec = getSpeechRecognition()
    if (!Rec) return
    if (listening) {
      recRef.current?.stop()
      return
    }
    const rec = new Rec()
    rec.lang = 'en-US'
    rec.interimResults = false
    rec.continuous = false
    rec.onresult = (e: any) => {
      const said = e?.results?.[0]?.[0]?.transcript ?? ''
      if (said) setInput((cur) => (cur ? `${cur} ${said}` : said))
    }
    rec.onerror = () => setListening(false)
    rec.onend = () => setListening(false)
    recRef.current = rec
    setListening(true)
    try {
      rec.start()
    } catch {
      setListening(false)
    }
  }

  const suggestions = [
    'How does this app work?',
    'Set me up — I’m new to trading',
    'How long have my trades been running?',
    `What's your read on ${symbol} ${timeframe}?`,
    `Place a safe ${symbol} buy for me`,
    'Set my stop loss to 2%',
    'How is my bot doing right now?',
  ]

  return (
    <div className="assistant">
      <div className="chat-col">
        <div className="chat-list" ref={listRef}>
            {turns.length === 0 ? (
              <div className="chat-empty">
                <p>
                  Ask about your bot, a market, or your risk — or how the app works
                  and how to set it up (“how do I connect Binance?”, “set me up”). It
                  sees your live, non-secret account state — positions, P&L, how long
                  each trade has run — pulls real headlines, and can take you to the
                  right screen. It can also DO things for you: place or close a
                  trade, change a risk setting, start/stop the bot, or train a
                  strategy — but it always asks you to confirm on a card first, and
                  nothing is ever faked.
                </p>
                <div className="chip-row">
                  {suggestions.map((s) => (
                    <button
                      key={s}
                      type="button"
                      className="chip"
                      onClick={() => send(s)}
                      disabled={busy}
                    >
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            ) : (
              turns.map((t, i) => (
                <div key={i} className={`bubble ${t.role}`}>
                  <div className="bubble-role">{t.role === 'you' ? 'You' : '🤖 AI'}</div>
                  <div className="bubble-text">{t.text}</div>
                  {t.nav && (
                    <button
                      type="button"
                      className="btn primary sm nav-cta"
                      onClick={() => onNavigate(t.nav!.dest)}
                    >
                      Take me to {t.nav.label} →
                    </button>
                  )}
                  {t.action && t.actionState && t.actionState !== 'dismissed' && (() => {
                    const d = describeAction(t.action)
                    const running = t.actionState === 'running'
                    const done = t.actionState === 'done'
                    const auto = t.action.auto === true
                    return (
                      <div className={`action-card${d.danger ? ' danger' : ''}`}>
                        <div className="action-title">
                          {done ? '✓ ' : '⚡ '}
                          {d.title}
                        </div>
                        <ul className="action-lines">
                          {d.lines.map((ln, k) => (
                            <li key={k}>{ln}</li>
                          ))}
                        </ul>
                        {t.action.reason && (
                          <div className="action-reason">Why: {t.action.reason}</div>
                        )}
                        {done ? (
                          <div className="action-status">Done ✓</div>
                        ) : auto && running ? (
                          <div className="action-status">Autopilot: applying now…</div>
                        ) : (
                          <div className="action-btns">
                            <button
                              type="button"
                              className="btn primary sm"
                              onClick={() => runAction(t.id!, t.action!)}
                              disabled={running || busy}
                            >
                              {running ? 'Working…' : 'Confirm'}
                            </button>
                            <button
                              type="button"
                              className="btn ghost sm"
                              onClick={() => dismissAction(t.id!)}
                              disabled={running}
                            >
                              Cancel
                            </button>
                          </div>
                        )}
                      </div>
                    )
                  })()}
                  {t.usedNews && <div className="bubble-note">grounded in live news</div>}
                  {t.live && <div className="bubble-note">🔔 live monitor</div>}
                </div>
              ))
            )}
            {busy && (
              <div className="bubble ai">
                <div className="bubble-role">🤖 AI</div>
                <div className="bubble-text typing">Thinking…</div>
              </div>
            )}
          </div>

          <div className="chat-controls">
            <label className="check">
              <input
                type="checkbox"
                checked={useSymbol}
                onChange={(e) => setUseSymbol(e.target.checked)}
              />
              Include {symbol} {timeframe} analysis
            </label>
            <label className="check">
              <input
                type="checkbox"
                checked={useNews}
                onChange={(e) => setUseNews(e.target.checked)}
              />
              Attach live news
            </label>
            {ttsSupported && (
              <label className="check">
                <input
                  type="checkbox"
                  checked={readAloud}
                  onChange={(e) => onReadAloudChange(e.target.checked)}
                />
                Read replies &amp; alerts aloud
              </label>
            )}
          </div>

          <div className="chat-input">
            {speechSupported && (
              <button
                type="button"
                className={`btn mic ${listening ? 'rec' : ''}`}
                onClick={toggleMic}
                title={listening ? 'Stop listening' : 'Speak your question'}
              >
                {listening ? '● Listening' : '🎤'}
              </button>
            )}
            <input
              className="input"
              placeholder="Ask the AI anything about your bot or the market…"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && send(input)}
              disabled={busy}
            />
            <button
              className="btn primary"
              onClick={() => send(input)}
              disabled={busy || !input.trim()}
            >
              {busy ? '…' : 'Send'}
            </button>
          </div>
          {speechSupported && (
            <p className="hint tiny">
              🎤 Voice uses your browser’s Web Speech API; some browsers send audio to
              a cloud service to transcribe. It stays off until you press the mic.
            </p>
          )}
        </div>
      </div>
  )
}

// Dedicated News dashboard: its own tab so headlines get full width instead of
// sharing the assistant column. Real public-feed items only — an empty list
// means the feeds were unreachable (never fabricated).
function NewsPanel({ onError }: { onError: (msg: string) => void }) {
  const [news, setNews] = useState<NewsItem[]>([])
  const [newsErrors, setNewsErrors] = useState<string[]>([])
  const [newsLoading, setNewsLoading] = useState(false)
  // When the headlines were last refreshed, so the dashboard shows it's live.
  const [newsFetchedAt, setNewsFetchedAt] = useState<number | null>(null)

  const loadNews = useCallback(async () => {
    setNewsLoading(true)
    try {
      const res = await api.news(12)
      setNews(res.items)
      setNewsErrors(res.errors)
      setNewsFetchedAt(Date.now())
    } catch (e) {
      onError((e as Error).message)
    } finally {
      setNewsLoading(false)
    }
  }, [onError])

  // Load on mount, then auto-refresh so the dashboard stays near-real-time (the
  // backend already limits results to the last day and caches briefly).
  useEffect(() => {
    loadNews()
    const id = setInterval(loadNews, 60000)
    return () => clearInterval(id)
  }, [loadNews])

  const fmtNewsTime = (iso: string) => {
    if (!iso) return ''
    const d = new Date(iso)
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString()
  }

  return (
    <div className="news-panel">
      <div className="news-head">
        <span>📰 Live market news</span>
        <span className="news-updated muted">
          {newsFetchedAt
            ? `updated ${new Date(newsFetchedAt).toLocaleTimeString([], {
                hour: '2-digit',
                minute: '2-digit',
              })}`
            : ''}
        </span>
        <button
          type="button"
          className="btn ghost sm"
          onClick={loadNews}
          disabled={newsLoading}
          title="Refresh headlines"
        >
          {newsLoading ? '…' : '↻'}
        </button>
      </div>
      <p className="hint tiny news-sub">
        Real headlines from public feeds, newest first — today’s news (or the most
        recent day if today is quiet). Never fabricated.
      </p>
      {news.length === 0 && !newsLoading ? (
        <div className="empty sm">
          {newsErrors.length
            ? 'News sources are unreachable right now. Nothing is fabricated — this is empty because the real feeds could not be fetched.'
            : 'No headlines available.'}
        </div>
      ) : (
        <ul className="news-list">
          {news.map((n, i) => (
            <li key={`${n.link ?? n.title}:${i}`}>
              {n.link ? (
                <a href={n.link} target="_blank" rel="noopener noreferrer">
                  {n.title}
                </a>
              ) : (
                <span>{n.title}</span>
              )}
              <div className="news-meta muted">
                {n.source}
                {n.published ? ` · ${fmtNewsTime(n.published)}` : ''}
              </div>
            </li>
          ))}
        </ul>
      )}
      {newsErrors.length > 0 && news.length > 0 && (
        <p className="hint tiny">Some feeds failed: {newsErrors.join(', ')}.</p>
      )}
    </div>
  )
}

function AnalyzePanel({
  symbol,
  timeframe,
  onError,
}: {
  symbol: string
  timeframe: string
  onError: (msg: string) => void
}) {
  const [analysis, setAnalysis] = useState<MarketAnalysis | null>(null)
  const [busy, setBusy] = useState(false)
  const [question, setQuestion] = useState('')
  const [answer, setAnswer] = useState('')
  const [asking, setAsking] = useState(false)

  // The verdict and AI answer are specific to one market; clear them when the
  // symbol/timeframe changes so stale results aren't shown against a new chart.
  useEffect(() => {
    setAnalysis(null)
    setAnswer('')
  }, [symbol, timeframe])

  const run = async (explain: boolean) => {
    setBusy(true)
    try {
      setAnalysis(await api.analyze(symbol, timeframe, explain))
    } catch (e) {
      onError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const runAssess = async () => {
    setBusy(true)
    try {
      setAnalysis(await api.analyze(symbol, timeframe, false, true))
    } catch (e) {
      onError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const ask = async () => {
    if (!question.trim()) return
    setAsking(true)
    setAnswer('')
    try {
      const res = await api.aiAsk(question, symbol, timeframe)
      setAnswer(res.answer)
    } catch (e) {
      onError((e as Error).message)
    } finally {
      setAsking(false)
    }
  }

  const verdictClass =
    analysis?.verdict === 'buy' ? 'pos' : analysis?.verdict === 'sell' ? 'neg' : ''

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <p className="hint">
        The analyzer weighs trend, RSI, MACD, momentum and volatility into one
        confidence-scored verdict for <b>{symbol}</b> <b>{timeframe}</b>. When
        signals are weak or volatility is extreme it says <b>HOLD</b> — protecting
        capital instead of forcing a trade.
      </p>
      <div className="row">
        <button className="btn primary" onClick={() => run(false)} disabled={busy}>
          {busy ? 'Analyzing…' : 'Analyze market'}
        </button>
        <button className="btn" onClick={() => run(true)} disabled={busy}>
          Analyze + explain
        </button>
        <button className="btn" onClick={() => runAssess()} disabled={busy}>
          🧠 Deep assessment
        </button>
      </div>

      {analysis && (
        <div>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 12 }}>
            <span className={`verdict ${verdictClass}`} style={{ fontSize: 22, fontWeight: 700 }}>
              {analysis.verdict.toUpperCase()}
            </span>
            <span className="hint">confidence {Math.round(analysis.confidence * 100)}%</span>
            <span className="mono hint">score {analysis.score.toFixed(2)}</span>
          </div>
          <p style={{ marginTop: 6 }}>{analysis.summary}</p>
          {analysis.narration && analysis.narration !== analysis.summary && (
            <p className="hint" style={{ fontStyle: 'italic' }}>🧠 {analysis.narration}</p>
          )}
          {analysis.assessment && analysis.assessment !== analysis.summary && (
            <div
              className="hint"
              style={{
                marginTop: 8,
                whiteSpace: 'pre-wrap',
                background: 'rgba(127,127,127,0.08)',
                borderRadius: 8,
                padding: 12,
              }}
            >
              {analysis.assessment}
            </div>
          )}
          <table style={{ marginTop: 8 }}>
            <thead>
              <tr>
                <th>Factor</th>
                <th>Signal</th>
                <th className="mono">Weight</th>
                <th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {analysis.factors.map((f) => (
                <tr key={f.name}>
                  <td>{f.name}</td>
                  <td className={f.signal === 'buy' ? 'pos' : f.signal === 'sell' ? 'neg' : ''}>
                    {f.signal}
                  </td>
                  <td className="mono">{f.weight.toFixed(2)}</td>
                  <td>{f.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="field" style={{ marginTop: 8 }}>
        <label>Ask the AI about this market (needs AI_API_KEY configured)</label>
        <div className="row">
          <input
            className="input"
            placeholder="e.g. Is this a good entry, and what's the main risk?"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && ask()}
          />
          <button className="btn" onClick={ask} disabled={asking}>
            {asking ? 'Asking…' : 'Ask'}
          </button>
        </div>
        {answer && <p className="hint" style={{ marginTop: 8 }}>{answer}</p>}
      </div>
    </div>
  )
}

function BacktestPanel({
  symbol,
  timeframe,
  onError,
}: {
  symbol: string
  timeframe: string
  onError: (msg: string) => void
}) {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([])
  const [strategy, setStrategy] = useState('ma_cross')
  const [feePct, setFeePct] = useState('0.1')
  const [slippagePct, setSlippagePct] = useState('0.05')
  const [useSaved, setUseSaved] = useState(false)
  const [result, setResult] = useState<BacktestResult | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api
      .strategies()
      .then((s) => {
        setStrategies(s)
        if (s.length) setStrategy(s[0].name)
      })
      .catch(() => {})
  }, [])

  // Results are tied to the selected market; drop them on a symbol/timeframe
  // switch so the panel never shows a backtest for the wrong chart.
  useEffect(() => {
    setResult(null)
  }, [symbol, timeframe])

  const run = async () => {
    setBusy(true)
    setResult(null)
    try {
      setResult(
        await api.backtest(
          symbol,
          strategy,
          timeframe,
          feePct === '' ? undefined : Number(feePct),
          slippagePct === '' ? undefined : Number(slippagePct),
          useSaved,
        ),
      )
    } catch (e) {
      onError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <p className="hint">
        Replay a strategy over real <b>{symbol}</b> <b>{timeframe}</b> candles. Fills happen at the
        next bar's open (no look-ahead), with fees and slippage applied against you, so results are
        conservative rather than optimistic.
      </p>
      <div className="row">
        <div className="field">
          <label>Strategy</label>
          <select className="select" value={strategy} onChange={(e) => setStrategy(e.target.value)}>
            {strategies.map((s) => (
              <option key={s.name} value={s.name}>
                {s.name}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label>Fee % per side</label>
          <input
            className="input"
            value={feePct}
            onChange={(e) => setFeePct(e.target.value)}
            inputMode="decimal"
          />
        </div>
        <div className="field">
          <label>Slippage %</label>
          <input
            className="input"
            value={slippagePct}
            onChange={(e) => setSlippagePct(e.target.value)}
            inputMode="decimal"
          />
        </div>
        <button className="btn primary" onClick={run} disabled={busy}>
          {busy ? 'Running…' : 'Run backtest'}
        </button>
      </div>
      <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <input
          type="checkbox"
          checked={useSaved}
          onChange={(e) => setUseSaved(e.target.checked)}
        />
        Use my saved (trained) strategy for {symbol} if one exists
      </label>

      {result && (
        <div>
          {result.used_saved && (
            <p className="hint" style={{ color: 'var(--green, #16a34a)' }}>
              ✅ Replayed your saved <b>{result.strategy}</b> strategy for {symbol} (the
              same config the bot trades with).
            </p>
          )}
          <div className="stats cols-3">
            <div className="stat">
              <div className="label">Return</div>
              <div className={`value ${result.total_return_pct >= 0 ? 'pos' : 'neg'}`}>
                {fmt(result.total_return_pct)}%
              </div>
            </div>
            <div className="stat">
              <div className="label">End balance</div>
              <div className="value">{fmt(result.ending_balance)}</div>
            </div>
            <div className="stat">
              <div className="label">Trades</div>
              <div className="value">{result.num_trades}</div>
            </div>
            <div className="stat">
              <div className="label">Win rate</div>
              <div className="value">{fmt(result.win_rate_pct)}%</div>
            </div>
            <div className="stat">
              <div className="label">Max drawdown</div>
              <div className="value neg">{fmt(result.max_drawdown_pct)}%</div>
            </div>
            <div className="stat">
              <div className="label">Fees paid</div>
              <div className="value">{fmt(result.total_fees)}</div>
            </div>
          </div>
          <p className="hint" style={{ marginTop: 8 }}>
            Applied the bot's live exit rules — stop-loss{' '}
            {fmt(result.stop_loss_pct ?? 0)}%, take-profit {fmt(result.take_profit_pct ?? 0)}%,
            trailing {fmt(result.trailing_stop_pct ?? 0)}% — so these numbers reflect how the
            bot would actually trade, not buy-and-hold.
          </p>
          {result.equity_curve.length > 1 && (
            <div style={{ marginTop: 12 }}>
              <EquitySparkline values={result.equity_curve} />
            </div>
          )}
          {result.num_trades === 0 && (
            <div className="empty">
              This strategy generated no trades on this data. Try another symbol, timeframe, or
              strategy.
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function EquitySparkline({ values }: { values: number[] }) {
  const w = 600
  const h = 120
  const min = Math.min(...values)
  const max = Math.max(...values)
  const range = max - min || 1
  const pts = values
    .map((v, i) => {
      const x = (i / (values.length - 1)) * w
      const y = h - ((v - min) / range) * h
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
  const up = values[values.length - 1] >= values[0]
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} preserveAspectRatio="none">
      <polyline
        points={pts}
        fill="none"
        stroke={up ? 'var(--green, #16a34a)' : 'var(--red, #dc2626)'}
        strokeWidth={2}
      />
    </svg>
  )
}

function TrainPanel({
  symbol,
  timeframe,
  onError,
}: {
  symbol: string
  timeframe: string
  onError: (msg: string) => void
}) {
  const [strategies, setStrategies] = useState<StrategyInfo[]>([])
  const [strategy, setStrategy] = useState('ma_cross')
  const [report, setReport] = useState<TrainingReport | null>(null)
  const [busy, setBusy] = useState(false)
  // Bumped after a successful train so the saved-strategies list below refetches
  // and the freshly-saved config appears immediately.
  const [savedKey, setSavedKey] = useState(0)

  useEffect(() => {
    api
      .strategies()
      .then((s) => {
        setStrategies(s)
        if (s.length) setStrategy(s[0].name)
      })
      .catch(() => {})
  }, [])

  // A training report is for one market only; clear it on a symbol/timeframe
  // switch so a stale leaderboard isn't shown against a different chart.
  useEffect(() => {
    setReport(null)
  }, [symbol, timeframe])

  const run = async () => {
    setBusy(true)
    setReport(null)
    try {
      const r = await api.train(symbol, strategy, timeframe)
      setReport(r)
      if (r.saved) setSavedKey((k) => k + 1) // refresh the saved list below
    } catch (e) {
      onError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <p className="hint">
        Teach the bot: it backtests every parameter combination for a strategy on real{' '}
        <b>{symbol}</b> <b>{timeframe}</b> candles and finds the best-performing setup.
      </p>
      <div className="row">
        <div className="field">
          <label>Strategy</label>
          <select className="select" value={strategy} onChange={(e) => setStrategy(e.target.value)}>
            {strategies.map((s) => (
              <option key={s.name} value={s.name}>
                {s.name}
              </option>
            ))}
          </select>
        </div>
        <button className="btn primary" onClick={run} disabled={busy}>
          {busy ? 'Training…' : 'Train'}
        </button>
      </div>

      {report && (
        <div>
          {report.best ? (
            <>
              <p className="hint">
                Tested {report.tested} configs on {report.candles} candles.{' '}
                {report.train_fraction < 1
                  ? `Fitted on the first ${Math.round(report.train_fraction * 100)}% and validated on the untouched remainder (out-of-sample).`
                  : 'Evaluated on the full dataset (no holdout split).'}{' '}
                Best setup:
              </p>
              {report.warning && (
                <p className="hint" style={{ color: 'var(--red)' }}>⚠️ {report.warning}</p>
              )}
              <pre className="code">{JSON.stringify(report.best.params, null, 2)}</pre>
              {report.saved ? (
                <p className="hint" style={{ color: 'var(--green, #16a34a)' }}>
                  ✅ Saved to your account for <b>{report.symbol}</b>. Turn on{' '}
                  <b>“Trade with my saved strategies”</b> in Settings and the bot
                  will trade {report.symbol} with this exact setup.
                </p>
              ) : (
                <p className="hint">
                  Not saved — no configuration beat a flat baseline on this data, so
                  nothing was persisted (the bot won't trade a losing setup).
                </p>
              )}
              <table>
                <thead>
                  <tr>
                    <th>Params</th>
                    <th className="mono">Return %</th>
                    <th className="mono">Win %</th>
                    <th className="mono">Max DD %</th>
                    <th className="mono">Trades</th>
                    <th className="mono">Val. return %</th>
                    <th className="mono">Overfit gap</th>
                  </tr>
                </thead>
                <tbody>
                  {report.leaderboard.map((c, i) => (
                    <tr key={i}>
                      <td>{Object.entries(c.params).map(([k, v]) => `${k}=${v}`).join(' ')}</td>
                      <td className={`mono ${c.total_return_pct >= 0 ? 'pos' : 'neg'}`}>
                        {fmt(c.total_return_pct)}
                      </td>
                      <td className="mono">{fmt(c.win_rate_pct)}</td>
                      <td className="mono">{fmt(c.max_drawdown_pct)}</td>
                      <td className="mono">{c.num_trades}</td>
                      <td
                        className={`mono ${
                          c.validation_return_pct == null
                            ? ''
                            : c.validation_return_pct >= 0
                            ? 'pos'
                            : 'neg'
                        }`}
                      >
                        {c.validation_return_pct == null ? '—' : fmt(c.validation_return_pct)}
                      </td>
                      <td className="mono">
                        {c.overfit_gap_pct == null ? '—' : fmt(c.overfit_gap_pct)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          ) : (
            <div className="empty">
              No profitable configuration traded on this data. Try a different symbol or timeframe.
            </div>
          )}
        </div>
      )}

      <SavedStrategiesCard refreshKey={savedKey} onError={onError} />
    </div>
  )
}

function SavedStrategiesCard({
  refreshKey,
  onError,
}: {
  refreshKey: number
  onError: (msg: string) => void
}) {
  const [saved, setSaved] = useState<SavedStrategy[]>([])
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState<string | null>(null)

  const load = async () => {
    setLoading(true)
    try {
      setSaved(await api.savedStrategies())
    } catch (e) {
      onError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshKey])

  const remove = async (symbol: string) => {
    setBusy(symbol)
    try {
      await api.deleteSavedStrategy(symbol)
      setSaved((prev) => prev.filter((s) => s.symbol !== symbol))
    } catch (e) {
      onError((e as Error).message)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="panel" style={{ marginTop: 8 }}>
      <div className="panel-head">Your saved strategies</div>
      <div className="panel-body">
        <p className="hint">
          The setups you've trained and saved, one per symbol. When{' '}
          <b>“Trade with my saved strategies”</b> is on in Settings, the bot trades
          each of these symbols with its saved config (its buy still yields to
          capital-preservation gates; its exit is always honoured). These live on
          your account and survive restarts.
        </p>
        {loading ? (
          <div className="empty">Loading…</div>
        ) : saved.length === 0 ? (
          <div className="empty">
            Nothing saved yet. Train a strategy above and the winning setup is saved
            here automatically.
          </div>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table>
              <thead>
                <tr>
                  <th>Symbol</th>
                  <th>Strategy</th>
                  <th>Timeframe</th>
                  <th className="mono">Return %</th>
                  <th className="mono">Win %</th>
                  <th className="mono">Max DD %</th>
                  <th>Trained</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {saved.map((s) => (
                  <tr key={s.symbol}>
                    <td><b>{s.symbol}</b></td>
                    <td>{s.strategy}</td>
                    <td>{s.timeframe ?? '—'}</td>
                    <td
                      className={`mono ${
                        s.metrics?.total_return_pct == null
                          ? ''
                          : s.metrics.total_return_pct >= 0
                          ? 'pos'
                          : 'neg'
                      }`}
                    >
                      {s.metrics?.total_return_pct == null ? '—' : fmt(s.metrics.total_return_pct)}
                    </td>
                    <td className="mono">
                      {s.metrics?.win_rate_pct == null ? '—' : fmt(s.metrics.win_rate_pct)}
                    </td>
                    <td className="mono">
                      {s.metrics?.max_drawdown_pct == null ? '—' : fmt(s.metrics.max_drawdown_pct)}
                    </td>
                    <td className="mono" style={{ whiteSpace: 'nowrap' }}>
                      {s.trained_at ? new Date(s.trained_at).toLocaleDateString() : '—'}
                    </td>
                    <td>
                      <button
                        className="btn danger"
                        onClick={() => remove(s.symbol)}
                        disabled={busy === s.symbol}
                      >
                        {busy === s.symbol ? '…' : 'Delete'}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}

// A numeric text field you can actually TYPE decimals into. The parent keeps
// the value as a number, but binding a <input> straight to a number re-writes
// its text on every keystroke, which swallows a trailing dot — "0." snaps back
// to "0", so you can never get to "0.1". This holds the raw text locally while
// you edit, pushes the parsed number up the moment it's a valid number, and
// only re-syncs from the parent number when the field isn't focused (e.g. after
// settings load or reset). It never lets a non-numeric character land.
function NumField({
  value,
  onChange,
  className,
  inputMode = 'decimal',
  placeholder,
}: {
  value: number
  onChange: (n: number) => void
  className?: string
  inputMode?: 'decimal' | 'numeric'
  placeholder?: string
}) {
  const [text, setText] = useState<string>(() => String(value))
  const editingRef = useRef(false)
  useEffect(() => {
    // Reflect an external change unless the user is mid-edit (never clobber what
    // they're typing). Compare numerically so "0." vs 0 doesn't force a rewrite.
    if (!editingRef.current && Number(text) !== value) setText(String(value))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value])
  return (
    <input
      className={className}
      value={text}
      inputMode={inputMode}
      placeholder={placeholder}
      onFocus={() => {
        editingRef.current = true
      }}
      onBlur={() => {
        // Normalise on the way out: blank -> 0, garbage -> last good value.
        editingRef.current = false
        const n = text.trim() === '' ? 0 : Number(text)
        const final = Number.isFinite(n) ? n : value
        setText(String(final))
        if (final !== value) onChange(final)
      }}
      onChange={(e) => {
        const raw = e.target.value
        // Only the shape of a number-in-progress: digits, one dot, a lead sign.
        // "", ".", "0.", "-", "-." are allowed so decimals can be typed freely.
        if (!/^-?\d*\.?\d*$/.test(raw)) return
        editingRef.current = true
        setText(raw)
        const n = Number(raw)
        if (raw !== '' && raw !== '.' && raw !== '-' && raw !== '-.' && Number.isFinite(n)) {
          onChange(n)
        }
      }}
    />
  )
}

function SettingsPanel({
  settings,
  loadError,
  onReload,
  access,
  onRefreshAccess,
  me,
  onSaved,
  onMeChanged,
  onError,
  onResetPaper,
}: {
  settings: Settings | null
  loadError: boolean
  onReload: () => void
  access: ExchangeAccess | null
  onRefreshAccess: () => Promise<ExchangeAccess | null>
  me: Me
  onSaved: (s: Settings) => void
  onMeChanged: (m: Me) => void
  onError: (msg: string) => void
  onResetPaper: () => void
}) {
  const [form, setForm] = useState<Settings | null>(settings)
  const [copied, setCopied] = useState(false)
  const [testing, setTesting] = useState(false)
  useEffect(() => setForm(settings), [settings])
  if (!form) {
    // Distinguish a failed fetch from one still in flight so the panel never
    // sits on "Loading…" forever when the request actually errored.
    if (loadError) {
      return (
        <div className="empty">
          Couldn't load your settings.
          <button className="btn" style={{ marginLeft: 10 }} onClick={onReload}>
            Retry
          </button>
        </div>
      )
    }
    return <div className="empty">Loading…</div>
  }

  const webhookUrl = `${location.origin}${form.webhook_path}`
  // Numeric field setter: NumField hands us an already-parsed finite number, so
  // we just store it. (NumField owns the "let me type a decimal" behaviour and
  // normalises blank/garbage, so risk settings never receive NaN.)
  const setNum = (k: keyof Settings) => (n: number) => setForm({ ...form, [k]: n } as Settings)

  const save = async () => {
    try {
      const saved = await api.updateSettings({
        trading_mode: form.trading_mode,
        max_open_positions: form.max_open_positions,
        risk_per_trade_pct: form.risk_per_trade_pct,
        daily_loss_limit_pct: form.daily_loss_limit_pct,
        default_stop_loss_pct: form.default_stop_loss_pct,
        default_take_profit_pct: form.default_take_profit_pct,
        trailing_stop_pct: form.trailing_stop_pct,
        max_total_exposure_pct: form.max_total_exposure_pct,
        paper_taker_fee_pct: form.paper_taker_fee_pct,
        min_signal_confidence: form.min_signal_confidence,
        auto_trade_enabled: form.auto_trade_enabled,
        auto_symbols: form.auto_symbols,
        auto_timeframe: form.auto_timeframe,
        auto_confirm_timeframe: form.auto_confirm_timeframe,
        use_saved_strategy: form.use_saved_strategy,
        ai_trade_confirm: form.ai_trade_confirm,
        ai_monitor_enabled: form.ai_monitor_enabled,
        ai_autopilot_enabled: form.ai_autopilot_enabled,
      })
      onSaved(saved)
    } catch (e) {
      onError((e as Error).message)
    }
  }

  const alertExample = JSON.stringify(
    { action: 'buy', symbol: 'BTC/USDT', amount: 0.001 },
    null,
    2,
  )

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div className="field">
        <label>Trading mode</label>
        <select
          className="select"
          value={form.trading_mode}
          onChange={(e) => setForm({ ...form, trading_mode: e.target.value })}
        >
          <option value="paper">paper (safe, simulated)</option>
          <option value="live">live (REAL orders)</option>
        </select>
      </div>
      {form.trading_mode === 'live' && (
        <p className="hint" style={{ color: 'var(--red)' }}>
          ⚠️ Live mode places REAL orders on Binance. Make sure your API keys are set in the
          backend .env and you have tested on testnet first.
        </p>
      )}

      <div className={`access-card ${access ? (access.ok ? 'ok' : 'bad') : ''}`}>
        <div className="access-head">
          {access
            ? `${access.ok ? '✅ Exchange ready to trade' : '⚠️ Exchange cannot trade yet'}${
                access.testnet ? ' (testnet)' : ' (live account)'
              }`
            : 'Exchange connection — not tested yet'}
          <button
            className="btn"
            style={{ marginLeft: 'auto' }}
            onClick={async () => {
              setTesting(true)
              try {
                const a = await onRefreshAccess()
                if (!a) onError('Could not reach the connection check.')
              } finally {
                setTesting(false)
              }
            }}
            disabled={testing}
          >
            {testing ? 'Testing…' : 'Test connection'}
          </button>
        </div>
        {access && (
          <>
            <div className="access-rows">
              <span>Public data: {access.can_read_public ? '✅' : '❌'}</span>
              <span>Account read: {access.can_read_account ? '✅' : '❌'}</span>
              <span>Trading: {access.can_trade ? '✅' : '❌'}</span>
            </div>
            <p className="hint" style={{ marginTop: 6 }}>{access.detail}</p>
          </>
        )}
        {!access && (
          <p className="hint" style={{ marginTop: 6 }}>
            Click <b>Test connection</b> to check — live — whether this app can
            reach your exchange, read your account, and place orders. Saving your
            API keys below also runs this test automatically.
          </p>
        )}
      </div>

      <div className="row">
        <div className="field">
          <label>Max open positions</label>
          <NumField
            className="input"
            value={form.max_open_positions}
            onChange={setNum('max_open_positions')}
            inputMode="numeric"
          />
        </div>
        <div className="field">
          <label>Risk per trade %</label>
          <NumField
            className="input"
            value={form.risk_per_trade_pct}
            onChange={setNum('risk_per_trade_pct')}
            inputMode="decimal"
          />
        </div>
      </div>
      <div className="row">
        <div className="field">
          <label>Stop-loss %</label>
          <NumField
            className="input"
            value={form.default_stop_loss_pct}
            onChange={setNum('default_stop_loss_pct')}
            inputMode="decimal"
          />
        </div>
        <div className="field">
          <label>Take-profit %</label>
          <NumField
            className="input"
            value={form.default_take_profit_pct}
            onChange={setNum('default_take_profit_pct')}
            inputMode="decimal"
          />
        </div>
        <div className="field">
          <label>Trailing stop % (0 = off)</label>
          <NumField
            className="input"
            value={form.trailing_stop_pct}
            onChange={setNum('trailing_stop_pct')}
            inputMode="decimal"
          />
        </div>
        <div className="field">
          <label>Daily loss limit %</label>
          <NumField
            className="input"
            value={form.daily_loss_limit_pct}
            onChange={setNum('daily_loss_limit_pct')}
            inputMode="decimal"
          />
        </div>
        <div className="field">
          <label>Max total exposure % (0 = off)</label>
          <NumField
            className="input"
            value={form.max_total_exposure_pct}
            onChange={setNum('max_total_exposure_pct')}
            inputMode="decimal"
          />
        </div>
        <div className="field">
          <label>Paper taker fee % (0 = off)</label>
          <NumField
            className="input"
            value={form.paper_taker_fee_pct}
            onChange={setNum('paper_taker_fee_pct')}
            inputMode="decimal"
          />
        </div>
      </div>
      <p className="hint">
        Paper taker fee models a real exchange fee on <b>both legs</b> of every
        simulated round trip, so your paper track record reflects the true cost of
        trading (e.g. 0.1% is Binance's standard taker rate). It only affects{' '}
        <b>paper</b> results — live P&amp;L always books the real fees already in
        your fills, never an invented number. Leave at 0 to keep paper fee-free.
      </p>

      <div className="panel-head" style={{ paddingLeft: 0, borderBottom: 'none' }}>
        Autonomous trading (the bot analyses & trades by itself)
      </div>
      <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <input
          type="checkbox"
          checked={form.auto_trade_enabled}
          onChange={(e) => setForm({ ...form, auto_trade_enabled: e.target.checked })}
        />
        Enable autonomous trading{' '}
        {form.trading_mode === 'live' && (
          <span style={{ color: 'var(--red)' }}>(LIVE — real money!)</span>
        )}
      </label>
      <div className="row">
        <div className="field">
          <label>Auto symbols (comma-separated)</label>
          <input
            className="input"
            value={form.auto_symbols}
            onChange={(e) => setForm({ ...form, auto_symbols: e.target.value })}
            placeholder="BTC/USDT, ETH/USDT"
          />
        </div>
        <div className="field">
          <label>Auto timeframe</label>
          <input
            className="input"
            value={form.auto_timeframe}
            onChange={(e) => setForm({ ...form, auto_timeframe: e.target.value })}
            placeholder="1h"
          />
        </div>
        <div className="field">
          <label>Confirm timeframe (higher; blank = off)</label>
          <input
            className="input"
            value={form.auto_confirm_timeframe}
            onChange={(e) => setForm({ ...form, auto_confirm_timeframe: e.target.value })}
            placeholder="4h"
          />
        </div>
        <div className="field">
          <label>Min signal confidence (0–1)</label>
          <NumField
            className="input"
            value={form.min_signal_confidence}
            onChange={setNum('min_signal_confidence')}
            inputMode="decimal"
          />
        </div>
      </div>
      <p className="hint">
        When enabled, on every monitor tick the bot runs its analyzer on each auto
        symbol and only opens a long (or exits one) when confidence clears the
        threshold. Higher confidence = fewer, higher-conviction trades.{' '}
        {form.ai_enabled ? `✅ AI commentary is configured${form.ai_model ? ` (${form.ai_model}${form.ai_style ? `, ${form.ai_style}` : ''})` : ''}.` : 'AI commentary is off (set AI_API_KEY to enable).'}
      </p>
      <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <input
          type="checkbox"
          checked={form.ai_trade_confirm}
          onChange={(e) => setForm({ ...form, ai_trade_confirm: e.target.checked })}
          disabled={!form.ai_enabled}
        />
        AI trade review (AI may VETO an autonomous entry)
      </label>
      <p className="hint">
        When on, and only while autonomous trading is on, the AI reviews each
        deterministic entry and can <b>block</b> one it judges too risky. It can
        never invent, size, or force a trade, and it never bypasses your risk
        limits — if the AI is unavailable the analyzer's own decision stands.{' '}
        {form.ai_enabled
          ? 'Test it in paper mode before trusting it with live orders.'
          : 'Add an AI key (Credentials) to enable this.'}
      </p>
      <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <input
          type="checkbox"
          checked={form.use_saved_strategy}
          onChange={(e) => setForm({ ...form, use_saved_strategy: e.target.checked })}
        />
        Trade with my saved (trained) strategies
      </label>
      <p className="hint">
        When on, the bot trades each symbol with the strategy you trained and
        saved for it (see <b>Train</b>) instead of its built-in analyzer. Capital
        preservation still comes first: a saved <b>BUY</b> is suppressed in a bear
        regime or a volatility shock, while its <b>SELL/exit is always honoured</b>.
        Symbols with no saved strategy fall back to the analyzer brain.
      </p>
      <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <input
          type="checkbox"
          checked={form.ai_monitor_enabled}
          onChange={(e) => setForm({ ...form, ai_monitor_enabled: e.target.checked })}
          disabled={!form.ai_enabled}
        />
        Live position monitor (proactive risk call-outs in the assistant)
      </label>
      <p className="hint">
        Opt-in and <b>off by default</b>. When on, a background watcher checks your{' '}
        <b>open positions and day P&amp;L on real live prices</b> and speaks up in
        the assistant about the single most material risk right now — a stop about
        to hit, a position deep in the red, or nearing your daily loss limit. It{' '}
        <b>never trades</b>, only calls things out, and every number it states is
        real.{' '}
        {form.ai_enabled
          ? 'Turn on "Read replies & alerts aloud" in the assistant to hear them.'
          : 'Add an AI key (Credentials) to enable this.'}
      </p>
      <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <input
          type="checkbox"
          checked={form.ai_autopilot_enabled}
          onChange={(e) => setForm({ ...form, ai_autopilot_enabled: e.target.checked })}
          disabled={!form.ai_enabled}
        />
        Assistant autopilot (apply the assistant's safe actions automatically)
      </label>
      <p className="hint">
        Opt-in and <b>off by default</b>. When on, the assistant <b>applies the safe
        actions it proposes the moment it proposes them</b> — settings within the
        allowlist, starting/stopping the bot, price alerts, and <b>paper</b> orders —
        instead of waiting for a Confirm tap. This is what lets a hands-off user say
        "set me up safely and start" and have it actually happen. <b>Real-money (live)
        orders and switching paper↔live are never autopiloted</b> — those always need
        your explicit confirmation. Every outcome shown is the real result of the
        action, never a claim.{' '}
        {form.ai_enabled ? '' : 'Add an AI key (Credentials) to enable this.'}
      </p>
      <p className="hint">
        Trailing stop ratchets an open long's stop-loss upward as price rises to
        lock in gains (never loosened). Binance keys:{' '}
        {form.api_key_set ? '✅ set (live trading available)' : '⚠️ not set — add them below to trade live'}
        . Telegram alerts: {form.notifications_enabled ? '✅ on' : 'off'}.
      </p>

      <button className="btn primary" onClick={save}>
        Save settings
      </button>

      <div className="panel" style={{ marginTop: 6 }}>
        <div className="panel-head">Paper (simulated) data</div>
        <div className="panel-body">
          <p className="hint" style={{ marginTop: 0 }}>
            Start fresh: delete your <b>paper</b> trades and signal history and
            reset the simulated wallet to its starting balance. Your <b>real</b>{' '}
            (live) trades are never touched. This can't be undone.
          </p>
          <button className="btn" onClick={onResetPaper}>
            Reset paper data
          </button>
        </div>
      </div>

      <CredentialsCard
        me={me}
        onMeChanged={onMeChanged}
        onError={onError}
        onRefreshAccess={onRefreshAccess}
      />

      <div className="panel" style={{ marginTop: 6 }}>
        <div className="panel-head">Your TradingView webhook</div>
        <div className="panel-body">
          <p className="hint">Point your TradingView alert's webhook URL here (unique to your account — keep it private, it acts as your secret):</p>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <code className="inline" style={{ flex: 1, minWidth: 240, wordBreak: 'break-all' }}>{webhookUrl}</code>
            <button
              className="btn"
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(webhookUrl)
                  setCopied(true)
                  setTimeout(() => setCopied(false), 1500)
                } catch {
                  /* clipboard blocked — user can select manually */
                }
              }}
            >
              {copied ? 'Copied ✓' : 'Copy'}
            </button>
          </div>
          <p className="hint" style={{ marginTop: 10 }}>Alert message (JSON) — no secret needed, the URL token authenticates you:</p>
          <pre className="code">{alertExample}</pre>
          <details className="help" style={{ marginTop: 10 }}>
            <summary>How do I connect TradingView?</summary>
            <div className="help-body">
              <p className="hint">
                TradingView has <b>no API key to paste</b> — the webhook URL above is
                your credential. TradingView just sends alerts to that URL; your bot
                holds the Binance keys and does the actual trading.
              </p>
              <ol className="hint">
                <li>Create a free account at{' '}
                  <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">tradingview.com</a>{' '}
                  (webhook alerts need a paid plan — Essential or higher).</li>
                <li>Open a chart, click the <b>alarm clock (Alerts)</b> icon → <b>Create Alert</b>.</li>
                <li>Set your condition (e.g. a strategy or indicator crossover).</li>
                <li>Under <b>Notifications</b>, tick <b>Webhook URL</b> and paste the URL above.</li>
                <li>In the alert's <b>Message</b> box, paste the JSON shown above (edit side/symbol as needed).</li>
                <li>Save. When the alert fires, TradingView calls your bot and it places the order on Binance.</li>
              </ol>
            </div>
          </details>
        </div>
      </div>
    </div>
  )
}

function CredentialsCard({
  me,
  onMeChanged,
  onError,
  onRefreshAccess,
}: {
  me: Me
  onMeChanged: (m: Me) => void
  onError: (msg: string) => void
  onRefreshAccess: () => Promise<ExchangeAccess | null>
}) {
  const [binKey, setBinKey] = useState('')
  const [binSecret, setBinSecret] = useState('')
  const [testnet, setTestnet] = useState(me.binance_testnet)
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState('')

  const disabled = !me.secrets_storage_enabled

  const save = async () => {
    setBusy(true)
    setSaved('')
    try {
      const body: Record<string, unknown> = { binance_testnet: testnet }
      if (binKey.trim()) body.binance_api_key = binKey.trim()
      if (binSecret.trim()) body.binance_api_secret = binSecret.trim()
      const updated = await api.updateCredentials(body)
      onMeChanged(updated)
      setBinKey('')
      setBinSecret('')
      // Don't just save-and-go quiet: immediately re-probe the exchange with the
      // new keys and report the REAL result (connected / read-only / geo-blocked)
      // so the user actually sees whether they're live, not an empty box.
      setSaved('Credentials saved (encrypted at rest). Testing connection…')
      const acc = await onRefreshAccess()
      if (!acc) {
        setSaved('Credentials saved (encrypted at rest). Could not run the connection test — hit “Test connection” above.')
      } else if (acc.can_trade) {
        setSaved(`✅ Saved & connected — ${acc.exchange ?? 'exchange'} keys work and trading is enabled (${acc.testnet ? 'testnet' : 'live'}).`)
      } else if (acc.can_read_account) {
        setSaved(`⚠️ Saved — keys authenticate and can read your account, but trading isn't available yet. ${acc.detail}`)
      } else {
        setSaved(`⚠️ Saved, but not connected: ${acc.detail}`)
      }
    } catch (e) {
      onError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="panel" style={{ marginTop: 6 }}>
      <div className="panel-head">Your Binance API keys</div>
      <div className="panel-body" style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        {disabled ? (
          <p className="hint" style={{ color: 'var(--red)' }}>
            ⚠️ Key storage is disabled: the operator has not set SECRET_KEY, so keys
            cannot be encrypted. Ask the administrator to configure it.
          </p>
        ) : (
          <p className="hint">
            Use <b>trade-only</b> keys (no withdrawal permission). Keys are encrypted
            at rest and never shown again. Binance keys currently{' '}
            {me.binance_keys_set ? '✅ set' : '❌ not set'}. The AI assistant is{' '}
            <b>built into the app</b> — {me.ai_key_set ? '✅ active for everyone' : 'not configured by the operator yet'}, so you don't enter any AI key.
          </p>
        )}
        <details className="help">
          <summary>How do I get my Binance API key?</summary>
          <div className="help-body">
            <p className="hint">
              <b>Live trading (real funds):</b>
            </p>
            <ol className="hint">
              <li>Log in at binance.com → profile menu → <b>API Management</b>.</li>
              <li>Click <b>Create API</b> → <b>System generated</b>, name it e.g. "trading-bot", pass 2FA.</li>
              <li>Copy the <b>API Key</b> and <b>Secret Key</b> — the secret is shown only once.</li>
              <li>In the key's permissions, turn ON <b>Enable Spot &amp; Margin Trading</b>. Leave <b>Enable Withdrawals</b> OFF.</li>
              <li>Paste both below and save.</li>
            </ol>
            <p className="hint">
              <b>Testnet (fake money, no risk):</b> go to testnet.binance.vision, log in
              with GitHub, <b>Generate HMAC_SHA256 Key</b> with <b>Spot enabled</b>, then
              tick "Use Binance testnet" below. (A testnet key without Spot permission
              causes a -2015 error.)
            </p>
            <p className="hint" style={{ color: 'var(--red)' }}>
              ⚠️ Only ever create <b>trade-only</b> keys (withdrawals disabled), and never
              share your Secret with anyone. Your keys are encrypted and tied to your
              account only — no other user can see or use them.
            </p>
          </div>
        </details>
        <div className="row">
          <div className="field">
            <label>Binance API key</label>
            <input
              className="input"
              type="password"
              value={binKey}
              onChange={(e) => setBinKey(e.target.value)}
              placeholder={me.binance_keys_set ? 'unchanged' : 'paste key'}
              disabled={disabled}
              autoComplete="off"
            />
          </div>
          <div className="field">
            <label>Binance API secret</label>
            <input
              className="input"
              type="password"
              value={binSecret}
              onChange={(e) => setBinSecret(e.target.value)}
              placeholder={me.binance_keys_set ? 'unchanged' : 'paste secret'}
              disabled={disabled}
              autoComplete="off"
            />
          </div>
        </div>
        <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <input
            type="checkbox"
            checked={testnet}
            onChange={(e) => setTestnet(e.target.checked)}
            disabled={disabled}
          />
          Use Binance testnet (recommended until you have verified everything)
        </label>
        {saved && <p className="hint" style={{ color: 'var(--green)' }}>{saved}</p>}
        <button className="btn primary" onClick={save} disabled={disabled || busy}>
          {busy ? 'Saving…' : 'Save API keys'}
        </button>
      </div>
    </div>
  )
}
