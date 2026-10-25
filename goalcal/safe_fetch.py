"""Bounded public HTTP(S) fetch, pinned DNS, verified TLS, no environment proxy."""
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


def fetch_ics(url):
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
            connection.request('GET', target, headers={'Host': f'[{host}]' if ':' in host else host,
                               'Accept-Encoding': 'identity', 'User-Agent': 'GoalCal/1'})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader('Location')
                if redirect == 3 or not location:
                    raise FetchError('Redirect limit exceeded')
                url = urljoin(url, location)
                continue
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
            return bytes(output)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise FetchError('Calendar download failed or exceeded limits') from None
        finally:
            timer.cancel()
            connection.close()
    raise FetchError('Redirect limit exceeded')
