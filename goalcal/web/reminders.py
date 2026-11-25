"""Web service facade for unified reminders settings and notification history."""
from __future__ import annotations

from sqlalchemy import select

from .. import mailer
from ..domain.reminders import get_email_digests_with_row, set_email_digests
from ..models import CalendarFeed, CalendarSource, NotificationOutbox, User
from .core import _iso, version_token


class ReminderService:
    def __init__(self, core):
        self.core = core
        self.session = core.session
        self.user_id = core.user_id
        self.tz = core.tz
        self.lang = core.lang

    def _t(self, zh: str, en: str) -> str:
        return self.core._t(zh, en)

    def _calendar_sync_connected(self) -> dict[str, bool]:
        has_two_way = (
            self.session.scalar(
                select(CalendarSource.id).where(
                    CalendarSource.user_id == self.user_id,
                    CalendarSource.enabled.is_(True),
                    CalendarSource.writable.is_(True),
                    CalendarSource.is_goalcal_calendar.is_(True),
                )
            )
            is not None
        )
        feed = self.session.scalar(select(CalendarFeed).where(CalendarFeed.user_id == self.user_id))
        return {
            "two_way": has_two_way,
            "feed": bool(feed and feed.enabled),
        }

    def serialize_email_digests(self) -> dict:
        digests, row = get_email_digests_with_row(self.session, self.user_id, now=self.core.now_naive)
        return {
            **digests,
            "version": version_token(row) if row is not None else None,
        }

    def _recent_notifications(self, limit: int = 10) -> list[dict]:
        rows = list(
            self.session.scalars(
                select(NotificationOutbox)
                .where(
                    NotificationOutbox.user_id == self.user_id,
                    NotificationOutbox.kind != "test_email",
                )
                .order_by(NotificationOutbox.created_at.desc(), NotificationOutbox.id.desc())
                .limit(limit)
            )
        )
        return [
            {
                "id": row.id,
                "channel": row.channel,
                "kind": row.kind,
                "subject": row.subject,
                "status": row.status,
                "sent_at": _iso(row.sent_at),
                "scheduled_for": _iso(row.scheduled_for),
            }
            for row in rows
        ]

    def state(self) -> dict:
        user = self.core._get(User, self.user_id)
        cap = mailer.smtp_capabilities()
        return {
            "calendar_alarms": self.core.calendar_sync.serialize_alarms(),
            "calendar_sync_connected": self._calendar_sync_connected(),
            "email_digests": self.serialize_email_digests(),
            "email_channel": {
                "smtp_configured": cap["configured"],
                "from_address": cap["from_address"],
                "user_email": user.email or None,
            },
            "recent_notifications": self._recent_notifications(),
        }

    def update_email(self, body: dict) -> dict:
        _, existing_row = get_email_digests_with_row(self.session, self.user_id, now=self.core.now_naive)
        if existing_row is not None and body.get("version") is not None:
            self.core.check_version(existing_row, body)
        digests, row = set_email_digests(self.session, self.user_id, body)
        self.session.flush()
        msg = (
            self._t("已开启并更新邮件节奏提醒设置。", "Email digest reminders saved and enabled.")
            if digests["enabled"]
            else self._t("已更新邮件节奏提醒设置（当前未开启自动发送）。", "Email digest settings saved (currently paused).")
        )
        return {
            "email_digests": {**digests, "version": version_token(row)},
            "message": msg,
        }
