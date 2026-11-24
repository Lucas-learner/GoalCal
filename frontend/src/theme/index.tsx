import React, { createContext, useCallback, useContext, useEffect, useState } from 'react';

export type Theme = 'light' | 'dark';
export type ThemePreference = 'auto' | 'light' | 'dark';
export type CalendarPalette = 'calm' | 'mono' | 'vivid';

const STORAGE_KEY = 'goalcal.theme';
const PALETTE_STORAGE_KEY = 'goalcal.calPalette';
const COOKIE_NAME = 'goalcal_theme';
const DARK_MEDIA_QUERY = '(prefers-color-scheme: dark)';

export function normalizeThemePref(value: string | null | undefined): ThemePreference {
  if (value === 'light' || value === 'dark') return value;
  return 'auto';
}

export function normalizeCalendarPalette(value: string | null | undefined): CalendarPalette {
  if (value === 'mono' || value === 'vivid') return value;
  return 'calm';
}

function readPaletteStoragePref(): CalendarPalette | null {
  try {
    const stored = typeof localStorage !== 'undefined' ? localStorage.getItem(PALETTE_STORAGE_KEY) : null;
    if (stored === 'calm' || stored === 'mono' || stored === 'vivid') {
      return stored;
    }
  } catch {
    // Ignore storage access failures.
  }
  return null;
}

function applyCalendarPaletteSideEffects(palette: CalendarPalette): void {
  if (typeof document !== 'undefined') {
    document.documentElement.dataset.calPalette = palette;
  }
  try {
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(PALETTE_STORAGE_KEY, palette);
    }
  } catch {
    // Ignore storage access failures.
  }
}

export function detectSystemTheme(): Theme {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
    return 'light';
  }
  return window.matchMedia(DARK_MEDIA_QUERY).matches ? 'dark' : 'light';
}

export function resolveTheme(pref: ThemePreference, systemTheme: Theme = detectSystemTheme()): Theme {
  if (pref === 'light' || pref === 'dark') return pref;
  return systemTheme;
}

function readCookiePref(): ThemePreference | null {
  if (typeof document === 'undefined') return null;
  const match = document.cookie.match(/(?:^|;\s*)goalcal_theme=([^;]+)/);
  if (!match) return null;
  const val = decodeURIComponent(match[1]);
  return val === 'light' || val === 'dark' ? val : null;
}

function readStoragePref(): ThemePreference | null {
  try {
    const stored = typeof localStorage !== 'undefined' ? localStorage.getItem(STORAGE_KEY) : null;
    if (stored === 'light' || stored === 'dark' || stored === 'auto') {
      return stored;
    }
  } catch {
    // Ignore storage access failures.
  }
  return null;
}

function readInitialPref(): ThemePreference {
  return readCookiePref() ?? readStoragePref() ?? 'auto';
}

function applySideEffects(pref: ThemePreference, theme: Theme): void {
  if (typeof document !== 'undefined') {
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.themePref = pref;
    document.documentElement.style.colorScheme = theme;
    const secure = typeof location !== 'undefined' && location.protocol === 'https:' ? '; Secure' : '';
    if (pref === 'light' || pref === 'dark') {
      document.cookie = `${COOKIE_NAME}=${pref}; Path=/; Max-Age=${365 * 24 * 60 * 60}; SameSite=Lax${secure}`;
    } else {
      document.cookie = `${COOKIE_NAME}=; Path=/; Max-Age=0; SameSite=Lax${secure}`;
    }
  }
  try {
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(STORAGE_KEY, pref);
    }
  } catch {
    // Ignore storage access failures.
  }
}

interface ThemeContextValue {
  theme: Theme;
  pref: ThemePreference;
  calendarPalette: CalendarPalette;
  setPreference: (next: ThemePreference) => void;
  syncFromUser: (serverTheme: string | null | undefined) => void;
  setCalendarPalette: (next: CalendarPalette) => void;
  syncCalendarPaletteFromUser: (serverPalette: string | null | undefined) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [pref, setPref] = useState<ThemePreference>(readInitialPref);
  const [systemTheme, setSystemTheme] = useState<Theme>(detectSystemTheme);
  const [calendarPalette, setCalendarPaletteState] = useState<CalendarPalette>(
    () => readPaletteStoragePref() ?? 'calm',
  );
  const theme = resolveTheme(pref, systemTheme);

  useEffect(() => {
    applySideEffects(pref, theme);
  }, [pref, theme]);

  useEffect(() => {
    applyCalendarPaletteSideEffects(calendarPalette);
  }, [calendarPalette]);

  useEffect(() => {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return undefined;
    }
    const media = window.matchMedia(DARK_MEDIA_QUERY);
    setSystemTheme(media.matches ? 'dark' : 'light');

    const handleChange = (event: MediaQueryListEvent) => {
      setSystemTheme(event.matches ? 'dark' : 'light');
    };

    if (typeof media.addEventListener === 'function') {
      media.addEventListener('change', handleChange);
      return () => media.removeEventListener('change', handleChange);
    }
    media.addListener(handleChange);
    return () => media.removeListener(handleChange);
  }, []);

  const setPreference = useCallback((next: ThemePreference) => {
    const resolved = resolveTheme(next, detectSystemTheme());
    applySideEffects(next, resolved);
    setPref(next);
  }, []);

  const syncFromUser = useCallback((serverTheme: string | null | undefined) => {
    if (serverTheme === undefined) return;
    const normalized = normalizeThemePref(serverTheme);
    if (normalized === 'light' || normalized === 'dark') {
      const resolved = resolveTheme(normalized, detectSystemTheme());
      applySideEffects(normalized, resolved);
      setPref(normalized);
    }
  }, []);

  const setCalendarPalette = useCallback((next: CalendarPalette) => {
    applyCalendarPaletteSideEffects(next);
    setCalendarPaletteState(next);
  }, []);

  const syncCalendarPaletteFromUser = useCallback((serverPalette: string | null | undefined) => {
    if (!serverPalette) return;
    const normalized = normalizeCalendarPalette(serverPalette);
    applyCalendarPaletteSideEffects(normalized);
    setCalendarPaletteState(normalized);
  }, []);

  return (
    <ThemeContext.Provider
      value={{
        theme,
        pref,
        calendarPalette,
        setPreference,
        syncFromUser,
        setCalendarPalette,
        syncCalendarPaletteFromUser,
      }}
    >
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (ctx) return ctx;
  return {
    theme: detectSystemTheme(),
    pref: 'auto',
    calendarPalette: 'calm',
    setPreference: () => undefined,
    syncFromUser: () => undefined,
    setCalendarPalette: () => undefined,
    syncCalendarPaletteFromUser: () => undefined,
  };
}
