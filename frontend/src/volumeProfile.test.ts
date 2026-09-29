import { describe, it, expect } from 'vitest'
import { volumeProfile, valueArea, type VolumeRow } from './volumeProfile'
import type { Candle } from './types'

// Build candles; OHLC default to the close, volume defaults to 1. Pass highs/
// lows/vols to shape the price range and the traded volume per bar.
function candles(
  closes: number[],
  opts?: { highs?: number[]; lows?: number[]; vols?: number[] },
): Candle[] {
  return closes.map((c, i) => ({
    time: 1_700_000_000 + i * 60,
    open: c,
    high: opts?.highs?.[i] ?? c,
    low: opts?.lows?.[i] ?? c,
    close: c,
    volume: opts?.vols?.[i] ?? 1,
  }))
}

const sumRows = (vols: number[]) => vols.reduce((a, b) => a + b, 0)

describe('volumeProfile', () => {
  it('returns empty for no candles', () => {
    const vp = volumeProfile([])
    expect(vp.rows).toEqual([])
    expect(vp.poc).toBeNull()
    expect(vp.maxVolume).toBe(0)
  })

  it('collapses a flat price range into one bucket holding all the volume', () => {
    const vp = volumeProfile(candles([100, 100, 100], { vols: [2, 3, 5] }), 24)
    expect(vp.rows).toHaveLength(1)
    expect(vp.rows[0].volume).toBe(10)
    expect(vp.poc).toBe(100)
  })

  it('conserves total volume across the buckets (nothing invented or lost)', () => {
    const c = candles([101, 103, 107, 104, 109], {
      lows: [100, 102, 105, 103, 108],
      highs: [102, 105, 108, 106, 110],
      vols: [4, 9, 2, 6, 3],
    })
    const vp = volumeProfile(c, 8)
    expect(sumRows(vp.rows.map((r) => r.volume))).toBeCloseTo(4 + 9 + 2 + 6 + 3, 6)
  })

  it('spreads one wide bar evenly across the buckets it spans', () => {
    // A single bar low=100 high=104 sets the range; across 4 buckets its volume
    // splits equally (overlap 1 of 4 each).
    const vp = volumeProfile(candles([102], { lows: [100], highs: [104], vols: [8] }), 4)
    expect(vp.rows).toHaveLength(4)
    for (const r of vp.rows) expect(r.volume).toBeCloseTo(2, 6)
  })

  it('puts the POC at the price band where the most volume traded', () => {
    // Heavy volume clustered at 100, light at 105/110.
    const vp = volumeProfile(candles([100, 100, 105, 110], { vols: [5, 5, 1, 1] }), 10)
    expect(vp.maxVolume).toBe(10)
    expect(vp.poc).not.toBeNull()
    expect(vp.poc as number).toBeGreaterThanOrEqual(100)
    expect(vp.poc as number).toBeLessThanOrEqual(101)
  })

  it('clamps a non-positive bin count to a single bucket', () => {
    const vp = volumeProfile(candles([100, 101, 102], { vols: [1, 1, 1] }), 0)
    expect(vp.rows).toHaveLength(1)
    expect(vp.rows[0].volume).toBe(3)
  })

  it('ignores non-finite / negative volumes without throwing', () => {
    const c = candles([100, 101, 102], { vols: [Number.NaN, -5, 4] })
    const vp = volumeProfile(c, 4)
    expect(sumRows(vp.rows.map((r) => r.volume))).toBeCloseTo(4, 6)
  })

  it('reports a value area that brackets the POC and drops the light tails', () => {
    // Volume clustered at 100–101, thin tails out to 110.
    const vp = volumeProfile(
      candles([100, 100, 100, 101, 101, 105, 110], { vols: [8, 8, 8, 6, 6, 1, 1] }),
      12,
    )
    expect(vp.vah).not.toBeNull()
    expect(vp.val).not.toBeNull()
    expect(vp.poc).not.toBeNull()
    // VAL <= POC <= VAH — the band contains the point of control.
    expect(vp.val as number).toBeLessThanOrEqual(vp.poc as number)
    expect(vp.vah as number).toBeGreaterThanOrEqual(vp.poc as number)
    // The thin 110 tail is outside the accepted range.
    expect(vp.vah as number).toBeLessThan(110)
  })

  it('has no value area when there is no volume', () => {
    const vp = volumeProfile(candles([100, 101], { vols: [0, 0] }), 4)
    expect(vp.vah).toBeNull()
    expect(vp.val).toBeNull()
  })

  it('collapses the value area onto the single price of a flat range', () => {
    const vp = volumeProfile(candles([100, 100], { vols: [2, 3] }), 24)
    expect(vp.vah).toBe(100)
    expect(vp.val).toBe(100)
  })
})

// Build unit-width rows (lo=i, hi=i+1, mid=i+0.5) with the given volumes.
const mkRows = (vols: number[]): VolumeRow[] =>
  vols.map((v, i) => ({ lo: i, hi: i + 1, mid: i + 0.5, volume: v }))

describe('valueArea', () => {
  it('returns nulls for an empty or out-of-range profile', () => {
    expect(valueArea([], 0)).toEqual({ vah: null, val: null })
    expect(valueArea(mkRows([1, 2, 1]), 9)).toEqual({ vah: null, val: null })
  })

  it('returns nulls when every bucket is empty', () => {
    expect(valueArea(mkRows([0, 0, 0]), 1)).toEqual({ vah: null, val: null })
  })

  it('expands from the POC toward the heavier side to reach ~70%', () => {
    // POC at index 3 (vol 20). total=31, target=21.7. The pair above (4+1=5)
    // beats the pair below (3+1=4), so the band grows up to index 5.
    const rows = mkRows([1, 1, 3, 20, 4, 1, 1])
    const { vah, val } = valueArea(rows, 3, 0.7)
    expect(val).toBe(3) // rows[3].lo — POC bucket stayed the low edge
    expect(vah).toBe(6) // rows[5].hi
  })

  it('covers at least the requested share of the volume', () => {
    const rows = mkRows([1, 1, 3, 20, 4, 1, 1])
    const total = 31
    const { vah, val } = valueArea(rows, 3, 0.7)
    const covered = rows
      .filter((r) => r.lo >= (val as number) && r.hi <= (vah as number))
      .reduce((a, r) => a + r.volume, 0)
    expect(covered / total).toBeGreaterThanOrEqual(0.7)
  })

  it('spans the whole profile at 100% and only the POC bucket at 0%', () => {
    const rows = mkRows([2, 5, 3])
    expect(valueArea(rows, 1, 1)).toEqual({ vah: 3, val: 0 }) // rows[2].hi / rows[0].lo
    expect(valueArea(rows, 1, 0)).toEqual({ vah: 2, val: 1 }) // POC bucket only
  })
})
