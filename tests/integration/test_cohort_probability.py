"""`spec §R-12.a`/`R-12.b` through the production probability inputs.

`test_calibrated_cohorts.py` replays the recorded calibration on the raw-unit
inputs it was recorded with.  This gate checks the scorer's own path
(`domain.forecast.inputs` at the shipped `forecast.distance_scale`):

* R-12.b — the stable cohort sits below amber at every horizon on every
  scoring day sampled across the series;
* R-12.a — sixty days before its labelled breach, the deteriorating cohort's
  60-day probability is above act.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from covenant_radar.config.settings import ForecastSettings
from covenant_radar.domain.forecast import Weights, first_crossing, probability, project
from covenant_radar.domain.forecast.inputs import probability_inputs
from evaluation.reference_portfolio.cohorts import STABLE_COHORT
from tests.integration.test_cohort_dating import _dataset, _utilisation_observations

pytestmark = pytest.mark.integration

_ACT = Decimal("0.70")
_AMBER = Decimal("0.40")


def _shipped() -> tuple[Weights, Decimal]:
    fields = ForecastSettings.model_fields
    weights = Weights(
        distance=Decimal("0.50"),
        velocity=Decimal("0.30"),
        pressure=Decimal("0.20"),
        max_probability=Decimal("0.99"),
    )
    default_scale = fields["distance_scale"].default
    assert isinstance(default_scale, float)
    return weights, Decimal(str(default_scale))


def _score(observations, threshold, direction, as_of, horizon) -> Decimal:
    weights, scale = _shipped()
    projection = project(observations, Decimal("0"), horizon, threshold, direction)
    crossing = first_crossing(projection, as_of_date=as_of)
    inputs = probability_inputs(projection, distance_scale=scale)
    assert inputs is not None
    return probability(
        inputs.distance,
        inputs.velocity,
        inputs.pressure,
        horizon,
        weights,
        already_breached=crossing.crossing_day == 0 and inputs.cushion == 0,
        projected_crossing=inputs.projected_crossing,
    ).probability


def test_stable_cohort_stays_below_amber_every_day() -> None:
    dataset = _dataset()
    day = dataset.signal_start_date + timedelta(days=30)
    worst = Decimal("0")
    while day <= dataset.signal_end_date:
        for assignment in dataset.assignments:
            if assignment.cohort != STABLE_COHORT:
                continue
            observations = _utilisation_observations(dataset, assignment.borrower_id, day)
            for horizon in (30, 60, 90):
                worst = max(worst, _score(observations, Decimal("85"), "max", day, horizon))
        day += timedelta(days=15)
    assert worst < _AMBER, f"stable cohort reached {worst}"


def test_deteriorating_cohort_is_act_sixty_days_out() -> None:
    dataset = _dataset()
    for label in dataset.labels:
        scoring_day = label.breach_date - timedelta(days=60)
        observations = _utilisation_observations(dataset, label.borrower_id, scoring_day)
        value = _score(observations, label.threshold, label.direction, scoring_day, 60)
        assert value > _ACT, (label.borrower_id, value)
