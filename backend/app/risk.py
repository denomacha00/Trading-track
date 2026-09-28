"""Risk management: pre-trade checks and position sizing."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Trade, TradeStatus


@dataclass
class RiskDecision:
    allowed: bool
    reason: str = ""
    amount: float = 0.0


class RiskManager:
    def __init__(self, settings: Settings, user_id: int | None = None) -> None:
        self.settings = settings
        self.user_id = user_id

    def update(self, settings: Settings) -> None:
        self.settings = settings

    def _scope(self, stmt):
        """Restrict a Trade query to this engine's user (multi-tenant isolation)."""
        if self.user_id is not None:
            stmt = stmt.where(Trade.user_id == self.user_id)
        return stmt

    def open_positions(self, db: Session) -> list[Trade]:
        # Pending limit orders count too: they reserve capital and a slot, so a
        # resting order must be included in position/exposure limits.
        stmt = self._scope(
            select(Trade).where(
                Trade.status.in_([TradeStatus.open.value, TradeStatus.pending.value])
            )
        )
        return list(db.scalars(stmt).all())

    def day_realized_pnl(self, db: Session) -> float:
        start = dt.datetime.now(dt.timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        # Scope to the CURRENT trading mode: paper and live are separate books,
        # so simulated losses must never trip the live daily-loss breaker (nor
        # the reverse). Without this filter a paper drawdown could halt live
        # trading, or live losses could be masked by paper wins.
        stmt = self._scope(
            select(Trade).where(
                Trade.status == TradeStatus.closed.value,
                Trade.closed_at >= start,
                Trade.mode == self.settings.trading_mode,
            )
        )
        return float(sum(t.pnl for t in db.scalars(stmt).all()))

    def size_position(
        self, equity: float, price: float, stop_fraction: float | None = None
    ) -> float:
        """Size a position from risk-per-trade % and the actual stop distance.

        Risk amount = equity * risk_per_trade_pct%.
        With a stop ``stop_fraction`` away (fraction of price), the position
        notional that risks exactly that amount is risk_amount / stop_fraction.
        When ``stop_fraction`` is omitted the configured ``default_stop_loss_pct``
        is used. Passing the *real* stop distance is important: sizing against
        the default while the order carries a wider stop would risk far more than
        risk_per_trade_pct% intends.
        """
        if price <= 0:
            return 0.0
        risk_amount = equity * (self.settings.risk_per_trade_pct / 100.0)
        if stop_fraction is None:
            stop_fraction = self.settings.default_stop_loss_pct / 100.0
        stop_fraction = max(stop_fraction, 1e-6)
        notional = risk_amount / stop_fraction
        # Never risk more notional than the equity itself.
        notional = min(notional, equity)
        return max(notional / price, 0.0)

    def check(
        self,
        db: Session,
        *,
        equity: float,
        price: float,
        requested_amount: float | None,
        is_opening: bool,
        stop_price: float | None = None,
        day_unrealized: float = 0.0,
        equity_for_limits: float | None = None,
        unattended: bool = False,
    ) -> RiskDecision:
        """Validate a prospective trade and return a sized decision.

        ``day_unrealized`` (optional) is the account's current open-position PnL.
        When supplied it is added to today's realized PnL for the daily-loss
        circuit breaker, so a large *unrealized* drawdown also halts new entries
        rather than letting losses compound until a stop fires.

        ``equity_for_limits`` (optional) is the TOTAL account equity (free cash +
        open-position value) used as the basis for the portfolio-level limits —
        the daily-loss breaker, the per-position concentration cap and the total-
        exposure cap. Position *sizing* still uses ``equity`` (free cash available
        to deploy). Separating them matters: as capital gets deployed the free
        cash shrinks, so basing these limits on free cash would tighten them the
        more you trade — the loss breaker would trip too early and the caps would
        mean different things at different deployment levels. Falls back to
        ``equity`` when not supplied, preserving the old behaviour for callers
        that don't pass it.
        """
        if price <= 0:
            return RiskDecision(False, "Invalid price")

        # Basis for every PORTFOLIO-level limit (daily-loss breaker, concentration
        # cap, total-exposure cap): TOTAL account equity, not the shrinking
        # free-cash figure used for sizing. See the docstring for why.
        basis = equity_for_limits if equity_for_limits is not None else equity

        if is_opening:
            open_trades = self.open_positions(db)
            if len(open_trades) >= self.settings.max_open_positions:
                return RiskDecision(
                    False,
                    f"Max open positions reached ({self.settings.max_open_positions})",
                )

            # Daily loss limit (loss is negative pnl). Include open drawdown so
            # the breaker reflects TOTAL current risk, not just closed trades.
            # The limit is a % of total account equity (see basis), not the
            # shrinking free-cash figure used for sizing.
            day_pnl = self.day_realized_pnl(db) + day_unrealized
            loss_limit = -abs(basis * (self.settings.daily_loss_limit_pct / 100.0))
            if day_pnl <= loss_limit:
                return RiskDecision(
                    False,
                    f"Daily loss limit hit (day PnL {day_pnl:.2f} <= {loss_limit:.2f})",
                )

        if requested_amount:
            amount = requested_amount
        else:
            # Size against the ACTUAL stop distance when a stop was supplied, so
            # a wider-than-default stop doesn't silently risk more than the
            # configured risk_per_trade_pct intends.
            stop_fraction = None
            if stop_price and stop_price > 0 and price > 0:
                stop_fraction = abs(price - stop_price) / price
            amount = self.size_position(equity, price, stop_fraction)
        if amount <= 0:
            return RiskDecision(False, "Computed position size is zero")

        notional = amount * price

        # Per-position CONCENTRATION cap (the primary blow-up guard). No single
        # position may exceed max_position_pct% of TOTAL equity.
        #  • AUTO-sizer (no explicit amount): clamp down to the cap — the bot chose
        #    the size, so shrinking it is honest (caps only ever reduce risk).
        #  • UNATTENDED explicit amount (autonomous decision / TradingView alert):
        #    refuse it rather than trade a size the sender didn't ask for OR let an
        #    automated feed quietly breach the blow-up guard.
        #  • MANUAL explicit amount: a deliberate human action — honoured here
        #    (still bounded by free cash and the exposure cap below).
        max_pos_pct = getattr(self.settings, "max_position_pct", 0.0)
        if is_opening and max_pos_pct > 0 and basis > 0:
            pos_cap = basis * (max_pos_pct / 100.0)
            if notional > pos_cap:
                if not requested_amount:
                    amount = pos_cap / price
                    notional = amount * price
                    if amount <= 0:
                        return RiskDecision(False, "Computed position size is zero")
                elif unattended:
                    return RiskDecision(
                        False,
                        f"Order notional {notional:.2f} exceeds the per-position "
                        f"cap {pos_cap:.2f} ({max_pos_pct:.0f}% of equity). Reduce "
                        f"the size or raise max_position_pct.",
                    )

        if is_opening and notional > equity:
            return RiskDecision(
                False,
                f"Order notional {notional:.2f} exceeds available equity {equity:.2f}",
            )

        # Portfolio-level exposure cap: total open notional + this order must stay
        # under max_total_exposure_pct% of TOTAL equity. Prevents many small
        # positions from quietly stacking into an oversized, correlated book.
        # Measured against total equity (not free cash) so the ceiling is stable
        # as capital gets deployed.
        max_exposure_pct = getattr(self.settings, "max_total_exposure_pct", 0.0)
        if is_opening and max_exposure_pct > 0:
            open_notional = sum(
                t.amount * t.entry_price for t in self.open_positions(db)
            )
            cap = basis * (max_exposure_pct / 100.0)
            if open_notional + notional > cap:
                return RiskDecision(
                    False,
                    f"Total exposure {open_notional + notional:.2f} would exceed "
                    f"cap {cap:.2f} ({max_exposure_pct:.0f}% of equity)",
                )

        return RiskDecision(True, "ok", amount)
