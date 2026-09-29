import { describe, it, expect } from 'vitest'
import {
  DEFAULT_INDICATOR_PARAMS,
  PARAM_GROUPS,
  PARAM_LABELS,
  clampField,
  sanitizeParams,
  sanitizeParamsPatch,
  parseParams,
  indicatorLabel,
  type IndicatorParams,
} from './indicatorParams'

describe('DEFAULT_INDICATOR_PARAMS', () => {
  it('matches the periods the chart shipped with (untouched chart unchanged)', () => {
    expect(DEFAULT_INDICATOR_PARAMS).toEqual({
      emaFast: 9, emaSlow: 21, smaFast: 50, smaSlow: 200, hma: 55,
      bbPeriod: 20, bbMult: 2,
      kcEma: 20, kcAtr: 10, kcMult: 2,
      donchian: 20,
      rsiPeriod: 14,
      macdFast: 12, macdSlow: 26, macdSignal: 9,
      stochK: 14, stochD: 3, stochSmooth: 3,
      atrPeriod: 14,
    })
  })

  it('has a field spec for every param (nothing tunable is forgotten)', () => {
    const specced = new Set(PARAM_GROUPS.flatMap((g) => g.fields.map((f) => f.key)))
    for (const key of Object.keys(DEFAULT_INDICATOR_PARAMS)) {
      expect(specced.has(key as keyof IndicatorParams)).toBe(true)
    }
    expect(specced.size).toBe(Object.keys(DEFAULT_INDICATOR_PARAMS).length)
  })
})

describe('clampField', () => {
  it('holds look-backs in [1, 1000] and rounds to whole bars', () => {
    expect(clampField('rsiPeriod', 0)).toBe(1)
    expect(clampField('rsiPeriod', -5)).toBe(1)
    expect(clampField('rsiPeriod', 5000)).toBe(1000)
    expect(clampField('rsiPeriod', 14.7)).toBe(15)
  })

  it('keeps multipliers fractional in [0.1, 10]', () => {
    expect(clampField('bbMult', 2.5)).toBe(2.5)
    expect(clampField('bbMult', 0)).toBe(0.1)
    expect(clampField('bbMult', 99)).toBe(10)
  })

  it('falls back to the default for non-finite / non-numeric input', () => {
    expect(clampField('rsiPeriod', NaN)).toBe(14)
    expect(clampField('rsiPeriod', Infinity)).toBe(14)
    expect(clampField('rsiPeriod', 'abc')).toBe(14)
    expect(clampField('bbMult', null)).toBe(2)
  })

  it('treats a cleared / blank box as the default (never a silent 0)', () => {
    expect(clampField('rsiPeriod', '')).toBe(14)
    expect(clampField('rsiPeriod', '   ')).toBe(14)
    expect(clampField('emaFast', undefined)).toBe(9)
  })
})
describe('sanitizeParams', () => {
  it('returns a fresh copy of the defaults for empty / non-object input', () => {
    expect(sanitizeParams({})).toEqual(DEFAULT_INDICATOR_PARAMS)
    expect(sanitizeParams(null)).toEqual(DEFAULT_INDICATOR_PARAMS)
    expect(sanitizeParams(42)).toEqual(DEFAULT_INDICATOR_PARAMS)
    expect(sanitizeParams('nope')).toEqual(DEFAULT_INDICATOR_PARAMS)
  })

  it('merges a partial patch, validating only the provided fields', () => {
    const out = sanitizeParams({ rsiPeriod: 21, bbMult: 3 })
    expect(out.rsiPeriod).toBe(21)
    expect(out.bbMult).toBe(3)
    expect(out.emaFast).toBe(9) // untouched keeps its default
  })

  it('clamps garbage values inside a patch instead of trusting them', () => {
    const out = sanitizeParams({ rsiPeriod: -1, macdFast: 9999, stochK: 'x' })
    expect(out.rsiPeriod).toBe(1)
    expect(out.macdFast).toBe(1000)
    expect(out.stochK).toBe(14) // non-numeric → default
  })

  it('ignores unknown keys and does not mutate the defaults', () => {
    const out = sanitizeParams({ bogus: 1, rsiPeriod: 30 })
    expect('bogus' in out).toBe(false)
    expect(DEFAULT_INDICATOR_PARAMS.rsiPeriod).toBe(14) // defaults intact
  })
})

describe('sanitizeParamsPatch', () => {
  it('keeps ONLY the provided finite fields, each clamped (never fills defaults)', () => {
    const out = sanitizeParamsPatch({ rsiPeriod: 21, bbMult: 2.5 })
    expect(out).toEqual({ rsiPeriod: 21, bbMult: 2.5 })
    // A field the patch did not mention is absent — NOT set to its default.
    expect('emaFast' in out).toBe(false)
  })

  it('clamps an out-of-range value instead of dropping it', () => {
    expect(sanitizeParamsPatch({ rsiPeriod: 0 })).toEqual({ rsiPeriod: 1 })
    expect(sanitizeParamsPatch({ rsiPeriod: 9999 })).toEqual({ rsiPeriod: 1000 })
    expect(sanitizeParamsPatch({ bbMult: 99 })).toEqual({ bbMult: 10 })
    expect(sanitizeParamsPatch({ rsiPeriod: 14.7 })).toEqual({ rsiPeriod: 15 })
  })

  it('DROPS null/blank/non-finite/boolean/unknown fields (never a silent default)', () => {
    const out = sanitizeParamsPatch({
      rsiPeriod: null,
      emaFast: '',
      macdFast: NaN,
      stochK: Infinity,
      atrPeriod: 'abc',
      bbPeriod: true,
      bogus: 5,
      emaSlow: 30, // the one real change survives
    })
    expect(out).toEqual({ emaSlow: 30 })
  })

  it('accepts a numeric string and returns {} for a non-object', () => {
    expect(sanitizeParamsPatch({ donchian: '25' })).toEqual({ donchian: 25 })
    expect(sanitizeParamsPatch(null)).toEqual({})
    expect(sanitizeParamsPatch('nope')).toEqual({})
    expect(sanitizeParamsPatch(42)).toEqual({})
  })
})

describe('PARAM_LABELS', () => {
  it('has a friendly label for EVERY param field (exhaustive, non-empty)', () => {
    for (const key of Object.keys(DEFAULT_INDICATOR_PARAMS) as (keyof IndicatorParams)[]) {
      expect(typeof PARAM_LABELS[key]).toBe('string')
      expect(PARAM_LABELS[key].length).toBeGreaterThan(0)
    }
    expect(Object.keys(PARAM_LABELS).sort()).toEqual(Object.keys(DEFAULT_INDICATOR_PARAMS).sort())
  })
})

describe('parseParams', () => {
  it('defaults on null / corrupt JSON (boot-safe)', () => {
    expect(parseParams(null)).toEqual(DEFAULT_INDICATOR_PARAMS)
    expect(parseParams('')).toEqual(DEFAULT_INDICATOR_PARAMS)
    expect(parseParams('{ not json')).toEqual(DEFAULT_INDICATOR_PARAMS)
  })

  it('round-trips a sanitized object through JSON', () => {
    const custom = sanitizeParams({ rsiPeriod: 21, emaFast: 12, bbMult: 2.5 })
    expect(parseParams(JSON.stringify(custom))).toEqual(custom)
  })

  it('re-validates persisted garbage on the way back in', () => {
    expect(parseParams(JSON.stringify({ rsiPeriod: -8 })).rsiPeriod).toBe(1)
  })
})

describe('indicatorLabel', () => {
  it('reflects the live params for parameterised indicators', () => {
    const p = DEFAULT_INDICATOR_PARAMS
    expect(indicatorLabel('ema9', p)).toBe('EMA 9')
    expect(indicatorLabel('sma200', p)).toBe('SMA 200')
    expect(indicatorLabel('bb', p)).toBe('Bollinger Bands (20, 2)')
    expect(indicatorLabel('macd', p)).toBe('MACD (12, 26, 9)')
    expect(indicatorLabel('stoch', p)).toBe('Stochastic (14, 3, 3)')
    expect(indicatorLabel('ema9', { ...p, emaFast: 30 })).toBe('EMA 30')
  })

  it('returns null for indicators with no tunables (caller keeps its own label)', () => {
    const p = DEFAULT_INDICATOR_PARAMS
    expect(indicatorLabel('vwap', p)).toBeNull()
    expect(indicatorLabel('obv', p)).toBeNull()
    expect(indicatorLabel('volume', p)).toBeNull()
    expect(indicatorLabel('volumeProfile', p)).toBeNull()
  })
})
