"""Presentation scenarios exercise ingestion, evidence gating and all forecast horizons."""

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from covenant_radar.db.models.audit import TraceRow
from covenant_radar.db.models.forecast import Forecast, ForecastDriver
from covenant_radar.db.models.signal import EvidenceItem
from covenant_radar.demo.scenarios import SCENARIOS, scenario_events
from covenant_radar.domain.signals import FAMILIES
from covenant_radar.scheduler.pipeline import run_nightly_pipeline
from tests.integration.test_nightly_evidence_lifecycle import _monitored, _Thresholds
from tests.integration.test_nightly_pipeline import _TODAY, _Fixture, _lines_provider

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("code", FAMILIES)
def test_every_demo_signal_reaches_evidence_and_all_horizons(tmp_path, code) -> None:
    fixture = _Fixture(tmp_path)
    try:
        borrower, facility_id, version_id = _monitored(fixture, "B-JUDGE")
        events = scenario_events(
            code, borrower_id=borrower.id, facility_id=facility_id, today=_TODAY, actor_id=uuid4()
        )
        service = fixture.build_service(
            statement_lines=_lines_provider({version_id: Decimal("2.1")}),
            signal_source=lambda: events,
            threshold_store=_Thresholds(decay_rate=Decimal("0.95")),
            horizons=(30, 60, 90),
        )
        result = run_nightly_pipeline(
            fixture.build_runner(service), trigger="manual", as_of=_TODAY.isoformat()
        )
        assert result.success, result
        with fixture.session_factory() as session:
            evidence = session.scalar(select(EvidenceItem).where(EvidenceItem.family == code))
            assert evidence is not None and evidence.state == "sustained"
            assert evidence.event_count_window == 3
            assert evidence.counts_toward_pressure
            forecasts = session.scalars(
                select(Forecast).where(Forecast.covenant_version_id == version_id)
            ).all()
            assert {f.horizon_days for f in forecasts} == {30, 60, 90}
            assert all(f.confidence is not None for f in forecasts)
            # Small contributions may be grouped as "other" under T5. The full
            # calculation must still retain their identity and contribution.
            for forecast in forecasts:
                trace = session.scalar(select(TraceRow).where(TraceRow.subject_id == forecast.id))
                terms = trace.inputs["pressure_terms"]
                matching = [term for term in terms if str(term["evidence_id"]) == str(evidence.id)]
                assert matching and matching[0]["included"]
                assert Decimal(str(matching[0]["contribution"])) > 0

    finally:
        fixture.close()


def test_one_off_news_does_not_count_as_forecast_pressure(tmp_path) -> None:
    fixture = _Fixture(tmp_path)
    try:
        borrower, facility_id, version_id = _monitored(fixture, "B-NOISE")
        events = scenario_events(
            "temporary_news",
            borrower_id=borrower.id,
            facility_id=facility_id,
            today=_TODAY,
            actor_id=uuid4(),
        )
        service = fixture.build_service(
            statement_lines=_lines_provider({version_id: Decimal("2.1")}),
            signal_source=lambda: events,
            threshold_store=_Thresholds(decay_rate=Decimal("0.95")),
            horizons=(30, 60, 90),
        )
        assert run_nightly_pipeline(
            fixture.build_runner(service), trigger="manual", as_of=_TODAY.isoformat()
        ).success
        with fixture.session_factory() as session:
            item = session.scalar(select(EvidenceItem).where(EvidenceItem.family == "news"))
            assert item.state == "transient"
            assert not item.counts_toward_pressure
            assert (
                session.scalar(select(ForecastDriver).where(ForecastDriver.evidence_id == item.id))
                is None
            )
    finally:
        fixture.close()


def test_combined_scenario_is_complete_dated_and_repeatable() -> None:
    identities = dict(borrower_id=uuid4(), facility_id=uuid4(), today=_TODAY, actor_id=uuid4())
    first = scenario_events("combined", **identities)
    second = scenario_events("combined", **identities)
    assert len(first) == 21
    assert {event.family for event in first} == set(FAMILIES)
    assert {event.event_date for event in first} == {_TODAY - timedelta(days=n) for n in range(3)}
    assert [event.content_hash for event in first] == [event.content_hash for event in second]
    assert all(event.payload["synthetic_walkthrough"] for event in first)
    assert set(FAMILIES) <= SCENARIOS.keys()
