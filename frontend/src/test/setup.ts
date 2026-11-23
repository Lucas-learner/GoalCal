import '@testing-library/jest-dom/vitest';
import { afterEach, beforeEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';

beforeEach(() => {
  Object.defineProperty(window.navigator, 'language', {
    value: 'zh-CN',
    configurable: true,
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  try {
    localStorage.clear();
  } catch {
    // ignore
  }
  document.cookie = 'goalcal_lang=; Path=/; Max-Age=0';
  document.cookie = 'goalcal_theme=; Path=/; Max-Age=0';
  document.documentElement.removeAttribute('data-theme');
  document.documentElement.removeAttribute('data-theme-pref');
  document.documentElement.style.colorScheme = '';
});
