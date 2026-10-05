"""A borrower's filed position and the drivers its own history supports.

The remediation simulator never starts from a generic constant.  Everything
it projects is anchored on two things this module reads from the borrower's
standing filings:

* the **filed position** — the latest period's normalised statement lines,
  exactly the lines the covenant engine tested; and
* the **drivers** — run-rate, trend and dispersion of operating profit, the
  implied cost of debt, and the drift of debt and working capital — each
  estimated from the borrower's own quarters and stated with the quarters it
  came from.

Two traps in Indian listed-company filings are handled here rather than left
to every caller.  Balance sheets are filed half-yearly, so a June or December
period carries the previous sheet forward; balance-sheet drift is estimated
over *distinct* sheets only, never over the carried copies.  And operating
profit is lumpy (acquisitions, seasonality), so the run-rate is a median and
every trend and drift is clamped to a stated bound instead of extrapolated.

Pure: no database, no framework, no randomness.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from covenant_radar.domain.ratios.compute import compute_ratio
from covenant_radar.domain.ratios.library import LIBRARY

_ZERO: Final[Decimal] = Decimal("0")
_DAYS_IN_QUARTER: Final[float] = 91.0

#: Statutory rate under section 115BAA, used for projected profit after tax.
TAX_RATE: Final[float] = 0.2517
#: Share of projected profit after tax retained in net worth.
RETENTION: Final[float] = 0.75
#: Persistence of a quarter's operating-profit surprise into the next.
EBIT_PERSISTENCE: Final[float] = 0.5

_EBIT_GROWTH_BOUND: Final[float] = 0.05
_EBIT_DISPERSION_BOUNDS: Final[tuple[float, float]] = (0.05, 0.45)
_RATE_BOUNDS: Final[tuple[float, float]] = (0.03, 0.18)
_DEBT_DRIFT_BOUND: Final[float] = 0.03
_DEBT_DISPERSION_BOUNDS: Final[tuple[float, float]] = (0.005, 0.03)
_WC_DRIFT_BOUND: Final[float] = 0.04
_WC_DISPERSION_BOUNDS: Final[tuple[float, float]] = (0.01, 0.05)

#: Balance-sheet lines whose equality marks a carried-forward sheet.
_SHEET_KEY: Final[tuple[str, ...]] = (
    "current_assets",
    "current_liabilities",
    "total_debt",
    "tangible_net_worth",
)

#: The covenants the line model can project, as (numerator, denominator)
#: signed line terms.  Each mirrors the ratio library's own formula; a unit
#: test holds the two equal on every demo borrower so they cannot drift.
SIMULATED_RATIOS: Final[
    Mapping[str, tuple[tuple[tuple[str, int], ...], tuple[tuple[str, int], ...]]]
] = {
    "leverage_ratio": ((("total_debt", 1),), (("tangible_net_worth", 1),)),
    "interest_coverage_ratio": ((("ebit", 1),), (("finance_cost", 1),)),
    "fixed_charge_coverage_ratio": ((("ebitda", 1),), (("finance_cost", 1),)),
    "current_ratio": ((("current_assets", 1),), (("current_liabilities", 1),)),
    "quick_ratio": (
        (("current_assets", 1), ("inventory", -1)),
        (("current_liabilities", 1),),
    ),
    "debt_to_ebitda": ((("total_debt", 1),), (("ebitda", 1),)),
    "net_debt_to_ebitda": (
        (("total_debt", 1), ("cash_and_bank", -1)),
        (("ebitda", 1),),
    ),
}


@dataclass(frozen=True, slots=True)
class QuarterLines:
    """One standing financial period's normalised lines, in ₹ crore."""

    period_end: date
    lines: Mapping[str, Decimal]

    def get(self, code: str) -> float | None:
        value = self.lines.get(code)
        return None if value is None else float(value)


@dataclass(frozen=True, slots=True)
class CovenantTerm:
    """The covenant version in force, reduced to what a projection tests."""

    reference: str
    name: str
    definition_ref: str
    threshold: Decimal
    direction: str
    unit: str = "x"

    @property
    def simulated(self) -> bool:
        return self.definition_ref in SIMULATED_RATIOS and self.direction in {"min", "max"}

    def breached(self, value: float) -> bool:
        """The engine's convention: the boundary itself is a breach (``evaluate.py``)."""

        threshold = float(self.threshold)
        if self.direction == "max":
            return value >= threshold
        if self.direction == "min":
            return value <= threshold
        return False


@dataclass(frozen=True, slots=True)
class BorrowerPosition:
    """The borrower, its covenants, and its standing filings oldest first."""

    reference: str
    name: str
    industry_code: str | None
    quarters: tuple[QuarterLines, ...]
    covenants: tuple[CovenantTerm, ...]

    def __post_init__(self) -> None:
        if not self.quarters:
            raise ValueError("a position requires at least one filed period.")
        ordered = tuple(sorted(self.quarters, key=lambda quarter: quarter.period_end))
        object.__setattr__(self, "quarters", ordered)

    @property
    def latest(self) -> QuarterLines:
        return self.quarters[-1]

    @property
    def as_of(self) -> date:
        return self.latest.period_end

    def distinct_sheets(self) -> tuple[QuarterLines, ...]:
        """Return the periods whose balance sheet was actually filed.

        A period whose balance-sheet lines equal its predecessor's carries
        that sheet forward; the first period of each run is the filing.
        """

        sheets: list[QuarterLines] = []
        previous: tuple[Decimal | None, ...] | None = None
        for quarter in self.quarters:
            key = tuple(quarter.lines.get(code) for code in _SHEET_KEY)
            if key != previous:
                sheets.append(quarter)
            previous = key
        return tuple(sheets)

    def filed_value(self, covenant: CovenantTerm) -> Decimal | None:
        """The covenant's value on the latest filed lines, through the library."""

        return ratio_value(covenant.definition_ref, self.latest.lines)


def ratio_value(code: str, lines: Mapping[str, Decimal]) -> Decimal | None:
    """Compute one ratio through the ratio library's own formula."""

    definition = LIBRARY.get(code)
    if definition is None:
        return None
    result = compute_ratio(definition, lines, None)
    return result.value if result.computable else None


def line_ratio(code: str, lines: Mapping[str, float]) -> float | None:
    """Float twin of the library formula for the simulated ratios.

    Returns ``None`` when the denominator is zero or negative, which the
    caller resolves by covenant direction: a negative net worth breaches a
    maximum-leverage covenant, a zero finance cost passes a coverage floor.
    """

    terms = SIMULATED_RATIOS.get(code)
    if terms is None:
        return None
    numerator_terms, denominator_terms = terms
    numerator = sum(lines.get(name, 0.0) * sign for name, sign in numerator_terms)
    denominator = sum(lines.get(name, 0.0) * sign for name, sign in denominator_terms)
    if denominator <= 0:
        return None
    return numerator / denominator


@dataclass(frozen=True, slots=True)
class Driver:
    """One estimated driver, with the evidence and the bound applied."""

    code: str
    label: str
    value: float
    unit: str
    basis: str
    clamped: bool = False


@dataclass(frozen=True, slots=True)
class Drivers:
    """Everything the projection needs, estimated from the borrower's filings."""

    ebit_run_rate: float
    ebit_growth: float
    ebit_dispersion: float
    ebit_dispersion_abs: float
    depreciation: float
    cash_operating_cost: float
    revenue_run_rate: float
    debt_rate: float | None
    finance_cost_other: float
    debt_drift: float
    debt_dispersion: float
    nca_drift: float
    nca_dispersion: float
    ndcl_drift: float
    ndcl_dispersion: float
    evidence: tuple[Driver, ...]

    def driver(self, code: str) -> Driver | None:
        return next((item for item in self.evidence if item.code == code), None)


def estimate_drivers(position: BorrowerPosition) -> Drivers:
    """Estimate the projection drivers from the borrower's own filings."""

    quarters = position.quarters
    latest = position.latest
    labels = [_quarter_label(quarter.period_end) for quarter in quarters]
    evidence: list[Driver] = []

    ebit = [quarter.get("ebit") for quarter in quarters]
    ebit_known = [value for value in ebit if value is not None]
    recent = ebit_known[-4:]
    run_rate = statistics.median(recent) if recent else 0.0
    evidence.append(
        Driver(
            "ebit_run_rate",
            "Operating profit (EBIT) run-rate",
            run_rate,
            "₹ cr / quarter",
            f"Median EBIT of the last {len(recent)} filed quarters "
            f"({labels[-len(recent)]}–{labels[-1]})."
            if recent
            else "No EBIT filed.",
        )
    )

    growth = 0.0
    growth_clamped = False
    if len(ebit_known) >= 8:
        trailing = sum(ebit_known[-4:])
        prior = sum(ebit_known[-8:-4])
        if trailing > 0 and prior > 0:
            raw = (trailing / prior) ** 0.25 - 1.0
            growth = _clamp(raw, -_EBIT_GROWTH_BOUND, _EBIT_GROWTH_BOUND)
            growth_clamped = growth != raw
    evidence.append(
        Driver(
            "ebit_growth",
            "EBIT trend",
            growth,
            "% / quarter",
            (
                "Compound quarterly change, trailing four quarters against the four before"
                + (f", capped at ±{_EBIT_GROWTH_BOUND:.0%} a quarter." if growth_clamped else ".")
                if len(ebit_known) >= 8
                else "Fewer than eight quarters filed; held flat."
            ),
            growth_clamped,
        )
    )

    dispersion = _EBIT_DISPERSION_BOUNDS[0]
    dispersion_abs = abs(run_rate) * dispersion
    if len(ebit_known) >= 4:
        centre = len(ebit_known) - 2.5
        relative = []
        for index, value in enumerate(ebit_known):
            fitted = run_rate * (1.0 + growth) ** (index - centre)
            if fitted > 0:
                relative.append(value / fitted - 1.0)
        if len(relative) >= 3 and run_rate > 0:
            raw = statistics.pstdev(relative)
            dispersion = _clamp(raw, *_EBIT_DISPERSION_BOUNDS)
            dispersion_abs = run_rate * dispersion
        else:
            dispersion_abs = max(statistics.pstdev(ebit_known), abs(run_rate) * 0.1, 1.0)
    evidence.append(
        Driver(
            "ebit_dispersion",
            "EBIT quarter-to-quarter dispersion",
            dispersion,
            "± share of run-rate (1σ)",
            f"Spread of the {len(ebit_known)} filed quarters around the trend, bounded "
            f"{_EBIT_DISPERSION_BOUNDS[0]:.0%}–{_EBIT_DISPERSION_BOUNDS[1]:.0%}; "
            f"surprises persist {EBIT_PERSISTENCE:.0%} into the next quarter.",
        )
    )

    depreciation = _median_recent(quarters, "depreciation")
    revenue = _median_recent(quarters, "revenue")
    ebitda = _median_recent(quarters, "ebitda")
    cash_cost = max(revenue - ebitda, 0.0)

    debt = latest.get("total_debt") or 0.0
    finance_recent = [
        value for value in (q.get("finance_cost") for q in quarters[-2:]) if value is not None
    ]
    finance_cost = statistics.median(finance_recent) if finance_recent else 0.0
    debt_rate: float | None = None
    finance_other = 0.0
    if debt > 0 and finance_cost > 0:
        raw = 4.0 * finance_cost / debt
        rate = min(max(raw, _RATE_BOUNDS[0]), _RATE_BOUNDS[1])
        debt_rate = rate
        # Finance cost beyond the bounded rate on filed borrowings is lease
        # interest and other charges a debt repayment does not remove.
        finance_other = max(finance_cost - debt * rate / 4.0, 0.0)
        evidence.append(
            Driver(
                "debt_rate",
                "Implied cost of borrowings",
                rate,
                "% a year",
                f"Annualised median finance cost of {labels[-len(finance_recent)]}–{labels[-1]} "
                f"(₹{finance_cost:,.0f} cr a quarter) over filed borrowings of ₹{debt:,.0f} cr"
                + (
                    f"; bounded at {_RATE_BOUNDS[1]:.0%}, the excess ₹{finance_other:,.0f} cr "
                    "a quarter treated as lease and other finance charges."
                    if raw > _RATE_BOUNDS[1]
                    else "."
                ),
                raw != rate,
            )
        )
    else:
        finance_other = finance_cost

    sheets = position.distinct_sheets()
    sheet_labels = ", ".join(_quarter_label(sheet.period_end) for sheet in sheets)

    def per_quarter_changes(extract: object) -> list[float]:
        changes: list[float] = []
        for before, after in zip(sheets, sheets[1:], strict=False):
            first = extract(before)  # type: ignore[operator]
            second = extract(after)  # type: ignore[operator]
            if first is None or second is None:
                continue
            gap = max(round((after.period_end - before.period_end).days / _DAYS_IN_QUARTER), 1)
            changes.append((second - first) / gap)
        return changes

    def drift_and_dispersion(
        changes: list[float], level: float, drift_bound: float, bounds: tuple[float, float]
    ) -> tuple[float, float, bool]:
        scale = max(abs(level), 1.0)
        if not changes:
            return 0.0, scale * bounds[0], False
        raw = statistics.median(changes)
        drift = _clamp(raw, -drift_bound * scale, drift_bound * scale)
        spread = statistics.pstdev(changes) / math.sqrt(2.0) if len(changes) > 1 else 0.0
        return drift, _clamp(spread, bounds[0] * scale, bounds[1] * scale), drift != raw

    debt_drift, debt_dispersion, debt_clamped = drift_and_dispersion(
        per_quarter_changes(lambda q: q.get("total_debt")),
        debt,
        _DEBT_DRIFT_BOUND,
        _DEBT_DISPERSION_BOUNDS,
    )
    evidence.append(
        Driver(
            "debt_drift",
            "Borrowings drift",
            debt_drift,
            "₹ cr / quarter",
            f"Median change between distinct filed balance sheets ({sheet_labels})"
            + (
                f", capped at ±{_DEBT_DRIFT_BOUND:.0%} of borrowings a quarter."
                if debt_clamped
                else "."
            ),
            debt_clamped,
        )
    )

    cash = latest.get("cash_and_bank") or 0.0
    short_debt = latest.get("short_term_debt") or 0.0
    current_assets = latest.get("current_assets") or 0.0
    current_liabilities = latest.get("current_liabilities") or 0.0

    def non_cash_assets(q: QuarterLines) -> float | None:
        assets = q.get("current_assets")
        return None if assets is None else assets - (q.get("cash_and_bank") or 0.0)

    def non_debt_liabilities(q: QuarterLines) -> float | None:
        liabilities = q.get("current_liabilities")
        return None if liabilities is None else liabilities - (q.get("short_term_debt") or 0.0)

    nca_drift, nca_dispersion, nca_clamped = drift_and_dispersion(
        per_quarter_changes(non_cash_assets),
        current_assets - cash,
        _WC_DRIFT_BOUND,
        _WC_DISPERSION_BOUNDS,
    )
    ndcl_drift, ndcl_dispersion, ndcl_clamped = drift_and_dispersion(
        per_quarter_changes(non_debt_liabilities),
        current_liabilities - short_debt,
        _WC_DRIFT_BOUND,
        _WC_DISPERSION_BOUNDS,
    )
    evidence.append(
        Driver(
            "working_capital_drift",
            "Working-capital drift",
            nca_drift - ndcl_drift,
            "₹ cr / quarter",
            f"Non-cash current assets {nca_drift:+,.0f} and non-debt current liabilities "
            f"{ndcl_drift:+,.0f} a quarter, median of distinct sheets ({sheet_labels})"
            + (
                f", each capped at ±{_WC_DRIFT_BOUND:.0%} a quarter."
                if nca_clamped or ndcl_clamped
                else "."
            ),
            nca_clamped or ndcl_clamped,
        )
    )

    return Drivers(
        ebit_run_rate=run_rate,
        ebit_growth=growth,
        ebit_dispersion=dispersion,
        ebit_dispersion_abs=dispersion_abs,
        depreciation=depreciation,
        cash_operating_cost=cash_cost,
        revenue_run_rate=revenue,
        debt_rate=debt_rate,
        finance_cost_other=finance_other,
        debt_drift=debt_drift,
        debt_dispersion=debt_dispersion,
        nca_drift=nca_drift,
        nca_dispersion=nca_dispersion,
        ndcl_drift=ndcl_drift,
        ndcl_dispersion=ndcl_dispersion,
        evidence=tuple(evidence),
    )


def _median_recent(quarters: Sequence[QuarterLines], code: str, count: int = 4) -> float:
    values = [
        value for value in (quarter.get(code) for quarter in quarters[-count:]) if value is not None
    ]
    return statistics.median(values) if values else 0.0


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


def _quarter_label(value: date) -> str:
    return f"{value:%b %Y}"


__all__ = [
    "EBIT_PERSISTENCE",
    "RETENTION",
    "SIMULATED_RATIOS",
    "TAX_RATE",
    "BorrowerPosition",
    "CovenantTerm",
    "Driver",
    "Drivers",
    "QuarterLines",
    "estimate_drivers",
    "line_ratio",
    "ratio_value",
]
