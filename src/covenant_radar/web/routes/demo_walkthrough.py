"""Explicitly synthetic, disabled-by-default monitoring walkthrough."""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.api.deps import requires
from covenant_radar.core.context import new_request_id
from covenant_radar.db.models.facility import Facility, FacilityConduct
from covenant_radar.db.repositories.borrower import BorrowerRepository
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.demo.readiness import ASSET_NAMES
from covenant_radar.demo.scenarios import SCENARIOS, scenario_events
from covenant_radar.scheduler.pipeline import PIPELINE_JOB_NAME
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.ingestion import SignalIngestionService
from covenant_radar.services.nightly_runtime import NightlyRuntime

_QUEUE_DEP = Depends(requires(Permission.VIEW_QUEUE))


def create_demo_walkthrough_router(
    session: Session, *, ingestion: SignalIngestionService, nightly: NightlyRuntime
) -> APIRouter:
    router = APIRouter(tags=["synthetic-demo"])

    @router.post("/demo/signal", name="demo_signal")
    async def inject_signal(
        request: Request,
        background_tasks: BackgroundTasks,
        principal: Principal = _QUEUE_DEP,
    ) -> RedirectResponse:
        if not request.app.state.settings.web.demo_walkthrough_enabled:
            raise HTTPException(status_code=404)
        # The demo launcher enables this route only for an isolated synthetic
        # database. Keep the trigger limited to the risk-head/admin authority
        # already trusted with model promotion, even inside that demo.
        if not principal.has(Permission.APPROVE_MODEL_PROMOTION):
            raise HTTPException(status_code=403)
        form = await request.form()
        reference = str(form.get("borrower_reference", ""))
        if not reference or len(reference) > 20:
            raise HTTPException(status_code=422, detail="Choose a borrower from the queue.")
        scope = resolve_scope(principal, session)
        borrower = BorrowerRepository(session).by_reference(reference, scope=scope)
        if borrower is None:
            raise HTTPException(status_code=404)
        facility = session.scalar(
            select(Facility)
            .where(Facility.borrower_id == borrower.id)
            .order_by(Facility.reference)
            .limit(1)
        )
        if facility is None:
            raise HTTPException(status_code=422, detail="This borrower has no facility to monitor.")
        scenario = str(form.get("scenario", "payment"))
        if scenario not in SCENARIOS:
            raise HTTPException(
                status_code=422, detail="Choose a supported demonstration scenario."
            )
        today = datetime.now(UTC).date()
        demo_actor = Principal.user(nightly.system_actor_id, (Permission.INGEST_DATA,))
        demo_scope = Scope(
            principal_id=nightly.system_actor_id,
            exact_paths=scope.exact_paths,
            descendant_paths=scope.descendant_paths,
        )
        events = scenario_events(
            scenario,
            borrower_id=borrower.id,
            facility_id=facility.id,
            today=today,
            actor_id=principal.human_id,
        )
        if scenario in {"payment", "combined"}:
            _record_late_conduct(session, facility, today, days_past_due=40, actor=demo_actor.id)
        report = ingestion.ingest(
            demo_actor,
            events,
            scope=demo_scope,
            request_id=getattr(request.state, "request_id", None),
            source_type="api",
            source_reference="synthetic-walkthrough",
        )
        if report.inserted:
            # The request middleware commits the ingestion transaction before
            # Starlette runs background tasks. The runner owns fresh sessions.
            background_tasks.add_task(nightly.runner.submit, PIPELINE_JOB_NAME, trigger="manual")
        return RedirectResponse(
            f"/borrowers/{borrower.reference}?walkthrough=received&scenario={scenario}",
            status_code=303,
        )

    @router.get("/demo/assets/{filename}", name="demo_asset")
    def download_asset(
        filename: str, request: Request, principal: Principal = _QUEUE_DEP
    ) -> FileResponse:
        settings = request.app.state.settings.web
        if not settings.demo_walkthrough_enabled or not principal.has(
            Permission.APPROVE_MODEL_PROMOTION
        ):
            raise HTTPException(status_code=404)
        directory = settings.demo_assets_path
        if directory is None or filename not in ASSET_NAMES or not (directory / filename).is_file():
            raise HTTPException(status_code=404)
        return FileResponse(directory / filename, filename=filename)

    return router


def _record_late_conduct(
    session: Session, facility: Facility, today: date, *, days_past_due: int, actor: UUID
) -> None:
    """Record today's facility conduct alongside the simulated late payment.

    The repayment-status (SMA) band is derived from facility conduct, not from
    signal events, so without this the queue would show the payment signal
    and still read "No overdues".  A second simulation the same day raises
    the same row rather than adding one (conduct is one row per facility per
    day).
    """

    now = datetime.now(UTC)
    row = session.scalar(
        select(FacilityConduct).where(
            FacilityConduct.facility_id == facility.id,
            FacilityConduct.as_of_date == today,
        )
    )
    if row is None:
        session.add(
            FacilityConduct(
                id=uuid4(),
                facility_id=facility.id,
                as_of_date=today,
                outstanding=facility.outstanding,
                utilisation_pct=None,
                days_past_due=days_past_due,
                overdue_amount=None,
                excess_amount=None,
                source_id=None,
                created_at=now,
                updated_at=now,
                created_by_id=actor,
                updated_by_id=actor,
                request_id=new_request_id(),
            )
        )
    else:
        row.days_past_due = max(row.days_past_due or 0, days_past_due)
        row.updated_at = now
        row.updated_by_id = actor
    session.flush()


__all__ = ["create_demo_walkthrough_router"]
