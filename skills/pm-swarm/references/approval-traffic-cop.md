# Approval traffic cop for PM swarms

Use this when PM-swarm workers are blocked on intended but approval-gated side effects such as `git commit --amend` or `git push --force-with-lease`, and the user wants approvals routed through Discord rather than manual shell intervention.

## Pattern

1. Detect `approval_required` blockers from Kanban worker state, latest summary, or doctor diagnostics.
2. Extract the exact blocked command and exact worktree from the worker card/comments. Do not synthesize a new command.
3. Generate a stable approval token from `(worker_id, workdir, command)`.
4. Send a concise Discord approval request containing:
   - a direct configured mention such as `approval_mention` / `approval_mentions` when available; on Discord prefer numeric `<@USER_ID>` over display-name text and send with explicit `allowed_mentions.users=[USER_ID]` (or equivalent platform API support), because visible mention rendering alone may not notify the human
   - worker id/title
   - token
   - workdir
   - exact command in a fenced block
   - high-signal action controls in the message, with emoji button-style rows: `APPROVE <token>` / `ALLOW ONCE <token>`, `ALLOW ALWAYS <token>` for the exact command+workdir policy, or `DENY <token>`; on Discord the script-driven `hermes send` path may not provide native clickable components, so make the copy/paste rows unmistakable and mention the user explicitly.
5. Poll Hermes session history for approval replies. If the user replies only `APPROVE` while replying to a specific approval message, treat the surrounding reply context as the token source when available; otherwise prefer explicit `APPROVE <token>`.
6. Execute only allowlisted command shapes in allowlisted worktrees, then comment/unblock/dispatch the worker.
7. Store request state durably so cron retries are idempotent and do not re-run executed approvals.

## Safety guardrails

- Allowlist exact command classes, not arbitrary shell:
  - `git push --force-with-lease origin ...`
  - `git status --short --branch && git push --force-with-lease`
  - recorded multi-line force-with-lease handoffs that fetch the target PR branch, compute `OLD=$(git rev-parse FETCH_HEAD)`, and push with `--force-with-lease=refs/heads/$PR_BRANCH:$OLD`
  - pre-recorded `git add ... && git diff --cached --stat && git commit --amend --no-edit && git push --force-with-lease origin ...`
  - narrowly scoped PR comment/review-thread helper scripts that take explicit PR/comment IDs already recorded in the worker handoff.
- Treat both `approval required` and machine-readable `approval-required` / `approval-blocked` summaries as approval blockers.
- Allowlist expected worktree roots, e.g. `/home/pfkagent/dev/resolve-wt/*` and the repository root if intentionally used.
- Do not scan only the latest few comments: repeated liveness wakeups can push the actual blocked command out of the tail. Extract the newest allowed command from the full worker comment history.
- Never approve based on stale broad shard cards; the worker must still be blocked/approval-blocked and the command must match the current blocker.
- If execution succeeds, record `status=executed`, output tails, timestamp, and unblock/dispatch the worker.
- If execution fails, keep the worker blocked and comment stderr/stdout evidence.
- If a crash/interruption happens after recording `status=approved` but before execution, the next poll must resume and execute approved-but-not-executed requests.
- Once a worker is unblocked/running, suppress new approval requests for the same stale blocker; otherwise liveness comments can create duplicate tokens.

## Integration points

- Liveness scripts should call the cop for blocked workers with approval-required summaries, but stay quiet when a request is already pending or the worker is no longer blocked.
- PM Swarm Doctor should call the cop when diagnostics include `approval_required`.
- A no-agent cron watchdog can run every minute: scan, poll, execute, and emit output only for new approval events.

## Pitfalls from the frankenbeast issue swarm

- A dry-run or interrupted poll can leave a request in `approved` without `executed_at`; add recovery logic for that state.
- If argument order is wrong (`--request` before positional ids for an argparse shape that expects `config [task_ids...]`), doctor/liveness invocations fail silently unless checked. Verify the exact CLI invocation under dry-run.
- After a successful approval execution, stale latest summaries may still contain the approval text. The cop must check the live Kanban status and skip workers that are no longer `blocked`.
- Discord reply context can carry the intended token even when the user replies only `APPROVE`/`ALLOW`; explicit token remains preferred, but reply-context parsing improves usability. Parse only the active `[New message]` text as the decision; quoted/context approval-control rows must not be treated as fresh `ALLOW ALWAYS <token>` commands.
- Kanban `workspace_path` may point at `.hermes/kanban/workspaces/<task>` instead of the actual git worktree. If no explicit `/home/pfkagent/dev/resolve-wt/issue-N` path appears in the blocker text, infer it from `issue #N` when that standard worktree exists before deciding `no-allowed-command`.
