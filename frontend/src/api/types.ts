export interface User {
  id: number;
  name: string;
  email?: string | null;
  timezone: string;
}

export type StepStatus = 'todo' | 'done' | 'dropped';

export interface Step {
  id: number;
  goal_id: number;
  title: string;
  outcome: string;
  est_min: number;
  status: StepStatus | string;
  milestone: string;
  version: string;
  scheduled_event?: CalendarEvent | null;
  energy?: number;
  kind?: string;
  seq?: number;
}

export type GoalStatus = 'active' | 'done' | 'paused' | 'archived';

export interface Goal {
  id: number;
  title: string;
  outcome: string;
  deadline: string | null;
  status: GoalStatus | string;
  pinned: boolean;
  version: string;
  progress: number;
  steps: Step[];
  outline?: string[];
  template_key?: string;
}

export type EventKind = 'block' | 'external' | 'buffer';
export type EventStatus = 'planned' | 'done' | 'partial' | 'missed' | 'skipped' | 'moved';

export interface CalendarEvent {
  id: number;
  title: string;
  start: string;
  end: string;
  kind: EventKind | string;
  status: EventStatus | string;
  step_id: number | null;
  goal_id: number | null;
  goal_title: string | null;
  version: string;
}

export type ExecutionStatus = 'running' | 'completed' | 'adjusted';

export interface Execution {
  id: string | number;
  event_id: number | null;
  step_id: number;
  goal_title: string;
  step_title: string;
  started_at: string;
  expected_end_at: string;
  status: ExecutionStatus | string;
  version: string;
}

export interface ProposalCandidate {
  start: string;
  end: string;
  reasons: string[];
  score: number;
}

export interface Proposal {
  id: string;
  version: string;
  status: string;
  goal_id: number;
  goal_title: string;
  step_id: number;
  step_title: string;
  expires_at: string;
  candidates: ProposalCandidate[];
}

export interface MutationResult {
  message: string;
  event?: CalendarEvent;
  execution?: Execution;
  proposal?: Proposal;
  goal?: Goal;
  operation_id?: string;
}

export interface DraftStep {
  title: string;
  outcome: string;
  est_min: number;
  energy: number;
  kind: string;
  milestone: string;
}

export interface Draft {
  id: string;
  title: string;
  deadline: string | null;
  outline: string[];
  steps: DraftStep[];
  source: string;
  version: string;
}

export interface TimeWindow {
  days: number[];
  start: string;
  end: string;
}

export interface Availability {
  available_windows: TimeWindow[];
  protected_windows: TimeWindow[];
  preferred_windows: TimeWindow[];
  buffer_minutes: number;
  max_goal_minutes_per_day: number;
  version: string;
}

export interface AssistantMessage {
  id: string | number;
  role: 'user' | 'assistant' | string;
  content: string;
}

export interface CreateGoalIntent {
  title: string;
  deadline?: string | null;
}

export interface AssistantState {
  messages: AssistantMessage[];
  proposal: Proposal | null;
  version: string;
  create_goal?: CreateGoalIntent;
}

export interface AssistantTurnResponse extends AssistantState {
  message?: string;
  operation_id?: string;
}

export interface TodayNext {
  goal: Goal;
  step: Step;
  event: CalendarEvent | null;
}

export interface TodayResponse {
  date: string;
  timezone: string;
  next: TodayNext | null;
  execution: Execution | null;
  recovery: CalendarEvent[];
  events: CalendarEvent[];
}

export interface GoalsListResponse {
  goals: Goal[];
}

export interface CalendarEventsResponse {
  events: CalendarEvent[];
}

export interface StepUpdateResponse {
  message: string;
  goal: Goal;
  operation_id?: string;
}

export interface IcsImportResponse {
  message: string;
  count: number;
}

export interface MetricsResponse {
  acceptance_rate?: number | null;
  recovery_rate_72h?: number | null;
  progress_rate?: number | null;
  acceptance?: number | null;
  recovery_72h?: number | null;
  progress?: number | null;
  [key: string]: unknown;
}
