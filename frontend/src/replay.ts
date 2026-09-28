// Bar replay — the pure state math behind stepping through history one candle
// at a time (TradingView-style "Replay"). No React and no chart here: given a
// total bar count and a cursor, it decides how many bars are revealed, which
// window of them to show, and how fast playback advances. Everything is
// deterministic so it can be unit-tested to the letter; the React wiring and
// the actual candle slicing live in ReplayControls.tsx / App.tsx.

// Playback speeds offered in the UI, in BARS PER SECOND, slowest → fastest.
export const REPLAY_SPEEDS = [0.5, 1, 2, 4, 8] as const
export type ReplaySpeed = (typeof REPLAY_SPEEDS)[number]

// How many bars stay on screen during replay. A fixed-width sliding window (as
// opposed to revealing a growing prefix) keeps the time axis stable — new bars
// scroll in from the right while old ones scroll off the left, the way an
// exchange replay feels — and means the chart never needs to re-fit mid-play.
export const REPLAY_WINDOW = 180

// Milliseconds between auto-advance ticks for a speed in bars/sec. A non-positive
// speed yields 0 (caller should treat that as "don't schedule").
export function speedToIntervalMs(speed: number): number {
  return speed > 0 ? Math.round(1000 / speed) : 0
}

// Clamp a revealed-bar count into [1, total] (or 0 when there are no bars). At
// least one bar is always revealed — an empty replay chart is useless — and
// never more bars than actually exist.
export function clampReplayCount(count: number, total: number): number {
  if (total <= 0) return 0
  const c = Math.round(count)
  if (c < 1) return 1
  if (c > total) return total
  return c
}

// Where replay starts when the user enters it without picking a bar: reveal a
// chunk of history for context, then let them step forward into the rest. ~60%
// of the bars, but always leave at least one bar still to reveal (so there is
// something to play into) and never fewer than one revealed.
export function initialReplayCount(total: number): number {
  if (total <= 0) return 0
  if (total <= 3) return 1
  return clampReplayCount(Math.floor(total * 0.6), total - 1)
}

// Advance (delta>0) or rewind (delta<0) the revealed count by `delta` bars,
// clamped to the valid range. Returns the new revealed count.
export function stepReplay(count: number, delta: number, total: number): number {
  return clampReplayCount(count + delta, total)
}

// True once every bar is revealed — playback should stop here.
export function isReplayEnd(count: number, total: number): boolean {
  return total > 0 && count >= total
}

// The window of bars to show for a given revealed count: the `width` bars ending
// at the cursor (so the newest revealed bar sits at the right edge with history
// to its left). Generic over the element type so it can be unit-tested with
// plain numbers and used with real candles alike.
export function replaySlice<T>(bars: T[], cursor: number, width: number = REPLAY_WINDOW): T[] {
  const end = clampReplayCount(cursor, bars.length)
  if (end <= 0) return []
  const start = Math.max(0, end - Math.max(1, Math.floor(width)))
  return bars.slice(start, end)
}
