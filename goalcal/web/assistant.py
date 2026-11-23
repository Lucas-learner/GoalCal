"""Conversation understanding proposes intentions; Service owns every write."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
import uuid
from zoneinfo import ZoneInfo
from sqlalchemy import select

from ..i18n import normalize_lang, pick
from ..models import Goal, Event, Step
from ..dialog import DialogContext
from ..llm import turn as turn_engine
from ..llm.rules import parse_with_rules, NO_SCHEDULE, ADVICE_QUESTION, QUERY_MARKERS
from ..llm.router import turn_client
from ..llm.natdate import extract_time, extract_window, has_explicit_clock
from .transport_models import WebThread


_HABIT_PREFIX = re.compile(
    r'^(?:(?:我想要?|我打算|我希望|我要)\s*|(?:帮我)?(?:新建|创建|添加)(?:一个)?习惯\s*[:：]?\s*|'
    r"(?:i want to|i(?:'d| would) like to|i plan to|(?:create|add) a habit(?: of)?)\s*:?\s*)", re.I,
)
_HABIT_COUNT = re.compile(r'每(?:周|星期)\s*(?P<activity>[^\d一二两三四五六七日天，,。.!！、\s]{1,12}\s*)?'
                          r'(?P<zh>[1-7一二两三四五六七])\s*次|'
                          r'\b(?P<en>[1-7]|once|twice|one|two|three|four|five|six|seven)\s*'
                          r'(?:times?\s+)?(?:a|per|each)\s+week\b', re.I)
_HABIT_DAILY = re.compile(r'每天|每日|\b(?:every day|daily|each day)\b', re.I)
_HABIT_WEEKDAYS = re.compile(r'每(?:周|星期)\s*([一二三四五六日天](?:\s*[、和及或/，,]\s*(?:(?:周|星期)\s*)?[一二三四五六日天])*)')
_HABIT_CLOCK = re.compile(
    r'(?:上午|下午|晚上|早上|中午)?\s*(?:\d{1,2}|[零一二两三四五六七八九十]{1,3})\s*点(?:半|\s*\d{1,2}分)?|'
    r'\b(?:at\s+)?\d{1,2}(?::[0-5]\d)?\s*(?:am|pm)\b|'
    r'\b(?:at\s+)?(?:[01]?\d|2[0-3]):[0-5]\d\b|\bat\s+(?:[01]?\d|2[0-3])\b', re.I,
)
_HABIT_NUMBERS = {'两': 2, 'once': 1, 'twice': 2, **dict(zip('一二三四五六七', range(1, 8))),
                  **dict(zip('one two three four five six seven'.split(), range(1, 8)))}
_HABIT_DURATION = re.compile(r'(?:\bfor\s+)?(\d{1,3})\s*(?:分钟|\bmin(?:ute)?s?\b)', re.I)
_HABIT_ADVICE = re.compile(r'如何|怎么|怎样|建议|是否|要不要|够吗|合适吗|'
                           r'\b(?:how|should|advice|tips)\b', re.I)
_HABIT_DECLINE = re.compile(r'^(?:先不|不想|不需要|不要|别|don.t|do not|never)', re.I)
_GOAL_OUTCOME = re.compile(r'(?:新建|创建)(?:一个)?目标|我的目标|目标是|'
                           r'\b(?:goal|by|until)\b|直到|争取|为了|截止|之前', re.I)
_REPLAN_TRIGGER = re.compile(
    r'(?:帮我)?(?:重新规划|重排(?:步骤|计划)?|重新拆解|调整(?:一下)?(?:这个)?(?:目标|计划|步骤)|改一下(?:这个)?(?:目标|计划|步骤)|'
    r'拆细|拆成更小|拆小|细化(?:一下)?|更细一点|太粗了|'
    r'(?:修改|改(?:一下|个)?|调整(?:一下)?|取消|去掉|延后|推迟|提前)[^。！!？?]{0,25}?截止(?:时间|日期)?|'
    r'截止(?:时间|日期)[^。！!？?]{0,15}?(?:改(?:到|成|为)?|调(?:整)?(?:到|成|为)?|延后|推迟|提前|顺延|取消|去掉))|'
    r'\b(?:help me )?(?:replan|re-plan|revise (?:this |the |my )?(?:goal|plan|steps)|adjust (?:this |the |my )?(?:goal|plan|steps)|'
    r'break\s+down|smaller\s+steps?|split\s+steps?|'
    r'(?:change|update|move|extend|remove|clear) (?:the |my |this )?deadline|deadline\s+(?:to|earlier|later|removed|cleared))\b',
    re.I,
)
_EXPLICIT_NEW_GOAL = re.compile(
    r'^(?:(?:帮我)?(?:新建|创建)(?:一个)?目标|(?:create|add)\s+a\s+(?:new\s+)?goal\b)',
    re.I,
)
_REPLAN_CANCEL = re.compile(
    r'^(?:算了|不用(?:了|改了)|取消(?:重新规划)?|先不改了|保持原样|never\s*mind|cancel(?:\s+replan)?|keep\s+it)[。.!！]?$',
    re.I,
)
_REPLAN_FILLER = re.compile(
    r'(?:帮我|请|我想|想要|把|将|这个|当前|该|的|一下|目标|计划|步骤|for|this|the|my|goal|plan|steps|please|i want to|help me)',
    re.I,
)


def _active_replan_context(raw_messages: list[dict]):
    if not raw_messages:
        return None, None, None, None
    latest = raw_messages[-1] if isinstance(raw_messages[-1], dict) else {}
    if latest.get('replan_goal'):
        rg = latest['replan_goal']
        return rg.get('goal_id'), rg, rg, None
    if latest.get('replan_prompt_goal_id'):
        return latest.get('replan_prompt_goal_id'), None, latest.get('replan_context'), None
    if latest.get('replan_disambiguate'):
        return None, None, None, latest.get('replan_disambiguate')
    return None, None, None, None


def _replan_detail(text: str, goal_title: str) -> str:
    # Keep deadline-modification and step-refinement phrases intact so evaluate_replan_turn knows the user's intent.
    if re.search(r'截止(?:时间|日期)?|拆细|拆成更小|拆小|细化|更细一点|太粗了|\b(?:deadline|due\s*date|break\s+down|smaller\s+steps?|split\s+steps?)\b', text, re.I):
        return text.strip()
    cleaned = _REPLAN_TRIGGER.sub(' ', text)
    if goal_title:
        cleaned = re.sub(re.escape(goal_title), ' ', cleaned, flags=re.I)
    cleaned = re.sub(r'[「」『』“”"\'《》【】\[\]()（）,，。:：;；?？!！\s]+', ' ', cleaned).strip()
    residual = _REPLAN_FILLER.sub('', cleaned).strip()
    return text.strip() if len(residual) >= 2 else ''


def _resolve_replan_goal(session, uid: int, text: str, row, body: dict, stale: bool, active_replan_id: int | None):
    goals = list(session.scalars(select(Goal).where(Goal.user_id == uid, Goal.status == 'active')))
    named = [g for g in goals if g.title.casefold() in text.casefold()]
    if len(named) == 1:
        return named[0], goals
    if len(named) > 1:
        return None, named
    for candidate_id in (active_replan_id, body.get('page_goal_id'), None if stale else (row.focus_goal_id if row else None)):
        if candidate_id is not None:
            match = next((g for g in goals if g.id == candidate_id), None)
            if match is not None:
                return match, goals
    if len(goals) == 1:
        return goals[0], goals
    return None, goals


def _rule_habit_draft(text):
    """Resolve common recurring requests without waiting for the model."""
    if (re.search(r'[?？]', text) or _HABIT_ADVICE.search(text) or _HABIT_DECLINE.search(text)
            or _GOAL_OUTCOME.search(text) or NO_SCHEDULE.search(text)):
        return None
    prefix = _HABIT_PREFIX.match(text)
    count = _HABIT_COUNT.search(text)
    daily = _HABIT_DAILY.search(text)
    weekdays = _HABIT_WEEKDAYS.search(text)
    repeat = count or daily or weekdays
    explicit = prefix and ('习惯' in prefix[0] or 'habit' in prefix[0].lower())
    if not (explicit or (repeat and (prefix or repeat.start() == 0))):
        return None
    title = text[prefix.end() if prefix else 0:].strip(' ，,。.!！')
    draft = {}
    if count:
        token = (count['zh'] or count['en']).lower()
        draft['weekly_target'] = int(token) if token.isdigit() else _HABIT_NUMBERS[token]
        title = _HABIT_COUNT.sub(lambda match: (match['activity'] or '').strip(), title, count=1)
    elif daily:
        draft['weekdays'] = list(range(7))
        title = _HABIT_DAILY.sub('', title, count=1)
    elif weekdays:
        draft['weekdays'] = sorted({'一二三四五六日'.index(day.replace('天', '日'))
                                    for day in re.findall('[一二三四五六日天]', weekdays[1])})
        title = _HABIT_WEEKDAYS.sub('', title, count=1)
    clock = _HABIT_CLOCK.search(title)
    if clock and not count:
        hour, minute = extract_time(clock[0])
        if 0 <= hour < 24 and 0 <= minute < 60:
            draft['start_time'] = f'{hour:02}:{minute:02}'
            title = _HABIT_CLOCK.sub('', title, count=1)
    duration = _HABIT_DURATION.search(title)
    if duration and 5 <= int(duration[1]) <= 480:
        draft['est_min'] = int(duration[1])
        title = _HABIT_DURATION.sub('', title, count=1)
    title = re.sub(r'(?:[，,、\s]*(?:每次|每回|一次|大约|大概|约|时长|each\s+time|per\s+session))+\s*$', '', title, flags=re.I)
    title = re.sub(r'^\s*(?:每次|每回|一次|大约|大概|约)\s*', '', title)
    title = re.sub(r'\s+', ' ', title).strip(' ，,。.!！')
    return {'title': title[:200], **draft} if title and title.lower() not in {'for', 'of'} else None


def _model_habit_draft(text, decision):
    if (not decision or decision.intent != 'create_habit' or _HABIT_ADVICE.search(text)
            or _HABIT_DECLINE.search(text) or NO_SCHEDULE.search(text)):
        return None
    title = (decision.title or '').strip()[:200]
    if not title:
        return None
    draft = {'title': title}
    if decision.duration_min and 5 <= decision.duration_min <= 480:
        draft['est_min'] = decision.duration_min
    if decision.habit_weekly_target and 1 <= decision.habit_weekly_target <= 7:
        return {**draft, 'weekly_target': decision.habit_weekly_target}
    if decision.habit_weekdays and all(0 <= day <= 6 for day in decision.habit_weekdays):
        draft['weekdays'] = sorted(set(decision.habit_weekdays))
    if has_explicit_clock(text) and decision.when_text and has_explicit_clock(decision.when_text):
        hour, minute = extract_time(decision.when_text)
        if 0 <= hour < 24 and 0 <= minute < 60:
            draft['start_time'] = f'{hour:02}:{minute:02}'
    return draft


def _is_stale_thread(messages: list[dict], now: datetime, tz: str) -> bool:
    """Auto-clear only when BOTH a new local calendar day AND >=6h have elapsed since last turn."""
    if not messages:
        return False
    raw_ts = None
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get('ts'):
            raw_ts = msg['ts']
            break
    if not raw_ts:
        return False
    try:
        last_dt = datetime.fromisoformat(str(raw_ts).replace('Z', '+00:00'))
    except ValueError:
        return False
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    if now_utc - last_dt < timedelta(hours=6):
        return False
    zone = ZoneInfo(tz)
    return last_dt.astimezone(zone).date() < now_utc.astimezone(zone).date()


def thread(session, uid):
    return session.scalar(select(WebThread).where(WebThread.user_id == uid))


def prepare_turn(session, principal, body, now, *, lang: str = 'zh'):
    """Read-only LLM inference; the later transaction checks this context version."""
    from .core import ApiError
    lang = normalize_lang(lang) or 'zh'
    row = thread(session, principal.user_id)
    if body['version'] != str(row.version if row else 0):
        raise ApiError(
            'stale_conversation',
            pick(lang, '对话已更新，请刷新后继续。', 'Conversation updated elsewhere; please refresh to continue.'),
        )
    if _rule_habit_draft(body['text'].strip()):
        return None
    text = body['text'].strip()
    local = now.astimezone(ZoneInfo(principal.tz))
    raw_messages = json.loads(row.messages_json) if row else []
    stale = _is_stale_thread(raw_messages, now, principal.tz)
    active_replan_id, prev_replan, prev_context, replan_disambig = (None, None, None, None) if stale else _active_replan_context(raw_messages)
    if active_replan_id is not None and _REPLAN_CANCEL.match(text):
        return {'_replan_cancel': True}
    # Handle picking a goal after ambiguous replan disambiguation ("第一个" or goal title)
    if replan_disambig:
        idx = _number(text)
        picked_id = replan_disambig[idx] if (idx is not None and 0 <= idx < len(replan_disambig)) else None
        if picked_id is None:
            goals = list(session.scalars(select(Goal).where(Goal.user_id == principal.user_id, Goal.status == 'active')))
            matched = [g for g in goals if g.id in replan_disambig and g.title.casefold() in text.casefold()]
            if len(matched) == 1:
                picked_id = matched[0].id
        if picked_id is not None:
            return {'_replan_ask': {'goal_id': picked_id, 'reply': '', 'context': None}}
    explicit_replan = bool(_REPLAN_TRIGGER.search(text))
    is_replan_followup = bool(
        active_replan_id is not None
        and not _EXPLICIT_NEW_GOAL.search(text)
        and _number(text) is None
    )
    if explicit_replan or is_replan_followup:
        goal, _ = _resolve_replan_goal(session, principal.user_id, text, row, body, stale, active_replan_id)
        if goal is None:
            return {'_replan_ambiguous': True}
        from datetime import date as date_type
        from ..agent import prepare_goal_draft
        from ..domain import goals as goal_service
        from ..domain.planner import GoalBrief
        from ..llm.replan import evaluate_replan_turn
        todo_steps = [
            {'title': s.title, 'est_min': s.est_min, 'milestone': s.milestone or '', 'energy': s.energy}
            for s in sorted(goal.steps, key=lambda x: x.seq)
            if s.status == 'todo'
        ]
        done_titles = [s.title for s in sorted(goal.steps, key=lambda x: x.seq) if s.status == 'done']
        goal_outline = goal_service.outline_phases(goal)
        same_goal_replan = prev_replan if (prev_replan and prev_replan.get('goal_id') == goal.id) else None
        same_goal_ctx = prev_context if (prev_context and prev_context.get('goal_id') == goal.id) else None
        detail = _replan_detail(text, goal.title) if explicit_replan else text
        replan_decision = evaluate_replan_turn(
            text=text,
            goal_title=goal.title,
            current_deadline=goal.deadline,
            done_steps=done_titles,
            todo_steps=todo_steps,
            outline=goal_outline,
            prev_replan=same_goal_replan,
            recent_messages=[] if stale else raw_messages[-8:],
            replan_detail=detail,
            local_now=local,
            tz=principal.tz,
            lang=lang,
            use_llm=True,
        )
        if replan_decision.action == 'cancel':
            return {'_replan_cancel': True, 'reply': replan_decision.reply}
        if replan_decision.action == 'apply' and same_goal_replan is not None:
            return {'_replan_apply': same_goal_replan}
        if replan_decision.action == 'ask':
            return {
                '_replan_ask': {
                    'goal_id': goal.id,
                    'reply': replan_decision.reply,
                    'context': same_goal_ctx,
                }
            }
        if replan_decision.action == 'propose':
            # Resolve target deadline: "" means clear, "YYYY-MM-DD" means set, None means unchanged/none
            prev_dl_val = same_goal_ctx.get('deadline') if same_goal_ctx else None
            if replan_decision.deadline == 'CLEAR':
                deadline_out: str | None = ''
                target_dl: date_type | None = None
            elif replan_decision.deadline:
                try:
                    target_dl = date_type.fromisoformat(replan_decision.deadline)
                    deadline_out = target_dl.isoformat()
                except ValueError:
                    target_dl = goal.deadline
                    deadline_out = goal.deadline.isoformat() if goal.deadline else None
            elif prev_dl_val is not None:
                deadline_out = prev_dl_val
                try:
                    target_dl = date_type.fromisoformat(prev_dl_val) if prev_dl_val else None
                except ValueError:
                    target_dl = goal.deadline
            else:
                target_dl = goal.deadline
                deadline_out = goal.deadline.isoformat() if goal.deadline else None

            prev_steps_changed = bool(same_goal_ctx and same_goal_ctx.get('steps_changed'))
            if replan_decision.steps_mode == 'keep' and not prev_steps_changed:
                steps_out = todo_steps
                outline_out = goal_outline
                steps_changed = False
                source_out = 'keep'
                combined_instruction = replan_decision.revision_summary or detail
            elif replan_decision.steps_mode == 'keep' and prev_steps_changed and same_goal_ctx:
                steps_out = same_goal_ctx.get('steps') or todo_steps
                outline_out = same_goal_ctx.get('outline') or goal_outline
                steps_changed = True
                source_out = same_goal_ctx.get('source', 'llm')
                combined_instruction = same_goal_ctx.get('instruction', '')
            else:
                current_steps = (same_goal_ctx.get('steps') or todo_steps) if same_goal_ctx else todo_steps
                current_outline = (same_goal_ctx.get('outline') or goal_outline) if same_goal_ctx else goal_outline
                prev_instr = (same_goal_ctx.get('instruction') or '') if same_goal_ctx else ''
                new_req = replan_decision.revision_summary or detail or text
                combined_instruction = new_req if (not prev_instr or prev_instr in new_req) else f'{prev_instr}\n{new_req}'.strip()
                done_note = f"\nCompleted steps to keep (do not repeat): {', '.join(done_titles)}" if done_titles else ''
                draft = prepare_goal_draft(
                    session,
                    principal.user_id,
                    GoalBrief(goal.title, f'{goal.title}\n{combined_instruction}{done_note}', deadline=target_dl),
                    local,
                    principal.tz,
                    use_llm=True,
                    lang=lang,
                    instruction=f'{combined_instruction}{done_note}',
                    current_steps=current_steps or None,
                    current_outline=current_outline or None,
                )
                steps_out = draft.step_defs
                outline_out = draft.outline
                steps_changed = True
                source_out = draft.source
            return {
                '_replan': {
                    'goal_id': goal.id,
                    'deadline': deadline_out,
                    'previous_deadline': goal.deadline.isoformat() if goal.deadline else None,
                    'steps_changed': steps_changed,
                    'outline': outline_out,
                    'steps': steps_out,
                    'source': source_out,
                    'instruction': combined_instruction,
                    'reply': replan_decision.reply,
                }
            }
        # replan_decision.action == 'leave': fall through cleanly to normal assistant turn handling!
    focus = session.get(Goal, row.focus_goal_id) if row and row.focus_goal_id and not stale else None
    ctx = DialogContext(kind='pending_candidates' if row and row.proposal_id and not stale else 'idle',
                        subject={'goal_title': focus.title} if focus else {})
    # Preserve recent references without copying internal legacy state into the browser.
    if row and not stale:
        ctx.subject['recent_messages'] = raw_messages[-8:]
    decision = turn_engine.decide(session, principal.user_id, body['text'], local,
                                  principal.tz, ctx, client=turn_client(), lang=lang, habit_drafts=True)
    return decision.model_dump() if decision else None


def read_state(svc):
    from .models import WebProposal
    row = thread(svc.session, svc.user_id)
    raw_messages = json.loads(row.messages_json) if row else []
    if _is_stale_thread(raw_messages, svc.now, svc.tz):
        return {'messages': [], 'version': str(row.version if row else 0), 'proposal': None}
    proposal = None
    if row and row.proposal_id:
        candidate = svc.session.get(WebProposal, row.proposal_id)
        if candidate and candidate.status == 'open' and candidate.expires_at > svc.now.replace(tzinfo=None):
            proposal = svc.serialize_proposal(candidate)
    latest = raw_messages[-1] if raw_messages else {}
    return {'messages': raw_messages, 'version': str(row.version if row else 0),
            'proposal': proposal, **{key: latest[key] for key in ('create_goal', 'create_habit', 'replan_goal') if key in latest}}


def clear_thread(svc, body):
    from .core import ApiError
    from .models import WebProposal
    lang = getattr(svc, 'lang', 'zh')
    row = thread(svc.session, svc.user_id)
    if body['version'] != str(row.version if row else 0):
        raise ApiError(
            'stale_conversation',
            pick(lang, '对话已更新，请刷新后继续。', 'Conversation updated elsewhere; please refresh to continue.'),
        )
    if row is None:
        row = WebThread(user_id=svc.user_id, version=0, messages_json='[]', choices_json='[]')
        svc.session.add(row)
    if row.proposal_id:
        prop = svc.session.get(WebProposal, row.proposal_id)
        if prop and prop.status == 'open':
            prop.status = 'superseded'
            prop.version += 1
    row.messages_json = '[]'
    row.choices_json = '[]'
    row.proposal_id = None
    row.focus_goal_id = None
    row.version += 1
    svc.session.flush()
    return {
        **read_state(svc),
        'message': pick(lang, '对话已清空', 'Conversation cleared'),
    }


def _number(text):
    match = re.fullmatch(r'\s*(?:选|选择|安排|就|用|choose|pick|book)?\s*(?:第)?\s*([一二三四五六1-6])\s*(?:个|项|个吧|吧|个时间)?[。.!！]?\s*', text, re.I)
    if not match:
        return None
    token = match.group(1)
    return int(token)-1 if token.isdigit() else '一二三四五六'.index(token)


def _disambiguate(named, lang: str) -> str:
    if lang == 'en':
        joined = ' or '.join(f'"{g.title}"' for g in named)
        return f'Which goal did you mean: {joined}?'
    return '你指的是' + '还是'.join(f'「{g.title}」' for g in named) + '？'


def turn(svc, body):
    from .core import ApiError
    from .models import WebProposal
    lang = getattr(svc, 'lang', 'zh')
    row = thread(svc.session, svc.user_id)
    if body['version'] != str(row.version if row else 0):
        raise ApiError(
            'stale_conversation',
            pick(lang, '对话已更新，请刷新后继续。', 'Conversation updated elsewhere; please refresh to continue.'),
        )
    if row is None:
        row = WebThread(user_id=svc.user_id, version=0, messages_json='[]', choices_json='[]')
        svc.session.add(row)
    text = body['text'].strip()
    if not text:
        raise ApiError('empty_message', pick(lang, '请输入想说的话。', 'Please enter a message.'), 422)
    messages = json.loads(row.messages_json)
    if _is_stale_thread(messages, svc.now, svc.tz):
        messages = []
        row.proposal_id = None
        row.focus_goal_id = None
        row.choices_json = '[]'
    now_iso = svc.now.isoformat()
    messages.append({'id':uuid.uuid4().hex, 'role':'user', 'content':text, 'ts': now_iso})
    local = svc.now.astimezone(ZoneInfo(svc.tz))
    old_proposal = svc.session.get(WebProposal, row.proposal_id) if row.proposal_id else None
    choices = json.loads(row.choices_json)
    index = _number(text)
    extra = {}
    if index is not None and old_proposal and not choices:
        result = svc.accept(old_proposal.id, {'version':str(old_proposal.version), 'index':index})
        reply = result['message']
        extra = {k:v for k,v in result.items() if k not in ('message', 'proposal')}
        row.proposal_id = None
    else:
        # Every new instruction invalidates prior candidates in the same atomic turn.
        for candidate in svc.session.scalars(select(WebProposal).where(
                WebProposal.user_id == svc.user_id, WebProposal.status == 'open')):
            candidate.status = 'superseded'
            candidate.version += 1
        row.proposal_id = None
        goals = list(svc.session.scalars(select(Goal).where(Goal.user_id == svc.user_id, Goal.status == 'active')))
        named = [g for g in goals if g.title.casefold() in text.casefold()]
        if index is not None and choices and index < len(choices):
            named = [g for g in goals if g.id == choices[index]]
        row.choices_json = '[]'
        action = parse_with_rules(text, local)
        decision_data = body.get('_decision')
        if isinstance(decision_data, dict) and '_replan_cancel' in decision_data:
            reply = decision_data.get('reply') or pick(lang, '好的，已取消重新规划，保持原计划不变。', 'Okay, cancelled replanning — your current plan stays unchanged.')
        elif isinstance(decision_data, dict) and '_replan_ambiguous' in decision_data:
            if goals:
                ids = [g.id for g in goals[:6]]
                row.choices_json = json.dumps(ids)
                extra['replan_disambiguate'] = ids
                reply = _disambiguate(goals[:6], lang)
            else:
                reply = pick(lang, '目前没有进行中的目标可以重新规划。', 'There are no active goals to replan right now.')
        elif isinstance(decision_data, dict) and '_replan_apply' in decision_data:
            pending = decision_data['_replan_apply']
            target_goal = next((g for g in goals if g.id == pending.get('goal_id')), None)
            if target_goal is None:
                reply = pick(lang, '没有找到这个进行中的目标。', 'Could not find that active goal.')
            else:
                apply_body: dict = {'version': pending['goal_version']}
                if pending.get('steps_changed', True):
                    apply_body['steps'] = pending['steps']
                    if pending.get('outline'):
                        apply_body['outline'] = pending['outline']
                if pending.get('deadline') is not None:
                    apply_body['deadline'] = pending['deadline']
                result = svc.replan_goal(target_goal.id, apply_body, update_thread=False)
                reply = result['message'] + pick(lang, '。', '.')
                extra['goal'] = result['goal']
                extra['message'] = result['message']
        elif isinstance(decision_data, dict) and '_replan_ask' in decision_data:
            ask_data = decision_data['_replan_ask']
            ask_goal_id = ask_data['goal_id'] if isinstance(ask_data, dict) else ask_data
            ask_reply = ask_data.get('reply', '') if isinstance(ask_data, dict) else ''
            ask_context = ask_data.get('context') if isinstance(ask_data, dict) else None
            target_goal = next((g for g in goals if g.id == ask_goal_id), None)
            if target_goal is None:
                reply = pick(lang, '没有找到这个进行中的目标。', 'Could not find that active goal.')
            else:
                row.focus_goal_id = target_goal.id
                extra['replan_prompt_goal_id'] = target_goal.id
                if ask_context:
                    extra['replan_context'] = ask_context
                if ask_reply:
                    reply = ask_reply
                else:
                    todo_steps = [s for s in sorted(target_goal.steps, key=lambda x: x.seq) if s.status == 'todo']
                    total_min = sum(s.est_min for s in todo_steps)
                    dl_note = (
                        pick(lang, f'，截止 {target_goal.deadline.isoformat()}', f', due {target_goal.deadline.isoformat()}')
                        if target_goal.deadline else ''
                    )
                    reply = pick(
                        lang,
                        f'「{target_goal.title}」目前还有 {len(todo_steps)} 个待办步骤（共 {total_min} 分钟{dl_note}）。\n你想怎么调整？比如：修改截止日期、缩短单步时长、放慢节奏，或调整重点方向？',
                        f'"{target_goal.title}" currently has {len(todo_steps)} remaining todo steps ({total_min} min total{dl_note}).\nHow would you like to adjust it — change the deadline, shorten steps, slow the pace, or shift focus?',
                    )
        elif isinstance(decision_data, dict) and '_replan' in decision_data:
            replan_data = decision_data['_replan']
            target_goal = next((g for g in goals if g.id == replan_data['goal_id']), None)
            if target_goal is None:
                reply = pick(lang, '没有找到这个进行中的目标。', 'Could not find that active goal.')
            else:
                row.focus_goal_id = target_goal.id
                serialized_goal = svc.serialize_goal(target_goal)
                steps = replan_data['steps']
                steps_changed = replan_data.get('steps_changed', True)
                total_min = sum(int(s.get('est_min', 30)) for s in steps)
                dl_str = replan_data.get('deadline')
                prev_dl_str = replan_data.get('previous_deadline')
                extra['replan_goal'] = {
                    'goal_id': target_goal.id,
                    'goal_title': target_goal.title,
                    'goal_version': serialized_goal['version'],
                    'deadline': dl_str,
                    'previous_deadline': prev_dl_str,
                    'steps_changed': steps_changed,
                    'outline': replan_data.get('outline') or [],
                    'steps': steps,
                    'source': replan_data.get('source', 'llm'),
                    'instruction': replan_data.get('instruction', ''),
                }
                model_reply = (replan_data.get('reply') or '').strip()
                if model_reply and not re.search(r'已(?:经)?(?:更新|保存|应用|修改完成|改好了)|\b(?:saved|applied|updated the goal)\b', model_reply, re.I):
                    reply = model_reply
                elif not steps_changed:
                    if dl_str == '':
                        reply = pick(
                            lang,
                            f'拟取消「{target_goal.title}」的截止日期，保留现有 {len(steps)} 个待办步骤不变。\n请点击下方「确认修改」（或回复“确认”），若还想调整步骤也可以直接告诉我。',
                            f'Proposed removing the deadline for "{target_goal.title}" while keeping its {len(steps)} todo steps unchanged.\nClick "Apply changes" below (or reply "confirm"), or tell me if you want to adjust steps too.',
                        )
                    else:
                        reply = pick(
                            lang,
                            f'拟将「{target_goal.title}」的截止日期调整为 {dl_str}，保留现有 {len(steps)} 个待办步骤不变。\n请点击下方「确认修改」（或回复“确认”）；如果需要按新截止日期调整步骤节奏，直接告诉我。',
                            f'Proposed moving "{target_goal.title}" deadline to {dl_str} while keeping its {len(steps)} todo steps unchanged.\nClick "Apply changes" below (or reply "confirm"), or tell me if you also want to revise the steps.',
                        )
                else:
                    dl_note = (
                        pick(lang, '，取消截止日期', ', deadline removed')
                        if dl_str == ''
                        else pick(lang, f'，截止 {dl_str}', f', due {dl_str}') if dl_str else ''
                    )
                    reply = pick(
                        lang,
                        f'已为「{target_goal.title}」起草了新方案（{len(steps)} 个待办步骤，共 {total_min} 分钟{dl_note}）。\n请确认下方方案并点击「确认修改」（已完成步骤会保留）；如果还想微调，直接告诉我。',
                        f'Drafted an updated plan for "{target_goal.title}" ({len(steps)} todo steps, {total_min} min total{dl_note}).\nReview it below and click "Apply changes" (completed steps are kept), or tell me what else to tweak.',
                    )
        else:
            decision = turn_engine.TurnDecision.model_validate(decision_data) if decision_data else None
            habit_draft = _rule_habit_draft(text) or _model_habit_draft(text, decision)
            if decision:
                action = turn_engine.to_action(decision, text, local)
            action = action.normalized(local)
            target = action.target or action.progress_target
            if not named and target:
                named = [g for g in goals if g.title.casefold() == target.casefold()]
            focus = next((g for g in goals if g.id == row.focus_goal_id), None)
            page = next((g for g in goals if g.id == body.get('page_goal_id')), None)
            window = extract_window(text, local) or action.window
            non_schedule_intents = {
                'query_events', 'add_event', 'move_event', 'delete_event',
                'create_goal', 'delete_goal', 'pause_goal', 'resume_goal', 'log_progress',
            }
            followup = bool(
                action.intent not in non_schedule_intents
                and (window or (old_proposal and named) or (choices and named))
            )
            explicit_schedule_kw = bool(
                re.search(r'找(?:个)?时间|安排(?:一下)?(?:下一步|时间|到日历|进日历)|排期|\b(?:schedule\b|find\s+time)\b', text, re.I)
            ) and not QUERY_MARKERS.search(text)
            wants_schedule = (
                action.intent == 'schedule_step'
                or (action.intent not in non_schedule_intents and explicit_schedule_kw)
                or followup
            )
            if NO_SCHEDULE.search(text) or ADVICE_QUESTION.search(text):
                wants_schedule = False
            goal = named[0] if len(named) == 1 else (focus or page or (goals[0] if len(goals) == 1 else None))
            # A newly named but unknown subject must not silently fall back to the previous goal.
            unknown_switch = re.search(r'^(?:那|换成|改成|换个目标)\s*(.+?)(?:呢|吧|怎么样)[？?。]?$|^(?:what about)\s+(.+)', text, re.I)
            if unknown_switch and not named and not window:
                goal = None
                wants_schedule = True
            if habit_draft:
                row.focus_goal_id = None
                extra['create_habit'] = habit_draft
                reply = pick(
                    lang,
                    '这适合做成一个习惯。打开草稿，确认重复节奏和每次时长后就可以保存。',
                    'This fits a habit. Open the draft to review the repeat pattern and session length before saving.',
                )
            elif len(named) > 1:
                row.choices_json = json.dumps([g.id for g in named])
                reply = _disambiguate(named, lang)
            elif action.intent == 'create_goal':
                from ..llm.replan import extract_replan_deadline
                title = action.title or text
                extra['create_goal'] = {'title':title}
                dl_iso = action.deadline.isoformat() if action.deadline else extract_replan_deadline(text, local, None)
                if dl_iso and dl_iso != 'CLEAR':
                    extra['create_goal']['deadline'] = dl_iso
                reply = pick(
                    lang,
                    f'可以，一起把「{title}」拆成可推进的步骤。打开新目标流程后，可以编辑计划再保存。',
                    f'Sure — let\'s break "{title}" into actionable steps. Open the New Goal flow to review and save the plan.',
                )
            elif action.intent == 'pause_goal':
                if goal is None:
                    if goals:
                        row.choices_json = json.dumps([g.id for g in goals[:6]])
                        reply = pick(lang, '你想暂停哪一个目标？', 'Which goal would you like to pause?') + '\n' + _disambiguate(goals[:6], lang)
                    else:
                        reply = pick(lang, '目前没有进行中的目标可以暂停。', 'There are no active goals to pause.')
                else:
                    ver = svc.serialize_goal(goal)['version']
                    result = svc.update_goal(goal.id, {'version': ver, 'status': 'paused'})
                    reply = pick(
                        lang,
                        f'已暂停目标「{goal.title}」（未开始的排期已取消），随时可以在「目标」→「已暂停」恢复。',
                        f'Paused "{goal.title}" (future scheduled blocks were cancelled). You can resume it anytime under Goals → Paused.',
                    )
                    extra['goal'] = result['goal']
                    extra['message'] = result['message']
            elif action.intent == 'resume_goal':
                paused_goals = list(svc.session.scalars(select(Goal).where(Goal.user_id == svc.user_id, Goal.status == 'paused')))
                paused_named = [g for g in paused_goals if g.title.casefold() in text.casefold() or (target and g.title.casefold() == target.casefold())]
                target_paused = paused_named[0] if len(paused_named) == 1 else (paused_goals[0] if len(paused_goals) == 1 else None)
                if target_paused is None:
                    reply = (
                        pick(lang, '你想恢复哪一个已暂停的目标？', 'Which paused goal would you like to resume?') + '\n' + _disambiguate(paused_goals[:6], lang)
                        if paused_goals
                        else pick(lang, '目前没有已暂停的目标。', 'There are no paused goals right now.')
                    )
                else:
                    ver = svc.serialize_goal(target_paused)['version']
                    result = svc.update_goal(target_paused.id, {'version': ver, 'status': 'active'})
                    reply = pick(
                        lang,
                        f'已恢复目标「{target_paused.title}」，现在可以继续推进或安排步骤。',
                        f'Resumed "{target_paused.title}". You can now continue or schedule its next step.',
                    )
                    extra['goal'] = result['goal']
                    extra['message'] = result['message']
            elif wants_schedule:
                if goal is None:
                    row.choices_json = json.dumps([g.id for g in goals[:6]])
                    reply = (
                        _disambiguate(goals[:6], lang)
                        if goals
                        else pick(lang, '先创建一个目标，就可以为第一步找时间。', 'Create a goal first, then I can find time for its first step.')
                    )
                else:
                    step = next((s for s in sorted(goal.steps, key=lambda x:x.seq) if s.status == 'todo'), None)
                    exact_steps = [s for s in goal.steps if s.status == 'todo' and s.title in text]
                    if len(exact_steps) == 1:
                        step = exact_steps[0]
                    if step is None:
                        reply = pick(
                            lang,
                            f'「{goal.title}」目前没有待安排的步骤。',
                            f'"{goal.title}" currently has no pending steps to schedule.',
                        )
                    else:
                        params = {'window_start':window[0].isoformat(),'window_end':window[1].isoformat()} if window else {}
                        proposal = svc.propose(step.id, params)
                        row.proposal_id = proposal['id']
                        row.focus_goal_id = goal.id
                        if proposal['candidates']:
                            reply = pick(
                                lang,
                                f'为「{goal.title}」的「{step.title}」找到了这些时间，选一个就能安排。',
                                f'Found these slots for "{goal.title}" · "{step.title}". Pick one to book it.',
                            )
                        else:
                            reply = pick(
                                lang,
                                f'「{goal.title}」在这个范围没有足够空档，可以换个时间或调整可安排时间。',
                                f'No open slots fit "{goal.title}" in that range. Try another window or adjust your availability.',
                            )
            elif action.intent == 'add_event':
                if action.start and action.end and action.title:
                    result = svc.create_event({'title':action.title,'start':action.start.isoformat(),'end':action.end.isoformat()})
                    reply = result['message']; extra.update({k:v for k,v in result.items() if k != 'message'})
                else:
                    reply = pick(
                        lang,
                        '请告诉我日程名称、开始时间和时长，例如“明天 10 点开会一小时”。',
                        'Please share the event title, start time, and duration — e.g. "Meeting tomorrow at 10am for 1 hour".',
                    )
            elif action.intent == 'query_events':
                target_dt = action.start.astimezone(local.tzinfo) if action.start else local
                day_start = target_dt.replace(hour=0, minute=0, second=0, microsecond=0)
                day_label = (
                    pick(lang, '今天', 'Today') if day_start.date() == local.date()
                    else pick(lang, '明天', 'Tomorrow') if day_start.date() == (local + timedelta(days=1)).date()
                    else day_start.date().isoformat()
                )
                events = svc.events(day_start.isoformat(), (day_start + timedelta(days=1)).isoformat())['events']
                if not events:
                    reply = pick(lang, f'{day_label}目前没有安排。', f'Nothing is scheduled for {day_label}.')
                else:
                    lines = []
                    for e in events:
                        s_local = datetime.fromisoformat(e['start'].replace('Z', '+00:00')).astimezone(local.tzinfo)
                        e_local = datetime.fromisoformat(e['end'].replace('Z', '+00:00')).astimezone(local.tzinfo)
                        span = f'{s_local.strftime("%H:%M")}–{e_local.strftime("%H:%M")}'
                        lines.append(f'- {span} "{e["title"]}"' if lang == 'en' else f'- {span}「{e["title"]}」')
                    header = pick(lang, f'{day_label}共有 {len(events)} 项安排：', f'{day_label} has {len(events)} scheduled item(s):')
                    reply = header + '\n' + '\n'.join(lines)
            elif action.intent in {'move_event','delete_event'}:
                hint = action.target or action.title or ''
                matches = list(svc.session.scalars(select(Event).where(Event.user_id == svc.user_id,
                               Event.title == hint, Event.kind != 'habit', Event.status.in_(['planned','moved']))))
                if len(matches) != 1:
                    reply = pick(
                        lang,
                        '请在日历中选中要修改的日程，或告诉我准确名称和日期。',
                        'Please select the event on the calendar or share its exact title and date.',
                    )
                else:
                    ev = matches[0]; version = svc.serialize_event(ev)['version']
                    if action.intent == 'delete_event':
                        result = svc.cancel_event(ev.id, {'version':version})
                    elif action.start:
                        duration = ev.end_utc-ev.start_utc
                        result = svc.move_event(ev.id, {'version':version,'start':action.start.isoformat(),
                                                       'end':(action.start+duration).isoformat()})
                    else:
                        result = {'message': pick(lang, f'想把「{ev.title}」移到什么时间？', f'When would you like to move "{ev.title}" to?')}
                    reply = result['message']; extra.update({k:v for k,v in result.items() if k != 'message'})
            elif action.intent == 'log_progress':
                hint = (action.progress_target or text).casefold()
                matched_step = next(
                    ((g, s) for g in goals for s in g.steps
                     if s.status == 'todo' and (s.title.casefold() in hint or (len(hint) >= 2 and hint in s.title.casefold()))),
                    None,
                )
                if matched_step:
                    mg, ms = matched_step
                    reply = pick(
                        lang,
                        f'太棒了！可以在「目标」页直接勾掉「{mg.title}」的「{ms.title}」，或在「今天」的进行中卡片里打卡完成。',
                        f'Nice work! You can check off "{ms.title}" under "{mg.title}" in Goals, or check in from the running card on Today.',
                    )
                else:
                    reply = pick(
                        lang,
                        '在“今天”选择正在执行的步骤，即可记录完成情况；也可以在目标详情中标记步骤完成。',
                        'Check in from the running session on Today, or mark the step done in Goals.',
                    )
            else:
                if named:
                    row.focus_goal_id = named[0].id
                # Free text cannot claim an unexecuted mutation. LLM chat is only advisory.
                fallback_chat = pick(
                    lang,
                    '我可以起草新目标和习惯，也可以为目标步骤找时间或调整日程。你想先做什么？',
                    'I can draft goals and habits, find time for goal steps, or adjust events. What would you like to do?',
                )
                reply = (decision.reply or '').strip() if decision and (decision.reply or '').strip() else fallback_chat
                if re.search(r'已(?:经)?(?:安排|创建|删除|完成|取消|修改)|\b(?:booked|scheduled|deleted|created)\b', reply, re.I):
                    reply = pick(
                        lang,
                        '可以继续告诉我你希望怎样安排；执行结果会在完成操作后明确显示。',
                        'Tell me how you would like to schedule or adjust it — changes only take effect once confirmed.',
                    )
    messages.append({'id':uuid.uuid4().hex, 'role':'assistant', 'content':reply, 'ts': now_iso,
                     **{key: extra[key] for key in ('create_goal', 'create_habit', 'replan_goal', 'replan_prompt_goal_id', 'replan_context', 'replan_disambiguate') if key in extra}})
    row.messages_json = json.dumps(messages[-40:], ensure_ascii=False)
    row.version += 1
    svc.session.flush()
    return {**read_state(svc), **{k: v for k, v in extra.items() if k not in ('replan_prompt_goal_id', 'replan_context', 'replan_disambiguate')}}
