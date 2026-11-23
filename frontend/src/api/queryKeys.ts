import type { QueryClient } from '@tanstack/react-query';

export const queryKeys = {
  me: ['me'] as const,
  today: ['today'] as const,
  goals: ['goals'] as const,
  goal: (id: number) => ['goals', id] as const,
  habits: ['habits'] as const,
  habit: (id: number, through?: string) => ['habits', id, through] as const,
  habitDay: (id: number, date: string) => ['habits', id, 'day', date] as const,
  habitSlots: (id: number, startDate: string, endDate: string, period: string, ignoreDate?: string) =>
    ['habits', id, 'slots', startDate, endDate, period, ignoreDate ?? ''] as const,
  calendarAll: ['calendar'] as const,
  calendarRange: (start: string, end: string) => ['calendar', start, end] as const,
  calendarSync: ['calendar-sync'] as const,
  availability: ['preferences', 'availability'] as const,
  assistant: ['assistant'] as const,
  metrics: ['metrics'] as const,
};

export async function invalidateAfterWrite(
  queryClient: QueryClient,
  options?: { goalId?: number | null; includeAvailability?: boolean },
): Promise<void> {
  const tasks: Promise<unknown>[] = [
    queryClient.invalidateQueries({ queryKey: queryKeys.today }),
    queryClient.invalidateQueries({ queryKey: queryKeys.goals }),
    queryClient.invalidateQueries({ queryKey: queryKeys.habits }),
    queryClient.invalidateQueries({ queryKey: queryKeys.calendarAll }),
    queryClient.invalidateQueries({ queryKey: queryKeys.assistant }),
    queryClient.invalidateQueries({ queryKey: queryKeys.metrics }),
  ];

  if (options?.goalId != null) {
    tasks.push(queryClient.invalidateQueries({ queryKey: queryKeys.goal(options.goalId) }));
  }
  if (options?.includeAvailability) {
    tasks.push(queryClient.invalidateQueries({ queryKey: queryKeys.availability }));
  }

  await Promise.all(tasks);
}
