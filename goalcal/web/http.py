"""Same-origin web API. Authentication, validation and atomic replay live here."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from functools import partial
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import event as sa_event, select
from sqlalchemy.exc import OperationalError, IntegrityError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, FileResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from .. import auth, db, tenancy
from ..config import ROOT
from ..i18n import normalize_lang, parse_accept_language, pick
from . import schemas
from .transport_models import WebRequest

COOKIE = 'goalcal_session'
LANG_COOKIE = 'goalcal_lang'


def _request_lang(request: Request) -> str:
    explicit = normalize_lang(request.headers.get('x-goalcal-lang'))
    if explicit:
        return explicit
    cookie_lang = normalize_lang(request.cookies.get(LANG_COOKIE))
    if cookie_lang:
        return cookie_lang
    return parse_accept_language(request.headers.get('accept-language'), default='zh')


def _effective_lang(request_lang: str | None, session, user_id: int) -> str:
    explicit = normalize_lang(request_lang)
    if explicit:
        return explicit
    from .. import memory
    prefs = memory.list_preferences(session, user_id, scope='ui')
    row = next((p for p in prefs if p.key == 'lang'), None)
    if row:
        try:
            user_lang = normalize_lang(json.loads(row.value))
            if user_lang:
                return user_lang
        except ValueError:
            pass
    return 'zh'


@contextmanager
def scoped(token: str, *, write=False, principal=None):
    """Acquire SQLite's cross-process writer lock before reading mutable state.

    ORM tenant filtering is retained; explicit SQL is limited to the connection
    transaction command. Domain services cannot commit or use raw SQL.
    """
    if principal is None or write:
        principal = auth.authenticate(token)
    with db.get_engine().connect() as connection:
        if write:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        session = tenancy.TenantSession(
            bind=connection, expire_on_commit=False, join_transaction_mode='rollback_only',
            info={'principal': principal, 'token': token, 'wrote': write},
        )
        pending_upsert_ids: list[int] = []
        capturing_sync = False

        if write:
            from ..models import Event as _EventModel
            from ..domain.bidirectional_sync import enqueue_event_sync, is_exportable_goalcal_event

            def _on_before_flush(sess, _ctx, _instances):
                nonlocal capturing_sync
                if capturing_sync:
                    return
                capturing_sync = True
                try:
                    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
                    suppress = sess.info.get('sync_suppress_outbox_source_id')
                    for obj in list(sess.deleted):
                        if isinstance(obj, _EventModel) and obj.user_id == principal.user_id and is_exportable_goalcal_event(obj):
                            enqueue_event_sync(
                                sess,
                                principal.user_id,
                                obj,
                                action='delete',
                                now_naive=now_naive,
                                suppress_source_id=suppress,
                            )
                finally:
                    capturing_sync = False

            def _on_after_flush(sess, _ctx):
                if capturing_sync:
                    return
                for obj in sess.new | sess.dirty:
                    if isinstance(obj, _EventModel) and obj.user_id == principal.user_id and obj.id is not None:
                        if is_exportable_goalcal_event(obj) and obj.id not in pending_upsert_ids:
                            pending_upsert_ids.append(obj.id)

            sa_event.listen(session, 'before_flush', _on_before_flush)
            sa_event.listen(session, 'after_flush', _on_after_flush)

        ctx = tenancy.current_session.set(session)
        try:
            yield session, principal
            if write:
                session.flush()
                if pending_upsert_ids:
                    from ..models import Event as _EventModel
                    from ..domain.bidirectional_sync import enqueue_event_sync

                    capturing_sync = True
                    try:
                        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
                        suppress = session.info.get('sync_suppress_outbox_source_id')
                        for eid in pending_upsert_ids:
                            ev = session.get(_EventModel, eid)
                            if ev is not None:
                                enqueue_event_sync(
                                    session,
                                    principal.user_id,
                                    ev,
                                    now_naive=now_naive,
                                    suppress_source_id=suppress,
                                )
                        session.flush()
                    finally:
                        capturing_sync = False
                auth.authenticate(token)
                connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            tenancy.current_session.reset(ctx)
            session.close()
            if connection.in_transaction():
                connection.rollback()


def fingerprint(method, path, body):
    raw = json.dumps([method, path, body], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def replay(session, uid, key, digest, *, lang: str = 'zh'):
    from .core import ApiError
    row = session.scalar(select(WebRequest).where(WebRequest.user_id == uid, WebRequest.key == key))
    if row:
        if row.fingerprint != digest:
            raise ApiError(
                'idempotency_conflict',
                pick(lang, '该操作编号已用于不同请求，请刷新后重试。', 'This idempotency key was used for a different request. Refresh and retry.'),
            )
        return json.loads(row.result_json)
    return None


def prepare(token, action, resource, body, *, lang: str | None = None):
    """External model / network requests run before the write lock, authenticated and metered."""
    from .core import ApiError
    if action == 'connect_caldav':
        from ..domain.sync_providers import CalDavAdapter, SyncAuthError, SyncProviderError
        from ..safe_fetch import FetchError
        from .calendar_sync import _localized_fetch_error

        with scoped(token) as (session, principal):
            resolved_lang = _effective_lang(lang, session, principal.user_id)
        try:
            server_url = CalDavAdapter.resolve_server_url(body.get('preset', 'icloud'), body.get('server_url'))
            discovered = CalDavAdapter.discover(
                server_url,
                body['username'].strip(),
                body['password'],
                ensure_goalcal=bool(body.get('enable_two_way', True)),
            )
            return {**body, '_discovered': discovered}
        except SyncAuthError as exc:
            raise ApiError('caldav_auth_failed', _localized_fetch_error(resolved_lang, str(exc)), 422) from None
        except (FetchError, SyncProviderError, ValueError) as exc:
            raise ApiError('caldav_connect_failed', _localized_fetch_error(resolved_lang, str(exc)), 422) from None

    if action == 'refresh_calendar_connection':
        from ..crypto import CredentialError, decrypt_secrets
        from ..domain.sync_providers import (
            CalDavAdapter,
            GoogleCalendarAdapter,
            OutlookCalendarAdapter,
            SyncAuthError,
            SyncProviderError,
        )
        from ..models import CalendarConnection
        from ..safe_fetch import FetchError
        from .calendar_sync import _localized_fetch_error

        with scoped(token) as (session, principal):
            resolved_lang = _effective_lang(lang, session, principal.user_id)
            tz = principal.tz
            conn = session.get(CalendarConnection, int(resource))
            if conn is None or conn.user_id != principal.user_id:
                raise ApiError('not_found', pick(resolved_lang, '记录不存在', 'Record not found'), 404)
            conn_provider = conn.provider
            conn_server = conn.server_url
            conn_user = conn.username
            try:
                secrets_dict = decrypt_secrets(conn.encrypted_secrets)
            except CredentialError as exc:
                raise ApiError('reauth_required', _localized_fetch_error(resolved_lang, str(exc)), 422) from None

        try:
            updated_secrets = None
            updated_expires = None
            if conn_provider == 'caldav':
                discovered = CalDavAdapter.discover(
                    conn_server,
                    conn_user,
                    str(secrets_dict.get('password') or ''),
                    ensure_goalcal=True,
                )
            elif conn_provider == 'google':
                active_secrets, exp, mutated = GoogleCalendarAdapter.ensure_fresh_token(secrets_dict)
                if mutated:
                    updated_secrets, updated_expires = active_secrets, exp
                discovered = ('', GoogleCalendarAdapter.discover(active_secrets, tz=tz, ensure_goalcal=True))
            elif conn_provider == 'outlook':
                active_secrets, exp, mutated = OutlookCalendarAdapter.ensure_fresh_token(secrets_dict)
                if mutated:
                    updated_secrets, updated_expires = active_secrets, exp
                discovered = ('', OutlookCalendarAdapter.discover(active_secrets, ensure_goalcal=True))
            else:
                raise SyncProviderError(f'Unsupported provider: {conn_provider}')
            return {
                **body,
                '_discovered': discovered,
                '_updated_secrets': updated_secrets,
                '_updated_expires': updated_expires,
            }
        except SyncAuthError as exc:
            return {**body, '_auth_error': str(exc)}
        except (FetchError, SyncProviderError, ValueError) as exc:
            raise ApiError('refresh_failed', _localized_fetch_error(resolved_lang, str(exc)), 422) from None

    if action in {'create_calendar_source', 'sync_calendar_source'}:
        from icalendar import Calendar as ICal
        from ..crypto import CredentialError, decrypt_secrets
        from ..domain.bidirectional_sync import pull_native_for_source
        from ..domain.calendar_sync import (
            MAX_SOURCES_PER_USER,
            fetch_source_content,
            get_provider,
            release_source,
            try_acquire_source,
        )
        from ..domain.sync_providers import SyncAuthError, SyncProviderError
        from ..models import CalendarConnection, CalendarSource
        from ..safe_fetch import FetchError
        from .calendar_sync import _localized_fetch_error

        with scoped(token) as (session, principal):
            resolved_lang = _effective_lang(lang, session, principal.user_id)
            tz = principal.tz
            if action == 'create_calendar_source':
                provider_name = body.get('provider', 'ics_url')
                try:
                    normalized_url = get_provider(provider_name).normalize_url(body.get('url', ''))
                except (FetchError, ValueError) as exc:
                    raise ApiError('invalid_url', _localized_fetch_error(resolved_lang, str(exc)), 422) from None
                existing = [
                    s for s in session.scalars(select(CalendarSource).where(CalendarSource.user_id == principal.user_id))
                    if s.provider == 'ics_url'
                ]
                if len(existing) >= MAX_SOURCES_PER_USER:
                    raise ApiError(
                        'limit_exceeded',
                        pick(
                            resolved_lang,
                            f'最多可添加 {MAX_SOURCES_PER_USER} 个外部 ICS 订阅源。',
                            f'You can connect up to {MAX_SOURCES_PER_USER} external ICS subscriptions.',
                        ),
                        422,
                    )
                if any(s.url == normalized_url for s in existing):
                    raise ApiError(
                        'duplicate_source',
                        pick(resolved_lang, '这个日历订阅链接已经添加过了。', 'This calendar subscription URL is already connected.'),
                        409,
                    )
                target_provider, target_url, source_id = provider_name, normalized_url, None
                native_ctx = None
            else:
                source = session.get(CalendarSource, int(resource))
                if source is None or source.user_id != principal.user_id:
                    raise ApiError('not_found', pick(resolved_lang, '记录不存在', 'Record not found'), 404)
                target_provider, target_url, source_id = source.provider, source.url, source.id
                native_ctx = None
                if target_provider in {'caldav', 'google', 'outlook'}:
                    conn = session.get(CalendarConnection, source.connection_id) if source.connection_id else None
                    if conn is None or conn.user_id != principal.user_id:
                        raise ApiError('not_found', pick(resolved_lang, '关联日历账号不存在', 'Linked account not found'), 404)
                    try:
                        secrets_dict = decrypt_secrets(conn.encrypted_secrets)
                    except CredentialError as exc:
                        return {**body, '_fetch_error': str(exc), '_auth_failed': True}
                    native_ctx = {
                        'conn_id': conn.id,
                        'conn_server_url': conn.server_url,
                        'conn_username': conn.username,
                        'secrets_dict': secrets_dict,
                        'remote_calendar_id': source.remote_calendar_id,
                        'sync_token': source.sync_token,
                        'all_day_mode': source.all_day_mode or 'ignore',
                        'tz': tz,
                    }

        if action == 'create_calendar_source':
            try:
                fetched = fetch_source_content(target_provider, target_url)
                if fetched.content is None:
                    raise ValueError('Empty calendar response')
                ICal.from_ical(fetched.content)
            except (FetchError, ValueError) as exc:
                raise ApiError('sync_fetch_failed', _localized_fetch_error(resolved_lang, str(exc)), 422) from None
            return {**body, '_normalized_url': target_url, '_fetched': fetched}

        assert source_id is not None
        if not try_acquire_source(source_id):
            raise ApiError(
                'sync_in_progress',
                pick(resolved_lang, '该日历正在同步中，请稍候再试。', 'This calendar is already syncing. Try again in a moment.'),
                409,
            )
        try:
            if native_ctx is not None:
                now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
                try:
                    pull_res, updated_secrets, updated_expires = pull_native_for_source(
                        target_provider,
                        conn_server_url=native_ctx['conn_server_url'],
                        conn_username=native_ctx['conn_username'],
                        secrets_dict=native_ctx['secrets_dict'],
                        source_url=target_url,
                        remote_calendar_id=native_ctx['remote_calendar_id'],
                        sync_token=native_ctx['sync_token'],
                        all_day_mode=native_ctx['all_day_mode'],
                        tz=native_ctx['tz'],
                        now_naive=now_naive,
                    )
                    return {
                        **body,
                        '_native_pull': pull_res,
                        '_conn_id': native_ctx['conn_id'],
                        '_updated_secrets': updated_secrets,
                        '_updated_expires': updated_expires,
                    }
                except SyncAuthError as exc:
                    return {**body, '_fetch_error': str(exc), '_auth_failed': True}
                except (FetchError, SyncProviderError, ValueError) as exc:
                    return {**body, '_fetch_error': str(exc)}
            fetched = fetch_source_content(target_provider, target_url)
            return {**body, '_fetched': fetched}
        except (FetchError, ValueError) as exc:
            return {**body, '_fetch_error': str(exc)}
        finally:
            release_source(source_id)

    if action not in {'intake', 'draft', 'assistant_turn', 'refill', 'update_step', 'checkin_execution', 'checkin_event'}:
        return body
    with scoped(token) as (session, principal):
        resolved_lang = _effective_lang(lang, session, principal.user_id)
        now = datetime.now(timezone.utc)
        from zoneinfo import ZoneInfo
        local = now.astimezone(ZoneInfo(principal.tz))
        if action == 'intake':
            from datetime import timedelta
            from .availability import intervals
            from .core import Service
            from ..llm.intake import evaluate_goal_intake
            try:
                deadline = date.fromisoformat(body['deadline']) if body.get('deadline') else None
            except ValueError:
                raise ApiError('invalid_deadline', pick(resolved_lang, '请填写有效的截止日期。', 'Please enter a valid deadline.'), 422) from None
            if deadline and deadline < local.date():
                raise ApiError('invalid_deadline', pick(resolved_lang, '截止日期不能在过去。', 'Deadline cannot be in the past.'), 422)
            svc = Service(session, principal.user_id, principal.tz, now, lang=resolved_lang)
            avail_cfg = svc._availability()
            weekly_min = 0
            for offset in range(7):
                day_dt = local + timedelta(days=offset)
                avail_spans = intervals(day_dt, avail_cfg.get('available_windows', []))
                prot_spans = intervals(day_dt, avail_cfg.get('protected_windows', []))
                for a, b in avail_spans:
                    mins = int((b - a).total_seconds() // 60)
                    for pa, pb in prot_spans:
                        ov = min(b, pb) - max(a, pa)
                        if ov.total_seconds() > 0:
                            mins -= int(ov.total_seconds() // 60)
                    weekly_min += max(0, mins)
            intake = evaluate_goal_intake(
                body['description'],
                deadline,
                today=local.date(),
                tz=principal.tz,
                weekly_available_minutes=weekly_min or None,
                lang=resolved_lang,
                use_llm=True,
            )
            return {**body, '_intake': intake}
        if action == 'assistant_turn':
            from .assistant import prepare_turn
            return {**body, '_decision': prepare_turn(session, principal, body, now, lang=resolved_lang)}
        if action in {'refill', 'update_step', 'checkin_execution', 'checkin_event'}:
            from ..agent import plan_refill
            from ..domain import goals as goal_service
            from ..models import Event, Goal, Step
            from .models import WebExecution
            goal = completing = None
            if action == 'refill' and resource is not None:
                candidate = session.get(Goal, int(resource))
                if candidate and candidate.status == 'active' and not any(s.status == 'todo' for s in candidate.steps):
                    goal = candidate
            elif action == 'update_step' and body.get('status') == 'done' and resource is not None:
                step = session.get(Step, int(resource))
                if step and step.status != 'done':
                    goal, completing = step.goal, step
            elif action == 'checkin_execution' and body.get('outcome') == 'done' and resource is not None:
                execution = session.get(WebExecution, resource)
                if execution and execution.status == 'running' and execution.step_id is not None:
                    step = session.get(Step, execution.step_id)
                    if step:
                        goal, completing = step.goal, step
            elif action == 'checkin_event' and body.get('outcome') == 'done' and resource is not None:
                event = session.get(Event, int(resource))
                if event and event.step_id is not None:
                    step = session.get(Step, event.step_id)
                    if step:
                        goal, completing = step.goal, step
            if goal and goal.status == 'active':
                remaining_todo = [s for s in goal.steps if s.status == 'todo' and s is not completing]
                remaining_phases = set(goal_service.outline_phases(goal)) - {s.milestone for s in goal.steps}
                if not remaining_todo and remaining_phases:
                    defs = plan_refill(session, principal.user_id, goal, local, use_llm=True, completing=completing, lang=resolved_lang)
                    return {**body, '_refill_defs': defs}
            return body
        from ..agent import prepare_goal_draft
        from ..domain.planner import GoalBrief
        try:
            deadline = date.fromisoformat(body['deadline']) if body.get('deadline') else None
        except ValueError:
            raise ApiError('invalid_deadline', pick(resolved_lang, '请填写有效的截止日期。', 'Please enter a valid deadline.'), 422) from None
        if deadline and deadline < local.date():
            raise ApiError('invalid_deadline', pick(resolved_lang, '截止日期不能在过去。', 'Deadline cannot be in the past.'), 422)
        raw_text = (body.get('raw') or body['title']).strip()
        draft = prepare_goal_draft(session, principal.user_id,
                                  GoalBrief(
                                      body['title'],
                                      raw_text,
                                      deadline=deadline,
                                      level=(body.get('level') or '').strip(),
                                      target=(body.get('target') or '').strip(),
                                  ),
                                  local, principal.tz, lang=resolved_lang,
                                  instruction=(body.get('instruction') or '').strip(),
                                  current_steps=body.get('current_steps'),
                                  current_outline=body.get('current_outline'))
        return {**body, '_prepared': {'title': draft.brief.title,
                    'deadline': draft.deadline.isoformat() if draft.deadline else None,
                    'outline': draft.outline, 'steps': draft.step_defs, 'source': draft.source,
                    'draft_id': draft.draft_id}}


def _dispatch_read(svc, action, resource, query):
    if action == 'goals':
        return svc.goals(include_archived=query.get('include_archived') in {'1', 'true'})
    if action in {'me', 'today', 'preferences'}:
        return getattr(svc, action)()
    if action == 'goal':
        return svc.goal(int(resource), include_archived=query.get('include_archived') in {'1', 'true'})
    if action == 'events':
        return svc.events(query.get('start'), query.get('end'))
    if action == 'habits':
        return svc.habits.list()
    if action == 'habit':
        return svc.habits.detail(int(resource), query)
    if action == 'habit_slots':
        return svc.habits.slots(int(resource), query)
    if action == 'calendar_sync':
        return svc.calendar_sync.state()
    return None


def execute(token, action, resource, body, query, key=None, digest=None, lang: str | None = None, *, principal=None):
    from .core import Service, ApiError
    roll_on_read = False
    if key:
        with scoped(token, principal=principal) as (session, principal):
            resolved_lang = _effective_lang(lang, session, principal.user_id)
            cached = replay(session, principal.user_id, key, digest, lang=resolved_lang)
            if cached is not None:
                return cached
        body = prepare(token, action, resource, body, lang=lang)
    elif action in {'today', 'events', 'habits', 'habit', 'habit_slots'}:
        with scoped(token, principal=principal) as (session, principal):
            resolved_lang = _effective_lang(lang, session, principal.user_id)
            now = datetime.now(timezone.utc)
            svc = Service(session, principal.user_id, principal.tz, now, lang=resolved_lang)
            if not svc.habits.needs_roll():
                return _dispatch_read(svc, action, resource, query)
            roll_on_read = True
    with scoped(token, write=bool(key) or roll_on_read, principal=principal) as (session, principal):
        resolved_lang = _effective_lang(lang, session, principal.user_id)
        if key:
            cached = replay(session, principal.user_id, key, digest, lang=resolved_lang)
            if cached is not None:
                return cached
        now = datetime.now(timezone.utc)
        svc = Service(session, principal.user_id, principal.tz, now, lang=resolved_lang)
        if key or roll_on_read:
            svc.habits.roll()
        read_result = _dispatch_read(svc, action, resource, query)
        if read_result is not None:
            result = read_result
        elif action in {'create_habit', 'preview_habit'}:
            result = svc.habits.create(body) if action == 'create_habit' else svc.habits.preview(body)
        elif action in {'update_habit', 'habit_status', 'habit_action', 'delete_habit'}:
            handler = {'update_habit': svc.habits.update, 'habit_status': svc.habits.status,
                       'habit_action': svc.habits.act, 'delete_habit': svc.habits.delete}[action]
            result = handler(int(resource), body)
        elif action == 'intake':
            result = body['_intake']
        elif action in {'draft', 'create_goal', 'create_event', 'save_preferences', 'update_me'}:
            result = getattr(svc, action)(body)
        elif action == 'undo':
            result = svc.undo(resource)
        elif action in {'accept', 'checkin_execution'}:
            result = getattr(svc, action)(resource, body)
        elif action in {'refill','replan_goal','update_goal','delete_goal','update_step','move_event','cancel_event','propose','schedule_step','start_event','start_step','checkin_event'}:
            result = getattr(svc, action)(int(resource), body)
        elif action in {'assistant','assistant_turn','assistant_clear'}:
            from .assistant import clear_thread, read_state, turn
            result = (
                read_state(svc)
                if action == 'assistant'
                else clear_thread(svc, body)
                if action == 'assistant_clear'
                else turn(svc, body)
            )
        elif action == 'import_calendar':
            from ..domain.ics_import import import_ics
            pad = timedelta(minutes=svc._availability()['buffer_minutes'])
            reserved = [(event.start_utc - pad, event.end_utc + pad) for event in svc._events()
                        if event.kind == 'habit' and event.status in ('planned', 'moved') and event.end_utc > svc.now_naive]
            imported = import_ics(session, principal.user_id, body['ics'].encode(), principal.tz,
                                  now=svc.now_naive,
                                  slot_allowed=lambda start, end: not any(start < hi and end > lo for lo, hi in reserved))
            result = {
                'message': pick(
                    resolved_lang,
                    f'已导入 {imported.added} 条、更新 {imported.updated} 条日程，跳过 {imported.skipped} 条。',
                    f'Imported {imported.added} event(s), updated {imported.updated}, skipped {imported.skipped}.',
                ),
                'count': imported.added + imported.updated, 'skipped': imported.skipped,
            }
        elif action == 'update_calendar_feed':
            result = svc.calendar_sync.update_feed(body)
        elif action == 'rotate_calendar_feed':
            result = svc.calendar_sync.rotate_feed(body)
        elif action == 'connect_caldav':
            result = svc.calendar_sync.connect_caldav(body)
        elif action == 'refresh_calendar_connection':
            result = svc.calendar_sync.refresh_connection(int(resource), body)
        elif action == 'delete_calendar_connection':
            result = svc.calendar_sync.delete_connection(int(resource), body)
        elif action == 'create_calendar_source':
            result = svc.calendar_sync.create_source(body)
        elif action == 'update_calendar_source':
            result = svc.calendar_sync.update_source(int(resource), body)
        elif action == 'sync_calendar_source':
            result = svc.calendar_sync.sync_source(int(resource), body)
        elif action == 'delete_calendar_source':
            result = svc.calendar_sync.delete_source(int(resource), body)
        elif action == 'metrics':
            from .. import metrics
            result = {name: getattr(metrics, name)(session, principal.user_id, now)
                      for name in ('acceptance_rate','recovery_rate_72h','progress_rate')}
        elif action == 'feedback':
            from .. import feedback
            text = body['message'].strip()
            if not 5 <= len(text) <= 2000:
                raise ApiError(
                    'invalid_input',
                    pick(resolved_lang, '请填写 5–2000 字的反馈内容。', 'Please enter 5–2000 characters of feedback.'),
                    422,
                )
            fid = feedback.submit(session, body['category'], text, body.get('operation_id'))
            result = {
                'id': fid,
                'message': pick(resolved_lang, f'已收到反馈（#{fid}），感谢！', f'Feedback saved (#{fid}). Thank you!'),
            }
        else:
            raise ApiError('not_found', pick(resolved_lang, '没有找到这个操作。', 'Action not found.'), 404)
        if key:
            session.add(WebRequest(user_id=principal.user_id, key=key, fingerprint=digest,
                                   result_json=json.dumps(result, ensure_ascii=False)))
        return result


def response(data, status=200):
    return JSONResponse(data, status_code=status, headers={
        'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff', 'Referrer-Policy':'same-origin',
    })


def error(code, message, status):
    return response({'error': {'code':code, 'message':message}}, status)


def valid_origin(request):
    if request.headers.get('sec-fetch-site') == 'cross-site':
        return False
    expected_scheme = request.url.scheme
    if request.client is None or request.client.host in {'127.0.0.1', '::1'}:
        forwarded = request.headers.get('x-forwarded-proto', '').split(',', 1)[0].strip().lower()
        if forwarded in {'http', 'https'}:
            expected_scheme = forwarded
    parsed = urlsplit(request.headers.get('origin', ''))
    return (parsed.scheme == expected_scheme and parsed.netloc.lower() == request.headers.get('host','').lower()
            and not parsed.path and not parsed.query and not parsed.fragment and not parsed.username)


async def endpoint(request: Request, *, action, schema=None):
    from .core import ApiError
    token = request.cookies.get(COOKIE)
    lang = _request_lang(request)
    try:
        principal = await run_in_threadpool(auth.authenticate, token)
        body = {}
        key = digest = None
        if request.method != 'GET':
            if not valid_origin(request):
                return error('origin_rejected', pick(lang, '请求来源无效，请从应用页面重试。', 'Invalid request origin; please retry from the app page.'), 403)
            if request.headers.get('content-type','').split(';')[0].lower() != 'application/json':
                return error('invalid_content_type', pick(lang, '请使用 JSON 请求。', 'Expected application/json request.'), 415)
            key = request.headers.get('idempotency-key','')
            if not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', key):
                return error('missing_idempotency_key', pick(lang, '缺少有效操作编号。', 'Missing valid Idempotency-Key.'), 422)
            limit = 6*1024*1024 if action == 'import_calendar' else 128*1024
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    return error('body_too_large', pick(lang, '内容过大。', 'Request payload is too large.'), 413)
                chunks.append(chunk)
            try:
                raw = json.loads(b''.join(chunks))
                body = (schema or schemas.Input).model_validate(raw).model_dump(exclude_none=True)
            except (ValueError, ValidationError, TypeError):
                return error('invalid_input', pick(lang, '输入格式有误，请检查必填项和时间。', 'Invalid input format; check required fields and times.'), 422)
            digest = fingerprint(request.method, request.url.path, body)
        result = await run_in_threadpool(execute, token, action, request.path_params.get('id'),
                                         body, dict(request.query_params), key, digest, lang,
                                         principal=principal)
        return response(result)
    except auth.AuthError:
        return error('unauthenticated', pick(lang, '登录已过期，请重新登录。', 'Your session has expired. Please log in again.'), 401)
    except tenancy.AccessDenied:
        return error('not_found', pick(lang, '内容不存在或不可访问。', 'Resource not found or inaccessible.'), 404)
    except ApiError as exc:
        return error(exc.code, exc.message, exc.status)
    except (ValueError, TypeError):
        return error('invalid_input', pick(lang, '输入无效，请检查日期、时间和内容。', 'Invalid input; please check dates, times, and fields.'), 422)
    except (OperationalError, IntegrityError):
        return error('write_conflict', pick(lang, '数据正在更新，请稍后使用同一操作编号重试。', 'Data is currently updating; retry shortly with the same key.'), 409)


SPECS = [
    ('/me','GET','me',None),('/me','PATCH','update_me',schemas.UpdateMe),
    ('/today','GET','today',None),('/goals','GET','goals',None),
    ('/habits','GET','habits',None),('/habits','POST','create_habit',schemas.CreateHabit),
    ('/habits/preview','POST','preview_habit',schemas.HabitPreview),
    ('/habits/{id:int}','GET','habit',None),('/habits/{id:int}/slots','GET','habit_slots',None),
    ('/habits/{id:int}','PATCH','update_habit',schemas.UpdateHabit),
    ('/habits/{id:int}','DELETE','delete_habit',schemas.Version),
    ('/habits/{id:int}/status','POST','habit_status',schemas.HabitStatus),
    ('/habits/{id:int}/occurrences','POST','habit_action',schemas.HabitAction),
    ('/goals/{id:int}','GET','goal',None),
    ('/goal-intake','POST','intake',schemas.IntakeInput),
    ('/goal-drafts','POST','draft',schemas.DraftInput),
    ('/goals/{id:int}/refill','POST','refill',schemas.Version),
    ('/goals/{id:int}/replan','POST','replan_goal',schemas.ReplanGoal),
    ('/goals','POST','create_goal',schemas.CreateGoal),('/goals/{id:int}','PATCH','update_goal',schemas.GoalEdit),
    ('/goals/{id:int}','DELETE','delete_goal',schemas.Version),
    ('/steps/{id:int}','PATCH','update_step',schemas.StepEdit),
    ('/calendar/events','GET','events',None),('/calendar/events','POST','create_event',schemas.CreateEvent),
    ('/events/{id:int}','PATCH','move_event',schemas.MoveEvent),
    ('/events/{id:int}/cancel','POST','cancel_event',schemas.Version),
    ('/steps/{id:int}/proposals','POST','propose',schemas.Propose),
    ('/steps/{id:int}/schedule','POST','schedule_step',schemas.ScheduleStep),
    ('/proposals/{id:str}/accept','POST','accept',schemas.Accept),
    ('/operations/{id:str}/undo','POST','undo',schemas.Input),
    ('/events/{id:int}/start','POST','start_event',schemas.Version),
    ('/steps/{id:int}/start','POST','start_step',schemas.Version),
    ('/execution-sessions/{id:str}/checkin','POST','checkin_execution',schemas.ExecutionCheckin),
    ('/events/{id:int}/checkin','POST','checkin_event',schemas.EventCheckin),
    ('/preferences/availability','GET','preferences',None),
    ('/preferences/availability','PUT','save_preferences',schemas.Availability),
    ('/assistant','GET','assistant',None),('/assistant/turns','POST','assistant_turn',schemas.Turn),
    ('/assistant/clear','POST','assistant_clear',schemas.Version),
    ('/calendar/import','POST','import_calendar',schemas.ImportCalendar),
    ('/calendar/sync','GET','calendar_sync',None),
    ('/calendar/sync/feed','PUT','update_calendar_feed',schemas.UpdateCalendarFeed),
    ('/calendar/sync/feed/rotate','POST','rotate_calendar_feed',schemas.RotateCalendarFeed),
    ('/calendar/sync/connections/caldav','POST','connect_caldav',schemas.ConnectCalDav),
    ('/calendar/sync/connections/{id:int}/refresh','POST','refresh_calendar_connection',schemas.SyncCalendarSource),
    ('/calendar/sync/connections/{id:int}','DELETE','delete_calendar_connection',schemas.Version),
    ('/calendar/sync/sources','POST','create_calendar_source',schemas.CreateCalendarSource),
    ('/calendar/sync/sources/{id:int}','PATCH','update_calendar_source',schemas.UpdateCalendarSource),
    ('/calendar/sync/sources/{id:int}/sync','POST','sync_calendar_source',schemas.SyncCalendarSource),
    ('/calendar/sync/sources/{id:int}','DELETE','delete_calendar_source',schemas.Version),
    ('/metrics','GET','metrics',None),
    ('/feedback','POST','feedback',schemas.SubmitFeedback),
]
api = Starlette(routes=[Route(path, partial(endpoint, action=action, schema=schema), methods=[method])
                        for path, method, action, schema in SPECS])


async def frontend(request):
    dist = (ROOT / 'frontend' / 'dist').resolve()
    assets_dir = (dist / 'assets').resolve()
    path = request.path_params.get('path', '')
    candidate = (dist / path).resolve()
    if not candidate.is_relative_to(dist):
        return PlainTextResponse('Not found', status_code=404)
    # Content-hashed Vite build assets under dist/assets/ contain no user data
    # and can be cached publicly at the browser and CDN edge without DB auth.
    if path.startswith('assets/') and candidate.is_relative_to(assets_dir) and candidate.is_file():
        return FileResponse(candidate, headers={
            'Cache-Control': 'public, max-age=31536000, immutable',
            'X-Content-Type-Options': 'nosniff',
            'Referrer-Policy': 'same-origin',
        })
    try:
        await run_in_threadpool(auth.authenticate, request.cookies.get(COOKIE))
    except auth.AuthError:
        return RedirectResponse('/_goalcal/login?next=/app/', status_code=303)
    if not path or not candidate.is_file():
        if Path(path).suffix:
            return PlainTextResponse('Not found', status_code=404)
        candidate = dist / 'index.html'
    if not candidate.is_file():
        return PlainTextResponse('Frontend not built. Run npm ci && npm run build in frontend/.', status_code=503)
    return FileResponse(candidate, headers={
        'Cache-Control': 'no-store' if candidate.name == 'index.html' else 'private, max-age=3600',
        'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        'X-Content-Type-Options': 'nosniff',
        'Referrer-Policy': 'same-origin',
    })
