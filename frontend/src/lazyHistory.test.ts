import { describe, it, expect } from 'vitest'
import {
  tfMs,
  oldestTime,
  nextOlderEndMs,
  shouldLoadOlder,
  mergeOlder,
  mergeRecent,
  TF_MS,
  OLDER_CHUNK,
} from './lazyHistory'
import type { Candle } from './types'

// Minimal candle at time t (seconds); price fields don't matter to the merge/
// range math, so they're derived from t just to keep rows distinct.
function bar(t: number): Candle {
  return { time: t, open: t, high: t + 1, low: t - 1, close: t, volume: 1 }
}
// A run of `n` ascending bars spaced `step` seconds apart, starting at `start`.
function run(start: number, n: number, step: number): Candle[] {
  return Array.from({ length: n }, (_, i) => bar(start + i * step))
}

describe('tfMs', () => {
  it('maps known timeframes and 0 for unknown', () => {
    expect(tfMs('1h')).toBe(3_600_000)
    expect(tfMs('1d')).toBe(86_400_000)
    expect(tfMs('nope')).toBe(0)
  })
  it('exposes OLDER_CHUNK and a full TF_MS map', () => {
    expect(OLDER_CHUNK).toBeGreaterThan(0)
    expect(Object.keys(TF_MS)).toContain('1w')
  })
})

describe('oldestTime / nextOlderEndMs', () => {
  it('returns the first bar time (seconds) or null', () => {
    expect(oldestTime([])).toBeNull()
    expect(oldestTime(run(1000, 3, 60))).toBe(1000)
  })
  it('nextOlderEndMs is the oldest bar in ms, exclusive, or null', () => {
    expect(nextOlderEndMs([])).toBeNull()
    expect(nextOlderEndMs(run(1000, 3, 60))).toBe(1_000_000)
  })
})

describe('shouldLoadOlder', () => {
  const candles = run(10_000, 100, 3600) // 100 hourly bars from t=10000

  it('triggers when the visible left edge nears the oldest bar', () => {
    // within 40 bars (40*3600s) of the oldest → true
    expect(shouldLoadOlder(candles, 10_000 + 5 * 3600, '1h')).toBe(true)
    expect(shouldLoadOlder(candles, 9_000, '1h')).toBe(true) // panned past the edge
  })
  it('does not trigger when still far from the oldest bar', () => {
    expect(shouldLoadOlder(candles, 10_000 + 80 * 3600, '1h')).toBe(false)
  })
  it('honestly returns false on empty chart, unknown tf, or a non-finite edge', () => {
    expect(shouldLoadOlder([], 10_000, '1h')).toBe(false)
    expect(shouldLoadOlder(candles, 10_000, 'weird-tf')).toBe(false)
    expect(shouldLoadOlder(candles, null, '1h')).toBe(false)
    expect(shouldLoadOlder(candles, Number.NaN, '1h')).toBe(false)
  })
  it('respects a custom threshold', () => {
    expect(shouldLoadOlder(candles, 10_000 + 20 * 3600, '1h', 10)).toBe(false)
    expect(shouldLoadOlder(candles, 10_000 + 20 * 3600, '1h', 25)).toBe(true)
  })
})

describe('mergeOlder', () => {
  it('prepends an older chunk in ascending order', () => {
    const existing = run(1000, 3, 100) // 1000,1100,1200
    const older = run(700, 3, 100) // 700,800,900
    const out = mergeOlder(existing, older)
    expect(out.map((c) => c.time)).toEqual([700, 800, 900, 1000, 1100, 1200])
  })
  it('drops overlap at/after the current oldest bar (defends a venue boundary dup)', () => {
    const existing = run(1000, 2, 100) // 1000,1100
    const older = [bar(800), bar(900), bar(1000) /* dup boundary */, bar(1100) /* overlap */]
    const out = mergeOlder(existing, older)
    expect(out.map((c) => c.time)).toEqual([800, 900, 1000, 1100])
  })
  it('skips non-finite times and de-dupes within the incoming chunk', () => {
    const existing = run(1000, 1, 100) // [1000]
    const older = [bar(800), bar(800), { ...bar(Number.NaN) }, bar(900)]
    const out = mergeOlder(existing, older)
    expect(out.map((c) => c.time)).toEqual([800, 900, 1000])
  })
  it('returns the SAME reference when nothing older survives (empty or all-overlap)', () => {
    const existing = run(1000, 2, 100)
    expect(mergeOlder(existing, [])).toBe(existing)
    expect(mergeOlder(existing, [bar(1000), bar(1100)])).toBe(existing) // all >= oldest
  })
  it('accepts any older chunk onto an empty existing list', () => {
    const older = run(700, 2, 100)
    expect(mergeOlder([], older).map((c) => c.time)).toEqual([700, 800])
  })
})

describe('mergeRecent', () => {
  it('preserves older history and refreshes the recent tail', () => {
    const existing = run(100, 10, 100) // 100..1000
    // poll returns the recent range 600..1200, with an updated (mutated) bar
    const recent = [bar(600), bar(700), bar(800), bar(900), { ...bar(1000), close: 999 }, bar(1100), bar(1200)]
    const out = mergeRecent(existing, recent)
    // head kept strictly < 600, then the whole poll appended (owns 600+)
    expect(out.map((c) => c.time)).toEqual([100, 200, 300, 400, 500, 600, 700, 800, 900, 1000, 1100, 1200])
    expect(out.find((c) => c.time === 1000)!.close).toBe(999) // poll's fresher bar wins
  })
  it('uses the poll alone when it reaches at least as far back as existing', () => {
    const existing = run(500, 3, 100) // 500,600,700
    const recent = run(400, 6, 100) // 400..900 (older start than existing)
    expect(mergeRecent(existing, recent)).toBe(recent)
  })
  it('empty poll keeps existing untouched; empty existing yields the poll', () => {
    const existing = run(500, 3, 100)
    expect(mergeRecent(existing, [])).toBe(existing)
    const recent = run(400, 2, 100)
    expect(mergeRecent([], recent)).toBe(recent)
  })
})
