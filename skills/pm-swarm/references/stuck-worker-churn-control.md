# Stuck Worker / Churn Control

Use this when a PM-swarm liveness report shows a worker sitting in `ready`/`todo` for many cycles, repeated `respawn_guarded(active_pr)`, duplicate closeout workers, or a worker that keeps heartbeating/waiting even after new evidence exists.

## Symptoms

- Broad shard remains `ready` while an active PR exists.
- Repeated `respawn_guarded(active_pr)` events with no forward progress.
- Doctor creates a fresh follow-up, but the old card remains in liveness scope.
- More than one card is working the same PR in the same workspace.
- Worker says it is waiting for Codex even after a new current-head Codex review has arrived.

## Liveness probe requirements

PM-swarm liveness must include a deterministic churn probe before it repeats nudges:

1. For `ready`/`todo` workers, inspect recent Kanban events. If there are repeated `respawn_guarded(active_pr)` events, treat that as active-PR churn and dispatch/idempotently recover PM Swarm Doctor, subject to the normal doctor suppression window.
2. For `running` workers, inspect recent heartbeat notes/comments. If the worker says it is still waiting for Codex or zero/no unresolved findings, query live GitHub GraphQL `reviewThreads`. If the current PR head has unresolved Codex threads, treat the worker as waiting on obsolete evidence and dispatch Doctor.
3. The doctor should collapse to exactly one canonical worker per active PR/issue, block duplicates with a non-auto-promoting blocker, and replace stale liveness `worker_ids` with the fresh focused card when necessary.
4. Keep the probe deterministic/no-agent so cron can run it every few minutes without token churn.

## Diagnosis steps

1. Inspect the old shard, any doctor-created follow-up, and the canonical closeout card:
   - `hermes kanban show <task> --json`
   - check status, latest summary, recent runs, events, comments, and worker PID.
2. Inspect live PR state directly; GitHub PR state is authoritative over stale card text.
3. For Codex inline findings, do not rely only on `gh pr view --json reviews/comments`: it does not expose `reviewThreads` in some gh versions. Use GraphQL:

```bash
gh api graphql \
  -f owner=OWNER -f name=REPO -F number=PR \
  -f query='query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){headRefOid reviewThreads(first:100){nodes{id isResolved isOutdated comments(first:20){nodes{id author{login} body path line commit{oid} createdAt url}}}} latestReviews(first:10){nodes{author{login} state submittedAt body commit{oid}}}}}}' \
  --jq '.data.repository.pullRequest | {headRefOid, latestReviews:[.latestReviews.nodes[] | {author:.author.login,state,submittedAt,commit:.commit.oid,body:(.body[0:500])}], unresolved:[.reviewThreads.nodes[] | select(.isResolved==false) | {id,isOutdated,comments:[.comments.nodes[] | {author:.author.login,commit:.commit.oid,path,line,body:(.body[0:700]),url}]}]}'
```

## Treatment pattern

1. Pick exactly one canonical active card for the PR/issue.
2. If the broad shard is stale and scope has moved to a focused card:
   - complete/archive the stale broad shard with a summary that it was superseded;
   - remove it from liveness `worker_ids`;
   - record it under `terminated_workers` so future liveness cleanup does not resurrect it.
3. If duplicate cards are working the same PR/workspace:
   - block duplicates with `--kind capability` or another human-held blocker, not `--kind dependency` when no real parent is gating them; dependency blocks can auto-promote and respawn;
   - kill/reclaim only the duplicate worker process, then verify `ps` no longer shows `work kanban task <duplicate>`.
4. If the canonical worker is still waiting on old evidence after current-head Codex findings arrive:
   - block/terminate that stale worker;
   - create one fresh focused card containing the current findings directly in the body;
   - replace the stale worker id with the fresh card id in liveness `worker_ids`;
   - dispatch once and verify liveness shows the new focused card as `running`.
5. Do not let liveness keep reporting archived/superseded broad-shard cards. The desired report should show one running focused worker for the active PR and no duplicate/stale ready card for the same issue.

## Verification

- `hermes kanban show <old>` reports `archived` or intentionally blocked.
- `hermes kanban show <duplicate>` reports `blocked` and no process remains.
- `hermes kanban show <fresh>` reports `running` with heartbeats.
- `python3 <project>_liveness.py` no longer lists the old ready worker and shows exactly one active worker for the stuck issue.
