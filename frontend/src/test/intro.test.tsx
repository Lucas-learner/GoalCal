import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { I18nProvider } from '../i18n';
import { IntroPage } from '../intro/IntroPage';

function renderIntro() {
  return render(<I18nProvider><IntroPage /></I18nProvider>);
}

describe('GoalCal public introduction', () => {
  it('localizes real product captures without API calls', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    const { container } = renderIntro();

    expect(screen.getByRole('heading', { name: /把长期目标/ })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '开始使用' }).getAttribute('href')).toBe('/_goalcal/register?next=/app/');
    expect(container.querySelector('.hero-screenshot')?.getAttribute('src')).toContain('hero-zh');
    expect(container.querySelector('.story-input-context')?.textContent).toContain('我要在 8 周内完成人生第一个 10 公里');
    expect(container.querySelector('.intro-manifesto')).not.toBeInTheDocument();
    expect(container.querySelector('.story-section-heading h2')?.textContent).toContain('不是管理每一分钟。');
    expect(container.querySelector('#story-0 h3')?.textContent).toBe('01澄清目标');
    expect(container.querySelectorAll('.story-chapter-index')).toHaveLength(0);
    expect(container.querySelector('.assistant-feature')?.textContent).toContain('起草目标与习惯');
    expect(container.querySelector('.reminder-feature')?.textContent).toContain('每周复盘');
    expect(container.querySelectorAll('.reminder-showcase img')).toHaveLength(2);
    expect(container.querySelector('.reminder-channel-calendar figcaption')?.textContent).toBe('系统日历提醒');
    expect(container.querySelector('.reminder-channel-email figcaption')?.textContent).toBe('每周邮件复盘');
    expect(container.querySelectorAll('.story-visual source')).toHaveLength(0);
    expect(Array.from(container.querySelectorAll('.story-visual')).every(element => element.querySelectorAll('img').length === 1)).toBe(true);

    await user.click(screen.getByRole('button', { name: 'EN' }));
    expect(screen.getByRole('heading', { name: /Turn a long-term goal/ })).toHaveTextContent('Turn a long-term goalinto something you can do today.');
    expect(document.documentElement.lang).toBe('en');
    expect(localStorage.getItem('goalcal.lang')).toBe('en');
    expect(container.querySelector('.hero-screenshot')?.getAttribute('src')).toContain('hero-en');
    expect(container.querySelector('.feature-visual-calendar img')?.getAttribute('src')).toContain('sync-en');
    expect(container.querySelector('.story-input-context')?.textContent).toContain('I want to run my first 10K in eight weeks.');
    expect(container.querySelector('.assistant-feature')?.textContent).toContain('GoalCal Assistant.');
    expect(container.querySelector('.assistant-feature')?.textContent).toContain('Draft goals and habits');
    expect(container.querySelector('.reminder-feature')?.textContent).toContain('weekly reviews');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('shows final content for reduced motion and allows direct chapter navigation', async () => {
    Object.defineProperty(window, 'matchMedia', {
      configurable: true,
      value: vi.fn().mockReturnValue({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() }),
    });
    const scrollIntoView = vi.fn();
    Object.defineProperty(Element.prototype, 'scrollIntoView', { configurable: true, value: scrollIntoView });
    const user = userEvent.setup();
    const { container } = renderIntro();

    const reveals = container.querySelectorAll('[data-reveal]');
    expect(reveals.length).toBeGreaterThan(0);
    expect(Array.from(reveals).every((element) => element.classList.contains('is-visible'))).toBe(true);

    const schedule = container.querySelectorAll<HTMLButtonElement>('.story-progress button')[2];
    await user.click(schedule);
    expect(schedule).toHaveAttribute('aria-current', 'step');
    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: 'auto', block: 'start' });
  });
});
