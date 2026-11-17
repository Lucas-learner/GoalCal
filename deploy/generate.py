"""Generate reviewable launchd plists and a named Cloudflare tunnel config."""
import argparse
from pathlib import Path
import plistlib
import re
import shutil
import sys
import json

root = Path(__file__).resolve().parents[1]
default_python = str(root / '.venv' / 'bin' / 'python') if (root / '.venv' / 'bin' / 'python').exists() else sys.executable
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
parser.add_argument('--gateway-port', type=int, default=8501)
parser.add_argument('--label-prefix', default='local.goalcal')
parser.add_argument('--env-file', type=Path, default=root / '.env.production')
parser.add_argument('--output-dir', type=Path, default=root / 'deploy' / 'generated')
parser.add_argument('--python', default=default_python)
parser.add_argument('--protocol', choices=['auto', 'quic', 'http2'], default='auto')
args = parser.parse_args()
if not re.fullmatch(r'[a-zA-Z0-9._-]+', args.label_prefix):
    parser.error('Invalid --label-prefix')
output = args.output_dir.expanduser().resolve()
output.mkdir(parents=True, exist_ok=True, mode=0o700)
env_file = args.env_file.expanduser().resolve()
config = output / 'cloudflared.yml'
if not args.local_only and args.provider == 'cloudflare':
    if not args.domain or not re.fullmatch(r'[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', args.domain):
        parser.error('Provide a valid --domain or use --local-only')
    if not args.tunnel_id or not re.fullmatch(r'[a-fA-F0-9-]{36}', args.tunnel_id):
        parser.error('Expected --tunnel-id UUID')
    if not args.credentials:
        parser.error('--credentials is required for the tunnel')
    # JSON is valid YAML and avoids quoting/path injection issues.
    tunnel_cfg = {'tunnel': args.tunnel_id, 'credentials-file': str(args.credentials.expanduser().resolve()),
        'ingress': [{'hostname': args.domain, 'service': f'http://127.0.0.1:{args.gateway_port}'}, {'service': 'http_status:404'}]}
    if args.protocol != 'auto':
        tunnel_cfg['protocol'] = args.protocol
    config.write_text(json.dumps(tunnel_cfg, indent=2))
services = ('app', 'backup') if args.local_only else ('app', 'tunnel', 'backup')
for service in services:
    command = [args.python, '-m', 'goalcal.service', service]
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
    content = {'Label': f'{args.label_prefix}.{service}', 'ProgramArguments': command,
               'WorkingDirectory': str(root), 'RunAtLoad': True, 'Umask': 63,
               'EnvironmentVariables': {'GOALCAL_ENV_FILE': str(env_file)}}
    if service == 'backup':
        content['StartCalendarInterval'] = {'Hour': 3, 'Minute': 15}
    else:
        content['KeepAlive'] = True
        content['ThrottleInterval'] = 10
    with (output / f'{args.label_prefix}.{service}.plist').open('wb') as f:
        plistlib.dump(content, f)
print(output)
