export interface User {
  id: number;
  name: string;
  email?: string | null;
  timezone: string;
  lang?: string;
  theme?: string;
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

export type EventKind = 'block' | 'external' | 'buffer' | 'habit';
export type EventStatus = 'planned' | 'done' | 'partial' | 'missed' | 'skipped' | 'moved';

export interface CalendarEvent {
  id: number;
  title: string;
  start: string;
  end: string;
  kind: EventKind | string;
  status: EventStatus | string;
  step_id: number | null;
  step_version?: string | null;
  goal_id: number | null;
  goal_title: string | null;
  version: string;
  habit?: HabitOccurrence;
}

export type HabitStatus = 'active' | 'paused' | 'archived';

export interface HabitRule {
  title: string;
  mode: 'fixed' | 'weekly';
  weekdays: number[];
  est_min: number;
  start_time: string | null;
  weekly_target: number | null;
}

export interface HabitOccurrence {
  id: string;
  habit_id: number;
  title: string;
  date: string;
  today: string;
  timezone: string;
  mode: HabitRule['mode'];
  est_min: number;
  status: 'pending' | 'done' | 'skipped' | 'missed';
  can_record: boolean;
  reason: string | null;
  event_id: number | null;
  start: string | null;
  end: string | null;
  version: string;
  habit_version: string;
}

export interface HabitProgress {
  habit_id: number;
  title: string;
  est_min: number;
  version: string;
  timezone: string;
  week: string;
  end: string;
  today: string;
  done: number;
  target: number;
  mode: HabitRule['mode'];
  schedule_from: string | null;
  partial: boolean;
  offer?: HabitOccurrence;
}

export interface Habit {
  id: number;
  status: HabitStatus;
  pinned?: boolean;
  goal_id: number | null;
  goal_title: string | null;
  goal_status: string | null;
  timezone: string;
  today: string;
  start_date: string;
  version: string;
  rule: HabitRule;
  upcoming_rule: { date: string; rule: HabitRule } | null;
  progress: HabitProgress | null;
  next: HabitOccurrence | null;
  next_unscheduled?: HabitOccurrence | null;
  next_week?: HabitProgress | null;
}

export interface HabitSlotCandidate {
  start: string;
  end: string;
  date: string;
  reasons: string[];
}

export interface HabitDetail extends Habit {
  through: string;
  week_items: HabitOccurrence[];
  history: HabitOccurrence[];
  weeks: HabitProgress[];
  scheduled: HabitOccurrence[];
}

export interface HabitPreview {
  effective_date: string;
  week: string;
  weekly_target: number | null;
  slots: { date: string; start: string | null; end: string | null; reason: string | null }[];
  replaces: string[];
}

export interface HabitActionBody {
  date: string;
  version?: string;
  habit_version?: string;
  action: 'schedule' | 'done' | 'skip' | 'cancel' | 'restore' | 'start';
  target_date?: string;
  start?: string;
  minutes?: number;
}

export interface HabitRange {
  items: HabitOccurrence[];
  weeks: HabitProgress[];
}

export type ExecutionStatus = 'running' | 'completed' | 'adjusted';

export interface Execution {
  id: string | number;
  event_id: number | null;
  step_id: number | null;
  habit_id?: number | null;
  habit_occurrence_id?: string | null;
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

export interface GoalIntakeOption {
  id: string;
  label: string;
  suggested_title?: string | null;
  suggested_deadline?: string | null;
  adjusted_scope?: string | null;
}

export interface GoalIntakeQuestion {
  id: string;
  prompt: string;
  options: GoalIntakeOption[];
}

export interface GoalIntakeResponse {
  needs_calibration: boolean;
  conflict_kind: 'none' | 'scope_vs_time' | 'vague_target' | 'missing_baseline' | string;
  suggested_title: string;
  suggested_outcome?: string;
  reason?: string;
  questions: GoalIntakeQuestion[];
  source?: string;
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

export interface CreateHabitIntent {
  title: string;
  weekdays?: number[];
  weekly_target?: number;
  est_min?: number;
  start_time?: string;
}

export interface ReplanGoalIntent {
  goal_id: number;
  goal_title: string;
  goal_version: string;
  deadline?: string | null;
  previous_deadline?: string | null;
  steps_changed?: boolean;
  outline: string[];
  steps: DraftStep[];
  source?: string;
  instruction?: string;
}

export interface AssistantState {
  messages: AssistantMessage[];
  proposal: Proposal | null;
  version: string;
  create_goal?: CreateGoalIntent;
  create_habit?: CreateHabitIntent;
  replan_goal?: ReplanGoalIntent;
}

export interface AssistantTurnResponse extends AssistantState {
  message?: string;
  operation_id?: string;
  goal?: Goal;
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
  habits?: HabitRange;
  habit_next?: HabitOccurrence | null;
}

export interface GoalsListResponse {
  goals: Goal[];
}

export interface CalendarEventsResponse {
  events: CalendarEvent[];
  habits?: HabitRange;
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

export type FeedbackCategory =
  | '功能问题'
  | 'AI 理解或计划问题'
  | '速度或连接问题'
  | '建议或其他';

export interface FeedbackResponse {
  id: number;
  message: string;
}
