import { describe, it, expect } from 'vitest'
import {
  mergeIndicators,
  normalizeName,
  sanitizeLayout,
  parseLayouts,
  serializeLayouts,
  findLayout,
  isFull,
  upsertLayout,
  removeLayout,
  MAX_LAYOUTS,
  MAX_NAME_LEN,
  type ChartLayout,
} from './layouts'
import { DEFAULT_INDICATORS } from './indicators'
import { DEFAULT_ICT_OVERLAYS } from './ictOverlays'
import { DEFAULT_INDICATOR_PARAMS } from './indicatorParams'

// A valid layout to build variations from.
function layout(name: string, over: Partial<ChartLayout> = {}): ChartLayout {
  return {
    name,
    timeframe: '1h',
    chartBars: 500,
    indicators: { ...DEFAULT_INDICATORS },
    ict: { ...DEFAULT_ICT_OVERLAYS },
    params: { ...DEFAULT_INDICATOR_PARAMS },
    ...over,
  }
}

describe('mergeIndicators', () => {
  it('overlays only real boolean keys onto the defaults', () => {
    const out = mergeIndicators(DEFAULT_INDICATORS, { rsi: true, macd: true })
    expect(out.rsi).toBe(true)
    expect(out.macd).toBe(true)
    expect(out.volume).toBe(true) // untouched default
    expect(out.ema9).toBe(false)
  })
  it('ignores unknown keys and non-boolean values', () => {
    const out = mergeIndicators(DEFAULT_INDICATORS, { bogus: true, rsi: 'yes', atr: 1 } as never)
    expect((out as Record<string, unknown>).bogus).toBeUndefined()
    expect(out.rsi).toBe(false) // 'yes' is not a boolean → default kept
    expect(out.atr).toBe(false)
  })
  it('returns a full prefs object for a null patch', () => {
    expect(mergeIndicators(DEFAULT_INDICATORS, null)).toEqual(DEFAULT_INDICATORS)
  })
})

describe('normalizeName', () => {
  it('trims and collapses inner whitespace', () => {
    expect(normalizeName('  My   Scalp  Setup ')).toBe('My Scalp Setup')
  })
  it('caps the length', () => {
    expect(normalizeName('x'.repeat(100)).length).toBe(MAX_NAME_LEN)
  })
  it('returns empty string for non-strings / blank', () => {
    expect(normalizeName(null)).toBe('')
    expect(normalizeName(42)).toBe('')
    expect(normalizeName('   ')).toBe('')
  })
})

describe('sanitizeLayout', () => {
  it('accepts a valid entry and fills prefs to full objects', () => {
    const out = sanitizeLayout({ name: 'A', timeframe: '4h', chartBars: 1000, indicators: { rsi: true }, ict: { swings: true } })
    expect(out).not.toBeNull()
    expect(out!.timeframe).toBe('4h')
    expect(out!.chartBars).toBe(1000)
    expect(out!.indicators.rsi).toBe(true)
    expect(out!.indicators.volume).toBe(true) // merged onto defaults
    expect(out!.ict.swings).toBe(true)
    // full key set present after merge
    expect(Object.keys(out!.indicators).sort()).toEqual(Object.keys(DEFAULT_INDICATORS).sort())
  })
  it('rejects entries with no usable name, timeframe, or positive bar count', () => {
    expect(sanitizeLayout({ timeframe: '1h', chartBars: 200 })).toBeNull()
    expect(sanitizeLayout({ name: 'x', chartBars: 200 })).toBeNull()
    expect(sanitizeLayout({ name: 'x', timeframe: '1h', chartBars: 0 })).toBeNull()
    expect(sanitizeLayout({ name: 'x', timeframe: '1h', chartBars: -5 })).toBeNull()
    expect(sanitizeLayout(null)).toBeNull()
    expect(sanitizeLayout('nope')).toBeNull()
  })
  it('floors a fractional bar count', () => {
    expect(sanitizeLayout({ name: 'x', timeframe: '1h', chartBars: 500.9 })!.chartBars).toBe(500)
  })
  it('captures indicator params, clamping bad values', () => {
    const out = sanitizeLayout({
      name: 'P',
      timeframe: '1h',
      chartBars: 200,
      params: { rsiPeriod: 21, emaFast: 0, bbMult: 'nope' },
    })
    expect(out!.params.rsiPeriod).toBe(21) // real value kept
    expect(out!.params.emaFast).toBe(1) // 0 → clamped up to the min look-back
    expect(out!.params.bbMult).toBe(DEFAULT_INDICATOR_PARAMS.bbMult) // garbage → default
    // full param key set present after merge
    expect(Object.keys(out!.params).sort()).toEqual(Object.keys(DEFAULT_INDICATOR_PARAMS).sort())
  })
  it('fills params to shipped defaults when an older layout omits them', () => {
    const out = sanitizeLayout({ name: 'Old', timeframe: '1h', chartBars: 200 })
    expect(out!.params).toEqual(DEFAULT_INDICATOR_PARAMS)
  })
})

describe('parseLayouts', () => {
  it('returns [] for null, blank, bad JSON, or a non-array', () => {
    expect(parseLayouts(null)).toEqual([])
    expect(parseLayouts('')).toEqual([])
    expect(parseLayouts('not json{')).toEqual([])
    expect(parseLayouts('{"a":1}')).toEqual([])
  })
  it('drops unusable entries and dedupes by name (case-insensitive, first wins)', () => {
    const raw = serializeLayouts([layout('Scalp'), layout('scalp', { timeframe: '5m' }), { name: '' } as never as ChartLayout])
    const out = parseLayouts(raw)
    expect(out.map((l) => l.name)).toEqual(['Scalp'])
    expect(out[0].timeframe).toBe('1h') // first wins, the '5m' dup is dropped
  })
  it('caps at MAX_LAYOUTS', () => {
    const many = Array.from({ length: MAX_LAYOUTS + 5 }, (_, i) => layout('L' + i))
    expect(parseLayouts(serializeLayouts(many)).length).toBe(MAX_LAYOUTS)
  })
  it('round-trips a clean list', () => {
    const list = [layout('A', { indicators: { ...DEFAULT_INDICATORS, rsi: true } }), layout('B', { timeframe: '1d' })]
    expect(parseLayouts(serializeLayouts(list))).toEqual(list)
  })
  it('round-trips non-default indicator params', () => {
    const tuned = layout('Tuned', {
      params: { ...DEFAULT_INDICATOR_PARAMS, rsiPeriod: 21, emaFast: 34, bbMult: 2.5 },
    })
    const out = parseLayouts(serializeLayouts([tuned]))
    expect(out[0].params.rsiPeriod).toBe(21)
    expect(out[0].params.emaFast).toBe(34)
    expect(out[0].params.bbMult).toBe(2.5)
  })
})

describe('upsertLayout / isFull / removeLayout / findLayout', () => {
  it('appends a new name and replaces an existing one in place (case-insensitive)', () => {
    let list = upsertLayout([], layout('A'))
    list = upsertLayout(list, layout('B'))
    expect(list.map((l) => l.name)).toEqual(['A', 'B'])
    const replaced = upsertLayout(list, layout('a', { timeframe: '15m' }))
    expect(replaced.map((l) => l.name)).toEqual(['A', 'B']) // slot kept, no dup
    expect(findLayout(replaced, 'A')!.timeframe).toBe('15m')
  })
  it('does not add a new name when full, but still replaces an existing one', () => {
    const full = Array.from({ length: MAX_LAYOUTS }, (_, i) => layout('L' + i))
    expect(isFull(full, 'new-one')).toBe(true)
    expect(isFull(full, 'L0')).toBe(false)
    expect(upsertLayout(full, layout('new-one'))).toBe(full) // unchanged reference
    const replaced = upsertLayout(full, layout('L0', { timeframe: '1w' }))
    expect(replaced.length).toBe(MAX_LAYOUTS)
    expect(findLayout(replaced, 'L0')!.timeframe).toBe('1w')
  })
  it('ignores an invalid layout', () => {
    const list = [layout('A')]
    expect(upsertLayout(list, { name: '', timeframe: '1h', chartBars: 1 } as never)).toBe(list)
    expect(upsertLayout(list, null)).toBe(list)
  })
  it('removes by name case-insensitively', () => {
    const list = [layout('A'), layout('B')]
    expect(removeLayout(list, 'a').map((l) => l.name)).toEqual(['B'])
    expect(removeLayout(list, 'missing')).toEqual(list)
  })
})
