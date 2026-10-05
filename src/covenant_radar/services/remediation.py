"""Application boundary for borrower-specific remediation planning.

Reads one borrower's standing filings and live covenant versions under the
caller's portfolio scope and hands them to the pure planner in
``domain/remediation``.  The planner is deterministic in its inputs, so a
report is cached in-process against a key built from exactly those inputs —
the latest filed period id and version, and every covenant version id — and
a restatement or an amended covenant produces a new key rather than a stale
answer.

``record`` carries a package into the memo.  It writes one ``Simulation``
row per step against the forecast the next memo is written about, each
citing the step's catalogue entry and persisting the sized wording, so the
memo copies a stored fact rather than recalculating one.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from threading import Lock
from typing import Final, Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.core.errors import NotFound, ValidationError
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.forecast import Forecast, Intervention, Simulation
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.statements import FinancialPeriod, StatementLineValue
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.remediation import (
    MODEL_VERSION,
    RECORD_SOURCE,
    BorrowerPosition,
    CovenantTerm,
    CustomPlan,
    PlanStep,
    QuarterLines,
    RemediationReport,
    build_report,
    catalogue_code,
    custom_plan,
    package_steps,
    step_wording,
)
from covenant_radar.services.memo_records import memo_subject_forecast, recorded_package_rows

#: Two years of quarters: enough for a trend and a year-on-year comparison.
MAX_QUARTERS: Final[int] = 8
_CACHE_SIZE: Final[int] = 64

_SIMULATION_CREATED_EVENT: Final[str] = "simulation_created"
_FRACTION: Final[Decimal] = Decimal("0.0001")

_cache: OrderedDict[tuple[object, ...], RemediationReport] = OrderedDict()
_cache_lock = Lock()


class AuditWriter(Protocol):
    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class RecordedPackage:
    """A package recorded for the memo, read back from its step rows."""

    package_id: str
    recorded_at: datetime
    kind: str
    sizes: Mapping[str, float]
    wording: tuple[str, ...]
    simulation_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class MemoAttachment:
    """What the next memo for a borrower would cite from the planner.

    ``covenant_name`` is ``None`` when there is no forecast to write a memo
    about, and then nothing can be recorded.
    """

    covenant_name: str | None
    recorded: RecordedPackage | None


class RemediationService:
    """Build remediation reports for in-scope borrowers."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def report(self, reference: str, *, scope: Scope) -> RemediationReport | None:
        """The borrower's remediation report, or ``None`` if not in scope or unfiled."""

        loaded = self._load(reference, scope)
        if loaded is None:
            return None
        key, position = loaded
        with _cache_lock:
            cached = _cache.get(key)
            if cached is not None:
                _cache.move_to_end(key)
                return cached
        report = build_report(position)
        with _cache_lock:
            _cache[key] = report
            while len(_cache) > _CACHE_SIZE:
                _cache.popitem(last=False)
        return report

    def custom(self, report: RemediationReport, sizes: Mapping[str, float]) -> CustomPlan:
        return custom_plan(report, sizes)

    def memo_attachment(self, reference: str, *, scope: Scope) -> MemoAttachment | None:
        """The memo's subject covenant and the package recorded against it."""

        borrower = self._borrower(reference, scope)
        if borrower is None:
            return None
        forecast = memo_subject_forecast(self.session, borrower, scope=scope)
        if forecast is None:
            return MemoAttachment(None, None)
        covenant = self._forecast_covenant(forecast)
        rows = recorded_package_rows(self.session, forecast, scope=scope)
        recorded = None
        if rows:
            first = rows[0][0].parameters
            recorded = RecordedPackage(
                package_id=str(first.get("package_id")),
                recorded_at=rows[0][0].created_at,
                kind=str(first.get("package", "recommended")),
                sizes={
                    str(row.parameters.get("lever_code")): float(str(row.parameters.get("size", 0)))
                    for row, _intervention in rows
                },
                wording=tuple(str(row.parameters.get("wording", "")) for row, _i in rows),
                simulation_ids=tuple(row.id for row, _intervention in rows),
            )
        return MemoAttachment(covenant.name if covenant is not None else None, recorded)

    def record(
        self,
        reference: str,
        *,
        scope: Scope,
        sizes: Mapping[str, float] | None,
        audit: AuditWriter,
        actor_id: UUID | None,
        request_id: str,
    ) -> RecordedPackage:
        """Record the package for the memo; ``sizes=None`` records the recommendation.

        Every size is clamped to the borrower's capacity exactly as the
        screen clamps it.  The rows are added to the caller's transaction and
        audited there, so a failure leaves neither behind.
        """

        borrower = self._borrower(reference, scope)
        if borrower is None:
            raise NotFound(f"Borrower {reference!r} was not found within the current scope.")
        report = self.report(reference, scope=scope)
        if report is None:
            raise ValidationError("No quarterly filings to plan from.", field="package")
        steps = package_steps(report, report.plan_sizes if sizes is None else sizes)
        if not steps:
            raise ValidationError(
                "There is no package to record: no lever is sized.", field="package"
            )
        forecast = memo_subject_forecast(self.session, borrower, scope=scope)
        if forecast is None:
            raise ValidationError(
                "A memo needs a forecast for this borrower and none is recorded yet, "
                "so there is nothing to attach the package to.",
                field="package",
            )
        covenant = self._forecast_covenant(forecast)
        entries = self._catalogue_entries({catalogue_code(step.lever) for step in steps})

        kind = "recommended" if sizes is None else "custom"
        package_id = str(uuid4())
        now = datetime.now(UTC)
        reference_code = covenant.reference if covenant is not None else None
        name = covenant.name if covenant is not None else "covenant"
        before = _probability(report.baseline.get(reference_code) if reference_code else None)
        rows: list[Simulation] = []
        for index, step in enumerate(steps, start=1):
            after = _probability(step.outcome.get(reference_code) if reference_code else None)
            row = Simulation(
                id=uuid4(),
                forecast_id=forecast.id,
                intervention_id=entries[catalogue_code(step.lever)].id,
                parameters={
                    "source": RECORD_SOURCE,
                    "model_version": MODEL_VERSION,
                    "package_id": package_id,
                    "package": kind,
                    "step": index,
                    "steps": len(steps),
                    "lever_code": step.lever.code,
                    "size": round(step.size, 4),
                    "unit": step.lever.unit,
                    "size_display": step.lever.describe_size(step.size),
                    "wording": step_wording(step.lever, step.size),
                    "covenant_reference": reference_code,
                    "content_hash": report.content_hash,
                    "paths": report.paths,
                    "horizon_quarters": report.horizon,
                    "as_of": report.position.as_of.isoformat(),
                },
                assumptions={
                    "assumptions": _step_assumptions(
                        report, step, index, len(steps), name, forecast, after is not None
                    )
                },
                projected_cross_date=None,
                probability=after,
                delta_days=None,
                delta_probability=(
                    after - before if after is not None and before is not None else None
                ),
                created_at=now,
                updated_at=now,
                created_by_id=actor_id,
                updated_by_id=actor_id,
                request_id=request_id,
            )
            self.session.add(row)
            rows.append(row)
            if after is not None:
                before = after
        self.session.flush()
        for row in rows:
            audit.record(
                _SIMULATION_CREATED_EVENT,
                ("simulation", row.id),
                {
                    "forecast_id": str(forecast.id),
                    "intervention_id": str(row.intervention_id),
                    "source": RECORD_SOURCE,
                    "package_id": package_id,
                    "step": row.parameters["step"],
                    "content_hash": report.content_hash,
                },
                actor=actor_id,
                request_id=request_id,
            )
        return RecordedPackage(
            package_id=package_id,
            recorded_at=now,
            kind=kind,
            sizes={step.lever.code: step.size for step in steps},
            wording=tuple(step_wording(step.lever, step.size) for step in steps),
            simulation_ids=tuple(row.id for row in rows),
        )

    def _forecast_covenant(self, forecast: Forecast) -> Covenant | None:
        return self.session.execute(
            select(Covenant)
            .join(CovenantVersion, CovenantVersion.covenant_id == Covenant.id)
            .where(CovenantVersion.id == forecast.covenant_version_id)
        ).scalar_one_or_none()

    def _catalogue_entries(self, codes: set[str]) -> dict[str, Intervention]:
        """The active catalogue entry for every code, or a refusal naming the gap.

        The memo may only cite active entries, so recording a step whose
        entry is retired would produce a package the memo silently drops.
        """

        found = {
            row.code: row
            for row in self.session.execute(
                select(Intervention).where(Intervention.code.in_(sorted(codes)))
            ).scalars()
        }
        missing = sorted(code for code in codes if code not in found or not found[code].is_active)
        if missing:
            raise ValidationError(
                "The action catalogue has no active entry for "
                + ", ".join(missing)
                + "; Risk can enable it on the catalogue screen.",
                field="package",
            )
        return found

    def borrower(self, reference: str, *, scope: Scope) -> Borrower | None:
        """The in-scope borrower, whether or not it has quarterly filings."""

        return self._borrower(reference, scope)

    def _borrower(self, reference: str, scope: Scope) -> Borrower | None:
        return self.session.execute(
            select(Borrower)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(Borrower.reference == reference.strip(), scope.predicate(Portfolio.path))
        ).scalar_one_or_none()

    def _load(
        self, reference: str, scope: Scope
    ) -> tuple[tuple[object, ...], BorrowerPosition] | None:
        borrower = self._borrower(reference, scope)
        if borrower is None:
            return None

        periods = list(
            self.session.execute(
                select(FinancialPeriod)
                .where(
                    FinancialPeriod.borrower_id == borrower.id,
                    FinancialPeriod.superseded_by_id.is_(None),
                    # The drivers annualise a quarter and step a quarter at a
                    # time; an annual or half-year period would distort both.
                    FinancialPeriod.period_type == "quarterly",
                )
                .order_by(
                    FinancialPeriod.period_end.desc(),
                    FinancialPeriod.version.desc(),
                    FinancialPeriod.id.desc(),
                )
            ).scalars()
        )
        # One standing period per period end, the latest version of each.
        by_end: dict[object, FinancialPeriod] = {}
        for period in periods:
            by_end.setdefault(period.period_end, period)
        chosen = sorted(by_end.values(), key=lambda period: period.period_end)[-MAX_QUARTERS:]
        if not chosen:
            return None
        lines: dict[object, dict[str, Decimal]] = {period.id: {} for period in chosen}
        for value in self.session.execute(
            select(StatementLineValue).where(
                StatementLineValue.period_id.in_([period.id for period in chosen])
            )
        ).scalars():
            lines[value.period_id][value.line_code] = value.value
        quarters = tuple(
            QuarterLines(period.period_end, lines[period.id])
            for period in chosen
            if lines[period.id]
        )
        if not quarters:
            return None

        records = self.session.execute(
            select(Covenant, CovenantVersion)
            .join(Facility, Facility.id == Covenant.facility_id)
            .join(CovenantVersion, CovenantVersion.covenant_id == Covenant.id)
            .where(Facility.borrower_id == borrower.id, Covenant.is_active.is_(True))
            .order_by(Covenant.reference, CovenantVersion.version_no, CovenantVersion.id)
        ).tuples()
        versions: dict[object, tuple[Covenant, CovenantVersion]] = {}
        for covenant, version in records:
            current = versions.get(covenant.id)
            live = version.status == "live"
            if (
                current is None
                or (live and current[1].status != "live")
                or (
                    live == (current[1].status == "live")
                    and version.version_no > current[1].version_no
                )
            ):
                versions[covenant.id] = (covenant, version)
        covenants = tuple(
            CovenantTerm(
                reference=covenant.reference,
                name=covenant.name,
                definition_ref=version.definition_ref or "custom",
                threshold=version.threshold,
                direction=version.direction,
                unit=version.unit,
            )
            for covenant, version in sorted(versions.values(), key=lambda item: item[0].reference)
        )
        position = BorrowerPosition(
            reference=borrower.reference,
            name=borrower.legal_name,
            industry_code=borrower.industry_code,
            quarters=quarters,
            covenants=covenants,
        )
        key = (
            borrower.id,
            tuple((period.id, period.version) for period in chosen),
            tuple(sorted(str(version.id) for _covenant, version in versions.values())),
        )
        return key, position


def _probability(outlook: object) -> Decimal | None:
    value = getattr(outlook, "breach_within_horizon", None)
    if not isinstance(value, float):
        return None
    return Decimal(str(value)).quantize(_FRACTION)


def _step_assumptions(
    report: RemediationReport,
    step: PlanStep,
    index: int,
    steps: int,
    covenant_name: str,
    forecast: Forecast,
    projected: bool,
) -> list[str]:
    as_of = f"{report.position.as_of:%d %b %Y}"
    statements = [
        f"Sized by the remediation planner ({MODEL_VERSION}) from the borrower's filed "
        f"quarterly lines to {as_of}; step {index} of {steps} in the package.",
    ]
    if projected:
        statements.append(
            f"The probability is the planner's simulated chance that the {covenant_name} "
            f"test fails in the next {report.horizon} quarters over {report.paths:,} seeded "
            "paths, after this step and the steps before it; it is not the forecast's "
            f"{forecast.horizon_days}-day probability."
        )
    else:
        statements.append(
            f"The planner does not project the {covenant_name} test, so no probability "
            "is attached to this step."
        )
    statements.extend(step.capacity.assumptions)
    if step.lever.lender_decision:
        statements.append("This step needs a lender decision; it is not a cure in itself.")
    statements.append("Advisory only: a package step is not a credit decision.")
    return statements


__all__ = [
    "MAX_QUARTERS",
    "AuditWriter",
    "MemoAttachment",
    "RecordedPackage",
    "RemediationService",
]
