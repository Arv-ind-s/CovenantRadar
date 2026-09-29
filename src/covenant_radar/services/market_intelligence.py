"""Observed market moves since a borrower's last statements; never a credit score.

A covenant test is computed from the last reported quarter. Markets keep moving
after that quarter ends, and the next statements arrive weeks after the next
quarter closes. This module measures, from public official series, how far each
market driver has moved since a reporting period, in two lenses:

* **quarter so far** - the average since the period ended against the
  period's own average. This is what the next, still unreported, quarter is
  absorbing.
* **at latest prices** - the latest observation against the period's
  average. This is what the following quarter absorbs if prices hold.

USD-priced commodities are converted into rupee terms with USD/INR over the
same windows, because an Indian borrower pays the rupee price.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from covenant_radar.ingestion.feeds.public_data import (
    NEWS_WINDOW,
    PublicSource,
    fetch_source,
    fred_source,
    news_source,
)

CACHE_VERSION = 2


@dataclass(frozen=True, slots=True)
class Driver:
    key: str
    label: str
    series: str
    unit: str
    frequency: str  # "daily" or "monthly"
    publisher: str
    kind: str = "price"  # "price", "fx" or "rate"
    usd_priced: bool = False
    news_query: str | None = None
    story: str = ""


DRIVERS: tuple[Driver, ...] = (
    Driver(
        "brent",
        "Brent crude",
        "DCOILBRENTEU",
        "US$/bbl",
        "daily",
        "U.S. Energy Information Administration",
        usd_priced=True,
        news_query="crude oil price",
        story="Diesel, ATF, bunker fuel and feedstock",
    ),
    Driver(
        "usdinr",
        "US dollar in rupees",
        "DEXINUS",
        "₹ per US$",
        "daily",
        "Federal Reserve H.10",
        kind="fx",
        news_query="rupee dollar",
        story="Import costs and export realisations",
    ),
    Driver(
        "call_rate",
        "Overnight call rate",
        "IRSTCI01INM156N",
        "% p.a.",
        "monthly",
        "OECD Main Economic Indicators",
        kind="rate",
        news_query="RBI repo rate",
        story="Tracks the RBI policy repo rate; floating loans reprice",
    ),
    Driver(
        "gsec10",
        "10-year G-sec yield",
        "INDIRLTLT01STM",
        "% p.a.",
        "monthly",
        "OECD Main Economic Indicators",
        kind="rate",
        story="Term refinancing cost",
    ),
    Driver(
        "iron_ore",
        "Iron ore",
        "PIORECRUSDM",
        "US$/t",
        "monthly",
        "IMF Primary Commodity Prices",
        usd_priced=True,
        news_query="steel prices india",
        story="Steel-making input; proxy for steel costs",
    ),
    Driver(
        "coal",
        "Thermal coal",
        "PCOALAUUSDM",
        "US$/t",
        "monthly",
        "IMF Primary Commodity Prices",
        usd_priced=True,
        news_query="coal prices india",
        story="Power generation and coke",
    ),
    Driver(
        "aluminium",
        "Aluminium",
        "PALUMUSDM",
        "US$/t",
        "monthly",
        "IMF Primary Commodity Prices",
        usd_priced=True,
        news_query="aluminium prices",
        story="Castings, extrusions and sheet",
    ),
    Driver(
        "copper",
        "Copper",
        "PCOPPUSDM",
        "US$/t",
        "monthly",
        "IMF Primary Commodity Prices",
        usd_priced=True,
        story="Wiring and electricals",
    ),
    Driver(
        "cotton",
        "Cotton",
        "PCOTTINDUSDM",
        "US¢/lb",
        "monthly",
        "IMF Primary Commodity Prices",
        usd_priced=True,
        news_query="cotton prices india",
        story="Spinning and weaving input",
    ),
    Driver(
        "sugar",
        "Sugar",
        "PSUGAISAUSDM",
        "US¢/lb",
        "monthly",
        "IMF Primary Commodity Prices",
        usd_priced=True,
        news_query="sugar prices india",
        story="Food and beverage input",
    ),
    Driver(
        "wheat",
        "Wheat",
        "PWHEAMTUSDM",
        "US$/t",
        "monthly",
        "IMF Primary Commodity Prices",
        usd_priced=True,
        news_query="wheat prices india",
        story="Flour and grain input; farm realisations",
    ),
    Driver(
        "lng",
        "Asia LNG",
        "PNGASJPUSDM",
        "US$/MMBtu",
        "monthly",
        "IMF Primary Commodity Prices",
        usd_priced=True,
        news_query="LNG gas price india",
        story="Process gas and fertiliser feedstock",
    ),
    Driver(
        "rubber",
        "Natural rubber",
        "PRUBBUSDM",
        "US¢/lb",
        "monthly",
        "IMF Primary Commodity Prices",
        usd_priced=True,
        story="Tyres, hoses and seals",
    ),
)
DRIVERS_BY_KEY = {driver.key: driver for driver in DRIVERS}
RATE_DRIVER = "call_rate"


@dataclass(frozen=True, slots=True)
class Exposure:
    driver: str
    role: str  # "cost": price rise is adverse; "revenue": price rise is supportive
    share: float  # assumed share of revenue
    note: str


# Analyst defaults, not borrower facts. `share` is the assumed share of revenue
# spent on (cost) or earned from (revenue) the driver. They only convert a
# borrower's actual EBIT cushion into a "breakeven move" and are shown next to
# every figure they produce. Replace them with borrower-specific cost data
# whenever it is available.
SECTOR_EXPOSURES: dict[str, tuple[Exposure, ...]] = {
    "A01": (
        Exposure("lng", "cost", 0.06, "Fertiliser and irrigation energy"),
        Exposure("wheat", "revenue", 0.40, "Crop realisations"),
    ),
    "B08": (Exposure("brent", "cost", 0.15, "Diesel for equipment and haulage"),),
    "C10": (
        Exposure("sugar", "cost", 0.20, "Sugar"),
        Exposure("wheat", "cost", 0.15, "Grain and flour"),
    ),
    "C13": (
        Exposure("cotton", "cost", 0.40, "Raw cotton"),
        Exposure("usdinr", "revenue", 0.30, "Export yarn and fabric"),
    ),
    "C20": (
        Exposure("brent", "cost", 0.20, "Petrochemical feedstock"),
        Exposure("lng", "cost", 0.08, "Process gas"),
        Exposure("usdinr", "revenue", 0.25, "Export sales"),
    ),
    "C21": (Exposure("usdinr", "revenue", 0.40, "Regulated-market exports"),),
    "C24": (
        Exposure("iron_ore", "cost", 0.30, "Iron ore"),
        Exposure("coal", "cost", 0.15, "Coal and coke"),
    ),
    "C25": (
        Exposure("iron_ore", "cost", 0.25, "Steel (iron ore as proxy)"),
        Exposure("aluminium", "cost", 0.10, "Aluminium stock"),
    ),
    "C29": (
        Exposure("iron_ore", "cost", 0.20, "Steel (iron ore as proxy)"),
        Exposure("aluminium", "cost", 0.12, "Aluminium castings"),
        Exposure("copper", "cost", 0.05, "Copper wiring"),
        Exposure("rubber", "cost", 0.05, "Rubber parts"),
    ),
    "D35": (Exposure("coal", "cost", 0.35, "Fuel for thermal generation"),),
    "F41": (
        Exposure("iron_ore", "cost", 0.12, "Steel (iron ore as proxy)"),
        Exposure("brent", "cost", 0.05, "Diesel and bitumen"),
    ),
    "H49": (Exposure("brent", "cost", 0.30, "Diesel"),),
    "H50": (Exposure("brent", "cost", 0.25, "Bunker fuel"),),
    "H51": (Exposure("brent", "cost", 0.30, "Aviation turbine fuel"),),
    "I55": (Exposure("lng", "cost", 0.04, "Kitchen and utility gas"),),
    "J62": (Exposure("usdinr", "revenue", 0.70, "Export services billed in US$"),),
    "L68": (Exposure("iron_ore", "cost", 0.08, "Steel (iron ore as proxy)"),),
}


def exposures_for(industry_code: str | None) -> tuple[Exposure, ...]:
    return SECTOR_EXPOSURES.get(industry_code or "", ())


SOURCES: tuple[PublicSource, ...] = (
    *(fred_source(driver.series, driver.label, driver.publisher) for driver in DRIVERS),
    *(news_source(driver.key, driver.news_query) for driver in DRIVERS if driver.news_query),
)


# --------------------------------------------------------------------------- moves


def _day(value: object) -> date:
    return date.fromisoformat(str(value)[:10])


def _month_end(day: date) -> date:
    following = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return following - timedelta(days=1)


def _mean(points: list[tuple[date, float]], low: date, high: date) -> float | None:
    values = [value for day, value in points if low <= day <= high]
    return sum(values) / len(values) if values else None


def _points(snapshot: dict[str, Any], driver: Driver) -> list[tuple[date, float]]:
    entry = snapshot.get("series", {}).get(driver.series) or {}
    return [(_day(day), float(value)) for day, value in entry.get("observations", [])]


def driver_move(
    snapshot: dict[str, Any], driver: Driver, start: date, end: date
) -> dict[str, Any] | None:
    """Both lenses for one driver against one reporting period, or ``None``.

    Price and FX changes are percentages; rate changes are basis points.
    USD-priced drivers also carry the rupee-equivalent change.
    """
    points = _points(snapshot, driver)
    if not points:
        return None
    base = _mean(points, start, end)
    if base is None:
        prior = [value for day, value in points if day <= end]
        if not prior:
            return None
        base = prior[-1]
    after = [(day, value) for day, value in points if day > end]
    latest_day, latest = points[-1]
    monthly = driver.frequency == "monthly"

    def change(value: float | None) -> float | None:
        if value is None:
            return None
        if driver.kind == "rate":
            return (value - base) * 100
        return (value / base - 1) * 100 if base else None

    qtd = sum(value for _, value in after) / len(after) if after else None
    qtd_from = after[0][0] if after else None
    qtd_to = (_month_end(after[-1][0]) if monthly else after[-1][0]) if after else None
    latest_to = _month_end(latest_day) if monthly else latest_day
    move: dict[str, Any] = {
        "driver": driver.key,
        "base": base,
        "qtd": qtd,
        "latest": latest,
        "latest_date": latest_day.isoformat(),
        # A monthly observation is the month's average, not a value on its 1st.
        "latest_label": (
            f"{latest_day:%b %Y}" if monthly else f"{latest_day.day} {latest_day:%b %Y}"
        ),
        "latest_is_after_period": latest_day > end,
        "qtd_from": qtd_from.isoformat() if qtd_from else None,
        "qtd_to": qtd_to.isoformat() if qtd_to else None,
        "qtd_points": len(after),
        "qtd_change": change(qtd),
        "latest_change": change(latest),
        "unit": "bp" if driver.kind == "rate" else "%",
        "qtd_label": _window_label(qtd_from, qtd_to, monthly),
        "base_label": _window_label(start, end, False),
    }
    move["qtd_rupee"] = move["qtd_change"]
    move["latest_rupee"] = move["latest_change"]
    if driver.usd_priced:
        fx = _points(snapshot, DRIVERS_BY_KEY["usdinr"])
        fx_base = _mean(fx, start, end)
        if fx and fx_base:
            fx_qtd = _mean(fx, qtd_from, qtd_to) if qtd_from and qtd_to else None
            fx_latest = _mean(fx, latest_day - timedelta(days=6), latest_to)
            move["qtd_rupee"] = _combine(move["qtd_change"], fx_qtd, fx_base)
            move["latest_rupee"] = _combine(move["latest_change"], fx_latest, fx_base)
    return move


def _combine(usd_change: float | None, fx: float | None, fx_base: float) -> float | None:
    if usd_change is None:
        return None
    if fx is None:
        return usd_change
    return ((1 + usd_change / 100) * (fx / fx_base) - 1) * 100


def _window_label(low: date | None, high: date | None, monthly: bool) -> str:
    if low is None or high is None:
        return ""
    if monthly:
        first, last = low.strftime("%b %Y"), high.strftime("%b %Y")
        return first if first == last else f"{low:%b}–{last}"
    return f"{low.day} {low:%b} – {high.day} {high:%b %Y}"


def history(snapshot: dict[str, Any], driver: Driver, since: date) -> list[tuple[date, float]]:
    return [point for point in _points(snapshot, driver) if point[0] >= since]


# --------------------------------------------------------------------------- cache


class MarketIntelligenceService:
    """A shared public-only cache: stale-while-revalidate, per-source isolation.

    Requests never wait on the network. When a source is due, one background
    refresh starts and the request is answered from the cache; an open page
    polls for the update. Successful snapshots survive restarts. Portfolio
    assessments are built per request and never enter this shared cache.
    """

    def __init__(
        self,
        *,
        cache_path: Path,
        enabled: bool = True,
        refresh_seconds: int = 900,
        fetcher: Callable[[PublicSource, datetime], list[object]] = fetch_source,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        background: bool = True,
        sources: tuple[PublicSource, ...] = SOURCES,
    ) -> None:
        self.cache_path = cache_path
        self.enabled = enabled
        self.refresh_seconds = refresh_seconds
        self.fetcher = fetcher
        self.clock = clock
        self.background = background
        self.sources = sources
        self._lock = threading.Lock()
        self._refreshing = False
        self._thread: threading.Thread | None = None
        self._sources: dict[str, Any] = {}
        self._cache_error = False
        try:
            if cache_path.stat().st_size <= 8_000_000:
                saved = json.loads(cache_path.read_text())
                if saved.get("version") == CACHE_VERSION and isinstance(saved.get("sources"), dict):
                    for source in sources:
                        entry = saved["sources"].get(source.key)
                        if isinstance(entry, dict) and isinstance(entry.get("items"), list):
                            for field in ("attempted_at", "fetched_at"):
                                if entry.get(field):
                                    self._instant(entry[field])
                            self._sources[source.key] = entry
        except (OSError, ValueError, TypeError, AttributeError):
            self._sources = {}

    @staticmethod
    def _instant(value: str) -> datetime:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None:
            raise ValueError("Snapshot timestamps must be timezone aware")
        return instant

    def snapshot(self, *, force: bool = False) -> dict[str, Any]:
        now = self.clock()
        start = False
        with self._lock:
            due = self._due(now, force) if self.enabled and not self._refreshing else []
            if due:
                self._refreshing = start = True
        if start:
            if self.background:
                self._thread = threading.Thread(
                    target=self._refresh, args=(due, now), name="market-refresh", daemon=True
                )
                self._thread.start()
            else:
                self._refresh(due, now)
        with self._lock:
            return self._build(self.clock())

    def wait(self, timeout: float = 60.0) -> None:
        """Block until an in-flight background refresh has finished (tests, CLI)."""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _due(self, now: datetime, force: bool) -> list[PublicSource]:
        due = []
        for source in self.sources:
            previous = self._sources.get(source.key, {})
            attempted = previous.get("attempted_at")
            interval = 60 if force or previous.get("error") else self.refresh_seconds
            if not attempted or now - self._instant(attempted) >= timedelta(seconds=interval):
                due.append(source)
        return due

    def _refresh(self, due: list[PublicSource], now: datetime) -> None:
        # One sequential worker per provider host: FRED throttles parallel
        # downloads into multi-minute trickles, while one at a time takes
        # well under a second each. Each result is stored as it arrives so an
        # open page fills progressively.
        hosts: dict[str, list[PublicSource]] = {}
        for source in due:
            hosts.setdefault(urlsplit(source.url).hostname or "", []).append(source)

        def run(group: list[PublicSource]) -> None:
            for source in group:
                try:
                    items = self.fetcher(source, now)
                except Exception:
                    # Never expose network exception text (proxy credentials etc.).
                    items = None
                with self._lock:
                    self._store(source.key, items, now)

        try:
            with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
                list(pool.map(run, hosts.values()))
        finally:
            with self._lock:
                self._save()
                self._refreshing = False

    def _store(self, key: str, items: list[object] | None, now: datetime) -> None:
        previous = self._sources.get(key, {})
        if items is None:
            self._sources[key] = {
                **previous,
                "items": previous.get("items", []),
                "attempted_at": now.isoformat(),
                "error": "Source unavailable",
            }
        else:
            self._sources[key] = {
                "items": items,
                "fetched_at": now.isoformat(),
                "attempted_at": now.isoformat(),
                "error": None,
            }

    def _build(self, now: datetime) -> dict[str, Any]:
        series: dict[str, Any] = {}
        news: dict[str, list[dict[str, Any]]] = {}
        health = []
        for source in self.sources:
            entry = self._sources.get(source.key, {})
            fetched = entry.get("fetched_at")
            stale = bool(
                fetched
                and (
                    not self.enabled
                    or entry.get("error")
                    or now - self._instant(fetched) > timedelta(seconds=self.refresh_seconds)
                )
            )
            status = (
                "Offline"
                if not self.enabled
                else "Unavailable"
                if not fetched
                else "Stale"
                if stale
                else "Current"
            )
            items = entry.get("items", [])
            latest = None
            if source.kind == "series" and items:
                latest = items[-1][0]
                series[source.key] = {"observations": items, "fetched_at": fetched, "stale": stale}
            elif source.kind == "news":
                fresh = [
                    {**item, "stale": stale}
                    for item in items
                    if now - self._instant(item["published_at"]) <= NEWS_WINDOW
                ]
                news[source.key.removeprefix("news:")] = fresh
                latest = fresh[0]["published_at"][:10] if fresh else None
            health.append(
                {
                    "key": source.key,
                    "kind": source.kind,
                    "name": source.name,
                    "publisher": source.publisher,
                    "url": source.page_url or source.url,
                    "status": status,
                    "fetched_at": fetched,
                    "attempted_at": entry.get("attempted_at"),
                    "error": entry.get("error"),
                    "count": len(items),
                    "latest": latest,
                }
            )
        return {
            "series": series,
            "news": news,
            "sources": health,
            "enabled": self.enabled,
            "refreshing": self._refreshing,
            "loaded": bool(series),
            "checked_at": now.isoformat(),
            "cache_error": self._cache_error,
            "refresh_seconds": self.refresh_seconds,
        }

    def _save(self) -> None:
        filename = None
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=self.cache_path.parent,
                prefix=".intelligence-",
                delete=False,
            ) as handle:
                filename = handle.name
                json.dump({"version": CACHE_VERSION, "sources": self._sources}, handle)
            os.replace(filename, self.cache_path)
            self._cache_error = False
        except OSError:
            self._cache_error = True
        finally:
            if filename and os.path.exists(filename):
                os.unlink(filename)
