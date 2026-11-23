"""Same-origin authentication, React frontend and Web API gateway.

The gateway owns the HttpOnly browser cookie and authenticates the Web API.
Session tokens are never sent into page JavaScript or a URL. Streamlit proxying
is disabled unless explicitly enabled for local compatibility regression.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import html
import os
import re
import secrets
from urllib.parse import quote, urlsplit
import zoneinfo

import httpx2
import websockets
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response, JSONResponse
from starlette.routing import Route, WebSocketRoute, Mount
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import auth
from .i18n import normalize_lang, parse_accept_language

COOKIE_NAME = "goalcal_session"
FORM_COOKIE_NAME = "goalcal_form"
LANG_COOKIE_NAME = "goalcal_lang"
THEME_COOKIE_NAME = "goalcal_theme"
SESSION_SECONDS = 12 * 60 * 60
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
    },
}


def _login_page(
    request: Request,
    message: str = "",
    *,
    register: bool = False,
    app_login: bool = False,
    status_code: int = 200,
) -> HTMLResponse:
    lang = _request_lang(request)
    theme = _request_theme(request)
    theme_attr = f' data-theme="{theme}"' if theme else ""
    copy = _AUTH_COPY[lang]
    existing_form_token = request.cookies.get(FORM_COOKIE_NAME, "")
    # Reuse the browser's CSRF cookie across tabs and history navigation instead of clobbering it on every GET.
    form_token = (
        existing_form_token
        if _FORM_TOKEN_RE.fullmatch(existing_form_token)
        else secrets.token_urlsafe(32)
    )
    script_nonce = secrets.token_urlsafe(32)
    notice = f'<p class="notice" role="alert">{html.escape(message)}</p>' if message else ""
    open_reg = auth.open_registration_enabled()
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
    else:
        user_label = copy["username_or_email"] if open_reg else copy["username"]
        fields = f"""<label>{user_label}<input name="username" autocomplete="username" required></label>
<label>{copy["password"]}<input name="password" type="password" autocomplete="current-password" required></label>"""
        action, button = "/_goalcal/login", copy["btn_login"]
        login_current, register_current = ' aria-current="page"', ""
        script = ""
    return_field = '<input type="hidden" name="next" value="/app/">' if app_login else ""
    login_href = "/_goalcal/login?next=/app/" if app_login else "/"
    register_href = "/_goalcal/register?next=/app/" if app_login else "/_goalcal/register"
    current_page = register_href if register else login_href
    zh_href = f"/_goalcal/lang?lang=zh&next={quote(current_page, safe='/?=&')}"
    en_href = f"/_goalcal/lang?lang=en&next={quote(current_page, safe='/?=&')}"
    zh_cls = ' class="active"' if lang == "zh" else ""
    en_cls = ' class="active"' if lang == "en" else ""
    brand_html = '<span class="brand-mark" aria-hidden="true"></span><span>GoalCal</span>'
    body = f"""<!doctype html><html lang="{copy["html_lang"]}"{theme_attr}><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GoalCal</title><style>
:root{{color-scheme:light;--bg:#fafaf8;--surface:#fffefd;--surface-soft:#f4f4f1;--ink:#1d1d1f;--muted:#6e6e73;--faint:#a1a1a6;--line:rgba(29,29,31,0.08);--line-strong:rgba(29,29,31,0.16);--accent:#303638;--accent-hover:#23282a;--accent-ink:#fff;--blue:#6b7f89;--tab-shadow:0 1px 2px rgba(20,22,24,0.06);--notice-bg:#fdf3e7;--notice-border:rgba(180,120,40,0.2);--notice-ink:#5c3d15;--radius:13px;--control:9px;--shadow:0 1px 2px rgba(20,22,24,0.02),0 8px 24px rgba(20,22,24,0.025)}}
:root[data-theme="dark"]{{color-scheme:dark;--bg:#131415;--surface:#1b1c1e;--surface-soft:#242629;--ink:#f0f0ee;--muted:#a2a3a8;--faint:#73757a;--line:rgba(255,255,255,0.09);--line-strong:rgba(255,255,255,0.18);--accent:#e5e7e6;--accent-hover:#d4d7d6;--accent-ink:#161819;--blue:#8aa2ad;--tab-shadow:0 1px 2px rgba(0,0,0,0.35);--notice-bg:rgba(218,162,86,0.12);--notice-border:rgba(218,162,86,0.28);--notice-ink:#f2cd98;--shadow:0 1px 2px rgba(0,0,0,0.28),0 8px 24px rgba(0,0,0,0.34)}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{color-scheme:dark;--bg:#131415;--surface:#1b1c1e;--surface-soft:#242629;--ink:#f0f0ee;--muted:#a2a3a8;--faint:#73757a;--line:rgba(255,255,255,0.09);--line-strong:rgba(255,255,255,0.18);--accent:#e5e7e6;--accent-hover:#d4d7d6;--accent-ink:#161819;--blue:#8aa2ad;--tab-shadow:0 1px 2px rgba(0,0,0,0.35);--notice-bg:rgba(218,162,86,0.12);--notice-border:rgba(218,162,86,0.28);--notice-ink:#f2cd98;--shadow:0 1px 2px rgba(0,0,0,0.28),0 8px 24px rgba(0,0,0,0.34)}}}}
*{{box-sizing:border-box}}
body{{font:14px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Display","PingFang SC","Helvetica Neue",Arial,sans-serif;background:var(--bg);color:var(--ink);margin:0;-webkit-font-smoothing:antialiased}}
main{{min-height:100dvh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:32px 18px}}
.shell{{width:100%;max-width:416px}}
.brand-wrap{{display:flex;flex-direction:column;align-items:center;gap:8px;margin:0 0 24px;text-align:center}}
.brand{{display:inline-flex;align-items:center;gap:10px;font-size:20px;font-weight:720;letter-spacing:-0.04em;margin:0;color:var(--ink)}}
.brand-mark{{width:27px;height:27px;border-radius:9px;background:var(--accent);position:relative;flex:0 0 auto}}
.brand-mark::before{{content:"";position:absolute;width:9px;height:9px;border:2px solid var(--surface);border-radius:50%;left:7px;top:7px}}
.subtitle{{margin:0;font-size:12.5px;color:var(--muted)}}
.card{{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);box-shadow:var(--shadow);padding:24px}}
nav{{display:grid;grid-template-columns:1fr 1fr;gap:4px;padding:4px;background:var(--surface-soft);border-radius:11px;margin-bottom:20px}}
nav a{{display:flex;align-items:center;justify-content:center;min-height:34px;padding:0 12px;border-radius:8px;color:var(--muted);text-decoration:none;font-size:13px;font-weight:500;transition:background-color .15s,color .15s}}
nav a:hover{{color:var(--ink)}}
nav a[aria-current="page"]{{background:var(--surface);color:var(--ink);font-weight:620;box-shadow:var(--tab-shadow)}}
label{{display:block;margin:0 0 15px;font-size:12px;font-weight:600;color:var(--ink)}}
input,select{{display:block;width:100%;min-height:40px;margin-top:6px;padding:8px 12px;border:1px solid var(--line-strong);border-radius:var(--control);background:var(--surface);font:400 14px/1.4 inherit;color:var(--ink)}}
input:focus,select:focus,button:focus-visible,nav a:focus-visible,.lang-switch a:focus-visible{{outline:2px solid var(--blue);outline-offset:2px}}
button{{display:inline-flex;align-items:center;justify-content:center;width:100%;height:40px;margin-top:6px;padding:0 16px;border:1px solid var(--accent);border-radius:var(--control);background:var(--accent);color:var(--accent-ink);font:600 13px/1 inherit;cursor:pointer;transition:background-color .15s}}
button:hover{{background:var(--accent-hover)}}
.notice{{padding:10px 12px;background:var(--notice-bg);border:1px solid var(--notice-border);border-radius:var(--control);color:var(--notice-ink);font-size:12.5px;margin:0 0 16px}}
.lang-switch{{display:flex;justify-content:center;align-items:center;gap:6px;margin-top:16px;font-size:11.5px;color:var(--faint)}}
.lang-switch a{{color:var(--muted);text-decoration:none;padding:2px 7px;border-radius:6px}}
.lang-switch a.active{{color:var(--ink);font-weight:620;background:var(--surface-soft)}}
</style></head><body><main><div class="shell">
<div class="brand-wrap"><h1 class="brand">{brand_html}</h1><p class="subtitle">{copy["subtitle"]}</p></div>
<div class="card">
<nav aria-label="{copy["nav_label"]}"><a href="{login_href}"{login_current}>{copy["tab_login"]}</a><a href="{register_href}"{register_current}>{copy["tab_register"]}</a></nav>
{notice}<form method="post" action="{action}"><input type="hidden" name="csrf" value="{form_token}">{return_field}{fields}<button type="submit">{button}</button></form>
</div>
<div class="lang-switch" aria-label="{copy["switch_label"]}"><a href="{zh_href}"{zh_cls}>中文</a><span>/</span><a href="{en_href}"{en_cls}>EN</a></div>
</div></main>{script}</body></html>"""
    response = HTMLResponse(body, status_code=status_code)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        f"default-src 'none'; script-src 'nonce-{script_nonce}'; style-src 'unsafe-inline'; form-action 'self'; "
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
            return schema == VERSION and (ROOT / 'frontend/dist/index.html').is_file()
        except Exception:
            return False

    ok = await run_in_threadpool(ready)
    return JSONResponse({'status': 'ok' if ok else 'unavailable', 'application': 'goalcal-web'},
                        status_code=200 if ok else 503, headers={'Cache-Control': 'no-store'})


def _is_app_target(next_value: str | None) -> bool:
    if next_value is not None and next_value != "":
        return next_value == "/app/"
    return _root_app_enabled()


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
    app_login = _is_app_target(str(form.get("next")) if form.get("next") is not None else None)
    lang = _request_lang(request)
    if not _valid_form_token(request, form):
        return None, _login_page(
            request,
            _AUTH_COPY[lang]["form_expired"],
            register=register,
            app_login=app_login,
            status_code=403,
        )
    return (form, app_login, lang), None


async def login(request: Request) -> Response:
    if request.method == "GET":
        return _login_page(request, app_login=_is_app_target(request.query_params.get("next")))
    parsed, error_response = await _parse_auth_form(request)
    if error_response is not None:
        return error_response
    form, app_login, lang = parsed
    try:
        token = await run_in_threadpool(
            auth.login, str(form.get("username", "")), str(form.get("password", ""))
        )
    except auth.AuthError as exc:
        return _login_page(request, localize_auth_error(str(exc), lang), app_login=app_login)
    cookie_lang = normalize_lang(request.cookies.get(LANG_COOKIE_NAME))
    if cookie_lang:
        await run_in_threadpool(_apply_login_lang, token, cookie_lang)
    response = RedirectResponse("/app/" if app_login else "/", status_code=303)
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_SECONDS, path="/",
        secure=_secure_cookie(request), httponly=True, samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


async def register(request: Request) -> Response:
    if request.method == "GET":
        return _login_page(request, register=True, app_login=_is_app_target(request.query_params.get("next")))
    parsed, error_response = await _parse_auth_form(request, register=True)
    if error_response is not None:
        return error_response
    form, app_login, lang = parsed
    try:
        await run_in_threadpool(
            auth.create_user,
            str(form.get("username", "")), str(form.get("password", "")),
            str(form.get("tz", "Asia/Shanghai")), str(form.get("invitation", "")),
            email=str(form.get("email", "")) if form.get("email") is not None else None,
        )
    except auth.AuthError as exc:
        return _login_page(request, localize_auth_error(str(exc), lang), register=True, app_login=app_login)
    return _login_page(request, _AUTH_COPY[lang]["created"], app_login=app_login)


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


async def proxy_http(request: Request) -> Response:
    if _root_app_enabled() and request.url.path == "/" and request.method in {"GET", "HEAD"}:
        if _browser_token(request) is None:
            return _login_page(request, app_login=True)
        return RedirectResponse("/app/", status_code=307)
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


from .web.http import api as web_api, frontend as web_frontend


app = Starlette(routes=[
    Route("/_goalcal/health", health, methods=["GET"]),
    Mount("/api/v1", app=web_api),
    Route("/app/{path:path}", web_frontend, methods=["GET"]),
    Route("/app", lambda request: RedirectResponse("/app/", status_code=307)),
    Route("/_goalcal/login", login, methods=["GET", "POST"]),
    Route("/_goalcal/register", register, methods=["GET", "POST"]),
    Route("/_goalcal/logout", logout, methods=["POST"]),
    Route("/_goalcal/lang", set_language, methods=["GET"]),
    Route("/{path:path}", proxy_http, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]),
    WebSocketRoute("/{path:path}", proxy_websocket),
])
