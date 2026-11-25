"""Lightweight SMTP mailer using stdlib smtplib/ssl."""
from __future__ import annotations

from email.message import EmailMessage
import os
import smtplib
import ssl


class MailerError(RuntimeError):
    """Raised when an SMTP delivery attempt fails."""


class MailerNotConfiguredError(MailerError):
    """Raised when SMTP settings are incomplete."""


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _smtp_config() -> dict:
    host = os.getenv("SMTP_HOST", "").strip()
    port_raw = os.getenv("SMTP_PORT", "587").strip() or "587"
    try:
        port = int(port_raw)
    except ValueError:
        port = 587
    user = os.getenv("SMTP_USER", "").strip()
    password = os.getenv("SMTP_PASSWORD", "")
    from_addr = os.getenv("SMTP_FROM", "").strip() or user
    use_ssl = _bool_env("SMTP_USE_SSL", port == 465)
    use_tls = _bool_env("SMTP_USE_TLS", not use_ssl)
    timeout_raw = os.getenv("SMTP_TIMEOUT", "15").strip() or "15"
    try:
        timeout = max(3.0, min(60.0, float(timeout_raw)))
    except ValueError:
        timeout = 15.0
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "from_addr": from_addr,
        "use_ssl": use_ssl,
        "use_tls": use_tls,
        "timeout": timeout,
    }


def smtp_capabilities() -> dict:
    cfg = _smtp_config()
    configured = bool(cfg["host"] and cfg["from_addr"])
    return {
        "configured": configured,
        "from_address": cfg["from_addr"] if configured else "",
    }


def send_email(
    *,
    to_address: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
) -> None:
    recipient = (to_address or "").strip()
    if not recipient:
        raise MailerError("Recipient email address is required")
    cfg = _smtp_config()
    if not (cfg["host"] and cfg["from_addr"]):
        raise MailerNotConfiguredError("SMTP is not configured on this server")

    msg = EmailMessage()
    msg["From"] = cfg["from_addr"]
    msg["To"] = recipient
    msg["Subject"] = (subject or "GoalCal").strip()[:240]
    msg.set_content(text_body or "")
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    ctx = ssl.create_default_context()
    try:
        if cfg["use_ssl"]:
            with smtplib.SMTP_SSL(cfg["host"], cfg["port"], timeout=cfg["timeout"], context=ctx) as client:
                if cfg["user"]:
                    client.login(cfg["user"], cfg["password"])
                client.send_message(msg)
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=cfg["timeout"]) as client:
                client.ehlo()
                if cfg["use_tls"]:
                    client.starttls(context=ctx)
                    client.ehlo()
                if cfg["user"]:
                    client.login(cfg["user"], cfg["password"])
                client.send_message(msg)
    except MailerError:
        raise
    except (smtplib.SMTPException, OSError) as exc:
        raise MailerError(f"SMTP delivery failed ({type(exc).__name__}): {exc}") from None


__all__ = [
    "MailerError",
    "MailerNotConfiguredError",
    "send_email",
    "smtp_capabilities",
]
