"""Unit coverage for borrower-specific remediation planning."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from covenant_radar.demo.curated import filed_statement, load_company_snapshot
from covenant_radar.domain.interventions.catalogue import CatalogueEntry
from covenant_radar.domain.remediation import (
    PLANNER_CATALOGUE_CODES,
    RESET_CATALOGUE_CODE,
    BorrowerPosition,
    CovenantTerm,
    QuarterLines,
    build_report,
    catalogue_code,
    custom_plan,
    estimate_drivers,
    package_steps,
    step_wording,
)
from covenant_radar.domain.remediation.levers import SELF_HELP_LEVERS
from covenant_radar.domain.remediation.montecarlo import filed_pro_forma
from covenant_radar.domain.remediation.position import (
    SIMULATED_RATIOS,
    line_ratio,
    ratio_value,
)
from covenant_radar.web.view_models.remediation import parse_sizes

pytestmark = pytest.mark.unit

_PACKAGE = (
    ("LEV", "leverage_ratio", Decimal("3.00"), "max", "Leverage ratio"),
    ("COV", "interest_coverage_ratio", Decimal("1.50"), "min", "Interest coverage ratio"),
    ("LIQ", "current_ratio", Decimal("1.20"), "min", "Current ratio"),
)


def _demo_position(symbol: str) -> BorrowerPosition:
    """The demo book's position for one company, from its filed results."""

    snapshot = load_company_snapshot()
    company = next(item for item in snapshot.companies if item.symbol == symbol)
    quarters = []
    for index, quarter in enumerate(company.quarters):
        pnl, sheet = filed_statement(company, index)
        quarters.append(QuarterLines(quarter.period_end, {**pnl.lines, **sheet.lines}))
    covenants = tuple(
        CovenantTerm(f"{symbol}-{code}", name, ref, threshold, direction)
        for code, ref, threshold, direction, name in _PACKAGE
    )
    return BorrowerPosition(
        symbol, company.legal_name, company.industry_code, tuple(quarters), covenants
    )


def _lever(code: str):  # type: ignore[no-untyped-def]
    return next(item for item in SELF_HELP_LEVERS if item.code == code)


def test_float_projection_formulas_match_the_ratio_library_on_every_filing() -> None:
    """The simulator's fast formulas must agree with the covenant library exactly."""

    snapshot = load_company_snapshot()
    checked = 0
    for company in snapshot.companies:
        for index in range(len(company.quarters)):
            pnl, sheet = filed_statement(company, index)
            lines = {**pnl.lines, **sheet.lines}
            floats = {code: float(value) for code, value in lines.items()}
            for code in SIMULATED_RATIOS:
                expected = ratio_value(code, lines)
                actual = line_ratio(code, floats)
                if expected is None or actual is None:
                    continue
                assert actual == pytest.approx(float(expected), rel=1e-9), (company.symbol, code)
                checked += 1
    assert checked > 500


def test_starting_values_are_the_filed_covenant_values() -> None:
    position = _demo_position("JSWENERGY")
    report = build_report(position, paths=200)

    for outlook in report.baseline.outlooks:
        assert outlook.start == position.filed_value(outlook.covenant)
    leverage = report.baseline.get("JSWENERGY-LEV")
    assert leverage is not None and leverage.start is not None
    assert float(leverage.start) == pytest.approx(4.2056, abs=1e-4)


def test_report_is_deterministic_for_the_same_filings() -> None:
    position = _demo_position("PRESTIGE")
    first = build_report(position, paths=300)
    second = build_report(position, paths=300)

    assert first.content_hash == second.content_hash
    assert first.plan_sizes == second.plan_sizes
    assert [o.breach_within_horizon for o in first.baseline.outlooks] == [
        o.breach_within_horizon for o in second.baseline.outlooks
    ]


def test_carried_forward_balance_sheets_are_not_counted_as_filings() -> None:
    position = _demo_position("JSWENERGY")
    sheets = position.distinct_sheets()

    # Eight quarters, but balance sheets are filed with September and March results only.
    assert len(position.quarters) == 8
    assert [sheet.period_end.month for sheet in sheets] == [9, 3, 9, 3]


def test_cash_lever_is_sized_from_the_borrowers_own_cash_and_buffer() -> None:
    position = _demo_position("JSWENERGY")
    drivers = estimate_drivers(position)
    capacity = _lever("PREPAY-FROM-SURPLUS-CASH").capacity(
        position, drivers, filed_pro_forma(position)
    )

    buffer = drivers.cash_operating_cost * 30 / 91
    assert capacity.available
    assert capacity.maximum == pytest.approx(4142.42 - buffer, rel=1e-3)
    assert any("₹4,142 cr" in line for line in capacity.rationale)
    # Current assets are below current liabilities, so the rationale says the
    # prepayment weakens the current ratio rather than hiding it.
    assert any("lowers the current ratio" in line for line in capacity.rationale)


def test_cash_alone_cannot_cure_a_deep_leverage_breach() -> None:
    """JSW Energy: ₹75,846 cr of debt over ₹18,035 cr of net worth."""

    position = _demo_position("JSWENERGY")
    drivers = estimate_drivers(position)
    start = filed_pro_forma(position)
    lever = _lever("PREPAY-FROM-SURPLUS-CASH")
    capacity = lever.capacity(position, drivers, start)
    after = lever.apply(start, capacity.maximum, drivers)

    assert after.total_debt / after.tangible_net_worth > 3.0
    # Equity that retires debt moves both sides: (D - E) / (T + E) = 3 at E = (D - 3T) / 4,
    # about ₹5,435 cr, against ₹7,250 cr if the same equity were held as cash.
    needed = (start.total_debt - 3.0 * start.tangible_net_worth) / 4.0
    assert needed == pytest.approx(5435, abs=5)
    equity = _lever("EQUITY-TO-RETIRE-DEBT").apply(start, needed, drivers)
    assert equity.total_debt / equity.tangible_net_worth == pytest.approx(3.0, abs=1e-9)


def test_cash_lever_is_unavailable_when_cash_is_inside_the_buffer() -> None:
    position = _demo_position("CRAFTSMAN")
    capacity = _lever("PREPAY-FROM-SURPLUS-CASH").capacity(
        position, estimate_drivers(position), filed_pro_forma(position)
    )

    assert not capacity.available
    assert capacity.unavailable_reason is not None
    assert "operating buffer" in capacity.unavailable_reason


def test_real_estate_inventory_is_not_treated_as_releasable_stock() -> None:
    position = _demo_position("PRESTIGE")
    capacity = _lever("WORKING-CAPITAL-RELEASE").capacity(
        position, estimate_drivers(position), filed_pro_forma(position)
    )

    assert any("land and work in progress" in line for line in capacity.rationale)


def test_term_out_cures_a_current_ratio_breach_without_moving_leverage() -> None:
    position = _demo_position("CRAFTSMAN")
    report = build_report(position)

    assert report.at_risk == ("CRAFTSMAN-LIQ",)
    assert [step.lever.code for step in report.plan] == ["TERM-OUT-SHORT-TERM-DEBT"]
    assert report.status == "cured"
    assert report.plan_outcome.probability("CRAFTSMAN-LIQ") <= report.target
    leverage_before = report.baseline.get("CRAFTSMAN-LEV")
    leverage_after = report.plan_outcome.get("CRAFTSMAN-LEV")
    assert leverage_before is not None and leverage_after is not None
    assert leverage_after.start == leverage_before.start


def test_healthy_borrower_gets_no_invented_package() -> None:
    report = build_report(_demo_position("TCI"), paths=300)

    assert report.status == "no_action"
    assert report.at_risk == ()
    assert report.plan == ()
    assert report.stresses == ()


def test_resets_are_sized_only_after_self_help_and_flagged_as_lender_decisions() -> None:
    report = build_report(_demo_position("JSWENERGY"))
    codes = [step.lever.code for step in report.plan]

    resets = [index for index, code in enumerate(codes) if code.startswith("COVENANT-RESET-")]
    own = [index for index, code in enumerate(codes) if not code.startswith("COVENANT-RESET-")]
    assert own and resets
    assert max(own) < min(resets)
    assert all(report.plan[index].lever.lender_decision for index in resets)
    assert report.status in {"partial", "cured_with_reset"}


def test_package_steps_replay_the_recommended_plan_exactly() -> None:
    report = build_report(_demo_position("JSWENERGY"), paths=300)
    steps = package_steps(report, report.plan_sizes)

    assert [(step.lever.code, step.size) for step in steps] == [
        (step.lever.code, step.size) for step in report.plan
    ]
    for replayed, planned in zip(steps, report.plan, strict=True):
        for outlook in planned.outcome.outlooks:
            assert replayed.outcome.probability(outlook.reference) == pytest.approx(
                planned.outcome.probability(outlook.reference)
            )


def test_package_steps_end_where_the_custom_package_ends() -> None:
    report = build_report(_demo_position("JSWENERGY"), paths=200)
    sizes = {"PREPAY-FROM-SURPLUS-CASH": 1_000_000.0, "REPRICE-BORROWINGS": 100.0}
    steps = package_steps(report, sizes)
    package = custom_plan(report, sizes)

    assert {step.lever.code: step.size for step in steps} == package.sizes
    for outlook in package.outcome.outlooks:
        assert steps[-1].outcome.probability(outlook.reference) == pytest.approx(
            outlook.breach_within_horizon
        )


def test_no_plan_ever_contains_a_zero_sized_step() -> None:
    snapshot = load_company_snapshot()
    for company in snapshot.companies[:8]:
        report = build_report(_demo_position(company.symbol), paths=200)
        assert all(step.size > 0 for step in report.plan), company.symbol
        if report.at_risk and not report.plan:
            assert report.status in {"clears", "partial"}


def test_step_wording_carries_the_displayed_size_and_owner() -> None:
    term_out = _lever("TERM-OUT-SHORT-TERM-DEBT")
    assert step_wording(term_out, 830.0) == (
        "Term out short-term borrowings: ₹830 cr (owner: Credit · lenders' approval)."
    )
    assert step_wording(_lever("OPERATING-COST-PROGRAMME"), 3.5).startswith(
        "Agree a monitored operating-cost programme: 3.5% of cash operating cost"
    )
    assert "240 bp off the cost of borrowings" in step_wording(_lever("REPRICE-BORROWINGS"), 240)
    report = build_report(_demo_position("JSWENERGY"), paths=200)
    reset = next(step for step in report.plan if step.lever.covenant is not None)
    assert catalogue_code(reset.lever) == RESET_CATALOGUE_CODE
    assert step_wording(reset.lever, 0.25).startswith(f"{reset.lever.title} by 0.25x")


def test_every_planner_lever_has_a_valid_seeded_catalogue_entry() -> None:
    """A memo can cite a recorded step only through an active catalogue entry."""

    seed = (
        Path(__file__).resolve().parents[2] / "src/covenant_radar/db/seed/data/interventions.json"
    )
    rows = {
        row["code"]: row for row in json.loads(seed.read_text(encoding="utf-8"))["interventions"]
    }
    assert PLANNER_CATALOGUE_CODES <= set(rows)
    for code in PLANNER_CATALOGUE_CODES:
        row = rows[code]
        entry = CatalogueEntry(
            id=code,
            role_tag=row["role_tag"],
            text=row["text"],
            effect_model=row["effect_model"],
            effect_parameters=row["effect_parameters"],
            applicable_covenant_classes=row["applicable_covenant_classes"],
            requires_approval=row["requires_approval"],
            is_active=row["is_active"],
        )
        assert entry.is_active
    assert rows[RESET_CATALOGUE_CODE]["role_tag"] == "risk"
    assert rows[RESET_CATALOGUE_CODE]["requires_approval"] is True


def test_custom_package_is_clamped_to_capacity() -> None:
    report = build_report(_demo_position("JSWENERGY"), paths=200)
    plan = custom_plan(report, {"PREPAY-FROM-SURPLUS-CASH": 1_000_000.0, "UNKNOWN": 5.0})

    assert set(plan.sizes) == {"PREPAY-FROM-SURPLUS-CASH"}
    cash = report.levers[1]
    assert cash.lever.code == "PREPAY-FROM-SURPLUS-CASH"
    assert plan.sizes["PREPAY-FROM-SURPLUS-CASH"] == pytest.approx(cash.capacity.maximum)


def test_position_without_cash_lines_degrades_with_reasons() -> None:
    quarters = tuple(
        QuarterLines(
            date(2025, month, 28 if month == 2 else 30),
            {
                "total_debt": Decimal(debt),
                "tangible_net_worth": Decimal("100"),
                "ebit": Decimal("30"),
                "ebitda": Decimal("44"),
                "revenue": Decimal("410"),
                "finance_cost": Decimal("10"),
                "current_assets": Decimal("240"),
                "current_liabilities": Decimal("120"),
            },
        )
        for month, debt in ((6, "260"), (9, "300"), (11, "340"))
    )
    position = BorrowerPosition(
        "B-1",
        "Example",
        None,
        quarters,
        (CovenantTerm("LEV", "Leverage ratio", "leverage_ratio", Decimal("3.25"), "max"),),
    )
    report = build_report(position, paths=200)
    cash = next(item for item in report.levers if item.lever.code == "PREPAY-FROM-SURPLUS-CASH")

    assert cash.capacity.unavailable_reason == "Cash is not reported separately in the filed lines."
    assert report.at_risk == ("LEV",)


def test_parse_sizes_reads_only_well_formed_lever_parameters() -> None:
    assert parse_sizes({"other": "1"}) is None
    sizes = parse_sizes(
        [
            ("size.prepay-from-surplus-cash", "120.5"),
            ("size.EQUITY-TO-RETIRE-DEBT", "-4"),
            ("size.BAD CODE", "1"),
            ("size.REPRICE-BORROWINGS", "nan"),
            ("size.COST", "abc"),
        ]
    )
    assert sizes == {"PREPAY-FROM-SURPLUS-CASH": 120.5, "EQUITY-TO-RETIRE-DEBT": 0.0}
