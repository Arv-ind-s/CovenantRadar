"""Market pressure routes: scope, escaping, filtering, permissions and the review draft."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

from fastapi.testclient import TestClient

from covenant_radar.asgi import create_app
from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.models.reference import IndustryReference
from covenant_radar.db.models.statements import (
    FieldProvenance,
    FinancialPeriod,
    StatementLineValue,
)
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.market_intelligence import MarketIntelligenceService
from covenant_radar.web.routes.intelligence import create_intelligence_router
from tests.integration.test_queue_screen import _Fixture
from tests.unit.test_market_intelligence import CAPTURED_AT, fixture_fetch

_NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)


def add_industries(fixture: _Fixture, *codes: tuple[str, str]) -> None:
    for code, name in codes:
        fixture.session.add(
            IndustryReference(
                code=code,
                name=name,
                taxonomy_version="test",
                created_at=_NOW,
                updated_at=_NOW,
                request_id=f"rq-industry-{code.lower()}",
            )
        )
    fixture.session.flush()


def add_financials(
    fixture: _Fixture,
    borrower,
    *,
    ebit: str,
    revenue: str = "380",
    finance_cost: str = "10",
    debt: str = "128",
    threshold: str = "1.50",
) -> None:
    """A live interest-cover covenant and one complete reported quarter to 30 Jun 2026."""
    tag = borrower.reference.lower()
    facility = Facility(
        reference=f"F-{borrower.reference}",
        borrower_id=borrower.id,
        facility_type="term_loan",
        sanctioned_limit=Decimal("1000"),
        currency="INR",
        sanction_date=date(2026, 1, 1),
        effective_from=date(2026, 1, 1),
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-mkt-facility-{tag}",
    )
    fixture.session.add(facility)
    fixture.session.flush()
    covenant = Covenant(
        reference=f"C-ICR-{borrower.reference}",
        facility_id=facility.id,
        name="Interest coverage ratio",
        covenant_class="financial",
        is_active=True,
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-mkt-covenant-{tag}",
    )
    fixture.session.add(covenant)
    fixture.session.flush()
    period = FinancialPeriod(
        id=uuid4(),
        borrower_id=borrower.id,
        fy_label="FY26Q2",
        period_type="quarterly",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 6, 30),
        is_complete=True,
        is_audited=False,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-mkt-period-{tag}",
    )
    provenance = FieldProvenance(
        id=uuid4(),
        source_type="json",
        source_reference="tests/integration/test_market_intelligence_routes.py",
        mapping_version=1,
        ingested_at=_NOW,
        batch_id=uuid4(),
        created_at=_NOW,
        updated_at=_NOW,
        request_id=f"rq-mkt-provenance-{tag}",
    )
    fixture.session.add_all(
        [
            CovenantVersion(
                covenant_id=covenant.id,
                version_no=1,
                definition_ref="interest_coverage_ratio",
                threshold=Decimal(threshold),
                direction="min",
                unit="x",
                frequency="quarterly",
                test_basis="standalone",
                effective_from=date(2026, 1, 1),
                status="live",
                tested_at_least_once=True,
                registered_by_id=fixture.principal.id,
                created_at=_NOW,
                updated_at=_NOW,
                request_id=f"rq-mkt-version-{tag}",
            ),
            period,
            provenance,
        ]
    )
    fixture.session.flush()
    lines = {"revenue": revenue, "ebit": ebit, "finance_cost": finance_cost, "total_debt": debt}
    for code, value in lines.items():
        fixture.session.add(
            StatementLineValue(
                id=uuid4(),
                period_id=period.id,
                line_code=code,
                value=Decimal(value),
                unit="amount",
                currency="INR",
                provenance_id=provenance.id,
                created_at=_NOW,
                updated_at=_NOW,
                request_id=f"rq-mkt-line-{tag}",
            )
        )
    fixture.session.flush()


def fixture_service(tmp_path, fetcher=fixture_fetch) -> MarketIntelligenceService:
    return MarketIntelligenceService(
        cache_path=tmp_path / "market.json",
        fetcher=fetcher,
        clock=lambda: CAPTURED_AT,
        background=False,
    )


def test_briefing_sizes_scoped_borrowers_against_real_markets(tmp_path):
    fixture = _Fixture()
    try:
        allowed = fixture.portfolio("ALLOWED")
        hidden = fixture.portfolio("HIDDEN")
        fixture.grant_scope(allowed)
        add_industries(fixture, ("H51", "Air transport"), ("C24", "Basic metals"))
        visible = fixture.borrower(allowed, "AIR", legal_name="Visible Aviation Private Limited")
        visible.industry_code = "H51"
        metals = fixture.borrower(allowed, "METAL", legal_name="Visible Metals Private Limited")
        metals.industry_code = "C24"
        secret = fixture.borrower(hidden, "SECRET", legal_name="SECRET Aviation Private Limited")
        secret.industry_code = "H51"
        add_financials(fixture, visible, ebit="16.2")
        add_financials(fixture, metals, ebit="40")
        add_financials(fixture, secret, ebit="15.1")

        def fetch(source, instant):
            items = fixture_fetch(source, instant)
            if source.key == "news:brent":
                items = [
                    {
                        "id": "hostile",
                        "title": "<script>alert(1)</script> crude spikes on supply fears",
                        "url": "https://news.example/hostile",
                        "publisher": "Reuters",
                        "publisher_url": "https://www.reuters.com",
                        "published_at": instant.isoformat(),
                        "established": True,
                    },
                    *items,
                ]
            return items

        service = fixture_service(tmp_path, fetch)
        app = create_app(
            routers=(create_intelligence_router(fixture.session, service=service),),
            principal_resolver=lambda request: fixture.principal,
        )
        with TestClient(app) as client:
            body = client.get("/intelligence/data").json()
            assert body["borrower_count"] == 2
            assert [row["reference"] for row in body["attention"]] == ["AIR"]
            assert body["attention"][0]["status"] == "exceeds"
            assert body["reporting"]["next_quarter"] == "Sep 2026 quarter"
            brent = next(tile for tile in body["tiles"] if tile["key"] == "brent")
            assert brent["latest"] == "114.89" and brent["latest_date"] == "22 Sep 2026"
            assert "spark" not in brent

            html = client.get("/intelligence")
            assert html.status_code == 200
            text = html.text
            assert "SECRET" not in text
            assert "Visible Aviation Private Limited" in text
            assert "Move exceeds cushion" in text
            assert "https://fred.stlouisfed.org/series/DCOILBRENTEU" in text
            assert "<script>alert(1)</script>" not in text
            assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
            assert "Record market review in case" not in text  # needs UPDATE_CASE

            cotton = client.get("/intelligence/data?driver=cotton").json()
            assert cotton["attention"] == [] and cotton["calm"] == []
            partial = client.get("/intelligence", headers={"HX-Request": "true"})
            assert 'id="intelligence-workspace"' in partial.text
            assert "<!doctype html>" not in partial.text.lower()
            assert client.post("/intelligence/refresh", follow_redirects=False).status_code == 303
    finally:
        fixture.close()


def test_review_draft_cites_sources_and_respects_scope(tmp_path):
    fixture = _Fixture()
    try:
        allowed = fixture.portfolio("ALLOWED")
        hidden = fixture.portfolio("HIDDEN")
        fixture.grant_scope(allowed)
        add_industries(fixture, ("H51", "Air transport"))
        visible = fixture.borrower(allowed, "AIR", legal_name="Visible Aviation Private Limited")
        visible.industry_code = "H51"
        secret = fixture.borrower(hidden, "SECRET")
        secret.industry_code = "H51"
        add_financials(fixture, visible, ebit="16.2")
        case = fixture.case(visible, state="open")
        app = create_app(
            routers=(
                create_intelligence_router(fixture.session, service=fixture_service(tmp_path)),
            ),
            principal_resolver=lambda request: fixture.principal,
        )
        with TestClient(app) as client:
            assert client.get("/intelligence/review/AIR").status_code == 403
            fixture.principal = Principal.user(
                fixture.principal.id, (Permission.VIEW_QUEUE, Permission.UPDATE_CASE)
            )
            response = client.get("/intelligence/review/AIR")
            assert response.status_code == 200
            assert f'action="/cases/{case.reference}"' in response.text
            draft = response.text.split('name="comment"', 1)[1].split("</textarea>", 1)[0]
            assert "Market pressure review" in draft
            assert "https://fred.stlouisfed.org/series/DCOILBRENTEU" in draft
            assert "assumed input-cost share" in draft
            assert len(draft) < 4_600  # HTML-escaped; the raw note stays within 4,000
            assert client.get("/intelligence/review/SECRET").status_code == 404
            assert client.get("/intelligence/review/AIR?case=OTHER").status_code == 404
    finally:
        fixture.close()


def test_market_intelligence_requires_queue_permission(tmp_path):
    fixture = _Fixture()
    try:
        fixture.principal = Principal.user(uuid4(), (Permission.VIEW_BORROWER,))
        service = MarketIntelligenceService(cache_path=tmp_path / "cache.json", enabled=False)
        app = create_app(
            routers=(create_intelligence_router(fixture.session, service=service),),
            principal_resolver=lambda request: fixture.principal,
        )
        with TestClient(app) as client:
            for path in ("/intelligence", "/intelligence/data"):
                assert client.get(path).status_code == 403
            assert client.post("/intelligence/refresh").status_code == 403
    finally:
        fixture.close()


def test_offline_first_visit_says_so_and_substitutes_nothing(tmp_path):
    fixture = _Fixture()
    try:
        fixture.grant_scope(fixture.portfolio("ALLOWED"))
        service = MarketIntelligenceService(cache_path=tmp_path / "cache.json", enabled=False)
        app = create_app(
            routers=(create_intelligence_router(fixture.session, service=service),),
            principal_resolver=lambda request: fixture.principal,
        )
        with TestClient(app) as client:
            text = client.get("/intelligence").text
            assert "Offline mode" in text
            assert "No estimated or synthetic values are substituted" in text
    finally:
        fixture.close()


def test_a_later_annual_statement_does_not_replace_the_quarter(tmp_path):
    from covenant_radar.services.borrower_market import load_positions

    fixture = _Fixture()
    try:
        allowed = fixture.portfolio("ALLOWED")
        fixture.grant_scope(allowed)
        borrower = fixture.borrower(allowed, "AIR")
        add_financials(fixture, borrower, ebit="16.2")
        annual = FinancialPeriod(
            id=uuid4(),
            borrower_id=borrower.id,
            fy_label="FY27",
            period_type="annual",
            period_start=date(2025, 10, 1),
            period_end=date(2026, 9, 30),
            is_complete=True,
            is_audited=True,
            version=1,
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-mkt-annual",
        )
        fixture.session.add(annual)
        fixture.session.flush()
        position = load_positions(fixture.session, [borrower.id])[borrower.id]
        assert position.fy_label == "FY26Q2"
        assert position.period_end == date(2026, 6, 30)
        assert position.finance_cost == 10.0
    finally:
        fixture.close()
