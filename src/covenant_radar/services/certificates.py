"""Application service for the compliance certificate workflow
(`spec §R-09`, `T-038` generation, `T-039` lifecycle).

`generate` (`T-038`) resolves the scoped, still-open testing-calendar
occurrences whose covenant version names a certificate as its evidence,
groups them per `domain.certificates.requirements`, raises one
`certificate_request` row per group (linking every member occurrence's
`covenant_schedule.certificate_id` to it), and separately sweeps
already-open requests whose every covenant has since been retired,
cancelling them with a reason.

`receive`, `accept`, `reject` and `sweep_overdue` (`T-039`) carry a request
through the rest of its life. Receipt and rejection both use
`_queue_recomputation` to open a fresh `due` `covenant_schedule` occurrence
for any covenant version that was already tested before the certificate's
state changed — the same idempotent-by-`(version, due_date)` mechanism
`EngineService._queue_one_retest` (`T-035`) uses, reproduced locally rather
than reused across a service boundary neither task's `Files owned` crosses.
`schedule_due_dates` puts each certificate covenant's testing-calendar due
dates (`domain.covenants.calendar`) on `covenant_schedule`, and `generate`
only ever raises a request for one of those calendar occurrences — a retest
row the engine or a certificate state change opens is never mistaken for a
certificate deadline. `run_cycle` chains the three steps the nightly test
step (`services/nightly.py`) runs every day: schedule, generate, sweep.
A raised request and an overdue one each queue an in-app
`certificate_due` notification to the relationship managers whose
portfolios hold the borrower; nothing is emailed to the borrower contact.

`sweep_overdue` and the overdue-satisfied half of `receive` both drive
`LedgerService.revise` with a synthetic, unpersisted `SignalEventFacts` and
a request-scoped `ContradictionRule` (`domain.signals.CERTIFICATE_OVERDUE_TYPE`
resolved by a local `"certificate_satisfied"` counterpart) — the evidence
item this produces is `T-046`'s ordinary supersession mechanism, never a
bespoke write path, so a satisfied overdue certificate is superseded exactly
like any other evidence item, never deleted.

Every method here never commits — the unit of work that owns the session
does that once the whole use case is complete, the same convention
`services/engine.py` documents.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Final, Protocol, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.audit.events import AuditEventType
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import get_request_id, new_request_id
from covenant_radar.core.errors import AuthorizationError, Conflict, NotFound, ValidationError
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.borrower import Borrower, BorrowerContact
from covenant_radar.db.models.covenant import Covenant, CovenantSchedule, CovenantVersion
from covenant_radar.db.models.document import Document
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.identity import Role, UserPortfolioScope, UserRole
from covenant_radar.db.models.organisation import Organisation
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.signal import CertificateRequest
from covenant_radar.db.repositories.certificate import CertificateRequestRepository
from covenant_radar.db.scoping import Scope, ownership_path_for, resolve_scope
from covenant_radar.db.seed.loader import DEFAULT_DATA_DIR
from covenant_radar.db.session import is_database_session
from covenant_radar.domain.certificates.requirements import (
    CERTIFICATE_TEST_BASIS,
    CertificateRequirement,
    ScheduleCertificateCandidate,
    derive_requirements,
    validate_lead_time_days,
)
from covenant_radar.domain.covenants.calendar import (
    FiscalCalendar,
    ScheduleState,
    generate_schedule_occurrences,
)
from covenant_radar.domain.covenants.cure import FREQUENCY_WINDOW_DAYS
from covenant_radar.domain.covenants.exceptions import DEFAULT_FISCAL_YEAR_START_MONTH
from covenant_radar.domain.signals.evidence import CERTIFICATE_OVERDUE_TYPE, SignalEventFacts
from covenant_radar.domain.signals.supersession import ContradictionRule
from covenant_radar.notifications.templates import CERTIFICATE_DUE_TEMPLATE
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, authorize
from covenant_radar.services.ledger import LedgerService
from covenant_radar.services.notifications import NotificationService

_RELATIONSHIP_MANAGER_ROLE_CODE = "relationship_manager"
_LIVE_STATUS = "live"
_CANCELLED_STATE = "rejected"

#: `signal_event.family`/`evidence_item.family` are a closed, spec-frozen
#: six-value vocabulary (`db/models/signal.py`, `spec §R-10`) that has no
#: member for a compliance/documentation signal. `"payment"` is the value
#: this codebase's own pre-existing fixture already uses for a
#: `CERTIFICATE_OVERDUE_TYPE` item (`tests/unit/test_evidence_model.py::
#: test_certificate_overdue_derives_like_any_family`), and is the nearest of
#: the six in kind — a missing certificate is, like a payment delay, a
#: borrower obligation not met on time.
_CERTIFICATE_EVIDENCE_FAMILY = "payment"

#: The event type a synthetic satisfying event carries — paired with
#: `CERTIFICATE_OVERDUE_TYPE` by `_OVERDUE_SATISFIED_RULE` below. Not a
#: value any real signal source ever produces; it exists only to resolve
#: the overdue evidence item this service itself created.
_CERTIFICATE_SATISFIED_TYPE = "certificate_satisfied"

_OVERDUE_SATISFIED_RULE = ContradictionRule(
    family=_CERTIFICATE_EVIDENCE_FAMILY,
    superseded_event_type=CERTIFICATE_OVERDUE_TYPE,
    superseding_event_type=_CERTIFICATE_SATISFIED_TYPE,
    rule="certificates.overdue_satisfied.v1",
)

#: Certificate-request states a reviewer may still act on (`spec §R-09`
#: `certificate_request.state`); `accepted` and `rejected` are terminal.
_RECEIVABLE_STATES = frozenset({"requested", "received", "under_review", "overdue"})
_ACCEPTABLE_STATES = frozenset({"received", "under_review"})
_REJECTABLE_STATES = frozenset({"requested", "received", "under_review", "overdue", "accepted"})

#: The evidence types this service itself writes to the ledger. They have no
#: `signal_event` rows behind them, so the nightly persistence pass must leave
#: them as this service scored them rather than re-deriving them from nothing.
CERTIFICATE_EVIDENCE_TYPES: Final[frozenset[str]] = frozenset(
    {CERTIFICATE_OVERDUE_TYPE, _CERTIFICATE_SATISFIED_TYPE}
)

#: How far either side of a due date to look for the fiscal boundary it was
#: adjusted from — wider than any weekend-plus-holiday run of non-business days.
_ADJUSTMENT_SLACK = timedelta(days=10)


@lru_cache(maxsize=1)
def _seed_calendar() -> Mapping[str, object]:
    """The weekend/holiday conventions and holidays of `calendar.json`, the
    calendar `db.seed.loader` validates; only its fiscal start month is
    persisted (`organisation.fiscal_year_start_month`)."""
    with (DEFAULT_DATA_DIR / "calendar.json").open(encoding="utf-8") as handle:
        return cast(Mapping[str, object], json.load(handle)["calendar"])


@dataclass(frozen=True, slots=True)
class CertificateCyclePolicy:
    """The configured lead time and grace period one daily cycle runs with."""

    lead_time_days: int
    grace_days: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.lead_time_days, bool)
            or not isinstance(self.lead_time_days, int)
            or self.lead_time_days < 1
        ):
            raise ValueError("lead_time_days must be a positive integer.")
        if (
            isinstance(self.grace_days, bool)
            or not isinstance(self.grace_days, int)
            or self.grace_days < 0
        ):
            raise ValueError("grace_days must be a non-negative integer.")


@dataclass(frozen=True, slots=True)
class CertificateCycleResult:
    """What one `run_cycle` call changed."""

    scheduled: tuple[CovenantSchedule, ...]
    raised: tuple[CertificateRequest, ...]
    cancelled: tuple[CertificateRequest, ...]
    overdue: tuple[CertificateRequest, ...]

    def as_metrics(self) -> dict[str, int]:
        return {
            "certificate_due_dates_scheduled": len(self.scheduled),
            "certificate_requests_raised": len(self.raised),
            "certificate_requests_cancelled": len(self.cancelled),
            "certificate_requests_overdue": len(self.overdue),
        }


class AuditWriter(Protocol):
    """The append-only audit boundary supplied by the caller."""

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        """Append one event in the current transaction."""


@dataclass(frozen=True, slots=True)
class CertificateRequestSummary:
    """One request with the names its screen shows beside it."""

    request: CertificateRequest
    borrower_reference: str
    borrower_name: str
    covenant_references: tuple[str, ...]
    document_filename: str | None


@dataclass(frozen=True, slots=True)
class CertificateGenerationResult:
    """What one `generate` call did — the evidence recorded for `T-038`."""

    raised: tuple[CertificateRequest, ...]
    cancelled: tuple[CertificateRequest, ...]


class CertificateService:
    """Derive, raise and cancel compliance certificate requests."""

    def __init__(
        self,
        session: Session,
        *,
        audit: AuditWriter,
        clock: Clock | None = None,
        request_id: str | None = None,
        scope_resolver: Callable[[Principal], Scope] | None = None,
        ledger: LedgerService | None = None,
    ) -> None:
        if not is_database_session(session):
            raise TypeError("CertificateService requires a SQLAlchemy Session.")
        if audit is None or not callable(getattr(audit, "record", None)):
            raise TypeError("CertificateService requires an append-only audit writer.")
        if scope_resolver is not None and not callable(scope_resolver):
            raise TypeError("CertificateService scope_resolver must be callable.")
        if ledger is not None and not isinstance(ledger, LedgerService):
            raise TypeError("ledger must be a LedgerService.")
        self.session = session
        self.audit = audit
        self.clock = clock or SystemClock()
        self.request_id = request_id or get_request_id() or new_request_id()
        if not isinstance(self.request_id, str) or not 1 <= len(self.request_id) <= 40:
            raise ValueError("Certificate service request_id must be between 1 and 40 characters.")
        self.scope_resolver = scope_resolver
        self.requests = CertificateRequestRepository(session, audit=audit)
        self.ledger = ledger or LedgerService(
            session,
            audit=audit,
            clock=self.clock,
            request_id=self.request_id,
            scope_resolver=scope_resolver,
        )

    def run_cycle(
        self,
        principal: Principal,
        *,
        as_of: date,
        policy: CertificateCyclePolicy,
        scope: Scope | None = None,
    ) -> CertificateCycleResult:
        """One day's pass of the whole workflow, in order: put certificate
        due dates on the calendar, raise the requests whose lead time has
        elapsed, then mark the ones past their grace period overdue.

        Every step is idempotent, so a re-run for the same day changes
        nothing further.
        """
        if not isinstance(policy, CertificateCyclePolicy):
            raise TypeError("run_cycle requires a CertificateCyclePolicy.")
        scheduled = self.schedule_due_dates(
            principal,
            as_of=as_of,
            lead_time_days=policy.lead_time_days,
            grace_days=policy.grace_days,
            scope=scope,
        )
        generated = self.generate(
            principal, as_of=as_of, lead_time_days=policy.lead_time_days, scope=scope
        )
        overdue = self.sweep_overdue(
            principal, as_of=as_of, grace_days=policy.grace_days, scope=scope
        )
        return CertificateCycleResult(
            scheduled=scheduled,
            raised=generated.raised,
            cancelled=generated.cancelled,
            overdue=overdue,
        )

    def schedule_due_dates(
        self,
        principal: Principal,
        *,
        as_of: date,
        lead_time_days: int,
        grace_days: int,
        scope: Scope | None = None,
    ) -> tuple[CovenantSchedule, ...]:
        """Open a `due` `covenant_schedule` row for every testing-calendar
        due date of every live certificate-basis covenant version, from one
        testing interval plus `grace_days` before `as_of` to `lead_time_days`
        after it.

        The look-back lets a first run, or a newly registered covenant,
        still request the certificate for the period that has just closed
        (and find it overdue if it already is) without turning years of
        calendar history into one request each. Idempotent per
        `(covenant_version_id, due_date)` whatever that row's state, so a
        date already tested or marked not-applicable is never reopened.
        """
        principal, resolved_scope = self._authorized_context(
            principal, scope, Permission.VIEW_COVENANT
        )
        validated_as_of = _validate_date(as_of, "as_of")
        if (
            isinstance(lead_time_days, bool)
            or not isinstance(lead_time_days, int)
            or lead_time_days < 1
        ):
            raise ValidationError(
                "lead_time_days must be a positive integer.", field="lead_time_days"
            )
        if isinstance(grace_days, bool) or not isinstance(grace_days, int) or grace_days < 0:
            raise ValidationError("grace_days must be a non-negative integer.", field="grace_days")
        calendar = self._fiscal_calendar()
        horizon_end = validated_as_of + timedelta(days=lead_time_days + 1)
        now = self._now()
        created: list[CovenantSchedule] = []
        for version in self._certificate_versions(resolved_scope):
            window_days = FREQUENCY_WINDOW_DAYS.get(version.frequency)
            if window_days is None:
                continue  # `on_event` has no calendar; it is tested on arrival only
            validate_lead_time_days(version.frequency, lead_time_days)
            start = max(
                version.effective_from,
                validated_as_of - timedelta(days=window_days + grace_days),
            )
            occurrences = generate_schedule_occurrences(
                effective_from=start,
                effective_to=version.effective_to,
                frequency=version.frequency,
                calendar=calendar,
                horizon_end=horizon_end,
            )
            if not occurrences:
                continue
            existing = set(
                self.session.execute(
                    select(CovenantSchedule.due_date).where(
                        CovenantSchedule.covenant_version_id == version.id
                    )
                ).scalars()
            )
            for occurrence in occurrences:
                if occurrence.due_date in existing:
                    continue
                row = CovenantSchedule(
                    id=new_id(),
                    covenant_version_id=version.id,
                    due_date=occurrence.due_date,
                    state=ScheduleState.DUE.value,
                    test_id=None,
                    certificate_id=None,
                    created_at=now,
                    updated_at=now,
                    request_id=self.request_id,
                    created_by_id=principal.id,
                    updated_by_id=principal.id,
                )
                self.session.add(row)
                self.session.flush()
                self.audit.record(
                    AuditEventType.COVENANT_SCHEDULE_GENERATED.value,
                    ("covenant_schedule", row.id),
                    {
                        "covenant_version_id": str(version.id),
                        "due_date": occurrence.due_date.isoformat(),
                        "period_end": occurrence.period_end.isoformat(),
                        "adjustment_reason": occurrence.adjustment_reason,
                        "test_basis": CERTIFICATE_TEST_BASIS,
                    },
                    actor=principal.id,
                    request_id=self.request_id,
                )
                existing.add(occurrence.due_date)
                created.append(row)
        return tuple(created)

    def generate(
        self,
        principal: Principal,
        *,
        as_of: date,
        lead_time_days: int,
        scope: Scope | None = None,
    ) -> CertificateGenerationResult:
        """Raise every certificate request whose lead time has elapsed as
        of `as_of`, and cancel every open request every one of whose
        covenants has since been retired.

        Idempotent per `(borrower, due_date)` group: a repeated call with
        the same arguments raises nothing new, because every candidate
        occurrence it would otherwise group already carries the prior
        call's `certificate_id`. A group that has grown since the prior
        call (one more covenant sharing the same due date) links only its
        new members to the existing request rather than raising a second
        one for the same due date.
        """
        principal, resolved_scope = self._authorized_context(
            principal, scope, Permission.VIEW_COVENANT
        )
        validated_as_of = _validate_date(as_of, "as_of")
        now = self._now()

        candidates = self._due_candidates(resolved_scope)
        requirements = derive_requirements(
            candidates, lead_time_days=lead_time_days, as_of=validated_as_of
        )

        cancelled = self._cancel_retired(resolved_scope, principal, validated_as_of, now)

        raised: list[CertificateRequest] = []
        candidates_by_schedule = {candidate.schedule_id: candidate for candidate in candidates}
        for requirement in requirements:
            request = self._apply_requirement(
                requirement, candidates_by_schedule, principal, resolved_scope, now
            )
            if request is not None:
                raised.append(request)

        return CertificateGenerationResult(raised=tuple(raised), cancelled=cancelled)

    def list_open(
        self, principal: Principal, *, scope: Scope | None = None
    ) -> tuple[CertificateRequest, ...]:
        """Return every scoped, still-open certificate request — the
        certificate screen's (`T-039`) own read path."""
        principal, resolved_scope = self._authorized_context(
            principal, scope, Permission.VIEW_COVENANT
        )
        return tuple(self.requests.open_requests(scope=resolved_scope))

    def for_borrower(
        self, principal: Principal, borrower_id: UUID, *, scope: Scope | None = None
    ) -> tuple[CertificateRequest, ...]:
        """Return every scoped certificate request for one borrower, newest
        due first — including settled ones, for the case-file strip."""
        principal, resolved_scope = self._authorized_context(
            principal, scope, Permission.VIEW_COVENANT
        )
        return tuple(self.requests.for_borrower(borrower_id, scope=resolved_scope))

    def recently_settled(
        self, principal: Principal, *, days: int = 90, scope: Scope | None = None
    ) -> tuple[CertificateRequest, ...]:
        """Return every scoped request accepted or rejected in the last
        `days` days, most recent first — the screen's settled history."""
        principal, resolved_scope = self._authorized_context(
            principal, scope, Permission.VIEW_COVENANT
        )
        since = self._now() - timedelta(days=days)
        return tuple(self.requests.settled_since(since, scope=resolved_scope))

    def summaries(
        self,
        principal: Principal,
        requests: Sequence[CertificateRequest],
        *,
        scope: Scope | None = None,
    ) -> tuple[CertificateRequestSummary, ...]:
        """Describe each request the way its screen names it: the borrower,
        the covenants the certificate evidences and the document received."""
        principal, resolved_scope = self._authorized_context(
            principal, scope, Permission.VIEW_COVENANT
        )
        if not requests:
            return ()
        request_ids = {request.id for request in requests}
        anchors = {request.covenant_schedule_id: request.id for request in requests}
        ownership = ownership_path_for(CovenantSchedule)
        covenant_rows = self.session.execute(
            ownership.apply(
                select(CovenantSchedule.id, CovenantSchedule.certificate_id, Covenant.reference)
            )
            .where(
                resolved_scope.predicate(ownership.path_column),
                (CovenantSchedule.certificate_id.in_(request_ids))
                | (CovenantSchedule.id.in_(anchors.keys())),
            )
            .order_by(Covenant.reference)
        ).all()
        covenants: dict[UUID, list[str]] = {}
        for schedule_id, certificate_id, reference in covenant_rows:
            for owner in {certificate_id, anchors.get(schedule_id)} & request_ids:
                if reference not in covenants.setdefault(owner, []):
                    covenants[owner].append(reference)
        borrowers = {
            borrower_id: (reference, legal_name)
            for borrower_id, reference, legal_name in self.session.execute(
                select(Borrower.id, Borrower.reference, Borrower.legal_name).where(
                    Borrower.id.in_({request.borrower_id for request in requests})
                )
            )
        }
        document_ids = {request.document_id for request in requests if request.document_id}
        filenames: dict[UUID, str] = {}
        if document_ids:
            document_ownership = ownership_path_for(Document)
            filenames = {
                document_id: filename
                for document_id, filename in self.session.execute(
                    document_ownership.apply(select(Document.id, Document.filename)).where(
                        resolved_scope.predicate(document_ownership.path_column),
                        Document.id.in_(document_ids),
                    )
                )
            }
        return tuple(
            CertificateRequestSummary(
                request=request,
                borrower_reference=borrowers.get(request.borrower_id, ("", ""))[0],
                borrower_name=borrowers.get(request.borrower_id, ("", ""))[1],
                covenant_references=tuple(covenants.get(request.id, ())),
                document_filename=(
                    filenames.get(request.document_id) if request.document_id else None
                ),
            )
            for request in requests
        )

    def get(
        self, principal: Principal, request_id: UUID, *, scope: Scope | None = None
    ) -> CertificateRequest:
        """Return one scoped certificate request by id."""
        principal, resolved_scope = self._authorized_context(
            principal, scope, Permission.VIEW_COVENANT
        )
        return self._load_request(request_id, resolved_scope)

    # -- lifecycle: receipt, review and overdue evidence (`T-039`) ----------------

    def receive(
        self,
        principal: Principal,
        request_id: UUID,
        *,
        document_id: UUID,
        scope: Scope | None = None,
    ) -> CertificateRequest:
        """Link an uploaded document to a request, moving it to `received`.

        Any covenant version this request covers that was already tested
        before the certificate arrived is flagged for recomputation — the
        test stood on incomplete evidence and must not be silently left as
        the record of truth. Receiving a certificate that satisfies a
        request already marked `overdue` resolves (supersedes, never
        deletes) the evidence item `sweep_overdue` raised for it.
        """
        principal, resolved_scope = self._authorized_context(
            principal, scope, Permission.UPLOAD_DOCUMENT
        )
        request = self._load_request(request_id, resolved_scope)
        if request.state not in _RECEIVABLE_STATES:
            raise Conflict(
                f"Certificate request {request_id} is {request.state!r} and cannot "
                "receive a document."
            )
        document = self._load_document(document_id, resolved_scope)
        was_overdue = request.state == "overdue"
        now = self._now()
        request.document_id = document.id
        request.state = "received"
        request.received_at = now
        request.updated_at = now
        request.updated_by_id = principal.id
        request.version += 1
        self.audit.record(
            AuditEventType.CERTIFICATE_RECEIVED.value,
            ("certificate_request", request.id),
            {
                "document_id": str(document.id),
                "was_overdue": was_overdue,
            },
            actor=principal.id,
            request_id=self.request_id,
        )
        self._flag_tested_schedules_for_recomputation(request, resolved_scope, principal, now)
        if was_overdue:
            self._resolve_overdue_evidence(request, resolved_scope, principal, now)
        return request

    def accept(
        self,
        principal: Principal,
        request_id: UUID,
        *,
        scope: Scope | None = None,
    ) -> CertificateRequest:
        """Accept a received certificate as valid evidence for its covenants."""
        principal, resolved_scope = self._authorized_context(
            principal, scope, Permission.RECORD_WAIVER
        )
        request = self._load_request(request_id, resolved_scope)
        if request.state not in _ACCEPTABLE_STATES:
            raise Conflict(
                f"Certificate request {request_id} is {request.state!r} and cannot be accepted."
            )
        now = self._now()
        request.state = "accepted"
        request.reviewed_by_id = principal.id
        request.updated_at = now
        request.updated_by_id = principal.id
        request.version += 1
        self.audit.record(
            AuditEventType.CERTIFICATE_ACCEPTED.value,
            ("certificate_request", request.id),
            {},
            actor=principal.id,
            request_id=self.request_id,
        )
        return request

    def reject(
        self,
        principal: Principal,
        request_id: UUID,
        *,
        reason: str,
        scope: Scope | None = None,
    ) -> CertificateRequest:
        """Reject a request with a reason, unlinking every covenant it
        supported and flagging any already-tested one for recomputation.

        Valid from `accepted` as well as every open state: a certificate
        found unsuitable only after acceptance (the wrong period, most
        often) must still be reversible, and both the earlier acceptance
        and this rejection remain in the audit trail — neither is ever
        erased, only superseded by the state that follows it.
        """
        principal, resolved_scope = self._authorized_context(
            principal, scope, Permission.RECORD_WAIVER
        )
        request = self._load_request(request_id, resolved_scope)
        if request.state not in _REJECTABLE_STATES:
            raise Conflict(
                f"Certificate request {request_id} is {request.state!r} and cannot be rejected."
            )
        validated_reason = _validate_reason(reason)
        now = self._now()
        request.state = "rejected"
        request.rejection_reason = validated_reason
        request.reviewed_by_id = principal.id
        request.updated_at = now
        request.updated_by_id = principal.id
        request.version += 1
        self.audit.record(
            AuditEventType.CERTIFICATE_REJECTED.value,
            ("certificate_request", request.id),
            {"reason": validated_reason},
            actor=principal.id,
            request_id=self.request_id,
        )
        self._unlink_and_flag(request, resolved_scope, principal, now)
        return request

    def sweep_overdue(
        self,
        principal: Principal,
        *,
        as_of: date,
        grace_days: int,
        scope: Scope | None = None,
    ) -> tuple[CertificateRequest, ...]:
        """Mark every still-`requested` request past its due date plus grace
        as `overdue`, and raise a `certificate_overdue` evidence item for
        each — the missing-evidence signal `spec §R-09.c` requires.

        A certificate already received is not missing: one still awaiting
        review past its grace period is left for the reviewer, never turned
        into an overdue signal against the borrower.
        """
        principal, resolved_scope = self._authorized_context(
            principal, scope, Permission.INGEST_DATA
        )
        validated_as_of = _validate_date(as_of, "as_of")
        if (
            isinstance(grace_days, bool)
            or not isinstance(grace_days, int)
            or grace_days < 0
        ):
            raise ValidationError("grace_days must be a non-negative integer.", field="grace_days")
        now = self._now()
        became_overdue: list[CertificateRequest] = []
        for request in self.requests.open_requests(scope=resolved_scope):
            if request.state != "requested":
                continue
            cutoff = request.due_date + timedelta(days=grace_days)
            if cutoff >= validated_as_of:
                continue
            request.state = "overdue"
            request.updated_at = now
            request.updated_by_id = principal.id
            request.version += 1
            self.audit.record(
                AuditEventType.CERTIFICATE_REQUEST_OVERDUE.value,
                ("certificate_request", request.id),
                {
                    "borrower_id": str(request.borrower_id),
                    "due_date": request.due_date.isoformat(),
                    "grace_days": grace_days,
                },
                actor=principal.id,
                request_id=self.request_id,
            )
            self._raise_overdue_evidence(request, resolved_scope, principal, now)
            days_late = (validated_as_of - request.due_date).days
            self._notify_relationship_managers(
                request,
                resolved_scope,
                principal,
                summary=(
                    f"The compliance certificate due {request.due_date:%d %b %Y} is "
                    f"{days_late} day{'' if days_late == 1 else 's'} overdue and is now "
                    "evidence on the case file."
                ),
            )
            became_overdue.append(request)
        return tuple(became_overdue)

    # -- lifecycle helpers ----------------------------------------------------------

    def _load_request(self, request_id: UUID, scope: Scope) -> CertificateRequest:
        if not isinstance(request_id, UUID):
            raise ValidationError("request_id must be a UUID.", field="request_id")
        request = self.requests.get(request_id, scope=scope)
        if request is None:
            raise NotFound(
                f"Certificate request {request_id} was not found within the current scope."
            )
        return request

    def _load_document(self, document_id: UUID, scope: Scope) -> Document:
        if not isinstance(document_id, UUID):
            raise ValidationError("document_id must be a UUID.", field="document_id")
        ownership = ownership_path_for(Document)
        statement = ownership.apply(select(Document)).where(
            scope.predicate(ownership.path_column), Document.id == document_id
        )
        document = self.session.execute(statement).scalars().one_or_none()
        if document is None:
            raise NotFound(f"Document {document_id} was not found within the current scope.")
        return cast(Document, document)

    def _unlink_and_flag(
        self,
        request: CertificateRequest,
        scope: Scope,
        principal: Principal,
        now: datetime,
    ) -> None:
        for schedule in self._schedules_for_certificate(request.id, scope):
            schedule.certificate_id = None
            schedule.updated_at = now
            schedule.updated_by_id = principal.id
            schedule.version += 1
            if schedule.test_id is not None:
                self._queue_recomputation(schedule.covenant_version_id, principal, now)

    def _flag_tested_schedules_for_recomputation(
        self,
        request: CertificateRequest,
        scope: Scope,
        principal: Principal,
        now: datetime,
    ) -> None:
        for schedule in self._schedules_for_certificate(request.id, scope):
            if schedule.test_id is not None:
                self._queue_recomputation(schedule.covenant_version_id, principal, now)

    def _schedules_for_certificate(
        self, certificate_id: UUID, scope: Scope
    ) -> tuple[CovenantSchedule, ...]:
        ownership = ownership_path_for(CovenantSchedule)
        statement = ownership.apply(select(CovenantSchedule)).where(
            scope.predicate(ownership.path_column),
            CovenantSchedule.certificate_id == certificate_id,
        )
        return tuple(self.session.execute(statement).scalars().all())

    def _queue_recomputation(
        self, covenant_version_id: UUID, principal: Principal, now: datetime
    ) -> CovenantSchedule:
        """Open a fresh `due` occurrence for `covenant_version_id`, dated
        today, idempotent per `(covenant_version_id, due_date)` — the same
        rule `EngineService._queue_one_retest` (`T-035`) applies, kept local
        to this service rather than reused across a service boundary
        neither `T-035` nor `T-039`'s `Files owned` crosses.
        """
        due_date = now.date()
        existing = self.session.execute(
            select(CovenantSchedule).where(
                CovenantSchedule.covenant_version_id == covenant_version_id,
                CovenantSchedule.due_date == due_date,
                CovenantSchedule.state == ScheduleState.DUE.value,
            )
        ).scalars().one_or_none()
        if existing is not None:
            return existing
        row = CovenantSchedule(
            id=new_id(),
            covenant_version_id=covenant_version_id,
            due_date=due_date,
            state=ScheduleState.DUE.value,
            test_id=None,
            certificate_id=None,
            created_at=now,
            updated_at=now,
            request_id=self.request_id,
            created_by_id=principal.id,
            updated_by_id=principal.id,
        )
        self.session.add(row)
        self.session.flush()
        self.audit.record(
            AuditEventType.COVENANT_RETEST_QUEUED.value,
            ("covenant_schedule", row.id),
            {
                "covenant_version_id": str(covenant_version_id),
                "due_date": due_date.isoformat(),
                "trigger_kind": "certificate",
            },
            actor=principal.id,
            request_id=self.request_id,
        )
        return row

    def _raise_overdue_evidence(
        self,
        request: CertificateRequest,
        scope: Scope,
        principal: Principal,
        now: datetime,
    ) -> None:
        event = SignalEventFacts(
            borrower_id=request.borrower_id,
            facility_id=None,
            event_date=now.date(),
            family=_CERTIFICATE_EVIDENCE_FAMILY,
            event_type=CERTIFICATE_OVERDUE_TYPE,
            evidence_type=CERTIFICATE_OVERDUE_TYPE,
            payload={
                "certificate_request_id": str(request.id),
                "due_date": request.due_date.isoformat(),
            },
            event_id=f"certificate-overdue-{request.id}",
        )
        revision = self.ledger.revise(
            self._augmented(principal, Permission.INGEST_DATA),
            request.borrower_id,
            [event],
            as_of=now.date(),
            scope=scope,
            rules=(_OVERDUE_SATISFIED_RULE,),
            request_id=self.request_id,
        )
        self.audit.record(
            AuditEventType.CERTIFICATE_OVERDUE_EVIDENCE_CREATED.value,
            ("certificate_request", request.id),
            {
                "borrower_id": str(request.borrower_id),
                "evidence_item_ids": [str(item.id) for item in revision.items if item.id],
            },
            actor=principal.id,
            request_id=self.request_id,
        )

    def _resolve_overdue_evidence(
        self,
        request: CertificateRequest,
        scope: Scope,
        principal: Principal,
        now: datetime,
    ) -> None:
        event = SignalEventFacts(
            borrower_id=request.borrower_id,
            facility_id=None,
            event_date=now.date(),
            family=_CERTIFICATE_EVIDENCE_FAMILY,
            event_type=_CERTIFICATE_SATISFIED_TYPE,
            evidence_type=_CERTIFICATE_SATISFIED_TYPE,
            payload={"certificate_request_id": str(request.id)},
            event_id=f"certificate-satisfied-{request.id}-{now.isoformat()}",
        )
        self.ledger.revise(
            self._augmented(principal, Permission.INGEST_DATA),
            request.borrower_id,
            [event],
            as_of=now.date(),
            scope=scope,
            rules=(_OVERDUE_SATISFIED_RULE,),
            request_id=self.request_id,
        )

    def _augmented(self, principal: Principal, permission: Permission) -> Principal:
        """Widen `principal`'s permission set for one internal collaborator
        call, keeping its own id as the recorded actor.

        Receiving a certificate is itself the authorized action; resolving
        the overdue evidence item that receipt satisfies is that action's
        own documented consequence (`spec §R-09.c`/`R-09.d`), not a
        separate one a reviewer must be independently permissioned for —
        the same reasoning `EngineService.test` relies on when it derives a
        borrower's SMA band as part of one authorized test.
        """
        if principal.has(permission):
            return principal
        return Principal(
            id=principal.id, permissions=principal.permissions | {permission}, kind=principal.kind
        )

    # -- requirement application -------------------------------------------------

    def _apply_requirement(
        self,
        requirement: CertificateRequirement,
        candidates_by_schedule: Mapping[UUID, ScheduleCertificateCandidate],
        principal: Principal,
        scope: Scope,
        now: datetime,
    ) -> CertificateRequest | None:
        existing_id = self._existing_request_id(requirement, candidates_by_schedule)
        schedule_rows = self._load_schedules(requirement.covenant_schedule_ids, scope)

        if existing_id is not None:
            self._link_unlinked(schedule_rows, existing_id, principal, now)
            return None

        request = CertificateRequest(
            id=new_id(),
            covenant_schedule_id=requirement.anchor_schedule_id,
            borrower_id=requirement.borrower_id,
            due_date=requirement.due_date,
            state="requested",
            requested_at=now,
            created_at=now,
            updated_at=now,
            request_id=self.request_id,
            created_by_id=principal.id,
            updated_by_id=principal.id,
        )
        self.requests.add(request)
        self.session.flush()
        self._link_unlinked(schedule_rows, request.id, principal, now)

        contact = self._primary_contact(requirement.borrower_id, scope)
        # The relationship managers are told either way: a contact on file is
        # who the certificate is requested from, not who chases it.
        notified_user_ids = self._relationship_manager_ids(requirement.borrower_id, scope)
        self.audit.record(
            AuditEventType.CERTIFICATE_REQUEST_RAISED.value,
            ("certificate_request", request.id),
            {
                "borrower_id": str(requirement.borrower_id),
                "due_date": requirement.due_date.isoformat(),
                "covenant_schedule_ids": [
                    str(schedule_id) for schedule_id in requirement.covenant_schedule_ids
                ],
                "covenant_count": len(requirement.covenant_schedule_ids),
                "notification_target": (
                    "borrower_contact" if contact is not None else _RELATIONSHIP_MANAGER_ROLE_CODE
                ),
                "contact_id": str(contact.id) if contact is not None else None,
                "notified_user_ids": [str(user_id) for user_id in notified_user_ids],
            },
            actor=principal.id,
            request_id=self.request_id,
        )
        covenant_count = len(requirement.covenant_schedule_ids)
        self._notify_relationship_managers(
            request,
            scope,
            principal,
            summary=(
                f"A compliance certificate covering {covenant_count} "
                f"covenant{'' if covenant_count == 1 else 's'} is due "
                f"{requirement.due_date:%d %b %Y}; the request has been raised."
            ),
            recipient_ids=notified_user_ids,
        )
        return request

    def _notify_relationship_managers(
        self,
        request: CertificateRequest,
        scope: Scope,
        principal: Principal,
        *,
        summary: str,
        recipient_ids: Sequence[UUID] | None = None,
    ) -> None:
        """Queue one in-app `certificate_due` notice to every relationship
        manager whose portfolio holds the request's borrower.

        `NotificationService` re-checks each recipient's own scope and
        preferences; a borrower nobody covers is simply not announced.
        """
        recipients = (
            recipient_ids
            if recipient_ids is not None
            else self._relationship_manager_ids(request.borrower_id, scope)
        )
        if not recipients:
            return
        reference = self.session.scalar(
            select(Borrower.reference).where(Borrower.id == request.borrower_id)
        )
        NotificationService(
            self.session,
            audit=self.audit,
            clock=self.clock,
            request_id=self.request_id,
        ).queue(
            CERTIFICATE_DUE_TEMPLATE,
            {"borrower_reference": reference or str(request.borrower_id), "summary": summary},
            recipient_ids=list(recipients),
            subject_type="borrower",
            subject_id=request.borrower_id,
            actor_id=principal.id,
        )

    def _existing_request_id(
        self,
        requirement: CertificateRequirement,
        candidates_by_schedule: Mapping[UUID, ScheduleCertificateCandidate],
    ) -> UUID | None:
        for schedule_id in requirement.covenant_schedule_ids:
            candidate = candidates_by_schedule.get(schedule_id)
            if candidate is not None and candidate.existing_certificate_id is not None:
                return candidate.existing_certificate_id
        return None

    def _link_unlinked(
        self,
        schedule_rows: Sequence[CovenantSchedule],
        certificate_id: UUID,
        principal: Principal,
        now: datetime,
    ) -> None:
        for schedule in schedule_rows:
            if schedule.certificate_id is not None:
                continue
            schedule.certificate_id = certificate_id
            schedule.updated_at = now
            schedule.updated_by_id = principal.id
            schedule.version += 1

    def _load_schedules(
        self, schedule_ids: Sequence[UUID], scope: Scope
    ) -> tuple[CovenantSchedule, ...]:
        ownership = ownership_path_for(CovenantSchedule)
        statement = ownership.apply(select(CovenantSchedule)).where(
            scope.predicate(ownership.path_column),
            CovenantSchedule.id.in_(schedule_ids),
        )
        return tuple(self.session.execute(statement).scalars().all())

    # -- candidate derivation -----------------------------------------------------

    def _due_candidates(self, scope: Scope) -> tuple[ScheduleCertificateCandidate, ...]:
        ownership = ownership_path_for(CovenantSchedule)
        statement = ownership.apply(
            select(
                CovenantSchedule.id,
                CovenantSchedule.covenant_version_id,
                CovenantSchedule.due_date,
                CovenantSchedule.certificate_id,
                CovenantVersion.frequency,
                CovenantVersion.test_basis,
                CovenantVersion.effective_from,
                CovenantVersion.effective_to,
                Facility.borrower_id,
            )
        ).where(
            scope.predicate(ownership.path_column),
            CovenantSchedule.state == ScheduleState.DUE.value,
            CovenantVersion.status == _LIVE_STATUS,
            CovenantVersion.test_basis == CERTIFICATE_TEST_BASIS,
        )
        calendar = self._fiscal_calendar()
        # A certificate is owed only on the covenant's testing calendar. The
        # same version also gets `due` rows dated whenever a retest is queued
        # (a statement upload, a certificate's own receipt or rejection);
        # those are not deadlines and must never raise a request of their own.
        rows = [
            row
            for row in self.session.execute(statement).all()
            if _on_calendar(
                row.frequency, row.effective_from, row.effective_to, row.due_date, calendar
            )
        ]
        return tuple(
            ScheduleCertificateCandidate(
                schedule_id=row.id,
                covenant_version_id=row.covenant_version_id,
                borrower_id=row.borrower_id,
                due_date=row.due_date,
                frequency=row.frequency,
                test_basis=row.test_basis,
                existing_certificate_id=row.certificate_id,
            )
            for row in rows
        )

    # -- retirement sweep ----------------------------------------------------------

    def _cancel_retired(
        self,
        scope: Scope,
        principal: Principal,
        as_of: date,
        now: datetime,
    ) -> tuple[CertificateRequest, ...]:
        cancelled: list[CertificateRequest] = []
        for request in self.requests.open_requests(scope=scope):
            if request.due_date < as_of:
                # Past its due date is the overdue sweep's concern (`T-039`),
                # never this one's.
                continue
            version_statuses = self._linked_version_statuses(request.id, scope)
            if not version_statuses or _LIVE_STATUS in version_statuses:
                continue
            request.state = _CANCELLED_STATE
            request.rejection_reason = (
                "Cancelled: every covenant this certificate request supported was "
                "retired before the request's due date."
            )
            request.updated_at = now
            request.updated_by_id = principal.id
            request.version += 1
            self.audit.record(
                AuditEventType.CERTIFICATE_REQUEST_CANCELLED.value,
                ("certificate_request", request.id),
                {
                    "borrower_id": str(request.borrower_id),
                    "due_date": request.due_date.isoformat(),
                    "reason": request.rejection_reason,
                },
                actor=principal.id,
                request_id=self.request_id,
            )
            cancelled.append(request)
        return tuple(cancelled)

    def _linked_version_statuses(self, certificate_id: UUID, scope: Scope) -> frozenset[str]:
        schedule_ownership = ownership_path_for(CovenantSchedule)
        statement = schedule_ownership.apply(
            select(CovenantVersion.status).select_from(CovenantSchedule)
        ).where(
            scope.predicate(schedule_ownership.path_column),
            CovenantSchedule.certificate_id == certificate_id,
        )
        return frozenset(self.session.execute(statement).scalars().all())

    def _certificate_versions(self, scope: Scope) -> tuple[CovenantVersion, ...]:
        ownership = ownership_path_for(CovenantVersion)
        statement = (
            ownership.apply(select(CovenantVersion))
            .where(
                scope.predicate(ownership.path_column),
                CovenantVersion.status == _LIVE_STATUS,
                CovenantVersion.test_basis == CERTIFICATE_TEST_BASIS,
            )
            .order_by(CovenantVersion.id)
        )
        return tuple(self.session.execute(statement).scalars().all())

    def _fiscal_calendar(self) -> FiscalCalendar:
        """The bank's calendar: its own fiscal start month, with the seed
        calendar's weekend and holiday conventions."""
        month = self.session.scalar(select(Organisation.fiscal_year_start_month).limit(1))
        return FiscalCalendar.from_mapping(
            {
                **_seed_calendar(),
                "fiscal_year_start_month": (
                    month if isinstance(month, int) else DEFAULT_FISCAL_YEAR_START_MONTH
                ),
            }
        )

    # -- contact / relationship-manager resolution ---------------------------------

    def _primary_contact(self, borrower_id: UUID, scope: Scope) -> BorrowerContact | None:
        ownership = ownership_path_for(BorrowerContact)
        statement = (
            ownership.apply(select(BorrowerContact))
            .where(
                scope.predicate(ownership.path_column),
                BorrowerContact.borrower_id == borrower_id,
            )
            .order_by(BorrowerContact.is_primary.desc(), BorrowerContact.created_at)
            .limit(1)
        )
        return self.session.execute(statement).scalars().one_or_none()

    def _relationship_manager_ids(self, borrower_id: UUID, scope: Scope) -> tuple[UUID, ...]:
        """Every user holding the relationship-manager role whose own
        portfolio scope covers this borrower — the fallback notification
        target `spec §R-09.a` requires when no contact is on file.

        Read directly rather than through `Scope`, because the question
        here is the inverse of what `Scope.predicate` answers: not "is this
        row inside my scope" but "whose scope covers this one, fixed,
        borrower".
        """
        borrower_ownership = ownership_path_for(Borrower)
        borrower = self.session.execute(
            borrower_ownership.apply(select(Borrower.id, Portfolio.path)).where(
                scope.predicate(borrower_ownership.path_column), Borrower.id == borrower_id
            )
        ).first()
        if borrower is None:
            return ()
        borrower_path = borrower[1]
        rows = self.session.execute(
            select(UserRole.user_id, Portfolio.path, UserPortfolioScope.include_descendants)
            .join(Role, Role.id == UserRole.role_id)
            .join(UserPortfolioScope, UserPortfolioScope.user_id == UserRole.user_id)
            .join(Portfolio, Portfolio.id == UserPortfolioScope.portfolio_id)
            .where(Role.code == _RELATIONSHIP_MANAGER_ROLE_CODE)
        ).all()
        matched: set[UUID] = set()
        for user_id, scope_path, include_descendants in rows:
            if include_descendants:
                if borrower_path.startswith(scope_path):
                    matched.add(user_id)
            elif scope_path == borrower_path:
                matched.add(user_id)
        return tuple(sorted(matched, key=lambda value: value.bytes))

    # -- shared context --------------------------------------------------------------

    def _authorized_context(
        self, principal: Principal, scope: Scope | None, permission: Permission
    ) -> tuple[Principal, Scope]:
        if not isinstance(principal, Principal):
            raise AuthorizationError("An authenticated principal is required.")
        authorize(principal, permission)
        if scope is None:
            resolved = (
                self.scope_resolver(principal)
                if self.scope_resolver is not None
                else resolve_scope(principal, self.session)
            )
        else:
            resolved = scope
        if not isinstance(resolved, Scope) or resolved.principal_id != principal.id:
            raise AuthorizationError(
                "The resolved scope does not belong to the authenticated principal."
            )
        return principal, resolved

    def _now(self) -> datetime:
        now = self.clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("Certificate service clock must return an aware datetime.")
        return now.astimezone(UTC)


def _on_calendar(
    frequency: str,
    effective_from: date,
    effective_to: date | None,
    due_date: date,
    calendar: FiscalCalendar,
) -> bool:
    """Whether `due_date` is one of the version's own testing-calendar due
    dates (after weekend and holiday adjustment)."""
    if frequency not in FREQUENCY_WINDOW_DAYS:
        return False
    occurrences = generate_schedule_occurrences(
        effective_from=max(effective_from, due_date - _ADJUSTMENT_SLACK),
        effective_to=effective_to,
        frequency=frequency,
        calendar=calendar,
        horizon_end=due_date + _ADJUSTMENT_SLACK,
    )
    return any(occurrence.due_date == due_date for occurrence in occurrences)


def _validate_date(value: object, name: str) -> date:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValidationError(f"{name} must be a calendar date.", field=name)
    return value


_REASON_MAX_LENGTH = 2000


def _validate_reason(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("A rejection reason is required.", field="reason")
    cleaned = value.strip()
    if len(cleaned) > _REASON_MAX_LENGTH:
        raise ValidationError(
            f"A rejection reason must be at most {_REASON_MAX_LENGTH} characters.", field="reason"
        )
    return cleaned


__all__ = [
    "CERTIFICATE_EVIDENCE_TYPES",
    "AuditWriter",
    "CertificateCyclePolicy",
    "CertificateCycleResult",
    "CertificateGenerationResult",
    "CertificateRequestSummary",
    "CertificateService",
]
