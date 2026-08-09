# Five-wide issue-swarm auto-refill

## Trigger

Use this pattern when an issue-to-PR PM swarm finishes/archives its current closeout workers while repository issues still remain. A liveness report of `✅ LIVENESS OK` with `worker_ids: []` is not healthy if the mission is still to burn down open issues.

## Pattern

1. Treat the empty worker set as a PM orchestration failure, not completion.
2. Query live open GitHub issues and rank by priority labels (`P0`, `P1`, `P2`, `P3`, then unlabeled; newer issue number as a tie-breaker unless the user specified otherwise).
3. Create/refetch idempotent one-issue worker cards until the configured active target is reached, commonly 5.
4. Each card body must restate the one-issue invariant:
   - one issue = one branch/worktree = one PR;
   - read shared lessons first;
   - use the required git identity;
   - run relevant checks;
   - open a PR closing only that issue;
   - use real GitHub Codex review loop until current-head clean;
   - after merge/blocker, record reusable lessons, complete/block, and stop.
5. Rewrite the liveness config `worker_ids` to the fresh active cards and include `target_active_workers` / `auto_refill_workers` where the local monitor supports it.
6. Dispatch with a max equal to the target and verify card statuses/PIDs/heartbeats.
7. Add a deterministic no-agent refill watchdog if the swarm should continue unattended. It should stay silent when no refill is needed and print only on create/update/dispatch/failure.
8. Comment the PM card with the refill action and expectation: keep N fresh one-issue workers moving while open issues remain.

## Pitfalls

- Do not call zero tracked workers “OK” just because there are no stale workers; if open issues remain and the user wants the train running, it is a capacity outage.
- Do not revive broad historical shard cards when the user expects one issue per worker. Create focused, idempotent one-issue cards instead.
- Do not create a worker for release-please or other automation-only PRs unless the user explicitly asks.
- Do not let completed/archived workers remain in `worker_ids`; prune them, then refill with fresh workers.
