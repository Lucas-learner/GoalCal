import { test, expect } from '@playwright/test';

test('public intro supports bilingual content, chapter navigation and local demos', async ({ page }) => {
  const errors: string[] = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: /把长期目标/ })).toBeVisible();
  await expect(page.locator('.hero-product')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);

  await page.locator('.story-progress button').nth(2).click();
  await expect(page.locator('.story-progress button').nth(2)).toHaveAttribute('aria-current', 'step');
  await expect(page.locator('.story-stage-visual img')).toHaveAttribute('src', /story-zh-2/);
  const step2HeadingTop = await page.locator('#story-2 h3').evaluate(el => el.getBoundingClientRect().top);
  expect(step2HeadingTop).toBeGreaterThan(220);
  expect(step2HeadingTop).toBeLessThan(390);
  expect(await page.locator('.story-stage-visual').evaluate(element => {
    const image = element.querySelector('img')!.getBoundingClientRect();
    const frame = element.querySelector('.story-visual')!.getBoundingClientRect();
    return Math.abs(image.width - frame.width) < 1;
  })).toBe(true);
  const scheduleHeight = await page.locator('.story-stage-visual img').evaluate(image => image.getBoundingClientRect().height);
  await page.locator('.story-progress button').nth(3).click();
  await expect(page.locator('.story-stage-visual img')).toHaveAttribute('src', /story-zh-3/);
  await expect.poll(() => page.locator('.story-stage-visual img').evaluate(image => image.naturalWidth)).toBeGreaterThan(0);
  expect(await page.locator('.story-stage-visual img').evaluate(image => image.getBoundingClientRect().height)).toBeCloseTo(scheduleHeight, 1);
  await expect(page.locator('.feature-visual-calendar img')).toHaveAttribute('src', /sync-zh/);
  await expect(page.locator('.feature-visual-habit img')).toHaveAttribute('src', /feature-zh-habit/);
  await page.locator('.reminder-feature').scrollIntoViewIfNeeded();
  expect(await page.locator('.reminder-feature').evaluate(card => {
    const copy = card.querySelector('.feature-card-copy')!.getBoundingClientRect();
    const calendar = card.querySelector('.reminder-channel-calendar')!.getBoundingClientRect();
    const email = card.querySelector('.reminder-channel-email')!.getBoundingClientRect();
    return (
      calendar.top >= copy.bottom - 1 &&
      calendar.top - copy.bottom < 90 &&
      Math.abs(calendar.left - copy.left) < 2 &&
      email.left >= copy.right - 1 &&
      email.left >= calendar.right - 1 &&
      Math.abs(calendar.bottom - email.bottom) < 2
    );
  })).toBe(true);

  await page.getByRole('button', { name: 'EN', exact: true }).click();
  await expect(page.getByRole('heading', { name: /Turn a long-term goal/ })).toBeVisible();
  await expect(page.locator('html')).toHaveAttribute('lang', 'en');
  await expect(page.locator('.hero-screenshot')).toHaveAttribute('src', /hero-en/);
  await expect(page.locator('.feature-visual-calendar img')).toHaveAttribute('src', /sync-en/);
  await expect(page.locator('.assistant-feature')).toContainText('GoalCal Assistant.');
  await expect(page.locator('.sync-feature')).not.toContainText('Outlook');
  await expect(page.getByRole('link', { name: 'Get started', exact: true })).toHaveAttribute('href', '/_goalcal/register?next=/app/');
  expect(errors).toEqual([]);
});

test('mobile intro stays readable with reduced motion and keeps the registration route', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  await expect(page.getByRole('heading', { name: /把长期目标/ })).toBeVisible();
  await expect(page.locator('.story-sticky-column')).toBeHidden();
  await expect(page.locator('.story-mobile-visual').first()).toBeVisible();
  expect(await page.locator('.story-mobile-visual').evaluateAll(elements => elements.every(element => {
    const frame = element.getBoundingClientRect();
    const visual = element.querySelector('.story-visual')!.getBoundingClientRect();
    return Math.abs((frame.left + frame.width / 2) - (visual.left + visual.width / 2)) < 1;
  }))).toBe(true);
  expect(await page.locator('[data-reveal]').evaluateAll(elements => elements.every(element => element.classList.contains('is-visible')))).toBe(true);
  await page.locator('.reminder-feature').scrollIntoViewIfNeeded();
  await expect(page.locator('.reminder-channel-calendar')).toBeVisible();
  await expect(page.locator('.reminder-channel-email')).toBeVisible();
  expect(await page.locator('.reminder-showcase').evaluate(element => element.getBoundingClientRect().height)).toBeGreaterThan(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.locator('.intro-nav-cta').click();
  await expect(page).toHaveURL(/\/_goalcal\/register/);
  await expect(page.getByRole('textbox', { name: '邮箱', exact: true })).toBeVisible();
});
