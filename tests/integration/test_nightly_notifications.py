"""Who hears about what after a nightly review (`spec §10`, `§R-18`, `§R-27`).

The notification centre is per-recipient: these tests assert who receives
each notice, that scope keeps a borrower's news inside the portfolios a user
covers, and that re-running a review never repeats a notice.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from covenant_radar.core.clock import FixedClock
from covenant_radar.db.models import AppUser, Case, Portfolio
from covenant_radar.db.models.identity import Role, UserPortfolioScope, UserRole
from covenant_radar.db.models.workflow import Notification
from covenant_radar.domain.triage.changes import ChangeThresholds
from covenant_radar.notifications.digest import deep_link
from covenant_radar.scheduler.pipeline import PipelineRunResult, run_nightly_pipeline
from tests.integration.test_nightly_pipeline import (
    _NOW,
    _TODAY,
    _YESTERDAY,
    _Fixture,
    _lines_provider,
)

pytestmark = pytest.mark.integration

_CHANGES = ChangeThresholds(
    reporting_threshold=Decimal("0.05"), dominant_driver_share=Decimal("0.50")
)


@pytest.fixture
def fixture(tmp_path: Path) -> Iterator[_Fixture]:
    built = _Fixture(tmp_path)
    try:
        yield built
    finally:
        built.close()


def _person(fixture: _Fixture, username: str, role_code: str, *portfolios: Portfolio) -> UUID:
    user_id = uuid4()
    with fixture.session_factory() as session:
        role = session.query(Role).filter(Role.code == role_code).one_or_none()
        if role is None:
            role = Role(
                id=uuid4(),
                code=role_code,
                name=role_code,
                created_at=_NOW,
                updated_at=_NOW,
                request_id=f"rq-role-{role_code}",
            )
            session.add(role)
        session.add(
            AppUser(
                id=user_id,
                username=username,
                email=f"{username}@example.com",
                full_name=username,
                auth_source="local",
                created_at=_NOW,
                updated_at=_NOW,
                request_id=f"rq-user-{username}",
            )
        )
        session.flush()
        session.add(
            UserRole(
                id=uuid4(),
                user_id=user_id,
                role_id=role.id,
                granted_at=_NOW,
                created_at=_NOW,
                updated_at=_NOW,
                request_id=f"rq-grant-{username}",
            )
        )
        for portfolio in portfolios:
            session.add(
                UserPortfolioScope(
                    id=uuid4(),
                    user_id=user_id,
                    portfolio_id=portfolio.id,
                    include_descendants=True,
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id=f"rq-scope-{username}-{portfolio.code}",
                )
            )
        session.commit()
    return user_id


def _night(fixture: _Fixture, lines: dict[UUID, Decimal], as_of, *, minutes: int = 0, rm=None):
    service = fixture.build_service(
        statement_lines=_lines_provider(lines),
        clock=FixedClock(_NOW + timedelta(minutes=minutes)),
        what_changed_thresholds=_CHANGES,
        default_assignee_id=rm,
    )
    result = run_nightly_pipeline(
        fixture.build_runner(service), trigger="manual", as_of=as_of.isoformat()
    )
    assert result.success is True, result
    return result


def _inbox(fixture: _Fixture, user_id: UUID) -> list[Notification]:
    return [n for n in fixture.notifications() if n.recipient_id == user_id]


def test_band_changes_reach_the_people_who_cover_the_borrower(fixture: _Fixture) -> None:
    north = fixture.portfolio("NORTH")
    south = fixture.portfolio("SOUTH")
    head = _person(fixture, "head", "risk_head", north, south)
    north_rm = _person(fixture, "northrm", "relationship_manager", north)
    south_rm = _person(fixture, "southrm", "relationship_manager", south)
    borrower = fixture.borrower(north, "B-NORTH")
    version = fixture.covenant_version(borrower, threshold=Decimal("3"), direction="max")
    fixture.seed_test(version, as_of_date=_YESTERDAY - timedelta(days=1), value=Decimal("2.0"))

    _night(fixture, {version.id: Decimal("2.0")}, _YESTERDAY)
    _night(fixture, {version.id: Decimal("3.5")}, _TODAY, minutes=5)

    def band_notices(person: UUID) -> list[Notification]:
        return [n for n in _inbox(fixture, person) if n.template == "band_change"]

    assert [n.payload["borrower_reference"] for n in band_notices(head)] == ["B-NORTH"]
    assert [n.payload["borrower_reference"] for n in band_notices(north_rm)] == ["B-NORTH"]
    assert band_notices(south_rm) == [], "never disclosed outside the RM's portfolios"
    assert "Act now" in band_notices(head)[0].payload["summary"]
    [notice] = band_notices(head)
    assert deep_link(notice.subject_type, notice.subject_id, notice.payload) == (
        "/borrowers/B-NORTH"
    ), "Open lands on the case file by reference, not an unrouted id"


def test_each_review_sends_one_summary_and_reruns_add_nothing(fixture: _Fixture) -> None:
    book = fixture.portfolio("BOOK")
    head = _person(fixture, "head", "risk_head", book)
    borrower = fixture.borrower(book, "B-BOOK")
    version = fixture.covenant_version(borrower)
    fixture.seed_test(version, as_of_date=_YESTERDAY, value=Decimal("2.0"))
    service = fixture.build_service(
        statement_lines=_lines_provider({version.id: Decimal("2.1")}),
        what_changed_thresholds=_CHANGES,
    )
    runner = fixture.build_runner(service)

    run_nightly_pipeline(runner, trigger="manual", run_id="rq-summary", as_of=_TODAY.isoformat())
    run_nightly_pipeline(runner, trigger="manual", run_id="rq-summary", as_of=_TODAY.isoformat())

    summaries = [n for n in _inbox(fixture, head) if n.template == "morning_queue"]
    assert len(summaries) == 1
    assert "1 borrowers in your portfolios" in summaries[0].payload["summary"]
    assert "first review" in summaries[0].payload["entries"]
    assert summaries[0].state == "pending", "visible in the centre straight away"


def test_a_second_review_the_same_day_does_not_repeat_the_summary(fixture: _Fixture) -> None:
    book = fixture.portfolio("DAILY")
    head = _person(fixture, "head", "risk_head", book)
    borrower = fixture.borrower(book, "B-DAILY")
    version = fixture.covenant_version(borrower)
    fixture.seed_test(version, as_of_date=_YESTERDAY - timedelta(days=1), value=Decimal("2.0"))

    _night(fixture, {version.id: Decimal("2.0")}, _YESTERDAY)
    _night(fixture, {version.id: Decimal("2.1")}, _TODAY, minutes=5)
    _night(fixture, {version.id: Decimal("2.1")}, _TODAY, minutes=10)

    dates = [
        n.payload["review_date"] for n in _inbox(fixture, head) if n.template == "morning_queue"
    ]
    assert sorted(dates) == [_YESTERDAY.isoformat(), _TODAY.isoformat()]


def test_a_first_review_does_not_flood_the_desk(fixture: _Fixture) -> None:
    book = fixture.portfolio("FIRST")
    head = _person(fixture, "head", "risk_head", book)
    for index in range(3):
        borrower = fixture.borrower(book, f"B-FIRST-{index}")
        version = fixture.covenant_version(borrower, threshold=Decimal("3"), direction="max")
        fixture.seed_test(version, as_of_date=_YESTERDAY, value=Decimal("3.5"))

    _night(fixture, {}, _TODAY)

    templates = [n.template for n in _inbox(fixture, head)]
    assert templates == ["morning_queue"], templates


def test_an_overdue_case_escalates_and_tells_its_assignee(fixture: _Fixture) -> None:
    book = fixture.portfolio("SLA")
    rm = _person(fixture, "rm", "relationship_manager", book)
    borrower = fixture.borrower(book, "B-SLA")
    version = fixture.covenant_version(borrower)
    fixture.seed_test(version, as_of_date=_YESTERDAY, value=Decimal("2.0"))
    with fixture.session_factory() as session:
        session.add(
            Case(
                id=uuid4(),
                reference="CASE-LATE",
                borrower_id=borrower.id,
                state="open",
                band_at_open="act",
                assignee_id=rm,
                due_at=_NOW - timedelta(hours=1),
                sla_hours=24,
                created_at=_NOW - timedelta(days=2),
                updated_at=_NOW - timedelta(days=2),
                request_id="rq-late-case",
            )
        )
        session.commit()

    _night(fixture, {version.id: Decimal("2.1")}, _TODAY)

    [case] = fixture.cases(borrower_id=borrower.id)
    assert case.state == "escalated"
    assert [n.template for n in _inbox(fixture, rm) if n.template == "sla_breach"] == ["sla_breach"]


def test_pipeline_cases_carry_their_sla(fixture: _Fixture) -> None:
    book = fixture.portfolio("DUE")
    rm = _person(fixture, "rm", "relationship_manager", book)
    borrower = fixture.borrower(book, "B-DUE")
    version = fixture.covenant_version(borrower, threshold=Decimal("3"), direction="max")
    fixture.seed_test(version, as_of_date=_YESTERDAY, value=Decimal("2.0"))
    from covenant_radar.services.nightly import NightlyPipelineService
    from tests.integration.test_nightly_pipeline import _FakeThresholdStore

    store = _FakeThresholdStore(uuid4())
    store._values["T11"] = {"act_sla_hours": 24, "amber_sla_hours": 72, "watch_sla_hours": 168}
    service = fixture.build_service(
        statement_lines=_lines_provider({version.id: Decimal("3.5")}),
        threshold_store=store,
        default_assignee_id=rm,
    )
    assert isinstance(service, NightlyPipelineService)
    run_nightly_pipeline(fixture.build_runner(service), trigger="manual", as_of=_TODAY.isoformat())

    [case] = fixture.cases(borrower_id=borrower.id)
    assert case.sla_hours == 24
    assert case.due_at is not None
    assert case.due_at.astimezone(UTC) == datetime(2026, 9, 1, 1, 0, tzinfo=UTC)


def test_a_halted_review_tells_the_administrators_once(fixture: _Fixture) -> None:
    admin = _person(fixture, "admin", "administrator")
    service = fixture.build_service()
    result = PipelineRunResult(
        run_id="rq-broken",
        borrower_id=None,
        completed_steps=("nightly.ingest",),
        failed_step="nightly.test",
        runs=(),
    )

    assert service.notify_pipeline_failure(result) == 1
    assert service.notify_pipeline_failure(result) == 0

    [notice] = _inbox(fixture, admin)
    assert notice.template == "job_failure"
    assert notice.payload["job_name"] == "nightly.test"
