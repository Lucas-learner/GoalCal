import io
import socket
import time
import pytest
from goalcal import safe_fetch as fetch


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '192.168.1.2', '169.254.169.254', '100.64.0.1', '0.0.0.0', '::1', 'fe80::1', 'fc00::1', '::ffff:8.8.8.8', '224.0.0.1', '192.0.2.1', '64:ff9b::7f00:1', '2001:20::1'])
def test_non_public_addresses(address):
    with pytest.raises(fetch.FetchError):
        fetch.public_address(address)


@pytest.mark.parametrize('url', ['file:///etc/passwd', 'ftp://example.com/a', 'http://localhost:8000/a', 'https://example.com:80/', 'https://user:pass@example.com/'])
def test_url_restrictions(url):
    with pytest.raises(fetch.FetchError):
        fetch.fetch_ics(url)


class Response(io.BytesIO):
    status = 200
    def __init__(self, body=b'calendar', headers=None, status=200):
        super().__init__(body); self.headers = headers or {}; self.status = status
    def getheader(self, key, default=None):
        return self.headers.get(key, default)


def mock_connection(monkeypatch, responses):
    addresses = []
    class Fake:
        sock = None
        def __init__(self, host, port, address, secure, deadline):
            addresses.append((host, address, secure))
        def request(self, *args, **kwargs): pass
        def getresponse(self): return responses.pop(0)
        def close(self): pass
    monkeypatch.setattr(fetch, 'PinnedConnection', Fake)
    return addresses


def test_redirect_revalidates_private_address(monkeypatch):
    mock_connection(monkeypatch, [Response(headers={'Location': 'http://127.0.0.1/private'}, status=302)])
    with pytest.raises(fetch.FetchError):
        fetch.fetch_ics('http://8.8.8.8/calendar')


def test_redirect_limit(monkeypatch):
    mock_connection(monkeypatch, [Response(headers={'Location': '/next'}, status=302) for _ in range(4)])
    with pytest.raises(fetch.FetchError):
        fetch.fetch_ics('http://8.8.8.8/calendar')


def test_size_limit_without_content_length(monkeypatch):
    monkeypatch.setattr(fetch, 'MAX_BYTES', 5)
    mock_connection(monkeypatch, [Response(b'123456')])
    with pytest.raises(fetch.FetchError):
        fetch.fetch_ics('http://8.8.8.8/calendar')


def test_dns_resolution_pinned_and_no_proxy(monkeypatch):
    count = []
    def resolve(host, deadline):
        count.append(host)
        return ['8.8.8.8'] if len(count) == 1 else ['127.0.0.1']
    monkeypatch.setattr(fetch, 'resolve', resolve)
    monkeypatch.setenv('HTTPS_PROXY', 'http://127.0.0.1:1')
    addresses = mock_connection(monkeypatch, [Response()])
    assert fetch.fetch_ics('https://calendar.example/a') == b'calendar'
    assert addresses == [('calendar.example', '8.8.8.8', True)]
    assert count == ['calendar.example']


def test_socket_uses_numeric_address_and_tls_hostname(monkeypatch):
    seen = []
    class Socket:
        def settimeout(self, timeout): pass
        def connect(self, address): seen.append(address)
        def do_handshake(self): pass
    class TLS:
        def wrap_socket(self, sock, server_hostname, do_handshake_on_connect):
            seen.append(server_hostname); return sock
    monkeypatch.setattr(fetch.socket, 'socket', lambda *args: Socket())
    monkeypatch.setattr(fetch.ssl, 'create_default_context', lambda: TLS())
    connection = fetch.PinnedConnection('calendar.example', 443, '8.8.8.8', True, time.monotonic()+15)
    connection.connect()
    assert seen == [('8.8.8.8', 443), 'calendar.example']


def test_mixed_dns_answers_rejected(monkeypatch):
    class Resolver:
        def resolve(self, *args, **kwargs):
            return ['8.8.8.8', '10.0.0.2']
    monkeypatch.setattr(fetch.dns.resolver, 'Resolver', Resolver)
    with pytest.raises(fetch.FetchError):
        fetch.resolve('calendar.example', time.monotonic()+15)


def test_total_deadline(monkeypatch):
    class Slow(Response):
        def read(self, amount):
            time.sleep(0.03)
            return b'a'
    monkeypatch.setattr(fetch, 'TIMEOUT', 0.01)
    mock_connection(monkeypatch, [Slow()])
    with pytest.raises(fetch.FetchError):
        fetch.fetch_ics('http://8.8.8.8/calendar')
