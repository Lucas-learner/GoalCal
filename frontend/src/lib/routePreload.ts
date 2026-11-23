import type { QueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { queryKeys } from '../api/queryKeys';

export const loadGoalsPage = () => import('../pages/GoalsPage');
export const loadNewGoalPage = () => import('../pages/NewGoalPage');
export const loadHabitsPage = () => import('../pages/HabitsPage');
export const loadCalendarPage = () => import('../pages/CalendarPage');
export const loadSettingsPage = () => import('../pages/SettingsPage');
export const loadAssistantDrawer = () => import('../components/AssistantDrawer');

export function prefetchRouteData(pathname: string, queryClient: QueryClient): void {
  if (pathname === '/' || pathname === '/today') {
    void queryClient.prefetchQuery({ queryKey: queryKeys.today, queryFn: api.getToday });
    return;
  }
  if (pathname === '/goals' || /^\/goals\/\d+$/.test(pathname)) {
    void queryClient.prefetchQuery({ queryKey: queryKeys.goals, queryFn: api.getGoals });
    return;
  }
  if (pathname === '/habits' || /^\/habits\/\d+$/.test(pathname)) {
    void queryClient.prefetchQuery({ queryKey: queryKeys.habits, queryFn: api.getHabits });
  }
}

export function preloadRoute(to: string, queryClient: QueryClient): void {
  if (to === '/' || to === '/today') {
    prefetchRouteData('/', queryClient);
    return;
  }
  if (to === '/goals') {
    void loadGoalsPage();
    prefetchRouteData('/goals', queryClient);
    return;
  }
  if (to === '/habits') {
    void loadHabitsPage();
    prefetchRouteData('/habits', queryClient);
    return;
  }
  if (to === '/calendar') {
    void loadCalendarPage();
    return;
  }
  if (to === '/settings') {
    void loadSettingsPage();
  }
}
