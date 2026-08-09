# All-Worker Liveness Audit / Closeout Refocus

Use this when the user says the swarm feels stuck, idle, or churning across multiple workers rather than one named card.

## Trigger signs

- Liveness repeatedly shows `todo`/`ready` broad worker shards while open PRs exist.
- Liveness comments/nudges the same non-running cards every cycle.
- A broad worker's latest summary references a PR that is already merged.
- Multiple cards point at the same PR while other open PRs have no focused owner.
- The PR closeout watchdog lists actionable open PRs, but liveness only tracks stale broad roots.

## Audit procedure

1. Inspect all configured `worker_ids` from the project liveness config.
2. Inspect active Kanban cards for the tenant, not only configured workers:
   - running/ready/todo/blocked status
   - latest summary
   - recent runs/events/comments
   - worker PID where present
3. Inspect live GitHub PR state for every open issue-swarm PR:
   - open/merged/closed
   - merge state
   - CI status
   - current head SHA
   - current-head Codex review threads via GraphQL, not only `gh pr view` summary fields.
4. Classify each tracked worker:
   - **active focused closeout**: keep and monitor.
   - **true human blocker**: keep as blocked if it has an exact blocked command / approval gate.
   - **merged/stale broad root**: remove from liveness scope and archive/terminate if appropriate.
   - **duplicate closeout**: block with a non-auto-promoting blocker and kill only the duplicate process.
   - **unowned actionable PR**: create a fresh focused closeout card.

## Treatment pattern

- Replace stale broad `worker_ids` with one focused card per actionable open PR.
- Each focused card should contain live evidence in the body: PR number, issue number, worktree path, merge/CI state, head SHA, and concise current-head Codex findings.
- During closeout freeze, do not keep nudging broad shards that should not start new issue work. Liveness should monitor active PR closeout cards and true human blockers instead.
- Dispatch the new focused cards and verify real worker PIDs plus fresh heartbeats before reporting that the swarm is active.
- Run liveness after the refocus and ensure the report no longer contains stale `todo`/`ready` worker noise.
- If a focused worker completes and merges its PR during the audit, allow liveness to archive/prune it, then verify the `worker_ids` list has been rewritten.

## Reporting shape

Keep the user-facing report concise:

- what was stale/churning
- which workers were replaced or pruned
- which focused closeout workers are now running
- which blockers remain human-gated
- the exact final `worker_ids`/liveness status
