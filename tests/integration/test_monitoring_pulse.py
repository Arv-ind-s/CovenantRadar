"""The monitoring UI reads durable records inside the caller's scope."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from covenant_radar.db.models.signal import SignalEvent
from covenant_radar.db.scoping import resolve_scope
from covenant_radar.services.monitoring_pulse import (
    borrower_risk_comparison,
    recent_changes,
)
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
        _signal(fixture, borrower.id, ingested_at=_NOW - timedelta(minutes=30), number=40)
        _signal(fixture, outside.id, ingested_at=_NOW - timedelta(minutes=5), number=41)
        fixture.session.flush()

        changes = recent_changes(fixture.session, resolve_scope(fixture.principal, fixture.session))
        comparison = borrower_risk_comparison(fixture.session, borrower.id)

        assert len(changes) == 1
        assert changes[0]["reference"] == "B-MONITORED"
        assert changes[0]["status"] == "Scored"
        assert changes[0]["observation"] == "40 days"
        assert comparison["previous"]["score"] == "20%"
        assert comparison["current"]["score"] == "90%"
    finally:
        fixture.close()
