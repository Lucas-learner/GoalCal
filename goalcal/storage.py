"""SQLite maintenance. Only local administrators use these unscoped connections."""
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
import os
import sqlite3
from sqlalchemy.engine import make_url
from .config import settings


def database_path(url=None):
    parsed = make_url(url or settings.db_url)
    if parsed.drivername != 'sqlite' or not parsed.database or parsed.database == ':memory:':
        raise ValueError('A file-backed SQLite database is required')
    return Path(parsed.database).expanduser().resolve()


@contextmanager
def connect(path=None):
    path = Path(path or database_path())
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    con = sqlite3.connect(path, timeout=10)
    os.chmod(path, 0o600)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA busy_timeout=10000')
    try:
        yield con
        con.commit()
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def backup(path=None, directory=None, prefix='daily'):
    source = Path(path or database_path())
    directory = Path(directory or source.parent / 'backups')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    target = directory / f'{prefix}-{stamp}.db'
    with connect(source) as src, connect(target) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('Backup integrity check failed')
    if prefix == 'daily':
        cutoff = datetime.now(timezone.utc).timestamp() - timedelta(days=14).total_seconds()
        for old in directory.glob('daily-*.db'):
            if old.stat().st_mtime < cutoff:
                old.unlink()
    return target


def restore(source, target=None):
    """Caller must stop the app before restoring. Preserve a pre-restore snapshot."""
    source, target = Path(source).resolve(), Path(target or database_path()).resolve()
    if source == target or not source.is_file():
        raise ValueError('Choose an existing backup distinct from the database')
    with sqlite3.connect(f'file:{source}?mode=ro', uri=True) as src:
        if src.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Invalid backup')
        if src.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Backup has invalid foreign keys')
        if target.exists():
            backup(target, prefix='pre-restore')
        with connect(target) as dst:
            src.backup(dst)
            # Restoring must not resurrect old browser sessions.
            if dst.execute("SELECT 1 FROM sqlite_master WHERE name='auth_sessions'").fetchone():
                dst.execute('DELETE FROM auth_sessions')
