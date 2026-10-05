"""Market pressure: observed public market moves set against each borrower's cushion."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.api.deps import requires
from covenant_radar.core.errors import NotFound
from covenant_radar.db.models.reference import IndustryReference
from covenant_radar.db.repositories.borrower import BorrowerRepository
from covenant_radar.db.repositories.case import CaseRepository
from covenant_radar.db.scoping import resolve_scope
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.borrower_market import assess, load_positions
from covenant_radar.services.market_intelligence import (
    DRIVERS_BY_KEY,
    MarketIntelligenceService,
)
from covenant_radar.web.preferences import theme_for_request
from covenant_radar.web.view_models.market import (
    borrower_row,
    briefing_json,
    build_briefing,
    feed_view,
    review_draft,
    workspace_version,
)

_READ = Depends(requires(Permission.VIEW_QUEUE))
_REVIEW = Depends(requires(Permission.UPDATE_CASE))


def create_intelligence_router(
    session: Session,
    *,
    service: MarketIntelligenceService,
) -> APIRouter:
    router = APIRouter(tags=["market-intelligence"])

    def render(request: Request, principal: Principal, template: str, **context: object) -> str:
        return request.app.state.templates.get_template(template).render(
            request=request,
            principal=principal,
            locale="en",
            theme=theme_for_request(request),
            text_direction="ltr",
            **context,
        )

    @router.get("/intelligence", response_class=HTMLResponse, name="market_intelligence")
    def index(
        request: Request,
        driver: str = Query("", max_length=20),
        v: str = Query("", max_length=64),
        principal: Principal = _READ,
    ) -> Response:
        snapshot = service.snapshot()
        partial = request.headers.get("HX-Request") == "true"
        # An open page re-checks every minute; until a series gains an
        # observation (or the redraw window turns) there is nothing new to
        # draw, so nothing is swapped and no borrower is reassessed. Headlines
        # update separately via the feed.
        if partial and v and snapshot["loaded"] and v == workspace_version(snapshot):
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        view = build_briefing(session, principal, snapshot, driver=driver)
        template = (
            "screens/intelligence/_workspace.html" if partial else "screens/intelligence/index.html"
        )
        return HTMLResponse(
            render(request, principal, template, view=view),
            headers={"Cache-Control": "no-store", "Vary": "HX-Request"},
        )

    @router.post("/intelligence/refresh", name="refresh_market_intelligence")
    def refresh(principal: Principal = _READ) -> RedirectResponse:
        del principal
        service.snapshot(force=True)
        return RedirectResponse("/intelligence", status_code=303)

    @router.get("/intelligence/feed", name="market_intelligence_feed")
    def feed(
        drivers: str = Query("", max_length=300),
        principal: Principal = _READ,
    ) -> JSONResponse:
        """The live headline stream. Public market news only; no borrower data."""
        del principal
        keys = [key for key in drivers.split(",") if key in DRIVERS_BY_KEY]
        return JSONResponse(
            feed_view(service.snapshot(), keys), headers={"Cache-Control": "no-store"}
        )

    @router.get("/intelligence/data", name="market_intelligence_data")
    def data(
        driver: str = Query("", max_length=20),
        principal: Principal = _READ,
    ) -> JSONResponse:
        view = build_briefing(session, principal, service.snapshot(), driver=driver)
        return JSONResponse(briefing_json(view), headers={"Cache-Control": "no-store"})

    @router.get("/intelligence/review/{reference}", response_class=HTMLResponse)
    def review(
        request: Request,
        reference: str,
        case: str = Query("", max_length=40),
        principal: Principal = _REVIEW,
    ) -> HTMLResponse:
        scope = resolve_scope(principal, session)
        borrower = BorrowerRepository(session).by_reference(reference, scope=scope)
        if borrower is None:
            raise NotFound("No borrower with that reference in your scope.")
        snapshot = service.snapshot()
        position = load_positions(session, [borrower.id])[borrower.id]
        assessment = assess(position, snapshot)
        cases = CaseRepository(session).open_cases_for_borrower(borrower.id, scope=scope)
        selected = next((row for row in cases if row.reference == case), None)
        if case and selected is None:
            raise NotFound("No open case with that reference for this borrower.")
        if selected is None and len(cases) == 1:
            selected = cases[0]
        sector = session.scalar(
            select(IndustryReference.name).where(IndustryReference.code == borrower.industry_code)
        )
        html = render(
            request,
            principal,
            "screens/intelligence/review.html",
            row=borrower_row(assessment, {borrower.industry_code or "": sector or "Unclassified"}),
            cases=cases,
            selected=selected,
            draft=review_draft(assessment, snapshot["checked_at"]),
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    return router
