"""Read model for the borrower remediation planner.

Everything shown is a figure the planner already computed; this module only
formats and arranges it.  The one input the reader controls is the size of
each lever, which arrives as ``size.<LEVER-CODE>`` query parameters, is
parsed here into floats and is clamped to the borrower's capacity by the
domain — never trusted as-is.
"""

from __future__ import annotations

import calendar
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final
from urllib.parse import urlencode

from markupsafe import Markup

from covenant_radar.domain.remediation import (
    CovenantOutlook,
    CustomPlan,
    LeverAssessment,
    RemediationReport,
)
from covenant_radar.domain.remediation.levers import format_size
from covenant_radar.services.remediation import MemoAttachment
from covenant_radar.web.svg.fan import FanSeries, render_fan_svg

SIZE_PREFIX: Final[str] = "size."
_MAX_SIZE_PARAMETERS: Final[int] = 24
_RATIO_LABELS: Final[Mapping[str, str]] = {
    "leverage_ratio": "Debt / tangible net worth",
    "interest_coverage_ratio": "EBIT / finance cost",
    "fixed_charge_coverage_ratio": "EBITDA / finance cost",
    "current_ratio": "Current assets / current liabilities",
    "quick_ratio": "Quick assets / current liabilities",
    "debt_to_ebitda": "Debt / EBITDA",
    "net_debt_to_ebitda": "Net debt / EBITDA",
}
_STATUS_TITLES: Final[Mapping[str, str]] = {
    "no_action": "No action indicated",
    "clears": "In breach on the filings, but projected to clear without a package",
    "cured": "A package restores every covenant without a reset",
    "cured_with_reset": "Self-help falls short; the package needs a covenant reset",
    "partial": "Even the full package leaves a covenant exposed",
}


@dataclass(frozen=True, slots=True)
class ProbabilityCell:
    display: str
    tone: str
    value: float


@dataclass(frozen=True, slots=True)
class QuarterRow:
    label: str
    baseline: str
    package: str
    baseline_probability: str
    package_probability: str


@dataclass(frozen=True, slots=True)
class CovenantRowView:
    reference: str
    name: str
    formula: str
    test_display: str
    filed_display: str
    filed_tone: str
    at_risk: bool
    baseline: ProbabilityCell
    package: ProbabilityCell
    package_start_display: str
    chart: Markup
    quarters: tuple[QuarterRow, ...]


@dataclass(frozen=True, slots=True)
class StepView:
    number: int
    code: str
    title: str
    owner: str
    size_display: str
    lender_decision: bool
    rationale: tuple[str, ...]
    assumptions: tuple[str, ...]
    cells: tuple[ProbabilityCell, ...]


@dataclass(frozen=True, slots=True)
class LeverView:
    code: str
    field_name: str
    title: str
    owner: str
    available: bool
    unavailable_reason: str | None
    rationale: tuple[str, ...]
    assumptions: tuple[str, ...]
    lender_decision: bool
    maximum: float
    step: float
    value: float
    value_display: str
    maximum_display: str
    recommended_display: str
    in_plan: bool
    plan_size_display: str | None
    helps: tuple[str, ...]
    hurts: tuple[str, ...]
    standalone: tuple[ProbabilityCell, ...]
    reaches_target: bool
    unit: str


@dataclass(frozen=True, slots=True)
class StressRowView:
    label: str
    cells: tuple[tuple[ProbabilityCell, ProbabilityCell], ...]


@dataclass(frozen=True, slots=True)
class DriverView:
    label: str
    value_display: str
    basis: str


@dataclass(frozen=True, slots=True)
class UnsimulatedCovenantView:
    reference: str
    name: str
    reason: str


@dataclass(frozen=True, slots=True)
class MemoCardView:
    """Whether the next memo cites the package on screen, and how to make it."""

    state: str
    title: str
    message: str
    recorded_label: str | None
    recorded_steps: tuple[str, ...]
    can_record: bool
    button_label: str
    action_href: str
    hidden_sizes: tuple[tuple[str, str], ...]
    error: str | None


@dataclass(frozen=True, slots=True)
class RemediationScreenView:
    borrower_reference: str
    borrower_name: str
    as_of_label: str
    status: str
    status_title: str
    status_message: str
    target_display: str
    horizon_label: str
    covenants: tuple[CovenantRowView, ...]
    unsimulated: tuple[UnsimulatedCovenantView, ...]
    at_risk_names: tuple[str, ...]
    baseline_cells: tuple[ProbabilityCell, ...]
    steps: tuple[StepView, ...]
    levers: tuple[LeverView, ...]
    other_levers: tuple[LeverView, ...]
    stress_headers: tuple[str, ...]
    stresses: tuple[StressRowView, ...]
    drivers: tuple[DriverView, ...]
    paths: int
    paths_display: str
    seed_display: str
    content_hash: str
    package_is_recommended: bool
    has_reset: bool
    reset_href: str
    case_href: str
    page_href: str
    memo: MemoCardView | None = None


def parse_sizes(query: Mapping[str, str] | Sequence[tuple[str, str]]) -> dict[str, float] | None:
    """Read ``size.<CODE>`` parameters; ``None`` when the reader set none."""

    items = query.items() if isinstance(query, Mapping) else query
    sizes: dict[str, float] = {}
    seen = False
    for key, raw in items:
        if not key.startswith(SIZE_PREFIX):
            continue
        seen = True
        if len(sizes) >= _MAX_SIZE_PARAMETERS:
            break
        code = key[len(SIZE_PREFIX) :].strip().upper()
        if not code or len(code) > 80 or not all(ch.isalnum() or ch == "-" for ch in code):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            sizes[code] = max(value, 0.0)
    return sizes if seen else None


def build_remediation_view(
    report: RemediationReport,
    package: CustomPlan,
    *,
    is_recommended: bool,
    attachment: MemoAttachment | None = None,
    record_error: str | None = None,
) -> RemediationScreenView:
    position = report.position
    quarter_labels = _quarter_labels(position.as_of, report.horizon)
    at_risk = report.at_risk
    outlooks = report.baseline.outlooks
    names = {item.reference: item.covenant.name for item in outlooks}

    covenants = tuple(
        _covenant_row(
            item,
            package.outcome.get(item.reference) if report.at_risk else None,
            report,
            quarter_labels,
        )
        for item in outlooks
    )
    steps = tuple(
        StepView(
            number=index,
            code=step.lever.code,
            title=step.lever.title,
            owner=step.lever.owner,
            size_display=step.lever.describe_size(step.size),
            lender_decision=step.lever.lender_decision,
            rationale=step.capacity.rationale,
            assumptions=step.capacity.assumptions,
            cells=tuple(_cell(step.outcome.probability(ref), report.target) for ref in at_risk),
        )
        for index, step in enumerate(report.plan, start=1)
    )
    plan_sizes = report.plan_sizes
    relevant: list[LeverView] = []
    others: list[LeverView] = []
    for assessment in report.levers:
        view = _lever_view(assessment, report, package, plan_sizes, names)
        if assessment.relevant and (at_risk and any(ref in at_risk for ref in assessment.helps)):
            relevant.append(view)
        elif assessment.lever.covenant is None:
            others.append(view)
    stresses = tuple(
        StressRowView(
            label=result.stress.label,
            cells=tuple(
                (
                    _cell(result.baseline.probability(ref), report.target),
                    _cell(result.plan.probability(ref), report.target),
                )
                for ref in at_risk
            ),
        )
        for result in report.stresses
    )
    unsimulated = tuple(
        UnsimulatedCovenantView(
            item.reference,
            item.name,
            (
                f"Its test ({item.definition_ref.replace('_', ' ')}) is not projected by the "
                "line model; read its position on the covenant tab."
            ),
        )
        for item in position.covenants
        if not item.simulated
    )
    reference = position.reference
    return RemediationScreenView(
        borrower_reference=reference,
        borrower_name=position.name,
        as_of_label=f"{position.as_of:%d %b %Y}",
        status=report.status,
        status_title=(
            "No action indicated for the simulated covenants"
            if report.status == "no_action" and unsimulated
            else _STATUS_TITLES[report.status]
        ),
        status_message=_status_message(report, names),
        target_display=f"{report.target:.0%}",
        horizon_label=(
            f"next {report.horizon} quarterly tests ({quarter_labels[1]}–{quarter_labels[-1]})"
        ),
        covenants=covenants,
        unsimulated=unsimulated,
        at_risk_names=tuple(names[ref] for ref in at_risk),
        baseline_cells=tuple(
            _cell(report.baseline.probability(ref), report.target) for ref in at_risk
        ),
        steps=steps,
        levers=tuple(relevant),
        other_levers=tuple(others),
        stress_headers=tuple(names[ref] for ref in at_risk),
        stresses=stresses,
        drivers=tuple(
            DriverView(
                driver.label, _driver_value(driver.code, driver.value, driver.unit), driver.basis
            )
            for driver in report.drivers.evidence
        ),
        paths=report.paths,
        paths_display=f"{report.paths:,}",
        seed_display=f"{report.baseline.seed:016x}"[:12],
        content_hash=report.content_hash[:16],
        package_is_recommended=is_recommended,
        has_reset=bool(report.at_risk)
        and any(code.startswith("COVENANT-RESET-") for code in package.sizes),
        reset_href=f"/borrowers/{reference}/remediation",
        case_href=f"/borrowers/{reference}",
        page_href=f"/borrowers/{reference}/remediation?"
        + urlencode({f"{SIZE_PREFIX}{code}": _plain(size) for code, size in package.sizes.items()}),
        memo=_memo_card(report, package, is_recommended, attachment, record_error),
    )


def _memo_card(
    report: RemediationReport,
    package: CustomPlan,
    is_recommended: bool,
    attachment: MemoAttachment | None,
    error: str | None,
) -> MemoCardView | None:
    """Tell the reader what the memo will cite, and offer to record this package.

    Hidden when there is neither a package on screen nor one recorded: a
    borrower with nothing at risk has nothing to carry into a memo.
    """

    if attachment is None:
        return None
    recorded = attachment.recorded
    has_package = bool(report.at_risk) and bool(package.sizes)
    if not has_package and recorded is None and error is None:
        return None
    covenant = attachment.covenant_name
    if covenant is None:
        state = "no_forecast"
        title = "Not available for the memo yet"
        message = (
            "A memo is written about this borrower's worst forecast covenant, and no "
            "forecast is recorded yet, so there is nothing to attach a package to."
        )
    elif recorded is not None and has_package and _same_sizes(recorded.sizes, package.sizes):
        state = "current"
        title = "The memo cites this package"
        message = (
            f"The next memo on the {covenant.lower()} covenant cites these steps in this "
            "sized wording, each under its catalogue entry."
        )
    elif recorded is not None:
        state = "different"
        title = "The memo cites a different package"
        message = f"The next memo on the {covenant.lower()} covenant cites the package below." + (
            " Record the one on screen to replace it." if has_package else ""
        )
    else:
        state = "unrecorded"
        title = "Carry this package into the memo"
        message = (
            f"Until a package is recorded, a memo on the {covenant.lower()} covenant can cite "
            "only the generic catalogue. Recording stores each step against that forecast "
            "with its size and assumptions, and audits it."
        )
    return MemoCardView(
        state=state,
        title=title,
        message=message,
        recorded_label=(
            f"Recorded {recorded.recorded_at:%d %b %Y, %H:%M} UTC"
            + (" · your package" if recorded.kind == "custom" else " · recommended package")
            if recorded is not None
            else None
        ),
        recorded_steps=recorded.wording if recorded is not None else (),
        can_record=has_package and state in {"unrecorded", "different"},
        button_label=(
            "Record this package instead"
            if state == "different"
            else "Record this package for the memo"
        ),
        action_href=f"/borrowers/{report.position.reference}/remediation/record",
        hidden_sizes=(
            ()
            if is_recommended
            else tuple(
                (f"{SIZE_PREFIX}{code}", _plain(size)) for code, size in package.sizes.items()
            )
        ),
        error=error,
    )


def _same_sizes(first: Mapping[str, float], second: Mapping[str, float]) -> bool:
    keys = set(first) | set(second)
    return all(abs(first.get(key, 0.0) - second.get(key, 0.0)) < 1e-3 for key in keys)


def _covenant_row(
    baseline: CovenantOutlook,
    package: CovenantOutlook | None,
    report: RemediationReport,
    quarter_labels: Sequence[str],
) -> CovenantRowView:
    covenant = baseline.covenant
    series = [FanSeries("baseline", "Do nothing", _float(baseline.start), baseline.quantiles)]
    if package is not None:
        series.append(
            FanSeries("package", "With package", _float(package.start), package.quantiles)
        )
    package = package or baseline
    symbol = "≤" if covenant.direction == "max" else "≥"
    threshold = float(covenant.threshold)
    threshold_label = f"Test {symbol} {threshold:.2f}x"
    package_threshold = float(package.threshold)
    reset = package_threshold if abs(package_threshold - threshold) > 1e-9 else None
    chart = render_fan_svg(
        f"fan-{covenant.reference}",
        title=f"{covenant.name}: do nothing against the package, next {report.horizon} quarters",
        quarter_labels=quarter_labels,
        threshold=threshold,
        threshold_label=threshold_label,
        breach_above=covenant.direction == "max",
        series=series,
        reset_threshold=reset,
        reset_label=f"Reset {symbol} {reset:.2f}x" if reset is not None else "",
    )
    quarters = [
        QuarterRow(
            quarter_labels[0] + " (filed)",
            _ratio(baseline.start),
            _ratio(package.start),
            "—",
            "—",
        )
    ]
    for index, label in enumerate(quarter_labels[1:]):
        quarters.append(
            QuarterRow(
                label,
                _median(baseline.quantiles[index]),
                _median(package.quantiles[index]),
                f"{baseline.breach_by_quarter[index]:.0%}",
                f"{package.breach_by_quarter[index]:.0%}",
            )
        )
    filed_breached = baseline.start_breached
    return CovenantRowView(
        reference=covenant.reference,
        name=covenant.name,
        formula=_RATIO_LABELS.get(
            covenant.definition_ref, covenant.definition_ref.replace("_", " ")
        ),
        test_display=f"{symbol} {threshold:.2f}x",
        filed_display=_ratio(baseline.start),
        filed_tone="breach" if filed_breached else "ok",
        at_risk=covenant.reference in report.at_risk,
        baseline=_cell(baseline.breach_within_horizon, report.target),
        package=_cell(package.breach_within_horizon, report.target),
        package_start_display=_ratio(package.start),
        chart=chart,
        quarters=tuple(quarters),
    )


def _lever_view(
    assessment: LeverAssessment,
    report: RemediationReport,
    package: CustomPlan,
    plan_sizes: Mapping[str, float],
    names: Mapping[str, str],
) -> LeverView:
    lever = assessment.lever
    capacity = assessment.capacity
    value = package.sizes.get(lever.code, 0.0)
    in_plan = lever.code in plan_sizes
    standalone: tuple[ProbabilityCell, ...] = ()
    if assessment.outcome is not None:
        standalone = tuple(
            _cell(assessment.outcome.probability(ref), report.target) for ref in report.at_risk
        )
    return LeverView(
        code=lever.code,
        field_name=f"{SIZE_PREFIX}{lever.code}",
        title=lever.title,
        owner=lever.owner,
        available=capacity.available,
        unavailable_reason=capacity.unavailable_reason,
        rationale=capacity.rationale,
        assumptions=capacity.assumptions,
        lender_decision=lever.lender_decision,
        maximum=capacity.maximum,
        step=capacity.step,
        value=value,
        value_display=lever.describe_size(value),
        maximum_display=lever.describe_size(capacity.maximum),
        recommended_display=lever.describe_size(assessment.recommended),
        in_plan=in_plan,
        plan_size_display=lever.describe_size(plan_sizes[lever.code]) if in_plan else None,
        helps=tuple(names.get(ref, ref) for ref in assessment.helps),
        hurts=tuple(names.get(ref, ref) for ref in assessment.hurts),
        standalone=standalone,
        reaches_target=assessment.reaches_target,
        unit=lever.unit,
    )


def _status_message(report: RemediationReport, names: Mapping[str, str]) -> str:
    paths = f"{report.paths:,}"
    target = f"{report.target:.0%}"
    if report.status == "no_action":
        return (
            f"Every simulated covenant passes on the filed lines and fails a projected test on "
            f"no more than {target} of {paths} simulated paths over the next "
            f"{report.horizon} quarters."
        )
    if report.status == "clears":
        breached = ", ".join(names[ref] for ref in report.at_risk)
        return (
            f"{breached} fails its test on the filed lines, yet fails a projected test on no "
            f"more than {target} of {paths} simulated paths over the next {report.horizon} "
            "quarters as the filed trends run on, so no package is sized. The filed breach "
            "itself still needs the lenders' response."
        )
    exposed = [
        names[ref] for ref in report.at_risk if report.plan_outcome.probability(ref) > report.target
    ]
    before = ", ".join(
        f"{names[ref].lower()} {report.baseline.probability(ref):.0%} → "
        f"{report.plan_outcome.probability(ref):.0%}"
        for ref in report.at_risk
    )
    self_help = report.self_help_outcome
    has_reset = any(step.lever.covenant for step in report.plan)
    alone = ", ".join(
        f"{names[ref].lower()} {self_help.probability(ref):.0%}" for ref in report.at_risk
    )
    if report.status == "partial":
        return (
            f"Breach probability over the horizon: {before}. "
            + (f"The borrower's own levers alone reach {alone}. " if has_reset else "")
            + f"{', '.join(exposed)} stays above the {target} target even after the whole "
            "package — a matter for escalation, not a package alone."
        )
    if report.status == "cured_with_reset":
        return (
            f"Breach probability over the horizon: {before}. The borrower's own levers alone "
            f"reach {alone}; the reset steps are lender concessions for the credit committee "
            "that move the test, not the borrower."
        )
    return f"Breach probability over the horizon: {before}, without resetting any covenant."


def _cell(probability: float, target: float) -> ProbabilityCell:
    if probability > 0.5:
        tone = "breach"
    elif probability > target:
        tone = "watch"
    else:
        tone = "ok"
    display = "<1%" if 0 < probability < 0.005 else f"{probability:.0%}"
    return ProbabilityCell(display, tone, probability)


def _quarter_labels(as_of: date, horizon: int) -> tuple[str, ...]:
    labels = [f"{as_of:%b %y}"]
    year, month = as_of.year, as_of.month
    for _ in range(horizon):
        month += 3
        if month > 12:
            month -= 12
            year += 1
        day = calendar.monthrange(year, month)[1]
        labels.append(f"{date(year, month, day):%b %y}")
    return tuple(labels)


def _float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _ratio(value: object) -> str:
    number = _float(value)
    if number is None or not math.isfinite(number):
        return "n/a"
    return f"{number:.2f}x"


def _median(quantiles: tuple[float, float, float]) -> str:
    return _ratio(quantiles[1])


def _plain(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _driver_value(code: str, value: float, unit: str) -> str:
    if unit.startswith("%"):
        return f"{value:+.1%} a quarter" if "quarter" in unit else f"{value:.2%} a year"
    if unit.startswith("±"):
        return f"±{value:.0%}"
    if unit.startswith("₹"):
        sign = (
            ("+" if value >= 0 else "−") if code.endswith("drift") else ("−" if value < 0 else "")
        )
        return f"{sign}₹{abs(value):,.0f} cr" + (" a quarter" if "quarter" in unit else "")
    return f"{value:,.2f}"


__all__ = [
    "MemoCardView",
    "SIZE_PREFIX",
    "UnsimulatedCovenantView",
    "RemediationScreenView",
    "build_remediation_view",
    "format_size",
    "parse_sizes",
]
