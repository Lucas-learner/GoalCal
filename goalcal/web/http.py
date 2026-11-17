"""Same-origin web API. Authentication, validation and atomic replay live here."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
from functools import partial
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, IntegrityError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, FileResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route

from .. import auth, db, tenancy
from ..config import ROOT
from . import schemas
from .transport_models import WebRequest

COOKIE = 'goalcal_session'


@contextmanager
def scoped(token: str, *, write=False):
    """Acquire SQLite's cross-process writer lock before reading mutable state.

    ORM tenant filtering is retained; explicit SQL is limited to the connection
    transaction command. Domain services cannot commit or use raw SQL.
    """
    principal = auth.authenticate(token)
    with db.get_engine().connect() as connection:
        if write:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
        session = tenancy.TenantSession(
            bind=connection, expire_on_commit=False, join_transaction_mode='rollback_only',
            info={'principal': principal, 'token': token, 'wrote': write},
        )
        ctx = tenancy.current_session.set(session)
        try:
            yield session, principal
            if write:
                session.flush()
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


def replay(session, uid, key, digest):
    from .core import ApiError
    row = session.scalar(select(WebRequest).where(WebRequest.user_id == uid, WebRequest.key == key))
    if row:
        if row.fingerprint != digest:
            raise ApiError('idempotency_conflict', '该操作编号已用于不同请求，请刷新后重试。')
        return json.loads(row.result_json)
    return None


def prepare(token, action, resource, body):
    """External model requests run before the write lock, authenticated and metered."""
    from .core import ApiError
    if action not in {'draft', 'assistant_turn', 'refill', 'update_step', 'checkin_execution', 'checkin_event'}:
        return body
    with scoped(token) as (session, principal):
        now = datetime.now(timezone.utc)
        from zoneinfo import ZoneInfo
        local = now.astimezone(ZoneInfo(principal.tz))
        if action == 'assistant_turn':
            from .assistant import prepare_turn
            return {**body, '_decision': prepare_turn(session, principal, body, now)}
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
                if execution and execution.status == 'running':
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
                    defs = plan_refill(session, principal.user_id, goal, local, use_llm=True, completing=completing)
                    return {**body, '_refill_defs': defs}
            return body
        from ..agent import prepare_goal_draft
        from ..domain.planner import GoalBrief
        try:
            deadline = date.fromisoformat(body['deadline']) if body.get('deadline') else None
        except ValueError:
            raise ApiError('invalid_deadline', '请填写有效的截止日期。', 422) from None
        if deadline and deadline < local.date():
            raise ApiError('invalid_deadline', '截止日期不能在过去。', 422)
        draft = prepare_goal_draft(session, principal.user_id,
                                  GoalBrief(body['title'], body['title'], deadline=deadline),
                                  local, principal.tz)
        return {**body, '_prepared': {'title': draft.brief.title,
                    'deadline': draft.deadline.isoformat() if draft.deadline else None,
                    'outline': draft.outline, 'steps': draft.step_defs, 'source': draft.source,
                    'draft_id': draft.draft_id}}


def execute(token, action, resource, body, query, key=None, digest=None):
    from .core import Service, ApiError
    if key:
        with scoped(token) as (session, principal):
            cached = replay(session, principal.user_id, key, digest)
            if cached is not None:
                return cached
        body = prepare(token, action, resource, body)
    with scoped(token, write=bool(key)) as (session, principal):
        if key:
            cached = replay(session, principal.user_id, key, digest)
            if cached is not None:
                return cached
        now = datetime.now(timezone.utc)
        svc = Service(session, principal.user_id, principal.tz, now)
        if action == 'goals':
            result = svc.goals(include_archived=query.get('include_archived') in {'1', 'true'})
        elif action in {'me', 'today', 'preferences'}:
            result = getattr(svc, action)()
        elif action == 'goal':
            result = svc.goal(int(resource), include_archived=query.get('include_archived') in {'1', 'true'})
        elif action == 'events':
            result = svc.events(query.get('start'), query.get('end'))
        elif action in {'draft', 'create_goal', 'create_event', 'save_preferences'}:
            result = getattr(svc, action)(body)
        elif action == 'undo':
            result = svc.undo(resource)
        elif action in {'accept', 'checkin_execution'}:
            result = getattr(svc, action)(resource, body)
        elif action in {'refill','update_goal','delete_goal','update_step','move_event','cancel_event','propose','schedule_step','start_event','start_step','checkin_event'}:
            result = getattr(svc, action)(int(resource), body)
        elif action in {'assistant','assistant_turn'}:
            from .assistant import read_state, turn
            result = read_state(svc) if action == 'assistant' else turn(svc, body)
        elif action == 'import_calendar':
            from ..domain.ics_import import import_ics
            imported = import_ics(session, principal.user_id, body['ics'].encode(), principal.tz,
                                  now=now.replace(tzinfo=None))
            result = {'message': f'已导入 {imported.added} 条、更新 {imported.updated} 条日程，跳过 {imported.skipped} 条。',
                      'count': imported.added + imported.updated, 'skipped': imported.skipped}
        elif action == 'metrics':
            from .. import metrics
            result = {name: getattr(metrics, name)(session, principal.user_id, now)
                      for name in ('acceptance_rate','recovery_rate_72h','progress_rate')}
        else:
            raise ApiError('not_found', '没有找到这个操作。', 404)
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
    try:
        await run_in_threadpool(auth.authenticate, token)
        body = {}
        key = digest = None
        if request.method != 'GET':
            if not valid_origin(request):
                return error('origin_rejected', '请求来源无效，请从应用页面重试。', 403)
            if request.headers.get('content-type','').split(';')[0].lower() != 'application/json':
                return error('invalid_content_type','请使用 JSON 请求。',415)
            key = request.headers.get('idempotency-key','')
            if not re.fullmatch(r'[A-Za-z0-9_.:-]{8,128}', key):
                return error('missing_idempotency_key','缺少有效操作编号。',422)
            limit = 6*1024*1024 if action == 'import_calendar' else 128*1024
            chunks, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    return error('body_too_large','内容过大。',413)
                chunks.append(chunk)
            try:
                raw = json.loads(b''.join(chunks))
                body = (schema or schemas.Input).model_validate(raw).model_dump(exclude_none=True)
            except (ValueError, ValidationError, TypeError):
                return error('invalid_input','输入格式有误，请检查必填项和时间。',422)
            digest = fingerprint(request.method, request.url.path, body)
        result = await run_in_threadpool(execute, token, action, request.path_params.get('id'),
                                         body, dict(request.query_params), key, digest)
        return response(result)
    except auth.AuthError:
        return error('unauthenticated', '登录已过期，请重新登录。', 401)
    except tenancy.AccessDenied:
        return error('not_found', '内容不存在或不可访问。', 404)
    except ApiError as exc:
        return error(exc.code, exc.message, exc.status)
    except (ValueError, TypeError):
        return error('invalid_input','输入无效，请检查日期、时间和内容。',422)
    except (OperationalError, IntegrityError):
        return error('write_conflict','数据正在更新，请稍后使用同一操作编号重试。',409)


SPECS = [
    ('/me','GET','me',None),('/today','GET','today',None),('/goals','GET','goals',None),
    ('/goals/{id:int}','GET','goal',None),('/goal-drafts','POST','draft',schemas.DraftInput),
    ('/goals/{id:int}/refill','POST','refill',schemas.Version),
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
    ('/calendar/import','POST','import_calendar',schemas.ImportCalendar),('/metrics','GET','metrics',None),
]
api = Starlette(routes=[Route(path, partial(endpoint, action=action, schema=schema), methods=[method])
                        for path, method, action, schema in SPECS])


async def frontend(request):
    try:
        await run_in_threadpool(auth.authenticate, request.cookies.get(COOKIE))
    except auth.AuthError:
        return RedirectResponse('/_goalcal/login?next=/app/', status_code=303)
    dist = ROOT / 'frontend' / 'dist'
    path = request.path_params.get('path','')
    candidate = (dist / path).resolve()
    if not candidate.is_relative_to(dist.resolve()):
        return PlainTextResponse('Not found', status_code=404)
    if not path or not candidate.is_file():
        if Path(path).suffix:
            return PlainTextResponse('Not found', status_code=404)
        candidate = dist / 'index.html'
    if not candidate.is_file():
        return PlainTextResponse('Frontend not built. Run npm ci && npm run build in frontend/.', status_code=503)
    return FileResponse(candidate, headers={
        'Cache-Control': 'no-store' if candidate.name == 'index.html' else 'private, max-age=3600',
        'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        'X-Content-Type-Options':'nosniff', 'Referrer-Policy':'same-origin',
    })
