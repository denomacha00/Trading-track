"""Live market-news fetcher for the AI assistant + News panel.

Honest scope: this pulls REAL headlines from public crypto/markets RSS/Atom
feeds (no API key, no user data sent — plain GETs to public URLs). If a feed is
unreachable or malformed we report it as an error and return whatever real items
we did get; we NEVER fabricate a headline. Results are cached briefly so the UI
and the AI can both read "today's" news without hammering the sources.
"""
from __future__ import annotations

import logging
import time
from email.utils import parsedate_to_datetime

import httpx

# Parse feeds with defusedxml so a hostile or malformed feed can't mount an XML
# attack (billion-laughs entity expansion, external-entity/SSRF, DTD retrieval)
# against the parser. Fall back to the stdlib parser only if defusedxml isn't
# installed — the response body is size-capped either way — so news never breaks.
try:
    from defusedxml.ElementTree import fromstring as _xml_fromstring
except Exception:  # pragma: no cover - dependency present in requirements.txt
    from xml.etree.ElementTree import fromstring as _xml_fromstring

logger = logging.getLogger(__name__)

# Small in-process cache: {feeds_key: (fetched_at, items, errors)}.
_CACHE: dict[str, tuple[float, list[dict], list[str]]] = {}
_UA = "Tranding-track/1.0 (+news reader)"
# Hard cap on how much of a feed we read. Real RSS/Atom feeds are a few hundred KB
# at most; refusing to buffer more protects memory against a huge/hostile body
# (and pairs with defusedxml, which stops entity-expansion blowups after decode).
_MAX_FEED_BYTES = 4 * 1024 * 1024  # 4 MiB


def _localname(tag: str) -> str:
    """Strip any XML namespace: '{ns}item' -> 'item'."""
    return tag.rsplit("}", 1)[-1].lower()


def _normalize_published(text: str | None) -> str | None:
    """Best-effort ISO-8601 normalisation of a feed date; keep raw if we can't."""
    if not text:
        return None
    text = text.strip()
    # RSS uses RFC-822 ("Mon, 22 Sep 2025 14:30:00 GMT").
    try:
        return parsedate_to_datetime(text).isoformat()
    except Exception:
        pass
    # Atom uses ISO-8601, sometimes with a trailing 'Z'.
    try:
        import datetime as dt

        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
    except Exception:
        return text  # show it as-is rather than dropping a real date


def _sort_key(published: str | None) -> float:
    if not published:
        return 0.0
    try:
        import datetime as dt

        return dt.datetime.fromisoformat(published.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _select_recent(
    items: list[dict], max_age_hours: float, *, now: float | None = None
) -> list[dict]:
    """Keep only the freshest REAL items so the panel shows the latest news.

    Preference order, newest-first (items must already be sorted newest-first):
      1. Everything published within ``max_age_hours`` (i.e. "today").
      2. If that's empty (a quiet day), widen to twice the window ("yesterday").
      3. If STILL empty but we did receive real items, return them all with their
         real dates — better than implying the feeds were unreachable.
    Undated items are excluded from the timed passes (we can't prove they're
    fresh) but kept in the final fallback. Nothing is ever fabricated.
    """
    if not items:
        return []
    if max_age_hours <= 0:
        return list(items)
    ref = time.time() if now is None else now
    for window in (max_age_hours, max_age_hours * 2.0):
        cutoff = ref - window * 3600.0
        fresh = [
            it for it in items if (ts := _sort_key(it.get("published"))) > 0 and ts >= cutoff
        ]
        if fresh:
            return fresh
    return list(items)


def _parse_feed(xml_text: str, source: str) -> list[dict]:
    """Parse an RSS or Atom document into a list of {title, link, source, published}."""
    items: list[dict] = []
    root = _xml_fromstring(xml_text)
    # RSS: channel/item ; Atom: feed/entry. Search by local name to ignore ns.
    entries = [el for el in root.iter() if _localname(el.tag) in ("item", "entry")]
    for el in entries:
        title = link = published = None
        for child in el:
            name = _localname(child.tag)
            if name == "title" and child.text:
                title = child.text.strip()
            elif name == "link":
                # RSS puts the URL in text; Atom in the href attribute.
                href = child.get("href")
                link = (href or (child.text or "")).strip() or link
            elif name in ("pubdate", "published", "updated") and child.text:
                published = published or child.text.strip()
        if title:
            items.append(
                {
                    "title": title,
                    "link": link,
                    "source": source,
                    "published": _normalize_published(published),
                }
            )
    return items


def _source_name(url: str) -> str:
    try:
        host = httpx.URL(url).host or url
    except Exception:
        return url
    return host.replace("www.", "")


def _fetch_capped(client: httpx.Client, url: str) -> str:
    """GET a feed, reading at most ``_MAX_FEED_BYTES`` of body.

    Streams the response and aborts if the body grows past the cap, so a huge or
    hostile feed can't exhaust memory. Decodes with the response's declared
    charset (best-effort, replacing undecodable bytes) rather than trusting the
    body to be valid UTF-8."""
    with client.stream("GET", url, follow_redirects=True) as resp:
        resp.raise_for_status()
        total = 0
        chunks: list[bytes] = []
        for chunk in resp.iter_bytes():
            total += len(chunk)
            if total > _MAX_FEED_BYTES:
                raise ValueError(
                    f"feed exceeded {_MAX_FEED_BYTES // (1024 * 1024)} MiB cap"
                )
            chunks.append(chunk)
        encoding = resp.encoding or "utf-8"
    return b"".join(chunks).decode(encoding, errors="replace")


def fetch_market_news(
    feeds: list[str], limit: int = 8, ttl: float = 60.0, max_age_hours: float = 24.0
) -> tuple[list[dict], list[str]]:
    """Return (items, errors). Items are real, newest-first, and limited to the
    most recent window (today; falls back to yesterday on a quiet day) so the UI
    always shows current news and drops stale headlines. ``errors`` names dead
    feeds. A short TTL keeps it near-real-time without hammering the sources."""
    if not feeds:
        return [], ["no news feeds configured"]
    key = "|".join(feeds)
    now = time.time()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < ttl:
        return cached[1][:limit], cached[2]

    items: list[dict] = []
    errors: list[str] = []
    for url in feeds:
        try:
            with httpx.Client(timeout=8.0, headers={"User-Agent": _UA}) as client:
                xml_text = _fetch_capped(client, url)
            items.extend(_parse_feed(xml_text, _source_name(url)))
        except Exception as exc:
            logger.warning("news feed failed %s: %s", url, exc)
            errors.append(f"{_source_name(url)} unavailable")

    # De-dupe by title, then sort newest-first by parsed date.
    seen: set[str] = set()
    unique: list[dict] = []
    for it in items:
        t = it["title"]
        if t not in seen:
            seen.add(t)
            unique.append(it)
    unique.sort(key=lambda it: _sort_key(it.get("published")), reverse=True)

    # Keep only the freshest items (today, else yesterday) so old news is dropped.
    recent = _select_recent(unique, max_age_hours, now=now)

    _CACHE[key] = (now, recent, errors)
    return recent[:limit], errors
