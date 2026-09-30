import { useEffect, useRef, useState } from 'react'
import { PriceChart } from './PriceChart'
import { api } from './api'
import { mergeRecent } from './lazyHistory'
import {
  gridColumns,
  GRID_TIMEFRAMES,
  type GridLayout,
  type GridCellConfig,
} from './chartGrid'
import type { Candle } from './types'
import type { Theme } from './theme'
import type { IndicatorPrefs } from './indicators'
import type { IndicatorParams } from './indicatorParams'
import type { ChartKind } from './chartTypes'

// Multi-chart grid view (TradingView Ultimate "16 charts per tab" parity).
//
// Each cell is a FULL, independent PriceChart bound to its own real symbol +
// timeframe, fed by its own REST OHLCV poll. Cells share the app's overlay
// indicator prefs / chart type / indicator params (so the grid reflects the
// same analysis setup as the main chart), but the heavy per-chart interactions
// (drawings, draggable alerts, bar replay, compare, the kline WebSocket) stay on
// the single main chart — a grid is for MONITORING many pairs at once, and each
// cell renders honest real candles, never a fabricated series.

// Per-cell candle feed: initial load owns the array, later polls merge only the
// recent tail (mergeRecent) so a transient hiccup never blanks a cell. Grid
// cells poll a touch slower than the main chart (many charts at once) — still
// real, just gentler on the venue.
const GRID_POLL_MS = 15000

function useCellCandles(symbol: string, timeframe: string, bars: number): Candle[] {
  const [candles, setCandles] = useState<Candle[]>([])
  useEffect(() => {
    let alive = true
    setCandles([]) // drop the previous pair's bars immediately on a switch
    const load = (initial: boolean) =>
      api
        .ohlcv(symbol, timeframe, bars)
        .then((c) => {
          if (!alive) return
          setCandles((prev) => (initial ? c : mergeRecent(prev, c)))
        })
        .catch(() => {
          if (alive && initial) setCandles([])
        })
    load(true)
    const id = setInterval(() => load(false), GRID_POLL_MS)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [symbol, timeframe, bars])
  return candles
}

// A tiny commit-on-blur/Enter symbol box. Uncontrolled draft so the user can
// type freely; the parent only hears a NORMALISED symbol on commit (bad input
// snaps back to the current pair — chartGrid.setGridCellSymbol enforces it).
function GridSymbolInput({ value, onCommit }: { value: string; onCommit: (raw: string) => void }) {
  const [draft, setDraft] = useState(value)
  // Keep the box in sync when the parent changes the pair (e.g. resize/reset).
  useEffect(() => setDraft(value), [value])
  const commit = () => {
    const v = draft.trim()
    if (v && v.toUpperCase() !== value.toUpperCase()) onCommit(v)
    else setDraft(value)
  }
  return (
    <input
      className="grid-cell-sym"
      value={draft}
      spellCheck={false}
      aria-label="Chart symbol"
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
        else if (e.key === 'Escape') setDraft(value)
      }}
    />
  )
}

function GridCell({
  cell,
  bars,
  theme,
  indicators,
  indicatorParams,
  chartType,
  onSymbol,
  onTimeframe,
  onFocus,
}: {
  cell: GridCellConfig
  bars: number
  theme: Theme
  indicators: IndicatorPrefs
  indicatorParams: IndicatorParams
  chartType: ChartKind
  onSymbol: (raw: string) => void
  onTimeframe: (tf: string) => void
  onFocus: () => void
}) {
  const candles = useCellCandles(cell.symbol, cell.timeframe, bars)
  const last = candles.length ? candles[candles.length - 1].close : null
  return (
    <div className="grid-cell">
      <div className="grid-cell-head">
        <GridSymbolInput value={cell.symbol} onCommit={onSymbol} />
        <select
          className="grid-cell-tf"
          value={cell.timeframe}
          aria-label="Chart timeframe"
          onChange={(e) => onTimeframe(e.target.value)}
        >
          {GRID_TIMEFRAMES.map((tf) => (
            <option key={tf} value={tf}>
              {tf}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="grid-cell-focus"
          title="Open this pair on the main chart"
          aria-label="Open this pair on the main chart"
          onClick={onFocus}
        >
          ⤢
        </button>
      </div>
      <div className="grid-cell-chart">
        <PriceChart
          candles={candles}
          theme={theme}
          last={last}
          symbol={cell.symbol}
          timeframe={cell.timeframe}
          indicators={indicators}
          indicatorParams={indicatorParams}
          chartType={chartType}
          fitKey={`grid:${cell.symbol}:${cell.timeframe}:${bars}`}
        />
      </div>
    </div>
  )
}

export function ChartGrid({
  layout,
  cells,
  bars,
  theme,
  indicators,
  indicatorParams,
  chartType,
  onSetSymbol,
  onSetTimeframe,
  onFocusSymbol,
}: {
  layout: GridLayout
  cells: GridCellConfig[]
  bars: number
  theme: Theme
  indicators: IndicatorPrefs
  indicatorParams: IndicatorParams
  chartType: ChartKind
  onSetSymbol: (index: number, raw: string) => void
  onSetTimeframe: (index: number, tf: string) => void
  onFocusSymbol: (symbol: string) => void
}) {
  const cols = gridColumns(layout)
  // Keep the chart depth light for a busy grid — many simultaneous charts each
  // stitching thousands of bars would hammer the venue for no visible gain.
  const cellBars = Math.min(bars, 500)
  const gridRef = useRef<HTMLDivElement | null>(null)
  return (
    <div
      className="chart-grid"
      ref={gridRef}
      style={{ gridTemplateColumns: `repeat(${cols}, minmax(0, 1fr))` }}
    >
      {cells.map((cell, i) => (
        <GridCell
          key={i}
          cell={cell}
          bars={cellBars}
          theme={theme}
          indicators={indicators}
          indicatorParams={indicatorParams}
          chartType={chartType}
          onSymbol={(raw) => onSetSymbol(i, raw)}
          onTimeframe={(tf) => onSetTimeframe(i, tf)}
          onFocus={() => onFocusSymbol(cell.symbol)}
        />
      ))}
    </div>
  )
}
