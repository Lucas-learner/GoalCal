"""Conversational goal replanning: understand user adjustments across turns before applying."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from ..config import settings
from ..i18n import normalize_lang, pick
from .natdate import find_dates
from .router import router

log = logging.getLogger(__name__)

REPLAN_SYSTEM = """You are GoalCal's conversational goal replanning assistant.
Today is {now} ({tz}), {weekday}.

You are helping the user revise ONE active goal through natural conversation BEFORE any change is saved.
You have the goal's title, current deadline, completed steps, remaining todo steps, any draft plan already proposed in this session, and the recent conversation.

Output ONLY ONE valid JSON object (no markdown):
{{
  "action": "ask | propose | apply | cancel | leave",
  "reply": "...",
  "deadline": null,
  "steps_mode": "keep | revise",
  "revision_summary": ""
}}

How to choose "action":
1. "ask": The user wants to replan, OR only named a broad category without concrete details of WHAT is wrong and HOW to change it.
   Examples:
   - Clicking Replan ("帮我重新规划「xxx」") with no specific change requested yet. (IMPORTANT: Dates or durations inside goal.title such as "一个月内拿到实习 offer" are ONLY part of the goal name — NEVER treat them as a request to change the deadline!)
   - Saying "修改截止时间" / "change the deadline" without giving a new date or saying to remove it.
   - Saying "大阶段不对", "当前阶段的步骤不对", "步骤不对", or "重新拆解" without explaining *what* should change (e.g. which phase to add/drop, or how steps should be adjusted).
   In "reply", briefly mention the goal's REAL current deadline, todo step count, and phases (if any), and ask clearly whether the issue is (1) overall phases/scope, (2) current phase steps, or (3) deadline/pace — or ask the missing specific detail before drafting a proposal.
2. "propose": ONLY when the user has clarified concrete direction — a specific or relative new deadline (outside goal.title), removing the deadline, shorter steps, slower/faster pace, adding/dropping a phase, narrowing scope, or specific step edits.
   - "deadline":
     * "YYYY-MM-DD" (must be >= today {now}) if the user specified or shifted the deadline (compute relative dates accurately from today or current_deadline).
     * "CLEAR" if the user explicitly wants to remove/clear the deadline.
     * null if the deadline is unchanged (preserve draft_in_progress.deadline if already updated earlier in this session).
   - "steps_mode":
     * "keep" when the user ONLY wants to change or clear the deadline and did NOT ask to revise steps, pace, duration, or scope.
     * "revise" whenever steps, pace, duration, difficulty, focus, or scope should be regenerated or updated.
   - "revision_summary": concise consolidation of ALL concrete requirements gathered across the conversation so far (used by the step planner when steps_mode="revise").
   - "reply": 1–2 concise sentences summarizing what you are proposing and asking the user to review the preview below and click 「确认修改」 ("Apply changes") — or tell you what else to adjust. NEVER claim the change has already been saved!
3. "apply": ONLY when draft_in_progress is NOT null AND the user explicitly confirms applying the proposed preview (e.g. "确认", "确认修改", "好的，就这样改", "looks good, apply it"). Never output "apply" when draft_in_progress is null.
4. "cancel": The user wants to cancel replanning and keep the current plan unchanged (e.g. "算了", "不用改了", "取消", "never mind").
5. "leave": ONLY when the user clearly switches to an UNRELATED command (creating a new habit, creating a different new goal, adding/querying a calendar event, or asking to schedule a time slot). NEVER output "leave" for anything about adjusting this goal's deadline, steps, pace, or scope.

Language rule:
- Always write "reply" and "revision_summary" in target_language ("Simplified Chinese" -> Chinese; "English" -> English).
"""


class ReplanDecision(BaseModel):
    action: Literal["ask", "propose", "apply", "cancel", "leave"]
    reply: str = Field(default="", max_length=600)
    deadline: str | None = Field(default=None, max_length=16)
    steps_mode: Literal["keep", "revise"] = "revise"
    revision_summary: str = Field(default="", max_length=800)

    @field_validator("reply", "revision_summary", mode="before")
    @classmethod
    def _coerce_str(cls, value: Any) -> str:
        return "" if value is None else str(value).strip()

    @field_validator("deadline", mode="before")
    @classmethod
    def _coerce_deadline(cls, value: Any) -> str | None:
        if value is None:
            return None
        s = str(value).strip()
        return s or None


_CANCEL_RE = re.compile(
    r"^(?:算了|不用(?:了|改了)|取消(?:重新规划)?|先不改了|保持原样|never\s*mind|cancel(?:\s+replan)?|keep\s+it)[。.!！]?$",
    re.I,
)
_CONFIRM_RE = re.compile(
    r"^(?:确认(?:修改|应用|保存)?|好的?[，,]?\s*(?:就这样|按这个改|改吧|应用吧|确认)|可以[，,]?\s*(?:就这样|改吧|确认)?|"
    r"就这样吧?|按这个来|没问题[，,]?\s*(?:改吧|确认)?|"
    r"confirm|apply(?:\s+changes)?|looks\s+good(?:,\s*apply)?|yes(?:,\s*apply)?)[。.!！]?$",
    re.I,
)
_CLEAR_DEADLINE_RE = re.compile(
    r"(?:取消|去掉|清除|删除|不要|不设|移除)[^。！!？?]{0,30}?截止(?:时间|日期)?|无截止(?:时间|日期)?|"
    r"\b(?:clear|remove|drop|no)\b[^.!?]{0,30}?\b(?:deadline|due\s*date)\b",
    re.I,
)
_DEADLINE_MENTION_RE = re.compile(
    r"截止(?:时间|日期)?|到期(?:时间|日期)?|\bdeadline\b|\bdue\s*date\b",
    re.I,
)
_STEP_ADJUST_RE = re.compile(
    r"步骤|节奏|时长|分钟|小时|重排|重新拆|拆细|拆小|缩短|放慢|加快|难度|简单|重点|方向|范围|顺便|同步|一起|"
    r"\b(?:steps?|pace|shorter|slower|faster|easier|focus|scope|minutes?|hours?|re-?plan|adjust\s+steps?)\b",
    re.I,
)
_EXPLICIT_LEAVE_RE = re.compile(
    r"^(?:(?:帮我)?(?:新建|创建|添加)(?:一个)?(?:目标|习惯)|(?:帮我)?(?:暂停|恢复)|(?:帮我)?(?:加一个|添加一个)?日程|"
    r"今天有什么安排|明天有什么安排|看看日程|"
    r"(?:create|add)\s+a\s+(?:new\s+)?(?:goal|habit|event)|(?:pause|resume)\b|what(?:'s|\s+is)\s+on\s+my\s+calendar)",
    re.I,
)
_SCHEDULE_HINT_RE = re.compile(
    r"找(?:个)?时间|安排(?:一下)?(?:下一步|时间|到日历)|排期|\b(?:find\s+time|schedule\s+(?:the\s+)?next)\b",
    re.I,
)
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "半": 0.5}


def _parse_num(token: str) -> float | None:
    token = token.strip()
    if token.isdigit():
        return float(token)
    if token in _CN_NUM:
        return float(_CN_NUM[token])
    if token.startswith("十") and len(token) == 2 and token[1] in _CN_NUM:
        return 10.0 + float(_CN_NUM[token[1]])
    return None


def _shift_months(base: date, months: int) -> date:
    total = base.year * 12 + (base.month - 1) + months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(base.day, last_day))


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def extract_replan_deadline(text: str, local_now: datetime, current_deadline: date | None) -> str | None:
    """Deterministically extract a target deadline ('YYYY-MM-DD' or 'CLEAR') from user text."""
    today = local_now.date()
    if _CLEAR_DEADLINE_RE.search(text):
        return "CLEAR"

    # 1. Explicit YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD
    m = re.search(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", text)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if d >= today:
                return d.isoformat()
        except ValueError:
            pass

    # 2. Chinese YYYY年M月D日/号
    m = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?", text)
    if m:
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if d >= today:
                return d.isoformat()
        except ValueError:
            pass

    # 3. Chinese year/relative year + month end (e.g. 2027年1月底 / 明年1月底 / 明年1月15日)
    m = re.search(r"(20\d{2}|今年|明年|后年)\s*年?\s*(\d{1,2})\s*月\s*(?:(\d{1,2})\s*[日号]|底)?", text)
    if m:
        yr_tok, mon_s, day_s = m.group(1), m.group(2), m.group(3)
        year = today.year if yr_tok == "今年" else today.year + 1 if yr_tok == "明年" else today.year + 2 if yr_tok == "后年" else int(yr_tok)
        month = int(mon_s)
        if 1 <= month <= 12:
            try:
                d = date(year, month, int(day_s)) if day_s else _month_end(year, month)
                if d >= today:
                    return d.isoformat()
            except ValueError:
                pass

    # 4. 本月底 / 这个月底 / 下月底 / 下个月底 / 年底
    if re.search(r"(?:下个?月底|\bend\s+of\s+next\s+month\b)", text, re.I):
        nxt = _shift_months(date(today.year, today.month, 1), 1)
        return _month_end(nxt.year, nxt.month).isoformat()
    if re.search(r"(?:(?:本|这个)月底|\bend\s+of\s+(?:this\s+)?month\b)", text, re.I):
        return _month_end(today.year, today.month).isoformat()
    if re.search(r"(?:今年底|年底|\bend\s+of\s+(?:the\s+)?year\b)", text, re.I):
        d = date(today.year, 12, 31)
        if d <= today:
            d = date(today.year + 1, 12, 31)
        return d.isoformat()

    # 5. Relative shift from current_deadline: 提前/延后/推迟/顺延 N 天/周/个月
    m = re.search(
        r"(提前|延后|推迟|顺延|往后推|延长)\s*([半一二两三四五六七八九十\d]+)\s*(天|日|个?周|星期|礼拜|个?月)",
        text,
    )
    if m:
        verb, num_tok, unit = m.group(1), m.group(2), m.group(3)
        val = _parse_num(num_tok)
        if val is not None:
            sign = -1 if verb == "提前" else 1
            base = current_deadline or today
            if "月" in unit:
                d = base + timedelta(days=sign * 15) if val == 0.5 else _shift_months(base, sign * int(val))
            elif any(u in unit for u in ("周", "星期", "礼拜")):
                d = base + timedelta(days=sign * int(round(val * 7)))
            else:
                d = base + timedelta(days=sign * int(round(val)))
            if d < today:
                d = today
            return d.isoformat()

    # English relative shift: N days/weeks/months earlier/later
    m = re.search(r"\b(\d+)\s*(days?|weeks?|months?)\s*(earlier|sooner|later|longer)\b", text, re.I)
    if m:
        val = int(m.group(1))
        unit = m.group(2).lower()
        sign = -1 if m.group(3).lower() in ("earlier", "sooner") else 1
        base = current_deadline or today
        if "month" in unit:
            d = _shift_months(base, sign * val)
        elif "week" in unit:
            d = base + timedelta(days=sign * val * 7)
        else:
            d = base + timedelta(days=sign * val)
        return max(d, today).isoformat()

    # 6. Relative from today: N 天/周/个月 后/内
    m = re.search(r"([半一二两三四五六七八九十\d]+)\s*(天|日|个?周|星期|礼拜|个?月)\s*(?:之)?(?:后|内|以内|里)", text)
    if m:
        val = _parse_num(m.group(1))
        unit = m.group(2)
        if val is not None:
            if "月" in unit:
                d = today + timedelta(days=15) if val == 0.5 else _shift_months(today, int(val))
            elif any(u in unit for u in ("周", "星期", "礼拜")):
                d = today + timedelta(days=int(round(val * 7)))
            else:
                d = today + timedelta(days=int(round(val)))
            return d.isoformat()

    m = re.search(r"\b(?:in|within)\s+(\d+)\s+(days?|weeks?|months?)\b", text, re.I)
    if m:
        val = int(m.group(1))
        unit = m.group(2).lower()
        if "month" in unit:
            return _shift_months(today, val).isoformat()
        if "week" in unit:
            return (today + timedelta(days=val * 7)).isoformat()
        return (today + timedelta(days=val)).isoformat()

    # 7. Standard natdate dates (e.g. 1月15日, Jan 15, 下周五)
    found = find_dates(text, local_now)
    if found:
        d = found[-1][1].date()
        if d >= today:
            return d.isoformat()

    return None


def _validate_deadline(raw: str | None, today: date) -> str | None:
    if raw is None:
        return None
    if raw.upper() == "CLEAR":
        return "CLEAR"
    try:
        parsed = date.fromisoformat(raw)
    except ValueError:
        return None
    return parsed.isoformat() if parsed >= today else None


def _strip_goal_title(text: str, goal_title: str) -> str:
    """Remove the goal title (and leftover empty quotes) so durations inside a title aren't parsed as new deadlines."""
    cleaned = text or ""
    if goal_title:
        cleaned = re.sub(re.escape(goal_title), " ", cleaned, flags=re.I)
    cleaned = re.sub(r"[「」『』“”\"'《》【】]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _build_replan_ask_reply(
    *,
    goal_title: str,
    base_deadline: date | None,
    todo_steps: list[dict[str, Any]],
    outline: list[str],
    lang: str,
) -> str:
    total_min = sum(int(s.get("est_min", 30)) for s in todo_steps)
    dl_note = (
        pick(lang, f"，截止 {base_deadline.isoformat()}", f", due {base_deadline.isoformat()}")
        if base_deadline else ""
    )
    outline_note = ""
    if outline:
        joined = " → ".join(outline[:5])
        outline_note = pick(lang, f"，阶段：{joined}", f", phases: {joined}")
    return pick(
        lang,
        f"「{goal_title}」当前有 {len(todo_steps)} 个待办步骤（共 {total_min} 分钟{dl_note}{outline_note}）。\n"
        "你想怎么调整？请先告诉我哪里不太合适，明确后再出新方案：\n"
        "1. 大阶段方向或范围不对\n"
        "2. 当前阶段的步骤不对（太粗、太细或内容要换）\n"
        "3. 截止日期或节奏要改",
        f'"{goal_title}" currently has {len(todo_steps)} todo steps ({total_min} min total{dl_note}{outline_note}).\n'
        "What feels off? Tell me before I draft a new plan:\n"
        "1. Overall phases or scope\n"
        "2. Current phase steps (too broad, too detailed, or wrong tasks)\n"
        "3. Deadline or pace",
    )


_UNDERSPECIFIED_AREA_RE = re.compile(
    r"^(?:我觉得|感觉|主要是)?(?:大阶段(?:方向)?|整体(?:方向|阶段|规划)|当前阶段的?步骤|现在(?:的)?步骤|步骤|计划|阶段)(?:不太?对|不合适|有问题|需要改|要改|要调|调整一下|改一下|重拆|重新拆)?[。.!！?？\s]*$",
    re.I,
)


def _rules_replan_decision(
    *,
    text: str,
    goal_title: str,
    current_deadline: date | None,
    todo_steps: list[dict[str, Any]],
    outline: list[str] | None = None,
    prev_replan: dict[str, Any] | None,
    replan_detail: str,
    local_now: datetime,
    lang: str,
) -> ReplanDecision:
    if _CANCEL_RE.match(text):
        return ReplanDecision(
            action="cancel",
            reply=pick(lang, "好的，已取消重新规划，保持原计划不变。", "Okay, cancelled replanning — your current plan stays unchanged."),
        )
    if _EXPLICIT_LEAVE_RE.search(text) or (_SCHEDULE_HINT_RE.search(text) and not _DEADLINE_MENTION_RE.search(text)):
        return ReplanDecision(action="leave")
    if prev_replan is not None and _CONFIRM_RE.match(text):
        return ReplanDecision(action="apply")

    base_deadline = current_deadline
    if prev_replan and prev_replan.get("deadline"):
        try:
            base_deadline = date.fromisoformat(prev_replan["deadline"])
        except ValueError:
            pass
    clean_text = _strip_goal_title(text, goal_title)
    parsed_dl = extract_replan_deadline(clean_text, local_now, base_deadline)
    mentions_deadline = bool(_DEADLINE_MENTION_RE.search(clean_text))
    mentions_steps = bool(_STEP_ADJUST_RE.search(clean_text))

    # User asked to modify deadline but didn't give a date yet
    if mentions_deadline and parsed_dl is None and not mentions_steps:
        if base_deadline:
            reply = pick(
                lang,
                f"当前「{goal_title}」的截止日期是 {base_deadline.isoformat()}。你想改到哪一天（或者说“取消截止时间”）？需要顺便调整后续步骤的节奏吗？",
                f'"{goal_title}" is currently due {base_deadline.isoformat()}. What date would you like to move it to (or say "no deadline"), and should we adjust the steps to match?',
            )
        else:
            reply = pick(
                lang,
                f"「{goal_title}」目前还没有截止日期。你想设定到哪一天？需要顺便调整后续步骤的节奏吗？",
                f'"{goal_title}" has no deadline yet. What date would you like to set, and should we adjust the steps to match?',
            )
        return ReplanDecision(action="ask", reply=reply)

    # User gave a concrete deadline (or clear deadline)
    if parsed_dl is not None:
        steps_mode: Literal["keep", "revise"] = "revise" if mentions_steps else "keep"
        return ReplanDecision(
            action="propose",
            deadline=parsed_dl,
            steps_mode=steps_mode,
            revision_summary=clean_text or text.strip(),
        )

    # No concrete detail yet (e.g. kickoff "帮我重新规划「xxx」" or vague "想调整一下")
    clean_detail = _strip_goal_title(replan_detail, goal_title)
    if not clean_detail or re.fullmatch(r"(?:想|想要|帮我)?(?:调整|修改|改|重排|重新规划|优化)(?:一下|一点)?(?:步骤|计划|目标)?", clean_detail):
        return ReplanDecision(
            action="ask",
            reply=_build_replan_ask_reply(
                goal_title=goal_title,
                base_deadline=base_deadline,
                todo_steps=todo_steps,
                outline=outline or [],
                lang=lang,
            ),
        )

    if _UNDERSPECIFIED_AREA_RE.match(clean_detail):
        if "阶段" in clean_detail and "步骤" not in clean_detail:
            reply = pick(
                lang,
                "了解，大阶段需要调整。具体想怎么改（比如去掉哪个阶段、新增什么阶段，或重新聚焦哪块范围）？明确后我再出新方案。",
                "Got it — the overall phases need adjustment. How specifically should they change (which phase to drop, add, or refocus)? Once clear, I'll draft the new plan.",
            )
        else:
            reply = pick(
                lang,
                "了解，当前步骤不太合适。具体希望怎么改（比如换掉哪些任务、拆得更细、每步控制在多少分钟，或换成实操为主）？明确后我再生成新方案。",
                "Got it — the current steps feel off. How specifically should they change (which tasks to replace, smaller steps, shorter duration, or more hands-on practice)? Once clear, I'll draft the new plan.",
            )
        return ReplanDecision(action="ask", reply=reply)

    return ReplanDecision(
        action="propose",
        steps_mode="revise",
        revision_summary=clean_detail,
    )


def evaluate_replan_turn(
    *,
    text: str,
    goal_title: str,
    current_deadline: date | None,
    done_steps: list[str],
    todo_steps: list[dict[str, Any]],
    outline: list[str],
    prev_replan: dict[str, Any] | None,
    recent_messages: list[dict[str, Any]],
    replan_detail: str,
    local_now: datetime,
    tz: str,
    lang: str = "zh",
    use_llm: bool = True,
) -> ReplanDecision:
    """Understand one conversational replan turn via LLM, with deterministic fallback."""
    resolved_lang = normalize_lang(lang) or ("zh" if re.search(r"[\u4e00-\u9fff]", text) else "en")
    today = local_now.date()

    # Fast deterministic paths for unambiguous cancel / confirm / empty kickoff / category-only complaint
    clean_text = _strip_goal_title(text, goal_title)
    clean_detail = _strip_goal_title(replan_detail, goal_title)
    if _CANCEL_RE.match(text):
        return _rules_replan_decision(
            text=text, goal_title=goal_title, current_deadline=current_deadline,
            todo_steps=todo_steps, outline=outline, prev_replan=prev_replan, replan_detail=replan_detail,
            local_now=local_now, lang=resolved_lang,
        )
    if prev_replan is not None and _CONFIRM_RE.match(text):
        return ReplanDecision(action="apply")

    base_deadline = current_deadline
    if prev_replan and prev_replan.get("deadline"):
        try:
            base_deadline = date.fromisoformat(prev_replan["deadline"])
        except ValueError:
            pass
    rule_dl = extract_replan_deadline(clean_text, local_now, base_deadline)

    # Empty kickoff (e.g. clicking Replan or typing "帮我重新规划「xxx」") or bare area complaint: ask first!
    if prev_replan is None and (not clean_detail or _UNDERSPECIFIED_AREA_RE.match(clean_detail)):
        return _rules_replan_decision(
            text=text, goal_title=goal_title, current_deadline=current_deadline,
            todo_steps=todo_steps, outline=outline, prev_replan=prev_replan, replan_detail="",
            local_now=local_now, lang=resolved_lang,
        ) if not clean_detail else _rules_replan_decision(
            text=text, goal_title=goal_title, current_deadline=current_deadline,
            todo_steps=todo_steps, outline=outline, prev_replan=prev_replan, replan_detail=clean_detail,
            local_now=local_now, lang=resolved_lang,
        )

    client = router.client("turn") if (use_llm and router.ready("turn")) else None
    if client is None or not settings.llm_enabled:
        return _rules_replan_decision(
            text=text, goal_title=goal_title, current_deadline=current_deadline,
            todo_steps=todo_steps, outline=outline, prev_replan=prev_replan, replan_detail=replan_detail,
            local_now=local_now, lang=resolved_lang,
        )

    compact_history = [
        {"role": m.get("role"), "content": str(m.get("content", ""))[:400]}
        for m in recent_messages[-8:]
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and m.get("content")
    ]
    payload = {
        "target_language": "Simplified Chinese" if resolved_lang == "zh" else "English",
        "goal": {
            "title": goal_title,
            "current_deadline": current_deadline.isoformat() if current_deadline else None,
            "done_steps": done_steps[:12],
            "todo_steps": [
                {"title": s["title"], "est_min": s["est_min"], "milestone": s.get("milestone", "")}
                for s in todo_steps[:12]
            ],
            "outline": outline[:8],
        },
        "draft_in_progress": (
            {
                "deadline": prev_replan.get("deadline"),
                "steps_changed": prev_replan.get("steps_changed", True),
                "steps": [
                    {"title": s["title"], "est_min": s["est_min"]}
                    for s in (prev_replan.get("steps") or [])[:12]
                ],
                "instruction": prev_replan.get("instruction", ""),
            }
            if prev_replan
            else None
        ),
        "recent_conversation": compact_history,
        "user_message": text,
    }
    system = REPLAN_SYSTEM.format(
        now=today.isoformat(), tz=tz, weekday=local_now.strftime("%A"),
    )
    try:
        raw_data = client.complete_json(system, json.dumps(payload, ensure_ascii=False), temperature=0.1)
        decision = ReplanDecision.model_validate(raw_data)
    except (ValidationError, Exception) as exc:  # noqa: BLE001
        log.warning("LLM replan turn failed (%s); falling back to rules", type(exc).__name__)
        return _rules_replan_decision(
            text=text, goal_title=goal_title, current_deadline=current_deadline,
            todo_steps=todo_steps, outline=outline, prev_replan=prev_replan, replan_detail=replan_detail,
            local_now=local_now, lang=resolved_lang,
        )

    # Safety guards on model output
    if decision.action == "apply" and prev_replan is None:
        decision.action = "ask"
    validated_dl = _validate_deadline(decision.deadline, today)
    # Deterministic date parser (excluding goal title) backs up model if model missed an explicit date
    decision.deadline = validated_dl or rule_dl
    # If model proposed without any concrete deadline or revision on a bare deadline request, ask instead
    if (
        decision.action == "propose"
        and decision.deadline is None
        and _DEADLINE_MENTION_RE.search(clean_text)
        and not _STEP_ADJUST_RE.search(clean_text)
    ):
        return _rules_replan_decision(
            text=text, goal_title=goal_title, current_deadline=current_deadline,
            todo_steps=todo_steps, outline=outline, prev_replan=prev_replan, replan_detail=replan_detail,
            local_now=local_now, lang=resolved_lang,
        )
    if decision.action == "propose" and not decision.revision_summary:
        decision.revision_summary = clean_detail or clean_text or text.strip()
    return decision
