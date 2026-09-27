import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import {
  ICT_OVERLAY_KEYS,
  DEFAULT_ICT_OVERLAYS,
  ICT_OVERLAY_GROUPS,
  ictOverlayCount,
  anyIctOverlayOn,
  allIctOverlays,
  mergeIctOverlays,
  parseIctOverlays,
  loadIctOverlays,
  saveIctOverlays,
  type IctOverlayPrefs,
} from './ictOverlays'

describe('ICT overlay defaults', () => {
  it('has a boolean for every canonical key', () => {
    for (const k of ICT_OVERLAY_KEYS) {
      expect(typeof DEFAULT_ICT_OVERLAYS[k]).toBe('boolean')
    }
    // No stray keys beyond the canonical list.
    expect(Object.keys(DEFAULT_ICT_OVERLAYS).sort()).toEqual([...ICT_OVERLAY_KEYS].sort())
  })
  it('starts with a clean curated subset on (the headline concepts), not everything', () => {
    const on = ICT_OVERLAY_KEYS.filter((k) => DEFAULT_ICT_OVERLAYS[k])
    expect(on).toContain('structure')
    expect(on).toContain('sweeps')
    expect(on).toContain('orderBlocks')
    expect(on).toContain('fvg')
    expect(on).toContain('dealingRange')
    expect(on).toContain('keyLevels')
    // Noisier/advanced layers default OFF so the first look stays readable.
    expect(DEFAULT_ICT_OVERLAYS.swings).toBe(false)
    expect(DEFAULT_ICT_OVERLAYS.volumeImbalance).toBe(false)
    // Some are on and some are off — never all-on by default.
    expect(on.length).toBeGreaterThan(0)
    expect(on.length).toBeLessThan(ICT_OVERLAY_KEYS.length)
  })
  it('lists every key exactly once across the menu groups', () => {
    const grouped = ICT_OVERLAY_GROUPS.flatMap((g) => g.items.map((i) => i.key))
    expect(grouped.sort()).toEqual([...ICT_OVERLAY_KEYS].sort())
    // Every menu item carries a beginner-facing description.
    for (const g of ICT_OVERLAY_GROUPS) {
      for (const item of g.items) expect(item.desc.length).toBeGreaterThan(0)
    }
  })
})

describe('count / any helpers', () => {
  it('counts only the on overlays', () => {
    expect(ictOverlayCount(allIctOverlays(false))).toBe(0)
    expect(ictOverlayCount(allIctOverlays(true))).toBe(ICT_OVERLAY_KEYS.length)
    expect(ictOverlayCount(DEFAULT_ICT_OVERLAYS)).toBe(
      ICT_OVERLAY_KEYS.filter((k) => DEFAULT_ICT_OVERLAYS[k]).length,
    )
  })
  it('anyIctOverlayOn is false only when all off', () => {
    expect(anyIctOverlayOn(allIctOverlays(false))).toBe(false)
    expect(anyIctOverlayOn(allIctOverlays(true))).toBe(true)
    expect(anyIctOverlayOn(DEFAULT_ICT_OVERLAYS)).toBe(true)
  })
})

describe('mergeIctOverlays', () => {
  it('applies only real keys and real booleans, ignoring junk', () => {
    const base = allIctOverlays(false)
    const merged = mergeIctOverlays(base, {
      structure: true, // real, applied
      fvg: 'yes', // wrong type, ignored
      bogusKey: true, // unknown key, ignored
      sweeps: true, // real, applied
    } as Record<string, unknown>)
    expect(merged.structure).toBe(true)
    expect(merged.sweeps).toBe(true)
    expect(merged.fvg).toBe(false) // unchanged — bad type was ignored
    expect('bogusKey' in merged).toBe(false)
  })
  it('does not mutate the base object', () => {
    const base = allIctOverlays(false)
    mergeIctOverlays(base, { structure: true })
    expect(base.structure).toBe(false)
  })
  it('tolerates null/undefined patches', () => {
    const base = { ...DEFAULT_ICT_OVERLAYS }
    expect(mergeIctOverlays(base, null)).toEqual(base)
    expect(mergeIctOverlays(base, undefined)).toEqual(base)
  })
})

describe('parseIctOverlays', () => {
  it('falls back to defaults on null/garbage and never throws', () => {
    expect(parseIctOverlays(null)).toEqual(DEFAULT_ICT_OVERLAYS)
    expect(parseIctOverlays('not json{')).toEqual(DEFAULT_ICT_OVERLAYS)
  })
  it('fills missing keys from the default and keeps stored ones', () => {
    const parsed = parseIctOverlays(JSON.stringify({ structure: false, swings: true }))
    expect(parsed.structure).toBe(false) // stored override
    expect(parsed.swings).toBe(true) // stored override
    expect(parsed.keyLevels).toBe(DEFAULT_ICT_OVERLAYS.keyLevels) // missing → default
    // Result is always a full, valid prefs object.
    expect(Object.keys(parsed).sort()).toEqual([...ICT_OVERLAY_KEYS].sort())
  })
})

describe('load/save roundtrip', () => {
  // Node test env has no DOM; give it a tiny in-memory localStorage.
  class MemStorage {
    private m = new Map<string, string>()
    getItem(k: string) {
      return this.m.has(k) ? (this.m.get(k) as string) : null
    }
    setItem(k: string, v: string) {
      this.m.set(k, String(v))
    }
    removeItem(k: string) {
      this.m.delete(k)
    }
    clear() {
      this.m.clear()
    }
  }
  beforeEach(() => {
    ;(globalThis as { localStorage?: unknown }).localStorage = new MemStorage()
  })
  afterEach(() => {
    delete (globalThis as { localStorage?: unknown }).localStorage
  })
  it('persists and reloads exactly', () => {
    const prefs: IctOverlayPrefs = { ...DEFAULT_ICT_OVERLAYS, swings: true, structure: false }
    saveIctOverlays(prefs)
    expect(loadIctOverlays()).toEqual(prefs)
  })
  it('returns defaults when nothing is stored', () => {
    expect(loadIctOverlays()).toEqual(DEFAULT_ICT_OVERLAYS)
  })
})
