"""Authenticated contract tests: real SQLite, cookies, transactions and concurrent requests."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import uuid

import httpx2
import pytest
from sqlalchemy import select

from goalcal import auth, db, storage, tenancy, migrations
from goalcal.auth_proxy import app
from goalcal.domain.goals import create_goal
from goalcal.models import Event, Step, Checkin
from goalcal.web import http
from goalcal.web.core import Service, ApiError
from goalcal.web.models import WebProposal, WebExecution
from goalcal.web.transport_models import WebRequest, WebThread

ORIGIN='https://goalcal.test'
PASSWORD='long-api-test-password'


def test_web_readiness_and_retired_legacy_routes(accounts, tmp_path, monkeypatch):
    from goalcal import config
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setenv('GOALCAL_ROOT_APP', '1')
    monkeypatch.delenv('GOALCAL_LEGACY_UI', raising=False)

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=ORIGIN) as c:
            assert (await c.get('/_goalcal/health')).status_code == 503
            dist = tmp_path / 'frontend' / 'dist'
            dist.mkdir(parents=True)
            (dist / 'index.html').write_text('<div id="root"></div>')
            result = await c.get('/_goalcal/health')
            assert result.status_code == 200
            assert result.json() == {'status': 'ok', 'application': 'goalcal-web'}
            assert (await c.get('/_stcore/health')).status_code == 404
            assert (await c.get('/settings')).status_code == 404
            assert (await c.get('/', headers={'Accept': 'text/html'})).status_code == 200
    asyncio.run(run())


@pytest.fixture()
def accounts(tmp_path, monkeypatch):
    path = tmp_path/'api.db'
    monkeypatch.setattr(db, 'settings', replace(db.settings, db_url=f'sqlite:///{path}'))
    monkeypatch.setattr(storage, 'database_path', lambda url=None: path)
    monkeypatch.setattr(migrations, 'database_path', lambda url=None: path)
    db.init_db()
    ids=[]; tokens=[]
    for i in range(2):
        name='api_'+uuid.uuid4().hex[:10]
        ids.append(auth.create_user(name,PASSWORD,admin=True))
        tokens.append(auth.login(name,PASSWORD))
    return ids,tokens


def request(token, method, path, body=None, *, key=None, headers=None):
    async def run():
        h={'Origin':ORIGIN,'Idempotency-Key':key or uuid.uuid4().hex}
        h.update(headers or {})
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),base_url=ORIGIN,
                                      cookies={'goalcal_session':token or ''}) as c:
            return await c.request(method,'/api/v1'+path,json=body,headers=h)
    return asyncio.run(run())


def seed(token,title='雅思备考'):
    with tenancy.session_scope(token) as s:
        uid=s.info['principal'].user_id
        g=create_goal(s,uid,title,deadline=(datetime.now(timezone.utc)+timedelta(days=40)).date(),
                      step_defs=[{'title':'练习阅读','est_min':30,'milestone':'准备'}])
        return g.id,g.steps[0].id


def availability(token):
    value=request(token,'GET','/preferences/availability').json()
    value.update(available_windows=[{'days':list(range(7)),'start':'00:00','end':'24:00'}],
                 protected_windows=[],preferred_windows=[],buffer_minutes=0,max_goal_minutes_per_day=1440)
    result=request(token,'PUT','/preferences/availability',value)
    assert result.status_code==200,result.text


def test_cookie_origin_and_input_boundary(accounts):
    _,tokens=accounts
    assert request(None,'GET','/today').status_code==401
    assert request(tokens[0],'GET','/me').json()['id']==accounts[0][0]
    body={'title':'meeting','start':'2030-01-01T10:00:00Z','end':'2030-01-01T11:00:00Z'}
    assert request(tokens[0],'POST','/calendar/events',body,headers={'Origin':'https://evil.test'}).status_code==403
    assert request(tokens[0],'POST','/calendar/events',body,headers={'Origin':''}).status_code==403
    assert request(tokens[0],'POST','/calendar/events',{**body,'user_id':accounts[0][1]}).status_code==422
    assert request(tokens[0],'POST','/calendar/events',body,headers={'Idempotency-Key':''}).status_code==422
    assert request(tokens[0],'POST','/calendar/events',{**body,'start':'2030-01-01T10:00:00'}).status_code==422
    assert request(tokens[0],'GET','/calendar/events?start=2000-01-01T00:00:00Z&end=2030-01-01T00:00:00Z').status_code==422


def test_create_draft_goal_replay_and_cross_tenant(accounts):
    _,tokens=accounts
    r=request(tokens[0],'POST','/goal-drafts',{'title':'雅思备考','deadline':'2030-12-01'})
    assert r.status_code==200,r.text
    draft=r.json(); assert draft['steps']
    body={k:draft[k] for k in ('title','deadline','steps','version')};body['draft_id']=draft['id']
    key=uuid.uuid4().hex
    a=request(tokens[0],'POST','/goals',body,key=key)
    b=request(tokens[0],'POST','/goals',body,key=key)
    assert a.status_code==200,a.text
    assert a.json()==b.json()
    assert request(tokens[0],'POST','/goals',{**body,'title':'other'},key=key).status_code==409
    assert request(tokens[1],'POST','/goals',body).status_code==404
    gid=a.json()['goal']['id']
    assert request(tokens[1],'GET',f'/goals/{gid}').status_code==404
    assert 'state_json' not in a.text


def test_concurrent_replay_single_event(accounts):
    _,tokens=accounts
    body={'title':'会议','start':'2030-03-01T09:00:00Z','end':'2030-03-01T10:00:00Z'}
    key=uuid.uuid4().hex; digest=http.fingerprint('POST','/api/v1/calendar/events',body)
    def write(_):return http.execute(tokens[0],'create_event',None,body,{},key,digest)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(write,range(4)))
    assert all(x==results[0] for x in results)
    with http.scoped(tokens[0]) as (s,p):
        assert len(list(s.scalars(select(Event))))==1
        assert len(list(s.scalars(select(WebRequest))))==1


def test_schedule_supersede_accept_undo(accounts):
    _,tokens=accounts; gid,sid=seed(tokens[0]);availability(tokens[0])
    a=request(tokens[0],'POST',f'/steps/{sid}/proposals',{}).json()
    b=request(tokens[0],'POST',f'/steps/{sid}/proposals',{}).json()
    assert a['candidates'] and b['candidates']
    assert request(tokens[0],'POST',f'/proposals/{a["id"]}/accept',{'version':a['version'],'index':0}).status_code==409
    assert request(tokens[1],'POST',f'/proposals/{b["id"]}/accept',{'version':b['version'],'index':0}).status_code==404
    r=request(tokens[0],'POST',f'/proposals/{b["id"]}/accept',{'version':b['version'],'index':0})
    assert r.status_code==200,r.text
    event=r.json()['event'];op=r.json()['operation_id']
    assert request(tokens[1],'POST',f'/operations/{op}/undo',{}).status_code==404
    assert request(tokens[0],'POST',f'/operations/{op}/undo',{}).status_code==200
    assert request(tokens[0],'POST',f'/operations/{op}/undo',{}).status_code==409
    with http.scoped(tokens[0]) as (s,p):assert s.get(Event,event['id']).status=='skipped'


def test_only_one_concurrent_accept(accounts):
    _,tokens=accounts; _,sid=seed(tokens[0]);availability(tokens[0])
    p=request(tokens[0],'POST',f'/steps/{sid}/proposals',{}).json()
    body={'version':p['version'],'index':0}
    def accept(_):return request(tokens[0],'POST',f'/proposals/{p["id"]}/accept',body).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(accept,range(2)))
    assert sorted(results)==[200,409]


def test_start_refresh_checkin_deduplication(accounts):
    _,tokens=accounts; gid,sid=seed(tokens[0]);availability(tokens[0])
    step=request(tokens[0],'GET',f'/goals/{gid}').json()['steps'][0]
    r=request(tokens[0],'POST',f'/steps/{sid}/start',{'version':step['version']})
    assert r.status_code==200,r.text
    execution=r.json()['execution']
    today=request(tokens[0],'GET','/today').json()
    assert today['execution']['id']==execution['id']
    assert not today['recovery']
    key=uuid.uuid4().hex;body={'version':execution['version'],'outcome':'done'}
    a=request(tokens[0],'POST',f'/execution-sessions/{execution["id"]}/checkin',body,key=key)
    b=request(tokens[0],'POST',f'/execution-sessions/{execution["id"]}/checkin',body,key=key)
    assert a.status_code==200,a.text
    assert a.json()==b.json()
    assert request(tokens[0],'POST',f'/execution-sessions/{execution["id"]}/checkin',body).status_code==409
    with http.scoped(tokens[0]) as (s,p):
        assert len(list(s.scalars(select(Checkin))))==1


def test_assistant_reference_switch_and_version(accounts):
    _,tokens=accounts; _,s1=seed(tokens[0]); _,s2=seed(tokens[0],'跑步');availability(tokens[0])
    state=request(tokens[0],'GET','/assistant').json();assert state['version']=='0'
    a=request(tokens[0],'POST','/assistant/turns',{'text':'给雅思备考找时间','version':'0'})
    assert a.status_code==200,a.text
    state=a.json();assert state['proposal']['step_id']==s1
    old=state['proposal']
    b=request(tokens[0],'POST','/assistant/turns',{'text':'那跑步呢','version':state['version']})
    assert b.status_code==200,b.text
    assert b.json()['proposal']['step_id']==s2
    assert request(tokens[0],'POST',f'/proposals/{old["id"]}/accept',{'version':old['version'],'index':0}).status_code==409
    assert request(tokens[0],'POST','/assistant/turns',{'text':'选第一个','version':state['version']}).status_code==409
    c=request(tokens[0],'POST','/assistant/turns',{'text':'选第一个','version':b.json()['version']})
    assert c.status_code==200,c.text
    assert '跑步' in c.json()['messages'][-1]['content']
    assert c.json()['operation_id']
    assert request(tokens[1],'GET','/assistant').json()['messages']==[]


def test_ambiguous_goal_does_not_write(accounts):
    _,tokens=accounts;seed(tokens[0]);seed(tokens[0],'跑步');availability(tokens[0])
    r=request(tokens[0],'POST','/assistant/turns',{'text':'给这个找时间','version':'0'})
    assert r.status_code==200,r.text
    assert '还是' in r.json()['messages'][-1]['content']
    assert r.json()['proposal'] is None
    with http.scoped(tokens[0]) as (s,p):assert list(s.scalars(select(Event)))==[]


def test_transaction_rolls_back_if_persistence_fails(accounts,monkeypatch):
    _,tokens=accounts;_,sid=seed(tokens[0]);availability(tokens[0])
    p=request(tokens[0],'POST',f'/steps/{sid}/proposals',{}).json()
    original=Service.accept
    def fail(self,*a,**kw):
        original(self,*a,**kw)
        raise ApiError('simulated_failure','failed',409)
    monkeypatch.setattr(Service,'accept',fail)
    r=request(tokens[0],'POST',f'/proposals/{p["id"]}/accept',{'version':p['version'],'index':0})
    assert r.status_code==409
    with http.scoped(tokens[0]) as (s,principal):
        assert list(s.scalars(select(Event)))==[]
        assert s.get(WebProposal,p['id']).status=='open'


def test_new_tables_tenant_isolation_and_foreign_references(accounts):
    ids,tokens=accounts;_,sid=seed(tokens[1]);availability(tokens[1])
    p=request(tokens[1],'POST',f'/steps/{sid}/proposals',{}).json()
    with http.scoped(tokens[0]) as (s,principal):assert s.get(WebProposal,p['id']) is None
    with pytest.raises(tenancy.AccessDenied),http.scoped(tokens[0],write=True) as (s,principal):
        s.add(WebThread(user_id=ids[0],proposal_id=p['id'],version=0,messages_json='[]',choices_json='[]'))


def test_frontend_requires_login_and_prevents_traversal(accounts,tmp_path,monkeypatch):
    root=tmp_path/'site';dist=root/'frontend'/'dist';dist.mkdir(parents=True)
    (dist/'index.html').write_text('<!doctype html><title>GoalCal</title>')
    monkeypatch.setattr(http,'ROOT',root)
    async def check():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),base_url=ORIGIN) as c:
            assert (await c.get('/app/')).status_code==303
            c.cookies.set('goalcal_session',accounts[1][0])
            r=await c.get('/app/goals');assert r.status_code==200
            assert "script-src 'self'" in r.headers['content-security-policy']
            assert (await c.get('/app/missing.js')).status_code==404
    asyncio.run(check())


def test_no_deadline_and_create_intent_survive_refresh(accounts):
    token=accounts[1][0]
    result=request(token,'POST','/goal-drafts',{'title':'学习钢琴','deadline':None})
    assert result.status_code==200, result.text
    draft=result.json(); assert draft['deadline'] is None
    result=request(token,'POST','/goals',{'draft_id':draft['id'],'version':draft['version'],
                  'title':draft['title'],'deadline':None,'steps':draft['steps']})
    assert result.status_code==200, result.text
    assert result.json()['goal']['deadline'] is None
    response=request(token,'POST','/assistant/turns',{'text':'新建目标：练习写作','version':'0'})
    assert response.status_code==200, response.text
    assert response.json().get('create_goal')
    assert request(token,'GET','/assistant').json()['create_goal']==response.json()['create_goal']


@pytest.mark.parametrize(('message', 'draft'), [
    ('我想每周跑三次步', {'title': '跑步', 'weekly_target': 3}),
    ('I want to run three times a week for 20 minutes', {'title': 'run', 'weekly_target': 3, 'est_min': 20}),
    ('我想每天晚上七点阅读 30 分钟', {'title': '阅读', 'weekdays': list(range(7)), 'est_min': 30, 'start_time': '19:00'}),
    ('我想每周一、三、五练琴', {'title': '练琴', 'weekdays': [0, 2, 4]}),
])
def test_assistant_recurring_action_opens_habit_draft_without_creating_it(accounts, monkeypatch, message, draft):
    from goalcal.web import assistant

    def unexpected_model_call(*args, **kwargs):
        pytest.fail('A clear recurring action should be parsed locally')

    monkeypatch.setattr(assistant, 'turn_client', unexpected_model_call)
    token = accounts[1][0]
    response = request(token, 'POST', '/assistant/turns', {'text': message, 'version': '0'})
    assert response.status_code == 200, response.text
    assert response.json()['create_habit'] == draft
    assert 'create_goal' not in response.json()
    assert request(token, 'GET', '/assistant').json()['create_habit'] == draft
    assert request(token, 'GET', '/habits').json()['habits'] == []


def test_new_login_return_is_bounded(accounts):
    import re
    from goalcal.models import User
    with db.new_session() as s:
        name=s.get(User,accounts[0][0]).username
    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),base_url=ORIGIN) as c:
            for target, expected in [('/app/', '/app/'), ('https://evil.test', '/')]:
                form=await c.get('/_goalcal/login?next=/app/')
                csrf=re.search(r'name="csrf" value="([^"]+)"', form.text).group(1)
                response=await c.post('/_goalcal/login',data={'username':name,'password':PASSWORD,'csrf':csrf,'next':target},headers={'Origin':ORIGIN})
                assert response.status_code==303
                assert response.headers['location']==expected
    asyncio.run(run())


def test_app_login_page_matches_new_shell_and_logout_returns(accounts):
    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=ORIGIN) as c:
            page = await c.get('/_goalcal/login?next=/app/')
            assert page.status_code == 200
            assert 'class="brand-mark"' in page.text
            assert '📅' not in page.text
            assert 'href="/_goalcal/register?next=/app/"' in page.text
            reg = await c.get('/_goalcal/register?next=/app/')
            assert reg.status_code == 200
            assert 'name="next" value="/app/"' in reg.text
            out = await c.post('/_goalcal/logout?next=/app/', headers={'Origin': ORIGIN})
            assert out.status_code == 303
            assert out.headers['location'] == '/_goalcal/login?next=/app/'
    asyncio.run(run())


def test_archive_hides_goal_and_delete_removes_goal_safely(accounts):
    t1, t2 = accounts[1]
    draft = request(t1, 'POST', '/goal-drafts', {'title': '待归档目标', 'deadline': None}).json()
    created = request(t1, 'POST', '/goals', {
        'draft_id': draft['id'], 'version': draft['version'],
        'title': draft['title'], 'deadline': None, 'steps': draft['steps'],
    }).json()['goal']
    gid = created['id']
    step_id = created['steps'][0]['id']

    # Propose & accept a slot so there is an event, proposal, interaction, and draft linked to the goal.
    prop = request(t1, 'POST', f'/steps/{step_id}/proposals', {}).json()
    if prop['candidates']:
        request(t1, 'POST', f'/proposals/{prop["id"]}/accept', {'version': prop['version'], 'index': 0})

    # Foreign user cannot delete.
    assert request(t2, 'DELETE', f'/goals/{gid}', {'version': created['version']}).status_code == 404

    # Archive removes goal from default /goals and /goals/{id}, while include_archived=1 can restore.
    latest = request(t1, 'GET', f'/goals/{gid}').json()
    archived = request(t1, 'PATCH', f'/goals/{gid}', {'version': latest['version'], 'status': 'archived'})
    assert archived.status_code == 200
    assert all(g['id'] != gid for g in request(t1, 'GET', '/goals').json()['goals'])
    assert request(t1, 'GET', f'/goals/{gid}').status_code == 404
    assert any(g['id'] == gid for g in request(t1, 'GET', '/goals?include_archived=1').json()['goals'])

    # Restore back to active, then permanently delete.
    restored = request(t1, 'PATCH', f'/goals/{gid}', {'version': archived.json()['goal']['version'], 'status': 'active'})
    assert restored.status_code == 200
    assert any(g['id'] == gid for g in request(t1, 'GET', '/goals').json()['goals'])

    deleted = request(t1, 'DELETE', f'/goals/{gid}', {'version': restored.json()['goal']['version']})
    assert deleted.status_code == 200
    assert all(g['id'] != gid for g in request(t1, 'GET', '/goals?include_archived=1').json()['goals'])
    assert request(t1, 'GET', f'/goals/{gid}?include_archived=1').status_code == 404


def test_completing_last_step_auto_refills_via_llm(accounts, monkeypatch):
    from types import SimpleNamespace
    from goalcal.domain import refine
    from goalcal.web.core import version_token

    uid, token = accounts[0][0], accounts[1][0]
    with db.new_session() as s:
        goal = create_goal(
            s, uid, '学会自由泳',
            deadline=(datetime.now(timezone.utc) + timedelta(days=30)).date(),
            with_template=False,
            step_defs=[{'title': '扶池边水中呼气练习', 'est_min': 20, 'milestone': '适应水性', 'seq': 0}],
            outline=['适应水性', '扶板打腿'],
        )
        gid = goal.id
        step = goal.steps[0]
        step_id, step_ver = step.id, version_token(step)
        s.commit()

    class FakeClient:
        ready = True
        def complete_json(self, system, user_text, temperature=0.0):
            return {'steps': [
                {'milestone': '扶板打腿', 'title': '扶板慢游25米保持腿部发力', 'outcome': '连续打腿25米不沉腿', 'est_min': 25, 'energy': 1, 'kind': 'physical'},
                {'milestone': '扶板打腿', 'title': '流线型蹬壁滑行接打腿练习', 'outcome': '每次滑行超过3米再打腿', 'est_min': 25, 'energy': 1, 'kind': 'physical'},
            ]}

    monkeypatch.setattr(refine, 'settings', SimpleNamespace(llm_enabled=True))
    monkeypatch.setattr(refine, 'plan_client', lambda: FakeClient())

    res = request(token, 'PATCH', f'/steps/{step_id}', {'version': step_ver, 'status': 'done'}, key='auto-refill-step-1')
    assert res.status_code == 200, res.text
    payload = res.json()
    assert '扶板打腿' in payload['message']
    goal_data = payload['goal']
    assert goal_data['status'] == 'active'
    titles = [s['title'] for s in goal_data['steps']]
    assert '扶板慢游25米保持腿部发力' in titles
    assert not any('Define the next action' in t for t in titles)
    latest = request(token, 'GET', f'/goals/{gid}').json()
    assert any(s['milestone'] == '扶板打腿' and s['status'] == 'todo' for s in latest['steps'])


def test_schedule_step_direct_and_undo(accounts):
    from goalcal.web.core import version_token
    uid, token = accounts[0][0], accounts[1][0]
    with db.new_session() as s:
        goal = create_goal(
            s, uid, '直接指定时间测试',
            deadline=(datetime.now(timezone.utc) + timedelta(days=30)).date(),
            with_template=False,
            step_defs=[{'title': '完成练习', 'est_min': 30, 'milestone': '阶段1', 'seq': 0}],
            outline=['阶段1'],
        )
        step = goal.steps[0]
        step_id, step_ver = step.id, version_token(step)
        s.commit()

    # Past time is rejected
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    assert request(token, 'POST', f'/steps/{step_id}/schedule', {'version': step_ver, 'start': past}).status_code == 422

    # Valid future morning slot (10:00 Asia/Shanghai = 02:00 UTC on +2 days)
    future = (datetime.now(timezone.utc) + timedelta(days=2)).replace(hour=2, minute=0, second=0, microsecond=0).isoformat().replace('+00:00', 'Z')
    scheduled = request(token, 'POST', f'/steps/{step_id}/schedule', {'version': step_ver, 'start': future})
    assert scheduled.status_code == 200, scheduled.text
    op_id = scheduled.json()['operation_id']
    assert scheduled.json()['event']['start'] == future

    undone = request(token, 'POST', f'/operations/{op_id}/undo', {})
    assert undone.status_code == 200


def test_root_app_redirects_root_to_app_and_trusts_local_forwarded_proto(accounts, monkeypatch):
    import re
    from starlette.requests import Request
    from goalcal.models import User
    from goalcal.web.http import valid_origin
    monkeypatch.setenv('GOALCAL_ROOT_APP', '1')
    with db.new_session() as s:
        name = s.get(User, accounts[0][0]).username

    fake_http_tunnel_req = Request({
        'type': 'http',
        'scheme': 'http',
        'method': 'POST',
        'path': '/api/v1/goal-drafts',
        'headers': [
            (b'host', b'goalcal.zhang.sarl'),
            (b'origin', b'https://goalcal.zhang.sarl'),
            (b'x-forwarded-proto', b'https'),
        ],
        'client': ('127.0.0.1', 50000),
    })
    assert valid_origin(fake_http_tunnel_req) is True
    fake_untrusted_req = Request({
        'type': 'http',
        'scheme': 'http',
        'method': 'POST',
        'path': '/api/v1/goal-drafts',
        'headers': [
            (b'host', b'goalcal.zhang.sarl'),
            (b'origin', b'https://goalcal.zhang.sarl'),
            (b'x-forwarded-proto', b'https'),
        ],
        'client': ('198.51.100.10', 50000),
    })
    assert valid_origin(fake_untrusted_req) is False

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url='https://goalcal.zhang.sarl') as c:
            landing = await c.get('/', headers={'Accept': 'text/html', 'X-Forwarded-Proto': 'https'})
            assert landing.status_code == 200
            assert 'name="next" value="/app/"' in landing.text
            csrf = re.search(r'name="csrf" value="([^"]+)"', landing.text).group(1)
            logged_in = await c.post(
                '/_goalcal/login',
                data={'username': name, 'password': PASSWORD, 'csrf': csrf, 'next': '/app/'},
                headers={'Origin': 'https://goalcal.zhang.sarl', 'X-Forwarded-Proto': 'https'},
                follow_redirects=False,
            )
            assert logged_in.status_code == 303
            assert logged_in.headers['location'] == '/app/'
            root_authed = await c.get('/', headers={'Accept': 'text/html'}, follow_redirects=False)
            assert root_authed.status_code == 307
            assert root_authed.headers['location'] == '/app/'
            api_ok = await c.post(
                '/api/v1/goal-drafts',
                json={'title': '隧道起源测试目标', 'deadline': None},
                headers={
                    'Origin': 'https://goalcal.zhang.sarl',
                    'X-Forwarded-Proto': 'https',
                    'Idempotency-Key': 'tunnel-origin-ok-1',
                },
            )
            assert api_ok.status_code == 200, api_ok.text
            api_bad = await c.post(
                '/api/v1/goal-drafts',
                json={'title': '非法来源', 'deadline': None},
                headers={
                    'Origin': 'https://evil.example',
                    'X-Forwarded-Proto': 'https',
                    'Idempotency-Key': 'tunnel-origin-bad-1',
                },
            )
            assert api_bad.status_code == 403
            out = await c.post('/_goalcal/logout', headers={'Origin': 'https://goalcal.zhang.sarl'}, follow_redirects=False)
            assert out.status_code == 303
            assert out.headers['location'] == '/_goalcal/login?next=/app/'

    asyncio.run(run())


def test_open_registration_requires_unique_email_and_allows_email_login(accounts, monkeypatch):
    import re
    monkeypatch.setenv('GOALCAL_OPEN_REGISTRATION', '1')
    monkeypatch.setenv('GOALCAL_ROOT_APP', '1')

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url='https://goalcal.zhang.sarl') as c:
            login_page = await c.get('/', headers={'Accept': 'text/html', 'X-Forwarded-Proto': 'https'})
            assert 'Username or email' in login_page.text

            reg_page = await c.get('/_goalcal/register?next=/app/')
            assert reg_page.status_code == 200
            assert 'name="email"' in reg_page.text
            assert 'name="invitation"' not in reg_page.text
            csrf = re.search(r'name="csrf" value="([^"]+)"', reg_page.text).group(1)

            bad_email = await c.post('/_goalcal/register', data={
                'username': 'email_user_1',
                'email': 'not-an-email',
                'password': PASSWORD,
                'tz': 'Asia/Shanghai',
                'csrf': csrf,
                'next': '/app/',
            }, headers={'Origin': 'https://goalcal.zhang.sarl'})
            assert 'Please enter a valid email address.' in bad_email.text
            csrf = re.search(r'name="csrf" value="([^"]+)"', bad_email.text).group(1)

            bad_pw = await c.post('/_goalcal/register', data={
                'username': 'email_user_1',
                'email': 'Alice.Test@Example.COM',
                'password': '12345',
                'tz': 'Asia/Shanghai',
                'csrf': csrf,
                'next': '/app/',
            }, headers={'Origin': 'https://goalcal.zhang.sarl'})
            assert 'Password must be 6–256 characters.' in bad_pw.text
            csrf = re.search(r'name="csrf" value="([^"]+)"', bad_pw.text).group(1)

            ok_reg = await c.post('/_goalcal/register', data={
                'username': 'email_user_1',
                'email': 'Alice.Test@Example.COM',
                'password': 'abc123',
                'tz': 'Asia/Shanghai',
                'csrf': csrf,
                'next': '/app/',
            }, headers={'Origin': 'https://goalcal.zhang.sarl'})
            assert 'Account created. Please log in.' in ok_reg.text

            dup_page = await c.get('/_goalcal/register?next=/app/')
            csrf_dup = re.search(r'name="csrf" value="([^"]+)"', dup_page.text).group(1)
            dup_email = await c.post('/_goalcal/register', data={
                'username': 'email_user_2',
                'email': 'alice.test@example.com',
                'password': 'abc123',
                'tz': 'Asia/Shanghai',
                'csrf': csrf_dup,
                'next': '/app/',
            }, headers={'Origin': 'https://goalcal.zhang.sarl'})
            assert 'This email is already registered.' in dup_email.text

            login_form = await c.get('/_goalcal/login?next=/app/')
            csrf_login = re.search(r'name="csrf" value="([^"]+)"', login_form.text).group(1)
            logged_in = await c.post('/_goalcal/login', data={
                'username': 'alice.test@example.com',
                'password': 'abc123',
                'csrf': csrf_login,
                'next': '/app/',
            }, headers={'Origin': 'https://goalcal.zhang.sarl'}, follow_redirects=False)
            assert logged_in.status_code == 303
            assert logged_in.headers['location'] == '/app/'

            me = await c.get('/api/v1/me')
            assert me.status_code == 200
            assert me.json()['email'] == 'alice.test@example.com'

            auth.manage('alice.test@example.com', password='New-Password-2026!')
            assert (await c.get('/api/v1/me')).status_code == 401
            login_form2 = await c.get('/_goalcal/login?next=/app/')
            csrf_login2 = re.search(r'name="csrf" value="([^"]+)"', login_form2.text).group(1)
            logged_in2 = await c.post('/_goalcal/login', data={
                'username': 'alice.test@example.com',
                'password': 'New-Password-2026!',
                'csrf': csrf_login2,
                'next': '/app/',
            }, headers={'Origin': 'https://goalcal.zhang.sarl'}, follow_redirects=False)
            assert logged_in2.status_code == 303

    asyncio.run(run())


def test_bilingual_api_me_draft_scheduler_and_assistant(accounts, monkeypatch):
    from goalcal.llm import plan as llm_plan

    _, tokens = accounts
    seen_target_langs = []

    class _MockPlanClient:
        ready = True

        def complete_json(self, system, user_prompt, **_kwargs):
            payload = json.loads(user_prompt)
            seen_target_langs.append(payload["target_language"])
            if payload["target_language"] == "English":
                return {
                    "outline": ["Foundation", "Practice"],
                    "steps": [
                        {"milestone": "Foundation", "title": "Learn chord shapes", "outcome": "Play 3 chords", "est_min": 25, "energy": 1, "kind": "deep"}
                    ],
                }
            return {
                "outline": ["基础准备", "上手练习"],
                "steps": [
                    {"milestone": "基础准备", "title": "熟悉基础和弦指法", "outcome": "能按出3个和弦", "est_min": 25, "energy": 1, "kind": "deep"}
                ],
            }

    monkeypatch.setattr(llm_plan, 'settings', replace(llm_plan.settings, llm_enabled=True))
    monkeypatch.setattr(llm_plan, 'plan_client', lambda: _MockPlanClient())

    async def run():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=ORIGIN,
            cookies={'goalcal_session': tokens[0]},
        ) as c:
            r = await c.patch('/api/v1/me', json={'lang': 'en'}, headers={'Origin': ORIGIN, 'Idempotency-Key': 'lang-en-1234'})
            assert r.status_code == 200
            assert r.json()['lang'] == 'en'

            draft_en = await c.post('/api/v1/goal-drafts', json={'title': 'Learn guitar', 'deadline': None},
                                    headers={'Origin': ORIGIN, 'Idempotency-Key': 'draft-en-1234', 'X-GoalCal-Lang': 'en'})
            assert draft_en.status_code == 200
            assert draft_en.json()['outline'] == ['Foundation', 'Practice']
            assert draft_en.json()['steps'][0]['title'] == 'Learn chord shapes'

            bad_ev = await c.post('/api/v1/calendar/events', json={'title': ' ', 'start': 'bad', 'end': 'bad'},
                                  headers={'Origin': ORIGIN, 'Idempotency-Key': 'bad-ev-1234', 'X-GoalCal-Lang': 'en'})
            assert bad_ev.status_code == 422
            assert 'Title must be' in bad_ev.json()['error']['message']

            turn_en = await c.post('/api/v1/assistant/turns', json={'text': 'find time', 'version': '0'},
                                   headers={'Origin': ORIGIN, 'Idempotency-Key': 'turn-en-1234', 'X-GoalCal-Lang': 'en'})
            assert turn_en.status_code == 200
            assert 'Create a goal first' in turn_en.json()['messages'][-1]['content']

            draft_zh = await c.post('/api/v1/goal-drafts', json={'title': '学吉他', 'deadline': None},
                                    headers={'Origin': ORIGIN, 'Idempotency-Key': 'draft-zh-1234', 'X-GoalCal-Lang': 'zh'})
            assert draft_zh.status_code == 200
            assert draft_zh.json()['outline'] == ['基础准备', '上手练习']

            fb_bad = await c.post('/api/v1/feedback', json={'category': '建议或其他', 'message': '   hi  '},
                                  headers={'Origin': ORIGIN, 'Idempotency-Key': 'fb-bad-1234', 'X-GoalCal-Lang': 'en'})
            assert fb_bad.status_code == 422

            fb_ok = await c.post('/api/v1/feedback', json={'category': '建议或其他', 'message': 'Great calendar drag UX!'},
                                 headers={'Origin': ORIGIN, 'Idempotency-Key': 'fb-ok-1234', 'X-GoalCal-Lang': 'en'})
            assert fb_ok.status_code == 200
            assert fb_ok.json()['id'] >= 1
            assert 'Feedback saved' in fb_ok.json()['message']

    asyncio.run(run())
    assert seen_target_langs == ['English', 'Simplified Chinese']


def test_assistant_clear_and_cross_day_6h_auto_reset(accounts):
    from goalcal.web.assistant import _is_stale_thread

    _, tokens = accounts

    # Auto-clear helper checks BOTH cross-day AND >=6h in user's timezone (Asia/Shanghai = UTC+8)
    nov16_1500_sh = [{'id': '1', 'role': 'user', 'content': 'hi', 'ts': '2026-11-16T07:00:00+00:00'}]
    # Same local day, 8h elapsed -> keep!
    assert _is_stale_thread(nov16_1500_sh, datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc), 'Asia/Shanghai') is False
    # Crossed midnight (23:30 Nov 16 -> 01:00 Nov 17), only 1.5h elapsed -> keep!
    nov16_2330_sh = [{'id': '1', 'role': 'user', 'content': 'hi', 'ts': '2026-11-16T15:30:00+00:00'}]
    assert _is_stale_thread(nov16_2330_sh, datetime(2026, 11, 16, 17, 0, tzinfo=timezone.utc), 'Asia/Shanghai') is False
    # Crossed midnight AND >=6h elapsed (21:00 Nov 16 -> 08:00 Nov 17 = 11h) -> auto-clear!
    nov16_2100_sh = [{'id': '1', 'role': 'user', 'content': 'hi', 'ts': '2026-11-16T13:00:00+00:00'}]
    assert _is_stale_thread(nov16_2100_sh, datetime(2026, 11, 17, 0, 0, tzinfo=timezone.utc), 'Asia/Shanghai') is True

    async def run():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=ORIGIN,
            cookies={'goalcal_session': tokens[0]},
        ) as c:
            turn_res = await c.post(
                '/api/v1/assistant/turns',
                json={'text': '今天有什么安排？', 'version': '0'},
                headers={'Origin': ORIGIN, 'Idempotency-Key': 'turn-clr-1234', 'X-GoalCal-Lang': 'zh'},
            )
            assert turn_res.status_code == 200
            ver = turn_res.json()['version']
            assert len(turn_res.json()['messages']) >= 2

            stale_clr = await c.post(
                '/api/v1/assistant/clear',
                json={'version': '0'},
                headers={'Origin': ORIGIN, 'Idempotency-Key': 'clr-stale-1234', 'X-GoalCal-Lang': 'zh'},
            )
            assert stale_clr.status_code == 409

            cleared = await c.post(
                '/api/v1/assistant/clear',
                json={'version': ver},
                headers={'Origin': ORIGIN, 'Idempotency-Key': 'clr-ok-1234', 'X-GoalCal-Lang': 'zh'},
            )
            assert cleared.status_code == 200
            assert cleared.json()['messages'] == []
            assert cleared.json()['message'] == '对话已清空'

            draft = await c.post('/api/v1/goal-drafts', json={'title': '学吉他', 'deadline': None},
                                 headers={'Origin': ORIGIN, 'Idempotency-Key': 'prop-draft-1234', 'X-GoalCal-Lang': 'zh'})
            created = await c.post('/api/v1/goals', json={
                'draft_id': draft.json()['id'],
                'version': draft.json()['version'],
                'title': draft.json()['title'],
                'deadline': draft.json()['deadline'],
                'steps': draft.json()['steps'],
            }, headers={'Origin': ORIGIN, 'Idempotency-Key': 'prop-goal-1234'})
            step_data = created.json()['goal']['steps'][0]
            step_id = step_data['id']
            prop = await c.post(f'/api/v1/steps/{step_id}/proposals', json={},
                                headers={'Origin': ORIGIN, 'Idempotency-Key': 'prop-sort-1234'})
            assert prop.status_code == 200
            starts = [cand['start'] for cand in prop.json()['candidates']]
            assert starts == sorted(starts)

            accepted = await c.post(
                f"/api/v1/proposals/{prop.json()['id']}/accept",
                json={'index': 0, 'version': prop.json()['version']},
                headers={'Origin': ORIGIN, 'Idempotency-Key': 'prop-accept-title-1234'},
            )
            assert accepted.status_code == 200
            ev = accepted.json()['event']
            assert ev['title'] == step_data['title']
            assert ev['goal_title'] == '学吉他'

            # Legacy goal-prefixed block titles in DB also serialize without the redundant goal prefix
            from goalcal import db
            from goalcal.models import Event
            with db.session_scope() as s:
                row = s.get(Event, ev['id'])
                row.title = f"学吉他 · {step_data['title']}"
            cal_res = await c.get(
                f"/api/v1/calendar/events?start={ev['start']}&end={ev['end']}",
            )
            assert cal_res.status_code == 200
            matched = next(e for e in cal_res.json()['events'] if e['id'] == ev['id'])
            assert matched['title'] == step_data['title']
            assert matched['goal_title'] == '学吉他'

    asyncio.run(run())


def test_today_next_prioritizes_today_then_unscheduled_pinned_then_future_fallback(accounts):
    from zoneinfo import ZoneInfo
    from goalcal import db
    from goalcal.domain import goals as goal_svc
    from goalcal.models import Event, User

    user_ids, tokens = accounts
    uid = user_ids[0]
    with db.session_scope() as s:
        user = s.get(User, uid)
        user.timezone = 'Asia/Shanghai'
        pinned_goal = goal_svc.create_goal(
            s,
            uid,
            title='置顶英语学习',
            step_defs=[{'title': '背 20 个词', 'est_min': 20, 'milestone': '基础', 'seq': 0}],
        )
        pinned_goal.pinned = True
        unpinned_goal = goal_svc.create_goal(
            s,
            uid,
            title='普通阅读目标',
            step_defs=[{'title': '读第一章', 'est_min': 30, 'milestone': '开始', 'seq': 0}],
        )
        pinned_step_id = pinned_goal.steps[0].id
        unpinned_step_id = unpinned_goal.steps[0].id

        now_utc = datetime.now(timezone.utc)
        tomorrow_local = (now_utc.astimezone(ZoneInfo('Asia/Shanghai')) + timedelta(days=1)).replace(
            hour=10, minute=0, second=0, microsecond=0,
        )
        tomorrow_start = tomorrow_local.astimezone(timezone.utc).replace(tzinfo=None)
        s.add(
            Event(
                user_id=uid,
                title='读第一章',
                start_utc=tomorrow_start,
                end_utc=tomorrow_start + timedelta(minutes=30),
                kind='block',
                status='planned',
                source='web',
                step_id=unpinned_step_id,
            )
        )

    # 1) Unpinned goal scheduled tomorrow must NOT displace an unscheduled pinned goal on Today
    t1 = request(tokens[0], 'GET', '/today').json()
    assert t1['next']['goal']['title'] == '置顶英语学习'
    assert t1['next']['step']['id'] == pinned_step_id
    assert t1['next']['event'] is None

    # 2) Once the pinned goal is also scheduled in the future (even further out), Today falls back to earliest future event
    with db.session_scope() as s:
        later_start = tomorrow_start + timedelta(days=2)
        s.add(
            Event(
                user_id=uid,
                title='背 20 个词',
                start_utc=later_start,
                end_utc=later_start + timedelta(minutes=20),
                kind='block',
                status='planned',
                source='web',
                step_id=pinned_step_id,
            )
        )
    t2 = request(tokens[0], 'GET', '/today').json()
    assert t2['next']['step']['id'] == unpinned_step_id
    assert t2['next']['event'] is not None

    # 3) A block within today always takes top priority on Today
    with db.session_scope() as s:
        today_start = (datetime.now(timezone.utc) + timedelta(minutes=15)).replace(tzinfo=None)
        # Ensure today_start stays before local midnight in test by placing it at least inside [now, now+15m] or near future if before 23:45
        ev_today = s.query(Event).filter(Event.step_id == pinned_step_id).one()
        local_now = datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))
        local_end = (local_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        if (local_end - local_now).total_seconds() > 1200:
            ev_today.start_utc = today_start
            ev_today.end_utc = today_start + timedelta(minutes=15)
            check_today = True
        else:
            check_today = False

    if check_today:
        t3 = request(tokens[0], 'GET', '/today').json()
        assert t3['next']['step']['id'] == pinned_step_id
        assert t3['next']['event'] is not None


def test_me_theme_preference_persistence_and_validation(accounts):
    _, tokens = accounts

    async def run():
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app),
            base_url=ORIGIN,
            cookies={'goalcal_session': tokens[0]},
        ) as c:
            initial = await c.get('/api/v1/me')
            assert initial.status_code == 200
            assert initial.json()['theme'] == ''

            set_lang = await c.patch('/api/v1/me', json={'lang': 'en'}, headers={'Origin': ORIGIN, 'Idempotency-Key': 'theme-lang-1'})
            assert set_lang.status_code == 200
            assert set_lang.json()['lang'] == 'en'
            assert set_lang.json()['theme'] == ''

            set_dark = await c.patch('/api/v1/me', json={'theme': 'dark'}, headers={'Origin': ORIGIN, 'Idempotency-Key': 'theme-dark-1'})
            assert set_dark.status_code == 200
            assert set_dark.json()['theme'] == 'dark'
            assert set_dark.json()['lang'] == 'en'

            set_light = await c.patch('/api/v1/me', json={'theme': 'light'}, headers={'Origin': ORIGIN, 'Idempotency-Key': 'theme-light-1'})
            assert set_light.status_code == 200
            assert set_light.json()['theme'] == 'light'

            reset_auto = await c.patch('/api/v1/me', json={'theme': ''}, headers={'Origin': ORIGIN, 'Idempotency-Key': 'theme-auto-1'})
            assert reset_auto.status_code == 200
            assert reset_auto.json()['theme'] == ''
            assert reset_auto.json()['lang'] == 'en'

            invalid = await c.patch('/api/v1/me', json={'theme': 'neon'}, headers={'Origin': ORIGIN, 'Idempotency-Key': 'theme-bad-1'})
            assert invalid.status_code == 422

    asyncio.run(run())


def test_habit_occurrence_is_tenant_scoped_and_concurrent_completion_counts_once(accounts):
    _, (owner, other) = accounts
    day = request(owner, 'GET', '/today').json()['date']
    body = {'title': 'Read daily', 'mode': 'fixed', 'est_min': 20, 'start_date': day, 'weekdays': list(range(7))}
    created = request(owner, 'POST', '/habits', body, key='create-reading')
    assert created.status_code == 200, created.text
    habit = created.json()['habit']
    assert request(owner, 'POST', '/habits', body, key='create-reading').json()['habit']['id'] == habit['id']
    path = f"/habits/{habit['id']}"
    assert request(other, 'GET', path).status_code == 404
    assert request(other, 'GET', '/habits').json()['habits'] == []
    item = request(owner, 'GET', f'{path}?date={day}').json()['occurrence']
    checkin = {'date': day, 'version': item['version'], 'action': 'done'}
    assert request(other, 'POST', f'{path}/occurrences', checkin).status_code == 404
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: request(owner, 'POST', f'{path}/occurrences', checkin), range(2)))
    assert sorted(r.status_code for r in results) == [200, 409]
    detail = request(owner, 'GET', path).json()
    assert detail['progress']['done'] == 1
    result = next(r.json() for r in results if r.status_code == 200)
    undo = request(owner, 'POST', f"/operations/{result['operation_id']}/undo", {})
    assert undo.status_code == 200, undo.text
    assert request(owner, 'GET', path).json()['progress']['done'] == 0
