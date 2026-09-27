// ICT / smart-money chart overlays: which layers of the REAL, computed ICT read
// (see backend app/ict.py + types.ts IctAnalysis) are drawn on the price chart.
//
// This module is pure + framework-free (unit-tested): the toggle model, its
// persistence, and small selector helpers. The actual canvas drawing lives in
// PriceChart.tsx; the AI can flip these via a view-only "chart" action; the user
// flips them in the ICT menu. Nothing here fabricates a level — it only decides
// which computed levels to SHOW.

export type IctOverlayKey =
  | 'swings' // confirmed swing highs/lows (fractals)
  | 'structure' // BOS / CHoCH / MSS break labels
  | 'sweeps' // liquidity sweeps (stop hunts)
  | 'orderBlocks' // order blocks
  | 'fvg' // fair-value gaps (+ void / inversion tags)
  | 'breakers' // breaker blocks (flipped order blocks)
  | 'rejection' // rejection blocks (long-wick swings)
  | 'bpr' // balanced price range (opposing FVG overlap)
  | 'volumeImbalance' // volume imbalances (body gaps)
  | 'liquidity' // resting liquidity pools + draw-on-liquidity
  | 'dealingRange' // premium/discount range, equilibrium, OTE band
  | 'keyLevels' // prior day/week highs & lows (PDH/PDL/PWH/PWL)

export type IctOverlayPrefs = Record<IctOverlayKey, boolean>

// Canonical order (also the menu order). Grouped for the menu below.
export const ICT_OVERLAY_KEYS: IctOverlayKey[] = [
  'swings',
  'structure',
  'sweeps',
  'orderBlocks',
  'fvg',
  'breakers',
  'rejection',
  'bpr',
  'volumeImbalance',
  'liquidity',
  'dealingRange',
  'keyLevels',
]

// Default view: the headline concepts the user named (structure, sweeps, order
// blocks, fair-value gaps, premium/discount, prior-day/week levels) ON; the
// noisier/advanced layers OFF so the first look stays clean. Every one is a real
// computed layer — a default of OFF hides it, it is never faked when ON.
export const DEFAULT_ICT_OVERLAYS: IctOverlayPrefs = {
  swings: false,
  structure: true,
  sweeps: true,
  orderBlocks: true,
  fvg: true,
  breakers: false,
  rejection: false,
  bpr: false,
  volumeImbalance: false,
  liquidity: false,
  dealingRange: true,
  keyLevels: true,
}

// Menu metadata: a short label + one-line description per overlay, arranged in
// three sections (structure / zones / levels). The description is the honest
// definition of the concept so a beginner learns what they're switching on.
export interface IctOverlayDef {
  key: IctOverlayKey
  label: string
  desc: string
}

export const ICT_OVERLAY_GROUPS: { title: string; items: IctOverlayDef[] }[] = [
  {
    title: 'Market structure',
    items: [
      { key: 'swings', label: 'Swing points', desc: 'Confirmed swing highs/lows (fractals) — the pivots structure is built from.' },
      { key: 'structure', label: 'BOS / CHoCH / MSS', desc: 'Breaks of structure (continuation) and changes of character (possible reversal).' },
      { key: 'sweeps', label: 'Liquidity sweeps', desc: 'Wicks that ran stops past a prior high/low and closed back inside (stop hunts).' },
    ],
  },
  {
    title: 'Zones',
    items: [
      { key: 'orderBlocks', label: 'Order blocks', desc: 'Last opposite candle before an impulsive break — a supply/demand origin.' },
      { key: 'fvg', label: 'Fair-value gaps', desc: 'Three-candle imbalances price often returns to fill (voids/inversions tagged).' },
      { key: 'breakers', label: 'Breaker blocks', desc: 'Order blocks price violated, so their role flipped to the other side.' },
      { key: 'rejection', label: 'Rejection blocks', desc: 'Long-wick swing candles — the wick is the zone that did the rejecting.' },
      { key: 'bpr', label: 'Balanced price range', desc: 'Where a bullish and a bearish FVG overlap — a strong reaction band.' },
      { key: 'volumeImbalance', label: 'Volume imbalances', desc: 'Body gaps between candles whose wicks still overlap.' },
    ],
  },
  {
    title: 'Liquidity & levels',
    items: [
      { key: 'liquidity', label: 'Liquidity pools + draw', desc: 'Resting buy/sell-side liquidity (EQH/EQL marked) and the nearest unswept draw.' },
      { key: 'dealingRange', label: 'Premium / discount', desc: 'The dealing range with its 50% equilibrium and the OTE (0.62–0.79) band.' },
      { key: 'keyLevels', label: 'Prior day / week', desc: 'Previous day & week highs and lows (PDH/PDL/PWH/PWL) — major liquidity draws.' },
    ],
  },
]

// --- colours (theme-agnostic; alpha keeps price readable underneath) ---------
export const ICT_COLORS = {
  bull: '#16c784',
  bear: '#ea3943',
  sweep: '#f0b90b',
  liquidity: '#a78bfa', // resting-liquidity dashed lines
  equilibrium: '#8892a6', // dealing-range 50%
  keyLevel: '#2dd4bf', // PDH/PDL/PWH/PWL
} as const

const STORAGE_KEY = 'tt.ict'

export function ictOverlayCount(p: IctOverlayPrefs): number {
  return ICT_OVERLAY_KEYS.reduce((n, k) => n + (p[k] ? 1 : 0), 0)
}

export function anyIctOverlayOn(p: IctOverlayPrefs): boolean {
  return ICT_OVERLAY_KEYS.some((k) => p[k])
}

export function allIctOverlays(on: boolean): IctOverlayPrefs {
  const out = {} as IctOverlayPrefs
  for (const k of ICT_OVERLAY_KEYS) out[k] = on
  return out
}

// Merge a (partial, possibly untrusted) patch onto known prefs, keeping only
// real keys and real booleans — used for both persistence and the AI action.
export function mergeIctOverlays(
  base: IctOverlayPrefs,
  patch: Partial<Record<string, unknown>> | null | undefined,
): IctOverlayPrefs {
  const out: IctOverlayPrefs = { ...base }
  if (patch && typeof patch === 'object') {
    for (const k of ICT_OVERLAY_KEYS) {
      const v = (patch as Record<string, unknown>)[k]
      if (typeof v === 'boolean') out[k] = v
    }
  }
  return out
}

// Parse persisted JSON into a full, valid prefs object (missing keys fall back
// to the default). Never throws.
export function parseIctOverlays(raw: string | null): IctOverlayPrefs {
  if (!raw) return { ...DEFAULT_ICT_OVERLAYS }
  try {
    const obj = JSON.parse(raw)
    return mergeIctOverlays(DEFAULT_ICT_OVERLAYS, obj)
  } catch {
    return { ...DEFAULT_ICT_OVERLAYS }
  }
}

export function loadIctOverlays(): IctOverlayPrefs {
  if (typeof localStorage === 'undefined') return { ...DEFAULT_ICT_OVERLAYS }
  return parseIctOverlays(localStorage.getItem(STORAGE_KEY))
}

export function saveIctOverlays(p: IctOverlayPrefs): void {
  if (typeof localStorage === 'undefined') return
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(p))
  } catch {
    /* storage full / unavailable — non-fatal, overlays just won't persist */
  }
}
