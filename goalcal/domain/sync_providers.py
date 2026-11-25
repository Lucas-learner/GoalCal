"""Native external calendar adapters: Apple iCloud / CalDAV, Google Calendar, Outlook Graph.

All external HTTP(S) traffic flows through ``safe_http_request`` (pinned DNS,
verified TLS, bounded payload, no environment proxy) and MUST run outside
SQLite write locks.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import secrets
from urllib.parse import quote, urlencode, urljoin, urlsplit
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event as ICalEvent

from ..safe_fetch import FetchError, normalize_caldav_url, safe_http_request
from .ics_import import _expand, _recurrence_id_utc, collect_recurrence_overrides, recurrence_instance_uid
from .reminders import build_valarm

UTC = timezone.utc

CALDAV_PRESETS: dict[str, str] = {
    "icloud": "https://caldav.icloud.com/",
    "fastmail": "https://caldav.fastmail.com/",
}

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GOOGLE_API_BASE = "https://www.googleapis.com/calendar/v3"
GOOGLE_SCOPES = "https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/userinfo.email"

OUTLOOK_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
OUTLOOK_SCOPES = "offline_access Calendars.ReadWrite User.Read"
OUTLOOK_EXT_PROP_ID = "String {66f5a359-4659-4830-9070-00047ec6ac6e} Name goalcalEventId"

_DAV_NS = "DAV:"
_CALDAV_NS = "urn:ietf:params:xml:ns:caldav"
_APPLE_NS = "http://apple.com/ns/ical/"
_CS_NS = "http://calendarserver.org/ns/"


class SyncProviderError(ValueError):
    pass


class SyncAuthError(SyncProviderError):
    pass


@dataclass(frozen=True)
class RemoteCalendar:
    remote_id: str
    name: str
    color: str = "#6b7f89"
    url: str = ""
    writable: bool = False
    is_goalcal: bool = False


@dataclass(frozen=True)
class RemoteEvent:
    remote_uid: str
    title: str
    start_utc: datetime
    end_utc: datetime
    all_day: bool = False
    status: str = "confirmed"
    remote_href: str = ""
    remote_etag: str = ""
    goalcal_event_id: int | None = None
    cancelled: bool = False


@dataclass
class PullResult:
    events: list[RemoteEvent] = field(default_factory=list)
    sync_token: str | None = None
    full_sync: bool = False


@dataclass(frozen=True)
class PushResult:
    remote_uid: str
    remote_href: str = ""
    remote_etag: str = ""


def provider_capabilities() -> dict[str, dict]:
    google_ready = bool(os.getenv("GOOGLE_CLIENT_ID", "").strip() and os.getenv("GOOGLE_CLIENT_SECRET", "").strip())
    outlook_ready = bool(os.getenv("MICROSOFT_CLIENT_ID", "").strip() and os.getenv("MICROSOFT_CLIENT_SECRET", "").strip())
    return {
        "caldav": {"configured": True, "label": "Apple iCloud / CalDAV"},
        "google": {"configured": google_ready, "label": "Google Calendar"},
        "outlook": {"configured": outlook_ready, "label": "Outlook / Microsoft 365"},
    }


def generate_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict[str, str], bytes]:
    try:
        return safe_http_request(method, url, headers=headers, body=body, timeout=timeout, require_https=True)
    except FetchError as exc:
        raise SyncProviderError(str(exc)) from None


def _normalize_color(raw: str | None, fallback: str = "#6b7f89") -> str:
    val = (raw or "").strip()
    if val.startswith("#") and len(val) in (7, 9):
        hex_part = val[1:7]
        if all(c in "0123456789abcdefABCDEF" for c in hex_part):
            return f"#{hex_part.lower()}"
    return fallback


def _utc_aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _utc_naive(dt: datetime) -> datetime:
    return _utc_aware(dt).replace(tzinfo=None)


def _valid_reminder_minutes(payload: dict) -> int | None:
    raw = payload.get("reminder_minutes")
    if isinstance(raw, bool) or not isinstance(raw, int) or not (0 <= raw <= 1440):
        return None
    return raw


def _build_vevent_ics(payload: dict) -> tuple[str, bytes]:
    uid = (payload.get("remote_uid") or f"goalcal-event-{payload['event_id']}@goalcal.app").strip()
    cal = Calendar()
    cal.add("version", "2.0")
    cal.add("prodid", "-//GoalCal//Calendar Sync//EN")
    cal.add("calscale", "GREGORIAN")

    vevent = ICalEvent()
    vevent.add("uid", uid)
    now_dt = datetime.now(UTC)
    vevent.add("dtstamp", now_dt)
    vevent.add("last-modified", now_dt)
    start_dt = datetime.fromisoformat(payload["start_utc"].replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(payload["end_utc"].replace("Z", "+00:00"))
    vevent.add("dtstart", _utc_aware(start_dt))
    vevent.add("dtend", _utc_aware(end_dt))
    vevent.add("summary", payload["title"])
    if payload.get("description"):
        vevent.add("description", payload["description"])
    vevent.add("status", "CONFIRMED")
    vevent.add("x-goalcal-event-id", str(payload["event_id"]))
    reminder_minutes = _valid_reminder_minutes(payload)
    if reminder_minutes is not None:
        vevent.add_component(
            build_valarm(
                reminder_minutes,
                str(payload.get("alarm_description") or payload["title"]),
            )
        )
    cal.add_component(vevent)
    return uid, cal.to_ical()


def _ics_bytes_to_remote_events(
    raw: bytes,
    *,
    tz: str,
    now_naive: datetime,
    href: str = "",
    etag: str = "",
    all_day_mode: str = "show",
) -> list[RemoteEvent]:
    try:
        cal = Calendar.from_ical(raw)
    except Exception:
        return []
    vevents = list(cal.walk("VEVENT"))
    overrides = collect_recurrence_overrides(vevents, tz)
    out: list[RemoteEvent] = []
    for comp in vevents:
        base_uid = str(comp.get("UID") or "").strip()
        if not base_uid:
            continue
        status_raw = str(comp.get("STATUS") or "").strip().upper()
        title = str(comp.get("SUMMARY") or "Event").strip()[:200] or "Event"
        gc_raw = str(comp.get("X-GOALCAL-EVENT-ID") or "").strip()
        goalcal_event_id: int | None = None
        if gc_raw.isdigit():
            goalcal_event_id = int(gc_raw)
        elif base_uid.startswith("goalcal-event-") and "@" in base_uid:
            tail = base_uid[len("goalcal-event-"):].split("@", 1)[0]
            if tail.isdigit():
                goalcal_event_id = int(tail)

        if status_raw == "CANCELLED":
            rec_utc = _recurrence_id_utc(comp, tz)
            cancelled_uid = recurrence_instance_uid(base_uid, rec_utc) if rec_utc is not None else base_uid
            out.append(
                RemoteEvent(
                    remote_uid=cancelled_uid,
                    title=title,
                    start_utc=now_naive,
                    end_utc=now_naive,
                    status="cancelled",
                    remote_href=href,
                    remote_etag=etag,
                    goalcal_event_id=goalcal_event_id if rec_utc is None else None,
                    cancelled=True,
                )
            )
            continue

        expanded = _expand(
            comp,
            tz,
            now_naive,
            [0],
            all_day_mode=all_day_mode,
            extra_excluded=overrides.get(base_uid),
            past_days=7,
        )
        if not expanded:
            continue
        for inst_uid, start_utc, end_utc, is_all_day in expanded:
            out.append(
                RemoteEvent(
                    remote_uid=inst_uid,
                    title=title,
                    start_utc=start_utc,
                    end_utc=end_utc,
                    all_day=is_all_day,
                    status="confirmed",
                    remote_href=href,
                    remote_etag=etag,
                    goalcal_event_id=goalcal_event_id if inst_uid == base_uid else None,
                    cancelled=False,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Apple iCloud / Generic CalDAV
# ---------------------------------------------------------------------------


class CalDavAdapter:
    provider = "caldav"

    @staticmethod
    def resolve_server_url(preset: str, server_url: str | None) -> str:
        if preset in CALDAV_PRESETS:
            return CALDAV_PRESETS[preset]
        return normalize_caldav_url(server_url or "")

    @staticmethod
    def _auth_headers(username: str, password: str, *, extra: dict[str, str] | None = None) -> dict[str, str]:
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/xml; charset=utf-8",
        }
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _dav_request(
        method: str,
        url: str,
        username: str,
        password: str,
        *,
        body: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        headers = CalDavAdapter._auth_headers(username, password, extra=extra_headers)
        status, resp_headers, raw = _request(method, url, headers=headers, body=body)
        if status in (401, 403):
            raise SyncAuthError("CalDAV authentication failed; check username and app-specific password")
        return status, resp_headers, raw

    @staticmethod
    def _find_href(elem: ET.Element, tag_ns: str, tag_name: str, base_url: str) -> str | None:
        container = elem.find(f".//{{{tag_ns}}}{tag_name}")
        if container is None:
            return None
        href = container.find(f"{{{_DAV_NS}}}href")
        if href is not None and href.text and href.text.strip():
            return urljoin(base_url, href.text.strip())
        return None

    @classmethod
    def discover(
        cls,
        server_url: str,
        username: str,
        password: str,
        *,
        ensure_goalcal: bool = True,
    ) -> tuple[str, list[RemoteCalendar]]:
        base = normalize_caldav_url(server_url)
        principal_body = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            b"<D:prop><D:current-user-principal/><C:calendar-home-set/></D:prop></D:propfind>"
        )
        status, _, raw = cls._dav_request("PROPFIND", base, username, password, body=principal_body, extra_headers={"Depth": "0"})
        if status not in (200, 207):
            raise SyncProviderError(f"CalDAV server returned HTTP {status}")

        root = ET.fromstring(raw)
        home_url = cls._find_href(root, _CALDAV_NS, "calendar-home-set", base)
        if not home_url:
            principal_url = cls._find_href(root, _DAV_NS, "current-user-principal", base) or base
            home_body = (
                b'<?xml version="1.0" encoding="utf-8"?>'
                b'<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
                b"<D:prop><C:calendar-home-set/></D:prop></D:propfind>"
            )
            status, _, raw_p = cls._dav_request("PROPFIND", principal_url, username, password, body=home_body, extra_headers={"Depth": "0"})
            if status not in (200, 207):
                raise SyncProviderError(f"CalDAV principal lookup returned HTTP {status}")
            home_url = cls._find_href(ET.fromstring(raw_p), _CALDAV_NS, "calendar-home-set", principal_url) or base

        list_body = (
            b'<?xml version="1.0" encoding="utf-8"?>'
            b'<D:propfind xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav" '
            b'xmlns:A="http://apple.com/ns/ical/" xmlns:CS="http://calendarserver.org/ns/">'
            b"<D:prop><D:displayname/><D:resourcetype/><D:current-user-privilege-set/>"
            b"<A:calendar-color/><C:supported-calendar-component-set/><D:sync-token/></D:prop>"
            b"</D:propfind>"
        )
        status, _, raw_list = cls._dav_request("PROPFIND", home_url, username, password, body=list_body, extra_headers={"Depth": "1"})
        if status not in (200, 207):
            raise SyncProviderError(f"CalDAV calendar list returned HTTP {status}")

        list_root = ET.fromstring(raw_list)
        calendars: list[RemoteCalendar] = []
        seen_urls: set[str] = set()
        for resp in list_root.findall(f"{{{_DAV_NS}}}response"):
            href_el = resp.find(f"{{{_DAV_NS}}}href")
            if href_el is None or not href_el.text:
                continue
            cal_url = urljoin(home_url, href_el.text.strip())
            if cal_url in seen_urls:
                continue
            resourcetype = resp.find(f".//{{{_DAV_NS}}}resourcetype")
            if resourcetype is None or resourcetype.find(f"{{{_CALDAV_NS}}}calendar") is None:
                continue
            # Skip iCloud VTODO-only reminder lists
            comp_set = resp.find(f".//{{{_CALDAV_NS}}}supported-calendar-component-set")
            if comp_set is not None:
                comps = [el.attrib.get("name", "").upper() for el in comp_set.findall(f"{{{_CALDAV_NS}}}comp")]
                if comps and "VEVENT" not in comps:
                    continue

            name_el = resp.find(f".//{{{_DAV_NS}}}displayname")
            cal_name = (name_el.text or "").strip() if name_el is not None and name_el.text else ""
            if not cal_name:
                cal_name = cal_url.rstrip("/").rsplit("/", 1)[-1] or "Calendar"

            color_el = resp.find(f".//{{{_APPLE_NS}}}calendar-color")
            color = _normalize_color(color_el.text if color_el is not None else None)

            priv_set = resp.find(f".//{{{_DAV_NS}}}current-user-privilege-set")
            writable = True
            if priv_set is not None:
                writable = any(
                    priv_set.find(f".//{{{_DAV_NS}}}{tag}") is not None
                    for tag in ("write", "write-content", "all")
                )

            seen_urls.add(cal_url)
            is_goalcal = cal_name.strip().lower() == "goalcal"
            calendars.append(
                RemoteCalendar(
                    remote_id=cal_url,
                    name=cal_name[:120],
                    color=color,
                    url=cal_url,
                    writable=writable,
                    is_goalcal=is_goalcal,
                )
            )

        if ensure_goalcal and not any(c.is_goalcal for c in calendars):
            slug = f"goalcal-{secrets.token_hex(4)}/"
            mk_url = urljoin(home_url if home_url.endswith("/") else home_url + "/", slug)
            mk_body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<C:mkcalendar xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav" '
                'xmlns:A="http://apple.com/ns/ical/">'
                "<D:set><D:prop>"
                "<D:displayname>GoalCal</D:displayname>"
                "<A:calendar-color>#6b7f89FF</A:calendar-color>"
                "<C:supported-calendar-component-set><C:comp name=\"VEVENT\"/></C:supported-calendar-component-set>"
                "</D:prop></D:set></C:mkcalendar>"
            ).encode("utf-8")
            mk_status, _, _ = cls._dav_request("MKCALENDAR", mk_url, username, password, body=mk_body)
            if mk_status in (200, 201, 204, 207):
                calendars.insert(
                    0,
                    RemoteCalendar(
                        remote_id=mk_url,
                        name="GoalCal",
                        color="#6b7f89",
                        url=mk_url,
                        writable=True,
                        is_goalcal=True,
                    ),
                )
        return home_url, calendars

    @classmethod
    def pull(
        cls,
        calendar_url: str,
        username: str,
        password: str,
        *,
        tz: str,
        now_naive: datetime,
        sync_token: str | None = None,
        all_day_mode: str = "show",
    ) -> PullResult:
        used_sync_collection = False
        if sync_token:
            sync_body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<D:sync-collection xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
                f"<D:sync-token>{sync_token}</D:sync-token>"
                "<D:sync-level>1</D:sync-level>"
                "<D:prop><D:getetag/><C:calendar-data/></D:prop>"
                "</D:sync-collection>"
            ).encode("utf-8")
            status, _, raw = cls._dav_request("REPORT", calendar_url, username, password, body=sync_body, extra_headers={"Depth": "1"})
            if status in (200, 207):
                used_sync_collection = True
            elif status not in (400, 403, 404, 410, 501):
                raise SyncProviderError(f"CalDAV sync-collection failed (HTTP {status})")

        if not used_sync_collection:
            start_str = (now_naive - timedelta(days=30)).strftime("%Y%m%dT%H%M%SZ")
            end_str = (now_naive + timedelta(days=180)).strftime("%Y%m%dT%H%M%SZ")
            query_body = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
                "<D:prop><D:getetag/><C:calendar-data/></D:prop>"
                "<C:filter><C:comp-filter name=\"VCALENDAR\"><C:comp-filter name=\"VEVENT\">"
                f'<C:time-range start="{start_str}" end="{end_str}"/>'
                "</C:comp-filter></C:comp-filter></C:filter>"
                "</C:calendar-query>"
            ).encode("utf-8")
            status, _, raw = cls._dav_request("REPORT", calendar_url, username, password, body=query_body, extra_headers={"Depth": "1"})
            if status not in (200, 207):
                raise SyncProviderError(f"CalDAV calendar-query failed (HTTP {status})")

        root = ET.fromstring(raw)
        next_token_el = root.find(f"{{{_DAV_NS}}}sync-token")
        next_token = next_token_el.text.strip() if next_token_el is not None and next_token_el.text else None

        events: list[RemoteEvent] = []
        for resp in root.findall(f"{{{_DAV_NS}}}response"):
            href_el = resp.find(f"{{{_DAV_NS}}}href")
            href = urljoin(calendar_url, href_el.text.strip()) if href_el is not None and href_el.text else ""
            status_el = resp.find(f"{{{_DAV_NS}}}status") or resp.find(f".//{{{_DAV_NS}}}status")
            status_text = (status_el.text or "") if status_el is not None else ""
            if " 404 " in status_text or " 410 " in status_text:
                events.append(
                    RemoteEvent(
                        remote_uid="",
                        title="",
                        start_utc=now_naive,
                        end_utc=now_naive,
                        remote_href=href,
                        cancelled=True,
                    )
                )
                continue
            etag_el = resp.find(f".//{{{_DAV_NS}}}getetag")
            etag = (etag_el.text or "").strip() if etag_el is not None and etag_el.text else ""
            cal_el = resp.find(f".//{{{_CALDAV_NS}}}calendar-data")
            if cal_el is None or not cal_el.text:
                continue
            events.extend(
                _ics_bytes_to_remote_events(
                    cal_el.text.encode("utf-8"),
                    tz=tz,
                    now_naive=now_naive,
                    href=href,
                    etag=etag,
                    all_day_mode=all_day_mode,
                )
            )
        return PullResult(events=events, sync_token=next_token, full_sync=not used_sync_collection)

    @classmethod
    def push_upsert(cls, calendar_url: str, username: str, password: str, payload: dict) -> PushResult:
        uid, ics_bytes = _build_vevent_ics(payload)
        target_url = (payload.get("remote_href") or "").strip() or urljoin(
            calendar_url if calendar_url.endswith("/") else calendar_url + "/",
            f"{quote(uid, safe='@._-')}.ics",
        )
        status, resp_headers, _ = cls._dav_request(
            "PUT",
            target_url,
            username,
            password,
            body=ics_bytes,
            extra_headers={"Content-Type": "text/calendar; charset=utf-8"},
        )
        if status not in (200, 201, 204):
            raise SyncProviderError(f"CalDAV PUT failed (HTTP {status})")
        return PushResult(
            remote_uid=uid,
            remote_href=target_url,
            remote_etag=(resp_headers.get("etag") or "").strip(),
        )

    @classmethod
    def push_delete(cls, calendar_url: str, username: str, password: str, remote_uid: str, remote_href: str) -> None:
        target_url = (remote_href or "").strip() or urljoin(
            calendar_url if calendar_url.endswith("/") else calendar_url + "/",
            f"{quote(remote_uid, safe='@._-')}.ics",
        )
        status, _, _ = cls._dav_request("DELETE", target_url, username, password, extra_headers={})
        if status not in (200, 204, 404, 410):
            raise SyncProviderError(f"CalDAV DELETE failed (HTTP {status})")


# ---------------------------------------------------------------------------
# Google Calendar (OAuth 2.0 + REST v3 + Watch Webhooks)
# ---------------------------------------------------------------------------


def _parse_google_datetime(part: dict, tz: str) -> tuple[datetime, bool]:
    if "date" in part and part["date"]:
        day = datetime.fromisoformat(part["date"]).date()
        local_dt = datetime(day.year, day.month, day.day, 0, 0, tzinfo=ZoneInfo(tz))
        return _utc_naive(local_dt), True
    raw = part.get("dateTime", "")
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(part.get("timeZone") or tz))
    return _utc_naive(dt), False


class GoogleCalendarAdapter:
    provider = "google"

    @staticmethod
    def _credentials() -> tuple[str, str]:
        cid = os.getenv("GOOGLE_CLIENT_ID", "").strip()
        secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
        if not cid or not secret:
            raise SyncProviderError("Google OAuth is not configured on this server")
        return cid, secret

    @classmethod
    def authorize_url(cls, *, redirect_uri: str, state: str, code_challenge: str) -> str:
        cid, _ = cls._credentials()
        params = {
            "client_id": cid,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": GOOGLE_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    @classmethod
    def exchange_code(cls, *, code: str, redirect_uri: str, code_verifier: str) -> tuple[dict, datetime | None]:
        cid, secret = cls._credentials()
        body = urlencode({
            "code": code,
            "client_id": cid,
            "client_secret": secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        }).encode("utf-8")
        status, _, raw = _request("POST", GOOGLE_TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded"}, body=body)
        if status >= 400:
            raise SyncAuthError("Google OAuth token exchange failed")
        data = json.loads(raw.decode("utf-8"))
        expires_in = int(data.get("expires_in", 3600))
        expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=expires_in)
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", ""),
            "expires_at": expires_at.isoformat(),
        }, expires_at

    @classmethod
    def ensure_fresh_token(cls, secrets_dict: dict) -> tuple[dict, datetime | None, bool]:
        exp_raw = secrets_dict.get("expires_at")
        now_naive = datetime.now(UTC).replace(tzinfo=None)
        if exp_raw:
            try:
                exp_dt = datetime.fromisoformat(str(exp_raw).replace("Z", ""))
                if exp_dt - timedelta(seconds=120) > now_naive and secrets_dict.get("access_token"):
                    return secrets_dict, exp_dt, False
            except ValueError:
                pass
        refresh = secrets_dict.get("refresh_token")
        if not refresh:
            if secrets_dict.get("access_token"):
                return secrets_dict, None, False
            raise SyncAuthError("Google Calendar authorization expired; reconnect required")
        cid, secret = cls._credentials()
        body = urlencode({
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }).encode("utf-8")
        status, _, raw = _request("POST", GOOGLE_TOKEN_URL, headers={"Content-Type": "application/x-www-form-urlencoded"}, body=body)
        if status in (400, 401, 403):
            raise SyncAuthError("Google Calendar refresh token expired or revoked")
        if status >= 400:
            raise SyncProviderError(f"Google token refresh failed (HTTP {status})")
        data = json.loads(raw.decode("utf-8"))
        expires_at = now_naive + timedelta(seconds=int(data.get("expires_in", 3600)))
        updated = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or refresh,
            "expires_at": expires_at.isoformat(),
        }
        return updated, expires_at, True

    @classmethod
    def _api(cls, method: str, path: str, secrets_dict: dict, *, body: dict | None = None) -> tuple[int, dict]:
        url = path if path.startswith("https://") else f"{GOOGLE_API_BASE}{path}"
        headers = {"Authorization": f"Bearer {secrets_dict['access_token']}"}
        raw_body = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        status, _, raw = _request(method, url, headers=headers, body=raw_body)
        if status == 401:
            raise SyncAuthError("Google Calendar token rejected (401)")
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
        return status, parsed

    @classmethod
    def userinfo_email(cls, secrets_dict: dict) -> str:
        status, data = cls._api("GET", GOOGLE_USERINFO_URL, secrets_dict)
        if status == 200 and data.get("email"):
            return str(data["email"])[:200]
        return "Google Account"

    @classmethod
    def discover(cls, secrets_dict: dict, *, tz: str = "Asia/Shanghai", ensure_goalcal: bool = True) -> list[RemoteCalendar]:
        calendars: list[RemoteCalendar] = []
        page_token: str | None = None
        while True:
            qs = f"?pageToken={quote(page_token)}" if page_token else ""
            status, data = cls._api("GET", f"/users/me/calendarList{qs}", secrets_dict)
            if status != 200:
                raise SyncProviderError(f"Google calendarList failed (HTTP {status})")
            for item in data.get("items", []):
                role = item.get("accessRole", "")
                name = str(item.get("summaryOverride") or item.get("summary") or item.get("id") or "Calendar").strip()[:120]
                calendars.append(
                    RemoteCalendar(
                        remote_id=str(item["id"]),
                        name=name,
                        color=_normalize_color(item.get("backgroundColor")),
                        writable=role in ("owner", "writer"),
                        is_goalcal=name.lower() == "goalcal" and role in ("owner", "writer"),
                    )
                )
            page_token = data.get("nextPageToken")
            if not page_token:
                break

        if ensure_goalcal and not any(c.is_goalcal for c in calendars):
            status, created = cls._api(
                "POST",
                "/calendars",
                secrets_dict,
                body={"summary": "GoalCal", "timeZone": tz},
            )
            if status in (200, 201) and created.get("id"):
                calendars.insert(
                    0,
                    RemoteCalendar(
                        remote_id=str(created["id"]),
                        name="GoalCal",
                        color="#6b7f89",
                        writable=True,
                        is_goalcal=True,
                    ),
                )
        return calendars

    @classmethod
    def pull(
        cls,
        remote_calendar_id: str,
        secrets_dict: dict,
        *,
        tz: str,
        now_naive: datetime,
        sync_token: str | None = None,
        all_day_mode: str = "show",
    ) -> PullResult:
        def fetch_pages(token: str | None) -> tuple[list[dict], str | None, bool, int]:
            items: list[dict] = []
            page_token: str | None = None
            next_sync: str | None = None
            while True:
                params: dict[str, str] = {"singleEvents": "true", "maxResults": "250"}
                if token:
                    params["syncToken"] = token
                else:
                    params["timeMin"] = _utc_aware(now_naive - timedelta(days=30)).isoformat()
                    params["timeMax"] = _utc_aware(now_naive + timedelta(days=180)).isoformat()
                if page_token:
                    params["pageToken"] = page_token
                status, data = cls._api(
                    "GET",
                    f"/calendars/{quote(remote_calendar_id, safe='')}/events?{urlencode(params)}",
                    secrets_dict,
                )
                if status == 410:
                    return [], None, True, 410
                if status != 200:
                    raise SyncProviderError(f"Google events.list failed (HTTP {status})")
                items.extend(data.get("items", []))
                page_token = data.get("nextPageToken")
                next_sync = data.get("nextSyncToken") or next_sync
                if not page_token:
                    break
            return items, next_sync, token is None, 200

        raw_items, next_sync_token, is_full, status = fetch_pages(sync_token)
        if status == 410:
            raw_items, next_sync_token, is_full, _ = fetch_pages(None)

        events: list[RemoteEvent] = []
        for item in raw_items:
            uid = str(item.get("id") or "").strip()
            if not uid:
                continue
            priv = ((item.get("extendedProperties") or {}).get("private") or {})
            gc_id = int(priv["goalcalEventId"]) if str(priv.get("goalcalEventId", "")).isdigit() else None
            if item.get("status") == "cancelled":
                events.append(
                    RemoteEvent(
                        remote_uid=uid,
                        title=str(item.get("summary") or "Event"),
                        start_utc=now_naive,
                        end_utc=now_naive,
                        status="cancelled",
                        remote_etag=str(item.get("etag") or ""),
                        goalcal_event_id=gc_id,
                        cancelled=True,
                    )
                )
                continue
            start_info = item.get("start") or {}
            end_info = item.get("end") or {}
            if not start_info or not end_info:
                continue
            start_utc, is_all_day = _parse_google_datetime(start_info, tz)
            end_utc, _ = _parse_google_datetime(end_info, tz)
            if is_all_day and all_day_mode == "ignore":
                continue
            events.append(
                RemoteEvent(
                    remote_uid=uid,
                    title=str(item.get("summary") or "Google event").strip()[:200] or "Google event",
                    start_utc=start_utc,
                    end_utc=end_utc,
                    all_day=is_all_day,
                    status="confirmed",
                    remote_etag=str(item.get("etag") or ""),
                    goalcal_event_id=gc_id,
                    cancelled=False,
                )
            )
        return PullResult(events=events, sync_token=next_sync_token, full_sync=is_full)

    @classmethod
    def push_upsert(cls, remote_calendar_id: str, secrets_dict: dict, payload: dict) -> PushResult:
        reminder_minutes = _valid_reminder_minutes(payload)
        reminders = (
            {"useDefault": False, "overrides": [{"method": "popup", "minutes": reminder_minutes}]}
            if reminder_minutes is not None
            else {"useDefault": False, "overrides": []}
        )
        body = {
            "summary": payload["title"],
            "description": payload.get("description") or "",
            "start": {"dateTime": payload["start_utc"]},
            "end": {"dateTime": payload["end_utc"]},
            "reminders": reminders,
            "extendedProperties": {"private": {"goalcalEventId": str(payload["event_id"])}},
        }
        remote_uid = (payload.get("remote_uid") or "").strip()
        cal_q = quote(remote_calendar_id, safe="")
        if remote_uid:
            status, data = cls._api("PATCH", f"/calendars/{cal_q}/events/{quote(remote_uid, safe='')}", secrets_dict, body=body)
            if status in (404, 410):
                remote_uid = ""
            elif status >= 400:
                raise SyncProviderError(f"Google events.patch failed (HTTP {status})")
        if not remote_uid:
            status, data = cls._api("POST", f"/calendars/{cal_q}/events", secrets_dict, body=body)
            if status >= 400:
                raise SyncProviderError(f"Google events.insert failed (HTTP {status})")
        return PushResult(
            remote_uid=str(data.get("id") or remote_uid),
            remote_etag=str(data.get("etag") or ""),
        )

    @classmethod
    def push_delete(cls, remote_calendar_id: str, secrets_dict: dict, remote_uid: str) -> None:
        cal_q = quote(remote_calendar_id, safe="")
        status, _ = cls._api("DELETE", f"/calendars/{cal_q}/events/{quote(remote_uid, safe='')}", secrets_dict)
        if status not in (200, 204, 404, 410):
            raise SyncProviderError(f"Google events.delete failed (HTTP {status})")

    @classmethod
    def watch(
        cls,
        remote_calendar_id: str,
        secrets_dict: dict,
        *,
        callback_url: str,
        channel_id: str,
        token: str,
    ) -> tuple[str, datetime | None]:
        cal_q = quote(remote_calendar_id, safe="")
        status, data = cls._api(
            "POST",
            f"/calendars/{cal_q}/events/watch",
            secrets_dict,
            body={"id": channel_id, "type": "web_hook", "address": callback_url, "token": token},
        )
        if status >= 400:
            raise SyncProviderError(f"Google watch registration failed (HTTP {status})")
        exp_ms = data.get("expiration")
        expires_at = datetime.fromtimestamp(int(exp_ms) / 1000.0, tz=UTC).replace(tzinfo=None) if exp_ms else None
        return str(data.get("resourceId") or ""), expires_at


# ---------------------------------------------------------------------------
# Microsoft Outlook / Graph (OAuth 2.0 + Delta Query + Subscriptions)
# ---------------------------------------------------------------------------


def _parse_outlook_datetime(part: dict, tz: str) -> datetime:
    raw = str(part.get("dateTime") or "")
    zone_name = str(part.get("timeZone") or "UTC")
    if "." in raw:
        raw = raw.split(".", 1)[0]
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        try:
            dt = dt.replace(tzinfo=UTC if zone_name.upper() == "UTC" else ZoneInfo(zone_name))
        except Exception:
            dt = dt.replace(tzinfo=ZoneInfo(tz))
    return _utc_naive(dt)


class OutlookCalendarAdapter:
    provider = "outlook"

    @staticmethod
    def _credentials() -> tuple[str, str, str]:
        cid = os.getenv("MICROSOFT_CLIENT_ID", "").strip()
        secret = os.getenv("MICROSOFT_CLIENT_SECRET", "").strip()
        tenant = os.getenv("MICROSOFT_TENANT_ID", "common").strip() or "common"
        if not cid or not secret:
            raise SyncProviderError("Microsoft Outlook OAuth is not configured on this server")
        return cid, secret, tenant

    @classmethod
    def authorize_url(cls, *, redirect_uri: str, state: str, code_challenge: str) -> str:
        cid, _, tenant = cls._credentials()
        params = {
            "client_id": cid,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "query",
            "scope": OUTLOOK_SCOPES,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"https://login.microsoftonline.com/{quote(tenant)}/oauth2/v2.0/authorize?{urlencode(params)}"

    @classmethod
    def exchange_code(cls, *, code: str, redirect_uri: str, code_verifier: str) -> tuple[dict, datetime | None]:
        cid, secret, tenant = cls._credentials()
        token_url = f"https://login.microsoftonline.com/{quote(tenant)}/oauth2/v2.0/token"
        body = urlencode({
            "client_id": cid,
            "client_secret": secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
            "scope": OUTLOOK_SCOPES,
        }).encode("utf-8")
        status, _, raw = _request("POST", token_url, headers={"Content-Type": "application/x-www-form-urlencoded"}, body=body)
        if status >= 400:
            raise SyncAuthError("Outlook OAuth token exchange failed")
        data = json.loads(raw.decode("utf-8"))
        expires_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=int(data.get("expires_in", 3600)))
        return {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", ""),
            "expires_at": expires_at.isoformat(),
        }, expires_at

    @classmethod
    def ensure_fresh_token(cls, secrets_dict: dict) -> tuple[dict, datetime | None, bool]:
        exp_raw = secrets_dict.get("expires_at")
        now_naive = datetime.now(UTC).replace(tzinfo=None)
        if exp_raw:
            try:
                exp_dt = datetime.fromisoformat(str(exp_raw).replace("Z", ""))
                if exp_dt - timedelta(seconds=120) > now_naive and secrets_dict.get("access_token"):
                    return secrets_dict, exp_dt, False
            except ValueError:
                pass
        refresh = secrets_dict.get("refresh_token")
        if not refresh:
            if secrets_dict.get("access_token"):
                return secrets_dict, None, False
            raise SyncAuthError("Outlook authorization expired; reconnect required")
        cid, secret, tenant = cls._credentials()
        token_url = f"https://login.microsoftonline.com/{quote(tenant)}/oauth2/v2.0/token"
        body = urlencode({
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
            "scope": OUTLOOK_SCOPES,
        }).encode("utf-8")
        status, _, raw = _request("POST", token_url, headers={"Content-Type": "application/x-www-form-urlencoded"}, body=body)
        if status in (400, 401, 403):
            raise SyncAuthError("Outlook refresh token expired or revoked")
        if status >= 400:
            raise SyncProviderError(f"Outlook token refresh failed (HTTP {status})")
        data = json.loads(raw.decode("utf-8"))
        expires_at = now_naive + timedelta(seconds=int(data.get("expires_in", 3600)))
        updated = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token") or refresh,
            "expires_at": expires_at.isoformat(),
        }
        return updated, expires_at, True

    @classmethod
    def _api(
        cls,
        method: str,
        path_or_url: str,
        secrets_dict: dict,
        *,
        body: dict | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict]:
        if path_or_url.startswith("https://"):
            parsed = urlsplit(path_or_url)
            if (parsed.hostname or "").lower() != "graph.microsoft.com":
                raise SyncProviderError("Invalid Microsoft Graph pagination link")
            url = path_or_url
        else:
            url = f"{OUTLOOK_GRAPH_BASE}{path_or_url}"
        headers = {"Authorization": f"Bearer {secrets_dict['access_token']}"}
        if extra_headers:
            headers.update(extra_headers)
        raw_body = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            raw_body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        status, _, raw = _request(method, url, headers=headers, body=raw_body)
        if status == 401:
            raise SyncAuthError("Outlook Graph token rejected (401)")
        parsed_json = json.loads(raw.decode("utf-8")) if raw else {}
        return status, parsed_json

    @classmethod
    def userinfo_email(cls, secrets_dict: dict) -> str:
        status, data = cls._api("GET", "/me?$select=mail,userPrincipalName", secrets_dict)
        if status == 200:
            return str(data.get("mail") or data.get("userPrincipalName") or "Outlook Account")[:200]
        return "Outlook Account"

    @classmethod
    def discover(cls, secrets_dict: dict, *, ensure_goalcal: bool = True) -> list[RemoteCalendar]:
        status, data = cls._api("GET", "/me/calendars?$top=50", secrets_dict)
        if status != 200:
            raise SyncProviderError(f"Outlook calendar discovery failed (HTTP {status})")
        calendars: list[RemoteCalendar] = []
        for item in data.get("value", []):
            name = str(item.get("name") or "Calendar").strip()[:120]
            can_edit = bool(item.get("canEdit", False))
            calendars.append(
                RemoteCalendar(
                    remote_id=str(item["id"]),
                    name=name,
                    color="#6b7f89",
                    writable=can_edit,
                    is_goalcal=name.lower() == "goalcal" and can_edit,
                )
            )
        if ensure_goalcal and not any(c.is_goalcal for c in calendars):
            c_status, created = cls._api("POST", "/me/calendars", secrets_dict, body={"name": "GoalCal"})
            if c_status in (200, 201) and created.get("id"):
                calendars.insert(
                    0,
                    RemoteCalendar(
                        remote_id=str(created["id"]),
                        name="GoalCal",
                        color="#6b7f89",
                        writable=True,
                        is_goalcal=True,
                    ),
                )
        return calendars

    @classmethod
    def pull(
        cls,
        remote_calendar_id: str,
        secrets_dict: dict,
        *,
        tz: str,
        now_naive: datetime,
        sync_token: str | None = None,
        all_day_mode: str = "show",
    ) -> PullResult:
        headers = {"Prefer": 'outlook.timezone="UTC"'}
        initial_path = (
            f"/me/calendars/{quote(remote_calendar_id, safe='')}/calendarView/delta?"
            + urlencode({
                "startDateTime": _utc_aware(now_naive - timedelta(days=30)).isoformat(),
                "endDateTime": _utc_aware(now_naive + timedelta(days=180)).isoformat(),
                "$expand": f"singleValueExtendedProperties($filter=id eq '{OUTLOOK_EXT_PROP_ID}')",
            })
        )
        next_url: str | None = sync_token or initial_path
        is_full = sync_token is None
        delta_link: str | None = None
        items: list[dict] = []

        while next_url:
            status, data = cls._api("GET", next_url, secrets_dict, extra_headers=headers)
            if status in (410, 404) and not is_full:
                is_full = True
                items.clear()
                next_url = initial_path
                continue
            if status != 200:
                raise SyncProviderError(f"Outlook delta query failed (HTTP {status})")
            items.extend(data.get("value", []))
            delta_link = data.get("@odata.deltaLink") or delta_link
            next_url = data.get("@odata.nextLink")

        events: list[RemoteEvent] = []
        for item in items:
            uid = str(item.get("id") or "").strip()
            if not uid:
                continue
            gc_id: int | None = None
            for prop in item.get("singleValueExtendedProperties") or []:
                if prop.get("id") == OUTLOOK_EXT_PROP_ID and str(prop.get("value", "")).isdigit():
                    gc_id = int(prop["value"])
                    break
            if "@removed" in item or item.get("isCancelled"):
                events.append(
                    RemoteEvent(
                        remote_uid=uid,
                        title=str(item.get("subject") or "Event"),
                        start_utc=now_naive,
                        end_utc=now_naive,
                        status="cancelled",
                        remote_etag=str(item.get("changeKey") or ""),
                        goalcal_event_id=gc_id,
                        cancelled=True,
                    )
                )
                continue
            start_info = item.get("start") or {}
            end_info = item.get("end") or {}
            if not start_info or not end_info:
                continue
            is_all_day = bool(item.get("isAllDay", False))
            if is_all_day and all_day_mode == "ignore":
                continue
            events.append(
                RemoteEvent(
                    remote_uid=uid,
                    title=str(item.get("subject") or "Outlook event").strip()[:200] or "Outlook event",
                    start_utc=_parse_outlook_datetime(start_info, tz),
                    end_utc=_parse_outlook_datetime(end_info, tz),
                    all_day=is_all_day,
                    status="confirmed",
                    remote_etag=str(item.get("changeKey") or ""),
                    goalcal_event_id=gc_id,
                    cancelled=False,
                )
            )
        return PullResult(events=events, sync_token=delta_link, full_sync=is_full)

    @classmethod
    def push_upsert(cls, remote_calendar_id: str, secrets_dict: dict, payload: dict) -> PushResult:
        reminder_minutes = _valid_reminder_minutes(payload)
        body: dict = {
            "subject": payload["title"],
            "body": {"contentType": "text", "content": payload.get("description") or ""},
            "start": {"dateTime": payload["start_utc"].replace("Z", ""), "timeZone": "UTC"},
            "end": {"dateTime": payload["end_utc"].replace("Z", ""), "timeZone": "UTC"},
            "isReminderOn": reminder_minutes is not None,
            "transactionId": f"goalcal-event-{payload['event_id']}",
            "singleValueExtendedProperties": [{"id": OUTLOOK_EXT_PROP_ID, "value": str(payload["event_id"])}],
        }
        if reminder_minutes is not None:
            body["reminderMinutesBeforeStart"] = reminder_minutes
        remote_uid = (payload.get("remote_uid") or "").strip()
        cal_q = quote(remote_calendar_id, safe="")
        if remote_uid:
            status, data = cls._api("PATCH", f"/me/calendars/{cal_q}/events/{quote(remote_uid, safe='')}", secrets_dict, body=body)
            if status in (404, 410):
                remote_uid = ""
            elif status >= 400:
                raise SyncProviderError(f"Outlook event update failed (HTTP {status})")
        if not remote_uid:
            status, data = cls._api("POST", f"/me/calendars/{cal_q}/events", secrets_dict, body=body)
            if status >= 400:
                raise SyncProviderError(f"Outlook event create failed (HTTP {status})")
        return PushResult(
            remote_uid=str(data.get("id") or remote_uid),
            remote_etag=str(data.get("changeKey") or ""),
        )

    @classmethod
    def push_delete(cls, remote_calendar_id: str, secrets_dict: dict, remote_uid: str) -> None:
        cal_q = quote(remote_calendar_id, safe="")
        status, _ = cls._api("DELETE", f"/me/calendars/{cal_q}/events/{quote(remote_uid, safe='')}", secrets_dict)
        if status not in (200, 204, 404, 410):
            raise SyncProviderError(f"Outlook event delete failed (HTTP {status})")

    @classmethod
    def watch(
        cls,
        remote_calendar_id: str,
        secrets_dict: dict,
        *,
        callback_url: str,
        client_state: str,
        now_naive: datetime,
    ) -> tuple[str, datetime | None]:
        exp_dt = _utc_aware(now_naive + timedelta(hours=60))
        status, data = cls._api(
            "POST",
            "/subscriptions",
            secrets_dict,
            body={
                "changeType": "created,updated,deleted",
                "notificationUrl": callback_url,
                "resource": f"me/calendars/{remote_calendar_id}/events",
                "expirationDateTime": exp_dt.isoformat(),
                "clientState": client_state,
            },
        )
        if status >= 400:
            raise SyncProviderError(f"Outlook subscription registration failed (HTTP {status})")
        return str(data.get("id") or ""), exp_dt.replace(tzinfo=None)
