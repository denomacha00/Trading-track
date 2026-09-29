import { describe, it, expect } from 'vitest'
import { heikinAshi, haBar, CHART_TYPES, isChartKind } from './chartTypes'
import type { Candle } from './types'

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
  it('lists candles and heikin_ashi', () => {
    expect(CHART_TYPES.map((t) => t.key)).toEqual(['candles', 'heikin_ashi'])
  })
  it('guards known kinds', () => {
    expect(isChartKind('candles')).toBe(true)
    expect(isChartKind('heikin_ashi')).toBe(true)
    expect(isChartKind('line')).toBe(false)
    expect(isChartKind(null)).toBe(false)
  })
})
