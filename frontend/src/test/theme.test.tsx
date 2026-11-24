import { QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { AppContent, createAppQueryClient } from '../App';
import { getDisplaySourceColor, getEditorialPigmentKey } from '../lib/calendarColors';
import { AppStateProvider } from '../state/AppStateContext';

function jsonResponse(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function createMatchMediaController(initialDark: boolean) {
  let matches = initialDark;
  const listeners = new Set<(event: MediaQueryListEvent) => void>();

  const matchMediaMock = vi.fn().mockImplementation((query: string) => ({
    matches: query === '(prefers-color-scheme: dark)' ? matches : false,
    media: query,
    onchange: null,
    addEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
      listeners.add(listener);
    },
    removeEventListener: (_type: string, listener: (event: MediaQueryListEvent) => void) => {
      listeners.delete(listener);
    },
    addListener: (listener: (event: MediaQueryListEvent) => void) => {
      listeners.add(listener);
    },
    removeListener: (listener: (event: MediaQueryListEvent) => void) => {
      listeners.delete(listener);
    },
    dispatchEvent: () => true,
  }));

  Object.defineProperty(window, 'matchMedia', {
    value: matchMediaMock,
    configurable: true,
    writable: true,
  });

  return {
    setDark(nextDark: boolean) {
      matches = nextDark;
      const event = { matches: nextDark, media: '(prefers-color-scheme: dark)' } as MediaQueryListEvent;
      for (const listener of listeners) {
        listener(event);
      }
    },
  };
}

function renderApp(initialPath = '/settings') {
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

describe('Color theme selection (light / dark / follow system)', () => {
  it('defaults to follow system, reacts to OS theme changes, and persists light/dark/auto via Settings', async () => {
    const media = createMatchMediaController(true);
    const patchPayloads: string[] = [];
    let currentTheme = '';

    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = init?.method ?? 'GET';

      if (url === '/api/v1/me' && method === 'GET') {
        return jsonResponse({
          id: 1,
          name: 'Yan Li',
          email: 'yan@example.com',
          timezone: 'Asia/Shanghai',
          lang: '',
          theme: currentTheme,
        });
      }
      if (url === '/api/v1/me' && method === 'PATCH') {
        const body = String(init?.body ?? '');
        patchPayloads.push(body);
        const parsed = JSON.parse(body) as { theme?: string };
        if (parsed.theme !== undefined) currentTheme = parsed.theme;
        return jsonResponse({
          id: 1,
          name: 'Yan Li',
          email: 'yan@example.com',
          timezone: 'Asia/Shanghai',
          lang: '',
          theme: currentTheme,
        });
      }
      if (url === '/api/v1/availability' || url === '/api/v1/preferences/availability') {
        return jsonResponse({
          version: 'a1',
          available_windows: [],
          protected_windows: [],
          preferred_windows: [],
          buffer_minutes: 10,
          max_goal_minutes_per_day: 120,
        });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();

    renderApp('/settings');

    const themeSelect = (await screen.findByLabelText('外观')) as HTMLSelectElement;
    expect(themeSelect.value).toBe('auto');
    expect(document.documentElement.dataset.theme).toBe('dark');
    expect(document.documentElement.dataset.themePref).toBe('auto');

    // System switches to light while in auto mode
    act(() => {
      media.setDark(false);
    });
    expect(document.documentElement.dataset.theme).toBe('light');

    // Switch explicitly to dark
    await user.selectOptions(themeSelect, 'dark');
    expect(document.documentElement.dataset.theme).toBe('dark');
    expect(document.documentElement.dataset.themePref).toBe('dark');
    expect(localStorage.getItem('goalcal.theme')).toBe('dark');
    expect(document.cookie).toContain('goalcal_theme=dark');
    await waitFor(() => {
      expect(patchPayloads).toContain(JSON.stringify({ theme: 'dark' }));
    });

    // Switch explicitly to light
    await user.selectOptions(themeSelect, 'light');
    expect(document.documentElement.dataset.theme).toBe('light');
    expect(document.documentElement.dataset.themePref).toBe('light');
    expect(localStorage.getItem('goalcal.theme')).toBe('light');
    expect(document.cookie).toContain('goalcal_theme=light');
    await waitFor(() => {
      expect(patchPayloads).toContain(JSON.stringify({ theme: 'light' }));
    });

    // Switch back to follow system (while OS is dark again)
    act(() => {
      media.setDark(true);
    });
    expect(document.documentElement.dataset.theme).toBe('light');

    await user.selectOptions(themeSelect, 'auto');
    expect(document.documentElement.dataset.theme).toBe('dark');
    expect(document.documentElement.dataset.themePref).toBe('auto');
    expect(localStorage.getItem('goalcal.theme')).toBe('auto');
    expect(document.cookie).not.toContain('goalcal_theme=');
    await waitFor(() => {
      expect(patchPayloads).toContain(JSON.stringify({ theme: '' }));
    });
  });

  it('syncs server theme preference on entry when user previously saved dark mode', async () => {
    createMatchMediaController(false);

    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input) => {
      const url = typeof input === 'string' ? input : input.toString();
      if (url === '/api/v1/me') {
        return jsonResponse({
          id: 1,
          name: 'Yan Li',
          email: 'yan@example.com',
          timezone: 'Asia/Shanghai',
          lang: '',
          theme: 'dark',
        });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    renderApp('/settings');

    const themeSelect = (await screen.findByLabelText('外观')) as HTMLSelectElement;
    await waitFor(() => {
      expect(themeSelect.value).toBe('dark');
    });
    expect(document.documentElement.dataset.theme).toBe('dark');
    expect(localStorage.getItem('goalcal.theme')).toBe('dark');
  });

  it('defaults calendar palette to calm and persists palette switching via Settings', async () => {
    createMatchMediaController(false);
    localStorage.removeItem('goalcal.calPalette');
    const patchPayloads: string[] = [];
    let currentPalette = '';

    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async (input, init) => {
      const url = typeof input === 'string' ? input : input.toString();
      const method = init?.method ?? 'GET';
      if (url === '/api/v1/me' && method === 'GET') {
        return jsonResponse({
          id: 1,
          name: 'Yan Li',
          email: 'yan@example.com',
          timezone: 'Asia/Shanghai',
          lang: '',
          theme: '',
          calendar_palette: currentPalette,
        });
      }
      if (url === '/api/v1/me' && method === 'PATCH') {
        const body = String(init?.body ?? '');
        patchPayloads.push(body);
        const parsed = JSON.parse(body) as { calendar_palette?: string };
        if (parsed.calendar_palette !== undefined) currentPalette = parsed.calendar_palette;
        return jsonResponse({
          id: 1,
          name: 'Yan Li',
          email: 'yan@example.com',
          timezone: 'Asia/Shanghai',
          lang: '',
          theme: '',
          calendar_palette: currentPalette,
        });
      }
      return jsonResponse({});
    });

    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    renderApp('/settings');

    const paletteSelect = (await screen.findByLabelText('日历配色')) as HTMLSelectElement;
    expect(paletteSelect.value).toBe('calm');
    expect(document.documentElement.dataset.calPalette).toBe('calm');

    await user.selectOptions(paletteSelect, 'mono');
    expect(document.documentElement.dataset.calPalette).toBe('mono');
    expect(localStorage.getItem('goalcal.calPalette')).toBe('mono');
    await waitFor(() => {
      expect(patchPayloads).toContain(JSON.stringify({ calendar_palette: 'mono' }));
    });

    await user.selectOptions(paletteSelect, 'vivid');
    expect(document.documentElement.dataset.calPalette).toBe('vivid');
    expect(localStorage.getItem('goalcal.calPalette')).toBe('vivid');
    await waitFor(() => {
      expect(patchPayloads).toContain(JSON.stringify({ calendar_palette: 'vivid' }));
    });
  });

  it('maps 6-digit, 8-digit CalDAV, and neutral colors to native editorial mineral pigments', () => {
    expect(getEditorialPigmentKey('#ff2968ff')).toBe('rose');
    expect(getEditorialPigmentKey('#d97706')).toBe('ochre');
    expect(getEditorialPigmentKey('#f6c33b')).toBe('sand');
    expect(getEditorialPigmentKey('#10b981')).toBe('sage');
    expect(getEditorialPigmentKey('#0ea5e9')).toBe('blue');
    expect(getEditorialPigmentKey('#9333ea')).toBe('iris');
    expect(getEditorialPigmentKey('#808080')).toBe('slate');
    expect(getEditorialPigmentKey(null)).toBe('slate');
    expect(getDisplaySourceColor('#ff0000', 'calm')).toBe('#b86b68');
    expect(getDisplaySourceColor('#ff0000', 'vivid')).toBe('#ff0000');
  });
});
