"""Explicitly synthetic, disabled-by-default monitoring walkthrough."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.api.deps import requires
from covenant_radar.db.models.facility import Facility
from covenant_radar.db.repositories.borrower import BorrowerRepository
from covenant_radar.db.scoping import Scope, resolve_scope
from covenant_radar.domain.signals import SignalEvent
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
            select(Facility).where(Facility.borrower_id == borrower.id)
            .order_by(Facility.reference).limit(1)
        )
        if facility is None:
            raise HTTPException(status_code=422, detail="This borrower has no facility to monitor.")
        today = datetime.now(UTC).date()
        walkthrough_id = str(uuid4())
        demo_actor = Principal.user(nightly.system_actor_id, (Permission.INGEST_DATA,))
        demo_scope = Scope(
            principal_id=nightly.system_actor_id,
            exact_paths=scope.exact_paths,
            descendant_paths=scope.descendant_paths,
        )
        events = (
            SignalEvent(
                borrower_id=borrower.id,
                facility_id=facility.id,
                event_date=today - timedelta(days=2 - offset),
                family="payment",
                event_type="payment_delay",
                magnitude=Decimal(days),
                unit="days",
                payload={
                    "days_past_due": days,
                    "is_adverse": True,
                    "synthetic_walkthrough": True,
                    "walkthrough_id": walkthrough_id,
                    "triggered_by": str(principal.human_id),
                },
            )
            for offset, days in enumerate((30, 35, 40))
        )
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
            f"/borrowers/{borrower.reference}?walkthrough=received", status_code=303
        )

    return router


__all__ = ["create_demo_walkthrough_router"]
