"""How much market move each borrower's interest cover can absorb, and what moved.

Facts come from the borrower's latest reported quarter: EBIT, finance cost,
debt and the tightest live interest-cover minimum. From those alone we derive

* the **EBIT cushion** - how much quarterly EBIT can fall before the
  interest-cover covenant is breached, and
* the **rate tolerance** - how far borrowing rates can rise, if all debt
  reprices for a full quarter, before the same breach.

Observed market moves since that quarter are then set against the cushion,
channel by channel: the sector's input-cost basket, borrowing rates and any
revenue exposure (export realisations, farm prices). Turning the cushion into
a *breakeven* basket move needs a cost share; those are sector defaults from
``SECTOR_EXPOSURES`` and are shown next to every figure they produce. No
projected ratio is published, and nothing here alters stored rankings,
forecasts, bands or covenant results.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.statements import FinancialPeriod, StatementLineValue
from covenant_radar.services.market_intelligence import (
    DRIVERS_BY_KEY,
    RATE_DRIVER,
    Exposure,
    driver_move,
    exposures_for,
)

_LINES = ("revenue", "ebit", "finance_cost", "total_debt")
ICR_CODE = "interest_coverage_ratio"
# Status order is also the review order: most urgent first.
STATUS_ORDER = (
    "below",
    "exceeds",
    "tight",
    "watch",
    "easing",
    "unexposed",
    "awaiting",
    "no_market",
    "no_data",
)
STATUS_LABELS = {
    "below": "Already below minimum",
    "exceeds": "Move exceeds cushion",
    "tight": "Move uses most of cushion",
    "watch": "Adverse, within cushion",
    "easing": "Moves easing",
    "unexposed": "No tracked pressure",
    "awaiting": "Awaiting newer market data",
    "no_market": "Market data unavailable",
    "no_data": "No interest-cover data",
}
QUEUE_PRIORITY = {
    "below": "now",
    "exceeds": "now",
    "tight": "check",
    "watch": "context",
    # Easing moves are shown on the screen, not flagged in the triage queue.
    "easing": "none",
    "unexposed": "none",
    "awaiting": "none",
    "no_market": "none",
    "no_data": "none",
}
_CHANNEL_RANK = {
    "exceeds": 0,
    "tight": 1,
    "adverse": 2,
    "watch": 3,
    "supportive": 4,
    "easing": 5,
    "flat": 6,
    "no_data": 7,
}


@dataclass(frozen=True, slots=True)
class BorrowerPosition:
    borrower_id: UUID
    reference: str
    name: str
    industry_code: str | None
    fy_label: str | None = None
    period_start: date | None = None
    period_end: date | None = None
    revenue: float | None = None
    ebit: float | None = None
    finance_cost: float | None = None
    total_debt: float | None = None
    threshold: float | None = None
    covenant_name: str | None = None


@dataclass(frozen=True, slots=True)
class Component:
    """One observed market series inside a channel."""

    driver: str
    label: str
    share: float | None
    note: str
    qtd: float | None
    latest: float | None
    qtd_label: str
    latest_date: str
    latest_label: str
    publisher: str
    series: str


@dataclass(frozen=True, slots=True)
class Channel:
    """A route from market prices into interest cover."""

    key: str
    label: str
    role: str  # "cost", "rate" or "revenue"
    unit: str  # "%" or "bp"
    summary: str
    components: tuple[Component, ...]
    share: float | None
    qtd: float | None
    latest: float | None
    latest_date: str
    latest_label: str
    breakeven: float | None
    status: str
    lens: str  # which lens set the status: "qtd", "latest" or ""
    usage: float | None  # adverse move as a share of the breakeven

    @property
    def move(self) -> float | None:
        return self.latest if self.lens == "latest" else self.qtd


@dataclass(frozen=True, slots=True)
class MarketAssessment:
    position: BorrowerPosition
    status: str
    label: str
    priority: str
    icr: float | None
    cushion: float | None
    cushion_pct: float | None
    rate_tolerance_bp: float | None
    channels: tuple[Channel, ...]
    headline: str
    implication: str
    pressure: float
    next_quarter: str
    following_quarter: str
    news: tuple[dict[str, Any], ...]

    @property
    def lead(self) -> Channel | None:
        return self.channels[0] if self.channels else None


# --------------------------------------------------------------------------- data


def load_positions(session: Session, borrower_ids: Iterable[UUID]) -> dict[UUID, BorrowerPosition]:
    """Latest complete quarter and tightest live interest-cover minimum per borrower.

    Only quarterly periods qualify: a later annual or half-yearly statement would
    overstate the rate tolerance, which is sized on one quarter's finance cost.
    Callers pass borrower ids that are already scoped to the principal.
    """
    ids = list(dict.fromkeys(borrower_ids))
    if not ids:
        return {}
    borrowers = {
        row.id: row for row in session.scalars(select(Borrower).where(Borrower.id.in_(ids)))
    }
    periods: dict[UUID, FinancialPeriod] = {}
    for period in session.scalars(
        select(FinancialPeriod)
        .where(
            FinancialPeriod.borrower_id.in_(ids),
            FinancialPeriod.superseded_by_id.is_(None),
            FinancialPeriod.is_complete.is_(True),
            # Rate tolerance and the quarter labels assume a quarter's finance cost.
            FinancialPeriod.period_type == "quarterly",
        )
        .order_by(FinancialPeriod.period_end)
    ):
        periods[period.borrower_id] = period
    lines: dict[UUID, dict[str, float]] = {}
    if periods:
        by_period = {period.id: borrower for borrower, period in periods.items()}
        for row in session.scalars(
            select(StatementLineValue).where(
                StatementLineValue.period_id.in_(list(by_period)),
                StatementLineValue.line_code.in_(_LINES),
            )
        ):
            lines.setdefault(by_period[row.period_id], {})[row.line_code] = float(row.value)
    thresholds: dict[UUID, tuple[float, str]] = {}
    for borrower_id, threshold, name in session.execute(
        select(Facility.borrower_id, CovenantVersion.threshold, Covenant.name)
        .join(Covenant, Covenant.facility_id == Facility.id)
        .join(CovenantVersion, CovenantVersion.covenant_id == Covenant.id)
        .where(
            Facility.borrower_id.in_(ids),
            Covenant.is_active.is_(True),
            CovenantVersion.status == "live",
            CovenantVersion.definition_ref == ICR_CODE,
            CovenantVersion.direction == "min",
        )
    ):
        value = float(Decimal(threshold))
        if borrower_id not in thresholds or value > thresholds[borrower_id][0]:
            thresholds[borrower_id] = (value, name)
    result = {}
    for borrower_id in ids:
        borrower = borrowers.get(borrower_id)
        if borrower is None:
            continue
        period = periods.get(borrower_id)
        values = lines.get(borrower_id, {})
        threshold = thresholds.get(borrower_id)
        result[borrower_id] = BorrowerPosition(
            borrower_id=borrower_id,
            reference=borrower.reference,
            name=borrower.legal_name,
            industry_code=borrower.industry_code,
            fy_label=period.fy_label if period else None,
            period_start=period.period_start if period else None,
            period_end=period.period_end if period else None,
            revenue=values.get("revenue"),
            ebit=values.get("ebit"),
            finance_cost=values.get("finance_cost"),
            total_debt=values.get("total_debt"),
            threshold=threshold[0] if threshold else None,
            covenant_name=threshold[1] if threshold else None,
        )
    return result


# --------------------------------------------------------------------------- maths


def quarter_label(period_end: date | None, quarters_ahead: int) -> str:
    """``"Sep 2026 quarter"`` for the quarter ending ``quarters_ahead`` after ``period_end``."""
    if period_end is None:
        return ""
    month = period_end.month - 1 + 3 * quarters_ahead
    end = date(period_end.year + month // 12, month % 12 + 1, 1)
    return f"{end:%b %Y} quarter"


def _components(
    exposures: Iterable[Exposure], snapshot: dict[str, Any], start: date, end: date
) -> list[Component]:
    result = []
    for exposure in exposures:
        driver = DRIVERS_BY_KEY[exposure.driver]
        move = driver_move(snapshot, driver, start, end)
        result.append(
            Component(
                driver=driver.key,
                label=driver.label,
                share=exposure.share,
                note=exposure.note,
                qtd=move["qtd_rupee"] if move else None,
                latest=move["latest_rupee"] if move and move["latest_beyond_base"] else None,
                qtd_label=move["qtd_label"] if move else "",
                latest_date=move["latest_date"] if move else "",
                latest_label=move["latest_label"] if move else "",
                publisher=driver.publisher,
                series=driver.series,
            )
        )
    return result


def _basket(components: list[Component], lens: str) -> float | None:
    """Share-weighted average move of the components that have data for ``lens``."""
    weighted = [
        (item.share or 0.0, getattr(item, lens))
        for item in components
        if getattr(item, lens) is not None
    ]
    total = sum(share for share, _ in weighted)
    return sum(share * move for share, move in weighted) / total if total else None


def _observed_share(components: list[Component], lens: str) -> float:
    """Assumed revenue share of the components that have data for ``lens``."""
    return sum(item.share or 0.0 for item in components if getattr(item, lens) is not None)


def _status(adverse: float | None, breakeven: float | None, below: bool) -> str:
    if adverse is None:
        return "no_data"
    if adverse <= 0:
        return "easing"
    if below or (breakeven is not None and adverse > breakeven):
        return "exceeds"
    if breakeven is not None and adverse >= 0.5 * breakeven:
        return "tight"
    return "watch"


def _worst(qtd: float | None, latest: float | None, *, sign: int = 1) -> tuple[float | None, str]:
    """The more adverse of the two lenses (rises when ``sign`` is 1, falls when -1)."""
    candidates = [
        (sign * v, lens) for v, lens in ((qtd, "qtd"), (latest, "latest")) if v is not None
    ]
    return max(candidates) if candidates else (None, "")


def _latest(components: list[Component]) -> tuple[str, str]:
    """The most recent observation date in a basket, with its display label."""
    dated = [(item.latest_date, item.latest_label) for item in components if item.latest]
    return max(dated) if dated else ("", "")


def _published(channels: Iterable[Channel]) -> str:
    """Label of the newest observation in any channel, whether or not it is a move."""
    dated = [
        (item.latest_date, item.latest_label)
        for channel in channels
        for item in channel.components
        if item.latest_date
    ]
    return max(dated)[1] if dated else ""


def _summary(components: list[Component]) -> str:
    names = [item.label for item in components]
    if len(names) == 1:
        return names[0]
    return "Input basket (" + ", ".join(name.lower() for name in names) + ")"


def assess(position: BorrowerPosition, snapshot: dict[str, Any]) -> MarketAssessment:
    start, end = position.period_start, position.period_end
    has_facts = bool(
        start and end and position.ebit is not None and position.finance_cost and position.threshold
    )
    icr = cushion = cushion_pct = tolerance = None
    if has_facts:
        icr = position.ebit / position.finance_cost
        cushion = position.ebit - position.threshold * position.finance_cost
        cushion_pct = cushion / position.ebit * 100 if position.ebit > 0 else None
        if cushion > 0 and position.total_debt and position.total_debt > 0:
            tolerance = 4 * cushion / position.threshold / position.total_debt * 10_000
    below = cushion is not None and cushion <= 0
    channels: list[Channel] = []
    exposures = exposures_for(position.industry_code)

    if start and end:
        costs = _components([e for e in exposures if e.role == "cost"], snapshot, start, end)
        if costs:
            qtd, latest = _basket(costs, "qtd"), _basket(costs, "latest")
            observed = {name: _observed_share(costs, name) for name in ("qtd", "latest")}
            # Lenses are ranked by revenue impact, so one covering fewer inputs
            # cannot win on a bigger average alone.
            _, lens = _worst(
                qtd * observed["qtd"] if qtd is not None else None,
                latest * observed["latest"] if latest is not None else None,
            )
            adverse = qtd if lens == "qtd" else latest if lens == "latest" else None
            # A basket only partly published is sized on the shares observed:
            # an unpublished input is assumed neither to move with the others
            # nor to stay flat.
            share = observed[lens] if lens else sum(item.share or 0 for item in costs)
            breakeven = (
                cushion / (position.revenue * share) * 100
                if cushion is not None and cushion > 0 and position.revenue and share
                else None
            )
            status = _status(adverse, breakeven, below)
            # Inputs that offset each other to within half a percent are flat,
            # as the page's trend arrows already show them.
            if adverse is not None and abs(adverse) < 0.5:
                status = "flat"
            channels.append(
                Channel(
                    key="inputs",
                    label="Input costs",
                    role="cost",
                    unit="%",
                    summary=_summary(
                        [item for item in costs if getattr(item, lens) is not None]
                        if lens
                        else costs
                    ),
                    components=tuple(costs),
                    share=share,
                    qtd=qtd,
                    latest=latest,
                    latest_date=_latest(costs)[0],
                    latest_label=_latest(costs)[1],
                    breakeven=breakeven,
                    status=status,
                    lens=lens,
                    usage=adverse / breakeven if breakeven and adverse and adverse > 0 else None,
                )
            )
        for exposure in (e for e in exposures if e.role == "revenue"):
            (item,) = _components([exposure], snapshot, start, end)
            worst, lens = _worst(item.qtd, item.latest, sign=-1)
            best, _ = _worst(item.qtd, item.latest)
            status = (
                "no_data"
                if worst is None
                else "adverse"
                if worst > 0.5
                else "supportive"
                if best is not None and best > 0.5
                else "flat"
            )
            channels.append(
                Channel(
                    key=f"revenue:{item.driver}",
                    label=exposure.note,
                    role="revenue",
                    unit="%",
                    summary=item.label,
                    components=(item,),
                    share=exposure.share,
                    qtd=item.qtd,
                    latest=item.latest,
                    latest_date=item.latest_date,
                    latest_label=item.latest_label,
                    breakeven=None,
                    status=status,
                    lens=lens if status == "adverse" else ("latest" if item.latest else "qtd"),
                    usage=None,
                )
            )
        rate = DRIVERS_BY_KEY[RATE_DRIVER]
        move = driver_move(snapshot, rate, start, end)
        if move is not None:
            qtd = move["qtd_change"]
            latest = move["latest_change"] if move["latest_beyond_base"] else None
            adverse, lens = _worst(qtd, latest)
            status = _status(adverse, tolerance, below)
            if adverse is not None and abs(adverse) < 1:
                status = "flat"
            channels.append(
                Channel(
                    key="rates",
                    label="Borrowing rates",
                    role="rate",
                    unit="bp",
                    summary=rate.label,
                    components=(
                        Component(
                            driver=rate.key,
                            label=rate.label,
                            share=None,
                            note=rate.story,
                            qtd=qtd,
                            latest=latest,
                            qtd_label=move["qtd_label"],
                            latest_date=move["latest_date"],
                            latest_label=move["latest_label"],
                            publisher=rate.publisher,
                            series=rate.series,
                        ),
                    ),
                    share=None,
                    qtd=qtd,
                    latest=latest,
                    latest_date=move["latest_date"],
                    latest_label=move["latest_label"],
                    breakeven=tolerance,
                    status=status,
                    lens=lens,
                    usage=adverse / tolerance if tolerance and adverse and adverse > 0 else None,
                )
            )

    channels.sort(key=lambda item: (_CHANNEL_RANK[item.status], -(item.usage or 0)))
    statuses = {item.status for item in channels}
    if not has_facts:
        status = "no_data"
    elif below:
        status = "below"
    elif "exceeds" in statuses:
        status = "exceeds"
    elif "tight" in statuses:
        status = "tight"
    elif statuses & {"watch", "adverse"}:
        status = "watch"
    elif statuses & {"easing", "supportive"}:
        status = "easing"
    elif not statuses or statuses == {"no_data"}:
        # Missing market data is not the same as no exposure, and a series not
        # yet published past the quarter is not the same as a missing one.
        status = "awaiting" if _published(channels) else "no_market"
    else:
        status = "unexposed"
    next_quarter = quarter_label(end, 1)
    following_quarter = quarter_label(end, 2)
    headline, implication = _narrative(
        status, channels, position, icr, cushion_pct, tolerance, next_quarter, following_quarter
    )
    pressure = max((item.usage or 0 for item in channels), default=0.0) + (100 if below else 0)
    return MarketAssessment(
        position=position,
        status=status,
        label=STATUS_LABELS[status],
        priority=QUEUE_PRIORITY[status],
        icr=icr,
        cushion=cushion,
        cushion_pct=cushion_pct,
        rate_tolerance_bp=tolerance,
        channels=tuple(channels),
        headline=headline,
        implication=implication,
        pressure=pressure,
        next_quarter=next_quarter,
        following_quarter=following_quarter,
        news=_news(snapshot, channels),
    )


def _move_text(channel: Channel, next_quarter: str) -> str:
    if channel.lens == "latest":
        where = f"at {channel.latest_label} prices"
    else:
        where = f"in the {next_quarter} so far"
    unit = " bp" if channel.unit == "bp" else "%"
    fmt = "{:+.0f}" if channel.unit == "bp" else "{:+.1f}"
    return f"{channel.summary} {fmt.format(channel.move)}{unit} {where}"


def _narrative(
    status: str,
    channels: list[Channel],
    position: BorrowerPosition,
    icr: float | None,
    cushion_pct: float | None,
    tolerance: float | None,
    next_quarter: str,
    following_quarter: str,
) -> tuple[str, str]:
    lead = channels[0] if channels else None
    if status == "no_data":
        return (
            "No complete quarter with EBIT, finance cost and an interest-cover minimum.",
            "Market moves cannot be sized against this borrower until statements are loaded.",
        )
    if status == "no_market":
        return (
            "No market series has data for this borrower's period yet.",
            "Nothing is inferred while sources are unavailable; check source health.",
        )
    if status == "below":
        headline = (
            f"Interest cover {icr:.2f}x is already below the {position.threshold:.2f}x minimum"
        )
        if lead and lead.status == "exceeds" and lead.move is not None:
            move = _move_text(lead, next_quarter)
            return (
                f"{headline}; {move[0].lower()}{move[1:]} adds pressure.",
                "The breach stands on reported numbers. Market moves change the size of the "
                "cure needed, not the breach itself.",
            )
        return (
            f"{headline}.",
            "The breach stands on reported numbers; tracked market moves have not worsened it.",
        )
    thin = ""
    if cushion_pct is not None and cushion_pct < 15:
        thin = f" The cushion is thin: EBIT can fall only {cushion_pct:.1f}%"
        thin += f", or rates rise {tolerance:.0f} bp, before a breach." if tolerance else "."
    if status == "awaiting" and position.period_end is not None:
        end = position.period_end
        return (
            f"Market series for this borrower are published only to {_published(channels)}; "
            f"nothing after the quarter to {end.day} {end:%b %Y} is out yet.",
            "Monthly IMF and OECD prices arrive about two months in arrears. Nothing is "
            "estimated until newer data is published." + thin,
        )
    if status in {"exceeds", "tight", "watch"} and lead is not None and lead.move is not None:
        unit = " bp" if lead.unit == "bp" else "%"
        headline = _move_text(lead, next_quarter)
        if lead.breakeven is not None:
            noun = "tolerance" if lead.role == "rate" else "breakeven"
            amount = f"{lead.breakeven:,.0f}" if lead.unit == "bp" else f"{lead.breakeven:.1f}"
            headline += f" vs a {amount}{unit} {noun}"
        headline += "."
        used = f"{lead.usage:.0%}" if lead.usage else "part"
        date_text = lead.latest_label
        if lead.role == "rate":
            implication = (
                "Confirm the floating-rate share of debt and the next reset dates; "
                "fixed-rate or hedged debt does not reprice."
            )
        elif lead.role == "revenue":
            implication = f"{lead.label}: realisations weaken. Check order book and pricing."
        elif status == "watch":
            implication = "Adverse but within the cushion. No action from market data alone."
        elif lead.lens == "latest" and lead.qtd is None:
            outcome = "is at risk" if status == "exceeds" else f"would lose {used} of its cushion"
            implication = (
                f"No {next_quarter} prices are published yet. If {date_text} prices hold, the "
                f"{next_quarter} test {outcome} unless costs are passed through."
            )
        elif lead.lens == "latest" and lead.qtd <= 0:
            outcome = "is at risk" if status == "exceeds" else f"would lose {used} of its cushion"
            implication = (
                f"The {next_quarter} test should benefit from lower average costs so far, but if "
                f"{date_text} prices hold, the {following_quarter} test {outcome} unless costs "
                "are passed through."
            )
        elif lead.lens == "latest":
            implication = (
                f"Both the {next_quarter} average so far and {date_text} prices are higher than "
                "in the reported quarter. Check pass-through clauses and hedges now."
            )
        elif status == "exceeds":
            implication = (
                f"Average costs in the {next_quarter} so far already exceed what the cushion "
                "absorbs. Check pass-through clauses, hedges and inventory cover before the "
                "statements arrive."
            )
        else:
            implication = (
                f"Average costs in the {next_quarter} so far would use {used} of the cushion "
                "if none is passed through. Check pass-through and hedges."
            )
        return headline, implication + thin
    if status == "easing":
        easing = next((c for c in channels if c.status in {"easing", "supportive"}), None)
        if easing is not None and easing.move is not None:
            implication = (
                f"Supports {easing.label.lower()}."
                if easing.role == "revenue"
                else "Lower costs support interest cover."
            )
            return _move_text(easing, next_quarter) + ".", implication + (
                thin or " No action from market data alone."
            )
    mapped = [channel for channel in channels if channel.role != "rate"]
    rates = next((channel for channel in channels if channel.role == "rate"), None)
    drivers = (
        "No mapped market driver for this sector"
        if not mapped
        else "Tracked market moves are flat"
        if all(channel.status == "flat" for channel in mapped)
        else "Tracked market moves are flat or without newer data"
    )
    rate_text = (
        "borrowing rates are flat"
        if rates is not None and rates.status == "flat"
        else "borrowing-rate data is not yet published past the quarter"
        if rates is not None
        else "no borrowing-rate data is available"
    )
    return (
        f"{drivers}, and {rate_text}.",
        "Market data adds no signal here; rely on borrower evidence." + thin,
    )


def _news(snapshot: dict[str, Any], channels: list[Channel]) -> tuple[dict[str, Any], ...]:
    stories: list[dict[str, Any]] = []
    seen: set[str] = set()
    for channel in channels:
        if channel.status in {"flat", "no_data"}:
            continue
        for component in sorted(channel.components, key=lambda item: -(item.share or 0)):
            # A case note cites established press first, then the most recent.
            reporting = sorted(
                snapshot.get("news", {}).get(component.driver, []),
                key=lambda story: (story["established"], story["published_at"]),
                reverse=True,
            )
            for story in reporting[:2]:
                if story["url"] not in seen:
                    seen.add(story["url"])
                    stories.append({**story, "driver_label": component.label})
    return tuple(stories[:4])


def sort_key(assessment: MarketAssessment) -> tuple[int, float, float]:
    return (
        STATUS_ORDER.index(assessment.status),
        -assessment.pressure,
        assessment.cushion_pct if assessment.cushion_pct is not None else 1e9,
    )
