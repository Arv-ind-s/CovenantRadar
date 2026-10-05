"""Borrower-specific remediation levers.

A lever is a *mechanism* the bank owns — "prepay borrowings from surplus
cash", "term out short-term lines" — never a fixed effect.  How large it can
be, what it would fund, and which covenant it helps or hurts are worked out
from the borrower's own filed lines every time:

* its **capacity** is bounded by what the borrower actually has (cash above
  an operating buffer, short-term lines outstanding, receivable days above
  its own best filed level, a cost of debt above the reference rate);
* its **rationale** quotes those figures and the period they were filed in;
* an **unavailable** lever stays visible with the reason, so a reader can
  see that a cash prepayment was considered and why it cannot help.

Every lever moves statement lines, not a ratio, so one action is tested
against every covenant at once — a cash prepayment that cures leverage can
still weaken the current ratio, and the simulator shows both.

All outputs are advisory (``spec P-01``): owners are named and anything
that needs a lender's decision says so.  Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final

from covenant_radar.domain.remediation.montecarlo import ProForma
from covenant_radar.domain.remediation.position import BorrowerPosition, Drivers, QuarterLines

#: Days of cash operating cost a borrower keeps before any cash is surplus.
OPERATING_BUFFER_DAYS: Final[int] = 30
#: Illustrative refinancing benchmark for an investment-grade term loan.
REFERENCE_BORROWING_RATE: Final[float] = 0.085
_MAX_REPRICING_BP: Final[float] = 300.0
_MIN_REPRICING_BP: Final[float] = 25.0
_MAX_COST_SAVING: Final[float] = 0.05
#: Largest fresh equity treated as plausible, as a share of net worth.
_MAX_EQUITY_SHARE: Final[float] = 0.25
_MAX_WORKING_CAPITAL_RELEASE: Final[float] = 0.25
_MAX_RESET_SHARE: Final[float] = 0.25
_DAYS_IN_QUARTER: Final[float] = 91.0
_LIQUIDITY_RATIOS: Final[frozenset[str]] = frozenset({"current_ratio", "quick_ratio"})
#: Industries whose inventory is land and work in progress, not stock.
_PROJECT_INVENTORY_PREFIXES: Final[tuple[str, ...]] = ("L68", "F41")


@dataclass(frozen=True, slots=True)
class Capacity:
    """What one lever can do for this borrower, before any size is chosen."""

    maximum: float
    step: float
    rationale: tuple[str, ...]
    assumptions: tuple[str, ...]
    unavailable_reason: str | None = None

    @property
    def available(self) -> bool:
        return self.unavailable_reason is None and self.maximum > 0


@dataclass(frozen=True, slots=True)
class Lever:
    """One bank-owned remediation mechanism."""

    code: str
    title: str
    owner: str
    unit: str
    order: int
    lender_decision: bool
    capacity: Callable[[BorrowerPosition, Drivers, ProForma], Capacity]
    apply: Callable[[ProForma, float, Drivers], ProForma]
    covenant: str | None = None
    #: Whether the lever may be proposed alone when it cures everything.
    #: Equity never is: dilution is asked of a promoter only after the
    #: cheaper levers have been tried.
    single_step: bool = True

    def describe_size(self, size: float) -> str:
        return format_size(size, self.unit)


def format_size(size: float, unit: str) -> str:
    if unit == "crore":
        return f"₹{size:,.0f} cr"
    if unit == "bp":
        return f"{size:,.0f} bp"
    if unit == "percent":
        return f"{size:.1f}%"
    if unit == "share":
        return f"{size:.0f}% of the trend"
    return f"{size:.2f}x"


def _crore(value: float) -> str:
    return f"₹{value:,.0f} cr"


def _label(value: date) -> str:
    return f"{value:%b %Y}"


def _step(maximum: float) -> float:
    """A slider step that gives about two hundred positions."""

    if maximum <= 0:
        return 1.0
    raw = maximum / 200.0
    for candidate in (1.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0):
        if raw <= candidate:
            return candidate
    return 1000.0


def _materiality(state: ProForma) -> float:
    return max(5.0, 0.0025 * state.total_debt)


def _sheet_revenue(position: BorrowerPosition, sheet: QuarterLines) -> float | None:
    return sheet.get("revenue")


def _sheet_cash_cost(sheet: QuarterLines) -> float | None:
    revenue = sheet.get("revenue")
    ebitda = sheet.get("ebitda")
    if revenue is None or ebitda is None or revenue - ebitda <= 0:
        return None
    return revenue - ebitda


def _retire_debt(state: ProForma, amount: float) -> ProForma:
    """Retire short-term lines first, then long-term; any excess stays as cash.

    Short-term first because it is never worse for the current ratio: both
    sides of it fall, rather than current assets alone.
    """

    short = min(amount, state.short_debt)
    long = min(amount - short, state.long_debt)
    excess = amount - short - long
    return state.moved(
        short_debt=state.short_debt - short,
        long_debt=state.long_debt - long,
        cash=None if state.cash is None and excess == 0 else (state.cash or 0.0) + excess,
    )


# ---------------------------------------------------------------------------
# Working-capital release
# ---------------------------------------------------------------------------


def _working_capital_capacity(
    position: BorrowerPosition, drivers: Drivers, state: ProForma
) -> Capacity:
    sheets = position.distinct_sheets()
    latest = sheets[-1]
    receivables = latest.get("receivables")
    inventory = latest.get("inventory")
    assumptions = (
        "Collection and stock days return to the borrower's own best filed level "
        "within the first projected quarter.",
        "Freed working capital retires short-term borrowings at once; any excess is held as cash.",
        f"Release is capped at {_MAX_WORKING_CAPITAL_RELEASE:.0%} of filed receivables "
        "and inventory.",
    )
    if receivables is None and inventory is None:
        return Capacity(
            0.0,
            1.0,
            (),
            assumptions,
            "Receivables and inventory are not reported separately in the filed lines.",
        )
    rationale: list[str] = []
    release = 0.0
    if receivables is not None:
        days = []
        for sheet in sheets:
            value, revenue = sheet.get("receivables"), _sheet_revenue(position, sheet)
            if value is not None and revenue and revenue > 0:
                days.append((value / revenue * _DAYS_IN_QUARTER, sheet.period_end))
        if days:
            current = days[-1][0]
            best, best_at = min(days)
            if best < current:
                freed = receivables * (1.0 - best / current)
                release += freed
                rationale.append(
                    f"Receivables {_crore(receivables)} are {current:.0f} days of sales on the "
                    f"{_label(latest.period_end)} sheet; the borrower collected in {best:.0f} days "
                    f"in {_label(best_at)}. Returning to that frees {_crore(freed)}."
                )
            else:
                rationale.append(
                    f"Receivables at {current:.0f} days are already the best filed level."
                )
    industry = (position.industry_code or "").upper()
    if inventory is not None and industry.startswith(_PROJECT_INVENTORY_PREFIXES):
        rationale.append(
            f"Inventory {_crore(inventory)} is land and work in progress for a "
            f"{industry} business, so it is not treated as releasable stock."
        )
    elif inventory is not None and inventory > 0:
        days = []
        for sheet in sheets:
            value, cost = sheet.get("inventory"), _sheet_cash_cost(sheet)
            if value is not None and cost:
                days.append((value / cost * _DAYS_IN_QUARTER, sheet.period_end))
        if days:
            current = days[-1][0]
            best, best_at = min(days)
            if best < current:
                freed = inventory * (1.0 - best / current)
                release += freed
                rationale.append(
                    f"Inventory {_crore(inventory)} is {current:.0f} days of cash operating cost; "
                    f"it was {best:.0f} days in {_label(best_at)}. Returning to that frees "
                    f"{_crore(freed)}."
                )
            else:
                rationale.append(
                    f"Inventory at {current:.0f} days is already the best filed level."
                )
    cap = _MAX_WORKING_CAPITAL_RELEASE * ((receivables or 0.0) + (inventory or 0.0))
    release = min(release, cap)
    if release < _materiality(state):
        return Capacity(
            0.0,
            1.0,
            tuple(rationale),
            assumptions,
            "Collection and stock days are already at, or within materiality of, the "
            "borrower's best filed level.",
        )
    return Capacity(release, _step(release), tuple(rationale), assumptions)


def _working_capital_apply(state: ProForma, size: float, drivers: Drivers) -> ProForma:
    share = state.inventory / state.non_cash_assets if state.non_cash_assets > 0 else 0.0
    released = state.moved(
        non_cash_assets=state.non_cash_assets - size,
        inventory=max(state.inventory - size * share, 0.0),
    )
    return _retire_debt(released, size)


# ---------------------------------------------------------------------------
# Prepayment from surplus cash
# ---------------------------------------------------------------------------


def _cash_capacity(position: BorrowerPosition, drivers: Drivers, state: ProForma) -> Capacity:
    buffer = drivers.cash_operating_cost * OPERATING_BUFFER_DAYS / _DAYS_IN_QUARTER
    assumptions = (
        f"The borrower keeps {OPERATING_BUFFER_DAYS} days of cash operating cost "
        f"({_crore(buffer)}) and only cash above it is surplus.",
        "The prepayment settles before the next covenant test date, without a prepayment penalty.",
        "Short-term lines are prepaid first, then term debt.",
    )
    if state.cash is None:
        return Capacity(
            0.0, 1.0, (), assumptions, "Cash is not reported separately in the filed lines."
        )
    surplus = state.cash - buffer
    as_of = _label(position.as_of)
    rationale = [
        f"Cash {_crore(state.cash)} against a {OPERATING_BUFFER_DAYS}-day operating buffer of "
        f"{_crore(buffer)} (cash costs {_crore(drivers.cash_operating_cost)} a quarter) leaves "
        f"{_crore(max(surplus, 0.0))} deployable, as filed for {as_of}."
    ]
    if state.current_assets < state.current_liabilities:
        rationale.append(
            f"Current assets {_crore(state.current_assets)} are below current liabilities "
            f"{_crore(state.current_liabilities)}, so any cash prepayment lowers the current "
            "ratio; prepaying short-term lines first limits that."
        )
    if surplus < _materiality(state):
        return Capacity(
            0.0,
            1.0,
            tuple(rationale),
            assumptions,
            f"Cash {_crore(state.cash)} is within the {OPERATING_BUFFER_DAYS}-day operating buffer "
            f"of {_crore(buffer)}; there is no surplus to deploy.",
        )
    maximum = min(surplus, state.total_debt)
    return Capacity(maximum, _step(maximum), tuple(rationale), assumptions)


def _cash_apply(state: ProForma, size: float, drivers: Drivers) -> ProForma:
    paid = state.moved(cash=(state.cash or 0.0) - size)
    short = min(size, paid.short_debt)
    return paid.moved(short_debt=paid.short_debt - short, long_debt=paid.long_debt - (size - short))


# ---------------------------------------------------------------------------
# Repricing
# ---------------------------------------------------------------------------


def _repricing_capacity(position: BorrowerPosition, drivers: Drivers, state: ProForma) -> Capacity:
    assumptions = (
        f"Refinanced at up to the {REFERENCE_BORROWING_RATE:.2%} reference rate for an "
        "investment-grade term loan; the bank configures the reference.",
        "The new pricing applies from the second projected quarter.",
        "No break cost is payable on the debt refinanced.",
    )
    if drivers.debt_rate is None or state.total_debt <= 0:
        return Capacity(
            0.0, 1.0, (), assumptions, "The borrower has no filed borrowings to reprice."
        )
    gap = (drivers.debt_rate - REFERENCE_BORROWING_RATE) * 10000.0
    quarterly = drivers.debt_rate * state.total_debt / 4.0
    rationale = (
        f"Borrowings {_crore(state.total_debt)} cost an implied {drivers.debt_rate:.2%} a year "
        f"({_crore(quarterly)} a quarter) against the {REFERENCE_BORROWING_RATE:.2%} reference; "
        f"each 25 bp saved adds {_crore(state.total_debt * 0.0025 / 4.0)} to quarterly cover.",
    )
    if gap < _MIN_REPRICING_BP:
        return Capacity(
            0.0,
            1.0,
            rationale,
            assumptions,
            f"The implied cost of {drivers.debt_rate:.2%} is already within "
            f"{_MIN_REPRICING_BP:.0f} bp of the reference rate.",
        )
    maximum = min(gap, _MAX_REPRICING_BP)
    return Capacity(maximum, 5.0, rationale, assumptions)


def _repricing_apply(state: ProForma, size: float, drivers: Drivers) -> ProForma:
    return state.moved(rate_cut=state.rate_cut + size / 10000.0)


# ---------------------------------------------------------------------------
# Borrowing cap
# ---------------------------------------------------------------------------


def _borrowing_cap_capacity(
    position: BorrowerPosition, drivers: Drivers, state: ProForma
) -> Capacity:
    assumptions = (
        "The cap applies to the trend in borrowings; quarter-to-quarter swings in "
        "drawn lines remain.",
        "Projects funded by the curtailed borrowing are deferred, not cancelled, and "
        "earnings are unaffected in the horizon.",
        "Set as a condition of continued lending; advisory until the lenders adopt it.",
    )
    sheets = position.distinct_sheets()
    drift = drivers.debt_drift
    if drift < _materiality(state) or len(sheets) < 2:
        return Capacity(
            0.0,
            1.0,
            (),
            assumptions,
            "Borrowings are not growing on the filed balance sheets, so a cap would not "
            "change the projection.",
        )
    first, last = sheets[0], sheets[-1]
    rationale = (
        f"Borrowings rose from {_crore(first.get('total_debt') or 0.0)} "
        f"({_label(first.period_end)}) to {_crore(last.get('total_debt') or 0.0)} "
        f"({_label(last.period_end)}); the projection carries that forward at "
        f"{_crore(drift)} a quarter. Holding new borrowing back is the lever on that drift.",
    )
    return Capacity(100.0, 5.0, rationale, assumptions)


def _borrowing_cap_apply(state: ProForma, size: float, drivers: Drivers) -> ProForma:
    return state.moved(borrowing_cap=min(state.borrowing_cap + size / 100.0, 1.0))


# ---------------------------------------------------------------------------
# Operating-cost programme
# ---------------------------------------------------------------------------


def _cost_capacity(position: BorrowerPosition, drivers: Drivers, state: ProForma) -> Capacity:
    assumptions = (
        "Half the saving is delivered in the first projected quarter and all of it thereafter.",
        "Revenue is unaffected by the saving.",
        f"A monitored programme is bounded at {_MAX_COST_SAVING:.0%} of cash operating cost.",
    )
    cost = drivers.cash_operating_cost
    if cost <= 0:
        return Capacity(
            0.0, 1.0, (), assumptions, "Cash operating cost cannot be derived from the filings."
        )
    rationale = (
        f"Cash operating cost runs at {_crore(cost)} a quarter (revenue less EBITDA, median of "
        f"the last four filed quarters); each 1% saved adds {_crore(cost / 100.0)} to quarterly "
        f"EBIT against a run-rate of {_crore(drivers.ebit_run_rate)}.",
    )
    return Capacity(_MAX_COST_SAVING * 100.0, 0.1, rationale, assumptions)


def _cost_apply(state: ProForma, size: float, drivers: Drivers) -> ProForma:
    return state.moved(ebit_uplift=state.ebit_uplift + drivers.cash_operating_cost * size / 100.0)


# ---------------------------------------------------------------------------
# Term-out of short-term borrowings
# ---------------------------------------------------------------------------


def _term_out_capacity(position: BorrowerPosition, drivers: Drivers, state: ProForma) -> Capacity:
    assumptions = (
        "Converted lines carry no instalment within twelve months of the conversion.",
        "Pricing is unchanged by the conversion.",
        "Requires the lenders' approval; shown as an option to put to them, not a decision.",
    )
    if not any(item.definition_ref in _LIQUIDITY_RATIOS for item in position.covenants):
        return Capacity(
            0.0, 1.0, (), assumptions, "No liquidity covenant depends on the tenor of borrowings."
        )
    rationale = (
        f"Short-term borrowings {_crore(state.short_debt)} sit inside current liabilities of "
        f"{_crore(state.current_liabilities)} against current assets of "
        f"{_crore(state.current_assets)}; converting them to term debt lifts the current ratio "
        "without changing leverage.",
    )
    if state.short_debt < _materiality(state):
        return Capacity(
            0.0,
            1.0,
            rationale,
            assumptions,
            "There are no material short-term borrowings to term out.",
        )
    return Capacity(state.short_debt, _step(state.short_debt), rationale, assumptions)


def _term_out_apply(state: ProForma, size: float, drivers: Drivers) -> ProForma:
    moved = min(size, state.short_debt)
    return state.moved(short_debt=state.short_debt - moved, long_debt=state.long_debt + moved)


# ---------------------------------------------------------------------------
# Equity
# ---------------------------------------------------------------------------


def _equity_capacity(position: BorrowerPosition, drivers: Drivers, state: ProForma) -> Capacity:
    base = max(state.tangible_net_worth, 0.1 * state.total_debt)
    maximum = _MAX_EQUITY_SHARE * base
    assumptions = (
        "Fresh equity (promoter infusion, rights or QIP) is received at book value.",
        "Proceeds retire short-term lines first, then term debt, in the quarter received.",
        f"Sized within {_MAX_EQUITY_SHARE:.0%} of tangible net worth, the largest raise "
        "treated as plausible.",
    )
    rationale = (
        f"Tangible net worth is {_crore(state.tangible_net_worth)} against borrowings of "
        f"{_crore(state.total_debt)}; equity used to retire debt moves both sides of leverage at "
        "once and, applied to short-term lines, lifts the current ratio too.",
    )
    if maximum < _materiality(state):
        return Capacity(
            0.0,
            1.0,
            rationale,
            assumptions,
            "The balance sheet is too small to size an equity raise.",
        )
    return Capacity(maximum, _step(maximum), rationale, assumptions)


def _equity_apply(state: ProForma, size: float, drivers: Drivers) -> ProForma:
    raised = state.moved(tangible_net_worth=state.tangible_net_worth + size)
    return _retire_debt(raised, size)


# ---------------------------------------------------------------------------
# Covenant reset (lender concession)
# ---------------------------------------------------------------------------


def covenant_reset_lever(reference: str, name: str, threshold: float, direction: str) -> Lever:
    """A lender-side reset of one covenant, sized only if self-help falls short."""

    def capacity(position: BorrowerPosition, drivers: Drivers, state: ProForma) -> Capacity:
        maximum = _MAX_RESET_SHARE * abs(threshold)
        side = "raised" if direction == "max" else "lowered"
        return Capacity(
            maximum,
            0.01,
            (
                f"Not a cure: the borrower's position is unchanged. The {name.lower()} test of "
                f"{threshold:.2f}x is {side} for the projected test dates.",
            ),
            (
                "Requires credit committee approval and a documented amendment; advisory only.",
                f"Bounded at {_MAX_RESET_SHARE:.0%} of the contractual threshold.",
            ),
        )

    def apply(state: ProForma, size: float, drivers: Drivers) -> ProForma:
        return state.relieved(reference, size)

    return Lever(
        code=f"COVENANT-RESET-{reference}",
        title=f"Reset the {name.lower()} test",
        owner="Risk · credit committee",
        unit="ratio",
        order=90,
        lender_decision=True,
        capacity=capacity,
        apply=apply,
        covenant=reference,
    )


SELF_HELP_LEVERS: Final[tuple[Lever, ...]] = (
    Lever(
        "WORKING-CAPITAL-RELEASE",
        "Release working capital to its best filed level",
        "Borrower · RM to agree and monitor",
        "crore",
        10,
        False,
        _working_capital_capacity,
        _working_capital_apply,
    ),
    Lever(
        "PREPAY-FROM-SURPLUS-CASH",
        "Prepay borrowings from surplus cash",
        "Borrower treasury · Credit to process",
        "crore",
        20,
        False,
        _cash_capacity,
        _cash_apply,
    ),
    Lever(
        "REPRICE-BORROWINGS",
        "Refinance borrowings priced above the reference rate",
        "Borrower treasury · Credit",
        "bp",
        30,
        False,
        _repricing_capacity,
        _repricing_apply,
    ),
    Lever(
        "CAP-NEW-BORROWING",
        "Hold back the growth in borrowings",
        "Borrower management · Credit to set as a condition",
        "share",
        35,
        False,
        _borrowing_cap_capacity,
        _borrowing_cap_apply,
    ),
    Lever(
        "OPERATING-COST-PROGRAMME",
        "Agree a monitored operating-cost programme",
        "Borrower management · RM to monitor",
        "percent",
        40,
        False,
        _cost_capacity,
        _cost_apply,
    ),
    Lever(
        "TERM-OUT-SHORT-TERM-DEBT",
        "Term out short-term borrowings",
        "Credit · lenders' approval",
        "crore",
        50,
        True,
        _term_out_capacity,
        _term_out_apply,
    ),
    Lever(
        "EQUITY-TO-RETIRE-DEBT",
        "Raise equity to retire borrowings",
        "Promoter · RM to negotiate",
        "crore",
        60,
        False,
        _equity_capacity,
        _equity_apply,
        single_step=False,
    ),
)


def levers_for(position: BorrowerPosition) -> Sequence[Lever]:
    """Every lever considered for this borrower, in the order a plan tries them."""

    resets = tuple(
        covenant_reset_lever(item.reference, item.name, float(item.threshold), item.direction)
        for item in position.covenants
        if item.simulated
    )
    return (*SELF_HELP_LEVERS, *resets)


# ---------------------------------------------------------------------------
# Catalogue citation
# ---------------------------------------------------------------------------

#: The one catalogue entry every covenant's reset cites; the lever code
#: carries the covenant, the catalogue entry names the kind of action.
RESET_CATALOGUE_CODE: Final[str] = "COVENANT-RESET"
#: Catalogue codes the planner sizes per borrower.  A memo may cite them only
#: through a recorded package, and the fixed-effect simulator never offers them.
PLANNER_CATALOGUE_CODES: Final[frozenset[str]] = frozenset(
    {lever.code for lever in SELF_HELP_LEVERS} | {RESET_CATALOGUE_CODE}
)


def catalogue_code(lever: Lever) -> str:
    """The bank catalogue entry a step of this lever cites."""

    return RESET_CATALOGUE_CODE if lever.covenant is not None else lever.code


#: What a bare size measures, for wording read away from the planner's sliders.
_SIZE_BASIS: Final[dict[str, str]] = {
    "percent": " of cash operating cost",
    "bp": " off the cost of borrowings",
}


def step_wording(lever: Lever, size: float) -> str:
    """One sized, self-contained sentence for a package step.

    It is the wording a memo copies verbatim, so every figure in it is the
    figure the planner showed, formatted the same way, and a bare percentage
    or basis-point figure says what it is a share of.
    """

    shown = lever.describe_size(size) + _SIZE_BASIS.get(lever.unit, "")
    if lever.covenant is not None:
        sentence = f"{lever.title} by {shown}"
    else:
        sentence = f"{lever.title}: {shown}"
    return f"{sentence} (owner: {lever.owner})."


__all__ = [
    "OPERATING_BUFFER_DAYS",
    "PLANNER_CATALOGUE_CODES",
    "REFERENCE_BORROWING_RATE",
    "RESET_CATALOGUE_CODE",
    "SELF_HELP_LEVERS",
    "Capacity",
    "Lever",
    "catalogue_code",
    "covenant_reset_lever",
    "format_size",
    "levers_for",
    "step_wording",
]
