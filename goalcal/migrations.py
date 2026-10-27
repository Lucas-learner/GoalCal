"""Versioned, transactional migration; failure aborts startup."""
from .storage import backup, connect, database_path
from .models import Base

VERSION = 4


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
            for name, ddl in [('username', 'TEXT'), ('password_hash', 'TEXT'),
                              ('disabled', 'BOOLEAN NOT NULL DEFAULT 0')]:
                if name not in columns:
                    con.exec_driver_sql(f'ALTER TABLE users ADD COLUMN {name} {ddl}')
            con.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users(username)')
            for ddl in [
                'CREATE TABLE IF NOT EXISTS invites (digest TEXT PRIMARY KEY, expires REAL NOT NULL, used_by INTEGER REFERENCES users(id))',
                'CREATE TABLE IF NOT EXISTS auth_sessions (digest TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires REAL NOT NULL)',
                'CREATE TABLE IF NOT EXISTS login_limits (key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, reset_at REAL NOT NULL)',
                'CREATE TABLE IF NOT EXISTS ai_usage (user_id INTEGER NOT NULL REFERENCES users(id), day TEXT NOT NULL, count INTEGER NOT NULL, PRIMARY KEY(user_id,day))',
            ]:
                con.exec_driver_sql(ddl)
            goal_columns = {r[1] for r in con.exec_driver_sql('PRAGMA table_info(goals)')}
            if 'draft_id' not in goal_columns:
                con.exec_driver_sql('ALTER TABLE goals ADD COLUMN draft_id TEXT')
            con.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS ix_goals_draft_id ON goals(draft_id)')
            if 'pinned' not in goal_columns:
                con.exec_driver_sql('ALTER TABLE goals ADD COLUMN pinned BOOLEAN NOT NULL DEFAULT 0')
            if con.exec_driver_sql('PRAGMA foreign_key_check').first():
                raise RuntimeError('Database has invalid foreign keys; repair the development copy before import')
            con.exec_driver_sql(f'PRAGMA user_version={VERSION}')
            con.commit()
        except BaseException:
            con.rollback()
            raise
