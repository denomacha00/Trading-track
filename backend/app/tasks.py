"""Background loops: per-user market monitoring and status broadcasting."""
from __future__ import annotations

import asyncio
import logging
import time

from sqlalchemy import select

from app.database import SessionLocal
from app.engine import TradingEngine
from app.models import PriceAlert, Trade, TradeStatus, _utcnow
from app.news import fetch_market_news
from app.state import load_monitor_interval
from app.ws import Broadcaster

logger = logging.getLogger(__name__)

# ---- Live monitor: cooldown so the same call-out never spams -----------
# The loop ticks every few seconds; without a cooldown a persistently-true
# condition (a red trade, price hovering just above a stop) would repeat every
# single tick. We remember when we last spoke a given (user, trade, event) and
# stay quiet until the window elapses. Purely in-memory: a restart re-arms it.
_MONITOR_COOLDOWN_S = 300.0
_ALERT_NEAR_PCT = 0.5   # "approaching" a stop/target = within this % of price
_last_spoken: dict[tuple, float] = {}


def _cooled(key: tuple) -> bool:
    """True (and re-arms) if ``key`` hasn't fired within the cooldown window."""
    now = time.monotonic()
    if now - _last_spoken.get(key, 0.0) >= _MONITOR_COOLDOWN_S:
        _last_spoken[key] = now
        # Keep the map bounded on a long-lived process.
        if len(_last_spoken) > 5000:
            cutoff = now - _MONITOR_COOLDOWN_S
            for k in [k for k, v in _last_spoken.items() if v < cutoff]:
                _last_spoken.pop(k, None)
        return True
    return False


def _check_price_alerts(db, user, engine, price_of) -> list[dict]:
    """Fire any armed alert whose level the REAL live price has crossed.

    One-shot (flips to ``triggered``); if the price can't be read this tick the
    alert stays armed — we never guess a price to force a trigger.
    """
    events: list[dict] = []
    try:
        rows = db.scalars(
            select(PriceAlert).where(
                PriceAlert.user_id == user.id, PriceAlert.status == "armed"
            )
        ).all()
    except Exception:
        return events
    fired = False
    for a in rows:
        px = price_of(a.symbol)
        if px is None:
            continue
        if not ((a.condition == "above" and px >= a.price)
                or (a.condition == "below" and px <= a.price)):
            continue
        a.status, a.triggered_at, a.triggered_price = "triggered", _utcnow(), px
        db.add(a)
        fired = True
        text = f"🔔 {a.symbol} crossed {a.condition} {a.price:g} — trading at {px:g} now."
        if a.note:
            text += f" ({a.note})"
        events.append({"user_id": user.id, "kind": "alert", "event": "crossed",
                       "symbol": a.symbol, "text": text, "level": "info"})
    if fired:
        try:
            db.commit()
        except Exception:
            db.rollback()
    return events


def _monitor_positions(db, user, engine, price_of) -> list[dict]:
    """Opt-in loud monitor: call out the single most material live risk.

    Deterministic detection on REAL numbers (marked P&L, stops, targets, day
    P&L vs the daily-loss limit). Emits at most ONE line per tick per user — the
    most urgent, each type on its own cooldown. The text is already true and
    complete; the LLM only rephrases it, never changes a number.
    """
    try:
        st = engine.status(db)
    except Exception:
        return []
    equity = float(st.get("equity") or 0)
    day_pnl = float(st.get("day_pnl") or 0)
    uid = user.id
    cands: list[tuple[int, tuple, dict]] = []

    def add(prio, key, event, symbol, level, text):
        cands.append((prio, key, {"user_id": uid, "kind": "monitor",
                     "event": event, "symbol": symbol, "level": level, "text": text}))

    dll = float(getattr(engine.settings, "daily_loss_limit_pct", 0) or 0)
    if dll > 0 and equity > 0:
        limit_amt = equity * dll / 100.0
        lost = max(0.0, -day_pnl)
        if limit_amt > 0 and lost >= 0.7 * limit_amt:
            add(1, (uid, 0, "day_loss"), "day_loss", None, "warn",
                f"You're at {lost / limit_amt * 100:.0f}% of today's loss limit — "
                f"down {lost:.2f} of ~{limit_amt:.2f}. Time to ease off.")
    try:
        rows = db.scalars(select(Trade).where(
            Trade.user_id == uid, Trade.status == TradeStatus.open.value)).all()
    except Exception:
        rows = []
    for t in rows:
        entry, amount = float(t.entry_price or 0), float(t.amount or 0)
        px = price_of(t.symbol)
        # Trade.pnl is only written when a trade CLOSES (it's 0.0 the whole time a
        # position is open), so reading it here would make the "turned red"
        # call-out never fire. Compute the REAL unrealized P&L from the current
        # live price instead; if the price can't be read this tick we leave it 0
        # (never a fabricated number) and simply skip the drawdown call-out.
        pnl = engine.unrealized_pnl(t, px) if px is not None else float(t.pnl or 0)
        is_long = (t.side or "buy").lower() != "sell"
        if px is not None and t.stop_loss and entry > 0:
            stop = float(t.stop_loss)
            d = (px - stop) / px * 100 if is_long else (stop - px) / px * 100
            if 0 <= d <= _ALERT_NEAR_PCT:
                add(0, (uid, t.id, "near_stop"), "near_stop", t.symbol, "warn",
                    f"{t.symbol} is only {d:.2f}% from your stop at {stop:g} — "
                    f"{px:g} now; it may close out shortly.")
        notional = entry * amount
        if notional > 0 and pnl < 0 and abs(pnl) >= 0.005 * notional:
            add(2, (uid, t.id, "red"), "turned_red", t.symbol, "warn",
                f"Your {t.symbol} trade is underwater — down {abs(pnl):.2f} "
                f"({pnl / notional * 100:.1f}%).")
        if px is not None and t.take_profit and entry > 0:
            tp = float(t.take_profit)
            d = (tp - px) / px * 100 if is_long else (px - tp) / px * 100
            if 0 <= d <= _ALERT_NEAR_PCT:
                add(3, (uid, t.id, "tp"), "tp_zone", t.symbol, "info",
                    f"{t.symbol} is almost at your take-profit {tp:g} — {px:g} now.")
    for _prio, key, ev in sorted(cands, key=lambda c: c[0]):
        if _cooled(key):
            return [ev]
    return []


def _tick_all(manager) -> tuple[list[dict], list[dict]]:
    """Synchronous DB work for every active user's engine.

    Runs autonomous analysis (if the user enabled it), checks pending/open
    orders for SL/TP, evaluates price alerts + the opt-in loud monitor, and
    returns ``(statuses, events)`` — a status snapshot per user plus any
    assistant call-outs to broadcast.
    """
    db = SessionLocal()
    statuses: list[dict] = []
    events: list[dict] = []
    try:
        for user, engine in manager.engines_for_active_users(db):
            try:
                # New-entry analysis is gated on the bot being "running": stopping
                # the bot halts NEW trades. It does NOT stop risk management —
                # open positions must keep their SL/TP protection and the
                # drawdown kill-switch must keep watching real money regardless.
                if engine.running:
                    if getattr(engine.settings, "auto_trade_enabled", False):
                        for symbol in engine.settings.auto_symbol_list:
                            try:
                                engine.auto_trade_symbol(
                                    db, symbol, engine.settings.auto_timeframe
                                )
                            except Exception as exc:  # resilient per-symbol
                                logger.warning(
                                    "auto_trade user=%s %s failed: %s",
                                    user.id, symbol, exc,
                                )
                    else:
                        # Autonomous execution OFF: still analyse + log the brain's
                        # verdict so the Signals tab shows a live read (no orders).
                        for symbol in engine.settings.auto_symbol_list:
                            try:
                                engine.observe_symbol(
                                    db, symbol, engine.settings.auto_timeframe
                                )
                            except Exception as exc:  # resilient per-symbol
                                logger.warning(
                                    "observe user=%s %s failed: %s",
                                    user.id, symbol, exc,
                                )
                # ALWAYS, even when the bot is stopped. Update the drawdown
                # kill-switch FIRST so a fresh trip is seen by check_pending_orders
                # in the SAME pass — it then cancels resting entries instead of
                # letting them fill into the very drawdown that just halted us.
                engine._update_drawdown(db)
                engine.check_pending_orders(db)
                # Timed "buy at 20:00" orders fire here too — ALWAYS, even when the
                # bot is stopped: like a resting limit, a scheduled order is a
                # deliberate human instruction, not autonomous trading. Any fire
                # (or honest refusal) becomes a broadcast call-out.
                events.extend(engine.check_scheduled_orders(db))
                engine.check_open_positions(db)
                statuses.append({"user_id": user.id, "status": engine.status(db)})

                # Assistant call-outs. A tiny per-user price cache keeps us to one
                # ticker fetch per symbol per tick (alerts + monitor may both want
                # the same price). A price we can't read is None — never guessed.
                price_cache: dict[str, float | None] = {}

                def _price_of(symbol: str, _cache=price_cache, _engine=engine):
                    if symbol not in _cache:
                        px = None
                        try:
                            tk = _engine.connector.fetch_ticker(symbol)
                            raw = (tk.get("last") or tk.get("close")) if tk else None
                            px = float(raw) if raw else None
                        except Exception:
                            px = None
                        _cache[symbol] = px
                    return _cache[symbol]

                # Price alerts fire ALWAYS (deterministic, one-shot, real price).
                events.extend(_check_price_alerts(db, user, engine, _price_of))
                # Loud monitor is opt-in AND needs the LLM to phrase its call-outs.
                if getattr(engine.settings, "ai_monitor_enabled", False) and engine.ai.available:
                    for ev in _monitor_positions(db, user, engine, _price_of):
                        ev["engine"] = engine
                        events.append(ev)
            except Exception as exc:  # keep other users' engines alive
                logger.warning("tick failed for user=%s: %s", user.id, exc)
        return statuses, events
    finally:
        db.close()


async def _broadcast_event(broadcaster: Broadcaster, ev: dict) -> None:
    """Broadcast one assistant call-out, letting the LLM rephrase a monitor line.

    The deterministic ``text`` is already true and complete. For a monitor event
    we give the model up to 12s to say the SAME fact more like a person; if it's
    slow, unavailable or errors we fall back to the exact deterministic text —
    the numbers are never touched. Price alerts are sent verbatim.
    """
    engine = ev.pop("engine", None)
    text = ev.get("text") or ""
    if ev.get("kind") == "monitor" and engine is not None:
        try:
            phrased = await asyncio.wait_for(
                asyncio.to_thread(engine.ai.narrate_event, text), timeout=12.0
            )
            if phrased:
                text = phrased
        except Exception:
            pass  # fall back to the deterministic text
    await broadcaster.broadcast({
        "event": "assistant",
        "user_id": ev["user_id"],
        "data": {
            "kind": ev.get("kind"),
            "event": ev.get("event"),
            "symbol": ev.get("symbol"),
            "text": text,
            "level": ev.get("level", "info"),
        },
    })


async def monitor_loop(manager, broadcaster: Broadcaster, interval: float = 5.0) -> None:
    """Periodically tick every active user's engine and broadcast status.

    Runs for the lifetime of the app. Status events carry a ``user_id`` so the
    WebSocket layer can route each snapshot to the right client. The tick cadence
    is read live from the global monitor-interval setting each iteration (clamped
    to a rate-limit-safe range), so changing it takes effect without a restart;
    ``interval`` is only the fallback when nothing is stored yet.
    """
    while True:
        try:
            statuses, events = await asyncio.to_thread(_tick_all, manager)
            for item in statuses:
                await broadcaster.broadcast(
                    {"event": "status", "user_id": item["user_id"], "data": item["status"]}
                )
            for ev in events:
                await _broadcast_event(broadcaster, ev)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - keep loop alive
            logger.exception("monitor_loop error: %s", exc)
        # Read the (clamped) cadence live so the operator can retune responsiveness
        # without a restart. A read failure just keeps the previous/fallback value.
        try:
            interval = await asyncio.to_thread(_current_interval, interval)
        except Exception:  # pragma: no cover - defensive
            pass
        await asyncio.sleep(interval)


def _current_interval(fallback: float) -> float:
    db = SessionLocal()
    try:
        return load_monitor_interval(db, fallback)
    finally:
        db.close()


# ---- News alarm: forward genuinely-new headlines to Telegram -----------
# A SEPARATE loop from the monitor so a slow multi-feed RSS fetch never delays
# time-critical SL/TP monitoring. Each engine primes on its first poll (records
# what's already out there WITHOUT alarming) so enabling the feature never dumps
# a 24h backlog; after that only headlines it hasn't seen fire, capped per poll.
_NEWS_POLL_S = 600.0        # 10 min between polls (feeds don't move faster)
_NEWS_ALARM_MAX = 3         # most new headlines to push in one poll (anti-burst)
_NEWS_SEEN_CAP = 400        # bound the per-engine seen-set on a long-lived process


def _news_key(item: dict) -> str:
    """Stable identity for a headline: prefer the link, fall back to the title."""
    return str(item.get("link") or item.get("title") or "").strip()


def _poll_news_once(manager) -> None:
    """Fetch the news feeds once and Telegram any genuinely-new headline to each
    opted-in user. Blocking (httpx + notify) — always run via asyncio.to_thread.
    """
    db = SessionLocal()
    try:
        engines = manager.engines_for_active_users(db)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("news poll: could not list engines: %s", exc)
        db.close()
        return
    finally:
        db.close()

    for _user, engine in engines:
        try:
            if not getattr(engine.settings, "alert_news_enabled", False):
                continue
            if not getattr(engine.notifier, "enabled", False):
                continue  # no Telegram creds -> nothing to alarm to
            feeds = engine.settings.news_feed_list
            if not feeds:
                continue
            items, _errors = fetch_market_news(feeds, limit=12)
            if not items:
                continue
            keys = [k for k in (_news_key(it) for it in items) if k]

            # First poll for this engine: record what's already out there and stay
            # silent — enabling the alarm must not dump the existing backlog.
            if not engine._news_primed:
                engine._news_seen = set(keys[:_NEWS_SEEN_CAP])
                engine._news_primed = True
                continue

            fresh = [it for it in items if _news_key(it) and _news_key(it) not in engine._news_seen]
            for it in items:
                k = _news_key(it)
                if k:
                    engine._news_seen.add(k)
            # Keep the seen-set bounded; the freshest keys are the ones worth keeping.
            if len(engine._news_seen) > _NEWS_SEEN_CAP:
                engine._news_seen = set(keys[:_NEWS_SEEN_CAP])
            if not fresh:
                continue

            shown = fresh[:_NEWS_ALARM_MAX]
            lines = [f"📰 Market news ({len(fresh)} new):"]
            for it in shown:
                src = (it.get("source") or "").strip()
                title = (it.get("title") or "").strip()
                line = f"• {title}" if not src else f"• [{src}] {title}"
                link = (it.get("link") or "").strip()
                if link:
                    line += f"\n  {link}"
                lines.append(line)
            if len(fresh) > len(shown):
                lines.append(f"…and {len(fresh) - len(shown)} more.")
            try:
                engine._notify("\n".join(lines))
            except Exception as exc:  # pragma: no cover - notify never breaks the loop
                logger.warning("news notify failed for user=%s: %s", _user.id, exc)
        except Exception as exc:  # keep one bad engine from stalling the rest
            logger.warning("news poll failed for user=%s: %s", getattr(_user, "id", "?"), exc)


async def news_alarm_loop(manager, interval: float = _NEWS_POLL_S) -> None:
    """Periodically poll the news feeds and push new headlines to opted-in users.

    Runs for the lifetime of the app, independent of the fast monitor loop so a
    slow RSS fetch can never delay SL/TP checks. Failures are logged and the loop
    keeps running; nothing here ever fabricates a headline.
    """
    while True:
        try:
            await asyncio.to_thread(_poll_news_once, manager)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - keep loop alive
            logger.exception("news_alarm_loop error: %s", exc)
        await asyncio.sleep(interval)
