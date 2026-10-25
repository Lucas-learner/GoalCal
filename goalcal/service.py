"""launchd child supervisor with bounded application/tunnel stdout logs."""
import argparse
import logging
from logging.handlers import RotatingFileHandler
import signal
import subprocess
import sys
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
        command = [sys.executable, '-m', 'streamlit', 'run', str(ROOT / 'app.py'),
                   '--server.address=127.0.0.1', '--server.port=8501', '--server.enableXsrfProtection=true']
    elif args.provider == 'ngrok':
        if not args.ngrok or not args.config:
            parser.error('ngrok requires --ngrok and --config')
        command = [args.ngrok, 'http', 'http://127.0.0.1:8501',
                   '--config', args.config, '--inspect=false',
                   '--log', 'stdout', '--log-format', 'json', '--log-level', 'warn']
        if args.url:
            command += ['--url', args.url]
    else:
        if not args.cloudflared or not args.config:
            parser.error('Cloudflare requires --cloudflared and --config')
        command = [args.cloudflared, 'tunnel', '--config', args.config, 'run']
    path = database_path().parent / 'logs' / f'{args.service}.log'
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5)
    log = logging.getLogger('service_output'); log.addHandler(handler); log.setLevel(logging.INFO)
    child = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    def stop(*_):
        child.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    for line in child.stdout:
        log.info(line.rstrip())
    raise SystemExit(child.wait())


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        logging.getLogger('goalcal').error('local_command_failed error_type=%s', type(exc).__name__)
        raise SystemExit(f'Operation failed ({type(exc).__name__}); check local configuration and backups') from None
