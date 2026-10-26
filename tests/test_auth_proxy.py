"""The public login gateway keeps browser credentials out of URLs and scripts."""

import asyncio
import re
from types import SimpleNamespace
import uuid

import httpx2
import pytest

from goalcal import auth, db, storage
from goalcal import auth_proxy
from goalcal.auth_proxy import COOKIE_NAME, app


@pytest.fixture(autouse=True)
def _isolated_auth_db(tmp_path, monkeypatch):
    path = tmp_path / "auth.db"
    db.init_db(f"sqlite:///{path}")
    monkeypatch.setattr(storage, "database_path", lambda url=None: path)
    yield
    db.get_engine()


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
                "/_goalcal/logout", data={"csrf": csrf},
                follow_redirects=False,
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
