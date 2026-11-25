"""Versioned, transactional migration; failure aborts startup."""
from .storage import backup, connect, database_path
from .models import Base
from .web import models as _web_models
from .web import transport_models as _transport_models

VERSION = 16


def migrate(engine):
    if engine.url.database == ':memory:':
        Base.metadata.create_all(engine)
        return
    path = database_path(str(engine.url))
    existed = path.exists() and path.stat().st_size > 0
    with connect(path) as con:
        version = con.execute('PRAGMA user_version').fetchone()[0]
    if version > VERSION:
        raise RuntimeError('Database is newer than this application; restore a compatible backup')
    if version == VERSION:
        return
    if existed:
        backup(path, prefix='pre-migration')
    # BEGIN IMMEDIATE makes schema + version atomic, even on concurrent startup.
    with engine.connect() as con:
        con.exec_driver_sql('BEGIN IMMEDIATE')
        try:
            if con.exec_driver_sql('PRAGMA user_version').scalar() == VERSION:
                con.rollback()
                return
            Base.metadata.create_all(con)
            columns = {r[1] for r in con.exec_driver_sql('PRAGMA table_info(users)')}
            for name, ddl in [('username', 'TEXT'), ('email', 'TEXT'), ('password_hash', 'TEXT'),
                              ('disabled', 'BOOLEAN NOT NULL DEFAULT 0')]:
                if name not in columns:
                    con.exec_driver_sql(f'ALTER TABLE users ADD COLUMN {name} {ddl}')
            con.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users(username)')
            con.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users(email)')
            for ddl in [
                'CREATE TABLE IF NOT EXISTS invites (digest TEXT PRIMARY KEY, expires REAL, used_by INTEGER REFERENCES users(id))',
                'CREATE TABLE IF NOT EXISTS auth_sessions (digest TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires REAL NOT NULL)',
                'CREATE TABLE IF NOT EXISTS password_reset_tokens (digest TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires REAL NOT NULL, used_at REAL)',
                'CREATE INDEX IF NOT EXISTS ix_password_reset_tokens_user_id ON password_reset_tokens(user_id)',
                'CREATE TABLE IF NOT EXISTS login_limits (key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, reset_at REAL NOT NULL)',
                'CREATE TABLE IF NOT EXISTS ai_usage (user_id INTEGER NOT NULL REFERENCES users(id), day TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(user_id,day))',
            ]:
                con.exec_driver_sql(ddl)
            invite_columns = {r[1]: r for r in con.exec_driver_sql('PRAGMA table_info(invites)')}
            needs_rebuild = 'max_uses' not in invite_columns
            if 'expires' in invite_columns and invite_columns['expires'][3]:
                needs_rebuild = True
            if needs_rebuild:
                con.exec_driver_sql('ALTER TABLE invites RENAME TO invites_old')
                con.exec_driver_sql('CREATE TABLE invites (digest TEXT PRIMARY KEY, expires REAL, used_by INTEGER REFERENCES users(id), max_uses INTEGER NOT NULL DEFAULT 1)')
                con.exec_driver_sql('INSERT INTO invites (digest, expires, used_by, max_uses) SELECT digest, expires, used_by, 1 FROM invites_old')
                con.exec_driver_sql('DROP TABLE invites_old')
            con.exec_driver_sql('CREATE TABLE IF NOT EXISTS invite_uses (digest TEXT NOT NULL REFERENCES invites(digest), user_id INTEGER NOT NULL REFERENCES users(id), PRIMARY KEY(digest, user_id))')
            con.exec_driver_sql('INSERT OR IGNORE INTO invite_uses (digest, user_id) SELECT digest, used_by FROM invites WHERE used_by IS NOT NULL')
            goal_columns = {r[1] for r in con.exec_driver_sql('PRAGMA table_info(goals)')}
            if 'draft_id' not in goal_columns:
                con.exec_driver_sql('ALTER TABLE goals ADD COLUMN draft_id TEXT')
            con.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS ix_goals_draft_id ON goals(draft_id)')
            if 'pinned' not in goal_columns:
                con.exec_driver_sql('ALTER TABLE goals ADD COLUMN pinned BOOLEAN NOT NULL DEFAULT 0')
            exec_columns = {r[1]: r for r in con.exec_driver_sql('PRAGMA table_info(web_executions)')}
            if exec_columns and ('habit_occurrence_id' not in exec_columns or exec_columns['step_id'][3]):
                con.exec_driver_sql('ALTER TABLE web_executions RENAME TO web_executions_old')
                con.exec_driver_sql(
                    'CREATE TABLE web_executions ('
                    'id VARCHAR(32) NOT NULL PRIMARY KEY, '
                    'user_id INTEGER NOT NULL REFERENCES users(id), '
                    'event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE, '
                    'step_id INTEGER REFERENCES steps(id) ON DELETE CASCADE, '
                    'habit_occurrence_id VARCHAR(40) REFERENCES habit_occurrences(id) ON DELETE CASCADE, '
                    'status VARCHAR(20) NOT NULL, '
                    'started_at DATETIME NOT NULL, '
                    'expected_end_at DATETIME NOT NULL, '
                    'resolved_at DATETIME, '
                    'version INTEGER NOT NULL)'
                )
                con.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_web_executions_user_id ON web_executions(user_id)')
                con.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_web_executions_event_id ON web_executions(event_id)')
                con.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_web_executions_step_id ON web_executions(step_id)')
                con.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_web_executions_habit_occurrence_id ON web_executions(habit_occurrence_id)')
                con.exec_driver_sql(
                    'INSERT INTO web_executions '
                    '(id, user_id, event_id, step_id, habit_occurrence_id, status, started_at, expected_end_at, resolved_at, version) '
                    'SELECT id, user_id, event_id, step_id, NULL, status, started_at, expected_end_at, resolved_at, version '
                    'FROM web_executions_old'
                )
                con.exec_driver_sql('DROP TABLE web_executions_old')
            habit_columns = {r[1] for r in con.exec_driver_sql('PRAGMA table_info(habits)')}
            if habit_columns and 'pinned' not in habit_columns:
                con.exec_driver_sql('ALTER TABLE habits ADD COLUMN pinned BOOLEAN NOT NULL DEFAULT 0')
            event_columns = {r[1] for r in con.exec_driver_sql('PRAGMA table_info(events)')}
            if event_columns and 'sync_source_id' not in event_columns:
                con.exec_driver_sql(
                    'ALTER TABLE events ADD COLUMN sync_source_id INTEGER '
                    'REFERENCES calendar_sources(id) ON DELETE CASCADE'
                )
            if event_columns and 'all_day' not in event_columns:
                con.exec_driver_sql('ALTER TABLE events ADD COLUMN all_day BOOLEAN NOT NULL DEFAULT 0')
            con.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_events_sync_source_id ON events(sync_source_id)')
            source_columns = {r[1] for r in con.exec_driver_sql('PRAGMA table_info(calendar_sources)')}
            if source_columns:
                source_alters = [
                    ('connection_id', 'INTEGER REFERENCES calendar_connections(id) ON DELETE CASCADE'),
                    ('remote_calendar_id', "VARCHAR(500) NOT NULL DEFAULT ''"),
                    ('color', "VARCHAR(16) NOT NULL DEFAULT '#6b7f89'"),
                    ('blocks_scheduling', 'BOOLEAN NOT NULL DEFAULT 1'),
                    ('all_day_mode', "VARCHAR(16) NOT NULL DEFAULT 'ignore'"),
                    ('writable', 'BOOLEAN NOT NULL DEFAULT 0'),
                    ('is_goalcal_calendar', 'BOOLEAN NOT NULL DEFAULT 0'),
                    ('webhook_channel_id', 'VARCHAR(128)'),
                    ('webhook_resource_id', 'VARCHAR(255)'),
                    ('webhook_secret', 'VARCHAR(128)'),
                    ('webhook_expires_at', 'DATETIME'),
                ]
                for col_name, col_ddl in source_alters:
                    if col_name not in source_columns:
                        con.exec_driver_sql(f'ALTER TABLE calendar_sources ADD COLUMN {col_name} {col_ddl}')
                con.exec_driver_sql(
                    'CREATE INDEX IF NOT EXISTS ix_calendar_sources_connection_id ON calendar_sources(connection_id)'
                )
                con.exec_driver_sql(
                    'CREATE INDEX IF NOT EXISTS ix_calendar_sources_webhook_channel_id ON calendar_sources(webhook_channel_id)'
                )
            if con.exec_driver_sql('PRAGMA foreign_key_check').first():
                raise RuntimeError('Database has invalid foreign keys; repair the development copy before import')
            con.exec_driver_sql(f'PRAGMA user_version={VERSION}')
            con.commit()
        except BaseException:
            con.rollback()
            raise
