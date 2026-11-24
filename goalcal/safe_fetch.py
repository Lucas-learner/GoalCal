"""Bounded public HTTP(S) fetch, pinned DNS, verified TLS, no environment proxy."""
from dataclasses import dataclass
import gzip
import http.client
import ipaddress
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit, urljoin
import dns.resolver

MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = 15


class FetchError(ValueError):
    pass


@dataclass(frozen=True)
class FetchResponse:
    content: bytes | None
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None


def normalize_ics_url(url: str) -> str:
    """Normalize webcal(s):// to https:// and validate standard HTTP(S) syntax."""
    cleaned = (url or '').strip()
    lowered = cleaned.lower()
    if lowered.startswith('webcal://'):
        cleaned = 'https://' + cleaned[len('webcal://'):]
    elif lowered.startswith('webcals://'):
        cleaned = 'https://' + cleaned[len('webcals://'):]
    parts = urlsplit(cleaned)
    try:
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        host = (parts.hostname or '').encode('idna').decode('ascii')
    except (ValueError, UnicodeError):
        raise FetchError('Invalid URL') from None
    if (parts.scheme not in ('http', 'https') or not host or parts.username is not None
            or parts.password is not None or port != (443 if parts.scheme == 'https' else 80)
            or any(ord(c) < 33 or ord(c) == 127 for c in cleaned) or '\\' in cleaned):
        raise FetchError('Only standard HTTP/HTTPS URLs are supported')
    try:
        public_address(str(ipaddress.ip_address(host)))
    except ValueError as exc:
        if isinstance(exc, FetchError):
            raise
    return cleaned


def public_address(value):
    address = ipaddress.ip_address(value)
    if address.version == 6 and (address not in ipaddress.ip_network('2000::/3')
                                 or address in ipaddress.ip_network('2001::/23')):
        raise FetchError('IPv6 transition and special-use ranges are not allowed')
    if (not address.is_global or address.is_multicast or address.is_unspecified
            or address.is_loopback or address.is_link_local
            or getattr(address, 'ipv4_mapped', None) is not None
            or getattr(address, 'sixtofour', None) is not None
            or getattr(address, 'teredo', None) is not None):
        raise FetchError('Only public addresses are allowed')
    return str(address)


def resolve(host, deadline):
    try:
        return [public_address(str(ipaddress.ip_address(host))) ]
    except ValueError as exc:
        if isinstance(exc, FetchError):
            raise
    resolver = dns.resolver.Resolver()
    addresses = []
    for kind in ('A', 'AAAA'):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchError('Download deadline exceeded')
        try:
            answers = resolver.resolve(host, kind, lifetime=remaining, search=False)
        except dns.resolver.NoAnswer:
            continue
        addresses.extend(public_address(str(answer)) for answer in answers)
    if not addresses:
        raise FetchError('No public address')
    return addresses


class PinnedConnection(http.client.HTTPConnection):
    def __init__(self, host, port, address, secure, deadline):
        super().__init__(host, port, timeout=max(0.01, deadline-time.monotonic()))
        self.address, self.secure, self.deadline = address, secure, deadline

    def connect(self):
        family = socket.AF_INET6 if ':' in self.address else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        self.sock = sock
        self.active_socket = sock
        sock.settimeout(max(0.01, self.deadline-time.monotonic()))
        # Numeric sockaddr: never resolve the hostname a second time.
        sock.connect((self.address, self.port))
        if self.secure:
            self.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host, do_handshake_on_connect=False)
            self.active_socket = self.sock
            self.sock.do_handshake()


def fetch_ics_conditional(
    url: str,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    allow_webcal: bool = True,
) -> FetchResponse:
    if allow_webcal:
        url = normalize_ics_url(url)
    deadline = time.monotonic() + TIMEOUT
    for redirect in range(4):
        parts = urlsplit(url)
        try:
            port = parts.port or (443 if parts.scheme == 'https' else 80)
            host = (parts.hostname or '').encode('idna').decode('ascii')
        except (ValueError, UnicodeError):
            raise FetchError('Invalid URL') from None
        if (parts.scheme not in ('http', 'https') or not host or parts.username is not None
                or parts.password is not None or port != (443 if parts.scheme == 'https' else 80)
                or any(ord(c) < 33 or ord(c) == 127 for c in url) or '\\' in url):
            raise FetchError('Only standard HTTP/HTTPS URLs are supported')
        addresses = resolve(host, deadline)
        connection = PinnedConnection(host, port, addresses[0], parts.scheme == 'https', deadline)
        def abort(connection=connection):
            active = getattr(connection, "active_socket", connection.sock)
            if active:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()
        timer = threading.Timer(max(0.001, deadline-time.monotonic()), abort)
        timer.daemon = True
        timer.start()
        try:
            target = parts.path or '/'
            if parts.query:
                target += '?' + parts.query
            headers = {
                'Host': f'[{host}]' if ':' in host else host,
                'Accept-Encoding': 'identity',
                'User-Agent': 'GoalCal/1',
            }
            if redirect == 0:
                if etag:
                    headers['If-None-Match'] = etag
                if last_modified:
                    headers['If-Modified-Since'] = last_modified
            connection.request('GET', target, headers=headers)
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if redirect == 3 or not location:
                    raise FetchError('Redirect limit exceeded')
                url = urljoin(url, location)
                continue
            if response.status == 304:
                return FetchResponse(
                    content=None,
                    not_modified=True,
                    etag=response.getheader('ETag') or etag,
                    last_modified=response.getheader('Last-Modified') or last_modified,
                )
            if response.status != 200 or response.getheader('Content-Encoding', 'identity') != 'identity':
                raise FetchError('Unsupported response')
            length = response.getheader('Content-Length')
            if length and int(length) > MAX_BYTES:
                raise FetchError('Calendar exceeds 5 MB')
            output = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise FetchError('Download deadline exceeded')
                chunk = response.read(min(65536, MAX_BYTES + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > MAX_BYTES:
                    raise FetchError('Calendar exceeds 5 MB')
            if time.monotonic() >= deadline:
                raise FetchError('Download deadline exceeded')
            return FetchResponse(
                content=bytes(output),
                not_modified=False,
                etag=response.getheader('ETag'),
                last_modified=response.getheader('Last-Modified'),
            )
        except (OSError, http.client.HTTPException, ValueError) as exc:
            if isinstance(exc, FetchError):
                raise
            raise FetchError('Calendar download failed or exceeded limits') from None
        finally:
            timer.cancel()
            connection.close()
    raise FetchError('Redirect limit exceeded')


def normalize_caldav_url(url: str) -> str:
    """Validate an HTTPS public CalDAV base URL (no embedded credentials or custom ports)."""
    cleaned = (url or '').strip()
    parts = urlsplit(cleaned)
    try:
        port = parts.port or (443 if parts.scheme == 'https' else 80)
        host = (parts.hostname or '').encode('idna').decode('ascii')
    except (ValueError, UnicodeError):
        raise FetchError('Invalid URL') from None
    if (parts.scheme != 'https' or not host or parts.username is not None
            or parts.password is not None or port != 443
            or any(ord(c) < 33 or ord(c) == 127 for c in cleaned) or '\\' in cleaned):
        raise FetchError('Only standard HTTPS CalDAV URLs are supported')
    try:
        public_address(str(ipaddress.ip_address(host)))
    except ValueError as exc:
        if isinstance(exc, FetchError):
            raise
    return cleaned


def safe_http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = TIMEOUT,
    max_bytes: int = MAX_BYTES,
    follow_redirects: int = 3,
    require_https: bool = True,
) -> tuple[int, dict[str, str], bytes]:
    """Perform a bounded SSRF-safe HTTP(S) request using pinned DNS resolution."""
    deadline = time.monotonic() + timeout
    req_method = method.upper()
    req_body = body
    req_headers = dict(headers or {})
    cur_url = url
    initial_origin: tuple[str, str, int] | None = None

    for redirect in range(follow_redirects + 1):
        parts = urlsplit(cur_url)
        try:
            port = parts.port or (443 if parts.scheme == 'https' else 80)
            host = (parts.hostname or '').encode('idna').decode('ascii')
        except (ValueError, UnicodeError):
            raise FetchError('Invalid URL') from None
        allowed_schemes = ('https',) if require_https else ('http', 'https')
        expected_port = 443 if parts.scheme == 'https' else 80
        if (parts.scheme not in allowed_schemes or not host or parts.username is not None
                or parts.password is not None or port != expected_port
                or any(ord(c) < 33 or ord(c) == 127 for c in cur_url) or '\\' in cur_url):
            raise FetchError('Only standard HTTPS URLs are supported' if require_https else 'Only standard HTTP/HTTPS URLs are supported')

        cur_origin = (parts.scheme, host.lower(), port)
        if initial_origin is None:
            initial_origin = cur_origin

        addresses = resolve(host, deadline)
        connection = PinnedConnection(host, port, addresses[0], parts.scheme == 'https', deadline)

        def abort(connection=connection):
            active = getattr(connection, 'active_socket', connection.sock)
            if active:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                connection.close()

        timer = threading.Timer(max(0.001, deadline - time.monotonic()), abort)
        timer.daemon = True
        timer.start()
        try:
            target = parts.path or '/'
            if parts.query:
                target += '?' + parts.query
            send_headers = {
                'Host': f'[{host}]' if ':' in host else host,
                'Accept-Encoding': 'identity',
                'User-Agent': 'GoalCal/2',
            }
            for k, v in req_headers.items():
                lk = k.lower()
                if lk in {'host', 'accept-encoding', 'content-length'}:
                    continue
                if lk == 'authorization' and cur_origin != initial_origin:
                    same_icloud = (
                        initial_origin[0] == cur_origin[0] == 'https'
                        and initial_origin[1].endswith('.icloud.com')
                        and cur_origin[1].endswith('.icloud.com')
                    )
                    if not same_icloud:
                        continue
                send_headers[k] = v
            if req_body is not None:
                send_headers['Content-Length'] = str(len(req_body))
            connection.request(req_method, target, body=req_body, headers=send_headers)
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if redirect >= follow_redirects or not location:
                    raise FetchError('Redirect limit exceeded')
                cur_url = urljoin(cur_url, location)
                if response.status == 303 and req_method not in {'GET', 'HEAD'}:
                    req_method = 'GET'
                    req_body = None
                continue

            length = response.getheader('Content-Length')
            if length and int(length) > max_bytes:
                raise FetchError('Response exceeds size limit')
            output = bytearray()
            while True:
                if time.monotonic() >= deadline:
                    raise FetchError('Request deadline exceeded')
                chunk = response.read(min(65536, max_bytes + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > max_bytes:
                    raise FetchError('Response exceeds size limit')
            if time.monotonic() >= deadline:
                raise FetchError('Request deadline exceeded')
            raw = bytes(output)
            encoding = (response.getheader('Content-Encoding') or 'identity').strip().lower()
            if encoding == 'gzip':
                raw = gzip.decompress(raw)
                if len(raw) > max_bytes:
                    raise FetchError('Response exceeds size limit')
            elif encoding != 'identity':
                raise FetchError('Unsupported response encoding')
            resp_headers = {k.lower(): v for k, v in response.getheaders()}
            return response.status, resp_headers, raw
        except (OSError, http.client.HTTPException, ValueError) as exc:
            if isinstance(exc, FetchError):
                raise
            raise FetchError('Remote calendar request failed or exceeded limits') from None
        finally:
            timer.cancel()
            connection.close()
    raise FetchError('Redirect limit exceeded')


def fetch_ics(url):
    result = fetch_ics_conditional(url, allow_webcal=False)
    assert result.content is not None
    return result.content
