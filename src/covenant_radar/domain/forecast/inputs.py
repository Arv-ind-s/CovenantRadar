"""Dimensionless probability inputs from a projection.

``probability`` saturates each input with ``1 / (1 + x)`` or ``x / (1 + x)``,
so its inputs must be on a scale where one means "a lot".  A covenant's raw
units are not: a leverage limit of 3.0x, an interest cover of 1.5x and a
utilisation cap of 85% would each map the same move to a different risk, and a
trend measured per day in ratio units (~0.001) saturates to almost nothing.
The scorer and the intervention simulator both read their inputs from here, so
a simulated baseline always equals the stored forecast.

The three inputs are kept apart, as the probability module describes them —
the distance term is the current state, velocity and pressure the
forward-looking deterioration — so each driver is credited only for its own
effect:

* ``distance`` — today's cushion to the limit (at the path's day zero), as a
  fraction of the threshold.
* ``velocity`` — the share of that cushion the financial trend uses up over
  the horizon (negative when the trend is improving).
* ``pressure`` — the share of that cushion sustained warning evidence uses up
  over the horizon.
* ``projected_crossing`` — whether the projected path reaches the limit within
  the horizon (with any exception's threshold in force at the endpoint).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from covenant_radar.domain.forecast.path import Projection
from covenant_radar.domain.forecast.trend import Direction

_ZERO: Final[Decimal] = Decimal("0")


@dataclass(frozen=True, slots=True)
class ProbabilityInputs:
    """The normalised inputs to ``probability``, with their basis."""

    distance: Decimal
    velocity: Decimal
    pressure: Decimal
    projected_crossing: bool
    cushion: Decimal


def probability_inputs(
    projection: Projection,
    *,
    threshold: Decimal | None = None,
    distance_scale: Decimal = Decimal("1"),
) -> ProbabilityInputs | None:
    """Return normalised inputs, or ``None`` when the projection has no value.

    ``threshold`` overrides the projection's own threshold for the horizon
    endpoint, for an exception that changes the limit inside the horizon.
    ``distance_scale`` stretches the cushion before the mapping saturates it
    (`forecast.distance_scale`): at 2, a flat covenant is amber on distance
    alone only inside about 12% headroom, beside the covenants' own 10%
    warning band, rather than anywhere inside 25%.
    """

    if not distance_scale.is_finite() or distance_scale <= _ZERO:
        raise ValueError("distance_scale must be positive.")

    first = projection.path[0] if projection.path else None
    last = projection.path[-1] if projection.path else None
    if first is None or last is None or first.value is None or last.value is None:
        return None
    scale = abs(projection.threshold)
    if scale == _ZERO:
        raise ValueError("A covenant threshold of zero has no relative distance.")
    end_threshold = projection.threshold if threshold is None else threshold
    # Today is the path's day zero: the latest value carried forward by the
    # trend over any gap since it was reported (`project`'s elapsed days).
    cushion = _toward_boundary(first.value, projection.threshold, projection.direction)
    sign = Decimal(1) if projection.direction is Direction.MAX else Decimal(-1)
    trend_move = sign * (last.trend_component - first.trend_component)
    pressure_move = max(_ZERO, sign * (last.pressure_component - first.pressure_component))
    return ProbabilityInputs(
        distance=cushion / scale * distance_scale,
        velocity=trend_move / cushion if cushion > _ZERO else _ZERO,
        pressure=pressure_move / cushion if cushion > _ZERO else _ZERO,
        # At the limit already (by the trend since the last statement) or
        # reaching it within the horizon; an observed breach is the caller's
        # `already_breached`, not this.
        projected_crossing=(
            cushion == _ZERO
            or _toward_boundary(last.value, end_threshold, projection.direction) == _ZERO
        ),
        cushion=cushion,
    )


def _toward_boundary(value: Decimal, threshold: Decimal, direction: Direction) -> Decimal:
    if direction is Direction.MAX:
        return max(_ZERO, threshold - value)
    return max(_ZERO, value - threshold)


__all__ = ["ProbabilityInputs", "probability_inputs"]
