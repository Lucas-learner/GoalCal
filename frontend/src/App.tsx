import { lazy, Suspense } from 'react';
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query';
import { ApiError, api } from './api/client';
import { queryKeys } from './api/queryKeys';
import { AppShell } from './components/AppShell';
import { ErrorBanner, SkeletonCard } from './components/StateFeedback';
const CalendarPage = lazy(() => import('./pages/CalendarPage').then((module) => ({ default: module.CalendarPage })));
import { GoalsPage } from './pages/GoalsPage';
import { NewGoalPage } from './pages/NewGoalPage';
import { SettingsPage } from './pages/SettingsPage';
import { TodayPage } from './pages/TodayPage';
import { AppStateProvider } from './state/AppStateContext';

export function createAppQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 15_000,
        refetchOnWindowFocus: true,
        retry: (failureCount, error) => {
          if (error instanceof ApiError && (error.status === 401 || error.status === 403 || error.status === 404)) {
            return false;
          }
          return failureCount < 1;
        },
      },
    },
  });
}

export function AppContent() {
  const meQuery = useQuery({
    queryKey: queryKeys.me,
    queryFn: api.getMe,
  });

  const user = meQuery.data;
  const timezone = user?.timezone || 'UTC';

  return (
    <AppShell user={user}>
      {meQuery.isLoading ? (
        <SkeletonCard label="正在加载账号信息" />
      ) : meQuery.isError ? (
        <div style={{ marginTop: 12 }}>
          <ErrorBanner error={meQuery.error} onRetry={() => void meQuery.refetch()} />
        </div>
      ) : (
        <Suspense fallback={<SkeletonCard label="正在加载页面" />}><Routes>
          <Route path="/" element={<TodayPage fallbackTimezone={timezone} />} />
          <Route path="/today" element={<TodayPage fallbackTimezone={timezone} />} />
          <Route path="/goals" element={<GoalsPage timezone={timezone} />} />
          <Route path="/goals/new" element={<NewGoalPage timezone={timezone} />} />
          <Route path="/goals/:goalId" element={<GoalsPage timezone={timezone} />} />
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
