"""Local accounts, email/invitation registration and revocable sessions."""
from dataclasses import dataclass
from hashlib import sha256
import logging
import os
import re
import secrets
import sqlite3
import time
from zoneinfo import ZoneInfo
from argon2 import PasswordHasher, Type
from argon2.exceptions import VerificationError
from .storage import connect

log = logging.getLogger('goalcal.audit')
_hasher = PasswordHasher(type=Type.ID)
_dummy = _hasher.hash(secrets.token_urlsafe(32))
_EMAIL_RE = re.compile(r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+$")


def open_registration_enabled() -> bool:
    return os.getenv('GOALCAL_OPEN_REGISTRATION', '').strip().lower() in {'1', 'true', 'yes', 'on'}


def normalize_email(email: str | None) -> str | None:
    cleaned = (email or '').strip().lower()
    if not cleaned:
        return None
    if len(cleaned) > 254 or not _EMAIL_RE.fullmatch(cleaned):
        raise AuthError('邮箱格式无效')
    return cleaned


class AuthError(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    user_id: int
    username: str
    tz: str


def digest(value):
    return sha256(value.encode()).hexdigest()


MIN_PASSWORD_LEN = 6
DEFAULT_SESSION_DAYS = 30
SESSION_SECONDS = DEFAULT_SESSION_DAYS * 24 * 60 * 60
SESSION_RENEW_INTERVAL_SECONDS = 24 * 60 * 60
PASSWORD_RESET_TTL_SECONDS = 30 * 60
_RESET_LIMIT_WINDOW = 900
_RESET_EMAIL_LIMIT = 3
_RESET_GLOBAL_LIMIT = 30


def session_ttl_seconds() -> int:
    raw = os.getenv('GOALCAL_SESSION_DAYS', '').strip()
    if raw:
        try:
            days = int(raw)
        except ValueError:
            return SESSION_SECONDS
        return max(1, min(365, days)) * 86400
    return SESSION_SECONDS


def _hash_password(password: str) -> str:
    if not MIN_PASSWORD_LEN <= len(password) <= 256:
        raise AuthError(f'密码须为 {MIN_PASSWORD_LEN}–256 个字符')
    return _hasher.hash(password)


def credentials(username, password, tz):
    username = username.strip().lower()
    if not re.fullmatch(r'[a-z0-9_]{3,32}', username):
        raise AuthError('用户名须为 3–32 个英文字母、数字或下划线')
    hashed = _hash_password(password)
    try:
        ZoneInfo(tz)
    except (ValueError, KeyError):
        raise AuthError('无效的时区') from None
    return username, hashed


def invite(days=7, max_uses=1):
    if not 0 <= days <= 365:
        raise AuthError('有效期须为 0–365 天，0 表示永久有效')
    if not 0 <= max_uses <= 10000:
        raise AuthError('使用次数须为 0–10000 次，0 表示不限次数')
    token = secrets.token_urlsafe(32)
    expires = None if days == 0 else time.time() + days * 86400
    with connect() as con:
        con.execute('INSERT INTO invites (digest, expires, max_uses) VALUES (?, ?, ?)',
                    (digest(token), expires, max_uses))
    log.info('invite_created')
    return token


def _invite_valid(con, invitation):
    row = con.execute('SELECT * FROM invites WHERE digest=?', (digest(invitation or ''),)).fetchone()
    if not row or (row['expires'] is not None and row['expires'] <= time.time()):
        return False
    if row['max_uses']:
        used = con.execute('SELECT COUNT(*) FROM invite_uses WHERE digest=?', (row['digest'],)).fetchone()[0]
        if used >= row['max_uses']:
            return False
    return True


def create_user(
    username,
    password,
    tz='Asia/Shanghai',
    invitation=None,
    *,
    admin=False,
    email=None,
    require_invite=None,
    require_email=False,
):
    invitation = (invitation or "").strip()
    if require_invite is None:
        require_invite = (not admin) and (not open_registration_enabled())
    norm_email = normalize_email(email)
    if (require_email or (not admin and open_registration_enabled())) and not norm_email:
        raise AuthError('请填写有效的邮箱地址')
    if require_invite:
        with connect() as con:
            if not _invite_valid(con, invitation):
                raise AuthError('邀请码无效、已使用或已过期')
    elif not admin:
        now = time.time()
        with connect() as con:
            con.execute('BEGIN IMMEDIATE')
            con.execute(
                'INSERT INTO login_limits VALUES (?, 0, ?) '
                'ON CONFLICT(key) DO UPDATE SET attempts=0, reset_at=excluded.reset_at WHERE login_limits.reset_at<=?',
                ('register_global', now + 900, now),
            )
            row = con.execute('SELECT attempts FROM login_limits WHERE key=?', ('register_global',)).fetchone()
            if row[0] >= 30:
                raise AuthError('注册尝试过多，请稍后再试')
            con.execute('UPDATE login_limits SET attempts=attempts+1 WHERE key=?', ('register_global',))
    username, hashed = credentials(username, password, tz)
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        if require_invite:
            if not _invite_valid(con, invitation):
                raise AuthError('邀请码无效、已使用或已过期')
        if con.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
            raise AuthError('用户名不可用')
        if norm_email and con.execute('SELECT 1 FROM users WHERE email=?', (norm_email,)).fetchone():
            raise AuthError('该邮箱已被注册')
        uid = con.execute(
            'INSERT INTO users (name, username, email, password_hash, tz, disabled, created_at) VALUES (?, ?, ?, ?, ?, 0, CURRENT_TIMESTAMP)',
            (username, username, norm_email, hashed, tz),
        ).lastrowid
        if require_invite:
            dig = digest(invitation)
            con.execute('INSERT INTO invite_uses (digest, user_id) VALUES (?, ?)', (dig, uid))
            con.execute('UPDATE invites SET used_by=? WHERE digest=?', (uid, dig))
    log.info('account_created user_id=%s', uid)
    return uid


def login(username, password):
    identifier = username.strip().lower()[:256]
    now = time.time()
    # Reserve an attempt BEFORE password hashing, durably and atomically.
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        for key, limit in [(digest(identifier), 5), ('global', 100)]:
            con.execute('INSERT INTO login_limits VALUES (?, 0, ?) ON CONFLICT(key) DO UPDATE SET attempts=0, reset_at=excluded.reset_at WHERE login_limits.reset_at<=?', (key, now+900, now))
            row = con.execute('SELECT attempts FROM login_limits WHERE key=?', (key,)).fetchone()
            if row[0] >= limit:
                raise AuthError('登录尝试过多，请在 15 分钟后重试')
        con.execute('UPDATE login_limits SET attempts=attempts+1 WHERE key IN (?, ?)', (digest(identifier), 'global'))
        col = 'email' if '@' in identifier else 'username'
        row = con.execute(f'SELECT * FROM users WHERE {col}=?', (identifier,)).fetchone()
    valid = False
    try:
        valid = _hasher.verify(row['password_hash'] if row and row['password_hash'] else _dummy, password[:257])
    except VerificationError:
        pass
    if not valid or not row or row['disabled']:
        log.info('login_failed')
        raise AuthError('用户名或密码错误')
    token = secrets.token_urlsafe(32)
    ttl = session_ttl_seconds()
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        current = con.execute('SELECT * FROM users WHERE id=?', (row['id'],)).fetchone()
        if current['disabled'] or current['password_hash'] != row['password_hash']:
            raise AuthError('账号状态已改变，请重新登录')
        con.execute('DELETE FROM auth_sessions WHERE expires<=?', (now,))
        con.execute('INSERT INTO auth_sessions VALUES (?, ?, ?)', (digest(token), row['id'], now + ttl))
        con.execute('DELETE FROM login_limits WHERE key=?', (digest(identifier),))
    log.info('login_succeeded user_id=%s', row['id'])
    return token


def authenticate_session(token) -> tuple[Principal, float]:
    """Read-only session check returning the principal and session expiry timestamp."""
    if not token:
        raise AuthError('请登录')
    with connect() as con:
        row = con.execute(
            'SELECT u.*, s.expires AS session_expires FROM users u '
            'JOIN auth_sessions s ON u.id=s.user_id '
            'WHERE s.digest=? AND s.expires>? AND u.disabled=0',
            (digest(token), time.time()),
        ).fetchone()
    if row is None:
        raise AuthError('登录已失效，请重新登录')
    return Principal(row['id'], row['username'], row['tz']), float(row['session_expires'])


def authenticate(token):
    return authenticate_session(token)[0]


def should_renew_session(expires: float, *, now: float | None = None, ttl: int | None = None) -> bool:
    current = time.time() if now is None else now
    effective_ttl = session_ttl_seconds() if ttl is None else ttl
    interval = min(SESSION_RENEW_INTERVAL_SECONDS, effective_ttl / 2)
    return expires > current and (expires - current) <= (effective_ttl - interval)


def renew_session_if_needed(
    token: str | None, *, expires: float | None = None, now: float | None = None
) -> int | None:
    """Extend active session expiry on a sliding window; returns new TTL if renewed."""
    if not token:
        return None
    current = time.time() if now is None else now
    ttl = session_ttl_seconds()
    if expires is not None and not should_renew_session(expires, now=current, ttl=ttl):
        return None
    interval = min(SESSION_RENEW_INTERVAL_SECONDS, ttl / 2)
    new_expires = current + ttl
    cutoff = current + ttl - interval
    try:
        with connect() as con:
            con.execute('BEGIN IMMEDIATE')
            cur = con.execute(
                'UPDATE auth_sessions SET expires=? '
                'WHERE digest=? AND expires>? AND expires<=? '
                'AND user_id IN (SELECT id FROM users WHERE disabled=0)',
                (new_expires, digest(token), current, cutoff),
            )
            return ttl if cur.rowcount > 0 else None
    except sqlite3.OperationalError:
        log.warning('session_renew_skipped_lock', exc_info=True)
        return None


def logout(token):
    with connect() as con:
        row = con.execute('SELECT user_id FROM auth_sessions WHERE digest=?', (digest(token or ''),)).fetchone()
        con.execute('DELETE FROM auth_sessions WHERE digest=?', (digest(token or ''),))
    if row:
        log.info('logged_out user_id=%s', row['user_id'])


def update_timezone(user_id: int, tz: str) -> str:
    cleaned = (tz or '').strip()
    if not cleaned or len(cleaned) > 64:
        raise AuthError('无效的时区')
    try:
        ZoneInfo(cleaned)
    except (ValueError, KeyError):
        raise AuthError('无效的时区') from None
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        cur = con.execute('UPDATE users SET tz=? WHERE id=? AND disabled=0', (cleaned, user_id))
        if cur.rowcount == 0:
            raise AuthError('账号不存在或已停用')
    log.info('timezone_updated user_id=%s tz=%s', user_id, cleaned)
    return cleaned


def manage(username, *, password=None, disabled=None):
    hashed = _hash_password(password) if password is not None else None
    identifier = username.strip().lower()
    col = 'email' if '@' in identifier else 'username'
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute(f'SELECT id FROM users WHERE {col}=?', (identifier,)).fetchone()
        if not row:
            raise AuthError('账号不存在')
        if hashed:
            con.execute('UPDATE users SET password_hash=? WHERE id=?', (hashed, row['id']))
        if disabled is not None:
            con.execute('UPDATE users SET disabled=? WHERE id=?', (disabled, row['id']))
        con.execute('DELETE FROM auth_sessions WHERE user_id=?', (row['id'],))
        con.execute('DELETE FROM password_reset_tokens WHERE user_id=?', (row['id'],))
    log.info('account_changed user_id=%s', row['id'])


def request_password_reset(email: str) -> tuple[str, str, str] | None:
    """Create a one-time reset token for an active email account, or return None without leaking existence."""
    norm_email = normalize_email(email)
    if not norm_email:
        raise AuthError('请填写有效的邮箱地址')
    now = time.time()
    email_limit_key = f'pwreset:{digest(norm_email)}'
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        for key, limit in [(email_limit_key, _RESET_EMAIL_LIMIT), ('pwreset_global', _RESET_GLOBAL_LIMIT)]:
            con.execute(
                'INSERT INTO login_limits VALUES (?, 0, ?) '
                'ON CONFLICT(key) DO UPDATE SET attempts=0, reset_at=excluded.reset_at WHERE login_limits.reset_at<=?',
                (key, now + _RESET_LIMIT_WINDOW, now),
            )
            row = con.execute('SELECT attempts FROM login_limits WHERE key=?', (key,)).fetchone()
            if row[0] >= limit:
                raise AuthError('密码重置请求过于频繁，请 15 分钟后再试')
        con.execute('UPDATE login_limits SET attempts=attempts+1 WHERE key IN (?, ?)', (email_limit_key, 'pwreset_global'))
        con.execute('DELETE FROM password_reset_tokens WHERE expires<=?', (now,))
        user_row = con.execute(
            'SELECT id, username, email, disabled FROM users WHERE email=?',
            (norm_email,),
        ).fetchone()
        if not user_row or user_row['disabled'] or not user_row['email']:
            return None
        con.execute('DELETE FROM password_reset_tokens WHERE user_id=? AND used_at IS NULL', (user_row['id'],))
        token = secrets.token_urlsafe(32)
        con.execute(
            'INSERT INTO password_reset_tokens (digest, user_id, expires, used_at) VALUES (?, ?, ?, NULL)',
            (digest(token), user_row['id'], now + PASSWORD_RESET_TTL_SECONDS),
        )
    log.info('password_reset_requested user_id=%s', user_row['id'])
    return token, user_row['email'], user_row['username'] or user_row['email']


def verify_password_reset_token(token: str) -> bool:
    """Read-only check confirming whether a reset token is unused, unexpired, and belongs to an active user."""
    cleaned = (token or '').strip()
    if not cleaned:
        return False
    with connect() as con:
        row = con.execute(
            'SELECT 1 FROM password_reset_tokens t '
            'JOIN users u ON u.id=t.user_id '
            'WHERE t.digest=? AND t.used_at IS NULL AND t.expires>? AND u.disabled=0',
            (digest(cleaned), time.time()),
        ).fetchone()
    return row is not None


def complete_password_reset(token: str, new_password: str) -> int:
    """Validate token and password, update password hash, burn tokens, and revoke existing sessions."""
    hashed = _hash_password(new_password)
    cleaned = (token or '').strip()
    if not cleaned:
        raise AuthError('重置链接无效或已过期，请重新申请')
    now = time.time()
    token_digest = digest(cleaned)
    with connect() as con:
        con.execute('BEGIN IMMEDIATE')
        row = con.execute(
            'SELECT t.user_id, t.expires, t.used_at, u.username, u.email, u.disabled '
            'FROM password_reset_tokens t JOIN users u ON u.id=t.user_id WHERE t.digest=?',
            (token_digest,),
        ).fetchone()
        if not row or row['used_at'] is not None or row['expires'] <= now or row['disabled']:
            raise AuthError('重置链接无效或已过期，请重新申请')
        uid = int(row['user_id'])
        con.execute('UPDATE users SET password_hash=? WHERE id=?', (hashed, uid))
        con.execute('UPDATE password_reset_tokens SET used_at=? WHERE digest=?', (now, token_digest))
        con.execute('DELETE FROM password_reset_tokens WHERE user_id=? AND used_at IS NULL', (uid,))
        con.execute('DELETE FROM auth_sessions WHERE user_id=?', (uid,))
        if row['username']:
            con.execute('DELETE FROM login_limits WHERE key=?', (digest(row['username'].strip().lower()),))
        if row['email']:
            con.execute('DELETE FROM login_limits WHERE key=?', (digest(row['email'].strip().lower()),))
    log.info('password_reset_completed user_id=%s', uid)
    return uid
