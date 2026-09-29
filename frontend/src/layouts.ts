// Saved chart layouts (a.k.a. templates) — TradingView-paid "multiple layouts"
// parity, honest version. A layout is a named, reusable snapshot of the chart's
// configuration: timeframe, history depth, indicator overlays and ICT overlays.
// It deliberately does NOT capture the symbol, so a layout applies to whatever
// pair you're viewing (an "indicator template"). Everything here is pure and
// storage-safe: parsing a corrupt or older value never throws and unknown keys
// are dropped, so a layout saved by an older build still applies cleanly.
import { DEFAULT_INDICATORS, type IndicatorPrefs } from './indicators'
import { DEFAULT_ICT_OVERLAYS, mergeIctOverlays, type IctOverlayPrefs } from './ictOverlays'

export const STORAGE_KEY = 'tt.layouts'
export const MAX_LAYOUTS = 24
export const MAX_NAME_LEN = 40

export type ChartLayout = {
  name: string
  timeframe: string
  chartBars: number
  indicators: IndicatorPrefs
  ict: IctOverlayPrefs
}

const INDICATOR_KEYS = Object.keys(DEFAULT_INDICATORS) as (keyof IndicatorPrefs)[]

// Merge a (partial, possibly untrusted) patch onto the indicator defaults,
// keeping only real keys and real booleans. Mirrors mergeIctOverlays.
export function mergeIndicators(
  base: IndicatorPrefs,
  patch: Partial<Record<string, unknown>> | null | undefined,
): IndicatorPrefs {
  const out: IndicatorPrefs = { ...base }
  if (patch && typeof patch === 'object') {
    for (const k of INDICATOR_KEYS) {
      const v = (patch as Record<string, unknown>)[k]
      if (typeof v === 'boolean') out[k] = v
    }
  }
  return out
}

// Trim + collapse whitespace and cap the length. Returns '' for a name that is
// empty or not a string (the caller treats '' as invalid — never saved).
export function normalizeName(name: unknown): string {
  if (typeof name !== 'string') return ''
  return name.replace(/\s+/g, ' ').trim().slice(0, MAX_NAME_LEN)
}

// Case-insensitive name match — layouts are unique by name, ignoring case.
function sameName(a: string, b: string): boolean {
  return a.toLowerCase() === b.toLowerCase()
}

// Coerce one raw entry into a valid layout, or null if it can't be salvaged (no
// usable name / timeframe / positive bar count). Indicators & ICT are merged
// onto their defaults so a missing or partial set still yields a full object.
export function sanitizeLayout(raw: unknown): ChartLayout | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  const name = normalizeName(o.name)
  if (!name) return null
  const timeframe = typeof o.timeframe === 'string' && o.timeframe.trim() ? o.timeframe.trim() : ''
  if (!timeframe) return null
  const barsNum = Number(o.chartBars)
  const chartBars = Number.isFinite(barsNum) && barsNum > 0 ? Math.floor(barsNum) : 0
  if (!chartBars) return null
  return {
    name,
    timeframe,
    chartBars,
    indicators: mergeIndicators(DEFAULT_INDICATORS, o.indicators as Partial<Record<string, unknown>>),
    ict: mergeIctOverlays(DEFAULT_ICT_OVERLAYS, o.ict as Partial<Record<string, unknown>>),
  }
}

// Parse persisted JSON into a clean list: sanitize each entry, drop unusable
// ones, dedupe by name (first wins), cap at MAX_LAYOUTS. Never throws.
export function parseLayouts(raw: string | null): ChartLayout[] {
  if (!raw) return []
  let arr: unknown
  try {
    arr = JSON.parse(raw)
  } catch {
    return []
  }
  if (!Array.isArray(arr)) return []
  const out: ChartLayout[] = []
  for (const item of arr) {
    const layout = sanitizeLayout(item)
    if (!layout) continue
    if (out.some((l) => sameName(l.name, layout.name))) continue
    out.push(layout)
    if (out.length >= MAX_LAYOUTS) break
  }
  return out
}

export function serializeLayouts(list: ChartLayout[]): string {
  return JSON.stringify(list)
}

export function findLayout(list: ChartLayout[], name: string): ChartLayout | undefined {
  const n = normalizeName(name)
  return list.find((l) => sameName(l.name, n))
}

// True when a NEW name can't be added because the list is at capacity (an
// existing name always fits — it replaces in place). Lets the caller warn
// honestly instead of silently dropping a layout.
export function isFull(list: ChartLayout[], name: string): boolean {
  return list.length >= MAX_LAYOUTS && !findLayout(list, name)
}

// Insert or replace a layout by name. Replacing an existing name keeps its slot;
// a genuinely new name appends. When the list is full and the name is new the
// SAME array is returned unchanged (see isFull) — never silently evicts another.
export function upsertLayout(list: ChartLayout[], layout: ChartLayout | null): ChartLayout[] {
  const clean = layout ? sanitizeLayout(layout) : null
  if (!clean) return list
  const idx = list.findIndex((l) => sameName(l.name, clean.name))
  if (idx >= 0) {
    const next = list.slice()
    // Overwrite the config but keep the existing entry's name casing/slot, so
    // re-saving "Scalp" as "scalp" updates the setup without renaming it.
    next[idx] = { ...clean, name: list[idx].name }
    return next
  }
  if (list.length >= MAX_LAYOUTS) return list
  return [...list, clean]
}

export function removeLayout(list: ChartLayout[], name: string): ChartLayout[] {
  const n = normalizeName(name)
  return list.filter((l) => !sameName(l.name, n))
}
