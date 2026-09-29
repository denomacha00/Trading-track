// Real technical indicators, computed straight from the market's OWN candles —
// the same math TradingView draws. Every value here is derived from real OHLCV
// the chart already loaded; nothing is invented or back-filled. Each function
// returns points aligned to candle open times, and only for bars where the
// indicator is actually defined (e.g. an SMA(50) starts at the 50th bar), so
// lightweight-charts draws a clean line that begins where the math is valid.
import type { Candle } from './types'

// One plotted point. `time` is the candle's unix open time (seconds), matching
// the price series so overlays sit exactly on their bar.
export type LinePoint = { time: number; value: number }

// Which price-overlay indicators the user has switched on. Persisted so the
// choice sticks between visits (see App: loaded from / saved to localStorage).
// The first group draws ON price; rsi/macd draw in their own sub-panes below it.
// `volume`/`volumeProfile` are the two volume VISUALISATIONS (the bottom bars
// and the right-edge VPVR histogram) — toggles handled in PriceChart, not line
// math here — kept in the same prefs object so they persist with the rest.
export type IndicatorPrefs = {
  ema9: boolean
  ema21: boolean
  sma50: boolean
  sma200: boolean
  bb: boolean
  donchian: boolean
  keltner: boolean
  hma: boolean
  vwap: boolean
  rsi: boolean
  macd: boolean
  volume: boolean
  volumeProfile: boolean
}

export const DEFAULT_INDICATORS: IndicatorPrefs = {
  ema9: false,
  ema21: false,
  sma50: false,
  sma200: false,
  bb: false,
  donchian: false,
  keltner: false,
  hma: false,
  vwap: false,
  rsi: false,
  macd: false,
  volume: true,
  volumeProfile: false,
}

// Simple moving average of the close over `period` bars.
export function sma(candles: Candle[], period: number): LinePoint[] {
  if (period <= 0) return []
  const out: LinePoint[] = []
  let sum = 0
  for (let i = 0; i < candles.length; i++) {
    sum += candles[i].close
    if (i >= period) sum -= candles[i - period].close
    if (i >= period - 1) out.push({ time: candles[i].time, value: sum / period })
  }
  return out
}

// Exponential moving average of the close. Seeded with the SMA of the first
// `period` closes (the standard warm-up), then rolled forward with k=2/(p+1).
export function ema(candles: Candle[], period: number): LinePoint[] {
  if (period <= 0 || candles.length < period) return []
  const k = 2 / (period + 1)
  const out: LinePoint[] = []
  let seed = 0
  for (let i = 0; i < period; i++) seed += candles[i].close
  let prev = seed / period
  out.push({ time: candles[period - 1].time, value: prev })
  for (let i = period; i < candles.length; i++) {
    prev = candles[i].close * k + prev * (1 - k)
    out.push({ time: candles[i].time, value: prev })
  }
  return out
}

// Bollinger Bands: an SMA basis with an upper/lower envelope at `mult` standard
// deviations (population stddev over the same window). Returns three aligned
// lines. Classic defaults are period 20, mult 2.
export function bollinger(
  candles: Candle[],
  period = 20,
  mult = 2,
): { basis: LinePoint[]; upper: LinePoint[]; lower: LinePoint[] } {
  const basis: LinePoint[] = []
  const upper: LinePoint[] = []
  const lower: LinePoint[] = []
  if (period <= 0) return { basis, upper, lower }
  for (let i = period - 1; i < candles.length; i++) {
    let sum = 0
    for (let j = i - period + 1; j <= i; j++) sum += candles[j].close
    const mean = sum / period
    let variance = 0
    for (let j = i - period + 1; j <= i; j++) {
      const d = candles[j].close - mean
      variance += d * d
    }
    const sd = Math.sqrt(variance / period)
    const t = candles[i].time
    basis.push({ time: t, value: mean })
    upper.push({ time: t, value: mean + mult * sd })
    lower.push({ time: t, value: mean - mult * sd })
  }
  return { basis, upper, lower }
}

// Volume-Weighted Average Price, accumulated from the first loaded bar using the
// typical price (H+L+C)/3 weighted by real volume. Anchored to the start of the
// loaded range (labelled as such in the UI), so it's honest about its window.
export function vwap(candles: Candle[]): LinePoint[] {
  const out: LinePoint[] = []
  let cumPV = 0
  let cumV = 0
  for (const c of candles) {
    const typical = (c.high + c.low + c.close) / 3
    const v = Number.isFinite(c.volume) ? c.volume : 0
    cumPV += typical * v
    cumV += v
    if (cumV > 0) out.push({ time: c.time, value: cumPV / cumV })
  }
  return out
}

// Relative Strength Index (Wilder's smoothing) over `period` bars. Returns 0–100
// points aligned to their bar, starting once the first averages are available.
export function rsi(candles: Candle[], period = 14): LinePoint[] {
  const out: LinePoint[] = []
  if (candles.length <= period) return out
  let gain = 0
  let loss = 0
  for (let i = 1; i <= period; i++) {
    const ch = candles[i].close - candles[i - 1].close
    if (ch >= 0) gain += ch
    else loss -= ch
  }
  let avgGain = gain / period
  let avgLoss = loss / period
  const rsiAt = () => (avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss))
  out.push({ time: candles[period].time, value: rsiAt() })
  for (let i = period + 1; i < candles.length; i++) {
    const ch = candles[i].close - candles[i - 1].close
    const g = ch >= 0 ? ch : 0
    const l = ch < 0 ? -ch : 0
    avgGain = (avgGain * (period - 1) + g) / period
    avgLoss = (avgLoss * (period - 1) + l) / period
    out.push({ time: candles[i].time, value: rsiAt() })
  }
  return out
}

// EMA over an arbitrary series of points (not candles). Used for the MACD signal
// line, which is an EMA of the MACD line itself. Same SMA-seed + k=2/(p+1) as
// ema(), and it carries each point's own timestamp through so the result stays
// aligned to the bars.
function emaOfPoints(points: LinePoint[], period: number): LinePoint[] {
  if (period <= 0 || points.length < period) return []
  const k = 2 / (period + 1)
  const out: LinePoint[] = []
  let seed = 0
  for (let i = 0; i < period; i++) seed += points[i].value
  let prev = seed / period
  out.push({ time: points[period - 1].time, value: prev })
  for (let i = period; i < points.length; i++) {
    prev = points[i].value * k + prev * (1 - k)
    out.push({ time: points[i].time, value: prev })
  }
  return out
}

// MACD — the classic momentum oscillator, all three lines real and computed from
// the candles' own closes: `macd` = EMA(fast) − EMA(slow); `signal` =
// EMA(signalPeriod) of that line; `histogram` = macd − signal. Each is time-
// aligned to its bar (the line begins once the slow EMA exists, the signal once
// enough line points exist), so a sub-pane draws them exactly under price.
// Classic defaults are 12 / 26 / 9.
export function macd(
  candles: Candle[],
  fast = 12,
  slow = 26,
  signalPeriod = 9,
): { macd: LinePoint[]; signal: LinePoint[]; histogram: LinePoint[] } {
  const emaFast = ema(candles, fast)
  const emaSlow = ema(candles, slow)
  if (!emaFast.length || !emaSlow.length) return { macd: [], signal: [], histogram: [] }
  // Subtract fast − slow only on bars where BOTH EMAs are defined (i.e. from the
  // slow EMA's first bar onward), matching them up by timestamp.
  const fastAt = new Map(emaFast.map((p) => [p.time, p.value]))
  const line: LinePoint[] = []
  for (const s of emaSlow) {
    const f = fastAt.get(s.time)
    if (f != null) line.push({ time: s.time, value: f - s.value })
  }
  const signal = emaOfPoints(line, signalPeriod)
  const lineAt = new Map(line.map((p) => [p.time, p.value]))
  const histogram: LinePoint[] = signal.map((s) => ({
    time: s.time,
    value: (lineAt.get(s.time) as number) - s.value,
  }))
  return { macd: line, signal, histogram }
}

// Weighted moving average over a plain numeric series: the newest value carries
// the largest weight (period, period-1, …, 1). Returns a SAME-LENGTH array with
// null before the window is full, so a caller can align it to bar times or feed
// it into another pass (the Hull MA does exactly that). Pure — no candle shape.
function wmaSeries(values: number[], period: number): (number | null)[] {
  const out: (number | null)[] = new Array(values.length).fill(null)
  if (period <= 0) return out
  const denom = (period * (period + 1)) / 2
  for (let i = period - 1; i < values.length; i++) {
    let weighted = 0
    for (let j = 0; j < period; j++) weighted += values[i - j] * (period - j)
    out[i] = weighted / denom
  }
  return out
}

// Hull Moving Average (Alan Hull) — a low-lag, smooth average:
//   HMA(n) = WMA( 2·WMA(n/2) − WMA(n), round(√n) )
// Computed from the candles' own closes; the line begins only where the full
// nested window is valid, so it never draws over warm-up bars.
export function hma(candles: Candle[], period = 55): LinePoint[] {
  if (period <= 1 || candles.length < period) return []
  const closes = candles.map((c) => c.close)
  const wHalf = wmaSeries(closes, Math.max(1, Math.floor(period / 2)))
  const wFull = wmaSeries(closes, period)
  // raw = 2·WMA(n/2) − WMA(n), defined once the full WMA exists (index period-1).
  const start = period - 1
  const raw: number[] = []
  for (let i = start; i < closes.length; i++) {
    raw.push(2 * (wHalf[i] as number) - (wFull[i] as number))
  }
  const sqrtN = Math.max(1, Math.round(Math.sqrt(period)))
  const hull = wmaSeries(raw, sqrtN)
  const out: LinePoint[] = []
  for (let i = 0; i < hull.length; i++) {
    const v = hull[i]
    if (v != null) out.push({ time: candles[start + i].time, value: v })
  }
  return out
}
// APPEND_MARKER

// True Range per bar, Wilder-smoothed into ATR. Returns a SAME-LENGTH per-candle
// array (null until the average exists at bar `period`) so Keltner can line ATR
// up with its EMA basis by index. TR uses the previous close, so it needs ≥2
// bars; ATR(period) is seeded with the mean of the first `period` true ranges.
function atrAligned(candles: Candle[], period = 14): (number | null)[] {
  const out: (number | null)[] = new Array(candles.length).fill(null)
  if (period <= 0 || candles.length <= period) return out
  const tr: number[] = new Array(candles.length).fill(0)
  for (let i = 1; i < candles.length; i++) {
    const h = candles[i].high
    const l = candles[i].low
    const pc = candles[i - 1].close
    tr[i] = Math.max(h - l, Math.abs(h - pc), Math.abs(l - pc))
  }
  let sum = 0
  for (let i = 1; i <= period; i++) sum += tr[i]
  let prev = sum / period
  out[period] = prev
  for (let i = period + 1; i < candles.length; i++) {
    prev = (prev * (period - 1) + tr[i]) / period
    out[i] = prev
  }
  return out
}

// Average True Range as plottable points — real volatility in price units,
// starting once Wilder's average is defined. Same values atrAligned() feeds to
// Keltner, just filtered to the bars where they exist.
export function atr(candles: Candle[], period = 14): LinePoint[] {
  const a = atrAligned(candles, period)
  const out: LinePoint[] = []
  for (let i = 0; i < a.length; i++) {
    if (a[i] != null) out.push({ time: candles[i].time, value: a[i] as number })
  }
  return out
}

// Donchian Channels — the highest high and lowest low over `period` bars, plus
// their midline. Pure highs/lows off the candles; defined from the period-th bar.
export function donchian(
  candles: Candle[],
  period = 20,
): { upper: LinePoint[]; basis: LinePoint[]; lower: LinePoint[] } {
  const upper: LinePoint[] = []
  const basis: LinePoint[] = []
  const lower: LinePoint[] = []
  if (period <= 0) return { upper, basis, lower }
  for (let i = period - 1; i < candles.length; i++) {
    let hi = -Infinity
    let lo = Infinity
    for (let j = i - period + 1; j <= i; j++) {
      if (candles[j].high > hi) hi = candles[j].high
      if (candles[j].low < lo) lo = candles[j].low
    }
    const t = candles[i].time
    upper.push({ time: t, value: hi })
    lower.push({ time: t, value: lo })
    basis.push({ time: t, value: (hi + lo) / 2 })
  }
  return { upper, basis, lower }
}
// APPEND_MARKER2

// Keltner Channels — an EMA basis with an envelope `mult`×ATR wide (the modern
// ATR form). basis = EMA(close, emaPeriod); upper/lower = basis ± mult·ATR
// (atrPeriod). Aligned only on bars where BOTH the EMA and the ATR are defined,
// matched by timestamp, so the three lines always share their x-points.
export function keltner(
  candles: Candle[],
  emaPeriod = 20,
  atrPeriod = 10,
  mult = 2,
): { basis: LinePoint[]; upper: LinePoint[]; lower: LinePoint[] } {
  const basis: LinePoint[] = []
  const upper: LinePoint[] = []
  const lower: LinePoint[] = []
  const emaLine = ema(candles, emaPeriod)
  if (!emaLine.length) return { basis, upper, lower }
  const a = atrAligned(candles, atrPeriod)
  const atrAtTime = new Map<number, number>()
  for (let i = 0; i < a.length; i++) {
    if (a[i] != null) atrAtTime.set(candles[i].time, a[i] as number)
  }
  for (const pt of emaLine) {
    const av = atrAtTime.get(pt.time)
    if (av == null) continue
    basis.push({ time: pt.time, value: pt.value })
    upper.push({ time: pt.time, value: pt.value + mult * av })
    lower.push({ time: pt.time, value: pt.value - mult * av })
  }
  return { basis, upper, lower }
}
