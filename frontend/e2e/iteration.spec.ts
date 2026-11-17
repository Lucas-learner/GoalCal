import { test, expect, type Page } from '@playwright/test';


async function generateDraft(page: Page) {
  await page.getByRole('button', { name: '生成草稿', exact: true }).click();
  await expect(page.locator('#draft-title-input').or(page.locator('#calibration-title-input'))).toBeVisible();
  if (await page.locator('#calibration-title-input').isVisible()) {
    await page.getByRole('button', { name: '按原目标生成', exact: true }).click();
  }
  await expect(page.locator('#draft-title-input')).toBeVisible();
}

test.beforeEach(async ({ page }) => {
  await page.goto('/');
  await page.getByRole('textbox', { name: '用户名或邮箱', exact: true }).fill('preview');
  await page.getByRole('textbox', { name: '密码', exact: true }).fill('GoalCal-preview-2026!');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '今天', exact: true })).toBeVisible();
});

test('create draft, edit, save, schedule and undo through real HTTP', async ({ page }) => {
  const errors: string[] = []; page.on('pageerror', e => errors.push(e.message));
  const title = `验收目标 ${Date.now()}`;
  await page.goto('/app/goals/new');
  await page.locator('#new-goal-title-input').fill(title);
  await page.getByRole('button', { name: '1 个月', exact: true }).click();
  await generateDraft(page);
  await expect(page.locator('#draft-title-input')).toHaveValue(title, { timeout: 20000 });
  await page.locator('#draft-step-title-0').fill('核对新版本闭环');
  await page.getByRole('button', { name: '保存并安排第一步', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.locator('.slot-btn').first()).toBeVisible();
  await expect(page.locator('.slot-reason')).toHaveCount(0);
  await page.getByRole('button', { name: '明天', exact: true }).click();
  await expect(page.locator('.slot-btn').first()).toBeVisible();
  await page.locator('.slot-btn').first().click();
  await expect(page.getByRole('button', { name: '撤销', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '撤销', exact: true }).click();
  await expect(page.getByRole('button', { name: '撤销', exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();
  await page.screenshot({ path: 'test-results/goals-desktop.png', fullPage: true });
  await page.getByRole('button', { name: '归档', exact: true }).click();
  await expect(page.locator('.goal-list')).not.toContainText(title);
  expect(errors).toEqual([]);
});

test('assistant accepts one candidate, survives reload and changes target', async ({ page }) => {
  await page.getByRole('button', { name: '问 GoalCal', exact: true }).click();
  const composer = page.getByPlaceholder('问问、安排或调整…');
  await composer.fill('帮雅思备考找明晚的时间');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.slot-btn').first()).toBeVisible();
  await expect(page.locator('.slot-reason')).toHaveCount(0);
  await expect(composer).toBeEnabled();
  await page.screenshot({ path: 'test-results/assistant-desktop.png' });
  await page.locator('.slot-btn').first().click();
  await expect(page.getByRole('button', { name: '撤销', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '撤销', exact: true }).click();
  await composer.fill('那恢复跑步呢');
  await page.getByRole('button', { name: '发送消息', exact: true }).click();
  await expect(page.locator('.proposal-card')).toContainText('恢复跑步');
  await expect(page.locator('.slot-btn').first()).toBeVisible();
  await expect(page.locator('.slot-btn .slot-meta')).toHaveCount(0);
  await page.reload();
  await page.getByRole('button', { name: '问 GoalCal', exact: true }).click();
  await expect(page.locator('.proposal-card')).toContainText('恢复跑步');
  await expect(page.locator('.slot-btn').first()).toBeVisible();
});

test('responsive navigation and calendar have no horizontal overflow', async ({ page }) => {
  const errors: string[] = []; page.on('pageerror', e => errors.push(e.message));
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    for (const [path, name] of [['/', '今天'], ['/goals', '目标'], ['/calendar', '日历'], ['/settings', '设置']]) {
      await page.goto('/app' + path);
      await expect(page.getByRole('heading', { name, exact: true }).first()).toBeVisible();
      if (path === '/calendar') {
        await expect(page.locator('.fc-event-main').first()).toBeVisible();
        expect(await page.locator('.fc-event-main').first().evaluate(el => getComputedStyle(el).color)).not.toBe('rgb(255, 255, 255)');
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
      await page.screenshot({ path: `test-results/${name}-${width}.png`, fullPage: true });
    }
    await page.goto('/app/');
    await page.getByRole('button', { name: '问 GoalCal', exact: true }).click();
    await expect(page.getByPlaceholder('问问、安排或调整…')).toBeVisible();
    await expect(page.getByText('正在加载助手对话', { exact: true })).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.screenshot({ path: `test-results/assistant-${width}.png` });
    await page.getByRole('button', { name: '关闭助手' }).click();
  }
  expect(errors).toEqual([]);
});

test('settings save persists and ICS import deduplicates', async ({ page }) => {
  await page.goto('/app/settings');
  await page.getByRole('button', { name: '时间偏好', exact: true }).click();
  await page.locator('#pref-max-minutes').fill('255');
  const saved = page.waitForResponse(r => r.url().endsWith('/preferences/availability') && r.request().method() === 'PUT');
  await page.getByRole('button', { name: '保存更改', exact: true }).click();
  expect((await saved).status()).toBe(200);
  await page.reload();
  await page.getByRole('button', { name: '时间偏好', exact: true }).click();
  await expect(page.locator('#pref-max-minutes')).toHaveValue('255');
  await page.getByRole('button', { name: '日历导入', exact: true }).click();
  const file = { name:'qa.ics', mimeType:'text/calendar', buffer: Buffer.from('BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:goalcal-ui-qa\r\nDTSTART:20300101T020000Z\r\nDTEND:20300101T023000Z\r\nSUMMARY:导入验收日程\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n') };
  await page.locator('#ics-file-input').setInputFiles(file);
  for (let i=0;i<2;i++) {
    const imported = page.waitForResponse(r => r.url().endsWith('/calendar/import') && r.request().method() === 'POST');
    await page.getByRole('button', { name: '导入', exact: true }).click();
    const response=await imported; expect(response.status()).toBe(200);
    if(i===1) expect((await response.json()).message).toContain('已导入 0 条');
  }
});

test('calendar edit remains editable after moving and supports cancel undo', async ({ page }) => {
  const title=`临时日程 ${Date.now()}`;
  await page.goto('/app/calendar');
  await page.getByRole('button', { name: '+ 新日程', exact: true }).click();
  await page.locator('#new-event-title').fill(title);
  await page.locator('#new-event-start').fill('2030-01-02T02:00');
  await page.locator('#new-event-end').fill('2030-01-02T02:30');
  const created=page.waitForResponse(r=>r.url().endsWith('/calendar/events') && r.request().method()==='POST');
  await page.getByRole('button', { name:'创建',exact:true }).click();
  expect((await created).status()).toBe(200);
  // Drive the supported date-jump control to locate the event in the actual calendar.
  await page.getByLabel('跳转日期').fill('2030-01-02');
  await page.getByText(title,{exact:true}).click();
  await page.locator('#event-start-local').fill('2030-01-02T03:00');
  await page.locator('#event-end-local').fill('2030-01-02T03:30');
  await page.getByRole('button',{name:'保存',exact:true}).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByText(`[已调整] ${title}`,{exact:true}).click();
  await expect(page.getByRole('button',{name:'保存',exact:true})).toBeEnabled();
  await page.getByRole('button',{name:'取消日程',exact:true}).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await page.getByRole('button',{name:'撤销',exact:true}).click();
  await expect(page.getByText(`[已调整] ${title}`,{exact:true})).toBeVisible();
  // Clean up so repeated E2E runs against the preview DB remain idempotent.
  await page.getByText(`[已调整] ${title}`,{exact:true}).click();
  await page.getByRole('button',{name:'取消日程',exact:true}).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
});

test('archiving removes goal from the active list and survives refresh', async ({ page }) => {
  const title = `可删除目标 ${Date.now()}`;
  await page.goto('/app/goals/new');
  await page.locator('#new-goal-title-input').fill(title);
  await generateDraft(page);
  await expect(page.locator('#draft-title-input')).toHaveValue(title, { timeout: 20000 });
  await page.getByRole('button', { name: '仅保存', exact: true }).click();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();

  await page.getByRole('button', { name: '归档', exact: true }).click();
  await expect(page.locator('.goal-list')).not.toContainText(title);

  await page.reload();
  await expect(page.locator('.goal-list')).not.toContainText(title);
});

test('completing phase auto-generates next phase and collapses completed steps', async ({ page }) => {
  const title = `自动续阶目标 ${Date.now()}`;
  await page.goto('/app/goals/new');
  await page.locator('#new-goal-title-input').fill(title);
  await generateDraft(page);
  await expect(page.locator('#draft-title-input')).toHaveValue(title, { timeout: 20000 });
  const firstStepTitle = await page.locator('#draft-step-title-0').inputValue();
  await page.getByRole('button', { name: '仅保存', exact: true }).click();
  await expect(page.getByRole('heading', { name: title, exact: true })).toBeVisible();

  // Complete all starter todo steps; finishing the last step of phase 1 auto-generates phase 2.
  for (let i = 0; i < 6; i++) {
    if (await page.locator('.phase-btn.done').count() >= 1) break;
    await page.locator('.steps-section .step-check-btn').first().click();
    await page.waitForTimeout(200);
  }

  await expect(page.locator('.steps-history-bar')).toBeVisible();
  await expect(page.locator('.steps-section')).not.toContainText(firstStepTitle);
  await expect(page.locator('.steps-section .step-row')).not.toHaveCount(0);

  // Clicking the completed phase reveals its completed steps, and "只看待办" returns.
  await page.locator('.phase-btn.done').first().click();
  await expect(page.locator('.steps-section')).toContainText(firstStepTitle);
  await page.getByRole('button', { name: '只看待办', exact: true }).click();
  await expect(page.locator('.steps-section')).not.toContainText(firstStepTitle);

  // Also test custom-time scheduling fallback inside ProposalModal ("找时间安排").
  await page.getByRole('button', { name: '找时间', exact: true }).first().click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.getByRole('button', { name: '自定义时间 →', exact: true }).click();
  const d = new Date(Date.now() + 2 * 86400_000);
  const pad = (n: number) => String(n).padStart(2, '0');
  const futureLocal = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T10:00`;
  await page.locator('#custom-schedule-start').fill(futureLocal);
  await page.getByRole('button', { name: '安排', exact: true }).click();
  await expect(page.getByRole('dialog')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '撤销', exact: true })).toBeVisible();
  await page.getByRole('button', { name: '撤销', exact: true }).click();

  // Clean up test goal
  await page.getByRole('button', { name: '归档', exact: true }).click();
  await expect(page.locator('.goal-list')).not.toContainText(title);
});
