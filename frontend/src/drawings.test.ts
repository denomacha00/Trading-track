import { describe, it, expect } from 'vitest'
import {
  distToSegment,
  pointNearSegment,
  pointNearRect,
  pointNearHLine,
  distToRay,
  pointNearRay,
  priceOnLine,
  channelOffset,
  fibLevels,
  FIB_LEVELS,
  positionStats,
  measure,
  drawingsKey,
  serializeDrawings,
  parseDrawings,
  validateDrawing,
  loadDrawings,
  saveDrawings,
  newDrawingId,
  TOOL_ANCHORS,
  type Drawing,
} from './drawings'

// These run under vitest's default node environment (no DOM). Everything tested
// here is pure — the canvas/mouse wiring in PriceChart is verified live in the
// preview, but every decision it delegates to is pinned down below.

describe('distToSegment', () => {
  it('is the perpendicular drop onto the segment body', () => {
    // Horizontal segment from (0,0) to (10,0); point straight above the middle.
    expect(distToSegment({ x: 5, y: 4 }, { x: 0, y: 0 }, { x: 10, y: 0 })).toBe(4)
  })
  it('clamps past an endpoint (distance to the nearer end, not the infinite line)', () => {
    // Point beyond the right end: on the infinite line its distance is 0, but
    // clamped to the segment it's the 5px gap to the endpoint (10,0).
    expect(distToSegment({ x: 15, y: 0 }, { x: 0, y: 0 }, { x: 10, y: 0 })).toBe(5)
  })
  it('returns 0 for a point lying on the segment', () => {
    expect(distToSegment({ x: 3, y: 3 }, { x: 0, y: 0 }, { x: 6, y: 6 })).toBeCloseTo(0)
  })
  it('handles a degenerate (zero-length) segment as distance to the point', () => {
    expect(distToSegment({ x: 3, y: 4 }, { x: 0, y: 0 }, { x: 0, y: 0 })).toBe(5)
  })
})

describe('pointNearSegment', () => {
  const a = { x: 0, y: 0 }
  const b = { x: 10, y: 0 }
  it('is true within tolerance and false beyond it', () => {
    expect(pointNearSegment({ x: 5, y: 5 }, a, b, 6)).toBe(true)
    expect(pointNearSegment({ x: 5, y: 5 }, a, b, 4)).toBe(false)
  })
})

describe('pointNearRect (selection follows the outline, not the fill)', () => {
  const a = { x: 0, y: 0 }
  const b = { x: 100, y: 60 }
  it('is true near any edge', () => {
    expect(pointNearRect({ x: 50, y: 2 }, a, b, 4)).toBe(true) // top edge
    expect(pointNearRect({ x: 98, y: 30 }, a, b, 4)).toBe(true) // right edge
    expect(pointNearRect({ x: 50, y: 58 }, a, b, 4)).toBe(true) // bottom edge
    expect(pointNearRect({ x: 2, y: 30 }, a, b, 4)).toBe(true) // left edge
  })
  it('is true near a corner', () => {
    expect(pointNearRect({ x: 1, y: 1 }, a, b, 4)).toBe(true)
  })
  it('is FALSE in the hollow middle', () => {
    expect(pointNearRect({ x: 50, y: 30 }, a, b, 4)).toBe(false)
  })
  it('is false far outside', () => {
    expect(pointNearRect({ x: 200, y: 200 }, a, b, 4)).toBe(false)
  })
  it('works regardless of which corners are passed (a/b order)', () => {
    expect(pointNearRect({ x: 50, y: 2 }, b, a, 4)).toBe(true)
  })
})

describe('pointNearHLine', () => {
  it('is a vertical tolerance band around the line', () => {
    expect(pointNearHLine(103, 100, 4)).toBe(true)
    expect(pointNearHLine(106, 100, 4)).toBe(false)
  })
})

describe('measure', () => {
  const H = 3600
  it('reads an up move: price delta, % of the FROM price, and whole bars', () => {
    const m = measure({ time: 0, price: 100 }, { time: 3 * H, price: 110 }, H)
    expect(m.dPrice).toBeCloseTo(10)
    expect(m.dPct).toBeCloseTo(10)
    expect(m.bars).toBe(3)
    expect(m.direction).toBe('up')
  })
  it('reads a down move and keeps bar count positive even if b is earlier', () => {
    const m = measure({ time: 5 * H, price: 100 }, { time: 2 * H, price: 90 }, H)
    expect(m.dPrice).toBeCloseTo(-10)
    expect(m.direction).toBe('down')
    expect(m.bars).toBe(3)
  })
  it('is flat when prices are equal (and % is 0)', () => {
    const m = measure({ time: 0, price: 100 }, { time: H, price: 100 }, H)
    expect(m.direction).toBe('flat')
    expect(m.dPct).toBe(0)
  })
  it('never divides by zero when the FROM price is 0', () => {
    expect(measure({ time: 0, price: 0 }, { time: H, price: 5 }, H).dPct).toBe(0)
  })
  it('returns 0 bars for a non-positive bar duration', () => {
    expect(measure({ time: 0, price: 1 }, { time: H, price: 2 }, 0).bars).toBe(0)
  })
})

describe('distToRay / pointNearRay (near end clamps, far end is infinite)', () => {
  const a = { x: 0, y: 0 }
  const b = { x: 10, y: 0 }
  it('is the perpendicular drop when the foot is on the drawn part', () => {
    expect(distToRay({ x: 5, y: 4 }, a, b)).toBe(4)
  })
  it('does NOT clamp past b — the ray runs to infinity', () => {
    expect(distToRay({ x: 100, y: 3 }, a, b)).toBe(3) // segment would clamp to b (~90px away)
  })
  it('clamps behind a to the distance to a', () => {
    expect(distToRay({ x: -3, y: 4 }, a, b)).toBeCloseTo(5) // foot pulled back to a=(0,0)
  })
  it('handles a degenerate ray as distance to a', () => {
    expect(distToRay({ x: 3, y: 4 }, a, a)).toBe(5)
  })
  it('pointNearRay respects tolerance', () => {
    expect(pointNearRay({ x: 100, y: 3 }, a, b, 4)).toBe(true)
    expect(pointNearRay({ x: 100, y: 3 }, a, b, 2)).toBe(false)
  })
})

describe('priceOnLine (linear inter/extrapolation in data space)', () => {
  const a = { time: 0, price: 10 }
  const b = { time: 10, price: 20 }
  it('interpolates between anchors', () => {
    expect(priceOnLine(a, b, 5)).toBeCloseTo(15)
  })
  it('extrapolates past b', () => {
    expect(priceOnLine(a, b, 20)).toBeCloseTo(30)
  })
  it('falls back to a.price for a vertical line (no divide-by-zero)', () => {
    expect(priceOnLine({ time: 5, price: 10 }, { time: 5, price: 99 }, 7)).toBe(10)
  })
})

describe('channelOffset (vertical gap from c to the a→b line)', () => {
  it('is the price of c minus the line price at c.time', () => {
    // line price == time here; c sits 3 above the line at time 5.
    expect(channelOffset({ time: 0, price: 0 }, { time: 10, price: 10 }, { time: 5, price: 8 })).toBeCloseTo(3)
  })
})

describe('fibLevels', () => {
  const a = { time: 0, price: 100 }
  const b = { time: 10, price: 0 }
  it('exposes the standard ratio ladder', () => {
    expect(FIB_LEVELS).toEqual([0, 0.236, 0.382, 0.5, 0.618, 0.786, 1, 1.272, 1.618])
  })
  it('anchors 0 at b.price and 1 at a.price, extends past 1', () => {
    const levels = fibLevels(a, b)
    expect(levels).toHaveLength(FIB_LEVELS.length)
    expect(levels[0]).toEqual({ ratio: 0, price: 0 }) // b.price
    expect(levels[levels.length - 3]).toEqual({ ratio: 1, price: 100 }) // a.price
    expect(levels.find((l) => l.ratio === 0.5)?.price).toBeCloseTo(50)
    expect(levels.find((l) => l.ratio === 1.618)?.price).toBeCloseTo(161.8) // extension past a
  })
})

describe('positionStats (direction + risk/reward, derived not guessed)', () => {
  it('reads a long: target above entry', () => {
    const s = positionStats(100, 130, 90)
    expect(s.dir).toBe('long')
    expect(s.risk).toBeCloseTo(10)
    expect(s.reward).toBeCloseTo(30)
    expect(s.riskPct).toBeCloseTo(10)
    expect(s.rewardPct).toBeCloseTo(30)
    expect(s.rr).toBeCloseTo(3)
  })
  it('reads a short: target below entry', () => {
    const s = positionStats(100, 80, 110)
    expect(s.dir).toBe('short')
    expect(s.risk).toBeCloseTo(10)
    expect(s.reward).toBeCloseTo(20)
    expect(s.rr).toBeCloseTo(2)
  })
  it('rr is 0 (not Infinity) when stop === entry', () => {
    expect(positionStats(100, 130, 100).rr).toBe(0)
  })
  it('percentages are 0 when entry is 0 (no divide-by-zero)', () => {
    const s = positionStats(0, 5, -5)
    expect(s.riskPct).toBe(0)
    expect(s.rewardPct).toBe(0)
  })
})

describe('TOOL_ANCHORS', () => {
  it('gives every tool the right number of clicks', () => {
    expect(TOOL_ANCHORS).toEqual({
      cursor: 0,
      hline: 1,
      trend: 2,
      rect: 2,
      ray: 2,
      fib: 2,
      measure: 2,
      channel: 3,
      position: 3,
    })
  })
})

describe('drawingsKey', () => {
  it('namespaces per symbol and timeframe', () => {
    expect(drawingsKey('BTC/USDT', '1h')).toBe('tt.drawings.BTC/USDT.1h')
    expect(drawingsKey('ETH/USDT', '15m')).toBe('tt.drawings.ETH/USDT.15m')
  })
})

const sample: Drawing[] = [
  { id: 'a', kind: 'trend', a: { time: 1, price: 10 }, b: { time: 2, price: 20 }, color: '#fff' },
  { id: 'b', kind: 'hline', price: 42, color: '#f0b90b' },
  { id: 'c', kind: 'rect', a: { time: 3, price: 5 }, b: { time: 9, price: 8 }, color: '#3b82f6' },
  { id: 'd', kind: 'ray', a: { time: 1, price: 10 }, b: { time: 4, price: 16 }, color: '#22d3ee' },
  { id: 'e', kind: 'fib', a: { time: 2, price: 30 }, b: { time: 7, price: 10 }, color: '#a78bfa' },
  { id: 'f', kind: 'measure', a: { time: 1, price: 100 }, b: { time: 5, price: 108 }, color: '#eab308' },
  {
    id: 'g',
    kind: 'channel',
    a: { time: 1, price: 10 },
    b: { time: 5, price: 30 },
    c: { time: 2, price: 5 },
    color: '#f472b6',
  },
  {
    id: 'h',
    kind: 'position',
    a: { time: 1, price: 100 },
    b: { time: 6, price: 130 },
    c: { time: 1, price: 90 },
    color: '#16c784',
  },
]

describe('serialize / parse round trip', () => {
  it('preserves every kind exactly', () => {
    expect(parseDrawings(serializeDrawings(sample))).toEqual(sample)
  })
})

describe('parseDrawings is defensive about untrusted storage', () => {
  it('returns [] for null / empty / bad JSON / non-array', () => {
    expect(parseDrawings(null)).toEqual([])
    expect(parseDrawings('')).toEqual([])
    expect(parseDrawings('{not json')).toEqual([])
    expect(parseDrawings('{}')).toEqual([])
    expect(parseDrawings('42')).toEqual([])
  })
  it('keeps the valid subset and drops junk entries', () => {
    const raw = JSON.stringify([
      sample[0],
      { id: 'x', kind: 'bogus', color: '#fff' }, // unknown kind
      { kind: 'hline', price: 1, color: '#fff' }, // missing id
      { id: 'y', kind: 'hline', color: '#fff' }, // missing price
      { id: 'z', kind: 'trend', a: { time: 1, price: NaN }, b: { time: 2, price: 3 }, color: '#fff' }, // NaN coord
      sample[1],
    ])
    expect(parseDrawings(raw)).toEqual([sample[0], sample[1]])
  })
  it('strips any extra fields (rebuilds a clean object)', () => {
    const raw = JSON.stringify([{ ...sample[1], evil: 'x', extra: 99 }])
    expect(parseDrawings(raw)).toEqual([sample[1]])
  })
})

describe('validateDrawing', () => {
  it('accepts each valid kind', () => {
    for (const d of sample) expect(validateDrawing(d)).toEqual(d)
  })
  it('rejects empty id, missing/overlong color, and non-objects', () => {
    expect(validateDrawing({ ...sample[1], id: '' })).toBeNull()
    expect(validateDrawing({ ...sample[1], color: '' })).toBeNull()
    expect(validateDrawing({ ...sample[1], color: 'x'.repeat(40) })).toBeNull()
    expect(validateDrawing(null)).toBeNull()
    expect(validateDrawing('nope')).toBeNull()
  })
  it('rejects a 2-point kind missing an anchor', () => {
    expect(validateDrawing({ id: 'r', kind: 'ray', a: { time: 1, price: 2 }, color: '#fff' })).toBeNull()
    expect(
      validateDrawing({ id: 'r', kind: 'fib', a: { time: 1, price: 2 }, b: { time: 3, price: NaN }, color: '#fff' }),
    ).toBeNull()
  })
  it('rejects a 3-point kind (channel/position) missing the third anchor', () => {
    const noC = { id: 'g2', kind: 'channel', a: { time: 1, price: 2 }, b: { time: 3, price: 4 }, color: '#fff' }
    expect(validateDrawing(noC)).toBeNull()
    const noCPos = { id: 'h2', kind: 'position', a: { time: 1, price: 2 }, b: { time: 3, price: 4 }, color: '#fff' }
    expect(validateDrawing(noCPos)).toBeNull()
  })
})

describe('localStorage wrappers stay safe without a DOM', () => {
  it('loadDrawings returns [] and saveDrawings does not throw under node', () => {
    expect(loadDrawings('BTC/USDT', '1h')).toEqual([])
    expect(() => saveDrawings('BTC/USDT', '1h', sample)).not.toThrow()
  })
})

describe('newDrawingId', () => {
  it('is a non-empty string and effectively unique per call', () => {
    const a = newDrawingId()
    const b = newDrawingId()
    expect(typeof a).toBe('string')
    expect(a.length).toBeGreaterThan(1)
    expect(a).not.toBe(b)
  })
})


