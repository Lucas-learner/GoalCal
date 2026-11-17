"""Conversation understanding proposes intentions; Service owns every write."""
from __future__ import annotations

from datetime import timedelta
import json
import re
import uuid
from zoneinfo import ZoneInfo
from sqlalchemy import select

from ..models import Goal, Event, Step
from ..dialog import DialogContext
from ..llm import turn as turn_engine
from ..llm.rules import parse_with_rules, NO_SCHEDULE, ADVICE_QUESTION
from ..llm.router import turn_client
from ..llm.natdate import extract_window
from .transport_models import WebThread


def thread(session, uid):
    return session.scalar(select(WebThread).where(WebThread.user_id == uid))


def prepare_turn(session, principal, body, now):
    """Read-only LLM inference; the later transaction checks this context version."""
    from .core import ApiError
    row = thread(session, principal.user_id)
    if body['version'] != str(row.version if row else 0):
        raise ApiError('stale_conversation', '对话已更新，请刷新后继续。')
    local = now.astimezone(ZoneInfo(principal.tz))
    focus = session.get(Goal, row.focus_goal_id) if row and row.focus_goal_id else None
    ctx = DialogContext(kind='pending_candidates' if row and row.proposal_id else 'idle',
                        subject={'goal_title': focus.title} if focus else {})
    # Preserve recent references without copying internal legacy state into the browser.
    if row:
        ctx.subject['recent_messages'] = json.loads(row.messages_json)[-8:]
    decision = turn_engine.decide(session, principal.user_id, body['text'], local,
                                  principal.tz, ctx, client=turn_client())
    return decision.model_dump() if decision else None


def read_state(svc):
    from .models import WebProposal
    row = thread(svc.session, svc.user_id)
    proposal = None
    if row and row.proposal_id:
        candidate = svc.session.get(WebProposal, row.proposal_id)
        if candidate and candidate.status == 'open' and candidate.expires_at > svc.now.replace(tzinfo=None):
            proposal = svc.serialize_proposal(candidate)
    messages = json.loads(row.messages_json) if row else []
    creation = messages[-1].get('create_goal') if messages else None
    return {'messages': messages, 'version': str(row.version if row else 0),
            'proposal': proposal, **({'create_goal': creation} if creation else {})}


def _number(text):
    match = re.fullmatch(r'\s*(?:选|选择|安排|就|用|choose|pick|book)?\s*(?:第)?\s*([一二三四五六1-6])\s*(?:个|项|个吧|吧|个时间)?[。.!！]?\s*', text, re.I)
    if not match:
        return None
    token = match.group(1)
    return int(token)-1 if token.isdigit() else '一二三四五六'.index(token)


def turn(svc, body):
    from .core import ApiError
    from .models import WebProposal
    row = thread(svc.session, svc.user_id)
    if body['version'] != str(row.version if row else 0):
        raise ApiError('stale_conversation', '对话已更新，请刷新后继续。')
    if row is None:
        row = WebThread(user_id=svc.user_id, version=0, messages_json='[]', choices_json='[]')
        svc.session.add(row)
    text = body['text'].strip()
    if not text:
        raise ApiError('empty_message', '请输入想说的话。', 422)
    messages = json.loads(row.messages_json)
    messages.append({'id':uuid.uuid4().hex, 'role':'user', 'content':text})
    local = svc.now.astimezone(ZoneInfo(svc.tz))
    old_proposal = svc.session.get(WebProposal, row.proposal_id) if row.proposal_id else None
    choices = json.loads(row.choices_json)
    index = _number(text)
    extra = {}
    if index is not None and old_proposal and not choices:
        result = svc.accept(old_proposal.id, {'version':str(old_proposal.version), 'index':index})
        reply = result['message']
        extra = {k:v for k,v in result.items() if k != 'message'}
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
        decision = turn_engine.TurnDecision.model_validate(decision_data) if decision_data else None
        if decision:
            action = turn_engine.to_action(decision, text, local)
        action = action.normalized(local)
        target = action.target or action.progress_target
        if not named and target:
            named = [g for g in goals if g.title.casefold() == target.casefold()]
        focus = next((g for g in goals if g.id == row.focus_goal_id), None)
        page = next((g for g in goals if g.id == body.get('page_goal_id')), None)
        window = extract_window(text, local) or action.window
        followup = bool(window or (old_proposal and named) or (choices and named))
        wants_schedule = action.intent == 'schedule_step' or bool(re.search(r'找(?:个)?时间|安排|排期|schedule|find time', text, re.I)) or followup
        if NO_SCHEDULE.search(text) or ADVICE_QUESTION.search(text):
            wants_schedule = False
        goal = named[0] if len(named) == 1 else (focus or page or (goals[0] if len(goals) == 1 else None))
        # A newly named but unknown subject must not silently fall back to the previous goal.
        unknown_switch = re.search(r'^(?:那|换成|改成|换个目标)\s*(.+?)(?:呢|吧|怎么样)[？?。]?$|^(?:what about)\s+(.+)', text, re.I)
        if unknown_switch and not named and not window:
            goal = None
            wants_schedule = True
        if len(named) > 1:
            row.choices_json = json.dumps([g.id for g in named])
            reply = '你指的是' + '还是'.join(f'「{g.title}」' for g in named) + '？'
        elif action.intent == 'create_goal':
            title = action.title or text
            extra['create_goal'] = {'title':title}
            if action.deadline:
                extra['create_goal']['deadline'] = action.deadline.isoformat()
            reply = f'可以，一起把「{title}」拆成可推进的步骤。打开新目标流程后，可以编辑计划再保存。'
        elif wants_schedule:
            if goal is None:
                row.choices_json = json.dumps([g.id for g in goals[:6]])
                reply = ('你指的是' + '还是'.join(f'「{g.title}」' for g in goals[:6]) + '？') if goals else '先创建一个目标，就可以为第一步找时间。'
            else:
                step = next((s for s in sorted(goal.steps, key=lambda x:x.seq) if s.status == 'todo'), None)
                exact_steps = [s for s in goal.steps if s.status == 'todo' and s.title in text]
                if len(exact_steps) == 1:
                    step = exact_steps[0]
                if step is None:
                    reply = f'「{goal.title}」目前没有待安排的步骤。'
                else:
                    params = {'window_start':window[0].isoformat(),'window_end':window[1].isoformat()} if window else {}
                    proposal = svc.propose(step.id, params)
                    row.proposal_id = proposal['id']
                    row.focus_goal_id = goal.id
                    reply = (f'为「{goal.title}」的「{step.title}」找到了这些时间，选一个就能安排。'
                             if proposal['candidates'] else f'「{goal.title}」在这个范围没有足够空档，可以换个时间或调整可安排时间。')
        elif action.intent == 'add_event':
            if action.start and action.end and action.title:
                result = svc.create_event({'title':action.title,'start':action.start.isoformat(),'end':action.end.isoformat()})
                reply = result['message']; extra.update({k:v for k,v in result.items() if k != 'message'})
            else:
                reply = '请告诉我日程名称、开始时间和时长，例如“明天 10 点开会一小时”。'
        elif action.intent == 'query_events':
            start = action.start or local.replace(hour=0,minute=0,second=0,microsecond=0)
            events = svc.events(start.isoformat(), (start+timedelta(days=1)).isoformat())['events']
            reply = '这个时间范围没有安排。' if not events else '\n'.join(f'「{e["title"]}」：{e["start"]} — {e["end"]}' for e in events)
        elif action.intent in {'move_event','delete_event'}:
            hint = action.target or action.title or ''
            matches = list(svc.session.scalars(select(Event).where(Event.user_id == svc.user_id,
                           Event.title == hint, Event.status.in_(['planned','moved']))))
            if len(matches) != 1:
                reply = '请在日历中选中要修改的日程，或告诉我准确名称和日期。'
            else:
                ev = matches[0]; version = svc.serialize_event(ev)['version']
                if action.intent == 'delete_event':
                    result = svc.cancel_event(ev.id, {'version':version})
                elif action.start:
                    duration = ev.end_utc-ev.start_utc
                    result = svc.move_event(ev.id, {'version':version,'start':action.start.isoformat(),
                                                   'end':(action.start+duration).isoformat()})
                else:
                    result = {'message':f'想把「{ev.title}」移到什么时间？'}
                reply = result['message']; extra.update({k:v for k,v in result.items() if k != 'message'})
        elif action.intent == 'log_progress':
            reply = '在“今天”选择正在执行的步骤，即可记录完成情况；也可以在目标详情中标记步骤完成。'
        else:
            if named:
                row.focus_goal_id = named[0].id
            # Free text cannot claim an unexecuted mutation. LLM chat is only advisory.
            reply = decision.reply if decision and decision.intent == 'chat' else '我可以帮你为目标找时间、调整日程，或把新目标带入计划流程。你想先推进哪一步？'
            if re.search(r'已(?:经)?(?:安排|创建|删除|完成|取消|修改)|\b(?:booked|scheduled|deleted|created)\b', reply, re.I):
                reply = '可以继续告诉我你希望怎样安排；执行结果会在完成操作后明确显示。'
    messages.append({'id':uuid.uuid4().hex, 'role':'assistant', 'content':reply,
                     **({'create_goal': extra['create_goal']} if 'create_goal' in extra else {})})
    row.messages_json = json.dumps(messages[-100:], ensure_ascii=False)
    row.version += 1
    svc.session.flush()
    return {**read_state(svc), **extra}
