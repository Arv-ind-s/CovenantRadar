"""Seeded Monte Carlo projection of a borrower's covenant lines.

Each path walks the borrower's filed lines forward a quarter at a time:

* operating profit follows the borrower's own run-rate and trend, with a
  surprise drawn at the borrower's own dispersion that persists partly into
  the next quarter;
* borrowings drift as the distinct filed balance sheets have drifted, and
  finance cost is the implied cost of debt on the borrowings outstanding;
* net worth accumulates the retained share of profit after tax (a loss is
  taken in full);
* non-cash current assets and non-debt current liabilities drift as filed,
  with correlated surprises.

Every covenant is re-tested on each projected quarter.  The draws are made
once per borrower and shared by the baseline and every plan — common random
numbers — so the difference between two outcomes is the plan, not sampling
noise.  The seed is derived from the borrower and the filed period, so the
same filings always produce the same numbers.

Pure: standard-library ``random`` only, no I/O.
"""

from __future__ import annotations

import hashlib
import math
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from types import MappingProxyType
from typing import Final

from covenant_radar.domain.remediation.position import (
    EBIT_PERSISTENCE,
    RETENTION,
    TAX_RATE,
    BorrowerPosition,
    CovenantTerm,
    Drivers,
    line_ratio,
    ratio_value,
)

DEFAULT_PATHS: Final[int] = 1000
DEFAULT_HORIZON_QUARTERS: Final[int] = 4
_WC_CORRELATION: Final[float] = 0.6
_SEED_SALT: Final[str] = "covenant-radar.remediation.v1"


@dataclass(frozen=True, slots=True)
class ProForma:
    """The filed line state after a plan's one-off movements.

    Balance-sheet movements (a prepayment, an equity issue, a term-out) are
    applied here at the start of the projection.  Ongoing effects — a lower
    cost of debt, an operating-cost saving, a threshold reset — are carried
    as modifiers and applied inside every projected quarter.
    """

    cash: float | None
    non_cash_assets: float
    inventory: float
    short_debt: float
    long_debt: float
    non_debt_liabilities: float
    tangible_net_worth: float
    rate_cut: float = 0.0
    ebit_uplift: float = 0.0
    borrowing_cap: float = 0.0
    threshold_relief: Mapping[str, float] = field(default_factory=dict)

    @property
    def total_debt(self) -> float:
        return self.short_debt + self.long_debt

    @property
    def current_assets(self) -> float:
        return (self.cash or 0.0) + self.non_cash_assets

    @property
    def current_liabilities(self) -> float:
        return self.short_debt + self.non_debt_liabilities

    def moved(self, **changes: float | None) -> ProForma:
        return replace(self, **changes)  # type: ignore[arg-type]

    def relieved(self, reference: str, amount: float) -> ProForma:
        relief = dict(self.threshold_relief)
        relief[reference] = relief.get(reference, 0.0) + amount
        return replace(self, threshold_relief=MappingProxyType(relief))


def filed_pro_forma(position: BorrowerPosition) -> ProForma:
    """The do-nothing starting state: the latest filed lines as they stand."""

    latest = position.latest
    current_assets = latest.get("current_assets") or 0.0
    current_liabilities = latest.get("current_liabilities") or 0.0
    cash = latest.get("cash_and_bank")
    short_debt = latest.get("short_term_debt")
    long_debt = latest.get("long_term_debt")
    total_debt = latest.get("total_debt") or 0.0
    if short_debt is None and long_debt is None:
        short_debt, long_debt = 0.0, total_debt
    elif short_debt is None:
        short_debt = max(total_debt - (long_debt or 0.0), 0.0)
    elif long_debt is None:
        long_debt = max(total_debt - short_debt, 0.0)
    return ProForma(
        cash=cash,
        non_cash_assets=current_assets - (cash or 0.0),
        inventory=latest.get("inventory") or 0.0,
        short_debt=short_debt or 0.0,
        long_debt=long_debt or 0.0,
        non_debt_liabilities=current_liabilities - (short_debt or 0.0),
        tangible_net_worth=latest.get("tangible_net_worth") or 0.0,
    )


def pro_forma_lines(
    position: BorrowerPosition, drivers: Drivers, state: ProForma
) -> dict[str, Decimal]:
    """The latest filed lines restated for a plan's one-off movements.

    The do-nothing state reproduces the filed lines exactly, so a covenant's
    starting value equals the value the engine tested.  Finance cost is the
    filed figure less the run-rate saving on borrowings retired and on any
    repricing, which is what the next test would see.
    """

    filed = filed_pro_forma(position)
    lines = dict(position.latest.lines)

    def put(code: str, value: float, before: float) -> None:
        if code in lines or value != before:
            delta = Decimal(repr(value)) - Decimal(repr(before))
            lines[code] = lines.get(code, Decimal(repr(before))) + delta

    put("current_assets", state.current_assets, filed.current_assets)
    put("current_liabilities", state.current_liabilities, filed.current_liabilities)
    put("short_term_debt", state.short_debt, filed.short_debt)
    put("long_term_debt", state.long_debt, filed.long_debt)
    put("total_debt", state.total_debt, filed.total_debt)
    put("tangible_net_worth", state.tangible_net_worth, filed.tangible_net_worth)
    if filed.cash is not None and state.cash is not None:
        put("cash_and_bank", state.cash, filed.cash)
    put("inventory", state.inventory, filed.inventory)
    if drivers.debt_rate is not None and "finance_cost" in lines:
        saving = (filed.total_debt - state.total_debt) * drivers.debt_rate / 4.0
        saving += state.total_debt * state.rate_cut / 4.0
        put("finance_cost", -saving, 0.0)
    if state.ebit_uplift and "ebit" in lines:
        put("ebit", state.ebit_uplift, 0.0)
        if "ebitda" in lines:
            put("ebitda", state.ebit_uplift, 0.0)
    return lines


@dataclass(frozen=True, slots=True)
class Stress:
    """A uniform shock applied on top of a projection."""

    code: str
    label: str
    ebit_shock: float = 0.0
    rate_shock: float = 0.0


STRESSES: Final[tuple[Stress, ...]] = (
    Stress("ebit-down-20", "Operating profit 20% lower", ebit_shock=-0.20),
    Stress("rates-up-150", "Cost of debt +150 bp", rate_shock=0.015),
    Stress("combined", "Both together", ebit_shock=-0.20, rate_shock=0.015),
)


@dataclass(frozen=True, slots=True)
class Draws:
    """Standard-normal draws shared by every projection of one borrower."""

    seed: int
    ebit: tuple[tuple[float, ...], ...]
    debt: tuple[tuple[float, ...], ...]
    assets: tuple[tuple[float, ...], ...]
    liabilities: tuple[tuple[float, ...], ...]

    @property
    def paths(self) -> int:
        return len(self.ebit)

    @property
    def horizon(self) -> int:
        return len(self.ebit[0]) if self.ebit else 0


def seed_for(position: BorrowerPosition) -> int:
    """A stable seed: the same borrower and filing always draw the same paths."""

    text = f"{_SEED_SALT}|{position.reference}|{position.as_of.isoformat()}"
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")


def make_draws(
    seed: int,
    *,
    paths: int = DEFAULT_PATHS,
    horizon: int = DEFAULT_HORIZON_QUARTERS,
) -> Draws:
    if not 1 <= paths <= 20000:
        raise ValueError("paths must be between 1 and 20000.")
    if not 1 <= horizon <= 12:
        raise ValueError("horizon must be between 1 and 12 quarters.")
    rng = random.Random(seed)
    gauss = rng.gauss

    def block() -> tuple[tuple[float, ...], ...]:
        return tuple(tuple(gauss(0.0, 1.0) for _ in range(horizon)) for _ in range(paths))

    return Draws(seed=seed, ebit=block(), debt=block(), assets=block(), liabilities=block())


@dataclass(frozen=True, slots=True)
class CovenantOutlook:
    """One covenant's projected distribution under one plan."""

    covenant: CovenantTerm
    start: Decimal | None
    threshold: Decimal
    quantiles: tuple[tuple[float, float, float], ...]
    breach_by_quarter: tuple[float, ...]
    breach_within_horizon: float

    @property
    def reference(self) -> str:
        return self.covenant.reference

    @property
    def median_shortfall(self) -> float:
        """How far the final quarter's median sits beyond the threshold, relative to it."""

        if not self.quantiles:
            return 0.0
        median = self.quantiles[-1][1]
        threshold = float(self.threshold)
        if not math.isfinite(median) or threshold == 0:
            return 0.0
        beyond = median - threshold if self.covenant.direction == "max" else threshold - median
        return max(beyond, 0.0) / abs(threshold)

    @property
    def start_breached(self) -> bool:
        if self.start is None:
            return False
        value = float(self.start)
        threshold = float(self.threshold)
        # The boundary itself is a breach, as in the covenant engine.
        return value >= threshold if self.covenant.direction == "max" else value <= threshold


@dataclass(frozen=True, slots=True)
class Outcome:
    """Every simulated covenant's outlook under one plan."""

    outlooks: tuple[CovenantOutlook, ...]
    paths: int
    horizon: int
    seed: int

    def get(self, reference: str) -> CovenantOutlook | None:
        return next((item for item in self.outlooks if item.reference == reference), None)

    def probability(self, reference: str) -> float:
        outlook = self.get(reference)
        return 0.0 if outlook is None else outlook.breach_within_horizon

    def severity(self, reference: str) -> float:
        """Breach probability plus how far the median ends beyond the threshold.

        Probability saturates at one for a covenant deep in breach, which
        would make every partial remedy look worthless; the second term keeps
        measuring progress until the median is back on the safe side.
        """

        outlook = self.get(reference)
        if outlook is None:
            return 0.0
        return outlook.breach_within_horizon + outlook.median_shortfall

    @property
    def any_breach(self) -> float:
        return max((item.breach_within_horizon for item in self.outlooks), default=0.0)


def project(
    position: BorrowerPosition,
    drivers: Drivers,
    state: ProForma,
    draws: Draws,
    *,
    stress: Stress | None = None,
    paths: int | None = None,
) -> Outcome:
    """Project every simulated covenant under ``state`` on the shared draws."""

    covenants = tuple(item for item in position.covenants if item.simulated)
    count = draws.paths if paths is None else min(paths, draws.paths)
    horizon = draws.horizon
    filed = filed_pro_forma(position)
    filed_debt = filed.total_debt
    short_share = filed.short_debt / filed_debt if filed_debt > 0 else 0.0
    inventory_share = state.inventory / state.non_cash_assets if state.non_cash_assets > 0 else 0.0
    filed_finance = position.latest.get("finance_cost") or 0.0
    ebit_shock = 1.0 + (stress.ebit_shock if stress else 0.0)
    rate_shock = stress.rate_shock if stress else 0.0
    innovation = math.sqrt(1.0 - EBIT_PERSISTENCE**2)
    drift = (
        drivers.debt_drift * (1.0 - state.borrowing_cap)
        if drivers.debt_drift > 0
        else drivers.debt_drift
    )
    independent = math.sqrt(1.0 - _WC_CORRELATION**2)
    trend = [
        drivers.ebit_run_rate * (1.0 + drivers.ebit_growth) ** (quarter + 1)
        for quarter in range(horizon)
    ]
    spread = [
        drivers.ebit_dispersion * abs(level)
        if drivers.ebit_run_rate > 0
        else drivers.ebit_dispersion_abs
        for level in trend
    ]
    # The latest filed quarter's own surprise against the run-rate starts
    # every path, so the projection continues from the filing rather than
    # jumping to the median; it decays at the persistence rate.
    latest_ebit = position.latest.get("ebit")
    opening_spread = (
        drivers.ebit_dispersion * abs(drivers.ebit_run_rate)
        if drivers.ebit_run_rate > 0
        else drivers.ebit_dispersion_abs
    )
    opening = 0.0
    if latest_ebit is not None and opening_spread > 0:
        opening = max(min((latest_ebit - drivers.ebit_run_rate) / opening_spread, 2.0), -2.0)
    thresholds = {
        item.reference: float(item.threshold)
        + _relief_sign(item) * state.threshold_relief.get(item.reference, 0.0)
        for item in covenants
    }

    values: dict[str, list[list[float]]] = {
        item.reference: [[0.0] * count for _ in range(horizon)] for item in covenants
    }
    breaches: dict[str, list[int]] = {item.reference: [0] * horizon for item in covenants}
    ever: dict[str, int] = {item.reference: 0 for item in covenants}

    for path in range(count):
        z_ebit = draws.ebit[path]
        z_debt = draws.debt[path]
        z_assets = draws.assets[path]
        z_liabilities = draws.liabilities[path]
        surprise = opening
        debt_change = 0.0
        non_cash_assets = state.non_cash_assets
        non_debt_liabilities = state.non_debt_liabilities
        net_worth = state.tangible_net_worth
        breached_once = dict.fromkeys(ever, False)
        for quarter in range(horizon):
            surprise = EBIT_PERSISTENCE * surprise + innovation * z_ebit[quarter]
            ebit = (trend[quarter] + spread[quarter] * surprise) * ebit_shock
            ebit += state.ebit_uplift * (0.5 if quarter == 0 else 1.0)
            debt_change += drift + drivers.debt_dispersion * z_debt[quarter]
            total_debt = max(state.total_debt + debt_change, 0.0)
            short_debt = max(state.short_debt + short_share * debt_change, 0.0)
            short_debt = min(short_debt, total_debt)
            non_cash_assets += drivers.nca_drift + drivers.nca_dispersion * z_assets[quarter]
            non_debt_liabilities += drivers.ndcl_drift + drivers.ndcl_dispersion * (
                _WC_CORRELATION * z_assets[quarter] + independent * z_liabilities[quarter]
            )
            if drivers.debt_rate is not None:
                rate = drivers.debt_rate + rate_shock - (state.rate_cut if quarter >= 1 else 0.0)
                finance_cost = drivers.finance_cost_other + max(rate, 0.0) / 4.0 * total_debt
            else:
                finance_cost = filed_finance * (1.0 + rate_shock * 10.0)
            profit = ebit - finance_cost
            net_worth += profit * (1.0 - TAX_RATE) * RETENTION if profit > 0 else profit
            lines = {
                "total_debt": total_debt,
                "tangible_net_worth": net_worth,
                "ebit": ebit,
                "ebitda": ebit + drivers.depreciation,
                "finance_cost": finance_cost,
                "current_assets": (state.cash or 0.0) + non_cash_assets,
                "current_liabilities": short_debt + non_debt_liabilities,
                "inventory": max(non_cash_assets, 0.0) * inventory_share,
                "cash_and_bank": state.cash or 0.0,
            }
            for covenant in covenants:
                reference = covenant.reference
                value = line_ratio(covenant.definition_ref, lines)
                if value is None:
                    # A non-positive denominator: negative net worth breaches
                    # a leverage cap; no finance cost or no current
                    # liabilities clears a floor.
                    value = math.inf
                values[reference][quarter][path] = value
                threshold = thresholds[reference]
                failed = value >= threshold if covenant.direction == "max" else value <= threshold
                if failed:
                    breaches[reference][quarter] += 1
                    breached_once[reference] = True
        for reference, failed in breached_once.items():
            if failed:
                ever[reference] += 1

    start_lines = pro_forma_lines(position, drivers, state)
    outlooks = []
    for covenant in covenants:
        reference = covenant.reference
        relief = state.threshold_relief.get(reference, 0.0)
        tested = covenant.threshold + Decimal(repr(round(_relief_sign(covenant) * relief, 4)))
        outlooks.append(
            CovenantOutlook(
                covenant=covenant,
                start=ratio_value(covenant.definition_ref, start_lines),
                threshold=tested,
                quantiles=tuple(_quantiles(row) for row in values[reference]),
                breach_by_quarter=tuple(count_ / count for count_ in breaches[reference]),
                breach_within_horizon=ever[reference] / count,
            )
        )
    return Outcome(outlooks=tuple(outlooks), paths=count, horizon=horizon, seed=draws.seed)


def _relief_sign(covenant: CovenantTerm) -> float:
    """A reset moves a maximum up and a minimum down."""

    return 1.0 if covenant.direction == "max" else -1.0


def _quantiles(values: Sequence[float]) -> tuple[float, float, float]:
    ordered = sorted(value for value in values if math.isfinite(value))
    if not ordered:
        return (math.inf, math.inf, math.inf)
    last = len(ordered) - 1
    return (
        ordered[round(0.1 * last)],
        ordered[round(0.5 * last)],
        ordered[round(0.9 * last)],
    )


__all__ = [
    "DEFAULT_HORIZON_QUARTERS",
    "DEFAULT_PATHS",
    "STRESSES",
    "CovenantOutlook",
    "Draws",
    "Outcome",
    "ProForma",
    "Stress",
    "filed_pro_forma",
    "make_draws",
    "pro_forma_lines",
    "project",
    "seed_for",
]
