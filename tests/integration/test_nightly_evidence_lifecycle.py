"""The nightly evidence pass: a warning counts while it is current.

Persistence is measured inside the T3 window, a later healthy observation
retires a warning, and a sustained warning decays from its last adverse day.
Before this, one sustained streak at any point in history kept a borrower's
evidence at full pressure for good.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from covenant_radar.db.models import Borrower, Facility
from covenant_radar.db.models.signal import EvidenceItem
from covenant_radar.domain.signals import SignalEvent
from covenant_radar.scheduler.pipeline import run_nightly_pipeline
from tests.integration.test_nightly_pipeline import (
    _TODAY,
    _YESTERDAY,
    _FakeThresholdStore,
    _Fixture,
    _lines_provider,
)

pytestmark = pytest.mark.integration


class _Thresholds(_FakeThresholdStore):
    def __init__(self, *, decay_rate: Decimal | None) -> None:
        super().__init__(uuid4())
        t3: dict[str, object] = {
            "sustained_days": 14,
            "sustained_events": 3,
            "event_window_days": 30,
        }
        if decay_rate is not None:
            t3["decay_rate"] = decay_rate
        self._values["T3"] = t3
        self._values["T4"] = {"headroom_erosion_pct": Decimal("0.05")}


@pytest.fixture
def fixture(tmp_path: Path) -> Iterator[_Fixture]:
    built = _Fixture(tmp_path)
    try:
        yield built
    finally:
        built.close()


def _late_payments(
    borrower: Borrower, facility_id: UUID, days: Mapping[date, bool]
) -> list[SignalEvent]:
    return [
        SignalEvent(
            borrower_id=borrower.id,
            facility_id=facility_id,
            event_date=day,
            family="payment",
            event_type="payment_delay",
            magnitude=Decimal("35") if adverse else Decimal("0"),
            unit="days",
            payload={"days_past_due": 35 if adverse else 0, "is_adverse": adverse},
        )
        for day, adverse in days.items()
    ]


def _monitored(fixture: _Fixture, reference: str) -> tuple[Borrower, UUID, UUID]:
    borrower = fixture.borrower(fixture.portfolio(reference), reference)
    version = fixture.covenant_version(borrower)
    fixture.seed_test(version, as_of_date=_YESTERDAY, value=Decimal("2.0"))
    with fixture.session_factory() as session:
        facility_id = session.execute(
            select(Facility.id).where(Facility.borrower_id == borrower.id)
        ).scalar_one()
    return borrower, facility_id, version.id


def _evidence(fixture: _Fixture, borrower: Borrower) -> EvidenceItem:
    with fixture.session_factory() as session:
        return session.execute(
            select(EvidenceItem).where(
                EvidenceItem.borrower_id == borrower.id,
                EvidenceItem.family == "payment",
                EvidenceItem.superseded_by_id.is_(None),
            )
        ).scalar_one()


def _run(fixture: _Fixture, events: list[SignalEvent], lines: dict[UUID, Decimal], store) -> None:
    service = fixture.build_service(
        statement_lines=_lines_provider(lines),
        signal_source=lambda: events,
        threshold_store=store,
    )
    result = run_nightly_pipeline(
        fixture.build_runner(service), trigger="manual", as_of=_TODAY.isoformat()
    )
    assert result.success is True, result


def _days(*offsets: int) -> list[date]:
    return [_TODAY - timedelta(days=offset) for offset in offsets]


def test_warnings_count_only_while_current(fixture: _Fixture) -> None:
    current, current_facility, current_version = _monitored(fixture, "B-CURRENT")
    old, old_facility, old_version = _monitored(fixture, "B-OLD")
    healed, healed_facility, healed_version = _monitored(fixture, "B-HEALED")
    events = [
        *_late_payments(current, current_facility, dict.fromkeys(_days(4, 3, 2), True)),
        # Twenty straight late days, but two months ago.
        *_late_payments(old, old_facility, dict.fromkeys(_days(*range(80, 60, -1)), True)),
        *_late_payments(
            healed,
            healed_facility,
            {**dict.fromkeys(_days(6, 5, 4), True), _TODAY - timedelta(days=1): False},
        ),
    ]
    lines = {v: Decimal("2.1") for v in (current_version, old_version, healed_version)}

    _run(fixture, events, lines, _Thresholds(decay_rate=Decimal("0.95")))

    live = _evidence(fixture, current)
    assert live.state == "sustained" and live.counts_toward_pressure
    assert live.decay_factor == Decimal("0.95") ** 2, "decayed from the last late day"

    stale = _evidence(fixture, old)
    assert stale.state == "transient" and not stale.counts_toward_pressure
    assert stale.event_count_window == 0

    recovered = _evidence(fixture, healed)
    assert recovered.state == "transient" and not recovered.counts_toward_pressure


def test_a_snapshot_without_a_decay_rate_still_runs(fixture: _Fixture) -> None:
    borrower, facility_id, version_id = _monitored(fixture, "B-NODECAY")
    events = _late_payments(borrower, facility_id, dict.fromkeys(_days(3, 2, 1), True))

    _run(fixture, events, {version_id: Decimal("2.1")}, _Thresholds(decay_rate=None))

    item = _evidence(fixture, borrower)
    assert item.state == "sustained"
    assert item.decay_factor == Decimal("1")


def test_a_crossing_caused_by_a_signal_is_attributed_to_it(fixture: _Fixture) -> None:
    from covenant_radar.db.models.forecast import Forecast, ForecastDriver

    borrower = fixture.borrower(fixture.portfolio("B-TIGHT"), "B-TIGHT")
    version = fixture.covenant_version(borrower, threshold=Decimal("3"), direction="max")
    for offset, value in ((273, "2.89"), (182, "2.90"), (91, "2.91")):
        fixture.seed_test(version, as_of_date=_TODAY - timedelta(days=offset), value=Decimal(value))
    with fixture.session_factory() as session:
        facility_id = session.execute(
            select(Facility.id).where(Facility.borrower_id == borrower.id)
        ).scalar_one()
    events = _late_payments(borrower, facility_id, dict.fromkeys(_days(2, 1, 0), True))

    _run(fixture, events, {version.id: Decimal("2.92")}, _Thresholds(decay_rate=Decimal("0.95")))

    with fixture.session_factory() as session:
        forecast = session.execute(
            select(Forecast).where(
                Forecast.covenant_version_id == version.id, Forecast.horizon_days == 90
            )
        ).scalar_one()
        drivers = {
            ("evidence" if row.evidence_id is not None else row.name): row.share
            for row in session.execute(
                select(ForecastDriver).where(ForecastDriver.forecast_id == forecast.id)
            ).scalars()
        }
    assert forecast.projected_cross_date is not None, "the late payment tips the tight covenant"
    assert drivers.get("evidence", Decimal("0")) > drivers.get("trend", Decimal("0"))
    # The clamp to 99% is the late payment's doing, so it outweighs the cushion.
    assert drivers["evidence"] > drivers["distance"], drivers

    # Every horizon names the evidence item, not only the longest one
    # (`spec §R-13.c`: a driver click opens the evidence).
    with fixture.session_factory() as session:
        for horizon in (30,):  # the fixture scores the 30- and 90-day horizons
            short = session.execute(
                select(Forecast).where(
                    Forecast.covenant_version_id == version.id,
                    Forecast.horizon_days == horizon,
                )
            ).scalar_one()
            linked = (
                session.execute(
                    select(ForecastDriver.evidence_id).where(
                        ForecastDriver.forecast_id == short.id,
                        ForecastDriver.evidence_id.is_not(None),
                    )
                )
                .scalars()
                .all()
            )
            assert linked, f"the {horizon}-day forecast lost its evidence link"


def test_what_changed_names_the_signal_that_moved_the_borrower(fixture: _Fixture) -> None:
    from covenant_radar.core.clock import FixedClock
    from covenant_radar.db.models.forecast import TriageEntry
    from covenant_radar.domain.triage.changes import ChangeThresholds
    from tests.integration.test_nightly_pipeline import _NOW

    borrower, facility_id, version_id = _monitored(fixture, "B-MOVED")
    from covenant_radar.db.models import CovenantVersion

    with fixture.session_factory() as session:
        version = session.get(CovenantVersion, version_id)
    assert version is not None
    for offset in (364, 273, 182):  # a flat quarterly history: every point kept
        fixture.seed_test(version, as_of_date=_TODAY - timedelta(days=offset), value=Decimal("2.0"))
    store = _Thresholds(decay_rate=Decimal("0.95"))
    changes = ChangeThresholds(
        reporting_threshold=Decimal("0.05"), dominant_driver_share=Decimal("0.50")
    )

    def night(as_of, events, minutes):
        service = fixture.build_service(
            statement_lines=_lines_provider({version_id: Decimal("2.0")}),
            signal_source=lambda: events,
            threshold_store=store,
            what_changed_thresholds=changes,
            clock=FixedClock(_NOW + timedelta(minutes=minutes)),
        )
        result = run_nightly_pipeline(
            fixture.build_runner(service), trigger="manual", as_of=as_of.isoformat()
        )
        assert result.success is True, result

    night(_YESTERDAY, [], 0)
    night(_TODAY, _late_payments(borrower, facility_id, dict.fromkeys(_days(2, 1, 0), True)), 5)

    with fixture.session_factory() as session:
        latest = (
            session.execute(
                select(TriageEntry)
                .where(TriageEntry.borrower_id == borrower.id)
                .order_by(TriageEntry.created_at.desc())
            )
            .scalars()
            .first()
        )
    assert latest is not None and latest.what_changed is not None
    assert "probability increased" in latest.what_changed or "band worsened" in latest.what_changed
    assert "dominant driver: evidence:" in latest.what_changed, latest.what_changed


def test_simulator_baseline_reproduces_the_stored_forecast(fixture: _Fixture) -> None:
    """`spec §R-15.a`: the do-nothing baseline equals the stored forecast,
    including the scaled evidence pressure behind it."""

    from covenant_radar.db.models import Portfolio
    from covenant_radar.db.models.forecast import Forecast
    from covenant_radar.db.scoping import Scope
    from covenant_radar.web.routes.simulator import _compare
    from covenant_radar.web.view_models.simulation import load_simulation_context

    borrower = fixture.borrower(fixture.portfolio("B-SIM"), "B-SIM")
    version = fixture.covenant_version(borrower, threshold=Decimal("3"), direction="max")
    for offset, value in ((273, "2.40"), (182, "2.45"), (91, "2.50")):
        fixture.seed_test(version, as_of_date=_TODAY - timedelta(days=offset), value=Decimal(value))
    with fixture.session_factory() as session:
        facility_id = session.execute(
            select(Facility.id).where(Facility.borrower_id == borrower.id)
        ).scalar_one()
    events = _late_payments(borrower, facility_id, dict.fromkeys(_days(2, 1, 0), True))
    _run(fixture, events, {version.id: Decimal("2.55")}, _Thresholds(decay_rate=Decimal("0.95")))

    with fixture.session_factory() as session:
        paths = session.execute(select(Portfolio.path)).scalars().all()
        scope = Scope(principal_id=uuid4(), descendant_paths=tuple(paths))
        forecast_id = session.execute(
            select(Forecast.id).where(Forecast.covenant_version_id == version.id).limit(1)
        ).scalar_one()
        context = load_simulation_context(session, forecast_id, scope=scope)
        comparisons = _compare(context, (), {}, None)
        for row in context.forecasts:
            baseline = comparisons[row.id].baseline
            assert baseline.probability is not None and row.probability is not None
            assert baseline.probability.quantize(Decimal("0.0001")) == row.probability, (
                row.horizon_days
            )
            assert baseline.crossing_date == row.projected_cross_date
