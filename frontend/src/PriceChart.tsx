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
import type { Theme } from './theme'
import { sma, ema, bollinger, vwap, rsi, macd, type IndicatorPrefs, type LinePoint } from './indicators'
import { volumeProfile, type VolumeProfile } from './volumeProfile'
import type { ChartMarker } from './chartMarkers'
import {
  loadDrawings,
  saveDrawings,
  newDrawingId,
  pointNearSegment,
  pointNearRect,
  pointNearHLine,
  type Drawing,
  type Pt,
  type Tool,
} from './drawings'

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
// Widen the price series' auto-scale range so it also brackets `levels` — the
// open position's entry / stop / target. Without this the vertical axis fits
// only the visible candles, so on a tight timeframe (e.g. 5m, where price has
// barely moved) a stop or target sitting a few % away falls OFF the top/bottom
// of the chart and can't be scrolled to. Including the levels keeps them in
// view on every timeframe. Style/scale only — it never invents a level; it just
// stretches the window to whatever real numbers were handed in.
function extendAutoscale(base: AutoscaleInfo | null, levels: number[]): AutoscaleInfo | null {
  let lo = Infinity
  let hi = -Infinity
  for (const p of levels) {
    if (Number.isFinite(p) && p > 0) {
      lo = Math.min(lo, p)
      hi = Math.max(hi, p)
    }
  }
  if (lo === Infinity) return base // no levels to honour → leave the fit untouched
  if (base) {
    return {
      priceRange: {
        minValue: Math.min(base.priceRange.minValue, lo),
        maxValue: Math.max(base.priceRange.maxValue, hi),
      },
      margins: base.margins,
    }
  }
  // No candle range yet (data still loading): still bracket the levels alone.
  return { priceRange: { minValue: lo, maxValue: hi } }
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

// Oscillator sub-panes (RSI, MACD) live in their own charts stacked under price,
// each with its own y-scale (RSI 0–100, MACD centred on 0). Line colours are
// fixed literals (not theme-driven) so the two panes read consistently; the MACD
// histogram uses the theme's up/down. Every value is real math on the candles.
type OscKind = 'rsi' | 'macd'
const OSC_ORDER: OscKind[] = ['rsi', 'macd']
const RSI_COLOR = '#d1a1ff'
const MACD_LINE = '#3b82f6'
const MACD_SIGNAL = '#f0b90b'
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
  { key: 'hline', glyph: '─', label: 'Horizontal line — click a price level' },
  { key: 'rect', glyph: '▭', label: 'Rectangle — click two opposite corners' },
]
const DRAW_COLORS = ['#2962ff', '#f0b90b', '#16c784', '#ea3943']
const HIT_TOL = 6 // px — how near a click must land to select a drawing

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

function fmtPrice(v: number): string {
  const dp = Math.abs(v) < 10 ? 4 : 2
  return v.toLocaleString('en-US', { minimumFractionDigits: dp, maximumFractionDigits: dp })
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
  indicators,
  markers,
  clearSignal,
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
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const volumeRef = useRef<ISeriesApi<'Histogram'> | null>(null)
  // The newest bar, kept current so live ticks extend it rather than reset it.
  const lastBarRef = useRef<CandlestickData | null>(null)
  const lastVolRef = useRef<number | undefined>(undefined)
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
  // Indicator overlay line series (EMA/SMA/Bollinger/VWAP), keyed so we can add,
  // update, or remove one without disturbing the candles or the others.
  const overlayRef = useRef<Map<string, ISeriesApi<'Line'>>>(new Map())
  // Oscillator sub-panes (RSI/MACD): each is its own synced chart under price.
  // The two container divs are always in the DOM; a chart is created inside one
  // only while its oscillator is switched on, and torn down when switched off.
  const rsiPaneRef = useRef<HTMLDivElement>(null)
  const macdPaneRef = useRef<HTMLDivElement>(null)
  const rsiLabelRef = useRef<HTMLDivElement>(null)
  const macdLabelRef = useRef<HTMLDivElement>(null)
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
  // First anchor of a two-click drawing (trend/rect) awaiting its second click.
  const pendingRef = useRef<Pt | null>(null)
  // Latest pointer position in data space, for the rubber-band preview.
  const hoverRef = useRef<Pt | null>(null)
  // Handle to the attached primitive's requestUpdate, so any state change can
  // ask lightweight-charts to repaint the drawing layer.
  const drawViewRef = useRef<{ requestUpdate: () => void } | null>(null)
  // Volume Profile (VPVR): the computed profile to paint up the right edge, and
  // whether it's switched on. The drawing primitive's renderer reads both so the
  // profile repaints in step with pans/zooms and theme flips.
  const vpRef = useRef<VolumeProfile | null>(null)
  const vpOnRef = useRef<boolean>(false)
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
    el.innerHTML =
      `<span class="cl-k">O</span><span class="cl-v">${fmtPrice(bar.open)}</span>` +
      `<span class="cl-k">H</span><span class="cl-v">${fmtPrice(bar.high)}</span>` +
      `<span class="cl-k">L</span><span class="cl-v">${fmtPrice(bar.low)}</span>` +
      `<span class="cl-k">C</span><span class="cl-v">${fmtPrice(bar.close)}</span>` +
      `<span class="cl-chg" style="color:${col}">${sign}${fmtPrice(chg)} (${sign}${chgPct.toFixed(2)}%)</span>` +
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
        if (pendingRef.current) drawViewRef.current?.requestUpdate()
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
        ctx.fillStyle = isPoc ? 'rgba(240,185,11,0.55)' : 'rgba(91,141,239,0.32)'
        ctx.fillRect(width - w, top, w, barH)
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
        } else {
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
        }
      }
      // Rubber-band preview between the first click and the pointer, for the
      // two-click tools (trend / rect), drawn dashed until the second click.
      const pend = pendingRef.current
      const hov = hoverRef.current
      const t = toolRef.current
      if (pend && hov && (t === 'trend' || t === 'rect')) {
        const a = px(pend)
        const b = px(hov)
        if (a && b) {
          ctx.strokeStyle = colorRef.current
          ctx.lineWidth = 1.5
          ctx.setLineDash([4, 4])
          if (t === 'trend') {
            strokeSeg(ctx, a.x, a.y, b.x, b.y)
          } else {
            const [rx, ry, rw, rh] = box(a, b)
            ctx.strokeRect(rx, ry, rw, rh)
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
      if (time == null) return // trend / rect need a time anchor
      if (!pendingRef.current) {
        pendingRef.current = { time, price }
        drawViewRef.current?.requestUpdate()
      } else {
        const a = pendingRef.current
        pendingRef.current = null
        commit({ id: newDrawingId(), kind: activeTool, a, b: { time, price }, color: colorRef.current })
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
      pendingRef.current = null
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
        } else if (d.kind === 'trend') {
          const a = px(d.a)
          const b = px(d.b)
          if (a && b && pointNearSegment({ x, y }, a, b, HIT_TOL)) return d.id
        } else {
          const a = px(d.a)
          const b = px(d.b)
          if (a && b && pointNearRect({ x, y }, a, b, HIT_TOL)) return d.id
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

  // Trade markers: buy/sell arrows on the exact bars where the user's OWN trades
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
    for (const kind of want) {
      const container = kind === 'rsi' ? rsiPaneRef.current : macdPaneRef.current
      const label = kind === 'rsi' ? rsiLabelRef.current : macdLabelRef.current
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
        } else {
          // Histogram first so the two lines paint over it.
          hist = sub.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false })
          lines.push(
            sub.addLineSeries({ color: MACD_LINE, lineWidth: 2, priceLineVisible: false, crosshairMarkerVisible: false }),
            sub.addLineSeries({ color: MACD_SIGNAL, lineWidth: 2, priceLineVisible: false, crosshairMarkerVisible: false }),
          )
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
      } else {
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

  // While a drawing tool is active, freeze pan/zoom so clicks place cleanly and
  // show a crosshair cursor; the cursor tool restores normal chart navigation.
  useEffect(() => {
    const chart = chartRef.current
    if (!chart) return
    const drawing = tool !== 'cursor'
    chart.applyOptions({ handleScroll: !drawing, handleScale: !drawing })
    if (containerRef.current) containerRef.current.style.cursor = drawing ? 'crosshair' : ''
  }, [tool])

  // Load this symbol/timeframe's saved drawings whenever either changes (and on
  // mount). skipSaveRef stops the next save effect from immediately rewriting
  // what we just read back in.
  useEffect(() => {
    const loaded = loadDrawings(symbol ?? '', timeframe ?? '')
    loadedKeyRef.current = `${symbol ?? ''}|${timeframe ?? ''}`
    skipSaveRef.current = true
    drawingsRef.current = loaded
    selectedRef.current = null
    pendingRef.current = null
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
    pendingRef.current = null
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
    pendingRef.current = null
    drawViewRef.current?.requestUpdate()
  }
  // Escape: abandon a half-placed drawing first, else drop the selection; either
  // way fall back to the cursor tool so the chart is navigable again.
  const cancelDraw = () => {
    if (pendingRef.current) {
      pendingRef.current = null
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
  const activeSubs = (indicators?.rsi ? 1 : 0) + (indicators?.macd ? 1 : 0)
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
    </div>
  )
}
