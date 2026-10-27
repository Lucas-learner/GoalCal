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

import httpx2
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route, WebSocketRoute
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


def english_auth_error(message: str) -> str:
    return {
        "用户名须为 3–32 个英文字母、数字或下划线": "Username must be 3–32 letters, numbers, or underscores.",
        "密码须为 12–256 个字符": "Password must be 12–256 characters.",
        "无效的时区": "Invalid time zone.",
        "邀请码无效、已使用或已过期": "Invitation code is invalid, used, or expired.",
        "用户名不可用": "Username is unavailable.",
        "登录尝试过多，请在 15 分钟后重试": "Too many login attempts. Try again in 15 minutes.",
        "用户名或密码错误": "Incorrect username or password.",
    }.get(message, message)


def _login_page(request: Request, message: str = "", *, register: bool = False) -> HTMLResponse:
    form_token = secrets.token_urlsafe(32)
    notice = f'<p class="notice" role="alert">{html.escape(message)}</p>' if message else ""
    if register:
        fields = """<label>Username<input name="username" autocomplete="username" required></label>
<label>Password (at least 12 characters)<input name="password" type="password" autocomplete="new-password" required></label>
<label>Invitation code<input name="invitation" required></label>
<label>Time zone<input name="tz" value="Asia/Shanghai" required></label>"""
        heading, action, button = "Create account", "/_goalcal/register", "Register"
    else:
        fields = """<label>Username<input name="username" autocomplete="username" required></label>
<label>Password<input name="password" type="password" autocomplete="current-password" required></label>"""
        heading, action, button = "Log in", "/_goalcal/login", "Log in"
    body = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GoalCal</title><style>
body{{font:16px system-ui,sans-serif;background:#f8fafc;color:#172033;margin:0}}
main{{max-width:420px;margin:10vh auto;padding:32px;background:white;border:1px solid #e6eaf0;border-radius:12px}}
h1{{font-size:28px;margin:0 0 20px}}nav{{display:flex;gap:18px;margin-bottom:22px}}
a{{color:#176c69}}label{{display:block;margin:14px 0;font-weight:600}}
input{{box-sizing:border-box;display:block;width:100%;padding:10px;margin-top:6px;border:1px solid #cbd5df;border-radius:6px;font:inherit}}
button{{width:100%;padding:11px;border:0;border-radius:6px;background:#176c69;color:white;font:inherit;cursor:pointer}}
.notice{{padding:10px;background:#fff3e6;border-radius:6px}}.disclosure{{font-size:12px;color:#64748b;line-height:1.5;margin-top:24px}}
</style></head><body><main><h1>📆 GoalCal</h1><nav><a href="/">Log in</a><a href="/_goalcal/register">Create account</a></nav>
<h2>{heading}</h2>{notice}<form method="post" action="{action}"><input type="hidden" name="csrf" value="{form_token}">{fields}<button type="submit">{button}</button></form>
<p class="disclosure">During the trial, we log action types, timing, and errors to improve the app. Passwords, chat messages, and calendar details are not automatically added to runtime logs.</p>
</main></body></html>"""
    response = HTMLResponse(body)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
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


async def login(request: Request) -> Response:
    if not await _small_form(request):
        return PlainTextResponse("Invalid form submission", status_code=403)
    form = await request.form()
    if not _valid_form_token(request, form):
        return PlainTextResponse("Invalid form submission", status_code=403)
    try:
        token = auth.login(str(form.get("username", "")), str(form.get("password", "")))
    except auth.AuthError as exc:
        return _login_page(request, english_auth_error(str(exc)))
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_SECONDS, path="/",
        secure=_secure_cookie(request), httponly=True, samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


async def register(request: Request) -> Response:
    if request.method == "GET":
        return _login_page(request, register=True)
    if not await _small_form(request):
        return PlainTextResponse("Invalid form submission", status_code=403)
    form = await request.form()
    if not _valid_form_token(request, form):
        return PlainTextResponse("Invalid form submission", status_code=403)
    try:
        auth.create_user(
            str(form.get("username", "")), str(form.get("password", "")),
            str(form.get("tz", "Asia/Shanghai")), str(form.get("invitation", "")),
        )
    except auth.AuthError as exc:
        return _login_page(request, english_auth_error(str(exc)), register=True)
    return _login_page(request, "Account created. Please log in.")


async def logout(request: Request) -> Response:
    # 登出不读取表单正文或依赖 10 分钟的登录 CSRF cookie；始终清掉浏览器 session。
    if not _logout_origin_allowed(request):
        return PlainTextResponse("Invalid request origin", status_code=403)
    token = _browser_token(request)
    if token is not None:
        auth.logout(token)
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


async def proxy_http(request: Request) -> Response:
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


app = Starlette(routes=[
    Route("/_goalcal/login", login, methods=["POST"]),
    Route("/_goalcal/register", register, methods=["GET", "POST"]),
    Route("/_goalcal/logout", logout, methods=["POST"]),
    Route("/{path:path}", proxy_http, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]),
    WebSocketRoute("/{path:path}", proxy_websocket),
])
