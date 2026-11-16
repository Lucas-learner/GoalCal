# Iteration plan and collaboration

Baseline: current workspace preserved in sibling original-workspace.tar.gz with snapshot.json SHA256 and separate SQLite online backups. Original root untouched. Independent repo baseline 61399c4. Baseline tests: 660 passed.

Controller owns contract, auth/transaction/tenancy/migrations, integration, independent tests and acceptance. Kimi 3.1 Pro reviews approved prototypes and fills implementation states. GPT-6.1-sol implements bounded core/UI modules in separate worktrees. Gemini 4 Hydrogen is not listed by the configured OpenCode runtime; no silent substitution or new paid provider. At most two workers concurrently. Existing visual direction is frozen.

Milestones: contract + baseline; service and application shell; scheduling/undo/preferences; goal creation/calendar; execution/assistant; security/regression/browser QA; release candidate and rollback instructions. Publishing remains a human decision. Routine/repeating Event/notifications/SSE deferred. Old Streamlit retained at /. New entry /app/ is opt-in. APIs are same-process mounts in existing Starlette gateway, avoiding a redundant extra server/process. New table migration is additive; old binary schema-version guard requires rollback to pre-migration database, while the new binary can serve old UI. Do not claim old-binary rollback preserves new writes without an explicit data conversion.

Validation uses temporary/test database or independent demo database; never production writes. All model output reviewed; bounded retry only; critical failures resolved by controller.
