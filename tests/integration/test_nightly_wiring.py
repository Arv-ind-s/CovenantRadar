"""The nightly pipeline's hand-off to the queue: every borrower is ranked
(`spec §R-14.c`), what-changed is recorded (`spec §R-14.d`), a return to the
act band is announced, and a single-borrower run never shrinks the queue."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from covenant_radar.core.clock import FixedClock
from covenant_radar.db.models import AppUser, Case, Facility
from covenant_radar.db.models.facility import FacilityConduct
from covenant_radar.domain.covenants.exceptions import (
    normalise_period,
    period_bounds_for_label,
    period_label_for_date,
)
from covenant_radar.domain.triage.changes import ChangeThresholds
from covenant_radar.scheduler.pipeline import run_nightly_pipeline
from covenant_radar.services.financial_pdf import _fy_label
from tests.integration.test_nightly_pipeline import (
    _NOW,
    _TODAY,
    _YESTERDAY,
    _Fixture,
    _lines_provider,
    _score_run,
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


def _run(
    fixture: _Fixture,
    lines: dict[UUID, Decimal],
    *,
    as_of: date,
    minutes: int = 0,
    borrower_id: UUID | None = None,
    assignee_id: UUID | None = None,
) -> UUID:
    service = fixture.build_service(
        statement_lines=_lines_provider(lines),
        clock=FixedClock(_NOW + timedelta(minutes=minutes)),
        what_changed_thresholds=_CHANGES,
        default_assignee_id=assignee_id,
    )
    result = run_nightly_pipeline(
        fixture.build_runner(service),
        trigger="manual",
        as_of=as_of.isoformat(),
        borrower_id=borrower_id,
    )
    assert result.success is True, result
    return UUID(_score_run(result.runs).metrics["forecast_run_id"])


def _assignee(fixture: _Fixture) -> UUID:
    assignee_id = uuid4()
    with fixture.session_factory() as session:
        session.add(
            AppUser(
                id=assignee_id,
                username="rm",
                email="rm@example.com",
                full_name="Relationship Manager",
                auth_source="local",
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-rm",
            )
        )
        session.commit()
    return assignee_id


def test_borrower_without_a_forecast_is_ranked_with_its_reason(fixture: _Fixture) -> None:
    portfolio = fixture.portfolio("ALL")
    scored = fixture.borrower(portfolio, "B-SCORED")
    unscored = fixture.borrower(portfolio, "B-NO-DATA")
    version = fixture.covenant_version(scored)
    fixture.covenant_version(unscored)  # live, but no statement and no history
    fixture.seed_test(version, as_of_date=_YESTERDAY, value=Decimal("2.0"))

    run_id = _run(fixture, {version.id: Decimal("2.1")}, as_of=_TODAY)

    entries = sorted(fixture.triage_entries(run_id), key=lambda entry: entry.rank)
    assert [entry.borrower_id for entry in entries] == [scored.id, unscored.id]
    missing = entries[-1]
    assert missing.worst_covenant_version_id is None
    assert missing.probability is None and missing.urgency is None
    assert missing.band == "watch"


def test_what_changed_names_the_prior_and_new_band(fixture: _Fixture) -> None:
    portfolio = fixture.portfolio("CHANGE")
    borrower = fixture.borrower(portfolio, "B-CHANGE")
    version = fixture.covenant_version(borrower, threshold=Decimal("3"), direction="max")
    fixture.seed_test(version, as_of_date=_YESTERDAY - timedelta(days=1), value=Decimal("2.0"))

    first = _run(fixture, {version.id: Decimal("2.0")}, as_of=_YESTERDAY)
    second = _run(fixture, {version.id: Decimal("3.5")}, as_of=_TODAY, minutes=5)

    [before] = fixture.triage_entries(first)
    [after] = fixture.triage_entries(second)
    assert before.band != "act" and after.band == "act"
    assert after.what_changed is not None
    assert before.band in after.what_changed and "act" in after.what_changed


def test_return_to_act_alerts_the_open_case(fixture: _Fixture) -> None:
    assignee_id = _assignee(fixture)
    portfolio = fixture.portfolio("ESCALATE")
    borrower = fixture.borrower(portfolio, "B-ESCALATE")
    version = fixture.covenant_version(borrower, threshold=Decimal("3"), direction="max")
    fixture.seed_test(version, as_of_date=_YESTERDAY - timedelta(days=1), value=Decimal("2.0"))
    _run(fixture, {version.id: Decimal("2.0")}, as_of=_YESTERDAY, assignee_id=assignee_id)
    with fixture.session_factory() as session:
        session.add(
            Case(
                id=uuid4(),
                reference="CASE-MONITOR",
                borrower_id=borrower.id,
                state="monitoring",
                band_at_open="act",
                assignee_id=assignee_id,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-monitoring-case",
            )
        )
        session.commit()

    _run(
        fixture,
        {version.id: Decimal("3.5")},
        as_of=_TODAY,
        minutes=5,
        assignee_id=assignee_id,
    )

    notifications = fixture.notifications()
    assert len(fixture.cases(borrower_id=borrower.id)) == 1, "no second case is opened"
    assert [n.payload["case_reference"] for n in notifications] == ["CASE-MONITOR"]
    assert "back into the act band" in notifications[0].payload["summary"]


def test_single_borrower_run_keeps_the_rest_of_the_book(fixture: _Fixture) -> None:
    portfolio = fixture.portfolio("CARRY")
    one = fixture.borrower(portfolio, "B-ONE")
    two = fixture.borrower(portfolio, "B-TWO")
    version_one = fixture.covenant_version(one)
    version_two = fixture.covenant_version(two)
    for version in (version_one, version_two):
        fixture.seed_test(version, as_of_date=_YESTERDAY, value=Decimal("2.0"))
    lines = {version_one.id: Decimal("2.1"), version_two.id: Decimal("2.2")}
    full = _run(fixture, lines, as_of=_TODAY)
    [carried_before] = [e for e in fixture.triage_entries(full) if e.borrower_id == one.id]

    recheck = _run(fixture, lines, as_of=_TODAY, minutes=5, borrower_id=two.id)

    entries = {entry.borrower_id: entry for entry in fixture.triage_entries(recheck)}
    assert set(entries) == {one.id, two.id}, "the re-ranked queue still covers the whole book"
    assert entries[one.id].probability == carried_before.probability
    assert sorted(entry.rank for entry in entries.values()) == [1, 2]

    # The carried row still finds its forecast details for the queue screen.
    from covenant_radar.db.repositories.triage import TriageRepository
    from covenant_radar.db.scoping import Scope
    from covenant_radar.web.view_models.queue import _crossing_dates, _source_runs

    scope = Scope(principal_id=uuid4(), descendant_paths=(portfolio.path,))
    with fixture.session_factory() as session:
        page = TriageRepository(session).query(scope)
        sources = dict(_source_runs(session, page))
        _crossing_dates(session, page)  # resolves without error across both runs
    assert page.run_id == recheck
    assert sources[version_one.id] == full, "the carried row reads the run that scored it"
    assert sources[version_two.id] == recheck


def test_single_borrower_recheck_acts_only_on_that_borrower(fixture: _Fixture) -> None:
    assignee_id = _assignee(fixture)
    portfolio = fixture.portfolio("SCOPED")
    one = fixture.borrower(portfolio, "B-ACT-OTHER")
    two = fixture.borrower(portfolio, "B-RECHECKED")
    version_one = fixture.covenant_version(one, threshold=Decimal("3"), direction="max")
    version_two = fixture.covenant_version(two, threshold=Decimal("3"), direction="max")
    for version in (version_one, version_two):
        fixture.seed_test(version, as_of_date=_YESTERDAY, value=Decimal("2.0"))
    lines = {version_one.id: Decimal("3.5"), version_two.id: Decimal("2.1")}
    _run(fixture, lines, as_of=_TODAY, assignee_id=assignee_id)
    [opened] = fixture.cases(borrower_id=one.id)
    with fixture.session_factory() as session:
        case = session.get(Case, opened.id)
        assert case is not None
        case.state = "closed"
        session.commit()

    def alerts_for_one() -> int:
        return sum(
            1
            for notification in fixture.notifications()
            if notification.payload["borrower_reference"] == "B-ACT-OTHER"
        )

    before = alerts_for_one()

    _run(fixture, lines, as_of=_TODAY, minutes=5, borrower_id=two.id, assignee_id=assignee_id)

    assert len(fixture.cases(borrower_id=one.id)) == 1, "no case reopened for another borrower"
    assert alerts_for_one() == before, "no alert raised for a borrower not being rechecked"


def test_sma_band_ignores_a_facility_closed_today(fixture: _Fixture) -> None:
    portfolio = fixture.portfolio("SMA")
    borrower = fixture.borrower(portfolio, "B-SMA")
    fixture.covenant_version(borrower)
    with fixture.session_factory() as session:
        live = session.execute(
            select(Facility).where(Facility.borrower_id == borrower.id)
        ).scalar_one()
        session.add(
            Facility(
                id=uuid4(),
                reference="F-CLOSED-TODAY",
                borrower_id=borrower.id,
                facility_type="cash_credit",
                sanctioned_limit=Decimal("100"),
                currency="INR",
                outstanding=Decimal("0"),
                sanction_date=date(2024, 1, 1),
                effective_from=date(2024, 1, 1),
                effective_to=_TODAY,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-closed-facility",
            )
        )
        session.add(
            FacilityConduct(
                id=uuid4(),
                facility_id=live.id,
                as_of_date=_TODAY,
                days_past_due=0,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-live-conduct",
            )
        )
        session.commit()

    service = fixture.build_service()
    with fixture.session_factory() as session:
        band = service._borrower_sma_band(session, borrower.id, _TODAY)

    assert band is not None, "a facility closed today must not make the band unknown"


def test_derived_period_labels_are_canonical() -> None:
    # The Indian financial year (April start), as `format_fy_label` renders it.
    assert period_label_for_date(date(2026, 6, 30)) == "FY27Q1"
    assert period_label_for_date(date(2026, 10, 2)) == "FY27Q3"
    assert period_label_for_date(date(2027, 3, 31)) == "FY27Q4"
    assert _fy_label(date(2026, 6, 30)) == "FY27Q1"
    assert normalise_period(_fy_label(date(2025, 3, 31))) == "FY25Q4"
    assert period_bounds_for_label("FY27Q4") == (date(2027, 1, 1), date(2027, 3, 31))
    assert period_label_for_date(date(2026, 2, 1), fiscal_year_start_month=1) == "FY26Q1"
    from covenant_radar.i18n.formatting import format_fy_label

    for day in (date(2026, 1, 15), date(2026, 4, 1), date(2026, 9, 30), date(2026, 12, 31)):
        assert period_label_for_date(day) == format_fy_label(day)
        first, last = period_bounds_for_label(period_label_for_date(day))
        assert first <= day <= last


def test_band_change_tile_counts_band_moves_only(fixture: _Fixture) -> None:
    from covenant_radar.db.repositories.triage import TriageRepository
    from covenant_radar.db.scoping import Scope

    portfolio = fixture.portfolio("TILE")
    mover = fixture.borrower(portfolio, "B-MOVER")
    steady = fixture.borrower(portfolio, "B-STEADY")
    moving = fixture.covenant_version(mover, threshold=Decimal("3"), direction="max")
    holding = fixture.covenant_version(steady, threshold=Decimal("3"), direction="max")
    for version in (moving, holding):
        fixture.seed_test(version, as_of_date=_YESTERDAY - timedelta(days=1), value=Decimal("2.0"))
    scope = Scope(principal_id=uuid4(), descendant_paths=(portfolio.path,))

    _run(fixture, {moving.id: Decimal("2.0"), holding.id: Decimal("2.0")}, as_of=_YESTERDAY)
    with fixture.session_factory() as session:
        first = TriageRepository(session).summary(scope)
    _run(
        fixture,
        {moving.id: Decimal("3.5"), holding.id: Decimal("2.0")},
        as_of=_TODAY,
        minutes=5,
    )
    with fixture.session_factory() as session:
        second = TriageRepository(session).summary(scope)

    assert first.what_changed == 0, "a first review has nothing to compare against"
    assert second.what_changed == 1
