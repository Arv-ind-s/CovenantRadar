"""The monitoring UI reads durable records inside the caller's scope."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fastapi import BackgroundTasks, HTTPException, Request

from covenant_radar.db.models.signal import SignalEvent
from covenant_radar.db.scoping import resolve_scope
from covenant_radar.notifications.inapp import InAppNotificationPage
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.live_activity import LiveActivityService
from covenant_radar.services.monitoring_pulse import (
    borrower_risk_comparison,
    monitoring_status,
    recent_changes,
)
from covenant_radar.web.routes.demo_walkthrough import create_demo_walkthrough_router
from tests.integration.test_queue_screen import _Fixture

_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


def _signal(fixture: _Fixture, borrower_id, *, ingested_at: datetime, number: int) -> None:
    fixture.session.add(
        SignalEvent(
            id=uuid4(),
            borrower_id=borrower_id,
            event_date=date(2026, 9, 29),
            family="payment",
            event_type="payment_delay",
            magnitude=Decimal(number),
            unit="days",
            payload={"days_past_due": number, "synthetic_walkthrough": True},
            content_hash=f"{number:064x}",
            is_late=False,
            ingested_at=ingested_at,
            created_at=ingested_at,
            updated_at=ingested_at,
            request_id=f"test-signal-{number}",
        )
    )
    fixture.session.flush()


def test_change_feed_is_scoped_and_compares_completed_runs() -> None:
    fixture = _Fixture()
    try:
        allowed = fixture.portfolio("MONITORED")
        other = fixture.portfolio("OTHER")
        fixture.grant_scope(allowed)
        borrower = fixture.borrower(allowed, "B-MONITORED")
        outside = fixture.borrower(other, "B-OTHER")
        previous = fixture.run(date(2026, 9, 28))
        previous.started_at = _NOW - timedelta(days=1, hours=1)
        previous.finished_at = _NOW - timedelta(days=1)
        current = fixture.run(date(2026, 9, 29))
        current.started_at = _NOW - timedelta(minutes=10)
        current.finished_at = _NOW
        fixture.entry(previous, borrower, 1, band="watch", probability=Decimal("0.20"))
        fixture.entry(current, borrower, 1, band="act", probability=Decimal("0.90"))
        fixture.entry(current, outside, 2, band="watch", probability=Decimal("0.10"))
        _signal(fixture, borrower.id, ingested_at=_NOW - timedelta(minutes=30), number=40)
        _signal(fixture, outside.id, ingested_at=_NOW - timedelta(minutes=5), number=41)
        fixture.session.flush()

        scope = resolve_scope(fixture.principal, fixture.session)
        changes = recent_changes(fixture.session, scope)
        comparison = borrower_risk_comparison(fixture.session, borrower.id)
        status = monitoring_status(fixture.session, scope)

        assert len(changes) == 1
        assert changes[0]["reference"] == "B-MONITORED"
        assert changes[0]["status"] == "Scored"
        assert changes[0]["observation"] == "40 days"
        assert comparison["previous"]["score"] == "20%"
        assert comparison["current"]["score"] == "90%"
        assert status["processed"] == 1
    finally:
        fixture.close()


def test_live_signal_targets_the_scoped_queue_row() -> None:
    fixture = _Fixture()
    try:
        allowed = fixture.portfolio("LIVE")
        outside = fixture.portfolio("PRIVATE")
        fixture.grant_scope(allowed)
        borrower = fixture.borrower(allowed, "B-LIVE")
        private = fixture.borrower(outside, "B-PRIVATE")
        _signal(fixture, borrower.id, ingested_at=_NOW, number=31)
        _signal(fixture, private.id, ingested_at=_NOW, number=32)

        class EmptyNotifications:
            def list_notifications(
                self, *_args: object, **_kwargs: object
            ) -> InAppNotificationPage:
                return InAppNotificationPage((), 0, 0, 1, 20, "all")

        service = LiveActivityService(
            fixture.session, EmptyNotifications(), cursor_secret=b"l" * 32
        )
        items = service.updates(fixture.principal, cursor=None).items
        signals = [item for item in items if item.category == "signal"]
        assert len(signals) == 1
        assert signals[0].deep_link == "/borrowers/B-LIVE#case-signals"
        assert f"queue-row-{borrower.id}" in signals[0].affected_regions
    finally:
        fixture.close()


def test_historical_rerun_does_not_claim_a_future_signal_was_scored() -> None:
    fixture = _Fixture()
    try:
        portfolio = fixture.portfolio("HISTORY")
        fixture.grant_scope(portfolio)
        borrower = fixture.borrower(portfolio, "B-HISTORY")
        historical = fixture.run(date(2026, 9, 28))
        historical.started_at = _NOW + timedelta(minutes=5)
        historical.finished_at = _NOW + timedelta(minutes=10)
        fixture.entry(historical, borrower, 1)
        _signal(fixture, borrower.id, ingested_at=_NOW, number=42)
        fixture.session.flush()

        changes = recent_changes(fixture.session, resolve_scope(fixture.principal, fixture.session))
        assert changes[0]["status"] == "Awaiting next scan"
        assert changes[0]["band"] is None
    finally:
        fixture.close()


def _demo_request(*, enabled: bool, borrower_reference: str) -> Request:
    body = urlencode({"borrower_reference": borrower_reference}).encode()

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    app = SimpleNamespace(
        state=SimpleNamespace(
            settings=SimpleNamespace(web=SimpleNamespace(demo_walkthrough_enabled=enabled))
        )
    )
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/demo/signal",
            "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
            "app": app,
        },
        receive,
    )


def test_demo_trigger_is_disabled_and_privileged_even_with_queue_access() -> None:
    fixture = _Fixture()
    try:
        actor_id = uuid4()
        fake_nightly = SimpleNamespace(system_actor_id=actor_id, runner=SimpleNamespace())
        router = create_demo_walkthrough_router(
            fixture.session, ingestion=SimpleNamespace(), nightly=fake_nightly
        )
        handler = router.routes[0].endpoint
        with pytest.raises(HTTPException) as disabled:
            asyncio.run(
                handler(
                    _demo_request(enabled=False, borrower_reference="B-1"),
                    BackgroundTasks(),
                    fixture.principal,
                )
            )
        assert disabled.value.status_code == 404
        with pytest.raises(HTTPException) as forbidden:
            asyncio.run(
                handler(
                    _demo_request(enabled=True, borrower_reference="B-1"),
                    BackgroundTasks(),
                    fixture.principal,
                )
            )
        assert forbidden.value.status_code == 403
    finally:
        fixture.close()


def test_demo_trigger_rejects_out_of_scope_and_ingests_only_scoped_borrower() -> None:
    fixture = _Fixture()
    try:
        permitted = fixture.portfolio("DEMO")
        private = fixture.portfolio("PRIVATE-DEMO")
        fixture.grant_scope(permitted)
        borrower = fixture.borrower(permitted, "B-DEMO")
        outside = fixture.borrower(private, "B-PRIVATE-DEMO")
        fixture.covenant_version(borrower, "CV-DEMO")
        fixture.principal = Principal.user(
            fixture.principal.id,
            (Permission.VIEW_QUEUE, Permission.APPROVE_MODEL_PROMOTION),
        )
        captured: dict[str, object] = {}

        class FakeIngestion:
            def ingest(self, actor: Principal, events: object, **kwargs: object) -> object:
                captured.update(actor=actor, events=tuple(events), **kwargs)
                return SimpleNamespace(inserted=3)

        fake_nightly = SimpleNamespace(
            system_actor_id=uuid4(), runner=SimpleNamespace(submit=lambda *_a, **_k: None)
        )
        router = create_demo_walkthrough_router(
            fixture.session, ingestion=FakeIngestion(), nightly=fake_nightly
        )
        handler = router.routes[0].endpoint
        with pytest.raises(HTTPException) as denied:
            asyncio.run(
                handler(
                    _demo_request(enabled=True, borrower_reference=outside.reference),
                    BackgroundTasks(),
                    fixture.principal,
                )
            )
        assert denied.value.status_code == 404
        response = asyncio.run(
            handler(
                _demo_request(enabled=True, borrower_reference=borrower.reference),
                BackgroundTasks(),
                fixture.principal,
            )
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/borrowers/B-DEMO")
        assert len(captured["events"]) == 3
        assert all(event.borrower_id == borrower.id for event in captured["events"])
        assert captured["actor"].id == fake_nightly.system_actor_id
        assert captured["scope"].paths == resolve_scope(fixture.principal, fixture.session).paths

        # The repayment-status band reads facility conduct: the simulation
        # records today's days past due, once per facility per day.
        from datetime import UTC, datetime

        from sqlalchemy import select

        from covenant_radar.db.models.facility import FacilityConduct

        asyncio.run(
            handler(
                _demo_request(enabled=True, borrower_reference=borrower.reference),
                BackgroundTasks(),
                fixture.principal,
            )
        )
        rows = (
            fixture.session.execute(
                select(FacilityConduct).where(
                    FacilityConduct.as_of_date == datetime.now(UTC).date()
                )
            )
            .scalars()
            .all()
        )
        assert [row.days_past_due for row in rows] == [40]
    finally:
        fixture.close()
