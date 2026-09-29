// Compare symbols — the pure data math behind overlaying a second instrument on
// the chart (TradingView's paid "Compare"). No React, no chart: given the main
// chart's bar times and a second symbol's candles, it lines the second series up
// on the same time axis and computes its move over the window. The overlay series
// and the left price scale are wired in PriceChart.tsx; the symbol picker + fetch
// live in App.tsx. Everything here is a pure read of real closes — nothing is
// invented or interpolated into existence.

// Anything with a bar time (unix seconds, matching the chart) and a close. Candle
// satisfies this structurally, so callers can pass Candle[] straight in.
export interface CloseBar {
  time: number
  close: number
}

export interface ComparePoint {
  time: number
  value: number
}

// Align a compare symbol's closes onto the main chart's bar times so the overlay
// line sits exactly on the same time axis as the candles. Fetched at the same
// timeframe, the timestamps line up 1:1; any main bar with no matching compare
// bar (e.g. a newer coin that didn't trade that far back) is simply left out,
// leaving an honest gap rather than a faked value. Non-finite closes are skipped.
export function alignCompare(mainTimes: number[], compareBars: CloseBar[]): ComparePoint[] {
  const byTime = new Map<number, number>()
  for (const b of compareBars) {
    if (Number.isFinite(b.close)) byTime.set(b.time, b.close)
  }
  const out: ComparePoint[] = []
  let prev = -Infinity
  for (const t of mainTimes) {
    const c = byTime.get(t)
    // Guard against out-of-order/duplicate times — a line series needs strictly
    // ascending, unique timestamps or lightweight-charts throws.
    if (c != null && t > prev) {
      out.push({ time: t, value: c })
      prev = t
    }
  }
  return out
}

// Percent change of a series of closes from its first finite close to its last —
// used for the compare legend label ("ETH/USDT  +3.2%"). Null when it can't be
// computed (fewer than two finite closes, or a zero base). Restricting to the
// bars that overlap the main window (pass the aligned closes) makes the label
// describe exactly what's on screen.
export function percentChange(closes: number[]): number | null {
  const finite = closes.filter((c) => Number.isFinite(c))
  if (finite.length < 2) return null
  const first = finite[0]
  const last = finite[finite.length - 1]
  if (first === 0) return null
  return (last / first - 1) * 100
}

// Format a percent change for the legend: signed, one decimal, or an em dash when
// unknown. Kept here (not in JSX) so it's unit-testable.
export function formatPct(pct: number | null): string {
  if (pct == null || !Number.isFinite(pct)) return '—'
  const sign = pct > 0 ? '+' : ''
  return `${sign}${pct.toFixed(1)}%`
}
