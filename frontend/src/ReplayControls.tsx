// Bar-replay UI: a `useReplay` hook that owns the replay cursor/playback state
// and a small `ReplayBar` control strip. The heavy lifting (which bars to show)
// is done by App slicing its own candle array with replaySlice(); this file only
// tracks WHERE the cursor is and drives auto-advance. Pure math lives in replay.ts.
import { useCallback, useEffect, useState } from 'react'
import {
  REPLAY_SPEEDS,
  clampReplayCount,
  initialReplayCount,
  isReplayEnd,
  speedToIntervalMs,
  stepReplay,
} from './replay'

export interface ReplayState {
  active: boolean
  playing: boolean
  speed: number
  cursor: number // revealed-bar count (1..total) while active; == total when inactive
  total: number
  atEnd: boolean
  enter: () => void
  exit: () => void
  togglePlay: () => void
  stepFwd: () => void
  stepBack: () => void
  setSpeed: (s: number) => void
}

// `total` is the full number of real candles available. The hook keeps the cursor
// valid as that number changes (e.g. when the user switches symbol/timeframe).
export function useReplay(total: number): ReplayState {
  const [active, setActive] = useState(false)
  const [playing, setPlaying] = useState(false)
  const [speed, setSpeed] = useState<number>(1)
  const [count, setCount] = useState(0)

  const enter = useCallback(() => {
    setActive(true)
    setPlaying(false)
    setCount(initialReplayCount(total))
  }, [total])

  const exit = useCallback(() => {
    setActive(false)
    setPlaying(false)
  }, [])

  const togglePlay = useCallback(() => setPlaying((p) => !p), [])

  const stepFwd = useCallback(() => {
    setPlaying(false)
    setCount((c) => stepReplay(c, 1, total))
  }, [total])

  const stepBack = useCallback(() => {
    setPlaying(false)
    setCount((c) => stepReplay(c, -1, total))
  }, [total])

  // Keep the cursor in range if the underlying series shrinks/grows under us.
  useEffect(() => {
    if (active) setCount((c) => clampReplayCount(c, total))
  }, [active, total])

  // Auto-advance one bar per tick while playing. Implemented as a self-rescheduling
  // timeout keyed on `count`: each reveal re-runs this effect, which schedules the
  // next step, until every bar is revealed — then playback stops on its own.
  useEffect(() => {
    if (!active || !playing) return
    if (isReplayEnd(count, total)) {
      setPlaying(false)
      return
    }
    const ms = speedToIntervalMs(speed)
    if (ms <= 0) return
    const id = setTimeout(() => setCount((c) => stepReplay(c, 1, total)), ms)
    return () => clearTimeout(id)
  }, [active, playing, speed, total, count])

  const cursor = active ? clampReplayCount(count, total) : total
  return {
    active,
    playing,
    speed,
    cursor,
    total,
    atEnd: active && isReplayEnd(cursor, total),
    enter,
    exit,
    togglePlay,
    stepFwd,
    stepBack,
    setSpeed,
  }
}

// The control strip. Collapsed to a single "Replay" button until entered, then a
// transport bar (step back / play-pause / step forward), a progress readout and a
// speed picker. Disabled when there aren't enough bars to replay.
export function ReplayBar({ replay }: { replay: ReplayState }) {
  const canReplay = replay.total > 2

  if (!replay.active) {
    return (
      <button
        type="button"
        className="replay-enter"
        onClick={replay.enter}
        disabled={!canReplay}
        title={canReplay ? 'Bar replay — step through history one candle at a time' : 'Not enough history to replay'}
      >
        <span aria-hidden="true">⏮</span> Replay
      </button>
    )
  }

  return (
    <div className="replay-bar" role="group" aria-label="Bar replay controls">
      <button
        type="button"
        className="replay-btn"
        onClick={replay.stepBack}
        title="Step back one bar"
        aria-label="Step back one bar"
      >
        ⏴
      </button>
      <button
        type="button"
        className={'replay-btn replay-play' + (replay.playing ? ' is-playing' : '')}
        onClick={replay.togglePlay}
        disabled={replay.atEnd}
        title={replay.atEnd ? 'Reached the latest bar' : replay.playing ? 'Pause' : 'Play'}
        aria-label={replay.playing ? 'Pause replay' : 'Play replay'}
      >
        {replay.playing ? '⏸' : '⏵'}
      </button>
      <button
        type="button"
        className="replay-btn"
        onClick={replay.stepFwd}
        disabled={replay.atEnd}
        title="Step forward one bar"
        aria-label="Step forward one bar"
      >
        ⏵⏵
      </button>
      <span className="replay-count" title="Revealed bars / total bars">
        {replay.cursor}/{replay.total}
      </span>
      <label className="replay-speed">
        <span className="sr-only">Playback speed</span>
        <select
          value={replay.speed}
          onChange={(e) => replay.setSpeed(Number(e.target.value))}
          title="Playback speed (bars per second)"
          aria-label="Playback speed in bars per second"
        >
          {REPLAY_SPEEDS.map((s) => (
            <option key={s} value={s}>
              {s}×
            </option>
          ))}
        </select>
      </label>
      <button
        type="button"
        className="replay-btn replay-exit"
        onClick={replay.exit}
        title="Exit replay and return to live"
        aria-label="Exit replay"
      >
        ✕
      </button>
    </div>
  )
}
