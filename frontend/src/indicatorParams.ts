// Per-indicator PARAMETERS (TradingView "settings"/inputs parity). Every function
// in indicators.ts already takes its look-backs as arguments; this module is just
// the user-tunable values threaded into those calls, defaulting to the EXACT
// numbers the chart shipped with (so an untouched chart stays pixel-identical).
// Every value is validated on the way in — coerced to a finite number, integer
// periods rounded, all clamped to sane bounds — so a corrupt localStorage or a
// fat-fingered input can never push a garbage period into the math (a negative or
// NaN length would otherwise silently blank an indicator). Nothing here computes
// an indicator or invents data; it only carries the lengths/multipliers the real
// math uses, and the labels that reflect them.
import type { IndicatorPrefs } from './indicators'

export type IndicatorParams = {
  emaFast: number // MA slot "ema9" length
  emaSlow: number // MA slot "ema21" length
  smaFast: number // MA slot "sma50" length
  smaSlow: number // MA slot "sma200" length
  hma: number // Hull MA length
  bbPeriod: number // Bollinger look-back
  bbMult: number // Bollinger stddev multiple
  kcEma: number // Keltner EMA basis length
  kcAtr: number // Keltner ATR length
  kcMult: number // Keltner ATR multiple
  donchian: number // Donchian look-back
  rsiPeriod: number // RSI look-back
  macdFast: number // MACD fast EMA
  macdSlow: number // MACD slow EMA
  macdSignal: number // MACD signal EMA
  stochK: number // Stochastic %K look-back
  stochD: number // Stochastic %D smoothing
  stochSmooth: number // Stochastic %K slowing
  atrPeriod: number // ATR look-back
}

export const DEFAULT_INDICATOR_PARAMS: IndicatorParams = {
  emaFast: 9,
  emaSlow: 21,
  smaFast: 50,
  smaSlow: 200,
  hma: 55,
  bbPeriod: 20,
  bbMult: 2,
  kcEma: 20,
  kcAtr: 10,
  kcMult: 2,
  donchian: 20,
  rsiPeriod: 14,
  macdFast: 12,
  macdSlow: 26,
  macdSignal: 9,
  stochK: 14,
  stochD: 3,
  stochSmooth: 3,
  atrPeriod: 14,
}

// One tunable input: which param it sets, a short UI label, its valid range and
// whether it's an integer (a look-back) or may be fractional (a multiplier).
export type ParamField = {
  key: keyof IndicatorParams
  label: string
  min: number
  max: number
  step: number
  int: boolean
}

// Shared range presets: look-backs are whole bars 1..1000; multiples are 0.1..10.
const LEN = { min: 1, max: 1000, step: 1, int: true } as const
const MULT = { min: 0.1, max: 10, step: 0.1, int: false } as const

// Fields grouped BY the indicator toggle they belong to (keys match IndicatorPrefs),
// so the Indicators menu renders a param row under each indicator, and the overlay
// / oscillator code and the validator share one source of truth. Indicators with
// no tunables (vwap, obv, volume, volumeProfile) simply don't appear here.
export const PARAM_GROUPS: { ind: keyof IndicatorPrefs; fields: ParamField[] }[] = [
  { ind: 'ema9', fields: [{ key: 'emaFast', label: 'Length', ...LEN }] },
  { ind: 'ema21', fields: [{ key: 'emaSlow', label: 'Length', ...LEN }] },
  { ind: 'sma50', fields: [{ key: 'smaFast', label: 'Length', ...LEN }] },
  { ind: 'sma200', fields: [{ key: 'smaSlow', label: 'Length', ...LEN }] },
  { ind: 'hma', fields: [{ key: 'hma', label: 'Length', ...LEN }] },
  {
    ind: 'bb',
    fields: [
      { key: 'bbPeriod', label: 'Length', ...LEN },
      { key: 'bbMult', label: 'StdDev', ...MULT },
    ],
  },
  {
    ind: 'keltner',
    fields: [
      { key: 'kcEma', label: 'EMA', ...LEN },
      { key: 'kcAtr', label: 'ATR', ...LEN },
      { key: 'kcMult', label: 'Mult', ...MULT },
    ],
  },
  { ind: 'donchian', fields: [{ key: 'donchian', label: 'Length', ...LEN }] },
  { ind: 'rsi', fields: [{ key: 'rsiPeriod', label: 'Length', ...LEN }] },
  {
    ind: 'macd',
    fields: [
      { key: 'macdFast', label: 'Fast', ...LEN },
      { key: 'macdSlow', label: 'Slow', ...LEN },
      { key: 'macdSignal', label: 'Signal', ...LEN },
    ],
  },
  {
    ind: 'stoch',
    fields: [
      { key: 'stochK', label: '%K', ...LEN },
      { key: 'stochD', label: '%D', ...LEN },
      { key: 'stochSmooth', label: 'Smooth', ...LEN },
    ],
  },
  { ind: 'atr', fields: [{ key: 'atrPeriod', label: 'Length', ...LEN }] },
]

// Fast key→spec lookup, built from the groups above (every param appears once).
const SPEC_BY_KEY = (() => {
  const m = {} as Record<keyof IndicatorParams, ParamField>
  for (const g of PARAM_GROUPS) for (const f of g.fields) m[f.key] = f
  return m
})()

// Clamp ONE field to its spec: null / blank / non-finite → the default (so
// clearing an input box restores the shipped period, never a silent 0); integer
// fields rounded; everything held within [min, max]. Used by the validator and
// the UI inputs alike.
export function clampField(key: keyof IndicatorParams, value: unknown): number {
  if (value === null || value === undefined) return DEFAULT_INDICATOR_PARAMS[key]
  if (typeof value === 'string' && value.trim() === '') return DEFAULT_INDICATOR_PARAMS[key]
  const spec = SPEC_BY_KEY[key]
  let n = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(n)) return DEFAULT_INDICATOR_PARAMS[key]
  if (spec.int) n = Math.round(n)
  return Math.min(spec.max, Math.max(spec.min, n))
}

// Merge an untrusted partial (parsed JSON, a patch) onto the defaults, validating
// every provided field. Unknown keys are ignored; missing ones keep their default.
export function sanitizeParams(raw: unknown): IndicatorParams {
  const out: IndicatorParams = { ...DEFAULT_INDICATOR_PARAMS }
  if (raw && typeof raw === 'object') {
    for (const key of Object.keys(DEFAULT_INDICATOR_PARAMS) as (keyof IndicatorParams)[]) {
      const v = (raw as Record<string, unknown>)[key]
      if (v !== undefined && v !== null) out[key] = clampField(key, v)
    }
  }
  return out
}

// Boot-safe parse of the persisted JSON string (never throws): corrupt or absent
// storage yields a fresh copy of the defaults.
export function parseParams(rawJson: string | null): IndicatorParams {
  if (!rawJson) return { ...DEFAULT_INDICATOR_PARAMS }
  try {
    return sanitizeParams(JSON.parse(rawJson))
  } catch {
    return { ...DEFAULT_INDICATOR_PARAMS }
  }
}

// The menu label for a parameterised indicator, reflecting the LIVE params (so
// changing EMA length to 30 relabels it "EMA 30"). Returns null for indicators
// with no tunables, so the caller falls back to its own static label.
export function indicatorLabel(ind: keyof IndicatorPrefs, p: IndicatorParams): string | null {
  switch (ind) {
    case 'ema9':
      return `EMA ${p.emaFast}`
    case 'ema21':
      return `EMA ${p.emaSlow}`
    case 'sma50':
      return `SMA ${p.smaFast}`
    case 'sma200':
      return `SMA ${p.smaSlow}`
    case 'hma':
      return `Hull MA (${p.hma})`
    case 'bb':
      return `Bollinger Bands (${p.bbPeriod}, ${p.bbMult})`
    case 'keltner':
      return `Keltner Channels (${p.kcEma}, ${p.kcAtr}, ${p.kcMult})`
    case 'donchian':
      return `Donchian Channels (${p.donchian})`
    case 'rsi':
      return `RSI (${p.rsiPeriod})`
    case 'macd':
      return `MACD (${p.macdFast}, ${p.macdSlow}, ${p.macdSignal})`
    case 'stoch':
      return `Stochastic (${p.stochK}, ${p.stochD}, ${p.stochSmooth})`
    case 'atr':
      return `ATR (${p.atrPeriod})`
    default:
      return null
  }
}
