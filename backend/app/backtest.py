"""Simple event-driven backtester for the built-in strategies.

Realism model:
- Entries/exits fill on the NEXT bar's open (no look-ahead: you can't trade on a
  bar's close using a signal computed from that same close).
- A per-side fee (taker by default) is charged on entry and exit.
- Slippage widens the fill against you (buy fills higher, sell fills lower),
  approximating spread + market impact. Backtest/training numbers are therefore
  conservative rather than optimistic.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from app.strategies import Strategy


@dataclass
class BacktestTrade:
    entry_index: int
    entry_price: float
    # Total cash spent to OPEN the position, INCLUDING the entry fee. P&L is
    # (exit proceeds - entry_cost); using entry_price * qty instead would silently
    # drop the entry fee and overstate every trade's profit by that fee.
    entry_cost: float = 0.0
    exit_index: int | None = None
    exit_price: float | None = None
    pnl: float = 0.0


@dataclass
class BacktestResult:
    starting_balance: float
    ending_balance: float
    total_return_pct: float
    num_trades: int
    win_rate_pct: float
    max_drawdown_pct: float
    total_fees: float = 0.0
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)


def run_backtest(
    candles: pd.DataFrame,
    strategy: Strategy,
    *,
    starting_balance: float = 10_000.0,
    fee_pct: float = 0.1,
    slippage_pct: float = 0.05,
    stop_loss_pct: float = 0.0,
    take_profit_pct: float = 0.0,
    trailing_stop_pct: float = 0.0,
) -> BacktestResult:
    """Long-only backtest: full-equity entry on buy, exit on sell.

    candles: DataFrame with columns timestamp, open, high, low, close, volume.
    fee_pct: fee applied on each side of a trade (percent of notional).
    slippage_pct: adverse price movement applied to each fill (percent).

    Signals are generated on bar i's close and executed at bar i+1's open to
    avoid look-ahead bias.

    stop_loss_pct / take_profit_pct / trailing_stop_pct model the SAME resting
    exits the live engine places, so a backtest reflects how the bot actually
    trades rather than an idealised buy-and-hold-until-sell-signal run. They are
    resting orders, so they fill INTRABAR on the triggering bar (using its high/
    low), not at the next open. All default to 0 (disabled) to preserve the
    plain signal-only behaviour. When both a stop and target are touched on the
    same bar we assume the stop filled first (worst case). A gap through a level
    fills at that bar's open; stop fills also take adverse slippage (they are
    market orders) while take-profit limit fills do not.
    """
    balance = starting_balance
    position_qty = 0.0
    fee = fee_pct / 100.0
    slip = slippage_pct / 100.0
    sl = max(stop_loss_pct, 0.0) / 100.0
    tp = max(take_profit_pct, 0.0) / 100.0
    trail = max(trailing_stop_pct, 0.0) / 100.0
    exits_enabled = sl > 0 or tp > 0 or trail > 0
    peak_price = 0.0  # highest price since entry, drives the trailing stop
    trades: list[BacktestTrade] = []
    equity_curve: list[float] = []
    current_trade: BacktestTrade | None = None
    total_fees = 0.0
    pending: str | None = None  # action queued from the previous bar's signal

    n = len(candles)
    min_bars = strategy.min_bars()
    closes = candles["close"].astype(float).tolist()
    opens = candles["open"].astype(float).tolist() if "open" in candles else closes
    highs = candles["high"].astype(float).tolist() if "high" in candles else closes
    lows = candles["low"].astype(float).tolist() if "low" in candles else closes

    for i in range(n):
        price = closes[i]
        fill_price = opens[i]  # execute queued orders at this bar's open

        # --- execute an order queued on the previous bar ---------------
        if pending == "buy" and position_qty == 0.0:
            buy_price = fill_price * (1 + slip)  # pay up (adverse)
            spend = balance
            fee_paid = spend * fee
            position_qty = (spend - fee_paid) / buy_price
            total_fees += fee_paid
            balance = 0.0
            current_trade = BacktestTrade(
                entry_index=i, entry_price=buy_price, entry_cost=spend
            )
            peak_price = buy_price
        elif pending == "sell" and position_qty > 0.0:
            sell_price = fill_price * (1 - slip)  # receive less (adverse)
            gross = position_qty * sell_price
            fee_paid = gross * fee
            total_fees += fee_paid
            proceeds = gross - fee_paid
            if current_trade is not None:
                current_trade.exit_index = i
                current_trade.exit_price = sell_price
                current_trade.pnl = proceeds - current_trade.entry_cost
                trades.append(current_trade)
                current_trade = None
            balance = proceeds
            position_qty = 0.0
        pending = None

        # --- resting stop-loss / take-profit / trailing exits ----------
        # These orders rest AT the exchange from the moment we entered, so they
        # can fill intrabar on this same bar (using its high/low). Not
        # look-ahead: the levels were fixed on entry, not on this close.
        if exits_enabled and position_qty > 0.0 and current_trade is not None:
            entry = current_trade.entry_price
            stop_level = entry * (1 - sl) if sl > 0 else 0.0
            if trail > 0:
                stop_level = max(stop_level, peak_price * (1 - trail))
            tp_level = entry * (1 + tp) if tp > 0 else 0.0
            exit_price: float | None = None
            if stop_level > 0 and lows[i] <= stop_level:
                # A gap through the stop fills at the open; stops are market
                # orders, so they also eat adverse slippage.
                exit_price = min(fill_price, stop_level) * (1 - slip)
            elif tp_level > 0 and highs[i] >= tp_level:
                # A gap up fills better than the target; limit fill, no slippage.
                exit_price = max(fill_price, tp_level)
            if exit_price is not None:
                gross = position_qty * exit_price
                fee_paid = gross * fee
                total_fees += fee_paid
                proceeds = gross - fee_paid
                current_trade.exit_index = i
                current_trade.exit_price = exit_price
                current_trade.pnl = proceeds - current_trade.entry_cost
                trades.append(current_trade)
                current_trade = None
                balance = proceeds
                position_qty = 0.0
            else:
                peak_price = max(peak_price, highs[i])  # ratchet for next bar

        # --- generate a signal to execute on the NEXT bar --------------
        if i + 1 >= min_bars:
            signal = strategy.generate(candles.iloc[: i + 1])
            if signal.action == "buy" and position_qty == 0.0:
                pending = "buy"
            elif signal.action == "sell" and position_qty > 0.0:
                pending = "sell"

        equity = balance + position_qty * price
        equity_curve.append(equity)

    # Liquidate any open position at the last close for final equity.
    if position_qty > 0.0:
        last_price = closes[-1] * (1 - slip)
        gross = position_qty * last_price
        fee_paid = gross * fee
        total_fees += fee_paid
        proceeds = gross - fee_paid
        if current_trade is not None:
            current_trade.exit_index = n - 1
            current_trade.exit_price = last_price
            # Cost basis is entry_cost (spend incl. entry fee), same as the
            # sell-signal and stop/TP exits above — NOT entry_price * qty, which
            # would drop the entry fee and overstate this trade's profit.
            current_trade.pnl = proceeds - current_trade.entry_cost
            trades.append(current_trade)
        balance = proceeds
        position_qty = 0.0

    ending_balance = balance
    total_return = (ending_balance / starting_balance - 1) * 100 if starting_balance else 0.0
    wins = sum(1 for t in trades if t.pnl > 0)
    win_rate = (wins / len(trades) * 100) if trades else 0.0

    peak = float("-inf")
    max_dd = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            dd = (peak - eq) / peak * 100
            max_dd = max(max_dd, dd)

    return BacktestResult(
        starting_balance=starting_balance,
        ending_balance=ending_balance,
        total_return_pct=total_return,
        num_trades=len(trades),
        win_rate_pct=win_rate,
        max_drawdown_pct=max_dd,
        total_fees=round(total_fees, 2),
        trades=trades,
        equity_curve=equity_curve,
    )
