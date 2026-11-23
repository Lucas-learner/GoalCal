import { test, expect } from '@playwright/test';

test('theme applies to Habits, and the compact Calendar keeps its controls in one row', async ({ page }) => {
  await page.emulateMedia({ colorScheme: 'dark' });
  await page.goto('/');
  expect(await page.locator('html').evaluate(root => getComputedStyle(root).colorScheme)).toBe('dark');
  await page.getByRole('textbox', { name: '用户名或邮箱', exact: true }).fill('preview');
  await page.getByRole('textbox', { name: '密码', exact: true }).fill('GoalCal-preview-2026!');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page.getByRole('heading', { name: '今天', exact: true })).toBeVisible();
  await page.goto('/app/settings');
  const theme = page.getByLabel('外观', { exact: true });
  await expect(theme).toHaveValue('auto');

  const setTheme = async (value: 'dark' | 'auto') => {
    const saved = page.waitForResponse(response => response.url().endsWith('/api/v1/me') && response.request().method() === 'PATCH');
    await theme.selectOption(value);
    expect((await saved).ok()).toBe(true);
  };
  await setTheme('dark');
  await page.emulateMedia({ colorScheme: 'light' });
  await page.goto('/app/goals');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  const goalsButton = page.locator('.topbar .btn.primary');
  const goalActionHeight = (await goalsButton.boundingBox())!.height;
  await page.goto('/app/habits');
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark');
  const habitsButton = page.locator('.topbar .btn.primary');
  expect((await habitsButton.boundingBox())!.height).toBe(goalActionHeight);
  await expect(page.locator('.subtitle')).toBeVisible();
  await expect(page.locator('.empty-state')).toContainText('从一个想坚持的习惯开始');
  await page.goto('/app/settings');
  await expect(theme).toHaveValue('dark');
  await setTheme('auto');

  await page.setViewportSize({ width: 320, height: 760 });
  await page.goto('/app/calendar');
  const toolbar = page.locator('.calendar-toolbar');
  await expect(toolbar.getByRole('button', { name: '今天', exact: true })).toHaveCount(0);
  await expect(toolbar.getByRole('button', { name: '日', exact: true })).toHaveAttribute('aria-pressed', 'true');
  const date = (await toolbar.locator('.calendar-date-nav').boundingBox())!;
  const views = (await toolbar.locator('.calendar-view-seg').boundingBox())!;
  expect(Math.abs(date.y - views.y)).toBeLessThan(2);
  await expect(page.getByRole('button', { name: /尚未安排/ })).toHaveAttribute('aria-expanded', 'false');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
});
