"""Size each lever for one borrower and compose a remediation package.

The planner answers three questions a credit officer asks of a deteriorating
borrower, in this order:

1. **Which covenants are at risk?**  A covenant is at risk when it is in
   breach on the filed lines or when the simulated probability of failing
   any projected test in the horizon exceeds the target.
2. **What would each lever do on its own?**  Each available lever is sized
   to the smallest amount that brings the covenants it helps to the target
   — or, where even its full capacity cannot, to the point beyond which more
   of it buys under one percentage point.
3. **What package cures the position?**  Self-help levers are tried in a
   fixed order (working capital, surplus cash, repricing, costs, tenor,
   equity), each sized on the position the earlier steps leave behind; a
   covenant reset is sized only for what self-help cannot reach, and is
   always flagged as a lender's concession, not a cure.

The package is then re-run under uniform stresses.  Every number is a
deterministic function of the filings: the seed comes from the borrower and
the filed period, and every option shares the baseline's draws.

Advisory only (``spec P-01``): nothing here approves, waives or decides.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from covenant_radar.domain.remediation.levers import Capacity, Lever, levers_for
from covenant_radar.domain.remediation.montecarlo import (
    DEFAULT_HORIZON_QUARTERS,
    DEFAULT_PATHS,
    STRESSES,
    Draws,
    Outcome,
    ProForma,
    Stress,
    filed_pro_forma,
    make_draws,
    project,
    seed_for,
)
from covenant_radar.domain.remediation.position import (
    BorrowerPosition,
    Drivers,
    estimate_drivers,
)

#: A covenant is treated as cured when its simulated probability of failing
#: any projected test in the horizon is at or below this.
TARGET_PROBABILITY: Final[float] = 0.10
#: Paths used while searching for a size; every displayed figure uses all of them.
SOLVE_PATHS: Final[int] = 400
_MEANINGFUL: Final[float] = 0.01
#: A package step must cut the summed severity of the at-risk covenants by this.
_MEANINGFUL_STEP: Final[float] = 0.03
#: Sizes are searched on a subset of paths, so they aim this far inside the
#: target to keep the full-path figure on the right side of it.
_SOLVE_MARGIN: Final[float] = 0.8
_BISECTION_STEPS: Final[int] = 9
MODEL_VERSION: Final[str] = "remediation.v1"
#: ``Simulation.parameters["source"]`` on a step recorded from the planner.
RECORD_SOURCE: Final[str] = "remediation_planner"


@dataclass(frozen=True, slots=True)
class LeverAssessment:
    """One lever on its own, sized for this borrower."""

    lever: Lever
    capacity: Capacity
    recommended: float
    outcome: Outcome | None
    helps: tuple[str, ...]
    hurts: tuple[str, ...]
    reaches_target: bool

    @property
    def available(self) -> bool:
        return self.capacity.available

    @property
    def relevant(self) -> bool:
        return self.available and bool(self.helps)


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One step of the package and the cumulative outcome after it."""

    lever: Lever
    size: float
    capacity: Capacity
    outcome: Outcome


@dataclass(frozen=True, slots=True)
class StressResult:
    stress: Stress
    baseline: Outcome
    plan: Outcome


@dataclass(frozen=True, slots=True)
class CustomPlan:
    """A package the reader assembled, with each size clamped to capacity."""

    sizes: Mapping[str, float]
    outcome: Outcome


@dataclass(frozen=True, slots=True)
class RemediationReport:
    position: BorrowerPosition
    drivers: Drivers
    baseline: Outcome
    at_risk: tuple[str, ...]
    levers: tuple[LeverAssessment, ...]
    plan: tuple[PlanStep, ...]
    plan_outcome: Outcome
    stresses: tuple[StressResult, ...]
    target: float
    content_hash: str
    paths: int = DEFAULT_PATHS
    horizon: int = DEFAULT_HORIZON_QUARTERS

    @property
    def status(self) -> str:
        if not self.at_risk:
            return "no_action"
        open_ = [ref for ref in self.at_risk if self.plan_outcome.probability(ref) > self.target]
        if open_:
            return "partial"
        if not self.plan:
            # In breach on the filed lines, but the projection already meets
            # the target: there is nothing for a lever to do.
            return "clears"
        if any(step.lever.lender_decision and step.lever.covenant for step in self.plan):
            return "cured_with_reset"
        return "cured"

    @property
    def self_help_outcome(self) -> Outcome:
        """The position after the borrower's own levers, before any covenant reset."""

        own = [step for step in self.plan if step.lever.covenant is None]
        return own[-1].outcome if own else self.baseline

    @property
    def plan_sizes(self) -> dict[str, float]:
        return {step.lever.code: step.size for step in self.plan}


class _Evaluator:
    """Memoised projections on one borrower's shared draws."""

    def __init__(self, position: BorrowerPosition, drivers: Drivers, draws: Draws) -> None:
        self.position = position
        self.drivers = drivers
        self.draws = draws
        self._cache: dict[tuple[object, ...], Outcome] = {}

    def __call__(
        self, state: ProForma, *, paths: int | None = None, stress: Stress | None = None
    ) -> Outcome:
        key = (_state_key(state), paths, stress.code if stress else None)
        cached = self._cache.get(key)
        if cached is None:
            cached = project(
                self.position, self.drivers, state, self.draws, stress=stress, paths=paths
            )
            self._cache[key] = cached
        return cached


def build_report(
    position: BorrowerPosition,
    *,
    paths: int = DEFAULT_PATHS,
    horizon: int = DEFAULT_HORIZON_QUARTERS,
    target: float = TARGET_PROBABILITY,
) -> RemediationReport:
    """Assess every lever, compose the package, and stress it."""

    drivers = estimate_drivers(position)
    draws = make_draws(seed_for(position), paths=paths, horizon=horizon)
    evaluate = _Evaluator(position, drivers, draws)
    solve_paths = min(SOLVE_PATHS, paths)
    start = filed_pro_forma(position)
    baseline = evaluate(start)
    at_risk = tuple(
        outlook.reference
        for outlook in baseline.outlooks
        if outlook.start_breached or outlook.breach_within_horizon > target
    )
    levers = levers_for(position)

    assessments = tuple(
        _assess(lever, position, drivers, start, at_risk, target, evaluate, solve_paths)
        for lever in levers
    )
    plan, plan_state = _compose(
        levers, position, drivers, start, at_risk, target, evaluate, solve_paths
    )
    plan_outcome = plan[-1].outcome if plan else baseline
    stresses = (
        tuple(
            StressResult(
                stress, evaluate(start, stress=stress), evaluate(plan_state, stress=stress)
            )
            for stress in STRESSES
        )
        if at_risk
        else ()
    )

    return RemediationReport(
        position=position,
        drivers=drivers,
        baseline=baseline,
        at_risk=at_risk,
        levers=assessments,
        plan=plan,
        plan_outcome=plan_outcome,
        stresses=stresses,
        target=target,
        content_hash=_content_hash(position, draws, baseline, plan, plan_outcome),
        paths=paths,
        horizon=horizon,
    )


def custom_plan(report: RemediationReport, sizes: Mapping[str, float]) -> CustomPlan:
    """Project a package the reader assembled, on the report's own draws.

    Sizes are applied in plan order and each is clamped to the capacity the
    borrower has *after* the earlier steps, so a reader cannot spend the same
    cash twice or term out lines an equity issue already retired.
    """

    position = report.position
    draws = make_draws(seed_for(position), paths=report.paths, horizon=report.horizon)
    evaluate = _Evaluator(position, report.drivers, draws)
    return _custom(
        levers_for(position), position, report.drivers, filed_pro_forma(position), sizes, evaluate
    )


def _assess(
    lever: Lever,
    position: BorrowerPosition,
    drivers: Drivers,
    start: ProForma,
    at_risk: Sequence[str],
    target: float,
    evaluate: _Evaluator,
    solve_paths: int,
) -> LeverAssessment:
    capacity = lever.capacity(position, drivers, start)
    if not capacity.available:
        return LeverAssessment(lever, capacity, 0.0, None, (), (), False)
    base = evaluate(start, paths=solve_paths)
    full = evaluate(lever.apply(start, capacity.maximum, drivers), paths=solve_paths)
    helps, hurts = _effects(base, full)
    if lever.covenant is not None:
        helps = tuple(ref for ref in helps if ref == lever.covenant)
    targets = [ref for ref in helps if ref in at_risk]
    if lever.covenant is not None and lever.covenant not in at_risk:
        return LeverAssessment(lever, capacity, 0.0, None, (), (), False)
    if not targets:
        return LeverAssessment(lever, capacity, 0.0, None, helps, hurts, False)
    size, reached = _solve(lever, start, capacity, targets, target, drivers, evaluate, solve_paths)
    outcome = evaluate(lever.apply(start, size, drivers))
    return LeverAssessment(lever, capacity, size, outcome, helps, hurts, reached)


def _compose(
    levers: Sequence[Lever],
    position: BorrowerPosition,
    drivers: Drivers,
    start: ProForma,
    at_risk: Sequence[str],
    target: float,
    evaluate: _Evaluator,
    solve_paths: int,
) -> tuple[tuple[PlanStep, ...], ProForma]:
    state = start
    steps: list[PlanStep] = []
    if not at_risk or all(evaluate(start).probability(ref) <= target for ref in at_risk):
        return (), state
    single = _single_lever_cure(
        levers, position, drivers, start, at_risk, target, evaluate, solve_paths
    )
    if single is not None:
        return single
    for lever in sorted(levers, key=lambda item: item.order):
        current = evaluate(state, paths=solve_paths)
        settled = evaluate(state)
        open_ = [ref for ref in at_risk if settled.probability(ref) > target]
        if not open_:
            break
        if lever.covenant is not None and lever.covenant not in open_:
            continue
        capacity = lever.capacity(position, drivers, state)
        if not capacity.available:
            continue
        full = evaluate(lever.apply(state, capacity.maximum, drivers), paths=solve_paths)
        targets = [
            ref
            for ref in open_
            if (lever.covenant is None or ref == lever.covenant)
            and full.severity(ref) <= current.severity(ref) - _MEANINGFUL
        ]
        if not targets:
            continue
        size, _reached = _solve(
            lever, state, capacity, targets, target, drivers, evaluate, solve_paths
        )
        if size <= 0:
            continue
        candidate = lever.apply(state, size, drivers)
        after = evaluate(candidate, paths=solve_paths)
        before_total = sum(current.severity(ref) for ref in at_risk)
        after_total = sum(after.severity(ref) for ref in at_risk)
        pushed_over = any(
            current.probability(outlook.reference) <= target < after.probability(outlook.reference)
            for outlook in after.outlooks
        )
        if after_total > before_total - _MEANINGFUL_STEP or pushed_over:
            continue
        state = candidate
        steps.append(PlanStep(lever, size, capacity, evaluate(state)))
    return _prune(steps, position, drivers, start, at_risk, target, evaluate)


def _single_lever_cure(
    levers: Sequence[Lever],
    position: BorrowerPosition,
    drivers: Drivers,
    start: ProForma,
    at_risk: Sequence[str],
    target: float,
    evaluate: _Evaluator,
    solve_paths: int,
) -> tuple[tuple[PlanStep, ...], ProForma] | None:
    """The first self-help lever, in plan order, that cures every at-risk covenant alone.

    One action the borrower can take is a better package than three that
    happen to add up, so it is preferred before any composition is tried.
    """

    for lever in sorted(levers, key=lambda item: item.order):
        if lever.covenant is not None or not lever.single_step:
            continue
        capacity = lever.capacity(position, drivers, start)
        if not capacity.available:
            continue
        full = evaluate(lever.apply(start, capacity.maximum, drivers), paths=solve_paths)
        if any(full.probability(ref) > target * _SOLVE_MARGIN for ref in at_risk):
            continue
        if any(
            full.probability(outlook.reference) > target
            for outlook in full.outlooks
            if outlook.reference not in at_risk
        ):
            continue
        size, reached = _solve(
            lever, start, capacity, at_risk, target, drivers, evaluate, solve_paths
        )
        if size <= 0:
            continue
        state = lever.apply(start, size, drivers)
        outcome = evaluate(state)
        if not reached or any(outcome.probability(ref) > target for ref in at_risk):
            continue
        return (PlanStep(lever, size, capacity, outcome),), state
    return None


def _prune(
    steps: Sequence[PlanStep],
    position: BorrowerPosition,
    drivers: Drivers,
    start: ProForma,
    at_risk: Sequence[str],
    target: float,
    evaluate: _Evaluator,
) -> tuple[tuple[PlanStep, ...], ProForma]:
    """Drop any step whose removal leaves every at-risk covenant where it was.

    A step that was useful when added can be made redundant by a later one;
    the earliest such step is removed first and the rest re-applied at the
    sizes already chosen, with each cumulative outcome recomputed.
    """

    def replay(kept: Sequence[PlanStep]) -> tuple[tuple[PlanStep, ...], ProForma]:
        state = start
        rebuilt: list[PlanStep] = []
        for step in kept:
            capacity = step.lever.capacity(position, drivers, state)
            size = min(step.size, capacity.maximum) if capacity.available else 0.0
            if size <= 0:
                continue
            state = step.lever.apply(state, size, drivers)
            rebuilt.append(PlanStep(step.lever, size, capacity, evaluate(state)))
        return tuple(rebuilt), state

    current, state = replay(steps)
    final = current[-1].outcome if current else evaluate(start)
    index = 0
    while index < len(current):
        trial, trial_state = replay(current[:index] + current[index + 1 :])
        outcome = trial[-1].outcome if trial else evaluate(start)
        if all(outcome.probability(ref) <= max(final.probability(ref), target) for ref in at_risk):
            current, state, final = trial, trial_state, outcome
            continue
        index += 1
    return current, state


def _solve(
    lever: Lever,
    state: ProForma,
    capacity: Capacity,
    targets: Sequence[str],
    target: float,
    drivers: Drivers,
    evaluate: _Evaluator,
    solve_paths: int,
) -> tuple[float, bool]:
    """Smallest size meeting the target, else the knee of diminishing returns."""

    def worst(size: float) -> float:
        outcome = evaluate(lever.apply(state, size, drivers), paths=solve_paths)
        return max(outcome.severity(ref) for ref in targets)

    def reaches(size: float) -> bool:
        outcome = evaluate(lever.apply(state, size, drivers), paths=solve_paths)
        return all(outcome.probability(ref) <= target * _SOLVE_MARGIN for ref in targets)

    maximum = capacity.maximum
    reached = reaches(maximum)
    if reached:
        low, high = 0.0, maximum
        if reaches(low):
            return 0.0, True
        for _ in range(_BISECTION_STEPS):
            middle = (low + high) / 2.0
            if reaches(middle):
                high = middle
            else:
                low = middle
        size = _round_up(high, capacity.step, maximum)
        # Confirm on every path; the search ran on a subset.
        for _ in range(8):
            outcome = evaluate(lever.apply(state, size, drivers))
            if all(outcome.probability(ref) <= target for ref in targets) or size >= maximum:
                break
            size = _round_up(size + maximum / 32.0, capacity.step, maximum)
        return size, True
    best = worst(maximum)
    goal = best + _MEANINGFUL
    low, high = 0.0, maximum
    if worst(low) <= goal:
        return 0.0, reached
    for _ in range(_BISECTION_STEPS):
        middle = (low + high) / 2.0
        if worst(middle) <= goal:
            high = middle
        else:
            low = middle
    return _round_up(high, capacity.step, maximum), reached


def package_steps(report: RemediationReport, sizes: Mapping[str, float]) -> tuple[PlanStep, ...]:
    """The package as steps, each with the cumulative outcome after it.

    Sizes are clamped exactly as ``custom_plan`` clamps them, so the last
    step's outcome is the package outcome the reader saw.  Each step's
    contribution is therefore read against the step before it, not alone.
    """

    position = report.position
    draws = make_draws(seed_for(position), paths=report.paths, horizon=report.horizon)
    evaluate = _Evaluator(position, report.drivers, draws)
    return tuple(
        PlanStep(lever, size, capacity, evaluate(state))
        for lever, size, capacity, state in _apply_sizes(
            levers_for(position), position, report.drivers, filed_pro_forma(position), sizes
        )
    )


def _apply_sizes(
    levers: Sequence[Lever],
    position: BorrowerPosition,
    drivers: Drivers,
    start: ProForma,
    sizes: Mapping[str, float],
) -> list[tuple[Lever, float, Capacity, ProForma]]:
    """Apply requested sizes in plan order, each clamped to what is left."""

    state = start
    applied: list[tuple[Lever, float, Capacity, ProForma]] = []
    for lever in sorted(levers, key=lambda item: item.order):
        requested = sizes.get(lever.code)
        if requested is None or requested <= 0:
            continue
        capacity = lever.capacity(position, drivers, state)
        if not capacity.available:
            continue
        size = min(max(float(requested), 0.0), capacity.maximum)
        state = lever.apply(state, size, drivers)
        applied.append((lever, size, capacity, state))
    return applied


def _custom(
    levers: Sequence[Lever],
    position: BorrowerPosition,
    drivers: Drivers,
    start: ProForma,
    sizes: Mapping[str, float],
    evaluate: _Evaluator,
) -> CustomPlan:
    applied = _apply_sizes(levers, position, drivers, start, sizes)
    state = applied[-1][3] if applied else start
    return CustomPlan({lever.code: size for lever, size, _c, _s in applied}, evaluate(state))


def _effects(base: Outcome, full: Outcome) -> tuple[tuple[str, ...], tuple[str, ...]]:
    helps: list[str] = []
    hurts: list[str] = []
    for outlook in base.outlooks:
        after = full.get(outlook.reference)
        if after is None:
            continue
        delta = full.severity(outlook.reference) - base.severity(outlook.reference)
        moved = _start_movement(outlook, after)
        if delta <= -_MEANINGFUL or (abs(delta) < _MEANINGFUL and moved > 0):
            helps.append(outlook.reference)
        elif delta >= _MEANINGFUL or moved < 0:
            hurts.append(outlook.reference)
    return tuple(helps), tuple(hurts)


def _start_movement(before: object, after: object) -> int:
    """+1 when the starting value moved to the safe side, -1 when away."""

    first = getattr(before, "start", None)
    second = getattr(after, "start", None)
    covenant = getattr(before, "covenant", None)
    if first is None or second is None or covenant is None:
        return 0
    tolerance = abs(float(covenant.threshold)) * 0.005
    change = float(second) - float(first)
    if abs(change) <= tolerance:
        return 0
    better = change < 0 if covenant.direction == "max" else change > 0
    return 1 if better else -1


def _round_up(value: float, step: float, maximum: float) -> float:
    if step <= 0:
        return min(value, maximum)
    steps = -(-value // step)
    return min(steps * step, maximum)


def _state_key(state: ProForma) -> tuple[object, ...]:
    return (
        state.cash,
        round(state.non_cash_assets, 6),
        round(state.inventory, 6),
        round(state.short_debt, 6),
        round(state.long_debt, 6),
        round(state.non_debt_liabilities, 6),
        round(state.tangible_net_worth, 6),
        round(state.rate_cut, 8),
        round(state.ebit_uplift, 6),
        round(state.borrowing_cap, 6),
        tuple(sorted((key, round(value, 6)) for key, value in state.threshold_relief.items())),
    )


def _content_hash(
    position: BorrowerPosition,
    draws: Draws,
    baseline: Outcome,
    plan: Sequence[PlanStep],
    plan_outcome: Outcome,
) -> str:
    payload = {
        "model": MODEL_VERSION,
        "borrower": position.reference,
        "as_of": position.as_of.isoformat(),
        "seed": draws.seed,
        "paths": draws.paths,
        "horizon": draws.horizon,
        "covenants": [
            [item.reference, item.definition_ref, str(item.threshold), item.direction]
            for item in position.covenants
        ],
        "baseline": {
            item.reference: round(item.breach_within_horizon, 6) for item in baseline.outlooks
        },
        "plan": [[step.lever.code, round(step.size, 4)] for step in plan],
        "plan_outcome": {
            item.reference: round(item.breach_within_horizon, 6) for item in plan_outcome.outlooks
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


Evaluate = Callable[[ProForma], Outcome]

__all__ = [
    "MODEL_VERSION",
    "SOLVE_PATHS",
    "TARGET_PROBABILITY",
    "CustomPlan",
    "LeverAssessment",
    "PlanStep",
    "RemediationReport",
    "StressResult",
    "build_report",
    "custom_plan",
]
