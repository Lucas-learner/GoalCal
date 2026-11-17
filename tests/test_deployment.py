"""Generated defaults must not recreate the retired production instance."""
import json
from pathlib import Path
import plistlib
import subprocess
import sys


def test_production_generator_defaults_to_web_instance(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([
        sys.executable, str(root / 'deploy/generate.py'),
        '--domain', 'goalcal.example',
        '--tunnel-id', '00000000-0000-0000-0000-000000000000',
        '--credentials', str(tmp_path / 'credentials.json'),
        '--output-dir', str(tmp_path / 'generated'),
    ], check=True, capture_output=True, text=True)
    output = Path(result.stdout.strip())
    config = json.loads((output / 'cloudflared.yml').read_text())
    assert config['ingress'][0]['service'] == 'http://127.0.0.1:8521'
    assert {p.name for p in output.glob('*.plist')} == {
        f'local.goalcal-v2.{kind}.plist' for kind in ('app', 'backup', 'tunnel')
    }
    app = plistlib.loads((output / 'local.goalcal-v2.app.plist').read_bytes())
    assert app['WorkingDirectory'] == str(root)
    assert app['EnvironmentVariables']['GOALCAL_ENV_FILE'] == str(root / '.env.production')
    assert '--legacy-ui' not in app['ProgramArguments']
