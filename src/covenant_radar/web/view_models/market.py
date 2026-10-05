"""Shape market moves and borrower cushions for the market-pressure screen."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import quote

from markupsafe import Markup, escape
from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.db.models.reference import IndustryReference
from covenant_radar.db.repositories.borrower import BorrowerRepository
from covenant_radar.db.scoping import resolve_scope
from covenant_radar.security.rbac import Principal
from covenant_radar.services.borrower_market import (
    STATUS_ORDER,
    Channel,
    MarketAssessment,
    assess,
    load_positions,
    quarter_label,
    sort_key,
)
from covenant_radar.services.market_intelligence import (
    DRIVERS,
    DRIVERS_BY_KEY,
    RATE_DRIVER,
    SECTOR_EXPOSURES,
    driver_move,
    history,
    news_feed,
)

MINUS = "−"
ATTENTION = ("below", "exceeds", "tight")
FEED_LIMIT = 12
# An open page also redraws at least this often, so new statements or a
# nightly run reach it even while no market series changes.
REDRAW_SECONDS = 900


# --------------------------------------------------------------------------- formatting


def signed(value: float | None, unit: str = "%", digits: int = 1) -> str:
    if value is None:
        return "—"
    if unit == "bp":
        text = f"{abs(value):.0f} bp"
    else:
        text = f"{abs(value):.{digits}f}{unit}"
    if round(value, 0 if unit == "bp" else digits) == 0:
        return "0 bp" if unit == "bp" else f"{0:.{digits}f}{unit}"
    return ("+" if value > 0 else MINUS) + text


def level(value: float | None, unit: str) -> str:
    if value is None:
        return "—"
    if unit.startswith("%"):
        return f"{value:.2f}%"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:,.2f}"


def day(value: str | date | None) -> str:
    if not value:
        return ""
    when = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return f"{when.day} {when:%b %Y}"


def workspace_version(snapshot: dict[str, Any]) -> str:
    """What an open page was drawn from: the series data, within a redraw window."""
    window = int(datetime.fromisoformat(snapshot["checked_at"]).timestamp() // REDRAW_SECONDS)
    return f"{snapshot['series_version']}.{window}"


def ago(instant: str, now: datetime) -> str:
    """``"4 min ago"``; the page script keeps these ticking between updates."""
    seconds = max((now - datetime.fromisoformat(instant)).total_seconds(), 0)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


def crore(value: float | None) -> str:
    if value is None:
        return "—"
    sign = MINUS if value < 0 else ""
    return f"{sign}₹{abs(value):,.2f} cr"


def _trend(value: float | None, unit: str) -> str:
    if value is None:
        return "flat"
    threshold = 1 if unit == "bp" else 0.5
    return "up" if value > threshold else "down" if value < -threshold else "flat"


# --------------------------------------------------------------------------- sparkline


def sparkline(
    points: list[tuple[date, float]],
    band: tuple[date, date] | None,
    *,
    label: str,
    width: int = 240,
    height: int = 56,
) -> Markup:
    """A small inline SVG line; the shaded band is the reported quarter."""
    if len(points) < 2:
        return Markup("")
    pad = 4
    x0, x1 = points[0][0].toordinal(), points[-1][0].toordinal()
    values = [value for _, value in points]
    low, high = min(values), max(values)
    span_x = max(x1 - x0, 1)
    span_y = (high - low) or abs(high) or 1

    def x(when: date) -> float:
        return pad + (when.toordinal() - x0) / span_x * (width - 2 * pad)

    def y(value: float) -> float:
        return height - pad - (value - low) / span_y * (height - 2 * pad)

    path = " ".join(
        f"{'M' if index == 0 else 'L'}{x(when):.1f},{y(value):.1f}"
        for index, (when, value) in enumerate(points)
    )
    shade = ""
    if band is not None:
        left = max(x(band[0]), pad)
        right = min(x(band[1]), width - pad)
        if right > left:
            shade = (
                f'<rect class="mkt-spark__band" x="{left:.1f}" y="0" '
                f'width="{right - left:.1f}" height="{height}"/>'
            )
    last_x, last_y = x(points[-1][0]), y(points[-1][1])
    return Markup(
        f'<svg class="mkt-spark" viewBox="0 0 {width} {height}" preserveAspectRatio="none" '
        f'role="img" aria-label="{escape(label)}">{shade}'
        f'<path class="mkt-spark__line" d="{path}" vector-effect="non-scaling-stroke"/>'
        f'<circle class="mkt-spark__dot" cx="{last_x:.1f}" cy="{last_y:.1f}" r="3"/></svg>'
    )


# --------------------------------------------------------------------------- briefing


def _channel_view(channel: Channel) -> dict[str, Any]:
    unit = channel.unit
    return {
        "key": channel.key,
        "label": channel.label,
        "role": channel.role,
        "summary": channel.summary,
        "status": channel.status,
        "qtd": signed(channel.qtd, unit),
        "latest": signed(channel.latest, unit),
        "qtd_trend": _trend(channel.qtd, unit),
        "latest_trend": _trend(channel.latest, unit),
        "latest_date": channel.latest_label,
        "breakeven": (
            f"+{channel.breakeven:,.0f} bp" if unit == "bp" else f"+{channel.breakeven:.1f}%"
        )
        if channel.breakeven is not None
        else "—",
        "share": f"{channel.share:.0%} of revenue" if channel.share else "",
        "usage": channel.usage,
        "usage_display": (
            f"{channel.usage:.1f}× the cushion"
            if channel.usage and channel.usage >= 1
            else f"{channel.usage:.0%} of the cushion"
            if channel.usage
            else ""
        ),
        "meter": min((channel.usage or 0) / 2, 1) * 100,
        "components": [
            {
                "label": item.label,
                "note": item.note,
                "share": f"{item.share:.0%}" if item.share else "",
                "qtd": signed(item.qtd, unit),
                "qtd_label": item.qtd_label,
                "latest": signed(item.latest, unit),
                "latest_date": item.latest_label,
                "publisher": item.publisher,
                "url": f"https://fred.stlouisfed.org/series/{item.series}",
            }
            for item in channel.components
        ],
    }


def borrower_row(assessment: MarketAssessment, sector_names: dict[str, str]) -> dict[str, Any]:
    position = assessment.position
    code = position.industry_code or ""
    return {
        "reference": position.reference,
        "anchor": "b-" + position.reference.lower(),
        "name": position.name,
        "url": "/borrowers/" + quote(position.reference, safe=""),
        "review_url": "/intelligence/review/" + quote(position.reference, safe=""),
        "sector": sector_names.get(code, "Unclassified"),
        "sector_code": code,
        "status": assessment.status,
        "label": assessment.label,
        "priority": assessment.priority,
        "headline": assessment.headline,
        "implication": assessment.implication,
        "icr": f"{assessment.icr:.2f}x" if assessment.icr is not None else "—",
        "threshold": f"{position.threshold:.2f}x" if position.threshold else "—",
        "cushion": crore(assessment.cushion),
        "cushion_pct": (
            signed(assessment.cushion_pct).lstrip("+") + " of EBIT"
            if assessment.cushion_pct is not None
            else ""
        ),
        "tolerance": (
            f"{assessment.rate_tolerance_bp:,.0f} bp"
            if assessment.rate_tolerance_bp is not None
            else "none"
        ),
        "fy_label": position.fy_label or "",
        "period_end": day(position.period_end),
        "next_quarter": assessment.next_quarter,
        "following_quarter": assessment.following_quarter,
        "channels": [_channel_view(channel) for channel in assessment.channels],
        "news": [{**story, "published": day(story["published_at"])} for story in assessment.news],
    }


def build_briefing(
    session: Session,
    principal: Principal,
    snapshot: dict[str, Any],
    *,
    driver: str = "",
) -> dict[str, Any]:
    borrowers = BorrowerRepository(session).ordered(
        scope=resolve_scope(principal, session), active_only=True
    )
    sector_names = {
        code: name
        for code, name in session.execute(select(IndustryReference.code, IndustryReference.name))
    }
    positions = load_positions(session, [borrower.id for borrower in borrowers])
    assessments = sorted((assess(p, snapshot) for p in positions.values()), key=sort_key)

    periods = Counter(
        (a.position.period_start, a.position.period_end)
        for a in assessments
        if a.position.period_end
    )
    reporting = periods.most_common(1)[0][0] if periods else None

    # Which borrowers each driver reaches, and how many it is currently hurting.
    reach: dict[str, list[MarketAssessment]] = {key: [] for key in DRIVERS_BY_KEY}
    hurting: Counter[str] = Counter()
    for assessment in assessments:
        for channel in assessment.channels:
            for component in channel.components:
                reach[component.driver].append(assessment)
                # An input with no move yet is not what puts its basket under pressure.
                moved = component.qtd is not None or component.latest is not None
                if moved and channel.status in {"exceeds", "tight", "watch", "adverse"}:
                    hurting[component.driver] += 1
    for assessment in assessments:
        if assessment.position.period_end and not any(
            c.role == "rate" for c in assessment.channels
        ):
            reach[RATE_DRIVER].append(assessment)

    tiles = []
    for item in DRIVERS:
        move = driver_move(snapshot, item, *reporting) if reporting else None
        points = history(
            snapshot,
            item,
            (reporting[0] if reporting else datetime.fromisoformat(snapshot["checked_at"]).date())
            - timedelta(days=100),
        )
        exposed = reach[item.key]
        sectors = sorted({sector_names.get(a.position.industry_code or "", "") for a in exposed})
        unit = "bp" if item.kind == "rate" else "%"
        # A series published only to the base it is measured from has no move yet.
        moved = move if move and move["latest_beyond_base"] else None
        tiles.append(
            {
                "key": item.key,
                "label": item.label,
                "unit": item.unit,
                "story": item.story,
                "frequency": item.frequency,
                "publisher": item.publisher,
                "url": f"https://fred.stlouisfed.org/series/{item.series}",
                "available": move is not None,
                "latest": level(move["latest"], item.unit) if move else "—",
                "latest_date": move["latest_label"] if move else "",
                "qtd": signed(move["qtd_change"], unit) if move else "—",
                "qtd_label": move["qtd_label"] if move else "",
                "latest_change": signed(moved["latest_change"], unit) if moved else "—",
                "qtd_trend": _trend(move["qtd_change"], unit) if move else "flat",
                "latest_trend": _trend(moved["latest_change"], unit) if moved else "flat",
                "pending": bool(move) and not moved,
                "rupee": signed(moved["latest_rupee"], unit) if moved and item.usd_priced else "",
                "spark": sparkline(
                    points,
                    reporting,
                    label=f"{item.label} since {day(points[0][0]) if points else ''}",
                ),
                "exposed": len(exposed),
                "hurting": hurting[item.key],
                "sectors": [name for name in sectors if name],
                "size": abs(moved["latest_change"] or 0) if moved else 0,
            }
        )
    relevant = [tile for tile in tiles if tile["exposed"]]
    relevant.sort(key=lambda tile: (-tile["hurting"], -tile["size"]))
    others = [tile for tile in tiles if not tile["exposed"]]

    selected = DRIVERS_BY_KEY.get(driver)
    visible = [
        a
        for a in assessments
        if selected is None
        or any(c.driver == selected.key for ch in a.channels for c in ch.components)
    ]
    rows = [borrower_row(a, sector_names) for a in visible]
    counts = Counter(a.status for a in assessments)

    # The feed follows the markets this book is exposed to, or the one selected.
    feed_drivers = [selected.key] if selected else [tile["key"] for tile in relevant]
    in_book = sorted({a.position.industry_code for a in assessments if a.position.industry_code})
    assumptions = [
        {
            "code": code,
            "name": sector_names.get(code, code),
            "exposures": [
                {
                    "label": DRIVERS_BY_KEY[e.driver].label,
                    "role": "Cost" if e.role == "cost" else "Revenue",
                    "share": f"{e.share:.0%}",
                    "note": e.note,
                }
                for e in SECTOR_EXPOSURES.get(code, ())
            ],
        }
        for code in in_book
    ]
    attention = counts["below"] + counts["exceeds"] + counts["tight"]
    return {
        **{key: snapshot[key] for key in ("enabled", "refreshing", "loaded", "cache_error")},
        "version": workspace_version(snapshot),
        "checked_at": snapshot["checked_at"],
        "refresh_seconds": snapshot["refresh_seconds"],
        "sources": snapshot["sources"],
        "sources_current": sum(s["status"] == "Current" for s in snapshot["sources"]),
        "series_sources": [s for s in snapshot["sources"] if s["kind"] == "series"],
        "news_sources": [s for s in snapshot["sources"] if s["kind"] == "news"],
        "reporting": {
            "label": f"{reporting[0]:%b}–{reporting[1]:%b %Y}" if reporting else "",
            "end": day(reporting[1]) if reporting else "",
            "next_quarter": quarter_label(reporting[1], 1) if reporting else "",
            "following_quarter": quarter_label(reporting[1], 2) if reporting else "",
            "count": periods.most_common(1)[0][1] if periods else 0,
        },
        "borrower_count": len(assessments),
        "counts": {
            "attention": attention,
            **{status: counts[status] for status in STATUS_ORDER},
            "calm": sum(
                counts[key] for key in ("easing", "unexposed", "awaiting", "no_market", "no_data")
            ),
        },
        "tiles": relevant,
        "other_tiles": others,
        "driver": selected.key if selected else "",
        "driver_label": selected.label if selected else "",
        "attention": [row for row in rows if row["status"] in ATTENTION],
        "watch": [row for row in rows if row["status"] == "watch"],
        "calm": [row for row in rows if row["status"] not in (*ATTENTION, "watch")],
        "feed": feed_view(snapshot, feed_drivers),
        "feed_drivers": ",".join(feed_drivers),
        "assumptions": assumptions,
    }


def feed_view(
    snapshot: dict[str, Any], drivers: list[str] | None = None, *, limit: int = FEED_LIMIT
) -> dict[str, Any]:
    """The live headline stream for the markets in ``drivers`` (all when empty)."""
    now = datetime.fromisoformat(snapshot["checked_at"])
    fetched = snapshot.get("news_fetched_at")
    news_sources = [source for source in snapshot["sources"] if source["kind"] == "news"]
    return {
        "items": [
            {
                "id": story["id"],
                "title": story["title"],
                "url": story["url"],
                "publisher": story["publisher"],
                "published_at": story["published_at"],
                "ago": ago(story["published_at"], now),
                "markets": " · ".join(story["driver_labels"]),
            }
            for story in news_feed(snapshot, drivers, limit=limit)
        ],
        "enabled": snapshot["enabled"],
        "refreshing": snapshot["refreshing"],
        "fetched_at": fetched,
        "fetched": ago(fetched, now) if fetched else "",
        "refresh_seconds": snapshot["news_refresh_seconds"],
        "sources_current": sum(source["status"] == "Current" for source in news_sources),
        "sources_total": len(news_sources),
        # Every source, for the page header's "N/M sources current" line.
        "health": {
            "current": sum(source["status"] == "Current" for source in snapshot["sources"]),
            "total": len(snapshot["sources"]),
        },
        "checked_at": snapshot["checked_at"],
    }


def briefing_json(view: dict[str, Any]) -> dict[str, Any]:
    """The briefing without rendered markup, for ``/intelligence/data``."""
    tiles = [
        {key: value for key, value in tile.items() if key != "spark"}
        for tile in (*view["tiles"], *view["other_tiles"])
    ]
    return {
        **{key: value for key, value in view.items() if key not in {"tiles", "other_tiles"}},
        "tiles": tiles,
    }


def review_draft(assessment: MarketAssessment, checked_at: str) -> str:
    """A plain-text case note that cites every figure's source."""
    position = assessment.position
    lines = [
        f"Market pressure review — {position.name} ({position.reference})",
        "",
        f"Reported: interest cover {assessment.icr:.2f}x vs {position.threshold:.2f}x minimum "
        f"({position.fy_label}, quarter to {day(position.period_end)}). "
        f"EBIT cushion {crore(assessment.cushion)}"
        + (
            f" ({signed(assessment.cushion_pct).lstrip('+')} of EBIT)."
            if assessment.cushion_pct is not None
            else "."
        )
        if assessment.icr is not None and position.threshold
        else "Reported interest cover: not available.",
        f"Assessment: {assessment.label}. {assessment.headline}",
        assessment.implication,
        "",
        f"Observed market moves against the average of the quarter to {day(position.period_end)}:",
    ]
    for channel in assessment.channels:
        unit = channel.unit
        window = channel.components[0].qtd_label or "quarter so far"
        line = (
            f"- {channel.label} ({channel.summary}): {window} average {signed(channel.qtd, unit)}"
        )
        if channel.latest is not None:
            line += f"; latest ({channel.latest_label}) {signed(channel.latest, unit)}"
        if channel.breakeven is not None:
            noun = "tolerance" if channel.role == "rate" else "breakeven"
            line += f"; {noun} {channel.breakeven:.1f}{' bp' if unit == 'bp' else '%'}"
        lines.append(line + ".")
        for item in channel.components:
            lines.append(
                f"    {item.label}: {item.publisher} via FRED, "
                f"https://fred.stlouisfed.org/series/{item.series}"
            )
    if any(channel.role == "cost" for channel in assessment.channels):
        lines.append(
            "  Breakeven uses the sector's assumed input-cost share of revenue and no "
            "pass-through; it is not a borrower fact."
        )
    footer = [
        "",
        f"Market data retrieved {datetime.fromisoformat(checked_at):%d %b %Y %H:%M} UTC. "
        "Figures are observed public data, not borrower-confirmed.",
        "",
        "Borrower confirmation (pass-through, hedges, floating-rate share) and next action: ",
    ]
    cited: list[str] = []
    for story in assessment.news:
        entry = (
            f'- "{story["title"]}" — {story["publisher"]}, {day(story["published_at"])}. '
            f"{story['url']}"
        )
        # Case notes are capped at 4,000 characters; news links are long.
        if len("\n".join([*lines, "", "Reporting cited:", *cited, entry, *footer])) > 3_800:
            break
        cited.append(entry)
    if cited:
        lines += ["", "Reporting cited:", *cited]
    return "\n".join([*lines, *footer])


def assessment_json(assessment: MarketAssessment) -> dict[str, Any]:
    data = asdict(assessment)
    data["position"]["borrower_id"] = str(assessment.position.borrower_id)
    for field in ("period_start", "period_end"):
        value = data["position"][field]
        data["position"][field] = value.isoformat() if value else None
    return data
