// Chart-type transforms (TradingView parity: "Candles", "Heikin-Ashi",
// "Hollow candles", "Line", "Area", "Bars").
//
// Candles / Heikin-Ashi / Hollow all render through ONE candlestick series (a
// data transform + per-point colours). Line / Area / Bars need a DIFFERENT
// lightweight-charts series constructor, so they are grouped by `seriesKind`
// below: the chart swaps the underlying series only when the FAMILY changes.
//
// Heikin-Ashi ("average bar") smooths the candles to make trend/consolidation
// easier to read. It is a pure DERIVATION of the real OHLC — no data is invented
// and no real bar is discarded: every HA value is an average of REAL prices, and
// the mapping is 1:1 with the real candles (same count, same timestamps, same
// volume). Indicators, drawings and alerts keep working on the real candles; only
// the drawn candle bodies change. Switching back to Candles is lossless.
//
// Formula (standard):
//   HA_close = (open + high + low + close) / 4
//   HA_open  = first bar: (open + close) / 2
//              else:       (prev HA_open + prev HA_close) / 2
//   HA_high  = max(high, HA_open, HA_close)
//   HA_low   = min(low,  HA_open, HA_close)
//
// Hollow candles draw the SAME real OHLC bars (no averaging, no series swap) but
// recolour them TradingView-style: the up/down COLOUR is bar-over-bar (green when
// this close >= the PREVIOUS close, red when below), while the body is HOLLOW
// (outline only) when the bar closed at/above its open and FILLED when it closed
// below. Pure styling derived from real prices — nothing invented.
import type { Candle } from './types'

export type ChartKind =
  | 'candles'
  | 'heikin_ashi'
  | 'hollow'
  | 'line'
  | 'area'
  | 'bars'

// The chart-type picker's options, in display order. Extend here to add more
// drawable types — the picker renders this list.
export const CHART_TYPES: { key: ChartKind; label: string }[] = [
  { key: 'candles', label: 'Candles' },
  { key: 'heikin_ashi', label: 'Heikin-Ashi' },
  { key: 'hollow', label: 'Hollow candles' },
  { key: 'bars', label: 'Bars' },
  { key: 'line', label: 'Line' },
  { key: 'area', label: 'Area' },
]

export function isChartKind(v: unknown): v is ChartKind {
  return (
    v === 'candles' ||
    v === 'heikin_ashi' ||
    v === 'hollow' ||
    v === 'line' ||
    v === 'area' ||
    v === 'bars'
  )
}

// Which lightweight-charts series constructor a chart kind needs. Candles /
// Heikin-Ashi / Hollow share ONE candlestick series (same family → no series
// swap, just a data/colour transform); Bars/Line/Area each need their own
// series type. The chart only tears down & rebuilds the main series when this
// value changes, so switching Candles↔Heikin-Ashi↔Hollow stays a cheap reskin.
export type SeriesKind = 'candlestick' | 'bar' | 'line' | 'area'

export function seriesKind(kind: ChartKind): SeriesKind {
  switch (kind) {
    case 'bars':
      return 'bar'
    case 'line':
      return 'line'
    case 'area':
      return 'area'
    default:
      // candles / heikin_ashi / hollow all render as candlesticks.
      return 'candlestick'
  }
}

// True for the single-value series (Line / Area) that plot ONE number per bar
// (the close) instead of a full OHLC quad. Callers use this to pick the data
// shape: `{ time, value }` vs `{ time, open, high, low, close }`.
export function isValueSeries(sk: SeriesKind): boolean {
  return sk === 'line' || sk === 'area'
}

// One single-value point per real bar for a Line / Area series: the bar's
// CLOSE. Same length/order/timestamps as the input — nothing invented, the
// close is a real traded price. (Line/Area show only the close, TradingView
// style; the real OHLC still drives indicators, drawings and the legend.)
export interface LinePoint {
  time: Candle['time']
  value: number
}
export function toLineData(candles: Candle[]): LinePoint[] {
  return candles.map((c) => ({ time: c.time, value: c.close }))
}

// Per-point candle styling for Hollow Candles. lightweight-charts lets each
// candlestick point carry its own body `color`, `borderColor` and `wickColor`;
// a transparent body + visible border renders as a hollow candle (the series
// must have borderVisible:true for the outline to show). No data is changed —
// only the colours of the real bar.
export interface CandleStyle {
  color: string // body fill — transparent for a hollow (unfilled) body
  borderColor: string
  wickColor: string
}

// A fully transparent fill = a hollow body (only the border/wick show).
export const HOLLOW_TRANSPARENT = 'rgba(0,0,0,0)'

// Style ONE bar the TradingView hollow-candle way: colour by close-vs-PREVIOUS-
// close (the trend), fill by close-vs-open (the bar's own body). The first bar
// has no previous close, so it falls back to close-vs-open for its colour.
export function hollowStyle(
  bar: Ohlc,
  prevClose: number | null,
  up: string,
  down: string,
): CandleStyle {
  const trendUp =
    prevClose == null || !Number.isFinite(prevClose)
      ? bar.close >= bar.open
      : bar.close >= prevClose
  const dir = trendUp ? up : down
  const filled = bar.close < bar.open // bearish body = solid, else hollow
  return { color: filled ? dir : HOLLOW_TRANSPARENT, borderColor: dir, wickColor: dir }
}

// Batch: one CandleStyle per real bar, each coloured against the PREVIOUS bar's
// close (first bar → close vs open). Same length/order as the input.
export function hollowStyles(candles: Candle[], up: string, down: string): CandleStyle[] {
  const out: CandleStyle[] = []
  let prevClose: number | null = null
  for (const c of candles) {
    out.push(hollowStyle(c, prevClose, up, down))
    prevClose = c.close
  }
  return out
}

// Just the OHLC quad an HA bar carries (time/volume are copied through by the
// callers that need them — the live-update path only wants the four prices).
export interface Ohlc {
  open: number
  high: number
  low: number
  close: number
}

// One Heikin-Ashi bar from one real bar plus the PREVIOUS HA bar (null for the
// first bar of the series). Used for the live/forming candle so it stays exactly
// consistent with the batch transform below as its real OHLC ticks.
export function haBar(raw: Ohlc, prevHA: Ohlc | null): Ohlc {
  const close = (raw.open + raw.high + raw.low + raw.close) / 4
  const open = prevHA ? (prevHA.open + prevHA.close) / 2 : (raw.open + raw.close) / 2
  const high = Math.max(raw.high, open, close)
  const low = Math.min(raw.low, open, close)
  return { open, high, low, close }
}

// Transform a full candle series to Heikin-Ashi. Returns a NEW array of the same
// length, same timestamps and same volumes — only OHLC is averaged. An empty or
// single-bar input is handled (the first bar seeds the recurrence).
export function heikinAshi(candles: Candle[]): Candle[] {
  const out: Candle[] = []
  let prev: Ohlc | null = null
  for (const c of candles) {
    const ha = haBar(c, prev)
    out.push({
      time: c.time,
      open: ha.open,
      high: ha.high,
      low: ha.low,
      close: ha.close,
      volume: c.volume,
    })
    prev = ha
  }
  return out
}
