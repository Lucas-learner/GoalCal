"""Same-origin authentication, React frontend and Web API gateway.

The gateway owns the HttpOnly browser cookie and authenticates the Web API.
Session tokens are never sent into page JavaScript or a URL. Streamlit proxying
is disabled unless explicitly enabled for local compatibility regression.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import html
import json
import logging
import os
import re
import secrets
from urllib.parse import quote, urlsplit
import zoneinfo

import httpx2
import websockets
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse, Response, JSONResponse
from starlette.routing import Route, WebSocketRoute, Mount
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import auth
from .i18n import normalize_lang, parse_accept_language

COOKIE_NAME = "goalcal_session"
FORM_COOKIE_NAME = "goalcal_form"
LANG_COOKIE_NAME = "goalcal_lang"
THEME_COOKIE_NAME = "goalcal_theme"
SESSION_SECONDS = auth.SESSION_SECONDS
FORM_COOKIE_SECONDS = 7 * 24 * 60 * 60
_FORM_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")
UPSTREAM_PORT = int(os.getenv("GOALCAL_UPSTREAM_PORT", "8518"))
UPSTREAM_HTTP = f"http://127.0.0.1:{UPSTREAM_PORT}"
UPSTREAM_WS = f"ws://127.0.0.1:{UPSTREAM_PORT}"

_HOP_HEADERS = {
    "connection", "content-encoding", "content-length", "date", "host",
    "keep-alive", "proxy-authenticate", "proxy-authorization", "server",
    "te", "trailer", "transfer-encoding", "upgrade",
}

_TIMEZONES = sorted(zoneinfo.available_timezones())
_TZ_OPTIONS_HTML = "".join(
    f'<option value="{html.escape(tz)}"'
    f'{" selected" if tz == "Asia/Shanghai" else ""}>'
    f'{html.escape(tz)}</option>'
    for tz in _TIMEZONES
)

_DETECT_TZ_SCRIPT = """<script nonce="{nonce}">
(function() {{
  try {{
    var tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (!tz) return;
    var sel = document.querySelector('select[name="tz"]');
    if (!sel) return;
    for (var i = 0; i < sel.options.length; i++) {{
      var opt = sel.options[i];
      if (opt.value === tz) {{
        if (i !== 0) {{
          sel.insertBefore(opt, sel.firstChild);
        }}
        sel.value = tz;
        break;
      }}
    }}
  }} catch (e) {{}}
}})();
</script>"""


def english_auth_error(message: str) -> str:
    return {
        "用户名须为 3–32 个英文字母、数字或下划线": "Username must be 3–32 letters, numbers, or underscores.",
        "密码须为 6–256 个字符": "Password must be 6–256 characters.",
        "密码须为 12–256 个字符": "Password must be 6–256 characters.",
        "邮箱格式无效": "Please enter a valid email address.",
        "请填写有效的邮箱地址": "Please enter a valid email address.",
        "无效的时区": "Invalid time zone.",
        "邀请码无效、已使用或已过期": "Invitation code is invalid, used, or expired.",
        "用户名不可用": "Username is unavailable.",
        "该邮箱已被注册": "This email is already registered.",
        "注册尝试过多，请稍后再试": "Too many registration attempts. Please try again later.",
        "登录尝试过多，请在 15 分钟后重试": "Too many login attempts. Try again in 15 minutes.",
        "用户名或密码错误": "Incorrect username or password.",
        "密码重置请求过于频繁，请 15 分钟后再试": "Too many password reset requests. Try again in 15 minutes.",
        "重置链接无效或已过期，请重新申请": "This reset link is invalid or has expired. Please request a new one.",
        "两次输入的密码不一致": "Passwords do not match.",
    }.get(message, message)


def localize_auth_error(message: str, lang: str) -> str:
    return message if lang == "zh" else english_auth_error(message)


def _request_lang(request: Request) -> str:
    cookie_lang = normalize_lang(request.cookies.get(LANG_COOKIE_NAME))
    if cookie_lang:
        return cookie_lang
    return parse_accept_language(request.headers.get("accept-language"), default="en")


def _request_theme(request: Request) -> str:
    raw = (request.cookies.get(THEME_COOKIE_NAME) or "").strip().lower()
    return raw if raw in {"light", "dark"} else ""


_AUTH_COPY = {
    "zh": {
        "html_lang": "zh-CN",
        "subtitle": "让计划适应你的生活节奏",
        "nav_label": "登录与注册",
        "tab_login": "登录",
        "tab_register": "注册",
        "username": "用户名",
        "username_or_email": "用户名或邮箱",
        "email": "邮箱",
        "invitation": "邀请码",
        "password": "密码",
        "password_new": "密码（至少 6 位）",
        "timezone": "时区",
        "btn_login": "登录",
        "btn_register": "注册",
        "created": "账号已创建，请登录。",
        "form_expired": "表单会话已过期，请再试一次。",
        "switch_label": "语言",
        "forgot_link": "忘记密码？",
        "forgot_title": "找回密码",
        "forgot_desc": "输入绑定账号的邮箱地址，我们会发送一次性重置链接（30 分钟内有效）。",
        "forgot_btn": "发送重置邮件",
        "forgot_sent": "如果该邮箱已绑定 GoalCal 账号，重置密码邮件已发送（30 分钟内有效），请查收收件箱及垃圾邮件箱。",
        "forgot_smtp_missing": "当前服务端尚未配置邮件发送（SMTP），请联系管理员协助重置密码。",
        "forgot_send_failed": "邮件发送暂时失败，请稍后重试。",
        "back_to_login": "返回登录",
        "reset_title": "设置新密码",
        "reset_desc": "请为你的 GoalCal 账号设置新密码（至少 6 个字符）。重置完成后原有登录会话将自动退出。",
        "reset_new_pw": "新密码（至少 6 位）",
        "reset_confirm_pw": "确认新密码",
        "reset_btn": "确认重置密码",
        "reset_invalid": "重置链接无效或已过期，请重新申请。",
        "reset_request_again": "重新申请重置链接",
        "password_reset_done": "密码已重置，请使用新密码登录。",
    },
    "en": {
        "html_lang": "en",
        "subtitle": "Plans that adapt to the rhythm of your life",
        "nav_label": "Authentication",
        "tab_login": "Log in",
        "tab_register": "Create account",
        "username": "Username",
        "username_or_email": "Username or email",
        "email": "Email",
        "invitation": "Invitation code",
        "password": "Password",
        "password_new": "Password (at least 6 characters)",
        "timezone": "Time zone",
        "btn_login": "Log in",
        "btn_register": "Register",
        "created": "Account created. Please log in.",
        "form_expired": "Form session expired. Please try again.",
        "switch_label": "Language",
        "forgot_link": "Forgot password?",
        "forgot_title": "Reset your password",
        "forgot_desc": "Enter the email linked to your account and we will send a one-time reset link (valid for 30 minutes).",
        "forgot_btn": "Send reset email",
        "forgot_sent": "If an account is linked to that email, a password reset link has been sent (valid for 30 minutes). Please check your inbox and spam folder.",
        "forgot_smtp_missing": "Outbound email (SMTP) is not configured on this server yet. Please contact the administrator to reset your password.",
        "forgot_send_failed": "Unable to send email right now. Please try again later.",
        "back_to_login": "Back to log in",
        "reset_title": "Set a new password",
        "reset_desc": "Choose a new password for your GoalCal account (at least 6 characters). Existing sessions will be signed out.",
        "reset_new_pw": "New password (at least 6 characters)",
        "reset_confirm_pw": "Confirm new password",
        "reset_btn": "Update password",
        "reset_invalid": "This reset link is invalid or has expired. Please request a new one.",
        "reset_request_again": "Request a new link",
        "password_reset_done": "Your password has been updated. Please log in with your new password.",
    },
}


_BRAND_MARK_SVG = (
    '<svg viewBox="14 14 92 92" fill="currentColor" focusable="false">'
    '<path d="M 99.05 42 A 43 43 0 0 0 60 17 A 43 43 0 0 0 17 60 A 43 43 0 0 0 19.70 75 H 41.27 A 24 24 0 0 1 36 60 A 24 24 0 0 1 60 36 A 24 24 0 0 1 75.87 42 Z"/>'
    '<path d="M 58 55 H 102.71 A 43 43 0 0 1 100.30 75 H 78.73 A 24 24 0 0 0 82.96 67 H 58 Z"/>'
    '<path d="M 21.83 79.8 A 43 43 0 0 0 27.37 88 H 92.63 A 43 43 0 0 0 98.17 79.8 H 73.56 A 24 24 0 0 1 46.44 79.8 Z"/>'
    '<path d="M 33.17 93.6 A 43 43 0 0 0 86.83 93.6 Z"/>'
    "</svg>"
)

_PUBLIC_BRAND_ICONS = {
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}


def _auth_shell_response(
    request: Request,
    *,
    lang: str,
    card_html: str,
    current_page: str,
    form_token: str,
    script: str = "",
    script_nonce: str = "",
    status_code: int = 200,
) -> HTMLResponse:
    theme = _request_theme(request)
    theme_attr = f' data-theme="{theme}"' if theme else ""
    copy = _AUTH_COPY[lang]
    zh_href = f"/_goalcal/lang?lang=zh&next={quote(current_page, safe='/')}"
    en_href = f"/_goalcal/lang?lang=en&next={quote(current_page, safe='/')}"
    zh_cls = ' class="active"' if lang == "zh" else ""
    en_cls = ' class="active"' if lang == "en" else ""
    brand_html = f'<span class="brand-mark" aria-hidden="true">{_BRAND_MARK_SVG}</span><span>GoalCal</span>'
    body = f"""<!doctype html><html lang="{copy["html_lang"]}"{theme_attr}><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<link rel="icon" href="/favicon.ico" sizes="any">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<title>GoalCal</title><style>
:root{{color-scheme:light;--bg:#fafaf8;--surface:#fffefd;--surface-soft:#f4f4f1;--ink:#1d1d1f;--muted:#6e6e73;--faint:#a1a1a6;--line:rgba(29,29,31,0.08);--line-strong:rgba(29,29,31,0.16);--accent:#303638;--accent-hover:#23282a;--accent-ink:#fff;--blue:#6b7f89;--tab-shadow:0 1px 2px rgba(20,22,24,0.06);--notice-bg:#fdf3e7;--notice-border:rgba(180,120,40,0.2);--notice-ink:#5c3d15;--info-bg:#f2f5f7;--info-border:rgba(107,127,137,0.24);--info-ink:#32444d;--radius:13px;--control:9px;--shadow:0 1px 2px rgba(20,22,24,0.02),0 8px 24px rgba(20,22,24,0.025)}}
:root[data-theme="dark"]{{color-scheme:dark;--bg:#131415;--surface:#1b1c1e;--surface-soft:#242629;--ink:#f0f0ee;--muted:#a2a3a8;--faint:#73757a;--line:rgba(255,255,255,0.09);--line-strong:rgba(255,255,255,0.18);--accent:#e5e7e6;--accent-hover:#d4d7d6;--accent-ink:#161819;--blue:#8aa2ad;--tab-shadow:0 1px 2px rgba(0,0,0,0.35);--notice-bg:rgba(218,162,86,0.12);--notice-border:rgba(218,162,86,0.28);--notice-ink:#f2cd98;--info-bg:rgba(138,162,173,0.12);--info-border:rgba(138,162,173,0.28);--info-ink:#d0dce2;--shadow:0 1px 2px rgba(0,0,0,0.28),0 8px 24px rgba(0,0,0,0.34)}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{color-scheme:dark;--bg:#131415;--surface:#1b1c1e;--surface-soft:#242629;--ink:#f0f0ee;--muted:#a2a3a8;--faint:#73757a;--line:rgba(255,255,255,0.09);--line-strong:rgba(255,255,255,0.18);--accent:#e5e7e6;--accent-hover:#d4d7d6;--accent-ink:#161819;--blue:#8aa2ad;--tab-shadow:0 1px 2px rgba(0,0,0,0.35);--notice-bg:rgba(218,162,86,0.12);--notice-border:rgba(218,162,86,0.28);--notice-ink:#f2cd98;--info-bg:rgba(138,162,173,0.12);--info-border:rgba(138,162,173,0.28);--info-ink:#d0dce2;--shadow:0 1px 2px rgba(0,0,0,0.28),0 8px 24px rgba(0,0,0,0.34)}}}}
*{{box-sizing:border-box}}
body{{font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Display","PingFang SC","Helvetica Neue",Arial,sans-serif;background:var(--bg);color:var(--ink);margin:0;-webkit-font-smoothing:antialiased}}
main{{min-height:100dvh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:32px 18px}}
.shell{{width:100%;max-width:416px}}
.brand-wrap{{display:flex;flex-direction:column;align-items:center;gap:8px;margin:0 0 24px;text-align:center}}
.brand{{display:inline-flex;align-items:center;gap:10px;font-size:20px;font-weight:720;letter-spacing:-0.04em;margin:0;color:var(--ink)}}
.brand-mark{{width:26px;height:26px;display:inline-flex;align-items:center;justify-content:center;color:var(--ink);flex:0 0 auto}}
.brand-mark svg{{width:100%;height:100%;display:block}}
.subtitle{{margin:0;font-size:12.5px;color:var(--muted)}}
.card{{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:24px}}
nav{{display:grid;grid-template-columns:1fr 1fr;gap:4px;padding:4px;background:var(--surface-soft);border-radius:11px;margin-bottom:20px}}
nav a{{display:flex;align-items:center;justify-content:center;min-height:34px;padding:0 12px;border-radius:8px;color:var(--muted);text-decoration:none;font-size:13px;font-weight:500;transition:background-color .15s,color .15s}}
nav a:hover{{color:var(--ink)}}
nav a[aria-current="page"]{{background:var(--surface);color:var(--ink);font-weight:620;box-shadow:var(--tab-shadow)}}
.card-title{{margin:0 0 8px;font-size:16px;font-weight:660;color:var(--ink)}}
.card-desc{{margin:0 0 18px;font-size:12.5px;line-height:1.55;color:var(--muted)}}
label{{display:block;margin:0 0 15px;font-size:12px;font-weight:600;color:var(--ink)}}
input,select{{display:block;width:100%;min-height:40px;margin-top:6px;padding:8px 12px;border:1px solid var(--line-strong);border-radius:var(--control);background:var(--surface);font:400 14px/1.4 inherit;color:var(--ink)}}
input:focus,select:focus,button:focus-visible,nav a:focus-visible,.lang-switch a:focus-visible,.auth-link:focus-visible{{outline:2px solid var(--blue);outline-offset:2px}}
button{{display:inline-flex;align-items:center;justify-content:center;width:100%;height:40px;margin-top:6px;padding:0 16px;border:1px solid var(--accent);border-radius:var(--control);background:var(--accent);color:var(--accent-ink);font:600 13px/1 inherit;cursor:pointer;transition:background-color .15s}}
button:hover{{background:var(--accent-hover)}}
.btn-secondary{{display:inline-flex;align-items:center;justify-content:center;width:100%;height:40px;margin-top:6px;padding:0 16px;border:1px solid var(--line-strong);border-radius:var(--control);background:var(--surface-soft);color:var(--ink);font:600 13px/1 inherit;text-decoration:none;cursor:pointer}}
.btn-secondary:hover{{background:var(--line)}}
.notice{{padding:10px 12px;background:var(--notice-bg);border:1px solid var(--notice-border);border-radius:var(--control);color:var(--notice-ink);font-size:12.5px;margin:0 0 16px}}
.notice.info{{background:var(--info-bg);border-color:var(--info-border);color:var(--info-ink)}}
.auth-links{{display:flex;justify-content:center;margin-top:14px;font-size:12px}}
.auth-link{{color:var(--muted);text-decoration:none;padding:2px 6px;border-radius:6px}}
.auth-link:hover{{color:var(--ink);text-decoration:underline}}
.lang-switch{{display:flex;justify-content:center;align-items:center;gap:6px;margin-top:16px;font-size:11.5px;color:var(--faint)}}
.lang-switch a{{color:var(--muted);text-decoration:none;padding:2px 7px;border-radius:6px}}
.lang-switch a.active{{color:var(--ink);font-weight:620;background:var(--surface-soft)}}
</style></head><body><main><div class="shell">
<div class="brand-wrap"><h1 class="brand">{brand_html}</h1><p class="subtitle">{copy["subtitle"]}</p></div>
<div class="card">{card_html}</div>
<div class="lang-switch" aria-label="{copy["switch_label"]}"><a href="{zh_href}"{zh_cls}>中文</a><span>/</span><a href="{en_href}"{en_cls}>EN</a></div>
</div></main>{script}</body></html>"""
    response = HTMLResponse(body, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    effective_nonce = script_nonce or secrets.token_urlsafe(32)
    response.headers["Content-Security-Policy"] = (
        f"default-src 'none'; script-src 'nonce-{effective_nonce}'; style-src 'unsafe-inline'; img-src 'self'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    # Chrome sends a null Origin for same-origin form posts under no-referrer.
    # Keep same-origin form provenance without sending referrers to other sites.
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.set_cookie(
        FORM_COOKIE_NAME, form_token, max_age=FORM_COOKIE_SECONDS, path="/",
        secure=_secure_cookie(request), httponly=True, samesite="lax",
    )
    return response


def _form_token_for(request: Request) -> str:
    existing_form_token = request.cookies.get(FORM_COOKIE_NAME, "")
    return (
        existing_form_token
        if _FORM_TOKEN_RE.fullmatch(existing_form_token)
        else secrets.token_urlsafe(32)
    )


def _login_page(
    request: Request,
    message: str = "",
    *,
    register: bool = False,
    app_login: bool = False,
    next_target: str | None = None,
    status_code: int = 200,
    info_notice: bool = False,
) -> HTMLResponse:
    lang = _request_lang(request)
    copy = _AUTH_COPY[lang]
    form_token = _form_token_for(request)
    script_nonce = secrets.token_urlsafe(32)
    notice_cls = "notice info" if info_notice else "notice"
    notice = f'<p class="{notice_cls}" role="alert">{html.escape(message)}</p>' if message else ""
    open_reg = auth.open_registration_enabled()
    target = next_target if next_target is not None else ("/app/" if app_login else None)
    return_field = f'<input type="hidden" name="next" value="{html.escape(target, quote=True)}">' if target else ""
    encoded_target = quote(target, safe="/") if target else ""
    login_href = f"/_goalcal/login?next={encoded_target}" if target else "/"
    register_href = f"/_goalcal/register?next={encoded_target}" if target else "/_goalcal/register"
    forgot_href = f"/_goalcal/forgot-password?next={encoded_target}" if target else "/_goalcal/forgot-password"
    if register:
        extra = (
            f'<label>{copy["email"]}<input name="email" type="email" autocomplete="email" required></label>'
            if open_reg
            else f'<label>{copy["invitation"]}<input name="invitation" required></label>'
        )
        fields = f"""<label>{copy["username"]}<input name="username" autocomplete="username" required></label>
{extra}
<label>{copy["password_new"]}<input name="password" type="password" autocomplete="new-password" required></label>
<label>{copy["timezone"]}<select name="tz" required>{_TZ_OPTIONS_HTML}</select></label>"""
        action, button = "/_goalcal/register", copy["btn_register"]
        login_current, register_current = "", ' aria-current="page"'
        script = _DETECT_TZ_SCRIPT.format(nonce=script_nonce)
        footer_html = ""
    else:
        user_label = copy["username_or_email"] if open_reg else copy["username"]
        fields = f"""<label>{user_label}<input name="username" autocomplete="username" required></label>
<label>{copy["password"]}<input name="password" type="password" autocomplete="current-password" required></label>"""
        action, button = "/_goalcal/login", copy["btn_login"]
        login_current, register_current = ' aria-current="page"', ""
        script = ""
        footer_html = f'<div class="auth-links"><a class="auth-link" href="{forgot_href}">{copy["forgot_link"]}</a></div>'
    current_page = register_href if register else login_href
    card_html = (
        f'<nav aria-label="{copy["nav_label"]}"><a href="{login_href}"{login_current}>{copy["tab_login"]}</a>'
        f'<a href="{register_href}"{register_current}>{copy["tab_register"]}</a></nav>'
        f'{notice}<form method="post" action="{action}"><input type="hidden" name="csrf" value="{form_token}">'
        f'{return_field}{fields}<button type="submit">{button}</button></form>{footer_html}'
    )
    return _auth_shell_response(
        request,
        lang=lang,
        card_html=card_html,
        current_page=current_page,
        form_token=form_token,
        script=script,
        script_nonce=script_nonce if script else "",
        status_code=status_code,
    )


def _forgot_password_page(
    request: Request,
    message: str = "",
    *,
    info_notice: bool = False,
    smtp_configured: bool = True,
    email_value: str = "",
    next_target: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    lang = _request_lang(request)
    copy = _AUTH_COPY[lang]
    form_token = _form_token_for(request)
    encoded_target = quote(next_target, safe="/") if next_target else ""
    current_page = f"/_goalcal/forgot-password?next={encoded_target}" if next_target else "/_goalcal/forgot-password"
    login_href = f"/_goalcal/login?next={encoded_target}" if next_target else "/_goalcal/login"
    return_field = f'<input type="hidden" name="next" value="{html.escape(next_target, quote=True)}">' if next_target else ""
    notice_html = ""
    if message:
        cls = "notice info" if info_notice else "notice"
        notice_html = f'<p class="{cls}" role="alert">{html.escape(message)}</p>'
    elif not smtp_configured:
        notice_html = f'<p class="notice" role="alert">{html.escape(copy["forgot_smtp_missing"])}</p>'
    form_html = (
        f'<form method="post" action="/_goalcal/forgot-password">'
        f'<input type="hidden" name="csrf" value="{form_token}">{return_field}'
        f'<label>{copy["email"]}<input name="email" type="email" autocomplete="email" '
        f'value="{html.escape(email_value, quote=True)}" required></label>'
        f'<button type="submit">{copy["forgot_btn"]}</button></form>'
        if smtp_configured
        else ""
    )
    card_html = (
        f'<h2 class="card-title">{copy["forgot_title"]}</h2>'
        f'<p class="card-desc">{copy["forgot_desc"]}</p>'
        f'{notice_html}{form_html}'
        f'<div class="auth-links"><a class="auth-link" href="{login_href}">{copy["back_to_login"]}</a></div>'
    )
    return _auth_shell_response(
        request,
        lang=lang,
        card_html=card_html,
        current_page=current_page,
        form_token=form_token,
        status_code=status_code,
    )


def _reset_password_page(
    request: Request,
    message: str = "",
    *,
    token: str = "",
    token_valid: bool = True,
    next_target: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    lang = _request_lang(request)
    copy = _AUTH_COPY[lang]
    form_token = _form_token_for(request)
    encoded_target = quote(next_target, safe="/") if next_target else ""
    encoded_token = quote(token, safe="") if token else ""
    current_page = "/_goalcal/reset-password"
    if encoded_token:
        current_page += f"?token={encoded_token}" + (f"&next={encoded_target}" if encoded_target else "")
    elif encoded_target:
        current_page += f"?next={encoded_target}"
    login_href = f"/_goalcal/login?next={encoded_target}" if next_target else "/_goalcal/login"
    forgot_href = f"/_goalcal/forgot-password?next={encoded_target}" if next_target else "/_goalcal/forgot-password"
    return_field = f'<input type="hidden" name="next" value="{html.escape(next_target, quote=True)}">' if next_target else ""

    if token_valid:
        notice_html = f'<p class="notice" role="alert">{html.escape(message)}</p>' if message else ""
        body_html = (
            f'<p class="card-desc">{copy["reset_desc"]}</p>{notice_html}'
            f'<form method="post" action="/_goalcal/reset-password">'
            f'<input type="hidden" name="csrf" value="{form_token}">{return_field}'
            f'<input type="hidden" name="token" value="{html.escape(token, quote=True)}">'
            f'<label>{copy["reset_new_pw"]}<input name="password" type="password" autocomplete="new-password" minlength="{auth.MIN_PASSWORD_LEN}" required></label>'
            f'<label>{copy["reset_confirm_pw"]}<input name="confirm_password" type="password" autocomplete="new-password" minlength="{auth.MIN_PASSWORD_LEN}" required></label>'
            f'<button type="submit">{copy["reset_btn"]}</button></form>'
        )
    else:
        err = message or copy["reset_invalid"]
        body_html = (
            f'<p class="notice" role="alert">{html.escape(err)}</p>'
            f'<a class="btn-secondary" href="{forgot_href}">{copy["reset_request_again"]}</a>'
        )
    card_html = (
        f'<h2 class="card-title">{copy["reset_title"]}</h2>'
        f'{body_html}'
        f'<div class="auth-links"><a class="auth-link" href="{login_href}">{copy["back_to_login"]}</a></div>'
    )
    return _auth_shell_response(
        request,
        lang=lang,
        card_html=card_html,
        current_page=current_page,
        form_token=form_token,
        status_code=status_code,
    )


async def _small_form(request: Request) -> bool:
    try:
        size = int(request.headers.get("content-length", "0"))
    except ValueError:
        return False
    return 0 <= size <= 4096 and len(await request.body()) <= 4096


def _valid_form_token(request: Request, form) -> bool:
    cookie = request.cookies.get(FORM_COOKIE_NAME, "")
    submitted = str(form.get("csrf", ""))
    return bool(cookie and submitted and secrets.compare_digest(cookie, submitted))


def _browser_websocket_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin", "")
    parsed = urlsplit(origin)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.netloc.lower() == websocket.headers.get("host", "").lower()
        and not parsed.path.strip("/")
    )


def _browser_token(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    try:
        auth.authenticate(token)
    except auth.AuthError:
        return None
    return token


def _secure_cookie(request: Request) -> bool:
    host = request.headers.get("host", "").split(":", 1)[0].lower()
    return request.headers.get("x-forwarded-proto", "").lower() == "https" or host not in {"127.0.0.1", "localhost", "[::1]"}


def _same_origin_post(request: Request) -> bool:
    """Reject cross-site form posts while allowing clients without Origin."""
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return False
    origin = request.headers.get("origin")
    if not origin:
        return True
    parsed = urlsplit(origin)
    hosts = {request.headers.get("host", "").lower()}
    forwarded = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip().lower()
    if forwarded:
        hosts.add(forwarded)
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() in hosts


def _root_app_enabled() -> bool:
    return os.getenv("GOALCAL_ROOT_APP", "1").strip().lower() in {"1", "true", "yes", "on"}


def _legacy_ui_enabled() -> bool:
    return os.getenv("GOALCAL_LEGACY_UI", "0").strip().lower() in {"1", "true", "yes", "on"}


async def health(request: Request) -> Response:
    """Readiness checks the actual Web database and built frontend, not Streamlit."""
    from .config import ROOT
    from .db import get_engine
    from .migrations import VERSION

    def ready():
        try:
            with get_engine().connect() as con:
                schema = con.exec_driver_sql('PRAGMA user_version').scalar()
            return (
                schema == VERSION
                and (ROOT / 'frontend/dist/index.html').is_file()
                and (ROOT / 'frontend/dist/intro.html').is_file()
            )
        except Exception:
            return False

    ok = await run_in_threadpool(ready)
    return JSONResponse({'status': 'ok' if ok else 'unavailable', 'application': 'goalcal-web'},
                        status_code=200 if ok else 503, headers={'Cache-Control': 'no-store'})


def _resolve_app_next(next_value: str | None) -> str | None:
    if next_value is None or next_value == "":
        return "/app/" if _root_app_enabled() else None
    if any(ch in next_value for ch in ("\r", "\n", "\\")):
        return None
    parsed = urlsplit(next_value)
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path.startswith("//"):
        return None
    if parsed.path != "/app" and not parsed.path.startswith("/app/"):
        return None
    if ".." in parsed.path.split("/"):
        return None
    path = "/app/" if parsed.path == "/app" else parsed.path
    return path + (f"?{parsed.query}" if parsed.query else "")


def _is_app_target(next_value: str | None) -> bool:
    return bool(_resolve_app_next(next_value))


def _apply_login_lang(token: str, lang: str) -> None:
    if lang not in {"zh", "en"}:
        return
    from .web.core import Service
    from .web.http import scoped

    with scoped(token, write=True) as (session, principal):
        Service(
            session, principal.user_id, principal.tz, datetime.now(timezone.utc), lang=lang
        ).update_me({"lang": lang})


async def _parse_auth_form(request: Request, *, register: bool = False):
    if not _same_origin_post(request) or not await _small_form(request):
        return None, PlainTextResponse("Invalid form submission", status_code=403)
    form = await request.form()
    next_target = _resolve_app_next(str(form.get("next")) if form.get("next") is not None else None)
    lang = _request_lang(request)
    if not _valid_form_token(request, form):
        return None, _login_page(
            request,
            _AUTH_COPY[lang]["form_expired"],
            register=register,
            next_target=next_target,
            status_code=403,
        )
    return (form, next_target, lang), None


async def login(request: Request) -> Response:
    if request.method == "GET":
        return _login_page(request, next_target=_resolve_app_next(request.query_params.get("next")))
    parsed, error_response = await _parse_auth_form(request)
    if error_response is not None:
        return error_response
    form, next_target, lang = parsed
    try:
        token = await run_in_threadpool(
            auth.login, str(form.get("username", "")), str(form.get("password", ""))
        )
    except auth.AuthError as exc:
        return _login_page(request, localize_auth_error(str(exc), lang), next_target=next_target)
    cookie_lang = normalize_lang(request.cookies.get(LANG_COOKIE_NAME))
    if cookie_lang:
        await run_in_threadpool(_apply_login_lang, token, cookie_lang)
    response = RedirectResponse(next_target or "/", status_code=303)
    response.set_cookie(
        COOKIE_NAME, token, max_age=auth.session_ttl_seconds(), path="/",
        secure=_secure_cookie(request), httponly=True, samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


async def register(request: Request) -> Response:
    if request.method == "GET":
        return _login_page(request, register=True, next_target=_resolve_app_next(request.query_params.get("next")))
    parsed, error_response = await _parse_auth_form(request, register=True)
    if error_response is not None:
        return error_response
    form, next_target, lang = parsed
    try:
        await run_in_threadpool(
            auth.create_user,
            str(form.get("username", "")), str(form.get("password", "")),
            str(form.get("tz", "Asia/Shanghai")), str(form.get("invitation", "")),
            email=str(form.get("email", "")) if form.get("email") is not None else None,
        )
    except auth.AuthError as exc:
        return _login_page(request, localize_auth_error(str(exc), lang), register=True, next_target=next_target)
    return _login_page(request, _AUTH_COPY[lang]["created"], next_target=next_target, info_notice=True)


def _build_password_reset_email(reset_url: str, username: str) -> tuple[str, str, str]:
    subject = "GoalCal 密码重置 / Password Reset"
    safe_user = html.escape(username)
    safe_url = html.escape(reset_url, quote=True)
    text_body = (
        f"你好 {username}：\n\n"
        "我们收到了你的 GoalCal 密码重置请求。请点击下方链接设置新密码（30 分钟内有效，仅可使用一次）：\n"
        f"{reset_url}\n\n"
        "如果这不是你本人操作，请忽略此邮件，你的密码不会发生任何变化。\n\n"
        "—\n"
        f"Hi {username},\n\n"
        "We received a request to reset your GoalCal password. Open the link below to set a new password "
        "(valid for 30 minutes, one-time use):\n"
        f"{reset_url}\n\n"
        "If you did not request this, you can safely ignore this email."
    )
    html_body = (
        '<!doctype html><html><body style="margin:0;padding:24px;background:#fafaf8;color:#1d1d1f;'
        'font-family:-apple-system,BlinkMacSystemFont,\'SF Pro Display\',\'PingFang SC\',Arial,sans-serif;">'
        '<div style="max-width:520px;margin:0 auto;background:#fffefd;border:1px solid rgba(29,29,31,0.1);'
        'border-radius:13px;padding:24px;">'
        '<div style="font-size:16px;font-weight:700;margin-bottom:12px;">GoalCal</div>'
        f'<p style="font-size:14px;line-height:1.6;margin:0 0 12px;">你好 {safe_user}，</p>'
        '<p style="font-size:14px;line-height:1.6;margin:0 0 18px;color:#4a4a4f;">'
        '请点击下方按钮重置你的 GoalCal 密码（链接 <strong>30 分钟</strong>内有效，仅可使用一次）：</p>'
        f'<p style="margin:0 0 18px;"><a href="{safe_url}" '
        'style="display:inline-block;padding:10px 18px;background:#303638;color:#ffffff;'
        'text-decoration:none;border-radius:9px;font-size:13px;font-weight:600;">重置密码 / Reset password</a></p>'
        f'<p style="font-size:12px;line-height:1.5;color:#6e6e73;word-break:break-all;margin:0 0 16px;">{safe_url}</p>'
        '<p style="font-size:12px;line-height:1.5;color:#6e6e73;margin:0;">'
        '如果这不是你本人发起的请求，请直接忽略本邮件。<br>'
        'If you did not request a password reset, you can safely ignore this email.</p>'
        '</div></body></html>'
    )
    return subject, text_body, html_body


async def forgot_password(request: Request) -> Response:
    from . import mailer

    caps = mailer.smtp_capabilities()
    smtp_ok = bool(caps.get("configured"))
    if request.method == "GET":
        return _forgot_password_page(
            request,
            smtp_configured=smtp_ok,
            next_target=_resolve_app_next(request.query_params.get("next")),
        )
    if not _same_origin_post(request) or not await _small_form(request):
        return PlainTextResponse("Invalid form submission", status_code=403)
    form = await request.form()
    next_target = _resolve_app_next(str(form.get("next")) if form.get("next") is not None else None)
    lang = _request_lang(request)
    copy = _AUTH_COPY[lang]
    email_raw = str(form.get("email", ""))
    if not _valid_form_token(request, form):
        return _forgot_password_page(
            request,
            copy["form_expired"],
            smtp_configured=smtp_ok,
            email_value=email_raw,
            next_target=next_target,
            status_code=403,
        )
    if not smtp_ok:
        return _forgot_password_page(
            request,
            copy["forgot_smtp_missing"],
            smtp_configured=False,
            email_value=email_raw,
            next_target=next_target,
            status_code=503,
        )
    try:
        issued = await run_in_threadpool(auth.request_password_reset, email_raw)
    except auth.AuthError as exc:
        return _forgot_password_page(
            request,
            localize_auth_error(str(exc), lang),
            smtp_configured=smtp_ok,
            email_value=email_raw,
            next_target=next_target,
        )

    if issued is not None:
        token, to_email, username = issued
        public_base = os.getenv("GOALCAL_PUBLIC_URL", "").strip().rstrip("/") or _public_base_url(request).rstrip("/")
        reset_url = f"{public_base}/_goalcal/reset-password?token={quote(token, safe='')}"
        if next_target:
            reset_url += f"&next={quote(next_target, safe='')}"
        subject, text_body, html_body = _build_password_reset_email(reset_url, username)
        try:
            await run_in_threadpool(
                mailer.send_email,
                to_address=to_email,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
            )
        except mailer.MailerError:
            logging.getLogger("goalcal.audit").warning("password_reset_email_failed", exc_info=True)
            return _forgot_password_page(
                request,
                copy["forgot_send_failed"],
                smtp_configured=smtp_ok,
                email_value=email_raw,
                next_target=next_target,
                status_code=502,
            )

    return _forgot_password_page(
        request,
        copy["forgot_sent"],
        info_notice=True,
        smtp_configured=smtp_ok,
        next_target=next_target,
    )


async def reset_password(request: Request) -> Response:
    if request.method == "GET":
        token = request.query_params.get("token", "").strip()
        next_target = _resolve_app_next(request.query_params.get("next"))
        valid = await run_in_threadpool(auth.verify_password_reset_token, token)
        return _reset_password_page(
            request,
            token=token,
            token_valid=valid,
            next_target=next_target,
        )
    if not _same_origin_post(request) or not await _small_form(request):
        return PlainTextResponse("Invalid form submission", status_code=403)
    form = await request.form()
    next_target = _resolve_app_next(str(form.get("next")) if form.get("next") is not None else None)
    token = str(form.get("token", "")).strip()
    lang = _request_lang(request)
    copy = _AUTH_COPY[lang]
    if not _valid_form_token(request, form):
        valid = await run_in_threadpool(auth.verify_password_reset_token, token)
        return _reset_password_page(
            request,
            copy["form_expired"],
            token=token,
            token_valid=valid,
            next_target=next_target,
            status_code=403,
        )
    password = str(form.get("password", ""))
    confirm_password = str(form.get("confirm_password", ""))
    if password != confirm_password:
        valid = await run_in_threadpool(auth.verify_password_reset_token, token)
        return _reset_password_page(
            request,
            localize_auth_error("两次输入的密码不一致", lang),
            token=token,
            token_valid=valid,
            next_target=next_target,
        )
    try:
        await run_in_threadpool(auth.complete_password_reset, token, password)
    except auth.AuthError as exc:
        msg = str(exc)
        is_token_err = "重置链接无效" in msg
        return _reset_password_page(
            request,
            localize_auth_error(msg, lang),
            token="" if is_token_err else token,
            token_valid=not is_token_err,
            next_target=next_target,
        )
    response = _login_page(
        request,
        copy["password_reset_done"],
        next_target=next_target,
        info_notice=True,
    )
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


def _safe_auth_next(target: str | None) -> str:
    if not target:
        return "/"
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/") or parsed.path.startswith("//"):
        return "/"
    return parsed.path + (f"?{parsed.query}" if parsed.query else "")


async def set_language(request: Request) -> Response:
    raw_lang = request.query_params.get("lang", "")
    lang = normalize_lang(raw_lang)
    target = _safe_auth_next(request.query_params.get("next"))
    response = RedirectResponse(target, status_code=303)
    if lang:
        response.set_cookie(
            LANG_COOKIE_NAME, lang, max_age=365 * 24 * 60 * 60, path="/",
            secure=_secure_cookie(request), httponly=False, samesite="lax",
        )
    else:
        response.delete_cookie(LANG_COOKIE_NAME, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


async def logout(request: Request) -> Response:
    # 登出不读取表单正文或依赖登录 CSRF cookie；始终清掉浏览器 session。
    if not _same_origin_post(request):
        return PlainTextResponse("Invalid request origin", status_code=403)
    token = _browser_token(request)
    if token is not None:
        auth.logout(token)
    target = "/_goalcal/login?next=/app/" if _is_app_target(request.query_params.get("next")) else "/"
    response = RedirectResponse(target, status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


async def public_brand_icon(request: Request) -> Response:
    spec = _PUBLIC_BRAND_ICONS.get(request.url.path)
    if spec is None:
        return PlainTextResponse("Not found", status_code=404)
    filename, media_type = spec
    from .config import ROOT
    for directory in (ROOT / "frontend" / "dist", ROOT / "frontend" / "public"):
        candidate = directory / filename
        if candidate.is_file():
            return FileResponse(candidate, media_type=media_type, headers={
                "Cache-Control": "public, max-age=86400",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "same-origin",
            })
    return PlainTextResponse("Not found", status_code=404)


async def landing(request: Request) -> Response:
    token = _browser_token(request)
    if token is not None:
        try:
            await run_in_threadpool(auth.authenticate, token)
            return RedirectResponse("/app/", status_code=307)
        except auth.AuthError:
            pass
    from .config import ROOT
    page = ROOT / "frontend" / "dist" / "intro.html"
    if not page.is_file():
        return PlainTextResponse("Frontend not built. Run npm ci && npm run build in frontend/.", status_code=503)
    return FileResponse(page, headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
    })


async def proxy_http(request: Request) -> Response:
    if _root_app_enabled() and request.url.path == "/" and request.method in {"GET", "HEAD"}:
        return await landing(request)
    if not _legacy_ui_enabled():
        return PlainTextResponse("Not found", status_code=404)
    if request.method not in {"GET", "HEAD", "OPTIONS"} and _browser_token(request) is None:
        return PlainTextResponse("Authentication required", status_code=401)
    if request.method == "GET" and request.headers.get("accept", "").find("text/html") >= 0:
        if _browser_token(request) is None:
            return _login_page(request)
    path = request.url.path
    if request.url.query:
        path += "?" + request.url.query
    headers = [(key, value) for key, value in request.headers.items() if key.lower() not in _HOP_HEADERS]
    headers.append(("host", request.headers.get("host", "")))
    try:
        async with httpx2.AsyncClient(trust_env=False, timeout=60) as client:
            upstream = await client.request(
                request.method, UPSTREAM_HTTP + path, headers=headers,
                content=await request.body(), follow_redirects=False,
            )
    except httpx2.HTTPError:
        return PlainTextResponse("Application unavailable", status_code=502)
    response = Response(upstream.content, status_code=upstream.status_code)
    for key, value in upstream.headers.multi_items():
        if key.lower() not in _HOP_HEADERS:
            response.headers.append(key, value)
    return response


async def proxy_websocket(websocket: WebSocket) -> None:
    if not _legacy_ui_enabled():
        await websocket.close(code=1008)
        return
    if not _browser_websocket_origin(websocket):
        await websocket.close(code=1008)
        return
    token = websocket.cookies.get(COOKIE_NAME)
    try:
        auth.authenticate(token)
    except auth.AuthError:
        await websocket.close(code=1008)
        return
    path = websocket.url.path
    if websocket.url.query:
        path += "?" + websocket.url.query
    headers = [
        (key.decode("latin-1"), value.decode("latin-1"))
        for key, value in websocket.scope["headers"]
        if key.lower() not in {
            b"host", b"connection", b"upgrade", b"sec-websocket-key",
            b"sec-websocket-version", b"sec-websocket-extensions", b"sec-websocket-protocol",
            b"origin",
        }
    ]
    try:
        async with websockets.connect(
            UPSTREAM_WS + path, additional_headers=headers,
            origin=UPSTREAM_HTTP,
            subprotocols=websocket.scope.get("subprotocols") or None,
            proxy=None,
        ) as upstream:
            await websocket.accept(subprotocol=upstream.subprotocol)

            async def to_upstream() -> None:
                try:
                    while True:
                        message = await websocket.receive()
                        if message["type"] == "websocket.disconnect":
                            break
                        if message.get("text") is not None:
                            await upstream.send(message["text"])
                        elif message.get("bytes") is not None:
                            await upstream.send(message["bytes"])
                except WebSocketDisconnect:
                    pass

            async def to_browser() -> None:
                async for message in upstream:
                    if isinstance(message, str):
                        await websocket.send_text(message)
                    else:
                        await websocket.send_bytes(message)

            tasks = [asyncio.create_task(to_upstream()), asyncio.create_task(to_browser())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
    except (OSError, websockets.WebSocketException):
        await websocket.close(code=1011)


_FEED_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,128}")


def _public_base_url(request: Request) -> str:
    scheme = request.url.scheme
    if request.client is None or request.client.host in {"127.0.0.1", "::1"}:
        forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
        if forwarded in {"http", "https"}:
            scheme = forwarded
    host = request.headers.get("host") or request.url.netloc
    return f"{scheme}://{host}" if host else ""


async def calendar_feed(request: Request) -> Response:
    """Public token-authenticated read-only .ics subscription feed."""
    filename = request.path_params.get("filename", "")
    if not filename.endswith(".ics"):
        return PlainTextResponse("Not found", status_code=404)
    token = filename[:-4]
    if not _FEED_TOKEN_RE.fullmatch(token):
        return PlainTextResponse("Not found", status_code=404)
    from .domain.calendar_sync import serve_feed_by_token

    status, body, etag = await run_in_threadpool(
        serve_feed_by_token,
        token,
        if_none_match=request.headers.get("if-none-match"),
        base_url=_public_base_url(request),
    )
    if status == 404:
        return PlainTextResponse("Not found", status_code=404, headers={"Cache-Control": "no-store"})
    headers = {
        "Cache-Control": "private, no-cache",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex, nofollow",
    }
    if etag:
        headers["ETag"] = etag
    if status == 304:
        return Response(b"", status_code=304, headers=headers)
    headers["Content-Disposition"] = 'inline; filename="goalcal.ics"'
    return Response(
        b"" if request.method == "HEAD" else body,
        status_code=200,
        media_type="text/calendar; charset=utf-8",
        headers=headers,
    )


def _sync_worker_enabled() -> bool:
    return os.getenv("GOALCAL_SYNC_WORKER", "1").strip().lower() not in {"0", "false", "no", "off"}


_OAUTH_COOKIE_NAME = "goalcal_oauth_state"


def _oauth_redirect_uri(request: Request, provider: str) -> str:
    base = _public_base_url(request).rstrip("/")
    return f"{base}/_goalcal/oauth/{provider}/callback"


async def oauth_start(request: Request) -> Response:
    """Start Google / Outlook OAuth 2.0 PKCE flow for an authenticated user."""
    provider = request.path_params.get("provider", "").strip().lower()
    if provider not in {"google", "outlook"}:
        return PlainTextResponse("Not found", status_code=404)
    token = request.cookies.get(COOKIE_NAME)
    try:
        principal = await run_in_threadpool(auth.authenticate, token)
    except auth.AuthError:
        return RedirectResponse("/_goalcal/login?next=/app/settings%3Fsection%3Dsync", status_code=303)

    from .crypto import sign_oauth_state
    from .domain.sync_providers import (
        GoogleCalendarAdapter,
        OutlookCalendarAdapter,
        generate_pkce_pair,
        provider_capabilities,
    )

    caps = provider_capabilities()
    if not caps.get(provider, {}).get("configured"):
        return RedirectResponse("/app/settings?section=sync&oauth_error=not_configured", status_code=303)

    verifier, challenge = generate_pkce_pair()
    nonce = secrets.token_urlsafe(16)
    state_token = sign_oauth_state({"uid": principal.user_id, "provider": provider, "nonce": nonce})
    cookie_token = sign_oauth_state({"uid": principal.user_id, "provider": provider, "nonce": nonce, "verifier": verifier})
    redirect_uri = _oauth_redirect_uri(request, provider)
    if provider == "google":
        auth_url = GoogleCalendarAdapter.authorize_url(redirect_uri=redirect_uri, state=state_token, code_challenge=challenge)
    else:
        auth_url = OutlookCalendarAdapter.authorize_url(redirect_uri=redirect_uri, state=state_token, code_challenge=challenge)

    resp = RedirectResponse(auth_url, status_code=302)
    resp.set_cookie(
        _OAUTH_COOKIE_NAME,
        cookie_token,
        max_age=600,
        path="/_goalcal/oauth/",
        secure=_secure_cookie(request),
        httponly=True,
        samesite="lax",
    )
    return resp


def _complete_oauth_sync_db(
    *,
    user_id: int,
    provider: str,
    account_label: str,
    secrets_dict: dict,
    expires_at,
    calendars: list,
) -> tuple[int, list[int]]:
    from datetime import datetime as _dt, timezone as _tz
    from sqlalchemy import select as _select
    from sqlalchemy.orm import Session as _Session
    from . import db as _db
    from .crypto import encrypt_secrets
    from .domain.bidirectional_sync import backfill_goalcal_source
    from .models import CalendarConnection
    from .web.calendar_sync import upsert_connection_sources

    now_naive = _dt.now(_tz.utc).replace(tzinfo=None)
    encrypted = encrypt_secrets(secrets_dict)
    with _db.get_engine().connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        ws = _Session(bind=connection, expire_on_commit=False, join_transaction_mode="rollback_only")
        try:
            conn = ws.scalar(
                _select(CalendarConnection).where(
                    CalendarConnection.user_id == user_id,
                    CalendarConnection.provider == provider,
                    CalendarConnection.account_label == account_label,
                )
            )
            if conn is None:
                conn = CalendarConnection(
                    user_id=user_id,
                    provider=provider,
                    account_label=account_label[:200],
                    server_url="",
                    username=account_label[:200],
                    encrypted_secrets=encrypted,
                    token_expires_at=expires_at,
                    status="active",
                    last_error="",
                    created_at=now_naive,
                    updated_at=now_naive,
                )
                ws.add(conn)
                ws.flush()
            else:
                if not secrets_dict.get("refresh_token") and conn.encrypted_secrets:
                    from .crypto import decrypt_secrets
                    try:
                        prev_sec = decrypt_secrets(conn.encrypted_secrets)
                        if prev_sec.get("refresh_token"):
                            secrets_dict = {**secrets_dict, "refresh_token": prev_sec["refresh_token"]}
                            encrypted = encrypt_secrets(secrets_dict)
                    except Exception:
                        pass
                conn.encrypted_secrets = encrypted
                conn.token_expires_at = expires_at
                conn.status = "active"
                conn.last_error = ""
                conn.updated_at = now_naive

            sources, created = upsert_connection_sources(
                ws,
                user_id=user_id,
                conn=conn,
                calendars=calendars,
                now_naive=now_naive,
                enable_goalcal=True,
            )
            for src in created:
                if src.is_goalcal_calendar and src.enabled:
                    backfill_goalcal_source(ws, user_id, src, now_naive=now_naive)
            source_ids = [s.id for s in sources if s.enabled]
            ws.flush()
            connection.commit()
            return conn.id, source_ids
        except BaseException:
            connection.rollback()
            raise
        finally:
            ws.close()


def _register_webhooks_best_effort(provider: str, public_base: str, secrets_dict: dict, source_ids: list[int]) -> None:
    if not public_base.startswith("https://") or not source_ids:
        return
    from datetime import datetime as _dt, timezone as _tz
    from sqlalchemy.orm import Session as _Session
    from . import db as _db
    from .domain.sync_providers import GoogleCalendarAdapter, OutlookCalendarAdapter
    from .models import CalendarSource

    now_naive = _dt.now(_tz.utc).replace(tzinfo=None)
    callback_url = f"{public_base.rstrip('/')}/calendar/webhook/{provider}"
    session = _db.new_session()
    targets: list[tuple[int, str]] = []
    try:
        for sid in source_ids:
            src = session.get(CalendarSource, sid)
            if src is not None and src.remote_calendar_id:
                targets.append((src.id, src.remote_calendar_id))
    finally:
        session.close()

    updates: list[tuple[int, str, str, str, object]] = []
    for sid, remote_cal_id in targets:
        try:
            channel_id = f"gc-{provider}-{sid}-{secrets.token_hex(6)}"
            wh_secret = secrets.token_urlsafe(24)
            if provider == "google":
                res_id, exp = GoogleCalendarAdapter.watch(
                    remote_cal_id,
                    secrets_dict,
                    callback_url=callback_url,
                    channel_id=channel_id,
                    token=wh_secret,
                )
            else:
                res_id, exp = OutlookCalendarAdapter.watch(
                    remote_cal_id,
                    secrets_dict,
                    callback_url=callback_url,
                    client_state=wh_secret,
                    now_naive=now_naive,
                )
            updates.append((sid, channel_id, res_id, wh_secret, exp))
        except Exception:
            pass  # Webhook failure never blocks initial OAuth connection; polling still syncs.

    if updates:
        with _db.get_engine().connect() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            ws = _Session(bind=connection, expire_on_commit=False, join_transaction_mode="rollback_only")
            try:
                for sid, ch_id, res_id, wh_sec, exp in updates:
                    src = ws.get(CalendarSource, sid)
                    if src is not None:
                        src.webhook_channel_id = ch_id[:128]
                        src.webhook_resource_id = res_id[:255]
                        src.webhook_secret = wh_sec[:128]
                        src.webhook_expires_at = exp
                ws.flush()
                connection.commit()
            except BaseException:
                connection.rollback()
            finally:
                ws.close()


async def oauth_callback(request: Request) -> Response:
    """Handle Google / Outlook OAuth callback cleanly outside SQLite write locks."""
    provider = request.path_params.get("provider", "").strip().lower()
    if provider not in {"google", "outlook"}:
        return PlainTextResponse("Not found", status_code=404)

    def _clear_redirect(url: str) -> Response:
        r = RedirectResponse(url, status_code=302)
        r.delete_cookie(_OAUTH_COOKIE_NAME, path="/_goalcal/oauth/")
        return r

    if request.query_params.get("error"):
        return _clear_redirect("/app/settings?section=sync&oauth_error=denied")

    token = request.cookies.get(COOKIE_NAME)
    try:
        principal = await run_in_threadpool(auth.authenticate, token)
    except auth.AuthError:
        return _clear_redirect("/_goalcal/login?next=/app/settings%3Fsection%3Dsync")

    from .crypto import CredentialError, verify_oauth_state
    from .domain.sync_providers import GoogleCalendarAdapter, OutlookCalendarAdapter, SyncProviderError

    state_raw = request.query_params.get("state", "")
    cookie_raw = request.cookies.get(_OAUTH_COOKIE_NAME, "")
    code = request.query_params.get("code", "")
    try:
        state_data = verify_oauth_state(state_raw)
        cookie_data = verify_oauth_state(cookie_raw)
    except CredentialError:
        return _clear_redirect("/app/settings?section=sync&oauth_error=invalid_state")

    if (
        not code
        or state_data.get("uid") != principal.user_id
        or cookie_data.get("uid") != principal.user_id
        or state_data.get("provider") != provider
        or cookie_data.get("provider") != provider
        or not secrets.compare_digest(str(state_data.get("nonce", "")), str(cookie_data.get("nonce", "")))
    ):
        return _clear_redirect("/app/settings?section=sync&oauth_error=invalid_state")

    verifier = str(cookie_data.get("verifier") or "")
    redirect_uri = _oauth_redirect_uri(request, provider)
    public_base = _public_base_url(request)

    def _finish_exchange():
        if provider == "google":
            secrets_dict, expires_at = GoogleCalendarAdapter.exchange_code(
                code=code, redirect_uri=redirect_uri, code_verifier=verifier
            )
            label = GoogleCalendarAdapter.userinfo_email(secrets_dict)
            calendars = GoogleCalendarAdapter.discover(secrets_dict, tz=principal.tz, ensure_goalcal=True)
        else:
            secrets_dict, expires_at = OutlookCalendarAdapter.exchange_code(
                code=code, redirect_uri=redirect_uri, code_verifier=verifier
            )
            label = OutlookCalendarAdapter.userinfo_email(secrets_dict)
            calendars = OutlookCalendarAdapter.discover(secrets_dict, ensure_goalcal=True)
        _, source_ids = _complete_oauth_sync_db(
            user_id=principal.user_id,
            provider=provider,
            account_label=label,
            secrets_dict=secrets_dict,
            expires_at=expires_at,
            calendars=calendars,
        )
        _register_webhooks_best_effort(provider, public_base, secrets_dict, source_ids)
        return source_ids

    try:
        await run_in_threadpool(_finish_exchange)
    except SyncProviderError:
        return _clear_redirect("/app/settings?section=sync&oauth_error=exchange_failed")
    except Exception:
        logging.getLogger("goalcal.calendar_sync").warning("oauth_callback_failed provider=%s", provider, exc_info=True)
        return _clear_redirect("/app/settings?section=sync&oauth_error=exchange_failed")

    return _clear_redirect(f"/app/settings?section=sync&connected={provider}")


async def google_webhook(request: Request) -> Response:
    """Receive push notifications from Google Calendar watch channels."""
    channel_id = request.headers.get("x-goog-channel-id", "").strip()
    channel_token = request.headers.get("x-goog-channel-token", "").strip()
    state = request.headers.get("x-goog-resource-state", "").strip().lower()
    if not channel_id or not channel_token:
        return PlainTextResponse("Not found", status_code=404)

    from sqlalchemy import select as _select
    from . import db as _db
    from .models import CalendarSource

    def _lookup_source():
        s = _db.new_session()
        try:
            src = s.scalar(
                _select(CalendarSource).where(
                    CalendarSource.provider == "google",
                    CalendarSource.webhook_channel_id == channel_id,
                    CalendarSource.enabled.is_(True),
                )
            )
            if src is None or not src.webhook_secret or not secrets.compare_digest(src.webhook_secret, channel_token):
                return None
            return src.id
        finally:
            s.close()

    source_id = await run_in_threadpool(_lookup_source)
    if source_id is None:
        return PlainTextResponse("Not found", status_code=404)
    if state != "sync":
        from .domain.bidirectional_sync import sync_native_source_once
        await run_in_threadpool(sync_native_source_once, source_id)
    return Response(status_code=202)


async def outlook_webhook(request: Request) -> Response:
    """Handle Microsoft Graph subscription validation handshake and change notifications."""
    validation_token = request.query_params.get("validationToken")
    if validation_token is not None:
        return PlainTextResponse(validation_token, status_code=200)

    try:
        payload = json.loads((await request.body()).decode("utf-8") or "{}")
    except Exception:
        return PlainTextResponse("Bad request", status_code=400)
    notifications = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(notifications, list):
        return Response(status_code=202)

    from sqlalchemy import select as _select
    from . import db as _db
    from .domain.bidirectional_sync import sync_native_source_once
    from .models import CalendarSource

    def _matching_sources() -> list[int]:
        s = _db.new_session()
        ids: list[int] = []
        try:
            for item in notifications:
                if not isinstance(item, dict):
                    continue
                sub_id = str(item.get("subscriptionId") or "").strip()
                client_state = str(item.get("clientState") or "").strip()
                if not sub_id or not client_state:
                    continue
                src = s.scalar(
                    _select(CalendarSource).where(
                        CalendarSource.provider == "outlook",
                        CalendarSource.webhook_resource_id == sub_id,
                        CalendarSource.enabled.is_(True),
                    )
                )
                if src is not None and src.webhook_secret and secrets.compare_digest(src.webhook_secret, client_state):
                    if src.id not in ids:
                        ids.append(src.id)
        finally:
            s.close()
        return ids

    source_ids = await run_in_threadpool(_matching_sources)
    for sid in source_ids:
        await run_in_threadpool(sync_native_source_once, sid)
    return Response(status_code=202)


async def _calendar_sync_loop() -> None:
    from .domain.calendar_sync import sync_due_sources_once

    log = logging.getLogger("goalcal.calendar_sync")
    while True:
        try:
            await asyncio.sleep(60)
            await run_in_threadpool(sync_due_sources_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("calendar_sync_background_tick_failed", exc_info=True)


async def _sync_outbox_loop() -> None:
    from .domain.bidirectional_sync import process_outbox_once

    log = logging.getLogger("goalcal.bidirectional_sync")
    while True:
        try:
            await asyncio.sleep(3)
            await run_in_threadpool(process_outbox_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("sync_outbox_tick_failed", exc_info=True)


async def _reminder_loop() -> None:
    from .domain.reminder_engine import plan_due_notifications_once, process_notification_outbox_once

    log = logging.getLogger("goalcal.reminders")
    while True:
        try:
            await asyncio.sleep(60)
            await run_in_threadpool(plan_due_notifications_once)
            await run_in_threadpool(process_notification_outbox_once)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("reminder_loop_tick_failed", exc_info=True)


@asynccontextmanager
async def _lifespan(_app: Starlette):
    tasks: list[asyncio.Task] = []
    if _sync_worker_enabled():
        tasks.append(asyncio.create_task(_calendar_sync_loop()))
        tasks.append(asyncio.create_task(_sync_outbox_loop()))
        tasks.append(asyncio.create_task(_reminder_loop()))
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass


from .web.http import api as web_api, frontend as web_frontend


app = Starlette(
    routes=[
        Route("/_goalcal/health", health, methods=["GET", "HEAD"]),
        Route("/favicon.svg", public_brand_icon, methods=["GET", "HEAD"]),
        Route("/favicon.ico", public_brand_icon, methods=["GET", "HEAD"]),
        Route("/apple-touch-icon.png", public_brand_icon, methods=["GET", "HEAD"]),
        Route("/calendar/feed/{filename:str}", calendar_feed, methods=["GET", "HEAD"]),
        Route("/calendar/webhook/google", google_webhook, methods=["POST"]),
        Route("/calendar/webhook/outlook", outlook_webhook, methods=["GET", "POST"]),
        Mount("/api/v1", app=web_api),
        Route("/app/{path:path}", web_frontend, methods=["GET", "HEAD"]),
        Route("/app", lambda request: RedirectResponse("/app/", status_code=307)),
        Route("/_goalcal/oauth/{provider:str}/start", oauth_start, methods=["GET"]),
        Route("/_goalcal/oauth/{provider:str}/callback", oauth_callback, methods=["GET"]),
        Route("/_goalcal/login", login, methods=["GET", "POST"]),
        Route("/_goalcal/register", register, methods=["GET", "POST"]),
        Route("/_goalcal/forgot-password", forgot_password, methods=["GET", "POST"]),
        Route("/_goalcal/reset-password", reset_password, methods=["GET", "POST"]),
        Route("/_goalcal/logout", logout, methods=["POST"]),
        Route("/_goalcal/lang", set_language, methods=["GET"]),
        Route("/{path:path}", proxy_http, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]),
        WebSocketRoute("/{path:path}", proxy_websocket),
    ],
    middleware=[Middleware(GZipMiddleware, minimum_size=500)],
    lifespan=_lifespan,
)
