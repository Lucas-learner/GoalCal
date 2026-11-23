"""The public login gateway keeps browser credentials out of URLs and scripts."""

import asyncio
import re
from types import SimpleNamespace
import uuid

import httpx2
import pytest

from dataclasses import replace

from goalcal import auth, db, storage
from goalcal import auth_proxy
from goalcal.auth_proxy import COOKIE_NAME, app


@pytest.fixture(autouse=True)
def _isolated_auth_db(tmp_path, monkeypatch):
    monkeypatch.setenv("GOALCAL_LEGACY_UI", "1")
    path = tmp_path / "auth.db"
    url = f"sqlite:///{path}"
    orig_settings = db.settings
    db.init_db(url)
    monkeypatch.setattr(db, "settings", replace(orig_settings, db_url=url))
    monkeypatch.setattr(storage, "database_path", lambda url=None: path)
    yield
    db.get_engine(orig_settings.db_url)


def _account():
    name = "cookie_" + uuid.uuid4().hex[:12]
    auth.create_user(name, "test-password-long", admin=True)
    return name


def _csrf(page):
    return re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)


def test_websocket_accepts_public_origin_and_uses_internal_origin(monkeypatch):
    public = "https://goalcal.example"

    class BrowserSocket:
        headers = {"origin": public, "host": "goalcal.example"}
        cookies = {COOKIE_NAME: "valid-test-token"}
        url = SimpleNamespace(path="/_stcore/stream", query="")
        scope = {"headers": [(b"host", b"goalcal.example"), (b"origin", public.encode())], "subprotocols": []}

        async def close(self, code):
            self.close_code = code

    browser = BrowserSocket()
    assert auth_proxy._browser_websocket_origin(browser)
    browser.headers = {"origin": "https://attacker.example", "host": "goalcal.example"}
    assert not auth_proxy._browser_websocket_origin(browser)
    browser.headers = {"origin": public, "host": "goalcal.example"}
    monkeypatch.setattr(auth_proxy.auth, "authenticate", lambda token: object())
    forwarded = {}

    def fake_connect(uri, **kwargs):
        forwarded["uri"] = uri
        forwarded["origin"] = kwargs["origin"]
        raise OSError("end test before network access")

    monkeypatch.setattr(auth_proxy.websockets, "connect", fake_connect)
    asyncio.run(auth_proxy.proxy_websocket(browser))
    assert forwarded == {
        "uri": auth_proxy.UPSTREAM_WS + "/_stcore/stream",
        "origin": auth_proxy.UPSTREAM_HTTP,
    }
    assert browser.close_code == 1011


def test_login_cookie_survives_new_client_and_is_revoked_on_logout():
    name = _account()

    async def scenario():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="https://goalcal.example") as client:
            page = await client.get("/", headers={"Accept": "text/html"})
            assert "action=\"/_goalcal/login\"" in page.text
            assert '<html lang="en">' in page.text
            assert "Username" in page.text and "Log in" in page.text
            assert "no-store" == page.headers["cache-control"]
            csrf = _csrf(page)

            rejected = await client.post(
                "/_goalcal/login", data={"username": name, "password": "test-password-long", "csrf": "wrong"},
                headers={"Origin": "https://another.example"},
            )
            assert rejected.status_code == 403

            login = await client.post(
                "/_goalcal/login", data={"username": name, "password": "test-password-long", "csrf": csrf},
                follow_redirects=False,
            )
            assert login.status_code == 303
            cookie = login.headers["set-cookie"]
            assert all(flag in cookie for flag in ("HttpOnly", "Secure", "SameSite=lax", "Max-Age=43200"))
            token = client.cookies.get(COOKIE_NAME)
            assert token and token not in login.headers["location"] and token not in page.text
            assert auth.authenticate(token).username == name

            logout = await client.post(
                "/_goalcal/logout", content=b"unneeded-body" * 400,
                headers={"Origin": "https://goalcal.example"}, follow_redirects=False,
            )
            assert logout.status_code == 303
            assert "Max-Age=0" in logout.headers["set-cookie"]
            with pytest.raises(auth.AuthError):
                auth.authenticate(token)
            page = await client.get("/", headers={"Accept": "text/html"})
            assert "action=\"/_goalcal/login\"" in page.text

    asyncio.run(scenario())


def test_account_disable_invalidates_existing_cookie():
    name = _account()

    async def scenario():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8512") as client:
            page = await client.get("/", headers={"Accept": "text/html"})
            login = await client.post(
                "/_goalcal/login", data={"username": name, "password": "test-password-long", "csrf": _csrf(page)},
                follow_redirects=False,
            )
            assert login.status_code == 303
            assert "Secure" not in login.headers["set-cookie"]
            token = client.cookies.get(COOKIE_NAME)
            auth.manage(name, disabled=True)
            with pytest.raises(auth.AuthError):
                auth.authenticate(token)
            page = await client.get("/", headers={"Accept": "text/html"})
            assert "action=\"/_goalcal/login\"" in page.text
            post = await client.post("/_stcore/upload_file", headers={"Origin": "http://127.0.0.1:8512"})
            assert post.status_code == 401

    asyncio.run(scenario())


def test_invitation_registration_remains_available():
    invitation = auth.invite()
    name = "invite_" + uuid.uuid4().hex[:12]

    async def scenario():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8512") as client:
            page = await client.get("/_goalcal/register")
            assert "Invitation code" in page.text
            registered = await client.post(
                "/_goalcal/register",
                data={"username": name, "password": "test-password-long",
                      "invitation": invitation, "tz": "Asia/Shanghai", "csrf": _csrf(page)},
            )
            assert "Account created. Please log in." in registered.text
            assert auth.login(name, "test-password-long")

    asyncio.run(scenario())


def test_logout_ignores_expired_form_cookie_and_clears_stale_session():
    name = _account()

    async def scenario():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="https://goalcal.example") as client:
            page = await client.get("/", headers={"Accept": "text/html"})
            login = await client.post(
                "/_goalcal/login", data={"username": name, "password": "test-password-long", "csrf": _csrf(page)},
                follow_redirects=False,
            )
            assert login.status_code == 303
            token = client.cookies.get(COOKIE_NAME)

            # form csrf cookie 只有 10 分钟：过期/缺失时 Log out 仍应成功
            client.cookies.delete(auth_proxy.FORM_COOKIE_NAME)
            logout = await client.post(
                "/_goalcal/logout", data={}, follow_redirects=False
            )
            assert logout.status_code == 303
            assert "Max-Age=0" in logout.headers["set-cookie"]
            with pytest.raises(auth.AuthError):
                auth.authenticate(token)

            # session 已撤销/伪造时也清 cookie 并回登录页，不留在错误页。
            stale = await client.post("/_goalcal/logout", data={}, follow_redirects=False)
            assert stale.status_code == 303

            client.cookies.set(COOKIE_NAME, "garbage-token")
            stale = await client.post("/_goalcal/logout", data={}, follow_redirects=False)
            assert stale.status_code == 303
            page = await client.get("/", headers={"Accept": "text/html"})
            assert "action=\"/_goalcal/login\"" in page.text

    asyncio.run(scenario())


def test_login_page_matches_app_shell_style():
    async def scenario():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="https://goalcal.example") as client:
            page = await client.get("/", headers={"Accept": "text/html"})
            assert 'class="brand-mark"' in page.text
            assert "GoalCal" in page.text
            assert "📅" not in page.text
            assert 'aria-current="page">Log in' in page.text
            assert 'href="/_goalcal/register"' in page.text
            register = await client.get("/_goalcal/register")
            assert 'aria-current="page">Create account' in register.text
            assert 'action="/_goalcal/register"' in register.text

    asyncio.run(scenario())


def test_logout_rejects_cross_site_post_without_revoking_session():
    name = _account()

    async def scenario():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="https://goalcal.example") as client:
            page = await client.get("/", headers={"Accept": "text/html"})
            await client.post("/_goalcal/login", data={
                "username": name, "password": "test-password-long", "csrf": _csrf(page),
            })
            token = client.cookies.get(COOKIE_NAME)
            rejected = await client.post("/_goalcal/logout", data={}, headers={
                "Origin": "https://attacker.example", "Sec-Fetch-Site": "cross-site",
            }, follow_redirects=False)
            assert rejected.status_code == 403
            assert auth.authenticate(token).username == name

    asyncio.run(scenario())


def test_register_page_timezone_select_and_nonce_csp():
    import zoneinfo
    all_timezones = sorted(zoneinfo.available_timezones())

    async def scenario():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="https://goalcal.example") as client:
            register = await client.get("/_goalcal/register")
            csp = register.headers.get("content-security-policy", "")
            match = re.search(r"script-src 'nonce-([^']+)'", csp)
            assert match, f"Expected nonce in CSP: {csp}"
            nonce = match.group(1)
            assert f'<script nonce="{nonce}">' in register.text
            assert "Intl.DateTimeFormat().resolvedOptions().timeZone" in register.text
            assert '<select name="tz" required>' in register.text
            assert '<option value="Asia/Shanghai" selected>Asia/Shanghai</option>' in register.text
            for tz in all_timezones[::50]:
                assert f'<option value="{tz}"' in register.text

            # Login page also has nonce in CSP, but no register script or select
            login = await client.get("/", headers={"Accept": "text/html"})
            login_csp = login.headers.get("content-security-policy", "")
            login_match = re.search(r"script-src 'nonce-([^']+)'", login_csp)
            assert login_match
            login_nonce = login_match.group(1)
            assert login_nonce != nonce  # Per-page unique nonce
            assert '<select name="tz"' not in login.text
            assert '<script nonce=' not in login.text

    asyncio.run(scenario())


def test_auth_pages_follow_accept_language_and_cookie_override():
    async def scenario():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="https://goalcal.example") as client:
            zh_login = await client.get("/", headers={"Accept": "text/html", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
            assert zh_login.status_code == 200
            assert '<html lang="zh-CN">' in zh_login.text
            assert "让计划适应你的生活节奏" in zh_login.text
            assert 'aria-current="page">登录</a>' in zh_login.text
            assert "Plans that adapt" not in zh_login.text
            assert "试运行期间" not in zh_login.text

            en_login = await client.get("/", headers={"Accept": "text/html", "Accept-Language": "en-US,en;q=0.9,zh;q=0.5"})
            assert en_login.status_code == 200
            assert '<html lang="en">' in en_login.text
            assert "Plans that adapt to the rhythm of your life" in en_login.text
            assert 'aria-current="page">Log in</a>' in en_login.text
            assert "让计划适应你的生活节奏" not in en_login.text
            assert "During the trial" not in en_login.text

            switched = await client.get("/_goalcal/lang?lang=zh&next=/", follow_redirects=False)
            assert switched.status_code == 303
            assert switched.headers["location"] == "/"
            overridden = await client.get("/", headers={"Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"})
            assert "让计划适应你的生活节奏" in overridden.text

            # Selecting a language on the login page persists it onto the user account on login.
            name = _account()
            await client.get("/_goalcal/lang?lang=en&next=/_goalcal/login?next=/app/", follow_redirects=False)
            page_en = await client.get("/_goalcal/login?next=/app/", headers={"Accept": "text/html"})
            logged = await client.post("/_goalcal/login", data={
                "username": name, "password": "test-password-long", "csrf": _csrf(page_en), "next": "/app/",
            }, follow_redirects=False)
            assert logged.status_code == 303
            token = client.cookies.get(COOKIE_NAME)
            assert token
            from datetime import datetime, timezone
            from goalcal.web.core import Service
            from goalcal.web.http import scoped
            with scoped(token) as (session, principal):
                me = Service(session, principal.user_id, principal.tz, datetime.now(timezone.utc)).me()
                assert me["lang"] == "en"

    asyncio.run(scenario())


def test_auth_pages_support_theme_cookie_and_system_dark_mode():
    async def scenario():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="https://goalcal.example") as client:
            auto_page = await client.get("/", headers={"Accept": "text/html"})
            assert auto_page.status_code == 200
            assert "prefers-color-scheme:dark" in auto_page.text
            assert '<html lang="en">' in auto_page.text

            dark_page = await client.get("/", headers={"Accept": "text/html"}, cookies={"goalcal_theme": "dark"})
            assert dark_page.status_code == 200
            assert '<html lang="en" data-theme="dark">' in dark_page.text

            light_page = await client.get("/", headers={"Accept": "text/html"}, cookies={"goalcal_theme": "light"})
            assert light_page.status_code == 200
            assert '<html lang="en" data-theme="light">' in light_page.text

    asyncio.run(scenario())


def test_login_reuses_form_cookie_across_tabs_and_recovers_from_missing_cookie(monkeypatch):
    monkeypatch.setenv("GOALCAL_ROOT_APP", "1")
    name = _account()

    async def scenario():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="https://goalcal.example") as client:
            tab1 = await client.get("/_goalcal/login", headers={"Accept": "text/html"})
            assert tab1.status_code == 200
            form_cookie = tab1.headers["set-cookie"]
            assert "SameSite=lax" in form_cookie
            assert f"Max-Age={auth_proxy.FORM_COOKIE_SECONDS}" in form_cookie
            tab1_csrf = _csrf(tab1)

            # Opening a second login or register tab must not invalidate tab1's CSRF token.
            tab2 = await client.get("/_goalcal/login?next=/app/", headers={"Accept": "text/html"})
            reg_tab = await client.get("/_goalcal/register?next=/app/", headers={"Accept": "text/html"})
            assert _csrf(tab2) == tab1_csrf
            assert _csrf(reg_tab) == tab1_csrf

            # Missing/expired form cookie on same-origin POST renders a recoverable login form instead of plain-text 403.
            client.cookies.delete(auth_proxy.FORM_COOKIE_NAME)
            expired = await client.post(
                "/_goalcal/login",
                data={"username": name, "password": "test-password-long", "csrf": tab1_csrf},
                headers={"Origin": "https://goalcal.example"},
                follow_redirects=False,
            )
            assert expired.status_code == 403
            assert "action=\"/_goalcal/login\"" in expired.text
            assert "Form session expired. Please try again." in expired.text
            assert client.cookies.get(COOKIE_NAME) is None

            # Resubmitting from the refreshed page succeeds and redirects to the app.
            retried = await client.post(
                "/_goalcal/login",
                data={"username": name, "password": "test-password-long", "csrf": _csrf(expired)},
                headers={"Origin": "https://goalcal.example"},
                follow_redirects=False,
            )
            assert retried.status_code == 303
            assert retried.headers["location"] == "/app/"
            assert auth.authenticate(client.cookies.get(COOKIE_NAME)).username == name

            # Tab1's original CSRF token also remains valid while the browser keeps its form cookie.
            client.cookies.delete(COOKIE_NAME)
            tab1_relogin = await client.get("/_goalcal/login", headers={"Accept": "text/html"})
            await client.get("/_goalcal/register", headers={"Accept": "text/html"})
            tab1_submit = await client.post(
                "/_goalcal/login",
                data={"username": name, "password": "test-password-long", "csrf": _csrf(tab1_relogin)},
                headers={"Origin": "https://goalcal.example"},
                follow_redirects=False,
            )
            assert tab1_submit.status_code == 303
            assert tab1_submit.headers["location"] == "/app/"

    asyncio.run(scenario())
