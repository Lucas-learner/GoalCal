"""Web service facade for calendar sync (outbound .ics feed + inbound ICS + native bidirectional)."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from ..crypto import encrypt_secrets
from ..domain.bidirectional_sync import (
    apply_native_pull,
    backfill_goalcal_source,
    requeue_upcoming_goalcal_events,
)
from ..domain.calendar_sync import (
    MAX_SOURCES_PER_USER,
    apply_source_sync,
    delete_source_events,
    detect_sync_conflicts,
    get_provider,
    new_feed_token,
    record_source_error,
)
from ..domain.reminders import get_calendar_alarms_with_row, set_calendar_alarms
from ..domain.sync_providers import RemoteCalendar, provider_capabilities
from ..models import CalendarConnection, CalendarFeed, CalendarSource, SyncOutbox
from ..safe_fetch import FetchError
from .core import ApiError, _iso, _text, version_token


def _localized_fetch_error(lang: str, detail: str) -> str:
    zh_map = {
        "Only standard HTTP/HTTPS URLs are supported": "仅支持标准 http://、https:// 或 webcal:// 链接（不支持端口和账号密码）。",
        "Only standard HTTPS CalDAV URLs are supported": "仅支持标准 https:// CalDAV 地址（不支持自定义端口或内嵌账号密码）。",
        "Only public addresses are allowed": "出于安全限制，仅允许公网可访问的日历地址。",
        "IPv6 transition and special-use ranges are not allowed": "出于安全限制，不允许访问该网络地址。",
        "Invalid URL": "日历链接格式无效，请检查后重试。",
        "Calendar exceeds 5 MB": "外部日历文件超过 5 MB 上限。",
        "Download deadline exceeded": "拉取外部日历超时，请检查链接是否可访问。",
        "Request deadline exceeded": "连接外部日历服务器超时，请稍后重试。",
        "Redirect limit exceeded": "外部日历重定向次数过多，无法拉取。",
        "Unsupported response": "外部日历服务器返回了不支持的响应状态。",
        "No public address": "无法解析该日历域名或地址不可公网访问。",
        "CalDAV authentication failed; check username and app-specific password": "CalDAV 认证失败：请检查账号邮箱及专用密码。",
    }
    if lang == "zh":
        return zh_map.get(detail, f"同步外部日历失败：{detail}")
    return detail


def upsert_connection_sources(
    session,
    *,
    user_id: int,
    conn: CalendarConnection,
    calendars: list[RemoteCalendar],
    now_naive: datetime,
    enable_goalcal: bool = True,
) -> tuple[list[CalendarSource], list[CalendarSource]]:
    """Create or update CalendarSource rows for discovered native calendars."""
    existing = list(
        session.scalars(
            select(CalendarSource).where(
                CalendarSource.user_id == user_id,
                CalendarSource.connection_id == conn.id,
            )
        )
    )
    by_remote_id = {src.remote_calendar_id: src for src in existing}
    created: list[CalendarSource] = []
    all_sources: list[CalendarSource] = []

    for cal in calendars:
        row = by_remote_id.get(cal.remote_id)
        if row is not None:
            row.name = cal.name[:120]
            row.url = (cal.url or "")[:2000]
            if cal.is_goalcal:
                row.writable = cal.writable
                row.is_goalcal_calendar = True
                row.direction = "bidirectional"
            all_sources.append(row)
            continue

        is_gc = bool(cal.is_goalcal and cal.writable)
        row = CalendarSource(
            user_id=user_id,
            connection_id=conn.id,
            provider=conn.provider,
            direction="bidirectional" if is_gc else "inbound",
            name=cal.name[:120],
            url=(cal.url or "")[:2000],
            remote_calendar_id=cal.remote_id[:500],
            color=cal.color or "#6b7f89",
            blocks_scheduling=not is_gc,
            all_day_mode="ignore" if is_gc else "show",
            writable=is_gc,
            is_goalcal_calendar=is_gc,
            enabled=enable_goalcal if is_gc else True,
            sync_interval_minutes=15,
            created_at=now_naive,
            updated_at=now_naive,
        )
        session.add(row)
        session.flush()
        created.append(row)
        all_sources.append(row)
    session.flush()
    return all_sources, created


class CalendarSyncService:
    def __init__(self, core):
        self.core = core
        self.session = core.session
        self.user_id = core.user_id
        self.tz = core.tz
        self.lang = core.lang

    def _t(self, zh: str, en: str) -> str:
        return self.core._t(zh, en)

    def _feed_row(self) -> CalendarFeed | None:
        return self.session.scalar(select(CalendarFeed).where(CalendarFeed.user_id == self.user_id))

    def _connections(self) -> list[CalendarConnection]:
        return list(
            self.session.scalars(
                select(CalendarConnection)
                .where(CalendarConnection.user_id == self.user_id)
                .order_by(CalendarConnection.created_at, CalendarConnection.id)
            )
        )

    def _sources(self) -> list[CalendarSource]:
        return list(
            self.session.scalars(
                select(CalendarSource)
                .where(CalendarSource.user_id == self.user_id)
                .order_by(CalendarSource.is_goalcal_calendar.desc(), CalendarSource.created_at, CalendarSource.id)
            )
        )

    def _outbox_pending_counts(self) -> dict[int, int]:
        rows = self.session.execute(
            select(SyncOutbox.source_id, func.count(SyncOutbox.id))
            .where(
                SyncOutbox.user_id == self.user_id,
                SyncOutbox.status == "pending",
            )
            .group_by(SyncOutbox.source_id)
        ).all()
        return {int(src_id): int(cnt) for src_id, cnt in rows}

    def _reserved_habit_spans(self):
        pad = timedelta(minutes=self.core._availability()["buffer_minutes"])
        return [
            (event.start_utc - pad, event.end_utc + pad)
            for event in self.core._events()
            if event.kind == "habit" and event.status in ("planned", "moved") and event.end_utc > self.core.now_naive
        ]

    def serialize_feed(self, feed: CalendarFeed | None) -> dict:
        if feed is None:
            return {
                "enabled": False,
                "path": None,
                "include_blocks": True,
                "include_habits": True,
                "include_manual": True,
                "rotated_at": None,
                "last_accessed_at": None,
                "version": None,
            }
        return {
            "enabled": bool(feed.enabled),
            "path": f"/calendar/feed/{feed.token}.ics" if feed.enabled else None,
            "include_blocks": bool(feed.include_blocks),
            "include_habits": bool(feed.include_habits),
            "include_manual": bool(feed.include_manual),
            "rotated_at": _iso(feed.rotated_at),
            "last_accessed_at": _iso(feed.last_accessed_at),
            "version": version_token(feed),
        }

    def serialize_connection(self, conn: CalendarConnection) -> dict:
        return {
            "id": conn.id,
            "provider": conn.provider,
            "account_label": conn.account_label,
            "server_url": conn.server_url,
            "username": conn.username,
            "status": conn.status,
            "last_error": conn.last_error,
            "created_at": _iso(conn.created_at),
            "version": version_token(conn),
        }

    def serialize_source(self, source: CalendarSource, outbox_pending: int = 0) -> dict:
        return {
            "id": source.id,
            "connection_id": source.connection_id,
            "provider": source.provider,
            "direction": source.direction,
            "name": source.name,
            "url": source.url,
            "remote_calendar_id": source.remote_calendar_id,
            "color": source.color or "#6b7f89",
            "blocks_scheduling": bool(source.blocks_scheduling),
            "all_day_mode": source.all_day_mode or "ignore",
            "writable": bool(source.writable),
            "is_goalcal_calendar": bool(source.is_goalcal_calendar),
            "outbox_pending": outbox_pending,
            "enabled": bool(source.enabled),
            "sync_interval_minutes": source.sync_interval_minutes,
            "last_synced_at": _iso(source.last_synced_at),
            "last_status": source.last_status,
            "last_error": source.last_error,
            "last_added": source.last_added,
            "last_updated": source.last_updated,
            "last_removed": source.last_removed,
            "last_skipped": source.last_skipped,
            "version": version_token(source),
        }

    def serialize_alarms(self) -> dict:
        alarms, row = get_calendar_alarms_with_row(self.session, self.user_id, now=self.core.now_naive)
        return {
            **alarms,
            "version": version_token(row) if row is not None else None,
        }

    def state(self) -> dict:
        pending_map = self._outbox_pending_counts()
        return {
            "feed": self.serialize_feed(self._feed_row()),
            "alarms": self.serialize_alarms(),
            "providers": provider_capabilities(),
            "connections": [self.serialize_connection(c) for c in self._connections()],
            "sources": [self.serialize_source(s, pending_map.get(s.id, 0)) for s in self._sources()],
            "conflicts": detect_sync_conflicts(self.session, self.user_id, self.core.now_naive),
        }

    def update_alarms(self, body: dict) -> dict:
        _, existing_row = get_calendar_alarms_with_row(self.session, self.user_id, now=self.core.now_naive)
        if existing_row is not None and body.get("version") is not None:
            self.core.check_version(existing_row, body)
        payload = {
            "enabled": bool(body["enabled"]),
            "block_minutes": body.get("block_minutes", None),
            "habit_minutes": body.get("habit_minutes", None),
            "manual_minutes": body.get("manual_minutes", None),
        }
        alarms, row = set_calendar_alarms(self.session, self.user_id, payload)
        self.session.flush()
        queued = requeue_upcoming_goalcal_events(self.session, self.user_id, now_naive=self.core.now_naive)
        msg = (
            self._t(
                f"已更新日历提醒闹钟设置（已排入 {queued} 条待同步日程）。",
                f"Calendar reminders updated ({queued} upcoming synced event(s) queued).",
            )
            if queued > 0
            else self._t("已更新日历提醒闹钟设置。", "Calendar reminders updated.")
        )
        return {
            "alarms": {**alarms, "version": version_token(row)},
            "queued_push": queued,
            "message": msg,
        }

    def update_feed(self, body: dict) -> dict:
        feed = self._feed_row()
        enabled = bool(body["enabled"])
        include_blocks = bool(body.get("include_blocks", True))
        include_habits = bool(body.get("include_habits", True))
        include_manual = bool(body.get("include_manual", True))

        if feed is None:
            feed = CalendarFeed(
                user_id=self.user_id,
                token=new_feed_token(),
                enabled=enabled,
                include_blocks=include_blocks,
                include_habits=include_habits,
                include_manual=include_manual,
                created_at=self.core.now_naive,
                rotated_at=self.core.now_naive,
            )
            self.session.add(feed)
        else:
            if body.get("version") is not None:
                self.core.check_version(feed, body)
            feed.enabled = enabled
            feed.include_blocks = include_blocks
            feed.include_habits = include_habits
            feed.include_manual = include_manual
        self.session.flush()
        msg = (
            self._t("已更新日历订阅导出设置。", "Calendar subscription feed updated.")
            if enabled
            else self._t("已停用日历订阅导出链接。", "Calendar subscription feed disabled.")
        )
        return {"feed": self.serialize_feed(feed), "message": msg}

    def rotate_feed(self, body: dict) -> dict:
        feed = self._feed_row()
        if feed is None:
            feed = CalendarFeed(
                user_id=self.user_id,
                token=new_feed_token(),
                enabled=True,
                include_blocks=True,
                include_habits=True,
                include_manual=True,
                created_at=self.core.now_naive,
                rotated_at=self.core.now_naive,
            )
            self.session.add(feed)
        else:
            if body.get("version") is not None:
                self.core.check_version(feed, body)
            feed.token = new_feed_token()
            feed.enabled = True
            feed.rotated_at = self.core.now_naive
            feed.last_accessed_at = None
        self.session.flush()
        return {
            "feed": self.serialize_feed(feed),
            "message": self._t("已生成新的订阅链接，旧链接已立即失效。", "New subscription link generated. The previous link is now invalid."),
        }

    def connect_caldav(self, body: dict) -> dict:
        discovered = body.get("_discovered")
        if not discovered:
            raise ApiError("caldav_connect_failed", self._t("未能验证 CalDAV 账号。", "Failed to verify CalDAV account."), 422)
        home_url, calendars = discovered
        username = body["username"].strip()
        password = body["password"]
        enable_two_way = bool(body.get("enable_two_way", True))

        conn = next(
            (c for c in self._connections() if c.provider == "caldav" and c.server_url == home_url and c.username == username),
            None,
        )
        encrypted = encrypt_secrets({"password": password})
        if conn is None:
            conn = CalendarConnection(
                user_id=self.user_id,
                provider="caldav",
                account_label=username[:200],
                server_url=home_url[:500],
                username=username[:200],
                encrypted_secrets=encrypted,
                status="active",
                last_error="",
                created_at=self.core.now_naive,
                updated_at=self.core.now_naive,
            )
            self.session.add(conn)
            self.session.flush()
        else:
            conn.encrypted_secrets = encrypted
            conn.status = "active"
            conn.last_error = ""
            conn.updated_at = self.core.now_naive

        sources, created = upsert_connection_sources(
            self.session,
            user_id=self.user_id,
            conn=conn,
            calendars=calendars,
            now_naive=self.core.now_naive,
            enable_goalcal=enable_two_way,
        )
        queued = 0
        for src in created:
            if src.is_goalcal_calendar and src.enabled:
                queued += backfill_goalcal_source(self.session, self.user_id, src, now_naive=self.core.now_naive)

        pending_map = self._outbox_pending_counts()
        return {
            "connection": self.serialize_connection(conn),
            "sources": [self.serialize_source(s, pending_map.get(s.id, 0)) for s in sources],
            "queued_push": queued,
            "message": self._t(
                f"已连接 {username}（发现 {len(sources)} 个日历），正在后台同步。",
                f"Connected {username} ({len(sources)} calendar(s) found); syncing in background.",
            ),
        }

    def refresh_connection(self, conn_id: int, body: dict) -> dict:
        conn = self.core._get(CalendarConnection, conn_id)
        if body.get("version") is not None:
            self.core.check_version(conn, body)
        updated_secrets = body.get("_updated_secrets")
        if updated_secrets is not None:
            conn.encrypted_secrets = encrypt_secrets(updated_secrets)
            conn.token_expires_at = body.get("_updated_expires")
        if body.get("_auth_error"):
            conn.status = "reauth_required"
            conn.last_error = str(body["_auth_error"])[:500]
            self.session.flush()
            raise ApiError("reauth_required", _localized_fetch_error(self.lang, conn.last_error), 422)

        discovered = body.get("_discovered")
        if not discovered:
            raise ApiError("refresh_failed", self._t("刷新日历列表失败。", "Failed to refresh remote calendars."), 422)
        home_url, calendars = discovered
        if home_url:
            conn.server_url = home_url[:500]
        conn.status = "active"
        conn.last_error = ""
        conn.updated_at = self.core.now_naive
        sources, created = upsert_connection_sources(
            self.session,
            user_id=self.user_id,
            conn=conn,
            calendars=calendars,
            now_naive=self.core.now_naive,
            enable_goalcal=True,
        )
        for src in created:
            if src.is_goalcal_calendar and src.enabled:
                backfill_goalcal_source(self.session, self.user_id, src, now_naive=self.core.now_naive)
        pending_map = self._outbox_pending_counts()
        return {
            "connection": self.serialize_connection(conn),
            "sources": [self.serialize_source(s, pending_map.get(s.id, 0)) for s in sources],
            "message": self._t(f"已刷新「{conn.account_label}」下的 {len(sources)} 个日历。", f'Refreshed {len(sources)} calendar(s) under "{conn.account_label}".'),
        }

    def delete_connection(self, conn_id: int, body: dict) -> dict:
        conn = self.core._get(CalendarConnection, conn_id)
        self.core.check_version(conn, body)
        label = conn.account_label or conn.username or conn.provider
        linked_sources = [s for s in self._sources() if s.connection_id == conn.id]
        removed_events = 0
        for src in linked_sources:
            removed_events += delete_source_events(self.session, self.user_id, src.id)
            self.session.delete(src)
        self.session.flush()
        self.session.delete(conn)
        self.session.flush()
        return {
            "removed_events": removed_events,
            "message": self._t(
                f"已断开「{label}」连接并清理 {removed_events} 条同步日程。",
                f'Disconnected "{label}" and cleared {removed_events} synced event(s).',
            ),
        }

    def create_source(self, body: dict) -> dict:
        name = _text(body.get("name"), field=self._t("日历名称", "Calendar name"), limit=120, lang=self.lang)
        provider_name = body.get("provider", "ics_url")
        interval = int(body.get("sync_interval_minutes", 30))
        color = body.get("color", "#6b7f89")
        blocks_scheduling = bool(body.get("blocks_scheduling", True))
        all_day_mode = body.get("all_day_mode", "ignore")

        existing_ics = [s for s in self._sources() if s.provider == "ics_url"]
        if len(existing_ics) >= MAX_SOURCES_PER_USER:
            raise ApiError(
                "limit_exceeded",
                self._t(f"最多可添加 {MAX_SOURCES_PER_USER} 个外部 ICS 订阅源。", f"You can connect up to {MAX_SOURCES_PER_USER} external ICS subscriptions."),
                422,
            )

        normalized_url = body.get("_normalized_url")
        if not normalized_url:
            try:
                normalized_url = get_provider(provider_name).normalize_url(body.get("url", ""))
            except (FetchError, ValueError) as exc:
                raise ApiError("invalid_url", _localized_fetch_error(self.lang, str(exc)), 422) from None

        if any(s.url == normalized_url for s in existing_ics):
            raise ApiError(
                "duplicate_source",
                self._t("这个日历订阅链接已经添加过了。", "This calendar subscription URL is already connected."),
                409,
            )

        source = CalendarSource(
            user_id=self.user_id,
            provider=provider_name,
            direction="inbound",
            name=name,
            url=normalized_url,
            color=color,
            blocks_scheduling=blocks_scheduling,
            all_day_mode=all_day_mode,
            enabled=True,
            sync_interval_minutes=interval,
            created_at=self.core.now_naive,
            updated_at=self.core.now_naive,
        )
        self.session.add(source)
        self.session.flush()

        fetched = body.get("_fetched")
        if fetched is None:
            raise ApiError(
                "sync_fetch_failed",
                self._t("未能读取该外部日历内容。", "Failed to fetch external calendar content."),
                422,
            )
        reserved = self._reserved_habit_spans()
        try:
            imported = apply_source_sync(
                self.session,
                self.user_id,
                self.tz,
                source,
                fetched,
                now=self.core.now,
                slot_allowed=lambda s, e, r=reserved: not any(s < hi and e > lo for lo, hi in r),
            )
        except ValueError as exc:
            raise ApiError(
                "invalid_ics",
                self._t(f"外部日历格式无效：{exc}", f"Invalid external calendar content: {exc}"),
                422,
            ) from None

        return {
            "source": self.serialize_source(source),
            "added": imported.added,
            "updated": imported.updated,
            "removed": imported.removed,
            "skipped": imported.skipped,
            "message": self._t(
                f"已添加「{name}」并同步：新增 {imported.added} 条、跳过 {imported.skipped} 条。",
                f'Connected "{name}": added {imported.added}, skipped {imported.skipped}.',
            ),
        }

    def update_source(self, source_id: int, body: dict) -> dict:
        source = self.core._get(CalendarSource, source_id)
        self.core.check_version(source, body)
        was_enabled = bool(source.enabled)
        if "name" in body:
            source.name = _text(body["name"], field=self._t("日历名称", "Calendar name"), limit=120, lang=self.lang)
        if "enabled" in body:
            source.enabled = bool(body["enabled"])
        if "sync_interval_minutes" in body:
            source.sync_interval_minutes = int(body["sync_interval_minutes"])
        if "color" in body:
            source.color = body["color"]
        if "blocks_scheduling" in body:
            source.blocks_scheduling = bool(body["blocks_scheduling"])
        if "all_day_mode" in body:
            if source.all_day_mode != body["all_day_mode"]:
                source.etag = None
                source.last_modified = None
                source.sync_token = None
            source.all_day_mode = body["all_day_mode"]
        if "writable" in body and source.is_goalcal_calendar:
            source.writable = bool(body["writable"])
            source.direction = "bidirectional" if source.writable else "inbound"
        if "url" in body and source.provider == "ics_url":
            try:
                normalized = get_provider(source.provider).normalize_url(body["url"])
            except (FetchError, ValueError) as exc:
                raise ApiError("invalid_url", _localized_fetch_error(self.lang, str(exc)), 422) from None
            if normalized != source.url:
                if any(s.id != source.id and s.url == normalized for s in self._sources() if s.provider == "ics_url"):
                    raise ApiError(
                        "duplicate_source",
                        self._t("这个日历订阅链接已经添加过了。", "This calendar subscription URL is already connected."),
                        409,
                    )
                source.url = normalized
                source.etag = None
                source.last_modified = None
                source.last_status = "idle"
                source.last_error = ""
        source.updated_at = self.core.now_naive
        self.session.flush()
        if not was_enabled and source.enabled and source.is_goalcal_calendar and source.writable:
            backfill_goalcal_source(self.session, self.user_id, source, now_naive=self.core.now_naive)
        pending = self._outbox_pending_counts().get(source.id, 0)
        return {
            "source": self.serialize_source(source, pending),
            "message": self._t(f"已更新「{source.name}」的同步设置。", f'Updated sync settings for "{source.name}".'),
        }

    def sync_source(self, source_id: int, body: dict) -> dict:
        source = self.core._get(CalendarSource, source_id)
        if body.get("version") is not None:
            self.core.check_version(source, body)

        if body.get("_conn_id") and body.get("_updated_secrets") is not None:
            conn = self.session.get(CalendarConnection, body["_conn_id"])
            if conn is not None and conn.user_id == self.user_id:
                conn.encrypted_secrets = encrypt_secrets(body["_updated_secrets"])
                conn.token_expires_at = body.get("_updated_expires")
                conn.status = "active"
                conn.last_error = ""

        fetch_error = body.get("_fetch_error")
        if fetch_error:
            err_text = _localized_fetch_error(self.lang, fetch_error)
            record_source_error(source, err_text, now=self.core.now)
            if body.get("_auth_failed") and source.connection_id:
                conn = self.session.get(CalendarConnection, source.connection_id)
                if conn is not None and conn.user_id == self.user_id:
                    conn.status = "reauth_required"
                    conn.last_error = err_text[:500]
            self.session.flush()
            pending = self._outbox_pending_counts().get(source.id, 0)
            return {
                "ok": False,
                "source": self.serialize_source(source, pending),
                "added": 0,
                "updated": 0,
                "removed": 0,
                "skipped": 0,
                "message": self._t(f"「{source.name}」同步失败：{err_text}", f'Sync failed for "{source.name}": {err_text}'),
            }

        reserved = self._reserved_habit_spans()
        slot_fn = lambda s, e, r=reserved: not any(s < hi and e > lo for lo, hi in r)

        if "_native_pull" in body:
            if source.connection_id:
                conn = self.session.get(CalendarConnection, source.connection_id)
                if conn is not None and conn.user_id == self.user_id and conn.status != "active":
                    conn.status = "active"
                    conn.last_error = ""
            prev_suppress = self.session.info.get("sync_suppress_outbox_source_id")
            self.session.info["sync_suppress_outbox_source_id"] = source.id
            try:
                imported = apply_native_pull(
                    self.session,
                    self.user_id,
                    self.tz,
                    source,
                    body["_native_pull"],
                    now=self.core.now,
                    slot_allowed=slot_fn,
                )
            finally:
                if prev_suppress is None:
                    self.session.info.pop("sync_suppress_outbox_source_id", None)
                else:
                    self.session.info["sync_suppress_outbox_source_id"] = prev_suppress
        else:
            fetched = body.get("_fetched")
            if fetched is None:
                err_text = _localized_fetch_error(self.lang, "Calendar download failed")
                record_source_error(source, err_text, now=self.core.now)
                self.session.flush()
                return {
                    "ok": False,
                    "source": self.serialize_source(source),
                    "added": 0,
                    "updated": 0,
                    "removed": 0,
                    "skipped": 0,
                    "message": self._t(f"「{source.name}」同步失败：{err_text}", f'Sync failed for "{source.name}": {err_text}'),
                }
            try:
                imported = apply_source_sync(
                    self.session,
                    self.user_id,
                    self.tz,
                    source,
                    fetched,
                    now=self.core.now,
                    slot_allowed=slot_fn,
                )
            except ValueError as exc:
                err_text = self._t(f"日历格式无效：{exc}", f"Invalid calendar data: {exc}")
                record_source_error(source, err_text, now=self.core.now)
                self.session.flush()
                return {
                    "ok": False,
                    "source": self.serialize_source(source),
                    "added": 0,
                    "updated": 0,
                    "removed": 0,
                    "skipped": 0,
                    "message": self._t(f"「{source.name}」同步失败：{err_text}", f'Sync failed for "{source.name}": {err_text}'),
                }

        pending = self._outbox_pending_counts().get(source.id, 0)
        return {
            "ok": True,
            "source": self.serialize_source(source, pending),
            "added": imported.added,
            "updated": imported.updated,
            "removed": imported.removed,
            "skipped": imported.skipped,
            "message": self._t(
                f"「{source.name}」同步完成：新增 {imported.added} 条、更新 {imported.updated} 条、清理 {imported.removed} 条。",
                f'Synced "{source.name}": added {imported.added}, updated {imported.updated}, removed {imported.removed}.',
            ),
        }

    def delete_source(self, source_id: int, body: dict) -> dict:
        source = self.core._get(CalendarSource, source_id)
        self.core.check_version(source, body)
        name = source.name
        removed_events = delete_source_events(self.session, self.user_id, source.id)
        self.session.delete(source)
        self.session.flush()
        return {
            "removed_events": removed_events,
            "message": self._t(
                f"已移除「{name}」并清理 {removed_events} 条同步日程。",
                f'Removed "{name}" and cleared {removed_events} synced event(s).',
            ),
        }
