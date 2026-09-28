"""Capital / money management for autonomous trading.

Sits ON TOP of the :class:`~app.risk.RiskManager`, never replacing it. The risk
manager answers "is this trade allowed, and what is the maximum SAFE size?"; the
money manager answers "of my run budget, how much should I actually deploy on
THIS trade right now, and how much should I hold back in reserve?" — the
disciplined, beginner-safe money behaviour the operator asked for:

  • a per-run budget (spend at most this much quote, e.g. $20 for the night);
  • partial deployment with a held reserve ("try with 5, hold the other 15") —
    each trade takes only a slice of what's still free, so capital is fed in
    gradually and there is always dry powder left;
  • outcome-based re-sizing — the slice grows a little after a winning streak and
    shrinks (faster) after losses, so a cold run automatically trades smaller;
  • a profit reserve — a slice of the day's realised profit is held OUT of the
    redeployable budget, so booked gains aren't immediately re-risked.

The per-trade max-hold *time-stop* lives in the engine's monitor (it needs live
price + the open-trade row); this module owns SIZING and the BUDGET accounting.

INVARIANT — safety by construction: the size returned is NEVER larger than the
risk manager's own risk-based size for the same trade (``risk_based_qty``). The
money manager can only deploy the SAME or LESS. So turning it on is always at
least as safe as leaving it off, and every RiskManager cap still binds on top.
Nothing here fabricates a number: the budget, the deployed figure and the day's
profit are all read from real settings and real closed/open trades.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Trade, TradeStatus
from app.risk import RiskManager


class MoneyManager:
    # Outcome re-sizing: grow slowly after wins, shrink FASTER after losses, and
    # clamp hard both ways so a streak can never run the size away.
    _WIN_STEP = 0.15
    _LOSS_STEP = 0.25
    _MULT_MIN = 0.5
    _MULT_MAX = 1.5
    _STREAK_LOOKBACK = 5

    def __init__(
        self, settings: Settings, user_id: int | None, risk: RiskManager
    ) -> None:
        self.settings = settings
        self.user_id = user_id
        self.risk = risk

    def update(self, settings: Settings) -> None:
        self.settings = settings

    # ---- settings (read live, safe defaults) ----------------------------
    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "capital_manager_enabled", True))

    @property
    def run_budget(self) -> float:
        return max(float(getattr(self.settings, "capital_run_budget_quote", 0.0) or 0.0), 0.0)

    @property
    def per_trade_pct(self) -> float:
        pct = float(getattr(self.settings, "capital_per_trade_pct", 25.0) or 0.0)
        return min(max(pct, 0.0), 100.0)

    @property
    def min_trade_quote(self) -> float:
        return max(float(getattr(self.settings, "capital_min_trade_quote", 5.0) or 0.0), 0.0)

    @property
    def resize(self) -> bool:
        return bool(getattr(self.settings, "capital_resize_on_outcome", True))

    @property
    def profit_reserve_pct(self) -> float:
        pct = float(getattr(self.settings, "capital_profit_reserve_pct", 0.0) or 0.0)
        return min(max(pct, 0.0), 100.0)

    @property
    def fixed_trade_quote(self) -> float:
        """Exact per-trade stake in quote terms (0 = off -> size by percentage)."""
        return max(float(getattr(self.settings, "capital_fixed_trade_quote", 0.0) or 0.0), 0.0)

    # ---- real book reads ------------------------------------------------
    def _scope_auto_open(self, stmt):
        stmt = stmt.where(
            Trade.source == "auto",
            Trade.mode == self.settings.trading_mode,
            Trade.status.in_([TradeStatus.open.value, TradeStatus.pending.value]),
        )
        if self.user_id is not None:
            stmt = stmt.where(Trade.user_id == self.user_id)
        return stmt

    def deployed_quote(self, db: Session) -> float:
        """Quote value of capital the autopilot has ALREADY put to work this run:
        the entry notional of open + pending AUTO positions (current mode)."""
        rows = db.scalars(self._scope_auto_open(select(Trade))).all()
        return float(sum((t.amount or 0.0) * (t.entry_price or 0.0) for t in rows))

    def outcome_multiplier(self, db: Session) -> float:
        """Size multiplier from the most recent AUTO outcomes (current mode).

        A run of wins nudges the slice up; a run of losses cuts it down faster.
        Reads real closed-trade P&L only — never invents a win rate.
        """
        stmt = (
            select(Trade)
            .where(
                Trade.source == "auto",
                Trade.mode == self.settings.trading_mode,
                Trade.status == TradeStatus.closed.value,
            )
            .order_by(Trade.closed_at.desc())
            .limit(self._STREAK_LOOKBACK)
        )
        if self.user_id is not None:
            stmt = stmt.where(Trade.user_id == self.user_id)
        recent = list(db.scalars(stmt).all())
        if not recent:
            return 1.0
        first = recent[0].pnl or 0.0
        if first > 0:  # winning streak from the latest backwards
            streak = 0
            for t in recent:
                if (t.pnl or 0.0) > 0:
                    streak += 1
                else:
                    break
            return min(1.0 + self._WIN_STEP * streak, self._MULT_MAX)
        if first < 0:  # losing streak
            streak = 0
            for t in recent:
                if (t.pnl or 0.0) < 0:
                    streak += 1
                else:
                    break
            return max(1.0 - self._LOSS_STEP * streak, self._MULT_MIN)
        return 1.0  # last trade broke even -> no adjustment

    def available_budget(self, db: Session, equity: float) -> float:
        """Quote still free to deploy this run, after reserve (and, for a fixed
        run budget, after what's already deployed).

        ``equity`` is FREE cash (it already excludes capital tied up in open
        positions), so we only subtract ``deployed`` when a FIXED run budget is
        set — otherwise we'd double-count it.
        """
        reserve = self.profit_reserve_pct / 100.0 * max(self.risk.day_realized_pnl(db), 0.0)
        if self.run_budget > 0:
            avail = self.run_budget - self.deployed_quote(db) - reserve
        else:
            avail = float(equity) - reserve
        return max(avail, 0.0)

    # ---- the decision ---------------------------------------------------
    def plan_amount(
        self, db: Session, *, equity: float, price: float, risk_based_qty: float
    ) -> float:
        """Base quantity to actually deploy on this entry (0 => hold / no budget).

        ``risk_based_qty`` is the risk manager's already-capped size; the result
        is ``min(risk_based_qty, budget_slice / price)`` — never larger.
        """
        if not self.enabled:
            return max(risk_based_qty, 0.0)
        if price <= 0 or risk_based_qty <= 0:
            return 0.0
        avail = self.available_budget(db, equity)
        fixed = self.fixed_trade_quote
        if fixed > 0:
            # EXACT stake mode: deploy `fixed` quote per trade (no percentage, no
            # outcome resize — a fixed stake stays fixed). Still capped by free
            # budget and (below) by the risk-based size, so it only ever deploys
            # this much OR LESS. If free budget can't even cover the dust floor,
            # hold; otherwise deploy the fixed stake or whatever's free, whichever
            # is smaller — never fabricating cash we don't have.
            if avail < min(fixed, self.min_trade_quote):
                return 0.0
            slice_quote = min(fixed, avail)
        else:
            if avail < self.min_trade_quote:
                return 0.0  # not enough free run-budget for a sane trade -> hold
            slice_quote = avail * (self.per_trade_pct / 100.0)
            if self.resize:
                slice_quote *= self.outcome_multiplier(db)
            # Keep the slice within [min_trade_quote, avail]: never a dust order, and
            # never more than what's actually free this run.
            slice_quote = max(self.min_trade_quote, min(slice_quote, avail))
        budget_qty = slice_quote / price
        return max(min(risk_based_qty, budget_qty), 0.0)
