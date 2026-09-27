import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import { loadTurns, saveTurns, clearTurns, type StoredTurn } from './chatHistory'

// The test env is node (no DOM), so give this file a tiny in-memory localStorage
// to exercise the real round trip. Removed after each test so the "no DOM" case
// can still be checked honestly.
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

const U = 7 // a user id
const now = () => Date.now()
const HOUR = 60 * 60 * 1000

function turn(over: Partial<StoredTurn> = {}): StoredTurn {
  return { role: 'you', text: 'hello', ts: now(), ...over }
}

describe('chatHistory persistence', () => {
  beforeEach(() => {
    ;(globalThis as any).localStorage = new MemStorage()
  })
  afterEach(() => {
    delete (globalThis as any).localStorage
  })

  it('round-trips a transcript for the same user', () => {
    const turns: StoredTurn[] = [
      turn({ role: 'you', text: 'how is my bot doing?' }),
      turn({ role: 'ai', text: 'Up 1.2% today.' }),
    ]
    saveTurns(U, turns)
    const back = loadTurns(U)
    expect(back).toHaveLength(2)
    expect(back.map((t) => t.text)).toEqual(['how is my bot doing?', 'Up 1.2% today.'])
    expect(back.map((t) => t.role)).toEqual(['you', 'ai'])
  })

  it('never returns another user\'s history', () => {
    saveTurns(U, [turn({ text: 'my private position' })])
    expect(loadTurns(U + 1)).toEqual([])
  })

  it('drops turns older than 24h but keeps recent ones', () => {
    saveTurns(U, [
      turn({ text: 'stale', ts: now() - 25 * HOUR }),
      turn({ text: 'fresh', ts: now() - 1 * HOUR }),
    ])
    const back = loadTurns(U)
    expect(back.map((t) => t.text)).toEqual(['fresh'])
  })

  it('neutralises a proposed-action card on load (no one-click execute after reload)', () => {
    saveTurns(U, [
      turn({
        role: 'ai',
        text: 'I suggest a safe BTC buy.',
        action: { type: 'order', side: 'buy', symbol: 'BTC/USDT', amount: null },
        actionState: 'pending',
      }),
    ])
    const back = loadTurns(U)
    expect(back).toHaveLength(1)
    expect(back[0].text).toBe('I suggest a safe BTC buy.')
    expect((back[0] as any).action).toBeUndefined()
    expect((back[0] as any).actionState).toBeUndefined()
  })

  it('strips an attached image on save (keeps text, drops the base64)', () => {
    saveTurns(U, [
      turn({
        role: 'you',
        text: 'what do you see in this chart?',
        image: { data: 'AAAABBBBCCCC', mediaType: 'image/jpeg' },
      } as StoredTurn),
    ])
    const back = loadTurns(U)
    expect(back).toHaveLength(1)
    expect(back[0].text).toBe('what do you see in this chart?')
    expect((back[0] as any).image).toBeUndefined()
  })

  it('keeps only the most recent 300 turns', () => {
    const many = Array.from({ length: 350 }, (_, i) => turn({ text: `m${i}` }))
    saveTurns(U, many)
    const back = loadTurns(U)
    expect(back).toHaveLength(300)
    expect(back[0].text).toBe('m50')
    expect(back[back.length - 1].text).toBe('m349')
  })

  it('stamps a missing timestamp instead of dropping the turn', () => {
    saveTurns(U, [{ role: 'ai', text: 'no ts here' } as StoredTurn])
    const back = loadTurns(U)
    expect(back).toHaveLength(1)
    expect(typeof back[0].ts).toBe('number')
  })

  it('ignores corrupt / non-matching stored data', () => {
    localStorage.setItem('tt.chat.v1', '{not json')
    expect(loadTurns(U)).toEqual([])
    localStorage.setItem('tt.chat.v1', JSON.stringify({ userId: U, turns: 'nope' }))
    expect(loadTurns(U)).toEqual([])
  })

  it('drops junk turns (bad role / missing text)', () => {
    localStorage.setItem(
      'tt.chat.v1',
      JSON.stringify({
        userId: U,
        turns: [
          { role: 'you', text: 'good', ts: now() },
          { role: 'bogus', text: 'x', ts: now() },
          { role: 'ai', ts: now() },
        ],
      }),
    )
    const back = loadTurns(U)
    expect(back.map((t) => t.text)).toEqual(['good'])
  })

  it('clearTurns wipes the transcript', () => {
    saveTurns(U, [turn()])
    clearTurns()
    expect(loadTurns(U)).toEqual([])
  })
})

describe('chatHistory stays safe without a DOM', () => {
  it('loadTurns returns [] and saveTurns/clearTurns do not throw under node', () => {
    expect(loadTurns(1)).toEqual([])
    expect(() => saveTurns(1, [turn()])).not.toThrow()
    expect(() => clearTurns()).not.toThrow()
  })
})
