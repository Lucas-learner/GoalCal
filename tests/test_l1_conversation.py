"""L1 多轮对话测试：fake L1 客户端（不打真实付费 API）驱动完整 AppTest 流程。

覆盖：每轮一次调用上限、动作执行、"好/可以"接受最近提议、打断后恢复、
clarify 不循环、L1 失败降级 rules + 熔断。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from goalcal import auth
from goalcal.db import init_db, session_scope
from goalcal.models import Event, Goal, Step, User

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture(autouse=True)
def _ui_account(monkeypatch):
    monkeypatch.setenv("GOALCAL_UI_TEST_ACCOUNT", "ui_" + uuid.uuid4().hex[:12])


def _app() -> AppTest:
    import os

    account = os.environ["GOALCAL_UI_TEST_ACCOUNT"]
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, at.exception
    init_db()
    with session_scope() as s:
        user = s.scalar(select(User).where(User.username == account))
        if user is None:
            from goalcal.domain import calendar_service as cal, goals, refine
            from goalcal.timeutil import local_now

            user = User(
                name="UI test", username=account, tz="Asia/Shanghai",
                password_hash=auth.credentials(account, "test-password-long", "Asia/Shanghai")[1],
            )
            s.add(user)
            s.flush()
            now = local_now("Asia/Shanghai")
            goal = goals.create_goal(s, user.id, "Publish my first short video")
            goals.complete_step(s, goal.steps[0], raw_text="explicit test fixture")
            refine.refine_upcoming(s, goal)
            cal.create_event(s, user.id, "Test appointment", now + timedelta(days=1),
                             now + timedelta(days=1, hours=1), tz="Asia/Shanghai")
        username = user.username
    next(t for t in at.text_input if t.key == "login_name").set_value(username)
    next(t for t in at.text_input if t.key == "login_password").set_value("test-password-long")
    next(b for b in at.button if b.label == "Log in").click().run()
    assert not at.exception, at.exception
    assert at.chat_input
    return at


class FakeTurn:
    """按消息内容返回脚本化决定的假 L1 客户端；记录每次调用以验证上限。"""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def complete_json(self, system, user, temperature=0.0):
        payload = json.loads(user)
        self.calls.append(payload)
        result = self.handler(payload)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture()
def l1(monkeypatch):
    from goalcal.llm import turn
    from goalcal.ui import controller

    turn.reset_breaker()

    def install(handler):
        fake = FakeTurn(handler)
        monkeypatch.setattr(controller, "turn_client", lambda: fake)
        return fake

    yield install
    turn.reset_breaker()


def _say(at, text):
    at.chat_input[0].set_value(text).run()
    assert not at.exception, at.exception


def _event_titles(at):
    with session_scope() as s:
        return sorted(
            e.title for e in s.scalars(select(Event).where(Event.user_id == at.session_state["user_id"]))
        )


def test_l1_executes_action_with_one_call_per_turn(l1):
    fake = l1(lambda payload: {
        "relation": "new_command", "intent": "add_event", "title": "Team lunch",
        "when_text": "tomorrow 10am", "reply": "好的，我来安排。",
    })
    at = _app()
    _say(at, "Add team lunch tomorrow 10am")

    assert "Team lunch" in _event_titles(at)
    assert len(fake.calls) == 1
    assert fake.calls[0]["message"] == "Add team lunch tomorrow 10am"
    # 执行结果文案是确定性代码出的，不是模型自称成功
    assert any("Added" in m["content"] and "Team lunch" in m["content"]
               for m in at.session_state["messages"])


def test_l1_chat_reply_writes_nothing(l1):
    fake = l1(lambda payload: {
        "relation": "new_command", "intent": "chat", "reply": "听起来今天有点满，先喝口水。",
    })
    at = _app()
    before = _event_titles(at)
    _say(at, "今天有点焦虑")

    assert at.session_state["messages"][-1]["content"] == "听起来今天有点满，先喝口水。"
    assert _event_titles(at) == before
    assert len(fake.calls) == 1


def test_accept_offer_confirms_goal_draft(l1):
    def handler(payload):
        if payload["message"] == "好":
            return {"relation": "accept_offer", "intent": "chat", "reply": "好，创建并排期。"}
        return {"relation": "new_command", "intent": "create_goal",
                "title": "Publish my first short video", "when_text": "in 30 days",
                "reply": "我先给你看个草稿。"}

    fake = l1(handler)
    at = _app()
    _say(at, "I want to publish my first short video in 30 days")
    assert at.session_state["goal_draft"] is not None
    with session_scope() as s:
        goals_before = len(list(s.scalars(select(Goal).where(
            Goal.user_id == at.session_state["user_id"], Goal.title == "Publish my first short video"))))

    _say(at, "好")

    assert at.session_state["goal_draft"] is None
    with session_scope() as s:
        assert len(list(s.scalars(select(Goal).where(
            Goal.user_id == at.session_state["user_id"],
            Goal.title == "Publish my first short video")))) == goals_before + 1
    assert len(fake.calls) == 1  # 逐字接受提议走零成本短路


def test_affirmative_does_not_guess_top_candidate(l1):
    def handler(payload):
        if payload["message"] == "可以":
            return {"relation": "accept_offer", "intent": "chat", "reply": "好，定第一个。"}
        return {"relation": "new_command", "intent": "schedule_step", "reply": "给你几个时段。"}

    l1(handler)
    at = _app()
    _say(at, "Find time for my next step")
    pending = at.session_state["pending"]
    assert pending and pending["candidates"]
    _say(at, "可以")
    assert at.session_state["pending"] is not None
    assert "请选择一个建议时段" in at.session_state["messages"][-1]["content"]
    with session_scope() as s:
        blocks = list(s.scalars(select(Event).where(Event.step_id == pending["step_id"])))
        assert not blocks

    _say(at, "第一个")
    assert at.session_state["pending"] is None
    with session_scope() as s:
        blocks = list(s.scalars(select(Event).where(Event.step_id == pending["step_id"])))
        assert len(blocks) == 1


def test_interrupt_pushes_return_to_and_resume_restores(l1):
    def handler(payload):
        message = payload["message"]
        if "恢复" in message:
            return {"relation": "resume", "intent": "chat", "reply": "好，回到刚才的目标。"}
        return {"relation": "new_command", "intent": "add_event",
                "title": "review meeting", "when_text": "tomorrow 10am", "reply": "已加。"}

    l1(handler)
    at = _app()
    # goal_brief 是旧版澄清态（新流程没期限也直接出草稿）；
    # 手工注入以覆盖旧会话的打断/恢复兼容路径
    from goalcal.domain.planner import GoalBrief

    at.session_state["goal_brief"] = GoalBrief(title="学会弹钢琴", raw="我要学会弹钢琴")
    _say(at, "Add review meeting tomorrow 10am")  # 打断澄清中的目标
    assert at.session_state["goal_brief"] is None
    assert "review meeting" in _event_titles(at)
    ctx = at.session_state["dialog_context"]
    assert [entry["kind"] for entry in ctx.return_to] == ["goal_brief"]

    _say(at, "我想恢复")  # 恢复被打断的目标澄清
    assert at.session_state["goal_brief"] is not None
    assert at.session_state["dialog_context"].return_to == []
    assert at.session_state["dialog_context"].kind == "goal_brief"


def test_clarify_escalates_then_stops(l1):
    """clarify 三轮升级：L1 短问 → 确定性选项清单 → 明确"不猜"并停止，不循环。"""
    l1(lambda payload: {"relation": "clarify", "intent": "chat", "reply": "你是指哪一步？"})
    at = _app()

    _say(at, "hmm")
    assert at.session_state["messages"][-1]["content"] == "你是指哪一步？"
    assert at.session_state["dialog_context"].clarify_count == 1

    _say(at, "hmm")
    from goalcal import agent
    second = at.session_state["messages"][-1]["content"]
    assert second != "你是指哪一步？"
    assert "•" in second  # 确定性的可执行选项清单
    assert at.session_state["dialog_context"].clarify_count == 2

    _say(at, "hmm")
    assert at.session_state["messages"][-1]["content"] == agent.CLARIFY_STOP
    assert at.session_state["dialog_context"].clarify_count == 0  # 停止追问，回到 idle

    _say(at, "hmm")  # 计数已清：若仍不明，从第一轮重新开始
    assert at.session_state["messages"][-1]["content"] == "你是指哪一步？"


def test_l1_failure_falls_back_to_rules_then_breaker(l1):
    fake = l1(lambda payload: RuntimeError("boom"))
    at = _app()

    for index in range(3):
        _say(at, f"Add fallback meeting {index} tomorrow 10am")
        assert f"fallback meeting {index}" in _event_titles(at)  # 规则降级仍可执行
    assert len(fake.calls) == 3

    _say(at, "Add fallback meeting 3 tomorrow 10am")  # 熔断打开：不再白等模型
    assert len(fake.calls) == 3
    assert "fallback meeting 3" in _event_titles(at)
    assert at.session_state["dialog_context"].kind == "idle"


def _goal_status(at, title):
    with session_scope() as s:
        goal = s.scalar(select(Goal).where(
            Goal.user_id == at.session_state["user_id"], Goal.title == title))
        return goal.status if goal is not None else None


def test_l1_risky_intent_always_requires_confirmation(l1):
    """H1：L1 提议 delete_goal（即使语气确定）也先 pending_action，确认后才执行。"""
    l1(lambda payload: {
        "relation": "new_command", "intent": "delete_goal",
        "target": "Publish my first short video", "reply": "好的，已删除。",
    })
    at = _app()
    _say(at, "Delete my video goal")

    assert _goal_status(at, "Publish my first short video") == "active"  # 未执行
    assert at.session_state["pending_action"] is not None
    assert any("please confirm" in m["content"] for m in at.session_state["messages"])

    _say(at, "yes")  # 逐字确认路径，不再问模型

    assert _goal_status(at, "Publish my first short video") == "archived"
    assert at.session_state["pending_action"] is None


@pytest.mark.parametrize("answer,relation,expected_status", [
    ("好的，确认吧", "accept_offer", "archived"),
    ("这次先不要删除", "reject_offer", "active"),
])
def test_pending_action_natural_answer(l1, answer, relation, expected_status):
    """危险操作的自然语言接受或拒绝都沿原确认路径处理。"""
    def handler(payload):
        if payload["message"] == answer:
            return {"relation": relation, "intent": "chat", "reply": "好的。"}
        return {"relation": "new_command", "intent": "delete_goal",
                "target": "Publish my first short video", "reply": "请确认。"}

    fake = l1(handler)
    at = _app()
    _say(at, "Delete my video goal")
    assert at.session_state["pending_action"] is not None

    _say(at, answer)

    assert len(fake.calls) == 2
    assert at.session_state["pending_action"] is None
    assert _goal_status(at, "Publish my first short video") == expected_status


def test_pending_action_ambiguous_answer_survives_l1_failure(l1):
    """L1 失败时不丢危险动作确认，也不把含糊回复当成同意。"""
    def handler(payload):
        if payload["message"] == "maybe later":
            return RuntimeError("turn unavailable")
        return {"relation": "new_command", "intent": "delete_goal",
                "target": "Publish my first short video", "reply": "请确认。"}

    l1(handler)
    at = _app()
    _say(at, "Delete my video goal")
    _say(at, "maybe later")

    assert at.session_state["pending_action"] is not None
    assert _goal_status(at, "Publish my first short video") == "active"
    assert "confirm or cancel" in at.session_state["messages"][-1]["content"]


def test_l1_failure_never_calls_plan_layer(l1, monkeypatch):
    """H2：L1 失败后的降级路径整轮不调任何 LLM——建目标走确定性模板。"""
    from goalcal.llm import plan as plan_mod

    def no_plan_client():
        raise AssertionError("L1 已失败，本轮不允许再调 plan 层")

    monkeypatch.setattr(plan_mod, "plan_client", no_plan_client)
    fake = l1(lambda payload: RuntimeError("boom"))
    at = _app()

    _say(at, "I want to publish my first short video in 30 days")

    assert len(fake.calls) == 1  # 只尝试了 L1 一次
    assert at.session_state["goal_draft"] is not None  # 模板草稿照常生成


def test_resume_goal_is_a_draft_exit_intent():
    """M5：恢复暂停目标是明确新指令，草稿显示中应退出草稿而不是改草稿。"""
    from goalcal.ui import controller

    assert "resume_goal" in controller._DRAFT_EXIT_INTENTS


def test_accept_offer_marks_offer_accepted(l1):
    """接受提议：匹配的 offer 标记 accepted，其余 open 失效；历史保留不残留 open。"""
    def handler(payload):
        if payload["message"] == "好":
            return {"relation": "accept_offer", "intent": "chat", "reply": "好，创建并排期。"}
        return {"relation": "new_command", "intent": "create_goal",
                "title": "Publish my first short video", "when_text": "in 30 days",
                "reply": "我先给你看个草稿。", "offer": "要不要创建并排期？",
                "offer_intent": "create_goal"}

    l1(handler)
    at = _app()
    _say(at, "I want to publish my first short video in 30 days")
    offers = at.session_state["dialog_context"].offers
    # 确定性状态提议（goal_draft）与 L1 文本提议（chat，带可执行 intent）都登记为完整记录
    by_kind = {o["kind"]: o for o in offers}
    assert by_kind["goal_draft"]["status"] == "open"
    assert by_kind["chat"]["text"] == "要不要创建并排期？"
    assert by_kind["chat"]["intent"] == "create_goal"
    assert by_kind["goal_draft"]["subject"]["title"] == "Publish my first short video"
    assert all(o["id"] and o["created_ts"] and o["expires_ts"] for o in offers)

    _say(at, "好")

    assert at.session_state["goal_draft"] is None  # 草稿已确认创建
    offers = at.session_state["dialog_context"].offers
    statuses = {o["kind"]: o["status"] for o in offers}
    assert statuses["goal_draft"] == "accepted"
    assert statuses["chat"] == "expired"  # 旧 L1 文本提议随接受失效
    # 确认后回复带了首批候选时段：新的 candidates 提议登记为 open
    assert statuses["candidates"] == "open"


def test_l1_failure_fallback_never_touches_llm_entries(l1, monkeypatch):
    """N2：L1 失败后的降级轮，event_plan/refine/plan 的 LLM 入口都不会被调用。"""
    from goalcal import agent
    from goalcal.llm import plan as plan_mod

    def no_llm(*args, **kwargs):
        raise AssertionError("L1 失败后整轮不得再调任何 LLM")

    monkeypatch.setattr(plan_mod, "plan_client", no_llm)
    monkeypatch.setattr("goalcal.llm.client.LLMClient", no_llm)
    l1(lambda payload: RuntimeError("boom"))
    at = _app()  # fixture 自己会调 refine_upcoming 续批，必须在打补丁之前建好
    monkeypatch.setattr(agent.event_plan, "extract_events", no_llm)
    monkeypatch.setattr(agent.refine, "_llm_defs", no_llm)
    monkeypatch.setattr(agent.refine, "_upcoming_llm_defs", no_llm)

    # 明确写日历但没有时间：降级后不再从上下文提取，确定性追问
    _say(at, "帮我安排进日历")
    assert any("安排在几点" in m["content"]
               for m in at.session_state["messages"])
    assert at.session_state["event_draft"] is None

    # 要求拆细步骤：降级后用确定性规则拆分，不走 LLM
    _say(at, "下一步拆细一点")
    assert any("split" in m["content"] for m in at.session_state["messages"])


def test_reject_offer_cancels_goal_draft_flow(l1):
    """reject 状态绑定提议 = 退出该流程：拒绝草稿提议即丢弃草稿（与逐字"取消"同语义）。"""
    def handler(payload):
        if payload["message"] == "这次先不建了":
            return {"relation": "reject_offer", "intent": "chat", "reply": "好，草稿先扔掉。"}
        return {"relation": "new_command", "intent": "create_goal",
                "title": "Learn guitar", "when_text": "in 30 days",
                "reply": "先看个草稿。", "offer": "要创建吗？", "offer_intent": "create_goal"}

    fake = l1(handler)
    at = _app()
    _say(at, "I want to learn guitar in 30 days")
    assert at.session_state["goal_draft"] is not None

    _say(at, "这次先不建了")  # 非逐字短语：走 L1 reject_offer，验证控制器拒绝路径

    assert at.session_state["messages"][-1]["content"] == "好，草稿先扔掉。"
    assert at.session_state["goal_draft"] is None  # 拒绝草稿提议 = 取消草稿
    with session_scope() as s:
        assert not list(s.scalars(select(Goal).where(
            Goal.user_id == at.session_state["user_id"], Goal.title == "Learn guitar")))
    offers = at.session_state["dialog_context"].offers
    assert not [o for o in offers if o["status"] == "open"]
    statuses = {o["kind"]: o["status"] for o in offers}
    assert statuses["goal_draft"] == "declined"
    assert statuses["chat"] == "expired"  # 同轮其它 open 提议一并失效
    assert len(fake.calls) == 2  # 每轮一次


def test_reject_chat_offer_keeps_all_state(l1):
    """reject 纯 chat 建议：只标记拒绝，不动任何会话状态。"""
    def handler(payload):
        if payload["message"] == "no thanks":
            return {"relation": "reject_offer", "intent": "chat", "reply": "好，不安排。"}
        return {"relation": "new_command", "intent": "chat",
                "reply": "要不要我给下一步找个时间？",
                "offer": "要不要我给下一步找个时间？", "offer_intent": "schedule_step"}

    l1(handler)
    at = _app()
    _say(at, "随便聊聊")
    offers = at.session_state["dialog_context"].offers
    assert [o["kind"] for o in offers if o["status"] == "open"] == ["chat"]

    _say(at, "no thanks")

    assert at.session_state["messages"][-1]["content"] == "好，不安排。"
    offers = at.session_state["dialog_context"].offers
    assert not [o for o in offers if o["status"] == "open"]
    assert offers[-1]["status"] == "declined"
    assert at.session_state["pending"] is None  # 没有产生任何状态变化


def test_chat_offer_without_intent_is_not_registered(l1):
    """M1：没有 offer_intent 的闲聊提议不登记为可接受 offer（避免"好"落空）。"""
    l1(lambda payload: {"relation": "new_command", "intent": "chat",
                        "reply": "要不要喝杯茶休息一下？", "offer": "要不要喝杯茶休息一下？"})
    at = _app()
    _say(at, "今天有点累")

    assert at.session_state["dialog_context"].offers == []


def test_chat_offer_with_intent_executes_on_accept(l1):
    """M1：L1 闲聊发可执行提议 → "好" → 真实执行（schedule_step 出候选），不是失效提示。"""
    def handler(payload):
        if payload["message"] == "好":
            return {"relation": "accept_offer", "intent": "chat", "reply": "好，找时间。"}
        return {"relation": "new_command", "intent": "chat",
                "reply": "要不要我帮你给下一步找时间？",
                "offer": "要不要我帮你给下一步找时间？", "offer_intent": "schedule_step"}

    l1(handler)
    at = _app()
    _say(at, "随便聊聊")

    _say(at, "好")

    pending = at.session_state["pending"]  # schedule_step 真执行：给出候选时段
    assert pending and pending["candidates"]
    assert "no longer on the table" not in at.session_state["messages"][-1]["content"]


def _add_running_goal(at):
    from goalcal.domain import goals as goal_svc

    with session_scope() as s:
        goal = goal_svc.create_goal(
            s, at.session_state["user_id"], "完成半马",
            step_defs=[{"seq": 0, "title": "逐步增加单次跑步距离",
                        "est_min": 45, "energy": 1, "kind": "physical"}],
        )
        return goal.id, goal.steps[0].id


def _add_tennis_goal(at):
    from goalcal.domain import goals as goal_svc

    with session_scope() as s:
        goal = goal_svc.create_goal(
            s, at.session_state["user_id"], "Learn tennis",
            step_defs=[{"seq": 0, "title": "Find a beginner tennis lesson",
                        "est_min": 40, "energy": 1, "kind": "admin"}],
        )
        return goal.id, goal.steps[0].id


def test_tennis_timeline_is_confirmed_before_plan_and_creation(l1):
    l1(lambda payload: {"relation": "new_command", "intent": "create_goal",
                        "title": "Learn tennis", "reply": "Let's plan tennis."})
    at = _app()
    _say(at, "I'd like to learn tennis")
    assert at.session_state["goal_brief"] is not None
    assert at.session_state["goal_draft"] is None
    with session_scope() as s:
        assert s.scalar(select(Goal).where(
            Goal.user_id == at.session_state["user_id"], Goal.title == "Learn tennis")) is None

    _say(at, "deadline is one month")
    draft = at.session_state["goal_draft"]
    assert draft is not None
    assert draft.deadline == datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=30)
    assert at.session_state["goal_brief"] is None

    next(b for b in at.button if b.label == "Create & schedule").click().run()
    assert not at.exception, at.exception
    with session_scope() as s:
        goal = s.scalar(select(Goal).where(
            Goal.user_id == at.session_state["user_id"], Goal.title == "Learn tennis"))
        assert goal is not None and goal.deadline == draft.deadline
        assert at.session_state["pending"]["step_id"] in {step.id for step in goal.steps}
    assert [m["role"] for m in at.session_state["messages"][-3:]] == [
        "assistant", "user", "assistant"
    ]

    # 已创建后仍可明确改期限；待选时段随旧期限失效，不会只给一句空口答复。
    _say(at, "deadline is two months")
    with session_scope() as s:
        goal = s.scalar(select(Goal).where(
            Goal.user_id == at.session_state["user_id"], Goal.title == "Learn tennis"))
        assert goal.deadline == datetime.now(ZoneInfo("Asia/Shanghai")).date() + timedelta(days=60)
    assert at.session_state["pending"] is None
    assert "updated" in at.session_state["messages"][-1]["content"]


def test_refine_offer_acceptance_is_bound_to_tennis(l1):
    def handler(payload):
        return {"relation": "new_command", "intent": "chat",
                "reply": "Want me to refine Learn tennis?",
                "offer": "Want me to refine Learn tennis?",
                "offer_intent": "refine_steps"}

    l1(handler)
    at = _app()
    running_goal_id, _ = _add_running_goal(at)
    tennis_goal_id, tennis_step_id = _add_tennis_goal(at)
    _say(at, "Please refine Learn tennis")
    offer = at.session_state["dialog_context"].offers[-1]
    assert offer["payload"]["goal_id"] == tennis_goal_id
    assert offer["payload"]["step_id"] == tennis_step_id
    _say(at, "yes")
    with session_scope() as s:
        running = s.get(Goal, running_goal_id)
        tennis = s.get(Goal, tennis_goal_id)
        assert len(running.steps) == 1
        assert len(tennis.steps) > 1


def test_named_tennis_refine_overrides_stale_model_target(l1):
    at = _app()
    running_goal_id, running_step_id = _add_running_goal(at)
    tennis_goal_id, _ = _add_tennis_goal(at)
    with session_scope() as s:
        running_title = s.get(Step, running_step_id).title
    l1(lambda payload: {"relation": "new_command", "intent": "refine_steps",
                        "progress_target": running_title, "reply": "I'll refine it."})
    _say(at, "Please refine my tennis goal")
    with session_scope() as s:
        assert len(s.get(Goal, running_goal_id).steps) == 1
        assert len(s.get(Goal, tennis_goal_id).steps) > 1


def test_named_tennis_booking_replaces_running_candidates(l1):
    l1(lambda payload: {"relation": "new_command", "intent": "schedule_step",
                        "reply": "I can find times for 完成半马."})
    at = _app()
    _, running_step_id = _add_running_goal(at)
    _, tennis_step_id = _add_tennis_goal(at)
    _say(at, "Find time for 完成半马")
    assert at.session_state["pending"]["step_id"] == running_step_id

    _say(at, "I want to book tennis goal")
    assert at.session_state["pending"]["step_id"] == tennis_step_id
    assert "tennis" in at.session_state["messages"][-1]["content"].lower()


def test_running_offer_stays_bound_to_goal_until_calendar_booking(l1, monkeypatch):
    """多个目标时，'可以'只能找半马时段；笼统写日历不能丢候选或猜时间。"""
    from goalcal.ui import controller

    now = datetime(2026, 10, 27, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(controller.runtime, "now", lambda: now)

    def handler(payload):
        message = payload["message"]
        if message == "我想跑步":
            return {"relation": "new_command", "intent": "chat",
                    "reply": "接着「完成半马」练习。要我把今晚留给跑步吗？",
                    "offer": "要我把今晚留给跑步吗？", "offer_intent": "schedule_step",
                    "offer_target": "完成半马", "offer_when_text": "今晚"}
        if message == "可以":
            return {"relation": "accept_offer", "intent": "schedule_step", "reply": "好。"}
        return {"relation": "new_command", "intent": "chat", "reply": "我不能写日历。"}

    l1(handler)
    at = _app()  # 视频目标先创建，不能误排到它的下一步
    goal_id, step_id = _add_running_goal(at)

    _say(at, "我想跑步")
    offer = at.session_state["dialog_context"].offers[-1]
    assert offer["payload"]["goal_id"] == goal_id
    assert offer["payload"]["step_id"] == step_id

    _say(at, "可以")
    pending = at.session_state["pending"]
    assert pending and pending["step_id"] == step_id
    assert pending["candidates"]
    assert "写进日历" in at.session_state["messages"][-1]["content"]

    _say(at, "帮我安排进日历")
    assert at.session_state["pending"]["step_id"] == step_id
    assert "选一个建议时段" in at.session_state["messages"][-1]["content"]

    _say(at, "第一个")
    assert "已将" in at.session_state["messages"][-1]["content"]
    with session_scope() as s:
        events = list(s.scalars(select(Event).where(
            Event.user_id == at.session_state["user_id"], Event.step_id == step_id)))
        assert len(events) == 1


def test_activity_choice_inherits_prior_evening_window(l1, monkeypatch):
    """A terse choice of running keeps the evening offered in the previous turn."""
    from goalcal.ui import controller

    now = datetime(2026, 10, 27, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(controller.runtime, "now", lambda: now)

    def handler(payload):
        if payload["message"] == "晚上我可以做什么":
            return {"relation": "new_command", "intent": "chat",
                    "reply": "今晚可以练吉他或跑半马，想先安排哪一个？"}
        return {"relation": "new_command", "intent": "schedule_step",
                "reply": "先推进「完成半马」吧，我来找时间。"}

    l1(handler)
    at = _app()
    _, step_id = _add_running_goal(at)
    _say(at, "晚上我可以做什么")
    _say(at, "我想跑步")

    pending = at.session_state["pending"]
    assert pending and pending["step_id"] == step_id
    assert pending["candidates"]
    assert all(candidate.start.date() == now.date() and 18 <= candidate.start.hour < 23
               for candidate in pending["candidates"])


def test_rule_fallback_offer_keeps_evening_from_advice_question(l1, monkeypatch):
    from goalcal.llm.client import LLMUnavailable
    from goalcal.ui import controller

    now = datetime(2026, 10, 27, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(controller.runtime, "now", lambda: now)
    l1(lambda payload: LLMUnavailable("offline"))
    at = _app()
    _, step_id = _add_running_goal(at)
    _say(at, "晚上我可以做什么")
    _say(at, "我想跑步")
    _say(at, "可以")

    pending = at.session_state["pending"]
    assert pending and pending["step_id"] == step_id
    assert pending["candidates"]
    assert all(candidate.start.date() == now.date() and 18 <= candidate.start.hour < 23
               for candidate in pending["candidates"])


def test_l1_failure_recognizes_existing_running_goal_and_accepts_offer(l1):
    """截断或故障降级时，已有跑步目标不会被重建，'可以'仍接住提议。"""
    l1(lambda payload: RuntimeError("model unavailable"))
    at = _app()
    goal_id, step_id = _add_running_goal(at)

    _say(at, "我想跑步")
    assert at.session_state["dialog_context"].offers[-1]["payload"]["goal_id"] == goal_id
    with session_scope() as s:
        assert len(list(s.scalars(select(Goal).where(
            Goal.user_id == at.session_state["user_id"], Goal.title == "跑步")))) == 0

    _say(at, "可以")
    assert at.session_state["pending"]["step_id"] == step_id


def test_l1_failure_chat_fallback_does_not_call_second_model(l1, monkeypatch):
    from goalcal.llm import respond

    l1(lambda payload: RuntimeError("turn unavailable"))
    monkeypatch.setattr(respond, "chat_reply", lambda *args, **kwargs:
                        (_ for _ in ()).throw(AssertionError("second model call")))
    at = _app()
    _say(at, "今天有点无聊")
    assert not at.exception


def test_generic_calendar_request_accepts_bound_offer(l1, monkeypatch):
    """'帮我安排进日历'紧接具体跑步提议时沿提议找时段。"""
    from goalcal.ui import controller

    now = datetime(2026, 10, 27, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    monkeypatch.setattr(controller.runtime, "now", lambda: now)

    def handler(payload):
        if payload["message"] == "我想跑步":
            return {"relation": "new_command", "intent": "chat", "reply": "要把半马跑步放到今晚吗？",
                    "offer": "要把半马跑步放到今晚吗？", "offer_intent": "schedule_step",
                    "offer_target": "完成半马", "offer_when_text": "今晚"}
        return {"relation": "new_command", "intent": "chat", "reply": "我不能写日历。"}

    l1(handler)
    at = _app()
    _, step_id = _add_running_goal(at)
    _say(at, "我想跑步")
    _say(at, "帮我安排进日历")

    assert at.session_state["pending"]["step_id"] == step_id
    assert "不能写日历" not in at.session_state["messages"][-1]["content"]


def test_ambiguous_running_offer_waits_for_goal_answer(l1):
    def handler(payload):
        if payload["message"] == "我想跑步":
            return {"relation": "new_command", "intent": "chat",
                    "reply": "要我找个时间吗？", "offer": "要我找个时间吗？",
                    "offer_intent": "schedule_step"}
        return {"relation": "accept_offer", "intent": "chat", "reply": "好。"}

    l1(handler)
    at = _app()
    _, step_id = _add_running_goal(at)
    _say(at, "我想跑步")
    assert at.session_state["dialog_context"].offers[-1]["payload"].get("step_id") is None
    _say(at, "可以")
    assert at.session_state["pending"] is None
    assert "哪个目标" in at.session_state["messages"][-1]["content"]
    assert at.session_state["dialog_context"].offers[-1]["status"] == "open"
    _say(at, "完成半马")
    assert at.session_state["pending"]["step_id"] == step_id


def test_stale_running_offer_cannot_execute_after_goal_archive(l1):
    def bound(payload):
        if payload["message"] == "我想跑步":
            return {"relation": "new_command", "intent": "chat", "reply": "要安排半马吗？",
                    "offer": "要安排半马吗？", "offer_intent": "schedule_step",
                    "offer_target": "完成半马"}
        return {"relation": "accept_offer", "intent": "chat", "reply": "好。"}
    l1(bound)
    at = _app()
    goal_id, step_id = _add_running_goal(at)
    _say(at, "我想跑步")
    with session_scope() as s:
        s.get(Goal, goal_id).status = "archived"
    _say(at, "可以")
    assert at.session_state["pending"] is None
    assert "no longer on the table" in at.session_state["messages"][-1]["content"]
    with session_scope() as s:
        assert not list(s.scalars(select(Event).where(Event.step_id == step_id)))


def test_direct_schedule_request_uses_named_goal_or_asks(l1):
    def handler(payload):
        if payload["message"] == "帮我安排半马的下一步":
            return {"relation": "in_flow", "intent": "schedule_step", "reply": "接着「完成半马」跑步。"}
        return {"relation": "new_command", "intent": "schedule_step", "reply": "我来找时间。"}

    l1(handler)
    at = _app()
    _, step_id = _add_running_goal(at)

    _say(at, "帮我安排下一步")
    assert at.session_state["pending"] is None
    assert "哪个目标" in at.session_state["messages"][-1]["content"]

    _say(at, "帮我安排半马的下一步")
    assert at.session_state["pending"]["step_id"] == step_id


def test_calendar_window_requires_clock_but_exact_chinese_time_writes(l1):
    def handler(payload):
        if payload["message"].startswith("明天晚上"):
            return {"relation": "new_command", "intent": "add_event", "title": "练吉他",
                    "when_text": "明天晚上", "reply": "记下了。"}
        return {"relation": "new_command", "intent": "add_event", "title": "跑五公里",
                "when_text": "明晚七点", "reply": "记下了。"}

    l1(handler)
    at = _app()
    before = _event_titles(at)

    _say(at, "明天晚上练吉他，帮我放进日历")
    assert _event_titles(at) == before
    assert "安排在几点" in at.session_state["messages"][-1]["content"]

    _say(at, "明晚七点跑五公里，帮我加到日历")
    with session_scope() as s:
        event = s.scalar(select(Event).where(
            Event.user_id == at.session_state["user_id"], Event.title == "跑五公里"))
        assert event is not None
        from goalcal.timeutil import as_local
        assert as_local(event.start_utc, "Asia/Shanghai").hour == 19


def test_explicit_calendar_request_cannot_become_incapability_chat(l1, monkeypatch):
    from goalcal import agent as agent_mod

    l1(lambda payload: {"relation": "new_command", "intent": "chat",
                        "reply": "这个对话里我没法写进日历。"})
    monkeypatch.setattr(agent_mod.event_plan, "extract_events", lambda *args, **kwargs: None)
    at = _app()
    _say(at, "帮我安排进日历")

    reply = at.session_state["messages"][-1]["content"]
    assert "没法写进日历" not in reply
    assert "安排在几点" in reply


def test_advice_and_negated_request_never_write_even_if_l1_misroutes(l1):
    l1(lambda payload: {"relation": "new_command", "intent": "add_event",
                        "title": "跑步", "when_text": "今晚", "reply": "我加好了。"})
    at = _app()
    before = _event_titles(at)

    _say(at, "晚上我可以做什么")
    assert "我加好了" not in at.session_state["messages"][-1]["content"]
    _say(at, "我想跑步，但先不要安排")

    assert _event_titles(at) == before
    assert "暂时不安排" in at.session_state["messages"][-1]["content"]


def test_accept_offer_with_mismatched_state_does_not_execute(l1):
    """offer 与实际状态不匹配（草稿已被取消）：失效并诚实提示，绝不执行。"""
    def handler(payload):
        if payload["message"] == "好":
            return {"relation": "accept_offer", "intent": "chat", "reply": "好。"}
        return {"relation": "new_command", "intent": "create_goal",
                "title": "Learn guitar", "when_text": "in 30 days", "reply": "先看个草稿。"}

    l1(handler)
    at = _app()
    _say(at, "I want to learn guitar in 30 days")
    assert at.session_state["goal_draft"] is not None

    _say(at, "取消")  # 零成本短路取消草稿；goal_draft 提议在下一轮 derive 失效
    assert at.session_state["goal_draft"] is None

    _say(at, "好")

    # 没有 open 提议可接受：诚实说明，不执行
    assert "no open offer" in at.session_state["messages"][-1]["content"]
    with session_scope() as s:
        assert not list(s.scalars(select(Goal).where(
            Goal.user_id == at.session_state["user_id"], Goal.title == "Learn guitar")))
    offers = at.session_state["dialog_context"].offers
    assert not [o for o in offers if o["status"] == "open"]


def test_deterministic_draft_offer_accepted_once_l1_recovers(l1):
    """确定性（rules 降级）出的草稿提议同样登记，L1 恢复后"好"可以接住。"""
    state = {"calls": 0}

    def handler(payload):
        state["calls"] += 1
        if state["calls"] == 1:
            return RuntimeError("boom")  # 第一轮 L1 失败，走规则降级
        return {"relation": "accept_offer", "intent": "chat", "reply": "好，创建。"}

    l1(handler)
    at = _app()
    _say(at, "I want to learn guitar in 30 days")
    assert at.session_state["goal_draft"] is not None  # 降级模板草稿
    offers = at.session_state["dialog_context"].offers
    assert any(o["kind"] == "goal_draft" and o["status"] == "open" for o in offers)

    _say(at, "好")

    assert at.session_state["goal_draft"] is None
    with session_scope() as s:
        assert list(s.scalars(select(Goal).where(
            Goal.user_id == at.session_state["user_id"], Goal.title == "learn guitar")))


def test_recovery_offer_accepted_via_l1(l1):
    """checkin 恢复提议登记为 recovery offer；"好"经 accept_offer 订下首选恢复时段。"""
    from goalcal.domain import calendar_service as cal, goals as goal_svc
    from goalcal.models import RescheduleLog
    from goalcal.timeutil import local_now

    def handler(payload):
        if payload["message"] == "好":
            return {"relation": "accept_offer", "intent": "chat", "reply": "好，就定这个。"}
        return {"relation": "in_flow", "intent": "chat", "reply": "嗯。"}

    l1(handler)
    at = _app()
    uid = at.session_state["user_id"]
    with session_scope() as s:
        goal = s.scalar(select(Goal).where(
            Goal.user_id == uid, Goal.title == "Publish my first short video"))
        step = goal_svc.next_step(goal)
        now = local_now("Asia/Shanghai")
        cal.create_event(s, uid, f"{goal.title}: {step.title}",
                         now - timedelta(hours=16), now - timedelta(hours=15),
                         kind="block", source="proposal", step_id=step.id, tz="Asia/Shanghai")

    _say(at, "没做")  # 签到 outcome：missed → 问原因
    assert at.session_state["checkin_context"] is not None
    _say(at, "卡住了")  # reason：stuck → 细化 + 恢复候选
    ctx = at.session_state["checkin_context"]
    offers = at.session_state["dialog_context"].offers
    recovery = next((o for o in offers if o["kind"] == "recovery"), None)
    assert recovery is not None and recovery["status"] == "open"
    assert recovery["subject"]["step_id"] == ctx.step_id  # 细化后的启动步骤

    _say(at, "好")

    offers = at.session_state["dialog_context"].offers
    assert next(o for o in offers if o["kind"] == "recovery")["status"] == "accepted"
    with session_scope() as s:
        blocks = list(s.scalars(select(Event).where(
            Event.user_id == uid, Event.source == "proposal", Event.kind == "block")))
        assert len(blocks) == 2  # 旧块保留（missed）+ 新恢复块
        assert s.scalar(select(RescheduleLog).join(
            Event, RescheduleLog.event_id == Event.id).where(Event.user_id == uid)) is not None


def _start_overdue_checkin(at):
    """给 fixture 目标的下一步建一个 16 小时前的过期块，返回 uid。"""
    from goalcal.domain import calendar_service as cal, goals as goal_svc
    from goalcal.timeutil import local_now

    uid = at.session_state["user_id"]
    with session_scope() as s:
        goal = s.scalar(select(Goal).where(
            Goal.user_id == uid, Goal.title == "Publish my first short video"))
        step = goal_svc.next_step(goal)
        now = local_now("Asia/Shanghai")
        cal.create_event(s, uid, f"{goal.title}: {step.title}",
                         now - timedelta(hours=16), now - timedelta(hours=15),
                         kind="block", source="proposal", step_id=step.id, tz="Asia/Shanghai")
    return uid


def test_reject_offer_during_recovery_advances_checkin(l1):
    """H1：recovery 阶段说"算了"（reject_offer）走 decline_recovery——
    步骤保持 todo、签到队列前进结束、不当轮重问。"""
    from goalcal.models import RescheduleLog

    def handler(payload):
        if payload["message"] == "not this time":
            return {"relation": "reject_offer", "intent": "chat", "reply": ""}
        return {"relation": "in_flow", "intent": "chat", "reply": "嗯。"}

    l1(handler)
    at = _app()
    uid = _start_overdue_checkin(at)

    _say(at, "没做")
    _say(at, "卡住了")
    assert at.session_state["checkin_context"].stage == "recovery"
    assert at.session_state["pending"]  # 恢复候选在展示

    _say(at, "not this time")  # 非逐字短语：走 L1 reject_offer

    assert at.session_state["checkin_context"] is None  # 队列前进后签到结束
    assert at.session_state["pending"] is None
    last = at.session_state["messages"][-1]["content"]
    assert "unscheduled" in last  # decline_recovery 的诚实确认
    assert "Did any of those times work" not in last  # 不当轮重问
    with session_scope() as s:
        blocks = list(s.scalars(select(Event).where(
            Event.user_id == uid, Event.source == "proposal", Event.kind == "block")))
        assert len(blocks) == 1  # 没有订新块；旧块保留
        assert not list(s.scalars(select(RescheduleLog).join(
            Event, RescheduleLog.event_id == Event.id).where(Event.user_id == uid)))


def test_reject_offer_during_goal_options_keeps_goal_and_advances(l1):
    """H1：goal_options 阶段拒绝 → 目标原样保留，签到前进结束。"""
    def handler(payload):
        if payload["message"] == "先不处理这个":
            return {"relation": "reject_offer", "intent": "chat", "reply": ""}
        return {"relation": "in_flow", "intent": "chat", "reply": "嗯。"}

    l1(handler)
    at = _app()
    _start_overdue_checkin(at)

    _say(at, "没做")
    _say(at, "不想做了")  # reason=unwilling → goal_options
    assert at.session_state["checkin_context"].stage == "goal_options"

    _say(at, "先不处理这个")

    assert at.session_state["checkin_context"] is None
    assert "goal stays as is" in at.session_state["messages"][-1]["content"]
    assert _goal_status(at, "Publish my first short video") == "active"  # 目标未被改动


def test_reject_offer_at_checkin_outcome_ends_checkin_without_reask(l1):
    """H1：outcome/reason 阶段"不聊了"明确结束签到；本轮不自动重问，下轮恢复。"""
    def handler(payload):
        if payload["message"] == "现在不想聊":
            return {"relation": "reject_offer", "intent": "chat", "reply": "好，那不问了。"}
        return {"relation": "new_command", "intent": "chat", "reply": "好的。"}

    l1(handler)
    at = _app()
    _start_overdue_checkin(at)

    _say(at, "hi")  # 触发渲染期自动签到（outcome 提问）
    assert at.session_state["checkin_context"] is not None

    _say(at, "现在不想聊")

    assert at.session_state["checkin_context"] is None
    assert at.session_state["messages"][-1]["content"] == "好，那不问了。"
    # 一次性抑制：下一次渲染不自动重问；用户主动发消息后签到逻辑恢复（可接受）
    _say(at, "聊点别的")
    contents = [m["content"] for m in at.session_state["messages"]]
    assert "好的。" in contents
    assert at.session_state["checkin_context"] is not None  # 签到在下一轮恢复


def test_clarify_during_goal_draft_keeps_draft_and_skips_l2(l1, monkeypatch):
    """M2：kind=goal_draft 时 relation=clarify 不当修订指令——保留草稿，不触发 L2。"""
    from goalcal import agent as agent_mod

    def handler(payload):
        if payload["message"] == "嗯……":
            return {"relation": "clarify", "intent": "chat", "reply": "你想改哪部分？"}
        return {"relation": "new_command", "intent": "create_goal",
                "title": "Publish my first short video", "when_text": "in 30 days",
                "reply": "先看个草稿。"}

    l1(handler)
    at = _app()
    _say(at, "I want to publish my first short video in 30 days")
    assert at.session_state["goal_draft"] is not None

    def no_l2(*args, **kwargs):
        raise AssertionError("含糊修订不得触发 generate_plan (L2)")

    monkeypatch.setattr(agent_mod, "generate_plan", no_l2)
    _say(at, "嗯……")

    assert at.session_state["goal_draft"] is not None  # 草稿原样保留
    assert at.session_state["messages"][-1]["content"] == "你想改哪部分？"
    assert at.session_state["dialog_context"].clarify_count == 1


def test_clarify_during_pending_candidates_keeps_candidates(l1):
    """M2：候选展示中 clarify 同样不丢状态、不落到通用执行。"""
    def handler(payload):
        if payload["message"] == "嗯":
            return {"relation": "clarify", "intent": "chat", "reply": "想换个时间段吗？"}
        return {"relation": "new_command", "intent": "schedule_step", "reply": "给你几个时段。"}

    l1(handler)
    at = _app()
    _say(at, "Find time for my next step")
    assert at.session_state["pending"]

    _say(at, "嗯")

    assert at.session_state["pending"] is not None  # 候选保留
    assert at.session_state["messages"][-1]["content"] == "想换个时间段吗？"


def test_accept_recovery_offer_without_checkin_context_is_honest(l1):
    """L2：recovery 候选仍在但 checkin_context 已清 → 诚实失效，不 AttributeError。"""
    from goalcal.models import RescheduleLog

    def handler(payload):
        if payload["message"] == "好":
            return {"relation": "accept_offer", "intent": "chat", "reply": "好。"}
        return {"relation": "in_flow", "intent": "chat", "reply": "嗯。"}

    l1(handler)
    at = _app()
    _start_overdue_checkin(at)

    _say(at, "没做")
    _say(at, "卡住了")
    assert at.session_state["pending"]
    at.session_state["checkin_context"] = None  # 模拟状态被外部清掉

    _say(at, "好")

    assert "no longer on the table" in at.session_state["messages"][-1]["content"]
    with session_scope() as s:
        assert not list(s.scalars(select(RescheduleLog).join(
            Event, RescheduleLog.event_id == Event.id
        ).where(Event.user_id == at.session_state["user_id"])))  # 没有执行
