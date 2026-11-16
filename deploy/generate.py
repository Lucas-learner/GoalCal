"""Generate reviewable launchd plists and a named Cloudflare tunnel config."""
import argparse
from pathlib import Path
import plistlib
import re
import shutil
import json

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--local-only', action='store_true', help='Generate app and backup services before a domain is available')
parser.add_argument('--provider', choices=['cloudflare', 'ngrok'], default='cloudflare')
parser.add_argument('--ngrok', default=shutil.which('ngrok') or '/opt/homebrew/bin/ngrok')
parser.add_argument('--ngrok-config', type=Path, default=Path.home() / 'Library/Application Support/ngrok/ngrok.yml')
parser.add_argument('--ngrok-url')
parser.add_argument('--domain')
parser.add_argument('--tunnel-id')
parser.add_argument('--credentials', type=Path)
parser.add_argument('--cloudflared', default=shutil.which('cloudflared') or '/opt/homebrew/bin/cloudflared')
args = parser.parse_args()
output = root / 'deploy' / 'generated'
output.mkdir(parents=True, exist_ok=True, mode=0o700)
config = output / 'cloudflared.yml'
if not args.local_only and args.provider == 'cloudflare':
    if not args.domain or not re.fullmatch(r'[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', args.domain):
        parser.error('Provide a valid --domain or use --local-only')
    if not args.tunnel_id or not re.fullmatch(r'[a-fA-F0-9-]{36}', args.tunnel_id):
        parser.error('Expected --tunnel-id UUID')
    if not args.credentials:
        parser.error('--credentials is required for the tunnel')
    # JSON is valid YAML and avoids quoting/path injection issues.
    config.write_text(json.dumps({'tunnel': args.tunnel_id, 'credentials-file': str(args.credentials.expanduser().resolve()),
        'ingress': [{'hostname': args.domain, 'service': 'http://127.0.0.1:8501'}, {'service': 'http_status:404'}]}, indent=2))
services = ('app', 'backup') if args.local_only else ('app', 'tunnel', 'backup')
for service in services:
    command = [str(root / '.venv' / 'bin' / 'python'), '-m', 'goalcal.service', service]
    if service == 'tunnel':
        if args.provider == 'ngrok':
            if not args.ngrok_config.expanduser().is_file():
                parser.error('ngrok configuration not found; sign in and configure its authtoken locally first')
            if args.ngrok_url and not re.fullmatch(r'https://[a-zA-Z0-9.-]+', args.ngrok_url):
                parser.error('--ngrok-url must be an HTTPS hostname')
            command += ['--provider', 'ngrok', '--ngrok', args.ngrok,
                        '--config', str(args.ngrok_config.expanduser().resolve())]
            if args.ngrok_url:
                command += ['--url', args.ngrok_url]
        else:
            command += ['--cloudflared', args.cloudflared, '--config', str(config)]
    content = {'Label': f'local.goalcal.{service}', 'ProgramArguments': command,
               'WorkingDirectory': str(root), 'RunAtLoad': True, 'Umask': 63,
               'EnvironmentVariables': {'GOALCAL_ENV_FILE': str(root / '.env.production')}}
    if service == 'backup':
        content['StartCalendarInterval'] = {'Hour': 3, 'Minute': 15}
    else:
        content['KeepAlive'] = True
        content['ThrottleInterval'] = 10
    with (output / f'local.goalcal.{service}.plist').open('wb') as f:
        plistlib.dump(content, f)
print(output)
