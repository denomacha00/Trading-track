// Price formatting shared by the chart's price axis, crosshair label and the
// OHLC legend.
//
// The one job here is honesty about magnitude: a sub-cent asset (SHIB, PEPE,
// BONK, …) must show its REAL price, not collapse to "0.00". lightweight-charts
// defaults a price series to 2 decimals, and a fixed `toFixed(2)` in a legend
// does the same — both round 0.00004523 to "0.00", which on a money tool is a
// lie about what the market is actually quoting.
//
// `priceDecimals` derives the number of decimal places from the price's own
// magnitude: at or above $1 it keeps cents (BTC / ETH / most alts), and below $1
// it widens to preserve ~4 significant figures, capped at 8 — Binance's finest
// price tick — so we never print more precision than an exchange actually
// quotes. It is a pure function of a real number: it never invents digits, it
// just stops hiding the ones we already have. (A future refinement could take a
// market's exact tick size from the exchange; until that is plumbed end-to-end,
// magnitude-derived precision matches the visible tick for display and is safe.)

export function priceDecimals(price: number): number {
  const a = Math.abs(price)
  if (!Number.isFinite(a) || a === 0) return 2
  if (a >= 1) return 2
  // Leading zeros right after the decimal point: 0 for 0.5, 2 for 0.004, 4 for
  // 0.00004. Adding 4 keeps ~4 significant figures; 8 is the exchange's floor.
  const leadingZeros = Math.floor(-Math.log10(a))
  return Math.min(8, leadingZeros + 4)
}

// The smallest price step for a given decimal count (lightweight-charts'
// `minMove`), e.g. 2 -> 0.01, 8 -> 0.00000001. Parsed from a string so the value
// carries exactly `decimals` places rather than picking up binary-float drift.
export function priceMinMove(decimals: number): number {
  return Number(`1e-${decimals}`)
}

// Format a price with either an explicit decimal count — so every O/H/L/C in one
// legend row shares the same precision — or, by default, the one its own
// magnitude calls for. Thousands separators keep large prices readable.
export function fmtPrice(value: number, decimals?: number): string {
  const dp = decimals ?? priceDecimals(value)
  return value.toLocaleString('en-US', {
    minimumFractionDigits: dp,
    maximumFractionDigits: dp,
  })
}
