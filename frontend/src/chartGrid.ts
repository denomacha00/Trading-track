// Multi-chart grid model (TradingView Ultimate "up to 16 charts per tab" parity).
//
// A grid layout is N independent chart cells shown at once, each bound to its
// OWN real symbol + timeframe. This module is the pure, storage-safe core —
// the layout catalogue, per-cell config sanitising, and localStorage
// parse/serialize. No React, no fetch, no chart library: every function here is
// a plain data transform so it is fully unit-testable and can never throw on a
// corrupt stored value (a bad value degrades to a real fallback pair, never a
// fabricated one — see [[nothing-fake-honest-data]]).

import { normalizeSymbol } from './watchlist'

// How many charts a layout shows. Mirrors TradingView's grid presets; 16 is the
// Ultimate ceiling. Kept small + explicit so the CSS column count is known.
export type GridLayout = 2 | 4 | 6 | 9 | 16

export const GRID_LAYOUTS: { value: GridLayout; label: string; cols: number }[] = [
  { value: 2, label: '2 charts', cols: 2 },
  { value: 4, label: '4 charts', cols: 2 },
  { value: 6, label: '6 charts', cols: 3 },
  { value: 9, label: '9 charts', cols: 3 },
  { value: 16, label: '16 charts', cols: 4 },
]

export const GRID_LAYOUT_VALUES: GridLayout[] = GRID_LAYOUTS.map((l) => l.value)
export const DEFAULT_GRID_LAYOUT: GridLayout = 4

// Timeframes a grid cell may use — the same ladder as the main chart. Declared
// here (not imported from App) so this module stays self-contained + testable.
export const GRID_TIMEFRAMES = ['1m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d', '1w']

export const STORAGE_LAYOUT = 'tt.gridLayout'
export const STORAGE_CELLS = 'tt.gridCells'

export interface GridCellConfig {
  symbol: string
  timeframe: string
}

export function isGridLayout(n: unknown): n is GridLayout {
  return typeof n === 'number' && (GRID_LAYOUT_VALUES as number[]).includes(n)
}

// CSS column count for a layout (rows follow from the cell count).
export function gridColumns(layout: GridLayout): number {
  const found = GRID_LAYOUTS.find((l) => l.value === layout)
  return found ? found.cols : 2
}

// Build EXACTLY `count` real cell configs from an untrusted stored value. Each
// cell's symbol is normalised (BASE/QUOTE, uppercased) and its timeframe held to
// the known ladder; anything blank/garbage/out-of-range falls back to the pair
// the user is already viewing (a real pair, never invented). Short input is
// padded, long input trimmed — the result always matches the layout's cell count.
export function sanitizeGridCells(
  raw: unknown,
  count: number,
  fallbackSymbol: string,
  fallbackTimeframe: string,
): GridCellConfig[] {
  const fbSym = normalizeSymbol(fallbackSymbol) || 'BTC/USDT'
  const fbTf = GRID_TIMEFRAMES.includes(fallbackTimeframe) ? fallbackTimeframe : '1h'
  const arr = Array.isArray(raw) ? raw : []
  const out: GridCellConfig[] = []
  for (let i = 0; i < count; i++) {
    const e = arr[i] && typeof arr[i] === 'object' ? (arr[i] as Record<string, unknown>) : {}
    const sym = typeof e.symbol === 'string' ? normalizeSymbol(e.symbol) : null
    const tf = typeof e.timeframe === 'string' && GRID_TIMEFRAMES.includes(e.timeframe) ? e.timeframe : null
    out.push({ symbol: sym || fbSym, timeframe: tf || fbTf })
  }
  return out
}

export function parseGridLayout(raw: string | null | undefined): GridLayout {
  const n = Number(raw)
  return isGridLayout(n) ? n : DEFAULT_GRID_LAYOUT
}

// Boot-safe: corrupt/absent JSON → a fresh set of fallback cells, never a throw.
export function parseGridCells(
  raw: string | null | undefined,
  count: number,
  fallbackSymbol: string,
  fallbackTimeframe: string,
): GridCellConfig[] {
  let parsed: unknown = []
  try {
    parsed = raw ? JSON.parse(raw) : []
  } catch {
    parsed = []
  }
  return sanitizeGridCells(parsed, count, fallbackSymbol, fallbackTimeframe)
}

export function serializeGridCells(cells: GridCellConfig[]): string {
  return JSON.stringify(cells)
}

// Set one cell's symbol, returning a NEW array. A bad/blank symbol is a no-op
// that returns the SAME array reference (so the caller can keep the current pair
// and toast rather than blank a cell) — mirrors watchlist.ts's honest contract.
export function setGridCellSymbol(cells: GridCellConfig[], index: number, raw: string): GridCellConfig[] {
  if (index < 0 || index >= cells.length) return cells
  const norm = normalizeSymbol(raw)
  if (!norm) return cells
  const next = cells.slice()
  next[index] = { ...next[index], symbol: norm }
  return next
}

// Set one cell's timeframe (validated against the ladder). Bad value → same ref.
export function setGridCellTimeframe(cells: GridCellConfig[], index: number, tf: string): GridCellConfig[] {
  if (index < 0 || index >= cells.length) return cells
  if (!GRID_TIMEFRAMES.includes(tf)) return cells
  const next = cells.slice()
  next[index] = { ...next[index], timeframe: tf }
  return next
}

// Grow/shrink the cell array to a new layout's count, keeping existing choices
// and padding new cells with the fallback pair/timeframe.
export function resizeGridCells(
  cells: GridCellConfig[],
  count: number,
  fallbackSymbol: string,
  fallbackTimeframe: string,
): GridCellConfig[] {
  return sanitizeGridCells(cells, count, fallbackSymbol, fallbackTimeframe)
}
