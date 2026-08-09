# Archived Worker Liveness Cleanup

Use this when a PM-swarm liveness report keeps showing a worker as `missing from Kanban` after the worker was intentionally completed/reflected and archived.

## Durable lesson

For issue-to-PR swarms, archived/completed workers are terminal. They should not remain in the recurring liveness monitor's configured `worker_ids` list. If they do, the active Kanban list no longer contains the archived card and the monitor can keep reporting a false `missing from Kanban` problem.

## Correct behavior

1. Verify the card was intentionally terminal:
   - `hermes kanban show <task_id> --json` shows completed/archived evidence, or the liveness `state_path` has the worker under `terminated_workers`.
   - The associated PR, if any, is merged/closed or otherwise truly terminal.
2. Do **not** respawn the archived worker and do **not** reopen PR work from the missing-card signal alone.
3. Prune the worker id from the liveness config's `worker_ids` list.
4. Keep the terminal evidence in the liveness state / PM comments / DSM so the audit trail remains available.
5. Verify by rerunning the liveness script and confirming the worker no longer appears in either the worker list or `missing from Kanban` problems.

## Implementation pattern

For deterministic liveness scripts, cleanup should happen automatically:

- Load `terminated_workers` from the liveness state file.
- While iterating configured `worker_ids`, if `kanban list` cannot find a worker but the id is present in `terminated_workers`, skip the problem and add the id to a prune list.
- When a `done` worker is auto-archived/terminated, also add the id to the prune list.
- Before saving state/output, rewrite the JSON config with `worker_ids` excluding the prune list and emit an action like `worker-ids-pruned:<ids>`.

Minimal pseudocode:

```python
workers_to_prune = []
terminated_workers = state.get("terminated_workers", {})

for wid in workers:
    task = by_id.get(wid)
    if not task:
        if wid in terminated_workers:
            workers_to_prune.append(wid)
            continue
        problems.append(f"worker {wid} missing from Kanban")
        continue

    if task["status"] == "done" and archive_succeeded:
        terminated_workers[wid] = {...}
        workers_to_prune.append(wid)

if workers_to_prune:
    cfg["worker_ids"] = [wid for wid in workers if wid not in set(workers_to_prune)]
    save_json(config_path, cfg)
    actions.append("worker-ids-pruned:" + ",".join(workers_to_prune))
```

## Verification recipe

Use a temporary config/state rather than mutating production while testing:

1. Copy the swarm liveness config to a temp file.
2. Set `worker_ids` to a known archived/terminated id.
3. Set `state_path` to a temp state file containing that id under `terminated_workers`.
4. Run the liveness script against the temp config.
5. Confirm output contains `worker-ids-pruned:<id>` and the temp config's `worker_ids` is now empty.
6. Confirm the production config still contains only active workers.
