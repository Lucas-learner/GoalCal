"""Local-only management: python -m goalcal.admin --help."""
import argparse
import json
import getpass
import logging
import sqlite3
from pathlib import Path
from . import auth
from .db import init_db, get_engine
from .storage import connect, database_path, backup, restore
from .logging_setup import configure_logging


def import_legacy(source, source_user, username):
    source = Path(source).expanduser().resolve()
    if source == database_path() or not source.is_file():
        raise ValueError('Choose an existing, separate development database')
    snapshot = backup(source, prefix='legacy-source')
    backup(prefix='pre-import')
    maps = {}
    with sqlite3.connect(f'file:{snapshot}?mode=ro', uri=True) as old, connect() as new:
        old.row_factory = sqlite3.Row
        new.execute('BEGIN IMMEDIATE')
        user = new.execute('SELECT id FROM users WHERE username=?', (username.lower(),)).fetchone()
        if not user:
            raise ValueError('Target account does not exist')
        uid = user['id']
        if not old.execute('SELECT id FROM users WHERE id=?', (source_user,)).fetchone():
            raise ValueError('Source user does not exist')
        for table in ('goals', 'events', 'state_logs', 'interactions'):
            if new.execute(f'SELECT 1 FROM {table} WHERE user_id=?', (uid,)).fetchone():
                raise ValueError('Import requires an empty target account')
        for table in ('goals', 'steps', 'events', 'checkins', 'state_logs', 'reschedule_logs', 'interactions'):
            maps[table] = {}
            if table in ('goals', 'events', 'state_logs', 'interactions'):
                rows = old.execute(f'SELECT * FROM {table} WHERE user_id=?', (source_user,))
            elif table == 'steps':
                rows = old.execute('SELECT * FROM steps WHERE goal_id IN (SELECT id FROM goals WHERE user_id=?)', (source_user,))
            elif table == 'checkins':
                rows = old.execute('SELECT * FROM checkins WHERE step_id IN (SELECT s.id FROM steps s JOIN goals g ON s.goal_id=g.id WHERE g.user_id=?)', (source_user,))
            else:
                rows = old.execute('SELECT * FROM reschedule_logs WHERE event_id IN (SELECT id FROM events WHERE user_id=?)', (source_user,))
            for row in rows:
                values = dict(row)
                old_id = values.pop('id')
                if 'user_id' in values:
                    values['user_id'] = uid
                for field, related in [('goal_id', 'goals'), ('step_id', 'steps'), ('event_id', 'events'), ('new_event_id', 'events')]:
                    if values.get(field) is not None:
                        if values[field] not in maps[related]:
                            raise ValueError('Legacy data contains cross-user references')
                        values[field] = maps[related][values[field]]
                columns = ','.join(values)
                placeholders = ','.join('?' for _ in values)
                maps[table][old_id] = new.execute(f'INSERT INTO {table} ({columns}) VALUES ({placeholders})', tuple(values.values())).lastrowid
        if new.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Imported data violates foreign keys')
    logging.getLogger('goalcal.audit').info('legacy_imported user_id=%s', uid)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    create = sub.add_parser('create-user'); create.add_argument('username'); create.add_argument('--tz', default='Asia/Shanghai')
    inv = sub.add_parser('invite'); inv.add_argument('--days', type=int, default=7)
    for name in ('disable', 'enable', 'reset-password'):
        sub.add_parser(name).add_argument('username')
    sub.add_parser('migrate'); sub.add_parser('backup')
    diagnostic = sub.add_parser('diagnostics'); diagnostic.add_argument('--days', type=int, default=7)
    feedback = sub.add_parser('feedback')
    selection = feedback.add_mutually_exclusive_group()
    selection.add_argument('--id', type=int); selection.add_argument('--resolve', type=int)
    res = sub.add_parser('restore'); res.add_argument('source'); res.add_argument('--app-stopped', action='store_true', required=True)
    imp = sub.add_parser('import-legacy'); imp.add_argument('source'); imp.add_argument('--source-user', type=int, required=True); imp.add_argument('--target', required=True)
    args = parser.parse_args()
    configure_logging()
    if args.command == 'restore':
        get_engine().dispose()
        restore(args.source)
        print('Restored; all prior sessions invalidated.')
        return
    init_db()
    if args.command == 'create-user':
        password = getpass.getpass('Password (12+ characters): ')
        if password != getpass.getpass('Repeat password: '):
            raise ValueError('Passwords do not match')
        auth.create_user(args.username, password, args.tz, admin=True)
    elif args.command == 'invite':
        print(auth.invite(args.days))
    elif args.command in ('disable', 'enable'):
        auth.manage(args.username, disabled=args.command == 'disable')
    elif args.command == 'reset-password':
        password = getpass.getpass('New password: ')
        if password != getpass.getpass('Repeat password: '):
            raise ValueError('Passwords do not match')
        auth.manage(args.username, password=password)
    elif args.command in ('diagnostics', 'feedback'):
        from . import diagnostics, feedback
        from .db import session_scope
        with session_scope() as session:
            if args.command == 'diagnostics':
                result = diagnostics.report(session, args.days)
            elif args.resolve is not None:
                feedback.resolve(session, args.resolve)
                result = {'resolved': args.resolve}
            else:
                result = feedback.list_feedback(session, feedback_id=args.id)
                if args.id is None:
                    result = [{k: v for k, v in row.items() if k != 'message'} for row in result]
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif args.command == 'backup':
        print(backup())
    elif args.command == 'import-legacy':
        import_legacy(args.source, args.source_user, args.target)
    logging.getLogger('goalcal.audit').info('admin_action=%s', args.command)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        logging.getLogger('goalcal').error('local_command_failed error_type=%s', type(exc).__name__)
        raise SystemExit(f'Operation failed ({type(exc).__name__}); check local configuration and backups') from None
