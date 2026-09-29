"""Contracts for the feature-flagged live workspace feed."""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from uuid import UUID, uuid4

from covenant_radar.notifications.inapp import InAppNotificationPage, InAppNotificationView
from covenant_radar.security.permissions import Permission
from covenant_radar.security.rbac import Principal
from covenant_radar.services.live_activity import LiveActivityService


class _Notifications:
    def __init__(self, page: InAppNotificationPage) -> None:
        self.page = page

    def list_notifications(self, _principal: Principal, **_kwargs: object) -> InAppNotificationPage:
        return self.page


def _principal() -> Principal:
    return Principal.user(uuid4(), (Permission.VIEW_QUEUE,))


def test_cursor_is_user_scoped_and_safe_notification_content_is_preserved() -> None:
    now = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    notification = InAppNotificationView(
        notification_id=uuid4(),
        title="Band changed",
        body="A scoped borrower moved into review.",
        created_at=now,
        is_read=False,
        is_accessible=True,
        deep_link="/borrowers/B-100",
        template="band_change",
        state="sent",
    )
    page = InAppNotificationPage((notification,), 1, 1, 1, 20, "all")
    service = LiveActivityService(
        object(),
        _Notifications(page),
        cursor_secret=b"l" * 32,  # type: ignore[arg-type]
    )
    principal = _principal()

    first = service.updates(principal, cursor=None)
    second = service.updates(principal, cursor=first.cursor)

    assert first.items[0].title == "Band changed"
    assert first.items[0].affected_regions == ("queue-ledger", "queue-summary")
    assert second.items == ()
    assert service.updates(_principal(), cursor=first.cursor).items
    other_secret = LiveActivityService(
        object(),
        _Notifications(page),
        cursor_secret=b"s" * 32,  # type: ignore[arg-type]
    )
    assert other_secret.updates(principal, cursor=first.cursor).items


def test_cursor_round_trip_when_binary_signature_contains_separator() -> None:
    service = LiveActivityService(
        object(),
        _Notifications(InAppNotificationPage((), 0, 0, 1, 20, "all")),
        cursor_secret=b"l" * 32,  # type: ignore[arg-type]
    )
    timestamp = "2026-09-01T12:00:00+00:00"
    for value in range(1, 1000):
        principal = Principal.user(UUID(int=value), (Permission.VIEW_QUEUE,))
        cursor = service._encode_cursor(principal, timestamp)
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        if b"." in raw[-32:]:
            assert service._decode_cursor(cursor, principal) == datetime.fromisoformat(timestamp)
            return
    raise AssertionError("Fixture must exercise a separator inside the binary signature")
