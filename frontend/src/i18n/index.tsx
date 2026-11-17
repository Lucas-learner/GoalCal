import React, { createContext, useCallback, useContext, useEffect, useState } from 'react';

export type Lang = 'zh' | 'en';
export type LangPreference = 'auto' | 'zh' | 'en';

const STORAGE_KEY = 'goalcal.lang';
const COOKIE_NAME = 'goalcal_lang';

export function normalizeLangPref(value: string | null | undefined): LangPreference {
  if (value === 'zh' || value === 'en') return value;
  return 'auto';
}

export function detectSystemLang(): Lang {
  if (typeof navigator === 'undefined') return 'zh';
  const raw = navigator.language || (navigator.languages && navigator.languages[0]) || 'zh';
  return /^zh\b/i.test(raw) ? 'zh' : 'en';
}

export function resolveLang(pref: LangPreference): Lang {
  if (pref === 'zh' || pref === 'en') return pref;
  return detectSystemLang();
}

function readCookiePref(): LangPreference | null {
  if (typeof document === 'undefined') return null;
  const match = document.cookie.match(/(?:^|;\s*)goalcal_lang=([^;]+)/);
  if (!match) return null;
  const val = decodeURIComponent(match[1]);
  return val === 'zh' || val === 'en' ? val : null;
}

function readStoragePref(): LangPreference | null {
  try {
    const stored = typeof localStorage !== 'undefined' ? localStorage.getItem(STORAGE_KEY) : null;
    if (stored === 'zh' || stored === 'en' || stored === 'auto') {
      return stored;
    }
  } catch {
    // Ignore storage access failures.
  }
  return null;
}

function readInitialPref(): LangPreference {
  // Login page /_goalcal/lang sets goalcal_lang cookie without touching localStorage,
  // so an explicit cookie choice must override any prior localStorage preference on entry.
  return readCookiePref() ?? readStoragePref() ?? 'auto';
}

let activeLang: Lang = resolveLang(readInitialPref());

export function getActiveLang(): Lang {
  return activeLang;
}

export function pickText(lang: Lang, zh: string, en: string): string {
  return lang === 'en' ? en : zh;
}

function applySideEffects(pref: LangPreference, lang: Lang): void {
  activeLang = lang;
  if (typeof document !== 'undefined') {
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
    const secure = typeof location !== 'undefined' && location.protocol === 'https:' ? '; Secure' : '';
    if (pref === 'zh' || pref === 'en') {
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

export function getLocalizedDayLabels(lang: Lang): string[] {
  return lang === 'en'
    ? ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    : ['周一', '周二', '周三', '周四', '周五', '周六', '周日'];
}

export function getStepKindLabel(kind: string | undefined, lang: Lang): string {
  const map: Record<string, [string, string]> = {
    deep: ['深度专注', 'Deep focus'],
    admin: ['轻务安排', 'Admin'],
    physical: ['身体行动', 'Physical'],
    social: ['沟通协作', 'Social'],
    research: ['调研资料', 'Research'],
  };
  const pair = map[kind || 'deep'];
  return pair ? pickText(lang, pair[0], pair[1]) : kind || '';
}

export function getEnergyLabel(energy: number | undefined, lang: Lang): string {
  const map: Record<number, [string, string]> = {
    0: ['低精力', 'Low energy'],
    1: ['中等精力', 'Medium energy'],
    2: ['高专注', 'High focus'],
  };
  const pair = map[energy ?? 1] ?? map[1];
  return pickText(lang, pair[0], pair[1]);
}

interface I18nContextValue {
  lang: Lang;
  pref: LangPreference;
  setPreference: (next: LangPreference) => void;
  syncFromUser: (serverLang: string | null | undefined) => void;
  t: (zh: string, en: string) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

export function I18nProvider({ children }: { children: React.ReactNode }) {
  const [pref, setPref] = useState<LangPreference>(readInitialPref);
  const lang = resolveLang(pref);

  useEffect(() => {
    applySideEffects(pref, lang);
  }, [pref, lang]);

  const setPreference = useCallback((next: LangPreference) => {
    const resolved = resolveLang(next);
    applySideEffects(next, resolved);
    setPref(next);
  }, []);

  const syncFromUser = useCallback((serverLang: string | null | undefined) => {
    if (serverLang === undefined) return;
    const normalized = normalizeLangPref(serverLang);
    if (normalized === 'zh' || normalized === 'en') {
      const resolved = resolveLang(normalized);
      applySideEffects(normalized, resolved);
      setPref(normalized);
    }
  }, []);

  const t = useCallback((zh: string, en: string) => (lang === 'en' ? en : zh), [lang]);

  return (
    <I18nContext.Provider value={{ lang, pref, setPreference, syncFromUser, t }}>
      {children}
    </I18nContext.Provider>
  );
}

export function useI18n(): I18nContextValue {
  const ctx = useContext(I18nContext);
  if (ctx) return ctx;
  const fallbackLang = getActiveLang();
  return {
    lang: fallbackLang,
    pref: 'auto',
    setPreference: () => undefined,
    syncFromUser: () => undefined,
    t: (zh: string, en: string) => (fallbackLang === 'en' ? en : zh),
  };
}
