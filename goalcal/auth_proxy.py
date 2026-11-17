"""Same-origin login gateway for the local Streamlit app.

The gateway owns the HttpOnly browser cookie. Streamlit only reads the cookie on
new connections; it never sends a session token into page JavaScript or a URL.
"""

from __future__ import annotations

import asyncio
import html
import os
import secrets
from urllib.parse import urlsplit
import zoneinfo

import httpx2
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route, WebSocketRoute, Mount
from starlette.websockets import WebSocket, WebSocketDisconnect

from . import auth

COOKIE_NAME = "goalcal_session"
FORM_COOKIE_NAME = "goalcal_form"
SESSION_SECONDS = 12 * 60 * 60
UPSTREAM_PORT = int(os.getenv("GOALCAL_UPSTREAM_PORT", "8502"))
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


def _login_page(request: Request, message: str = "", *, register: bool = False, app_login: bool = False) -> HTMLResponse:
    form_token = secrets.token_urlsafe(32)
    script_nonce = secrets.token_urlsafe(32)
    notice = f'<p class="notice" role="alert">{html.escape(message)}</p>' if message else ""
    open_reg = auth.open_registration_enabled()
    if register:
        extra = (
            '<label>Email<input name="email" type="email" autocomplete="email" required></label>'
            if open_reg
            else '<label>Invitation code<input name="invitation" required></label>'
        )
        fields = f"""<label>Username<input name="username" autocomplete="username" required></label>
{extra}
<label>Password (at least 6 characters)<input name="password" type="password" autocomplete="new-password" required></label>
<label>Time zone<select name="tz" required>{_TZ_OPTIONS_HTML}</select></label>"""
        action, button = "/_goalcal/register", "Register"
        login_current, register_current = "", ' aria-current="page"'
        script = _DETECT_TZ_SCRIPT.format(nonce=script_nonce)
    else:
        user_label = "Username or email" if open_reg else "Username"
        fields = f"""<label>{user_label}<input name="username" autocomplete="username" required></label>
<label>Password<input name="password" type="password" autocomplete="current-password" required></label>"""
        action, button = "/_goalcal/login", "Log in"
        login_current, register_current = ' aria-current="page"', ""
        script = ""
    return_field = '<input type="hidden" name="next" value="/app/">' if app_login else ""
    login_href = "/_goalcal/login?next=/app/" if app_login else "/"
    register_href = "/_goalcal/register?next=/app/" if app_login else "/_goalcal/register"
    brand_html = '<span class="brand-mark" aria-hidden="true"></span><span>GoalCal</span>'
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GoalCal</title><style>
:root{{--bg:#fafaf8;--surface:#fffefd;--surface-soft:#f4f4f1;--ink:#1d1d1f;--muted:#6e6e73;--faint:#a1a1a6;--line:rgba(29,29,31,0.08);--line-strong:rgba(29,29,31,0.16);--accent:#303638;--blue:#6b7f89;--radius:13px;--control:9px;--shadow:0 1px 2px rgba(20,22,24,0.02),0 8px 24px rgba(20,22,24,0.025)}}
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
nav a[aria-current="page"]{{background:var(--surface);color:var(--ink);font-weight:620;box-shadow:0 1px 2px rgba(20,22,24,0.06)}}
label{{display:block;margin:0 0 15px;font-size:12px;font-weight:600;color:var(--ink)}}
input,select{{display:block;width:100%;min-height:40px;margin-top:6px;padding:8px 12px;border:1px solid var(--line-strong);border-radius:var(--control);background:var(--surface);font:400 14px/1.4 inherit;color:var(--ink)}}
input:focus,select:focus,button:focus-visible,nav a:focus-visible{{outline:2px solid var(--blue);outline-offset:2px}}
button{{display:inline-flex;align-items:center;justify-content:center;width:100%;height:40px;margin-top:6px;padding:0 16px;border:1px solid var(--accent);border-radius:var(--control);background:var(--accent);color:#fff;font:600 13px/1 inherit;cursor:pointer;transition:background-color .15s}}
button:hover{{background:#23282a}}
.notice{{padding:10px 12px;background:#fdf3e7;border:1px solid rgba(180,120,40,0.2);border-radius:var(--control);color:#5c3d15;font-size:12.5px;margin:0 0 16px}}
.disclosure{{font-size:11.5px;color:var(--faint);line-height:1.6;margin:16px 0 0;text-align:center}}
</style></head><body><main><div class="shell">
<div class="brand-wrap"><h1 class="brand">{brand_html}</h1><p class="subtitle">让计划适应你的生活节奏</p></div>
<div class="card">
<nav aria-label="Authentication"><a href="{login_href}"{login_current}>Log in</a><a href="{register_href}"{register_current}>Create account</a></nav>
{notice}<form method="post" action="{action}"><input type="hidden" name="csrf" value="{form_token}">{return_field}{fields}<button type="submit">{button}</button></form>
</div>
<p class="disclosure">During the trial, we log action types, timing, and errors to improve the app. Passwords, chat messages, and calendar details are not automatically added to runtime logs.</p>
</div></main>{script}</body></html>"""
    response = HTMLResponse(body)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        f"default-src 'none'; script-src 'nonce-{script_nonce}'; style-src 'unsafe-inline'; form-action 'self'; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.set_cookie(
        FORM_COOKIE_NAME, form_token, max_age=600, path="/",
        secure=_secure_cookie(request), httponly=True, samesite="strict",
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


def _logout_origin_allowed(request: Request) -> bool:
    """Reject cross-site logout forms while allowing clients without Origin."""
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
    return os.getenv("GOALCAL_ROOT_APP", "").strip().lower() in {"1", "true", "yes", "on"}


def _is_app_target(next_value: str | None) -> bool:
    if next_value is not None and next_value != "":
        return next_value == "/app/"
    return _root_app_enabled()


async def login(request: Request) -> Response:
    if request.method == "GET":
        return _login_page(request, app_login=_is_app_target(request.query_params.get("next")))
    if not await _small_form(request):
        return PlainTextResponse("Invalid form submission", status_code=403)
    form = await request.form()
    if not _valid_form_token(request, form):
        return PlainTextResponse("Invalid form submission", status_code=403)
    app_login = _is_app_target(str(form.get("next")) if form.get("next") is not None else None)
    try:
        token = auth.login(str(form.get("username", "")), str(form.get("password", "")))
    except auth.AuthError as exc:
        return _login_page(request, english_auth_error(str(exc)), app_login=app_login)
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
    if not await _small_form(request):
        return PlainTextResponse("Invalid form submission", status_code=403)
    form = await request.form()
    if not _valid_form_token(request, form):
        return PlainTextResponse("Invalid form submission", status_code=403)
    app_login = _is_app_target(str(form.get("next")) if form.get("next") is not None else None)
    try:
        auth.create_user(
            str(form.get("username", "")), str(form.get("password", "")),
            str(form.get("tz", "Asia/Shanghai")), str(form.get("invitation", "")),
            email=str(form.get("email", "")) if form.get("email") is not None else None,
        )
    except auth.AuthError as exc:
        return _login_page(request, english_auth_error(str(exc)), register=True, app_login=app_login)
    return _login_page(request, "Account created. Please log in.", app_login=app_login)


async def logout(request: Request) -> Response:
    # 登出不读取表单正文或依赖 10 分钟的登录 CSRF cookie；始终清掉浏览器 session。
    if not _logout_origin_allowed(request):
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
    Mount("/api/v1", app=web_api),
    Route("/app/{path:path}", web_frontend, methods=["GET"]),
    Route("/app", lambda request: RedirectResponse("/app/", status_code=307)),
    Route("/_goalcal/login", login, methods=["GET", "POST"]),
    Route("/_goalcal/register", register, methods=["GET", "POST"]),
    Route("/_goalcal/logout", logout, methods=["POST"]),
    Route("/{path:path}", proxy_http, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]),
    WebSocketRoute("/{path:path}", proxy_websocket),
])
