from __future__ import annotations

import urllib.request
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import streamlit as st
from sqlalchemy import select

from goalcal import agent, metrics
from goalcal.config import settings
from goalcal.db import get_engine, init_db, session_scope
from goalcal.domain import calendar_service as cal
from goalcal.domain import goals as goal_svc
from goalcal.domain import ics_import, planner
from goalcal.models import Base, Event, Goal, Step
from goalcal.seed import seed_if_empty
from goalcal.timeutil import as_local, as_utc_naive, local_now
from goalcal.ui import calendar_view, chat as chat_ui, progress

st.set_page_config(page_title="GoalCal", page_icon="📆", layout="wide")
init_db()
TZ = settings.tz
ZONE = ZoneInfo(TZ)

def _reset_demo() -> None:
    Base.metadata.drop_all(get_engine())
    Base.metadata.create_all(get_engine())
    with session_scope() as s:
        seed_if_empty(s, TZ)
    for key in (
        "messages", "pending", "user_id", "selected_event_id",
        "confirm_delete_id", "new_event_start", "_cal_sig", "flash", "goal_brief",
        "checkin_context", "replan_draft",
    ):
        st.session_state.pop(key, None)


def _ensure_state() -> None:
    if st.session_state.get("user_id"):
        return
    with session_scope() as s:
        user = seed_if_empty(s, TZ)
        st.session_state["user_id"] = user.id
    if "messages" not in st.session_state:
        st.session_state["messages"] = [
            {
                "role": "assistant",
                "content": "Hi — tell me a goal, or click **Find time** on a goal step. "
                "Not sure? Try a quick action below.",
            }
        ]
    st.session_state.setdefault("pending", None)
    st.session_state.setdefault("goal_brief", None)
    st.session_state.setdefault("checkin_context", None)


def _now() -> datetime:
    return local_now(TZ) + timedelta(days=st.session_state.get("day_offset", 0))


def _flash(message: str) -> None:
    st.session_state["flash"] = message


def _pct(value: float | None) -> str:
    return f"{value:.0%}" if value is not None else "—"


def _start_checkin_if_needed() -> None:
    """打开页面 / 时间机器快进了新的一天：有过期块就主动发起签到。"""
    if (
        st.session_state.get("checkin_context") is not None
        or st.session_state.get("goal_brief") is not None
    ):
        return
    with session_scope() as s:
        ctx, question = agent.begin_checkin(s, st.session_state["user_id"], _now(), TZ)
    if ctx is not None:
        st.session_state["checkin_context"] = ctx
        st.session_state["messages"].append({"role": "assistant", "content": question})


def _record_reply(reply) -> None:
    st.session_state["messages"].append({"role": "assistant", "content": reply.text})
    st.session_state["goal_brief"] = reply.goal_brief
    ctx = reply.checkin_context
    origin = "recovery" if ctx is not None and ctx.stage == "recovery" else "chat"
    if reply.batch:
        st.session_state["pending"] = {"batch": reply.batch, "origin": "goal"}
    elif reply.candidates:
        st.session_state["pending"] = {
            "candidates": reply.candidates,
            "step_id": reply.step_id,
            "origin": origin,
        }
    else:
        st.session_state["pending"] = None
    st.session_state["last_parser"] = reply.parser


def _run_turn(text: str) -> None:
    st.session_state["messages"].append({"role": "user", "content": text})
    brief = st.session_state.get("goal_brief")
    if brief is not None and agent.looks_like_new_command(text, _now()):
        brief = None
        st.session_state["goal_brief"] = None
    ctx = st.session_state.get("checkin_context")
    follow_up = None
    with session_scope() as s:
        with st.spinner("Thinking…"):
            if brief is not None:
                reply = agent.continue_goal(
                    s, st.session_state["user_id"], brief, text, _now(), TZ
                )
            elif ctx is not None:
                reply = agent.continue_checkin(
                    s, st.session_state["user_id"], ctx, text, _now(), TZ
                )
                if reply is None:
                    # 新指令打断：签到暂存，先处理指令，再把问题提回来
                    reply = agent.handle(s, st.session_state["user_id"], text, _now(), TZ)
                    follow_up = agent.checkin_prompt(s, ctx, _now(), TZ) or None
                else:
                    st.session_state["checkin_context"] = reply.checkin_context
            else:
                reply = agent.handle(s, st.session_state["user_id"], text, _now(), TZ)
            if (
                st.session_state.get("checkin_context") is None
                and reply.goal_brief is None
            ):
                new_ctx, question = agent.begin_checkin(
                    s, st.session_state["user_id"], _now(), TZ
                )
                if new_ctx is not None:
                    st.session_state["checkin_context"] = new_ctx
                    follow_up = follow_up or question
    _record_reply(reply)
    if follow_up:
        st.session_state["messages"].append({"role": "assistant", "content": follow_up})


def _accept(step_id: int, candidate) -> None:
    with session_scope() as s:
        msg = agent.accept(s, st.session_state["user_id"], step_id, candidate, _now(), TZ)
    st.session_state["messages"].append({"role": "assistant", "content": msg})
    st.session_state["pending"] = None
    _flash(msg)
    st.rerun()


def _accept_batch(batch: list[dict]) -> None:
    with session_scope() as s:
        msg = agent.accept_batch(s, st.session_state["user_id"], batch, _now(), TZ)
    st.session_state["messages"].append({"role": "assistant", "content": msg})
    st.session_state["pending"] = None
    _flash(msg)
    st.rerun()


def _dismiss_pending() -> None:
    st.session_state["pending"] = None
    st.rerun()


_CHIP_WINDOWS = {
    "today_pm": (0, 12, 18),
    "tonight": (0, 18, 23),
    "tomorrow": (1, 8, 22),
}


def _chip_window(chip: str, now: datetime) -> tuple[datetime, datetime] | None:
    if chip == "any":
        return None
    if chip == "weekend":
        day = now.date() + timedelta(days=(5 - now.weekday()) % 7)
        lo, hi = 8, 22
    else:
        offset, lo, hi = _CHIP_WINDOWS[chip]
        day = now.date() + timedelta(days=offset)
    return (
        datetime.combine(day, time(hour=lo), tzinfo=ZONE),
        datetime.combine(day, time(hour=hi), tzinfo=ZONE),
    )


def _refilter_pending(step_id: int, chip: str) -> None:
    """chip 窗口内重新 propose 同一步骤；窗口内无候选则保留原候选并提示。"""
    window = _chip_window(chip, _now())
    with session_scope() as s:
        candidates, _ = agent.propose_for_step(
            s, st.session_state["user_id"], step_id, _now(), TZ, window=window
        )
    if candidates and st.session_state.get("pending"):
        st.session_state["pending"]["candidates"] = candidates
    else:
        _flash("Nothing free in that window — kept the current suggestions.")
    st.rerun()


def _accept_recovery(step_id: int, candidate) -> None:
    ctx = st.session_state.get("checkin_context")
    with session_scope() as s:
        reply = agent.accept_recovery(
            s, st.session_state["user_id"], ctx, candidate, _now(), TZ
        )
    st.session_state["checkin_context"] = reply.checkin_context
    st.session_state["messages"].append({"role": "assistant", "content": reply.text})
    st.session_state["pending"] = (
        {"candidates": reply.candidates, "step_id": reply.step_id, "origin": "recovery"}
        if reply.candidates
        else None
    )
    _flash(reply.text)
    st.rerun()


def _dismiss_recovery() -> None:
    ctx = st.session_state.get("checkin_context")
    with session_scope() as s:
        reply = agent.decline_recovery(s, st.session_state["user_id"], ctx, _now(), TZ)
    st.session_state["checkin_context"] = reply.checkin_context
    st.session_state["messages"].append({"role": "assistant", "content": reply.text})
    st.session_state["pending"] = (
        {"candidates": reply.candidates, "step_id": reply.step_id, "origin": "recovery"}
        if reply.candidates
        else None
    )
    st.rerun()


# ---------------------------------------------------------------- goal actions


def _schedule_step(step_id: int) -> None:
    with session_scope() as s:
        candidates, step = agent.propose_for_step(
            s, st.session_state["user_id"], step_id, _now(), TZ
        )
        title = step.title if step is not None else ""
    if candidates:
        st.session_state["pending"] = {
            "candidates": candidates,
            "step_id": step_id,
            "origin": "goal",
        }
        _flash(f"Found {len(candidates)} slots for “{title}” — pick one below.")
    else:
        _flash(f"No free slot for “{title}” in the next 5 days. Try a shorter step.")
    st.rerun()


def _complete_step(step_id: int) -> None:
    with session_scope() as s:
        step = s.get(Step, step_id)
        if step is None or step.status == "done":
            return
        title = step.title
        now_utc = as_utc_naive(_now(), TZ)
        goal_svc.complete_step(s, step, raw_text="completed from UI", now=now_utc)
        cal.resolve_due_blocks_done(s, step_id, now_utc)
        cal.cancel_future_blocks(s, step_id, now_utc)
        note = agent.auto_refine_if_low(s, st.session_state["user_id"], step.goal, _now())
    pending = st.session_state.get("pending")
    if pending and pending.get("step_id") == step_id:
        st.session_state["pending"] = None
    _flash(f"“{title}” marked done." + (f" {note}" if note else ""))
    st.rerun()


def _unschedule_step(step_id: int, event_id: int) -> None:
    with session_scope() as s:
        ev = s.get(Event, event_id)
        if ev is not None:
            cal.delete_event(s, ev)
    _flash("Block removed — the step is back to todo.")
    st.rerun()


def _add_step(goal_id: int, title: str, minutes: int, energy: int, kind: str) -> None:
    with session_scope() as s:
        goal = s.get(Goal, goal_id)
        if goal is None:
            return
        goal_svc.add_step(s, goal, title, est_min=minutes, energy=energy, kind=kind)
    _flash(f"Added step “{title}”.")
    st.rerun()


def _edit_step(
    step_id: int,
    title: str,
    milestone: str,
    outcome: str,
    minutes: int,
    energy: int,
    kind: str,
) -> None:
    with session_scope() as s:
        step = s.get(Step, step_id)
        if step is None:
            return
        goal_svc.update_step(
            s,
            step,
            title=title,
            milestone=milestone,
            outcome=outcome,
            est_min=minutes,
            energy=energy,
            kind=kind,
        )
        saved = step.title
    _flash(f"Updated step “{saved}”.")
    st.rerun()


def _delete_step(step_id: int) -> None:
    with session_scope() as s:
        step = s.get(Step, step_id)
        if step is None:
            return
        title = step.title
        goal_svc.delete_step(s, step)
    pending = st.session_state.get("pending")
    if pending and pending.get("step_id") == step_id:
        st.session_state["pending"] = None
    _flash(f"Deleted step “{title}”.")
    st.rerun()


def _create_goal_ui(title: str, deadline, with_template: bool) -> None:
    now = _now()
    if with_template:
        brief = planner.assess(title, deadline, title, today=now.date())
        brief = planner.fill_defaults(brief, today=now.date())
        with session_scope() as s:
            with st.spinner("Planning steps…"):
                _, source = agent.create_goal_from_brief(
                    s, st.session_state["user_id"], brief, now, TZ
                )
        kind = "AI-plan" if source == "llm" else "template"
    else:
        with session_scope() as s:
            goal_svc.create_goal(
                s,
                st.session_state["user_id"],
                title,
                deadline=deadline,
                with_template=False,
            )
        kind = "blank"
    _flash(f"Created {kind} goal “{title}”.")
    st.rerun()


def _archive_goal(goal_id: int) -> None:
    with session_scope() as s:
        goal = s.get(Goal, goal_id)
        if goal is None:
            return
        title = goal.title
        step_ids = {step.id for step in goal.steps}
        goal_svc.archive_goal(s, goal)
    pending = st.session_state.get("pending")
    if (
        pending
        and pending.get("origin") == "goal"
        and pending.get("step_id") in step_ids
    ):
        st.session_state["pending"] = None
    if st.session_state.get("replan_draft", {}).get("goal_id") == goal_id:
        st.session_state.pop("replan_draft", None)
    _flash(f"Archived goal “{title}”.")
    st.rerun()


def _preview_replan(goal_id: int, instruction: str) -> None:
    """只生成待预览计划，不写库；用户确认后才替换。"""
    if not instruction.strip():
        _flash("Describe what should change first — this avoids blind re-plans.")
        st.rerun()
        return
    existing = st.session_state.get("replan_draft")
    if (
        existing
        and existing.get("goal_id") == goal_id
        and existing.get("instruction") == instruction
    ):
        _flash(
            "That preview is already below. Change the request to generate a different plan."
        )
        st.rerun()
        return
    with session_scope() as s:
        goal = s.get(Goal, goal_id)
        if goal is None:
            return
        title = goal.title
        with st.spinner("Revising plan…"):
            plan = agent.propose_goal_plan(
                s, goal, _now(), TZ, instruction=instruction
            )
    if not plan:
        _flash(
            "AI couldn't produce a valid revised plan — the old plan was kept. "
            "Try a more specific change request."
        )
        st.rerun()
        return
    st.session_state["replan_draft"] = {
        "goal_id": goal_id,
        "plan": plan,
        "instruction": instruction,
    }
    _flash(f"Preview ready for “{title}” — review it before applying.")
    st.rerun()


def _apply_replan(goal_id: int) -> None:
    draft = st.session_state.get("replan_draft")
    if not draft or draft.get("goal_id") != goal_id:
        return
    with session_scope() as s:
        goal = s.get(Goal, goal_id)
        if goal is None:
            return
        title = goal.title
        old_step_ids = {step.id for step in goal.steps}
        goal_svc.replace_steps(s, goal, draft["plan"], template_key="llm")
    pending = st.session_state.get("pending")
    if (
        pending
        and pending.get("origin") == "goal"
        and pending.get("step_id") in old_step_ids
    ):
        st.session_state["pending"] = None
    st.session_state.pop("replan_draft", None)
    _flash(f"Applied the revised plan to “{title}”.")
    st.rerun()


def _discard_replan() -> None:
    st.session_state.pop("replan_draft", None)
    _flash("Discarded the plan preview — nothing changed.")
    st.rerun()


# ---------------------------------------------------------------- calendar actions


def _blocks_by_step(session, user_id: int, step_ids: list[int]) -> dict[int, dict]:
    if not step_ids:
        return {}
    reference = as_utc_naive(_now(), TZ)
    rows = session.scalars(
        select(Event)
        .where(
            Event.user_id == user_id,
            Event.step_id.in_(step_ids),
            Event.kind == "block",
            Event.start_utc >= reference,
        )
        .order_by(Event.start_utc)
    )
    out: dict[int, dict] = {}
    for ev in rows:
        out.setdefault(
            ev.step_id,
            {"id": ev.id, "start": as_local(ev.start_utc, TZ), "end": as_local(ev.end_utc, TZ)},
        )
    return out


def _delete_calendar_event(event_id: int) -> None:
    with session_scope() as s:
        ev = s.get(Event, event_id)
        title = ev.title if ev is not None else ""
        if ev is not None:
            cal.delete_event(s, ev)
    st.session_state["selected_event_id"] = None
    st.session_state["confirm_delete_id"] = None
    _flash(f"Deleted “{title}”." if title else "Event already gone.")
    st.rerun()


def _create_quick_event(title: str, start: datetime, minutes: int) -> None:
    with session_scope() as s:
        cal.create_event(
            s, st.session_state["user_id"], title, start,
            start + timedelta(minutes=minutes), source="ui", tz=TZ,
        )
    st.session_state["new_event_start"] = None
    _flash(f"Added “{title}” on {start:%a %b %d, %H:%M}.")
    st.rerun()


# ---------------------------------------------------------------- calendar dialogs


@st.dialog("Event details")
def _event_detail_dialog(event_id: int) -> None:
    with session_scope() as s:
        row = s.get(Event, event_id)
        detail = None
        if row is not None:
            step_title = None
            if row.step_id is not None:
                step = s.get(Step, row.step_id)
                if step is not None:
                    step_title = f"{step.goal.title} · {step.title}"
            detail = {
                "id": row.id,
                "title": row.title,
                "start": as_local(row.start_utc, TZ),
                "end": as_local(row.end_utc, TZ),
                "kind": row.kind,
                "source": row.source,
                "step": step_title,
            }
    if detail is None:
        st.info("That event is already gone.")
        if st.button("Close", key=f"close_gone_{event_id}", use_container_width=True):
            st.session_state["selected_event_id"] = None
            st.rerun()
        return
    st.markdown(f"**{detail['title']}**")
    st.caption(
        f"{detail['start']:%a %b %d, %H:%M}–{detail['end']:%H:%M} · "
        f"{detail['kind']} · {detail['source']}"
    )
    if detail["step"]:
        st.caption(f"Goal step: {detail['step']}")
    if st.session_state.get("confirm_delete_id") == event_id:
        st.warning("Delete this event? This cannot be undone.")
        yes, no = st.columns(2)
        if yes.button("Yes, delete", key=f"confirm_{event_id}", use_container_width=True):
            _delete_calendar_event(event_id)
        if no.button("Keep it", key=f"keep_{event_id}", use_container_width=True):
            st.session_state["confirm_delete_id"] = None
            st.rerun()
    else:
        delete, close = st.columns(2)
        if delete.button("Delete event", key=f"delete_{event_id}", use_container_width=True):
            st.session_state["confirm_delete_id"] = event_id
            st.rerun()
        if close.button("Close", key=f"close_{event_id}", use_container_width=True):
            st.session_state["selected_event_id"] = None
            st.rerun()


@st.dialog("New event")
def _new_event_dialog(start: datetime) -> None:
    with st.form("new_event_form", clear_on_submit=True):
        title = st.text_input("Title", placeholder="e.g. Coffee with Amy")
        picked_time = st.time_input("Start", value=start.time())
        minutes = st.selectbox("Duration", [30, 45, 60, 90, 120], index=2)
        submitted = st.form_submit_button(
            "Add event", use_container_width=True, type="primary"
        )
    if st.button("Cancel", key="cancel_new_event", use_container_width=True):
        st.session_state["new_event_start"] = None
        st.rerun()
    if submitted:
        if not title.strip():
            st.warning("Give the event a title first.")
        else:
            picked = datetime.combine(start.date(), picked_time, tzinfo=ZONE)
            _create_quick_event(title.strip(), picked, int(minutes))


@st.dialog("Suggested slots")
def _slots_dialog() -> None:
    """候选时段悬浮框：chat / goal / recovery / batch 都走这里，不占栏内空间。"""
    pending = st.session_state.get("pending")
    if not pending:
        return
    if pending.get("batch"):
        chat_ui.render_batch(pending["batch"], _accept_batch, _dismiss_pending)
        return
    on_filter = lambda key: _refilter_pending(pending["step_id"], key)  # noqa: E731
    if pending.get("origin") == "recovery":
        chat_ui.render_proposals(
            pending["candidates"], pending["step_id"], _accept_recovery, _dismiss_recovery,
            on_filter=on_filter, now=_now(),
        )
    else:
        chat_ui.render_proposals(
            pending["candidates"], pending["step_id"], _accept, _dismiss_pending,
            on_filter=on_filter, now=_now(),
        )


_ensure_state()
_start_checkin_if_needed()

# ---------------------------------------------------------------- sidebar

with st.sidebar:
    st.subheader("Time machine")
    st.slider(
        "Days ahead", 0, 14, 0,
        help="Reminders and rescheduling are time-triggered — use this to fast-forward during the demo.",
        key="day_offset",
    )
    st.caption(f"Now: {_now():%a %b %d, %H:%M}")
    if settings.db_url.startswith("sqlite") and st.button(
        "Reset demo data", use_container_width=True
    ):
        _reset_demo()
        st.rerun()

    with st.expander("Import my calendar (.ics)", expanded=False):
        uploaded = st.file_uploader("Choose an .ics file", type=["ics"])
        url = st.text_input("…or ICS subscription URL", key="ics_url")
        if st.button("Import / Refresh", key="ics_import", use_container_width=True):
            content = None
            try:
                if uploaded is not None:
                    content = uploaded.getvalue()
                elif url.strip():
                    with urllib.request.urlopen(url.strip(), timeout=15) as resp:
                        content = resp.read()
            except Exception as exc:
                _flash(f"Couldn't fetch that calendar: {exc}")
                st.rerun()
            if content is None:
                _flash("Pick an .ics file or paste a URL first.")
                st.rerun()
            try:
                with session_scope() as s:
                    result = ics_import.import_ics(s, st.session_state["user_id"], content, TZ)
                _flash(
                    f"Imported: {result.added} added · "
                    f"{result.updated} updated · {result.skipped} skipped"
                )
            except Exception as exc:
                _flash(f"Import failed: {exc}")
            st.rerun()

    st.divider()
    st.subheader("How do you feel")
    c1, c2, c3 = st.columns(3)
    for col, label, text in [
        (c1, "😴 Tired", "I'm tired"),
        (c2, "🔥 Busy", "I'm busy"),
        (c3, "🫥 Stuck", "I'm uninspired"),
    ]:
        if col.button(label, use_container_width=True):
            st.session_state["goal_brief"] = None
            _run_turn(text)
            st.rerun()

    st.divider()
    st.subheader("Metrics (last 7 days, simulated)")
    with session_scope() as s:
        uid = st.session_state["user_id"]
        acc = metrics.acceptance_rate(s, uid, _now())
        rec = metrics.recovery_rate_72h(s, uid, _now())
        prog = metrics.progress_rate(s, uid, _now())
    m1, m2, m3 = st.columns(3)
    m1.metric("Acceptance", _pct(acc))
    m2.metric("Recovery 72h", _pct(rec))
    m3.metric("Progress", _pct(prog))

    st.divider()
    backend = "LLM + rules fallback" if settings.llm_ready else "rules only (no API key)"
    st.caption(f"Parser: {backend} · Last turn: {st.session_state.get('last_parser', '—')}")

# ---------------------------------------------------------------- header

st.title("📆 GoalCal")
st.caption("A calendar that advances your goals, not just your meetings.")

if flash := st.session_state.pop("flash", None):
    st.toast(flash)

chat_col, cal_col, goals_col = st.columns([1, 1.3, 1], gap="large")

with chat_col:
    st.subheader("Chat")
    # 消息、快捷回复、候选卡片都在同一个固定高度滚动区里，列高恒定
    with st.container(height=620, border=True):
        chat_ui.render_messages(st.session_state["messages"])

        brief = st.session_state.get("goal_brief")
        if brief is not None:
            st.caption("Quick answers")
            options = planner.quick_options(brief)
            for row_idx, row in enumerate((options[:2], options[2:])):
                if not row:
                    continue
                cols = st.columns(len(row))
                for col_idx, (col, option) in enumerate(zip(cols, row)):
                    key = f"goal_option_{row_idx}_{col_idx}"
                    if col.button(option, key=key, use_container_width=True):
                        _run_turn(option)
                        st.rerun()

        checkin_ctx = st.session_state.get("checkin_context")
        if brief is None and checkin_ctx is not None:
            options = agent.checkin_quick_options(checkin_ctx)
            if options:
                st.caption("Quick replies")
                cols = st.columns(len(options))
                for col_idx, (col, option) in enumerate(zip(cols, options)):
                    key = f"checkin_option_{checkin_ctx.stage}_{col_idx}"
                    if col.button(option, key=key, use_container_width=True):
                        _run_turn(option)
                        st.rerun()

    prompt = st.chat_input("Tell me a goal, or add something to your calendar…")
    if prompt:
        _run_turn(prompt)
        st.rerun()

with cal_col:
    st.subheader("Calendar")
    now_local = _now()
    start_utc = as_utc_naive(now_local - timedelta(days=7), TZ)
    end_utc = as_utc_naive(now_local + timedelta(days=28), TZ)
    with session_scope() as s:
        events = cal.events_between(s, st.session_state["user_id"], start_utc, end_utc)
        fc_events = cal.to_fullcalendar(events, TZ)

    # 日历底部与左右两栏（容器 620 + 下方输入框/按钮约 60px）对齐
    state = calendar_view.render(fc_events, height=680, initial_date=now_local.date().isoformat())
    if state:
        signature = repr(state)
        if signature != st.session_state.get("_cal_sig"):
            st.session_state["_cal_sig"] = signature
            callback = state.get("callback")

            if callback == "eventClick":
                st.session_state["selected_event_id"] = int(
                    state["eventClick"]["event"]["id"]
                )
                st.session_state["confirm_delete_id"] = None
                st.session_state["new_event_start"] = None

            elif callback == "dateClick":
                info = state["dateClick"]
                picked = datetime.fromisoformat(info["date"])
                if picked.tzinfo is None:
                    picked = picked.replace(tzinfo=ZONE)
                picked = picked.astimezone(ZONE)
                if info.get("allDay"):
                    picked = picked.replace(hour=9, minute=0, second=0, microsecond=0)
                st.session_state["new_event_start"] = picked
                st.session_state["selected_event_id"] = None

            elif callback == "eventChange":
                moved = state["eventChange"]["event"]
                start = datetime.fromisoformat(moved["start"])
                end = datetime.fromisoformat(moved["end"])
                with session_scope() as s:
                    row = s.get(Event, int(moved["id"]))
                    if row is not None:
                        row.start_utc = as_utc_naive(start, TZ)
                        row.end_utc = as_utc_naive(end, TZ)
                        row.status = "moved"
                        _flash(f"Moved “{row.title}”.")
                st.rerun()

    selected_id = st.session_state.get("selected_event_id")
    if selected_id:
        _event_detail_dialog(selected_id)

    new_start = st.session_state.get("new_event_start")
    if new_start:
        _new_event_dialog(new_start)

with goals_col:
    st.subheader("Goals")
    with session_scope() as s:
        goals = goal_svc.active_goals(s, st.session_state["user_id"])
        step_ids = [step.id for goal in goals for step in goal.steps]
        blocks = _blocks_by_step(s, st.session_state["user_id"], step_ids)
        snaps = [goal_svc.snapshot(g, now=now_local) for g in goals]
    if not snaps:
        st.info("No active goal yet — tell me one in the chat.")

    # 目标目录：多于 1 个目标时出现，用于在列表里跳转/过滤
    show_directory = len(snaps) > 1
    if show_directory:
        choice = st.selectbox(
            "Goal directory",
            range(len(snaps) + 1),
            key="goal_directory",
            format_func=lambda i: "All goals" if i == 0 else f"{i}. {snaps[i - 1]['title']}",
            label_visibility="collapsed",
        )
        if choice != 0:
            snaps = [snaps[choice - 1]]

    def _render_goal_list() -> None:
        for snap in snaps:
            draft = st.session_state.get("replan_draft")
            if draft and draft.get("goal_id") != snap["id"]:
                draft = None
            progress.render_goal(
                snap,
                blocks,
                on_schedule=_schedule_step,
                on_complete=_complete_step,
                on_unschedule=_unschedule_step,
                on_add_step=_add_step,
                on_delete_step=_delete_step,
                on_edit_step=_edit_step,
                on_archive_goal=_archive_goal,
                on_preview_replan=_preview_replan,
                on_apply_replan=_apply_replan,
                on_discard_replan=_discard_replan,
                replan_draft=draft,
            )

    # 固定高度容器保证三栏主体对齐；目录下拉框占的高度从容器里扣除，
    # New goal 在容器外下方，与聊天输入框呼应
    with st.container(height=576 if show_directory else 620, border=True):
        _render_goal_list()

    with st.expander("➕ New goal", expanded=False):
        with st.form("new_goal_form"):
            new_title = st.text_input("Goal title", placeholder="e.g. Run a half marathon")
            new_deadline = st.date_input(
                "Deadline", value=_now().date() + timedelta(days=30)
            )
            with_template = st.checkbox("Auto-decompose with AI (outline + first steps)", value=True)
            created = st.form_submit_button("Create goal", use_container_width=True)
        if created:
            if new_title.strip():
                _create_goal_ui(new_title.strip(), new_deadline, with_template)
            else:
                st.warning("Give the goal a title first.")

# 有待选时段（chat/goal/recovery/batch）就弹出悬浮框
if st.session_state.get("pending"):
    _slots_dialog()
