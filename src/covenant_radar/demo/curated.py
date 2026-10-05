"""Idempotent, presentation-ready demo data built on real Indian companies.

The 24 demo borrowers are companies listed on the National Stock Exchange,
and every statement line the covenant engine tests comes from the quarterly
results each company filed there (`demo/data/indian_companies.json`,
refreshed by `scripts/fetch_indian_financials.py`).  Nothing in a borrower's
financial history is invented, smoothed or tuned: whether a company sits in
Act now, Amber or Watch is what the product's own engine and forecast make
of its published numbers.

What a bank holds privately cannot be real in a public demo, so it is
illustrative and labelled as such: the covenant terms (one standard package
for every borrower), the facility limits (sized from each company's reported
borrowings) and the behavioural signals.  The signals are deliberately
neutral and every borrower's repayment conduct is clean: the demo never
attributes an overdue, an SMA status or adverse conduct to a real company.

The seed uses the same registry and covenant engine as a live deployment, so
queue bands, forecast crossings, traces, audit rows and threshold snapshots
are all produced by the product's real services rather than a UI fixture.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from covenant_radar.audit.record import AuditRecorder
from covenant_radar.config.thresholds import DEFAULT_THRESHOLD_PATH, ThresholdStore
from covenant_radar.core.clock import Clock, SystemClock
from covenant_radar.core.context import new_request_id
from covenant_radar.core.ids import new_id
from covenant_radar.db.models.audit import ThresholdSnapshot
from covenant_radar.db.models.borrower import (
    Borrower,
    BorrowerContact,
    BorrowerGroup,
    RelatedParty,
)
from covenant_radar.db.models.covenant import Covenant, CovenantTest, CovenantVersion
from covenant_radar.db.models.facility import Facility, FacilityConduct
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.signal import SignalEvent
from covenant_radar.db.models.statements import (
    FieldProvenance,
    FinancialPeriod,
    ImportBatch,
    ImportMapping,
    StatementLineValue,
)
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.db.scoping import Scope
from covenant_radar.domain.certificates.requirements import CERTIFICATE_TEST_BASIS
from covenant_radar.domain.covenants.evaluate import PeriodFacts
from covenant_radar.domain.covenants.exceptions import (
    DEFAULT_FISCAL_YEAR_START_MONTH,
    period_label_for_date,
)
from covenant_radar.domain.covenants.model import CovenantVersionTerms
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.engine import EngineService
from covenant_radar.services.master_data import MasterDataService
from covenant_radar.services.registry import RegistryService
from evaluation.reference_portfolio.names import build_group_name

DEMO_PORTFOLIO_CODE: Final[str] = "REF-PORTFOLIO"
DEMO_PORTFOLIO_NAME: Final[str] = "Indian listed corporates"
#: The filed-results snapshot `scripts/fetch_indian_financials.py` writes.
COMPANY_SNAPSHOT_PATH: Final[Path] = Path(__file__).with_name("data") / "indian_companies.json"
COMPANY_SNAPSHOT_SCHEMA: Final[str] = "indian-companies-v1"
DEMO_SOURCE_REFERENCE: Final[str] = (
    "https://www.nseindia.com/companies-listing/corporate-filings-financial-results"
)
#: Eight filed quarters per company: enough history for the forecast's trend
#: fit, and two full years of half-yearly balance sheets.
DEMO_PERIOD_COUNT: Final[int] = 8
DEMO_BORROWER_COUNT: Final[int] = 24

#: The mapping the `/financial-statements` screen imports against.
#:
#: `nse-results-xbrl` below is only a provenance label for the seeded
#: statement rows — an `import_batch` needs a `mapping_id`.  This is a real
#: `ImportMappingSpec` over the same chart lines the demo portfolio carries,
#: so an imported quarter lands in the same shape as the seeded history and
#: feeds the same covenant tests.
#:
#: Only non-derived lines are mapped; `ebitda`, `ebit`, `current_assets`,
#: `current_liabilities` and `total_debt` are left for the chart to derive,
#: which is what the covenant ratios read.
DEMO_IMPORT_MAPPING_NAME: Final[str] = "quarterly-financials-v1"
DEMO_IMPORT_MAPPING_SPEC: Final[dict[str, object]] = {
    "borrower_key_column": "borrower_key",
    "fy_label_column": "fy_label",
    "period_type_column": "period_type",
    "period_start_column": "period_start",
    "period_end_column": "period_end",
    "is_audited_column": "is_audited",
    "unit": "lakh",
    "currency": "INR",
    "sign": "as_reported",
    "columns": {
        "revenue_lakh": "revenue",
        "cogs_lakh": "cost_of_goods_sold",
        "opex_lakh": "operating_expenses",
        "depreciation_lakh": "depreciation",
        "finance_cost_lakh": "finance_cost",
        "tax_expense_lakh": "tax_expense",
        "pat_lakh": "profit_after_tax",
        "cash_and_bank_lakh": "cash_and_bank",
        "inventory_lakh": "inventory",
        "receivables_lakh": "receivables",
        "other_current_assets_lakh": "other_current_assets",
        "payables_lakh": "payables",
        "short_term_debt_lakh": "short_term_debt",
        "other_current_liabilities_lakh": "other_current_liabilities",
        "long_term_debt_lakh": "long_term_debt",
        "total_liabilities_lakh": "total_liabilities",
        "tangible_net_worth_lakh": "tangible_net_worth",
        "total_assets_lakh": "total_assets",
    },
    "totals_row": {"column": "borrower_key", "value": "TOTAL"},
}
_FILED_MAPPING_NAME: Final[str] = "nse-results-xbrl"
DEMO_COVENANTS_PER_BORROWER: Final[int] = 3
DEMO_SIGNAL_DAYS: Final[int] = 35
#: Marks every seeded behavioural signal as illustrative; the borrower page
#: and the queue's change feed label such events "Synthetic portfolio signal".
DEMO_SIGNAL_VERSION: Final[str] = "illustrative-bank-signals-v1"
DEMO_SIGNAL_PATH: Final[Path] = Path("var/inbox/covenant-radar-demo-signals.json")
#: Only the five lender-side families are seeded.  Industry and news are
#: public facts about a company, so an invented reading beside a real name
#: would read as a claim about it; the market-intelligence workspace carries
#: the real ones, and the walkthrough's scenarios can still add either.
DEMO_SIGNAL_FAMILIES: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("account_activity", "account_activity_change", "%", "activity_change_pct"),
    ("payment", "payment_delay", "days", "days_past_due"),
    ("utilisation", "facility_utilisation", "%", "utilisation_pct"),
    ("treasury", "treasury_outflow", "ratio", "cash_outflow_ratio"),
    ("concentration", "concentration_exposure", "%", "top_group_exposure_pct"),
)

#: The one illustrative covenant package every borrower carries: market
#: standard Indian bank terms, never tuned per company, so a breach on screen
#: is the company's published numbers crossing an ordinary limit.
_COVENANT_PACKAGE: Final[tuple[tuple[str, str, Decimal, str, str, str], ...]] = (
    ("LEV", "leverage_ratio", Decimal("3.00"), "max", "leverage", "Leverage ratio"),
    (
        "COV",
        "interest_coverage_ratio",
        Decimal("1.50"),
        "min",
        "coverage",
        "Interest coverage ratio",
    ),
    ("LIQ", "current_ratio", Decimal("1.20"), "min", "liquidity", "Current ratio"),
)
_DRIVERS: Final[Mapping[str, str]] = {
    "LEV": "total borrowings / tangible net worth",
    "COV": "EBIT / finance cost",
    "LIQ": "current assets / current liabilities",
}

#: Borrowers (1-based roster position) whose current-ratio covenant is
#: evidenced by a quarterly CA compliance certificate rather than the filed
#: statement alone, so the nightly certificate cycle (`spec §R-09`) has real
#: requests to raise and chase on the walkthrough.  They are companies whose
#: liquidity is comfortable, so the certificate cycle never stands in front
#: of a current-ratio story the filings tell.
_CERTIFIED_LIQUIDITY: Final[tuple[int, ...]] = (2, 5, 13, 16, 18, 20)

#: The illustrative bank's facilities are sized as this share of each
#: company's reported borrowings — one lender in a consortium — so a limit
#: reads in proportion to the company beside it.
_FACILITY_SHARE_OF_BORROWINGS: Final[Decimal] = Decimal("0.05")
_FACILITY_MINIMUM_CRORE: Final[Decimal] = Decimal("25")
_FACILITY_ROUNDING_CRORE: Final[Decimal] = Decimal("5")
_CONTACT_NAME: Final[str] = "Company secretarial desk"
_CONTACT_DESIGNATION: Final[str] = "Company Secretary and Compliance Officer"
_PROMOTER_ROLE: Final[str] = "Promoter group, as disclosed in the NSE shareholding pattern"
_LISTED_CONSTITUTION: Final[str] = "public_limited"
_ZERO: Final[Decimal] = Decimal("0")


@dataclass(frozen=True, slots=True)
class FiledQuarter:
    """One quarter's results as filed: profit and loss for the quarter, and
    the balance sheet when that filing carried one (half-year and year end)."""

    period_end: date
    xbrl_url: str
    filed_at: str
    audited: bool
    profit_and_loss: Mapping[str, Decimal]
    balance_sheet: Mapping[str, Decimal] | None


@dataclass(frozen=True, slots=True)
class ListedCompany:
    symbol: str
    legal_name: str
    business_group: str | None
    industry_code: str
    basis: str
    source_url: str
    quarters: tuple[FiledQuarter, ...]


@dataclass(frozen=True, slots=True)
class CompanySnapshot:
    retrieved_on: date
    source: str
    quarter_ends: tuple[date, ...]
    companies: tuple[ListedCompany, ...]
    content_hash: str


def load_company_snapshot(path: Path = COMPANY_SNAPSHOT_PATH) -> CompanySnapshot:
    """Read and validate the filed-results snapshot."""

    raw = path.read_bytes()
    payload = json.loads(raw)
    if not isinstance(payload, dict) or payload.get("schema") != COMPANY_SNAPSHOT_SCHEMA:
        raise ValueError(f"{path} is not an {COMPANY_SNAPSHOT_SCHEMA} snapshot.")
    quarter_ends = tuple(date.fromisoformat(value) for value in payload["quarter_ends"])
    companies: list[ListedCompany] = []
    for row in payload["companies"]:
        quarters = tuple(
            FiledQuarter(
                period_end=date.fromisoformat(quarter["period_end"]),
                xbrl_url=quarter["filing"]["xbrl_url"],
                filed_at=quarter["filing"]["filed_at"],
                audited=quarter["filing"]["audited"] == "Audited",
                profit_and_loss={
                    name: Decimal(value) for name, value in quarter["profit_and_loss"].items()
                },
                balance_sheet=(
                    {name: Decimal(value) for name, value in quarter["balance_sheet"].items()}
                    if quarter["balance_sheet"]
                    else None
                ),
            )
            for quarter in row["quarters"]
        )
        if tuple(quarter.period_end for quarter in quarters) != quarter_ends:
            raise ValueError(f"{row['symbol']}: its quarters do not match the snapshot's.")
        if quarters[0].balance_sheet is None:
            raise ValueError(f"{row['symbol']}: the oldest quarter carries no balance sheet.")
        companies.append(
            ListedCompany(
                symbol=row["symbol"],
                legal_name=row["legal_name"],
                business_group=row.get("business_group"),
                industry_code=row["industry_code"],
                basis=row["basis"],
                source_url=row["source_url"],
                quarters=quarters,
            )
        )
    return CompanySnapshot(
        retrieved_on=date.fromisoformat(payload["retrieved_on"]),
        source=payload["source"],
        quarter_ends=quarter_ends,
        companies=tuple(companies),
        content_hash=hashlib.sha256(raw).hexdigest(),
    )


def snapshot_periods(
    snapshot: CompanySnapshot,
    *,
    fiscal_year_start_month: int = DEFAULT_FISCAL_YEAR_START_MONTH,
) -> tuple[tuple[str, date, date], ...]:
    """The filed quarters as ``(fy_label, period_start, period_end)``, oldest
    first, labelled in the bank's fiscal convention."""

    return tuple(
        (
            period_label_for_date(end, fiscal_year_start_month=fiscal_year_start_month),
            date(end.year, end.month - 2, 1),
            end,
        )
        for end in snapshot.quarter_ends
    )


@dataclass(frozen=True, slots=True)
class _LineGroup:
    """Statement lines that share one source filing, and its provenance."""

    lines: Mapping[str, Decimal]
    source_reference: str
    row_reference: str
    transform_note: str


def filed_statement(company: ListedCompany, quarter_index: int) -> tuple[_LineGroup, _LineGroup]:
    """Map one filed quarter onto the chart, as ``(profit_and_loss, balance_sheet)``.

    EBIT is profit before exceptional items, tax and finance costs: total
    income less total expenses, plus finance costs back.  Exceptional items
    are left out so a one-off gain or write-down cannot swing interest
    cover.  Listed companies file a balance sheet only with their September
    and March results, so a June or December quarter is tested against the
    most recent balance sheet filed before it, and its provenance says so.
    Every total is the filed figure; the component lines beside it are
    filed figures too, with the remainder shown as the "other" line so a
    derived total can never disagree with what was filed.
    """

    quarter = company.quarters[quarter_index]
    pnl = quarter.profit_and_loss

    def filed(values: Mapping[str, Decimal], name: str) -> Decimal:
        return values.get(name, _ZERO)

    finance_cost = filed(pnl, "FinanceCosts")
    depreciation = filed(pnl, "DepreciationDepletionAndAmortisationExpense")
    revenue = filed(pnl, "RevenueFromOperations")
    ebit = revenue + filed(pnl, "OtherIncome") - filed(pnl, "Expenses") + finance_cost
    basis = "consolidated" if company.basis == "consolidated" else "standalone"
    profit_and_loss = _LineGroup(
        lines={
            "revenue": revenue,
            "depreciation": depreciation,
            "finance_cost": finance_cost,
            "ebit": ebit,
            "ebitda": ebit + depreciation,
        },
        source_reference=quarter.xbrl_url,
        row_reference=f"{company.symbol}:{quarter.period_end.isoformat()}:pl",
        transform_note=(
            f"NSE {company.symbol} {basis} results for the quarter ended "
            f"{quarter.period_end:%d %b %Y}, filed {quarter.filed_at[:10]}"
            f" ({'audited' if quarter.audited else 'limited review'}); rupees "
            "converted to crore. EBIT = revenue from operations + other income "
            "- total expenses + finance costs (before exceptional items and tax); "
            "EBITDA = EBIT + depreciation and amortisation."
        ),
    )

    sheet_quarter = next(
        candidate
        for candidate in reversed(company.quarters[: quarter_index + 1])
        if candidate.balance_sheet is not None
    )
    sheet = sheet_quarter.balance_sheet or {}
    current_assets = filed(sheet, "CurrentAssets")
    asset_parts = {
        "cash_and_bank": filed(sheet, "CashAndCashEquivalents"),
        "inventory": filed(sheet, "Inventories"),
        "receivables": filed(sheet, "TradeReceivablesCurrent"),
    }
    current_liabilities = filed(sheet, "CurrentLiabilities")
    liability_parts = {
        "payables": filed(sheet, "TradePayablesCurrent"),
        "short_term_debt": filed(sheet, "BorrowingsCurrent"),
    }
    long_term_debt = filed(sheet, "BorrowingsNoncurrent")
    balance: dict[str, Decimal] = {
        "current_assets": current_assets,
        "current_liabilities": current_liabilities,
        "short_term_debt": liability_parts["short_term_debt"],
        "long_term_debt": long_term_debt,
        "total_debt": liability_parts["short_term_debt"] + long_term_debt,
        "tangible_net_worth": (
            filed(sheet, "EquityAttributableToOwnersOfParent")
            - filed(sheet, "Goodwill")
            - filed(sheet, "OtherIntangibleAssets")
        ),
    }
    if "Liabilities" in sheet:
        balance["total_liabilities"] = sheet["Liabilities"]
    other_assets = current_assets - sum(asset_parts.values(), _ZERO)
    if other_assets >= _ZERO:
        balance.update(asset_parts, other_current_assets=other_assets)
    other_liabilities = current_liabilities - sum(liability_parts.values(), _ZERO)
    if other_liabilities >= _ZERO:
        balance.update(
            payables=liability_parts["payables"], other_current_liabilities=other_liabilities
        )
    held = (
        ""
        if sheet_quarter.period_end == quarter.period_end
        else (
            f" Held from the {sheet_quarter.period_end:%d %b %Y} balance sheet: listed "
            "companies publish balance sheets half-yearly."
        )
    )
    balance_sheet = _LineGroup(
        lines=balance,
        source_reference=sheet_quarter.xbrl_url,
        row_reference=f"{company.symbol}:{sheet_quarter.period_end.isoformat()}:bs",
        transform_note=(
            f"NSE {company.symbol} {basis} balance sheet as at "
            f"{sheet_quarter.period_end:%d %b %Y}, filed {sheet_quarter.filed_at[:10]}; "
            f"rupees converted to crore.{held} Tangible net worth = equity attributable "
            "to owners - goodwill - other intangible assets. Total debt = current + "
            "non-current borrowings as filed (lease liabilities are reported "
            "separately and not included)."
        ),
    )
    return profit_and_loss, balance_sheet


@dataclass(frozen=True, slots=True)
class DemoSeedReport:
    """Stable counts printed by ``radarctl seed --demo-covenants``."""

    borrowers: int
    covenants_created: int
    periods_created: int
    tests_created: int
    threshold_snapshot_id: UUID
    signal_events: int = 0
    latest_quarter: date | None = None
    snapshot_retrieved_on: date | None = None


class _DemoAuditWriter:
    """Adapt the broad recorder API to service audit protocols."""

    def __init__(self, recorder: AuditRecorder) -> None:
        self._recorder = recorder

    def record(
        self,
        event_type: str,
        subject: object,
        payload: Mapping[str, object],
        *,
        actor: object,
        request_id: str,
    ) -> object:
        return self._recorder.record(
            event_type,
            subject,  # type: ignore[arg-type]
            payload,
            actor=actor,
            request_id=request_id,
        )


def seed_demo_covenants(
    session: Session,
    *,
    system_actor_id: UUID,
    clock: Clock | None = None,
    signal_path: str | Path | None = None,
    snapshot_path: Path = COMPANY_SNAPSHOT_PATH,
) -> DemoSeedReport:
    """Turn the reference portfolio into the real-company demo book.

    Re-running the function only fills missing rows.  Existing statement,
    covenant and test rows are never rewritten, which makes it safe for a
    presenter to restart the bootstrap command or refresh an already-running
    demo environment.
    """

    if not isinstance(session, Session):
        raise TypeError("seed_demo_covenants requires a SQLAlchemy Session.")
    if not isinstance(system_actor_id, UUID):
        raise TypeError("system_actor_id must be a UUID.")

    demo_clock = clock or SystemClock()
    now = demo_clock.now()
    request_id = "demo-" + new_request_id()
    portfolio = session.scalar(select(Portfolio).where(Portfolio.code == DEMO_PORTFOLIO_CODE))
    if portfolio is None:
        raise ValueError(
            "The reference portfolio is missing. Run `radarctl seed --reference-portfolio` first."
        )

    snapshot = load_company_snapshot(snapshot_path)
    if len(snapshot.companies) != DEMO_BORROWER_COUNT:
        raise ValueError(
            f"The company snapshot holds {len(snapshot.companies)} companies; "
            f"the demo roster needs {DEMO_BORROWER_COUNT}."
        )
    borrowers = list(
        session.scalars(
            select(Borrower)
            .where(Borrower.portfolio_id == portfolio.id, Borrower.is_active.is_(True))
            .order_by(Borrower.reference)
            .limit(DEMO_BORROWER_COUNT)
        )
    )
    if len(borrowers) < DEMO_BORROWER_COUNT:
        raise ValueError(
            f"The reference portfolio contains only {len(borrowers)} active borrowers; "
            f"the {DEMO_BORROWER_COUNT}-company demo roster requires all of them."
        )

    periods = snapshot_periods(snapshot)
    if portfolio.name != DEMO_PORTFOLIO_NAME:
        portfolio.name = DEMO_PORTFOLIO_NAME
        portfolio.updated_at = now
        portfolio.updated_by_id = system_actor_id
        portfolio.request_id = request_id
        portfolio.version += 1
    _clear_non_curated_financials(session, borrowers, periods)
    _clear_generated_conduct(session, borrowers)

    batch = _ensure_import_batch(
        session,
        snapshot=snapshot,
        system_actor_id=system_actor_id,
        now=now,
        request_id=request_id,
        borrower_count=len(borrowers),
    )
    _ensure_statement_import_mapping(
        session,
        system_actor_id=system_actor_id,
        now=now,
        request_id=request_id,
    )
    threshold_snapshot_id = _ensure_demo_threshold_snapshot(
        session, system_actor_id=system_actor_id, now=now, request_id=request_id
    )
    principal = Principal.user(
        system_actor_id,
        (Permission.REGISTER_COVENANT, Permission.VIEW_COVENANT),
    )
    scope = Scope(principal_id=system_actor_id, descendant_paths=(portfolio.path,))
    audit = _DemoAuditWriter(
        AuditRecorder(AuditRepository(session), clock=demo_clock, request_id=request_id)
    )
    registry = RegistryService(
        session,
        audit=audit,
        clock=demo_clock,
        request_id=request_id,
        maker_checker_enabled=False,
    )
    engine = EngineService(
        session,
        audit=audit,
        clock=demo_clock,
        request_id=request_id,
    )
    master_data = MasterDataService(
        session,
        audit=audit,
        clock=demo_clock,
        request_id=request_id,
    )
    groups = _ensure_business_groups(
        session,
        snapshot.companies,
        system_actor_id=system_actor_id,
        now=now,
        request_id=request_id,
    )

    covenants_created = 0
    periods_created = 0
    tests_created = 0
    for borrower_index, (borrower, company) in enumerate(
        zip(borrowers, snapshot.companies, strict=True), start=1
    ):
        _apply_company_identity(
            master_data,
            borrower=borrower,
            company=company,
            group_id=groups.get(company.business_group or ""),
            principal=Principal.user(system_actor_id, (Permission.CORRECT_SOURCE_DATA,)),
            scope=scope,
        )
        _apply_company_contacts(
            session,
            borrower=borrower,
            company=company,
            system_actor_id=system_actor_id,
            now=now,
            request_id=request_id,
        )
        facility = _size_facilities(
            session,
            borrower=borrower,
            company=company,
            system_actor_id=system_actor_id,
            now=now,
            request_id=request_id,
        )
        if facility is None:
            continue
        _ensure_demo_conduct(
            session,
            facility=facility,
            as_of_date=now.date(),
            system_actor_id=system_actor_id,
            now=now,
            request_id=request_id,
        )

        versions: dict[str, CovenantVersion] = {}
        for (
            kind,
            definition,
            threshold,
            direction,
            covenant_class,
            display_name,
        ) in _COVENANT_PACKAGE:
            reference = f"D{borrower_index:02d}{kind}"
            covenant = session.scalar(select(Covenant).where(Covenant.reference == reference))
            if covenant is None:
                registered = registry.register(
                    principal,
                    facility_id=facility.id,
                    reference=reference,
                    name=display_name,
                    covenant_class=covenant_class,
                    terms=CovenantVersionTerms(
                        definition_ref=definition,
                        custom_formula=None,
                        threshold=threshold,
                        direction=direction,
                        unit="x",
                        frequency="quarterly",
                        test_basis=(
                            CERTIFICATE_TEST_BASIS
                            if kind == "LIQ" and borrower_index in _CERTIFIED_LIQUIDITY
                            else "period_end"
                        ),
                        effective_from=periods[0][1],
                        warning_headroom_pct=Decimal("10.00"),
                        cure_days=120,
                        grace_days=0,
                    ),
                    scope=scope,
                )
                covenant = registered.covenant
                covenants_created += 1
            version = session.scalar(
                select(CovenantVersion)
                .where(CovenantVersion.covenant_id == covenant.id)
                .order_by(CovenantVersion.version_no.desc())
                .limit(1)
            )
            if version is not None:
                versions[kind] = version

        period_rows: list[tuple[FinancialPeriod, Mapping[str, Decimal]]] = []
        for period_index, (label, period_start, period_end) in enumerate(periods):
            filed_quarter = company.quarters[period_index]
            period = session.scalar(
                select(FinancialPeriod).where(
                    FinancialPeriod.borrower_id == borrower.id,
                    FinancialPeriod.fy_label == label,
                    FinancialPeriod.version == 1,
                )
            )
            if period is None:
                period = FinancialPeriod(
                    id=new_id(),
                    borrower_id=borrower.id,
                    fy_label=label,
                    period_type="quarterly",
                    period_start=period_start,
                    period_end=period_end,
                    is_complete=True,
                    is_audited=filed_quarter.audited,
                    source_batch_id=batch.id,
                    version=1,
                    created_at=now,
                    updated_at=now,
                    created_by_id=system_actor_id,
                    updated_by_id=system_actor_id,
                    request_id=request_id,
                )
                session.add(period)
                session.flush()
                periods_created += 1

            groups_for_period = filed_statement(company, period_index)
            _ensure_statement_lines(
                session,
                period=period,
                groups=groups_for_period,
                batch=batch,
                system_actor_id=system_actor_id,
                now=now,
                request_id=request_id,
            )
            period_lines: dict[str, Decimal] = {}
            for group in groups_for_period:
                period_lines.update(group.lines)
            period_rows.append((period, period_lines))

        session.flush()
        for kind, version in versions.items():
            for period, lines in period_rows:
                existing = session.scalar(
                    select(CovenantTest.id).where(
                        CovenantTest.covenant_version_id == version.id,
                        CovenantTest.period_id == period.id,
                    )
                )
                if existing is not None:
                    continue
                result = engine.test(
                    principal,
                    covenant_version_id=version.id,
                    period=PeriodFacts(
                        period_label=period.fy_label,
                        period_id=period.id,
                        as_of_date=period.period_end,
                        is_complete=period.is_complete,
                    ),
                    lines=lines,
                    scope=scope,
                    as_of_date=period.period_end,
                    period_id=period.id,
                )
                current_inputs = dict(result.inputs) if isinstance(result.inputs, Mapping) else {}
                current_inputs["demo_driver"] = _DRIVERS[kind]
                result.inputs = current_inputs
                tests_created += 1
        session.flush()

    _remove_unused_generated_groups(session)

    signal_events = 0
    if signal_path is not None:
        signal_events = _write_demo_signal_source(
            Path(signal_path),
            borrowers,
            session,
            end_date=now.date() - timedelta(days=1),
        )

    return DemoSeedReport(
        borrowers=len(borrowers),
        covenants_created=covenants_created,
        periods_created=periods_created,
        tests_created=tests_created,
        threshold_snapshot_id=threshold_snapshot_id,
        signal_events=signal_events,
        latest_quarter=snapshot.quarter_ends[-1],
        snapshot_retrieved_on=snapshot.retrieved_on,
    )


def _apply_company_identity(
    master_data: MasterDataService,
    *,
    borrower: Borrower,
    company: ListedCompany,
    group_id: UUID | None,
    principal: Principal,
    scope: Scope,
) -> None:
    """Give the borrower its real legal name, constitution and group.

    The reference portfolio's generated CIN and PAN are cleared rather than
    kept beside a real name: the filings carry neither, and a made-up
    identifier on a real company would be a false record.  The change goes
    through the master-data service, so the audit trail shows it.
    """

    changes: dict[str, object] = {}
    if borrower.legal_name != company.legal_name:
        changes["legal_name"] = company.legal_name
    if borrower.cin_enc is not None or borrower.cin_fingerprint is not None:
        changes["cin"] = None
    if borrower.pan_enc is not None:
        changes["pan"] = None
    if borrower.constitution != _LISTED_CONSTITUTION:
        changes["constitution"] = _LISTED_CONSTITUTION
    if borrower.incorporation_date is not None:
        changes["incorporation_date"] = None
    if borrower.industry_code != company.industry_code:
        changes["industry_code"] = company.industry_code
    if borrower.group_id != group_id:
        changes["group_id"] = group_id
    if changes:
        master_data.update_borrower(
            principal,
            borrower.reference,
            expected_version=borrower.version,
            scope=scope,
            **changes,
        )


def _apply_company_contacts(
    session: Session,
    *,
    borrower: Borrower,
    company: ListedCompany,
    system_actor_id: UUID,
    now: datetime,
    request_id: str,
) -> None:
    """Replace generated people with roles, never with invented individuals.

    The contact is the company's secretarial desk at a non-deliverable
    `.invalid` address, so no notification can reach a real inbox; the
    promoter record names the disclosed promoter group, not a person.
    """

    email = f"secretarial@{company.symbol.lower()}.invalid"
    for contact in session.scalars(
        select(BorrowerContact).where(BorrowerContact.borrower_id == borrower.id)
    ):
        if (contact.name_enc, contact.email_enc, contact.phone_enc, contact.designation) == (
            _CONTACT_NAME,
            email,
            None,
            _CONTACT_DESIGNATION,
        ):
            continue
        contact.name_enc = _CONTACT_NAME
        contact.email_enc = email
        contact.phone_enc = None
        contact.designation = _CONTACT_DESIGNATION
        _touch(contact, system_actor_id=system_actor_id, now=now, request_id=request_id)
    promoter = (
        f"{company.business_group} promoter group"
        if company.business_group
        else f"Promoter group of {company.legal_name}"
    )
    for party in session.scalars(
        select(RelatedParty).where(
            RelatedParty.borrower_id == borrower.id, RelatedParty.party_type == "promoter"
        )
    ):
        if (party.name_enc, party.identifier_enc, party.role) == (promoter, None, _PROMOTER_ROLE):
            continue
        party.name_enc = promoter
        party.identifier_enc = None
        party.role = _PROMOTER_ROLE
        _touch(party, system_actor_id=system_actor_id, now=now, request_id=request_id)
    session.flush()


def _size_facilities(
    session: Session,
    *,
    borrower: Borrower,
    company: ListedCompany,
    system_actor_id: UUID,
    now: datetime,
    request_id: str,
) -> Facility | None:
    """Scale the borrower's illustrative facilities to its real borrowings.

    The bank's total limit is `_FACILITY_SHARE_OF_BORROWINGS` of the latest
    filed borrowings, split across the facilities in their existing
    proportions; drawing power and outstanding keep their ratios to the
    limit.  Returns the facility the covenants attach to.
    """

    facilities = list(
        session.scalars(
            select(Facility).where(Facility.borrower_id == borrower.id).order_by(Facility.reference)
        )
    )
    if not facilities:
        return None
    sheet = next(
        quarter.balance_sheet
        for quarter in reversed(company.quarters)
        if quarter.balance_sheet is not None
    )
    borrowings = sheet.get("BorrowingsCurrent", _ZERO) + sheet.get("BorrowingsNoncurrent", _ZERO)
    target = max(
        _FACILITY_MINIMUM_CRORE,
        (borrowings * _FACILITY_SHARE_OF_BORROWINGS / _FACILITY_ROUNDING_CRORE).quantize(
            Decimal("1")
        )
        * _FACILITY_ROUNDING_CRORE,
    )
    current_total = sum((facility.sanctioned_limit for facility in facilities), _ZERO)
    if current_total == target or current_total <= _ZERO:
        return facilities[0]
    remaining = target
    for position, facility in enumerate(facilities):
        if position == len(facilities) - 1:
            limit = remaining
        else:
            limit = (target * facility.sanctioned_limit / current_total).quantize(Decimal("0.01"))
            remaining -= limit
        factor = limit / facility.sanctioned_limit
        facility.sanctioned_limit = limit
        if facility.drawing_power is not None:
            facility.drawing_power = (facility.drawing_power * factor).quantize(Decimal("0.01"))
        if facility.outstanding is not None:
            facility.outstanding = (facility.outstanding * factor).quantize(Decimal("0.01"))
        _touch(facility, system_actor_id=system_actor_id, now=now, request_id=request_id)
    session.flush()
    return facilities[0]


def _ensure_business_groups(
    session: Session,
    companies: Sequence[ListedCompany],
    *,
    system_actor_id: UUID,
    now: datetime,
    request_id: str,
) -> dict[str, UUID]:
    """Create one borrower group per real business group in the roster."""

    names = sorted({company.business_group for company in companies if company.business_group})
    groups: dict[str, UUID] = {}
    for name in names:
        group = session.scalar(select(BorrowerGroup).where(BorrowerGroup.name == name))
        if group is None:
            group = BorrowerGroup(
                id=new_id(),
                name=name,
                parent_id=None,
                created_at=now,
                updated_at=now,
                created_by_id=system_actor_id,
                updated_by_id=system_actor_id,
                request_id=request_id,
            )
            session.add(group)
            session.flush()
        groups[name] = group.id
    return groups


def _remove_unused_generated_groups(session: Session) -> None:
    """Drop the reference generator's invented groups once nobody is in them."""

    generated = {build_group_name(sequence) for sequence in range(1, 11)}
    for group in session.scalars(select(BorrowerGroup).where(BorrowerGroup.name.in_(generated))):
        members = session.scalar(
            select(func.count()).select_from(Borrower).where(Borrower.group_id == group.id)
        )
        children = session.scalar(
            select(func.count())
            .select_from(BorrowerGroup)
            .where(BorrowerGroup.parent_id == group.id)
        )
        if not members and not children:
            session.delete(group)
    session.flush()


def _touch(row: object, *, system_actor_id: UUID, now: datetime, request_id: str) -> None:
    row.updated_at = now  # type: ignore[attr-defined]
    row.updated_by_id = system_actor_id  # type: ignore[attr-defined]
    row.request_id = request_id  # type: ignore[attr-defined]
    row.version += 1  # type: ignore[attr-defined]


def _ensure_demo_conduct(
    session: Session,
    *,
    facility: Facility,
    as_of_date: date,
    system_actor_id: UUID,
    now: datetime,
    request_id: str,
) -> None:
    """Write today's clean repayment-conduct row for the facility.

    The SMA band is read from `facility_conduct` for the exact as-of date.
    Every real company is recorded with no days past due: repayment conduct
    is private to a lender, and the demo attributes none to a real name.  The
    walkthrough's "Simulate a signal" is the only way an overdue appears, and
    it is labelled as synthetic wherever it shows.
    """

    existing = session.scalar(
        select(FacilityConduct).where(
            FacilityConduct.facility_id == facility.id,
            FacilityConduct.as_of_date == as_of_date,
        )
    )
    if existing is not None:
        return
    utilisation = (
        (facility.outstanding / facility.sanctioned_limit * 100).quantize(Decimal("0.01"))
        if facility.outstanding is not None and facility.sanctioned_limit
        else None
    )
    session.add(
        FacilityConduct(
            id=new_id(),
            facility_id=facility.id,
            as_of_date=as_of_date,
            outstanding=facility.outstanding,
            utilisation_pct=utilisation,
            days_past_due=0,
            overdue_amount=None,
            excess_amount=Decimal("0.00"),
            source_id=None,
            created_at=now,
            updated_at=now,
            created_by_id=system_actor_id,
            updated_by_id=system_actor_id,
            request_id=request_id,
        )
    )
    session.flush()


def _clear_non_curated_financials(
    session: Session,
    borrowers: Sequence[Borrower],
    periods: Sequence[tuple[str, date, date]],
) -> None:
    """Remove the base reference-portfolio's own random financial history.

    ``load_reference_portfolio`` gives every borrower randomly generated
    financial periods.  The filed statements fully own these borrowers'
    history, so the generated periods are removed rather than left beside a
    real company's numbers, where the forecast trend fit would also pick
    them up as off-trend observations.
    """

    curated_labels = {label for label, _, _ in periods}
    borrower_ids = [borrower.id for borrower in borrowers]
    stray_period_ids = tuple(
        session.scalars(
            select(FinancialPeriod.id).where(
                FinancialPeriod.borrower_id.in_(borrower_ids),
                FinancialPeriod.fy_label.not_in(curated_labels),
            )
        )
    )
    if not stray_period_ids:
        return
    session.execute(
        delete(StatementLineValue).where(StatementLineValue.period_id.in_(stray_period_ids))
    )
    session.execute(delete(FinancialPeriod).where(FinancialPeriod.id.in_(stray_period_ids)))
    session.flush()


def _clear_generated_conduct(session: Session, borrowers: Sequence[Borrower]) -> None:
    """Remove the reference generator's random signals and conduct rows.

    The generator writes one day of signals per borrower, some with days
    past due, and derives `facility_conduct` from them.  Beside a real name
    those would read as an observation about the company, so they go.  The
    generator's rows are the ones written without an acting user; nightly
    ingestion, the seed and the walkthrough all record who wrote theirs.
    """

    borrower_ids = [borrower.id for borrower in borrowers]
    facility_ids = tuple(
        session.scalars(select(Facility.id).where(Facility.borrower_id.in_(borrower_ids)))
    )
    session.execute(
        delete(SignalEvent).where(
            SignalEvent.borrower_id.in_(borrower_ids), SignalEvent.created_by_id.is_(None)
        )
    )
    if facility_ids:
        session.execute(
            delete(FacilityConduct).where(
                FacilityConduct.facility_id.in_(facility_ids),
                FacilityConduct.created_by_id.is_(None),
            )
        )
    session.flush()


def _write_demo_signal_source(
    path: Path, borrowers: Sequence[Borrower], session: Session, *, end_date: date
) -> int:
    """Write the illustrative lender-side signal history for the demo source.

    The file is deliberately a source artifact, not a database fixture.  The
    nightly ingest step reads it through ``FileSignalSource`` and therefore
    exercises the same validation, quarantine, evidence, attribution and
    audit path as a production connector.

    Behavioural signals are a lender's private data, so for real companies
    they can only be illustrative.  Every reading here is unremarkable and
    none is flagged adverse; each carries `demo_version` so the product
    labels it synthetic.  The file is rewritten on every seed so it always
    matches the current roster and ends the day before the launch.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    start_date = end_date - timedelta(days=DEMO_SIGNAL_DAYS - 1)
    rows: list[dict[str, object]] = []
    for borrower_index, borrower in enumerate(borrowers, start=1):
        facility = session.scalar(
            select(Facility)
            .where(Facility.borrower_id == borrower.id)
            .order_by(Facility.reference)
            .limit(1)
        )
        if facility is None:
            continue
        for day_offset in range(DEMO_SIGNAL_DAYS):
            event_date = start_date + timedelta(days=day_offset)
            for family, event_type, unit, value_field in DEMO_SIGNAL_FAMILIES:
                value = _neutral_signal_value(family, day_offset, borrower_index)
                rows.append(
                    {
                        "borrower_id": str(borrower.id),
                        "facility_id": str(facility.id),
                        "event_date": event_date.isoformat(),
                        "family": family,
                        "event_type": event_type,
                        "magnitude": value,
                        "unit": unit,
                        "payload": {
                            value_field: value,
                            "is_adverse": False,
                            "profile": "neutral",
                            "demo_version": DEMO_SIGNAL_VERSION,
                            "source_date": event_date.isoformat(),
                        },
                    }
                )
    path.write_text(
        json.dumps(_json_safe(rows), ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    return len(rows)


def _neutral_signal_value(family: str, day_offset: int, borrower_index: int) -> Decimal:
    """An unremarkable reading for one family on one day.

    Small, deterministic day-to-day variation keeps the Signals tab readable
    without suggesting a trend that a real company never showed.
    """

    wobble = Decimal((borrower_index * 3 + day_offset) % 5)
    value = {
        "account_activity": Decimal("-1.00") + wobble / 2,
        "payment": _ZERO,
        "utilisation": Decimal("55.00") + wobble * 2,
        "treasury": Decimal("0.150") + wobble / 100,
        "concentration": Decimal("18.00") + Decimal(borrower_index % 7),
    }[family]
    return value.quantize(Decimal("0.001"))


def _ensure_statement_import_mapping(
    session: Session,
    *,
    system_actor_id: UUID,
    now: datetime,
    request_id: str,
) -> ImportMapping:
    """Seed the one mapping the financial-statement import screen can use.

    Kept separate from `_ensure_import_batch`'s provenance-label mapping so
    the two are not confused: that one records where the seeded rows came
    from, this one is a real `ImportMappingSpec` a presenter can import a
    fresh quarter against.
    """

    mapping = session.scalar(
        select(ImportMapping).where(
            ImportMapping.name == DEMO_IMPORT_MAPPING_NAME, ImportMapping.version == 1
        )
    )
    if mapping is not None:
        return mapping
    mapping = ImportMapping(
        id=new_id(),
        name=DEMO_IMPORT_MAPPING_NAME,
        source_type="csv",
        version=1,
        spec=dict(DEMO_IMPORT_MAPPING_SPEC),
        is_active=True,
        created_at=now,
        updated_at=now,
        created_by_id=system_actor_id,
        updated_by_id=system_actor_id,
        request_id=request_id,
    )
    session.add(mapping)
    session.flush()
    return mapping


def _ensure_import_batch(
    session: Session,
    *,
    snapshot: CompanySnapshot,
    system_actor_id: UUID,
    now: datetime,
    request_id: str,
    borrower_count: int,
) -> ImportBatch:
    batch = session.scalar(
        select(ImportBatch).where(ImportBatch.content_hash == snapshot.content_hash)
    )
    if batch is not None:
        return batch
    mapping = session.scalar(
        select(ImportMapping).where(
            ImportMapping.name == _FILED_MAPPING_NAME, ImportMapping.version == 1
        )
    )
    if mapping is None:
        mapping = ImportMapping(
            id=new_id(),
            name=_FILED_MAPPING_NAME,
            source_type="api",
            version=1,
            spec={
                "mapping_version": 1,
                "purpose": "Quarterly results XBRL filed with NSE, mapped to the chart",
                "unit": "INR crore",
            },
            is_active=True,
            created_at=now,
            updated_at=now,
            created_by_id=system_actor_id,
            updated_by_id=system_actor_id,
            request_id=request_id,
        )
        session.add(mapping)
        session.flush()
    row_count = borrower_count * DEMO_PERIOD_COUNT
    batch = ImportBatch(
        id=new_id(),
        source_type="api",
        source_reference=DEMO_SOURCE_REFERENCE,
        mapping_id=mapping.id,
        content_hash=snapshot.content_hash,
        started_at=now,
        finished_at=now,
        row_count=row_count,
        accepted_count=row_count,
        quarantined_count=0,
        state="completed",
        report={
            "source": snapshot.source,
            "retrieved_on": snapshot.retrieved_on.isoformat(),
            "quarters": [end.isoformat() for end in snapshot.quarter_ends],
            "companies": [company.symbol for company in snapshot.companies],
            "rows": row_count,
        },
        created_at=now,
        updated_at=now,
        created_by_id=system_actor_id,
        updated_by_id=system_actor_id,
        request_id=request_id,
    )
    session.add(batch)
    session.flush()
    return batch


def _ensure_demo_threshold_snapshot(
    session: Session, *, system_actor_id: UUID, now: datetime, request_id: str
) -> UUID:
    existing = session.scalar(
        select(ThresholdSnapshot.id).where(
            ThresholdSnapshot.source == "calibration",
            ThresholdSnapshot.note == "Phase 7A demo calibration",
        )
    )
    if existing is not None:
        return existing
    values = _json_safe(ThresholdStore(path=DEFAULT_THRESHOLD_PATH).values())
    if not isinstance(values, dict):
        raise RuntimeError("The packaged threshold snapshot did not produce a JSON object.")
    values.update(
        {
            "T1": {"act": 0.70, "amber": 0.40},
            "T3": {"sustained_days": 14, "sustained_events": 3, "event_window_days": 30},
            "T4": {"headroom_erosion_pct": 0.05},
            "T5": {"contribution_share": 0.10},
        }
    )
    snapshot_id = new_id()
    session.add(
        ThresholdSnapshot(
            id=snapshot_id,
            values=values,
            source="calibration",
            effective_from=now,
            proposed_by_id=system_actor_id,
            approved_by_id=system_actor_id,
            note="Phase 7A demo calibration",
            version=(
                session.scalar(
                    select(ThresholdSnapshot.version)
                    .order_by(ThresholdSnapshot.version.desc())
                    .limit(1)
                )
                or 0
            )
            + 1,
            created_at=now,
            updated_at=now,
            created_by_id=system_actor_id,
            updated_by_id=system_actor_id,
            request_id=request_id,
        )
    )
    session.flush()
    return snapshot_id


def _ensure_statement_lines(
    session: Session,
    *,
    period: FinancialPeriod,
    groups: Sequence[_LineGroup],
    batch: ImportBatch,
    system_actor_id: UUID,
    now: datetime,
    request_id: str,
) -> None:
    """Write each filed line once, pointing at the filing it came from."""

    existing_codes = set(
        session.scalars(
            select(StatementLineValue.line_code).where(StatementLineValue.period_id == period.id)
        )
    )
    for group in groups:
        missing = {code: value for code, value in group.lines.items() if code not in existing_codes}
        if not missing:
            continue
        provenance = FieldProvenance(
            id=new_id(),
            source_type="api",
            source_reference=group.source_reference,
            row_reference=group.row_reference,
            mapping_version=1,
            ingested_at=now,
            batch_id=batch.id,
            transform_note=group.transform_note,
            created_at=now,
            updated_at=now,
            created_by_id=system_actor_id,
            updated_by_id=system_actor_id,
            request_id=request_id,
        )
        session.add(provenance)
        session.flush()
        for code, value in missing.items():
            session.add(
                StatementLineValue(
                    id=new_id(),
                    period_id=period.id,
                    line_code=code,
                    value=value,
                    unit="amount",
                    currency="INR",
                    provenance_id=provenance.id,
                    created_at=now,
                    updated_at=now,
                    created_by_id=system_actor_id,
                    updated_by_id=system_actor_id,
                    request_id=request_id,
                )
            )
            existing_codes.add(code)
    session.flush()


def _json_safe(value: object) -> object:
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return value


__all__ = [
    "COMPANY_SNAPSHOT_PATH",
    "CompanySnapshot",
    "DemoSeedReport",
    "ListedCompany",
    "filed_statement",
    "load_company_snapshot",
    "seed_demo_covenants",
    "snapshot_periods",
]
