"""A remediation package recorded on the planner is what the next memo cites."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from covenant_radar.ai.shapes import check_stage7_shapes, normalise_catalogue_actions
from covenant_radar.asgi import create_app
from covenant_radar.db.models import Intervention
from covenant_radar.db.models.audit import AuditEvent
from covenant_radar.db.models.covenant import Covenant, CovenantVersion
from covenant_radar.db.models.forecast import Forecast, Simulation, TriageEntry
from covenant_radar.db.models.statements import StatementLineValue
from covenant_radar.db.scoping import resolve_scope
from covenant_radar.domain.remediation import PLANNER_CATALOGUE_CODES, RECORD_SOURCE
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.catalogue import CatalogueService
from covenant_radar.services.memo import MemoAssemblyService, MemoOutcomeKind
from covenant_radar.services.memo_records import collect_memo_records
from covenant_radar.web.routes.remediation import create_remediation_router
from covenant_radar.web.routes.simulator import _resolve_entries
from tests.integration.test_case_file import _NOW, financials
from tests.integration.test_memo_route import _MemoFixture

pytestmark = pytest.mark.integration

_SEED = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "covenant_radar"
    / "db"
    / "seed"
    / "data"
    / "interventions.json"
)


class _PlannerFixture(_MemoFixture):
    """The memo world with filed quarters, the seeded catalogue and the planner.

    ``strained`` thins the filed margins: on the shared fixture's lines,
    retained profit outgrows the debt and the projection clears by itself,
    so there is no package to record.
    """

    def __init__(self, *, strained: bool = True) -> None:
        super().__init__()
        self.principal = Principal.user(
            self.principal.id,
            (Permission.VIEW_BORROWER, Permission.GENERATE_MEMO, Permission.RUN_SIMULATION),
        )
        financials(self)
        if strained:
            self._strain()
        self._catalogue()

    def _strain(self) -> None:
        thinner = {"ebit": Decimal("14"), "ebitda": Decimal("28"), "finance_cost": Decimal("12")}
        for line in self.session.execute(
            select(StatementLineValue).where(StatementLineValue.line_code.in_(thinner))
        ).scalars():
            line.value = thinner[line.line_code]
        self.session.flush()

    def _catalogue(self) -> None:
        """The real seeded catalogue, so its rows are proven usable as citations."""

        for item in json.loads(_SEED.read_text(encoding="utf-8"))["interventions"]:
            self.session.add(
                Intervention(
                    id=uuid4(),
                    code=item["code"],
                    role_tag=item["role_tag"],
                    text=item["text"],
                    effect_model=item["effect_model"],
                    effect_parameters=item["effect_parameters"],
                    applicable_covenant_classes=item["applicable_covenant_classes"],
                    requires_approval=item["requires_approval"],
                    is_active=item["is_active"],
                    created_at=_NOW,
                    updated_at=_NOW,
                    request_id="rq-remedy-catalogue",
                )
            )
        self.session.flush()

    def planner(self) -> TestClient:
        app = create_app(
            routers=(create_remediation_router(self.session),),
            principal_resolver=lambda _request: self.principal,
        )
        return TestClient(app)

    def subject_on_leverage(self) -> Forecast:
        """Make the projected leverage covenant the memo's subject."""

        version = self.session.execute(
            select(CovenantVersion)
            .join(Covenant, Covenant.id == CovenantVersion.covenant_id)
            .where(Covenant.reference == "CV-T075-LEV")
        ).scalar_one()
        forecast = Forecast(
            id=uuid4(),
            run_id=self.run.id,
            covenant_version_id=version.id,
            horizon_days=90,
            probability=Decimal("0.7000"),
            confidence=Decimal("0.90"),
            below_confidence_floor=False,
            direction="max",
            created_at=_NOW,
            updated_at=_NOW,
            request_id="rq-remedy-forecast",
        )
        self.session.add(forecast)
        self.session.add(
            TriageEntry(
                id=uuid4(),
                run_id=self.run.id,
                borrower_id=self.borrower.id,
                worst_covenant_version_id=version.id,
                worst_horizon=90,
                probability=Decimal("0.7000"),
                confidence=Decimal("0.90"),
                exposure=Decimal("624000000"),
                urgency=Decimal("1"),
                band="watch",
                rank=1,
                created_at=_NOW,
                updated_at=_NOW,
                request_id="rq-remedy-triage",
            )
        )
        self.session.flush()
        return forecast

    def recorded(self) -> list[Simulation]:
        return [
            row
            for row in self.session.execute(select(Simulation)).scalars()
            if row.parameters.get("source") == RECORD_SOURCE
        ]


def _record(client: TestClient, data: dict[str, str] | None = None):
    return client.post(
        "/borrowers/B-T075/remediation/record", data=data or {}, follow_redirects=False
    )


def test_recording_writes_one_audited_step_per_lever_against_the_memo_forecast() -> None:
    fixture = _PlannerFixture()
    try:
        forecast = fixture.subject_on_leverage()
        with fixture.planner() as client:
            before = client.get("/borrowers/B-T075/remediation")
            response = _record(client)
            after = client.get("/borrowers/B-T075/remediation")

        assert "Carry this package into the memo" in before.text
        assert response.status_code == 303
        assert response.headers["location"].endswith("/borrowers/B-T075/remediation#remedy-memo")

        rows = sorted(fixture.recorded(), key=lambda row: row.parameters["step"])
        assert rows, "the recommended package should have at least one step"
        assert {row.forecast_id for row in rows} == {forecast.id}
        assert len({row.parameters["package_id"] for row in rows}) == 1
        for row in rows:
            assert row.parameters["wording"].startswith(row.parameters["wording"][0])
            assert row.parameters["size_display"] in row.parameters["wording"]
            # The leverage covenant is projected, so each step carries the
            # planner's cumulative probability and says what it is not.
            assert row.probability is not None
            statements = row.assumptions["assumptions"]
            assert any("not the forecast's 90-day probability" in line for line in statements)
        audits = fixture.session.execute(
            select(AuditEvent).where(AuditEvent.event_type == "simulation_created")
        ).scalars()
        assert len(list(audits)) == len(rows)

        assert "The memo cites this package" in after.text
        assert "Record this package for the memo" not in after.text
    finally:
        fixture.close()


def test_the_memo_recommends_the_recorded_steps_and_passes_the_catalogue_check() -> None:
    fixture = _PlannerFixture()
    try:
        fixture.ground()
        with fixture.planner() as client:
            assert _record(client).status_code == 303
        scope = resolve_scope(fixture.principal, fixture.session)
        records = collect_memo_records(fixture.session, fixture.borrower, scope=scope).records
        steps = sorted(fixture.recorded(), key=lambda row: row.parameters["step"])

        recommended = [record.values for record in records.recommendations]
        assert [item["text"] for item in recommended] == [
            row.parameters["wording"] for row in steps
        ]
        assert {item["code"] for item in recommended} <= PLANNER_CATALOGUE_CODES
        assert all(
            record.reference.record_type == "simulation" for record in records.recommendations
        )
        # The memo's subject (CV-T075) is not one the line model projects, so
        # the steps carry no probability rather than a borrowed one.
        assert all(row.probability is None for row in steps)

        slots = MemoAssemblyService().assemble(records)
        actions = normalise_catalogue_actions(CatalogueService(fixture.session))
        first = recommended[0]
        draft = {
            "headline": "Total Debt / Tangible Net Worth is projected to reach the action point.",
            "summary": "A sized remediation package is recorded for review.",
            "drivers": ["Cash-flow pressure"],
            "actions": [{"id": first["code"], "role_tag": first["role_tag"]}],
            "recommended_next_step": first["text"],
            "disclaimer": "human credit review is required before action",
        }
        report = check_stage7_shapes(draft, slots, actions, require_actions=True)
        assert report.passed, report.failures

        # A generic entry cannot be passed off as the step's wording.
        report = check_stage7_shapes(
            {**draft, "recommended_next_step": "Agree a monitored operating-cost programme."},
            slots,
            actions,
            require_actions=True,
        )
        assert not report.passed
    finally:
        fixture.close()


def test_a_drafted_memo_persists_the_sized_steps() -> None:
    fixture = _PlannerFixture()
    try:
        fixture.ground()
        with fixture.planner() as client:
            assert _record(client).status_code == 303
        fixture.session.commit()
        steps = sorted(fixture.recorded(), key=lambda row: row.parameters["step"])
        first = steps[0]
        intervention = fixture.session.get(Intervention, first.intervention_id)
        assert intervention is not None
        reply = json.dumps(
            {
                "headline": "Total Debt / Tangible Net Worth is projected to reach the "
                "action point on 2026-11-29.",
                "summary": "A sized remediation package is recorded for review.",
                "drivers": ["Cash-flow pressure"],
                "actions": [{"id": intervention.code, "role_tag": intervention.role_tag}],
                "recommended_next_step": first.parameters["wording"],
                "disclaimer": "human credit review is required before action",
            }
        )
        scope = resolve_scope(fixture.principal, fixture.session)
        outcome = fixture.generation_service(reply).generate(
            borrower_id=fixture.borrower.id,
            records=collect_memo_records(fixture.session, fixture.borrower, scope=scope).records,
            catalogue=CatalogueService(fixture.session),
            run_id=fixture.run.id,
        )

        assert outcome.kind is MemoOutcomeKind.GENERATED, outcome.message
        assert outcome.memo is not None
        assert outcome.memo.actions == {
            "items": [{"id": intervention.code, "role_tag": intervention.role_tag}]
        }
        cited = [item["text"] for item in outcome.memo.simulations["items"]]
        assert cited[: len(steps)] == [row.parameters["wording"] for row in steps]
    finally:
        fixture.close()


def test_only_the_latest_package_is_cited() -> None:
    fixture = _PlannerFixture()
    try:
        fixture.subject_on_leverage()
        with fixture.planner() as client:
            assert _record(client).status_code == 303
            first_package = {row.parameters["package_id"] for row in fixture.recorded()}
            code = fixture.recorded()[0].parameters["lever_code"]
            size = float(fixture.recorded()[0].parameters["size"])
            custom = {f"size.{code}": str(round(size / 2, 2))}
            assert _record(client, custom).status_code == 303
            recommended_view = client.get("/borrowers/B-T075/remediation")

        latest = {row.parameters["package_id"] for row in fixture.recorded()} - first_package
        assert len(latest) == 1
        scope = resolve_scope(fixture.principal, fixture.session)
        records = collect_memo_records(fixture.session, fixture.borrower, scope=scope).records
        cited = {
            row.parameters["package_id"]
            for row in fixture.recorded()
            if row.id in {record.reference.record_id for record in records.simulations}
        }
        assert cited == latest
        assert len(records.recommendations) == 1
        assert "The memo cites a different package" in recommended_view.text
        assert "Record this package instead" in recommended_view.text
    finally:
        fixture.close()


def test_a_filed_breach_the_projection_clears_has_nothing_to_record() -> None:
    fixture = _PlannerFixture(strained=False)
    try:
        fixture.subject_on_leverage()
        with fixture.planner() as client:
            page = client.get("/borrowers/B-T075/remediation")
            response = _record(client)

        assert "projected to clear without a package" in page.text
        assert 'id="remedy-plan-title"' not in page.text
        assert "No package is sized" in page.text
        assert 'id="remedy-memo"' not in page.text
        assert response.status_code == 422
        assert "no lever is sized" in response.text
        assert fixture.recorded() == []
    finally:
        fixture.close()


def test_recording_without_a_forecast_is_refused_and_writes_nothing() -> None:
    fixture = _PlannerFixture()
    try:
        with fixture.planner() as client:
            page = client.get("/borrowers/B-T075/remediation")
            response = _record(client)

        assert "Not available for the memo yet" in page.text
        assert response.status_code == 422
        assert "nothing to attach the package to" in response.text
        assert fixture.recorded() == []
    finally:
        fixture.close()


def test_recording_requires_simulation_permission() -> None:
    fixture = _PlannerFixture()
    try:
        fixture.subject_on_leverage()
        fixture.principal = Principal.user(fixture.principal.id, (Permission.VIEW_BORROWER,))
        with fixture.planner() as client:
            response = _record(client)
        assert response.status_code == 403
        assert fixture.recorded() == []
    finally:
        fixture.close()


def test_without_a_recording_the_memo_offers_only_fixed_catalogue_entries() -> None:
    fixture = _PlannerFixture()
    try:
        # A leverage covenant, so the planner's entries (which list leverage)
        # would apply too if they were not held back for recorded packages.
        fixture.covenant.covenant_class = "leverage"
        fixture.ground()
        scope = resolve_scope(fixture.principal, fixture.session)
        records = collect_memo_records(fixture.session, fixture.borrower, scope=scope).records
        codes = {record.values["code"] for record in records.recommendations}
        assert codes
        assert not codes & PLANNER_CATALOGUE_CODES
    finally:
        fixture.close()


def test_the_fixed_effect_simulator_refuses_planner_entries() -> None:
    fixture = _PlannerFixture()
    try:
        catalogue = CatalogueService(fixture.session)
        with pytest.raises(Exception, match="sized per borrower in the remediation planner"):
            _resolve_entries(catalogue, ("TERM-OUT-SHORT-TERM-DEBT",), "leverage")
        offered = {entry.code for entry in catalogue.applicable("leverage")}
        # Still governed catalogue entries, so the memo can cite them.
        assert PLANNER_CATALOGUE_CODES <= {entry.code for entry in catalogue.list()}
        assert offered
    finally:
        fixture.close()
