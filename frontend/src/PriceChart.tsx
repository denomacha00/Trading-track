import { useEffect, useRef, useState } from 'react'
import {
  createChart,
  ColorType,
  CrosshairMode,
  LineStyle,
  type AutoscaleInfo,
  type CandlestickData,
  type HistogramData,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type ISeriesPrimitive,
  type ISeriesPrimitivePaneRenderer,
  type ISeriesPrimitivePaneView,
  type LogicalRange,
  type MouseEventParams,
  type SeriesAttachedParameter,
  type SeriesMarker,
  type Time,
} from 'lightweight-charts'
import type { Candle } from './types'
import type { IctAnalysis, IctZone } from './types'
import type { Theme } from './theme'
import { sma, ema, bollinger, vwap, rsi, macd, hma, donchian, keltner, stochastic, atr, obv, type IndicatorPrefs, type LinePoint } from './indicators'
import { volumeProfile, type VolumeProfile } from './volumeProfile'
import { priceDecimals, fmtPrice, priceMinMove } from './priceFormat'
import type { ChartMarker } from './chartMarkers'
import { DEFAULT_ICT_OVERLAYS, ICT_COLORS, type IctOverlayPrefs } from './ictOverlays'
import {
  loadDrawings,
  saveDrawings,
  newDrawingId,
  pointNearSegment,
  pointNearRect,
  pointNearHLine,
  pointNearRay,
  channelOffset,
  fibLevels,
  positionStats,
  measure,
  TOOL_ANCHORS,
  type Drawing,
  type Pt,
  type Tool,
} from './drawings'
import {
  nearestAlert,
  conditionForDrag,
  sanitizeAlertPrice,
  type AlertHandle,
} from './alertDrag'

// Candlestick price chart powered by TradingView's lightweight-charts library.
//
// Live like an exchange chart: `candles` seeds the history, and `last` (a live
// ticker price polled every few seconds) moves the newest bar in real time via
// series.update() — the forming candle's close/high/low track the market
// without waiting for the next full OHLCV reload. Under the price sits a volume
// histogram; an OHLC + volume legend follows the crosshair (defaulting to the
// latest bar), and a countdown shows the time left on the forming candle — the
// same read-outs a TradingView chart gives you. Colours come from the active
// theme's CSS variables so it re-themes with the rest of the app.
// Gently widen the price series' auto-scale so it also brackets NEARBY `levels`
// (the open position's entry / stop / target) — but never so far that it squashes
// the candles. The candles always keep the vertical space first (TradingView-style):
// a level only pins into the fit while it sits within half the candles' own range
// of them, so a stop/target a few % away on a zoomed-in 1m chart is NOT forced in
// (that flattened the candles into a thin strip). A far level stays a drawn line
// you scroll the price axis up/down to reach — it is not "fixed" into view. Style/
// scale only — it never invents a level; it just brackets real numbers handed in.
function extendAutoscale(base: AutoscaleInfo | null, levels: number[]): AutoscaleInfo | null {
  if (!base) {
    // No candle range yet (data still loading): briefly bracket the levels alone.
    let lo = Infinity
    let hi = -Infinity
    for (const p of levels) {
      if (Number.isFinite(p) && p > 0) {
        lo = Math.min(lo, p)
        hi = Math.max(hi, p)
      }
    }
    return lo === Infinity ? base : { priceRange: { minValue: lo, maxValue: hi } }
  }
  const { minValue, maxValue } = base.priceRange
  const span = maxValue - minValue
  // How far beyond the candles' own range a level may sit and still be pinned in.
  // Bounded to half the candle span so the fit can grow at most ~2x (candles keep
  // ≳50% of the height); span 0 (flat/loading) allows any level through.
  const pad = span > 0 ? span * 0.5 : Infinity
  let lo = minValue
  let hi = maxValue
  let changed = false
  for (const p of levels) {
    if (!Number.isFinite(p) || p <= 0) continue
    if (p >= minValue - pad && p <= maxValue + pad) {
      lo = Math.min(lo, p)
      hi = Math.max(hi, p)
      changed = true
    }
  }
  return changed ? { priceRange: { minValue: lo, maxValue: hi }, margins: base.margins } : base
}

type Palette = {
  bg: string
  text: string
  grid: string
  up: string
  down: string
}

function readPalette(): Palette {
  const s = getComputedStyle(document.documentElement)
  const v = (name: string, fallback: string) => s.getPropertyValue(name).trim() || fallback
  return {
    bg: v('--chart-bg', '#151b24'),
    text: v('--muted', '#8b98a9'),
    grid: v('--border', '#263241'),
    up: v('--green', '#16c784'),
    down: v('--red', '#ea3943'),
  }
}

// Translucent bar colours for the volume histogram — a secondary layer that
// reads clearly under the candles on either theme.
const VOL_UP = 'rgba(38, 166, 154, 0.45)'
const VOL_DOWN = 'rgba(239, 83, 80, 0.45)'

// Oscillator sub-panes live in their own charts stacked under price, each with
// its own y-scale (RSI/Stoch 0–100, MACD centred on 0, ATR in price units, OBV a
// cumulative volume total). Line colours are fixed literals (not theme-driven) so
// the panes read consistently; the MACD histogram uses the theme's up/down. Every
// value is real math on the candles.
type OscKind = 'rsi' | 'macd' | 'stoch' | 'atr' | 'obv'
const OSC_ORDER: OscKind[] = ['rsi', 'macd', 'stoch', 'atr', 'obv']
const RSI_COLOR = '#d1a1ff'
const MACD_LINE = '#3b82f6'
const MACD_SIGNAL = '#f0b90b'
const STOCH_K = '#22d3ee'
const STOCH_D = '#f0b90b'
const ATR_COLOR = '#fb923c'
const OBV_COLOR = '#4ade80'
// One live sub-pane: its chart, the line series it draws, the optional histogram
// (MACD), a floating value label, and the range handler we subscribed for sync.
type SubPane = {
  chart: IChartApi
  lines: ISeriesApi<'Line'>[]
  hist?: ISeriesApi<'Histogram'>
  label: HTMLDivElement | null
  rangeHandler: (r: LogicalRange | null) => void
}

// --- Drawing tools --------------------------------------------------------
// The left-edge toolbar's buttons and a small preset palette. All the geometry,
// data model, hit-testing and persistence live in ./drawings (pure + unit-
// tested); this component only wires mouse events and canvas rendering to it.
const DRAW_TOOLS: { key: Tool; glyph: string; label: string }[] = [
  { key: 'cursor', glyph: '↖', label: 'Cursor — click a drawing to select, right-click it to remove' },
  { key: 'trend', glyph: '╱', label: 'Trend line — click start, then click end' },
  { key: 'ray', glyph: '↗', label: 'Ray — click start, then a point on it; the line extends to the right edge' },
  { key: 'hline', glyph: '─', label: 'Horizontal line — click a price level' },
  { key: 'rect', glyph: '▭', label: 'Rectangle — click two opposite corners' },
  { key: 'channel', glyph: '⫽', label: 'Parallel channel — click two points for the line, then a third to set its width' },
  { key: 'fib', glyph: 'φ', label: 'Fibonacci retracement — click the swing start, then the swing end' },
  { key: 'measure', glyph: '↔', label: 'Measure — click two points to read Δprice, Δ% and bar count' },
  { key: 'position', glyph: '⇅', label: 'Position tool — click entry, then target, then stop (shows reward:risk)' },
]
const DRAW_COLORS = ['#2962ff', '#f0b90b', '#16c784', '#ea3943']
const HIT_TOL = 6 // px — how near a click must land to select a drawing
// Fixed profit/loss tints for the position tool, independent of the stroke
// colour so the reward zone always reads green and the risk zone red.
const POS_REWARD = '#16c784'
const POS_RISK = '#ea3943'

// Tiny media-space canvas helpers (no deps). Coordinates are CSS pixels, which
// is exactly what priceToCoordinate / timeToCoordinate return.
function strokeSeg(ctx: CanvasRenderingContext2D, ax: number, ay: number, bx: number, by: number) {
  ctx.beginPath()
  ctx.moveTo(ax, ay)
  ctx.lineTo(bx, by)
  ctx.stroke()
}
// A small square endpoint handle, drawn only on the selected drawing.
function strokeHandle(ctx: CanvasRenderingContext2D, x: number, y: number, color: string) {
  ctx.save()
  ctx.fillStyle = '#ffffff'
  ctx.strokeStyle = color
  ctx.lineWidth = 1.5
  ctx.beginPath()
  ctx.rect(x - 3, y - 3, 6, 6)
  ctx.fill()
  ctx.stroke()
  ctx.restore()
}
// A compact text label on a translucent dark plate so it stays legible over the
// candles in either theme. (x,y) is the plate's top-left unless align==='right',
// in which case the plate's RIGHT edge sits at x (used to keep labels on-screen
// at the right edge). Returns the plate width so callers can stack labels.
function fillLabel(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  text: string,
  color: string,
  align: 'left' | 'right' = 'left',
): number {
  ctx.save()
  ctx.font = '11px ui-sans-serif, system-ui, -apple-system, sans-serif'
  const padX = 4
  const w = Math.ceil(ctx.measureText(text).width) + padX * 2
  const h = 15
  const bx = align === 'right' ? x - w : x
  ctx.fillStyle = 'rgba(15,17,26,0.72)'
  ctx.fillRect(bx, y, w, h)
  ctx.textBaseline = 'middle'
  ctx.textAlign = 'left'
  ctx.fillStyle = color
  ctx.fillText(text, bx + padX, y + h / 2 + 0.5)
  ctx.restore()
  return w
}

// Seconds per candle — used to count down to the forming bar's close (the
// "time left" read-out, like TradingView) and to snap trade markers onto their
// bar. Frames we don't map show no timer.
export const TF_SECONDS: Record<string, number> = {
  '1m': 60,
  '5m': 300,
  '15m': 900,
  '30m': 1800,
  '1h': 3600,
  '2h': 7200,
  '4h': 14400,
  '6h': 21600,
  '12h': 43200,
  '1d': 86400,
  '1w': 604800,
}

// Compact volume (1.23K / 4.56M / 7.89B) so a busy bar doesn't overflow.
function fmtVol(v: number | undefined): string {
  if (v == null || !Number.isFinite(v)) return '—'
  const a = Math.abs(v)
  if (a >= 1e9) return (v / 1e9).toFixed(2) + 'B'
  if (a >= 1e6) return (v / 1e6).toFixed(2) + 'M'
  if (a >= 1e3) return (v / 1e3).toFixed(2) + 'K'
  return v.toFixed(a < 1 ? 4 : 2)
}

// mm:ss, or h:mm:ss once an hour or more remains.
function fmtDur(secs: number): string {
  const s = Math.max(0, Math.floor(secs))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const ss = s % 60
  const p2 = (n: number) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${p2(m)}:${p2(ss)}` : `${p2(m)}:${p2(ss)}`
}

export function PriceChart({
  candles,
  theme,
  last,
  liveBar,
  fitKey,
  symbol,
  timeframe,
  priceLines,
  alerts,
  onAlertMove,
  indicators,
  markers,
  clearSignal,
  ict,
  ictOverlays,
  fullscreen,
  zoomLock,
  compare,
}: {
  candles: Candle[]
  theme: Theme
  last?: number | null
  // A full OHLCV frame for the forming/just-closed candle, straight from a
  // real-time kline WebSocket. When present it drives the newest bar (including
  // rolling over to a brand-new candle the instant the market opens one) and the
  // scalar `last` path is skipped — this is the exchange-grade live update. When
  // null (no stream) the chart falls back to moving the last bar by `last`.
  liveBar?: (Candle & { closed?: boolean }) | null
  // Changes when the symbol/timeframe changes. The chart re-fits the view only
  // when this changes (or on first data) so periodic reloads don't yank the
  // user's pan/zoom back — an exchange chart stays where you left it.
  fitKey?: string
  symbol?: string
  timeframe?: string
  // Horizontal reference levels drawn on the price axis (real "marking"): armed
  // price alerts and open-position entry / stop-loss / take-profit levels. Each
  // is a genuine number from the user's OWN data — nothing decorative or faked.
  // `dashed` / `width` let a caller make a line stand out (e.g. an open
  // position's entry / stop / target) versus a faint reference (armed alerts).
  // `scale: true` also pins the level inside the vertical auto-fit so it stays
  // on-screen on any timeframe. All three are style/scale only — they never
  // change WHICH real number is drawn.
  priceLines?: { price: number; color?: string; title?: string; dashed?: boolean; width?: 1 | 2 | 3 | 4; scale?: boolean }[]
  // Armed price alerts drawn as DRAGGABLE horizontal lines (TradingView-style):
  // grab the line and slide it to a new price to re-arm the alert there. Each is
  // a real user alert — nothing invented. Kept separate from `priceLines` (which
  // stays static) so only these respond to the drag handlers. `onAlertMove` is
  // called with the dropped price and the honest condition (above/below the last
  // price); the parent persists it (PATCH /api/alerts/{id}) and feeds the fresh
  // list back down. Undefined/empty = no draggable alerts.
  alerts?: { id: number; price: number; condition: 'above' | 'below'; color?: string; title?: string }[]
  onAlertMove?: (id: number, price: number, condition: 'above' | 'below') => void
  // Which moving-average / band / VWAP overlays to draw, all computed from the
  // real candles above. Undefined = none (unchanged plain chart).
  indicators?: IndicatorPrefs
  // Buy/sell arrows on the exact bars where the user's OWN trades opened and
  // closed (see tradesToMarkers). Real trade history only — undefined or empty
  // means no markers; nothing here is ever invented.
  markers?: ChartMarker[]
  // Bumped by the parent to wipe every hand-drawn line (the assistant's "clear the
  // drawings" command). A change in value is the trigger; the initial value is a
  // no-op so mounting never clears the user's saved drawings.
  clearSignal?: number
  // The REAL, computed ICT / smart-money read for this symbol/timeframe (from
  // /api/analyze), or null when ICT is off / thin data / the read errored. The
  // chart draws only what's actually here — never a fabricated level.
  ict?: IctAnalysis | null
  // Which ICT overlays to draw. Undefined = the module defaults.
  ictOverlays?: IctOverlayPrefs
  // True while the chart is the full-screen overlay. On a phone this governs who
  // gets touch gestures: locked (the default) sends pinch/drag to the CHART so
  // pinching zooms the candles instead of the whole page; unlocked hands pinch
  // back to the browser so the user can zoom the entire app. Ignored on desktop
  // (the mouse wheel always zooms the chart) — it only shapes `touch-action`.
  fullscreen?: boolean
  // In full screen, true = chart owns the zoom (page can't pinch-zoom), false =
  // page owns the zoom. Undefined is treated as locked. No effect when not
  // full-screen, where the page scrolls/zooms exactly as before.
  zoomLock?: boolean
  // A second instrument to overlay for correlation (TradingView "Compare"). Its
  // real closes are drawn as a line on an independent LEFT price scale (so wildly
  // different price magnitudes each use their own vertical range and you compare
  // shape/timing), time-aligned to the main bars upstream. `pct` is that symbol's
  // move over the shown window, for the legend. Null = no overlay. Real data only.
  compare?: { label: string; color?: string; data: { time: number; value: number }[]; pct?: number | null } | null
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volumeRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  // The compare-symbol overlay line (TradingView "Compare"), on its own left
  // price scale. Lazily created/removed by its effect; null when no overlay.
  const compareRef = useRef<ISeriesApi<'Line'> | null>(null)
  // The newest bar, kept current so live ticks extend it rather than reset it.
  const lastBarRef = useRef<CandlestickData | null>(null)
  const lastVolRef = useRef<number | undefined>(undefined)
  // The price-axis decimal precision currently applied to the candle series,
  // derived from the asset's magnitude (see priceDecimals) so a sub-cent coin
  // reads its real value on the axis/crosshair instead of collapsing to "0.00".
  const priceDpRef = useRef<number>(2)
  // True while the pointer is over the chart, so live updates don't fight the
  // crosshair read-out for the bar the user is inspecting.
  const hoveringRef = useRef(false)
  const paletteRef = useRef<Palette | null>(null)
  const legendRef = useRef<HTMLSpanElement>(null)
  const countdownRef = useRef<HTMLDivElement>(null)
  // Tracks whether we've fitted the view, and for which symbol/timeframe.
  const didFitRef = useRef(false)
  const fitKeyRef = useRef<string | undefined>(undefined)
  // Horizontal price lines we've drawn (alert / SL / TP / entry markers), kept so
  // we can clear and redraw them when the set changes.
  const priceLineObjsRef = useRef<IPriceLine[]>([])
  // Levels the vertical auto-scale must keep in view (open position entry/stop/
  // target). Read live by the series' autoscaleInfoProvider; see extendAutoscale.
  const scaleLevelsRef = useRef<number[]>([])
  // Draggable armed alerts, drawn as native price lines kept by id so a single
  // one can be moved live during a drag and the set reconciled when it changes.
  const alertLineObjsRef = useRef<Map<number, IPriceLine>>(new Map())
  const alertsRef = useRef<{ id: number; price: number; condition: 'above' | 'below'; color?: string; title?: string }[]>([])
  const onAlertMoveRef = useRef<typeof onAlertMove>(onAlertMove)
  // Drag in progress: which alert, and its original price/condition to restore
  // if the drag ends on an invalid price. Null when nothing is being dragged.
  const alertDragRef = useRef<{ id: number; origPrice: number; origCond: 'above' | 'below' } | null>(null)
  // Fullscreen / zoom-lock mirrored so the drag can restore the exact
  // touch-action the tool effect would otherwise own (that effect doesn't re-run
  // after a drag). Kept fresh in the tool effect below.
  const fullscreenRef = useRef<boolean | undefined>(fullscreen)
  const zoomLockRef = useRef<boolean | undefined>(zoomLock)
  // Indicator overlay line series (EMA/SMA/Bollinger/VWAP), keyed so we can add,
  // update, or remove one without disturbing the candles or the others.
  const overlayRef = useRef<Map<string, ISeriesApi<'Line'>>>(new Map())
  // Oscillator sub-panes (RSI/MACD/Stoch/ATR/OBV): each is its own synced chart
  // under price. The container divs are always in the DOM; a chart is created
  // inside one only while its oscillator is switched on, torn down when off.
  const rsiPaneRef = useRef<HTMLDivElement>(null)
  const macdPaneRef = useRef<HTMLDivElement>(null)
  const stochPaneRef = useRef<HTMLDivElement>(null)
  const atrPaneRef = useRef<HTMLDivElement>(null)
  const obvPaneRef = useRef<HTMLDivElement>(null)
  const rsiLabelRef = useRef<HTMLDivElement>(null)
  const macdLabelRef = useRef<HTMLDivElement>(null)
  const stochLabelRef = useRef<HTMLDivElement>(null)
  const atrLabelRef = useRef<HTMLDivElement>(null)
  const obvLabelRef = useRef<HTMLDivElement>(null)
  const subPanesRef = useRef<Map<OscKind, SubPane>>(new Map())
  // Every chart (price + active sub-panes) so a pan/zoom on any one drives the
  // rest; the guard stops the programmatic echo from looping back.
  const allChartsRef = useRef<Set<IChartApi>>(new Set())
  const syncingRef = useRef(false)

  // --- Drawing-tools state --------------------------------------------------
  // React state drives the toolbar; matching refs give the canvas renderer and
  // the once-created mouse handlers a synchronous read of the latest values.
  const [tool, setTool] = useState<Tool>('cursor')
  const [drawings, setDrawings] = useState<Drawing[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [color, setColor] = useState<string>(DRAW_COLORS[0])
  const toolRef = useRef<Tool>('cursor')
  const colorRef = useRef<string>(DRAW_COLORS[0])
  const drawingsRef = useRef<Drawing[]>([])
  const selectedRef = useRef<string | null>(null)
  // Anchors already clicked for the drawing in progress, in order. Two-click
  // tools (trend/rect/ray/fib/measure) hold one here awaiting the second click;
  // the three-click tools (channel/position) hold up to two. Empty = nothing
  // being placed. TOOL_ANCHORS says how many each tool needs before it commits.
  const pendingRef = useRef<Pt[]>([])
  // Latest pointer position in data space, for the rubber-band preview.
  const hoverRef = useRef<Pt | null>(null)
  // Seconds per bar for this timeframe, kept fresh from the prop each render so
  // the measure tool (built once at mount) reads the current bar duration when
  // it counts how many bars a span covers.
  const barSecRef = useRef(0)
  // Handle to the attached primitive's requestUpdate, so any state change can
  // ask lightweight-charts to repaint the drawing layer.
  const drawViewRef = useRef<{ requestUpdate: () => void } | null>(null)
  // Volume Profile (VPVR): the computed profile to paint up the right edge, and
  // whether it's switched on. The drawing primitive's renderer reads both so the
  // profile repaints in step with pans/zooms and theme flips.
  const vpRef = useRef<VolumeProfile | null>(null)
  const vpOnRef = useRef<boolean>(false)
  // The ICT read + which overlays to draw, mirrored into refs so the primitive's
  // canvas renderer reads the latest synchronously (like the drawings/VP refs).
  const ictRef = useRef<IctAnalysis | null>(null)
  const ictPrefsRef = useRef<IctOverlayPrefs>(DEFAULT_ICT_OVERLAYS)
  // Persistence bookkeeping: which symbol|timeframe is currently loaded, and a
  // one-shot flag so the load itself doesn't immediately re-save.
  const loadedKeyRef = useRef<string>('')
  const skipSaveRef = useRef(false)
  // Latest delete/cancel actions, so the window keydown handler (created once)
  // always calls the current closures.
  const actionsRef = useRef<{ del: () => void; cancel: () => void }>({ del: () => {}, cancel: () => {} })

  // Mirror one chart's visible range onto every other chart so price and its
  // oscillator panes pan and zoom as one. The guard swallows the echo that the
  // programmatic setVisibleLogicalRange would otherwise bounce back.
  const syncRange = (self: IChartApi, range: LogicalRange | null) => {
    if (!range || syncingRef.current) return
    syncingRef.current = true
    for (const c of allChartsRef.current) {
      if (c !== self) {
        try {
          c.timeScale().setVisibleLogicalRange(range)
        } catch {
          /* chart torn down mid-sync */
        }
      }
    }
    syncingRef.current = false
  }

  // Tear one oscillator pane down: unsubscribe its sync, drop it from the synced
  // set, remove the chart, and blank its label. Safe to call when absent.
  const destroySubPane = (kind: OscKind) => {
    const pane = subPanesRef.current.get(kind)
    if (!pane) return
    try {
      pane.chart.timeScale().unsubscribeVisibleLogicalRangeChange(pane.rangeHandler)
    } catch {
      /* already gone */
    }
    allChartsRef.current.delete(pane.chart)
    try {
      pane.chart.remove()
    } catch {
      /* already removed */
    }
    if (pane.label) pane.label.textContent = ''
    subPanesRef.current.delete(kind)
  }


  // Paint the OHLC + volume legend for one bar. Values are all numeric, so
  // writing them via innerHTML is safe; the symbol/timeframe label is rendered
  // by React below (never interpolated here) to stay XSS-safe for typed pairs.
  const renderLegend = (bar: CandlestickData, vol: number | undefined) => {
    const el = legendRef.current
    const p = paletteRef.current
    if (!el || !p) return
    const chg = bar.close - bar.open
    const chgPct = bar.open ? (chg / bar.open) * 100 : 0
    const col = chg >= 0 ? p.up : p.down
    const sign = chg >= 0 ? '+' : ''
    // One precision for the whole row (from the close) so O/H/L/C/chg line up and
    // a sub-cent asset shows its real figures instead of "0.00".
    const dp = priceDecimals(bar.close)
    el.innerHTML =
      `<span class="cl-k">O</span><span class="cl-v">${fmtPrice(bar.open, dp)}</span>` +
      `<span class="cl-k">H</span><span class="cl-v">${fmtPrice(bar.high, dp)}</span>` +
      `<span class="cl-k">L</span><span class="cl-v">${fmtPrice(bar.low, dp)}</span>` +
      `<span class="cl-k">C</span><span class="cl-v">${fmtPrice(bar.close, dp)}</span>` +
      `<span class="cl-chg" style="color:${col}">${sign}${fmtPrice(chg, dp)} (${sign}${chgPct.toFixed(2)}%)</span>` +
      `<span class="cl-k">Vol</span><span class="cl-v" style="color:${col}">${fmtVol(vol)}</span>`
  }

  // Create the chart once on mount.
  useEffect(() => {
    if (!containerRef.current) return
    const p = readPalette()
    paletteRef.current = p
    const chart = createChart(containerRef.current, {
      layout: { background: { type: ColorType.Solid, color: p.bg }, textColor: p.text },
      grid: { vertLines: { color: p.grid }, horzLines: { color: p.grid } },
      timeScale: { borderColor: p.grid, timeVisible: true },
      rightPriceScale: { borderColor: p.grid, minimumWidth: 68 },
      crosshair: { mode: CrosshairMode.Normal },
      autoSize: true,
    })
    const series = chart.addCandlestickSeries({
      upColor: p.up,
      downColor: p.down,
      borderVisible: false,
      wickUpColor: p.up,
      wickDownColor: p.down,
      // Keep the open position's entry/stop/target inside the vertical fit on
      // every timeframe (reads scaleLevelsRef live; updated by the effect below).
      autoscaleInfoProvider: (orig: () => AutoscaleInfo | null) =>
        extendAutoscale(orig(), scaleLevelsRef.current),
    })
    // Leave room at the bottom for the volume histogram (its own overlay scale).
    series.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.26 } })
    const volume = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: '',
    })
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } })
    chartRef.current = chart
    seriesRef.current = series
    volumeRef.current = volume

    // Follow the crosshair: show the hovered bar, or fall back to the latest.
    const onMove = (param: MouseEventParams<Time>) => {
      const s = seriesRef.current
      if (!s) return
      // Capture the pointer in data space for the drawing rubber-band preview,
      // then only repaint the drawing layer while a placement is in progress.
      if (param.point) {
        const pr = s.coordinateToPrice(param.point.y)
        let tm = param.time as number | undefined
        if (tm == null && chartRef.current) {
          const t = chartRef.current.timeScale().coordinateToTime(param.point.x)
          tm = (t as number | null) ?? undefined
        }
        hoverRef.current = pr != null && tm != null ? { time: tm, price: pr } : null
        if (pendingRef.current.length) drawViewRef.current?.requestUpdate()
      } else {
        hoverRef.current = null
      }
      if (param.time && param.seriesData.size) {
        const cd = param.seriesData.get(s) as CandlestickData | undefined
        const vd = volumeRef.current
          ? (param.seriesData.get(volumeRef.current) as HistogramData | undefined)
          : undefined
        if (cd) {
          hoveringRef.current = true
          renderLegend(cd, vd?.value)
          return
        }
      }
      hoveringRef.current = false
      if (lastBarRef.current) renderLegend(lastBarRef.current, lastVolRef.current)
    }
    chart.subscribeCrosshairMove(onMove)

    // Register price as the anchor of the synced group and keep the sub-panes'
    // time axes locked to whatever range the user drags price to.
    allChartsRef.current.add(chart)
    const onMainRange = (r: LogicalRange | null) => syncRange(chart, r)
    chart.timeScale().subscribeVisibleLogicalRangeChange(onMainRange)

    // Paint the Volume Profile (VPVR) up the right edge: one horizontal bar per
    // price bucket, its width proportional to the REAL volume that traded there,
    // the fattest (POC) highlighted. Translucent so price stays readable. Drawn
    // before the user's drawings so those sit on top; y comes from the live price
    // scale so the bars stay pinned to their price as the chart pans/zooms.
    const renderVolumeProfile = (ctx: CanvasRenderingContext2D, width: number) => {
      if (!vpOnRef.current) return
      const vp = vpRef.current
      const s = seriesRef.current
      if (!vp || !s || vp.maxVolume <= 0) return
      const maxW = Math.max(40, width * 0.3) // widest bar spans ~30% of the plot
      // A row is in the Value Area when its band overlaps [VAL, VAH].
      const inVa = (lo: number, hi: number) =>
        vp.val != null && vp.vah != null && hi > vp.val && lo < vp.vah
      ctx.save()
      for (const row of vp.rows) {
        if (row.volume <= 0) continue
        const yHi = s.priceToCoordinate(row.hi)
        const yLo = s.priceToCoordinate(row.lo)
        if (yHi == null || yLo == null) continue
        const top = Math.min(yHi, yLo)
        const barH = Math.max(1, Math.abs(yLo - yHi) - 1) // 1px gap between rows
        const w = (row.volume / vp.maxVolume) * maxW
        const isPoc = vp.poc != null && row.lo <= vp.poc && vp.poc <= row.hi
        // POC gold; value-area rows a solid blue; the tails outside the ~70% band
        // faded so the accepted range reads clearly (TradingView's VA shading).
        ctx.fillStyle = isPoc
          ? 'rgba(240,185,11,0.55)'
          : inVa(row.lo, row.hi)
            ? 'rgba(91,141,239,0.34)'
            : 'rgba(91,141,239,0.14)'
        ctx.fillRect(width - w, top, w, barH)
      }
      // VAH / VAL boundary lines with small labels, spanning the profile's width.
      const vaLine = (price: number, label: string) => {
        const y = s.priceToCoordinate(price)
        if (y == null) return
        ctx.strokeStyle = 'rgba(91,141,239,0.7)'
        ctx.lineWidth = 1
        ctx.setLineDash([4, 3])
        ctx.beginPath()
        ctx.moveTo(width - maxW, y)
        ctx.lineTo(width, y)
        ctx.stroke()
        ctx.setLineDash([])
        ctx.font = '10px sans-serif'
        ctx.fillStyle = 'rgba(150,180,255,0.95)'
        ctx.textAlign = 'right'
        ctx.textBaseline = label === 'VAH' ? 'bottom' : 'top'
        ctx.fillText(label, width - 2, y)
      }
      if (vp.vah != null) vaLine(vp.vah, 'VAH')
      if (vp.val != null) vaLine(vp.val, 'VAL')
      ctx.restore()
    }
    // Paint the computed ICT / smart-money read (see app/ict.py). Every layer is
    // gated by a toggle and drawn ONLY from real computed levels — nothing here is
    // invented. Zones extend to the right edge (they stay relevant until price
    // mitigates them); mitigated zones are drawn faint and unlabelled so the live
    // structure stands out. Drawn under the user's own drawings.
    const renderIct = (ctx: CanvasRenderingContext2D, width: number) => {
      const a = ictRef.current
      const prefs = ictPrefsRef.current
      const s = seriesRef.current
      const c = chartRef.current
      const pal = paletteRef.current
      if (!a || !s || !c || !pal) return
      const ts = c.timeScale()
      const yOf = (price: number) => s.priceToCoordinate(price)
      const xOf = (time: number | null) => (time == null ? null : ts.timeToCoordinate(time as Time))
      const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))
      // Outlined text reads on any candle/background and in both themes.
      const label = (x: number, y: number, text: string, color: string, align: CanvasTextAlign = 'left') => {
        ctx.save()
        ctx.font = '10px system-ui, -apple-system, sans-serif'
        ctx.textAlign = align
        ctx.textBaseline = 'middle'
        ctx.lineJoin = 'round'
        ctx.lineWidth = 3
        ctx.strokeStyle = pal.bg
        ctx.strokeText(text, x, y)
        ctx.fillStyle = color
        ctx.fillText(text, x, y)
        ctx.restore()
      }
      const hline = (price: number, color: string, dash: number[], w: number, tag?: string) => {
        const y = yOf(price)
        if (y == null) return
        ctx.save()
        ctx.strokeStyle = color
        ctx.lineWidth = w
        ctx.setLineDash(dash)
        strokeSeg(ctx, 0, y, width, y)
        ctx.setLineDash([])
        ctx.restore()
        if (tag) label(width - 4, y, tag, color, 'right')
      }
      const zoneTag = (z: IctZone): string => {
        switch (z.subtype) {
          case 'order-block': return 'OB'
          case 'fvg': return z.void ? 'FVG•void' : z.inverted ? 'iFVG' : 'FVG'
          case 'breaker': return 'BRK'
          case 'rejection': return 'REJ'
          case 'bpr': return 'BPR'
          case 'volume-imbalance': return 'VI'
          default: return ''
        }
      }
      const drawZone = (z: IctZone) => {
        const yTop = yOf(z.top)
        const yBot = yOf(z.bottom)
        if (yTop == null || yBot == null) return
        const xc = xOf(z.time)
        const x0 = xc == null ? 0 : clamp(xc, 0, width)
        const top = Math.min(yTop, yBot)
        const h = Math.max(1, Math.abs(yBot - yTop))
        const col = z.kind === 'bullish' ? ICT_COLORS.bull : ICT_COLORS.bear
        ctx.save()
        ctx.fillStyle = col
        ctx.globalAlpha = z.mitigated ? 0.05 : 0.13
        ctx.fillRect(x0, top, Math.max(0, width - x0), h)
        ctx.globalAlpha = 1
        if (!z.mitigated) {
          ctx.strokeStyle = col
          ctx.lineWidth = 1
          ctx.setLineDash(z.inverted ? [3, 3] : [])
          ctx.strokeRect(x0 + 0.5, top + 0.5, Math.max(0, width - x0 - 1), Math.max(1, h - 1))
          ctx.setLineDash([])
          if (h >= 12) label(x0 + 4, top + 7, zoneTag(z), col)
        }
        ctx.restore()
      }

      ctx.save()
      // Dealing range: shade premium (red) above equilibrium and discount (green)
      // below, mark the 50% equilibrium, the range extremes, and the OTE bands.
      const dr = a.dealing_range
      if (prefs.dealingRange && dr) {
        const yHi = yOf(dr.high)
        const yLo = yOf(dr.low)
        const yEq = yOf(dr.equilibrium)
        if (yHi != null && yLo != null && yEq != null) {
          ctx.save()
          ctx.globalAlpha = 0.05
          ctx.fillStyle = ICT_COLORS.bear
          ctx.fillRect(0, Math.min(yHi, yEq), width, Math.abs(yEq - yHi))
          ctx.fillStyle = ICT_COLORS.bull
          ctx.fillRect(0, Math.min(yEq, yLo), width, Math.abs(yLo - yEq))
          ctx.globalAlpha = 1
          ctx.restore()
          // OTE (0.62–0.79) — the classic optimal-trade-entry retracement band.
          const oteBand = (band: [number, number], col: string) => {
            const y1 = yOf(band[0])
            const y2 = yOf(band[1])
            if (y1 == null || y2 == null) return
            ctx.save()
            ctx.globalAlpha = 0.14
            ctx.fillStyle = col
            ctx.fillRect(0, Math.min(y1, y2), width, Math.max(1, Math.abs(y2 - y1)))
            ctx.globalAlpha = 1
            ctx.restore()
          }
          oteBand(dr.ote_discount, ICT_COLORS.bull)
          oteBand(dr.ote_premium, ICT_COLORS.bear)
          hline(dr.equilibrium, ICT_COLORS.equilibrium, [2, 3], 1, 'EQ 50%')
          hline(dr.high, ICT_COLORS.equilibrium, [1, 4], 1, 'range H')
          hline(dr.low, ICT_COLORS.equilibrium, [1, 4], 1, 'range L')
        }
      }

      // Prior day/week highs & lows — major draws on liquidity.
      if (prefs.keyLevels && a.key_levels) {
        const kl = a.key_levels
        if (kl.pdh != null) hline(kl.pdh, ICT_COLORS.keyLevel, [6, 3], 1, 'PDH')
        if (kl.pdl != null) hline(kl.pdl, ICT_COLORS.keyLevel, [6, 3], 1, 'PDL')
        if (kl.pwh != null) hline(kl.pwh, ICT_COLORS.keyLevel, [2, 2], 1, 'PWH')
        if (kl.pwl != null) hline(kl.pwl, ICT_COLORS.keyLevel, [2, 2], 1, 'PWL')
      }
      // Resting liquidity pools (unswept), with EQH/EQL called out. The nearest
      // unswept pool above/below is the "draw on liquidity" — flag it stronger.
      if (prefs.liquidity) {
        const drawAbove = a.draw_on_liquidity?.above ?? null
        const drawBelow = a.draw_on_liquidity?.below ?? null
        for (const pool of a.liquidity) {
          if (pool.swept) continue
          const isDraw =
            (drawAbove != null && pool.index === drawAbove.index && pool.price === drawAbove.price) ||
            (drawBelow != null && pool.index === drawBelow.index && pool.price === drawBelow.price)
          const tag = pool.equal
            ? pool.kind === 'buy-side' ? 'EQH' : 'EQL'
            : isDraw ? (pool.kind === 'buy-side' ? 'draw↑' : 'draw↓') : undefined
          hline(pool.price, ICT_COLORS.liquidity, isDraw ? [] : [2, 3], isDraw ? 1.5 : 1, tag)
        }
      }

      // Zones (drawn least → most significant so the key ones sit on top).
      if (prefs.volumeImbalance) for (const z of a.volume_imbalances) drawZone(z)
      if (prefs.bpr) for (const z of a.bpr) drawZone(z)
      if (prefs.rejection) for (const z of a.rejection_blocks) drawZone(z)
      if (prefs.breakers) for (const z of a.breakers) drawZone(z)
      if (prefs.fvg) for (const z of a.fvgs) drawZone(z)
      if (prefs.orderBlocks) for (const z of a.order_blocks) drawZone(z)
      // Liquidity sweeps (stop hunts): a wick past the level that closed back in.
      if (prefs.sweeps) {
        for (const sw of a.sweeps) {
          const x = xOf(sw.time)
          const yLvl = yOf(sw.level)
          const yExt = yOf(sw.extreme)
          if (x == null || yLvl == null || yExt == null) continue
          ctx.save()
          ctx.strokeStyle = ICT_COLORS.sweep
          ctx.lineWidth = 1.5
          strokeSeg(ctx, x, yLvl, x, yExt)
          ctx.fillStyle = ICT_COLORS.sweep
          ctx.beginPath()
          ctx.arc(x, yExt, 2.5, 0, Math.PI * 2)
          ctx.fill()
          ctx.restore()
          label(x + 4, yExt, sw.side === 'buy-side' ? 'BSL✕' : 'SSL✕', ICT_COLORS.sweep)
        }
      }

      // Structure: BOS (continuation) / CHoCH (reversal); CHoCH + displacement = MSS.
      if (prefs.structure) {
        for (const ev of a.events) {
          const y = yOf(ev.level)
          if (y == null) continue
          const xc = xOf(ev.time)
          const x1 = xc == null ? width : clamp(xc, 0, width)
          const col = ev.direction === 'bull' ? ICT_COLORS.bull : ICT_COLORS.bear
          ctx.save()
          ctx.strokeStyle = col
          ctx.lineWidth = 1
          ctx.setLineDash([4, 2])
          strokeSeg(ctx, Math.max(0, x1 - 52), y, Math.min(width, x1 + 6), y)
          ctx.setLineDash([])
          ctx.restore()
          const name = ev.kind === 'CHoCH' && ev.displacement ? 'MSS' : ev.kind
          label(Math.min(width - 2, x1 + 8), y, name, col)
        }
      }

      // Confirmed swing pivots (fractals) — the skeleton structure is built from.
      if (prefs.swings) {
        for (const swg of a.swings) {
          const x = xOf(swg.time)
          const y = yOf(swg.price)
          if (x == null || y == null) continue
          ctx.save()
          ctx.fillStyle = pal.text
          ctx.globalAlpha = 0.7
          ctx.beginPath()
          ctx.arc(x, y, 2, 0, Math.PI * 2)
          ctx.fill()
          ctx.restore()
        }
      }

      ctx.restore()
    }
    // Paint every saved drawing (plus the in-progress preview) onto the price
    // pane each frame, projecting data anchors to pixels through the live scales
    // so lines stay pinned to their bar/price as the chart pans and zooms.
    const renderDrawings = (ctx: CanvasRenderingContext2D, width: number) => {
      const s = seriesRef.current
      const c = chartRef.current
      if (!s || !c) return
      const ts = c.timeScale()
      const px = (pt: Pt) => {
        const y = s.priceToCoordinate(pt.price)
        const x = ts.timeToCoordinate(pt.time as Time)
        return x == null || y == null ? null : { x, y }
      }
      const box = (a: { x: number; y: number }, b: { x: number; y: number }) =>
        [Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y)] as const
      // Far endpoint of the ray a→b at whichever vertical chart edge it heads
      // toward, so the line runs off-screen like TradingView's Ray. A vertical
      // ray just shoots far up or down.
      const rayEnd = (a: { x: number; y: number }, b: { x: number; y: number }) => {
        const dx = b.x - a.x
        const dy = b.y - a.y
        if (dx === 0) return { x: b.x, y: dy >= 0 ? 1e5 : -1e5 }
        const tx = dx > 0 ? width : 0
        const k = (tx - a.x) / dx
        return { x: tx, y: a.y + dy * k }
      }
      const dp = priceDpRef.current // price precision shared by every label
      const bs = barSecRef.current // seconds/bar, for the measure read-out
      ctx.save()
      for (const d of drawingsRef.current) {
        const sel = d.id === selectedRef.current
        ctx.strokeStyle = d.color
        ctx.lineWidth = sel ? 2.5 : 1.5
        if (d.kind === 'hline') {
          const y = s.priceToCoordinate(d.price)
          if (y == null) continue
          strokeSeg(ctx, 0, y, width, y)
        } else if (d.kind === 'trend') {
          const a = px(d.a)
          const b = px(d.b)
          if (!a || !b) continue
          strokeSeg(ctx, a.x, a.y, b.x, b.y)
          if (sel) { strokeHandle(ctx, a.x, a.y, d.color); strokeHandle(ctx, b.x, b.y, d.color) }
        } else if (d.kind === 'ray') {
          const a = px(d.a)
          const b = px(d.b)
          if (!a || !b) continue
          const far = rayEnd(a, b)
          strokeSeg(ctx, a.x, a.y, far.x, far.y)
          if (sel) { strokeHandle(ctx, a.x, a.y, d.color); strokeHandle(ctx, b.x, b.y, d.color) }
        } else if (d.kind === 'rect') {
          const a = px(d.a)
          const b = px(d.b)
          if (!a || !b) continue
          const [rx, ry, rw, rh] = box(a, b)
          ctx.globalAlpha = sel ? 0.14 : 0.08
          ctx.fillStyle = d.color
          ctx.fillRect(rx, ry, rw, rh)
          ctx.globalAlpha = 1
          ctx.strokeRect(rx, ry, rw, rh)
          if (sel) { strokeHandle(ctx, a.x, a.y, d.color); strokeHandle(ctx, b.x, b.y, d.color) }
        } else if (d.kind === 'fib') {
          const a = px(d.a)
          const b = px(d.b)
          if (!a || !b) continue
          const x0 = Math.min(a.x, b.x)
          for (const lv of fibLevels(d.a, d.b)) {
            const ly = s.priceToCoordinate(lv.price)
            if (ly == null) continue
            ctx.globalAlpha = sel ? 0.9 : 0.55
            ctx.lineWidth = lv.ratio === 0 || lv.ratio === 1 ? (sel ? 2 : 1.5) : 1
            strokeSeg(ctx, x0, ly, width, ly)
            ctx.globalAlpha = 1
            fillLabel(ctx, x0 + 2, ly - 7, `${(lv.ratio * 100).toFixed(1)}%  ${fmtPrice(lv.price, dp)}`, d.color)
          }
          if (sel) { strokeHandle(ctx, a.x, a.y, d.color); strokeHandle(ctx, b.x, b.y, d.color) }
        } else if (d.kind === 'measure') {
          const a = px(d.a)
          const b = px(d.b)
          if (!a || !b) continue
          const m = measure(d.a, d.b, bs)
          const dc = m.direction === 'up' ? POS_REWARD : m.direction === 'down' ? POS_RISK : d.color
          const [rx, ry, rw, rh] = box(a, b)
          ctx.globalAlpha = 0.1
          ctx.fillStyle = dc
          ctx.fillRect(rx, ry, rw, rh)
          ctx.globalAlpha = 1
          ctx.strokeStyle = dc
          ctx.setLineDash([3, 3])
          strokeSeg(ctx, a.x, a.y, b.x, b.y)
          ctx.setLineDash([])
          const arrow = m.direction === 'up' ? '▲' : m.direction === 'down' ? '▼' : '▶'
          const sp = m.dPrice >= 0 ? '+' : ''
          const pp = m.dPct >= 0 ? '+' : ''
          fillLabel(ctx, b.x + 4, b.y - 7, `${arrow} ${sp}${fmtPrice(m.dPrice, dp)} (${pp}${m.dPct.toFixed(2)}%) · ${m.bars} bars`, dc)
          if (sel) { strokeHandle(ctx, a.x, a.y, d.color); strokeHandle(ctx, b.x, b.y, d.color) }
        } else if (d.kind === 'channel') {
          const a = px(d.a)
          const b = px(d.b)
          const cc = px(d.c)
          if (!a || !b) continue
          const off = channelOffset(d.a, d.b, d.c)
          const a2 = px({ time: d.a.time, price: d.a.price + off })
          const b2 = px({ time: d.b.time, price: d.b.price + off })
          if (a2 && b2) {
            ctx.globalAlpha = sel ? 0.12 : 0.07
            ctx.fillStyle = d.color
            ctx.beginPath()
            ctx.moveTo(a.x, a.y)
            ctx.lineTo(b.x, b.y)
            ctx.lineTo(b2.x, b2.y)
            ctx.lineTo(a2.x, a2.y)
            ctx.closePath()
            ctx.fill()
            ctx.globalAlpha = 1
            strokeSeg(ctx, a2.x, a2.y, b2.x, b2.y)
          }
          strokeSeg(ctx, a.x, a.y, b.x, b.y)
          if (sel) { strokeHandle(ctx, a.x, a.y, d.color); strokeHandle(ctx, b.x, b.y, d.color); if (cc) strokeHandle(ctx, cc.x, cc.y, d.color) }
        } else {
          // Position planner: entry (a), target (b, its time = right edge), stop
          // (c.price). Green reward zone entry→target, red risk zone entry→stop.
          const e = px(d.a)
          if (!e) continue
          const st = positionStats(d.a.price, d.b.price, d.c.price)
          const rp = px(d.b)
          const yT = s.priceToCoordinate(d.b.price)
          const yS = s.priceToCoordinate(d.c.price)
          const xL = Math.min(e.x, rp?.x ?? e.x)
          const xR = Math.max(e.x, rp?.x ?? width)
          const w2 = Math.max(8, xR - xL)
          if (yT != null) {
            ctx.globalAlpha = sel ? 0.2 : 0.12
            ctx.fillStyle = POS_REWARD
            ctx.fillRect(xL, Math.min(e.y, yT), w2, Math.abs(yT - e.y))
          }
          if (yS != null) {
            ctx.globalAlpha = sel ? 0.2 : 0.12
            ctx.fillStyle = POS_RISK
            ctx.fillRect(xL, Math.min(e.y, yS), w2, Math.abs(yS - e.y))
          }
          ctx.globalAlpha = 1
          ctx.strokeStyle = d.color
          ctx.lineWidth = sel ? 2 : 1.5
          strokeSeg(ctx, xL, e.y, xL + w2, e.y)
          ctx.setLineDash([3, 3])
          if (yT != null) { ctx.strokeStyle = POS_REWARD; strokeSeg(ctx, xL, yT, xL + w2, yT) }
          if (yS != null) { ctx.strokeStyle = POS_RISK; strokeSeg(ctx, xL, yS, xL + w2, yS) }
          ctx.setLineDash([])
          fillLabel(ctx, xL + w2, e.y - 7, `${st.dir.toUpperCase()} @ ${fmtPrice(d.a.price, dp)} · R:R ${st.rr.toFixed(2)}`, d.color, 'right')
          if (yT != null) fillLabel(ctx, xL + w2, yT - 7, `TP ${fmtPrice(d.b.price, dp)} +${st.rewardPct.toFixed(2)}%`, POS_REWARD, 'right')
          if (yS != null) fillLabel(ctx, xL + w2, yS - 7, `SL ${fmtPrice(d.c.price, dp)} -${st.riskPct.toFixed(2)}%`, POS_RISK, 'right')
          if (sel) { strokeHandle(ctx, e.x, e.y, d.color); if (rp && yT != null) strokeHandle(ctx, rp.x, yT, d.color); if (yS != null) strokeHandle(ctx, xL + w2, yS, d.color) }
        }
      }
      // Rubber-band preview from the anchors placed so far to the pointer,
      // dashed until the drawing commits. Mirrors what the next click(s) will
      // lay down, for every placing tool (not just trend/rect).
      const pend = pendingRef.current
      const hov = hoverRef.current
      const t = toolRef.current
      if (pend.length && hov) {
        const p0 = px(pend[0])
        const hv = px(hov)
        if (p0 && hv) {
          ctx.strokeStyle = colorRef.current
          ctx.lineWidth = 1.5
          ctx.setLineDash([4, 4])
          if (t === 'rect' || t === 'fib') {
            const [rx, ry, rw, rh] = box(p0, hv)
            ctx.strokeRect(rx, ry, rw, rh)
          } else if (t === 'ray') {
            const far = rayEnd(p0, hv)
            strokeSeg(ctx, p0.x, p0.y, far.x, far.y)
          } else if ((t === 'channel' || t === 'position') && pend.length >= 2) {
            const p1 = px(pend[1])
            if (p1) {
              ctx.setLineDash([])
              strokeSeg(ctx, p0.x, p0.y, p1.x, p1.y)
              ctx.setLineDash([4, 4])
              if (t === 'channel') {
                const off = channelOffset(pend[0], pend[1], hov)
                const a2 = px({ time: pend[0].time, price: pend[0].price + off })
                const b2 = px({ time: pend[1].time, price: pend[1].price + off })
                if (a2 && b2) strokeSeg(ctx, a2.x, a2.y, b2.x, b2.y)
              } else {
                strokeSeg(ctx, p0.x, hv.y, p1.x, hv.y)
              }
            }
          } else {
            strokeSeg(ctx, p0.x, p0.y, hv.x, hv.y)
          }
          ctx.setLineDash([])
        }
      }
      ctx.restore()
    }
    // Attach one primitive to the candle series; its single pane view renders
    // the whole drawing layer on top of price. requestUpdate (captured on
    // attach) lets any state change trigger a repaint of that layer.
    const paneRenderer: ISeriesPrimitivePaneRenderer = {
      draw: (target) => {
        target.useMediaCoordinateSpace(({ context, mediaSize }) => {
          renderVolumeProfile(context, mediaSize.width)
          renderIct(context, mediaSize.width)
          renderDrawings(context, mediaSize.width)
        })
      },
    }
    const paneView: ISeriesPrimitivePaneView = {
      renderer: () => paneRenderer,
      zOrder: () => 'top',
    }
    let requestUpdate: (() => void) | null = null
    const primitive: ISeriesPrimitive<Time> = {
      paneViews: () => [paneView],
      attached: (param: SeriesAttachedParameter<Time>) => {
        requestUpdate = param.requestUpdate
      },
      detached: () => {
        requestUpdate = null
      },
    }
    series.attachPrimitive(primitive)
    drawViewRef.current = { requestUpdate: () => requestUpdate?.() }
    // Place / select on click. In cursor mode a click selects the nearest
    // drawing (or clears the selection). A tool click lays down an anchor: one
    // click for an h-line, two for a trend line or rectangle. Times come from
    // param.time (already snapped to a bar) so anchors sit on real candles.
    const onClick = (param: MouseEventParams<Time>) => {
      const s = seriesRef.current
      const c = chartRef.current
      if (!s || !c || !param.point) return
      const price = s.coordinateToPrice(param.point.y)
      if (price == null) return
      let time = param.time as number | undefined
      if (time == null) {
        const t = c.timeScale().coordinateToTime(param.point.x)
        time = (t as number | null) ?? undefined
      }
      const activeTool = toolRef.current
      if (activeTool === 'cursor') {
        selectAt(param.point.x, param.point.y)
        return
      }
      if (activeTool === 'hline') {
        commit({ id: newDrawingId(), kind: 'hline', price, color: colorRef.current })
        return
      }
      if (time == null) return // every other tool anchors on a bar time
      // Collect anchors click by click; commit once the tool has all it needs
      // (2 for trend/ray/rect/fib/measure, 3 for channel/position).
      const anchors = [...pendingRef.current, { time, price }]
      const need = TOOL_ANCHORS[activeTool]
      if (anchors.length < need) {
        pendingRef.current = anchors
        drawViewRef.current?.requestUpdate()
        return
      }
      pendingRef.current = []
      const id = newDrawingId()
      const col = colorRef.current
      if (activeTool === 'channel' || activeTool === 'position') {
        commit({ id, kind: activeTool, a: anchors[0], b: anchors[1], c: anchors[2], color: col })
      } else {
        commit({ id, kind: activeTool, a: anchors[0], b: anchors[1], color: col })
      }
    }
    chart.subscribeClick(onClick)
    // Commit a finished drawing: append it, drop back to the cursor, and select
    // the new object so it can be deleted immediately. (Declared as a function
    // so onClick above can call it regardless of order — hoisting.)
    function commit(d: Drawing) {
      const next = [...drawingsRef.current, d]
      drawingsRef.current = next
      setDrawings(next)
      pendingRef.current = []
      hoverRef.current = null
      toolRef.current = 'cursor'
      setTool('cursor')
      selectedRef.current = d.id
      setSelected(d.id)
      drawViewRef.current?.requestUpdate()
    }
    // Hit-test a point (chart-pane pixels) against the drawings, topmost first,
    // and return the id of the first within tolerance, else null. Shared by
    // click-to-select and right-click-to-remove.
    function hitTest(x: number, y: number): string | null {
      const s = seriesRef.current
      const c = chartRef.current
      if (!s || !c) return null
      const ts = c.timeScale()
      const px = (pt: Pt) => {
        const py = s.priceToCoordinate(pt.price)
        const pxx = ts.timeToCoordinate(pt.time as Time)
        return pxx == null || py == null ? null : { x: pxx, y: py }
      }
      const list = drawingsRef.current
      for (let i = list.length - 1; i >= 0; i--) {
        const d = list[i]
        if (d.kind === 'hline') {
          const ly = s.priceToCoordinate(d.price)
          if (ly != null && pointNearHLine(y, ly, HIT_TOL)) return d.id
        } else if (d.kind === 'trend' || d.kind === 'measure') {
          const a = px(d.a)
          const b = px(d.b)
          if (a && b && pointNearSegment({ x, y }, a, b, HIT_TOL)) return d.id
        } else if (d.kind === 'ray') {
          const a = px(d.a)
          const b = px(d.b)
          if (a && b && pointNearRay({ x, y }, a, b, HIT_TOL)) return d.id
        } else if (d.kind === 'rect') {
          const a = px(d.a)
          const b = px(d.b)
          if (a && b && pointNearRect({ x, y }, a, b, HIT_TOL)) return d.id
        } else if (d.kind === 'fib') {
          const a = px(d.a)
          const b = px(d.b)
          if (!a || !b) continue
          const x0 = Math.min(a.x, b.x)
          if (x >= x0 - HIT_TOL) {
            for (const lv of fibLevels(d.a, d.b)) {
              const ly = s.priceToCoordinate(lv.price)
              if (ly != null && pointNearHLine(y, ly, HIT_TOL)) return d.id
            }
          }
        } else if (d.kind === 'channel') {
          const a = px(d.a)
          const b = px(d.b)
          if (!a || !b) continue
          const off = channelOffset(d.a, d.b, d.c)
          const a2 = px({ time: d.a.time, price: d.a.price + off })
          const b2 = px({ time: d.b.time, price: d.b.price + off })
          if (pointNearSegment({ x, y }, a, b, HIT_TOL)) return d.id
          if (a2 && b2 && pointNearSegment({ x, y }, a2, b2, HIT_TOL)) return d.id
        } else {
          // position: the outline of the zone box, plus the entry line
          const e = px(d.a)
          if (!e) continue
          const rp = px(d.b)
          const paneW = containerRef.current?.clientWidth ?? 0
          const yT = s.priceToCoordinate(d.b.price)
          const yS = s.priceToCoordinate(d.c.price)
          const xR = rp?.x ?? paneW
          const ys = [e.y, yT, yS].filter((v) => v != null).map(Number)
          const tl = { x: Math.min(e.x, xR), y: Math.min(...ys) }
          const br = { x: Math.max(e.x, xR), y: Math.max(...ys) }
          if (pointNearRect({ x, y }, tl, br, HIT_TOL)) return d.id
          if (pointNearHLine(y, e.y, HIT_TOL) && x >= tl.x - HIT_TOL && x <= br.x + HIT_TOL) return d.id
        }
      }
      return null
    }
    // Select the drawing under a click (or clear the selection when it misses).
    function selectAt(x: number, y: number) {
      const hit = hitTest(x, y)
      selectedRef.current = hit
      setSelected(hit)
      drawViewRef.current?.requestUpdate()
    }
    // Right-click straight on a drawing removes it (TradingView-style — no need
    // to switch to the cursor and press Delete first). Off any drawing we leave
    // the browser's own menu alone. Coordinates come from the container's rect,
    // which lines up with the price pane's top-left, same space hitTest expects.
    const onContextMenu = (e: MouseEvent) => {
      const el = containerRef.current
      if (!el) return
      const r = el.getBoundingClientRect()
      const id = hitTest(e.clientX - r.left, e.clientY - r.top)
      if (!id) return
      e.preventDefault()
      const next = drawingsRef.current.filter((d) => d.id !== id)
      drawingsRef.current = next
      setDrawings(next)
      if (selectedRef.current === id) {
        selectedRef.current = null
        setSelected(null)
      }
      drawViewRef.current?.requestUpdate()
    }
    containerRef.current?.addEventListener('contextmenu', onContextMenu)

    // --- Drag-to-move price alerts (TradingView-style) --------------------
    // Grab an armed alert's dashed line and slide it to a new price; on release
    // the parent persists the move (re-arming the alert). Only in cursor mode —
    // a drawing tool owns clicks. Pointer events cover mouse AND touch. We freeze
    // chart pan/scale for the drag so the candles don't slide under the line, and
    // capture the pointer so the whole gesture routes here, then restore both.
    const ALERT_GRAB_PX = 8
    const alertHandles = (): AlertHandle[] => {
      const s = seriesRef.current
      if (!s) return []
      const out: AlertHandle[] = []
      for (const a of alertsRef.current) {
        const y = s.priceToCoordinate(a.price)
        if (y != null) out.push({ id: a.id, y, price: a.price, condition: a.condition })
      }
      return out
    }
    const restoreTouchAction = () => {
      const el = containerRef.current
      if (!el) return
      el.style.touchAction = !fullscreenRef.current
        ? ''
        : zoomLockRef.current === false
          ? 'pinch-zoom'
          : 'none'
    }
    const onAlertPointerDown = (e: PointerEvent) => {
      if (e.button > 0) return // primary button / touch only (right-click deletes)
      if (toolRef.current !== 'cursor') return // a drawing tool owns the click
      const el = containerRef.current
      const s = seriesRef.current
      if (!el || !s) return
      const r = el.getBoundingClientRect()
      const id = nearestAlert(e.clientY - r.top, alertHandles(), ALERT_GRAB_PX)
      if (id == null) return
      const a = alertsRef.current.find((x) => x.id === id)
      if (!a) return
      alertDragRef.current = { id, origPrice: a.price, origCond: a.condition }
      chart.applyOptions({ handleScroll: false, handleScale: false })
      el.style.cursor = 'ns-resize'
      el.style.touchAction = 'none'
      try {
        el.setPointerCapture(e.pointerId)
      } catch {
        /* capture unsupported */
      }
      e.preventDefault()
      e.stopPropagation()
    }
    const onAlertPointerMove = (e: PointerEvent) => {
      const el = containerRef.current
      const s = seriesRef.current
      if (!el || !s) return
      const r = el.getBoundingClientRect()
      const y = e.clientY - r.top
      const drag = alertDragRef.current
      if (!drag) {
        // Hover cue: the grab cursor when hovering an alert line in cursor mode.
        if (toolRef.current === 'cursor') {
          el.style.cursor = nearestAlert(y, alertHandles(), ALERT_GRAB_PX) != null ? 'ns-resize' : ''
        }
        return
      }
      const price = sanitizeAlertPrice((s.coordinateToPrice(y) as number | null) ?? Number.NaN)
      if (price == null) return
      const ln = alertLineObjsRef.current.get(drag.id)
      if (ln) {
        try {
          ln.applyOptions({ price })
        } catch {
          /* series torn down mid-drag */
        }
      }
      e.preventDefault()
    }
    const endAlertDrag = (e: PointerEvent, commit: boolean) => {
      const drag = alertDragRef.current
      if (!drag) return
      alertDragRef.current = null
      const el = containerRef.current
      const s = seriesRef.current
      try {
        el?.releasePointerCapture(e.pointerId)
      } catch {
        /* not captured */
      }
      if (el) el.style.cursor = ''
      restoreTouchAction()
      chart.applyOptions({ handleScroll: true, handleScale: true })
      const ln = alertLineObjsRef.current.get(drag.id)
      const r = el?.getBoundingClientRect()
      const price =
        commit && s && r
          ? sanitizeAlertPrice((s.coordinateToPrice(e.clientY - r.top) as number | null) ?? Number.NaN)
          : null
      // Invalid drop or no real move → snap the line back to where it was.
      if (price == null || Math.abs(price - drag.origPrice) < drag.origPrice * 1e-9) {
        if (ln) {
          try {
            ln.applyOptions({ price: drag.origPrice })
          } catch {
            /* gone */
          }
        }
        return
      }
      const last = lastBarRef.current?.close ?? null
      const cond = conditionForDrag(price, last, drag.origCond)
      onAlertMoveRef.current?.(drag.id, price, cond)
    }
    const onAlertPointerUp = (e: PointerEvent) => endAlertDrag(e, true)
    const onAlertPointerCancel = (e: PointerEvent) => endAlertDrag(e, false)
    const alertEl = containerRef.current
    alertEl?.addEventListener('pointerdown', onAlertPointerDown, true)
    alertEl?.addEventListener('pointermove', onAlertPointerMove)
    alertEl?.addEventListener('pointerup', onAlertPointerUp)
    alertEl?.addEventListener('pointercancel', onAlertPointerCancel)

    // Keyboard: Delete/Backspace removes the selected drawing; Escape cancels an
    // in-progress placement or clears the selection. Ignored while a form field
    // is focused so it never eats typing elsewhere in the app. Delegates to the
    // latest actions (kept fresh in a ref) so this once-bound handler stays live.
    const onKeyDown = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      const tag = t?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || t?.isContentEditable) return
      if (e.key === 'Delete' || e.key === 'Backspace') {
        if (selectedRef.current) {
          e.preventDefault()
          actionsRef.current.del()
        }
      } else if (e.key === 'Escape') {
        actionsRef.current.cancel()
      }
    }
    window.addEventListener('keydown', onKeyDown)

    return () => {
      chart.unsubscribeCrosshairMove(onMove)
      chart.unsubscribeClick(onClick)
      window.removeEventListener('keydown', onKeyDown)
      containerRef.current?.removeEventListener('contextmenu', onContextMenu)
      alertEl?.removeEventListener('pointerdown', onAlertPointerDown, true)
      alertEl?.removeEventListener('pointermove', onAlertPointerMove)
      alertEl?.removeEventListener('pointerup', onAlertPointerUp)
      alertEl?.removeEventListener('pointercancel', onAlertPointerCancel)
      chart.timeScale().unsubscribeVisibleLogicalRangeChange(onMainRange)
      for (const kind of OSC_ORDER) destroySubPane(kind)
      try {
        series.detachPrimitive(primitive)
      } catch {
        /* series already gone with the chart */
      }
      drawViewRef.current = null
      allChartsRef.current.delete(chart)
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
      volumeRef.current = null
      overlayRef.current.clear()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Re-colour in place when the theme flips (no teardown, keeps the live bar).
  useEffect(() => {
    if (!chartRef.current || !seriesRef.current) return
    const p = readPalette()
    paletteRef.current = p
    chartRef.current.applyOptions({
      layout: { background: { type: ColorType.Solid, color: p.bg }, textColor: p.text },
      grid: { vertLines: { color: p.grid }, horzLines: { color: p.grid } },
      timeScale: { borderColor: p.grid },
      rightPriceScale: { borderColor: p.grid },
    })
    seriesRef.current.applyOptions({
      upColor: p.up,
      downColor: p.down,
      wickUpColor: p.up,
      wickDownColor: p.down,
    })
    if (lastBarRef.current && !hoveringRef.current) {
      renderLegend(lastBarRef.current, lastVolRef.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theme])

  // Seed / replace the full history when the candle set changes.
  useEffect(() => {
    if (!seriesRef.current || candles.length === 0) return
    const data: CandlestickData[] = candles.map((c) => ({
      time: c.time as Time,
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }))
    seriesRef.current.setData(data)
    // Match the price-axis / crosshair precision to this asset's magnitude so a
    // sub-cent coin shows its real price instead of "0.00" (lightweight-charts
    // defaults to 2 dp). Derived from the latest real close; only re-applied when
    // it actually changes so periodic reloads don't churn the series options.
    const repClose = data[data.length - 1]?.close
    if (repClose != null && Number.isFinite(repClose) && repClose > 0) {
      const dp = priceDecimals(repClose)
      if (dp !== priceDpRef.current) {
        priceDpRef.current = dp
        seriesRef.current.applyOptions({
          priceFormat: { type: 'price', precision: dp, minMove: priceMinMove(dp) },
        })
      }
    }
    if (volumeRef.current) {
      const vol: HistogramData[] = candles.map((c) => ({
        time: c.time as Time,
        value: c.volume,
        color: c.close >= c.open ? VOL_UP : VOL_DOWN,
      }))
      volumeRef.current.setData(vol)
    }
    lastBarRef.current = { ...data[data.length - 1] }
    lastVolRef.current = candles[candles.length - 1]?.volume
    if (!hoveringRef.current) renderLegend(lastBarRef.current, lastVolRef.current)
    // Fit the view on the first load and whenever the symbol/timeframe changes
    // (fitKey), but NOT on the periodic reloads of the same series — otherwise
    // every 10s refresh would snap the user's pan/zoom back to the full range.
    if (!didFitRef.current || fitKeyRef.current !== fitKey) {
      chartRef.current?.timeScale().fitContent()
      didFitRef.current = true
      fitKeyRef.current = fitKey
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candles, fitKey])

  // Move the newest bar live as the ticker price updates. Skipped entirely when
  // a real-time kline stream is feeding `liveBar` — that path is richer (true
  // OHLC + volume + rollover) and the two must not fight over the same bar.
  useEffect(() => {
    if (liveBar) return
    if (!seriesRef.current || last == null || !Number.isFinite(last) || last <= 0) return
    const bar = lastBarRef.current
    if (!bar) return
    const updated: CandlestickData = {
      time: bar.time,
      open: bar.open,
      high: Math.max(bar.high, last),
      low: Math.min(bar.low, last),
      close: last,
    }
    lastBarRef.current = updated
    seriesRef.current.update(updated)
    if (!hoveringRef.current) renderLegend(updated, lastVolRef.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [last, liveBar])

  // Real-time kline stream: move (and roll over) the newest bar from full OHLCV
  // frames — the same data an exchange chart draws — so a fresh candle appears
  // the instant the market opens it, not on the next REST reload. Out-of-order
  // frames older than the bar on screen are ignored.
  useEffect(() => {
    const series = seriesRef.current
    if (!series || !liveBar) return
    if (!Number.isFinite(liveBar.close) || liveBar.close <= 0) return
    const prev = lastBarRef.current
    if (prev && (liveBar.time as number) < (prev.time as number)) return
    const bar: CandlestickData = {
      time: liveBar.time as Time,
      open: liveBar.open,
      high: liveBar.high,
      low: liveBar.low,
      close: liveBar.close,
    }
    series.update(bar)
    lastBarRef.current = bar
    if (volumeRef.current && Number.isFinite(liveBar.volume)) {
      volumeRef.current.update({
        time: liveBar.time as Time,
        value: liveBar.volume,
        color: liveBar.close >= liveBar.open ? VOL_UP : VOL_DOWN,
      })
      lastVolRef.current = liveBar.volume
    }
    if (!hoveringRef.current) renderLegend(bar, lastVolRef.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveBar])

  // Count down to the forming candle's close (open time + one frame), ticking
  // every second. Frames without a known length simply show no timer.
  useEffect(() => {
    const el = countdownRef.current
    if (!el) return
    const secs = TF_SECONDS[timeframe ?? '']
    const lastBar = candles[candles.length - 1]
    if (!secs || !lastBar) {
      el.textContent = ''
      return
    }
    const tick = () => {
      // Prefer the live forming bar's open time (kept current by the live/stream
      // effects) so the timer rolls over the instant a new candle opens, not on
      // the next REST reload; fall back to the seeded last bar.
      const openT = (lastBarRef.current?.time as number) ?? (lastBar.time as number)
      el.textContent = '⏱ ' + fmtDur(openT + secs - Math.floor(Date.now() / 1000))
    }
    tick()
    const id = window.setInterval(tick, 1000)
    return () => window.clearInterval(id)
  }, [candles, timeframe])

  // Draw horizontal reference levels (real "marking"): armed price alerts and
  // open-position entry/SL/TP. Cleared and redrawn only when the set actually
  // changes (via a stable key) so live ticks never churn them. Every level is a
  // real number from the user's own data — the chart never invents a line.
  const priceLinesKey = JSON.stringify(
    (priceLines ?? []).map((l) => [l.price, l.color, l.title, l.dashed, l.width, l.scale]),
  )
  useEffect(() => {
    const series = seriesRef.current
    if (!series) return
    for (const ln of priceLineObjsRef.current) {
      try {
        series.removePriceLine(ln)
      } catch {
        /* series already torn down */
      }
    }
    priceLineObjsRef.current = []
    for (const pl of priceLines ?? []) {
      if (!Number.isFinite(pl.price) || pl.price <= 0) continue
      priceLineObjsRef.current.push(
        series.createPriceLine({
          price: pl.price,
          color: pl.color || '#8b98a9',
          lineWidth: pl.width ?? 1,
          lineStyle: pl.dashed === false ? LineStyle.Solid : LineStyle.Dashed,
          axisLabelVisible: true,
          title: pl.title || '',
        }),
      )
    }
    // Pin the flagged levels (open-position entry/stop/target) into the vertical
    // auto-fit, then re-apply the provider so the axis rescales immediately —
    // without this a far-off stop/target on a tight timeframe stays off-screen
    // until the next candle tick. Style/scale only; no level is invented.
    scaleLevelsRef.current = (priceLines ?? [])
      .filter((pl) => pl.scale && Number.isFinite(pl.price) && pl.price > 0)
      .map((pl) => pl.price)
    series.applyOptions({
      autoscaleInfoProvider: (orig: () => AutoscaleInfo | null) =>
        extendAutoscale(orig(), scaleLevelsRef.current),
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [priceLinesKey])

  // Draggable armed alerts. Rendered as native price lines (so they get a clean
  // axis label) but kept in a by-id map so the pointer handlers can move ONE of
  // them live during a drag. Reconciled — create new, update changed, remove
  // gone — only when the real alert set changes, never on a live tick. Every
  // line is a real user alert; the drag re-arms it at a real price (see the
  // pointer handlers in the mount effect and alertDrag.ts).
  const alertsKey = JSON.stringify(
    (alerts ?? []).map((a) => [a.id, a.price, a.condition, a.color, a.title]),
  )
  useEffect(() => {
    alertsRef.current = alerts ?? []
    onAlertMoveRef.current = onAlertMove
    const series = seriesRef.current
    if (!series) return
    const map = alertLineObjsRef.current
    const wanted = new Set<number>()
    for (const a of alerts ?? []) {
      if (!Number.isFinite(a.price) || a.price <= 0) continue
      wanted.add(a.id)
      const opts = {
        price: a.price,
        color: a.color || '#f0b90b',
        lineWidth: 2 as const,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: a.title || `⤿ ${a.condition}`,
      }
      const existing = map.get(a.id)
      if (existing) {
        try {
          existing.applyOptions(opts)
        } catch {
          /* series torn down */
        }
      } else {
        try {
          map.set(a.id, series.createPriceLine(opts))
        } catch {
          /* series torn down */
        }
      }
    }
    // Drop lines whose alert is gone.
    for (const [id, ln] of map) {
      if (!wanted.has(id)) {
        try {
          series.removePriceLine(ln)
        } catch {
          /* already gone */
        }
        map.delete(id)
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [alertsKey])

  // Keep the move callback fresh for the once-bound pointer handlers even when
  // the alert set itself hasn't changed.
  useEffect(() => {
    onAlertMoveRef.current = onAlertMove
  }, [onAlertMove])


  // opened and closed (see tradesToMarkers). Real history only. Re-applied when
  // the set changes AND when the candles reload, so a marker never vanishes on a
  // periodic refresh (setData can drop markers) and always sits on a real bar.
  const markersKey = JSON.stringify(markers ?? [])
  useEffect(() => {
    const series = seriesRef.current
    if (!series) return
    const list: SeriesMarker<Time>[] = (markers ?? []).map((m) => ({
      time: m.time as Time,
      position: m.position,
      color: m.color,
      shape: m.shape,
      text: m.text,
    }))
    series.setMarkers(list)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [markersKey, candles])

  // Indicator overlays: moving averages, Bollinger Bands and VWAP, each a real
  // line computed from the candles above. We reconcile against what's on screen
  // — add a newly-enabled line, drop a disabled one, refresh values on reload —
  // so toggling one never churns the others or the candles. Times align to the
  // bars, so the overlays sit exactly on price.
  const indKey = JSON.stringify(indicators ?? {})
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const p = indicators
    const specs: { key: string; color: string; data: LinePoint[] }[] = []
    if (p?.ema9) specs.push({ key: 'ema9', color: '#f0b90b', data: ema(candles, 9) })
    if (p?.ema21) specs.push({ key: 'ema21', color: '#3b82f6', data: ema(candles, 21) })
    if (p?.sma50) specs.push({ key: 'sma50', color: '#a855f7', data: sma(candles, 50) })
    if (p?.sma200) specs.push({ key: 'sma200', color: '#9aa7b8', data: sma(candles, 200) })
    if (p?.vwap) specs.push({ key: 'vwap', color: '#e6c200', data: vwap(candles) })
    if (p?.bb) {
      const bb = bollinger(candles, 20, 2)
      specs.push({ key: 'bbUpper', color: 'rgba(120,144,180,0.9)', data: bb.upper })
      specs.push({ key: 'bbBasis', color: 'rgba(120,144,180,0.45)', data: bb.basis })
      specs.push({ key: 'bbLower', color: 'rgba(120,144,180,0.9)', data: bb.lower })
    }
    if (p?.donchian) {
      const dc = donchian(candles, 20)
      specs.push({ key: 'dcUpper', color: 'rgba(45,212,191,0.95)', data: dc.upper })
      specs.push({ key: 'dcBasis', color: 'rgba(45,212,191,0.45)', data: dc.basis })
      specs.push({ key: 'dcLower', color: 'rgba(45,212,191,0.95)', data: dc.lower })
    }
    if (p?.keltner) {
      const kc = keltner(candles, 20, 10, 2)
      specs.push({ key: 'kcUpper', color: 'rgba(251,146,60,0.95)', data: kc.upper })
      specs.push({ key: 'kcBasis', color: 'rgba(251,146,60,0.45)', data: kc.basis })
      specs.push({ key: 'kcLower', color: 'rgba(251,146,60,0.95)', data: kc.lower })
    }
    if (p?.hma) specs.push({ key: 'hma', color: '#ec4899', data: hma(candles, 55) })
    const want = new Set(specs.map((s) => s.key))
    const map = overlayRef.current
    for (const [key, series] of map) {
      if (!want.has(key)) {
        try {
          chart.removeSeries(series)
        } catch {
          /* chart already torn down */
        }
        map.delete(key)
      }
    }
    for (const spec of specs) {
      let series = map.get(spec.key)
      if (!series) {
        series = chart.addLineSeries({
          color: spec.color,
          lineWidth: 2,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
        })
        map.set(spec.key, series)
      } else {
        series.applyOptions({ color: spec.color })
      }
      series.setData(spec.data.map((pt) => ({ time: pt.time as Time, value: pt.value })))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candles, indKey])

  // Compare-symbol overlay (TradingView "Compare"). Draws the second instrument's
  // real closes as a line on an INDEPENDENT left price scale, so a $60k asset and
  // a sub-dollar one each fill their own vertical range and you read correlation of
  // shape/timing rather than absolute magnitude. Deliberately isolated from the
  // candle / live-tick / drawing logic — it only adds, updates or removes its one
  // line series and toggles the left axis. Re-runs when the overlay data changes
  // (including on every replay step, since App re-slices the aligned points).
  const cmpKey = compare
    ? `${compare.label}|${compare.color ?? ''}|${compare.data.length}|${compare.data[compare.data.length - 1]?.time ?? 0}|${compare.data[compare.data.length - 1]?.value ?? 0}`
    : ''
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    if (!compare || compare.data.length === 0) {
      if (compareRef.current) {
        try {
          chart.removeSeries(compareRef.current)
        } catch {
          /* chart already torn down */
        }
        compareRef.current = null
      }
      chart.applyOptions({ leftPriceScale: { visible: false } })
      return
    }
    chart.applyOptions({ leftPriceScale: { visible: true } })
    let series = compareRef.current
    if (!series) {
      series = chart.addLineSeries({
        priceScaleId: 'left',
        lineWidth: 2,
        priceLineVisible: false,
        crosshairMarkerVisible: true,
        lastValueVisible: true,
      })
      // Match the candles' vertical band so the compare line sits over price, not
      // down in the volume histogram's strip.
      series.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.26 } })
      compareRef.current = series
    }
    series.applyOptions({ color: compare.color ?? '#22d3ee', title: compare.label })
    series.setData(compare.data.map((pt) => ({ time: pt.time as Time, value: pt.value })))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cmpKey])

  // Volume visualisations. `volume` shows/hides the bottom histogram (on by
  // default — a missing pref counts as on). `volumeProfile` recomputes the VPVR
  // from the loaded candles into a ref the drawing primitive paints, then asks
  // it to repaint. Both are pure reads of real volume; nothing is fabricated.
  useEffect(() => {
    const showVol = indicators?.volume !== false
    volumeRef.current?.applyOptions({ visible: showVol })
    // Reclaim the bottom band for price when the volume bars are hidden.
    seriesRef.current?.priceScale().applyOptions({
      scaleMargins: { top: 0.08, bottom: showVol ? 0.26 : 0.08 },
    })
    if (indicators?.volumeProfile) {
      vpRef.current = volumeProfile(candles, 24)
      vpOnRef.current = true
    } else {
      vpRef.current = null
      vpOnRef.current = false
    }
    drawViewRef.current?.requestUpdate()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candles, indKey])

  // Feed the computed ICT read + the per-overlay toggles to the canvas layer and
  // repaint. We only stash refs (the pane renderer reads them each frame) so a
  // new analysis or a toggle flip redraws without rebuilding the chart. null ict
  // or all-off prefs simply draws nothing — honest empty, never a fake level.
  useEffect(() => {
    ictRef.current = ict ?? null
    ictPrefsRef.current = ictOverlays ?? DEFAULT_ICT_OVERLAYS
    drawViewRef.current?.requestUpdate()
  }, [ict, ictOverlays])

  // price and time-synced to it (pan/zoom price and the panes follow). Every
  // value is real math on the same candles — RSI(14) with 70/30 guides, MACD
  // (12/26/9) as line + signal + histogram. We reconcile like the overlays: add
  // a newly-enabled pane, tear down a disabled one, refresh data + the live
  // value read-out on reload, and re-colour on theme flips — never recreating a
  // pane that's already up. The bottom-most pane owns the shared time axis.
  useEffect(() => {
    const main = chartRef.current
    const p = paletteRef.current
    if (!main || !p) return
    const want: OscKind[] = OSC_ORDER.filter((k) => !!indicators?.[k])
    for (const kind of OSC_ORDER) {
      if (!want.includes(kind)) destroySubPane(kind)
    }
    const containers: Record<OscKind, HTMLDivElement | null> = {
      rsi: rsiPaneRef.current,
      macd: macdPaneRef.current,
      stoch: stochPaneRef.current,
      atr: atrPaneRef.current,
      obv: obvPaneRef.current,
    }
    const labels: Record<OscKind, HTMLDivElement | null> = {
      rsi: rsiLabelRef.current,
      macd: macdLabelRef.current,
      stoch: stochLabelRef.current,
      atr: atrLabelRef.current,
      obv: obvLabelRef.current,
    }
    for (const kind of want) {
      const container = containers[kind]
      const label = labels[kind]
      if (!container) continue
      let pane = subPanesRef.current.get(kind)
      if (!pane) {
        const sub = createChart(container, {
          layout: { background: { type: ColorType.Solid, color: p.bg }, textColor: p.text },
          grid: { vertLines: { color: p.grid }, horzLines: { color: p.grid } },
          timeScale: { borderColor: p.grid, timeVisible: true, visible: false },
          rightPriceScale: { borderColor: p.grid, minimumWidth: 68 },
          crosshair: { mode: CrosshairMode.Normal },
          autoSize: true,
        })
        const lines: ISeriesApi<'Line'>[] = []
        let hist: ISeriesApi<'Histogram'> | undefined
        if (kind === 'rsi') {
          const line = sub.addLineSeries({
            color: RSI_COLOR,
            lineWidth: 2,
            priceLineVisible: false,
            crosshairMarkerVisible: false,
          })
          // Real RSI reference levels — overbought 70 / oversold 30.
          line.createPriceLine({ price: 70, color: p.grid, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: '70' })
          line.createPriceLine({ price: 30, color: p.grid, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: '30' })
          lines.push(line)
        } else if (kind === 'macd') {
          // Histogram first so the two lines paint over it.
          hist = sub.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false })
          lines.push(
            sub.addLineSeries({ color: MACD_LINE, lineWidth: 2, priceLineVisible: false, crosshairMarkerVisible: false }),
            sub.addLineSeries({ color: MACD_SIGNAL, lineWidth: 2, priceLineVisible: false, crosshairMarkerVisible: false }),
          )
        } else if (kind === 'stoch') {
          // %K then %D, with overbought 80 / oversold 20 guides (classic).
          const kLine = sub.addLineSeries({ color: STOCH_K, lineWidth: 2, priceLineVisible: false, crosshairMarkerVisible: false })
          kLine.createPriceLine({ price: 80, color: p.grid, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: '80' })
          kLine.createPriceLine({ price: 20, color: p.grid, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: '20' })
          lines.push(kLine, sub.addLineSeries({ color: STOCH_D, lineWidth: 2, priceLineVisible: false, crosshairMarkerVisible: false }))
        } else if (kind === 'atr') {
          lines.push(sub.addLineSeries({ color: ATR_COLOR, lineWidth: 2, priceLineVisible: false, crosshairMarkerVisible: false }))
        } else {
          // obv
          lines.push(sub.addLineSeries({ color: OBV_COLOR, lineWidth: 2, priceLineVisible: false, crosshairMarkerVisible: false }))
        }
        const rangeHandler = (r: LogicalRange | null) => syncRange(sub, r)
        sub.timeScale().subscribeVisibleLogicalRangeChange(rangeHandler)
        allChartsRef.current.add(sub)
        pane = { chart: sub, lines, hist, label, rangeHandler }
        subPanesRef.current.set(kind, pane)
      } else {
        pane.chart.applyOptions({
          layout: { background: { type: ColorType.Solid, color: p.bg }, textColor: p.text },
          grid: { vertLines: { color: p.grid }, horzLines: { color: p.grid } },
          timeScale: { borderColor: p.grid },
          rightPriceScale: { borderColor: p.grid },
        })
      }
      if (kind === 'rsi') {
        const data = rsi(candles, 14)
        pane.lines[0].setData(data.map((pt) => ({ time: pt.time as Time, value: pt.value })))
        const latest = data.length ? data[data.length - 1].value : null
        if (pane.label) pane.label.textContent = latest == null ? 'RSI 14' : `RSI 14  ${latest.toFixed(2)}`
      } else if (kind === 'macd') {
        const m = macd(candles)
        pane.lines[0].setData(m.macd.map((pt) => ({ time: pt.time as Time, value: pt.value })))
        pane.lines[1].setData(m.signal.map((pt) => ({ time: pt.time as Time, value: pt.value })))
        pane.hist?.setData(
          m.histogram.map((pt) => ({ time: pt.time as Time, value: pt.value, color: pt.value >= 0 ? VOL_UP : VOL_DOWN })),
        )
        const lastLine = m.macd.length ? m.macd[m.macd.length - 1].value : null
        const lastSig = m.signal.length ? m.signal[m.signal.length - 1].value : null
        if (pane.label) {
          pane.label.textContent = lastLine == null
            ? 'MACD 12 26 9'
            : `MACD 12 26 9  ${lastLine.toFixed(2)} / ${lastSig != null ? lastSig.toFixed(2) : '—'}`
        }
      } else if (kind === 'stoch') {
        const s = stochastic(candles, 14, 3, 3)
        pane.lines[0].setData(s.k.map((pt) => ({ time: pt.time as Time, value: pt.value })))
        pane.lines[1].setData(s.d.map((pt) => ({ time: pt.time as Time, value: pt.value })))
        const lastK = s.k.length ? s.k[s.k.length - 1].value : null
        const lastD = s.d.length ? s.d[s.d.length - 1].value : null
        if (pane.label) {
          pane.label.textContent = lastK == null
            ? 'Stoch 14 3 3'
            : `Stoch 14 3 3  ${lastK.toFixed(2)} / ${lastD != null ? lastD.toFixed(2) : '—'}`
        }
      } else if (kind === 'atr') {
        const a = atr(candles, 14)
        pane.lines[0].setData(a.map((pt) => ({ time: pt.time as Time, value: pt.value })))
        const latest = a.length ? a[a.length - 1].value : null
        if (pane.label) {
          pane.label.textContent = latest == null
            ? 'ATR 14'
            : `ATR 14  ${latest.toLocaleString('en-US', { maximumSignificantDigits: 5 })}`
        }
      } else {
        // obv — cumulative from the loaded range; compact label (values are large).
        const o = obv(candles)
        pane.lines[0].setData(o.map((pt) => ({ time: pt.time as Time, value: pt.value })))
        const latest = o.length ? o[o.length - 1].value : null
        if (pane.label) {
          pane.label.textContent = latest == null
            ? 'OBV'
            : `OBV  ${latest.toLocaleString('en-US', { notation: 'compact', maximumFractionDigits: 2 })}`
        }
      }
      const mainRange = main.timeScale().getVisibleLogicalRange()
      if (mainRange) {
        try {
          pane.chart.timeScale().setVisibleLogicalRange(mainRange)
        } catch {
          /* not ready yet; the main-range subscription will sync it */
        }
      }
    }
    // Time axis on the bottom-most visible chart only, so it reads once under
    // the whole stack (price alone when no oscillators are on).
    const bottom: 'price' | OscKind = want.length ? want[want.length - 1] : 'price'
    main.timeScale().applyOptions({ visible: bottom === 'price' })
    for (const kind of OSC_ORDER) {
      subPanesRef.current.get(kind)?.chart.timeScale().applyOptions({ visible: bottom === kind })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candles, indKey, theme])

  // Route pan/zoom between the chart, the page, and drawing.
  //
  // The GRAPH ITSELF STAYS ZOOMABLE AT ALL TIMES — the lock never takes chart
  // zoom away. Chart nav (wheel/drag/pinch scroll+scale) is on whenever we're not
  // mid-drawing; a drawing tool alone freezes it so a click/drag places the shape
  // cleanly (crosshair cursor) instead of sliding the chart.
  //
  // The lock decides one thing only: who a two-finger PINCH that lands on the
  // chart belongs to, and it matters solely in full screen on a touch screen.
  //   • Locked (the default): `touch-action: none` → the chart owns the pinch, so
  //     pinching the candles zooms the GRAPH and never the surrounding chat/app.
  //     This is the reported phone bug — pinching the graph used to zoom the whole
  //     chat. Locked fixes it while the graph stays fully zoomable.
  //   • Unlocked: `touch-action: pinch-zoom` → the browser owns the pinch, so a
  //     pinch (including over the chart) zooms the whole chat/app, for when the
  //     user deliberately wants that. The graph is still zoomable by wheel/drag.
  // Inline (not full screen) is left untouched ('') so the page scrolls on a phone
  // exactly as it did before.
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    fullscreenRef.current = fullscreen
    zoomLockRef.current = zoomLock
    const drawing = tool !== 'cursor'
    chart.applyOptions({ handleScroll: !drawing, handleScale: !drawing })
    const el = containerRef.current
    if (el) {
      el.style.cursor = drawing ? 'crosshair' : ''
      el.style.touchAction = !fullscreen ? '' : zoomLock === false ? 'pinch-zoom' : 'none'
    }
  }, [tool, fullscreen, zoomLock])

  // Load this symbol/timeframe's saved drawings whenever either changes (and on
  // mount). skipSaveRef stops the next save effect from immediately rewriting
  // what we just read back in.
  useEffect(() => {
    const loaded = loadDrawings(symbol ?? '', timeframe ?? '')
    loadedKeyRef.current = `${symbol ?? ''}|${timeframe ?? ''}`
    skipSaveRef.current = true
    drawingsRef.current = loaded
    selectedRef.current = null
    pendingRef.current = []
    setSelected(null)
    setDrawings(loaded)
    drawViewRef.current?.requestUpdate()
  }, [symbol, timeframe])

  // Persist per symbol/timeframe and keep the renderer's ref in step with state.
  // The one-shot skip covers the commit where a load just set everything.
  useEffect(() => {
    if (skipSaveRef.current) {
      skipSaveRef.current = false
      return
    }
    drawingsRef.current = drawings
    drawViewRef.current?.requestUpdate()
    if (loadedKeyRef.current !== `${symbol ?? ''}|${timeframe ?? ''}`) return
    saveDrawings(symbol ?? '', timeframe ?? '', drawings)
  }, [drawings, symbol, timeframe])

  // --- Drawing-tools actions (the toolbar and keyboard shortcuts share these).
  // Each mirrors its change into the matching ref immediately so the once-built
  // mouse handlers + canvas renderer read the current value without a re-mount.
  const selectTool = (t: Tool) => {
    // Tapping the tool that's already active toggles it back off to the cursor
    // (TradingView-style "touch again to unuse") so a second click on Trend /
    // H-line / Rectangle stops drawing. The cursor has nothing to toggle off to.
    const next = t !== 'cursor' && toolRef.current === t ? 'cursor' : t
    toolRef.current = next
    setTool(next)
    pendingRef.current = []
    hoverRef.current = null
    drawViewRef.current?.requestUpdate()
  }
  const chooseColor = (c: string) => {
    colorRef.current = c
    setColor(c)
    // If something's selected, recolour it to match the freshly picked swatch.
    const id = selectedRef.current
    if (id) {
      const next = drawingsRef.current.map((d) => (d.id === id ? { ...d, color: c } : d))
      drawingsRef.current = next
      setDrawings(next)
    }
    drawViewRef.current?.requestUpdate()
  }
  const deleteSelected = () => {
    const id = selectedRef.current
    if (!id) return
    const next = drawingsRef.current.filter((d) => d.id !== id)
    drawingsRef.current = next
    setDrawings(next)
    selectedRef.current = null
    setSelected(null)
    drawViewRef.current?.requestUpdate()
  }
  const clearAll = () => {
    if (drawingsRef.current.length === 0) return
    drawingsRef.current = []
    setDrawings([])
    selectedRef.current = null
    setSelected(null)
    pendingRef.current = []
    drawViewRef.current?.requestUpdate()
  }
  // Escape: abandon a half-placed drawing first, else drop the selection; either
  // way fall back to the cursor tool so the chart is navigable again.
  const cancelDraw = () => {
    if (pendingRef.current.length) {
      pendingRef.current = []
    } else {
      selectedRef.current = null
      setSelected(null)
    }
    toolRef.current = 'cursor'
    setTool('cursor')
    drawViewRef.current?.requestUpdate()
  }
  // Keep the ref the window keydown handler calls pointed at the live closures.
  actionsRef.current = { del: deleteSelected, cancel: cancelDraw }
  // And keep the bar duration fresh for the measure tool's bar count.
  barSecRef.current = TF_SECONDS[timeframe ?? ''] ?? 0

  // Wipe all drawings when the parent bumps clearSignal (the assistant's "clear
  // the drawings" command). A change in value is the trigger; the value seen on
  // mount is ignored so first render never nukes the user's saved drawings.
  const prevClearRef = useRef(clearSignal)
  useEffect(() => {
    if (clearSignal === prevClearRef.current) return
    prevClearRef.current = clearSignal
    clearAll()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clearSignal])

  // Grow the wrapper by one fixed-height slot per active oscillator so price
  // keeps its height and each pane stacks below (like adding TradingView panes).
  const activeSubs =
    (indicators?.rsi ? 1 : 0) +
    (indicators?.macd ? 1 : 0) +
    (indicators?.stoch ? 1 : 0) +
    (indicators?.atr ? 1 : 0) +
    (indicators?.obv ? 1 : 0)
  return (
    <div className="chart-wrap" style={{ height: 380 + activeSubs * 118 }}>
      <div className="chart-legend">
        <span className="cl-sym">
          {symbol || ''}
          {timeframe ? ` · ${timeframe}` : ''}
        </span>
        <span className="cl-ohlc" ref={legendRef} />
      </div>
      <div className="chart-countdown" ref={countdownRef} title="Time left until this candle closes" />
      <div className="chart-toolbar" role="toolbar" aria-label="Drawing tools">
        {DRAW_TOOLS.map((t) => (
          <button
            key={t.key}
            type="button"
            className={`ct-btn${tool === t.key ? ' active' : ''}`}
            title={t.label}
            aria-label={t.label}
            aria-pressed={tool === t.key}
            onClick={() => selectTool(t.key)}
          >
            {t.glyph}
          </button>
        ))}
        <div className="ct-sep" />
        <div className="ct-colors">
          {DRAW_COLORS.map((c) => (
            <button
              key={c}
              type="button"
              className={`ct-swatch${color === c ? ' active' : ''}`}
              style={{ background: c }}
              title={`Colour ${c}${selected ? ' (recolours the selected drawing)' : ''}`}
              aria-label={`Colour ${c}`}
              aria-pressed={color === c}
              onClick={() => chooseColor(c)}
            />
          ))}
        </div>
        <div className="ct-sep" />
        <button
          type="button"
          className="ct-btn"
          title="Delete selected drawing (Del key, or right-click the drawing)"
          aria-label="Delete selected drawing"
          disabled={!selected}
          onClick={deleteSelected}
        >
          🗑
        </button>
        <button
          type="button"
          className="ct-btn"
          title="Clear all drawings on this chart"
          aria-label="Clear all drawings"
          disabled={drawings.length === 0}
          onClick={clearAll}
        >
          ⌫
        </button>
      </div>
      <div className="chart" ref={containerRef} />
      <div className={`chart-sub${indicators?.rsi ? '' : ' hidden'}`}>
        <div className="chart-sub-label" ref={rsiLabelRef} />
        <div className="chart-sub-canvas" ref={rsiPaneRef} />
      </div>
      <div className={`chart-sub${indicators?.macd ? '' : ' hidden'}`}>
        <div className="chart-sub-label" ref={macdLabelRef} />
        <div className="chart-sub-canvas" ref={macdPaneRef} />
      </div>
      <div className={`chart-sub${indicators?.stoch ? '' : ' hidden'}`}>
        <div className="chart-sub-label" ref={stochLabelRef} />
        <div className="chart-sub-canvas" ref={stochPaneRef} />
      </div>
      <div className={`chart-sub${indicators?.atr ? '' : ' hidden'}`}>
        <div className="chart-sub-label" ref={atrLabelRef} />
        <div className="chart-sub-canvas" ref={atrPaneRef} />
      </div>
      <div className={`chart-sub${indicators?.obv ? '' : ' hidden'}`}>
        <div className="chart-sub-label" ref={obvLabelRef} />
        <div className="chart-sub-canvas" ref={obvPaneRef} />
      </div>
    </div>
  )
}
