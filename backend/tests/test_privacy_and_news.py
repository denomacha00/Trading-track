"""Privacy redaction + news-parsing unit tests (no network, no credentials)."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from app.main import _redact_raw
from app.news import _fetch_capped, _parse_feed, _select_recent, fetch_market_news


def test_redact_masks_secret_fields():
    raw = json.dumps({"secret": "hunter2", "action": "buy", "symbol": "BTC/USDT"})
    out = json.loads(_redact_raw(raw))
    assert out["secret"] == "***redacted***"
    assert out["action"] == "buy"
    assert out["symbol"] == "BTC/USDT"


def test_redact_normalizes_key_variants():
    raw = json.dumps({"API_Key": "k", "webhookSecret": "w", "note": "ok"})
    out = json.loads(_redact_raw(raw))
    assert out["API_Key"] == "***redacted***"
    assert out["webhookSecret"] == "***redacted***"
    assert out["note"] == "ok"  # non-sensitive field preserved


def test_redact_non_json_drops_body():
    # A non-JSON body might hide a secret we can't locate -> never store it raw.
    out = _redact_raw("secret=hunter2&action=buy")
    assert "hunter2" not in out
    assert "redacted" in out.lower()


def test_parse_rss_extracts_real_items():
    rss = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <title>Feed</title>
      <item><title>BTC rallies</title><link>https://x/1</link>
        <pubDate>Mon, 22 Sep 2025 14:30:00 GMT</pubDate></item>
      <item><title>ETH update</title><link>https://x/2</link></item>
    </channel></rss>"""
    items = _parse_feed(rss, "x")
    titles = [i["title"] for i in items]
    assert "BTC rallies" in titles and "ETH update" in titles
    first = next(i for i in items if i["title"] == "BTC rallies")
    assert first["link"] == "https://x/1"
    assert first["published"]  # normalised, not empty


def test_parse_atom_uses_href_link():
    atom = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Markets move</title>
        <link href="https://y/1"/><updated>2025-09-22T14:30:00Z</updated></entry>
    </feed>"""
    items = _parse_feed(atom, "y")
    assert items[0]["title"] == "Markets move"
    assert items[0]["link"] == "https://y/1"


def test_fetch_news_no_feeds_reports_error_not_fake():
    items, errors = fetch_market_news([], limit=5)
    assert items == []
    assert errors  # honest: says why it's empty, never invents headlines


# ---- freshness: show the latest day, drop old news, never fabricate ----

_REF = dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.timezone.utc)


def _aged(hours: float, title: str) -> dict:
    """A real-looking item published `hours` before the fixed reference time."""
    published = (_REF - dt.timedelta(hours=hours)).isoformat()
    return {"title": title, "link": f"https://x/{title}", "published": published}


def test_select_recent_keeps_today_drops_old():
    items = [_aged(2, "fresh"), _aged(50, "old"), _aged(24 * 7, "ancient")]
    out = _select_recent(items, 24.0, now=_REF.timestamp())
    assert [i["title"] for i in out] == ["fresh"]  # only the last 24h


def test_select_recent_falls_back_to_yesterday_when_quiet():
    # Nothing in the last 24h -> widen to 48h ("yesterday") rather than show nothing.
    items = [_aged(30, "yesterday"), _aged(200, "stale")]
    out = _select_recent(items, 24.0, now=_REF.timestamp())
    assert [i["title"] for i in out] == ["yesterday"]


def test_select_recent_final_fallback_shows_real_older_items():
    # Only old real items exist: show them (with real dates) instead of implying
    # the feeds were unreachable (which is what an empty list means).
    items = [_aged(500, "old-but-real")]
    out = _select_recent(items, 24.0, now=_REF.timestamp())
    assert [i["title"] for i in out] == ["old-but-real"]


def test_select_recent_empty_stays_empty():
    assert _select_recent([], 24.0, now=_REF.timestamp()) == []


# ---- feed hardening: XML-attack resistance + body-size cap -------------


def test_parse_feed_rejects_declared_entities():
    # A feed that declares XML entities (billion-laughs / external-entity vector)
    # must be REFUSED by the defused parser, not expanded. This also fails loudly
    # if the deploy image is missing defusedxml (the stdlib fallback would expand).
    payload = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE lolz [<!ENTITY lol "lol">]>'
        "<rss><channel><item><title>&lol;</title></item></channel></rss>"
    )
    with pytest.raises(Exception):
        _parse_feed(payload, "x")


class _FakeStreamResp:
    def __init__(self, chunks: list[bytes], encoding: str = "utf-8"):
        self._chunks = chunks
        self.encoding = encoding

    def raise_for_status(self):
        return None

    def iter_bytes(self):
        yield from self._chunks


class _FakeStreamCtx:
    def __init__(self, resp: _FakeStreamResp):
        self._resp = resp

    def __enter__(self):
        return self._resp

    def __exit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    def stream(self, method, url, follow_redirects=False):
        return _FakeStreamCtx(_FakeStreamResp(self._chunks))


def test_fetch_capped_aborts_on_oversize_body():
    # A hostile/huge feed must not be buffered without bound.
    one_mib = b"x" * (1024 * 1024)
    client = _FakeClient([one_mib] * 5)  # 5 MiB > 4 MiB cap
    with pytest.raises(ValueError):
        _fetch_capped(client, "https://feed.example/rss")


def test_fetch_capped_returns_small_body_decoded():
    client = _FakeClient([b"<rss></rss>"])
    assert _fetch_capped(client, "https://feed.example/rss") == "<rss></rss>"
