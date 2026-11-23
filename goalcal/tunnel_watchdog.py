"""Local Cloudflare connection watchdog; never relies on the public DNS/proxy."""
import math
import time
from urllib.request import ProxyHandler, build_opener


METRICS_ADDRESS = '127.0.0.1:20241'


def tunnel_connected():
    # macOS/system proxies must not intercept the loopback probe.
    opener = build_opener(ProxyHandler({}))
    with opener.open(f'http://{METRICS_ADDRESS}/metrics', timeout=3) as response:
        metrics = response.read(1_000_000).decode('utf-8')
    for line in metrics.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == 'cloudflared_tunnel_ha_connections':
            count = float(fields[1])
            return math.isfinite(count) and count > 0
    return False


class TunnelWatchdog:
    def __init__(self, log, *, probe=tunnel_connected, clock=time.monotonic,
                 interval=30, failure_timeout=180):
        self.log = log
        self.probe = probe
        self.clock = clock
        self.interval = interval
        self.failure_timeout = failure_timeout
        self.next_check = clock() + interval
        self.unhealthy_since = None
        self.log.info('tunnel_watchdog_started interval_seconds=%s failure_timeout_seconds=%s',
                      interval, failure_timeout)

    def restart_required(self):
        now = self.clock()
        if now < self.next_check:
            return False
        self.next_check = now + self.interval
        try:
            healthy = self.probe()
        except Exception as exc:
            healthy = False
            self.log.warning('tunnel_watchdog_probe_failed error_type=%s', type(exc).__name__)
        if healthy:
            if self.unhealthy_since is not None:
                self.log.info('tunnel_watchdog_recovered')
            self.unhealthy_since = None
            return False
        if self.unhealthy_since is None:
            self.unhealthy_since = now
            self.log.warning('tunnel_watchdog_disconnected')
        if now - self.unhealthy_since >= self.failure_timeout:
            self.log.error('tunnel_watchdog_restart disconnected_seconds=%s',
                           int(now - self.unhealthy_since))
            return True
        return False
