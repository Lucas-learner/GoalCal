"""Web service facade for calendar sync (outbound .ics feed + inbound sync sources)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from ..domain.calendar_sync import (
    MAX_SOURCES_PER_USER,
    apply_source_sync,
    delete_source_events,
    get_provider,
    new_feed_token,
    record_source_error,
)
from ..models import CalendarFeed, CalendarSource
from ..safe_fetch import FetchError
from .core import ApiError, _iso, _text, version_token


def _localized_fetch_error(lang: str, detail: str) -> str:
    zh_map = {
        "Only standard HTTP/HTTPS URLs are supported": "仅支持标准 http://、https:// 或 webcal:// 链接（不支持端口和账号密码）。",
        "Only public addresses are allowed": "出于安全限制，仅允许公网可访问的日历地址。",
        "IPv6 transition and special-use ranges are not allowed": "出于安全限制，不允许访问该网络地址。",
        "Invalid URL": "日历链接格式无效，请检查后重试。",
        "Calendar exceeds 5 MB": "外部日历文件超过 5 MB 上限。",
        "Download deadline exceeded": "拉取外部日历超时，请检查链接是否可访问。",
        "Redirect limit exceeded": "外部日历重定向次数过多，无法拉取。",
        "Unsupported response": "外部日历服务器返回了不支持的响应状态。",
        "No public address": "无法解析该日历域名或地址不可公网访问。",
    }
    if lang == "zh":
        return zh_map.get(detail, f"拉取外部日历失败：{detail}")
    return detail


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

    def _sources(self) -> list[CalendarSource]:
        return list(
            self.session.scalars(
                select(CalendarSource)
                .where(CalendarSource.user_id == self.user_id)
                .order_by(CalendarSource.created_at, CalendarSource.id)
            )
        )

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

    def serialize_source(self, source: CalendarSource) -> dict:
        return {
            "id": source.id,
            "provider": source.provider,
            "direction": source.direction,
            "name": source.name,
            "url": source.url,
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

    def state(self) -> dict:
        return {
            "feed": self.serialize_feed(self._feed_row()),
            "sources": [self.serialize_source(s) for s in self._sources()],
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

    def create_source(self, body: dict) -> dict:
        name = _text(body.get("name"), field=self._t("日历名称", "Calendar name"), limit=120, lang=self.lang)
        provider_name = body.get("provider", "ics_url")
        interval = int(body.get("sync_interval_minutes", 30))
        existing_sources = self._sources()
        if len(existing_sources) >= MAX_SOURCES_PER_USER:
            raise ApiError(
                "limit_exceeded",
                self._t(f"最多可添加 {MAX_SOURCES_PER_USER} 个外部日历订阅源。", f"You can connect up to {MAX_SOURCES_PER_USER} external calendars."),
                422,
            )

        normalized_url = body.get("_normalized_url")
        if not normalized_url:
            try:
                normalized_url = get_provider(provider_name).normalize_url(body.get("url", ""))
            except (FetchError, ValueError) as exc:
                raise ApiError("invalid_url", _localized_fetch_error(self.lang, str(exc)), 422) from None

        if any(s.url == normalized_url for s in existing_sources):
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
        if "name" in body:
            source.name = _text(body["name"], field=self._t("日历名称", "Calendar name"), limit=120, lang=self.lang)
        if "enabled" in body:
            source.enabled = bool(body["enabled"])
        if "sync_interval_minutes" in body:
            source.sync_interval_minutes = int(body["sync_interval_minutes"])
        if "url" in body:
            try:
                normalized = get_provider(source.provider).normalize_url(body["url"])
            except (FetchError, ValueError) as exc:
                raise ApiError("invalid_url", _localized_fetch_error(self.lang, str(exc)), 422) from None
            if normalized != source.url:
                if any(s.id != source.id and s.url == normalized for s in self._sources()):
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
        return {
            "source": self.serialize_source(source),
            "message": self._t(f"已更新「{source.name}」的同步设置。", f'Updated sync settings for "{source.name}".'),
        }

    def sync_source(self, source_id: int, body: dict) -> dict:
        source = self.core._get(CalendarSource, source_id)
        if body.get("version") is not None:
            self.core.check_version(source, body)

        fetch_error = body.get("_fetch_error")
        fetched = body.get("_fetched")
        if fetch_error or fetched is None:
            err_text = _localized_fetch_error(self.lang, fetch_error or "Calendar download failed")
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

        return {
            "ok": True,
            "source": self.serialize_source(source),
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
