import { lazy, Suspense, useEffect } from 'react';
import { BrowserRouter, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from '@tanstack/react-query';
import { ApiError, api } from './api/client';
import { queryKeys } from './api/queryKeys';
import { AppShell } from './components/AppShell';
import { ErrorBanner, SkeletonCard } from './components/StateFeedback';
import { useI18n } from './i18n';
import {
  loadCalendarPage,
  loadGoalsPage,
  loadHabitsPage,
  loadNewGoalPage,
  loadSettingsPage,
  prefetchRouteData,
} from './lib/routePreload';
import { TodayPage } from './pages/TodayPage';
import { AppStateProvider } from './state/AppStateContext';
import { useTheme } from './theme';

const CalendarPage = lazy(() => loadCalendarPage().then((module) => ({ default: module.CalendarPage })));
const GoalsPage = lazy(() => loadGoalsPage().then((module) => ({ default: module.GoalsPage })));
const NewGoalPage = lazy(() => loadNewGoalPage().then((module) => ({ default: module.NewGoalPage })));
const HabitsPage = lazy(() => loadHabitsPage().then((module) => ({ default: module.HabitsPage })));
const NewHabitPage = lazy(() => loadHabitsPage().then((module) => ({ default: module.NewHabitPage })));
const SettingsPage = lazy(() => loadSettingsPage().then((module) => ({ default: module.SettingsPage })));

export function shouldRetryQuery(failureCount: number, error: unknown): boolean {
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
    return false;
  }
  return failureCount < 2;
}

export function createAppQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        refetchOnWindowFocus: true,
        retry: shouldRetryQuery,
        retryDelay: (attemptIndex) => Math.min(400 * 2 ** attemptIndex, 2000),
      },
    },
  });
}

export function AppContent() {
  const { t, syncFromUser } = useI18n();
  const { syncFromUser: syncThemeFromUser } = useTheme();
  const location = useLocation();
  const queryClient = useQueryClient();

  const meQuery = useQuery({
    queryKey: queryKeys.me,
    queryFn: api.getMe,
    staleTime: 5 * 60_000,
  });

  // Start fetching the initial route's primary data in parallel with /me
  // to avoid a 2-RTT waterfall on cold page loads.
  useEffect(() => {
    prefetchRouteData(location.pathname, queryClient);
  }, [location.pathname, queryClient]);

  const user = meQuery.data;
  const timezone = user?.timezone || 'UTC';

  useEffect(() => {
    if (user?.lang !== undefined) {
      syncFromUser(user.lang);
    }
  }, [user?.lang, syncFromUser]);

  useEffect(() => {
    if (user?.theme !== undefined) {
      syncThemeFromUser(user.theme);
    }
  }, [user?.theme, syncThemeFromUser]);

  return (
    <AppShell user={user}>
      {meQuery.isLoading ? (
        <SkeletonCard label={t('正在加载账号信息', 'Loading account')} />
      ) : meQuery.isError ? (
        <div style={{ marginTop: 12 }}>
          <ErrorBanner error={meQuery.error} onRetry={() => void meQuery.refetch()} />
        </div>
      ) : (
        <Suspense fallback={<SkeletonCard label={t('正在加载页面', 'Loading page')} />}><Routes>
          <Route path="/" element={<TodayPage fallbackTimezone={timezone} />} />
          <Route path="/today" element={<TodayPage fallbackTimezone={timezone} />} />
          <Route path="/goals" element={<GoalsPage timezone={timezone} />} />
          <Route path="/goals/new" element={<NewGoalPage timezone={timezone} />} />
          <Route path="/goals/:goalId" element={<GoalsPage timezone={timezone} />} />
          <Route path="/habits" element={<HabitsPage />} />
          <Route path="/habits/new" element={<NewHabitPage timezone={timezone} />} />
          <Route path="/habits/:habitId" element={<HabitsPage />} />
          <Route path="/calendar" element={<CalendarPage timezone={timezone} />} />
          <Route path="/settings" element={<SettingsPage user={user} />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes></Suspense>
      )}
    </AppShell>
  );
}

const defaultQueryClient = createAppQueryClient();

export function App() {
  return (
    <QueryClientProvider client={defaultQueryClient}>
      <AppStateProvider>
        <BrowserRouter basename="/app">
          <AppContent />
        </BrowserRouter>
      </AppStateProvider>
    </QueryClientProvider>
  );
}

export default App;
