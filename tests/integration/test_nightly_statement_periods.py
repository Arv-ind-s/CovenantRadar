"""The nightly test step against dated statement periods.

A statement is tested once per period, under that period's label (so its
exception applies), and dated at the period end in the forecast.  Re-testing
the same statement every night used to restamp old data as today's: the trend
flattened, the stale-data guard never fired, exceptions were never looked up
and a breach's cure window moved forward every night.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from covenant_radar.core.clock import FixedClock
from covenant_radar.db.models import Borrower, CovenantTest, CovenantVersion
from covenant_radar.db.models.covenant import CovenantException
from covenant_radar.db.models.forecast import Forecast
from covenant_radar.db.models.statements import FinancialPeriod
from covenant_radar.domain.covenants.evaluate import (
    CovenantEvaluation,
    continue_cure_window,
)
from covenant_radar.domain.forecast import Observation
from covenant_radar.scheduler.pipeline import run_nightly_pipeline
from covenant_radar.services.nightly import StatementSnapshot
from tests.integration.test_nightly_pipeline import _NOW, _Fixture

pytestmark = pytest.mark.integration


@pytest.fixture
def fixture(tmp_path: Path) -> Iterator[_Fixture]:
    built = _Fixture(tmp_path)
    try:
        yield built
    finally:
        built.close()


def _period(fixture: _Fixture, borrower: Borrower, label: str, end: date) -> FinancialPeriod:
    with fixture.session_factory() as session:
        period = FinancialPeriod(
            id=uuid4(),
            borrower_id=borrower.id,
            fy_label=label,
            period_type="quarterly",
            period_start=end - timedelta(days=89),
            period_end=end,
            is_complete=True,
            is_audited=True,
            created_at=_NOW,
            updated_at=_NOW,
            request_id=f"rq-period-{label.lower()}",
        )
        session.add(period)
        session.commit()
        return period


def _provider(
    periods: dict[UUID, list[tuple[FinancialPeriod, Decimal]]],
) -> Callable[[CovenantVersion, date], StatementSnapshot | None]:
    """The latest period ending on or before the run date, like the runtime."""

    def provider(version: CovenantVersion, as_of: date) -> StatementSnapshot | None:
        available = [
            (period, debt)
            for period, debt in periods.get(version.id, [])
            if period.period_end <= as_of
        ]
        if not available:
            return None
        period, debt = max(available, key=lambda item: item[0].period_end)
        return StatementSnapshot(
            lines={"total_debt": debt, "tangible_net_worth": Decimal("1")},
            period_id=period.id,
            period_label=period.fy_label,
            period_end=period.period_end,
        )

    return provider


def _night(fixture: _Fixture, provider: object, as_of: date) -> dict[str, object]:
    service = fixture.build_service(statement_lines=provider, clock=FixedClock(_NOW))
    result = run_nightly_pipeline(
        fixture.build_runner(service), trigger="manual", as_of=as_of.isoformat()
    )
    assert result.success is True, result
    return {run.job_name: run.metrics for run in result.runs}


def _tests(fixture: _Fixture, version: CovenantVersion) -> list[CovenantTest]:
    with fixture.session_factory() as session:
        return list(
            session.execute(
                select(CovenantTest)
                .where(CovenantTest.covenant_version_id == version.id)
                .order_by(CovenantTest.as_of_date, CovenantTest.computed_at)
            )
            .scalars()
            .all()
        )


def test_a_statement_is_tested_once_under_its_own_period(fixture: _Fixture) -> None:
    borrower = fixture.borrower(fixture.portfolio("ONCE"), "B-ONCE")
    version = fixture.covenant_version(borrower, threshold=Decimal("3"))
    june = _period(fixture, borrower, "FY27Q1", date(2026, 6, 30))
    provider = _provider({version.id: [(june, Decimal("2.0"))]})

    _night(fixture, provider, date(2026, 8, 30))
    _night(fixture, provider, date(2026, 8, 31))

    [test] = _tests(fixture, version)
    assert test.period_id == june.id
    assert test.inputs["period_label"] == "FY27Q1"


def test_a_new_statement_period_is_tested(fixture: _Fixture) -> None:
    borrower = fixture.borrower(fixture.portfolio("NEXT"), "B-NEXT")
    version = fixture.covenant_version(borrower, threshold=Decimal("3"))
    june = _period(fixture, borrower, "FY27Q1", date(2026, 6, 30))
    sept = _period(fixture, borrower, "FY27Q2", date(2026, 9, 30))
    provider = _provider({version.id: [(june, Decimal("2.0")), (sept, Decimal("2.4"))]})

    _night(fixture, provider, date(2026, 8, 30))
    _night(fixture, provider, date(2026, 11, 2))

    assert [test.period_id for test in _tests(fixture, version)] == [june.id, sept.id]


def test_the_period_exception_applies_to_the_nightly_test(fixture: _Fixture) -> None:
    borrower = fixture.borrower(fixture.portfolio("EXC"), "B-EXC")
    version = fixture.covenant_version(borrower, threshold=Decimal("3"))
    june = _period(fixture, borrower, "FY27Q1", date(2026, 6, 30))
    with fixture.session_factory() as session:
        exception = CovenantException(
            id=uuid4(),
            covenant_version_id=version.id,
            from_period="FY27Q1",
            to_period="FY27Q1",
            relaxed_threshold=Decimal("4.0"),
            reason="Approved holiday for the June quarter",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-exception",
        )
        session.add(exception)
        session.commit()
    provider = _provider({version.id: [(june, Decimal("3.5"))]})

    _night(fixture, provider, date(2026, 8, 30))

    [test] = _tests(fixture, version)
    assert test.exception_id == exception.id
    assert test.threshold_used == Decimal("4.0")
    assert test.verdict != "breach", "3.5x passes the relaxed 4.0x limit"


def _exception(fixture: _Fixture, version: CovenantVersion, to_period: str) -> None:
    with fixture.session_factory() as session:
        session.add(
            CovenantException(
                id=uuid4(),
                covenant_version_id=version.id,
                from_period="FY27Q1",
                to_period=to_period,
                relaxed_threshold=Decimal("4.0"),
                reason="Approved covenant holiday",
                created_at=_NOW,
                updated_at=_NOW,
                request_id=f"rq-exception-{to_period.lower()}",
            )
        )
        session.commit()


def _forecasts(fixture: _Fixture, version: CovenantVersion) -> dict[int, Forecast]:
    """The latest run's forecasts for one covenant, by horizon."""

    from covenant_radar.db.models.forecast import ForecastRun

    with fixture.session_factory() as session:
        rows = session.execute(
            select(Forecast)
            .join(ForecastRun, ForecastRun.id == Forecast.run_id)
            .where(Forecast.covenant_version_id == version.id)
            .order_by(ForecastRun.as_of_date)
        ).scalars()
        return {row.horizon_days: row for row in rows}


def test_the_forecast_follows_the_exception_window(fixture: _Fixture) -> None:
    covered = fixture.borrower(fixture.portfolio("COVERED"), "B-COVERED")
    lapsing = fixture.borrower(fixture.portfolio("LAPSING"), "B-LAPSING")
    covered_version = fixture.covenant_version(covered, threshold=Decimal("3"))
    lapsing_version = fixture.covenant_version(lapsing, threshold=Decimal("3"))
    _exception(fixture, covered_version, "FY27Q3")  # relaxed to the end of December
    _exception(fixture, lapsing_version, "FY27Q2")  # relaxed to the end of September
    provider = _provider(
        {
            covered_version.id: [
                (_period(fixture, covered, "FY26Q4", date(2026, 3, 31)), Decimal("3.5")),
                (_period(fixture, covered, "FY27Q1", date(2026, 6, 30)), Decimal("3.5")),
            ],
            lapsing_version.id: [
                (_period(fixture, lapsing, "FY26Q4", date(2026, 3, 31)), Decimal("3.5")),
                (_period(fixture, lapsing, "FY27Q1", date(2026, 6, 30)), Decimal("3.5")),
            ],
        }
    )
    _night(fixture, provider, date(2026, 5, 1))

    _night(fixture, provider, date(2026, 8, 30))

    still_covered = _forecasts(fixture, covered_version)
    assert all(row.projected_cross_date is None for row in still_covered.values()), (
        "3.5x is inside the relaxed 4.0x limit for the whole 90 days"
    )
    reverting = _forecasts(fixture, lapsing_version)
    assert reverting[90].projected_cross_date == date(2026, 10, 1), (
        "the base 3.0x limit returns when the exception ends on 30 September"
    )
    assert reverting[30].projected_cross_date is None


def test_simulator_baseline_matches_a_dated_crossing(fixture: _Fixture) -> None:
    from covenant_radar.db.models import Portfolio
    from covenant_radar.db.scoping import Scope
    from covenant_radar.web.routes.simulator import _compare
    from covenant_radar.web.view_models.simulation import load_simulation_context

    borrower = fixture.borrower(fixture.portfolio("SIMDATE"), "B-SIMDATE")
    version = fixture.covenant_version(borrower, threshold=Decimal("3"))
    _exception(fixture, version, "FY27Q2")
    provider = _provider(
        {
            version.id: [
                (_period(fixture, borrower, "FY26Q4", date(2026, 3, 31)), Decimal("3.4")),
                (_period(fixture, borrower, "FY27Q1", date(2026, 6, 30)), Decimal("3.5")),
            ]
        }
    )
    _night(fixture, provider, date(2026, 5, 1))
    _night(fixture, provider, date(2026, 8, 30))

    with fixture.session_factory() as session:
        paths = session.execute(select(Portfolio.path)).scalars().all()
        scope = Scope(principal_id=uuid4(), descendant_paths=tuple(paths))
        from covenant_radar.db.models.forecast import ForecastRun

        forecast = (
            session.execute(
                select(Forecast)
                .join(ForecastRun, ForecastRun.id == Forecast.run_id)
                .where(Forecast.covenant_version_id == version.id, Forecast.horizon_days == 90)
                .order_by(ForecastRun.as_of_date.desc())
            )
            .scalars()
            .first()
        )
        assert forecast is not None
        assert forecast.projected_cross_date is not None, "the scenario must cross"
        assert forecast.data_as_of == date(2026, 6, 30)
        context = load_simulation_context(session, forecast.id, scope=scope)
        comparisons = _compare(context, (), {}, None)
        for row in context.forecasts:
            baseline = comparisons[row.id].baseline
            assert baseline.crossing_date == row.projected_cross_date, row.horizon_days
            assert baseline.probability is not None and row.probability is not None
            assert baseline.probability.quantize(Decimal("0.0001")) == row.probability


def test_forecast_dates_the_value_at_the_period_end_and_counts_overdue_days(
    fixture: _Fixture,
) -> None:
    borrower = fixture.borrower(fixture.portfolio("STALE"), "B-STALE")
    version = fixture.covenant_version(borrower, threshold=Decimal("3"))
    march = _period(fixture, borrower, "FY26Q4", date(2026, 3, 31))
    june = _period(fixture, borrower, "FY27Q1", date(2026, 6, 30))
    provider = _provider({version.id: [(march, Decimal("2.0")), (june, Decimal("2.2"))]})
    _night(fixture, provider, date(2026, 5, 1))

    current = _night(fixture, provider, date(2026, 10, 2))  # 94 days: within 90 + 60
    with fixture.session_factory() as session:
        run_id = UUID(str(current["nightly.score"]["forecast_run_id"]))
        row = session.execute(select(Forecast).where(Forecast.run_id == run_id)).scalars().first()
    assert row is not None
    assert row.data_as_of == date(2026, 6, 30)
    assert row.staleness_days == 0

    overdue = _night(fixture, provider, date(2026, 12, 7))  # 160 days: 10 overdue
    with fixture.session_factory() as session:
        run_id = UUID(str(overdue["nightly.score"]["forecast_run_id"]))
        row = session.execute(select(Forecast).where(Forecast.run_id == run_id)).scalars().first()
    assert row is not None and row.staleness_days == 10
    assert overdue["nightly.test"]["marked_stale"] == 1
    assert [test.verdict for test in _tests(fixture, version)][-1] == "stale"

    again = _night(fixture, provider, date(2026, 12, 8))
    assert again["nightly.test"]["marked_stale"] == 0, "a missing statement is marked once"


def test_a_continuing_breach_keeps_its_first_cure_window(fixture: _Fixture) -> None:
    borrower = fixture.borrower(fixture.portfolio("CURE"), "B-CURE")
    version = fixture.covenant_version(borrower, threshold=Decimal("3"))
    with fixture.session_factory() as session:
        stored = session.get(CovenantVersion, version.id)
        assert stored is not None
        stored.cure_days = 30
        session.commit()
    june = _period(fixture, borrower, "FY27Q1", date(2026, 6, 30))
    sept = _period(fixture, borrower, "FY27Q2", date(2026, 9, 30))
    provider = _provider({version.id: [(june, Decimal("3.4")), (sept, Decimal("3.5"))]})

    _night(fixture, provider, date(2026, 8, 1))
    _night(fixture, provider, date(2026, 8, 20))  # a retest of a still-open window
    _night(fixture, provider, date(2026, 11, 1))  # next quarter, still in breach

    first, second = _tests(fixture, version)
    assert first.verdict == "breach_cure_open" and first.cure_ends_on == date(2026, 8, 31)
    assert second.verdict == "breach" and second.cure_ends_on is None, (
        "the cure window lapsed on 31 Aug; a later breach must not reopen it"
    )


def test_cure_window_rules() -> None:
    open_breach = CovenantEvaluation(
        value=Decimal("3.4"),
        threshold_used=Decimal("3"),
        headroom_pct=Decimal("-13"),
        verdict="breach_cure_open",
        cure_ends_on=date(2026, 9, 30),
    )
    kept = continue_cure_window(
        open_breach,
        prior_verdict="breach_cure_open",
        prior_cure_ends_on=date(2026, 8, 31),
        test_date=date(2026, 8, 20),
    )
    assert kept.verdict == "breach_cure_open" and kept.cure_ends_on == date(2026, 8, 31)
    lapsed = continue_cure_window(
        open_breach,
        prior_verdict="breach_cure_open",
        prior_cure_ends_on=date(2026, 8, 31),
        test_date=date(2026, 9, 1),
    )
    assert lapsed.verdict == "breach" and lapsed.cure_ends_on is None
    fresh = continue_cure_window(
        open_breach, prior_verdict="pass", prior_cure_ends_on=None, test_date=date(2026, 9, 1)
    )
    assert fresh is open_breach


def test_daily_copies_of_one_statement_are_one_observation(fixture: _Fixture) -> None:
    borrower = fixture.borrower(fixture.portfolio("COPIES"), "B-COPIES")
    version = fixture.covenant_version(borrower, threshold=Decimal("3"))
    quarter_ends = (date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30))
    for index, end in enumerate(quarter_ends):
        fixture.seed_test(version, as_of_date=end, value=Decimal("2.0") + Decimal(index) / 10)
    for offset in range(1, 40):  # an older nightly's daily re-tests of June
        fixture.seed_test(
            version, as_of_date=date(2026, 6, 30) + timedelta(days=offset), value=Decimal("2.2")
        )

    service = fixture.build_service()
    with fixture.session_factory() as session:
        history = service._test_history(session, version.id, date(2026, 8, 31))
        series = service._observations(session, history, statement_based=True)

    assert [item.observed_on for item in series] == list(quarter_ends)
    assert all(isinstance(item, Observation) for item in series)


def test_a_lapsed_cure_window_becomes_a_breach_without_a_new_statement(
    fixture: _Fixture,
) -> None:
    borrower = fixture.borrower(fixture.portfolio("LAPSE"), "B-LAPSE")
    version = fixture.covenant_version(borrower, threshold=Decimal("3"))
    with fixture.session_factory() as session:
        stored = session.get(CovenantVersion, version.id)
        assert stored is not None
        stored.cure_days = 30
        session.commit()
    june = _period(fixture, borrower, "FY27Q1", date(2026, 6, 30))
    provider = _provider({version.id: [(june, Decimal("3.4"))]})

    _night(fixture, provider, date(2026, 8, 1))  # breach, cure open to 31 Aug
    _night(fixture, provider, date(2026, 8, 31))  # still inside the window
    _night(fixture, provider, date(2026, 9, 1))  # window over, no new statement
    _night(fixture, provider, date(2026, 9, 2))

    verdicts = [(test.as_of_date, test.verdict) for test in _tests(fixture, version)]
    assert verdicts == [
        (date(2026, 8, 1), "breach_cure_open"),
        (date(2026, 9, 1), "breach"),
    ]


def test_an_observed_breach_is_not_hidden_by_an_overdue_statement(fixture: _Fixture) -> None:
    borrower = fixture.borrower(fixture.portfolio("OVERDUE"), "B-OVERDUE")
    version = fixture.covenant_version(borrower, threshold=Decimal("3"))
    march = _period(fixture, borrower, "FY26Q4", date(2026, 3, 31))
    june = _period(fixture, borrower, "FY27Q1", date(2026, 6, 30))
    provider = _provider({version.id: [(march, Decimal("3.4")), (june, Decimal("3.5"))]})
    _night(fixture, provider, date(2026, 5, 1))
    _night(fixture, provider, date(2026, 8, 1))

    _night(fixture, provider, date(2026, 12, 7))  # the next statement is 10 days overdue

    rows = _forecasts(fixture, version)
    assert rows[90].staleness_days == 10, "the true staleness is recorded"
    assert rows[90].probability is not None, "the breach is still shown"
    assert rows[90].below_confidence_floor is False


def test_simulator_holds_a_tested_breach_like_the_forecast(fixture: _Fixture) -> None:
    """A breach on the latest test stays at the clamp even when the trend has
    since carried the path back under the limit; the do-nothing baseline must
    agree (`spec §R-12.f`, `§R-15.a`)."""

    from covenant_radar.db.models import Portfolio
    from covenant_radar.db.models.forecast import ForecastRun
    from covenant_radar.db.scoping import Scope
    from covenant_radar.web.routes.simulator import _compare
    from covenant_radar.web.view_models.simulation import load_simulation_context

    borrower = fixture.borrower(fixture.portfolio("IMPROVING"), "B-IMPROVING")
    version = fixture.covenant_version(borrower, threshold=Decimal("3"))
    provider = _provider(
        {
            version.id: [
                (_period(fixture, borrower, "FY26Q4", date(2026, 3, 31)), Decimal("3.40")),
                (_period(fixture, borrower, "FY27Q1", date(2026, 6, 30)), Decimal("3.05")),
            ]
        }
    )
    _night(fixture, provider, date(2026, 5, 1))
    _night(fixture, provider, date(2026, 9, 15))  # the trend is now under 3.0x

    with fixture.session_factory() as session:
        paths = session.execute(select(Portfolio.path)).scalars().all()
        scope = Scope(principal_id=uuid4(), descendant_paths=tuple(paths))
        forecast = (
            session.execute(
                select(Forecast)
                .join(ForecastRun, ForecastRun.id == Forecast.run_id)
                .where(Forecast.covenant_version_id == version.id, Forecast.horizon_days == 30)
                .order_by(ForecastRun.as_of_date.desc())
            )
            .scalars()
            .first()
        )
        assert forecast is not None and forecast.probability == Decimal("0.99")
        context = load_simulation_context(session, forecast.id, scope=scope)
        comparisons = _compare(context, (), {}, None)
        for row in context.forecasts:
            baseline = comparisons[row.id].baseline
            assert baseline.probability is not None and row.probability is not None
            assert baseline.probability.quantize(Decimal("0.0001")) == row.probability
