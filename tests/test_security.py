from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import sqlite3
import time
import uuid

import pytest
from sqlalchemy import select, update, delete
from goalcal import auth, db, tenancy, quota, agent
from goalcal.domain import goals, calendar_service as cal, ics_import
from goalcal.models import Goal, Step, Event, Checkin, StateLog, Interaction, RescheduleLog
from goalcal.storage import connect, backup, restore
from goalcal.timeutil import local_now

PASSWORD = 'a-long-test-password-42'


@pytest.fixture
def accounts():
    db.init_db()
    names = ['test_' + uuid.uuid4().hex[:12] for _ in range(2)]
    ids = [auth.create_user(name, PASSWORD, admin=True) for name in names]
    tokens = [auth.login(name, PASSWORD) for name in names]
    return names, ids, tokens


def test_invite_single_use_concurrently(accounts):
    token = auth.invite()
    def register(i):
        try:
            return auth.create_user('invite_' + uuid.uuid4().hex[:12], PASSWORD, invitation=token)
        except auth.AuthError:
            return None
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(pool.map(register, range(5)))
    assert sum(x is not None for x in results) == 1
    with connect() as con:
        row = con.execute('SELECT * FROM invites WHERE digest=?', (auth.digest(token),)).fetchone()
        assert row['used_by'] in results
        assert token not in str(tuple(row))


def test_login_rate_limit_and_revocation(accounts):
    names, ids, tokens = accounts
    for _ in range(5):
        with pytest.raises(auth.AuthError):
            auth.login(names[0], 'wrong')
    with pytest.raises(auth.AuthError, match='15'):
        auth.login(names[0], PASSWORD)
    auth.manage(names[0], disabled=True)
    with pytest.raises(auth.AuthError):
        auth.authenticate(tokens[0])
    auth.manage(names[1], password='replacement-password-42')
    with pytest.raises(auth.AuthError):
        auth.authenticate(tokens[1])
    token = auth.login(names[1], 'replacement-password-42')
    auth.logout(token)
    with pytest.raises(auth.AuthError):
        auth.authenticate(token)


def test_expired_invite_and_session(accounts):
    token = auth.invite()
    with connect() as con:
        con.execute('UPDATE invites SET expires=0 WHERE digest=?', (auth.digest(token),))
        con.execute('UPDATE auth_sessions SET expires=0 WHERE digest=?', (auth.digest(accounts[2][0]),))
    with pytest.raises(auth.AuthError):
        auth.create_user('expired_invite', PASSWORD, invitation=token)
    with pytest.raises(auth.AuthError):
        auth.authenticate(accounts[2][0])


def populate(token):
    now = local_now('Asia/Shanghai')
    with tenancy.session_scope(token) as s:
        uid = s.info['principal'].user_id
        goal = goals.create_goal(s, uid, 'Learn piano')
        step = goal.steps[0]
        ev = cal.create_event(s, uid, 'Practice', now, now + timedelta(hours=1), step_id=step.id, kind='block')
        ci = Checkin(step_id=step.id, event_id=ev.id)
        s.add_all([ci, StateLog(user_id=uid), Interaction(user_id=uid, ts_sim=now.replace(tzinfo=None), kind='test', step_id=step.id, goal_id=goal.id, event_id=ev.id), RescheduleLog(event_id=ev.id)])
        s.flush()
        return goal.id, step.id, ev.id


def test_scoped_reads_bulk_writes_and_forged_ids(accounts):
    names, ids, tokens = accounts
    own = populate(tokens[0]); other = populate(tokens[1])
    with tenancy.session_scope(tokens[0]) as s:
        for cls, id_ in zip((Goal, Step, Event), other):
            assert s.get(cls, id_) is None
        for cls in (Goal, Step, Event, Checkin, StateLog, Interaction, RescheduleLog):
            assert len(list(s.scalars(select(cls)))) == 1 if cls != Step else len(list(s.scalars(select(cls)))) >= 1
        assert agent.propose_for_step(s, ids[0], other[1], local_now('Asia/Shanghai'), 'Asia/Shanghai') == ([], None)
        assert s.execute(update(Event).where(Event.id == other[2]).values(title='hacked')).rowcount == 0
        assert s.execute(delete(Checkin).where(Checkin.step_id == other[1])).rowcount == 0
        result = agent.accept_batch(s, ids[0], [{'step_id': other[1]}], local_now('Asia/Shanghai'), 'Asia/Shanghai')
        assert 'nothing' in result
    with pytest.raises(tenancy.AccessDenied), tenancy.session_scope(tokens[0]) as s:
        s.add(Checkin(step_id=own[1], event_id=other[2]))
        s.flush()
    with pytest.raises(tenancy.AccessDenied), tenancy.session_scope(tokens[0]) as s:
        s.add(Event(user_id=ids[1], title='bad', start_utc=datetime.now(), end_utc=datetime.now()))
        s.flush()
    with tenancy.session_scope(tokens[1]) as s:
        assert s.get(Event, other[2]).title == 'Practice'


def test_foreign_keys_and_delete_existing_history(accounts):
    gid, sid, eid = populate(accounts[2][0])
    with tenancy.session_scope(accounts[2][0]) as s:
        goals.delete_step(s, s.get(Step, sid))
    with tenancy.session_scope(accounts[2][0]) as s:
        assert s.get(Step, sid) is None
        assert s.get(Event, eid) is None
    with connect() as con:
        assert not con.execute('PRAGMA foreign_key_check').fetchall()


def test_quota_atomic(accounts):
    def request(_):
        try:
            quota.consume(accounts[1][0])
            return True
        except RuntimeError:
            return False
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(request, range(45)))
    assert sum(results) == 30
    quota.consume(accounts[1][1])


def test_five_concurrent_users_core_workflow(accounts):
    names = ['concurrent_' + uuid.uuid4().hex[:10] for _ in range(5)]
    ids = [auth.create_user(name, PASSWORD, admin=True) for name in names]
    tokens = [auth.login(name, PASSWORD) for name in names]
    def flow(token):
        gid, sid, eid = populate(token)
        with tenancy.session_scope(token) as s:
            uid = s.info['principal'].user_id
            assert {g.user_id for g in s.scalars(select(Goal))} == {uid}
            goals.complete_step(s, s.get(Step, sid))
        return gid
    with ThreadPoolExecutor(max_workers=5) as pool:
        assert len(set(pool.map(flow, tokens))) == 5


def test_online_backup_restore_and_session_revocation(accounts, tmp_path):
    populate(accounts[2][0])
    copy = backup(directory=tmp_path)
    restored = tmp_path / 'restored.db'
    restore(copy, restored)
    with sqlite3.connect(restored) as con:
        assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert not con.execute('PRAGMA foreign_key_check').fetchall()
        assert con.execute('SELECT count(*) FROM auth_sessions').fetchone()[0] == 0
        assert con.execute('SELECT count(*) FROM goals').fetchone()[0] > 0


def test_import_limits_rollback(accounts, monkeypatch):
    token = accounts[2][0]
    raw = b'BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:a\r\nDTSTART:20261023T100000Z\r\nSUMMARY:One\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:b\r\nDTSTART:20261023T100000Z\r\nSUMMARY:Two\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
    monkeypatch.setattr(ics_import, 'MAX_EVENTS', 1)
    with tenancy.session_scope(token) as s:
        with pytest.raises(ValueError):
            ics_import.import_ics(s, accounts[1][0], raw, 'Asia/Shanghai')
        assert list(s.scalars(select(Event))) == []
    with tenancy.session_scope(token) as s:
        with pytest.raises(ValueError):
            ics_import.import_ics(s, accounts[1][0], b' ' * (5*1024*1024+1), 'Asia/Shanghai')


def test_recurrence_scan_limit():
    from icalendar import Calendar
    raw = b'BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:a\r\nDTSTART:19000101T100000Z\r\nRRULE:FREQ=DAILY\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n'
    component = Calendar.from_ical(raw).walk('VEVENT')[0]
    with pytest.raises(ValueError, match='scan limit'):
        ics_import._expand(component, 'Asia/Shanghai', datetime(2026, 10, 23))


def test_llm_quota_failure_and_write_transaction_skip(accounts, monkeypatch):
    from types import SimpleNamespace
    from goalcal.llm.client import LLMClient, LLMUnavailable
    called = []
    def create(**kwargs):
        called.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))])
    client = LLMClient(api_key='test')
    client._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with tenancy.session_scope(accounts[2][0]) as s:
        client.complete_json('system', 'user')
        goals.create_goal(s, accounts[1][0], 'Goal', with_template=False)
        with pytest.raises(LLMUnavailable):
            client.complete_json('system', 'user')
    assert len(called) == 1
    for _ in range(29):
        quota.consume(accounts[1][0])
    with tenancy.session_scope(accounts[2][0]), pytest.raises(RuntimeError):
        client.complete_json('system', 'user')
    assert len(called) == 1


def test_tenant_timezone_reaches_parser(accounts, monkeypatch):
    from goalcal.llm import parser
    from zoneinfo import ZoneInfo
    captured = []
    class Client:
        def complete_json(self, system, user):
            captured.append(system)
            return {'intent': 'chat'}
    monkeypatch.setattr(parser, 'LLMClient', Client)
    parser._parse_with_llm('hello', datetime(2026, 10, 23, 9, tzinfo=ZoneInfo('America/New_York')))
    assert 'America/New_York' in captured[0]


def test_legacy_migration_preserves_unclaimed_data(tmp_path):
    from sqlalchemy import create_engine
    from goalcal.migrations import migrate
    path = tmp_path / 'old.db'
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT, tz TEXT, created_at DATETIME)")
        con.execute("INSERT INTO users VALUES (1, 'demo', 'Asia/Shanghai', CURRENT_TIMESTAMP)")
    engine = create_engine(f'sqlite:///{path}')
    migrate(engine)
    migrate(engine)
    with sqlite3.connect(path) as con:
        assert con.execute('PRAGMA user_version').fetchone()[0] == 1
        assert con.execute('SELECT name,username,password_hash FROM users').fetchone() == ('demo', None, None)
    assert list((tmp_path / 'backups').glob('pre-migration-*.db'))
    engine.dispose()


def test_forged_checkin_context_cannot_change_other_user(accounts):
    own = populate(accounts[2][0]); other = populate(accounts[2][1])
    from goalcal.agent import CheckinContext
    ctx = CheckinContext(event_ids=[other[2]], stage='outcome')
    with tenancy.session_scope(accounts[2][0]) as s:
        agent.continue_checkin(s, accounts[1][0], ctx, 'done', local_now('Asia/Shanghai'))
    with tenancy.session_scope(accounts[2][1]) as s:
        assert s.get(Step, other[1]).status == 'todo'
        assert s.get(Event, other[2]).status == 'planned'


def test_calendar_callback_uses_account_zone_not_browser_zone():
    from goalcal.ui.calendar_view import calendar_wall_time
    from goalcal.timeutil import as_utc_naive
    value = calendar_wall_time('2026-10-23T09:00:00Z', 'Asia/Shanghai')
    assert as_utc_naive(value, 'Asia/Shanghai') == datetime(2026, 10, 23, 1)
    value = calendar_wall_time('2026-10-23T09:00:00Z', 'America/New_York')
    assert as_utc_naive(value, 'America/New_York') == datetime(2026, 10, 23, 13)


def test_legacy_import_remaps_references(accounts, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from goalcal.models import Base, User
    from goalcal.admin import import_legacy
    source = tmp_path / 'legacy.db'
    engine = create_engine(f'sqlite:///{source}')
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        user = User(name='old'); s.add(user); s.flush()
        old_uid = user.id
        goal = goals.create_goal(s, user.id, 'Legacy goal')
        goals.complete_step(s, goal.steps[0])
        now = local_now('Asia/Shanghai')
        cal.create_event(s, user.id, 'Legacy event', now, now+timedelta(hours=1), step_id=goal.steps[0].id)
        s.commit()
    engine.dispose()
    import_legacy(source, old_uid, accounts[0][0])
    with tenancy.session_scope(accounts[2][0]) as s:
        goal = s.scalar(select(Goal))
        assert goal.title == 'Legacy goal' and goal.user_id == accounts[1][0]
        ev = s.scalar(select(Event))
        assert s.get(Step, ev.step_id).goal_id == goal.id
        assert s.scalar(select(Checkin)).step_id == goal.steps[0].id
    with pytest.raises(ValueError, match='empty'):
        import_legacy(source, old_uid, accounts[0][0])
    with sqlite3.connect(source) as con:
        assert con.execute('SELECT user_id FROM goals').fetchone()[0] == old_uid


def test_failed_migration_rolls_back(tmp_path):
    from sqlalchemy import create_engine
    from goalcal.models import Base
    from goalcal.migrations import migrate
    source = tmp_path / 'invalid.db'
    engine = create_engine(f'sqlite:///{source}')
    Base.metadata.create_all(engine)
    with sqlite3.connect(source) as con:
        con.execute("INSERT INTO goals (user_id,title,outcome,status,created_at,template_key,outline) VALUES (999,'bad','','active',CURRENT_TIMESTAMP,'generic','')")
    with pytest.raises(RuntimeError, match='foreign keys'):
        migrate(engine)
    with sqlite3.connect(source) as con:
        assert con.execute('PRAGMA user_version').fetchone()[0] == 0
        assert not con.execute("SELECT name FROM sqlite_master WHERE name='invites'").fetchall()
    engine.dispose()
