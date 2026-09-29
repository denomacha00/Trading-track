import { describe, it, expect } from 'vitest'
import { alignCompare, percentChange, formatPct, type CloseBar } from './compare'

describe('alignCompare', () => {
  const main = [100, 200, 300, 400, 500]

  it('lines the compare closes up on matching main bar times', () => {
    const cmp: CloseBar[] = [
      { time: 100, close: 10 },
      { time: 200, close: 11 },
      { time: 300, close: 12 },
      { time: 400, close: 13 },
      { time: 500, close: 14 },
    ]
    const out = alignCompare(main, cmp)
    expect(out).toEqual([
      { time: 100, value: 10 },
      { time: 200, value: 11 },
      { time: 300, value: 12 },
      { time: 400, value: 13 },
      { time: 500, value: 14 },
    ])
  })

  it('leaves an honest gap where the compare symbol has no bar', () => {
    const cmp: CloseBar[] = [
      { time: 100, close: 10 },
      // no 200
      { time: 300, close: 12 },
      { time: 500, close: 14 },
    ]
    const out = alignCompare(main, cmp)
    expect(out.map((p) => p.time)).toEqual([100, 300, 500])
  })

  it('ignores compare bars whose time is not on the main axis', () => {
    const cmp: CloseBar[] = [
      { time: 150, close: 99 }, // off-grid
      { time: 200, close: 11 },
    ]
    const out = alignCompare(main, cmp)
    expect(out).toEqual([{ time: 200, value: 11 }])
  })

  it('skips non-finite closes', () => {
    const cmp: CloseBar[] = [
      { time: 100, close: NaN },
      { time: 200, close: Infinity },
      { time: 300, close: 12 },
    ]
    const out = alignCompare(main, cmp)
    expect(out).toEqual([{ time: 300, value: 12 }])
  })

  it('enforces strictly ascending unique times (drops duplicates/rewinds)', () => {
    const cmp: CloseBar[] = [
      { time: 100, close: 10 },
      { time: 100, close: 10.5 },
      { time: 200, close: 11 },
    ]
    // main has 100 once, so only one 100 survives regardless; verify a rewound
    // main axis never emits a non-increasing time.
    const out = alignCompare([100, 100, 200], cmp)
    const times = out.map((p) => p.time)
    for (let i = 1; i < times.length; i++) expect(times[i]).toBeGreaterThan(times[i - 1])
  })

  it('returns empty when nothing overlaps', () => {
    expect(alignCompare(main, [{ time: 999, close: 1 }])).toEqual([])
    expect(alignCompare([], [{ time: 100, close: 1 }])).toEqual([])
  })
})

describe('percentChange', () => {
  it('computes first→last percent move', () => {
    expect(percentChange([100, 110])).toBeCloseTo(10, 6)
    expect(percentChange([100, 90])).toBeCloseTo(-10, 6)
    expect(percentChange([50, 60, 75])).toBeCloseTo(50, 6)
  })
  it('ignores non-finite values at the ends', () => {
    expect(percentChange([NaN, 100, 120, Infinity])).toBeCloseTo(20, 6)
  })
  it('is null when it cannot be computed', () => {
    expect(percentChange([])).toBeNull()
    expect(percentChange([42])).toBeNull()
    expect(percentChange([NaN, NaN])).toBeNull()
    expect(percentChange([0, 100])).toBeNull() // zero base
  })
})

describe('formatPct', () => {
  it('signs and rounds to one decimal', () => {
    expect(formatPct(3.24)).toBe('+3.2%')
    expect(formatPct(-1.26)).toBe('-1.3%')   // negative keeps its sign, rounds up
    expect(formatPct(0)).toBe('0.0%')
  })
  it('renders an em dash when unknown', () => {
    expect(formatPct(null)).toBe('—')
    expect(formatPct(NaN)).toBe('—')
    expect(formatPct(Infinity)).toBe('—')
  })
})
