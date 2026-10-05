"""The nightly pipeline's step handlers (`T-121`, `spec §R-28.a`/`R-28.c`)
and its partial-failure policy (`T-122`, `spec §R-28.b`/`R-28.d`).

`NightlyPipelineService` supplies the six `JobHandler`s
`scheduler.pipeline.register_nightly_pipeline` registers under
`nightly.ingest`, `nightly.test`, `nightly.score`, `nightly.rank`,
`nightly.update_cases` and `nightly.dispatch`. Each handler opens and commits
its own session — a `JobHandler` receives only a `JobRunContext`, no session,
matching every other job this codebase runs (`scheduler.runner.JobRunner`).

**Partial failure (`T-122`).** Every step that loops over borrowers or
covenants isolates each item in its own `Session.begin_nested()` savepoint
(`scheduler.policy.IsolationTracker`): one bad borrower is recorded and
skipped, never lets its exception halt the rest of the book, and every
isolated failure stays visible in that step's own `JobRun.metrics` rather
than being silently folded into a clean-looking success count. Two further
capabilities, `check_deadline` (T12) and `check_recurring_failure`, are not
steps themselves — they read the job ledger's own history and, through the
same `AuditRecorder` boundary every other write in this module already uses,
raise a durable, idempotent-per-run record of a missed deadline or an
unresolved recurring failure. `T-145` is the deferred task that turns that
record into an actual paged alert; this module's job stops at raising it.

**Idempotency by pipeline run id, without a new table.** A pipeline run id
(`JobRunContext.run_id`) is a string shared by every step's own `job_run`
row for that run — `scheduler.ledger.JobLedger` already gives each attempt its
own row while keeping `run_id` stable across retries. `nightly.score` and
`nightly.rank` need a stable link from that string to the `ForecastRun` UUID
their domain tables use; rather than mint one (which `ForecastScoringService`
has no way to accept — it always creates a run with a fresh id, only ever
*resuming* one given an existing id), this module discovers it by joining
`forecast_run.job_run_id` back to the `job_run` rows already recorded for
`(run_id, "nightly.score")`. The first attempt finds none and creates a run;
every later attempt (a retry, or the same run triggered twice) finds the one
already there and resumes or reads it — which is also how a threshold
snapshot changing mid-run stays pinned: the snapshot id embedded in that first
`ForecastRun` row is reused verbatim by every later attempt, never
re-resolved against whatever is active by then.

**Single-borrower runs.** `borrower_id` on `JobRunContext` scopes the
work to one borrower: `nightly.test` tests only their covenants,
`nightly.score` forecasts only them, and `nightly.update_cases`/
`nightly.dispatch` act only on them.  `nightly.rank` re-ranks the whole book —
that borrower from this run, everyone else carried forward from the queue it
replaces — because the newest ranked run is what the queue serves, and a
one-borrower queue would hide the rest of the book.  The queue resolves a
carried row's forecast details from the earlier run that produced them.

**No connector yet.** `signal_source` and `statement_lines` are the seams
`nightly.ingest` and `nightly.test` use to reach real source data; both
default to "nothing available" because `T-123`'s connector framework and the
statement-reconstruction pipeline are out of this window's scope. That is an
honest, observable outcome (zero events ingested; a covenant left untested
with a recorded reason) rather than a fabricated one, and needs no change
here once a real provider exists — only a constructor argument.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Final, Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from covenant_radar.audit.record import AuditRecorder, AuditSubject
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import new_request_id
from covenant_radar.core.errors import NotFound
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.audit import AuditEvent
from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.covenant import (
    Covenant,
    CovenantException,
    CovenantSchedule,
    CovenantTest,
    CovenantVersion,
)
from covenant_radar.db.models.facility import Facility, FacilityConduct
from covenant_radar.db.models.forecast import Forecast, ForecastRun
from covenant_radar.db.models.forecast import TriageEntry as TriageEntryModel
from covenant_radar.db.models.identity import AppUser, Role, UserRole
from covenant_radar.db.models.operations import JobRun
from covenant_radar.db.models.organisation import Organisation
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.signal import (
    EvidenceItem,
    EvidenceTransition,
)
from covenant_radar.db.models.signal import (
    SignalEvent as SignalEventModel,
)
from covenant_radar.db.models.statements import FinancialPeriod
from covenant_radar.db.models.workflow import Case, Notification
from covenant_radar.db.repositories.audit import SqlAlchemyAuditStore
from covenant_radar.db.repositories.evidence import EvidenceRepository
from covenant_radar.db.repositories.forecast import COMPLETE as COMPLETE_FORECAST_RUN_STATE
from covenant_radar.db.repositories.trace import TraceRepository
from covenant_radar.db.repositories.triage import TriageRepository
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.db.session import SessionFactory
from covenant_radar.domain.cases.sla import derive_sla
from covenant_radar.domain.certificates.requirements import CERTIFICATE_TEST_BASIS
from covenant_radar.domain.covenants.calendar import ScheduleState
from covenant_radar.domain.covenants.cure import FREQUENCY_WINDOW_DAYS
from covenant_radar.domain.covenants.evaluate import PeriodFacts
from covenant_radar.domain.covenants.exceptions import (
    DEFAULT_FISCAL_YEAR_START_MONTH,
    normalise_period,
    period_bounds_for_label,
    period_label_for_date,
)
from covenant_radar.domain.covenants.sma import derive_borrower_sma
from covenant_radar.domain.forecast import (
    Observation,
    ThresholdChange,
    Weights,
    evidence_pressure,
)
from covenant_radar.domain.forecast.predictor import ForecastPredictor
from covenant_radar.domain.signals import SignalEvent
from covenant_radar.domain.signals.decay import DecayThresholds
from covenant_radar.domain.signals.decay import decay_factor as signal_decay_factor
from covenant_radar.domain.signals.evidence import EvidenceFacts
from covenant_radar.domain.signals.persistence import PersistenceThresholds
from covenant_radar.domain.triage.banding import ACT_BAND, TriageThresholds
from covenant_radar.domain.triage.changes import ChangeThresholds, ChangeType
from covenant_radar.domain.triage.urgency import ForecastFact, TriageInput
from covenant_radar.domain.triage.urgency import rank as rank_triage_entries
from covenant_radar.notifications.templates import (
    BAND_CHANGE_TEMPLATE,
    JOB_FAILURE_TEMPLATE,
    MORNING_QUEUE_TEMPLATE,
)
from covenant_radar.ports.notifier import NotificationChannel
from covenant_radar.scheduler.jobs import JobHandler, JobRunContext
from covenant_radar.scheduler.ledger import FAILED, SUCCEEDED
from covenant_radar.scheduler.pipeline import (
    PIPELINE_STEPS,
    STEP_DISPATCH,
    STEP_INGEST,
    STEP_RANK,
    STEP_SCORE,
    STEP_TEST,
    STEP_UPDATE_CASES,
    PipelineRunResult,
)
from covenant_radar.scheduler.policy import (
    DEFAULT_RECURRING_FAILURE_THRESHOLD,
    IsolationTracker,
    PipelineRunStatus,
    evaluate_deadline,
    is_recurring_failure,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal, PrincipalKind
from covenant_radar.services.cases import CaseService
from covenant_radar.services.certificates import (
    CERTIFICATE_EVIDENCE_TYPES,
    CertificateCyclePolicy,
    CertificateService,
)
from covenant_radar.services.engine import EngineService, reads_facility_conduct
from covenant_radar.services.ingestion import SignalIngestionService
from covenant_radar.services.ledger import LedgerService, _stage3_trace
from covenant_radar.services.notifications import NotificationService
from covenant_radar.services.scoring import (
    PREDICTOR_MODES,
    SHADOW_PREDICTOR_MODE,
    ForecastCandidate,
    ForecastScoringService,
)
from covenant_radar.services.triage import TriageService


@runtime_checkable
class ThresholdSnapshotProvider(Protocol):
    """The adapter-neutral threshold surface this service needs.

    `config.thresholds.ThresholdStore` satisfies this directly. Accepting the
    structural shape rather than that concrete class matches how
    `domain.triage.banding.TriageThresholds.from_store` and
    `services.scoring.ForecastScoringService` already treat threshold access
    everywhere else in the codebase — and lets a test double stand in without
    a database-backed `ThresholdRepository`, which nothing in this codebase
    implements yet.
    """

    def get(self, name: str) -> Mapping[str, object]:
        """Return one named threshold's fields (e.g. `"T1"` -> act/amber)."""

    def snapshot_id(self) -> UUID:
        """Return the snapshot id that must be stamped on a decision."""


class _AuditWriterAdapter:
    """Narrows `AuditRecorder.record`'s wider signature to the exact
    `AuditWriter` Protocol each of `EngineService`, `SignalIngestionService`
    and `ForecastScoringService` separately declares (`subject: object`,
    `request_id: str` with no default) — `AuditRecorder` is documented as
    "the single application-facing audit write boundary" but is a wider,
    optional-`request_id` signature than any of those three narrower
    protocols, so nothing else in this codebase actually hands one to any of
    them today. This adapter is that boundary.
    """

    __slots__ = ("_recorder",)

    def __init__(self, recorder: AuditRecorder) -> None:
        self._recorder = recorder

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        # Every caller in this module passes the (subject_type, subject_id)
        # tuple shape `AuditRecorder.record` actually requires; `object` here
        # only widens this adapter's own declared Protocol to match callers'.
        return self._recorder.record(
            event_type,
            subject,  # type: ignore[arg-type]
            payload,
            actor=actor,
            request_id=request_id,
        )


#: A source of raw signal events for `nightly.ingest`; `None` (the default)
#: means no connector is configured yet, so the step honestly ingests nothing.
SignalSourceProvider = Callable[[], Iterable[SignalEvent | Mapping[str, object]]]


@dataclass(frozen=True, slots=True)
class StatementSnapshot:
    """The latest complete statement period behind one covenant, with its lines.

    Carrying the period is what lets the test step test each statement once,
    apply the exception written for that period, date the observation at the
    period end and notice when the next statement is overdue.
    """

    lines: Mapping[str, Decimal]
    period_id: UUID
    period_label: str
    period_end: date


#: Resolves the statement behind one covenant version as of one date.  A
#: `StatementSnapshot` names its period; a bare mapping of lines is treated as
#: data current on the run date (a source that does not know its period).
#: `None` means "no statement data available for this covenant yet" — the
#: step leaves it untested rather than inventing a result.
StatementLinesProvider = Callable[
    [CovenantVersion, date], StatementSnapshot | Mapping[str, Decimal] | None
]

_MODEL_VERSION: Final[str] = "nightly.pipeline.v1"
# Must name a template the in-app registry knows
# (`notifications.templates`), or the notification centre cannot render the
# row and falls back to "no longer available in your access scope".  An
# act-band case *is* a band change, which is the registered template for it.
_ACT_ALERT_TEMPLATE: Final[str] = BAND_CHANGE_TEMPLATE.name
# The act alert is a workspace notice for the case assignee, so it is written
# to the always-available in-app channel that the notification centre reads
# (`notifications.inapp._IN_APP_CHANNELS`).  Writing it to "email" made it
# invisible in the app *and* undeliverable wherever SMTP is unconfigured,
# leaving every act-band alert stranded as a `pending` row nobody ever saw.
_NOTIFICATION_CHANNEL: Final[str] = NotificationChannel.IN_APP.value
_BREACH_VERDICTS: Final[frozenset[str]] = frozenset({"breach", "breach_cure_open"})
_TEST_HISTORY_LIMIT: Final[int] = 366
#: Filing time allowed after a period's successor ends before its statement
#: is overdue.  The runtime passes `forecast.statement_grace_days`.
_DEFAULT_STATEMENT_GRACE_DAYS: Final[int] = 60
#: The runtime passes `forecast.pressure_rate`; see `pressure_scale` below.
_DEFAULT_PRESSURE_RATE: Final[Decimal] = Decimal("0.25")
#: The runtime passes `forecast.distance_scale`; see `probability_inputs`.
_DEFAULT_DISTANCE_SCALE: Final[Decimal] = Decimal("2")
#: Two statements of the same value closer together than this cannot be two
#: periods (the shortest reporting period is monthly), so the later one is a
#: nightly copy of the first.
_MIN_STATEMENT_GAP_DAYS: Final[int] = 28
#: Reporting period assumed for an `on_event` covenant's pressure scale.
_DEFAULT_PERIOD_DAYS: Final[int] = 90
_VALUED_VERDICTS: Final[frozenset[str]] = frozenset(
    {"pass", "warning", "breach", "breach_cure_open"}
)
#: `Facility` money columns are denominated in ₹ crore; `TriageEntry.exposure`
#: is in rupees.  See `_borrower_exposure`.
_RUPEES_PER_CRORE: Final[Decimal] = Decimal("10000000")

#: T12 (`spec §17.5`): the audit event `check_deadline` raises when a
#: pipeline run is still open past its deadline. `T-145` is the deferred
#: task that turns this durable, replayable record into an actual paged
#: alert; this module's job stops at recording the fact, once, per run.
_DEADLINE_ALERT_EVENT: Final[str] = "nightly.deadline_alert_raised"
_DEADLINE_ALERT_SUBJECT_TYPE: Final[str] = "pipeline_run"

#: The audit event `check_recurring_failure` raises when one step has
#: failed with the same normalized cause on `threshold` consecutive
#: pipeline runs — "a nightly failure that alerts identically every night
#: stops being read" (`spec §R-28.b`).
_RECURRING_FAILURE_EVENT: Final[str] = "nightly.step_failure_escalated"
_RECURRING_FAILURE_SUBJECT_TYPE: Final[str] = "job_step_run"

#: How many of a job's most recent, run-distinct terminal outcomes
#: `check_recurring_failure` reads before deciding — always at least
#: `policy.DEFAULT_RECURRING_FAILURE_THRESHOLD`.
_RECURRING_FAILURE_LOOKBACK: Final[int] = 10


class NightlyPipelineService:
    """The six step handlers behind the nightly pipeline's job names, plus
    two standing capabilities the batch's partial-failure policy needs:
    `check_deadline` (T12) and `check_recurring_failure` (`spec §R-28.b`).
    Neither is one of the six pipeline steps — both are meant to be called
    by whatever schedules them (`T-145`'s deferred alert wiring, or an
    operator/test calling directly), against a run or job that has already
    been observed to be open or failing.
    """

    def __init__(
        self,
        session_factory: SessionFactory,
        *,
        threshold_store: ThresholdSnapshotProvider,
        horizons: Sequence[int],
        weights: Weights,
        system_actor_id: UUID,
        clock: Clock | None = None,
        signal_source: SignalSourceProvider | None = None,
        statement_lines: StatementLinesProvider | None = None,
        default_assignee_id: UUID | None = None,
        predictor: ForecastPredictor | None = None,
        predictor_mode: str = SHADOW_PREDICTOR_MODE,
        model_version: str = _MODEL_VERSION,
        what_changed_thresholds: ChangeThresholds | None = None,
        statement_grace_days: int = _DEFAULT_STATEMENT_GRACE_DAYS,
        pressure_rate: Decimal = _DEFAULT_PRESSURE_RATE,
        distance_scale: Decimal = _DEFAULT_DISTANCE_SCALE,
        certificate_policy: CertificateCyclePolicy | None = None,
    ) -> None:
        if not callable(session_factory):
            raise TypeError("NightlyPipelineService requires a callable session_factory.")
        if not isinstance(threshold_store, ThresholdSnapshotProvider):
            raise TypeError("NightlyPipelineService requires a ThresholdSnapshotProvider.")
        if not isinstance(weights, Weights):
            raise TypeError("NightlyPipelineService requires forecast probability Weights.")
        normalised_horizons = tuple(sorted({int(value) for value in horizons}))
        if not normalised_horizons:
            raise ValueError("horizons must be a non-empty sequence of non-negative integers.")
        if any(value < 0 for value in normalised_horizons):
            raise ValueError("horizons must not contain negative values.")
        if not isinstance(system_actor_id, UUID):
            raise TypeError("system_actor_id must be a UUID.")
        if predictor_mode not in PREDICTOR_MODES:
            raise ValueError(
                f"predictor_mode must be one of {sorted(PREDICTOR_MODES)}, not {predictor_mode!r}."
            )
        self.session_factory = session_factory
        self.threshold_store = threshold_store
        self.horizons = normalised_horizons
        self.weights = weights
        self.system_actor_id = system_actor_id
        self.clock = clock or SystemClock()
        self.signal_source = signal_source
        self.statement_lines = statement_lines
        self.default_assignee_id = default_assignee_id
        self.predictor = predictor
        self.predictor_mode = predictor_mode
        self.model_version = model_version
        #: `spec §R-14.d`'s what-changed policy.  The runtime always supplies
        #: it from settings; a caller that omits it ranks without summaries.
        self.what_changed_thresholds = what_changed_thresholds
        if isinstance(statement_grace_days, bool) or statement_grace_days < 0:
            raise ValueError("statement_grace_days must be a non-negative integer.")
        #: Days after a period's expected successor before a statement counts
        #: as overdue (filing time for the next period's statement).
        self.statement_grace_days = int(statement_grace_days)
        if (
            not isinstance(pressure_rate, Decimal)
            or not pressure_rate.is_finite()
            or pressure_rate < 0
        ):
            raise ValueError("pressure_rate must be a non-negative Decimal.")
        #: Fraction of a covenant's threshold that sustained evidence at full
        #: materiality moves it over one reporting period.
        self.pressure_rate = pressure_rate
        if not isinstance(distance_scale, Decimal) or not distance_scale > 0:
            raise ValueError("distance_scale must be a positive Decimal.")
        self.distance_scale = distance_scale
        if certificate_policy is not None and not isinstance(
            certificate_policy, CertificateCyclePolicy
        ):
            raise TypeError("certificate_policy must be a CertificateCyclePolicy.")
        #: `spec §R-09`'s daily certificate cycle, run by `nightly.test`;
        #: `None` leaves the certificate workflow to be driven by hand.
        self.certificate_policy = certificate_policy

    def handlers(self) -> Mapping[str, JobHandler]:
        """The six `JobHandler`s, keyed by the step name each answers to."""

        return {
            STEP_INGEST: self._run_ingest,
            STEP_TEST: self._run_test,
            STEP_SCORE: self._run_score,
            STEP_RANK: self._run_rank,
            STEP_UPDATE_CASES: self._run_update_cases,
            STEP_DISPATCH: self._run_dispatch,
        }

    # -- job failure notice ---------------------------------------------

    def notify_pipeline_failure(self, result: PipelineRunResult) -> int:
        """Tell the administrators that a pipeline run halted, once per run.

        Runs in its own transaction: the failed step has already rolled back
        its own work, and the notice must persist even so.
        """

        session = self.session_factory()
        try:
            run_id = str(result.run_id)
            failed = result.failed_step or "unknown step"
            admins = self._users_with_roles(session, _FAILURE_NOTICE_ROLES)
            error = next(
                (run.error for run in reversed(result.runs) if run.error),
                None,
            )
            pending = [
                person
                for person in admins
                if not any(
                    (payload or {}).get("run_id") == run_id
                    for payload in session.execute(
                        select(Notification.payload).where(
                            Notification.recipient_id == person,
                            Notification.template == JOB_FAILURE_TEMPLATE.name,
                        )
                    ).scalars()
                )
            ]
            if pending:
                NotificationService(
                    session, audit=self._audit(session, new_request_id()), clock=self.clock
                ).queue(
                    JOB_FAILURE_TEMPLATE,
                    {
                        "job_name": failed,
                        "summary": (
                            f"The nightly pipeline stopped at {failed}; the previous queue "
                            "is still being served." + (f" Error: {error[:300]}" if error else "")
                        ),
                        "run_id": run_id,
                    },
                    recipient_ids=pending,
                    actor_id=self.system_actor_id,
                )
            session.commit()
            return len(pending)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- T12: batch completion deadline --------------------------------

    def check_deadline(
        self,
        run_id: str,
        *,
        as_of: str | None = None,
        request_id: str | None = None,
    ) -> Mapping[str, object]:
        """T12 (`spec §17.5`): raise a deadline alert, once, if `run_id`'s
        pipeline is still open at or past its configured deadline — "the
        run still open at the deadline: alert raised, run continues"
        (`spec §R-28.b`).

        Idempotent per `run_id`: a second call after the alert is already
        recorded is a no-op, so a caller polling this on a schedule
        (`T-145`, which owns actually delivering the alert) never floods
        the audit trail with the same fact twice.
        """

        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string.")
        session = self.session_factory()
        try:
            as_of_date = self._resolve_as_of(as_of)
            status = PipelineRunStatus(
                steps=self._latest_step_states(session, run_id),
                ordered_steps=PIPELINE_STEPS,
            )
            deadline_ist = str(self.threshold_store.get("T12")["deadline_ist"])
            evaluation = evaluate_deadline(
                run_id=run_id,
                as_of_date=as_of_date,
                deadline_ist=deadline_ist,
                now=self.clock.now(),
                is_complete=status.is_complete,
            )
            result: dict[str, object] = {
                "run_id": run_id,
                "is_complete": status.is_complete,
                "breached": evaluation.breached,
                "deadline_at": evaluation.deadline_at.isoformat(),
                "alert_raised": False,
                "already_raised": False,
            }
            if not evaluation.should_alert:
                session.commit()
                return result

            subject = AuditSubject(_DEADLINE_ALERT_SUBJECT_TYPE, run_id)
            already_raised = (
                session.execute(
                    select(AuditEvent.id).where(
                        AuditEvent.event_type == _DEADLINE_ALERT_EVENT,
                        AuditEvent.subject_id == subject.subject_id,
                    )
                ).scalar()
                is not None
            )
            if already_raised:
                result["already_raised"] = True
                session.commit()
                return result

            resolved_request_id = request_id or new_request_id()
            self._audit(session, resolved_request_id).record(
                _DEADLINE_ALERT_EVENT,
                subject,
                {
                    "run_id": run_id,
                    "as_of_date": as_of_date.isoformat(),
                    "deadline_at": evaluation.deadline_at.isoformat(),
                    "checked_at": evaluation.now.isoformat(),
                    "first_incomplete_step": status.first_incomplete_step,
                    "completed_steps": [
                        step for step in PIPELINE_STEPS if status.steps.get(step) == SUCCEEDED
                    ],
                },
                actor=self.system_actor_id,
                request_id=resolved_request_id,
            )
            session.commit()
            result["alert_raised"] = True
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- recurring-failure escalation -----------------------------------

    def check_recurring_failure(
        self,
        job_name: str,
        *,
        run_id: str,
        threshold: int = DEFAULT_RECURRING_FAILURE_THRESHOLD,
        request_id: str | None = None,
    ) -> Mapping[str, object]:
        """`spec §R-28.b`: escalate, once, when `job_name` has failed with
        the same normalized cause on `threshold` consecutive pipeline
        runs, `run_id`'s attempt included — "the same failure recurring
        across nights: escalated, because a nightly failure that alerts
        identically every night stops being read."

        Call this after observing `run_id`'s attempt of `job_name` end
        `failed`; idempotent per `(job_name, run_id)`.
        """

        if not isinstance(job_name, str) or not job_name.strip():
            raise ValueError("job_name must be a non-empty string.")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id must be a non-empty string.")
        session = self.session_factory()
        try:
            recent = self._recent_step_outcomes(
                session, job_name, limit=max(threshold, _RECURRING_FAILURE_LOOKBACK)
            )
            consecutive_failures: list[str] = []
            for run in recent:
                if run.state != FAILED:
                    break
                consecutive_failures.append(run.error or "")
            escalate = is_recurring_failure(consecutive_failures, threshold=threshold)
            result: dict[str, object] = {
                "job_name": job_name,
                "run_id": run_id,
                "consecutive_failures": len(consecutive_failures),
                "escalated": False,
                "already_escalated": False,
            }
            if not escalate:
                session.commit()
                return result

            subject = AuditSubject(_RECURRING_FAILURE_SUBJECT_TYPE, f"{job_name}:{run_id}")
            already_escalated = (
                session.execute(
                    select(AuditEvent.id).where(
                        AuditEvent.event_type == _RECURRING_FAILURE_EVENT,
                        AuditEvent.subject_id == subject.subject_id,
                    )
                ).scalar()
                is not None
            )
            if already_escalated:
                result["already_escalated"] = True
                session.commit()
                return result

            resolved_request_id = request_id or new_request_id()
            self._audit(session, resolved_request_id).record(
                _RECURRING_FAILURE_EVENT,
                subject,
                {
                    "job_name": job_name,
                    "run_id": run_id,
                    "consecutive_failures": len(consecutive_failures),
                    "threshold": threshold,
                },
                actor=self.system_actor_id,
                request_id=resolved_request_id,
            )
            session.commit()
            result["escalated"] = True
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- ingest ---------------------------------------------------------

    def _run_ingest(self, context: JobRunContext) -> Mapping[str, object]:
        if self.signal_source is None:
            return {
                "received": 0,
                "inserted": 0,
                "duplicates": 0,
                "rejected": 0,
                "note": "no signal source is configured",
            }
        session = self.session_factory()
        try:
            borrower_id = _parse_uuid(context.borrower_id)
            scope = self._scope_for(session, borrower_id)
            service = SignalIngestionService(
                session,
                audit=self._audit(session, context.request_id),
                clock=self.clock,
                request_id=context.request_id,
            )
            events = tuple(self.signal_source())
            report = service.ingest(self._system_principal(), events, scope=scope)
            session.commit()
            return {
                "received": report.received,
                "inserted": report.inserted,
                "duplicates": report.duplicates,
                "rejected": report.rejected,
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- test -------------------------------------------------------------

    def _run_test(self, context: JobRunContext) -> Mapping[str, object]:
        session = self.session_factory()
        try:
            as_of_date = self._resolve_as_of(context.as_of)
            borrower_id = _parse_uuid(context.borrower_id)
            scope = self._scope_for(session, borrower_id)
            engine = EngineService(
                session,
                audit=self._audit(session, context.request_id),
                clock=self.clock,
                request_id=context.request_id,
                scope_resolver=lambda _principal: scope,
            )
            principal = self._system_principal()
            certificates = self._run_certificate_cycle(session, scope, as_of_date, context)
            due = self._live_covenant_versions(session, scope, as_of_date, borrower_id)
            fiscal = _fiscal_start_month(session)
            tested = 0
            already_tested = 0
            skipped_no_data = 0
            marked_stale = 0
            tracker = IsolationTracker()
            for version, _covenant in due:
                if self._already_tested(session, version.id, as_of_date):
                    already_tested += 1
                    continue
                # A `statement_lines` failure is left uncaught: it means the
                # statement source itself is unavailable for every covenant
                # behind it, which is `spec §R-28.a`'s halt-the-step case, not
                # one borrower's own bad data.
                supplied = (
                    self.statement_lines(version, as_of_date)
                    if self.statement_lines is not None
                    else None
                )
                if supplied is None:
                    skipped_no_data += 1
                    continue
                pending = self._pending_retests(session, version, as_of_date)
                snapshot: StatementSnapshot | None
                lines: Mapping[str, Decimal]
                if isinstance(supplied, StatementSnapshot):
                    snapshot, lines = supplied, supplied.lines
                else:
                    snapshot, lines = None, supplied
                stale = False
                if snapshot is not None and not reads_facility_conduct(version.definition_ref):
                    # A statement covenant is tested once per statement period
                    # (and again only when a retest is queued).  Re-testing an
                    # unchanged statement every night restamped old data as
                    # today's, flattened the trend and reopened the cure window.
                    label = _canonical_label(snapshot.period_label, snapshot.period_end, fiscal)
                    # A cure window that has run out without a new statement
                    # is re-tested on the statement it was opened on, so the
                    # verdict becomes a plain breach the day the window ends.
                    retest = bool(pending) or self._cure_lapsed(session, version.id, as_of_date)
                    if self._statement_overdue(version, snapshot.period_end, as_of_date):
                        if not retest and self._stale_marked(session, version.id, snapshot):
                            already_tested += 1
                            continue
                        stale = True
                        period = PeriodFacts(
                            period_label=label,
                            is_complete=False,
                            last_complete_period=label,
                            period_id=snapshot.period_id,
                            as_of_date=as_of_date,
                        )
                    elif not retest and self._period_tested(session, version.id, snapshot):
                        already_tested += 1
                        continue
                    else:
                        period = PeriodFacts(
                            period_label=label,
                            period_id=snapshot.period_id,
                            as_of_date=as_of_date,
                        )
                else:
                    # Conduct-based covenants read daily facility data, so
                    # they are tested daily against the quarter they fall in.
                    period = PeriodFacts(
                        period_label=period_label_for_date(
                            as_of_date, fiscal_year_start_month=fiscal
                        ),
                        as_of_date=as_of_date,
                    )
                # `spec §R-28.b`: one covenant's own test failure is isolated
                # in its own savepoint so the rest of the book still gets
                # tested tonight.
                try:
                    with session.begin_nested():
                        row = engine.test(
                            principal,
                            covenant_version_id=version.id,
                            period=period,
                            lines=lines,
                            as_of_date=as_of_date,
                            scope=scope,
                        )
                        self._resolve_retests(pending, row)
                except Exception as error:  # noqa: BLE001 - isolated and recorded, not swallowed
                    tracker.record_failure(version.id, error)
                    continue
                tracker.record_success()
                tested += 1
                marked_stale += int(stale)
            session.commit()
            report = tracker.report()
            return {
                "due": len(due),
                "tested": tested,
                "already_tested": already_tested,
                "skipped_no_data": skipped_no_data,
                "marked_stale": marked_stale,
                **certificates,
                **report.as_metrics(),
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _run_certificate_cycle(
        self,
        session: Session,
        scope: Scope,
        as_of_date: date,
        context: JobRunContext,
    ) -> Mapping[str, object]:
        """`spec §R-09`: put certificate due dates on the calendar, raise the
        requests now inside their lead time and mark the ones past grace
        overdue, so tonight's scoring already reads any overdue evidence.

        Isolated in its own savepoint (`spec §R-28.b`): a certificate failure
        is reported in this step's metrics and never stops the book being
        tested.
        """
        if self.certificate_policy is None:
            return {}
        audit = self._audit(session, context.request_id)
        try:
            with session.begin_nested():
                service = CertificateService(
                    session,
                    audit=audit,
                    clock=self.clock,
                    request_id=context.request_id,
                    scope_resolver=lambda _principal: scope,
                    ledger=LedgerService(
                        session,
                        audit=audit,
                        clock=self.clock,
                        request_id=context.request_id,
                        threshold_store=self.threshold_store,
                    ),
                )
                result = service.run_cycle(
                    self._system_principal(),
                    as_of=as_of_date,
                    policy=self.certificate_policy,
                    scope=scope,
                )
        except Exception as error:  # noqa: BLE001 - isolated and recorded, not swallowed
            return {"certificate_cycle_error": f"{type(error).__name__}: {error}"}
        return result.as_metrics()

    def _decay_rate(self) -> Decimal | None:
        """T3's daily evidence retention factor, or `None` for a threshold
        snapshot written before decay was configured (no decay is applied;
        the windowed persistence rule still retires old warnings)."""

        try:
            section = self.threshold_store.get("T3")
        except KeyError:
            return None
        value = section.get("decay_rate") if isinstance(section, Mapping) else None
        if value is None:
            return None
        return DecayThresholds(decay_rate=Decimal(str(value))).decay_rate

    def _statement_allowance_days(self, version: CovenantVersion) -> int | None:
        """Days after a period end before its successor's statement is overdue,
        or `None` for a covenant with no reporting rhythm (`on_event`)."""

        period_days = FREQUENCY_WINDOW_DAYS.get(version.frequency)
        if period_days is None:
            return None
        return period_days + max(version.grace_days or 0, self.statement_grace_days)

    def _statement_overdue(self, version: CovenantVersion, period_end: date, as_of: date) -> bool:
        allowance = self._statement_allowance_days(version)
        return allowance is not None and (as_of - period_end).days > allowance

    def _period_tested(
        self, session: Session, version_id: UUID, snapshot: StatementSnapshot
    ) -> bool:
        statement = (
            select(CovenantTest.id)
            .where(
                CovenantTest.covenant_version_id == version_id,
                CovenantTest.period_id == snapshot.period_id,
                CovenantTest.verdict != "stale",
            )
            .limit(1)
        )
        return session.execute(statement).scalar() is not None

    def _cure_lapsed(self, session: Session, version_id: UUID, as_of_date: date) -> bool:
        latest = session.execute(
            select(CovenantTest.verdict, CovenantTest.cure_ends_on)
            .where(
                CovenantTest.covenant_version_id == version_id,
                CovenantTest.as_of_date <= as_of_date,
                CovenantTest.verdict.in_(_VALUED_VERDICTS),
            )
            .order_by(CovenantTest.as_of_date.desc(), CovenantTest.computed_at.desc())
            .limit(1)
        ).first()
        return (
            latest is not None
            and latest.verdict == "breach_cure_open"
            and latest.cure_ends_on is not None
            and latest.cure_ends_on < as_of_date
        )

    def _stale_marked(
        self, session: Session, version_id: UUID, snapshot: StatementSnapshot
    ) -> bool:
        statement = (
            select(CovenantTest.id)
            .where(
                CovenantTest.covenant_version_id == version_id,
                CovenantTest.period_id == snapshot.period_id,
                CovenantTest.verdict == "stale",
            )
            .limit(1)
        )
        return session.execute(statement).scalar() is not None

    def _pending_retests(
        self, session: Session, version: CovenantVersion, as_of_date: date
    ) -> list[CovenantSchedule]:
        """Queued retests up to today.  Certificate-basis schedules belong to
        the certificate workflow and are left for it to resolve."""

        if version.test_basis == CERTIFICATE_TEST_BASIS:
            return []
        statement = select(CovenantSchedule).where(
            CovenantSchedule.covenant_version_id == version.id,
            CovenantSchedule.due_date <= as_of_date,
            CovenantSchedule.state == ScheduleState.DUE.value,
        )
        return list(session.execute(statement).scalars().all())

    def _resolve_retests(self, pending: Sequence[CovenantSchedule], test: CovenantTest) -> None:
        now = self.clock.now()
        for schedule in pending:
            if schedule.state != ScheduleState.DUE.value:
                continue  # the engine already linked the occurrence dated today
            schedule.state = ScheduleState.TESTED.value
            schedule.test_id = test.id
            schedule.updated_at = now
            schedule.updated_by_id = self.system_actor_id
            schedule.version += 1

    # -- score (projection, probability and driver attribution) -----------

    def _run_score(self, context: JobRunContext) -> Mapping[str, object]:
        session = self.session_factory()
        try:
            as_of_date = self._resolve_as_of(context.as_of)
            borrower_id = _parse_uuid(context.borrower_id)
            scope = self._scope_for(session, borrower_id)
            evidence_count = self._refresh_evidence(
                session, scope, as_of_date, borrower_id, context
            )
            candidates = self._forecast_candidates(session, scope, as_of_date, borrower_id)
            if not candidates:
                session.commit()
                return {"covenant_count": 0, "evidence_items": evidence_count}

            current_job_run = self._current_job_run(session, context.run_id, STEP_SCORE)
            existing_run = self._existing_forecast_run(session, context.run_id, STEP_SCORE)
            snapshot_id = (
                existing_run.threshold_snapshot_id
                if existing_run is not None
                else self.threshold_store.snapshot_id()
            )
            service = ForecastScoringService(
                session,
                audit=self._audit(session, context.request_id),
                threshold_store=self.threshold_store,
                clock=self.clock,
                request_id=context.request_id,
                predictor=self.predictor,
                predictor_mode=self.predictor_mode,
            )
            result = service.score(
                candidates,
                as_of_date=as_of_date,
                horizons=self.horizons,
                weights=self.weights,
                run_id=existing_run.id if existing_run is not None else None,
                threshold_snapshot_id=snapshot_id,
                job_run_id=current_job_run.id,
                model_version=self.model_version,
                request_id=context.request_id,
            )
            session.commit()
            return {
                "forecast_run_id": str(result.run_id),
                "state": result.state,
                "covenant_count": len(candidates),
                "evidence_items": evidence_count,
                "content_hash": result.content_hash,
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _refresh_evidence(
        self,
        session: Session,
        scope: Scope,
        as_of_date: date,
        borrower_id: UUID | None,
        context: JobRunContext,
    ) -> int:
        """Materialise the persisted signal ledger before forecasting.

        The original pipeline had a signal ingestion seam but did not connect
        the immutable events to the stage-3 evidence ledger.  Forecasting now
        refreshes that ledger from the complete, scoped event history, applies
        the approved persistence rule, and records a bounded materiality value
        for the deterministic pressure model.  Raw events are never changed.
        """
        statement = (
            select(SignalEventModel.borrower_id)
            .join(Borrower, Borrower.id == SignalEventModel.borrower_id)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(
                SignalEventModel.event_date <= as_of_date,
                scope.predicate(Portfolio.path),
            )
            .distinct()
        )
        if borrower_id is not None:
            statement = statement.where(SignalEventModel.borrower_id == borrower_id)
        borrower_ids = tuple(session.execute(statement).scalars().all())
        if not borrower_ids:
            return 0

        persistence = PersistenceThresholds.from_store(self.threshold_store)
        decay_rate = self._decay_rate()
        evidence = EvidenceRepository(session, audit=self._audit(session, context.request_id))
        principal = self._system_principal()
        total = 0
        for current_borrower_id in borrower_ids:
            ledger = LedgerService(
                session,
                audit=self._audit(session, context.request_id),
                clock=self.clock,
                request_id=context.request_id,
                threshold_store=self.threshold_store,
            )
            # The ledger's contradiction engine is intentionally append-only:
            # one revision per source polarity is persisted between runs.  A
            # first run may contain thousands of daily observations, so feed
            # it one representative (latest adverse, otherwise latest)
            # observation per family.  The complete raw history is still used
            # immediately below for persistence/materiality and remains fully
            # auditable in ``signal_event``.
            all_events = (
                session.execute(
                    select(SignalEventModel).where(
                        SignalEventModel.borrower_id == current_borrower_id,
                        SignalEventModel.event_date <= as_of_date,
                    )
                )
                .scalars()
                .all()
            )
            representative_events: list[SignalEventModel] = []
            by_identity: dict[tuple[str, str], list[SignalEventModel]] = {}
            for event in all_events:
                by_identity.setdefault((event.family, event.event_type), []).append(event)
            for values in by_identity.values():
                adverse_values = [
                    event
                    for event in values
                    if bool((event.payload or {}).get("is_adverse", False))
                ]
                representative_events.append(
                    max(
                        adverse_values or values,
                        key=lambda event: (event.event_date, str(event.id)),
                    )
                )
            ledger_revision = ledger.revise(
                principal,
                current_borrower_id,
                events=representative_events,
                as_of=as_of_date,
                scope=scope,
                request_id=context.request_id,
            )
            # Only the active interpretation is refreshed.  Superseded rows
            # remain immutable history and must never be resurrected by the
            # persistence/materiality pass below.
            rows = list(
                evidence.for_borrower(
                    current_borrower_id,
                    scope=scope,
                    include_superseded=False,
                )
            )
            if not rows:
                continue
            event_rows = all_events
            grouped: dict[tuple[str, str], list[SignalEventModel]] = {}
            for event in event_rows:
                grouped.setdefault((event.family, event.event_type), []).append(event)
            for item in rows:
                if item.evidence_type in CERTIFICATE_EVIDENCE_TYPES:
                    # Written by the certificate workflow, not derived from
                    # signal events: there is nothing here to re-score it from.
                    total += 1
                    continue
                events = grouped.get((item.family, item.evidence_type), [])
                # Persistence is calculated from adverse observations only.
                # A stream of healthy observations must not make a warning
                # look sustained merely because the account reported daily.
                adverse = [
                    event
                    for event in events
                    if bool((event.payload or {}).get("is_adverse", False))
                ]
                observed_dates = sorted({event.event_date for event in events})
                # Only the current window counts: a streak from months ago is
                # history, not a warning that is still running.
                window_start = as_of_date.fromordinal(
                    as_of_date.toordinal() - persistence.event_window_days + 1
                )
                dates = sorted(
                    {event.event_date for event in adverse if window_start <= event.event_date}
                )
                consecutive = _longest_consecutive_days(dates)
                window_count = len(dates)
                last_adverse = max((event.event_date for event in adverse), default=None)
                # A healthy observation after the last adverse one means the
                # signal has recovered (payments current again, say).
                recovered = last_adverse is not None and any(
                    event.event_date > last_adverse
                    and not bool((event.payload or {}).get("is_adverse", False))
                    for event in events
                )
                sustained = not recovered and (
                    consecutive >= persistence.sustained_days
                    or window_count >= persistence.sustained_events
                )
                materiality_pct = (
                    _signal_materiality_pct(item.family, adverse) if sustained else Decimal("0")
                )
                # Geometric decay from the last adverse day (`domain.signals.decay`),
                # so a warning that has stopped recurring fades rather than
                # weighing on the forecast at full strength.
                decay = (
                    signal_decay_factor((as_of_date - last_adverse).days, decay_rate)
                    if decay_rate is not None and last_adverse is not None
                    else Decimal("1")
                )
                next_state = "sustained" if sustained else "transient"
                source_event_ids = [str(event.id) for event in events if event.id is not None]
                changed = (
                    item.first_seen != (observed_dates[0] if observed_dates else as_of_date)
                    or item.last_seen != (observed_dates[-1] if observed_dates else as_of_date)
                    or item.persistence_days != consecutive
                    or item.event_count_window != window_count
                    or item.state != next_state
                    or item.materiality_pct != materiality_pct
                    or item.decay_factor != decay
                    or item.counts_toward_pressure != (sustained and materiality_pct > 0)
                    or list(item.source_event_ids or []) != source_event_ids
                )
                if not changed:
                    total += 1
                    continue
                previous_state = item.state
                if observed_dates:
                    item.first_seen = observed_dates[0]
                    item.last_seen = observed_dates[-1]
                item.persistence_days = consecutive
                item.event_count_window = window_count
                item.state = next_state
                item.materiality_pct = materiality_pct
                item.decay_factor = decay
                item.counts_toward_pressure = sustained and materiality_pct > 0
                item.source_event_ids = source_event_ids
                item.last_scored_at = self.clock.now()
                item.updated_at = item.last_scored_at
                item.updated_by_id = self.system_actor_id
                item.request_id = context.request_id
                item.version += 1
                if previous_state != next_state:
                    session.add(
                        EvidenceTransition(
                            id=new_id(),
                            evidence_id=item.id,
                            from_state=previous_state,
                            to_state=next_state,
                            occurred_on=as_of_date,
                            rule=(
                                "T3.sustained_days_or_events"
                                if next_state == "sustained"
                                else "T3.persistence_not_met"
                            ),
                            threshold_snapshot_id=self.threshold_store.snapshot_id(),
                            created_at=self.clock.now(),
                            updated_at=self.clock.now(),
                            created_by_id=self.system_actor_id,
                            updated_by_id=self.system_actor_id,
                            request_id=context.request_id,
                        )
                    )
                total += 1
            # LedgerService records a stage-3 trace while deriving its
            # identity rows.  The persistence/materiality pass above then
            # enriches those rows with the complete observed window.  Append
            # the corrected trace so Why? shows the exact values that drive
            # forecast pressure, while retaining the original derivation in
            # the immutable trace history.
            trace_items = tuple(
                EvidenceFacts.from_item(item)
                for item in evidence.for_borrower(
                    current_borrower_id,
                    scope=scope,
                    include_superseded=True,
                )
            )
            TraceRepository(
                session,
                clock=self.clock,
                request_id=context.request_id,
            ).write(
                ("borrower", current_borrower_id),
                _stage3_trace(
                    trace_items,
                    ledger_revision.supersessions,
                    as_of_date,
                    self.threshold_store,
                ),
                actor_id=self.system_actor_id,
                request_id=context.request_id,
                occurred_at=self.clock.now(),
            )
        session.flush()
        return total

    # -- rank ---------------------------------------------------------------

    def _run_rank(self, context: JobRunContext) -> Mapping[str, object]:
        session = self.session_factory()
        try:
            as_of_date = self._resolve_as_of(context.as_of)
            borrower_id = _parse_uuid(context.borrower_id)
            forecast_run = self._existing_forecast_run(session, context.run_id, STEP_SCORE)
            if forecast_run is None:
                session.commit()
                return {"ranked": 0, "note": "no forecast run to rank"}
            # A run whose scoring did not finish has an unknown number of
            # forecasts still to come.  Ranking it would publish a partial
            # book as the day's queue, so the step fails here instead and the
            # pipeline halts with the prior day's run still serving
            # (`spec §R-28.a`).
            if forecast_run.state != COMPLETE_FORECAST_RUN_STATE:
                raise RuntimeError(
                    f"Forecast run {forecast_run.id} is {forecast_run.state!r}, not "
                    f"{COMPLETE_FORECAST_RUN_STATE!r}; ranking an unfinished run would "
                    "publish a partial queue."
                )

            existing_entries = self._entries_for_run(session, forecast_run.id)
            if existing_entries:
                session.commit()
                return {"ranked": len(existing_entries), "resumed": True}

            # `spec §R-14.c`: every borrower in the book is ranked, including one
            # with no forecast (no statement yet, every covenant not
            # computable), which `rank` places after the rankable rows with
            # its reason.  A single-borrower run re-scores one borrower and
            # carries everyone else forward from the queue it replaces, so
            # the queue it becomes never shrinks to that one borrower.
            carried = (
                self._carried_forward_facts(session, forecast_run, borrower_id)
                if borrower_id is not None
                else {}
            )
            borrower_ids = self._borrowers_to_rank(session, forecast_run.id, borrower_id)
            borrower_ids.extend(b_id for b_id in carried if b_id not in set(borrower_ids))
            thresholds = TriageThresholds.from_store(self.threshold_store)
            # `spec §R-28.b`: one borrower's own bad exposure or forecast
            # data (a negative outstanding balance, a malformed fact) is
            # isolated here — excluded from tonight's ranking and recorded
            # — rather than aborting ranking for the whole book.
            tracker = IsolationTracker()
            triage_inputs: list[TriageInput] = []
            for b_id in borrower_ids:
                borrower = session.get(Borrower, b_id)
                if borrower is None:
                    continue
                try:
                    forecasts = (
                        carried[borrower.id]
                        if borrower.id in carried
                        else self._forecast_facts(session, forecast_run.id, borrower.id)
                    )
                    triage_inputs.append(
                        TriageInput(
                            borrower_id=borrower.id,
                            reference=borrower.reference,
                            exposure=self._borrower_exposure(session, borrower.id, as_of_date),
                            forecasts=forecasts,
                            sma_band=self._borrower_sma_band(session, borrower.id, as_of_date),
                        )
                    )
                except Exception as error:  # noqa: BLE001 - isolated and recorded, not swallowed
                    tracker.record_failure(borrower.id, error)
                    continue
                tracker.record_success()
            ranked = rank_triage_entries(triage_inputs, thresholds)
            # A scored book that ranks to nothing is never a legitimate empty
            # day: the queue reads the newest complete run, so committing zero
            # entries here would blank the portfolio screen.  Fail the step and
            # leave the previous run in place instead.
            if borrower_ids and not ranked:
                raise RuntimeError(
                    f"Ranking produced no triage entries for forecast run {forecast_run.id} "
                    f"from {len(borrower_ids)} scored borrowers; refusing to publish an "
                    "empty queue."
                )

            now = self.clock.now()
            for entry in ranked:
                session.add(
                    TriageEntryModel(
                        id=new_id(),
                        run_id=forecast_run.id,
                        borrower_id=entry.borrower_id,
                        worst_covenant_version_id=entry.worst_covenant_version_id,
                        worst_horizon=entry.worst_horizon,
                        probability=entry.probability,
                        confidence=entry.confidence,
                        exposure=entry.exposure,
                        urgency=entry.urgency,
                        band=entry.band,
                        sma_band=entry.sma_band,
                        rank=entry.rank,
                        created_at=now,
                        updated_at=now,
                        created_by_id=self.system_actor_id,
                        updated_by_id=self.system_actor_id,
                        request_id=context.request_id,
                    )
                )
            # `spec §R-14.d`: what-changed is written in the same transaction
            # as the entries, so the queue never serves a run without it.
            changed = 0
            if self.what_changed_thresholds is not None:
                session.flush()
                comparison = TriageService(
                    session,
                    self.what_changed_thresholds,
                    audit=self._audit(session, context.request_id),
                    request_id=context.request_id,
                ).persist_what_changed(forecast_run.id)
                changed = sum(
                    1
                    for change in comparison.current
                    if change.kind not in (ChangeType.NO_CHANGE, ChangeType.FIRST_RUN)
                )
            session.commit()
            report = tracker.report()
            return {
                "ranked": len(ranked),
                "resumed": False,
                "what_changed": changed,
                **report.as_metrics(),
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _borrower_sma_band(
        self, session: Session, borrower_id: UUID, as_of_date: date
    ) -> str | None:
        """Return a complete current-day SMA derivation for queue ranking.

        ``SmaBand.NONE`` is a real value only when every effective facility
        has a conduct row with days-past-due.  Any absent row/value leaves the
        queue field unknown instead of making missing data look healthy.
        """

        facility_ids = tuple(
            session.scalars(
                select(Facility.id)
                .where(
                    Facility.borrower_id == borrower_id,
                    Facility.effective_from <= as_of_date,
                    # Half-open, like exposure and the engine: a facility
                    # closed today is no longer effective today.
                    or_(Facility.effective_to.is_(None), Facility.effective_to > as_of_date),
                )
                .order_by(Facility.id)
            ).all()
        )
        if not facility_ids:
            return None
        conduct = tuple(
            session.scalars(
                select(FacilityConduct).where(
                    FacilityConduct.facility_id.in_(facility_ids),
                    FacilityConduct.as_of_date == as_of_date,
                )
            ).all()
        )
        derivation = derive_borrower_sma(
            conduct,
            borrower_id=borrower_id,
            as_of_date=as_of_date,
            facility_ids=facility_ids,
        )
        if derivation.reason is not None:
            return None
        return derivation.band.value

    # -- update cases -------------------------------------------------------

    def _run_update_cases(self, context: JobRunContext) -> Mapping[str, object]:
        session = self.session_factory()
        try:
            forecast_run = self._existing_forecast_run(session, context.run_id, STEP_SCORE)
            if forecast_run is None:
                session.commit()
                return {"opened": 0}

            act_statement = select(TriageEntryModel).where(
                TriageEntryModel.run_id == forecast_run.id,
                TriageEntryModel.band == ACT_BAND,
            )
            # A single-borrower recheck re-ranks the whole book but acts only
            # on the borrower it was asked about.
            target = _parse_uuid(context.borrower_id)
            if target is not None:
                act_statement = act_statement.where(TriageEntryModel.borrower_id == target)
            act_entries = session.execute(act_statement).scalars().all()
            now = self.clock.now()
            opened = 0
            already_open = 0
            tracker = IsolationTracker()
            for entry in act_entries:
                open_case = (
                    session.execute(
                        select(Case)
                        .where(Case.borrower_id == entry.borrower_id, Case.state != "closed")
                        .order_by(Case.created_at.desc())
                        .limit(1)
                    )
                    .scalars()
                    .first()
                )
                if open_case is not None:
                    already_open += 1
                    continue
                # Closed cases still hold their reference, so the next case
                # for this borrower takes the next ordinal rather than
                # colliding with the UNIQUE constraint on `case.reference`.
                case_sequence = (
                    session.scalar(
                        select(func.count())
                        .select_from(Case)
                        .where(Case.borrower_id == entry.borrower_id)
                    )
                    or 0
                ) + 1
                # `spec §R-28.b`: one borrower's case failing to open (a
                # constraint violation on its own row) is isolated so the
                # rest of tonight's act band still gets a case.
                try:
                    with session.begin_nested():
                        # `spec §R-18`: the band sets the case's SLA (T11), so
                        # an act case left untouched escalates on schedule.
                        due_at, sla_hours = self._case_sla(entry.band or ACT_BAND, now)
                        session.add(
                            Case(
                                id=new_id(),
                                reference=_case_reference(entry.borrower_id, case_sequence),
                                borrower_id=entry.borrower_id,
                                opened_from_run_id=forecast_run.id,
                                state="open",
                                band_at_open=entry.band,
                                assignee_id=self.default_assignee_id,
                                due_at=due_at,
                                sla_hours=sla_hours,
                                created_at=now,
                                updated_at=now,
                                created_by_id=self.system_actor_id,
                                updated_by_id=self.system_actor_id,
                                request_id=context.request_id,
                            )
                        )
                        session.flush()
                except Exception as error:  # noqa: BLE001 - isolated and recorded, not swallowed
                    tracker.record_failure(entry.borrower_id, error)
                    continue
                tracker.record_success()
                opened += 1
            # The overdue sweep runs with the full book, never on a recheck of
            # one borrower.
            escalated = (
                self._escalate_overdue_cases(session, context.request_id, now)
                if target is None
                else 0
            )
            session.commit()
            report = tracker.report()
            return {
                "opened": opened,
                "already_open": already_open,
                "escalated_overdue": escalated,
                **report.as_metrics(),
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- dispatch -------------------------------------------------------------

    def _run_dispatch(self, context: JobRunContext) -> Mapping[str, object]:
        session = self.session_factory()
        try:
            forecast_run = self._existing_forecast_run(session, context.run_id, STEP_SCORE)
            if forecast_run is None:
                session.commit()
                return {"dispatched": 0}

            new_cases = (
                session.execute(select(Case).where(Case.opened_from_run_id == forecast_run.id))
                .scalars()
                .all()
            )
            # A borrower whose case is already open (a monitoring case, say)
            # and who moves back into the act band tonight gets no new case,
            # but the assignee still has to hear about it.
            escalated_cases = self._re_escalated_cases(
                session, forecast_run, _parse_uuid(context.borrower_id)
            )
            alerts = [
                (
                    case,
                    f"Moved into the {case.band_at_open} band; "
                    f"case {case.reference} is open for review.",
                    False,
                )
                for case in new_cases
            ] + [
                (
                    case,
                    f"Moved back into the act band; case {case.reference} is already open.",
                    True,
                )
                for case in escalated_cases
            ]
            now = self.clock.now()
            # The band_change template requires the borrower's own reference,
            # not the case reference, so resolve them once for the batch.
            borrower_references = {
                borrower_id: reference
                for borrower_id, reference in session.execute(
                    select(Borrower.id, Borrower.reference).where(
                        Borrower.id.in_({case.borrower_id for case, _summary, _re in alerts})
                    )
                ).all()
            }
            run_marker = str(forecast_run.id)
            dispatched = 0
            skipped_unassigned = 0
            already_notified = 0
            tracker = IsolationTracker()
            for case, summary, re_escalation in alerts:
                if case.assignee_id is None:
                    skipped_unassigned += 1
                    continue
                previous = (
                    session.execute(
                        select(Notification.payload).where(
                            Notification.subject_type == "case",
                            Notification.subject_id == case.id,
                            Notification.template == _ACT_ALERT_TEMPLATE,
                        )
                    )
                    .scalars()
                    .all()
                )
                # A new case is announced once.  A re-escalation is announced
                # once per run, so a later return to act is not silenced by
                # the alert that announced the case originally.
                if (not re_escalation and previous) or any(
                    (payload or {}).get("forecast_run_id") == run_marker for payload in previous
                ):
                    already_notified += 1
                    continue
                # `spec §R-28.b`: one case's notification failing to send
                # (a bad recipient, a constraint violation) is isolated so
                # the rest of tonight's act band still gets notified.
                try:
                    with session.begin_nested():
                        session.add(
                            Notification(
                                id=new_id(),
                                recipient_id=case.assignee_id,
                                channel=_NOTIFICATION_CHANNEL,
                                template=_ACT_ALERT_TEMPLATE,
                                subject_type="case",
                                subject_id=case.id,
                                payload={
                                    "borrower_reference": borrower_references.get(
                                        case.borrower_id, case.reference
                                    ),
                                    "summary": summary,
                                    "details": f"Case {case.reference}",
                                    "case_reference": case.reference,
                                    "forecast_run_id": run_marker,
                                },
                                state="pending",
                                created_at=now,
                                updated_at=now,
                                created_by_id=self.system_actor_id,
                                updated_by_id=self.system_actor_id,
                                request_id=context.request_id,
                            )
                        )
                        session.flush()
                except Exception as error:  # noqa: BLE001 - isolated and recorded, not swallowed
                    tracker.record_failure(case.id, error)
                    continue
                tracker.record_success()
                dispatched += 1
            desk_notices, summaries = self._notify_risk_desk(
                session,
                forecast_run,
                context.request_id,
                target=_parse_uuid(context.borrower_id),
                already_alerted={case.borrower_id for case, _summary, _re in alerts},
            )
            session.commit()
            report = tracker.report()
            return {
                "desk_notices": desk_notices,
                "morning_summaries": summaries,
                "dispatched": dispatched,
                "skipped_unassigned": skipped_unassigned,
                "already_notified": already_notified,
                **report.as_metrics(),
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _notify_risk_desk(
        self,
        session: Session,
        forecast_run: ForecastRun,
        request_id: str,
        *,
        target: UUID | None,
        already_alerted: set[UUID],
    ) -> tuple[int, int]:
        """`spec §10`/`§R-27`: a band change reaches the people responsible
        for the borrower — the risk desk and relationship managers whose
        portfolios hold it — and each of them gets one morning summary.

        `NotificationService` applies each recipient's portfolio scope and
        preferences, so nobody hears about a borrower outside their book.
        A first review has nothing to compare against: it sends only the
        summary, not one notice per borrower already in the act band.
        """

        service = NotificationService(
            session,
            audit=self._audit(session, request_id),
            clock=self.clock,
            request_id=request_id,
        )
        marker = str(forecast_run.id)
        people = self._users_with_roles(session, _BAND_NOTICE_ROLES)
        entries = self._entries_for_run(session, forecast_run.id)
        previous = self._previous_serving_run(session, forecast_run)
        notices = 0
        if previous is not None and people:
            before = {
                entry.borrower_id: entry.band
                for entry in self._entries_for_run(session, previous.id)
            }
            scopes = {
                person: resolve_scope(Principal.user(person, ()), session) for person in people
            }
            paths: dict[UUID, str] = {
                borrower_id: path
                for borrower_id, path in session.execute(
                    select(Borrower.id, Portfolio.path)
                    .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
                    .where(Borrower.id.in_({entry.borrower_id for entry in entries}))
                )
            }
            references: dict[UUID, str] = {
                borrower_id: reference
                for borrower_id, reference in session.execute(
                    select(Borrower.id, Borrower.reference).where(
                        Borrower.id.in_({entry.borrower_id for entry in entries})
                    )
                )
            }
            for entry in entries:
                if target is not None and entry.borrower_id != target:
                    continue
                prior = before.get(entry.borrower_id)
                if not _band_worsened(prior, entry.band):
                    continue
                if self._already_sent(
                    session, BAND_CHANGE_TEMPLATE.name, entry.borrower_id, marker
                ):
                    continue
                # Only people whose portfolios hold the borrower; the case
                # assignee already heard through the case alert.
                path = paths.get(entry.borrower_id)
                recipients = [
                    person
                    for person in people
                    if path is not None
                    and _in_scope(scopes[person], path)
                    and not (
                        entry.borrower_id in already_alerted and person == self.default_assignee_id
                    )
                ]
                if not recipients:
                    continue
                service.queue(
                    BAND_CHANGE_TEMPLATE,
                    {
                        "borrower_reference": references.get(entry.borrower_id, ""),
                        "summary": (
                            f"Moved from {_BAND_WORDS.get(prior or '', 'not monitored')} to "
                            f"{_BAND_WORDS.get(entry.band or 'watch', 'Watch')} in the review "
                            f"of {forecast_run.as_of_date:%d %b %Y}."
                        ),
                        "details": "Open the borrower to see what changed and why.",
                        "forecast_run_id": marker,
                    },
                    recipient_ids=recipients,
                    subject_type="borrower",
                    subject_id=entry.borrower_id,
                    actor_id=self.system_actor_id,
                )
                notices += 1
        summaries = 0
        review_date = forecast_run.as_of_date.isoformat()
        if target is None:
            for person in self._users_with_roles(session, _SUMMARY_ROLES):
                # One summary per review date: a re-run or a walkthrough
                # signal later the same day is covered by its band notices.
                if self._already_sent(
                    session,
                    MORNING_QUEUE_TEMPLATE.name,
                    None,
                    review_date,
                    recipient_id=person,
                    key="review_date",
                ):
                    continue
                scope = resolve_scope(Principal.user(person, ()), session)
                summary = TriageRepository(session).summary(scope)
                if summary.total == 0:
                    continue
                changes = summary.what_changed
                service.queue(
                    MORNING_QUEUE_TEMPLATE,
                    {
                        "summary": (
                            f"Review of {forecast_run.as_of_date:%d %b %Y}: "
                            f"{summary.act} act now, {summary.amber} amber and "
                            f"{summary.watch} watch across {summary.total} borrowers in "
                            "your portfolios."
                        ),
                        "entries": (
                            "This is the first review, so there is nothing earlier to "
                            "compare against."
                            if previous is None
                            else "No band changes since the last review."
                            if changes == 0
                            else f"{changes} band change{'' if changes == 1 else 's'} "
                            "since the last review; see What's new on the queue."
                        ),
                        "forecast_run_id": marker,
                        "review_date": review_date,
                    },
                    recipient_ids=[person],
                    actor_id=self.system_actor_id,
                )
                summaries += 1
        return notices, summaries

    def _users_with_roles(self, session: Session, roles: tuple[str, ...]) -> list[UUID]:
        return list(
            session.execute(
                select(AppUser.id)
                .distinct()
                .join(UserRole, UserRole.user_id == AppUser.id)
                .join(Role, Role.id == UserRole.role_id)
                .where(AppUser.is_active.is_(True), Role.code.in_(roles))
                .order_by(AppUser.id)
            )
            .scalars()
            .all()
        )

    def _already_sent(
        self,
        session: Session,
        template: str,
        subject_id: UUID | None,
        marker: str,
        *,
        recipient_id: UUID | None = None,
        key: str = "forecast_run_id",
    ) -> bool:
        statement = select(Notification.payload).where(Notification.template == template)
        if subject_id is not None:
            statement = statement.where(Notification.subject_id == subject_id)
        if recipient_id is not None:
            statement = statement.where(Notification.recipient_id == recipient_id)
        return any(
            (payload or {}).get(key) == marker for payload in session.execute(statement).scalars()
        )

    def _case_sla(self, band: str, now: datetime) -> tuple[datetime | None, int | None]:
        """T11's deadline for a new case, or none when T11 is not configured
        (the case still opens; it simply has no SLA to escalate on)."""

        try:
            deadline = derive_sla(band, now, self.threshold_store)
        except (KeyError, LookupError, TypeError, ValueError):
            return None, None
        return deadline.due_at, deadline.hours

    def _escalate_overdue_cases(self, session: Session, request_id: str, now: datetime) -> int:
        """Escalate every case past its SLA and notify its owner (`spec §R-18.b`)."""

        principal = Principal(
            id=self.system_actor_id,
            permissions=frozenset({Permission.VIEW_QUEUE, Permission.UPDATE_CASE}),
            kind=PrincipalKind.USER,
        )
        service = CaseService(
            session,
            audit=self._audit(session, request_id),
            clock=self.clock,
            request_id=request_id,
            scope_resolver=lambda _principal: self._full_book_scope(session),
        )
        return len(service.escalate_overdue(principal, now=now))

    def _re_escalated_cases(
        self, session: Session, forecast_run: ForecastRun, borrower_id: UUID | None
    ) -> list[Case]:
        """Open cases, opened by an earlier run, whose borrower entered the
        act band in ``forecast_run`` from a lower band (or from no entry).
        A single-borrower run considers only that borrower."""

        act_borrowers = set(
            session.execute(
                select(TriageEntryModel.borrower_id).where(
                    TriageEntryModel.run_id == forecast_run.id,
                    TriageEntryModel.band == ACT_BAND,
                )
            )
            .scalars()
            .all()
        )
        if borrower_id is not None:
            act_borrowers &= {borrower_id}
        if not act_borrowers:
            return []
        previous = self._previous_serving_run(session, forecast_run)
        previously_act: set[UUID] = set()
        if previous is not None:
            previously_act = set(
                session.execute(
                    select(TriageEntryModel.borrower_id).where(
                        TriageEntryModel.run_id == previous.id,
                        TriageEntryModel.band == ACT_BAND,
                    )
                )
                .scalars()
                .all()
            )
        escalated = act_borrowers - previously_act
        if not escalated:
            return []
        cases = (
            session.execute(
                select(Case)
                .where(
                    Case.borrower_id.in_(escalated),
                    Case.state != "closed",
                    or_(
                        Case.opened_from_run_id.is_(None),
                        Case.opened_from_run_id != forecast_run.id,
                    ),
                )
                .order_by(Case.borrower_id, Case.created_at.desc())
            )
            .scalars()
            .all()
        )
        latest_by_borrower: dict[UUID, Case] = {}
        for case in cases:
            latest_by_borrower.setdefault(case.borrower_id, case)
        return list(latest_by_borrower.values())

    # -- shared queries and adapters -----------------------------------------

    def _latest_step_states(self, session: Session, run_id: str) -> Mapping[str, str]:
        """The latest attempt's recorded state for each of `PIPELINE_STEPS`
        under one pipeline run id. A step never attempted for this run is
        simply absent, distinguishing "not reached yet" from any recorded
        state (`policy.PipelineRunStatus` treats both as incomplete)."""

        rows = session.execute(
            select(JobRun.job_name, JobRun.attempt, JobRun.state).where(
                JobRun.run_id == run_id, JobRun.job_name.in_(PIPELINE_STEPS)
            )
        ).all()
        latest: dict[str, tuple[int, str]] = {}
        for job_name, attempt, state in rows:
            current = latest.get(job_name)
            if current is None or attempt > current[0]:
                latest[job_name] = (attempt, state)
        return {name: state for name, (_attempt, state) in latest.items()}

    def _recent_step_outcomes(self, session: Session, job_name: str, *, limit: int) -> list[JobRun]:
        """The most recent `limit` pipeline runs' *latest* attempt of
        `job_name`, newest first — exactly one row per run id, regardless
        of how many times that run id's attempt was retried."""

        latest_attempt = (
            select(JobRun.run_id, func.max(JobRun.attempt).label("attempt"))
            .where(JobRun.job_name == job_name)
            .group_by(JobRun.run_id)
            .subquery()
        )
        statement = (
            select(JobRun)
            .join(
                latest_attempt,
                (JobRun.run_id == latest_attempt.c.run_id)
                & (JobRun.attempt == latest_attempt.c.attempt),
            )
            .where(JobRun.job_name == job_name)
            .order_by(JobRun.started_at.desc())
            .limit(limit)
        )
        return list(session.execute(statement).scalars().all())

    def _live_covenant_versions(
        self,
        session: Session,
        scope: Scope,
        as_of_date: date,
        borrower_id: UUID | None,
    ) -> list[tuple[CovenantVersion, Covenant]]:
        statement = (
            select(CovenantVersion, Covenant, Borrower.id)
            .join(Covenant, Covenant.id == CovenantVersion.covenant_id)
            .join(Facility, Facility.id == Covenant.facility_id)
            .join(Borrower, Borrower.id == Facility.borrower_id)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(
                scope.predicate(Portfolio.path),
                CovenantVersion.status == "live",
                Covenant.is_active.is_(True),
                CovenantVersion.effective_from <= as_of_date,
                or_(
                    CovenantVersion.effective_to.is_(None),
                    CovenantVersion.effective_to > as_of_date,
                ),
            )
            .order_by(CovenantVersion.id)
        )
        if borrower_id is not None:
            statement = statement.where(Borrower.id == borrower_id)
        rows = session.execute(statement).all()
        return [(version, covenant) for version, covenant, _borrower_id in rows]

    def _already_tested(
        self, session: Session, covenant_version_id: UUID, as_of_date: date
    ) -> bool:
        statement = (
            select(CovenantTest.id)
            .where(
                CovenantTest.covenant_version_id == covenant_version_id,
                CovenantTest.as_of_date == as_of_date,
            )
            .limit(1)
        )
        if session.execute(statement).scalar() is None:
            return False
        # A statement/restatement queues a fresh ``due`` schedule alongside
        # the historical same-day test.  That explicit trigger must win over
        # this optimization or newly accepted financials never reach a live
        # forecast until tomorrow.
        pending = session.execute(
            select(CovenantSchedule.id)
            .where(
                CovenantSchedule.covenant_version_id == covenant_version_id,
                CovenantSchedule.due_date == as_of_date,
                CovenantSchedule.state == ScheduleState.DUE.value,
            )
            .limit(1)
        ).scalar()
        return pending is None

    def _test_history(
        self, session: Session, covenant_version_id: UUID, as_of_date: date
    ) -> list[CovenantTest]:
        # Newest first so the limit keeps the most recent tests, then flipped
        # back to chronological order for the trend fit.  Same-day retests sort
        # by computation time, so the last row is always the latest result.
        statement = (
            select(CovenantTest)
            .where(
                CovenantTest.covenant_version_id == covenant_version_id,
                CovenantTest.as_of_date <= as_of_date,
            )
            .order_by(CovenantTest.as_of_date.desc(), CovenantTest.computed_at.desc())
            .limit(_TEST_HISTORY_LIMIT)
        )
        return list(reversed(session.execute(statement).scalars().all()))

    def _forecast_candidates(
        self,
        session: Session,
        scope: Scope,
        as_of_date: date,
        borrower_id: UUID | None,
    ) -> list[ForecastCandidate]:
        candidates: list[ForecastCandidate] = []
        due = self._live_covenant_versions(session, scope, as_of_date, borrower_id)
        for version, _covenant in due:
            history = [
                row
                for row in self._test_history(session, version.id, as_of_date)
                if row.value is not None
            ]
            if not history:
                continue
            latest = history[-1]
            statement_based = not reads_facility_conduct(version.definition_ref)
            series = self._observations(session, history, statement_based=statement_based)
            data_as_of = series[-1].observed_on
            allowance = self._statement_allowance_days(version) if statement_based else None
            evidence_items = self._evidence_for_covenant(session, version, scope)
            threshold, threshold_changes = self._threshold_schedule(session, version, as_of_date)
            candidates.append(
                ForecastCandidate(
                    covenant_version_id=version.id,
                    # The limit in force today, and its changes inside the
                    # horizon, so an approved exception relaxes the forecast
                    # exactly where it relaxes the covenant test.
                    threshold=threshold,
                    threshold_changes=threshold_changes,
                    direction=version.direction,
                    series=series,
                    # The date the latest value describes (a statement's period
                    # end), not the night it was re-tested: staleness is
                    # measured from it, past the expected reporting lag.
                    data_as_of=data_as_of,
                    reporting_lag_days=allowance or 0,
                    distance_scale=self.distance_scale,
                    # Sustained evidence at full materiality moves the covenant
                    # by `pressure_rate` of its threshold over one reporting
                    # period, not by whole units of it per day.
                    pressure_scale=(
                        self.pressure_rate
                        * abs(version.threshold)
                        / Decimal(
                            FREQUENCY_WINDOW_DAYS.get(version.frequency, _DEFAULT_PERIOD_DAYS)
                        )
                    ),
                    computable=True,
                    already_breached=latest.verdict in _BREACH_VERDICTS,
                    pressure=evidence_pressure(evidence_items, version.direction),
                    formula_inputs={
                        "signal_families": [item.family for item in evidence_items],
                        "evidence_ids": [str(item.id) for item in evidence_items],
                    },
                )
            )
        return candidates

    def _threshold_schedule(
        self, session: Session, version: CovenantVersion, as_of_date: date
    ) -> tuple[Decimal, list[ThresholdChange]]:
        """The threshold in force on ``as_of_date`` and each change to it
        within the longest horizon, from the version's exception windows."""

        fiscal = _fiscal_start_month(session)
        windows: list[tuple[date, date, Decimal]] = []
        rows = session.execute(
            select(CovenantException).where(
                CovenantException.covenant_version_id == version.id,
                CovenantException.relaxed_threshold.is_not(None),
            )
        ).scalars()
        for row in rows:
            try:
                start, _ = period_bounds_for_label(row.from_period, fiscal_year_start_month=fiscal)
                _, end = period_bounds_for_label(row.to_period, fiscal_year_start_month=fiscal)
            except (TypeError, ValueError):
                continue  # a malformed window cannot be placed on the calendar
            assert row.relaxed_threshold is not None
            windows.append((start, end, row.relaxed_threshold))

        def in_force(day: date) -> Decimal:
            for start, end, relaxed in windows:
                if start <= day <= end:
                    return relaxed
            return version.threshold

        current = in_force(as_of_date)
        horizon_end = date.fromordinal(as_of_date.toordinal() + max(self.horizons))
        boundaries = sorted(
            {start for start, _end, _relaxed in windows}
            | {date.fromordinal(end.toordinal() + 1) for _start, end, _relaxed in windows}
        )
        changes: list[ThresholdChange] = []
        previous = current
        for boundary in boundaries:
            if as_of_date < boundary <= horizon_end and in_force(boundary) != previous:
                previous = in_force(boundary)
                changes.append(
                    ThresholdChange(
                        threshold=previous,
                        effective_date=boundary,
                        reason="covenant exception window",
                    )
                )
        return current, changes

    def _observations(
        self, session: Session, history: Sequence[CovenantTest], *, statement_based: bool
    ) -> list[Observation]:
        """One observation per date the values describe, oldest first.

        A test tied to a statement period describes that period's end, however
        late it was run; a re-test of the same period replaces the earlier
        value.  Older statement tests carry no period, and older nightly runs
        re-tested the same statement daily — an identical value within days of
        the last one is that statement again, kept at its first date, or the
        copies would flatten the trend toward zero.  A genuinely flat history
        a quarter apart keeps every point.
        """

        period_ids = {row.period_id for row in history if row.period_id is not None}
        period_ends: dict[UUID, date] = {}
        if period_ids:
            statement = select(FinancialPeriod.id, FinancialPeriod.period_end).where(
                FinancialPeriod.id.in_(period_ids)
            )
            period_ends = {
                period_id: period_end for period_id, period_end in session.execute(statement)
            }
        by_date: dict[date, Decimal] = {}
        previous_untied: tuple[date, Decimal] | None = None
        for row in history:  # chronological; a later test of a date wins
            assert row.value is not None
            if row.period_id is not None and row.period_id in period_ends:
                by_date[period_ends[row.period_id]] = row.value
                previous_untied = None
                continue
            copy = (
                statement_based
                and previous_untied is not None
                and row.value == previous_untied[1]
                and (row.as_of_date - previous_untied[0]).days < _MIN_STATEMENT_GAP_DAYS
            )
            previous_untied = (row.as_of_date, row.value)
            if copy:
                continue  # a nightly copy of the same statement, not a new period
            by_date[row.as_of_date] = row.value
        return [Observation(date=day, value=value) for day, value in sorted(by_date.items())]

    def _evidence_for_covenant(
        self,
        session: Session,
        version: CovenantVersion,
        scope: Scope,
    ) -> tuple[EvidenceItem, ...]:
        """Return scoped borrower evidence for the forecast pressure terms."""
        borrower_id = session.scalar(
            select(Borrower.id)
            .join(Facility, Facility.borrower_id == Borrower.id)
            .join(Covenant, Covenant.facility_id == Facility.id)
            .where(Covenant.id == version.covenant_id)
            .limit(1)
        )
        if borrower_id is None:
            return ()
        return tuple(
            EvidenceRepository(session).for_borrower(
                borrower_id, scope=scope, include_superseded=False
            )
        )

    def _current_job_run(self, session: Session, run_id: str, job_name: str) -> JobRun:
        statement = (
            select(JobRun)
            .where(JobRun.run_id == run_id, JobRun.job_name == job_name, JobRun.state == "running")
            .order_by(JobRun.attempt.desc())
            .limit(1)
        )
        row = session.execute(statement).scalars().first()
        if row is None:
            raise RuntimeError(
                f"No running job_run row for job_name={job_name!r}, run_id={run_id!r}; "
                "this handler must run inside JobRunner."
            )
        return row

    def _existing_forecast_run(
        self, session: Session, run_id: str, job_name: str
    ) -> ForecastRun | None:
        job_run_ids = select(JobRun.id).where(JobRun.run_id == run_id, JobRun.job_name == job_name)
        statement = (
            select(ForecastRun)
            .where(ForecastRun.job_run_id.in_(job_run_ids))
            .order_by(ForecastRun.created_at.desc())
            .limit(1)
        )
        return session.execute(statement).scalars().first()

    def _entries_for_run(self, session: Session, run_id: UUID) -> list[TriageEntryModel]:
        statement = select(TriageEntryModel).where(TriageEntryModel.run_id == run_id)
        return list(session.execute(statement).scalars().all())

    def _borrowers_to_rank(
        self, session: Session, forecast_run_id: UUID, borrower_id: UUID | None
    ) -> list[UUID]:
        """Every active borrower in the book, plus any borrower scored in
        this run — not only those that happen to have a forecast row."""

        if borrower_id is not None:
            return [borrower_id]
        scope = self._full_book_scope(session)
        active = (
            select(Borrower.id)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(scope.predicate(Portfolio.path), Borrower.is_active.is_(True))
        )
        scored = (
            select(Borrower.id)
            .join(Facility, Facility.borrower_id == Borrower.id)
            .join(Covenant, Covenant.facility_id == Facility.id)
            .join(CovenantVersion, CovenantVersion.covenant_id == Covenant.id)
            .join(Forecast, Forecast.covenant_version_id == CovenantVersion.id)
            .where(Forecast.run_id == forecast_run_id)
        )
        ids = set(session.execute(active).scalars().all())
        ids.update(session.execute(scored).scalars().all())
        return sorted(ids, key=str)

    def _carried_forward_facts(
        self, session: Session, forecast_run: ForecastRun, borrower_id: UUID
    ) -> dict[UUID, list[ForecastFact]]:
        """The rest of the book's worst-horizon facts from the queue a
        single-borrower run replaces, so its re-rank covers every borrower."""

        serving = self._previous_serving_run(session, forecast_run)
        if serving is None:
            return {}
        carried: dict[UUID, list[ForecastFact]] = {}
        for entry in self._entries_for_run(session, serving.id):
            if entry.borrower_id == borrower_id:
                continue
            if entry.worst_covenant_version_id is None or entry.worst_horizon is None:
                carried[entry.borrower_id] = []
                continue
            carried[entry.borrower_id] = [
                ForecastFact(
                    covenant_version_id=entry.worst_covenant_version_id,
                    horizon_days=entry.worst_horizon,
                    probability=entry.probability,
                    confidence=entry.confidence,
                    suppressed=entry.probability is None,
                )
            ]
        return carried

    def _previous_serving_run(self, session: Session, current: ForecastRun) -> ForecastRun | None:
        """The newest complete run before ``current`` that has a ranked queue."""

        statement = (
            select(ForecastRun)
            .where(
                ForecastRun.id != current.id,
                ForecastRun.state == COMPLETE_FORECAST_RUN_STATE,
                ForecastRun.as_of_date <= current.as_of_date,
                exists(select(1).where(TriageEntryModel.run_id == ForecastRun.id)),
            )
            .order_by(
                ForecastRun.as_of_date.desc(),
                ForecastRun.finished_at.desc().nullslast(),
                ForecastRun.id.desc(),
            )
        )
        if current.finished_at is not None:
            statement = statement.where(
                or_(
                    ForecastRun.finished_at.is_(None),
                    ForecastRun.finished_at <= current.finished_at,
                )
            )
        return session.execute(statement.limit(1)).scalars().first()

    def _forecast_facts(
        self, session: Session, forecast_run_id: UUID, borrower_id: UUID
    ) -> list[ForecastFact]:
        statement = (
            select(Forecast)
            .join(CovenantVersion, CovenantVersion.id == Forecast.covenant_version_id)
            .join(Covenant, Covenant.id == CovenantVersion.covenant_id)
            .join(Facility, Facility.id == Covenant.facility_id)
            .where(Forecast.run_id == forecast_run_id, Facility.borrower_id == borrower_id)
        )
        rows = session.execute(statement).scalars().all()
        return [
            ForecastFact(
                covenant_version_id=row.covenant_version_id,
                horizon_days=row.horizon_days,
                probability=row.probability,
                confidence=row.confidence,
                below_confidence_floor=row.below_confidence_floor,
                suppressed=row.below_confidence_floor,
            )
            for row in rows
        ]

    def _borrower_exposure(
        self, session: Session, borrower_id: UUID, as_of_date: date
    ) -> Decimal | None:
        statement = select(Facility.sanctioned_limit, Facility.outstanding).where(
            Facility.borrower_id == borrower_id,
            Facility.effective_from <= as_of_date,
            or_(Facility.effective_to.is_(None), Facility.effective_to > as_of_date),
        )
        total = Decimal("0")
        found = False
        for sanctioned_limit, outstanding in session.execute(statement).all():
            found = True
            total += outstanding if outstanding is not None else sanctioned_limit
        # `Facility` money is held in ₹ crore — the ratio library declares
        # `unit="₹ crore"` on every absolute-amount covenant and bands
        # `drawing_power_headroom` accordingly.  `TriageEntry.exposure` is
        # rupees, which is the unit the queue and the case file format and
        # `tests/integration/test_case_file.py` pins.  This is the one hop
        # between the two, and it was carrying the number across unchanged:
        # a ₹636 crore book reached the queue as the figure 636.30 and
        # rendered as "₹636.30".
        return total * _RUPEES_PER_CRORE if found else None

    def _scope_for(self, session: Session, borrower_id: UUID | None) -> Scope:
        if borrower_id is None:
            return self._full_book_scope(session)
        borrower = session.get(Borrower, borrower_id)
        if borrower is None:
            raise NotFound(f"Borrower {borrower_id} was not found.")
        portfolio = session.get(Portfolio, borrower.portfolio_id)
        if portfolio is None:  # pragma: no cover - referential integrity guarantees this
            raise NotFound(f"Portfolio {borrower.portfolio_id} was not found.")
        return Scope(principal_id=self.system_actor_id, exact_paths=(portfolio.path,))

    def _full_book_scope(self, session: Session) -> Scope:
        statement = select(Portfolio.path).where(Portfolio.parent_id.is_(None))
        roots = session.execute(statement).scalars().all()
        return Scope(principal_id=self.system_actor_id, descendant_paths=tuple(roots))

    def _system_principal(self) -> Principal:
        return Principal(
            id=self.system_actor_id,
            permissions=frozenset({Permission.VIEW_COVENANT, Permission.INGEST_DATA}),
            kind=PrincipalKind.USER,
        )

    def _audit(self, session: Session, request_id: str) -> _AuditWriterAdapter:
        recorder = AuditRecorder(
            SqlAlchemyAuditStore(session), clock=self.clock, request_id=request_id
        )
        return _AuditWriterAdapter(recorder)

    def _resolve_as_of(self, as_of: str | None) -> date:
        if as_of is None:
            return self.clock.now().date()
        return date.fromisoformat(as_of)


def _parse_uuid(value: str | UUID | None) -> UUID | None:
    if value is None or isinstance(value, UUID):
        return value
    return UUID(value)


def _case_reference(borrower_id: UUID, sequence: int = 1) -> str:
    """Build the human-readable case reference for one borrower's Nth case.

    ``case.reference`` is UNIQUE, so a reference derived from the borrower
    alone caps a borrower at exactly one case for the lifetime of the
    database: once that case closes, a borrower who deteriorates again can
    never be raised a second time.  The first case keeps the original
    unsuffixed reference so existing rows and links stay valid, and each
    re-raise appends its ordinal.
    """

    base = f"C-{borrower_id.hex[:12].upper()}"
    return base if sequence <= 1 else f"{base}-{sequence}"


_BAND_ORDER: Final[Mapping[str, int]] = {"watch": 0, "amber": 1, "act": 2}
_BAND_WORDS: Final[Mapping[str, str]] = {"watch": "Watch", "amber": "Amber", "act": "Act now"}
#: Who hears about a borrower's band worsening: the risk desk and the
#: relationship managers, each only for borrowers in their own portfolios.
_BAND_NOTICE_ROLES: Final[tuple[str, ...]] = ("risk_head", "risk", "relationship_manager")
_SUMMARY_ROLES: Final[tuple[str, ...]] = _BAND_NOTICE_ROLES
_FAILURE_NOTICE_ROLES: Final[tuple[str, ...]] = ("administrator",)


def _in_scope(scope: Scope, path: str) -> bool:
    return path in scope.exact_paths or any(
        path.startswith(prefix) for prefix in scope.descendant_paths
    )


def _band_worsened(before: str | None, after: str | None) -> bool:
    """A move into amber or act from a lower band, or a first appearance there."""

    after_rank = _BAND_ORDER.get(after or "watch", 0)
    if after_rank == 0:
        return False
    return before is None or after_rank > _BAND_ORDER.get(before, 0)


def _fiscal_start_month(session: Session) -> int:
    """The bank's fiscal-year start month (`organisation`), April by default."""

    value = session.scalar(select(Organisation.fiscal_year_start_month).limit(1))
    return value if isinstance(value, int) else DEFAULT_FISCAL_YEAR_START_MONTH


def _canonical_label(label: str, period_end: date, fiscal_year_start_month: int) -> str:
    """The statement's own `FYyyQn` label, or one derived from its period end
    when the stored label is not canonical (so exception lookup never fails)."""

    try:
        return normalise_period(label)
    except (TypeError, ValueError):
        return period_label_for_date(period_end, fiscal_year_start_month=fiscal_year_start_month)


def _longest_consecutive_days(values: Sequence[date]) -> int:
    longest = 0
    current = 0
    previous: date | None = None
    for value in values:
        if previous is not None and value.toordinal() == previous.toordinal() + 1:
            current += 1
        else:
            current = 1
        longest = max(longest, current)
        previous = value
    return longest


def _signal_materiality_pct(family: str, events: Sequence[SignalEventModel]) -> Decimal:
    """Normalise raw signal magnitudes to the stored percentage-point score."""
    if not events:
        return Decimal("0")
    latest = max(events, key=lambda event: (event.event_date, str(event.id)))
    magnitude = latest.magnitude or Decimal("0")
    scale = {
        "payment": Decimal("2"),
        "utilisation": Decimal("1"),
        "account_activity": Decimal("1"),
        "treasury": Decimal("100"),
        "concentration": Decimal("1"),
        "industry": Decimal("100"),
        "news": Decimal("100"),
    }.get(family, Decimal("1"))
    value = abs(magnitude) * scale
    # A sustained adverse signal is material by construction, but remains
    # bounded so one malformed magnitude cannot dominate the portfolio.
    return min(Decimal("100"), max(Decimal("5"), value))


__all__ = [
    "NightlyPipelineService",
    "SignalSourceProvider",
    "StatementLinesProvider",
]
