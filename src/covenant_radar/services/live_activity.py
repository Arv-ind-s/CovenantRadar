"""Disclosure-safe activity envelopes for the progressive live workspace."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from covenant_radar.db.models.borrower import Borrower
from covenant_radar.db.models.operations import JobRun
from covenant_radar.db.models.portfolio import Portfolio
from covenant_radar.db.models.signal import SignalEvent
from covenant_radar.db.scoping import resolve_scope
from covenant_radar.db.session import is_database_session
from covenant_radar.notifications.inapp import InAppNotificationService
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal

_MAX_ITEMS: Final[int] = 20


@dataclass(frozen=True, slots=True)
class LiveActivityItem:
    """A browser-safe event.  It deliberately contains no raw audit payload."""

    id: str
    timestamp: str
    severity: str
    category: str
    title: str
    body: str
    grouping_key: str
    deep_link: str | None
    affected_regions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LiveUpdateEnvelope:
    version: int
    server_time: str
    cursor: str
    items: tuple[LiveActivityItem, ...]
    freshness: str = "connected"

    def as_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["items"] = [asdict(item) for item in self.items]
        return value


class LiveActivityService:
    """Read a compact, permission-scoped activity feed from durable records."""

    def __init__(
        self,
        session: Session,
        notifications: InAppNotificationService,
        *,
        cursor_secret: bytes,
    ) -> None:
        if not isinstance(cursor_secret, bytes) or len(cursor_secret) < 32:
            raise ValueError("LiveActivityService cursor_secret must be at least 32 bytes.")
        self.session = session
        self.notifications = notifications
        self.cursor_secret = cursor_secret

    def updates(self, principal: Principal, *, cursor: str | None) -> LiveUpdateEnvelope:
        since = self._decode_cursor(cursor, principal)
        items = list(self._notification_items(principal, since))
        items.extend(self._job_items(principal, since))
        if is_database_session(self.session):
            items.extend(self._signal_items(principal, since))
        items.sort(key=lambda item: (item.timestamp, item.id), reverse=True)
        items = items[:_MAX_ITEMS]
        newest = max(
            (item.timestamp for item in items),
            default=since.isoformat() if since is not None else self._now(),
        )
        return LiveUpdateEnvelope(
            version=1,
            server_time=self._now(),
            cursor=self._encode_cursor(principal, newest),
            items=tuple(items),
        )

    def _notification_items(
        self, principal: Principal, since: datetime | None
    ) -> tuple[LiveActivityItem, ...]:
        # This view service is the disclosure boundary: it renders generic
        # content when a recipient has lost access, and no raw payload leaks
        # into the live stream.
        page = self.notifications.list_notifications(principal, page=1, page_size=_MAX_ITEMS)
        rows: list[LiveActivityItem] = []
        for item in page.items:
            if since is not None and item.created_at <= since:
                continue
            severity = (
                "critical"
                if item.template in {"system_failure", "job_failure", "sla_breach"}
                else "attention"
            )
            regions = (
                ("queue-ledger", "queue-summary")
                if item.template == "band_change"
                else ("notification-results",)
            )
            rows.append(
                LiveActivityItem(
                    id=f"notification:{item.id}",
                    timestamp=item.created_at.astimezone(UTC).isoformat(),
                    severity=severity,
                    category="notification",
                    title=item.title,
                    body=item.body,
                    grouping_key=f"notification:{item.template}",
                    deep_link=item.deep_link,
                    affected_regions=regions,
                )
            )
        return tuple(rows)

    def _signal_items(
        self, principal: Principal, since: datetime | None
    ) -> tuple[LiveActivityItem, ...]:
        scope = resolve_scope(principal, self.session)
        if scope.is_empty:
            return ()
        statement = (
            select(SignalEvent, Borrower)
            .join(Borrower, Borrower.id == SignalEvent.borrower_id)
            .join(Portfolio, Portfolio.id == Borrower.portfolio_id)
            .where(scope.predicate(Portfolio.path))
            .order_by(SignalEvent.ingested_at.desc())
            .limit(_MAX_ITEMS)
        )
        rows: list[LiveActivityItem] = []
        for event, borrower in self.session.execute(statement):
            if since is not None and event.ingested_at <= since:
                continue
            synthetic = bool(
                event.payload.get("synthetic_walkthrough") or event.payload.get("demo_version")
            )
            rows.append(
                LiveActivityItem(
                    id=f"signal:{event.id}",
                    timestamp=event.ingested_at.astimezone(UTC).isoformat(),
                    severity="attention" if event.family == "payment" else "info",
                    category="signal",
                    title=f"{borrower.legal_name}: {event.family.replace('_', ' ')} received",
                    body=("Synthetic signal received; awaiting scoring." if synthetic
                          else "New source observation received; review the stored evidence."),
                    grouping_key=f"signal:{borrower.id}:{event.family}",
                    deep_link=f"/borrowers/{borrower.reference}#case-signals",
                    affected_regions=(f"queue-row-{borrower.id}", "queue-summary"),
                )
            )
        return tuple(rows)

    def _job_items(
        self, principal: Principal, since: datetime | None
    ) -> tuple[LiveActivityItem, ...]:
        if not principal.has(Permission.MANAGE_JOBS):
            return ()
        statement = (
            select(JobRun)
            .where(JobRun.finished_at.is_not(None))
            .order_by(JobRun.finished_at.desc())
            .limit(_MAX_ITEMS)
        )
        rows: list[LiveActivityItem] = []
        for run in self.session.scalars(statement):
            if run.finished_at is None or (since is not None and run.finished_at <= since):
                continue
            failed = run.state.lower() in {"failed", "dead_lettered"}
            rows.append(
                LiveActivityItem(
                    id=f"job:{run.id}",
                    timestamp=run.finished_at.astimezone(UTC).isoformat(),
                    severity="critical" if failed else "info",
                    category="operations",
                    title=f"{run.job_name} {'failed' if failed else 'completed'}",
                    body=(
                        "Review the operational run and retry guidance."
                        if failed
                        else "A scheduled operational run completed."
                    ),
                    grouping_key=f"job:{run.job_name}",
                    deep_link="/admin/jobs",
                    affected_regions=("admin-ops-region",),
                )
            )
        return tuple(rows)

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def _encode_cursor(self, principal: Principal, timestamp: str) -> str:
        payload = json.dumps(
            {"u": str(principal.id), "t": timestamp}, separators=(",", ":")
        ).encode()
        signature = hmac.new(self.cursor_secret, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload + b"." + signature).decode().rstrip("=")

    def _decode_cursor(self, value: str | None, principal: Principal) -> datetime | None:
        if not value:
            return None
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            # A binary SHA-256 signature can itself contain the separator.
            # Locate it by the fixed digest length, never by splitting bytes.
            digest_size = hashlib.sha256().digest_size
            if len(raw) <= digest_size or raw[-digest_size - 1 : -digest_size] != b".":
                return None
            payload, signature = raw[: -digest_size - 1], raw[-digest_size:]
            expected = hmac.new(self.cursor_secret, payload, hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                return None
            decoded = json.loads(payload)
            if decoded.get("u") != str(principal.id):
                return None
            return datetime.fromisoformat(str(decoded["t"])).astimezone(UTC)
        except (ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None


__all__ = ["LiveActivityItem", "LiveActivityService", "LiveUpdateEnvelope"]
