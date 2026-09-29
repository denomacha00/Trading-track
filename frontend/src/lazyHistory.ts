// Lazy "load older history when you pan left" — TradingView-paid infinite-
// scroll parity. This module is PURE helpers only: given the bars already
// loaded and the chart's visible range, decide whether the user has panned near
// the oldest loaded bar and, if so, what older window to fetch; then stitch a
// fetched older chunk onto the front without duplicating or reordering. Every
// bar stays real exchange data — nothing here fabricates or pads a candle; it
// only works out which real ones to ask for next and merges them in time order.
import type { Candle } from './types'

// Bar spacing in milliseconds, mirroring the backend's _TIMEFRAME_MS so the
// "give me the N bars ending before T" math lines up on both sides. An unknown
// timeframe maps to 0, which callers read as "can't page older" — an honest
// no-op rather than a guessed spacing.
export const TF_MS: Record<string, number> = {
  '1m': 60_000,
  '5m': 5 * 60_000,
  '15m': 15 * 60_000,
  '30m': 30 * 60_000,
  '1h': 60 * 60_000,
  '2h': 2 * 60 * 60_000,
  '4h': 4 * 60 * 60_000,
  '6h': 6 * 60 * 60_000,
  '12h': 12 * 60 * 60_000,
  '1d': 24 * 60 * 60_000,
  '1w': 7 * 24 * 60 * 60_000,
}

// How many older bars to pull per lazy fetch, and how near (in bars) the left
// edge must come to the oldest loaded bar before we trigger one.
export const OLDER_CHUNK = 500
export const DEFAULT_THRESHOLD_BARS = 40

export function tfMs(timeframe: string): number {
  return TF_MS[timeframe] ?? 0
}

// The oldest (first) loaded bar time in SECONDS, or null when nothing's loaded.
export function oldestTime(candles: Candle[]): number | null {
  return candles.length ? candles[0].time : null
}

// End timestamp (ms, EXCLUSIVE) for the next older fetch: everything the backend
// returns must fall strictly before the oldest bar we already hold, so the two
// windows abut without overlapping. null when there's nothing loaded yet.
export function nextOlderEndMs(candles: Candle[]): number | null {
  const o = oldestTime(candles)
  return o == null ? null : o * 1000
}

// Whether the user has panned close enough to the oldest loaded bar that we
// should fetch an older chunk. `visibleFromSec` is the left edge of the chart's
// visible range (seconds); we trigger once it comes within `thresholdBars` of
// the oldest loaded bar. Guards: needs a non-empty chart, a known timeframe and
// a finite visible edge — otherwise it honestly returns false (no stray fetch).
export function shouldLoadOlder(
  candles: Candle[],
  visibleFromSec: number | null | undefined,
  timeframe: string,
  thresholdBars: number = DEFAULT_THRESHOLD_BARS,
): boolean {
  if (!candles.length) return false
  if (visibleFromSec == null || !Number.isFinite(visibleFromSec)) return false
  const ms = tfMs(timeframe)
  if (!ms) return false
  const oldest = candles[0].time
  return visibleFromSec <= oldest + thresholdBars * (ms / 1000)
}

// Prepend an older chunk onto the loaded bars, kept unique and strictly
// ascending by time. Any incoming row at/after the current oldest bar is dropped
// (the older window should end before it, but we defend against a venue handing
// back an overlapping boundary bar), non-finite times are skipped, and if
// nothing genuinely older survives the SAME array reference is returned — so a
// caller can detect "no older data came back" and stop paging.
export function mergeOlder(existing: Candle[], older: Candle[]): Candle[] {
  if (!older.length) return existing
  const cutoff = existing.length ? existing[0].time : Infinity
  const seen = new Set(existing.map((c) => c.time))
  const add: Candle[] = []
  for (const c of older) {
    if (!Number.isFinite(c.time)) continue
    if (c.time >= cutoff) continue
    if (seen.has(c.time)) continue
    seen.add(c.time)
    add.push(c)
  }
  if (!add.length) return existing
  add.sort((a, b) => a.time - b.time)
  return [...add, ...existing]
}

// Merge a fresh "recent bars" poll onto bars that may already include older,
// lazily-loaded history: keep every existing bar strictly older than the poll's
// first bar, then hand the recent range to the poll (which owns it, including
// the still-forming last bar). This lets the routine tail refresh WITHOUT wiping
// the older history a user scrolled in. Empty poll → keep existing untouched
// (same ref); no older history to preserve → just use the poll.
export function mergeRecent(existing: Candle[], recent: Candle[]): Candle[] {
  if (!recent.length) return existing
  if (!existing.length) return recent
  const firstRecent = recent[0].time
  const head: Candle[] = []
  for (const c of existing) {
    if (c.time < firstRecent) head.push(c)
    else break // existing is ascending — once we reach the recent range, stop
  }
  return head.length ? [...head, ...recent] : recent
}
