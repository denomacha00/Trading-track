// Export the AI-assistant transcript to a downloadable Markdown file
// (TradingView-premium chat parity: "export/save your conversation").
//
// This is a faithful, HONEST dump of what's already on screen — every real turn
// in order, with its real timestamp when we have one, and a small tag when a turn
// was a proactive live call-out, carried attached news, or included an image. No
// turn is invented, dropped or reworded; an empty transcript exports just a
// header. The DOM download itself (Blob + anchor) lives in the caller — this
// module is pure so it can be unit-tested.

// The minimal shape we read off each transcript turn. Kept structural (not tied
// to App's ChatMsg) so the formatter stays decoupled and easy to test.
export interface ExportTurn {
  role: 'you' | 'ai'
  text: string
  ts?: number // unix ms when the turn was created (absent on older data)
  live?: boolean // a proactive monitor/alert call-out, not a reply you typed
  usedNews?: boolean // live headlines were attached to this question
  image?: unknown // a user-attached image rode along (not persisted across reloads)
}

// Two-digit zero-pad for the timestamp/filename builders.
function pad(n: number): string {
  return n < 10 ? '0' + n : String(n)
}

// A readable local timestamp for a turn: "2026-09-30 01:30". Returns null for a
// missing/non-finite ts so the caller omits the stamp rather than printing a fake
// epoch (Jan 1970) — honest about what we don't know.
export function formatStamp(ts: number | undefined, now: Date = new Date()): string | null {
  void now
  if (ts == null || !Number.isFinite(ts)) return null
  const d = new Date(ts)
  if (Number.isNaN(d.getTime())) return null
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}`
  )
}

// A safe, sortable download filename: "trading-track-chat-2026-09-30-0130.md".
export function transcriptFilename(now: Date = new Date()): string {
  return (
    `trading-track-chat-${now.getFullYear()}-${pad(now.getMonth() + 1)}-` +
    `${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}.md`
  )
}

// Which side said it, in the exported label.
function speaker(role: ExportTurn['role']): string {
  return role === 'you' ? 'You' : 'Assistant'
}

// Build the whole Markdown transcript. Header carries the app name and the export
// time; then one block per turn, in the SAME order they appear on screen. An empty
// transcript still returns a valid header (so the file is never blank/misleading).
export function formatTranscript(turns: ExportTurn[], now: Date = new Date()): string {
  const header = [
    '# Trading-track — AI assistant transcript',
    '',
    `Exported ${formatStamp(now.getTime(), now)} · ${turns.length} message${turns.length === 1 ? '' : 's'}`,
    '',
  ]
  if (turns.length === 0) {
    header.push('_No messages yet._', '')
    return header.join('\n')
  }
  const blocks = turns.map((t) => {
    const stamp = formatStamp(t.ts, now)
    const tags: string[] = []
    if (t.live) tags.push('live alert')
    if (t.usedNews) tags.push('with news')
    if (t.image) tags.push('image attached')
    const meta = [stamp, tags.length ? tags.join(', ') : null].filter(Boolean).join(' · ')
    const head = `## ${speaker(t.role)}${meta ? ` (${meta})` : ''}`
    // Preserve the message text verbatim (trailing whitespace trimmed only at the
    // very end of the block); never reflow or truncate it.
    return `${head}\n\n${t.text}`
  })
  return header.join('\n') + blocks.join('\n\n') + '\n'
}
