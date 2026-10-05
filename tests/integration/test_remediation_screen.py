"""Integration coverage for the borrower remediation planner screen."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from covenant_radar.asgi import create_app
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.web.routes.borrower import create_borrower_router
from covenant_radar.web.routes.remediation import create_remediation_router
from tests.integration.test_case_file import _Fixture, financials

pytestmark = pytest.mark.integration


def _client(fixture: _Fixture) -> TestClient:
    app = create_app(
        routers=(
            create_remediation_router(fixture.session),
            create_borrower_router(fixture.session),
        ),
        principal_resolver=lambda _request: fixture.principal,
    )
    return TestClient(app)


def _simulating(fixture: _Fixture) -> None:
    fixture.principal = Principal.user(
        fixture.principal.id, (Permission.VIEW_BORROWER, Permission.RUN_SIMULATION)
    )


def test_planner_sizes_levers_from_the_borrowers_filings() -> None:
    fixture = _Fixture()
    try:
        _simulating(fixture)
        financials(fixture)
        with _client(fixture) as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}/remediation")

        assert response.status_code == 200
        body = response.text
        # The filed position is the stored test value, not a constant.
        assert "Filed 3.40x" in body
        assert "test ≤ 3.25x" in body
        # Leverage is in breach on the filings, but retained profit outgrows
        # the debt drift, so the projection clears and no package is sized —
        # never a zero-sized step.  Levers still show capacity in rupees from
        # this borrower's own lines.
        assert "projected to clear without a package" in body
        assert 'id="remedy-plan-title"' not in body
        assert "₹" in body
        # Lines the filings do not carry are named as the reason, not guessed.
        assert "Cash is not reported separately in the filed lines." in body
        assert "not credit decisions" in body
        assert 'class="fan"' in body
    finally:
        fixture.close()


def test_planner_clamps_a_requested_size_and_marks_it_as_the_readers_package() -> None:
    fixture = _Fixture()
    try:
        _simulating(fixture)
        financials(fixture)
        reference = fixture.borrower.reference
        with _client(fixture) as client:
            response = client.get(
                f"/borrowers/{reference}/remediation",
                params={"size.EQUITY-TO-RETIRE-DEBT": "999999999", "size.NOT-A-LEVER": "5"},
            )

        assert response.status_code == 200
        assert "Your package" in response.text
        # Equity is bounded at a quarter of tangible net worth (₹100 cr here).
        assert 'name="size.EQUITY-TO-RETIRE-DEBT"' in response.text
        assert 'value="25.0"' in response.text
    finally:
        fixture.close()


def test_drawer_summary_names_the_package_for_the_covenant() -> None:
    fixture = _Fixture()
    try:
        _simulating(fixture)
        financials(fixture)
        with _client(fixture) as client:
            response = client.get(
                f"/borrowers/{fixture.borrower.reference}/remediation/summary",
                params={"covenant": "CV-T075-LEV"},
            )

        assert response.status_code == 200
        assert "Leverage ratio: filed 3.40x against ≤ 3.25x" in response.text
        assert "Open the remediation planner" in response.text
        assert "<html" not in response.text
    finally:
        fixture.close()


def test_planner_requires_simulation_permission() -> None:
    fixture = _Fixture()
    try:
        financials(fixture)
        with _client(fixture) as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}/remediation")
        assert response.status_code == 403
    finally:
        fixture.close()


def test_out_of_scope_borrower_is_not_found() -> None:
    fixture = _Fixture()
    try:
        financials(fixture)
        fixture.principal = Principal.user(
            uuid4(), (Permission.VIEW_BORROWER, Permission.RUN_SIMULATION)
        )
        with _client(fixture) as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}/remediation")
        assert response.status_code == 404
    finally:
        fixture.close()


def test_case_file_links_to_the_planner_for_simulating_users() -> None:
    fixture = _Fixture()
    try:
        _simulating(fixture)
        fixture.triage()
        financials(fixture)
        with _client(fixture) as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}")

        assert response.status_code == 200
        assert f"/borrowers/{fixture.borrower.reference}/remediation" in response.text
        assert "Remediation planner" in response.text
    finally:
        fixture.close()


def test_covenant_the_line_model_cannot_project_is_named_not_dropped() -> None:
    fixture = _Fixture()
    try:
        _simulating(fixture)
        financials(fixture)
        with _client(fixture) as client:
            response = client.get(f"/borrowers/{fixture.borrower.reference}/remediation")

        assert response.status_code == 200
        # CV-T075 has no ratio definition the simulator can project.
        assert "Not projected here" in response.text
        assert "CV-T075 · Total Debt / Tangible Net Worth" in response.text
    finally:
        fixture.close()


def test_borrower_without_quarterly_filings_gets_an_empty_state() -> None:
    fixture = _Fixture()
    try:
        _simulating(fixture)
        reference = fixture.borrower.reference
        with _client(fixture) as client:
            page = client.get(f"/borrowers/{reference}/remediation")
            summary = client.get(f"/borrowers/{reference}/remediation/summary")

        assert page.status_code == 200
        assert "No quarterly filings to plan from" in page.text
        assert summary.status_code == 200
        assert "No quarterly filings to plan from" in summary.text
    finally:
        fixture.close()
