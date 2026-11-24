"""Symmetric credential encryption and signed OAuth state tokens."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import get_settings, settings
from .storage import database_path

_STATE_SALT = "goalcal-oauth-state-v1"


class CredentialError(ValueError):
    pass


def _production_mode() -> bool:
    raw = os.getenv("GOALCAL_PRODUCTION")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return settings.production


def _coerce_fernet_key(raw: str) -> bytes:
    cleaned = raw.strip()
    if len(cleaned) == 44:
        try:
            Fernet(cleaned.encode("ascii"))
            return cleaned.encode("ascii")
        except Exception:
            pass
    digest = hashlib.sha256(cleaned.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _load_or_create_key_file(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            return _coerce_fernet_key(raw)
    generated = Fernet.generate_key()
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raw = path.read_text(encoding="utf-8").strip()
        return _coerce_fernet_key(raw)
    try:
        os.write(fd, generated + b"\n")
    finally:
        os.close(fd)
    return generated


def _fernet_key() -> bytes:
    env_key = os.getenv("GOALCAL_CREDENTIAL_KEY", "").strip()
    if env_key:
        return _coerce_fernet_key(env_key)
    if _production_mode():
        db_file = database_path(get_settings().db_url)
        return _load_or_create_key_file(db_file.parent / ".credential_key")
    db_url = os.getenv("GOALCAL_DB_URL", settings.db_url)
    digest = hashlib.sha256(f"dev-credential-key:{db_url}".encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def _fernet() -> Fernet:
    return Fernet(_fernet_key())


def _state_serializer() -> URLSafeTimedSerializer:
    signing_key = hashlib.sha256(b"oauth-state:" + _fernet_key()).digest()
    return URLSafeTimedSerializer(signing_key, salt=_STATE_SALT)


def encrypt_secrets(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _fernet().encrypt(payload).decode("ascii")


def decrypt_secrets(token: str) -> dict:
    if not token:
        return {}
    try:
        raw = _fernet().decrypt(token.encode("ascii"))
        parsed = json.loads(raw.decode("utf-8"))
    except (InvalidToken, ValueError, TypeError) as exc:
        raise CredentialError("Stored calendar credentials could not be decrypted") from exc
    if not isinstance(parsed, dict):
        raise CredentialError("Invalid decrypted credential payload")
    return parsed


def sign_oauth_state(payload: dict) -> str:
    return _state_serializer().dumps(payload)


def verify_oauth_state(token: str, max_age: int = 600) -> dict:
    if not token:
        raise CredentialError("Missing OAuth state token")
    try:
        data = _state_serializer().loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired) as exc:
        raise CredentialError("Invalid or expired OAuth state token") from exc
    if not isinstance(data, dict):
        raise CredentialError("Invalid OAuth state payload")
    return data
