import { describe, it, expect } from 'vitest'
import {
  heikinAshi,
  haBar,
  CHART_TYPES,
  isChartKind,
  hollowStyle,
  hollowStyles,
  HOLLOW_TRANSPARENT,
  seriesKind,
  isValueSeries,
  toLineData,
} from './chartTypes'
import type { Candle } from './types'

const UP = '#16c784'
const DOWN = '#ea3943'

// A small hand-verifiable series (values chosen so the HA arithmetic is exact).
const SERIES: Candle[] = [
  { time: 1, open: 100, high: 110, low: 95, close: 105, volume: 10 },
  { time: 2, open: 105, high: 112, low: 100, close: 108, volume: 20 },
  { time: 3, open: 108, high: 115, low: 104, close: 106, volume: 30 },
]

describe('heikinAshi', () => {
  it('returns [] for empty input', () => {
    expect(heikinAshi([])).toEqual([])
  })

  it('seeds the first bar from (open+close)/2', () => {
    const [b0] = heikinAshi(SERIES)
    expect(b0.close).toBe((100 + 110 + 95 + 105) / 4) // 102.5
    expect(b0.open).toBe((100 + 105) / 2) // 102.5
    expect(b0.high).toBe(110) // max(high, open, close)
    expect(b0.low).toBe(95) // min(low, open, close)
  })

  it('computes the standard HA recurrence for later bars', () => {
    const ha = heikinAshi(SERIES)
    // Bar 1: close = 425/4 = 106.25, open = (102.5+102.5)/2 = 102.5
    expect(ha[1].close).toBeCloseTo(106.25, 10)
    expect(ha[1].open).toBeCloseTo(102.5, 10)
    expect(ha[1].high).toBe(112)
    expect(ha[1].low).toBe(100)
    // Bar 2: close = 433/4 = 108.25, open = (102.5+106.25)/2 = 104.375
    expect(ha[2].close).toBeCloseTo(108.25, 10)
    expect(ha[2].open).toBeCloseTo(104.375, 10)
    expect(ha[2].high).toBe(115)
    expect(ha[2].low).toBe(104)
  })

  it('preserves length, timestamps and volume 1:1 with the real candles', () => {
    const ha = heikinAshi(SERIES)
    expect(ha.length).toBe(SERIES.length)
    expect(ha.map((c) => c.time)).toEqual(SERIES.map((c) => c.time))
    expect(ha.map((c) => c.volume)).toEqual(SERIES.map((c) => c.volume))
  })

  it('never lets HA high/low fall inside the HA body (high>=max, low<=min)', () => {
    for (const c of heikinAshi(SERIES)) {
      expect(c.high).toBeGreaterThanOrEqual(Math.max(c.open, c.close))
      expect(c.low).toBeLessThanOrEqual(Math.min(c.open, c.close))
      expect(c.high).toBeGreaterThanOrEqual(c.low)
    }
  })

  it('does not mutate the input candles', () => {
    const snapshot = JSON.parse(JSON.stringify(SERIES))
    heikinAshi(SERIES)
    expect(SERIES).toEqual(snapshot)
  })
})

describe('haBar (live-bar transform)', () => {
  it('matches the batch transform for the first bar (prevHA = null)', () => {
    const [b0] = heikinAshi(SERIES)
    const live = haBar(SERIES[0], null)
    expect(live).toEqual({ open: b0.open, high: b0.high, low: b0.low, close: b0.close })
  })

  it('matches the batch transform for the forming bar given the prior HA bar', () => {
    // The live path feeds the previous CLOSED HA bar; the forming bar must land
    // on exactly what the full transform produces — otherwise the last candle
    // would jump when a REST reload re-seeds the whole series.
    const ha = heikinAshi(SERIES)
    const live = haBar(SERIES[2], ha[1])
    expect(live.open).toBeCloseTo(ha[2].open, 10)
    expect(live.high).toBeCloseTo(ha[2].high, 10)
    expect(live.low).toBeCloseTo(ha[2].low, 10)
    expect(live.close).toBeCloseTo(ha[2].close, 10)
  })
})

describe('CHART_TYPES / isChartKind', () => {
  it('lists all six drawable kinds in display order', () => {
    expect(CHART_TYPES.map((t) => t.key)).toEqual([
      'candles',
      'heikin_ashi',
      'hollow',
      'bars',
      'line',
      'area',
    ])
  })
  it('every listed kind has a non-empty label', () => {
    for (const t of CHART_TYPES) {
      expect(typeof t.label).toBe('string')
      expect(t.label.length).toBeGreaterThan(0)
    }
  })
  it('guards known kinds', () => {
    expect(isChartKind('candles')).toBe(true)
    expect(isChartKind('heikin_ashi')).toBe(true)
    expect(isChartKind('hollow')).toBe(true)
    expect(isChartKind('bars')).toBe(true)
    expect(isChartKind('line')).toBe(true)
    expect(isChartKind('area')).toBe(true)
    expect(isChartKind('nope')).toBe(false)
    expect(isChartKind(null)).toBe(false)
    expect(isChartKind(undefined)).toBe(false)
  })
})

describe('seriesKind (which lightweight-charts series a kind needs)', () => {
  it('groups candles / heikin_ashi / hollow under the ONE candlestick series', () => {
    expect(seriesKind('candles')).toBe('candlestick')
    expect(seriesKind('heikin_ashi')).toBe('candlestick')
    expect(seriesKind('hollow')).toBe('candlestick')
  })
  it('maps bars/line/area to their own series types', () => {
    expect(seriesKind('bars')).toBe('bar')
    expect(seriesKind('line')).toBe('line')
    expect(seriesKind('area')).toBe('area')
  })
  it('covers every CHART_TYPES entry (no kind maps to undefined)', () => {
    for (const t of CHART_TYPES) {
      expect(['candlestick', 'bar', 'line', 'area']).toContain(seriesKind(t.key))
    }
  })
})

describe('isValueSeries', () => {
  it('is true only for the single-value series (line / area)', () => {
    expect(isValueSeries('line')).toBe(true)
    expect(isValueSeries('area')).toBe(true)
    expect(isValueSeries('candlestick')).toBe(false)
    expect(isValueSeries('bar')).toBe(false)
  })
})

describe('toLineData (single-value points for line/area)', () => {
  it('plots the real CLOSE, one point per bar, same length/order/timestamps', () => {
    const pts = toLineData(SERIES)
    expect(pts).toEqual([
      { time: 1, value: 105 },
      { time: 2, value: 108 },
      { time: 3, value: 106 },
    ])
  })
  it('returns [] for empty input and does not mutate the source', () => {
    expect(toLineData([])).toEqual([])
    const snapshot = JSON.parse(JSON.stringify(SERIES))
    toLineData(SERIES)
    expect(SERIES).toEqual(snapshot)
  })
})

describe('hollowStyle (one bar)', () => {
  it('first bar (no prev close) colours by close vs open — up & hollow when close>=open', () => {
    const s = hollowStyle({ open: 100, high: 110, low: 95, close: 105 }, null, UP, DOWN)
    expect(s.borderColor).toBe(UP) // close(105) >= open(100) → up colour
    expect(s.wickColor).toBe(UP)
    expect(s.color).toBe(HOLLOW_TRANSPARENT) // bullish body → hollow
  })

  it('first bar down & filled when close<open', () => {
    const s = hollowStyle({ open: 105, high: 106, low: 95, close: 98 }, null, UP, DOWN)
    expect(s.borderColor).toBe(DOWN)
    expect(s.color).toBe(DOWN) // bearish body → filled with the down colour
  })

  it('colours by PREVIOUS close, independent of the body fill', () => {
    // Bar closed UP vs its open (hollow) but DOWN vs the previous close → red hollow.
    const hollowButDown = hollowStyle({ open: 100, high: 112, low: 99, close: 108 }, 120, UP, DOWN)
    expect(hollowButDown.color).toBe(HOLLOW_TRANSPARENT) // close>=open → hollow
    expect(hollowButDown.borderColor).toBe(DOWN) // close(108) < prevClose(120) → down colour
    // Bar closed DOWN vs its open (filled) but UP vs the previous close → green filled.
    const filledButUp = hollowStyle({ open: 110, high: 111, low: 104, close: 106 }, 100, UP, DOWN)
    expect(filledButUp.color).toBe(UP) // close<open → filled, coloured up
    expect(filledButUp.borderColor).toBe(UP) // close(106) >= prevClose(100) → up colour
  })

  it('falls back to close vs open when prevClose is non-finite', () => {
    const s = hollowStyle({ open: 100, high: 110, low: 95, close: 105 }, NaN, UP, DOWN)
    expect(s.borderColor).toBe(UP)
  })
})

describe('hollowStyles (batch)', () => {
  it('keeps length/order and colours each bar against the prior close', () => {
    const styles = hollowStyles(SERIES, UP, DOWN)
    expect(styles.length).toBe(SERIES.length)
    // Bar 0: no prev close, close(105)>=open(100) → up.
    expect(styles[0].borderColor).toBe(UP)
    // Bar 1: close(108) >= prevClose(105) → up.
    expect(styles[1].borderColor).toBe(UP)
    // Bar 2: close(106) < prevClose(108) → down (even though 106>=open? open=108 → filled).
    expect(styles[2].borderColor).toBe(DOWN)
    expect(styles[2].color).toBe(DOWN) // close(106) < open(108) → filled
  })
})
