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


def test_public_landing_and_authenticated_root_redirect(monkeypatch, tmp_path):
    monkeypatch.setenv("GOALCAL_ROOT_APP", "1")
    from goalcal import config
    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "intro.html").write_text("<!doctype html><html lang=\"en\"><title>GoalCal intro</title><main>Big goals. One small next step.</main>")
    monkeypatch.setattr(config, "ROOT", tmp_path)

    async def scenario():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="https://goalcal.example") as client:
            page = await client.get("/")
            assert page.status_code == 200
            assert "Big goals. One small next step." in page.text
            assert 'action="/_goalcal/login"' not in page.text
            assert page.headers["cache-control"] == "no-store"
            assert "script-src 'self'" in page.headers["content-security-policy"]

            name = _account()
            login_page = await client.get("/_goalcal/login?next=/app/")
            login = await client.post(
                "/_goalcal/login",
                data={"username": name, "password": "test-password-long", "csrf": _csrf(login_page), "next": "/app/"},
                follow_redirects=False,
            )
            assert login.status_code == 303
            root = await client.get("/", follow_redirects=False)
            assert root.status_code == 307
            assert root.headers["location"] == "/app/"

            client.cookies.clear()
            client.cookies.set(COOKIE_NAME, "invalid-session")
            invalid = await client.get("/")
            assert invalid.status_code == 200
            assert "GoalCal intro" in invalid.text

    asyncio.run(scenario())


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
            page = await client.get("/_goalcal/login?next=/app/", headers={"Accept": "text/html"})
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
            assert all(flag in cookie for flag in ("HttpOnly", "Secure", "SameSite=lax", f"Max-Age={auth.SESSION_SECONDS}"))
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
            page = await client.get("/_goalcal/login?next=/app/", headers={"Accept": "text/html"})
            assert "action=\"/_goalcal/login\"" in page.text

    asyncio.run(scenario())


def test_account_disable_invalidates_existing_cookie():
    name = _account()

    async def scenario():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8512") as client:
            page = await client.get("/_goalcal/login?next=/app/", headers={"Accept": "text/html"})
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
            page = await client.get("/_goalcal/login?next=/app/", headers={"Accept": "text/html"})
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
            page = await client.get("/_goalcal/login?next=/app/", headers={"Accept": "text/html"})
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
            page = await client.get("/_goalcal/login?next=/app/", headers={"Accept": "text/html"})
            assert "action=\"/_goalcal/login\"" in page.text

    asyncio.run(scenario())


def test_login_page_matches_app_shell_style():
    async def scenario():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="https://goalcal.example") as client:
            page = await client.get("/_goalcal/login", headers={"Accept": "text/html"})
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
            page = await client.get("/_goalcal/login?next=/app/", headers={"Accept": "text/html"})
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
            login = await client.get("/_goalcal/login?next=/app/", headers={"Accept": "text/html"})
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
            zh_login = await client.get("/_goalcal/login?next=/app/", headers={"Accept": "text/html", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
            assert zh_login.status_code == 200
            assert '<html lang="zh-CN">' in zh_login.text
            assert "让计划适应你的生活节奏" in zh_login.text
            assert 'aria-current="page">登录</a>' in zh_login.text
            assert "Plans that adapt" not in zh_login.text
            assert "试运行期间" not in zh_login.text

            en_login = await client.get("/_goalcal/login?next=/app/", headers={"Accept": "text/html", "Accept-Language": "en-US,en;q=0.9,zh;q=0.5"})
            assert en_login.status_code == 200
            assert '<html lang="en">' in en_login.text
            assert "Plans that adapt to the rhythm of your life" in en_login.text
            assert 'aria-current="page">Log in</a>' in en_login.text
            assert "让计划适应你的生活节奏" not in en_login.text
            assert "During the trial" not in en_login.text

            switched = await client.get("/_goalcal/lang?lang=zh&next=/", follow_redirects=False)
            assert switched.status_code == 303
            assert switched.headers["location"] == "/"
            overridden = await client.get("/_goalcal/login?next=/app/", headers={"Accept": "text/html", "Accept-Language": "en-US,en;q=0.9"})
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
            auto_page = await client.get("/_goalcal/login?next=/app/", headers={"Accept": "text/html"})
            assert auto_page.status_code == 200
            assert "prefers-color-scheme:dark" in auto_page.text
            assert '<html lang="en">' in auto_page.text

            dark_page = await client.get("/_goalcal/login?next=/app/", headers={"Accept": "text/html"}, cookies={"goalcal_theme": "dark"})
            assert dark_page.status_code == 200
            assert '<html lang="en" data-theme="dark">' in dark_page.text

            light_page = await client.get("/_goalcal/login?next=/app/", headers={"Accept": "text/html"}, cookies={"goalcal_theme": "light"})
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


def test_session_lifetime_and_sliding_renewal(monkeypatch, tmp_path):
    assert auth.SESSION_SECONDS == 30 * 24 * 60 * 60
    assert auth.session_ttl_seconds() == 30 * 24 * 60 * 60

    monkeypatch.setenv("GOALCAL_SESSION_DAYS", "45")
    assert auth.session_ttl_seconds() == 45 * 24 * 60 * 60
    monkeypatch.setenv("GOALCAL_SESSION_DAYS", "999")
    assert auth.session_ttl_seconds() == 365 * 24 * 60 * 60
    monkeypatch.setenv("GOALCAL_SESSION_DAYS", "0")
    assert auth.session_ttl_seconds() == 1 * 24 * 60 * 60
    monkeypatch.setenv("GOALCAL_SESSION_DAYS", "invalid")
    assert auth.session_ttl_seconds() == 30 * 24 * 60 * 60
    monkeypatch.delenv("GOALCAL_SESSION_DAYS", raising=False)

    name = _account()
    dist = tmp_path / "site" / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>GoalCal</title>")
    from goalcal.web import http as web_http
    monkeypatch.setattr(web_http, "ROOT", tmp_path / "site")

    async def scenario():
        import time as _time

        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="https://goalcal.example") as client:
            page = await client.get("/_goalcal/login?next=/app/", headers={"Accept": "text/html"})
            login = await client.post(
                "/_goalcal/login",
                data={"username": name, "password": "test-password-long", "csrf": _csrf(page), "next": "/app/"},
                headers={"Origin": "https://goalcal.example"},
                follow_redirects=False,
            )
            assert login.status_code == 303
            assert f"Max-Age={auth.SESSION_SECONDS}" in login.headers["set-cookie"]
            token = client.cookies.get(COOKIE_NAME)
            assert token

            with storage.connect() as con:
                row = con.execute("SELECT expires FROM auth_sessions WHERE digest=?", (auth.digest(token),)).fetchone()
                initial_exp = float(row["expires"])
            assert initial_exp - _time.time() > 29 * 24 * 60 * 60

            # Fresh session does not emit Set-Cookie on API or app requests.
            me_fresh = await client.get("/api/v1/me")
            assert me_fresh.status_code == 200
            assert "set-cookie" not in me_fresh.headers

            app_fresh = await client.get("/app/")
            assert app_fresh.status_code == 200
            assert "set-cookie" not in app_fresh.headers

            # Simulate session older than 24h: next API request renews DB expiry and refreshes cookie.
            stale_exp = _time.time() + (auth.SESSION_SECONDS - auth.SESSION_RENEW_INTERVAL_SECONDS - 60)
            with storage.connect() as con:
                con.execute("UPDATE auth_sessions SET expires=? WHERE digest=?", (stale_exp, auth.digest(token)))

            me_renewed = await client.get("/api/v1/me")
            assert me_renewed.status_code == 200
            assert f"Max-Age={auth.SESSION_SECONDS}" in me_renewed.headers.get("set-cookie", "")
            with storage.connect() as con:
                new_exp = float(
                    con.execute("SELECT expires FROM auth_sessions WHERE digest=?", (auth.digest(token),)).fetchone()["expires"]
                )
            assert new_exp > stale_exp + 23 * 60 * 60

            # Subsequent immediate request does not renew again.
            me_after = await client.get("/api/v1/me")
            assert me_after.status_code == 200
            assert "set-cookie" not in me_after.headers

            # Frontend entry also triggers sliding renewal when older than 24h.
            with storage.connect() as con:
                con.execute("UPDATE auth_sessions SET expires=? WHERE digest=?", (stale_exp, auth.digest(token)))
            app_renewed = await client.get("/app/goals")
            assert app_renewed.status_code == 200
            assert f"Max-Age={auth.SESSION_SECONDS}" in app_renewed.headers.get("set-cookie", "")

            # Disabled/expired sessions cannot be renewed.
            with storage.connect() as con:
                con.execute("UPDATE auth_sessions SET expires=? WHERE digest=?", (_time.time() - 10, auth.digest(token)))
            assert auth.renew_session_if_needed(token) is None
            auth.manage(name, disabled=True)
            assert auth.renew_session_if_needed(token) is None

    asyncio.run(scenario())


def test_login_preserves_deep_app_next_and_blocks_open_redirects():
    name = _account()

    async def scenario():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="https://goalcal.example") as client:
            # Deep link with query string is preserved across language switch and login.
            deep_login = await client.get("/_goalcal/login?next=/app/calendar%3Fview%3Dweek", headers={"Accept": "text/html"})
            assert deep_login.status_code == 200
            assert 'name="next" value="/app/calendar?view=week"' in deep_login.text
            assert 'href="/_goalcal/register?next=/app/calendar%3Fview%3Dweek"' in deep_login.text

            # Switching language keeps the deep next path intact.
            lang_switch = await client.get(
                "/_goalcal/lang?lang=zh&next=/_goalcal/login%3Fnext%3D%2Fapp%2Fcalendar%253Fview%253Dweek",
                follow_redirects=False,
            )
            assert lang_switch.status_code == 303
            assert lang_switch.headers["location"] == "/_goalcal/login?next=/app/calendar%3Fview%3Dweek"

            logged = await client.post(
                "/_goalcal/login",
                data={
                    "username": name,
                    "password": "test-password-long",
                    "csrf": _csrf(deep_login),
                    "next": "/app/calendar?view=week",
                },
                headers={"Origin": "https://goalcal.example"},
                follow_redirects=False,
            )
            assert logged.status_code == 303
            assert logged.headers["location"] == "/app/calendar?view=week"

            # Open-redirect or traversal payloads are rejected and fall back to '/'.
            for bad_next in ("https://evil.test/app/", "//evil.test/app/", "/app/../secret", "/app/foo\r\nbar"):
                page = await client.get("/_goalcal/login", headers={"Accept": "text/html"})
                bad_resp = await client.post(
                    "/_goalcal/login",
                    data={
                        "username": name,
                        "password": "test-password-long",
                        "csrf": _csrf(page),
                        "next": bad_next,
                    },
                    headers={"Origin": "https://goalcal.example"},
                    follow_redirects=False,
                )
                assert bad_resp.status_code == 303
                assert bad_resp.headers["location"] == "/"

    asyncio.run(scenario())


def test_password_reset_full_flow_revokes_sessions_and_is_one_time(monkeypatch):
    from goalcal import mailer

    monkeypatch.setenv("GOALCAL_PUBLIC_URL", "https://goalcal.example")
    monkeypatch.setattr(mailer, "smtp_capabilities", lambda: {"configured": True, "from_address": "noreply@goalcal.example"})
    sent_messages = []
    monkeypatch.setattr(mailer, "send_email", lambda **kwargs: sent_messages.append(kwargs))

    name = "pwuser_" + uuid.uuid4().hex[:8]
    email = f"{name}@example.com"
    auth.create_user(name, "old-password-123", admin=True, email=email)

    async def scenario():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="https://goalcal.example") as client:
            login_page = await client.get("/_goalcal/login", headers={"Accept": "text/html"})
            assert "/_goalcal/forgot-password" in login_page.text
            logged = await client.post(
                "/_goalcal/login",
                data={"username": name, "password": "old-password-123", "csrf": _csrf(login_page)},
                follow_redirects=False,
            )
            assert logged.status_code == 303
            old_session = client.cookies.get(COOKIE_NAME)
            assert old_session and auth.authenticate(old_session).username == name

            forgot_page = await client.get("/_goalcal/forgot-password?next=/app/goals")
            assert forgot_page.status_code == 200
            assert "Reset your password" in forgot_page.text
            forgot_resp = await client.post(
                "/_goalcal/forgot-password",
                data={"email": email.upper(), "next": "/app/goals", "csrf": _csrf(forgot_page)},
            )
            assert forgot_resp.status_code == 200
            assert "valid for 30 minutes" in forgot_resp.text
            assert len(sent_messages) == 1
            assert sent_messages[0]["to_address"] == email

            match = re.search(r"https://goalcal\.example/_goalcal/reset-password\?token=([^\s\"&]+)", sent_messages[0]["text_body"])
            assert match is not None
            token = match.group(1)

            reset_get = await client.get(f"/_goalcal/reset-password?token={token}&next=/app/goals")
            assert reset_get.status_code == 200
            assert 'name="confirm_password"' in reset_get.text

            mismatch = await client.post(
                "/_goalcal/reset-password",
                data={
                    "token": token,
                    "password": "new-password-456",
                    "confirm_password": "different-password",
                    "next": "/app/goals",
                    "csrf": _csrf(reset_get),
                },
            )
            assert mismatch.status_code == 200
            assert "Passwords do not match" in mismatch.text

            done_resp = await client.post(
                "/_goalcal/reset-password",
                data={
                    "token": token,
                    "password": "new-password-456",
                    "confirm_password": "new-password-456",
                    "next": "/app/goals",
                    "csrf": _csrf(mismatch),
                },
            )
            assert done_resp.status_code == 200
            assert "Your password has been updated" in done_resp.text

            # Old session is revoked and old password fails; new password works; token cannot be reused.
            with pytest.raises(auth.AuthError):
                auth.authenticate(old_session)
            with pytest.raises(auth.AuthError):
                auth.login(name, "old-password-123")
            new_token = auth.login(name, "new-password-456")
            assert auth.authenticate(new_token).username == name

            reused = await client.get(f"/_goalcal/reset-password?token={token}")
            assert "invalid or has expired" in reused.text

    asyncio.run(scenario())


def test_password_reset_anti_enumeration_rate_limit_expiry_and_smtp_unavailable(monkeypatch):
    import time as _time
    from goalcal import mailer

    sent_messages = []
    monkeypatch.setattr(mailer, "send_email", lambda **kwargs: sent_messages.append(kwargs))

    active_email = f"active_{uuid.uuid4().hex[:8]}@example.com"
    disabled_email = f"disabled_{uuid.uuid4().hex[:8]}@example.com"
    auth.create_user("active_" + uuid.uuid4().hex[:6], "pw-long-1", admin=True, email=active_email)
    dis_name = "dis_" + uuid.uuid4().hex[:6]
    auth.create_user(dis_name, "pw-long-2", admin=True, email=disabled_email)
    auth.manage(dis_name, disabled=True)

    async def scenario():
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="https://goalcal.example") as client:
            # 1. SMTP unconfigured returns friendly notice.
            monkeypatch.setattr(mailer, "smtp_capabilities", lambda: {"configured": False, "from_address": ""})
            unconf = await client.get("/_goalcal/forgot-password")
            assert "not configured" in unconf.text

            # 2. Unknown and disabled emails yield identical response without sending email.
            monkeypatch.setattr(mailer, "smtp_capabilities", lambda: {"configured": True, "from_address": "bot@goalcal.example"})
            fp = await client.get("/_goalcal/forgot-password")
            csrf = _csrf(fp)
            unknown_resp = await client.post("/_goalcal/forgot-password", data={"email": "nobody@example.com", "csrf": csrf})
            disabled_resp = await client.post("/_goalcal/forgot-password", data={"email": disabled_email, "csrf": csrf})
            assert "If an account is linked to that email" in unknown_resp.text
            assert "If an account is linked to that email" in disabled_resp.text
            assert sent_messages == []

            # 3. Rate limit triggers after 3 requests for the same email.
            for _ in range(3):
                ok_resp = await client.post("/_goalcal/forgot-password", data={"email": active_email, "csrf": csrf})
                assert "If an account is linked to that email" in ok_resp.text
            limited = await client.post("/_goalcal/forgot-password", data={"email": active_email, "csrf": csrf})
            assert "Too many password reset requests" in limited.text

            # 4. Expired token is rejected.
            with storage.connect() as con:
                con.execute("UPDATE password_reset_tokens SET expires=?", (_time.time() - 5,))
            token_match = re.search(r"\?token=([^\s\"&]+)", sent_messages[-1]["text_body"])
            assert token_match is not None
            expired_get = await client.get(f"/_goalcal/reset-password?token={token_match.group(1)}")
            assert "invalid or has expired" in expired_get.text

    asyncio.run(scenario())


def test_migrate_v14_to_v15_creates_password_reset_tokens(tmp_path):
    import sqlite3
    from sqlalchemy import create_engine
    from goalcal import migrations

    db_path = tmp_path / "v14.db"
    with sqlite3.connect(db_path) as con:
        con.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, email TEXT, password_hash TEXT, disabled BOOLEAN NOT NULL DEFAULT 0, name TEXT NOT NULL DEFAULT 'demo', tz TEXT NOT NULL DEFAULT 'Asia/Shanghai', created_at DATETIME);
            CREATE TABLE goals (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), draft_id TEXT, title TEXT, outcome TEXT, deadline DATE, status TEXT, pinned BOOLEAN NOT NULL DEFAULT 0, created_at DATETIME, template_key TEXT, outline TEXT);
            CREATE TABLE web_executions (id VARCHAR(32) NOT NULL PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), event_id INTEGER NOT NULL, step_id INTEGER, habit_occurrence_id VARCHAR(40), status VARCHAR(20) NOT NULL, started_at DATETIME NOT NULL, expected_end_at DATETIME NOT NULL, resolved_at DATETIME, version INTEGER NOT NULL);
            CREATE TABLE habits (id INTEGER PRIMARY KEY, pinned BOOLEAN NOT NULL DEFAULT 0);
            CREATE TABLE events (id INTEGER PRIMARY KEY, sync_source_id INTEGER, all_day BOOLEAN NOT NULL DEFAULT 0);
            INSERT INTO users (id, username, email, name, tz) VALUES (1, 'bob', 'bob@example.com', 'Bob', 'Asia/Shanghai');
            PRAGMA user_version = 14;
            """
        )

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        migrations.migrate(engine)
    finally:
        engine.dispose()

    with sqlite3.connect(db_path) as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == migrations.VERSION
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "password_reset_tokens" in tables
        assert con.execute("SELECT email FROM users WHERE id=1").fetchone()[0] == "bob@example.com"
