"""Bounded, read-only public market adapters. No borrower data leaves the application.

Two kinds of source are fetched, both over fixed application URLs:

* ``series``: FRED CSV downloads. FRED republishes official data (U.S. EIA
  Brent spot, Federal Reserve H.10 USD/INR, OECD Indian interest rates and
  IMF primary-commodity prices) under one stable, key-less endpoint.
* ``news``: Google News RSS searches for a market driver ("crude oil price",
  "rupee dollar" ...). Queries name commodities and rates only, never a
  borrower, so nothing about the loan book is disclosed to the provider.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
import re
import ssl
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import quote_plus, urljoin, urlsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx
import truststore

MAX_BYTES = 2_000_000
NEWS_WINDOW = timedelta(days=14)
SERIES_HISTORY = timedelta(days=430)
_NEWS_PER_QUERY = 20
# Social reposts and chart widgets are not reporting; they are dropped.
_SKIP_PUBLISHERS = ("linkedin", "youtube", "facebook", "instagram", "tradingview", "scribd")
# Established business and trade press is flagged so views can prefer it; the
# stored list itself is newest first, because it feeds a live headline stream.
_ESTABLISHED = (
    "reuters",
    "economic times",
    "et ",
    "mint",
    "business standard",
    "businessline",
    "the hindu",
    "moneycontrol",
    "cnbc",
    "ndtv profit",
    "financial express",
    "bloomberg",
    "business today",
    "s&p global",
    "argus",
    "fibre2fashion",
    "bigmint",
    "alcircle",
    "press information bureau",
    "reserve bank",
    "hindustan times",
    "times of india",
)


@dataclass(frozen=True)
class PublicSource:
    key: str
    name: str
    url: str
    kind: str
    publisher: str = ""
    page_url: str = ""
    timeout: float = 12.0


def fred_source(series_id: str, name: str, publisher: str) -> PublicSource:
    return PublicSource(
        key=series_id,
        name=name,
        url=f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}",
        kind="series",
        publisher=publisher,
        page_url=f"https://fred.stlouisfed.org/series/{series_id}",
        timeout=20.0,
    )


def news_source(key: str, query: str) -> PublicSource:
    search = quote_plus(f"{query} when:14d")
    return PublicSource(
        key=f"news:{key}",
        name=f"Google News · “{query}”",
        url=f"https://news.google.com/rss/search?q={search}&hl=en-IN&gl=IN&ceid=IN:en",
        kind="news",
        publisher="Google News",
        page_url=f"https://news.google.com/search?q={quote_plus(query)}&hl=en-IN&gl=IN&ceid=IN:en",
    )


def safe_link(value: str) -> str | None:
    try:
        parts = urlsplit(value.strip())
        if parts.scheme in {"http", "https"} and parts.hostname and not parts.username:
            return value.strip()
    except ValueError:
        pass
    return None


def publication_date(value: str) -> datetime | None:
    """Indian official feeds sometimes omit a zone or the time of day."""
    if not value.strip():
        return None
    try:
        result = parsedate_to_datetime(value)
    except (ValueError, TypeError):
        try:
            result = datetime.strptime(value, "%d %b, %Y %z")
        except ValueError:
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=ZoneInfo("Asia/Kolkata"))
    return result.astimezone(UTC)


def _rss_items(payload: bytes) -> list[ElementTree.Element]:
    # Reject declarations before parsing; these feeds need neither DTDs nor entities.
    if len(payload) > MAX_BYTES or re.search(rb"<!\s*(DOCTYPE|ENTITY)", payload, re.I):
        raise ValueError("Unsafe or oversized XML feed")
    root = ElementTree.fromstring(payload.decode("utf-8-sig"))
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("Source did not return RSS")
    return root.findall("./channel/item")[:200]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", "", text))).strip()


def _title_key(title: str) -> str:
    return " ".join(re.findall(r"\w+", title.casefold()))[:120]


def parse_news(payload: bytes, source: PublicSource, now: datetime) -> list[dict[str, object]]:
    """Google News RSS, newest first: publisher from ``<source>``, deduplicated by headline."""
    result: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in _rss_items(payload):
        publisher_node = item.find("source")
        publisher = _clean(publisher_node.text or "") if publisher_node is not None else ""
        publisher_url = (
            safe_link(publisher_node.get("url", "")) if publisher_node is not None else None
        )
        title = _clean(item.findtext("title") or "")
        # Google appends " - Publisher" to every headline; the publisher is shown separately.
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3].rstrip()
        # Some trade feeds prefix headlines with a section separator ("| India-EU FTA ...").
        title = title.lstrip("|-–—:· ").strip()[:300]
        link = safe_link(item.findtext("link") or "")
        published = publication_date(item.findtext("pubDate") or "")
        key = _title_key(title)
        name = publisher.casefold()
        if not link or not published or not publisher or key in seen or len(title) < 25:
            continue
        if any(skip in name for skip in _SKIP_PUBLISHERS):
            continue
        if not now - NEWS_WINDOW <= published <= now + timedelta(hours=1):
            continue
        seen.add(key)
        result.append(
            {
                "id": hashlib.sha256(link.encode()).hexdigest()[:24],
                "title": title,
                "url": link,
                "publisher": publisher[:80],
                "publisher_url": publisher_url,
                "published_at": published.isoformat(),
                "established": any(mark in f"{name} " for mark in _ESTABLISHED),
            }
        )
    result.sort(key=lambda row: str(row["published_at"]), reverse=True)
    return result[:_NEWS_PER_QUERY]


def parse_series(payload: bytes, source: PublicSource) -> list[list[object]]:
    """FRED CSV: ``date,value`` rows with ``.`` for missing observations."""
    if len(payload) > MAX_BYTES:
        raise ValueError("Oversized series")
    rows = list(csv.reader(io.StringIO(payload.decode("utf-8-sig"))))
    if not rows or len(rows[0]) != 2 or rows[0][1].strip() != source.key:
        raise ValueError("Unexpected FRED CSV header")
    observations: list[list[object]] = []
    for row in rows[1:]:
        if len(row) != 2 or row[1].strip() in {"", "."}:
            continue
        try:
            day = date.fromisoformat(row[0].strip())
            value = float(row[1])
        except ValueError:
            continue
        if math.isfinite(value):
            observations.append([day.isoformat(), round(value, 4)])
    observations.sort(key=lambda point: str(point[0]))
    if not observations:
        raise ValueError("No usable observations returned")
    return observations


def fetch_source(source: PublicSource, now: datetime) -> list[object]:
    url = source.url
    if source.kind == "series":
        url += f"&cosd={(now - SERIES_HISTORY).date().isoformat()}"
    payload = _download(url, source.timeout)
    if source.kind == "series":
        return list(parse_series(payload, source))
    return list(parse_news(payload, source, now))


def _tls_context() -> ssl.SSLContext:
    # Verify against the operating system's trust store, not certifi's bundle:
    # corporate networks re-sign TLS for some hosts (news.google.com) with a
    # root CA that only the OS knows, and certifi alone rejects every request.
    return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


_TLS = _tls_context()


def _download(url: str, timeout: float) -> bytes:
    # URLs are fixed application constants, never user-supplied. A small
    # redirect budget permits canonical/language redirects on the same host.
    # httpx timeouts are per read; a provider trickling bytes needs a wall-clock cap too.
    deadline = time.monotonic() + timeout * 2
    host = urlsplit(url).hostname or ""
    allowed_hosts = {host, "www." + host.removeprefix("www.")}
    with httpx.Client(timeout=timeout, follow_redirects=False, verify=_TLS) as client:
        for _ in range(3):
            # The client's default User-Agent: FRED's edge silently stalls custom agents.
            with client.stream("GET", url) as response:
                if response.is_redirect:
                    url = _redirect_url(url, response.headers.get("location", ""), allowed_hosts)
                    continue
                response.raise_for_status()
                payload = bytearray()
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > MAX_BYTES:
                        raise ValueError("Public source exceeded size limit")
                    if time.monotonic() > deadline:
                        raise TimeoutError("Public source exceeded its time budget")
                return bytes(payload)
    raise ValueError("Too many source redirects")


def _redirect_url(url: str, location: str, allowed_hosts: set[str]) -> str:
    target = urljoin(url, location)
    parsed = urlsplit(target)
    if (
        not location
        or parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username
        or parsed.port not in {None, 443}
    ):
        raise ValueError("Source redirected outside its approved host")
    return target
