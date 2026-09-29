"""The queue's market overlay respects scope and never rewrites stored ranks or scores."""

from datetime import date, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from covenant_radar.asgi import create_app
from covenant_radar.services.market_intelligence import MarketIntelligenceService
from covenant_radar.web.routes.queue import create_queue_router
from tests.integration.test_market_intelligence_routes import add_financials, add_industries
from tests.integration.test_queue_screen import _Fixture
from tests.unit.test_market_intelligence import CAPTURED_AT, fixture_fetch


def test_queue_shows_market_pressure_without_mutating_rank_or_scores(tmp_path):
    fixture = _Fixture()
    now = [CAPTURED_AT]
    outage = [False]
    try:
        allowed = fixture.portfolio("ALLOWED")
        hidden = fixture.portfolio("HIDDEN")
        fixture.grant_scope(allowed)
        add_industries(fixture, ("H49", "Land transport"), ("J62", "Software"))
        truck = fixture.borrower(allowed, "TRUCK")
        truck.industry_code = "H49"
        software = fixture.borrower(allowed, "SOFTWARE")
        software.industry_code = "J62"
        secret = fixture.borrower(hidden, "SECRET")
        secret.industry_code = "H49"
        add_financials(fixture, truck, ebit="17.4", revenue="328")
        add_financials(fixture, secret, ebit="17.4", revenue="328")
        fixture.case(truck, state="open")
        run = fixture.run(date(2026, 9, 29))
        record = fixture.entry(run, truck, 2, band="amber", probability=Decimal("0.50"))
        fixture.entry(run, software, 1, band="act")
        fixture.entry(run, secret, 3, band="amber")
        fixture.session.flush()

        def fetch(source, instant):
            if outage[0]:
                raise TimeoutError()
            return fixture_fetch(source, instant)

        service = MarketIntelligenceService(
            cache_path=tmp_path / "market.json",
            fetcher=fetch,
            clock=lambda: now[0],
            background=False,
        )
        app = create_app(
            routers=(create_queue_router(fixture.session, intelligence=service),),
            principal_resolver=lambda request: fixture.principal,
        )
        with TestClient(app) as client:
            text = client.get("/").text
            assert 'data-market-priority="now"' in text
            assert "Market pressure: move exceeds cushion" in text
            assert "Brent crude" in text and "breakeven" in text
            assert "SECRET" not in text
            # Software has no statements: no badge, no invented assessment.
            software_row = text.split('data-borrower-id="SOFTWARE"', 1)[1].split("</tr>", 1)[0]
            assert "queue-market-badge" not in software_row
            assert text.index('data-borrower-id="SOFTWARE"') < text.index(
                'data-borrower-id="TRUCK"'
            )

            now[0] += timedelta(minutes=16)
            outage[0] = True
            fragment = client.get("/", headers={"HX-Request": "true", "HX-Target": "queue-ledger"})
            assert fragment.status_code == 200
            # The last good market data stays usable during an outage.
            assert 'data-market-priority="now"' in fragment.text
            fixture.session.refresh(record)
            assert (record.rank, record.band, record.probability) == (2, "amber", Decimal("0.50"))
    finally:
        fixture.close()


def test_queue_without_market_data_shows_no_market_claims(tmp_path):
    fixture = _Fixture()
    try:
        allowed = fixture.portfolio("ALLOWED")
        fixture.grant_scope(allowed)
        add_industries(fixture, ("H49", "Land transport"))
        truck = fixture.borrower(allowed, "TRUCK")
        truck.industry_code = "H49"
        add_financials(fixture, truck, ebit="17.4", revenue="328")
        run = fixture.run(date(2026, 9, 29))
        fixture.entry(run, truck, 1, band="amber")
        fixture.session.flush()
        service = MarketIntelligenceService(cache_path=tmp_path / "m.json", enabled=False)
        app = create_app(
            routers=(create_queue_router(fixture.session, intelligence=service),),
            principal_resolver=lambda request: fixture.principal,
        )
        with TestClient(app) as client:
            text = client.get("/").text
            assert "queue-market-badge" not in text
            assert "queue-market-impact" not in text
    finally:
        fixture.close()
