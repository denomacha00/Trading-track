"""Backtester tests (deterministic, no network)."""
from __future__ import annotations

import pandas as pd

from app.backtest import run_backtest
from app.strategies import MovingAverageCrossStrategy


def _candles(prices: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": range(len(prices)),
            "open": prices,
            "high": prices,
            "low": prices,
            "close": prices,
            "volume": [1.0] * len(prices),
        }
    )


def test_backtest_profits_on_uptrend():
    # Dip then strong uptrend: MA cross should buy low and ride up.
    prices = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1] + list(range(2, 40))
    result = run_backtest(_candles(prices), MovingAverageCrossStrategy(3, 5),
                          starting_balance=1000, fee_pct=0.0, slippage_pct=0.0)
    assert result.num_trades >= 1
    assert result.ending_balance > result.starting_balance
    assert len(result.equity_curve) == len(prices)


def test_backtest_fees_and_slippage_reduce_return():
    prices = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1] + list(range(2, 40))
    clean = run_backtest(_candles(prices), MovingAverageCrossStrategy(3, 5),
                         starting_balance=1000, fee_pct=0.0, slippage_pct=0.0)
    costed = run_backtest(_candles(prices), MovingAverageCrossStrategy(3, 5),
                          starting_balance=1000, fee_pct=0.1, slippage_pct=0.1)
    # Costs must eat into the return and be reported.
    assert costed.ending_balance < clean.ending_balance
    assert costed.total_fees > 0.0


def test_backtest_no_lookahead_executes_next_open():
    # Signal computed on a bar's close must NOT fill at that same close.
    # Distinct open != close lets us verify the fill uses the NEXT bar's open.
    df = pd.DataFrame(
        {
            "timestamp": range(8),
            "open": [10, 10, 10, 10, 20, 30, 40, 50],
            "high": [10, 10, 10, 10, 20, 30, 40, 50],
            "low": [10, 10, 10, 10, 20, 30, 40, 50],
            "close": [10, 10, 10, 15, 25, 35, 45, 55],
            "volume": [1.0] * 8,
        }
    )
    result = run_backtest(df, MovingAverageCrossStrategy(2, 3),
                          starting_balance=1000, fee_pct=0.0, slippage_pct=0.0)
    # Any entry price must match some bar's OPEN, never the close that triggered it.
    for t in result.trades:
        assert t.entry_price in set(df["open"].tolist())


def test_backtest_no_trades_flat_market():
    prices = [100.0] * 30
    result = run_backtest(_candles(prices), MovingAverageCrossStrategy(3, 5),
                          starting_balance=1000)
    assert result.num_trades == 0
    assert result.ending_balance == 1000
    assert result.max_drawdown_pct == 0.0


def test_backtest_drawdown_non_negative():
    prices = [10, 20, 5, 25, 3, 30, 2, 35]
    result = run_backtest(_candles(prices), MovingAverageCrossStrategy(2, 3),
                          starting_balance=1000)
    assert result.max_drawdown_pct >= 0.0


# ---- resting exits: stop-loss / take-profit / trailing --------------


def test_stop_loss_caps_downside():
    # Rise (to trigger a long) then a hard crash. A stop-loss must exit early
    # and lose LESS than the same run with no stop riding the crash down.
    prices = [10, 10, 10, 10, 10, 11, 12, 13, 14, 15, 16, 8, 6, 4, 2]
    df = _candles(prices)
    no_stop = run_backtest(df, MovingAverageCrossStrategy(3, 5),
                           starting_balance=1000, fee_pct=0.0, slippage_pct=0.0)
    stopped = run_backtest(df, MovingAverageCrossStrategy(3, 5),
                           starting_balance=1000, fee_pct=0.0, slippage_pct=0.0,
                           stop_loss_pct=10.0)
    assert stopped.num_trades >= 1
    assert stopped.ending_balance > no_stop.ending_balance


def test_take_profit_locks_gain_on_spike():
    # Enter on the cross, spike above the +20% target, then collapse. The
    # take-profit should bank the gain instead of giving it back.
    prices = [10, 10, 10, 10, 10, 11, 12, 13, 14, 15, 30, 12, 11, 10, 9, 8]
    df = _candles(prices)
    none = run_backtest(df, MovingAverageCrossStrategy(3, 5), starting_balance=1000,
                        fee_pct=0.0, slippage_pct=0.0)
    tp = run_backtest(df, MovingAverageCrossStrategy(3, 5), starting_balance=1000,
                      fee_pct=0.0, slippage_pct=0.0, take_profit_pct=20.0)
    assert tp.num_trades >= 1
    assert tp.ending_balance > none.ending_balance


def test_trailing_stop_locks_in_after_peak():
    # Ride an uptrend to a peak, then pull back. A trailing stop should exit on
    # the pullback and beat holding through the round-trip back down.
    prices = [10, 10, 10, 10, 10, 11, 12, 14, 16, 18, 20, 18, 16, 14, 12, 10]
    df = _candles(prices)
    none = run_backtest(df, MovingAverageCrossStrategy(3, 5), starting_balance=1000,
                        fee_pct=0.0, slippage_pct=0.0)
    trailed = run_backtest(df, MovingAverageCrossStrategy(3, 5), starting_balance=1000,
                           fee_pct=0.0, slippage_pct=0.0, trailing_stop_pct=10.0)
    assert trailed.num_trades >= 1
    assert trailed.ending_balance > none.ending_balance


def test_exits_disabled_by_default_match_signal_only():
    # With all exit percentages at 0 the result must be identical to the plain
    # signal-only backtest (guards the back-compat default path).
    prices = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1] + list(range(2, 40))
    df = _candles(prices)
    base = run_backtest(df, MovingAverageCrossStrategy(3, 5), starting_balance=1000)
    zeroed = run_backtest(df, MovingAverageCrossStrategy(3, 5), starting_balance=1000,
                          stop_loss_pct=0.0, take_profit_pct=0.0, trailing_stop_pct=0.0)
    assert base.ending_balance == zeroed.ending_balance
    assert base.num_trades == zeroed.num_trades


def test_final_liquidation_pnl_includes_entry_fee():
    # A position held through the last bar (uptrend, no sell signal) is liquidated
    # at the final close. Its P&L must use entry_cost (spend INCLUDING the entry
    # fee) as the cost basis, exactly like the sell-signal and stop/TP exits — not
    # entry_price*qty, which drops the entry fee and overstates the trade. With a
    # non-zero fee, per-trade P&L must reconcile to the cent with the balance
    # change; the old buggy basis broke this by exactly the entry fee and could
    # flip a real losing trade into a counted "winner", inflating win_rate_pct.
    prices = [10, 9, 8, 7, 6, 5, 4, 3, 2, 1] + list(range(2, 40))
    result = run_backtest(_candles(prices), MovingAverageCrossStrategy(3, 5),
                          starting_balance=1000, fee_pct=0.1, slippage_pct=0.0)
    assert result.num_trades >= 1
    assert result.total_fees > 0.0
    # The last trade is the end-of-run liquidation (still open at the final bar).
    assert result.trades[-1].exit_index == len(prices) - 1
    reconciled = sum(t.pnl for t in result.trades)
    assert abs(reconciled - (result.ending_balance - result.starting_balance)) < 1e-6
