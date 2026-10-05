"""Borrower remediation planner screen (``/borrowers/{reference}/remediation``).

Thin by design: it resolves the borrower in the caller's scope, asks
``RemediationService`` for the report, and lets the reader re-size the
package through ``size.<LEVER-CODE>`` query parameters.  The browser chooses
sizes only; the levers, their capacity and their effects stay bank-owned
code, and every requested size is clamped to the borrower's capacity in the
domain.  A GET with sizes is idempotent and bookmarkable — the same filings
and sizes always produce the same figures.

The one write is ``POST .../remediation/record``: it records the package on
screen against the forecast the next memo is written about, so the memo can
cite the borrower's sized steps instead of the generic catalogue.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Final, cast
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy.orm import Session

from covenant_radar.api.deps import requires
from covenant_radar.audit.record import AuditRecorder
from covenant_radar.core.errors import NotFound, ValidationError
from covenant_radar.db.repositories.audit import AuditRepository
from covenant_radar.db.scoping import resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.remediation import AuditWriter, RemediationService
from covenant_radar.web.preferences import theme_for_request
from covenant_radar.web.view_models.remediation import (
    SIZE_PREFIX,
    build_remediation_view,
    parse_sizes,
)

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_RUN = requires(Permission.RUN_SIMULATION)
_RUN_DEP = Depends(_RUN)
_MAX_FORM_BYTES: Final[int] = 8_192
_FROM_QUERY: Final[object] = object()


def create_remediation_router(
    session: Session,
    *,
    template_directory: Path | str = _TEMPLATE_ROOT,
) -> APIRouter:
    if not is_database_session(session):
        raise TypeError("create_remediation_router requires a SQLAlchemy Session.")
    service = RemediationService(session)
    router = APIRouter(tags=["remediation-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    def _view(
        request: Request,
        reference: str,
        principal: Principal,
        *,
        requested: object = _FROM_QUERY,
        with_memo: bool = True,
        record_error: str | None = None,
    ) -> object | None:
        """The screen view, or ``None`` for an in-scope borrower with no quarterly filings.

        Out of scope and unknown are the same answer — not found — so a
        reference outside the caller's portfolios reveals nothing.
        """

        scope = resolve_scope(principal, session)
        report = service.report(reference, scope=scope)
        if report is None:
            if service.borrower(reference, scope=scope) is None:
                raise NotFound(f"Borrower {reference!r} was not found within the current scope.")
            return None
        sizes_requested = (
            parse_sizes(list(request.query_params.multi_items()))
            if requested is _FROM_QUERY
            else cast(dict[str, float] | None, requested)
        )
        sizes = report.plan_sizes if sizes_requested is None else sizes_requested
        package = service.custom(report, sizes)
        recommended = sizes_requested is None or _same_sizes(package.sizes, report.plan_sizes)
        return build_remediation_view(
            report,
            package,
            is_recommended=recommended,
            attachment=service.memo_attachment(reference, scope=scope) if with_memo else None,
            record_error=record_error,
        )

    @router.get(
        "/borrowers/{reference}/remediation",
        response_class=HTMLResponse,
        name="borrower_remediation",
    )
    def remediation(
        request: Request,
        reference: str,
        principal: Principal = _RUN_DEP,
    ) -> HTMLResponse:
        view = _view(request, reference, principal)
        if view is None:
            borrower = service.borrower(reference, scope=resolve_scope(principal, session))
            return _render(
                request,
                fallback_environment,
                "screens/remediation/empty.html",
                principal,
                None,
                borrower_reference=reference,
                borrower_name=borrower.legal_name if borrower is not None else reference,
            )
        return _render(
            request, fallback_environment, "screens/remediation/index.html", principal, view
        )

    @router.get(
        "/borrowers/{reference}/remediation/summary",
        response_class=HTMLResponse,
        name="borrower_remediation_summary",
    )
    def remediation_summary(
        request: Request,
        reference: str,
        covenant: str = "",
        principal: Principal = _RUN_DEP,
    ) -> HTMLResponse:
        view = _view(request, reference, principal, with_memo=False)
        if view is None:
            return _render(
                request,
                fallback_environment,
                "screens/remediation/_summary_empty.html",
                principal,
                None,
            )
        return _render(
            request,
            fallback_environment,
            "screens/remediation/_summary.html",
            principal,
            view,
            covenant_reference=covenant,
        )

    @router.post(
        "/borrowers/{reference}/remediation/record",
        response_class=HTMLResponse,
        name="borrower_remediation_record",
    )
    async def record_package(
        request: Request,
        reference: str,
        principal: Principal = _RUN_DEP,
    ) -> Response:
        """Record the package on screen for the memo, then show the planner again.

        The form carries sizes only when the reader changed the package, so
        an unchanged form records the recommendation as the planner sized
        it.  A refusal re-renders the planner with the reason beside the
        button; nothing is written.
        """

        body = await request.body()
        if len(body) > _MAX_FORM_BYTES:
            raise ValidationError("The submitted package is too large.", field="package")
        try:
            pairs = parse_qsl(body.decode("utf-8"), keep_blank_values=True)
        except UnicodeDecodeError as error:
            raise ValidationError(
                "The submitted package is not valid UTF-8.", field="package"
            ) from error
        sizes = parse_sizes(pairs)
        scope = resolve_scope(principal, session)
        try:
            with session.begin_nested():
                service.record(
                    reference,
                    scope=scope,
                    sizes=sizes,
                    audit=_audit_writer(request, session),
                    actor_id=None if principal.is_api_key else principal.id,
                    request_id=_request_id(request),
                )
        except ValidationError as error:
            view = _view(request, reference, principal, requested=sizes, record_error=error.message)
            if view is None:
                raise
            return _render(
                request,
                fallback_environment,
                "screens/remediation/index.html",
                principal,
                view,
                status_code=422,
            )
        query = (
            ""
            if sizes is None
            else "?" + urlencode({f"{SIZE_PREFIX}{code}": value for code, value in sizes.items()})
        )
        return RedirectResponse(
            f"/borrowers/{reference}/remediation{query}#remedy-memo", status_code=303
        )

    return router


def _audit_writer(request: Request, session: Session) -> AuditWriter:
    configured = getattr(request.app.state, "audit_writer", None)
    if configured is not None and callable(getattr(configured, "record", None)):
        return cast(AuditWriter, configured)
    return cast(AuditWriter, AuditRecorder(AuditRepository(session)))


def _request_id(request: Request) -> str:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) and value.strip() else "web-remediation"


def _same_sizes(first: Mapping[str, float], second: Mapping[str, float]) -> bool:
    keys = set(first) | set(second)
    return all(abs(first.get(key, 0.0) - second.get(key, 0.0)) < 1e-6 for key in keys)


def _render(
    request: Request,
    fallback_environment: Environment,
    template_name: str,
    principal: Principal,
    view: object,
    *,
    status_code: int = 200,
    **context: object,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template(template_name)
    response = HTMLResponse(
        status_code=status_code,
        content=template.render(
            request=request,
            principal=principal,
            locale="en",
            theme=theme_for_request(request),
            text_direction="ltr",
            csrf_token=getattr(request.state, "csrf_token", ""),
            view=view,
            **context,
        ),
    )
    response.headers["Vary"] = "HX-Request"
    return response


__all__ = ["create_remediation_router"]
