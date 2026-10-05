"""Unit coverage for mapping filed NSE results onto the chart of accounts."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from covenant_radar.demo.curated import (
    COMPANY_SNAPSHOT_PATH,
    DEMO_BORROWER_COUNT,
    DEMO_PERIOD_COUNT,
    filed_statement,
    load_company_snapshot,
    snapshot_periods,
)

pytestmark = pytest.mark.unit

_QUARTER_ENDS = ("2025-09-30", "2025-12-31")


def _write_snapshot(tmp_path: Path) -> Path:
    def quarter(period_end: str, balance_sheet: dict[str, str] | None) -> dict[str, object]:
        return {
            "period_end": period_end,
            "filing": {
                "xbrl_url": f"https://nsearchives.nseindia.com/{period_end}.xml",
                "filed_at": f"{period_end}T20:00:00+05:30",
                "audited": "Un-Audited",
            },
            "profit_and_loss": {
                "RevenueFromOperations": "1000.00",
                "OtherIncome": "20.00",
                "Expenses": "900.00",
                "FinanceCosts": "40.00",
                "DepreciationDepletionAndAmortisationExpense": "30.00",
                "ExceptionalItemsBeforeTax": "-500.00",
                "ProfitBeforeTax": "-380.00",
            },
            "balance_sheet": balance_sheet,
        }

    snapshot = {
        "schema": "indian-companies-v1",
        "retrieved_on": "2026-10-03",
        "source": "test",
        "unit": "INR crore",
        "quarter_ends": list(_QUARTER_ENDS),
        "companies": [
            {
                "symbol": "TESTCO",
                "legal_name": "Test Company Limited",
                "business_group": None,
                "industry_code": "C24",
                "basis": "consolidated",
                "source_url": "https://www.nseindia.com/get-quotes/equity?symbol=TESTCO",
                "quarters": [
                    quarter(
                        "2025-09-30",
                        {
                            "CashAndCashEquivalents": "50.00",
                            "Inventories": "200.00",
                            "TradeReceivablesCurrent": "150.00",
                            "CurrentAssets": "600.00",
                            "TradePayablesCurrent": "180.00",
                            "BorrowingsCurrent": "120.00",
                            "CurrentLiabilities": "500.00",
                            "BorrowingsNoncurrent": "380.00",
                            "Liabilities": "1100.00",
                            "EquityAttributableToOwnersOfParent": "900.00",
                            "Goodwill": "100.00",
                            "OtherIntangibleAssets": "50.00",
                        },
                    ),
                    quarter("2025-12-31", None),
                ],
            }
        ],
    }
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot))
    return path


def test_ebit_excludes_exceptional_items_and_adds_back_finance_cost(tmp_path: Path) -> None:
    company = load_company_snapshot(_write_snapshot(tmp_path)).companies[0]

    profit_and_loss, _ = filed_statement(company, 0)

    # 1000 revenue + 20 other income - 900 total expenses + 40 finance cost.
    # The -500 exceptional write-down never reaches EBIT.
    assert profit_and_loss.lines["ebit"] == Decimal("160.00")
    assert profit_and_loss.lines["ebitda"] == Decimal("190.00")
    assert profit_and_loss.lines["finance_cost"] == Decimal("40.00")
    assert profit_and_loss.source_reference.endswith("2025-09-30.xml")


def test_balance_sheet_lines_are_filed_totals_with_reconciling_components(
    tmp_path: Path,
) -> None:
    company = load_company_snapshot(_write_snapshot(tmp_path)).companies[0]

    _, balance_sheet = filed_statement(company, 0)
    lines = balance_sheet.lines

    assert lines["tangible_net_worth"] == Decimal("750.00")
    assert lines["total_debt"] == Decimal("500.00")
    assert lines["current_assets"] == Decimal("600.00")
    assert lines["current_liabilities"] == Decimal("500.00")
    assert (
        lines["cash_and_bank"]
        + lines["inventory"]
        + lines["receivables"]
        + lines["other_current_assets"]
        == lines["current_assets"]
    )
    assert (
        lines["payables"] + lines["short_term_debt"] + lines["other_current_liabilities"]
        == lines["current_liabilities"]
    )
    # Total assets is deliberately absent: tangible net worth deducts
    # intangibles, so the filed total would fail the balance-sheet identity.
    assert "total_assets" not in lines


def test_a_quarter_without_a_balance_sheet_holds_the_last_filed_one(tmp_path: Path) -> None:
    company = load_company_snapshot(_write_snapshot(tmp_path)).companies[0]

    _, held = filed_statement(company, 1)

    assert held.lines["tangible_net_worth"] == Decimal("750.00")
    assert held.source_reference.endswith("2025-09-30.xml")
    assert "Held from the 30 Sep 2025 balance sheet" in held.transform_note
    assert len(held.transform_note) <= 1000
    assert len(held.row_reference) <= 50


def test_periods_follow_the_snapshot_quarters_in_fiscal_labels(tmp_path: Path) -> None:
    snapshot = load_company_snapshot(_write_snapshot(tmp_path))

    assert snapshot_periods(snapshot) == (
        ("FY26Q2", date(2025, 7, 1), date(2025, 9, 30)),
        ("FY26Q3", date(2025, 10, 1), date(2025, 12, 31)),
    )


def test_the_committed_snapshot_is_a_complete_demo_roster() -> None:
    snapshot = load_company_snapshot(COMPANY_SNAPSHOT_PATH)

    assert len(snapshot.companies) == DEMO_BORROWER_COUNT
    assert len(snapshot.quarter_ends) == DEMO_PERIOD_COUNT
    assert len({company.symbol for company in snapshot.companies}) == DEMO_BORROWER_COUNT
    for company in snapshot.companies:
        for index in range(DEMO_PERIOD_COUNT):
            profit_and_loss, balance_sheet = filed_statement(company, index)
            assert profit_and_loss.lines["finance_cost"] > 0, company.symbol
            assert balance_sheet.lines["tangible_net_worth"] > 0, company.symbol
            assert balance_sheet.lines["current_liabilities"] > 0, company.symbol
