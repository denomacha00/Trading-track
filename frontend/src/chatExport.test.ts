import { describe, it, expect } from 'vitest'
import {
  formatTranscript,
  transcriptFilename,
  formatStamp,
  type ExportTurn,
} from './chatExport'

// A fixed instant so the header/filename are deterministic: 2026-09-30 01:30 local.
const NOW = new Date(2026, 8, 30, 1, 30, 0)
// A turn timestamp: 2026-09-30 01:25 local.
const TS = new Date(2026, 8, 30, 1, 25, 0).getTime()

describe('formatStamp', () => {
  it('formats a finite ts as YYYY-MM-DD HH:MM', () => {
    expect(formatStamp(TS)).toBe('2026-09-30 01:25')
  })
  it('returns null for missing/non-finite ts (never a fake 1970 epoch)', () => {
    expect(formatStamp(undefined)).toBeNull()
    expect(formatStamp(NaN)).toBeNull()
    expect(formatStamp(Infinity)).toBeNull()
  })
})

describe('transcriptFilename', () => {
  it('is safe, sortable and stamped to the minute', () => {
    expect(transcriptFilename(NOW)).toBe('trading-track-chat-2026-09-30-0130.md')
  })
  it('zero-pads single-digit month/day/hour/minute', () => {
    expect(transcriptFilename(new Date(2026, 0, 5, 9, 7, 0))).toBe(
      'trading-track-chat-2026-01-05-0907.md',
    )
  })
})

describe('formatTranscript', () => {
  it('empty transcript → header only, marked "No messages yet"', () => {
    const out = formatTranscript([], NOW)
    expect(out).toContain('# Trading-track — AI assistant transcript')
    expect(out).toContain('0 messages')
    expect(out).toContain('_No messages yet._')
  })

  it('singular vs plural message count', () => {
    const one: ExportTurn[] = [{ role: 'you', text: 'hi' }]
    expect(formatTranscript(one, NOW)).toContain('· 1 message')
    const two: ExportTurn[] = [
      { role: 'you', text: 'hi' },
      { role: 'ai', text: 'hello' },
    ]
    expect(formatTranscript(two, NOW)).toContain('· 2 messages')
  })

  it('labels speakers and preserves order + verbatim text', () => {
    const turns: ExportTurn[] = [
      { role: 'you', text: 'What is BTC doing?', ts: TS },
      { role: 'ai', text: 'It is ranging.\n\nWith a newline.', ts: TS },
    ]
    const out = formatTranscript(turns, NOW)
    expect(out).toContain('## You (2026-09-30 01:25)')
    expect(out).toContain('## Assistant (2026-09-30 01:25)')
    // Order preserved: You appears before Assistant.
    expect(out.indexOf('## You')).toBeLessThan(out.indexOf('## Assistant'))
    // Text is verbatim, including the internal blank line.
    expect(out).toContain('It is ranging.\n\nWith a newline.')
  })

  it('omits the stamp (not a fake epoch) when a turn has no ts', () => {
    const out = formatTranscript([{ role: 'ai', text: 'no stamp here' }], NOW)
    expect(out).toContain('## Assistant\n')
    expect(out).not.toContain('1970')
  })

  it('tags live call-outs, news-attached turns and images', () => {
    const turns: ExportTurn[] = [
      { role: 'ai', text: 'BTC just broke out', ts: TS, live: true },
      { role: 'you', text: 'news?', ts: TS, usedNews: true },
      { role: 'you', text: 'read this chart', ts: TS, image: { data: 'x' } },
    ]
    const out = formatTranscript(turns, NOW)
    expect(out).toContain('live alert')
    expect(out).toContain('with news')
    expect(out).toContain('image attached')
  })

  it('combines multiple tags with the stamp in one meta clause', () => {
    const out = formatTranscript(
      [{ role: 'ai', text: 'x', ts: TS, live: true, usedNews: true }],
      NOW,
    )
    expect(out).toContain('## Assistant (2026-09-30 01:25 · live alert, with news)')
  })

  it('does not invent, drop or reword turns', () => {
    const turns: ExportTurn[] = [
      { role: 'you', text: 'one' },
      { role: 'ai', text: 'two' },
      { role: 'you', text: 'three' },
    ]
    const out = formatTranscript(turns, NOW)
    for (const t of turns) expect(out).toContain(t.text)
    // Exactly three "## " turn headers.
    expect((out.match(/\n## /g) || []).length).toBe(3)
  })
})
