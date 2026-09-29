import { describe, it, expect } from 'vitest'
import {
  nearestAlert,
  conditionForDrag,
  sanitizeAlertPrice,
  type AlertHandle,
} from './alertDrag'

const h = (id: number, y: number): AlertHandle => ({
  id,
  y,
  price: 100 + id,
  condition: 'above',
})

describe('nearestAlert', () => {
  it('returns null when no handle is within the threshold', () => {
    expect(nearestAlert(100, [h(1, 50), h(2, 200)], 7)).toBeNull()
  })

  it('grabs the line under the pointer when within threshold', () => {
    expect(nearestAlert(52, [h(1, 50), h(2, 200)], 7)).toBe(1)
  })

  it('picks the closest of several nearby lines', () => {
    expect(nearestAlert(100, [h(1, 108), h(2, 103), h(3, 96)], 10)).toBe(2)
  })

  it('breaks an exact tie toward the earlier handle', () => {
    // both 5px away
    expect(nearestAlert(100, [h(1, 95), h(2, 105)], 7)).toBe(1)
  })

  it('includes the exact threshold edge', () => {
    expect(nearestAlert(100, [h(1, 107)], 7)).toBe(1)
    expect(nearestAlert(100, [h(1, 108)], 7)).toBeNull()
  })

  it('ignores non-finite pointer or handle positions', () => {
    expect(nearestAlert(Number.NaN, [h(1, 100)], 7)).toBeNull()
    expect(nearestAlert(100, [{ ...h(1, 100), y: Number.NaN }], 7)).toBeNull()
  })

  it('returns null for an empty handle list', () => {
    expect(nearestAlert(100, [], 7)).toBeNull()
  })
})

describe('conditionForDrag', () => {
  it('is "above" when the level sits above the market price', () => {
    expect(conditionForDrag(110, 100, 'below')).toBe('above')
  })

  it('is "below" when the level sits below the market price', () => {
    expect(conditionForDrag(90, 100, 'above')).toBe('below')
  })

  it('keeps the fallback when the market price is unknown', () => {
    expect(conditionForDrag(110, null, 'below')).toBe('below')
    expect(conditionForDrag(110, undefined, 'above')).toBe('above')
  })

  it('keeps the fallback when the level sits exactly on the market price', () => {
    expect(conditionForDrag(100, 100, 'below')).toBe('below')
  })

  it('keeps the fallback for a non-finite level', () => {
    expect(conditionForDrag(Number.NaN, 100, 'above')).toBe('above')
  })
})

describe('sanitizeAlertPrice', () => {
  it('passes a finite positive price through', () => {
    expect(sanitizeAlertPrice(123.45)).toBe(123.45)
  })

  it('rejects zero, negative, and non-finite prices', () => {
    expect(sanitizeAlertPrice(0)).toBeNull()
    expect(sanitizeAlertPrice(-5)).toBeNull()
    expect(sanitizeAlertPrice(Number.NaN)).toBeNull()
    expect(sanitizeAlertPrice(Infinity)).toBeNull()
  })
})
