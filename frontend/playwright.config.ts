import { defineConfig } from '@playwright/test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Always own an isolated local server/database; never run mutating E2E on production.
const database = join(mkdtempSync(join(tmpdir(), 'goalcal-e2e-')), 'test.db');
const baseURL = 'http://127.0.0.1:8537';
export default defineConfig({
  testDir: './e2e', workers: 1, timeout: 45000,
  webServer: {
    command: `../.venv/bin/python ../tools/web_preview.py --database '${database}' --port 8537 --open-registration`,
    url: `${baseURL}/_goalcal/health`, reuseExistingServer: false, timeout: 30000,
  },
  use: { baseURL, channel: 'chrome', locale: 'zh-CN',
    viewport: { width: 1440, height: 1000 },
    trace: 'retain-on-failure', screenshot: 'only-on-failure' },
});
