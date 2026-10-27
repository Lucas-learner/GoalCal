"""UI controller Goals 写入口的租户隔离回归测试。

直接调用 goalcal.ui.controller 的写入口（monkeypatch runtime/st，session_scope
落到内存 session），断言传入其他用户的 Goal/Step/Event ID 时安静 no-op：
不写库、不泄露对象标题、不产生误导性成功提示；本人操作照常成功。
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import streamlit as st
from sqlalchemy import func, select

from goalcal.domain import goals as svc
from goalcal.models import Checkin, Event, Goal, Step, User

TZ = "Asia/Shanghai"
NOW = datetime(2026, 10, 27, 9, 0, tzinfo=ZoneInfo(TZ))
PLAN = [
    {
        "seq": 1,
        "milestone": "阶段一",
        "title": "整理范围",
        "outcome": "清单",
        "est_min": 25,
        "energy": 0,
        "kind": "admin",
    },
    {
        "seq": 2,
        "milestone": "阶段二",
        "title": "动手练习",
        "outcome": "成果",
        "est_min": 60,
        "energy": 2,
        "kind": "deep",
    },
]


class _UiEnv:
    """monkeypatch 后的控制器运行环境句柄。"""

    def __init__(self, session, state, reruns, identity):
        self.session = session
        self.state = state
        self.reruns = reruns
        self.identity = identity

    def login(self, user_id: int) -> None:
        self.identity["uid"] = user_id

    def assert_quiet_noop(self, *foreign_titles: str) -> None:
        assert self.reruns == []  # 不触发 rerun（与现有 None 分支一致）
        assert self.state.get("flash") is None  # 无成功提示
        for title in foreign_titles:
            assert title not in str(self.state)  # 不泄露对象标题


@pytest.fixture()
def ui_env(monkeypatch, session):
    from goalcal.ui import controller, runtime

    state: dict = {}
    reruns: list = []
    identity: dict = {}

    @contextmanager
    def scope(operation="test"):
        yield session

    monkeypatch.setattr(st, "session_state", state)
    monkeypatch.setattr(st, "rerun", lambda *args, **kwargs: reruns.append(1))
    monkeypatch.setattr(runtime, "session_scope", scope)
    monkeypatch.setattr(runtime, "user_id", lambda: identity["uid"])
    monkeypatch.setattr(runtime, "now", lambda: NOW)
    monkeypatch.setattr(runtime, "timezone", lambda: TZ)
    return _UiEnv(session, state, reruns, identity)


def _user(session, name):
    u = User(name=name)
    session.add(u)
    session.flush()
    return u


def _goal_with_step(session, uid, title):
    goal = svc.create_goal(session, uid, title, with_template=False)
    step = svc.add_step(session, goal, f"{title} step", est_min=30, energy=1, kind="deep")
    session.flush()
    session.refresh(goal)  # 失效集合缓存：写测试与控制器共享同一 session
    return goal, step


def _block(session, user_id, step, start):
    event = Event(
        user_id=user_id,
        title=f"block for {step.title}",
        start_utc=start,
        end_utc=start + timedelta(minutes=step.est_min),
        kind="block",
        step_id=step.id,
    )
    session.add(event)
    session.flush()
    return event


def _checkin_count(session, step_id):
    return session.scalar(select(func.count()).select_from(Checkin).where(Checkin.step_id == step_id))


def _step_titles(session, goal_id):
    rows = session.execute(select(Step.title).where(Step.goal_id == goal_id)).all()
    return [title for (title,) in rows]


@pytest.fixture()
def tenants(session):
    me = _user(session, "me")
    other = _user(session, "other")
    my_goal, my_step = _goal_with_step(session, me.id, "我的目标")
    other_goal, other_step = _goal_with_step(session, other.id, "Other goal")
    return me, other, my_goal, my_step, other_goal, other_step


# ---------------------------------------------------------------- complete_step


def test_complete_step_foreign_id_is_quiet_noop(ui_env, tenants):
    me, _, _, _, _, other_step = tenants
    ui_env.login(me.id)

    from goalcal.ui import controller

    controller.complete_step(other_step.id)

    assert ui_env.session.get(Step, other_step.id).status == "todo"
    assert _checkin_count(ui_env.session, other_step.id) == 0  # 写操作没有发生
    ui_env.assert_quiet_noop("Other goal step")


def test_complete_step_owner_marks_done(ui_env, tenants):
    me, _, my_goal, my_step, _, _ = tenants
    # 三个 todo 步骤：完成一个后仍剩两个，auto_refine 不触发，路径确定
    svc.add_step(ui_env.session, my_goal, "extra 1")
    svc.add_step(ui_env.session, my_goal, "extra 2")
    ui_env.login(me.id)

    from goalcal.ui import controller

    controller.complete_step(my_step.id)

    assert ui_env.session.get(Step, my_step.id).status == "done"
    assert _checkin_count(ui_env.session, my_step.id) == 1
    assert "marked done" in ui_env.state["flash"]
    assert ui_env.reruns == [1]


# --------------------------------------------------------------- unschedule_step


def test_unschedule_step_foreign_pair_is_quiet_noop(ui_env, tenants):
    me, other, _, _, _, other_step = tenants
    other_block = _block(ui_env.session, other.id, other_step, datetime(2026, 10, 28, 1))
    ui_env.login(me.id)

    from goalcal.ui import controller

    controller.unschedule_step(other_step.id, other_block.id)

    assert ui_env.session.get(Event, other_block.id) is not None
    ui_env.assert_quiet_noop("block for Other goal step")


def test_unschedule_step_own_event_with_foreign_step_is_quiet_noop(ui_env, tenants):
    me, _, _, my_step, _, other_step = tenants
    my_block = _block(ui_env.session, me.id, my_step, datetime(2026, 10, 28, 1))
    ui_env.login(me.id)

    from goalcal.ui import controller

    # 自己的 event_id 配别人的 step_id：event.step_id 对不上，不能删
    controller.unschedule_step(other_step.id, my_block.id)

    assert ui_env.session.get(Event, my_block.id) is not None
    ui_env.assert_quiet_noop()


def test_unschedule_step_foreign_event_with_own_step_is_quiet_noop(ui_env, tenants):
    me, other, _, my_step, _, other_step = tenants
    other_block = _block(ui_env.session, other.id, other_step, datetime(2026, 10, 28, 1))
    ui_env.login(me.id)

    from goalcal.ui import controller

    # 别人的 event：event.user_id 不符，即使 step_id 巧合相等也不能删
    controller.unschedule_step(my_step.id, other_block.id)

    assert ui_env.session.get(Event, other_block.id) is not None
    ui_env.assert_quiet_noop()


def test_unschedule_step_owner_removes_block(ui_env, tenants):
    me, _, _, my_step, _, _ = tenants
    my_block = _block(ui_env.session, me.id, my_step, datetime(2026, 10, 28, 1))
    ui_env.login(me.id)

    from goalcal.ui import controller

    controller.unschedule_step(my_step.id, my_block.id)

    assert ui_env.session.get(Event, my_block.id) is None
    assert "Block removed" in ui_env.state["flash"]
    assert ui_env.reruns == [1]


# --------------------------------------------------------------------- add_step


def test_add_step_foreign_goal_is_quiet_noop(ui_env, tenants):
    me, _, _, _, other_goal, _ = tenants
    before = _step_titles(ui_env.session, other_goal.id)
    ui_env.login(me.id)

    from goalcal.ui import controller

    controller.add_step(other_goal.id, "Injected step", 30, 1, "deep")

    assert _step_titles(ui_env.session, other_goal.id) == before
    ui_env.assert_quiet_noop("Injected step")


def test_add_step_owner_adds(ui_env, tenants):
    me, _, my_goal, _, _, _ = tenants
    ui_env.login(me.id)

    from goalcal.ui import controller

    controller.add_step(my_goal.id, "New step", 30, 1, "deep")

    assert "New step" in _step_titles(ui_env.session, my_goal.id)
    assert "Added step" in ui_env.state["flash"]
    assert ui_env.reruns == [1]


# -------------------------------------------------------------------- edit_step


def test_edit_step_foreign_id_is_quiet_noop(ui_env, tenants):
    me, _, _, _, _, other_step = tenants
    ui_env.login(me.id)

    from goalcal.ui import controller

    controller.edit_step(other_step.id, "Hijacked", "m", "o", 30, 1, "deep")

    step = ui_env.session.get(Step, other_step.id)
    assert step.title == "Other goal step"
    assert (step.est_min, step.energy, step.kind) == (30, 1, "deep")
    ui_env.assert_quiet_noop("Hijacked")


def test_edit_step_owner_updates(ui_env, tenants):
    me, _, _, my_step, _, _ = tenants
    ui_env.login(me.id)

    from goalcal.ui import controller

    controller.edit_step(my_step.id, "Renamed", "milestone", "outcome", 45, 2, "admin")

    step = ui_env.session.get(Step, my_step.id)
    assert (step.title, step.milestone, step.outcome) == ("Renamed", "milestone", "outcome")
    assert (step.est_min, step.energy, step.kind) == (45, 2, "admin")
    assert "Updated step" in ui_env.state["flash"]


# ------------------------------------------------------------------ delete_step


def test_delete_step_foreign_id_is_quiet_noop(ui_env, tenants):
    me, _, _, _, other_goal, other_step = tenants
    ui_env.login(me.id)

    from goalcal.ui import controller

    controller.delete_step(other_step.id)

    assert ui_env.session.get(Step, other_step.id) is not None
    assert _step_titles(ui_env.session, other_goal.id) == ["Other goal step"]
    ui_env.assert_quiet_noop()


def test_delete_step_owner_deletes(ui_env, tenants):
    me, _, my_goal, my_step, _, _ = tenants
    ui_env.state["pending"] = {"candidates": [], "step_id": my_step.id, "origin": "goal"}
    ui_env.login(me.id)

    from goalcal.ui import controller

    controller.delete_step(my_step.id)

    assert ui_env.session.get(Step, my_step.id) is None
    assert ui_env.state["pending"] is None  # 关联候选状态随删除清理
    assert "Deleted step" in ui_env.state["flash"]


# ----------------------------------------------------------------- archive_goal


def test_archive_goal_foreign_id_is_quiet_noop(ui_env, tenants):
    me, _, _, _, other_goal, _ = tenants
    ui_env.login(me.id)

    from goalcal.ui import controller

    controller.archive_goal(other_goal.id)

    assert ui_env.session.get(Goal, other_goal.id).status == "active"
    ui_env.assert_quiet_noop("Other goal")


def test_archive_goal_owner_archives(ui_env, tenants):
    me, _, my_goal, my_step, _, _ = tenants
    ui_env.state["pending"] = {"candidates": [], "step_id": my_step.id, "origin": "goal"}
    ui_env.login(me.id)

    from goalcal.ui import controller

    controller.archive_goal(my_goal.id)

    assert ui_env.session.get(Goal, my_goal.id).status == "archived"
    assert ui_env.state["pending"] is None
    assert "Archived goal" in ui_env.state["flash"]


# ------------------------------------------------------------------ toggle_pin


def test_toggle_pin_foreign_id_is_quiet_noop(ui_env, tenants):
    me, _, _, _, other_goal, _ = tenants
    ui_env.login(me.id)

    from goalcal.ui import controller

    controller.toggle_pin(other_goal.id)

    assert ui_env.session.get(Goal, other_goal.id).pinned is False
    ui_env.assert_quiet_noop("Other goal")


def test_toggle_pin_owner_toggles(ui_env, tenants):
    me, _, my_goal, _, _, _ = tenants
    ui_env.login(me.id)

    from goalcal.ui import controller

    controller.toggle_pin(my_goal.id)
    assert ui_env.session.get(Goal, my_goal.id).pinned is True
    assert "Pinned" in ui_env.state["flash"]

    controller.toggle_pin(my_goal.id)
    assert ui_env.session.get(Goal, my_goal.id).pinned is False
    assert "Unpinned" in ui_env.state["flash"]


# -------------------------------------------------------- preview/apply replan


def test_preview_replan_foreign_goal_skips_llm(ui_env, monkeypatch, tenants):
    me, _, _, _, other_goal, _ = tenants
    ui_env.login(me.id)
    calls: list = []

    def fake_propose(session, goal, now, tz, *, instruction=""):
        calls.append(goal.id)
        return PLAN

    from goalcal.ui import controller

    monkeypatch.setattr(controller.agent, "propose_goal_plan", fake_propose)
    controller.preview_replan(other_goal.id, "make it lighter")

    assert calls == []  # 越权时不调 LLM
    assert "replan_draft" not in ui_env.state
    ui_env.assert_quiet_noop("Other goal")


def test_preview_replan_owner_previews(ui_env, monkeypatch, tenants):
    me, _, my_goal, _, _, _ = tenants
    ui_env.login(me.id)
    calls: list = []

    def fake_propose(session, goal, now, tz, *, instruction=""):
        calls.append(goal.id)
        return PLAN

    from goalcal.ui import controller

    monkeypatch.setattr(controller.agent, "propose_goal_plan", fake_propose)
    controller.preview_replan(my_goal.id, "make it lighter")

    assert calls == [my_goal.id]
    assert ui_env.state["replan_draft"]["goal_id"] == my_goal.id
    assert ui_env.state["replan_draft"]["plan"] == PLAN
    assert "Preview ready" in ui_env.state["flash"]
    assert ui_env.reruns == [1]


def test_apply_replan_foreign_goal_is_quiet_noop(ui_env, tenants):
    me, _, _, _, other_goal, other_step = tenants
    ui_env.state["replan_draft"] = {"goal_id": other_goal.id, "plan": PLAN, "instruction": "x"}
    ui_env.login(me.id)
    before = _step_titles(ui_env.session, other_goal.id)

    from goalcal.ui import controller

    controller.apply_replan(other_goal.id)

    assert _step_titles(ui_env.session, other_goal.id) == before == ["Other goal step"]
    assert ui_env.session.get(Step, other_step.id) is not None
    # 越权时不做状态清理：草稿保留，避免误导
    assert ui_env.state["replan_draft"]["goal_id"] == other_goal.id
    ui_env.assert_quiet_noop("Other goal")


def test_apply_replan_owner_applies(ui_env, tenants):
    me, _, my_goal, my_step, _, _ = tenants
    goal_id, my_step_id = my_goal.id, my_step.id  # 调用后实例被删，先取 id
    ui_env.state["replan_draft"] = {"goal_id": goal_id, "plan": PLAN, "instruction": "x"}
    ui_env.login(me.id)

    from goalcal.ui import controller

    controller.apply_replan(goal_id)

    assert _step_titles(ui_env.session, goal_id) == ["整理范围", "动手练习"]
    # 旧步骤行已删除（get 会命中身份缓存里已删除的实例，用计数查库更直接）
    assert ui_env.session.scalar(
        select(func.count()).select_from(Step).where(Step.id == my_step_id)
    ) == 0
    assert "replan_draft" not in ui_env.state
    assert "Applied the revised plan" in ui_env.state["flash"]
