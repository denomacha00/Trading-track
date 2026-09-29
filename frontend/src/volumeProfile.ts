// Volume Profile (a.k.a. VPVR — Volume Profile Visible Range): the horizontal
// histogram TradingView draws up the right edge, showing HOW MUCH real volume
// traded at each PRICE (not at each time). Every number here is summed straight
// from the chart's own candles — nothing is invented. We slice the loaded price
// range into equal buckets and spread each bar's real volume across the buckets
// its high–low range covers (weighted by overlap), which is the standard way to
// build a profile from OHLCV when tick data isn't available. The fattest bucket
// is the POC (Point of Control) — the price the market spent the most volume at.
// The Value Area (VAH/VAL) is the band around the POC holding ~70% of the volume
// — the range price "accepted" — computed the standard Market-Profile way below.
import type { Candle } from './types'

// One horizontal price bucket of the profile.
export type VolumeRow = {
  lo: number // bucket's low price (inclusive)
  hi: number // bucket's high price
  mid: number // bucket midpoint (used to anchor/label the row)
  volume: number // total real volume attributed to this price band
}

export type VolumeProfile = {
  rows: VolumeRow[]
  maxVolume: number // volume of the fattest bucket (for scaling bar widths)
  poc: number | null // price (bucket mid) of the fattest bucket; null when empty
  vah: number | null // Value Area High — top price of the ~70%-volume band; null when empty
  val: number | null // Value Area Low — bottom price of the ~70%-volume band; null when empty
  priceMin: number
  priceMax: number
}

const EMPTY: VolumeProfile = {
  rows: [], maxVolume: 0, poc: null, vah: null, val: null, priceMin: 0, priceMax: 0,
}

// Build a volume profile from candles over `bins` equal-height price buckets.
// Returns EMPTY for no candles or an unusable price range. Deterministic and
// side-effect free so it can be unit-tested and memoised.
export function volumeProfile(candles: Candle[], bins = 24): VolumeProfile {
  if (!candles.length) return EMPTY
  const n = Math.max(1, Math.floor(bins))
  let priceMin = Infinity
  let priceMax = -Infinity
  for (const c of candles) {
    if (c.low < priceMin) priceMin = c.low
    if (c.high > priceMax) priceMax = c.high
  }
  if (!Number.isFinite(priceMin) || !Number.isFinite(priceMax)) return EMPTY

  // Flat range (every bar the same price): one bucket holds all the volume.
  if (priceMax <= priceMin) {
    let vol = 0
    for (const c of candles) vol += Number.isFinite(c.volume) ? c.volume : 0
    return {
      rows: [{ lo: priceMin, hi: priceMax, mid: priceMin, volume: vol }],
      maxVolume: vol,
      poc: vol > 0 ? priceMin : null,
      vah: vol > 0 ? priceMax : null,
      val: vol > 0 ? priceMin : null,
      priceMin,
      priceMax,
    }
  }

  const h = (priceMax - priceMin) / n
  const rows: VolumeRow[] = []
  for (let i = 0; i < n; i++) {
    const lo = priceMin + i * h
    const hi = i === n - 1 ? priceMax : priceMin + (i + 1) * h
    rows.push({ lo, hi, mid: (lo + hi) / 2, volume: 0 })
  }
  // Which bucket a price falls in (clamped to the ends).
  const idxOf = (price: number) => {
    if (price <= priceMin) return 0
    if (price >= priceMax) return n - 1
    const i = Math.floor((price - priceMin) / h)
    return i < 0 ? 0 : i >= n ? n - 1 : i
  }

  for (const c of candles) {
    const v = Number.isFinite(c.volume) ? c.volume : 0
    if (v <= 0) continue
    const lo = Math.min(c.low, c.high)
    const hi = Math.max(c.low, c.high)
    const iLo = idxOf(lo)
    const iHi = idxOf(hi)
    if (hi <= lo || iLo === iHi) {
      // A bar with no range (or one that sits inside a single bucket) dumps all
      // its volume into that one price band.
      rows[iLo].volume += v
      continue
    }
    // Spread across every bucket the bar's high–low span touches, in proportion
    // to how much of the span lands in each bucket.
    const range = hi - lo
    for (let i = iLo; i <= iHi; i++) {
      const overlap = Math.min(hi, rows[i].hi) - Math.max(lo, rows[i].lo)
      if (overlap > 0) rows[i].volume += v * (overlap / range)
    }
  }

  let maxVolume = 0
  let pocMid = rows[0].mid
  let pocIndex = 0
  for (let i = 0; i < rows.length; i++) {
    if (rows[i].volume > maxVolume) {
      maxVolume = rows[i].volume
      pocMid = rows[i].mid
      pocIndex = i
    }
  }
  const va = maxVolume > 0 ? valueArea(rows, pocIndex) : { vah: null, val: null }
  return {
    rows,
    maxVolume,
    poc: maxVolume > 0 ? pocMid : null,
    vah: va.vah,
    val: va.val,
    priceMin,
    priceMax,
  }
}

// The Value Area: the price band around the POC that holds `percent` (70% by
// TradingView's default) of all the profile's volume — the range the market
// "accepted". Built the standard Market-Profile way: start at the POC bucket and
// step outward, each step adding whichever side carries more volume (the next two
// rows above the band vs. the next two below), until the accumulated volume
// reaches the target. VAH is the top price of the resulting band, VAL its bottom.
// Pure and deterministic; returns nulls for an empty or zero-volume profile.
export function valueArea(
  rows: VolumeRow[],
  pocIndex: number,
  percent = 0.7,
): { vah: number | null; val: number | null } {
  const n = rows.length
  if (n === 0 || pocIndex < 0 || pocIndex >= n) return { vah: null, val: null }
  const volAt = (i: number) => (rows[i].volume > 0 ? rows[i].volume : 0)
  let total = 0
  for (let i = 0; i < n; i++) total += volAt(i)
  if (total <= 0) return { vah: null, val: null }
  const target = total * Math.min(1, Math.max(0, percent))

  let up = pocIndex // highest bucket index currently in the value area
  let down = pocIndex // lowest bucket index currently in the value area
  let acc = volAt(pocIndex)
  while (acc < target && (up < n - 1 || down > 0)) {
    // Volume of the next up-to-two rows on each side (Market-Profile pairs).
    let aboveVol = 0
    let aboveTo = up
    if (up + 1 <= n - 1) { aboveVol += volAt(up + 1); aboveTo = up + 1 }
    if (up + 2 <= n - 1) { aboveVol += volAt(up + 2); aboveTo = up + 2 }
    let belowVol = 0
    let belowTo = down
    if (down - 1 >= 0) { belowVol += volAt(down - 1); belowTo = down - 1 }
    if (down - 2 >= 0) { belowVol += volAt(down - 2); belowTo = down - 2 }

    const canUp = up < n - 1
    const canDown = down > 0
    if (canUp && (!canDown || aboveVol >= belowVol)) {
      acc += aboveVol
      up = aboveTo
    } else if (canDown) {
      acc += belowVol
      down = belowTo
    } else {
      break
    }
  }
  return { vah: rows[up].hi, val: rows[down].lo }
}
