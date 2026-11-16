from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select

from goalcal import agent, conversation
from goalcal.models import Goal, Step, Event

NOW = datetime(2026, 10, 26, 9, tzinfo=ZoneInfo('Asia/Shanghai'))
TZ = 'Asia/Shanghai'


def _draft(session, user):
    reply = agent.handle(session, user.id, 'I want to publish my first short video in 30 days', NOW, TZ)
    assert reply.goal_draft is not None
    return reply.goal_draft


def test_template_preview_and_restored_confirmation_are_idempotent(session, user):
    draft = _draft(session, user)
    assert draft.source != 'llm'
    assert 2 <= len(draft.step_defs) <= 3
    assert list(session.scalars(select(Goal))) == []
    assert list(session.scalars(select(Step))) == []
    conversation.save_state(session, user.id, [], {'goal_draft': draft})
    _, state = conversation.load_state(session, user.id)
    restored = state['goal_draft']
    assert restored == draft
    saved = agent.confirm_goal_draft(session, user.id, restored, False, NOW, TZ)
    session.commit()
    # A stale tab still has the original draft after the first tab confirmed.
    again = agent.confirm_goal_draft(session, user.id, draft, False, NOW, TZ)
    assert saved.goal_id == again.goal_id
    assert len(list(session.scalars(select(Goal)))) == 1
    assert len(list(session.scalars(select(Step)))) == len(draft.step_defs)
    assert list(session.scalars(select(Event))) == []
    assert not saved.candidates


def test_llm_preview_and_revision_never_write_goal_or_steps(session, user, monkeypatch):
    from copy import deepcopy
    from goalcal.data.goal_templates import full_steps, first_batch
    steps = first_batch(full_steps('generic'))
    monkeypatch.setattr(agent, 'generate_outline_plan', lambda *a, **k: (['Start', 'Finish'], steps))
    draft = _draft(session, user)
    assert draft.source == 'llm'
    revised_steps = deepcopy(steps)
    revised_steps[0]['title'] = 'A smaller first step'
    monkeypatch.setattr(agent, 'generate_plan', lambda *a, **k: revised_steps)
    revised = agent.revise_goal_draft(draft, 'Make the first step smaller, in 60 days', NOW, TZ)
    assert revised.step_defs[0]['title'] == 'A smaller first step'
    assert (revised.deadline - NOW.date()).days == 60
    assert revised.brief.deadline == revised.deadline
    assert revised.draft_id == draft.draft_id
    assert draft.step_defs[0]['title'] != revised.step_defs[0]['title']
    assert list(session.scalars(select(Goal))) == []
    assert list(session.scalars(select(Step))) == []
    reply = agent.confirm_goal_draft(session, user.id, revised, True, NOW, TZ)
    assert reply.candidates and reply.step_id
    assert list(session.scalars(select(Event))) == []  # time selection is separate


def test_failed_revision_retains_original_preview(session, user, monkeypatch):
    draft = _draft(session, user)
    monkeypatch.setattr(agent, 'generate_plan', lambda *a, **k: None)
    assert agent.revise_goal_draft(draft, 'Make it shorter', NOW, TZ) is None
    assert list(session.scalars(select(Goal))) == []


def test_deadline_only_revision_works_offline_without_changing_steps(session, user, monkeypatch):
    draft = _draft(session, user)
    monkeypatch.setattr(agent, 'generate_plan', lambda *a, **k: None)

    revised = agent.revise_goal_draft(draft, 'Make the deadline 45 days', NOW, TZ)

    assert revised is not None
    assert (revised.deadline - NOW.date()).days == 45
    assert revised.brief.deadline == revised.deadline
    assert revised.step_defs == draft.step_defs
    assert revised.outline == draft.outline
    assert revised.draft_id == draft.draft_id
    assert draft.deadline != revised.deadline
    assert list(session.scalars(select(Goal))) == []
    assert list(session.scalars(select(Step))) == []


def test_mixed_revision_does_not_silently_apply_only_the_deadline(session, user, monkeypatch):
    draft = _draft(session, user)
    monkeypatch.setattr(agent, 'generate_plan', lambda *a, **k: None)
    assert agent.revise_goal_draft(draft, 'Make the first step smaller and the deadline 45 days', NOW, TZ) is None


def test_failed_confirmation_transaction_rolls_back_goal_and_steps(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from goalcal.models import Base
    from goalcal.domain.goals import ensure_user
    engine = create_engine(f"sqlite:///{tmp_path / 'rollback.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        user = ensure_user(session, 'rollback')
        session.commit()
        draft = _draft(session, user)
        agent.confirm_goal_draft(session, user.id, draft, False, NOW, TZ)
        session.rollback()
    with Session(engine) as session:
        assert list(session.scalars(select(Goal))) == []
        assert list(session.scalars(select(Step))) == []
    engine.dispose()


def test_version_two_migration_preserves_existing_goals(tmp_path):
    import sqlite3
    from sqlalchemy import create_engine
    from goalcal.migrations import migrate, VERSION
    path = tmp_path / 'v2.db'
    with sqlite3.connect(path) as con:
        con.execute('CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, disabled BOOLEAN NOT NULL DEFAULT 0, name TEXT, tz TEXT, created_at DATETIME)')
        con.execute('CREATE TABLE goals (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), title TEXT, outcome TEXT, deadline DATE, status TEXT, created_at DATETIME, template_key TEXT, outline TEXT)')
        con.execute("INSERT INTO users (id,name) VALUES (1,'existing')")
        con.execute("INSERT INTO goals (id,user_id,title,status) VALUES (1,1,'Keep my plan','active')")
        con.execute('PRAGMA user_version=2')
    engine = create_engine(f'sqlite:///{path}')
    migrate(engine)
    migrate(engine)
    with sqlite3.connect(path) as con:
        assert con.execute('PRAGMA user_version').fetchone()[0] == VERSION
        assert con.execute('SELECT title,status,draft_id FROM goals').fetchone() == ('Keep my plan','active',None)
    engine.dispose()


def test_legacy_steps_can_be_read_and_booked_after_migration(tmp_path):
    import sqlite3
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from goalcal.migrations import migrate
    from goalcal.domain.goals import active_goals, snapshot
    from goalcal.tenancy import TenantSession
    from types import SimpleNamespace

    path = tmp_path / 'existing-steps.db'
    # An actual v2 schema, rather than create_all from today's models.
    with sqlite3.connect(path) as con:
        con.executescript('''
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT,
                disabled BOOLEAN NOT NULL DEFAULT 0, name TEXT, tz TEXT, created_at DATETIME);
            CREATE TABLE goals (id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id),
                title TEXT, outcome TEXT, deadline DATE, status TEXT, created_at DATETIME,
                template_key TEXT, outline TEXT);
            CREATE TABLE steps (id INTEGER PRIMARY KEY, goal_id INTEGER REFERENCES goals(id),
                seq INTEGER, milestone TEXT, title TEXT, outcome TEXT, est_min INTEGER,
                energy INTEGER, kind TEXT, status TEXT, completed_at DATETIME, minutes_spent INTEGER);
            INSERT INTO users VALUES (1,'owner',NULL,0,'Owner','Asia/Shanghai',CURRENT_TIMESTAMP);
            INSERT INTO goals VALUES (1,1,'Existing goal','','2026-12-01','active',CURRENT_TIMESTAMP,'custom','');
            INSERT INTO steps VALUES (1,1,1,'Start','Existing step','',30,1,'deep','todo',NULL,0);
            PRAGMA user_version=2;
        ''')
    engine = create_engine(f'sqlite:///{path}')
    migrate(engine)
    with TenantSession(engine, info={'principal': SimpleNamespace(user_id=1)}) as session:
        goal = active_goals(session, 1)[0]
        assert snapshot(goal, now=NOW)['next_step'].title == 'Existing step'
    with Session(engine) as session:
        candidates, step = agent.propose_for_step(session, 1, 1, NOW, TZ)
        assert candidates and step.id == 1
        agent.accept(session, 1, 1, candidates[0], NOW, TZ)
        session.commit()
        assert session.scalar(select(Event).where(Event.step_id == 1)) is not None
    engine.dispose()
