"""Learning / training: optimise strategy parameters from historical data.

This is how you "teach" Tranding-track: give it a symbol + strategy, and it
searches the parameter space by backtesting every combination on real candles,
then reports the best-performing configuration (and can save it as the active
strategy config). This is a transparent, deterministic optimiser — no black box.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any

import pandas as pd

from app.backtest import run_backtest
from app.strategies import STRATEGY_REGISTRY, Strategy


# Parameter grids to explore per strategy. Kept modest so a training run stays
# responsive; users can widen these later.
PARAM_GRIDS: dict[str, dict[str, list[Any]]] = {
    "ma_cross": {
        "fast": [5, 9, 12, 20],
        "slow": [21, 30, 50, 100],
    },
    "rsi": {
        "period": [7, 14, 21],
        "oversold": [20, 25, 30],
        "overbought": [70, 75, 80],
    },
}


@dataclass
class TrainingCandidate:
    params: dict[str, Any]
    total_return_pct: float
    win_rate_pct: float
    max_drawdown_pct: float
    num_trades: int
    score: float
    # Out-of-sample (validation) performance, when a holdout split is used.
    # None means the candidate was evaluated on the full dataset only. All four
    # OOS figures are captured together so the auto-trade gate can judge a saved
    # strategy on its held-out behaviour, never the optimistic in-sample fit.
    validation_return_pct: float | None = None
    validation_num_trades: int | None = None
    validation_win_rate_pct: float | None = None
    validation_max_drawdown_pct: float | None = None
    overfit_gap_pct: float | None = None


@dataclass
class TrainingReport:
    symbol: str
    strategy: str
    timeframe: str
    candles: int
    tested: int
    best: TrainingCandidate | None
    leaderboard: list[TrainingCandidate] = field(default_factory=list)
    # Fraction of data used for in-sample fitting (rest is holdout validation).
    train_fraction: float = 1.0
    warning: str | None = None


def _valid_params(strategy: str, params: dict[str, Any]) -> bool:
    """Reject nonsensical combinations before instantiating a strategy."""
    if strategy == "ma_cross":
        return params["fast"] < params["slow"]
    if strategy == "rsi":
        return params["oversold"] < params["overbought"]
    return True


def _build(strategy: str, params: dict[str, Any]) -> Strategy:
    return STRATEGY_REGISTRY[strategy](**params)


def _score(total_return_pct: float, max_drawdown_pct: float, num_trades: int) -> float:
    """Reward return, penalise drawdown, and require a minimum of activity.

    A config that never trades (num_trades == 0) scores strictly below any that
    actually trades, so "do nothing" can't win by having zero drawdown.
    """
    if num_trades == 0:
        return float("-inf")
    # Return per unit of risk taken (drawdown), with a small floor on drawdown.
    return total_return_pct - 0.5 * max_drawdown_pct


def _overfit_gap(
    in_sample_return_pct: float,
    in_sample_bars: int,
    oos_return_pct: float,
    oos_bars: int,
) -> float | None:
    """The overfit gap measured on a LIKE-FOR-LIKE window.

    The in-sample and out-of-sample periods are different lengths (the split is
    chronological, e.g. 70/30), so their raw *total* returns are NOT comparable:
    the longer in-sample window compounds over more bars and posts a bigger total
    even when the per-bar performance is identical — subtracting the two totals
    would fake an overfit gap out of nothing. Instead take the in-sample GEOMETRIC
    per-bar growth, project it over the OOS window's own length, and compare that
    to the actual OOS return. Positive => the strategy underperformed its
    in-sample pace out-of-sample (the real overfitting signal), now measured on
    equal ground and in the same units as ``validation_return_pct``.

    Returns None when it can't be computed honestly — either window empty, or an
    in-sample total loss (growth factor <= 0) where a real per-bar root doesn't
    exist. Never a fabricated 0.
    """
    if in_sample_bars <= 0 or oos_bars <= 0:
        return None
    growth = 1.0 + in_sample_return_pct / 100.0
    if growth <= 0:
        return None
    per_bar = growth ** (1.0 / in_sample_bars)
    projected_oos_pct = (per_bar ** oos_bars - 1.0) * 100.0
    return projected_oos_pct - oos_return_pct


def train(
    candles: pd.DataFrame,
    strategy: str,
    *,
    symbol: str = "",
    timeframe: str = "",
    starting_balance: float = 10_000.0,
    fee_pct: float = 0.1,
    slippage_pct: float = 0.05,
    stop_loss_pct: float = 0.0,
    take_profit_pct: float = 0.0,
    trailing_stop_pct: float = 0.0,
    top_n: int = 5,
    train_fraction: float = 0.7,
) -> TrainingReport:
    """Grid-search the strategy's parameters over the given candles.

    To guard against overfitting, the data is split chronologically: parameters
    are fitted and ranked on the first ``train_fraction`` of candles (in-sample),
    then the winner is re-tested on the untouched remainder (out-of-sample). A
    config that looks great in-sample but falls apart out-of-sample is overfit,
    and we surface that gap instead of hiding it. Set ``train_fraction=1.0`` to
    disable the split (e.g. when there isn't enough data).
    """
    if strategy not in STRATEGY_REGISTRY:
        raise ValueError(f"Unknown strategy '{strategy}'")
    if strategy not in PARAM_GRIDS:
        raise ValueError(f"No training grid defined for '{strategy}'")

    train_fraction = min(max(train_fraction, 0.1), 1.0)
    n = len(candles)
    warning: str | None = None
    # Need enough bars on BOTH sides for a split to be meaningful.
    use_split = train_fraction < 1.0 and n >= 120
    if train_fraction < 1.0 and not use_split:
        warning = (
            "Not enough candles for a train/validation split; "
            "optimised on the full dataset (higher overfitting risk)."
        )
    if use_split:
        cut = int(n * train_fraction)
        train_df = candles.iloc[:cut].reset_index(drop=True)
        valid_df = candles.iloc[cut:].reset_index(drop=True)
    else:
        train_df = candles
        valid_df = None

    grid = PARAM_GRIDS[strategy]
    keys = list(grid.keys())
    candidates: list[TrainingCandidate] = []
    tested = 0

    for combo in product(*(grid[k] for k in keys)):
        params = dict(zip(keys, combo))
        if not _valid_params(strategy, params):
            continue
        tested += 1
        strat = _build(strategy, params)
        result = run_backtest(
            train_df, strat, starting_balance=starting_balance,
            fee_pct=fee_pct, slippage_pct=slippage_pct,
            stop_loss_pct=stop_loss_pct, take_profit_pct=take_profit_pct,
            trailing_stop_pct=trailing_stop_pct,
        )
        candidate = TrainingCandidate(
            params=params,
            total_return_pct=round(result.total_return_pct, 2),
            win_rate_pct=round(result.win_rate_pct, 2),
            max_drawdown_pct=round(result.max_drawdown_pct, 2),
            num_trades=result.num_trades,
            score=round(
                _score(
                    result.total_return_pct,
                    result.max_drawdown_pct,
                    result.num_trades,
                ),
                2,
            ),
        )
        # Evaluate the same params on the untouched holdout period.
        if valid_df is not None:
            v = run_backtest(
                valid_df,
                _build(strategy, params),
                starting_balance=starting_balance,
                fee_pct=fee_pct,
                slippage_pct=slippage_pct,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                trailing_stop_pct=trailing_stop_pct,
            )
            candidate.validation_return_pct = round(v.total_return_pct, 2)
            candidate.validation_num_trades = v.num_trades
            candidate.validation_win_rate_pct = round(v.win_rate_pct, 2)
            candidate.validation_max_drawdown_pct = round(v.max_drawdown_pct, 2)
            # Like-for-like: project the in-sample per-bar pace over the OOS
            # window instead of subtracting totals from unequal-length windows.
            gap = _overfit_gap(
                result.total_return_pct, len(train_df),
                v.total_return_pct, len(valid_df),
            )
            candidate.overfit_gap_pct = round(gap, 2) if gap is not None else None
        candidates.append(candidate)

    candidates.sort(key=lambda c: c.score, reverse=True)
    # Drop candidates that never traded from the "best" pick, but keep them out
    # of the leaderboard entirely to avoid confusing -inf scores.
    tradeable = [c for c in candidates if c.num_trades > 0]
    best = tradeable[0] if tradeable else None

    if best and best.validation_num_trades == 0 and valid_df is not None:
        warning = (
            "Best in-sample config did not trade in the validation period — "
            "treat it with caution before going live."
        )

    return TrainingReport(
        symbol=symbol,
        strategy=strategy,
        timeframe=timeframe,
        candles=n,
        tested=tested,
        best=best,
        leaderboard=tradeable[:top_n],
        train_fraction=train_fraction if use_split else 1.0,
        warning=warning,
    )
