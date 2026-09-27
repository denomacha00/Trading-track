import { describe, it, expect } from 'vitest'
import { priceDecimals, priceMinMove, fmtPrice } from './priceFormat'

// M12 — sub-cent assets (SHIB, PEPE, …) must show their REAL price, never round
// to "0.00". These pin the magnitude-derived precision that the chart's axis,
// crosshair and OHLC legend all share.

describe('priceDecimals', () => {
  it('keeps cents for prices at or above $1', () => {
    expect(priceDecimals(63500.12)).toBe(2)
    expect(priceDecimals(3120.45)).toBe(2)
    expect(priceDecimals(1.5)).toBe(2)
    expect(priceDecimals(1)).toBe(2)
  })

  it('widens below $1 to keep ~4 significant figures', () => {
    expect(priceDecimals(0.5234)).toBe(4)
    expect(priceDecimals(0.08234)).toBe(5)
    expect(priceDecimals(0.004523)).toBe(6)
    expect(priceDecimals(0.00004523)).toBe(8)
  })

  it('caps at 8 — the exchange floor — for deep sub-cent prices', () => {
    expect(priceDecimals(0.0000004523)).toBe(8)
    expect(priceDecimals(1e-12)).toBe(8)
  })

  it('uses magnitude, not sign', () => {
    expect(priceDecimals(-0.00004523)).toBe(8)
    expect(priceDecimals(-63500.12)).toBe(2)
  })

  it('never shows fewer decimals than a sub-cent price needs', () => {
    // The safe direction for a money tool is to over-show, never round a real
    // digit away — precision is monotone as the price shrinks.
    expect(priceDecimals(0.004523)).toBeGreaterThanOrEqual(4)
    expect(priceDecimals(0.00004523)).toBeGreaterThan(priceDecimals(0.4523))
  })

  it('falls back to 2 for zero / non-finite input', () => {
    expect(priceDecimals(0)).toBe(2)
    expect(priceDecimals(NaN)).toBe(2)
    expect(priceDecimals(Infinity)).toBe(2)
    expect(priceDecimals(-Infinity)).toBe(2)
  })
})

describe('priceMinMove', () => {
  it('maps a decimal count to its smallest step', () => {
    expect(priceMinMove(2)).toBe(0.01)
    expect(priceMinMove(4)).toBe(0.0001)
    expect(priceMinMove(8)).toBe(1e-8)
  })
})

describe('fmtPrice', () => {
  it('renders a sub-cent price at full precision, not "0.00"', () => {
    const out = fmtPrice(0.00004523)
    expect(out).not.toBe('0.00')
    expect(out).toContain('4523')
    expect(out).toBe('0.00004523')
  })

  it('renders large prices with cents and thousands separators', () => {
    expect(fmtPrice(63500.5)).toBe('63,500.50')
    expect(fmtPrice(0.5234)).toBe('0.5234')
  })

  it('honours an explicit decimal count so one legend row lines up', () => {
    expect(fmtPrice(1234.567, 2)).toBe('1,234.57')
    expect(fmtPrice(0.00004523, 8)).toBe('0.00004523')
    // A tiny OHLC row shares the close's precision even for the near-zero ones.
    expect(fmtPrice(0, 8)).toBe('0.00000000')
  })
})
