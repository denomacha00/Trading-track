"""Proactive Telegram alarms — heads-up notifications that NEVER trade.

Two hands-off features for a user who watches Telegram, not the screen:
- flat-signal alarm: ping me when the brain prints a confident BUY on a symbol I
  hold nothing in (a live entry the bot isn't taking) — long-only, confidence-
  gated, deduped on a cooldown, OFF by default;
- news alarm: forward genuinely-new market headlines, priming silently on the
  first poll (no backlog dump) and deduping by link. Nothing here places an order
  or fabricates a headline.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import tasks
from app.config import Settings
from app.database import Base
from app.engine import TradingEngine
from app.models import Trade, TradeStatus


class FakeConnector:
    connected = True
    has_credentials = False

    def __init__(self, price: float = 100.0):
        self._price = price

    def reload(self, settings):
        pass

    def fetch_price(self, symbol):
        return self._price

    def fetch_ohlcv(self, symbol, timeframe, limit):
        return []

    def fetch_position_amounts(self):
        return {}

    def fetch_balance(self, quote="USDT"):
        return None


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _engine(**over) -> TradingEngine:
    base = dict(
        trading_mode="paper",
        max_open_positions=5,
        default_stop_loss_pct=2.0,
        default_take_profit_pct=4.0,
        paper_starting_balance=10_000.0,
        min_signal_confidence=0.1,
    )
    base.update(over)
    # user_id=None keeps the trade book unscoped (single-tenant), matching the
    # other autopilot tests so a row without a user_id is still "the open trade".
    eng = TradingEngine(Settings(**base), FakeConnector(), user_id=None)
    # Capture notifications instead of firing a Telegram thread.
    eng._notified: list[str] = []
    eng._notify = lambda text: eng._notified.append(text)  # type: ignore[assignment]
    return eng


def _news_engine(**over) -> TradingEngine:
    # Telegram creds present -> notifier.enabled is True (a read-only property), so
    # the poller will actually alarm; _notify is still captured, never sent.
    over.setdefault("telegram_bot_token", "test-token")
    over.setdefault("telegram_chat_id", "test-chat")
    return _engine(**over)


def _buy(verdict: str = "buy", confidence: float = 0.9):
    return SimpleNamespace(verdict=verdict, confidence=confidence)


def _open_buy(db, symbol="BTC/USDT") -> Trade:
    t = Trade(symbol=symbol, side="buy", amount=1.0, entry_price=100.0,
              stop_loss=98.0, status=TradeStatus.open.value, mode="paper")
    db.add(t)
    db.commit()
    return t


# ---- flat-signal alarm ----------------------------------------------------


def test_flat_alarm_off_by_default(db):
    eng = _engine()  # alert_signal_on_flat defaults False
    eng._maybe_alarm_flat_signal(db, "BTC/USDT", _buy(confidence=0.99))
    assert eng._notified == []


def test_flat_alarm_fires_on_confident_buy_while_flat(db):
    eng = _engine(alert_signal_on_flat=True, alert_signal_min_confidence=0.75)
    eng._maybe_alarm_flat_signal(db, "BTC/USDT", _buy(confidence=0.9))
    assert len(eng._notified) == 1
    assert "BTC/USDT" in eng._notified[0]
    assert "BUY" in eng._notified[0].upper()


def test_flat_alarm_suppressed_when_in_position(db):
    eng = _engine(alert_signal_on_flat=True, alert_signal_min_confidence=0.5)
    _open_buy(db, "BTC/USDT")  # already long -> not flat
    eng._maybe_alarm_flat_signal(db, "BTC/USDT", _buy(confidence=0.99))
    assert eng._notified == []


def test_flat_alarm_ignores_sell_and_hold(db):
    eng = _engine(alert_signal_on_flat=True, alert_signal_min_confidence=0.1)
    eng._maybe_alarm_flat_signal(db, "BTC/USDT", _buy("sell", 0.99))
    eng._maybe_alarm_flat_signal(db, "BTC/USDT", _buy("hold", 0.99))
    assert eng._notified == []  # long-only: nothing to alarm on a sell/hold


def test_flat_alarm_respects_confidence_threshold(db):
    eng = _engine(alert_signal_on_flat=True, alert_signal_min_confidence=0.75)
    eng._maybe_alarm_flat_signal(db, "BTC/USDT", _buy(confidence=0.5))  # below bar
    assert eng._notified == []
    eng._maybe_alarm_flat_signal(db, "BTC/USDT", _buy(confidence=0.8))  # clears bar
    assert len(eng._notified) == 1


def test_flat_alarm_deduped_within_cooldown(db):
    eng = _engine(alert_signal_on_flat=True, alert_signal_min_confidence=0.5)
    eng._maybe_alarm_flat_signal(db, "BTC/USDT", _buy(confidence=0.9))
    eng._maybe_alarm_flat_signal(db, "BTC/USDT", _buy(confidence=0.9))  # same window
    assert len(eng._notified) == 1  # second is de-duped


# ---- news alarm -----------------------------------------------------------


class _FakeManager:
    def __init__(self, user, engine):
        self._pair = (user, engine)

    def engines_for_active_users(self, db):
        return [self._pair]


def _news(monkeypatch, items):
    monkeypatch.setattr(tasks, "fetch_market_news", lambda *a, **k: (items, []))


def test_news_primes_silently_then_alarms_new(db, monkeypatch):
    eng = _news_engine(alert_news_enabled=True)
    user = SimpleNamespace(id=1)
    mgr = _FakeManager(user, eng)

    _news(monkeypatch, [
        {"title": "Old A", "link": "http://x/a", "source": "CoinDesk", "published": None},
        {"title": "Old B", "link": "http://x/b", "source": "CoinDesk", "published": None},
    ])
    tasks._poll_news_once(mgr)  # first poll -> prime, do NOT alarm the backlog
    assert eng._notified == []
    assert eng._news_primed is True

    # A genuinely-new headline appears next poll -> exactly one alarm, listing it.
    _news(monkeypatch, [
        {"title": "BREAKING C", "link": "http://x/c", "source": "Reuters", "published": None},
        {"title": "Old A", "link": "http://x/a", "source": "CoinDesk", "published": None},
    ])
    tasks._poll_news_once(mgr)
    assert len(eng._notified) == 1
    assert "BREAKING C" in eng._notified[0]
    assert "Old A" not in eng._notified[0]  # already seen, not re-alarmed


def test_news_dedupes_by_link_no_repeat(db, monkeypatch):
    eng = _news_engine(alert_news_enabled=True)
    mgr = _FakeManager(SimpleNamespace(id=1), eng)
    _news(monkeypatch, [{"title": "T", "link": "http://x/1", "source": "S", "published": None}])
    tasks._poll_news_once(mgr)  # prime
    _news(monkeypatch, [{"title": "T2", "link": "http://x/2", "source": "S", "published": None}])
    tasks._poll_news_once(mgr)  # alarms T2
    tasks._poll_news_once(mgr)  # same item again -> no new alarm
    assert len(eng._notified) == 1


def test_news_off_when_disabled(db, monkeypatch):
    eng = _news_engine(alert_news_enabled=False)
    mgr = _FakeManager(SimpleNamespace(id=1), eng)
    _news(monkeypatch, [{"title": "T", "link": "http://x/1", "source": "S", "published": None}])
    tasks._poll_news_once(mgr)
    tasks._poll_news_once(mgr)
    assert eng._notified == []
    assert eng._news_primed is False  # never even primed while disabled


def test_news_silent_without_telegram(db, monkeypatch):
    eng = _engine(alert_news_enabled=True)  # no telegram creds -> notifier disabled
    mgr = _FakeManager(SimpleNamespace(id=1), eng)
    _news(monkeypatch, [{"title": "T", "link": "http://x/1", "source": "S", "published": None}])
    tasks._poll_news_once(mgr)
    _news(monkeypatch, [{"title": "T2", "link": "http://x/2", "source": "S", "published": None}])
    tasks._poll_news_once(mgr)
    assert eng._notified == []
