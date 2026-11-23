"""Explicit browser input whitelist. Internal service parameters never cross this boundary."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class Input(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class Version(Input):
    version: str = Field(min_length=1, max_length=128)


class HabitInput(Input):
    title: str = Field(min_length=1, max_length=200)
    mode: Literal['fixed', 'weekly']
    est_min: int = Field(ge=5, le=480)
    weekdays: list[int] = Field(default_factory=list, max_length=7)
    start_time: str | None = Field(default=None, max_length=5)
    weekly_target: int | None = Field(default=None, ge=1, le=7)
    goal_id: int | None = None


class CreateHabit(HabitInput):
    start_date: str = Field(min_length=10, max_length=10)


class HabitPreview(HabitInput):
    start_date: str = Field(min_length=10, max_length=10)
    habit_id: int | None = None


class UpdateHabit(HabitInput, Version):
    effective_date: str | None = Field(default=None, min_length=10, max_length=10)


class HabitStatus(Version):
    status: Literal['active', 'paused', 'archived'] | None = None
    pinned: bool | None = None


class HabitAction(Input):
    date: str = Field(min_length=10, max_length=10)
    action: Literal['schedule', 'done', 'skip', 'cancel', 'restore', 'start']
    version: str | None = Field(default=None, max_length=128)
    habit_version: str | None = Field(default=None, max_length=128)
    target_date: str | None = Field(default=None, min_length=10, max_length=10)
    start: str | None = Field(default=None, max_length=64)
    minutes: int | None = Field(default=None, ge=5, le=480)


class IntakeInput(Input):
    description: str = Field(min_length=1, max_length=2000)
    deadline: str | None = Field(default=None, min_length=10, max_length=10)


class StepDefinition(Input):
    title: str = Field(min_length=1, max_length=200)
    outcome: str = Field(default='', max_length=2000)
    est_min: int = Field(ge=5, le=480)
    energy: int = Field(default=1, ge=0, le=2)
    kind: Literal['deep','admin','physical','social','research'] = 'deep'
    milestone: str = Field(default='', max_length=120)


class DraftInput(Input):
    title: str = Field(min_length=1, max_length=200)
    raw: str | None = Field(default=None, max_length=2000)
    level: str | None = Field(default=None, max_length=120)
    target: str | None = Field(default=None, max_length=400)
    instruction: str | None = Field(default=None, max_length=600)
    current_steps: list[StepDefinition] | None = Field(default=None, max_length=60)
    current_outline: list[str] | None = Field(default=None, max_length=10)
    deadline: str | None = Field(default=None, min_length=10, max_length=10)


class CreateGoal(Version, DraftInput):
    draft_id: str = Field(min_length=1, max_length=40)
    steps: list[StepDefinition] = Field(min_length=1, max_length=60)


class GoalEdit(Version):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal['active','paused','archived','done'] | None = None
    pinned: bool | None = None
    deadline: str | None = Field(default=None, max_length=10)


class ReplanGoal(Version):
    steps: list[StepDefinition] | None = Field(default=None, min_length=1, max_length=40)
    outline: list[str] | None = Field(default=None, max_length=20)
    deadline: str | None = Field(default=None, max_length=10)


class StepEdit(Version):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    est_min: int | None = Field(default=None, ge=5, le=480)
    status: Literal['todo','done','dropped'] | None = None


class EventTimes(Input):
    start: str = Field(max_length=64)
    end: str = Field(max_length=64)


class CreateEvent(EventTimes):
    title: str = Field(min_length=1, max_length=200)


class MoveEvent(EventTimes, Version):
    pass


class Propose(Input):
    window_start: str | None = Field(default=None, max_length=64)
    window_end: str | None = Field(default=None, max_length=64)


class ScheduleStep(Version):
    start: str = Field(max_length=64)


class Accept(Version):
    index: int = Field(ge=0, le=5)


class ExecutionCheckin(Version):
    outcome: Literal['done','extend','adjust']


class EventCheckin(Version):
    outcome: Literal['done','missed','partial']


class Window(Input):
    days: list[int] = Field(min_length=1, max_length=7)
    start: str = Field(pattern=r'^([01][0-9]|2[0-3]):[0-5][0-9]$')
    end: str = Field(pattern=r'^(([01][0-9]|2[0-3]):[0-5][0-9]|24:00)$')


class Availability(Version):
    available_windows: list[Window] = Field(max_length=42)
    protected_windows: list[Window] = Field(max_length=42)
    preferred_windows: list[Window] = Field(max_length=42)
    buffer_minutes: int = Field(ge=0, le=120)
    max_goal_minutes_per_day: int = Field(ge=1, le=1440)


class Turn(Version):
    text: str = Field(min_length=1, max_length=4000)
    page_goal_id: int | None = Field(default=None, ge=1)


class ImportCalendar(Input):
    ics: str = Field(min_length=1, max_length=5*1024*1024)


class UpdateMe(Input):
    lang: Literal['', 'zh', 'en'] | None = None
    theme: Literal['', 'light', 'dark'] | None = None


class SubmitFeedback(Input):
    category: Literal['功能问题', 'AI 理解或计划问题', '速度或连接问题', '建议或其他']
    message: str = Field(min_length=5, max_length=2000)
    operation_id: str | None = Field(default=None, max_length=16)
