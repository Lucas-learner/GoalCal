> 历史迭代约束（归并前）。当前上线状态、根路径和运维以 [生产手册](../../production.md) 为准；当时的 design-preview 原型已删除，当前视觉依据为 [DESIGN](../../DESIGN.md)。

# GoalCal web iteration contract

Approved product: warm moon-white #FAFAF8, surface #FFFEFD, ink #1D1D1F, primary #303638, mist-blue #6B7F89. Follow design-preview/today-balanced.html and pages.html. Today single column ~1040px; Goals master-detail; Calendar week + backlog / mobile day; Settings; global assistant right drawer / mobile sheet. NO visible discussion-context manager. Input remains usable when candidates exist. Select means execute, then undo. Explicit goal name in results. Old Streamlit stays at /, new app at /app/. No production deployment.

API prefix /api/v1. JSON bodies, errors {error:{code,message}}, credentials same-origin HttpOnly cookie. Every write uses Idempotency-Key (unique per logical operation, retry with same key). Server authenticates and runs tenant-scoped transaction; version conflicts 409, validation 422, unauth 401. No user_id accepted. Versions are opaque strings. Datetimes UTC ISO with Z, UI uses user.timezone; API accepts aware ISO only.

DTOs:
- User {id, name, timezone}
- Step {id,goal_id,title,outcome,est_min,status,milestone,version}
- Goal {id,title,outcome,deadline,status,pinned,version,progress,steps:Step[]}; progress 0..1
- Event {id,title,start,end,kind,status,step_id,goal_id,goal_title,version}; kind block|external|buffer
- Execution {id,event_id,step_id,goal_title,step_title,started_at,expected_end_at,status,version}; status running|completed|adjusted. Waiting checkin inferred from time; no background timer necessary.
- Proposal {id,version,status,goal_id,goal_title,step_id,step_title,expires_at,candidates:[{start,end,reasons,score}]}
- Mutation {message,event?:Event,execution?:Execution,proposal?:Proposal,goal?:Goal,operation_id?:string}
- Draft {id,title,deadline,outline:string[],steps:[{title,outcome,est_min,energy,kind,milestone}],source,version}
- Availability {available_windows:[{days:number[],start:string,end:string}],protected_windows:[same],preferred_windows:[same],buffer_minutes,max_goal_minutes_per_day,version}; days Monday=0..Sunday=6; HH:MM; end can 24:00; same-day windows; empty available means no slots. Legacy day_start/end and lunch materialize when new prefs absent. Preferred affects ranking only. Stored as one JSON pref key web_availability scheduler scope. All new/old scheduler calls must enforce these windows once set.
- AssistantState {messages:[{id,role,content}],proposal:Proposal|null,version:string,create_goal?:{title,deadline?}}

Endpoints (all GET read-only):
GET /me -> User
GET /today -> {date,timezone,next:{goal:Goal,step:Step,event:Event|null}|null,execution:Execution|null,recovery:Event[],events:Event[]}
GET /goals -> {goals:Goal[]}
GET /goals/{id} -> Goal
POST /goal-drafts {title,deadline} -> Draft (generate server draft; fallback templates disclosed in source)
POST /goals {draft_id,version,title,deadline,steps:Draft.steps} -> Mutation with goal; frontend save+schedule uses returned first step proposal endpoint
PATCH /goals/{id} {version,title?,status?,pinned?} -> Mutation with goal
PATCH /steps/{id} {version,title?,est_min?,status?} -> {message,goal}; allowed status todo|done|dropped
GET /calendar/events?start=ISO&end=ISO -> {events:Event[]}; <=93days
POST /calendar/events {title,start,end} -> Mutation (external)
PATCH /events/{id} {version,start,end} -> Mutation with operation_id (move/resize, overlap rejected)
POST /events/{id}/cancel {version} -> Mutation with operation_id (soft cancellation status skipped, restorable)
POST /steps/{id}/proposals {window_start?:ISO,window_end?:ISO} -> Proposal; supersedes prior open proposals for user
POST /proposals/{id}/accept {version,index:number} -> Mutation; excludes same step's open blocks from conflicts, soft-cancels those, retains completed history; atomic and revalidated against prefs/state/deadline/conflicts; index zero based
POST /operations/{id}/undo {} -> Mutation; refuses if affected events modified or restoration conflicts; accepted proposal not reopened
POST /events/{id}/start {version} -> Mutation with execution; actual now..now+original duration; revalidate conflicts/windows; one active session/user
POST /steps/{id}/start {version} -> Mutation with execution; same logic for unscheduled next step
POST /execution-sessions/{id}/checkin {version,outcome:'done'|'extend'|'adjust'} -> Mutation; extend +15 min with revalidation; adjust ends session and resolves event as partial (step remains todo)
POST /events/{id}/checkin {version,outcome:'done'|'missed'|'partial'} -> Mutation; unresolved overdue block only; dedupe against active executions
GET /preferences/availability -> Availability
PUT /preferences/availability Availability -> Availability
GET /assistant -> AssistantState
POST /assistant/turns {text,version,page_goal_id?:number} -> AssistantState & {message?,operation_id?}; optimistic conversation version, natural references and ambiguity, "第一个" uses same accept method. New turn supersedes old candidates. Creating goal returns create_goal entry for SAME structured flow, never separate auto creation.
POST /calendar/import {ics:string} -> {message,count}; validate size, reuse existing import
GET /metrics -> existing aggregate DTO for settings

Backend service interface (worker implements goalcal/web/core.py): Service(session, user_id:int, tz:str, now:aware datetime). Methods me(), today(), goals(), goal(id), draft(body), create_goal(body), update_goal(id,body), update_step(id,body), events(start,end), create_event(body), move_event(id,body), cancel_event(id,body), propose(step_id,body), accept(proposal_id,body), undo(operation_id), start_event(id,body), start_step(id,body), checkin_execution(id,body), checkin_event(id,body), preferences(), save_preferences(body). Return dict. Raise ApiError(code,message,status=409). No commits or raw SQL; controller owns transaction/auth/idempotency. Methods may flush. now UTC aware; convert existing UTC-naive model dates carefully. Core must offer helpers for serialization/version checking usable by assistant.

New models worker owns goalcal/web/models.py, imported by controller into migrations/tenancy: WebProposal(id string,user_id,step_id,goal_id,status,version integer,payload_json,expires_at UTC-naive), WebOperation(id string,user_id,payload_json,undone bool), WebExecution(id string,user_id,event_id,step_id,status,started_at,expected_end_at,resolved_at,version integer), WebDraft(id string,user_id,payload_json,version integer,goal_id optional). Controller adds request/assistant tables separately. Avoid hard deletes of events used by new execution references; parent will integrate legacy delete compatibility.

Controller handles authenticated HTTP, CSRF Origin, bounded body, route validators, atomic transaction with BEGIN IMMEDIATE for writes, persistent idempotency table including payload hash/result; frontend always error handles. LLM calls must not hold SQLite writer lock; draft preparation can run before transaction with auth/quota, then persist. Service.draft initially supports deterministic template; controller integrates LLM prepare flow. Workers cannot modify existing gateway, tenancy, migration or shared interfaces unilaterally.

Safety tests: tenant isolation; same key identical replay/different payload rejected; stale candidates; concurrent accept; undo restoration and subsequent edit rejection; start/reentry/checkin dedupe; conflicting extension; available/protected/preferred semantics; old prefs preserved; no state_json exposed. UI: keyboard-accessible dialogs and focus, error/retry/empty states, small viewport, no fabricated data or fake successful buttons.
