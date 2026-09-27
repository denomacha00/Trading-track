// Browser-local persistence for the AI-assistant transcript, so the conversation
// survives a page refresh OR a full browser quit for 24 hours. The assistant can
// then still "remember" what you asked earlier: on your next question the panel
// feeds these turns back as history (see AssistantPanel.send), so "what was my
// last question?" is answerable instead of the AI claiming it has no history.
//
// Real messages only — nothing is invented or back-filled. Privacy: a transcript
// can mention your positions and P&L, so it's SCOPED TO YOUR USER ID; a different
// account logging in on the same browser never sees it. It lives only in this
// browser's localStorage and ages out after 24h.
import type { ChatTurn } from './types'

// A transcript turn as persisted: the ChatTurn plus a creation timestamp and any
// UI metadata (nav button, proposed-action card, live flag) carried opaquely, so
// this module doesn't depend on App's ChatMsg (which pulls in TabKey /
// ProposedAction). Proposed-action cards are STRIPPED on load (see loadTurns) so
// a stale, hours-old "place this order" card can never be one-click executed at
// today's very different price — only the message text survives as memory.
export type StoredTurn = ChatTurn & {
  ts?: number
  action?: unknown
  actionState?: unknown
  // Stable per-session action id (App._actionTurnSeq). STRIPPED on load: the
  // session counter resets on reload, so keeping it would let a fresh action's
  // id collide with a restored turn's — and action-card state is keyed by id.
  id?: unknown
  nav?: unknown
  live?: boolean
  // A user-attached image (raw base64). STRIPPED on save (see prune): it's large
  // binary that would blow the localStorage quota, so the thumbnail is a
  // this-session convenience only — the message text is what persists as memory.
  image?: unknown
}

const CHAT_KEY = 'tt.chat.v1'
const TTL_MS = 24 * 60 * 60 * 1000 // keep history for 24 hours
const MAX_TURNS = 300 // hard cap so storage can't grow without bound

type Envelope = { userId: number; turns: StoredTurn[] }

// Stamp any turn still missing a timestamp with `now` (defensive — real turns are
// stamped at creation), drop everything older than the 24h window, and keep only
// the most recent MAX_TURNS.
function prune(turns: StoredTurn[], now: number): StoredTurn[] {
  const out: StoredTurn[] = []
  for (const t of turns) {
    if (!t || (t.role !== 'you' && t.role !== 'ai') || typeof t.text !== 'string') continue
    const ts = typeof t.ts === 'number' && Number.isFinite(t.ts) ? t.ts : now
    if (now - ts > TTL_MS) continue
    // Never persist attached image bytes — large base64 would blow the quota and
    // isn't needed as memory. Drop it (and stamp the ts) by always cloning.
    const { image, ...rest } = t
    void image
    out.push({ ...rest, ts })
  }
  return out.slice(-MAX_TURNS)
}

// Save the transcript for `userId`. Guarded so it's safe under SSR/tests (no
// localStorage global) and never throws on a full or blocked quota.
export function saveTurns<T extends StoredTurn>(userId: number, turns: T[]): void {
  if (typeof localStorage === 'undefined') return
  try {
    const env: Envelope = { userId, turns: prune(turns as StoredTurn[], Date.now()) }
    localStorage.setItem(CHAT_KEY, JSON.stringify(env))
  } catch {
    /* quota / private mode — history just won't persist this time, no crash */
  }
}

// Load the saved transcript for `userId`. Returns [] when there's nothing stored,
// storage is unavailable, the data is corrupt, or it belongs to a DIFFERENT user
// (never show one account's chat to another). Turns older than 24h are dropped,
// and every proposed-action card is neutralised so nothing can be executed after
// a reload — only the message text (and harmless nav buttons) survive.
export function loadTurns<T extends StoredTurn>(userId: number): T[] {
  if (typeof localStorage === 'undefined') return []
  try {
    const raw = localStorage.getItem(CHAT_KEY)
    if (!raw) return []
    const env = JSON.parse(raw) as Partial<Envelope>
    if (!env || env.userId !== userId || !Array.isArray(env.turns)) return []
    const clean: T[] = []
    for (const t of prune(env.turns as StoredTurn[], Date.now())) {
      // Drop the actionable card + any stale lifecycle state; keep the text. The
      // id goes too: it's a per-session action key that must not survive to
      // collide with a fresh action's id after the session counter resets.
      const { action, actionState, id, ...rest } = t
      void action
      void actionState
      void id
      clean.push(rest as unknown as T)
    }
    return clean
  } catch {
    return []
  }
}

// Wipe the stored transcript (e.g. an explicit "clear chat"). Safe under SSR.
export function clearTurns(): void {
  if (typeof localStorage === 'undefined') return
  try {
    localStorage.removeItem(CHAT_KEY)
  } catch {
    /* ignore */
  }
}
