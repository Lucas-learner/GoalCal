import logging
from io import BytesIO

import pytest

from goalcal import tunnel_watchdog as module


def test_sustained_disconnect_restarts_after_grace_period(caplog):
    now = [0]
    probes = []
    def probe():
        probes.append(now[0])
        return False
    caplog.set_level(logging.INFO)
    watchdog = module.TunnelWatchdog(logging.getLogger('watchdog-test'),
                                     clock=lambda: now[0], probe=probe)
    for second in (0, 29, 30, 31, 60, 120, 180, 209):
        now[0] = second
        assert not watchdog.restart_required()
    now[0] = 240
    assert watchdog.restart_required()
    assert 0 not in probes and 29 not in probes and 31 not in probes
    assert 'tunnel_watchdog_restart' in caplog.text


def test_recovery_clears_failure_timer(caplog):
    now = [0]
    connected = [False]
    caplog.set_level(logging.INFO)
    watchdog = module.TunnelWatchdog(logging.getLogger('watchdog-test'),
        clock=lambda: now[0], probe=lambda: connected[0])
    for second, healthy in ((30, False), (180, True), (210, False), (360, False)):
        now[0], connected[0] = second, healthy
        assert not watchdog.restart_required()
    assert 'tunnel_watchdog_recovered' in caplog.text
    now[0] = 390
    assert watchdog.restart_required()


def test_failed_probe_also_triggers_recovery_without_leaking_error(caplog):
    now = [0]
    def broken_probe():
        raise OSError('sensitive details')
    watchdog = module.TunnelWatchdog(logging.getLogger('watchdog-test'),
        clock=lambda: now[0], probe=broken_probe)
    now[0] = 30
    assert not watchdog.restart_required()
    now[0] = 210
    assert watchdog.restart_required()
    assert 'error_type=OSError' in caplog.text
    assert 'sensitive details' not in caplog.text


@pytest.mark.parametrize('metrics,expected', [
    ('# HELP metric\ncloudflared_tunnel_ha_connections 4\n', True),
    ('cloudflared_tunnel_ha_connections 1\n', True),
    ('cloudflared_tunnel_ha_connections 0\n', False),
    ('cloudflared_tunnel_ha_connections NaN\n', False),
    ('cloudflared_tunnel_ha_connections +Inf\n', False),
    ('other_metric 4\n', False),
])
def test_probe_reads_connection_count_without_proxy(monkeypatch, metrics, expected):
    class Opener:
        def open(self, url, timeout):
            assert url == 'http://127.0.0.1:20241/metrics'
            assert timeout == 3
            return BytesIO(metrics.encode())
    def build(handler):
        assert handler.proxies == {}
        return Opener()
    monkeypatch.setattr(module, 'build_opener', build)
    assert module.tunnel_connected() is expected


def test_supervisor_stops_stuck_child_and_exits_for_launchd(monkeypatch, tmp_path):
    from goalcal import service
    (tmp_path / 'logs').mkdir()
    now = [0]
    commands = []
    class Child:
        stdout = []
        terminated = False
        def poll(self):
            return 0 if self.terminated else None
        def terminate(self):
            self.terminated = True
        def wait(self, timeout):
            assert self.terminated
    child = Child()
    def popen(command, **kwargs):
        commands.append(command)
        return child
    def sleep(_):
        now[0] += 30
    monkeypatch.setattr(service.sys, 'argv', ['service', 'tunnel', '--cloudflared',
                                             '/fake/cloudflared', '--config', '/fake/config'])
    monkeypatch.setattr(service, 'configure_logging', lambda: None)
    monkeypatch.setattr(service, 'database_path', lambda: tmp_path / 'db')
    monkeypatch.setattr(service.subprocess, 'Popen', popen)
    monkeypatch.setattr(service.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(service.time, 'sleep', sleep)
    monkeypatch.setattr(service, 'TunnelWatchdog', lambda log: module.TunnelWatchdog(
        log, clock=lambda: now[0], probe=lambda: False))
    try:
        with pytest.raises(SystemExit) as result:
            service.main()
        assert result.value.code == 1
        assert child.terminated
        assert now[0] == 210
        assert commands[0][-3:] == ['--metrics', '127.0.0.1:20241', 'run']
    finally:
        logger = logging.getLogger('service_output')
        for handler in list(logger.handlers):
            if getattr(handler, 'baseFilename', '') == str(tmp_path / 'logs/tunnel.log'):
                logger.removeHandler(handler)
                handler.close()
