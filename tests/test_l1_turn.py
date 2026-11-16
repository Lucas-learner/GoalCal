"""L1 轮引擎 / ModelRouter / DialogContext 单元测试（fake 客户端，不打真实 API）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from goalcal import agent, conversation
from goalcal.config import settings
from goalcal.dialog import DialogContext
from goalcal.domain import goals as goal_svc
from goalcal.domain.planner import GoalBrief
from goalcal.llm import respond, turn
from goalcal.llm.action import Action
from goalcal.llm.client import LLMClient, LLMOutputTruncated, LLMUnavailable
from goalcal.llm.router import ModelRouter
from goalcal.models import Event, Goal

TZ = "Asia/Shanghai"
NOW = datetime(2026, 10, 21, 9, 0, tzinfo=ZoneInfo(TZ))  # Wednesday


class FakeTurnClient:
    def __init__(self, payload=None, error=None):
        self.payload = payload or {}
        self.error = error
        self.calls = []

    def complete_json(self, system, user, temperature=0.0):
        self.calls.append(json.loads(user))
        if self.error is not None:
            raise self.error
        return self.payload


@pytest.fixture(autouse=True)
def _breaker_isolated():
    turn.reset_breaker()
    yield
    turn.reset_breaker()


# ------------------------------------------------------------- ModelRouter


def test_router_turn_layer_overrides_and_budget(monkeypatch):
    monkeypatch.setenv("LLM_MODEL_TURN", "turn-model")
    monkeypatch.setenv("LLM_BASE_URL_TURN", "https://turn.example/v1")
    monkeypatch.setenv("LLM_API_KEY_TURN", "turn-key")
    client = ModelRouter().client("turn")
    assert client.model == "turn-model"
    assert client.base_url == "https://turn.example/v1"
    assert client.api_key == "turn-key"
    assert client.layer == "turn"
    assert client.timeout == 8 and client.max_retries == 0 and client.max_tokens == 1000


def test_router_falls_back_field_by_field(monkeypatch):
    monkeypatch.setenv("LLM_MODEL_PLAN", "plan-model")
    router = ModelRouter()
    plan = router.client("plan")
    assert plan.model == "plan-model"
    assert plan.base_url == settings.llm_base_url
    assert plan.api_key == settings.llm_api_key
    # 非 L1 层保持原有延迟预算
    assert plan.timeout == 30 and plan.max_tokens is None
    # 默认层（旧调用方）行为不变
    memory = router.client("memory")
    assert memory.model == settings.llm_model


def test_router_caches_clients_per_layer():
    router = ModelRouter()
    assert router.client("turn") is router.client("turn")
    assert router.client("turn") is not router.client("plan")
    with pytest.raises(ValueError):
        router.client("unknown")


def test_https_proxy_takes_precedence_over_optional_socks_proxy(monkeypatch):
    """Constructing the SDK must work when socksio is absent and ALL_PROXY is SOCKS."""
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7897")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    client = LLMClient(api_key="test", base_url="https://example.invalid/v1")
    assert client._get() is not None


# ------------------------------------------------------------- turn.decide


def test_decide_returns_validated_decision(session, user):
    fake = FakeTurnClient({"relation": "new_command", "intent": "chat", "reply": "收到。"})
    ctx = DialogContext(kind="checkin", stage="outcome", subject={"step_id": 3})

    decision = turn.decide(session, user.id, "你好", NOW, TZ, ctx, client=fake)

    assert decision is not None and decision.reply == "收到。"
    assert len(fake.calls) == 1
    payload = fake.calls[0]
    assert payload["message"] == "你好"
    assert payload["dialog"] == {
        "kind": "checkin", "stage": "outcome", "subject": {"step_id": 3},
        "return_to": [], "offers": [],
    }


def test_decide_rejects_unknown_intent_and_counts_failure(session, user):
    breaker = turn.CircuitBreaker()
    fake = FakeTurnClient({"relation": "new_command", "intent": "drop_table", "reply": "ok"})
    assert turn.decide(session, user.id, "hack", NOW, TZ, DialogContext(),
                       client=fake, breaker=breaker) is None
    assert breaker.failures == 1


def test_decide_rejects_empty_decision(session, user):
    """An empty JSON object from an exhausted reasoning budget is a failure, not chat."""
    breaker = turn.CircuitBreaker()
    assert turn.decide(session, user.id, "帮我安排进日历", NOW, TZ, DialogContext(),
                       client=FakeTurnClient({}), breaker=breaker) is None
    assert breaker.failures == 1


def test_client_reports_truncated_json(monkeypatch):
    from types import SimpleNamespace
    from goalcal.llm.client import LLMClient

    response = SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="length", message=SimpleNamespace(content=""))])
    fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda **kwargs: response)))
    client = LLMClient(api_key="test", max_tokens=300)
    monkeypatch.setattr(client, "_preflight", lambda: (fake, None))
    with pytest.raises(LLMOutputTruncated):
        client.complete_json("system", "payload")


def test_calendar_guard_distinguishes_question_window_and_clock():
    question = turn.TurnDecision(relation="new_command", intent="add_event", reply="好。")
    assert turn.to_action(question, "晚上我可以做什么", NOW).intent == "chat"

    broad = turn.TurnDecision(relation="new_command", intent="add_event", title="跑步",
                              when_text="今晚", reply="好。")
    assert turn.to_action(broad, "把跑步安排进日历", NOW).start is None

    exact = turn.TurnDecision(relation="new_command", intent="add_event", title="跑步",
                              when_text="今晚七点", reply="好。")
    assert turn.to_action(exact, "今晚七点跑步", NOW).start.hour == 19

    chat = turn.TurnDecision(relation="new_command", intent="chat", reply="我不能写日历。")
    assert turn.to_action(chat, "帮我安排进日历", NOW).intent == "add_event"


def test_decide_malformed_json_falls_back(session, user):
    fake = FakeTurnClient(error=json.JSONDecodeError("x", "x", 0))
    assert turn.decide(session, user.id, "hi", NOW, TZ, DialogContext(), client=fake) is None


def test_breaker_opens_after_consecutive_failures_and_skips_calls(session, user):
    breaker = turn.CircuitBreaker(threshold=3, cooldown_seconds=300)
    fake = FakeTurnClient(error=RuntimeError("boom"))
    for _ in range(3):
        assert turn.decide(session, user.id, "hi", NOW, TZ, DialogContext(),
                           client=fake, breaker=breaker) is None
    assert len(fake.calls) == 3
    # 熔断打开：不再调用，直接降级
    assert turn.decide(session, user.id, "hi", NOW, TZ, DialogContext(),
                       client=fake, breaker=breaker) is None
    assert len(fake.calls) == 3


def test_breaker_half_open_after_cooldown(session, user, monkeypatch):
    breaker = turn.CircuitBreaker(threshold=1, cooldown_seconds=300)
    fake = FakeTurnClient(error=RuntimeError("boom"))
    assert turn.decide(session, user.id, "hi", NOW, TZ, DialogContext(),
                       client=fake, breaker=breaker) is None
    assert not breaker.allow()
    monkeypatch.setattr(turn.time, "monotonic", lambda: breaker.opened_at + 301)
    ok = FakeTurnClient({"relation": "new_command", "intent": "chat", "reply": "back"})
    decision = turn.decide(session, user.id, "hi", NOW, TZ, DialogContext(),
                           client=ok, breaker=breaker)
    assert decision is not None and decision.reply == "back"
    assert breaker.failures == 0 and breaker.opened_at is None


def test_unavailable_guard_does_not_count_toward_breaker(session, user):
    breaker = turn.CircuitBreaker(threshold=1)
    fake = FakeTurnClient(error=LLMUnavailable("write transaction"))
    for _ in range(3):
        assert turn.decide(session, user.id, "hi", NOW, TZ, DialogContext(),
                           client=fake, breaker=breaker) is None
    assert breaker.failures == 0 and breaker.allow()


def test_decide_without_client_is_none(session, user):
    assert turn.decide(session, user.id, "hi", NOW, TZ, DialogContext(), client=None) is None


# ------------------------------------------------------------- to_action


def test_to_action_recomputes_dates_with_natdate():
    decision = turn.TurnDecision(
        relation="new_command", intent="add_event", title="meeting",
        when_text="tomorrow 10am", reply="好的",
    )
    action = turn.to_action(decision, "Add meeting tomorrow 10am", NOW)
    assert action.intent == "add_event"
    assert action.start == datetime(2026, 10, 22, 10, 0, tzinfo=ZoneInfo(TZ))
    assert action.end - action.start == timedelta(minutes=60)
    assert action.raw == "Add meeting tomorrow 10am"


def test_to_action_delete_goal_guard_overrides_model():
    decision = turn.TurnDecision(relation="new_command", intent="create_goal", title="my goal")
    action = turn.to_action(decision, 'delete my goal "my goal"', NOW)
    assert action.intent == "delete_goal"


def test_to_action_ignores_model_computed_fields():
    decision = turn.TurnDecision(
        relation="new_command", intent="chat", reply="hi",
    )
    action = turn.to_action(decision, "hello", NOW)
    assert action.start is None and action.end is None


# ------------------------------------------------------------- DialogContext


def test_derive_priority_and_carry_over():
    ctx = DialogContext(return_to=[{"kind": "goal_brief"}], clarify_count=1,
                        offers=[{"text": "offer"}])
    from goalcal import dialog

    state = {"pending_action": None, "goal_draft": None, "pending": None,
             "event_draft": None, "goal_brief": None, "checkin_context": None}
    assert dialog.derive(state, ctx).kind == "idle"

    state["checkin_context"] = agent.CheckinContext(stage="reason", event_ids=[1], step_id=5)
    out = dialog.derive(state, ctx)
    assert out.kind == "checkin" and out.stage == "reason"
    assert out.subject == {"step_id": 5, "goal_id": None}
    assert out.return_to == ctx.return_to and out.clarify_count == 1
    # 旧格式的 offer 被规范化为完整记录，状态 open
    assert [o["text"] for o in out.offers] == ["offer"]
    assert out.offers[0]["status"] == "open" and out.offers[0]["kind"] == "chat"

    state["pending"] = {"candidates": [], "step_id": 7, "origin": "goal"}
    out = dialog.derive(state, ctx)
    assert out.kind == "pending_candidates" and out.subject["step_id"] == 7

    state["pending"] = {"batch": [], "origin": "goal"}
    assert dialog.derive(state, ctx).kind == "pending_batch"


def test_dialog_context_persists_round_trip(session, user):
    from goalcal import dialog

    ctx = DialogContext(
        kind="goal_brief",
        subject={"title": "学钢琴"},
        return_to=[{"kind": "goal_brief",
                    "brief": GoalBrief(title="学钢琴", raw="我要学会弹钢琴")}],
        clarify_count=1,
        offers=[{"text": "要不要找时间？"}],
    )
    conversation.save_state(
        session, user.id, [{"role": "user", "content": "hi"}], {"dialog_context": ctx}
    )
    loaded = conversation.load_task_state(session, user.id)["dialog_context"]
    assert loaded == ctx
    assert loaded.return_to[0]["brief"].title == "学钢琴"


# ------------------------------------------------------------- agent.execute


def test_execute_chat_uses_l1_text_without_extra_model_call(session, user, monkeypatch):
    def no_call(*args, **kwargs):
        raise AssertionError("L1 已经给出回复，本轮不能再调 chat_reply")

    monkeypatch.setattr(agent.respond, "chat_reply", no_call)
    reply = agent.execute(
        session, user.id, Action(intent="chat", raw="hello"), "l1:test", NOW, TZ,
        chat_text="Got it — no rush.",
    )
    assert reply.text == "Got it — no rush."
    assert session.scalars(select(Event)).all() == []


def test_execute_add_event_writes_deterministically(session, user):
    action = Action(
        intent="add_event", title="Team lunch", confidence=0.8, raw="x",
        start=NOW + timedelta(days=1),
    ).normalized(NOW)
    reply = agent.execute(session, user.id, action, "l1:test", NOW, TZ)
    assert "Added" in reply.text and "Team lunch" in reply.text
    event = session.scalar(select(Event).where(Event.user_id == user.id))
    assert event.title == "Team lunch"


def test_execute_risky_low_confidence_still_requires_confirmation(session, user):
    action = Action(intent="delete_event", target="ghost", confidence=0.3, raw="delete ghost")
    reply = agent.execute(session, user.id, action, "l1:test", NOW, TZ)
    assert reply.pending_action is not None
    assert session.scalars(select(Event)).all() == []


def test_execute_resume_goal_reactivates_paused_goal(session, user):
    goal = goal_svc.create_goal(session, user.id, "Learn guitar")
    goal_svc.pause_goal(session, goal)
    assert goal.status == "paused"

    reply = agent.execute(
        session, user.id, Action(intent="resume_goal", confidence=0.8, raw="我想恢复"),
        "l1:test", NOW, TZ,
    )

    assert goal.status == "active"
    assert "active again" in reply.text
    assert reply.candidates  # 恢复后顺带给出下一步候选


def test_execute_resume_goal_reports_when_nothing_paused(session, user):
    reply = agent.execute(
        session, user.id, Action(intent="resume_goal", confidence=0.8, raw="resume"),
        "l1:test", NOW, TZ,
    )
    assert "paused goal" in reply.text


def test_rules_parse_resume_goal_before_goal_markers():
    from goalcal.llm.rules import parse_with_rules

    action = parse_with_rules("我想恢复之前暂停的目标", NOW)
    assert action.intent == "resume_goal"
    action = parse_with_rules('恢复目标 "Learn guitar"', NOW)
    assert action.intent == "resume_goal" and action.target == "Learn guitar"


# ------------------------------------------------------------- respond.fallback


def test_fallback_does_not_repeat_identical_suggestion(session, user):
    goal = goal_svc.create_goal(session, user.id, "Learn guitar")
    step = goal_svc.next_step(goal)
    suggestion = f"我在听。想推进「{goal.title}」时，我可以帮你为「{step.title}」找时间。"
    conversation.save_state(
        session, user.id, [{"role": "assistant", "content": suggestion}], {}
    )
    assert respond.fallback(session, user.id, "嗯") == "我在听。你可以继续说说，或告诉我现在最想推进的一件事。"


def test_fallback_suggests_once_when_fresh(session, user):
    goal = goal_svc.create_goal(session, user.id, "Learn guitar")
    step = goal_svc.next_step(goal)
    text = respond.fallback(session, user.id, "嗯")
    assert goal.title in text and step.title in text


# ------------------------------------------------------------- 审查回归（H/M/L）


def test_router_plan_layer_key_alone_is_enough(monkeypatch):
    """H3：只配 LLM_API_KEY_PLAN（全局 key 为空）时 plan 层客户端就绪。"""
    assert settings.llm_api_key == ""  # conftest 保证全局 key 为空
    monkeypatch.setenv("LLM_API_KEY_PLAN", "plan-only-key")
    client = ModelRouter().client("plan")
    assert client.api_key == "plan-only-key"
    assert client.ready


def test_rules_resume_markers_do_not_match_unrelated_resume():
    """M1：英文 resume 只有在指向 goal/plan 时才触发 resume_goal。"""
    from goalcal.llm.rules import parse_with_rules

    assert parse_with_rules("send my resume to Anna", NOW).intent != "resume_goal"
    assert parse_with_rules("resume the meeting", NOW).intent != "resume_goal"
    assert parse_with_rules("resume my goals", NOW).intent == "resume_goal"


def test_decide_auth_error_propagates_without_counting_breaker(session, user):
    """N1：认证错误原样抛出（外层 session_scope 据此登出），但不烧熔断次数。"""
    from goalcal.auth import AuthError

    breaker = turn.CircuitBreaker(threshold=1)
    fake = FakeTurnClient(error=AuthError("bad credentials"))
    for _ in range(3):
        with pytest.raises(AuthError):
            turn.decide(session, user.id, "hi", NOW, TZ, DialogContext(),
                        client=fake, breaker=breaker)
    assert breaker.failures == 0 and breaker.allow()


def test_decide_invalid_output_emits_llm_invalid(session, user, monkeypatch):
    """L4：JSON 非法 / 字段越界各自上报 llm_invalid 遥测并计入熔断。"""
    events = []
    monkeypatch.setattr(turn.telemetry, "emit",
                        lambda event, **fields: events.append((event, fields)))
    breaker = turn.CircuitBreaker()
    bad_json = FakeTurnClient(error=json.JSONDecodeError("x", "x", 0))
    assert turn.decide(session, user.id, "hi", NOW, TZ, DialogContext(),
                       client=bad_json, breaker=breaker) is None
    bad_fields = FakeTurnClient({"relation": "new_command", "intent": "drop_table", "reply": ""})
    assert turn.decide(session, user.id, "hi", NOW, TZ, DialogContext(),
                       client=bad_fields, breaker=breaker) is None
    assert breaker.failures == 2
    assert [e for e, _ in events] == ["llm_invalid", "llm_invalid"]
    assert all(f["layer"] == "turn" for _, f in events)
    assert {f["error_type"] for _, f in events} == {"JSONDecodeError", "ValidationError"}


def test_to_action_duration_is_clamped():
    """L2：模型给的极端时长 clamp 到 5–480 分钟；None 保持 None。"""
    huge = turn.TurnDecision(relation="new_command", intent="add_event",
                             title="marathon", when_text="tomorrow 10am",
                             duration_min=100000)
    action = turn.to_action(huge, "x", NOW)
    assert action.duration_min == 480
    tiny = turn.TurnDecision(relation="new_command", intent="add_event",
                             title="blink", when_text="tomorrow 10am", duration_min=1)
    assert turn.to_action(tiny, "x", NOW).duration_min == 5
    none = turn.TurnDecision(relation="new_command", intent="chat", reply="hi")
    assert turn.to_action(none, "x", NOW).duration_min is None


def test_derive_topic_inherited_only_within_same_kind():
    """M3：topic 只在话题 kind 不变时继承；换话题即清理。"""
    from goalcal import dialog

    prev = DialogContext(kind="goal_brief", subject={"title": "学钢琴", "topic": "钢琴目标"})
    state = {"pending_action": None, "goal_draft": None, "pending": None,
             "event_draft": None,
             "goal_brief": GoalBrief(title="学钢琴", raw="我要学会弹钢琴"),
             "checkin_context": None}
    assert dialog.derive(state, prev).subject["topic"] == "钢琴目标"

    state["goal_brief"] = None  # 回到 idle：kind 变了，topic 不继承
    out = dialog.derive(state, prev)
    assert out.kind == "idle" and "topic" not in out.subject


def test_derive_expires_old_offers_but_keeps_history():
    """offers 有 600s 生命周期：超时标记 expired 但保留为少量历史；无 ts 的旧数据视为不过期。"""
    from goalcal import dialog

    prev = DialogContext(offers=[
        {"text": "旧提议", "created_ts": 1000.0, "expires_ts": 1600.0, "status": "open"},
        {"text": "新提议", "created_ts": 999.0, "expires_ts": 2199.0, "status": "open"},
        {"text": "兼容旧条目"},  # 旧存档只有 text/ts
    ])
    out = dialog.derive({}, prev, now_ts=1601.0)
    assert [o["text"] for o in out.offers] == ["旧提议", "新提议", "兼容旧条目"]
    assert [o["status"] for o in out.offers] == ["expired", "open", "open"]
    # 喂给模型的只有 open 提议
    assert dialog.latest_open_offer(out, now_ts=1601.0)["text"] == "兼容旧条目"


def test_offer_lifecycle_api():
    """登记去重/刷新、最近 open、接受/拒绝/过期的完整生命周期。"""
    from goalcal import dialog

    ctx = DialogContext()
    first = dialog.register_offer(ctx, "创建目标草稿？", kind="goal_draft",
                                  subject={"title": "学钢琴"}, ts=1000.0)
    dialog.register_offer(ctx, "定第一个时段？", kind="candidates",
                          subject={"step_id": 7}, ts=1001.0)
    # 同 kind+subject 刷新而不是堆叠
    refreshed = dialog.register_offer(ctx, "创建目标草稿（已更新）？", kind="goal_draft",
                                      subject={"title": "学钢琴"}, ts=1002.0)
    assert refreshed["id"] == first["id"]
    assert len([o for o in ctx.offers if o["status"] == "open"]) == 2
    assert refreshed["text"] == "创建目标草稿（已更新）？"

    # 最近 open 是刚刷新的草稿提议
    assert dialog.latest_open_offer(ctx, now_ts=1003.0)["id"] == first["id"]
    # 拒绝最近提议：只动这一条
    declined = dialog.decline_offer(ctx, now_ts=1003.0)
    assert declined["id"] == first["id"] and declined["status"] == "declined"
    assert dialog.latest_open_offer(ctx, now_ts=1003.0)["kind"] == "candidates"
    # 接受候选提议：其余 open（chat 提议）随之失效；历史保留
    dialog.register_offer(ctx, "要不要找时间？", ts=1003.5)
    accepted = dialog.accept_offer(ctx, now_ts=1004.0)
    assert accepted["kind"] == "chat" and accepted["status"] == "accepted"
    assert {o["kind"] for o in ctx.offers if o["status"] == "open"} == set()
    # 过期清理：新提议超过 TTL 自动 expired
    dialog.register_offer(ctx, "再看一眼？", ts=1005.0)
    assert dialog.latest_open_offer(ctx, now_ts=1005.0 + dialog.OFFER_TTL_SECONDS + 1) is None
    assert ctx.offers[-1]["status"] == "expired"


def test_state_bound_offer_expires_when_state_gone():
    """状态绑定提议：对应会话状态消失（取消/确认/重置）后 derive 自动失效。"""
    from goalcal import dialog

    ctx = DialogContext(kind="goal_draft", subject={"title": "学钢琴"})
    dialog.register_offer(ctx, "创建目标草稿？", kind="goal_draft",
                          subject={"title": "学钢琴"}, ts=1000.0)
    empty = {"pending_action": None, "goal_draft": None, "pending": None,
             "event_draft": None, "goal_brief": None, "checkin_context": None}
    out = dialog.derive(empty, ctx, now_ts=1001.0)
    assert out.kind == "idle"
    assert [o["status"] for o in out.offers] == ["expired"]
    # chat 类提议不绑定状态，话题切换不自动失效（靠 TTL / new_command）
    ctx2 = DialogContext()
    dialog.register_offer(ctx2, "要不要找时间？", ts=1000.0)
    out2 = dialog.derive(empty, ctx2, now_ts=1001.0)
    assert out2.offers[0]["status"] == "open"


def test_decide_feeds_only_open_offers(session, user):
    """喂给 L1 的 offers 只含 open 提议的文本，不含 ID/状态，模型无法挑选。"""
    from goalcal import dialog

    ctx = DialogContext()
    dialog.register_offer(ctx, "旧提议", kind="candidates", subject={"step_id": 1}, ts=1000.0)
    dialog.register_offer(ctx, "新提议", ts=1001.0)
    dialog.decline_offer(ctx, now_ts=1002.0)  # 新提议被拒绝
    fake = FakeTurnClient({"relation": "new_command", "intent": "chat", "reply": "ok"})

    turn.decide(session, user.id, "hi", NOW, TZ, ctx, client=fake)

    assert fake.calls[0]["dialog"]["offers"] == ["旧提议"]


# ------------------------------------------------------------- N2：use_llm=False 整轮不调 LLM


def _no_llm(*args, **kwargs):
    raise AssertionError("use_llm=False 的轮次不得触达任何 LLM 入口")


def test_execute_add_event_without_llm_skips_context_extraction(session, user, monkeypatch):
    """明确写日历但没时间：use_llm=False 不调 event_plan 提取，确定性追问时间。"""
    monkeypatch.setattr(agent.event_plan, "extract_events", _no_llm)
    reply = agent.execute(
        session, user.id,
        Action(intent="add_event", raw="帮我安排进日历", confidence=0.8),
        "test", NOW, TZ, use_llm=False,
    )
    assert reply.event_draft is None
    assert "安排在几点" in reply.text
    assert session.scalars(select(Event)).all() == []


def test_execute_refine_steps_without_llm_uses_rule_split(session, user, monkeypatch):
    """拆细步骤：use_llm=False 不调 refine 的 LLM 入口，用确定性规则拆分。"""
    goal = goal_svc.create_goal(session, user.id, "Publish my first short video")
    monkeypatch.setattr(agent.refine, "_llm_defs", _no_llm)
    monkeypatch.setattr("goalcal.llm.client.LLMClient", _no_llm)

    reply = agent.execute(
        session, user.id,
        Action(intent="refine_steps", raw="下一步拆细一点", confidence=0.8),
        "test", NOW, TZ, use_llm=False,
    )

    assert "split" in reply.text
    starter = sorted(goal.steps, key=lambda s: s.seq)[0]
    assert starter.est_min == 5 and "5-minute starter" in starter.title


def test_execute_log_progress_auto_refine_without_llm(session, user, monkeypatch):
    """log_progress 触发自动续批：use_llm=False 不调 LLM，模板目标按模板顺序补。"""
    goal = goal_svc.create_goal(session, user.id, "Publish my first short video")
    first = sorted(goal.steps, key=lambda s: s.seq)[0]
    monkeypatch.setattr(agent.refine, "_upcoming_llm_defs", _no_llm)
    monkeypatch.setattr("goalcal.llm.client.LLMClient", _no_llm)

    reply = agent.execute(
        session, user.id,
        Action(intent="log_progress", progress_target=first.title, raw=f"完成了{first.title}",
               confidence=0.8),
        "test", NOW, TZ, use_llm=False,
    )

    session.refresh(goal)
    assert len(goal.steps) == 5  # 首批 2 步 + 模板续批 3 步
    assert "added" in reply.text


def test_refine_functions_keep_llm_path_by_default(session, user, monkeypatch):
    """use_llm 默认 True：向后兼容，LLM 入口照常可达（这里用 fake 证明）。"""
    from goalcal.domain import refine

    called = []
    monkeypatch.setattr(
        refine, "_llm_defs",
        lambda goal, step: called.append(1) or [
            {"title": "更小的一步", "est_min": 10, "energy": 0},
            {"title": "再小的一步", "est_min": 15, "energy": 0},
        ],
    )
    goal = goal_svc.create_goal(session, user.id, "Publish my first short video")
    step = sorted(goal.steps, key=lambda s: s.seq)[0]

    created = refine.refine_next_steps(session, goal, step)

    assert called == [1]
    assert created[0].title == "更小的一步"


def test_checkin_done_with_llm_disabled_never_touches_llm(session, user, monkeypatch):
    """checkin outcome=done 的降级轮：auto_refine 走模板续批，不触 LLM。"""
    from goalcal.domain import calendar_service as cal

    monkeypatch.setattr(agent.refine, "_upcoming_llm_defs", _no_llm)
    monkeypatch.setattr(agent.refine, "_llm_defs", _no_llm)
    monkeypatch.setattr("goalcal.llm.client.LLMClient", _no_llm)

    goal = goal_svc.create_goal(session, user.id, "Publish my first short video")
    step = sorted(goal.steps, key=lambda s: s.seq)[0]
    ev = cal.create_event(
        session, user.id, f"{goal.title}: {step.title}",
        NOW - timedelta(hours=16), NOW - timedelta(hours=16) + timedelta(minutes=step.est_min),
        kind="block", source="proposal", step_id=step.id, tz=TZ,
    )
    ctx, _ = agent.begin_checkin(session, user.id, NOW, TZ)
    assert ctx is not None and ctx.stage == "outcome"

    reply = agent.continue_checkin(session, user.id, ctx, "Done", NOW, TZ, use_llm=False)

    assert reply is not None and reply.checkin_context is None
    assert "marked done" in reply.text
    assert session.get(Event, ev.id).status == "done"
    session.refresh(goal)
    assert len(goal.steps) == 5  # 首批 2 步 + 模板续批 3 步（无 LLM）
    assert "added" in reply.text


def test_checkin_stuck_with_llm_disabled_uses_rule_refine(session, user, monkeypatch):
    """checkin 回答"卡住"的降级轮：stuck 细化走确定性规则，不触 LLM。"""
    from goalcal.domain import calendar_service as cal

    monkeypatch.setattr(agent.refine, "_llm_defs", _no_llm)
    monkeypatch.setattr(agent.refine, "_upcoming_llm_defs", _no_llm)
    monkeypatch.setattr("goalcal.llm.client.LLMClient", _no_llm)

    goal = goal_svc.create_goal(session, user.id, "Publish my first short video")
    step = sorted(goal.steps, key=lambda s: s.seq)[0]
    ev = cal.create_event(
        session, user.id, f"{goal.title}: {step.title}",
        NOW - timedelta(hours=16), NOW - timedelta(hours=16) + timedelta(minutes=step.est_min),
        kind="block", source="proposal", step_id=step.id, tz=TZ,
    )
    ctx, _ = agent.begin_checkin(session, user.id, NOW, TZ)

    reply = agent.continue_checkin(session, user.id, ctx, "没做", NOW, TZ, use_llm=False)
    assert reply is not None and reply.checkin_context is not None
    assert reply.checkin_context.stage == "reason"

    reply = agent.continue_checkin(
        session, user.id, reply.checkin_context, "卡住了", NOW, TZ, use_llm=False
    )

    assert reply is not None and reply.checkin_context is not None
    assert reply.checkin_context.stage == "recovery"
    assert reply.candidates  # 细化出 5 分钟启动动作并给出候选时段
    starter = sorted(goal.steps, key=lambda s: s.seq)[0]
    assert starter.est_min == 5 and "5-minute starter" in starter.title
    from goalcal.models import Checkin
    checkin = session.scalar(select(Checkin))
    assert checkin.reason == "stuck"
