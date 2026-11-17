import { QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { AppContent, createAppQueryClient } from '../App';
import { AppStateProvider } from '../state/AppStateContext';

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function renderApp(initialPath = '/') {
  const qc = createAppQueryClient();
  return render(
    <QueryClientProvider client={qc}>
      <AppStateProvider>
        <MemoryRouter initialEntries={[initialPath]}>
          <AppContent />
        </MemoryRouter>
      </AppStateProvider>
    </QueryClientProvider>,
  );
}

describe('Bilingual i18n behavior', () => {
  it('auto-detects English from system navigator.language and switches cleanly to Chinese in Settings', async () => {
    Object.defineProperty(window.navigator, 'language', {
      value: 'en-US',
      configurable: true,
    });

    const seenLangHeaders: string[] = [];
    const patchPayloads: string[] = [];
    let currentLang = '';

    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = init?.method ?? 'GET';
      const headers = new Headers(init?.headers);
      const langHdr = headers.get('X-GoalCal-Lang');
      if (langHdr) seenLangHeaders.push(langHdr);

      if (url === '/api/v1/me' && method === 'GET') {
        return jsonResponse({
          id: 1,
          name: 'Yan Li',
          email: 'yan@example.com',
          timezone: 'Asia/Shanghai',
          lang: currentLang,
        });
      }
      if (url === '/api/v1/me' && method === 'PATCH') {
        const body = String(init?.body ?? '');
        patchPayloads.push(body);
        const parsed = JSON.parse(body) as { lang: string };
        currentLang = parsed.lang;
        return jsonResponse({
          id: 1,
          name: 'Yan Li',
          email: 'yan@example.com',
          timezone: 'Asia/Shanghai',
          lang: currentLang,
        });
      }
      if (url === '/api/v1/today') {
        return jsonResponse({
          date: '2026-11-17',
          timezone: 'Asia/Shanghai',
          next: null,
          execution: null,
          recovery: [],
          events: [],
        });
      }
      if (url === '/api/v1/availability') {
        return jsonResponse({
          version: 'a1',
          available_windows: [],
          protected_windows: [],
          preferred_windows: [],
          buffer_minutes: 10,
          max_goal_minutes_per_day: 120,
        });
      }
      if (url === '/api/v1/metrics') {
        return jsonResponse({});
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();

    const firstRender = renderApp('/');
    expect(await screen.findByRole('heading', { name: 'Today' })).toBeInTheDocument();
    expect(await screen.findByText('Tue, Nov 17')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Ask GoalCal' })).toBeInTheDocument();
    expect(seenLangHeaders[0]).toBe('en');
    expect(document.documentElement.lang).toBe('en');
    firstRender.unmount();

    renderApp('/settings');
    expect(await screen.findByRole('heading', { name: 'Settings' })).toBeInTheDocument();

    const langSelect = await screen.findByLabelText('Language');
    await user.selectOptions(langSelect, 'zh');

    expect(await screen.findByRole('heading', { name: '设置' })).toBeInTheDocument();
    expect(document.documentElement.lang).toBe('zh-CN');
    await waitFor(() => {
      expect(patchPayloads).toEqual([JSON.stringify({ lang: 'zh' })]);
    });
  });

  it('honors login-page goalcal_lang cookie over a prior localStorage auto setting on entry', async () => {
    localStorage.setItem('goalcal.lang', 'auto');
    document.cookie = 'goalcal_lang=en; Path=/';

    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url === '/api/v1/me') {
        return jsonResponse({ id: 1, name: 'Yan Li', email: 'yan@example.com', timezone: 'Asia/Shanghai', lang: 'en' });
      }
      if (url === '/api/v1/today') {
        return jsonResponse({
          date: '2026-11-17',
          timezone: 'Asia/Shanghai',
          next: null,
          execution: null,
          recovery: [],
          events: [],
        });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    renderApp('/');
    expect(await screen.findByRole('heading', { name: 'Today' })).toBeInTheDocument();
    expect(localStorage.getItem('goalcal.lang')).toBe('en');
  });
});
