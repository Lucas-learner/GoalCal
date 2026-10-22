from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import streamlit as st
from sqlalchemy import select

from goalcal import agent
from goalcal.config import settings
from goalcal.db import get_engine, init_db, session_scope
from goalcal.domain import calendar_service as cal
from goalcal.domain import goals as goal_svc
from goalcal.domain import planner
from goalcal.models import Base, Event, Goal, Step
from goalcal.seed import seed_if_empty
from goalcal.timeutil import as_local, as_utc_naive, local_now
from goalcal.ui import calendar_view, chat as chat_ui, progress

st.set_page_config(page_title="GoalCal", page_icon="📆", layout="wide")
init_db()
TZ = settings.tz
ZONE = ZoneInfo(TZ)

QUICK_ACTIONS = [
    ("➕ Meeting", "Add a meeting tomorrow 10am"),
    ("🎯 New goal", "I want to publish my first short video in 30 days"),
    ("✅ Finished", "I finished the script today"),
    ("😴 Tired", "I'm tired"),
    ("📅 This week", "What do I have this week?"),
    ("🆓 Free time", "When am I free this week?"),
]


def _reset_demo() -> None:
    Base.metadata.drop_all(get_engine())
    Base.metadata.create_all(get_engine())
    with session_scope() as s:
        seed_if_empty(s, TZ)
    for key in (
        "messages", "pending", "user_id", "selected_event_id",
        "confirm_delete_id", "new_event_start", "_cal_sig", "flash", "goal_brief",
        "replan_draft",
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


def _now() -> datetime:
    return local_now(TZ) + timedelta(days=st.session_state.get("day_offset", 0))


def _flash(message: str) -> None:
    st.session_state["flash"] = message


def _run_turn(text: str) -> None:
    st.session_state["messages"].append({"role": "user", "content": text})
    brief = st.session_state.get("goal_brief")
    if brief is not None and agent.looks_like_new_command(text, _now()):
        brief = None
        st.session_state["goal_brief"] = None
    with session_scope() as s:
        with st.spinner("Thinking…"):
            if brief is not None:
                reply = agent.continue_goal(
                    s, st.session_state["user_id"], brief, text, _now(), TZ
                )
            else:
                reply = agent.handle(s, st.session_state["user_id"], text, _now(), TZ)
    st.session_state["messages"].append({"role": "assistant", "content": reply.text})
    st.session_state["goal_brief"] = reply.goal_brief
    st.session_state["pending"] = (
        {
            "candidates": reply.candidates,
            "step_id": reply.step_id,
            "origin": "chat",
        }
        if reply.candidates
        else None
    )
    st.session_state["last_parser"] = reply.parser


def _accept(step_id: int, candidate) -> None:
    with session_scope() as s:
        msg = agent.accept(s, st.session_state["user_id"], step_id, candidate, TZ)
    st.session_state["messages"].append({"role": "assistant", "content": msg})
    st.session_state["pending"] = None
    _flash(msg)
    st.rerun()


def _dismiss_pending() -> None:
    st.session_state["pending"] = None
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
        goal_svc.complete_step(s, step, raw_text="completed from UI", now=as_utc_naive(_now(), TZ))
        cal.cancel_future_blocks(s, step_id, as_utc_naive(_now(), TZ))
    pending = st.session_state.get("pending")
    if pending and pending.get("step_id") == step_id:
        st.session_state["pending"] = None
    _flash(f"“{title}” marked done.")
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


_ensure_state()

# ---------------------------------------------------------------- sidebar

with st.sidebar:
    st.header("Demo controls")
    st.slider(
        "Time machine (days ahead)", 0, 14, 0,
        help="F4 的提醒/重排是时间触发的，演示时用它快进。",
        key="day_offset",
    )
    st.caption(f"Now: {_now():%a %b %d, %H:%M}")

    st.markdown("**How do you feel right now?**")
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
    backend = "LLM + rules fallback" if settings.llm_ready else "rules only (no API key)"
    st.caption(f"Parser: {backend}")
    st.caption(f"Last turn: {st.session_state.get('last_parser', '—')}")
    if settings.db_url.startswith("sqlite") and st.button(
        "Reset demo data", use_container_width=True
    ):
        _reset_demo()
        st.rerun()

# ---------------------------------------------------------------- header

st.title("📆 GoalCal")
st.caption("A calendar that advances your goals, not just your meetings.")

if flash := st.session_state.pop("flash", None):
    st.toast(flash)

chat_col, cal_col = st.columns([1.05, 1], gap="large")

with chat_col:
    st.caption("Quick actions")
    for row in (QUICK_ACTIONS[:3], QUICK_ACTIONS[3:]):
        cols = st.columns(len(row))
        for col, (label, text) in zip(cols, row):
            if col.button(label, key=f"quick_{label}", use_container_width=True):
                st.session_state["goal_brief"] = None
                _run_turn(text)
                st.rerun()

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

    pending = st.session_state.get("pending")
    if pending and pending.get("origin") != "goal":
        chat_ui.render_proposals(
            pending["candidates"], pending["step_id"], _accept, _dismiss_pending
        )

    prompt = st.chat_input("Tell me a goal, or add something to your calendar…")
    if prompt:
        _run_turn(prompt)
        st.rerun()

with cal_col:
    now_local = _now()
    start_utc = as_utc_naive(now_local - timedelta(days=7), TZ)
    end_utc = as_utc_naive(now_local + timedelta(days=28), TZ)
    with session_scope() as s:
        events = cal.events_between(s, st.session_state["user_id"], start_utc, end_utc)
        fc_events = cal.to_fullcalendar(events, TZ)

    state = calendar_view.render(fc_events)
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
        with session_scope() as s:
            row = s.get(Event, selected_id)
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
            st.session_state["selected_event_id"] = None
        else:
            with st.container(border=True):
                st.markdown(f"**{detail['title']}**")
                st.caption(
                    f"{detail['start']:%a %b %d, %H:%M}–{detail['end']:%H:%M} · "
                    f"{detail['kind']} · {detail['source']}"
                )
                if detail["step"]:
                    st.caption(f"Goal step: {detail['step']}")
                if st.session_state.get("confirm_delete_id") == selected_id:
                    st.warning("Delete this event? This cannot be undone.")
                    yes, no = st.columns(2)
                    if yes.button("Yes, delete", key=f"confirm_{selected_id}", use_container_width=True):
                        _delete_calendar_event(selected_id)
                    if no.button("Keep it", key=f"keep_{selected_id}", use_container_width=True):
                        st.session_state["confirm_delete_id"] = None
                        st.rerun()
                else:
                    delete, close = st.columns(2)
                    if delete.button("Delete event", key=f"delete_{selected_id}", use_container_width=True):
                        st.session_state["confirm_delete_id"] = selected_id
                        st.rerun()
                    if close.button("Close", key=f"close_{selected_id}", use_container_width=True):
                        st.session_state["selected_event_id"] = None
                        st.rerun()

    new_start = st.session_state.get("new_event_start")
    if new_start:
        with st.container(border=True):
            st.markdown("**New event**")
            with st.form("new_event_form", clear_on_submit=True):
                title = st.text_input("Title", placeholder="e.g. Coffee with Amy")
                picked_time = st.time_input("Start", value=new_start.time())
                minutes = st.selectbox("Duration", [30, 45, 60, 90, 120], index=2)
                submitted = st.form_submit_button("Add event", use_container_width=True)
            if st.button("Cancel", key="cancel_new_event", use_container_width=True):
                st.session_state["new_event_start"] = None
                st.rerun()
            if submitted:
                if not title.strip():
                    st.warning("Give the event a title first.")
                else:
                    start = datetime.combine(new_start.date(), picked_time, tzinfo=ZONE)
                    _create_quick_event(title.strip(), start, int(minutes))

    st.subheader("Goals")
    with session_scope() as s:
        goals = goal_svc.active_goals(s, st.session_state["user_id"])
        step_ids = [step.id for goal in goals for step in goal.steps]
        blocks = _blocks_by_step(s, st.session_state["user_id"], step_ids)
        snaps = [goal_svc.snapshot(g, now=now_local) for g in goals]
    if not snaps:
        st.info("No active goal yet — tell me one in the chat.")
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
        pending = st.session_state.get("pending")
        if (
            pending
            and pending.get("origin") == "goal"
            and pending["step_id"] in {step.id for step in snap["steps"]}
        ):
            chat_ui.render_proposals(
                pending["candidates"], pending["step_id"], _accept, _dismiss_pending
            )

    with st.expander("➕ New goal", expanded=False):
        with st.form("new_goal_form"):
            new_title = st.text_input("Goal title", placeholder="e.g. Run a half marathon")
            new_deadline = st.date_input(
                "Deadline", value=_now().date() + timedelta(days=30)
            )
            with_template = st.checkbox("Auto-decompose with AI (4–12 steps)", value=True)
            created = st.form_submit_button("Create goal", use_container_width=True)
        if created:
            if new_title.strip():
                _create_goal_ui(new_title.strip(), new_deadline, with_template)
            else:
                st.warning("Give the goal a title first.")
