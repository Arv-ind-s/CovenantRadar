"""Integration coverage for the daily compliance certificate cycle
(`spec §R-09`): calendar due dates, request raising, overdue chasing and the
notices that go with them, run on their own and inside the nightly pipeline."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from covenant_radar.core.clock import FixedClock
from covenant_radar.db.models import CovenantVersion
from covenant_radar.db.models.covenant import CovenantSchedule
from covenant_radar.db.models.document import Document
from covenant_radar.db.models.signal import CertificateRequest, EvidenceItem
from covenant_radar.db.models.workflow import Notification
from covenant_radar.domain.certificates.requirements import CERTIFICATE_TEST_BASIS
from covenant_radar.scheduler.pipeline import STEP_TEST, run_nightly_pipeline
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.certificates import CertificateCyclePolicy, CertificateService
from tests.integration.test_certificate_generation import _Bundle
from tests.integration.test_nightly_evidence_lifecycle import (
    _late_payments,
    _monitored,
    _Thresholds,
)
from tests.integration.test_nightly_pipeline import _TODAY, _Fixture, _lines_provider

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)
_DAY = date(2026, 10, 3)
_POLICY = CertificateCyclePolicy(lead_time_days=30, grace_days=15)


def _cycle_bundle() -> tuple[_Bundle, CertificateService]:
    bundle = _Bundle()
    bundle.principal = Principal.user(
        bundle.principal.id,
        (
            Permission.VIEW_COVENANT,
            Permission.REGISTER_COVENANT,
            Permission.INGEST_DATA,
            Permission.UPLOAD_DOCUMENT,
            Permission.RECORD_WAIVER,
        ),
    )
    service = CertificateService(
        bundle.session,
        audit=bundle.audit,
        clock=FixedClock(_NOW),
        request_id="rq-certificate-cycle",
        scope_resolver=lambda _principal: bundle.scope,
    )
    return bundle, service


def _schedules(bundle: _Bundle, version_id: object) -> list[CovenantSchedule]:
    return list(
        bundle.session.execute(
            select(CovenantSchedule)
            .where(CovenantSchedule.covenant_version_id == version_id)
            .order_by(CovenantSchedule.due_date)
        ).scalars()
    )


def test_due_dates_follow_the_calendar_with_one_period_lookback() -> None:
    bundle, service = _cycle_bundle()
    try:
        version = bundle.register_covenant("CV-CYCLE-A")

        scheduled = service.schedule_due_dates(
            bundle.principal, as_of=_DAY, lead_time_days=30, grace_days=15
        )

        # One quarter plus grace back (to 20 Jun) and the lead time forward
        # (to 2 Nov): the quarter-ends of an April fiscal year in between,
        # and none of the eighteen months of history before them.
        assert [row.due_date for row in scheduled] == [date(2026, 6, 30), date(2026, 9, 30)]
        assert all(row.state == "due" for row in scheduled)

        _schedules(bundle, version.id)[0].state = "tested"
        bundle.session.flush()
        again = service.schedule_due_dates(
            bundle.principal, as_of=_DAY, lead_time_days=30, grace_days=15
        )
        assert again == ()
        assert len(_schedules(bundle, version.id)) == 2
    finally:
        bundle.close()


def test_retest_rows_never_raise_a_request() -> None:
    bundle, service = _cycle_bundle()
    try:
        version = bundle.register_covenant("CV-CYCLE-B")
        # A retest the engine queued on the day a statement arrived: `due`,
        # but not a date on the covenant's testing calendar.
        bundle.add_schedule(version.id, due_date=date(2026, 9, 14))

        assert service.generate(bundle.principal, as_of=_DAY, lead_time_days=30).raised == ()

        bundle.add_schedule(version.id, due_date=date(2026, 9, 30))
        raised = service.generate(bundle.principal, as_of=_DAY, lead_time_days=30).raised
        assert [request.due_date for request in raised] == [date(2026, 9, 30)]
    finally:
        bundle.close()


def test_cycle_raises_chases_and_notifies_once() -> None:
    bundle, service = _cycle_bundle()
    try:
        rm_user_id = bundle.add_relationship_manager()
        bundle.register_covenant("CV-CYCLE-C")

        result = service.run_cycle(bundle.principal, as_of=_DAY, policy=_POLICY)

        assert len(result.scheduled) == 2
        assert sorted(request.due_date for request in result.raised) == [
            date(2026, 6, 30),
            date(2026, 9, 30),
        ]
        assert [request.due_date for request in result.overdue] == [date(2026, 6, 30)]
        assert result.as_metrics() == {
            "certificate_due_dates_scheduled": 2,
            "certificate_requests_raised": 2,
            "certificate_requests_cancelled": 0,
            "certificate_requests_overdue": 1,
        }
        evidence = bundle.session.execute(
            select(EvidenceItem).where(
                EvidenceItem.borrower_id == bundle.borrower.id,
                EvidenceItem.evidence_type == "certificate_overdue",
            )
        ).scalar_one()
        assert evidence.superseded_by_id is None

        notices = list(
            bundle.session.execute(
                select(Notification).where(
                    Notification.recipient_id == rm_user_id,
                    Notification.template == "certificate_due",
                )
            ).scalars()
        )
        assert len(notices) == 3
        assert all(notice.state == "pending" for notice in notices)
        summaries = sorted(str(notice.payload["summary"]) for notice in notices)
        assert any("95 days overdue" in summary for summary in summaries)
        assert all(notice.payload["borrower_reference"] == "B-T038" for notice in notices)

        repeat = service.run_cycle(bundle.principal, as_of=_DAY, policy=_POLICY)
        assert repeat.as_metrics() == dict.fromkeys(result.as_metrics(), 0)
        notice_count = bundle.session.execute(
            select(Notification.id).where(Notification.template == "certificate_due")
        ).all()
        assert len(notice_count) == 3
    finally:
        bundle.close()


def test_received_certificate_is_never_marked_overdue() -> None:
    bundle, service = _cycle_bundle()
    try:
        bundle.register_covenant("CV-CYCLE-D")
        service.schedule_due_dates(bundle.principal, as_of=_DAY, lead_time_days=30, grace_days=15)
        raised = service.generate(bundle.principal, as_of=_DAY, lead_time_days=30).raised
        june = next(request for request in raised if request.due_date == date(2026, 6, 30))
        document = Document(
            id=uuid4(),
            borrower_id=bundle.borrower.id,
            doc_type="compliance_certificate",
            filename="q1-certificate.pdf",
            content_hash=uuid4().hex,
            byte_size=1024,
            mime_type="application/pdf",
            storage_key=f"documents/{uuid4().hex}.pdf",
            uploaded_by_id=bundle.principal.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-certificate-cycle-document",
        )
        bundle.session.add(document)
        bundle.session.flush()
        service.receive(bundle.principal, june.id, document_id=document.id)

        overdue = service.sweep_overdue(bundle.principal, as_of=_DAY, grace_days=15)

        assert overdue == ()
        assert bundle.session.get(CertificateRequest, june.id).state == "received"
        summary = service.summaries(bundle.principal, (june,))[0]
        assert summary.borrower_reference == "B-T038"
        assert summary.covenant_references == ("CV-CYCLE-D",)
        assert summary.document_filename == "q1-certificate.pdf"
    finally:
        bundle.close()


def test_certificate_rejected_after_acceptance_is_chased_again() -> None:
    """`spec §R-09.d`: an accepted certificate later found to be for the wrong
    period is rejected, and the next cycle owes and chases it afresh."""
    bundle, service = _cycle_bundle()
    try:
        bundle.register_covenant("CV-CYCLE-E")
        first = service.run_cycle(bundle.principal, as_of=_DAY, policy=_POLICY)
        june = first.overdue[0]
        document = Document(
            id=uuid4(),
            borrower_id=bundle.borrower.id,
            doc_type="compliance_certificate",
            filename="wrong-quarter.pdf",
            content_hash=uuid4().hex,
            byte_size=1024,
            mime_type="application/pdf",
            storage_key=f"documents/{uuid4().hex}.pdf",
            uploaded_by_id=bundle.principal.id,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-certificate-cycle-wrong-quarter",
        )
        bundle.session.add(document)
        bundle.session.flush()
        service.receive(bundle.principal, june.id, document_id=document.id)
        service.accept(bundle.principal, june.id)
        service.reject(bundle.principal, june.id, reason="Covers the March quarter, not June.")

        again = service.run_cycle(bundle.principal, as_of=_DAY, policy=_POLICY)

        assert [request.due_date for request in again.raised] == [date(2026, 6, 30)]
        assert [request.id for request in again.overdue] == [again.raised[0].id]
        assert bundle.session.get(CertificateRequest, june.id).state == "rejected"
        items = list(
            bundle.session.execute(
                select(EvidenceItem)
                .where(
                    EvidenceItem.borrower_id == bundle.borrower.id,
                    EvidenceItem.evidence_type.in_(
                        ("certificate_overdue", "certificate_satisfied")
                    ),
                )
                .order_by(EvidenceItem.created_at, EvidenceItem.id)
            ).scalars()
        )
        current = [item for item in items if item.superseded_by_id is None]
        assert [item.evidence_type for item in current].count("certificate_overdue") == 1
        # The first overdue item and the receipt that satisfied it are history.
        assert len(items) == 3
    finally:
        bundle.close()


@pytest.fixture
def nightly(tmp_path: Path) -> Iterator[_Fixture]:
    built = _Fixture(tmp_path)
    try:
        yield built
    finally:
        built.close()


def test_nightly_pipeline_runs_the_cycle_and_keeps_overdue_evidence(nightly: _Fixture) -> None:
    borrower, facility_id, version_id = _monitored(nightly, "B-CERT")
    with nightly.session_factory() as session:
        session.execute(
            update(CovenantVersion)
            .where(CovenantVersion.id == version_id)
            .values(test_basis=CERTIFICATE_TEST_BASIS)
        )
        session.commit()
    # A live payment signal, so tonight's evidence pass walks this
    # borrower's ledger — the certificate item included.
    events = _late_payments(
        borrower,
        facility_id,
        {_TODAY - timedelta(days=offset): True for offset in (3, 2, 1)},
    )
    service = nightly.build_service(
        statement_lines=_lines_provider({version_id: Decimal("2.1")}),
        signal_source=lambda: events,
        threshold_store=_Thresholds(decay_rate=Decimal("0.95")),
        certificate_policy=CertificateCyclePolicy(lead_time_days=30, grace_days=15),
    )
    runner = nightly.build_runner(service)

    first = run_nightly_pipeline(runner, trigger="manual", as_of=_TODAY.isoformat())

    assert first.success is True, first
    test_metrics = next(run for run in first.runs if run.job_name == STEP_TEST).metrics
    assert "certificate_cycle_error" not in test_metrics
    assert test_metrics["certificate_requests_raised"] == 2
    assert test_metrics["certificate_requests_overdue"] == 1
    with nightly.session_factory() as session:
        states = sorted(
            (request.due_date, request.state)
            for request in session.execute(select(CertificateRequest)).scalars()
        )
        assert states == [(date(2026, 6, 30), "overdue"), (date(2026, 9, 30), "requested")]
        overdue_item = session.execute(
            select(EvidenceItem).where(
                EvidenceItem.borrower_id == borrower.id,
                EvidenceItem.evidence_type == "certificate_overdue",
            )
        ).scalar_one()
        assert overdue_item.superseded_by_id is None
        assert any(
            str(source).startswith("certificate-overdue-")
            for source in overdue_item.source_event_ids or []
        )
        scored_signal = session.execute(
            select(EvidenceItem).where(
                EvidenceItem.borrower_id == borrower.id,
                EvidenceItem.evidence_type == "payment_delay",
            )
        ).scalar_one()
        assert scored_signal.persistence_days == 3

    second = run_nightly_pipeline(runner, trigger="manual", as_of=_TODAY.isoformat())

    assert second.success is True, second
    with nightly.session_factory() as session:
        assert len(session.execute(select(CertificateRequest.id)).all()) == 2
        items = list(
            session.execute(
                select(EvidenceItem).where(
                    EvidenceItem.borrower_id == borrower.id,
                    EvidenceItem.evidence_type == "certificate_overdue",
                )
            ).scalars()
        )
        assert len(items) == 1
        assert items[0].superseded_by_id is None
