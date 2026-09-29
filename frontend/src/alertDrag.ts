// Pure geometry + decision helpers for dragging price-alert lines on the chart.
// Kept free of any chart/DOM dependency so the logic is unit-testable: the
// canvas layer (PriceChart) supplies pixel positions and the price<->pixel
// conversion, and calls these to decide what the drag means. Nothing here
// fabricates a price — it only reshapes values the chart already measured.

export interface AlertHandle {
  id: number
  /** Pixel y of the alert's horizontal line on the chart canvas. */
  y: number
  price: number
  condition: 'above' | 'below'
}

/**
 * Id of the alert line nearest the pointer's y, within `threshold` px, or null
 * when nothing is close enough (so a normal click/pan isn't hijacked). On a
 * tie the earlier handle in the list wins.
 */
export function nearestAlert(
  pointerY: number,
  handles: AlertHandle[],
  threshold = 7,
): number | null {
  if (!Number.isFinite(pointerY) || threshold < 0) return null
  let bestId: number | null = null
  let bestDist = Infinity
  for (const h of handles) {
    if (!Number.isFinite(h.y)) continue
    const d = Math.abs(h.y - pointerY)
    if (d <= threshold && d < bestDist) {
      bestDist = d
      bestId = h.id
    }
  }
  return bestId
}

/**
 * The honest condition for a line dragged to `price`, read against the current
 * market `lastPrice`: a level ABOVE the market means "notify when it rises to
 * here" (above); a level BELOW means "below" — exactly how a trader reads an
 * alert line. When the market price is unknown or sits exactly on the level we
 * can't tell, so we keep `fallback` (the alert's existing condition) rather
 * than guess.
 */
export function conditionForDrag(
  price: number,
  lastPrice: number | null | undefined,
  fallback: 'above' | 'below',
): 'above' | 'below' {
  if (lastPrice == null || !Number.isFinite(lastPrice) || !Number.isFinite(price)) {
    return fallback
  }
  if (price > lastPrice) return 'above'
  if (price < lastPrice) return 'below'
  return fallback
}

/**
 * A dragged price is valid only if finite and strictly positive (the backend
 * requires price > 0). Returns the cleaned number, or null to abandon the drag.
 */
export function sanitizeAlertPrice(price: number): number | null {
  if (!Number.isFinite(price) || price <= 0) return null
  return price
}
