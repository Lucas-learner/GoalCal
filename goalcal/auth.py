"""Local accounts, single-use invitation registration and revocable sessions."""
from dataclasses import dataclass
from hashlib import sha256
import logging
import re
import secrets
import time
from zoneinfo import ZoneInfo
from argon2 import PasswordHasher, Type
from argon2.exceptions import VerificationError
from .storage import connect

log = logging.getLogger('goalcal.audit')
_hasher = PasswordHasher(type=Type.ID)
_dummy = _hasher.hash(secrets.token_urlsafe(32))


class AuthError(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    user_id: int
    username: str
    tz: str


def digest(value):
    return sha256(value.encode()).hexdigest()


def credentials(username, password, tz):
    username = username.strip().lower()
    if not re.fullmatch(r'[a-z0-9_]{3,32}', username):
        raise AuthError('用户名须为 3–32 个英文字母、数字或下划线')
    if not 12 <= len(password) <= 256:
        raise AuthError('密码须为 12–256 个字符')
    try:
        ZoneInfo(tz)
    except (ValueError, KeyError):
        raise AuthError('无效的时区') from None
    return username, _hasher.hash(password)


def invite(days=7):
    if not 1 <= days <= 365:
        raise AuthError('有效期须为 1–365 天')
    token = secrets.token_urlsafe(32)
    with connect() as con:
        con.execute('INSERT INTO invites VALUES (?, ?, NULL)', (digest(token), time.time()+days*86400))
    log.info('invite_created')
    return token


def create_user(username, password, tz='Asia/Shanghai', invitation=None, *, admin=False):
    invitation = (invitation or "").strip()
    if not admin:
        with connect() as con:
            row = con.execute('SELECT * FROM invites WHERE digest=?', (digest(invitation or ''),)).fetchone()
            if not row or row['used_by'] is not None or row['expires'] <= time.time():
                raise AuthError('邀请码无效、已使用或已过期')
    username, hashed = credentials(username, password, tz)
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        if not admin:
            row = con.execute('SELECT * FROM invites WHERE digest=?', (digest(invitation or ''),)).fetchone()
            if not row or row['used_by'] is not None or row['expires'] <= time.time():
                raise AuthError('邀请码无效、已使用或已过期')
        if con.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
            raise AuthError('用户名不可用')
        uid = con.execute('INSERT INTO users (name, username, password_hash, tz, disabled, created_at) VALUES (?, ?, ?, ?, 0, CURRENT_TIMESTAMP)', (username, username, hashed, tz)).lastrowid
        if not admin:
            con.execute('UPDATE invites SET used_by=? WHERE digest=?', (uid, digest(invitation)))
    log.info('account_created user_id=%s', uid)
    return uid


def login(username, password):
    username = username.strip().lower()[:256]
    now = time.time()
    # Reserve an attempt BEFORE password hashing, durably and atomically.
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        for key, limit in [(digest(username), 5), ('global', 100)]:
            con.execute('INSERT INTO login_limits VALUES (?, 0, ?) ON CONFLICT(key) DO UPDATE SET attempts=0, reset_at=excluded.reset_at WHERE login_limits.reset_at<=?', (key, now+900, now))
            row = con.execute('SELECT attempts FROM login_limits WHERE key=?', (key,)).fetchone()
            if row[0] >= limit:
                raise AuthError('登录尝试过多，请在 15 分钟后重试')
        con.execute('UPDATE login_limits SET attempts=attempts+1 WHERE key IN (?, ?)', (digest(username), 'global'))
        row = con.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    valid = False
    try:
        valid = _hasher.verify(row['password_hash'] if row and row['password_hash'] else _dummy, password[:257])
    except VerificationError:
        pass
    if not valid or not row or row['disabled']:
        log.info('login_failed')
        raise AuthError('用户名或密码错误')
    token = secrets.token_urlsafe(32)
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        current = con.execute('SELECT * FROM users WHERE id=?', (row['id'],)).fetchone()
        if current['disabled'] or current['password_hash'] != row['password_hash']:
            raise AuthError('账号状态已改变，请重新登录')
        con.execute('DELETE FROM auth_sessions WHERE expires<=?', (now,))
        con.execute('INSERT INTO auth_sessions VALUES (?, ?, ?)', (digest(token), row['id'], now+43200))
        con.execute('DELETE FROM login_limits WHERE key=?', (digest(username),))
    log.info('login_succeeded user_id=%s', row['id'])
    return token


def authenticate(token):
    if not token:
        raise AuthError('请登录')
    with connect() as con:
        row = con.execute('SELECT u.* FROM users u JOIN auth_sessions s ON u.id=s.user_id WHERE s.digest=? AND s.expires>? AND u.disabled=0', (digest(token), time.time())).fetchone()
    if row is None:
        raise AuthError('登录已失效，请重新登录')
    return Principal(row['id'], row['username'], row['tz'])


def logout(token):
    with connect() as con:
        row = con.execute('SELECT user_id FROM auth_sessions WHERE digest=?', (digest(token or ''),)).fetchone()
        con.execute('DELETE FROM auth_sessions WHERE digest=?', (digest(token or ''),))
    if row:
        log.info('logged_out user_id=%s', row['user_id'])


def manage(username, *, password=None, disabled=None):
    hashed = credentials(username, password, 'Asia/Shanghai')[1] if password is not None else None
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute('SELECT id FROM users WHERE username=?', (username.lower(),)).fetchone()
        if not row:
            raise AuthError('账号不存在')
        if hashed:
            con.execute('UPDATE users SET password_hash=? WHERE id=?', (hashed, row['id']))
        if disabled is not None:
            con.execute('UPDATE users SET disabled=? WHERE id=?', (disabled, row['id']))
        con.execute('DELETE FROM auth_sessions WHERE user_id=?', (row['id'],))
    log.info('account_changed user_id=%s', row['id'])
