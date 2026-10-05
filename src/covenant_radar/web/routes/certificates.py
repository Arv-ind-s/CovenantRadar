"""Browser screen and form actions for the compliance certificate
workflow (`T-039`, `spec §R-09`).

The nightly test step raises and chases the requests; this screen is where a
credit officer records what came back. Receiving takes the certificate file
itself (stored through `DocumentService` as a `compliance_certificate`) or a
document already on file for the borrower.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.datastructures import UploadFile

from covenant_radar.api.deps import requires
from covenant_radar.core.errors import DomainError, ValidationError
from covenant_radar.documents.extract_native import NativePdfExtractionError
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.certificates import CertificateRequestSummary, CertificateService
from covenant_radar.services.documents import DocumentService
from covenant_radar.web.preferences import theme_for_request

_TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates"
_MAX_FORM_BYTES = 16 * 1024
_MAX_MULTIPART_FIELDS = 8
_CERTIFICATE_DOC_TYPE = "compliance_certificate"
_DOCUMENT_CHOICES = 20
_READ = requires(Permission.VIEW_COVENANT)
_RECEIVE = requires(Permission.UPLOAD_DOCUMENT)
_REVIEW = requires(Permission.RECORD_WAIVER)
_READ_DEP = Depends(_READ)
_RECEIVE_DEP = Depends(_RECEIVE)
_REVIEW_DEP = Depends(_REVIEW)

_LABELS = {
    "title": "Certificates",
    "heading": "Compliance certificate requests",
    "intro": (
        "Requests are raised by the nightly review ahead of each certificate due date. "
        "One still not received after the grace period is marked overdue and becomes "
        "evidence on the borrower's case file."
    ),
    "open_heading": "Open requests",
    "empty": "No open certificate requests are in this scope.",
    "settled_heading": "Settled in the last 90 days",
    "settled_empty": "No certificate request was accepted or rejected in the last 90 days.",
    "borrower": "Borrower",
    "covenants": "Covenants",
    "due_date": "Due date",
    "state": "Status",
    "requested_at": "Requested",
    "received_at": "Received",
    "certificate": "Certificate",
    "outcome": "Outcome",
    "actions": "Actions",
    "receive": "Receive",
    "receive_heading": "Record the certificate",
    "accept": "Accept",
    "reject": "Reject",
    "reject_heading": "Reject",
    "file": "Certificate file",
    "file_hint": "PDF, image or text, as received from the borrower or their CA.",
    "document_id": "Or a document already on file",
    "document_none": "Choose a document",
    "reason": "Reason",
    "not_received": "Not received",
    "requested": "Requested",
    "received": "Received",
    "under_review": "Under review",
    "accepted": "Accepted",
    "rejected": "Rejected",
    "overdue": "Overdue",
}


def create_certificates_router(
    service: CertificateService,
    *,
    documents: DocumentService | None = None,
    template_directory: Path | str = _TEMPLATE_ROOT,
) -> APIRouter:
    """Build the protected certificate list and review-action screens."""
    if not isinstance(service, CertificateService):
        raise TypeError("create_certificates_router requires a CertificateService.")
    if documents is not None and not isinstance(documents, DocumentService):
        raise TypeError("create_certificates_router documents must be a DocumentService.")
    router = APIRouter(tags=["certificates-web"])
    fallback_environment = Environment(
        loader=FileSystemLoader(str(template_directory)),
        autoescape=select_autoescape(("html", "xml")),
    )

    def render(
        request: Request, principal: Principal, *, error: str = "", status_code: int = 200
    ) -> HTMLResponse:
        today = service.clock.now().date()
        open_rows = service.summaries(principal, service.list_open(principal))
        settled_rows = service.summaries(principal, service.recently_settled(principal))
        choices: dict[UUID, list[dict[str, str]]] = {}
        if (
            documents is not None
            and principal.has(Permission.UPLOAD_DOCUMENT)
            and principal.has(Permission.VIEW_DOCUMENT)
        ):
            for borrower_id in {row.request.borrower_id for row in open_rows}:
                choices[borrower_id] = [
                    {"value": str(document.id), "label": document.filename}
                    for document in documents.list_documents(
                        principal, borrower_id, limit=_DOCUMENT_CHOICES
                    )
                ]
        return _render(
            request,
            fallback_environment,
            principal=principal,
            open_rows=[
                _row(row, today, choices.get(row.request.borrower_id, [])) for row in open_rows
            ],
            settled_rows=[_row(row, today, []) for row in settled_rows],
            uploads_enabled=documents is not None,
            error=error,
            status_code=status_code,
        )

    @router.get("/certificates", response_class=HTMLResponse, name="certificate_list")
    async def certificate_list(request: Request, principal: Principal = _READ_DEP) -> HTMLResponse:
        return render(request, principal)

    @router.post(
        "/certificates/{request_id}/receive",
        response_class=HTMLResponse,
        name="certificate_receive_submit",
    )
    async def certificate_receive_submit(
        request: Request,
        request_id: UUID,
        principal: Principal = _RECEIVE_DEP,
    ) -> Response:
        try:
            values, upload = await _receive_form(request, documents)
            document_id = _optional_uuid(values.get("document_id"), "document_id")
            if upload is not None:
                if documents is None:
                    raise ValidationError(
                        "Certificate uploads are not configured on this server.", field="file"
                    )
                summary = service.summaries(principal, (service.get(principal, request_id),))[0]
                document = documents.upload_file(
                    principal,
                    borrower_ref=summary.borrower_reference,
                    doc_type=_CERTIFICATE_DOC_TYPE,
                    upload=upload,
                )
                if document.mime_type.lower() == "application/pdf":
                    try:
                        documents.extract_document(principal, document.id)
                    except NativePdfExtractionError:
                        # A scanned or locked PDF is still the certificate the
                        # borrower sent; it is received, just without text.
                        pass
                document_id = document.id
            if document_id is None:
                raise ValidationError(
                    "Upload the certificate, or choose a document already on file.",
                    field="file",
                )
            service.receive(principal, request_id, document_id=document_id)
        except DomainError as error:
            return render(request, principal, error=error.message, status_code=422)
        return RedirectResponse("/certificates", status_code=303)

    @router.post(
        "/certificates/{request_id}/accept",
        response_class=HTMLResponse,
        name="certificate_accept_submit",
    )
    async def certificate_accept_submit(
        request: Request,
        request_id: UUID,
        principal: Principal = _REVIEW_DEP,
    ) -> Response:
        try:
            service.accept(principal, request_id)
        except DomainError as error:
            return render(request, principal, error=error.message, status_code=422)
        return RedirectResponse("/certificates", status_code=303)

    @router.post(
        "/certificates/{request_id}/reject",
        response_class=HTMLResponse,
        name="certificate_reject_submit",
    )
    async def certificate_reject_submit(
        request: Request,
        request_id: UUID,
        principal: Principal = _REVIEW_DEP,
    ) -> Response:
        try:
            values = await _form_values(request)
            service.reject(principal, request_id, reason=values.get("reason", ""))
        except DomainError as error:
            return render(request, principal, error=error.message, status_code=422)
        return RedirectResponse("/certificates", status_code=303)

    return router


def _render(
    request: Request,
    fallback_environment: Environment,
    *,
    principal: Principal,
    open_rows: Sequence[dict[str, object]],
    settled_rows: Sequence[dict[str, object]],
    uploads_enabled: bool,
    error: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    environment = getattr(request.app.state, "template_env", fallback_environment)
    template = environment.get_template("screens/certificates/index.html")
    values = {
        "request": request,
        "principal": principal,
        "locale": "en",
        "theme": theme_for_request(request),
        "text_direction": "ltr",
        "labels": _LABELS,
        "csrf_token": getattr(request.state, "csrf_token", ""),
        "open_rows": open_rows,
        "settled_rows": settled_rows,
        "uploads_enabled": uploads_enabled,
        "error": error,
    }
    return HTMLResponse(template.render(**values), status_code=status_code)


def _row(
    summary: CertificateRequestSummary, today: date, documents: Sequence[dict[str, str]]
) -> dict[str, object]:
    row = summary.request
    return {
        "id": str(row.id),
        "state": row.state,
        "state_label": _LABELS.get(row.state, row.state),
        "timing": _timing(row.state, row.due_date, today),
        "borrower_reference": summary.borrower_reference,
        "borrower_name": summary.borrower_name,
        "covenants": ", ".join(summary.covenant_references) or "—",
        "due_date": f"{row.due_date:%d %b %Y}",
        "requested_at": f"{row.requested_at:%d %b %Y}" if row.requested_at else "",
        "received_at": f"{row.received_at:%d %b %Y}" if row.received_at else "",
        "document_id": str(row.document_id) if row.document_id else "",
        "document_filename": summary.document_filename or "",
        "rejection_reason": row.rejection_reason or "",
        "documents": [{"value": "", "label": _LABELS["document_none"]}, *documents],
    }


def _timing(state: str, due_date: date, today: date) -> str:
    """How the due date stands today, for a request still awaiting the certificate."""
    if state not in ("requested", "overdue"):
        return ""
    days = (due_date - today).days
    if days > 0:
        return f"due in {days} day{'' if days == 1 else 's'}"
    if days == 0:
        return "due today"
    return f"{-days} day{'' if days == -1 else 's'} past due"


async def _receive_form(
    request: Request, documents: DocumentService | None
) -> tuple[dict[str, str], UploadFile | None]:
    """Read the receive form: a multipart upload with an optional file, or a
    plain form naming a document already on file."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type != "multipart/form-data":
        return await _form_values(request), None
    max_part_size = (
        documents.scans.guard.policy.max_bytes + 64 * 1024
        if documents is not None
        else _MAX_FORM_BYTES
    )
    form = await request.form(
        max_files=1, max_fields=_MAX_MULTIPART_FIELDS, max_part_size=max_part_size
    )
    values = {
        key: value
        for key, value in form.multi_items()
        if key != "csrf_token" and isinstance(value, str)
    }
    upload = form.get("file")
    # A browser submits an empty, unnamed file part when no file was chosen.
    if isinstance(upload, UploadFile) and upload.filename:
        return values, upload
    return values, None


async def _form_values(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > _MAX_FORM_BYTES:
        raise ValidationError("The submitted form is too large.", field="form")
    content_type = request.headers.get("content-type", "").split(";", 1)[0].lower()
    if content_type == "application/x-www-form-urlencoded" or not content_type:
        try:
            decoded = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValidationError("The submitted form is not valid UTF-8.", field="form") from error
        parsed = parse_qs(decoded, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items() if values and key != "csrf_token"}
    form = await request.form()
    values: dict[str, str] = {}
    for key, value in form.multi_items():
        if key != "csrf_token" and isinstance(value, str):
            values[key] = value
    return values


def _optional_uuid(value: object, field: str) -> UUID | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return UUID(value.strip())
    except ValueError as error:
        raise ValidationError(f"{field} must be a UUID.", field=field) from error


__all__ = ["create_certificates_router"]
