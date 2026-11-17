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
