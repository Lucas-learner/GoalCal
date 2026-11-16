"""launchd child supervisor with bounded application/tunnel stdout logs."""
import argparse
import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import subprocess
import sys
import threading
import time
from .config import ROOT
from .db import init_db
from .storage import database_path, backup
from .logging_setup import configure_logging


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('service', choices=['app', 'tunnel', 'backup'])
    parser.add_argument('--provider', choices=['cloudflare', 'ngrok'], default='cloudflare')
    parser.add_argument('--cloudflared')
    parser.add_argument('--ngrok')
    parser.add_argument('--url')
    parser.add_argument('--config')
    args = parser.parse_args()
    configure_logging()
    if args.service == 'backup':
        init_db()
        backup()
        logging.getLogger('goalcal.audit').info('daily_backup_completed')
        return
    if args.service == 'app':
        init_db()  # fail before binding port if migration fails
        upstream_port = os.getenv('GOALCAL_UPSTREAM_PORT', '8502')
        gateway_port = os.getenv('GOALCAL_GATEWAY_PORT', '8501')
        commands = [
            ('streamlit', [sys.executable, '-m', 'streamlit', 'run', str(ROOT / 'app.py'),
                           '--server.address=127.0.0.1', f'--server.port={upstream_port}',
                           '--server.enableXsrfProtection=true']),
            ('gateway', [sys.executable, '-m', 'uvicorn', 'goalcal.auth_proxy:app',
                         '--host', '127.0.0.1', '--port', gateway_port,
                         '--no-access-log', '--log-level', 'warning']),
        ]
    elif args.provider == 'ngrok':
        if not args.ngrok or not args.config:
            parser.error('ngrok requires --ngrok and --config')
        command = [args.ngrok, 'http', 'http://127.0.0.1:8501',
                   '--config', args.config, '--inspect=false',
                   '--log', 'stdout', '--log-format', 'json', '--log-level', 'warn']
        if args.url:
            command += ['--url', args.url]
        commands = [('tunnel', command)]
    else:
        if not args.cloudflared or not args.config:
            parser.error('Cloudflare requires --cloudflared and --config')
        commands = [('tunnel', [args.cloudflared, 'tunnel', '--config', args.config, 'run'])]
    path = database_path().parent / 'logs' / f'{args.service}.log'
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5)
    log = logging.getLogger('service_output'); log.addHandler(handler); log.setLevel(logging.INFO)
    children = [
        (name, subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True))
        for name, command in commands
    ]
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True
        for _, child in children:
            if child.poll() is None:
                child.terminate()

    def pump(name, child):
        for line in child.stdout:
            log.info('%s %s', name, line.rstrip())

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    threads = [threading.Thread(target=pump, args=(name, child), daemon=True)
               for name, child in children]
    for thread in threads:
        thread.start()
    failed = False
    try:
        while not stopping and all(child.poll() is None for _, child in children):
            time.sleep(0.25)
        failed = not stopping
    finally:
        stop()
        for _, child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for thread in threads:
            thread.join(timeout=1)
    raise SystemExit(1 if failed else 0)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        logging.getLogger('goalcal').error('local_command_failed error_type=%s', type(exc).__name__)
        raise SystemExit(f'Operation failed ({type(exc).__name__}); check local configuration and backups') from None
