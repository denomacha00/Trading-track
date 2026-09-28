import { describe, it, expect } from 'vitest'
import {
  REPLAY_SPEEDS,
  REPLAY_WINDOW,
  speedToIntervalMs,
  clampReplayCount,
  initialReplayCount,
  stepReplay,
  isReplayEnd,
  replaySlice,
} from './replay'

describe('REPLAY_SPEEDS', () => {
  it('is a non-empty ascending list of positive bars/sec', () => {
    expect(REPLAY_SPEEDS.length).toBeGreaterThan(0)
    for (const s of REPLAY_SPEEDS) expect(s).toBeGreaterThan(0)
    const sorted = [...REPLAY_SPEEDS].sort((a, b) => a - b)
    expect([...REPLAY_SPEEDS]).toEqual(sorted)
  })
})

describe('speedToIntervalMs', () => {
  it('inverts bars/sec into a millisecond interval', () => {
    expect(speedToIntervalMs(1)).toBe(1000)
    expect(speedToIntervalMs(2)).toBe(500)
    expect(speedToIntervalMs(4)).toBe(250)
    expect(speedToIntervalMs(0.5)).toBe(2000)
    expect(speedToIntervalMs(8)).toBe(125)
  })
  it('returns 0 for a non-positive speed (caller should not schedule)', () => {
    expect(speedToIntervalMs(0)).toBe(0)
    expect(speedToIntervalMs(-3)).toBe(0)
  })
})

describe('clampReplayCount', () => {
  it('returns 0 when there are no bars', () => {
    expect(clampReplayCount(5, 0)).toBe(0)
    expect(clampReplayCount(0, -2)).toBe(0)
  })
  it('never reveals fewer than one bar when bars exist', () => {
    expect(clampReplayCount(0, 10)).toBe(1)
    expect(clampReplayCount(-4, 10)).toBe(1)
  })
  it('never reveals more bars than exist', () => {
    expect(clampReplayCount(999, 10)).toBe(10)
  })
  it('rounds fractional counts', () => {
    expect(clampReplayCount(3.2, 10)).toBe(3)
    expect(clampReplayCount(3.7, 10)).toBe(4)
  })
  it('passes valid counts through', () => {
    expect(clampReplayCount(5, 10)).toBe(5)
  })
})

describe('initialReplayCount', () => {
  it('is 0 for an empty series', () => {
    expect(initialReplayCount(0)).toBe(0)
  })
  it('reveals a single bar for tiny histories', () => {
    expect(initialReplayCount(1)).toBe(1)
    expect(initialReplayCount(2)).toBe(1)
    expect(initialReplayCount(3)).toBe(1)
  })
  it('reveals ~60% but always leaves at least one bar to play into', () => {
    expect(initialReplayCount(100)).toBe(60)
    expect(initialReplayCount(10)).toBe(6)
    // total-1 cap: 60% of 4 = 2 (floored), still <= 3
    expect(initialReplayCount(4)).toBe(2)
  })
  it('never returns the full total (there must be something to reveal)', () => {
    for (const n of [4, 5, 10, 50, 100, 999]) {
      expect(initialReplayCount(n)).toBeLessThan(n)
    }
  })
})

describe('stepReplay', () => {
  it('advances forward, clamped to total', () => {
    expect(stepReplay(5, 1, 10)).toBe(6)
    expect(stepReplay(10, 1, 10)).toBe(10)
    expect(stepReplay(9, 5, 10)).toBe(10)
  })
  it('rewinds backward, clamped to one', () => {
    expect(stepReplay(5, -1, 10)).toBe(4)
    expect(stepReplay(1, -1, 10)).toBe(1)
    expect(stepReplay(3, -9, 10)).toBe(1)
  })
})

describe('isReplayEnd', () => {
  it('is true only once every bar is revealed', () => {
    expect(isReplayEnd(9, 10)).toBe(false)
    expect(isReplayEnd(10, 10)).toBe(true)
    expect(isReplayEnd(11, 10)).toBe(true)
  })
  it('is false when there are no bars', () => {
    expect(isReplayEnd(0, 0)).toBe(false)
  })
})

describe('replaySlice', () => {
  const bars = Array.from({ length: 500 }, (_, i) => i) // 0..499

  it('returns the window of bars ending at the cursor', () => {
    const w = replaySlice(bars, 300, 180)
    expect(w.length).toBe(180)
    expect(w[0]).toBe(120)
    expect(w[w.length - 1]).toBe(299) // cursor is exclusive end → last revealed is 299
  })
  it('does not run off the left edge early in playback', () => {
    const w = replaySlice(bars, 50, 180)
    expect(w[0]).toBe(0)
    expect(w.length).toBe(50)
    expect(w[w.length - 1]).toBe(49)
  })
  it('slides forward one bar at a time', () => {
    const a = replaySlice(bars, 300, 180)
    const b = replaySlice(bars, 301, 180)
    expect(b[0]).toBe(a[0] + 1) // oldest bar scrolled off
    expect(b[b.length - 1]).toBe(a[a.length - 1] + 1) // one new bar at the right
  })
  it('clamps the cursor into range', () => {
    expect(replaySlice(bars, 9999, 180).length).toBe(180)
    expect(replaySlice(bars, 9999, 180).slice(-1)[0]).toBe(499)
  })
  it('returns an empty window for an empty series', () => {
    expect(replaySlice([], 5, 180)).toEqual([])
  })
  it('defaults to REPLAY_WINDOW width', () => {
    expect(replaySlice(bars, 500).length).toBe(REPLAY_WINDOW)
  })
})
