# Liveness deferred-worker hygiene

Use this when a PM-swarm liveness report repeatedly flags a `todo`/`ready` worker that is described as frozen, superseded, closeout-frozen, or otherwise intentionally non-actionable.

## Failure mode

A stale worker card can remain in the liveness config's `worker_ids` even after the PM/orchestrator has commented that it is superseded or intentionally frozen. If the refill watchdog counts that stale card as active capacity, the swarm silently runs below target concurrency while reports only say something vague like `worker <id> not running while issues remain (todo)`.

This is not harmless noise. It is a liveness/config hygiene defect.

## Required handling

1. Inspect the card title, assignee, latest summary, and recent comments/events before calling it noise.
2. If the card has a closeout-freeze/superseded/intentionally-not-actionable note, do not keep it in active `worker_ids`.
3. Remove the stale card from active liveness scope and record it in a `deferred_worker_ids`/equivalent ignore map with a concise reason.
4. Patch/refill logic so deferred IDs are not re-added as active managed workers.
5. Trigger/refill capacity immediately so the target worker count is restored with a fresh one-issue card.
6. Patch liveness output so future reports include at least: worker id, title, assignee, status, and the freeze/superseded context. Do not emit bare opaque IDs for actionable liveness failures.
7. Comment on the stale card explaining that it was removed from active liveness capacity and remains deferred for later priority handling.

## Reporting requirement

When the user asks why a liveness item is a problem, answer with the concrete card title/issue, whether it is active vs deferred, and the exact reason it affects capacity. Avoid dismissing it as "intentional noise" unless the config has actually excluded it from active capacity.
