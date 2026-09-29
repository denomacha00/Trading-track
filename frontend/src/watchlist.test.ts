import { describe, it, expect } from 'vitest'
import {
  MAX_WATCHLIST,
  normalizeSymbol,
  inList,
  addSymbol,
  removeSymbol,
  toggleSymbol,
  moveSymbol,
  parseStored,
  serialize,
} from './watchlist'

describe('normalizeSymbol', () => {
  it('uppercases and trims a valid pair', () => {
    expect(normalizeSymbol('  btc/usdt ')).toBe('BTC/USDT')
    expect(normalizeSymbol('ETH/USDT')).toBe('ETH/USDT')
  })

  it('accepts alphanumeric bases (e.g. 1INCH)', () => {
    expect(normalizeSymbol('1inch/usdt')).toBe('1INCH/USDT')
  })

  it('rejects a missing or empty side', () => {
    expect(normalizeSymbol('BTC')).toBeNull()
    expect(normalizeSymbol('BTC/')).toBeNull()
    expect(normalizeSymbol('/USDT')).toBeNull()
    expect(normalizeSymbol('')).toBeNull()
    expect(normalizeSymbol('   ')).toBeNull()
  })

  it('rejects more than one slash', () => {
    expect(normalizeSymbol('BTC/USDT/USD')).toBeNull()
  })

  it('rejects a derivatives contract (colon suffix)', () => {
    expect(normalizeSymbol('BTC/USDT:USDT')).toBeNull()
  })

  it('rejects symbols with punctuation/spaces in either leg', () => {
    expect(normalizeSymbol('BT C/USDT')).toBeNull()
    expect(normalizeSymbol('BTC/US-DT')).toBeNull()
  })
})

describe('addSymbol / inList', () => {
  it('appends a new normalized symbol', () => {
    expect(addSymbol([], 'btc/usdt')).toEqual(['BTC/USDT'])
    expect(addSymbol(['BTC/USDT'], 'eth/usdt')).toEqual(['BTC/USDT', 'ETH/USDT'])
  })

  it('is a no-op for a duplicate (case-insensitive)', () => {
    const list = ['BTC/USDT']
    expect(addSymbol(list, 'BTC/USDT')).toBe(list)
    expect(addSymbol(list, 'btc/usdt')).toBe(list)
  })

  it('is a no-op for an invalid symbol', () => {
    const list = ['BTC/USDT']
    expect(addSymbol(list, 'garbage')).toBe(list)
    expect(addSymbol(list, 'BTC/USDT:USDT')).toBe(list)
  })

  it('refuses to grow past the cap', () => {
    const full = Array.from({ length: MAX_WATCHLIST }, (_, i) => `C${i}/USDT`)
    expect(addSymbol(full, 'BTC/USDT')).toBe(full)
    expect(full.length).toBe(MAX_WATCHLIST)
  })

  it('inList matches regardless of case', () => {
    expect(inList(['BTC/USDT'], 'btc/usdt')).toBe(true)
    expect(inList(['BTC/USDT'], 'eth/usdt')).toBe(false)
    expect(inList(['BTC/USDT'], 'nonsense')).toBe(false)
  })
})

describe('removeSymbol', () => {
  it('removes a present symbol (case-insensitive)', () => {
    expect(removeSymbol(['BTC/USDT', 'ETH/USDT'], 'btc/usdt')).toEqual(['ETH/USDT'])
  })

  it('is a no-op when absent or invalid', () => {
    const list = ['BTC/USDT']
    expect(removeSymbol(list, 'ETH/USDT')).toBe(list)
    expect(removeSymbol(list, 'junk')).toBe(list)
  })
})

describe('toggleSymbol', () => {
  it('adds when absent and removes when present', () => {
    const a = toggleSymbol([], 'btc/usdt')
    expect(a).toEqual(['BTC/USDT'])
    const b = toggleSymbol(a, 'BTC/USDT')
    expect(b).toEqual([])
  })

  it('leaves invalid symbols untouched', () => {
    const list = ['BTC/USDT']
    expect(toggleSymbol(list, 'nope')).toBe(list)
  })
})

describe('moveSymbol', () => {
  const list = ['A/USDT', 'B/USDT', 'C/USDT']

  it('moves up and down', () => {
    expect(moveSymbol(list, 'B/USDT', -1)).toEqual(['B/USDT', 'A/USDT', 'C/USDT'])
    expect(moveSymbol(list, 'B/USDT', 1)).toEqual(['A/USDT', 'C/USDT', 'B/USDT'])
  })

  it('clamps at the ends (no wrap)', () => {
    expect(moveSymbol(list, 'A/USDT', -1)).toBe(list)
    expect(moveSymbol(list, 'C/USDT', 1)).toBe(list)
  })

  it('is a no-op for an absent or invalid symbol', () => {
    expect(moveSymbol(list, 'Z/USDT', -1)).toBe(list)
    expect(moveSymbol(list, 'junk', 1)).toBe(list)
  })
})

describe('parseStored / serialize', () => {
  it('round-trips a clean list', () => {
    const list = ['BTC/USDT', 'ETH/USDT']
    expect(parseStored(serialize(list))).toEqual(list)
  })

  it('returns [] for null, garbage, or a non-array', () => {
    expect(parseStored(null)).toEqual([])
    expect(parseStored(undefined)).toEqual([])
    expect(parseStored('not json')).toEqual([])
    expect(parseStored('{"a":1}')).toEqual([])
    expect(parseStored('42')).toEqual([])
  })

  it('normalizes, drops invalid entries, and dedupes in order', () => {
    const raw = JSON.stringify(['btc/usdt', 'JUNK', 'BTC/USDT', 12, 'eth/usdt', 'X/Y:Z'])
    expect(parseStored(raw)).toEqual(['BTC/USDT', 'ETH/USDT'])
  })

  it('caps an over-long stored list', () => {
    const many = Array.from({ length: MAX_WATCHLIST + 10 }, (_, i) => `C${i}/USDT`)
    const parsed = parseStored(JSON.stringify(many))
    expect(parsed.length).toBe(MAX_WATCHLIST)
    expect(parsed[0]).toBe('C0/USDT')
  })
})
