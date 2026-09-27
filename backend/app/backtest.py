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
    # None when zero trades were taken (there is no rate to report), NOT 0.0 —
    # a fabricated 0% would read as "traded and lost every time". A genuine 0%
    # (traded, all losers) is still reported as 0.0.
    win_rate_pct: float | None
    max_drawdown_pct: float
    total_fees: float = 0.0
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    # True when the candles lacked separate open/high/low columns, so those were
    # substituted with the close. Intrabar stop-loss / take-profit / trailing
    # fills are then approximated on the close instead of the real bar range —
    # surfaced so the result is never silently less realistic than it looks.
    ohlc_synthetic: bool = False


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
    # If the frame carries no separate open/high/low we fall back to the close,
    # but we record that so the result can say the intrabar exits were
    # approximated rather than pretend the bar range was real.
    ohlc_synthetic = not ("open" in candles and "high" in candles and "low" in candles)
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
    # The final equity point must equal the realised ending balance: an open
    # position is liquidated above at a price net of exit fee + slippage, but the
    # in-loop mark-to-market wrote the raw last close with no exit cost. Overwrite
    # so equity_curve[-1] == ending_balance and the drawdown scan sees the true
    # final equity. No-op when nothing was open at the last bar.
    if equity_curve:
        equity_curve[-1] = ending_balance
    total_return = (ending_balance / starting_balance - 1) * 100 if starting_balance else 0.0
    wins = sum(1 for t in trades if t.pnl > 0)
    # None (not 0.0) when no trade was taken — see BacktestResult.win_rate_pct.
    win_rate = (wins / len(trades) * 100) if trades else None

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
        ohlc_synthetic=ohlc_synthetic,
    )


def _bar_seconds(candles: pd.DataFrame) -> float | None:
    """Median spacing between candle timestamps, in seconds. None when the frame
    has no usable timestamp column — we never guess a bar size."""
    if "timestamp" not in candles or len(candles) < 2:
        return None
    try:
        ts = candles["timestamp"].astype(float).tolist()
    except (ValueError, TypeError):
        return None
    diffs = [b - a for a, b in zip(ts, ts[1:]) if b > a]
    if not diffs:
        return None
    diffs.sort()
    mid = diffs[len(diffs) // 2]  # ccxt timestamps are milliseconds
    return round(mid / 1000.0, 3) if mid > 0 else None


def _human_duration(seconds: float | None) -> str:
    """'3h 20m', '2d 4h', '45s' — plain words for a non-trader. '' when unknown."""
    if not seconds or seconds <= 0:
        return ""
    s = int(round(seconds))
    days, rem = divmod(s, 86_400)
    hours, rem = divmod(rem, 3_600)
    mins, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {mins}m" if mins else f"{hours}h"
    if mins:
        return f"{mins}m {secs}s" if secs else f"{mins}m"
    return f"{secs}s"


def summarize_backtest(
    result: BacktestResult,
    candles: pd.DataFrame,
    *,
    timeframe: str | None = None,
) -> dict:
    """Real, explainable analytics computed ONLY from the backtest's own closed
    trades and equity curve — nothing is fabricated. Every figure traces back to a
    trade the strategy actually took or a price it actually would have paid.

    Fields that need data which may not exist are honest about it: ``profit_factor``
    is ``None`` when there were no losing trades (you cannot divide by zero losses),
    and ``avg_hold_seconds`` is ``None`` when the candles carry no usable timestamps.
    We never substitute a placeholder number for missing data.
    """
    trades = result.trades
    pnls = [float(t.pnl) for t in trades]
    win_pnls = [p for p in pnls if p > 0]
    loss_pnls = [p for p in pnls if p < 0]
    breakeven = sum(1 for p in pnls if p == 0)

    gross_profit = sum(win_pnls)
    gross_loss = -sum(loss_pnls)  # reported as a positive magnitude
    # profit_factor: only meaningful with at least one loss. None (not 0, not inf)
    # when nothing lost — the UI shows "-" rather than an invented ratio.
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    n = len(trades)
    avg_win = (gross_profit / len(win_pnls)) if win_pnls else 0.0
    avg_loss = (sum(loss_pnls) / len(loss_pnls)) if loss_pnls else 0.0  # negative
    avg_trade_pnl = (sum(pnls) / n) if n else 0.0  # expectancy, in currency
    largest_win = max(win_pnls) if win_pnls else 0.0
    largest_loss = min(loss_pnls) if loss_pnls else 0.0  # negative

    start_bal = result.starting_balance or 0.0
    expectancy_pct = (avg_trade_pnl / start_bal * 100.0) if start_bal else 0.0
    # Per-trade return relative to the cash actually put in on that trade.
    per_trade_returns = [
        (t.pnl / t.entry_cost * 100.0) for t in trades if t.entry_cost > 0
    ]
    avg_trade_return_pct = (
        sum(per_trade_returns) / len(per_trade_returns) if per_trade_returns else 0.0
    )

    # Holding time: bars between entry and exit, converted to wall-clock using the
    # frame's own bar spacing. Left as bar-count only when timestamps are missing.
    held_bars = [
        (t.exit_index - t.entry_index)
        for t in trades
        if t.exit_index is not None and t.entry_index is not None
    ]
    avg_bars_held = (sum(held_bars) / len(held_bars)) if held_bars else 0.0
    bar_secs = _bar_seconds(candles)
    avg_hold_seconds = round(avg_bars_held * bar_secs, 1) if bar_secs else None

    # Buy & hold benchmark: what a hands-off holder would have made just owning the
    # asset across the same window (first close -> last close). This is the honest
    # bar to clear — a strategy that trades a lot but trails buy & hold is usually
    # not worth the fees/risk.
    closes = candles["close"].astype(float).tolist() if "close" in candles else []
    buy_hold_pct: float | None = None
    if len(closes) >= 2 and closes[0] > 0:
        buy_hold_pct = (closes[-1] / closes[0] - 1) * 100.0
    vs_bh = (
        result.total_return_pct - buy_hold_pct if buy_hold_pct is not None else None
    )

    fees_pct_of_start = (
        (result.total_fees / start_bal * 100.0) if start_bal else 0.0
    )

    # ---- plain-language explanation (no jargon, no invented figures) -----
    bars = len(candles)
    tf = timeframe or "bars"
    lines: list[str] = []
    if n == 0:
        lines.append(
            f"Over {bars} {tf} candles this strategy never met its own entry "
            "conditions, so it placed no trades. There is nothing to judge yet — "
            "try a longer history or a different timeframe before trusting it."
        )
    else:
        verb = "made" if result.total_return_pct >= 0 else "lost"
        lines.append(
            f"Over {bars} {tf} candles the strategy took {n} "
            f"trade{'s' if n != 1 else ''} and {verb} "
            f"{abs(result.total_return_pct):.2f}% "
            f"(${result.ending_balance - start_bal:,.2f} on a "
            f"${start_bal:,.0f} stake)."
        )
        lines.append(
            f"It won {len(win_pnls)} and lost {len(loss_pnls)}"
            + (f" ({breakeven} scratch)" if breakeven else "")
            + f" — a {result.win_rate_pct:.0f}% win rate. "
            f"Average winner ${avg_win:,.2f}, average loser ${avg_loss:,.2f}."
        )
        if profit_factor is not None:
            pf_words = (
                "it made more than it lost"
                if profit_factor > 1
                else "it lost more than it made"
            )
            lines.append(
                f"Profit factor {profit_factor:.2f} — for every $1 lost it earned "
                f"${profit_factor:.2f}, so {pf_words}."
            )
        elif win_pnls:
            lines.append("No losing trades in this window, so there is no loss to divide against — treat that as too small a sample, not a sure thing.")
        avg_hold_txt = _human_duration(avg_hold_seconds)
        if avg_hold_txt:
            lines.append(f"A typical trade was held about {avg_hold_txt}.")
        if buy_hold_pct is not None:
            if vs_bh is not None and vs_bh >= 0:
                lines.append(
                    f"Simply holding the coin over the same window would have "
                    f"returned {buy_hold_pct:.2f}%, so the strategy beat buy & hold "
                    f"by {vs_bh:.2f} points."
                )
            else:
                lines.append(
                    f"Simply holding the coin would have returned "
                    f"{buy_hold_pct:.2f}% — the strategy trailed buy & hold by "
                    f"{abs(vs_bh):.2f} points, so the extra trading did not pay off "
                    "here."
                )
        lines.append(
            f"Fees cost ${result.total_fees:,.2f} ({fees_pct_of_start:.2f}% of the "
            "stake); the worst peak-to-trough dip along the way was "
            f"{result.max_drawdown_pct:.2f}%."
        )
        if result.ohlc_synthetic:
            lines.append(
                "Note: this history had no separate open/high/low prices, so any "
                "stop-loss or take-profit was checked against the closing price "
                "only. Real intrabar highs and lows could have triggered those "
                "exits differently — treat the exit timing as approximate."
            )

    return {
        "wins": len(win_pnls),
        "losses": len(loss_pnls),
        "breakeven": breakeven,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "avg_trade_pnl": round(avg_trade_pnl, 2),
        "expectancy_pct": round(expectancy_pct, 4),
        "avg_trade_return_pct": round(avg_trade_return_pct, 4),
        "largest_win": round(largest_win, 2),
        "largest_loss": round(largest_loss, 2),
        "avg_bars_held": round(avg_bars_held, 2),
        "avg_hold_seconds": avg_hold_seconds,
        "fees_pct_of_start": round(fees_pct_of_start, 4),
        "buy_hold_return_pct": round(buy_hold_pct, 2) if buy_hold_pct is not None else None,
        "vs_buy_hold_pct": round(vs_bh, 2) if vs_bh is not None else None,
        "beat_buy_hold": (vs_bh is not None and vs_bh >= 0),
        "profitable": result.total_return_pct > 0,
        "ohlc_synthetic": result.ohlc_synthetic,
        "bars": bars,
        "explanation": " ".join(lines),
    }
