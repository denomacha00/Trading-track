import { describe, it, expect } from 'vitest'
import {
  GRID_LAYOUTS,
  GRID_LAYOUT_VALUES,
  DEFAULT_GRID_LAYOUT,
  GRID_TIMEFRAMES,
  isGridLayout,
  gridColumns,
  sanitizeGridCells,
  parseGridLayout,
  parseGridCells,
  serializeGridCells,
  setGridCellSymbol,
  setGridCellTimeframe,
  resizeGridCells,
} from './chartGrid'

describe('grid layout catalogue', () => {
  it('every layout value is a positive cell count with a real column count', () => {
    for (const l of GRID_LAYOUTS) {
      expect(l.value).toBeGreaterThan(0)
      expect(l.cols).toBeGreaterThan(0)
      expect(l.cols).toBeLessThanOrEqual(l.value)
    }
    expect(GRID_LAYOUT_VALUES).toEqual([2, 4, 6, 9, 16])
    expect(GRID_LAYOUT_VALUES).toContain(DEFAULT_GRID_LAYOUT)
  })

  it('isGridLayout accepts only known counts', () => {
    expect(isGridLayout(4)).toBe(true)
    expect(isGridLayout(16)).toBe(true)
    expect(isGridLayout(3)).toBe(false)
    expect(isGridLayout('4')).toBe(false)
    expect(isGridLayout(null)).toBe(false)
  })

  it('gridColumns returns the layout column count (fallback 2)', () => {
    expect(gridColumns(2)).toBe(2)
    expect(gridColumns(6)).toBe(3)
    expect(gridColumns(16)).toBe(4)
    // @ts-expect-error unknown layout falls back
    expect(gridColumns(7)).toBe(2)
  })
})

describe('sanitizeGridCells', () => {
  it('returns EXACTLY count cells, padding short input with the fallback', () => {
    const cells = sanitizeGridCells([{ symbol: 'ETH/USDT', timeframe: '4h' }], 4, 'BTC/USDT', '1h')
    expect(cells).toHaveLength(4)
    expect(cells[0]).toEqual({ symbol: 'ETH/USDT', timeframe: '4h' })
    expect(cells[1]).toEqual({ symbol: 'BTC/USDT', timeframe: '1h' })
    expect(cells[3]).toEqual({ symbol: 'BTC/USDT', timeframe: '1h' })
  })

  it('trims long input to count', () => {
    const many = Array.from({ length: 20 }, () => ({ symbol: 'sol/usdt', timeframe: '1d' }))
    const cells = sanitizeGridCells(many, 2, 'BTC/USDT', '1h')
    expect(cells).toHaveLength(2)
    expect(cells[0]).toEqual({ symbol: 'SOL/USDT', timeframe: '1d' })
  })

  it('normalises symbols and rejects bad ones to the fallback (never fabricates)', () => {
    const cells = sanitizeGridCells(
      [
        { symbol: 'btc/usdt', timeframe: '1h' }, // lowercased -> uppercased
        { symbol: 'BTC/USDT:USDT', timeframe: '1h' }, // derivatives -> fallback
        { symbol: 'garbage', timeframe: '1h' }, // no slash -> fallback
        { symbol: 'ETH/USDT', timeframe: '3s' }, // bad tf -> fallback tf
      ],
      4,
      'BNB/USDT',
      '15m',
    )
    expect(cells[0].symbol).toBe('BTC/USDT')
    expect(cells[1].symbol).toBe('BNB/USDT')
    expect(cells[2].symbol).toBe('BNB/USDT')
    expect(cells[3]).toEqual({ symbol: 'ETH/USDT', timeframe: '15m' })
  })

  it('a garbage fallback symbol degrades to BTC/USDT, never null/undefined', () => {
    const cells = sanitizeGridCells([], 1, 'not-a-pair', 'nope')
    expect(cells[0].symbol).toBe('BTC/USDT')
    expect(GRID_TIMEFRAMES).toContain(cells[0].timeframe)
    expect(cells[0].timeframe).toBe('1h')
  })

  it('non-array / non-object entries never throw', () => {
    expect(sanitizeGridCells(null, 2, 'BTC/USDT', '1h')).toHaveLength(2)
    expect(sanitizeGridCells('oops', 2, 'BTC/USDT', '1h')).toHaveLength(2)
    expect(sanitizeGridCells([42, 'x', null], 3, 'BTC/USDT', '1h').every((c) => c.symbol === 'BTC/USDT')).toBe(true)
  })
})

describe('parse / serialize', () => {
  it('parseGridLayout maps stored strings to a known layout (fallback default)', () => {
    expect(parseGridLayout('16')).toBe(16)
    expect(parseGridLayout('4')).toBe(4)
    expect(parseGridLayout('3')).toBe(DEFAULT_GRID_LAYOUT)
    expect(parseGridLayout(null)).toBe(DEFAULT_GRID_LAYOUT)
    expect(parseGridLayout('nonsense')).toBe(DEFAULT_GRID_LAYOUT)
  })

  it('parseGridCells is boot-safe on corrupt JSON', () => {
    expect(parseGridCells('{not json', 2, 'BTC/USDT', '1h')).toHaveLength(2)
    expect(parseGridCells(undefined, 4, 'BTC/USDT', '1h')).toHaveLength(4)
  })

  it('round-trips real cells', () => {
    const cells = [
      { symbol: 'BTC/USDT', timeframe: '1h' },
      { symbol: 'ETH/USDT', timeframe: '15m' },
    ]
    const back = parseGridCells(serializeGridCells(cells), 2, 'BTC/USDT', '1h')
    expect(back).toEqual(cells)
  })
})

describe('cell mutation', () => {
  const base = [
    { symbol: 'BTC/USDT', timeframe: '1h' },
    { symbol: 'ETH/USDT', timeframe: '4h' },
  ]

  it('setGridCellSymbol replaces one cell and returns a new array', () => {
    const next = setGridCellSymbol(base, 1, 'sol/usdt')
    expect(next).not.toBe(base)
    expect(next[1].symbol).toBe('SOL/USDT')
    expect(next[1].timeframe).toBe('4h') // timeframe preserved
    expect(next[0]).toEqual(base[0])
  })

  it('setGridCellSymbol is a no-op (same ref) on a bad symbol or out-of-range index', () => {
    expect(setGridCellSymbol(base, 0, 'garbage')).toBe(base)
    expect(setGridCellSymbol(base, 9, 'BTC/USDT')).toBe(base)
  })

  it('setGridCellTimeframe validates against the ladder', () => {
    const next = setGridCellTimeframe(base, 0, '1d')
    expect(next[0].timeframe).toBe('1d')
    expect(setGridCellTimeframe(base, 0, '3s')).toBe(base) // unknown tf -> no-op
  })

  it('resizeGridCells keeps choices and pads/trims to the new count', () => {
    const grown = resizeGridCells(base, 4, 'BNB/USDT', '1h')
    expect(grown).toHaveLength(4)
    expect(grown[0]).toEqual(base[0])
    expect(grown[1]).toEqual(base[1])
    expect(grown[2]).toEqual({ symbol: 'BNB/USDT', timeframe: '1h' })
    const shrunk = resizeGridCells(base, 2, 'BNB/USDT', '1h')
    expect(shrunk).toHaveLength(2)
  })
})
