// Chart drawing tools — the data model and all the *pure* math behind them.
// Everything here is deterministic and framework-free so it can be unit-tested
// to the letter; the actual canvas rendering and mouse wiring live in
// PriceChart.tsx, but every geometric decision it makes (what did the user
// click on? how far is this line? what does this measurement read?) is a call
// into one of these functions. Anchors are stored in DATA space — a price and
// a candle open time (unix seconds) — never pixels, so a drawing stays pinned
// to the same bar/price as you pan, zoom, or reload.

// A single anchor in data space.
export type Pt = { time: number; price: number }

// A user-drawn object. Each kind carries only what it needs:
//  - trend:    a segment between two anchors
//  - hline:    a full-width horizontal line at one price (time-independent)
//  - rect:     an axis-aligned box between two opposite corners
//  - ray:      a line from `a` through `b` extended to the right chart edge
//  - fib:      Fibonacci retracement/extension over the price range a↔b
//  - measure:  a ruler between two anchors (reads Δprice / Δ% / bars)
//  - channel:  a parallel channel — base line a→b plus a line through `c`
//              drawn parallel to it
//  - position: a long/short planner — `a` entry (time+price), `b` target
//              (time+price; its time sets the right edge), `c` stop (price).
//              Long vs short is read from the geometry (target above/below entry).
// `color` is a CSS color string; `id` is a stable unique key for React + hit
// selection.
export type Drawing =
  | { id: string; kind: 'trend'; a: Pt; b: Pt; color: string }
  | { id: string; kind: 'hline'; price: number; color: string }
  | { id: string; kind: 'rect'; a: Pt; b: Pt; color: string }
  | { id: string; kind: 'ray'; a: Pt; b: Pt; color: string }
  | { id: string; kind: 'fib'; a: Pt; b: Pt; color: string }
  | { id: string; kind: 'measure'; a: Pt; b: Pt; color: string }
  | { id: string; kind: 'channel'; a: Pt; b: Pt; c: Pt; color: string }
  | { id: string; kind: 'position'; a: Pt; b: Pt; c: Pt; color: string }

// The active toolbar tool. 'cursor' selects/deletes; the rest place drawings.
export type Tool =
  | 'cursor'
  | 'trend'
  | 'hline'
  | 'rect'
  | 'ray'
  | 'fib'
  | 'measure'
  | 'channel'
  | 'position'

// How many time+price anchors each tool collects before it commits. 'hline'
// needs a price only (1 click, time ignored); the two-corner/two-point tools
// need 2; the parallel channel and the position planner need 3. The mouse
// wiring in PriceChart reads this to know when a placement is finished.
export const TOOL_ANCHORS: Record<Tool, number> = {
  cursor: 0,
  hline: 1,
  trend: 2,
  rect: 2,
  ray: 2,
  fib: 2,
  measure: 2,
  channel: 3,
  position: 3,
}

// A point in pixel (screen) space, used only for hit-testing against what the
// user actually sees. PriceChart projects each drawing's data anchors to these
// before calling the hit helpers below.
export type Px = { x: number; y: number }

// --- Geometry (pure pixel-space math) -------------------------------------

// Shortest distance from point p to the line SEGMENT ab (not the infinite
// line): projects p onto ab, clamps the projection to the segment, and returns
// the distance to that clamped foot. Degenerate segment (a===b) → distance to a.
export function distToSegment(p: Px, a: Px, b: Px): number {
  const dx = b.x - a.x
  const dy = b.y - a.y
  const len2 = dx * dx + dy * dy
  if (len2 === 0) return Math.hypot(p.x - a.x, p.y - a.y)
  let t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2
  t = Math.max(0, Math.min(1, t))
  const fx = a.x + t * dx
  const fy = a.y + t * dy
  return Math.hypot(p.x - fx, p.y - fy)
}

// Is p within `tol` px of segment ab?
export function pointNearSegment(p: Px, a: Px, b: Px, tol: number): boolean {
  return distToSegment(p, a, b) <= tol
}

// Is p within `tol` px of the BORDER of the axis-aligned rectangle with
// opposite corners a and b? (Selection follows the drawn outline, like
// TradingView — clicking the hollow middle doesn't grab a huge box.)
export function pointNearRect(p: Px, a: Px, b: Px, tol: number): boolean {
  const tl = { x: Math.min(a.x, b.x), y: Math.min(a.y, b.y) }
  const br = { x: Math.max(a.x, b.x), y: Math.max(a.y, b.y) }
  const tr = { x: br.x, y: tl.y }
  const bl = { x: tl.x, y: br.y }
  return (
    pointNearSegment(p, tl, tr, tol) ||
    pointNearSegment(p, tr, br, tol) ||
    pointNearSegment(p, br, bl, tol) ||
    pointNearSegment(p, bl, tl, tol)
  )
}

// Is p within `tol` px (vertically) of a full-width horizontal line at y=lineY?
export function pointNearHLine(py: number, lineY: number, tol: number): boolean {
  return Math.abs(py - lineY) <= tol
}

// Shortest distance from p to the RAY that starts at a and passes through b,
// running to infinity past b. Same projection as distToSegment but the far end
// is never clamped — only the near end (t<0, "behind" a) is pulled back to a.
// Used for the ray tool, whose drawn line extends off the right of the chart.
export function distToRay(p: Px, a: Px, b: Px): number {
  const dx = b.x - a.x
  const dy = b.y - a.y
  const len2 = dx * dx + dy * dy
  if (len2 === 0) return Math.hypot(p.x - a.x, p.y - a.y)
  let t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / len2
  if (t < 0) t = 0
  const fx = a.x + t * dx
  const fy = a.y + t * dy
  return Math.hypot(p.x - fx, p.y - fy)
}

// Is p within `tol` px of the ray a→b (extended past b)?
export function pointNearRay(p: Px, a: Px, b: Px, tol: number): boolean {
  return distToRay(p, a, b) <= tol
}

// --- Line math in DATA space (for rays, channels, fibs) -------------------

// Price on the infinite line through anchors a and b, evaluated at `time`
// (linear inter/extrapolation). A vertical line (a.time===b.time) has no single
// price per time, so we fall back to a.price rather than dividing by zero.
export function priceOnLine(a: Pt, b: Pt, time: number): number {
  const dt = b.time - a.time
  if (dt === 0) return a.price
  return a.price + (b.price - a.price) * ((time - a.time) / dt)
}

// The vertical (price) gap between point c and the a→b line at c's time. This is
// the offset that turns the base trendline into the channel's parallel line:
// adding it to any point on a→b gives the matching point on the second rail.
export function channelOffset(a: Pt, b: Pt, c: Pt): number {
  return c.price - priceOnLine(a, b, c.time)
}

// --- Fibonacci -------------------------------------------------------------

// Standard retracement ratios plus two common extension targets. 0 sits at the
// second anchor (b), 1 at the first (a); >1 extends beyond a — matching how
// TradingView lays a retracement drawn from swing to swing.
export const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1, 1.272, 1.618]

// Resolve each ratio to a real price on the a↔b range. price = b + (a-b)*ratio,
// so 0→b.price, 1→a.price, and the extension ratios run past a. Pure and exact.
export function fibLevels(a: Pt, b: Pt): { ratio: number; price: number }[] {
  return FIB_LEVELS.map((ratio) => ({ ratio, price: b.price + (a.price - b.price) * ratio }))
}

// --- Position planner ------------------------------------------------------

export type PositionStats = {
  dir: 'long' | 'short'
  risk: number // absolute price distance entry→stop
  reward: number // absolute price distance entry→target
  riskPct: number // risk as % of entry
  rewardPct: number // reward as % of entry
  rr: number // reward-to-risk ratio (0 when there is no risk distance)
}

// Turn an entry/target/stop triplet into the numbers a trader actually reads:
// direction (long if the target is above entry, else short), absolute and % risk
// and reward, and the reward:risk ratio. All derived, never guessed; rr is 0
// when stop===entry so we never divide by zero or imply infinite reward.
export function positionStats(entry: number, target: number, stop: number): PositionStats {
  const dir: 'long' | 'short' = target >= entry ? 'long' : 'short'
  const risk = Math.abs(entry - stop)
  const reward = Math.abs(target - entry)
  const riskPct = entry !== 0 ? (risk / entry) * 100 : 0
  const rewardPct = entry !== 0 ? (reward / entry) * 100 : 0
  const rr = risk > 0 ? reward / risk : 0
  return { dir, risk, reward, riskPct, rewardPct, rr }
}

// --- Measurement ----------------------------------------------------------

export type Measure = {
  dPrice: number
  dPct: number
  bars: number
  direction: 'up' | 'down' | 'flat'
}

// Read a trend/rect's two anchors as a measurement: absolute price change, %
// change relative to the FROM price, and the whole number of bars spanned
// (from the bar duration in seconds). All derived, never guessed.
export function measure(a: Pt, b: Pt, barSeconds: number): Measure {
  const dPrice = b.price - a.price
  const dPct = a.price !== 0 ? (dPrice / a.price) * 100 : 0
  const bars = barSeconds > 0 ? Math.round(Math.abs(b.time - a.time) / barSeconds) : 0
  const direction = dPrice > 0 ? 'up' : dPrice < 0 ? 'down' : 'flat'
  return { dPrice, dPct, bars, direction }
}

// --- Persistence (per symbol + timeframe) ---------------------------------

// localStorage key for one symbol/timeframe's drawings. Kept stable so a
// symbol's lines survive reloads and don't bleed across markets/timeframes.
export function drawingsKey(symbol: string, timeframe: string): string {
  return `tt.drawings.${symbol}.${timeframe}`
}

export function serializeDrawings(drawings: Drawing[]): string {
  return JSON.stringify(drawings)
}

// Validate one untrusted value into a Drawing, or null. Rebuilds a clean object
// (drops any extra fields) so nothing odd from storage reaches the renderer.
export function validateDrawing(v: unknown): Drawing | null {
  if (!v || typeof v !== 'object') return null
  const o = v as Record<string, unknown>
  if (typeof o.id !== 'string' || !o.id) return null
  if (typeof o.color !== 'string' || !o.color || o.color.length > 32) return null
  const pt = (q: unknown): Pt | null => {
    if (!q || typeof q !== 'object') return null
    const r = q as Record<string, unknown>
    return Number.isFinite(r.time) && Number.isFinite(r.price)
      ? { time: r.time as number, price: r.price as number }
      : null
  }
  if (
    o.kind === 'trend' ||
    o.kind === 'rect' ||
    o.kind === 'ray' ||
    o.kind === 'fib' ||
    o.kind === 'measure'
  ) {
    const a = pt(o.a)
    const b = pt(o.b)
    if (a && b) return { id: o.id, kind: o.kind, a, b, color: o.color }
    return null
  }
  if (o.kind === 'channel' || o.kind === 'position') {
    const a = pt(o.a)
    const b = pt(o.b)
    const c = pt(o.c)
    if (a && b && c) return { id: o.id, kind: o.kind, a, b, c, color: o.color }
    return null
  }
  if (o.kind === 'hline') {
    if (Number.isFinite(o.price)) return { id: o.id, kind: 'hline', price: o.price as number, color: o.color }
    return null
  }
  return null
}

// Parse a raw localStorage string into a clean Drawing[]. Any corruption — bad
// JSON, not an array, junk entries — is dropped silently; you get [] or the
// valid subset, never a throw.
export function parseDrawings(raw: string | null): Drawing[] {
  if (!raw) return []
  let data: unknown
  try {
    data = JSON.parse(raw)
  } catch {
    return []
  }
  if (!Array.isArray(data)) return []
  const out: Drawing[] = []
  for (const item of data) {
    const d = validateDrawing(item)
    if (d) out.push(d)
  }
  return out
}

// Thin localStorage wrappers, guarded so they're safe under SSR/tests (no
// localStorage global) and never throw on a full/blocked quota.
export function loadDrawings(symbol: string, timeframe: string): Drawing[] {
  if (typeof localStorage === 'undefined') return []
  try {
    return parseDrawings(localStorage.getItem(drawingsKey(symbol, timeframe)))
  } catch {
    return []
  }
}

export function saveDrawings(symbol: string, timeframe: string, drawings: Drawing[]): void {
  if (typeof localStorage === 'undefined') return
  try {
    localStorage.setItem(drawingsKey(symbol, timeframe), serializeDrawings(drawings))
  } catch {}
}

// A short, unique-enough id for a new drawing. Not cryptographic — just stable
// and collision-resistant for a handful of local drawings.
export function newDrawingId(): string {
  return 'd' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8)
}




