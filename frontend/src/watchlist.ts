// Watchlist — the pure list math behind the user's saved symbols (TradingView's
// paid "watchlists" parity). No React, no network, no storage side-effects: given
// a list and a symbol it normalizes, dedupes, caps and reorders, so the panel and
// the star toggles stay a thin shell over logic that's fully unit-tested. The
// live prices shown against each row are real per-symbol tickers fetched in the
// panel — nothing here invents a price; this module only owns which symbols the
// user chose to track and in what order.

// How many symbols a single watchlist may hold. TradingView's free tier caps its
// watchlist; ours is generous but bounded so a runaway localStorage value or a
// paste of the whole market can't grow without limit.
export const MAX_WATCHLIST = 50

// Canonicalize a raw symbol into the exchange's `BASE/QUOTE` shape, or null when
// it isn't a real spot pair we can chart. Uppercased and trimmed; must have
// exactly one slash with a non-empty base and quote of letters/digits only. A
// `:`-suffixed derivatives contract (e.g. `BTC/USDT:USDT`) is rejected — the
// watchlist tracks spot pairs, matching the movers board. Returning null (rather
// than a guessed value) keeps junk out of the saved list by construction.
export function normalizeSymbol(raw: string): string | null {
  if (typeof raw !== 'string') return null
  const s = raw.trim().toUpperCase()
  if (!s || s.includes(':')) return null
  const parts = s.split('/')
  if (parts.length !== 2) return null
  const [base, quote] = parts
  if (!base || !quote) return null
  if (!/^[A-Z0-9]+$/.test(base) || !/^[A-Z0-9]+$/.test(quote)) return null
  return `${base}/${quote}`
}

// Is this symbol already on the list? Normalizes both sides so `btc/usdt` and a
// stored `BTC/USDT` count as the same pair.
export function inList(list: string[], raw: string): boolean {
  const sym = normalizeSymbol(raw)
  return sym != null && list.includes(sym)
}

// Add a symbol to the end of the list. A no-op (returns the same array contents)
// when the symbol is invalid, already present, or the list is at its cap — so the
// caller can compare lengths to tell whether anything changed and message the user
// ("already tracked" / "watchlist full") honestly.
export function addSymbol(list: string[], raw: string): string[] {
  const sym = normalizeSymbol(raw)
  if (sym == null) return list
  if (list.includes(sym)) return list
  if (list.length >= MAX_WATCHLIST) return list
  return [...list, sym]
}

// Remove a symbol from the list. No-op when it isn't present.
export function removeSymbol(list: string[], raw: string): string[] {
  const sym = normalizeSymbol(raw)
  if (sym == null) return list
  if (!list.includes(sym)) return list
  return list.filter((s) => s !== sym)
}

// Star toggle: drop the symbol if it's tracked, otherwise add it. Used by the ★
// buttons on the movers rows and the chart header.
export function toggleSymbol(list: string[], raw: string): string[] {
  return inList(list, raw) ? removeSymbol(list, raw) : addSymbol(list, raw)
}

// Move a tracked symbol one slot up (dir -1) or down (dir +1) so the user can
// arrange their watchlist. No-op when the symbol isn't present or is already at
// the relevant end (clamped, never wraps).
export function moveSymbol(list: string[], raw: string, dir: -1 | 1): string[] {
  const sym = normalizeSymbol(raw)
  if (sym == null) return list
  const i = list.indexOf(sym)
  if (i < 0) return list
  const j = i + dir
  if (j < 0 || j >= list.length) return list
  const next = [...list]
  ;[next[i], next[j]] = [next[j], next[i]]
  return next
}

// Parse a stored watchlist string (localStorage) back into a clean list: JSON that
// must be an array, each entry normalized, invalids dropped, deduped in order, and
// capped. Never throws — a corrupt or hand-edited value yields an empty list rather
// than crashing the app on boot.
export function parseStored(raw: string | null | undefined): string[] {
  if (!raw) return []
  let data: unknown
  try {
    data = JSON.parse(raw)
  } catch {
    return []
  }
  if (!Array.isArray(data)) return []
  const out: string[] = []
  for (const item of data) {
    if (typeof item !== 'string') continue
    const sym = normalizeSymbol(item)
    if (sym == null || out.includes(sym)) continue
    out.push(sym)
    if (out.length >= MAX_WATCHLIST) break
  }
  return out
}

// Serialize a list for storage. Trivial, but paired with parseStored so the
// storage format lives in one place and round-trips in tests.
export function serialize(list: string[]): string {
  return JSON.stringify(list)
}
