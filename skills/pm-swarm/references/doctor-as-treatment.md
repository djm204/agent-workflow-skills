# Doctor-as-treatment PM-swarm pattern

## Trigger

Use this when liveness/PM Doctor reports only diagnoses while workers remain stuck, blocked, or waiting on stale evidence. A doctor that only says "ask PM" or "no obvious blocker" is insufficient when live PR/Codex/CI evidence shows actionable work.

## Pattern

1. Doctor must inspect worker card, recent events/heartbeats, live PR state, CI, Codex comments/reviews, and unresolved Codex review threads.
2. Doctor must post a concrete `DOCTOR TREATMENT` on the target worker, not just a PM summary.
3. If safe and mechanical, doctor should perform the treatment directly:
   - distinguish stale Codex usage-limit comments from active limits by comparing timestamps against current-head Codex reviews/triggers;
   - retrigger `@codex review`, unblock, and dispatch when a usage-limit blocker is retryable/stale and no human approval is required;
   - unblock non-human blocked cards;
   - dispatch ready/todo/unstable cards;
   - request approval via approval-cop for approval-required exact commands;
   - create a takeover doctor card with implementation/review-loop skills when code/PR work is required.
4. Doctor cards must be fixers/takeover agents, not pure reporters. They should actively patch/fix/reply/resolve/push/retrigger/merge within the original one-issue scope, or leave exact HITL approval.
5. Idempotency must avoid reusing a completed doctor card forever. Include an incident bucket or live evidence key so recurring stalls get a fresh runnable doctor after the liveness cooldown.
6. Doctor creation should immediately dispatch so the new card actually starts.
7. DSM entries should record symptom → treatment outcomes so future liveness can choose a treatment quickly.

## Pitfalls

- Do not keep creating diagnosis-only doctor cards that complete after commenting “worker is active.” If active evidence reveals new Codex findings, the treatment is to make the worker/doctor address them.
- Do not reuse a terminal doctor card via a static idempotency key; that makes later doctor dispatches look successful but do no work.
- When a doctor finishes/merges an issue PR but scoped tooling cannot complete the original stale worker card, liveness must auto-kill by checking live GitHub terminal state from the full worker detail/comments, not just latest summary: if the PR is merged/closed or the issue is closed, comment on the stale card, archive/terminate if possible, record it in `completed_worker_ids`, prune it from `worker_ids`, and run refill. Refill must treat `completed_worker_ids` as excluded so it cannot resurrect the stale card.
- When liveness detects a blocked/ready worker whose stale blocker has been superseded by a current-head Codex response, it should dispatch PM Swarm Doctor as a treatment, not just post another nudge. If an active doctor card is already running for the same worker/PR, replace the stale worker id in `worker_ids` with the active doctor id so future liveness ticks monitor the real fixer instead of repeatedly reporting the old ready card.
- Refill must not re-add deferred worker ids or recreate their issue via an idempotency key while the issue remains deferred; parse deferred issue numbers and skip them until the deferral is explicitly removed.
- Do not silently bypass human approval when the blocked command is dangerous and exact approval is required; route it through approval-cop or create an explicit HITL request.
- If a worker records a PR number plus exact Codex inline comment IDs but the long GitHub API command is truncated, approval-cop may synthesize a narrow helper command that replies/resolves only those IDs and retriggers `@codex review`; still require approval before executing it.
- Do not spawn duplicate code editors for the same PR if the original worker is alive and actively fixing; comment exact treatment and let the active worker proceed unless it stalls again.
