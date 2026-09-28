"""The trading engine: executes signals (from TradingView or manual) with risk
checks, supports paper and live modes, tracks positions and PnL, and broadcasts
state to connected dashboard clients.

Thread-safety: order execution is guarded by a lock because signals can arrive
concurrently (webhook + manual + monitor loop).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import threading
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.exchange import BinanceConnector
from app.analysis import MarketAnalyzer
from app.ai import AICommentator
from app.models import SignalLog, Trade, TradeStatus
from app.risk import RiskManager
from app.notifier import Notifier
from app.strategies import build_strategy
from app.state import (
    load_engine_runtime,
    load_paper_balance,
    load_settings_overrides,
    load_strategy_configs,
    save_engine_runtime,
    save_paper_balance,
    save_settings_overrides,
    save_strategy_configs,
)

logger = logging.getLogger(__name__)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class TradingEngine:
    """Coordinates market data, risk, execution and position bookkeeping."""

    def __init__(self, settings: Settings, connector: BinanceConnector,
                 user_id: int | None = None) -> None:
        self.settings = settings
        self.connector = connector
        self.user_id = user_id
        self.risk = RiskManager(settings, user_id=user_id)
        self.analyzer = MarketAnalyzer(min_confidence=settings.min_signal_confidence)
        self.notifier = Notifier(settings)
        self.ai = AICommentator(settings)
        self._lock = threading.Lock()
        self.running = False
        # Last autonomous verdict per symbol, so a signal row is logged only when
        # the brain's decision CHANGES (not an identical row every ~5s tick).
        self._last_auto_verdict: dict[str, str] = {}
        # Last market-regime snapshot per symbol (bull/bear/neutral + whether new
        # longs are paused and why), for the visible "stand aside in a bad market,
        # step back in when it's good" status. Updated on each monitor analysis.
        self._last_regime: dict[str, dict[str, Any]] = {}
        # Per-open-trade count of CONSECUTIVE bearish reads while in profit, so the
        # early "red-flag" exit fires only after the reversal persists (anti-whipsaw)
        # rather than on a single noisy tick. Keyed by trade id; reset when the flag
        # clears or the trade closes. In-memory: a restart re-arms it harmlessly.
        self._reversal_flags: dict[int, int] = {}
        # ---- risk-safeguard state ----
        # Peak TOTAL equity (free cash + open-position value) seen so far, for the
        # max-drawdown kill-switch. Seeded on the first drawdown check and, once
        # persisted, RESTORED on restart (see restore_state) so a rebuild never
        # resets the drawdown baseline to a lower value and blinds the safety net.
        self._peak_equity: float = 0.0
        # True once the drawdown kill-switch has fired. While set, ALL new entries
        # are blocked (open positions keep their stops). DURABLE: a trip survives a
        # restart/redeploy and requires an explicit human re-arm (reset_killswitch
        # via POST /api/bot/start) — it is never silently cleared by a rebuild.
        self._killswitch_tripped: bool = False
        # Whether restore_state found a persisted runtime blob for this account.
        # The manager uses this to decide the default run/stop for a FRESH engine
        # (no history -> default running) without ever overriding a persisted stop
        # or a tripped kill-switch.
        self._runtime_restored: bool = False
        # Last equity peak actually written to the KV store, so the monitor tick
        # can throttle peak persistence (only write on a materially higher peak).
        self._last_persisted_peak: float = 0.0
        # Per-symbol time of the last LOSING exit, for the re-entry cooldown.
        self._last_loss_exit: dict[str, dt.datetime] = {}
        # Symbols whose price feed is currently unreachable during monitoring, so
        # a "protection degraded" alert is emitted once per outage, not every tick.
        self._monitor_degraded: set[str] = set()
        # Paper wallet (quote currency, e.g. USDT).
        self.paper_balance = settings.paper_starting_balance
        # Trained strategies the user saved, keyed by uppercase SYMBOL. Loaded in
        # restore_state; used to OVERRIDE the analyzer verdict when the user opts
        # in via settings.use_saved_strategy (see analyze_symbol).
        self.strategy_configs: dict[str, dict[str, Any]] = {}
        # Event broadcaster set by the app on startup.
        self._broadcaster: Optional[Any] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    def _scope(self, stmt):
        """Restrict a Trade query to this engine's user (multi-tenant isolation).

        In single-tenant mode (user_id is None) queries are unscoped, preserving
        the original global behaviour used by the tests.
        """
        if self.user_id is not None:
            stmt = stmt.where(Trade.user_id == self.user_id)
        return stmt

    def _scope_book(self, stmt):
        """Scope a Trade query to this engine's user AND its CURRENT trading mode.

        Paper and live are SEPARATE books that coexist in the same table (``mode``
        is stored per trade). Any query about the *current* book — open positions,
        resting orders, this-mode equity/realized-PnL, the SL/TP monitor — must
        exclude the other mode. Otherwise leftover open PAPER positions would
        inflate live equity, count against live position/exposure limits, or (the
        dangerous one) be handed to the live SL/TP monitor and closed with a REAL
        exchange order. History/analytics that intentionally span modes (e.g. the
        performance report) use :meth:`_scope` instead.
        """
        return self._scope(stmt).where(Trade.mode == self.settings.trading_mode)

    # ---- wiring ------------------------------------------------------

    def attach_broadcaster(self, broadcaster: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._broadcaster = broadcaster
        self._loop = loop

    def restore_state(self, db: Session) -> None:
        """Load persisted settings overrides + paper balance on startup.

        Called once at app start so a restart doesn't silently reset trading
        mode, auto-trade flags, risk params or the simulated wallet.
        """
        overrides = load_settings_overrides(db, self.user_id)
        if overrides:
            for key, value in overrides.items():
                if hasattr(self.settings, key):
                    setattr(self.settings, key, value)
            self.risk.update(self.settings)
            self.connector.reload(self.settings)
            self.analyzer.min_confidence = self.settings.min_signal_confidence
        # Paper wallet: restore or seed from configured starting balance.
        self.paper_balance = load_paper_balance(
            db, self.settings.paper_starting_balance, self.user_id
        )
        # Trained strategies saved to this account, so "train once, trade with it"
        # survives restarts instead of being lost when the request returned.
        self.strategy_configs = load_strategy_configs(db, self.user_id)
        # Durable run/stop + kill-switch + drawdown baseline. A halted or stopped
        # bot MUST stay that way across a restart/redeploy/rebuild: never
        # auto-resume into the drawdown that tripped the kill-switch, and never
        # forget the equity peak the drawdown is measured against. When nothing has
        # been persisted yet (a brand-new engine), we leave running at its __init__
        # default (False) and let the manager pick the fresh-engine default.
        runtime = load_engine_runtime(db, self.user_id)
        if runtime:
            self._runtime_restored = True
            self._peak_equity = float(runtime.get("peak_equity", 0.0) or 0.0)
            self._last_persisted_peak = self._peak_equity
            self._killswitch_tripped = bool(runtime.get("killswitch_tripped", False))
            self.running = bool(runtime.get("running", False))
        # A tripped kill-switch NEVER auto-resumes — it demands an explicit human
        # re-arm (reset_killswitch) whatever the persisted/default running flag says.
        if self._killswitch_tripped:
            self.running = False
        self._reconcile_live_positions(db)

    def _reconcile_live_positions(self, db: Session) -> None:
        """On startup in live mode, heal DB/exchange disagreements.

        SL/TP monitoring trusts the DB as the source of truth. If the bot was
        down while a position changed on the exchange (manual trade, liquidation,
        a stop that fired), the two can diverge and the bot would otherwise keep
        managing a position that no longer exists. We take the EXCHANGE as ground
        truth and auto-heal: when the exchange no longer holds enough of the base
        asset to back an open long, we mark that DB trade closed (at the last
        known price) so the bot stops acting on a stale position. Every heal is
        logged and broadcast so the operator can see what happened.
        """
        if not self.settings.is_live or not self.connector.has_credentials:
            return
        open_trades = list(
            db.scalars(
                self._scope_book(
                    select(Trade).where(Trade.status == TradeStatus.open.value)
                )
            ).all()
        )
        if not open_trades:
            return
        try:
            base_balances = self.connector.fetch_position_amounts()
        except Exception as exc:
            logger.warning("position reconciliation skipped: %s", exc)
            return
        for t in open_trades:
            base = t.symbol.split("/")[0]
            held = base_balances.get(base, 0.0)
            if t.side == "buy" and held + 1e-9 < t.amount:
                # Exchange holds less than our DB long expects. If essentially
                # nothing is held, the position is gone — auto-close the record.
                logger.warning(
                    "Reconciliation: DB shows open long %s %s but exchange holds "
                    "only %s — position changed while the bot was down; healing.",
                    t.amount, t.symbol, held,
                )
                if held <= max(t.amount * 0.01, 1e-8):
                    # Cancel any orphaned protective stop, then close the record.
                    if t.stop_order_id:
                        self.connector.cancel_order(t.stop_order_id, t.symbol)
                        t.stop_order_id = None
                    price = self._price(t.symbol, fallback=t.entry_price)
                    t.exit_price = price
                    t.pnl = self._realized_pnl(t, price)
                    t.status = TradeStatus.closed.value
                    t.closed_at = _utcnow()
                    t.note = (t.note + " | " if t.note else "") + (
                        "auto-reconciled: exchange no longer holds this position"
                    )
                    db.commit()
                    self._emit(
                        "reconcile_closed",
                        {"symbol": t.symbol, "db_amount": t.amount,
                         "exchange_amount": held, "pnl": t.pnl},
                    )
                    self._notify(
                        f"\u2699\ufe0f Reconciled {t.symbol}: exchange no longer holds it; "
                        f"closed stale record (PnL {t.pnl:.2f})."
                    )
                else:
                    # Partial mismatch: shrink the DB amount to what's actually
                    # held rather than closing, and warn.
                    old = t.amount
                    t.amount = held
                    db.commit()
                    self._emit(
                        "reconcile_adjusted",
                        {"symbol": t.symbol, "db_amount": old,
                         "exchange_amount": held},
                    )
                    self._notify(
                        f"\u2699\ufe0f Reconciled {t.symbol}: adjusted tracked amount "
                        f"{old} → {held} to match the exchange."
                    )

    def persist_settings(self, db: Session, overrides: dict[str, Any]) -> None:
        """Merge and persist settings overrides so they survive restarts."""
        current = load_settings_overrides(db, self.user_id)
        current.update(overrides)
        save_settings_overrides(db, current, self.user_id)

    def apply_settings(self, settings: Settings) -> None:
        self.settings = settings
        self.risk.update(settings)
        self.connector.reload(settings)
        self.analyzer.min_confidence = settings.min_signal_confidence
        self.notifier.reload(settings)
        self.ai.reload(settings)

    # ---- saved strategies (train once, let the bot trade it) ---------

    def set_strategy_config(
        self, db: Session, symbol: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        """Save (or replace) the active trained strategy for a symbol.

        Updates the in-memory engine AND persists, mirroring persist/apply for
        settings, so the running bot trades the new strategy immediately and a
        restart keeps it.
        """
        sym = symbol.upper()
        self.strategy_configs[sym] = config
        save_strategy_configs(db, self.strategy_configs, self.user_id)
        return config

    def remove_strategy_config(self, db: Session, symbol: str) -> bool:
        sym = symbol.upper()
        if sym in self.strategy_configs:
            del self.strategy_configs[sym]
            save_strategy_configs(db, self.strategy_configs, self.user_id)
            return True
        return False

    @staticmethod
    def _protective_hold(analysis) -> bool:
        """True when the analyzer is standing aside to protect capital (extreme
        volatility or a shock bar). A saved strategy must not override this."""
        for f in analysis.factors:
            if f.name in ("volatility", "shock") and (
                "aside" in f.detail or "spike" in f.detail
            ):
                return True
        return False

    @staticmethod
    def _bear_regime(analysis) -> bool:
        """True when price is under a falling long-term EMA — no new longs."""
        return any(f.name == "regime" and f.signal == "sell" for f in analysis.factors)

    def _validate_saved_strategy(self, cfg: dict) -> tuple[bool, str, dict]:
        """Judge whether a saved strategy has earned the right to drive new BUYS.

        Judges the strategy on its OUT-OF-SAMPLE (holdout) behaviour — return,
        win rate, trade count AND drawdown are all read from the validation run,
        never the optimistic in-sample fit. Returns ``(ok, reason, detail)``. It
        never invents or substitutes a number: if the metrics are missing, lack
        an out-of-sample split, or an ENABLED check has no out-of-sample figure
        recorded (e.g. a strategy saved before OOS metrics were captured), the
        gate fails CLOSED asking for a retrain — an unproven strategy has not
        earned real money's trust. This is the honest stand-in for "make it 95%
        correct": we can't promise accuracy, but we refuse to auto-trade a
        strategy that hasn't shown a positive, out-of-sample edge.
        """
        metrics = (cfg or {}).get("metrics")
        if not isinstance(metrics, dict) or not metrics:
            return False, "no backtest metrics yet — retrain to validate", {}
        s = self.settings
        oos_return = metrics.get("validation_return_pct")
        in_sample_only = oos_return is None
        # Out-of-sample figures: present only when a holdout split ran on a build
        # that persists them. Missing => unknown, NEVER the rosier in-sample value.
        oos_win_rate = metrics.get("validation_win_rate_pct")
        oos_num_trades = metrics.get("validation_num_trades")
        oos_drawdown = metrics.get("validation_max_drawdown_pct")
        return_pct = float(
            oos_return if oos_return is not None
            else (metrics.get("total_return_pct", 0.0) or 0.0)
        )
        detail = {
            "return_pct": round(return_pct, 2),
            "return_basis": "in-sample only" if in_sample_only else "out-of-sample",
            "win_rate_pct": round(oos_win_rate, 2) if oos_win_rate is not None else None,
            "num_trades": oos_num_trades,
            "max_drawdown_pct": round(oos_drawdown, 2) if oos_drawdown is not None else None,
            "overfit_gap_pct": metrics.get("overfit_gap_pct"),
        }
        fails: list[str] = []
        min_return = getattr(s, "strategy_min_return_pct", 0.0) or 0.0
        if return_pct <= min_return:
            basis = "in-sample " if in_sample_only else "out-of-sample "
            fails.append(f"{basis}return {return_pct:+.1f}% ≤ required {min_return:.1f}%")
        min_trades = getattr(s, "strategy_min_trades", 0) or 0
        if min_trades > 0:
            if oos_num_trades is None:
                fails.append("no out-of-sample trade count (retrain to validate)")
            elif int(oos_num_trades) < min_trades:
                fails.append(
                    f"only {int(oos_num_trades)} out-of-sample trades "
                    f"(need ≥ {min_trades})"
                )
        min_wr = getattr(s, "strategy_min_win_rate_pct", 0.0) or 0.0
        if min_wr > 0:
            if oos_win_rate is None:
                fails.append("no out-of-sample win rate (retrain to validate)")
            elif float(oos_win_rate) < min_wr:
                fails.append(
                    f"out-of-sample win rate {float(oos_win_rate):.0f}% "
                    f"< required {min_wr:.0f}%"
                )
        max_dd = getattr(s, "strategy_max_drawdown_pct", 0.0) or 0.0
        if max_dd > 0:
            if oos_drawdown is None:
                fails.append("no out-of-sample drawdown (retrain to validate)")
            elif float(oos_drawdown) > max_dd:
                fails.append(
                    f"out-of-sample drawdown {float(oos_drawdown):.0f}% "
                    f"> allowed {max_dd:.0f}%"
                )
        if in_sample_only:
            fails.append("no out-of-sample validation (retrain to produce one)")
        ok = not fails
        return ok, ("validated" if ok else "; ".join(fails)), detail

    def _strategy_signal_confidence(self, cfg: dict, analysis) -> float:
        """An HONEST confidence for a saved-strategy verdict.

        A strategy firing is a deterministic rule trigger, not a probability, so
        we never stamp it 100%. Prefer the strategy's REAL out-of-sample win rate
        (the share of held-out trades that won); if that wasn't recorded, keep the
        analyzer's own computed confidence. Never a fabricated 1.0.
        """
        wr = ((cfg or {}).get("metrics") or {}).get("validation_win_rate_pct")
        if isinstance(wr, (int, float)):
            return max(0.0, min(1.0, float(wr) / 100.0))
        return max(0.0, min(1.0, float(getattr(analysis, "confidence", 0.0) or 0.0)))

    def _apply_saved_strategy(self, symbol: str, df, analysis):
        """Let the user's SAVED strategy own the verdict when they've opted in.

        Capital-preservation is non-negotiable: a strategy BUY is still refused
        while the analyzer is protectively standing aside (extreme vol / shock)
        or in a bear regime (don't catch a falling knife). A strategy SELL/exit
        is never blocked — reducing risk is always allowed.
        """
        if not getattr(self.settings, "use_saved_strategy", False):
            return analysis
        cfg = self.strategy_configs.get(symbol.upper())
        if not cfg:
            return analysis
        try:
            strat = build_strategy(cfg["strategy"], **(cfg.get("params") or {}))
            sig = strat.generate(df)
        except Exception as exc:  # bad params / unknown strategy -> stay with analyzer
            logger.warning("saved strategy for %s failed, using analyzer: %s", symbol, exc)
            return analysis

        action = sig.action
        reason = getattr(sig, "reason", "") or ""
        label = f"Saved {cfg['strategy']} strategy"
        if action == "buy":
            pause_bear = getattr(self.settings, "auto_pause_in_bear", True)
            if self._protective_hold(analysis) or (pause_bear and self._bear_regime(analysis)):
                analysis.verdict = "hold"
                analysis.summary = (
                    f"{label} signalled BUY, but standing aside to protect "
                    f"capital: {analysis.summary}"
                )
                return analysis
            # Validation gate: a saved strategy only earns the right to drive a
            # NEW long once its real backtest metrics clear the operator's bar.
            # Unvalidated -> defer to the deterministic analyzer verdict rather
            # than trade on an unproven edge. A SELL/exit is never gated.
            if getattr(self.settings, "require_strategy_validation", True):
                ok_val, why, _detail = self._validate_saved_strategy(cfg)
                if not ok_val:
                    analysis.summary = (
                        f"{label} signalled BUY but is not validated ({why}); "
                        f"deferring to analyzer: {analysis.summary}"
                    )
                    return analysis
            analysis.verdict = "buy"
            analysis.confidence = self._strategy_signal_confidence(cfg, analysis)
        elif action == "sell":
            analysis.verdict = "sell"
            analysis.confidence = self._strategy_signal_confidence(cfg, analysis)
        else:
            analysis.verdict = "hold"
        analysis.summary = f"{label} → {action.upper()}" + (f": {reason}" if reason else "")
        return analysis

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._broadcaster and self._loop:
            message: dict[str, Any] = {"event": event, "data": payload}
            if self.user_id is not None:
                message["user_id"] = self.user_id
            asyncio.run_coroutine_threadsafe(
                self._broadcaster.broadcast(message),
                self._loop,
            )

    def _notify(self, text: str) -> None:
        """Fire-and-forget Telegram notification.

        ``notifier.send`` makes a blocking HTTP call (up to its timeout) and
        several callers here hold ``self._lock``. Sending inline would stall the
        whole order path for that user on a slow/unreachable Telegram. Dispatch
        on a daemon thread instead so a notification can never block or break a
        trade; ``send`` swallows its own errors.
        """
        if not self.notifier.enabled:
            return
        threading.Thread(
            target=self.notifier.send, args=(text,), daemon=True
        ).start()

    # ---- helpers -----------------------------------------------------

    def _price(self, symbol: str, fallback: float | None = None) -> float:
        try:
            return self.connector.fetch_price(symbol)
        except Exception as exc:
            if fallback is not None:
                return fallback
            raise RuntimeError(f"Could not fetch price for {symbol}: {exc}") from exc

    def _equity(self, db: Session) -> float:
        """Available equity used for sizing/limits."""
        if self.settings.is_live:
            bal = self.connector.fetch_balance("USDT")
            if bal is not None:
                return bal
            # If we cannot read the live balance, be conservative.
            return 0.0
        return self.paper_balance

    def _open_trade_for_symbol(self, db: Session, symbol: str) -> Optional[Trade]:
        # A resting (pending) limit order also occupies the symbol slot.
        stmt = self._scope_book(
            select(Trade).where(
                Trade.symbol == symbol,
                Trade.status.in_(
                    [TradeStatus.open.value, TradeStatus.pending.value]
                ),
            )
        )
        return db.scalars(stmt).first()

    def _open_unrealized(self, db: Session) -> float:
        """Sum current unrealized PnL across this account's OPEN positions.

        Fed into the daily-loss circuit breaker so a large *open* drawdown blocks
        NEW entries even before any losing trade is realized — capital
        preservation ("less loss") shouldn't wait for a stop to fire.

        RAISES (via ``_price``) if any open position can't be priced right now. A
        feed outage must NOT be silently valued at ``entry_price`` — that reads as
        zero unrealized PnL and would blind the loss breaker / kill-switch to a
        real drawdown. Callers treat the raise as "account currently unvaluable"
        and degrade safe rather than trusting a fabricated all-clear.
        """
        open_trades = db.scalars(
            self._scope_book(select(Trade).where(Trade.status == TradeStatus.open.value))
        ).all()
        total = 0.0
        for t in open_trades:
            total += self.unrealized_pnl(t, self._price(t.symbol))
        return total

    # ---- risk safeguards --------------------------------------------

    def _total_equity(self, db: Session) -> float:
        """Total account equity = free cash + current market value of open
        positions.

        Matches the ``equity`` figure reported by ``status`` and is the basis for
        the max-drawdown kill-switch.

        RAISES (via ``_price``) if any open position can't be priced right now, so
        the kill-switch's "can't value the account → never fabricate a breach"
        branch fires instead of reading a fabricated, unchanged equity off
        ``entry_price``. On a feed outage the honest answer is "unknown", not
        "break-even".
        """
        balance = self._equity(db)
        position_value = 0.0
        open_trades = db.scalars(
            self._scope_book(select(Trade).where(Trade.status == TradeStatus.open.value))
        ).all()
        for t in open_trades:
            price = self._price(t.symbol)
            position_value += t.entry_price * t.amount + self.unrealized_pnl(t, price)
        return balance + position_value

    def _update_drawdown(self, db: Session) -> bool:
        """Track peak equity and trip the kill-switch on catastrophic drawdown.

        Returns True if the kill-switch is (now) tripped. On the FIRST trip it
        halts autonomous trading (``running=False``), emits an event and notifies.
        Open positions keep being managed — only NEW entries are blocked — until
        the operator restarts the bot (which calls :meth:`reset_killswitch`).
        """
        dd_pct = getattr(self.settings, "max_drawdown_pct", 0.0) or 0.0
        if dd_pct <= 0:
            return False
        try:
            equity = self._total_equity(db)
        except Exception:
            # Can't value the account right now — never fabricate a breach.
            return self._killswitch_tripped
        if equity <= 0:
            return self._killswitch_tripped
        if equity > self._peak_equity:
            self._peak_equity = equity
            # Persist the growing drawdown baseline so a rebuild can't reset it to a
            # lower value (which would blind the kill-switch). Throttled: only write
            # when the peak climbs materially (>= 0.25%) above the last persisted
            # value, so the ~5s monitor tick doesn't hammer the KV store.
            if self._peak_equity >= self._last_persisted_peak * 1.0025:
                self.persist_runtime(db)
        if self._peak_equity <= 0:
            return False
        drawdown = (self._peak_equity - equity) / self._peak_equity * 100.0
        if drawdown >= dd_pct and not self._killswitch_tripped:
            self._killswitch_tripped = True
            was_running = self.running
            self.running = False
            # Durably record the halt BEFORE emitting/notifying so a crash mid-event
            # can't lose it — a rebuilt engine must see the trip and stay halted.
            self.persist_runtime(db)
            self._emit(
                "killswitch",
                {
                    "peak_equity": round(self._peak_equity, 2),
                    "equity": round(equity, 2),
                    "drawdown_pct": round(drawdown, 2),
                    "max_drawdown_pct": dd_pct,
                },
            )
            self._notify(
                f"\U0001F6D1 KILL-SWITCH: equity {equity:.2f} is {drawdown:.1f}% below "
                f"peak {self._peak_equity:.2f} (limit {dd_pct:.0f}%). Autonomous trading "
                f"halted and new entries blocked; open positions keep their stops. "
                f"Restart the bot to resume."
            )
            if was_running:
                logger.warning(
                    "kill-switch tripped for user=%s (drawdown %.1f%% >= %.1f%%)",
                    self.user_id, drawdown, dd_pct,
                )
        return self._killswitch_tripped

    def _drawdown_block(self, db: Session) -> tuple[bool, str]:
        """Return (blocked, reason) for a prospective NEW entry."""
        if self._update_drawdown(db):
            return True, (
                "max-drawdown kill-switch active — new entries are blocked "
                "until the bot is restarted"
            )
        return False, ""

    def _daily_loss_hit(self, db: Session) -> bool:
        """True if today's realized + open PnL has breached the daily-loss limit.

        Mirrors the daily-loss branch of :meth:`RiskManager.check` so that a
        RESTING-order fill is held to the SAME breaker as a fresh entry — filling
        a limit/DCA leg IS opening a new position, and must not sneak past the
        loss limit just because the order was placed earlier. Scoped to the
        current book (paper vs live) via ``day_realized_pnl``. Never fabricates a
        breach: if the account can't be valued right now it returns False and lets
        the deterministic gates elsewhere surface the degraded state.
        """
        limit_pct = getattr(self.settings, "daily_loss_limit_pct", 0.0) or 0.0
        if limit_pct <= 0:
            return False
        try:
            day_pnl = self.risk.day_realized_pnl(db) + self._open_unrealized(db)
            basis = self._total_equity(db)
        except Exception:
            return False
        if basis <= 0:
            return False
        loss_limit = -abs(basis * (limit_pct / 100.0))
        return day_pnl <= loss_limit

    def reset_killswitch(self, db: Session | None = None) -> None:
        """Clear the kill-switch and reseed the equity peak (human re-arm).

        Called when the operator (re)starts the bot: restarting is the explicit
        human acknowledgement that resumes trading, and the drawdown budget is
        measured fresh from the equity at restart rather than an old, higher peak.
        When ``db`` is supplied the cleared state is persisted immediately, so the
        re-arm itself survives a later restart/rebuild.
        """
        self._killswitch_tripped = False
        self._peak_equity = 0.0
        self._last_persisted_peak = 0.0
        if db is not None:
            self.persist_runtime(db)

    def persist_runtime(self, db: Session) -> None:
        """Durably save run/stop + kill-switch + equity peak to the KV store.

        This is the backbone of the durable halt: a restart, redeploy or engine
        rebuild resumes EXACTLY where the operator left off — a stopped bot stays
        stopped, and a tripped drawdown kill-switch stays tripped (never silently
        cleared into the drawdown that caused it). Best-effort: a KV write failure
        must never crash a trade or a monitor tick, so we log and carry on.
        """
        try:
            save_engine_runtime(
                db,
                {
                    "running": bool(self.running),
                    "killswitch_tripped": bool(self._killswitch_tripped),
                    "peak_equity": round(float(self._peak_equity), 2),
                },
                self.user_id,
            )
            self._last_persisted_peak = self._peak_equity
        except Exception as exc:  # bookkeeping must never break the trading path
            logger.warning(
                "failed to persist engine runtime for user=%s: %s", self.user_id, exc
            )

    def _live_readonly_block(self) -> Optional[str]:
        """Guard for live ORDER PLACEMENT with a read-only key.

        In live mode the app shows the user's REAL balances and market data even
        when the API key can only READ the account (Spot trading not enabled yet,
        or an IP restriction not set). But it must never *pretend* to place an
        order it can't. When we KNOW the key can't trade (already probed), refuse
        placement with a clear, honest message instead of firing an order the
        exchange would reject with a cryptic -2015. Returns the message to
        surface, or None when placement is allowed — including when trade-access
        is still unknown (None), in which case the real order attempt itself is
        the source of truth and will surface any real error."""
        if not self.settings.is_live:
            return None
        # getattr with a default keeps this working for lightweight test/stub
        # connectors that don't implement the trade-access probe at all.
        can = getattr(self.connector, "can_trade", None)
        if can is None:
            # Not probed since the last (re)connect — find out BEFORE attempting a
            # live order, so a read-only key gets an honest message instead of a
            # doomed -2015. Best-effort: if the probe is absent or itself fails,
            # fall through and let the real order attempt surface the true error.
            probe = getattr(self.connector, "check_trading_access", None)
            if callable(probe):
                try:
                    probe()
                    can = getattr(self.connector, "can_trade", None)
                except Exception:
                    can = None
        if can is False:
            return (
                "Live mode is read-only right now: your API key can read your "
                "real Binance account but can't place orders yet. Enable Spot "
                "trading (and any required IP restriction) on the key, then run "
                "Test connection. Your balances and market data are live and real "
                "in the meantime."
            )
        return None

    def _friendly_exchange_error(self, exc: Exception, action: str = "order") -> str:
        """Turn a raw exchange rejection into plain, actionable guidance.

        A -2015 ("Invalid API-key, IP, or permissions") on an order almost always
        means the key can READ the account but Spot trading isn't enabled (or an
        IP restriction blocks this server) — the same read-only case the
        pre-check catches. If that pre-check was bypassed (trade-access couldn't
        be probed, e.g. the apiRestrictions endpoint was unavailable), still give
        the user the real reason instead of a cryptic code. Nothing is fabricated:
        it only rephrases an error the exchange actually returned."""
        msg = str(exc)
        if "-2015" in msg or "Invalid API-key" in msg or "permissions for action" in msg:
            return (
                "Can't place this order: your Binance API key can read your real "
                "account and market data, but Spot trading isn't enabled for the "
                "key (or an IP restriction is blocking this server). Turn on "
                "'Enable Spot & Margin Trading' for the key in Binance API "
                "Management, clear any IP limit, then run Test connection. Nothing "
                "was traded."
            )
        return f"Exchange {action} failed: {msg}"

    def reset_paper_data(self, db: Session) -> dict[str, Any]:
        """Wipe this account's SIMULATED (paper) history for a clean slate.

        Deletes the user's paper trades and their signal log, and resets the
        paper wallet to its starting balance. REAL (live) trades are never
        touched, so this can't destroy real-money records. Irreversible."""
        with self._lock:
            paper_trades = list(
                db.scalars(
                    self._scope(select(Trade).where(Trade.mode == "paper"))
                ).all()
            )
            n_trades = len(paper_trades)
            for t in paper_trades:
                db.delete(t)
            sig_stmt = select(SignalLog)
            if self.user_id is not None:
                sig_stmt = sig_stmt.where(SignalLog.user_id == self.user_id)
            signals = list(db.scalars(sig_stmt).all())
            n_signals = len(signals)
            for slog in signals:
                db.delete(slog)
            db.commit()
            self.paper_balance = self.settings.paper_starting_balance
            save_paper_balance(db, self.paper_balance, self.user_id)
            self._last_auto_verdict.clear()
            self.reset_killswitch(db)
        return {
            "trades_deleted": n_trades,
            "signals_deleted": n_signals,
            "paper_balance": self.paper_balance,
        }

    def _consecutive_losses(self, db: Session) -> int:
        """Count the current run of losing CLOSED trades (most recent first)."""
        rows = db.scalars(
            self._scope_book(
                select(Trade)
                .where(Trade.status == TradeStatus.closed.value)
                .order_by(Trade.closed_at.desc())
            )
        ).all()
        streak = 0
        for t in rows:
            if (t.pnl or 0.0) < 0:
                streak += 1
            else:
                break
        return streak

    def _reentry_cooldown_remaining(self, symbol: str) -> float:
        """Seconds left before the bot may re-enter ``symbol`` after a loss."""
        minutes = getattr(self.settings, "reentry_cooldown_minutes", 0.0) or 0.0
        if minutes <= 0:
            return 0.0
        last = self._last_loss_exit.get(symbol)
        if not last:
            return 0.0
        remaining = minutes * 60.0 - (_utcnow() - last).total_seconds()
        return remaining if remaining > 0 else 0.0

    def _spread_guard(self, symbol: str) -> str | None:
        """Reason string if the live bid/ask spread is too wide to enter, else None.

        Only meaningful for LIVE market entries. Never blocks when the spread
        can't be measured (missing bid/ask) — refusing on unknowable data would
        be inventing a reason.
        """
        max_spread = getattr(self.settings, "max_spread_pct", 0.0) or 0.0
        if max_spread <= 0:
            return None
        fn = getattr(self.connector, "spread_pct", None)
        if not callable(fn):
            return None
        try:
            spread = fn(symbol)
        except Exception:
            return None
        if spread is None or spread <= max_spread:
            return None
        return (
            f"{symbol}: bid/ask spread {spread:.2f}% exceeds max {max_spread:.2f}% "
            "— skipping entry to avoid a bad fill"
        )

    # How recent a regime snapshot must be to gate a webhook long. Snapshots are
    # refreshed every monitor tick (~seconds) while the bot watches a symbol, so
    # a reading older than this means the bot ISN'T currently watching it — we
    # then decline to block on a stale read rather than invent a current one.
    _REGIME_FRESH_SECONDS = 600.0

    def _entry_guards(
        self, db: Session, symbol: str, *, check_regime: bool = False
    ) -> tuple[bool, str]:
        """Discretionary "should we open NEW risk right now?" gates for UNATTENDED
        entries (the autonomous loop and TradingView webhooks). Returns
        ``(ok, reason)``.

        These sit alongside — never replace — the hard risk gates (drawdown
        kill-switch, position/exposure caps, daily-loss limit, spread, live
        read-only), which apply to every order including manual ones. A manual
        order is a human acting deliberately, so it is NOT subject to these
        anti-whipsaw / streak / regime pauses; an unattended entry is, so a
        webhook that fires repeatedly into a losing streak or a bear market is
        gated exactly like the bot's own decisions. Cheap and deterministic: no
        network I/O and nothing fabricated — when a reading is unavailable the
        gate stands aside rather than block on invented data. ``check_regime``
        is opt-in because the autonomous path already applies the bear-regime
        pause at analysis time and only needs the cooldown/streak checks here.
        """
        sym = symbol.upper().strip()
        # Anti-whipsaw: honour the re-entry cooldown after a losing exit on this
        # symbol so we don't buy straight back into the chop that stopped us out.
        cooldown = self._reentry_cooldown_remaining(sym)
        if cooldown > 0:
            return (
                False,
                f"{symbol}: re-entry cooldown ({cooldown / 60:.1f} min left "
                "after a losing exit)",
            )
        # Consecutive-loss circuit breaker: stop opening new risk after a losing
        # streak until a win breaks it.
        max_streak = getattr(self.settings, "max_consecutive_losses", 0) or 0
        if max_streak > 0:
            streak = self._consecutive_losses(db)
            if streak >= max_streak:
                return (
                    False,
                    f"{symbol}: paused after {streak} consecutive losses "
                    "(circuit breaker)",
                )
        # Bear-regime pause (opt-in via auto_pause_in_bear). Acts ONLY on a real,
        # recent regime reading captured by the monitor loop — never a fabricated
        # or stale one, so it can't block on a market read the bot doesn't have.
        if check_regime and getattr(self.settings, "auto_pause_in_bear", True):
            snap = self._last_regime.get(sym)
            if snap and snap.get("entries_paused"):
                at = snap.get("at")
                fresh = False
                if at:
                    try:
                        age = (_utcnow() - dt.datetime.fromisoformat(at)).total_seconds()
                        fresh = 0 <= age <= self._REGIME_FRESH_SECONDS
                    except Exception:
                        fresh = False
                if fresh:
                    detail = (snap.get("detail") or "").strip()
                    reason = (
                        f"{symbol}: new longs paused — "
                        f"{snap.get('regime', 'bear')} regime"
                    )
                    if detail:
                        reason += f" ({detail})"
                    return False, reason
        return True, ""

    # ---- core execution ---------------------------------------------

    def execute_signal(
        self,
        db: Session,
        *,
        action: str,
        symbol: str,
        amount: float | None,
        stop_loss: float | None,
        take_profit: float | None,
        source: str,
        note: str | None = None,
        limit_price: float | None = None,
    ) -> tuple[bool, str, Optional[Trade]]:
        """Execute a buy/sell/close signal. Returns (accepted, message, trade)."""
        symbol = symbol.upper().strip()
        action = action.lower().strip()
        if action not in {"buy", "sell", "close"}:
            return False, f"Unknown action '{action}'", None

        with self._lock:
            existing = self._open_trade_for_symbol(db, symbol)

            # ---- CLOSE (or opposite-side signal that closes) ----------
            if action == "close":
                if not existing:
                    return False, f"No open position for {symbol} to close", None
                if existing.status == TradeStatus.pending.value:
                    return self._cancel_pending(db, existing, note or "close signal")
                return self._close_trade(db, existing, note or "close signal")

            # A sell with an open long closes it; a buy with an open short closes it.
            if existing and existing.side != action:
                if existing.status == TradeStatus.pending.value:
                    return self._cancel_pending(
                        db, existing, note or f"{action} signal cancelled resting order"
                    )
                return self._close_trade(db, existing, note or f"{action} signal closed position")

            if existing and existing.side == action:
                state = (
                    "resting limit"
                    if existing.status == TradeStatus.pending.value
                    else action
                )
                return False, f"Already in a {state} position for {symbol}", None

            # ---- OPEN a new position ---------------------------------
            # Read-only live guard: in live mode with a key that can READ the
            # real account but not TRADE yet (Spot not enabled / IP not set), we
            # still show real balances and market data — but must never pretend
            # to open a position. Refuse placement honestly. Exits above already
            # returned, so a close is never blocked.
            ro = self._live_readonly_block()
            if ro:
                return False, ro, None
            # Account-level kill-switch first: on catastrophic drawdown, refuse
            # ALL new entries (manual, webhook or autonomous) until the bot is
            # restarted. Exits above already returned, so this never blocks a
            # close — only new risk.
            blocked, why = self._drawdown_block(db)
            if blocked:
                return False, f"Rejected by risk manager: {why}", None
            # Spot markets can't be shorted: a live SELL with no existing long to
            # close (all closing cases returned above) would attempt to sell base
            # currency we don't hold and be rejected by Binance — or, worse, sell
            # unrelated holdings. Refuse it explicitly on live. Paper still
            # simulates sell-to-open for symmetry/backtest-style exploration.
            if self.settings.is_live and action == "sell":
                return (
                    False,
                    f"{symbol}: short selling is not supported on live spot "
                    "(no open long to close).",
                    None,
                )
            # Discretionary anti-runaway gates for UNATTENDED entries: an
            # autonomous decision or a TradingView webhook must respect the
            # re-entry cooldown, the consecutive-loss circuit breaker and (webhook)
            # the bear-regime pause — the same protections the bot applies to its
            # own trades, so an alert firing repeatedly into a losing streak or a
            # falling market is stood aside. A manual order is a deliberate human
            # action and is exempt here (it stays bounded by the hard risk gates
            # above and the risk manager below). The autonomous path already
            # applied the regime pause at analysis time, so only the webhook needs
            # the (cached, freshness-checked) regime read.
            if source in {"auto", "tradingview"}:
                ok_guard, guard_reason = self._entry_guards(
                    db, symbol, check_regime=(source == "tradingview")
                )
                if not ok_guard:
                    return False, guard_reason, None
            price = self._price(symbol)
            # For a limit order, size and validate against the LIMIT price (the
            # intended fill), not the current market price.
            ref_price = limit_price if limit_price else price
            equity = self._equity(db)
            # A live open needs a readable balance. _equity returns 0.0 BOTH when
            # the account is genuinely empty AND when the balance can't be read
            # (network/exchange hiccup) — sizing would then fail with an opaque
            # "Computed position size is zero". Disambiguate so the operator sees
            # the true reason instead of a misleading one.
            if self.settings.is_live and equity <= 0:
                if self.connector.fetch_balance("USDT") is None:
                    return False, (
                        "Live balance unavailable — couldn't read your account "
                        "balance right now (network or exchange issue). Holding "
                        "new entries until it recovers."
                    ), None
                return False, (
                    "Live account has no free USDT to open a new position — "
                    "deposit or free up funds on the exchange first."
                ), None
            # Value the rest of the book to feed the loss/exposure checks. If a
            # currently-open position can't be priced (feed outage), we're blind to
            # real exposure — refuse the NEW entry rather than deploy capital on a
            # fabricated all-clear. Existing positions keep their own stops.
            try:
                day_unrealized = self._open_unrealized(db)
                equity_for_limits = self._total_equity(db)
            except Exception:
                return False, (
                    "Rejected by risk manager: can't value open positions right "
                    "now (price feed unavailable) — holding new entries until it "
                    "recovers."
                ), None
            decision = self.risk.check(
                db,
                equity=equity,
                price=ref_price,
                requested_amount=amount,
                is_opening=True,
                stop_price=stop_loss,
                day_unrealized=day_unrealized,
                equity_for_limits=equity_for_limits,
                unattended=source in {"auto", "tradingview"},
            )
            if not decision.allowed:
                return False, f"Rejected by risk manager: {decision.reason}", None

            qty = decision.amount
            exchange_order_id: str | None = None
            entry_fee = 0.0  # real quote fee observed on a LIVE open; paper stays 0

            # ---- LIMIT order: rest it, fill later when price crosses ----
            if limit_price:
                if self.settings.is_live:
                    adj_qty, err = self.connector.normalize_amount(
                        symbol, qty, limit_price
                    )
                    if err:
                        return False, f"Rejected: {err}", None
                    qty = adj_qty
                    try:
                        order = self.connector.create_limit_order(
                            symbol, action, qty, limit_price
                        )
                        exchange_order_id = str(order.get("id")) if order else None
                    except Exception as exc:
                        return False, self._friendly_exchange_error(exc, "limit order"), None
                else:
                    # Paper: reserve notional now so equity/exposure is honest
                    # while the order rests; released if cancelled, consumed on fill.
                    self.paper_balance -= qty * limit_price
                    save_paper_balance(db, self.paper_balance, self.user_id)

                trade = Trade(
                    symbol=symbol,
                    side=action,
                    amount=qty,
                    entry_price=limit_price,
                    status=TradeStatus.pending.value,
                    order_type="limit",
                    limit_price=limit_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    mode=self.settings.trading_mode,
                    source=source,
                    exchange_order_id=exchange_order_id,
                    note=note,
                    opened_at=_utcnow(),
                    user_id=self.user_id,
                )
                db.add(trade)
                db.commit()
                db.refresh(trade)
                self._emit(
                    "order_pending",
                    {"id": trade.id, "symbol": symbol, "side": action,
                     "limit_price": limit_price},
                )
                self._notify(
                    f"\U0001F4DD Limit {action.upper()} {qty:.8f} {symbol} resting @ "
                    f"{limit_price:.2f} ({self.settings.trading_mode})"
                )
                return (
                    True,
                    f"Limit {action} {qty:.8f} {symbol} resting @ {limit_price:.2f}",
                    trade,
                )

            if self.settings.is_live:
                # Liquidity guard: don't take a market entry into a wide book.
                # Applies to opening orders only (this branch); exits are never
                # spread-blocked.
                spread_reason = self._spread_guard(symbol)
                if spread_reason:
                    return False, f"Rejected: {spread_reason}", None
                adj_qty, err = self.connector.normalize_amount(symbol, qty, price)
                if err:
                    return False, f"Rejected: {err}", None
                qty = adj_qty
                try:
                    order = self.connector.create_market_order(symbol, action, qty)
                    exchange_order_id = str(order.get("id")) if order else None
                    filled_price = float(order.get("average") or order.get("price") or price)
                    price = filled_price
                    # Book the REAL filled base qty (net of any base-asset fee),
                    # not the requested size — over-reporting holdings is what
                    # later hits -2010 on the close. Capture the entry fee too.
                    qty, entry_fee = self._net_fill(order, symbol, price, action, qty)
                    if qty <= 0:
                        return False, "Rejected: exchange reported a zero fill", None
                except Exception as exc:
                    return False, self._friendly_exchange_error(exc, "order"), None
            else:
                # Paper: reserve notional from the paper wallet.
                self.paper_balance -= qty * price
                save_paper_balance(db, self.paper_balance, self.user_id)

            sl = stop_loss or self._auto_stop(price, action)
            tp = take_profit or self._auto_take(price, action)

            # Live: place an exchange-side stop-loss so the position is protected
            # even if this bot process is down. Best-effort; the in-process SL/TP
            # monitor remains the fallback.
            stop_order_id: str | None = None
            if self.settings.is_live and sl and action == "buy":
                stop_order = self.connector.create_stop_loss_order(
                    symbol, "sell", qty, sl
                )
                if stop_order:
                    stop_order_id = str(stop_order.get("id"))
                else:
                    # The venue rejected / couldn't rest the protective stop. The
                    # in-process SL/TP monitor still guards this position WHILE the
                    # bot runs, but nothing rests on the exchange if the process
                    # goes offline. Surface it loudly rather than let the operator
                    # believe a hard stop is in place (silent gap = false safety).
                    self._emit("stop_unprotected", {
                        "symbol": symbol, "side": action, "stop": round(sl, 8),
                    })
                    self._notify(
                        f"⚠️ {symbol}: could NOT place an exchange-side "
                        f"stop-loss @ {sl:.2f}. The bot will still exit at your "
                        f"stop while it is running, but no stop is resting on the "
                        f"exchange if the bot goes offline — consider setting "
                        f"one on the exchange manually."
                    )

            trade = Trade(
                symbol=symbol,
                side=action,
                amount=qty,
                entry_price=price,
                stop_loss=sl,
                take_profit=tp,
                status=TradeStatus.open.value,
                mode=self.settings.trading_mode,
                source=source,
                exchange_order_id=exchange_order_id,
                stop_order_id=stop_order_id,
                fee=entry_fee,
                note=note,
                opened_at=_utcnow(),
                user_id=self.user_id,
            )
            db.add(trade)
            db.commit()
            db.refresh(trade)
            self._emit("trade_opened", {"id": trade.id, "symbol": symbol, "side": action})
            self._notify(
                f"\U0001F4C8 Opened <b>{action.upper()}</b> {qty:.8f} {symbol} @ "
                f"{price:.2f} ({self.settings.trading_mode})"
            )
            return True, f"Opened {action} {qty:.8f} {symbol} @ {price:.2f}", trade

    # ---- scaled (DCA) entries ---------------------------------------
    def _place_leg(
        self,
        db: Session,
        *,
        symbol: str,
        qty: float,
        kind: str,
        limit_price: float | None,
        stop_loss: float | None,
        take_profit: float | None,
        source: str,
        note: str,
    ) -> tuple[bool, str, Optional[Trade]]:
        """Place ONE buy leg of a scaled entry. Caller holds the lock and has
        already (a) risk-checked the TOTAL size, (b) confirmed slot capacity and
        (c) confirmed there's no existing position and the kill-switch is clear.

        Mirrors ``execute_signal``'s single-order placement for one tranche and
        deliberately does NOT re-run risk checks (they apply to the whole entry,
        not each slice). ``kind="market"`` opens immediately; ``kind="limit"``
        rests at ``limit_price`` until price crosses it. Returns (ok, msg, trade).
        """
        if kind == "market":
            price = self._price(symbol)
            exchange_order_id: str | None = None
            entry_fee = 0.0  # real quote fee observed on a LIVE open; paper stays 0
            if self.settings.is_live:
                spread_reason = self._spread_guard(symbol)
                if spread_reason:
                    return False, f"Rejected: {spread_reason}", None
                adj_qty, err = self.connector.normalize_amount(symbol, qty, price)
                if err:
                    return False, f"Rejected: {err}", None
                qty = adj_qty
                try:
                    order = self.connector.create_market_order(symbol, "buy", qty)
                    exchange_order_id = str(order.get("id")) if order else None
                    price = float(order.get("average") or order.get("price") or price)
                    # Real filled base qty (net of base-asset fee) + observed fee.
                    qty, entry_fee = self._net_fill(order, symbol, price, "buy", qty)
                    if qty <= 0:
                        return False, "Rejected: exchange reported a zero fill", None
                except Exception as exc:
                    return False, self._friendly_exchange_error(exc, "order"), None
            else:
                self.paper_balance -= qty * price
                save_paper_balance(db, self.paper_balance, self.user_id)
            sl = stop_loss or self._auto_stop(price, "buy")
            tp = take_profit or self._auto_take(price, "buy")
            stop_order_id: str | None = None
            if self.settings.is_live and sl:
                stop_order = self.connector.create_stop_loss_order(
                    symbol, "sell", qty, sl
                )
                if stop_order:
                    stop_order_id = str(stop_order.get("id"))
            trade = Trade(
                symbol=symbol, side="buy", amount=qty, entry_price=price,
                stop_loss=sl, take_profit=tp, status=TradeStatus.open.value,
                mode=self.settings.trading_mode, source=source,
                exchange_order_id=exchange_order_id, stop_order_id=stop_order_id,
                fee=entry_fee, note=note, opened_at=_utcnow(), user_id=self.user_id,
            )
            db.add(trade)
            db.commit()
            db.refresh(trade)
            self._emit("trade_opened", {"id": trade.id, "symbol": symbol, "side": "buy"})
            self._notify(
                f"\U0001F4C8 Opened <b>BUY</b> {qty:.8f} {symbol} @ {price:.2f} "
                f"({self.settings.trading_mode}) [{note}]"
            )
            return True, f"Opened buy {qty:.8f} {symbol} @ {price:.2f}", trade

        # ---- limit leg: rest until price crosses ----
        lp = float(limit_price or 0.0)
        if lp <= 0:
            return False, "Invalid limit price for scaled leg", None
        exchange_order_id = None
        if self.settings.is_live:
            adj_qty, err = self.connector.normalize_amount(symbol, qty, lp)
            if err:
                return False, f"Rejected: {err}", None
            qty = adj_qty
            try:
                order = self.connector.create_limit_order(symbol, "buy", qty, lp)
                exchange_order_id = str(order.get("id")) if order else None
            except Exception as exc:
                return False, self._friendly_exchange_error(exc, "limit order"), None
        else:
            self.paper_balance -= qty * lp
            save_paper_balance(db, self.paper_balance, self.user_id)
        trade = Trade(
            symbol=symbol, side="buy", amount=qty, entry_price=lp,
            status=TradeStatus.pending.value, order_type="limit", limit_price=lp,
            stop_loss=stop_loss, take_profit=take_profit,
            mode=self.settings.trading_mode, source=source,
            exchange_order_id=exchange_order_id, note=note,
            opened_at=_utcnow(), user_id=self.user_id,
        )
        db.add(trade)
        db.commit()
        db.refresh(trade)
        self._emit(
            "order_pending",
            {"id": trade.id, "symbol": symbol, "side": "buy", "limit_price": lp},
        )
        self._notify(
            f"\U0001F4DD Limit BUY {qty:.8f} {symbol} resting @ {lp:.2f} "
            f"({self.settings.trading_mode}) [{note}]"
        )
        return True, f"Limit buy {qty:.8f} {symbol} resting @ {lp:.2f}", trade

    def execute_scaled_entry(
        self,
        db: Session,
        *,
        symbol: str,
        amount: float | None,
        legs: int,
        step_pct: float,
        first_at_market: bool,
        stop_loss: float | None,
        take_profit: float | None,
        source: str,
        note: str | None = None,
    ) -> tuple[bool, str, list[Trade]]:
        """Open a BUY position in scaled (DCA) legs to average the entry price.

        Splits one buy into ``legs`` tranches: the first optionally at market, the
        rest as resting limit orders stepped ``step_pct``% apart BELOW it. As the
        price dips, more legs fill and the average entry improves; if it never
        dips, only the market/first leg fills — so a single badly-timed entry is
        avoided. Each filled leg becomes its OWN protected position (its own
        SL/TP). Buy-side only (spot can't be scaled short); the caller opts in.

        The TOTAL size is validated ONCE by the risk manager (slots, daily-loss,
        exposure) then split equally across legs, so scaling never risks more than
        a normal single entry. Returns (accepted, message, created_trades).
        """
        symbol = symbol.upper().strip()
        if legs < 2:
            return False, "Scaled entry needs at least 2 legs", []
        if legs > 20:
            return False, "Scaled entry allows at most 20 legs", []
        if step_pct <= 0:
            return False, "Scaled entry step % must be positive", []
        # Read-only live guard: block a scaled ENTRY when the live key can't
        # trade yet (same rationale as execute_signal). Real data stays live.
        ro = self._live_readonly_block()
        if ro:
            return False, ro, []

        with self._lock:
            # Don't stack onto an existing position/resting order for this symbol.
            existing = self._open_trade_for_symbol(db, symbol)
            if existing:
                state = (
                    "resting limit"
                    if existing.status == TradeStatus.pending.value
                    else "open"
                )
                return False, (
                    f"Already in a {state} position for {symbol}; close it before "
                    "scaling in"
                ), []
            # Account-level kill-switch: refuse ALL new entries on drawdown halt.
            blocked, why = self._drawdown_block(db)
            if blocked:
                return False, f"Rejected by risk manager: {why}", []
            # Reserve slots for ALL legs up front so we never place a few then
            # fail partway for lack of capacity.
            current = len(self.risk.open_positions(db))
            if current + legs > self.settings.max_open_positions:
                free = max(self.settings.max_open_positions - current, 0)
                return False, (
                    f"Scaled entry needs {legs} slots but only {free} free "
                    f"(max {self.settings.max_open_positions}). Reduce legs or "
                    "close positions."
                ), []
            price = self._price(symbol)
            if price <= 0:
                return False, f"Could not fetch a valid price for {symbol}", []
            equity = self._equity(db)
            # Size the TOTAL once (validates daily-loss, notional, exposure), then
            # split equally. Sizing uses the CURRENT price, which is conservative
            # because most legs rest below it (slightly less notional than sized).
            # Refuse the ladder if the rest of the book can't be valued (feed
            # outage): scaling in while blind to real exposure is exactly the
            # account-blowup risk this bot is meant to prevent.
            try:
                day_unrealized = self._open_unrealized(db)
                equity_for_limits = self._total_equity(db)
            except Exception:
                return False, (
                    "Can't value open positions right now (price feed "
                    "unavailable) — holding new entries until it recovers."
                ), []
            decision = self.risk.check(
                db, equity=equity, price=price, requested_amount=amount,
                is_opening=True, stop_price=stop_loss,
                day_unrealized=day_unrealized,
                equity_for_limits=equity_for_limits,
            )
            if not decision.allowed:
                return False, f"Rejected by risk manager: {decision.reason}", []
            leg_qty = decision.amount / legs
            if leg_qty <= 0:
                return False, (
                    "Per-leg size rounds to zero — reduce legs or increase amount"
                ), []

            # Ladder DOWN for a long: leg 0 at current price, each next leg
            # step_pct% lower, so dips improve the average entry.
            step = step_pct / 100.0
            created: list[Trade] = []
            for i in range(legs):
                leg_price = price * (1 - i * step)
                if leg_price <= 0:
                    break  # ladder walked to/below zero — stop adding legs
                tag = f"{(note + ' | ') if note else ''}DCA {i + 1}/{legs}"
                if i == 0 and first_at_market:
                    ok, msg, trade = self._place_leg(
                        db, symbol=symbol, qty=leg_qty, kind="market",
                        limit_price=None, stop_loss=stop_loss,
                        take_profit=take_profit, source=source,
                        note=f"{tag} market",
                    )
                else:
                    ok, msg, trade = self._place_leg(
                        db, symbol=symbol, qty=leg_qty, kind="limit",
                        limit_price=leg_price, stop_loss=stop_loss,
                        take_profit=take_profit, source=source,
                        note=f"{tag} @ {leg_price:.2f}",
                    )
                if not ok or trade is None:
                    # Legs already placed are REAL orders — don't silently roll
                    # them back. Report honestly what was placed and why the next
                    # leg failed. If none placed, it's a clean rejection.
                    if not created:
                        return False, f"Scaled entry rejected: {msg}", []
                    return True, (
                        f"Scaled entry partially placed: {len(created)} of {legs} "
                        f"legs for {symbol}. Leg {len(created) + 1} failed: {msg}"
                    ), created
                created.append(trade)

            if not created:
                return False, "Scaled entry placed no legs", []
            n_market = 1 if first_at_market else 0
            n_rest = max(len(created) - n_market, 0)
            return True, (
                f"Scaled buy: {len(created)} legs for {symbol} "
                f"({n_market} at market, {n_rest} resting, step {step_pct:.2f}%)."
            ), created

    def close_symbol(self, db: Session, symbol: str) -> tuple[int, float, list[str]]:
        """Close every OPEN position and cancel every RESTING order for a symbol.

        One-action exit for a scaled (multi-leg) DCA entry so a ladder is never
        left half-managed. Acquires the lock once and reuses the existing close/
        cancel helpers (which assume the caller holds it). Returns
        (num_affected, total_realized_pnl, messages).
        """
        symbol = symbol.upper().strip()
        affected = 0
        total_pnl = 0.0
        msgs: list[str] = []
        with self._lock:
            rows = list(
                db.scalars(
                    self._scope_book(
                        select(Trade).where(
                            Trade.symbol == symbol,
                            Trade.status.in_(
                                [TradeStatus.open.value, TradeStatus.pending.value]
                            ),
                        )
                    )
                ).all()
            )
            for t in rows:
                db.refresh(t)
                if t.status == TradeStatus.pending.value:
                    ok, msg, tr = self._cancel_pending(db, t, "close-all")
                elif t.status == TradeStatus.open.value:
                    ok, msg, tr = self._close_trade(db, t, "close-all")
                else:
                    continue
                if ok:
                    affected += 1
                    if tr is not None:
                        total_pnl += tr.pnl or 0.0
                    msgs.append(msg)
        return affected, total_pnl, msgs


    def _auto_stop(self, price: float, side: str) -> float:
        pct = self.settings.default_stop_loss_pct / 100.0
        return price * (1 - pct) if side == "buy" else price * (1 + pct)

    def _auto_take(self, price: float, side: str) -> float:
        pct = self.settings.default_take_profit_pct / 100.0
        return price * (1 + pct) if side == "buy" else price * (1 - pct)

    def _atr_floored_stop(self, analysis) -> float | None:
        """Autonomous BUY stop: the wider of the fixed % stop and an ATR floor.

        A fixed ``default_stop_loss_pct`` can sit inside normal volatility and get
        knocked out on noise. We ensure the stop is at least ``atr_stop_mult`` ×
        ATR below entry (never tighter than ATR noise). ``execute_signal`` then
        sizes the position against this real distance, so a wider stop shrinks the
        position and keeps risk-per-trade constant. Returns None to fall back to
        ``execute_signal``'s own auto-stop when there's no usable price/ATR.
        """
        price = getattr(analysis, "price", 0.0) or 0.0
        if price <= 0:
            return None
        pct_stop = price * (1 - self.settings.default_stop_loss_pct / 100.0)
        atr_val = getattr(analysis, "atr", 0.0) or 0.0
        mult = getattr(self.settings, "atr_stop_mult", 0.0) or 0.0
        if mult <= 0 or atr_val <= 0:
            return pct_stop if pct_stop > 0 else None
        atr_stop = price - mult * atr_val
        # Lower price = wider (safer) stop for a long; clear the ATR noise band.
        stop = min(pct_stop, atr_stop)
        return stop if stop > 0 else (pct_stop if pct_stop > 0 else None)

    # ---- resting limit orders ---------------------------------------

    def _cancel_pending(
        self, db: Session, trade: Trade, reason: str
    ) -> tuple[bool, str, Optional[Trade]]:
        """Cancel a resting (pending) limit order. Caller holds the lock.

        Releases the paper reservation and cancels the live exchange order.
        """
        if self.settings.is_live and trade.exchange_order_id:
            self.connector.cancel_order(trade.exchange_order_id, trade.symbol)
        else:
            # Paper: give back the notional we reserved when the order was placed.
            self.paper_balance += trade.amount * (trade.limit_price or trade.entry_price)
            save_paper_balance(db, self.paper_balance, self.user_id)

        trade.status = TradeStatus.canceled.value
        trade.closed_at = _utcnow()
        trade.note = (trade.note + " | " if trade.note else "") + reason
        db.commit()
        db.refresh(trade)
        self._emit(
            "order_canceled", {"id": trade.id, "symbol": trade.symbol}
        )
        return True, f"Cancelled resting {trade.side} {trade.symbol}", trade

    def _fill_pending(self, db: Session, trade: Trade, fill_price: float) -> None:
        """Promote a resting limit order to an open position once it fills.

        Caller holds the lock. Paper notional was already reserved at placement,
        so no wallet change happens here. Sets auto SL/TP if none were provided
        and (live) places the protective exchange stop.
        """
        trade.entry_price = fill_price
        trade.status = TradeStatus.open.value
        if trade.stop_loss is None:
            trade.stop_loss = self._auto_stop(fill_price, trade.side)
        if trade.take_profit is None:
            trade.take_profit = self._auto_take(fill_price, trade.side)
        if self.settings.is_live and trade.stop_loss and trade.side == "buy":
            stop_order = self.connector.create_stop_loss_order(
                trade.symbol, "sell", trade.amount, trade.stop_loss
            )
            if stop_order:
                trade.stop_order_id = str(stop_order.get("id"))
        trade.opened_at = _utcnow()
        db.commit()
        db.refresh(trade)
        self._emit(
            "trade_opened",
            {"id": trade.id, "symbol": trade.symbol, "side": trade.side},
        )
        self._notify(
            f"\U0001F4C8 Limit filled <b>{trade.side.upper()}</b> {trade.amount:.8f} "
            f"{trade.symbol} @ {fill_price:.2f} ({self.settings.trading_mode})"
        )

    def check_pending_orders(self, db: Session) -> list[Trade]:
        """Fill resting limit orders whose price has been reached. Returns filled.

        Paper: a buy fills when market <= limit, a sell fills when market >= limit.
        Live: we trust the exchange — poll the order and fill when it reports
        closed/filled, using the exchange's average fill price.
        """
        filled: list[Trade] = []
        stmt = self._scope_book(select(Trade).where(Trade.status == TradeStatus.pending.value))
        for trade in list(db.scalars(stmt).all()):
            limit = trade.limit_price or trade.entry_price
            filled_amt: float | None = None  # live: exchange-reported fill qty
            entry_fee: float = 0.0           # live: real quote fee on the fill
            if self.settings.is_live:
                order = self.connector.fetch_order(
                    trade.exchange_order_id or "", trade.symbol
                )
                if not order:
                    continue
                status = (order.get("status") or "").lower()
                if status in {"canceled", "cancelled", "rejected", "expired"}:
                    with self._lock:
                        db.refresh(trade)
                        if trade.status == TradeStatus.pending.value:
                            self._cancel_pending(db, trade, f"exchange {status}")
                    continue
                # Capital-preservation halt: a tripped max-drawdown kill-switch
                # means NO new risk. Cancel a still-resting live order ON THE
                # EXCHANGE so it can't fill into the very drawdown that halted us.
                # An order the exchange has ALREADY filled is booked below instead
                # — we can't un-fill reality, and pretending we did would desync
                # our book from the real position.
                if self._killswitch_tripped and status not in {"closed", "filled"}:
                    with self._lock:
                        db.refresh(trade)
                        if trade.status == TradeStatus.pending.value:
                            self._cancel_pending(
                                db, trade,
                                "canceled: max-drawdown kill-switch active",
                            )
                    continue
                # Only promote a resting order once the exchange reports it FULLY
                # filled. A partial fill ("open" with a nonzero filled amount) must
                # keep resting — booking it as a complete position would track base
                # we don't fully hold. Sync the tracked size to the actually-filled
                # quantity so venue rounding never leaves us over-reporting.
                if status not in {"closed", "filled"}:
                    continue
                fill_price = float(
                    order.get("average") or order.get("price") or limit
                )
                # Track the REAL filled base qty (net of any base-asset fee) and
                # capture the observed entry fee, so we never over-report holdings
                # nor overstate P&L. Falls back to the tracked size if the venue
                # reports no positive fill.
                filled_amt, entry_fee = self._net_fill(
                    order, trade.symbol, fill_price, trade.side, trade.amount
                )
            else:
                # PAPER fill: only when the market REALLY trades through our limit.
                # Fetch WITHOUT a fallback — `_price(fallback=limit)` would hand back
                # the limit itself during a feed outage, trivially "crossing" it and
                # filling every resting order at its own price on dead data. On any
                # failure skip this tick and re-check once the feed is back.
                try:
                    price = self._price(trade.symbol)
                except Exception:
                    continue
                crossed = (
                    price <= limit if trade.side == "buy" else price >= limit
                )
                if not crossed:
                    continue
                fill_price = limit  # paper fills at the limit price
            with self._lock:
                # Re-read under the lock so a concurrent cancel/fill on another
                # session can't make us fill the same resting order twice.
                db.refresh(trade)
                if trade.status != TradeStatus.pending.value:
                    continue
                if not self.settings.is_live:
                    # A PAPER fill is OUR simulated action, so it must respect the
                    # same capital-preservation gates as a fresh entry:
                    #  • kill-switch tripped (catastrophe, needs human re-arm) →
                    #    cancel the resting order and give back its reservation;
                    #  • daily-loss breaker (auto-resets at UTC midnight) → don't
                    #    fill today, but LEAVE it resting to be re-evaluated later.
                    # (A LIVE order the exchange already filled is booked as-is:
                    #  the fill really happened, so recording it keeps our book
                    #  honest; the kill-switch still blocks the NEXT new entry.)
                    if self._killswitch_tripped:
                        self._cancel_pending(
                            db, trade,
                            "canceled: max-drawdown kill-switch active",
                        )
                        continue
                    if self._daily_loss_hit(db):
                        continue
                if filled_amt is not None:
                    trade.amount = filled_amt
                    trade.fee = (trade.fee or 0.0) + entry_fee
                self._fill_pending(db, trade, fill_price)
            filled.append(trade)
        return filled

    def _exchange_stop_filled_price(self, trade: Trade) -> Optional[float]:
        """Average fill price if this trade's exchange-side stop has ALREADY fired.

        A live stop-loss is placed as a real resting order on the venue. If price
        gaps through it the exchange fills it and the base asset is gone — but our
        in-process monitor still sees the trade as open and tries to market-close
        it, which the exchange rejects with -2010 (insufficient balance) on every
        tick, wedging the trade open forever. Detecting the already-filled stop
        lets us book the close at the REAL fill price instead of looping.

        Returns the fill price when the stop order reports closed/filled with a
        positive filled quantity, else None (unknown / still resting / no stop).
        Never fabricates: on any fetch failure we return None and let the normal
        market-close path run and surface any real error.
        """
        if not (self.settings.is_live and trade.stop_order_id):
            return None
        try:
            order = self.connector.fetch_order(trade.stop_order_id, trade.symbol)
        except Exception:
            return None
        if not order:
            return None
        status = (order.get("status") or "").lower()
        filled = float(order.get("filled") or 0)
        if status in {"closed", "filled"} and filled > 0:
            # Prefer the venue's average fill; fall back to the stop trigger we
            # set (a real level we chose, never an invented number) only if the
            # venue reports no price.
            return float(
                order.get("average")
                or order.get("price")
                or trade.stop_loss
                or trade.entry_price
            )
        return None

    def _close_trade(
        self, db: Session, trade: Trade, reason: str,
        fill_price: float | None = None,
    ) -> tuple[bool, str, Optional[Trade]]:
        """Close an open trade. Caller holds the lock.

        ``fill_price``, when given, is the price the caller already observed for
        this close (e.g. the monitor tick that tripped a stop/target, capped at
        the level by :meth:`_paper_exit_fill`). On PAPER it is booked directly,
        avoiding a redundant re-fetch and the ``entry_price`` fallback. On LIVE the
        REAL exchange fill always governs; ``fill_price`` only serves as the
        last-resort ticker fallback if the venue reports no average.
        """
        if fill_price is not None:
            price = fill_price
        else:
            price = self._price(trade.symbol, fallback=trade.entry_price)

        if self.settings.is_live:
            # Read-only key? Say so honestly BEFORE touching the exchange, so the
            # user gets the same clear "enable Spot" guidance as on the open path
            # instead of a raw -2015 after they've confirmed the close.
            ro = self._live_readonly_block()
            if ro:
                return False, ro, None
            # If the protective exchange-side stop has ALREADY fired, the base
            # asset is gone: a fresh market close would hit -2010 forever and
            # wedge the trade open. Book the close at the stop's REAL fill price.
            if trade.stop_order_id:
                stop_fill = self._exchange_stop_filled_price(trade)
                if stop_fill is not None:
                    trade.stop_order_id = None
                    return self._book_close(
                        db, trade, stop_fill,
                        f"{reason} | exchange stop already filled",
                    )
                # Not filled yet — cancel it before we market-close, so it can't
                # fire later against a position we no longer hold.
                self.connector.cancel_order(trade.stop_order_id, trade.symbol)
                trade.stop_order_id = None
            close_side = "sell" if trade.side == "buy" else "buy"
            try:
                close_order = self.connector.create_market_order(
                    trade.symbol, close_side, trade.amount
                )
            except Exception as exc:
                return False, self._friendly_exchange_error(exc, "close"), None
            # Book PnL at the price we ACTUALLY got, not the pre-trade ticker: a
            # market-close fill can differ from the last quote (slippage/spread).
            # Fall back to the ticker price if the venue reports no average.
            if close_order:
                price = float(
                    close_order.get("average") or close_order.get("price") or price
                )
                # Add the REAL exit fee to the trade's fee tally so realized P&L
                # is booked net of both legs (entry fee was captured at open).
                _, exit_fee, _ = self._fill_details(close_order, trade.symbol, price)
                trade.fee = (trade.fee or 0.0) + exit_fee

        return self._book_close(db, trade, price, reason)

    def _book_close(
        self, db: Session, trade: Trade, price: float, reason: str
    ) -> tuple[bool, str, Optional[Trade]]:
        """Book a closed trade: realize PnL, refund the paper wallet, close the
        DB row, record a losing exit for the re-entry cooldown, emit + notify.

        Caller holds the lock and has ALREADY done any live exchange work (the
        market-close, or determined the fill price of an exchange-side stop that
        fired). Shared by the normal market-close path and the already-filled-stop
        path so every close books identically and honestly.
        """
        pnl = self._realized_pnl(trade, price)
        if not self.settings.is_live:
            # Return notional + pnl to the paper wallet.
            self.paper_balance += trade.amount * trade.entry_price + pnl
            save_paper_balance(db, self.paper_balance, self.user_id)

        trade.exit_price = price
        trade.pnl = pnl
        trade.status = TradeStatus.closed.value
        trade.closed_at = _utcnow()
        trade.note = (trade.note + " | " if trade.note else "") + reason
        db.commit()
        db.refresh(trade)
        # Remember a LOSING exit so the anti-whipsaw re-entry cooldown can keep
        # the bot from immediately buying back into the same falling symbol.
        if pnl < 0:
            self._last_loss_exit[trade.symbol] = _utcnow()
        self._emit("trade_closed", {"id": trade.id, "symbol": trade.symbol, "pnl": pnl})
        self._notify(
            f"\U0001F4B0 Closed {trade.symbol} @ {price:.2f} | PnL <b>{pnl:.2f}</b> "
            f"({reason})"
        )
        return True, f"Closed {trade.symbol} @ {price:.2f} (PnL {pnl:.2f})", trade

    def close_by_id(
        self, db: Session, trade_id: int
    ) -> tuple[bool, str, Optional[Trade]]:
        """Close (or cancel) ONE specific trade by id — the exact row the operator
        asked for, never "some open trade for this symbol".

        Closing by symbol via :meth:`execute_signal` is ambiguous when a symbol
        holds several legs (a DCA ladder): it closes whichever row the query
        returns first, which may not be the one the user clicked. This targets the
        precise trade and enforces book isolation — a trade from the OTHER book
        (paper while the bot is live, or the reverse) is refused rather than
        closed with a real exchange order against the wrong book.
        """
        with self._lock:
            trade = db.get(Trade, trade_id)
            if trade is None or (
                self.user_id is not None and trade.user_id != self.user_id
            ):
                return False, "Trade not found.", None
            if trade.status not in (
                TradeStatus.open.value,
                TradeStatus.pending.value,
            ):
                return False, "That trade is not open or pending.", None
            if (trade.mode or "paper") != self.settings.trading_mode:
                return (
                    False,
                    f"That is a {trade.mode} position but the bot is in "
                    f"{self.settings.trading_mode} mode — switch modes to close it.",
                    None,
                )
            if trade.status == TradeStatus.pending.value:
                return self._cancel_pending(db, trade, "manual cancel")
            return self._close_trade(db, trade, "manual close")

    def _realized_pnl(self, trade: Trade, exit_price: float) -> float:
        if trade.side == "buy":
            gross = (exit_price - trade.entry_price) * trade.amount
        else:
            gross = (trade.entry_price - exit_price) * trade.amount
        # PAPER mode charges a realistic, configurable taker fee on BOTH legs so
        # the simulated wallet reflects the true cost of trading (fees are a real
        # drag every trader pays). This is honest simulation. LIVE P&L subtracts
        # the REAL fees the venue reported (trade.fee, in quote), captured on the
        # entry and exit fills — never a fee we invented, never zero-by-omission.
        if not self.settings.is_live:
            fee_pct = max(getattr(self.settings, "paper_taker_fee_pct", 0.0) or 0.0, 0.0)
            if fee_pct > 0:
                fee_rate = fee_pct / 100.0
                gross -= (trade.entry_price + exit_price) * trade.amount * fee_rate
            return gross
        return gross - (getattr(trade, "fee", 0.0) or 0.0)

    @staticmethod
    def _fill_details(
        order: dict | None, symbol: str, fill_price: float
    ) -> tuple[float, float, float]:
        """Parse a ccxt order into ``(filled_base, fee_quote, fee_base)`` — the
        REAL economic result of a live fill, never an invented one.

        * ``filled_base`` — base quantity the venue reports filled
          (``order['filled']``); ``0.0`` when absent/unparseable.
        * ``fee_quote`` — total fee expressed in the QUOTE asset, summed over
          every fee entry we can value from data we actually have: a quote fee is
          taken as-is; a base-asset fee is valued at ``fill_price`` (a real fill
          price, not a guess). A fee charged in a THIRD asset (e.g. BNB) is left
          OUT rather than converted with a price we never observed — we under-
          count that rare case instead of fabricating a number.
        * ``fee_base`` — the portion charged in the BASE asset. On a spot BUY the
          venue takes this out of the coins you receive, so it reduces the amount
          you can later sell; booking the gross fill over-reports holdings and a
          later close hits Binance -2010 (insufficient balance).

        Handles both ccxt fee shapes: the itemised ``order['fees']`` list when
        present (authoritative — avoids double counting), else single ``fee``.
        """
        if not order:
            return 0.0, 0.0, 0.0
        try:
            filled_base = float(order.get("filled") or 0.0)
        except (TypeError, ValueError):
            filled_base = 0.0
        if filled_base < 0:
            filled_base = 0.0
        base = quote = ""
        if symbol and "/" in symbol:
            base_part, _, rest = symbol.partition("/")
            base = base_part.upper()
            quote = rest.split(":", 1)[0].upper()  # drop any ":settle" suffix
        entries = order.get("fees")
        if not isinstance(entries, list) or not entries:
            single = order.get("fee")
            entries = [single] if isinstance(single, dict) else []
        fee_quote = 0.0
        fee_base = 0.0
        for f in entries:
            if not isinstance(f, dict):
                continue
            try:
                cost = float(f.get("cost") or 0.0)
            except (TypeError, ValueError):
                cost = 0.0
            if cost <= 0:
                continue
            cur = str(f.get("currency") or "").upper()
            if quote and cur == quote:
                fee_quote += cost
            elif base and cur == base:
                fee_base += cost
                if fill_price > 0:
                    fee_quote += cost * fill_price
            # else: fee in a third asset we can't value from real data — skip it
            # rather than convert with a price we never observed.
        return filled_base, fee_quote, fee_base

    def _net_fill(
        self, order: dict | None, symbol: str, fill_price: float,
        side: str, requested_qty: float,
    ) -> tuple[float, float]:
        """Return ``(net_base_qty, entry_fee_quote)`` for a live open fill.

        ``net_base_qty`` is what we can actually SELL later: the venue-reported
        filled quantity, minus any base-asset fee the venue skimmed off a BUY.
        Falls back to ``requested_qty`` when the venue reports no positive fill.
        """
        filled_base, fee_quote, fee_base = self._fill_details(order, symbol, fill_price)
        qty = filled_base if filled_base > 0 else requested_qty
        if side == "buy" and fee_base > 0:
            qty = max(qty - fee_base, 0.0)
        return qty, fee_quote


    @staticmethod
    def unrealized_pnl(trade: Trade, price: float) -> float:
        if trade.side == "buy":
            return (price - trade.entry_price) * trade.amount
        return (trade.entry_price - price) * trade.amount

    @staticmethod
    def _cap_fill_at_level(
        side: str, hit: str, observed: float, level: float | None
    ) -> float:
        """Cap a PAPER stop/target exit fill at its LEVEL when the observed tick
        has overshot it.

        This bot places REAL resting stop/target orders on live, which fire AT
        their trigger. But the paper monitor only samples price every ~5s, so by
        the time it notices a stop was crossed the tick can be well past the
        level — and booking that raw overshoot fabricates an exit no resting order
        would have gotten (extra profit on a target, or a deeper-than-real loss on
        a stop, purely from poll timing). Cap at the level so paper matches how the
        live resting order actually behaves. Returns the price to book.

        ``level`` None/≤0 (no level set) → book the observed tick unchanged.
        """
        if not level or level <= 0:
            return observed
        if side == "buy":  # long: stop below entry, target above
            return max(observed, level) if hit == "stop-loss" else min(observed, level)
        # short: stop above entry, target below
        return min(observed, level) if hit == "stop-loss" else max(observed, level)

    def _paper_exit_fill(self, trade: Trade, observed: float, hit: str) -> float | None:
        """Price to book a monitor-triggered close at, or None on live.

        On LIVE the REAL exchange fill governs (``None`` lets ``_close_trade`` use
        the exchange average). On PAPER we book the price the monitor actually
        observed — capped at the stop/target LEVEL for a stop/target hit (see
        :meth:`_cap_fill_at_level`); a reversal/other close books the raw tick.
        """
        if self.settings.is_live:
            return None
        if hit == "stop-loss":
            return self._cap_fill_at_level(trade.side, hit, observed, trade.stop_loss)
        if hit == "take-profit":
            return self._cap_fill_at_level(trade.side, hit, observed, trade.take_profit)
        return observed

    # ---- monitoring (stop-loss / take-profit) ------------------------

    def check_open_positions(self, db: Session) -> list[tuple[Trade, str]]:
        """Check SL/TP for all open trades and close those that hit. Returns closed."""
        closed: list[tuple[Trade, str]] = []
        stmt = self._scope_book(select(Trade).where(Trade.status == TradeStatus.open.value))
        for trade in list(db.scalars(stmt).all()):
            try:
                # Fetch WITHOUT a fallback: during a real data outage we must NOT
                # silently pretend the price is the entry price (which reads as
                # "no trigger" and hides that protection is degraded).
                price = self._price(trade.symbol)
            except Exception as exc:
                # Can't evaluate this position's stop/target right now. Surface it
                # once per symbol instead of skipping in silence, so the operator
                # knows in-process protection is paused. On LIVE the exchange-side
                # stop order remains the backstop.
                if trade.symbol not in self._monitor_degraded:
                    self._monitor_degraded.add(trade.symbol)
                    self._emit(
                        "monitor_degraded",
                        {"symbol": trade.symbol, "error": str(exc)},
                    )
                    backstop = (
                        " Exchange-side stop still protects it."
                        if self.settings.is_live
                        else ""
                    )
                    self._notify(
                        f"⚠️ Price feed for {trade.symbol} is unreachable — "
                        f"in-process stop/target checks are paused for it.{backstop}"
                    )
                continue
            if trade.symbol in self._monitor_degraded:
                # Feed recovered: clear the flag and let the operator know.
                self._monitor_degraded.discard(trade.symbol)
                self._emit("monitor_recovered", {"symbol": trade.symbol})
                self._notify(
                    f"✅ Price feed for {trade.symbol} recovered — stop/target "
                    f"checks resumed."
                )
            self._maybe_trail_stop(db, trade, price)
            self._maybe_lock_profit(db, trade, price)
            hit: str | None = None
            if trade.side == "buy":
                if trade.stop_loss and price <= trade.stop_loss:
                    hit = "stop-loss"
                elif trade.take_profit and price >= trade.take_profit:
                    hit = "take-profit"
            else:  # sell / short
                if trade.stop_loss and price >= trade.stop_loss:
                    hit = "stop-loss"
                elif trade.take_profit and price <= trade.take_profit:
                    hit = "take-profit"
            if hit is None and self._should_take_profit_on_reversal(db, trade, price):
                hit = "reversal"
            if hit:
                reason = (
                    "banked profit on confirmed reversal"
                    if hit == "reversal" else f"{hit} triggered"
                )
                # Paper books at the level (capped on overshoot) so a gap past the
                # stop/target can't fabricate a better- or worse-than-real fill;
                # live passes None so the REAL exchange fill governs.
                fill = self._paper_exit_fill(trade, price, hit)
                with self._lock:
                    # Re-read under the lock: a manual close (on a different DB
                    # session) may have closed this trade between our SELECT and
                    # acquiring the lock. Closing again would double the order.
                    db.refresh(trade)
                    if trade.status != TradeStatus.open.value:
                        continue
                    ok, _msg, _t = self._close_trade(db, trade, reason, fill_price=fill)
                self._reversal_flags.pop(trade.id, None)
                if ok:
                    closed.append((trade, hit))
        return closed

    def _move_exchange_stop(self, trade: Trade, new_stop: float) -> None:
        """Move the LIVE exchange-side protective stop to ``new_stop`` for the
        full held quantity. Caller holds the lock and has confirmed the trade is
        still open. No-op in paper mode.

        On spot the resting stop reserves the base asset, so a replacement can't
        be placed until the old one is cancelled — we cancel first, then create,
        and keep that brief window honest and crash-proof:
          • can't cancel the old stop → leave it in place (still protective at the
            old level) and bail, rather than stack a second the venue would reject
            for the now-reserved base;
          • cancelled but can't re-place → drop the id and tell the operator the
            exchange stop is off (the in-process stop still enforces the level
            while the bot runs).
        The caller always updates ``trade.stop_loss`` first, so the new level is
        enforced in-process regardless of the exchange outcome.
        """
        if not self.settings.is_live:
            return
        side = "sell" if trade.side == "buy" else "buy"
        old = trade.stop_order_id
        if old:
            try:
                self.connector.cancel_order(old, trade.symbol)
            except Exception as exc:
                self._emit(
                    "stop_move_failed",
                    {"id": trade.id, "stage": "cancel", "error": str(exc)},
                )
                return
            trade.stop_order_id = None
        try:
            placed = self.connector.create_stop_loss_order(
                trade.symbol, side, trade.amount, new_stop
            )
        except Exception as exc:
            placed = None
            self._emit(
                "stop_move_failed",
                {"id": trade.id, "stage": "place", "error": str(exc)},
            )
        if placed:
            trade.stop_order_id = str(placed.get("id"))
        else:
            self._notify(
                f"⚠️ {trade.symbol}: couldn't move the exchange stop-loss. The "
                f"in-app stop at {new_stop:g} is still active while the bot runs, "
                f"but the exchange-side safety net is off — check your API keys "
                f"and that the market allows stop orders."
            )

    def _maybe_trail_stop(self, db: Session, trade: Trade, price: float) -> None:
        """Ratchet a long position's stop-loss upward as price rises.

        Only tightens (raises) the stop, never loosens it, and only for longs.
        Disabled when trailing_stop_pct is 0. The mutation and any exchange-side
        stop move run UNDER THE LOCK after re-reading the row, so a close racing
        on another session can't leave an orphaned resting stop on the venue.
        """
        pct = self.settings.trailing_stop_pct
        if pct <= 0 or trade.side != "buy":
            return
        candidate = price * (1 - pct / 100.0)
        # Cheap pre-check on the (possibly stale) row; re-verified under the lock.
        if not (candidate < price and (trade.stop_loss is None or candidate > trade.stop_loss)):
            return
        with self._lock:
            db.refresh(trade)
            if trade.status != TradeStatus.open.value:
                return  # closed underneath us — never place a stop for it
            if trade.stop_loss is not None and candidate <= float(trade.stop_loss):
                return  # already trailed at least this far
            trade.stop_loss = candidate
            self._move_exchange_stop(trade, candidate)
            db.commit()
        self._emit(
            "stop_trailed",
            {"id": trade.id, "symbol": trade.symbol, "stop_loss": candidate},
        )

    def _round_trip_fee_pct(self) -> float:
        """Approximate round-trip cost (%) of a trade: taker fee on entry + exit.

        Used only as an internal SAFETY FLOOR so profit-lock never "banks" a gain
        thinner than the fees that would erase it. When no paper fee is configured
        we fall back to ~0.1%/leg (typical Binance spot taker) — a conservative
        estimate for the floor, never a figure shown to the user as if measured.
        """
        try:
            fee = float(getattr(self.settings, "paper_taker_fee_pct", 0.0) or 0.0)
        except (ValueError, TypeError):
            fee = 0.0
        if fee <= 0:
            fee = 0.1
        return fee * 2.0

    def _maybe_lock_profit(self, db: Session, trade: Trade, price: float) -> None:
        """Bank a winning long early by ratcheting the stop into profit.

        The autopilot profit-taking asked for: once an open long is up by at least
        ``profit_lock_trigger_pct`` (and by more than round-trip fees, so the exit
        is genuinely net-positive), raise the stop to a small profit floor above
        entry. A later "red-flag" pullback then closes the trade in the green
        instead of giving the gain back. Raise-only, never above the live price;
        on LIVE the exchange-side stop is moved too (cancel + replace). Longs only.

        This does NOT withdraw to a bank: a closed live trade realizes to USDT in
        the Binance spot wallet (real, spendable) — as far as an API key without
        withdrawal permission can, or should, go.
        """
        if not getattr(self.settings, "profit_lock_enabled", False) or trade.side != "buy":
            return
        entry = float(trade.entry_price or 0)
        if entry <= 0 or price <= 0:
            return
        gain_pct = (price - entry) / entry * 100.0
        trigger = max(
            float(getattr(self.settings, "profit_lock_trigger_pct", 1.0) or 0.0),
            self._round_trip_fee_pct(),
        )
        if gain_pct < trigger:
            return
        # Lock a floor that is net-positive after fees, and never at/above price.
        floor_pct = max(
            float(getattr(self.settings, "profit_lock_floor_pct", 0.3) or 0.0),
            self._round_trip_fee_pct(),
        )
        candidate = entry * (1 + floor_pct / 100.0)
        if candidate >= price:
            return
        if trade.stop_loss is not None and candidate <= float(trade.stop_loss):
            return  # existing stop is already at least this protective
        with self._lock:
            db.refresh(trade)
            if trade.status != TradeStatus.open.value:
                return  # closed underneath us — never place a stop for it
            if trade.stop_loss is not None and candidate <= float(trade.stop_loss):
                return
            trade.stop_loss = candidate
            self._move_exchange_stop(trade, candidate)
            db.commit()
        self._emit(
            "profit_locked",
            {"id": trade.id, "symbol": trade.symbol, "stop_loss": candidate,
             "locked_pct": round(floor_pct, 3)},
        )
        self._notify(
            f"🔒 {trade.symbol}: up {gain_pct:.2f}% — stop raised to lock in "
            f"~{floor_pct:.2f}% ({candidate:g}). A pullback now banks the gain."
        )

    def _should_take_profit_on_reversal(self, db: Session, trade: Trade, price: float) -> bool:
        """True when a NET-POSITIVE long should be banked because the read flipped
        bearish and STAYED bearish long enough to confirm (anti-whipsaw).

        The active half of the profit-take, built for a hands-off operator: even
        before the locked stop is hit, if the position is genuinely in profit
        (after round-trip fees) and a fresh analysis turns bearish for
        ``reversal_confirm_count`` reads in a row (the confirmed "red flag"), close
        now to keep the gain. It only ever sells a WINNER — a dip into a loss is
        left to the stop, never realized early — and a single noisy bearish tick is
        ignored. Opt-in via ``take_profit_on_reversal``; longs only. On exit the
        proceeds (stake + net gain) land back as USDT, ready to re-enter on the next
        good signal (a winning exit has no re-entry cooldown).
        """
        if not getattr(self.settings, "take_profit_on_reversal", False) or trade.side != "buy":
            return False
        notional = float(trade.entry_price or 0) * float(trade.amount or 0)
        if notional <= 0:
            return False
        net = self.unrealized_pnl(trade, price) - notional * self._round_trip_fee_pct() / 100.0
        if net <= 0:
            self._reversal_flags.pop(trade.id, None)  # not a net winner -> reset streak
            return False
        try:
            analysis = self.analyze_symbol(
                trade.symbol, self.settings.auto_timeframe or "1h", apply_strategy=True
            )
        except Exception:
            return False  # can't read -> don't force an exit
        if getattr(analysis, "verdict", "hold") != "sell":
            self._reversal_flags.pop(trade.id, None)  # red flag cleared -> reset streak
            return False
        need = max(1, int(getattr(self.settings, "reversal_confirm_count", 2) or 1))
        seen = self._reversal_flags.get(trade.id, 0) + 1
        self._reversal_flags[trade.id] = seen
        return seen >= need

    def analyze_symbol(
        self, symbol: str, timeframe: str = "1h", limit: int = 200,
        apply_strategy: bool = False,
    ):
        """Fetch candles and run the deterministic market analyzer.

        The decision is made on CLOSED bars only — a live feed's last candle is
        still forming, and acting on it would repaint the signal and diverge from
        the backtest that validated it (see MarketAnalyzer.analyze_live). The live
        price is kept for display and stop-anchoring.

        When ``apply_strategy`` is set (autonomous + observe paths), a saved,
        trained strategy for this symbol can OVERRIDE the analyzer verdict — but
        only if the user opted in and never against the capital-preservation
        gates (see _apply_saved_strategy). The saved strategy runs on the SAME
        closed-bar frame, so it can't repaint either.
        """
        import pandas as pd

        raw = self.connector.fetch_ohlcv(symbol.upper(), timeframe, min(limit, 1000))
        if not raw:
            raise RuntimeError(f"No candle data for {symbol}")
        df = pd.DataFrame(
            raw, columns=["timestamp", "open", "high", "low", "close", "volume"]
        )
        analysis, closed = self.analyzer.analyze_live(df, symbol.upper())
        if apply_strategy:
            analysis = self._apply_saved_strategy(symbol, closed, analysis)
        return analysis

    def auto_trade_symbol(
        self, db: Session, symbol: str, timeframe: str = "1h"
    ) -> tuple[bool, str]:
        """Analyse one symbol and act only on a confident buy/sell verdict.

        Capital-preservation rules:
        - HOLD verdicts never open or close anything.
        - A confident SELL closes an existing long but does NOT open a short
          (spot, long-only autonomous mode) — avoids doubling risk on noise.
        """
        try:
            analysis = self.analyze_symbol(symbol, timeframe, apply_strategy=True)
        except Exception as exc:
            return False, f"analysis failed for {symbol}: {exc}"
        self._record_regime(symbol, analysis)
        ok, msg = self._decide_and_act(db, symbol, timeframe, analysis)
        # Persist the brain's OWN verdict (deduped on change) so the Signals tab
        # shows an honest timeline of autonomous decisions, not just TradingView
        # alerts. Logging must never break the trading loop.
        try:
            self._log_auto_verdict(db, symbol.upper(), analysis, ok, msg, timeframe)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("verdict logging failed for %s: %s", symbol, exc)
        return ok, msg

    def _decide_and_act(
        self, db: Session, symbol: str, timeframe: str, analysis
    ) -> tuple[bool, str]:
        """Decide and execute from a computed analysis. Returns (accepted, msg)."""
        if analysis.verdict == "hold":
            return False, f"{symbol}: hold ({analysis.confidence:.0%})"

        # Multi-timeframe confirmation: refuse to act against a higher timeframe.
        _confirm_tf = (self.settings.auto_confirm_timeframe or "").strip()
        if _confirm_tf and _confirm_tf != timeframe:
            try:
                _higher = self.analyze_symbol(symbol, _confirm_tf, apply_strategy=True)
            except Exception as exc:
                return False, f"{symbol}: confirm timeframe {_confirm_tf} failed: {exc}"
            if analysis.verdict == "buy" and _higher.verdict == "sell":
                return False, f"{symbol}: buy blocked - {_confirm_tf} reads sell ({_higher.confidence:.0%})"
            if analysis.verdict == "sell" and _higher.verdict == "buy":
                return False, f"{symbol}: exit blocked - {_confirm_tf} reads buy ({_higher.confidence:.0%})"
        with self._lock:
            existing = self._open_trade_for_symbol(db, symbol.upper())

        if analysis.verdict == "buy":
            if existing:
                return False, f"{symbol}: already long"
            # Anti-whipsaw re-entry cooldown + consecutive-loss circuit breaker.
            # The bear-regime pause was already applied at analysis time (a
            # bear-regime BUY is turned to HOLD upstream), so it isn't re-checked
            # here. execute_signal enforces the same gates for webhook entries.
            ok_guard, guard_reason = self._entry_guards(db, symbol)
            if not ok_guard:
                return False, guard_reason
            # Permission-gated AI review of the ENTRY. Risk-first and veto-only:
            # it can BLOCK new risk but never invent a trade, and if the AI is
            # unavailable it falls back to the deterministic decision. Governed by
            # ai_trade_confirm (off by default) — the risk manager still applies.
            if getattr(self.settings, "ai_trade_confirm", False) and self.ai.available:
                proceed, reason = self.ai.confirm_trade(analysis)
                if not proceed:
                    return False, f"{symbol}: {reason}"
            # Explanatory AI pre-trade rationale (opt-in, fails safe). Never decides
            # or blocks — it just says WHY in plain language for a hands-off user.
            note = f"auto: {analysis.summary}"
            if getattr(self.settings, "ai_pretrade_analysis", False) and self.ai.available:
                try:
                    rationale = self.ai.pretrade_analysis(analysis)
                except Exception:
                    rationale = ""
                if rationale:
                    note = f"auto: {rationale}"
                    self._emit("pretrade_analysis", {
                        "symbol": symbol.upper(),
                        "text": rationale,
                        "confidence": round(analysis.confidence, 3),
                    })
            # Use an ATR-floored stop so a fixed % stop can't sit inside noise;
            # execute_signal sizes the position against this real stop distance.
            ok, msg, _ = self.execute_signal(
                db, action="buy", symbol=symbol, amount=None,
                stop_loss=self._atr_floored_stop(analysis), take_profit=None,
                source="auto", note=note,
            )
            return ok, msg
        # sell verdict: close a long if we hold one, else stand aside.
        if existing and existing.side == "buy":
            ok, msg, _ = self.execute_signal(
                db, action="close", symbol=symbol, amount=None,
                stop_loss=None, take_profit=None, source="auto",
                note=f"auto exit: {analysis.summary}",
            )
            return ok, msg
        return False, f"{symbol}: sell signal, no long to close"

    def _log_auto_verdict(
        self, db: Session, symbol: str, analysis, acted: bool, message: str,
        timeframe: str = "1h",
    ) -> None:
        """Record an autonomous verdict — but only when it CHANGES for a symbol.

        A stable trend would otherwise write a near-identical row every ~5s
        tick; de-duping on the verdict keeps the log a compact, readable
        timeline. These are the brain's real, already-made decisions (nothing
        fabricated), so persisting them honours the "nothing fake" rule.
        """
        prev = self._last_auto_verdict.get(symbol)
        if prev == analysis.verdict:
            return
        self._last_auto_verdict[symbol] = analysis.verdict
        detail = analysis.summary
        if message and message != analysis.summary:
            detail = f"{analysis.summary} — {message}"
        log = SignalLog(
            user_id=self.user_id,
            source="analyzer",
            symbol=symbol,
            action=analysis.verdict,
            raw=json.dumps(analysis.as_dict()),
            accepted=1 if acted else 0,
            message=detail,
        )
        db.add(log)
        db.commit()
        db.refresh(log)
        # The real factors that drove THIS decision (name, direction, weight).
        # Sent verbatim from the analyzer so the UI can show the operator WHY the
        # brain decided — and map factors to the matching chart indicators —
        # without re-fetching. Nothing here is fabricated: it is the analysis
        # that was just made. Kept compact (no free-text detail) for the wire.
        factors = [
            {"name": f.name, "signal": f.signal, "weight": round(f.weight, 3)}
            for f in getattr(analysis, "factors", [])
        ]
        self._emit(
            "signal",
            {
                "id": log.id,
                "source": "analyzer",
                "symbol": symbol,
                "action": analysis.verdict,
                "accepted": bool(acted),
                "confidence": round(analysis.confidence, 3),
                "message": detail,
                "timeframe": timeframe,
                "factors": factors,
            },
        )

    def observe_symbol(
        self, db: Session, symbol: str, timeframe: str = "1h"
    ) -> tuple[bool, str]:
        """Analyse a symbol and LOG the verdict WITHOUT trading.

        Used when autonomous execution is off, so the Signals tab still shows the
        brain's live read of the market (honest, deduped on change). No order is
        ever placed here — this observes and records only.
        """
        try:
            analysis = self.analyze_symbol(symbol, timeframe, apply_strategy=True)
        except Exception as exc:
            return False, f"analysis failed for {symbol}: {exc}"
        self._record_regime(symbol, analysis)
        try:
            self._log_auto_verdict(
                db, symbol.upper(), analysis, False,
                "monitoring — autonomous execution off", timeframe,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("verdict logging failed for %s: %s", symbol, exc)
        return True, f"{symbol}: {analysis.verdict} ({analysis.confidence:.0%}) observed"

    def _record_regime(self, symbol: str, analysis) -> None:
        """Snapshot the market regime for a symbol so status() can SHOW the bot
        standing aside in a bad market and re-engaging in a good one — the visible
        pause/resume the user asked for. Pure observation of the analyzer's own
        regime factor and protective holds; nothing here is fabricated.
        """
        regime, detail = "neutral", ""
        for f in getattr(analysis, "factors", []):
            if f.name == "regime":
                regime = {"sell": "bear", "buy": "bull"}.get(f.signal, "neutral")
                detail = getattr(f, "detail", "") or ""
                break
        protective = self._protective_hold(analysis)
        pause_bear = getattr(self.settings, "auto_pause_in_bear", True)
        # A new long is stood aside here iff the analyzer is protecting capital, or
        # we're in a bear regime AND the operator opted to pause in bears.
        paused = protective or (regime == "bear" and pause_bear)
        try:
            self._last_regime[symbol.upper()] = {
                "regime": regime,
                "detail": detail,
                "protective_hold": protective,
                "entries_paused": paused,
                "verdict": getattr(analysis, "verdict", "hold"),
                "at": _utcnow().isoformat(),
            }
        except Exception:  # pragma: no cover - never let bookkeeping break trading
            pass

    # ---- status ------------------------------------------------------

    def status(self, db: Session) -> dict[str, Any]:
        open_trades = list(
            db.scalars(
                self._scope_book(
                    select(Trade).where(Trade.status == TradeStatus.open.value)
                )
            ).all()
        )
        unrealized = 0.0
        position_value = 0.0
        prices_stale = False  # True if the feed can't value ≥1 open position now
        for t in open_trades:
            try:
                # No entry_price fallback here: pretending "price == entry" during
                # a feed outage would report a FAKE break-even equity/unrealized.
                # Mark the totals stale instead and let the UI show "-".
                price = self._price(t.symbol)
            except Exception:
                prices_stale = True
                continue
            u = self.unrealized_pnl(t, price)
            unrealized += u
            # Current market value of an open position = its entry notional plus
            # its unrealized PnL. In BOTH modes `balance` is FREE cash *after* the
            # entry notional was taken out (paper: reserved on open; live: spent
            # on the real buy), so the position's value must be added back for an
            # honest total-equity figure instead of understating by the notional.
            position_value += t.entry_price * t.amount + u

        realized = float(
            sum(
                t.pnl
                for t in db.scalars(
                    self._scope_book(
                        select(Trade).where(Trade.status == TradeStatus.closed.value)
                    )
                ).all()
            )
        )
        balance = self._equity(db)
        # Circuit-breaker / pause visibility (honest runtime state, not settings).
        max_streak = int(getattr(self.settings, "max_consecutive_losses", 0) or 0)
        try:
            streak = self._consecutive_losses(db)
        except Exception:
            streak = 0
        entries_paused, pause_reason = False, None
        if self._killswitch_tripped:
            entries_paused, pause_reason = True, "drawdown kill-switch tripped"
        elif max_streak > 0 and streak >= max_streak:
            entries_paused, pause_reason = True, f"{streak} consecutive losses (circuit breaker)"
        elif not self.running:
            entries_paused, pause_reason = True, "bot stopped — new entries halted"
        return {
            "running": self.running,
            "trading_mode": self.settings.trading_mode,
            "testnet": self.settings.binance_testnet,
            "exchange_connected": self.connector.connected,
            "open_positions": len(open_trades),
            "balance": balance,
            # Equity / unrealized are NULL (not a fabricated break-even) whenever a
            # position can't be priced, so the UI shows "-". Free cash is still
            # honest and reported. `prices_stale` lets the UI flag the degraded read.
            "equity": None if prices_stale else balance + position_value,
            "realized_pnl": realized,
            "unrealized_pnl": None if prices_stale else unrealized,
            "prices_stale": prices_stale,
            "day_pnl": self.risk.day_realized_pnl(db),
            "max_open_positions": self.settings.max_open_positions,
            # Risk-safeguard state (honest, in-memory): the kill-switch flag lets
            # the UI show a clear HALTED state instead of a silently idle bot.
            "killswitch": self._killswitch_tripped,
            "max_drawdown_pct": getattr(self.settings, "max_drawdown_pct", 0.0),
            "peak_equity": round(self._peak_equity, 2),
            # Autopilot / pause-resume visibility.
            "auto_trade_enabled": bool(getattr(self.settings, "auto_trade_enabled", False)),
            "consecutive_losses": streak,
            "max_consecutive_losses": max_streak,
            "entries_paused": entries_paused,
            "entries_pause_reason": pause_reason,
            # Per-symbol regime snapshots (bull/bear/neutral + whether new longs are
            # stood aside there). Empty until the monitor has analysed each symbol.
            "regimes": dict(self._last_regime),
        }
