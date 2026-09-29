// Chart-type transforms (TradingView parity: "Candles" vs "Heikin-Ashi").
//
// Heikin-Ashi ("average bar") smooths the candles to make trend/consolidation
// easier to read. It is a pure DERIVATION of the real OHLC — no data is invented
// and no real bar is discarded: every HA value is an average of REAL prices, and
// the mapping is 1:1 with the real candles (same count, same timestamps, same
// volume). Indicators, drawings and alerts keep working on the real candles; only
// the drawn candle bodies change. Switching back to Candles is lossless.
//
// Formula (standard):
//   HA_close = (open + high + low + close) / 4
//   HA_open  = first bar: (open + close) / 2
//              else:       (prev HA_open + prev HA_close) / 2
//   HA_high  = max(high, HA_open, HA_close)
//   HA_low   = min(low,  HA_open, HA_close)
import type { Candle } from './types'

export type ChartKind = 'candles' | 'heikin_ashi'

// The chart-type picker's options, in display order. Extend here to add more
// drawable types later (line / area / bars) — the picker renders this list.
export const CHART_TYPES: { key: ChartKind; label: string }[] = [
  { key: 'candles', label: 'Candles' },
  { key: 'heikin_ashi', label: 'Heikin-Ashi' },
]

export function isChartKind(v: unknown): v is ChartKind {
  return v === 'candles' || v === 'heikin_ashi'
}

// Just the OHLC quad an HA bar carries (time/volume are copied through by the
// callers that need them — the live-update path only wants the four prices).
export interface Ohlc {
  open: number
  high: number
  low: number
  close: number
}

// One Heikin-Ashi bar from one real bar plus the PREVIOUS HA bar (null for the
// first bar of the series). Used for the live/forming candle so it stays exactly
// consistent with the batch transform below as its real OHLC ticks.
export function haBar(raw: Ohlc, prevHA: Ohlc | null): Ohlc {
  const close = (raw.open + raw.high + raw.low + raw.close) / 4
  const open = prevHA ? (prevHA.open + prevHA.close) / 2 : (raw.open + raw.close) / 2
  const high = Math.max(raw.high, open, close)
  const low = Math.min(raw.low, open, close)
  return { open, high, low, close }
}

// Transform a full candle series to Heikin-Ashi. Returns a NEW array of the same
// length, same timestamps and same volumes — only OHLC is averaged. An empty or
// single-bar input is handled (the first bar seeds the recurrence).
export function heikinAshi(candles: Candle[]): Candle[] {
  const out: Candle[] = []
  let prev: Ohlc | null = null
  for (const c of candles) {
    const ha = haBar(c, prev)
    out.push({
      time: c.time,
      open: ha.open,
      high: ha.high,
      low: ha.low,
      close: ha.close,
      volume: c.volume,
    })
    prev = ha
  }
  return out
}
