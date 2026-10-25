"""Read-only, content-free aggregate for local trial review."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
from sqlalchemy import select, func
from .models import Interaction, User
from . import metrics
from .storage import database_path
from .feedback import list_feedback


def report(session, days=7):
    if not 1 <= days <= 90:
        raise ValueError('days must be between 1 and 90')
    since = datetime.now(timezone.utc) - timedelta(days=days)
    counts = dict(session.execute(select(Interaction.kind, func.count()).where(
        Interaction.ts_wall >= since.replace(tzinfo=None)).group_by(Interaction.kind)).all())
    parses = session.scalars(select(Interaction.payload).where(
        Interaction.kind == metrics.PARSE, Interaction.ts_wall >= since.replace(tzinfo=None)))
    intents, parsers = Counter(), Counter()
    for payload in parses:
        data = json.loads(payload)
        intents[data.get('intent', 'unknown')] += 1
        parsers[data.get('parser', 'unknown')] += 1
    accounts = session.scalar(select(func.count()).select_from(User).where(User.username.is_not(None)))
    operations = defaultdict(lambda: {'completed': 0, 'failed': 0, 'durations': []})
    ai = Counter()
    errors = []
    for path in (database_path().parent / 'logs').glob('application.log*'):
        with path.open(errors='replace') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if datetime.fromisoformat(row['time']) < since:
                        continue
                except (ValueError, KeyError, TypeError):
                    continue  # Historic plain text logs are not exported.
                event = row.get('event')
                if event in ('operation_completed', 'operation_failed'):
                    group = operations[row['operation']]
                    group['completed' if event == 'operation_completed' else 'failed'] += 1
                    group['durations'].append(row['duration_ms'])
                    if event == 'operation_failed':
                        errors.append({key: row.get(key) for key in
                                       ('time', 'operation', 'operation_id', 'user_id', 'error_type', 'frames')})
                if event in ('llm_completed', 'llm_failed', 'llm_skipped'):
                    ai[event + ':' + row.get('reason', 'request')] += 1
    for group in operations.values():
        durations = sorted(group.pop('durations'))
        group['p95_ms'] = durations[int((len(durations)-1)*.95)]
    feedback = list_feedback(session, limit=100)
    return {'days': days, 'registered_accounts': accounts, 'interaction_counts': counts,
            'intent_counts': dict(intents), 'parser_counts': dict(parsers),
            'operations': dict(operations), 'ai': dict(ai),
            'recent_errors': sorted(errors, key=lambda row: row['time'], reverse=True)[:20],
            'feedback': [{k: row[k] for k in ('id', 'user_id', 'time', 'category', 'status', 'operation_id')}
                         for row in feedback]}
