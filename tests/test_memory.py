from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine

from goalcal import auth, db, memory, tenancy
from goalcal.domain.scheduler import Preferences
from goalcal.models import Event, UserPreference
from goalcal.timeutil import as_utc_naive

PASSWORD = "a-long-test-password-42"
NOW = datetime(2026, 10, 25, 12, 0, 0)
SCHED = memory.SCHEDULER_SCOPE


@pytest.fixture
def accounts():
    db.init_db()
    names = ["mem_" + uuid.uuid4().hex[:12] for _ in range(2)]
    ids = [auth.create_user(n, PASSWORD, admin=True) for n in names]
    tokens = [auth.login(n, PASSWORD) for n in names]
    return ids, tokens


def test_scheduler_preferences_default(session, user):
    assert memory.get_scheduler_preferences(session, user.id, now=NOW) == Preferences()


def test_set_update_delete_roundtrip(session, user):
    uid = user.id
    memory.set_preference(session, uid, "day_start_hour", 7, scope=SCHED)
    prefs = memory.get_scheduler_preferences(session, uid, now=NOW)
    assert prefs.day_start_hour == 7
    assert prefs.granularity == Preferences().granularity  # 其余字段保持默认

    memory.set_preference(
        session, uid, "day_start_hour", 6, scope=SCHED,
        source="agent", confidence=0.8, evidence="user said mornings",
    )
    rows = memory.list_preferences(session, uid, scope=SCHED)
    assert len(rows) == 1  # key+scope 唯一：重复设置是更新
    row = rows[0]
    assert row.value == "6"
    assert row.source == "agent"
    assert row.confidence == 0.8
    assert row.evidence == "user said mornings"
    assert row.created_at is not None and row.updated_at is not None

    assert memory.delete_preference(session, uid, "day_start_hour", scope=SCHED)
    assert not memory.delete_preference(session, uid, "day_start_hour", scope=SCHED)
    assert memory.get_scheduler_preferences(session, uid, now=NOW) == Preferences()


def test_same_key_different_scope_is_distinct(session, user):
    uid = user.id
    memory.set_preference(session, uid, "note", "a", scope="work")
    memory.set_preference(session, uid, "note", "b", scope="life")
    rows = memory.list_preferences(session, uid)
    assert [json.loads(r.value) for r in rows] == ["b", "a"]  # 按 (scope, key) 排序
    assert memory.delete_preference(session, uid, "note", scope="work")
    assert len(memory.list_preferences(session, uid)) == 1


def test_json_arrays_become_tuples(session, user):
    uid = user.id
    memory.set_preference(session, uid, "preferred_windows", [[8, 11], [19, 21]], scope=SCHED)
    memory.set_preference(session, uid, "lunch", [12, 13], scope=SCHED)
    prefs = memory.get_scheduler_preferences(session, uid, now=NOW)
    assert prefs.preferred_windows == ((8, 11), (19, 21))
    assert all(isinstance(w, tuple) for w in prefs.preferred_windows)
    assert prefs.lunch == (12, 13)
    assert isinstance(prefs.lunch, tuple)


def test_expired_preferences_are_inactive(session, user):
    uid = user.id
    memory.set_preference(
        session, uid, "granularity", 30, scope=SCHED, expires_at=NOW - timedelta(minutes=1)
    )
    assert memory.get_scheduler_preferences(session, uid, now=NOW) == Preferences()
    assert memory.list_preferences(session, uid, scope=SCHED, now=NOW) == []
    assert len(memory.list_preferences(session, uid, scope=SCHED, include_expired=True)) == 1

    # 同一 key+scope 重设会更新过期时间
    memory.set_preference(
        session, uid, "granularity", 30, scope=SCHED, expires_at=NOW + timedelta(hours=1)
    )
    assert memory.get_scheduler_preferences(session, uid, now=NOW).granularity == 30
    # 恰好等于 now 也算过期
    memory.set_preference(session, uid, "granularity", 45, scope=SCHED, expires_at=NOW)
    assert memory.get_scheduler_preferences(session, uid, now=NOW).granularity == 15


@pytest.mark.parametrize(
    "key,value",
    [
        ("day_start_hour", 24),
        ("day_start_hour", "8"),
        ("day_start_hour", True),
        ("day_start_hour", 8.0),
        ("day_end_hour", 0),
        ("max_goal_minutes_per_day", 0),
        ("max_goal_minutes_per_day", 1441),
        ("buffer_minutes", -1),
        ("buffer_minutes", 121),
        ("granularity", 0),
        ("granularity", 121),
        ("lunch", [13, 12]),
        ("lunch", [12]),
        ("lunch", [12, 13, 14]),
        ("lunch", ["12", "13"]),
        ("preferred_windows", [[12, 12]]),
        ("preferred_windows", [[9, "x"]]),
        ("preferred_windows", "9-12"),
    ],
)
def test_invalid_scheduler_values_rejected_on_write(session, user, key, value):
    with pytest.raises(memory.PreferenceError):
        memory.set_preference(session, user.id, key, value, scope=SCHED)


def test_invalid_stored_rows_are_skipped(session, user):
    uid = user.id
    session.add_all([
        UserPreference(user_id=uid, key="granularity", value="0", scope=SCHED),
        UserPreference(user_id=uid, key="preferred_windows", value="not json", scope=SCHED),
        UserPreference(user_id=uid, key="mystery_key", value="42", scope=SCHED),  # 白名单外
    ])
    session.flush()
    assert memory.get_scheduler_preferences(session, uid, now=NOW) == Preferences()


def test_invalid_metadata_rejected(session, user):
    uid = user.id
    with pytest.raises(memory.PreferenceError):
        memory.set_preference(session, uid, "k", 1, confidence=1.5)
    with pytest.raises(memory.PreferenceError):
        memory.set_preference(session, uid, "k", 1, confidence=True)
    with pytest.raises(memory.PreferenceError):
        memory.set_preference(session, uid, "k", 1, source="root")
    with pytest.raises(memory.PreferenceError):
        memory.set_preference(session, uid, "k", 1, expires_at="2027-01-01")
    with pytest.raises(memory.PreferenceError):
        memory.set_preference(session, uid, "  ", 1)
    with pytest.raises(memory.PreferenceError):
        memory.set_preference(session, uid, "notes", object())  # 不可 JSON 序列化


def test_arbitrary_json_value_roundtrip(session, user):
    uid = user.id
    payload = {"rgb": [1, 2, 3], "label": "深蓝"}
    memory.set_preference(session, uid, "favorite_color", payload, scope="ui")
    row = memory.list_preferences(session, uid, scope="ui")[0]
    assert json.loads(row.value) == payload


def test_conflicting_day_bounds_fall_back_to_defaults(session, user):
    uid = user.id
    memory.set_preference(session, uid, "day_start_hour", 20, scope=SCHED)
    memory.set_preference(session, uid, "day_end_hour", 6, scope=SCHED)
    prefs = memory.get_scheduler_preferences(session, uid, now=NOW)
    assert prefs.day_start_hour == Preferences().day_start_hour
    assert prefs.day_end_hour == Preferences().day_end_hour


def test_tenant_isolation(accounts):
    ids, tokens = accounts
    with tenancy.session_scope(tokens[0]) as s:
        memory.set_preference(s, ids[0], "granularity", 30, scope=SCHED)
        row = memory.set_preference(s, ids[0], "lunch", [11, 12], scope=SCHED)
        s.flush()
        row_id = row.id
        assert memory.get_scheduler_preferences(s, ids[0], now=NOW).granularity == 30

    with tenancy.session_scope(tokens[1]) as s:
        assert memory.get_scheduler_preferences(s, ids[1], now=NOW) == Preferences()
        assert memory.list_preferences(s, ids[1]) == []
        assert s.get(UserPreference, row_id) is None  # 直接按 id 也读不到
        assert not memory.delete_preference(s, ids[1], "granularity", scope=SCHED)

    with pytest.raises(tenancy.AccessDenied), tenancy.session_scope(tokens[0]) as s:
        memory.set_preference(s, ids[1], "day_start_hour", 7, scope=SCHED)


def test_conversation_message_cannot_reference_another_tenant(accounts):
    from goalcal.models import Conversation, Message

    ids, tokens = accounts
    with tenancy.session_scope(tokens[0]) as s:
        conversation = Conversation(user_id=ids[0])
        s.add(conversation)
        s.flush()
        conversation_id = conversation.id

    with pytest.raises(tenancy.AccessDenied), tenancy.session_scope(tokens[1]) as s:
        s.add(Message(conversation_id=conversation_id, role="user", content="forged"))
        s.flush()


def test_v1_database_gains_user_preferences(tmp_path):
    from goalcal.migrations import migrate

    path = tmp_path / "v1.db"
    with sqlite3.connect(path) as con:
        con.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT,"
            " disabled BOOLEAN NOT NULL DEFAULT 0, name TEXT, tz TEXT, created_at DATETIME)"
        )
        con.execute("CREATE TABLE invites (digest TEXT PRIMARY KEY, expires REAL NOT NULL, used_by INTEGER)")
        con.execute(
            "CREATE TABLE auth_sessions (digest TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL)"
        )
        con.execute("CREATE TABLE login_limits (key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, reset_at REAL NOT NULL)")
        con.execute(
            "CREATE TABLE ai_usage (user_id INTEGER NOT NULL, day TEXT NOT NULL,"
            " count INTEGER NOT NULL, PRIMARY KEY(user_id,day))"
        )
        con.execute(
            "INSERT INTO users (username, name, tz, created_at)"
            " VALUES ('demo', 'demo', 'Asia/Shanghai', CURRENT_TIMESTAMP)"
        )
        con.execute("PRAGMA user_version=1")
    engine = create_engine(f"sqlite:///{path}")
    migrate(engine)
    migrate(engine)  # 幂等

    with sqlite3.connect(path) as con:
        from goalcal.migrations import VERSION
        assert con.execute("PRAGMA user_version").fetchone()[0] == VERSION
        ddl = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='user_preferences'"
        ).fetchone()[0]
        assert "uq_user_preferences_user_key_scope" in ddl
        for col in ("user_id", "key", "value", "scope", "source", "confidence",
                    "evidence", "expires_at", "created_at", "updated_at"):
            assert col in ddl
        assert con.execute("SELECT username FROM users").fetchone()[0] == "demo"

        def insert(key, scope):
            con.execute(
                "INSERT INTO user_preferences (user_id, key, scope, source, confidence, evidence, value, created_at, updated_at)"
                " VALUES (1, ?, ?, 'user', 1.0, '', 'null', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (key, scope),
            )

        insert("granularity", "scheduler")
        with pytest.raises(sqlite3.IntegrityError):
            insert("granularity", "scheduler")  # key+scope 唯一
        insert("granularity", "global")  # 不同 scope 允许
        insert("day_start_hour", "scheduler")
        con.commit()
        assert con.execute("SELECT COUNT(*) FROM user_preferences").fetchone()[0] == 3
    engine.dispose()


def test_agent_scheduling_uses_global_preferences(session, user):
    from goalcal import agent
    from goalcal.domain import goals

    goal = goals.create_goal(session, user.id, "Evening project", with_template=False)
    step = goals.add_step(session, goal, "Write draft", est_min=30)
    memory.set_preference(session, user.id, "day_start_hour", 20)
    memory.set_preference(session, user.id, "day_end_hour", 24)
    memory.set_preference(session, user.id, "preferred_windows", [[20, 24]])
    now = datetime(2026, 10, 25, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    candidates, _ = agent.propose_for_step(session, user.id, step.id, now, "Asia/Shanghai", days=1)

    assert candidates
    assert all(candidate.start.hour >= 20 for candidate in candidates)


def _add_blocks(session, user_id, blocks, *, tz="Asia/Shanghai", kind="block"):
    for hour, status in blocks:
        start = as_utc_naive(datetime(2026, 10, 1, hour), tz)
        session.add(Event(
            user_id=user_id,
            title="Goal block",
            start_utc=start,
            end_utc=start + timedelta(hours=1),
            kind=kind,
            status=status,
        ))
    session.flush()


@pytest.mark.parametrize(
    "hour,expected",
    [(9, ((6, 12),)), (14, ((12, 18),)), (20, ((18, 24),))],
)
def test_learns_dominant_resolved_window(session, user, hour, expected):
    _add_blocks(session, user.id, [(hour, "done")] * 5)

    prefs = memory.get_scheduler_preferences(session, user.id, now=NOW)

    assert prefs.preferred_windows == expected
    assert memory.list_preferences(session, user.id, include_expired=True) == []
    assert not session.new and not session.dirty


def test_learning_requires_enough_resolved_blocks(session, user):
    _add_blocks(session, user.id, [(9, "done")] * 4)

    assert memory.get_scheduler_preferences(session, user.id, now=NOW) == Preferences()


def test_learning_requires_a_clear_dominant_window(session, user):
    _add_blocks(session, user.id, [(9, "done")] * 3 + [(14, "done")] * 3)

    assert memory.get_scheduler_preferences(session, user.id, now=NOW) == Preferences()


def test_learning_ignores_unresolved_non_block_and_night_events(session, user):
    _add_blocks(session, user.id, [(9, "done")] * 4)
    _add_blocks(session, user.id, [(9, "planned"), (9, "moved")])
    _add_blocks(session, user.id, [(9, "done")] * 5, kind="external")
    _add_blocks(session, user.id, [(3, "done")] * 5)

    assert memory.get_scheduler_preferences(session, user.id, now=NOW) == Preferences()


def test_partial_blocks_count_as_half(session, user):
    _add_blocks(session, user.id, [(9, "done")] * 4 + [(14, "partial")] * 4)

    prefs = memory.get_scheduler_preferences(session, user.id, now=NOW)

    assert prefs.preferred_windows == ((6, 12),)


def test_missed_blocks_add_samples_but_no_score(session, user):
    _add_blocks(session, user.id, [(9, "done")] * 3 + [(20, "missed")] * 2)

    prefs = memory.get_scheduler_preferences(session, user.id, now=NOW)

    assert prefs.preferred_windows == ((6, 12),)


def test_explicit_preferred_windows_override_learning(session, user):
    _add_blocks(session, user.id, [(20, "done")] * 5)
    memory.set_preference(session, user.id, "preferred_windows", [[13, 17]], scope=SCHED)

    prefs = memory.get_scheduler_preferences(session, user.id, now=NOW)

    assert prefs.preferred_windows == ((13, 17),)


def test_expired_explicit_preferred_windows_allow_learning(session, user):
    _add_blocks(session, user.id, [(20, "done")] * 5)
    memory.set_preference(
        session,
        user.id,
        "preferred_windows",
        [[13, 17]],
        scope=SCHED,
        expires_at=NOW - timedelta(seconds=1),
    )

    prefs = memory.get_scheduler_preferences(session, user.id, now=NOW)

    assert prefs.preferred_windows == ((18, 24),)


def test_learning_uses_the_users_timezone(session, user):
    user.tz = "America/Los_Angeles"
    _add_blocks(session, user.id, [(9, "done")] * 5, tz=user.tz)

    prefs = memory.get_scheduler_preferences(session, user.id, now=NOW)

    assert prefs.preferred_windows == ((6, 12),)


def test_learning_isolated_by_user(session, user):
    from goalcal.domain import goals

    other = goals.ensure_user(session, "other", tz="UTC")
    _add_blocks(session, user.id, [(9, "done")] * 5)
    _add_blocks(session, other.id, [(20, "done")] * 5, tz=other.tz)

    assert memory.get_scheduler_preferences(session, user.id, now=NOW).preferred_windows == ((6, 12),)
    assert memory.get_scheduler_preferences(session, other.id, now=NOW).preferred_windows == ((18, 24),)


def test_history_learning_can_be_disabled_and_reenabled(session, user):
    _add_blocks(session, user.id, [(9, "done")] * 5)
    memory.set_preference(session, user.id, memory.LEARNING_ENABLED_KEY, False)

    assert memory.get_scheduler_preferences(session, user.id, now=NOW) == Preferences()

    memory.set_preference(session, user.id, memory.LEARNING_ENABLED_KEY, True)
    assert memory.get_scheduler_preferences(session, user.id, now=NOW).preferred_windows == ((6, 12),)


def test_expired_learning_disable_does_not_block_learning(session, user):
    _add_blocks(session, user.id, [(9, "done")] * 5)
    memory.set_preference(
        session, user.id, memory.LEARNING_ENABLED_KEY, False,
        expires_at=NOW - timedelta(seconds=1),
    )

    assert memory.get_scheduler_preferences(session, user.id, now=NOW).preferred_windows == ((6, 12),)
