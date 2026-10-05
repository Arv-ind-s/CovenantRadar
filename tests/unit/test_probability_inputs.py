"""Forecast probability inputs are dimensionless, and evidence pressure moves a
covenant in its own units — the two mistakes that let one late payment project
a healthy borrower into breach within days."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from covenant_radar.domain.forecast import Observation, evidence_pressure, first_crossing, project
from covenant_radar.domain.forecast.inputs import probability_inputs

pytestmark = pytest.mark.unit

_QUARTERS = (date(2025, 9, 30), date(2025, 12, 31), date(2026, 3, 31), date(2026, 6, 30))
_TODAY = date(2026, 7, 1)


def _series(*values: str) -> list[Observation]:
    return [Observation(day, Decimal(value)) for day, value in zip(_QUARTERS, values, strict=True)]


def _late_payment(materiality_pct: str) -> object:
    return evidence_pressure(
        [
            {
                "id": "late-payment",
                "state": "sustained",
                "counts_toward_pressure": True,
                "materiality_pct": Decimal(materiality_pct),
                "decay_factor": Decimal("1"),
            }
        ],
        "max",
    )


def _scale(threshold: str, rate: str = "0.25", period_days: int = 90) -> Decimal:
    return Decimal(rate) * Decimal(threshold) / Decimal(period_days)


def test_inputs_are_the_same_for_the_same_relative_position() -> None:
    leverage = project(
        _series("2.40", "2.40", "2.40", "2.40"), Decimal("0"), 90, Decimal("3"), "max"
    )
    utilisation = project(_series("80", "80", "80", "80"), Decimal("0"), 90, Decimal("100"), "max")

    lev, util = probability_inputs(leverage), probability_inputs(utilisation)

    assert lev is not None and util is not None
    assert lev.distance == util.distance == Decimal("0.2")


def test_velocity_is_the_share_of_the_cushion_the_trend_uses() -> None:
    # Leverage rising 0.1x a quarter with 0.4x of cushion left: over 90 days
    # the trend uses about a quarter of it.
    projection = project(
        _series("2.30", "2.40", "2.50", "2.60"), Decimal("0"), 90, Decimal("3"), "max"
    )

    inputs = probability_inputs(projection)

    assert inputs is not None
    assert Decimal("0.2") < inputs.velocity < Decimal("0.3")


def test_velocity_excludes_evidence_pressure() -> None:
    flat = _series("2.40", "2.40", "2.40", "2.40")
    pressured = project(
        flat, _late_payment("80"), 90, Decimal("3"), "max", pressure_scale=_scale("3")
    )

    inputs = probability_inputs(pressured)

    assert inputs is not None
    assert inputs.velocity == 0, "the trend is flat; the signal is the pressure input"
    assert inputs.distance == Decimal("0.2"), "distance is today's cushion, not the endpoint"
    # 80% materiality x 25% of 3.0x over the quarter = 0.6x: the whole cushion.
    assert inputs.pressure.quantize(Decimal("0.0001")) == Decimal("1.0000")
    assert inputs.projected_crossing


def test_a_floor_materiality_signal_no_longer_breaches_a_healthy_covenant() -> None:
    flat = _series("2.00", "2.00", "2.00", "2.00")

    projection = project(
        flat, _late_payment("5"), 90, Decimal("3"), "max", pressure_scale=_scale("3")
    )

    assert first_crossing(projection, as_of_date=_TODAY).crossing_day is None
    # 5% materiality x 25% of the 3.0x limit per quarter = 0.0375x over 90 days.
    end = projection.path[-1].value
    assert end is not None and end.quantize(Decimal("0.0001")) == Decimal("2.0375")


def test_a_late_payment_pushes_the_tight_covenant_but_not_the_healthy_ones() -> None:
    # Alderwyn in the demo: leverage 2.92x against a 3.0x limit, interest cover
    # 3.22x against a 1.5x minimum, after three days of 30-40 days past due.
    signal = _late_payment("80")
    leverage = project(
        _series("2.83", "2.86", "2.89", "2.92"),
        signal,
        90,
        Decimal("3"),
        "max",
        pressure_scale=_scale("3"),
    )
    interest_cover = project(
        _series("3.13", "3.16", "3.19", "3.22"),
        evidence_pressure([], "min"),
        90,
        Decimal("1.5"),
        "min",
        pressure_scale=_scale("1.5"),
    )
    pressured_cover = project(
        _series("3.13", "3.16", "3.19", "3.22"),
        evidence_pressure(
            [
                {
                    "id": "late",
                    "state": "sustained",
                    "counts_toward_pressure": True,
                    "materiality_pct": Decimal("80"),
                    "decay_factor": Decimal("1"),
                }
            ],
            "min",
        ),
        90,
        Decimal("1.5"),
        "min",
        pressure_scale=_scale("1.5"),
    )

    assert first_crossing(leverage, as_of_date=_TODAY).crossing_day is not None
    assert first_crossing(interest_cover, as_of_date=_TODAY).crossing_day is None
    assert first_crossing(pressured_cover, as_of_date=_TODAY).crossing_day is None


def test_without_a_scale_pressure_keeps_the_c35_contract() -> None:
    flat = _series("2.00", "2.00", "2.00", "2.00")

    projection = project(flat, Decimal("0.05"), 10, Decimal("3"), "max")

    assert projection.path[-1].value == Decimal("2.00") + Decimal("0.05") * 10


def test_a_projected_crossing_is_not_called_a_breach() -> None:
    from covenant_radar.domain.forecast import Weights, probability

    weights = Weights(distance=Decimal("0.5"), velocity=Decimal("0.3"), pressure=Decimal("0.2"))
    projected = probability(
        Decimal("0.08"), Decimal("0.25"), Decimal("1.6"), 60, weights, projected_crossing=True
    )
    observed = probability(
        Decimal("0"), Decimal("0"), Decimal("0"), 60, weights, already_breached=True
    )

    assert projected.probability == weights.max_probability
    assert projected.reason is not None and "projected" in projected.reason
    assert projected.clamp_reason is not None and "already in breach" not in projected.clamp_reason
    assert observed.reason is not None and "already in breach" in observed.reason


def test_the_trend_runs_on_between_the_statement_and_today() -> None:
    # Leverage rising 0.1x a quarter, last reported 90 days ago at 2.60x.
    rising = _series("2.30", "2.40", "2.50", "2.60")
    stale_view = project(rising, Decimal("0"), 90, Decimal("3"), "max")
    today_view = project(rising, Decimal("0"), 90, Decimal("3"), "max", elapsed_days=90)

    day_zero = today_view.path[0].value
    assert stale_view.path[0].value == Decimal("2.60")
    assert day_zero is not None and Decimal("2.69") < day_zero < Decimal("2.71")
    inputs = probability_inputs(today_view)
    assert inputs is not None
    # The within-horizon trend move is the same 90 days of drift either way.
    stale = probability_inputs(stale_view)
    assert stale is not None
    assert inputs.cushion < stale.cushion, "today is closer to the limit than the statement"
    moved_today = inputs.velocity * inputs.cushion
    moved_stale = stale.velocity * stale.cushion
    assert abs(moved_today - moved_stale) < Decimal("1e-12")


def test_a_path_already_past_the_limit_today_is_a_projection() -> None:
    rising = _series("2.70", "2.80", "2.90", "2.98")
    projection = project(rising, Decimal("0"), 30, Decimal("3"), "max", elapsed_days=60)

    inputs = probability_inputs(projection)

    assert inputs is not None
    assert inputs.cushion == 0 and inputs.projected_crossing
