# Manual PM Worker Debug Ping Pattern

Use this when a PM-swarm needs an operator-triggered way to ask the PM for deep status on specific workers without spending LLM tokens in the polling path.

## Goal

Add a deterministic manual ping/doctor path beside the recurring liveness watchdog. The path should collect existing Kanban/GitHub evidence, post it to the PM card, and optionally print it only for dry-run/debug.

Recommended CLI shape:

```bash
python3 ~/.hermes/scripts/pm_swarm_liveness.py ping ~/.hermes/scripts/<swarm>_liveness.json WORKER_ID [WORKER_ID ...] --deep --dry-run --note "what to inspect"
```

## Design rules

- Keep the ping path non-LLM and deterministic; it should be safe for frequent use.
- Reuse the existing liveness JSON config: `workdir`, `board`, `tenant`, `pm_id`, `worker_ids`, `repo`, `progress_path`, `state_path`.
- `--dry-run` prints the full report and does not post comments.
- Without `--dry-run`, post to the PM card, not directly to the user-only transcript.
- Chunk PM comments so deep reports do not silently truncate at Kanban comment limits.
- Append a concise liveness/progress log line, but do not treat the progress doc as the source of truth for current state.

## Deep report contents

For each requested worker id, include:

- task title, status, assignee, priority, session id, current step
- started/completed timestamps
- latest summary
- parent/child cards
- recent comments
- recent Kanban events/heartbeats with notes
- run history (`hermes kanban runs`)
- worker log tail (`hermes kanban log`)
- PR gate details when the latest summary names a PR: state, URL, head SHA, merge state, checks, latest Codex/comment signal

## PM instruction text

The posted comment should ask the PM to reply on the PM/root card with diagnosis and exact next action, citing worker ids, PRs, and blockers explicitly.

## Doctor mode and Discord reporting

When the user asks for a doctor agent/workflow rather than a read-only PM ping, keep the collector deterministic and make the output operational:

- Diagnose each selected worker into explicit buckets: symptoms, findings, remedies/treatments, and ongoing issues.
- Persist the diagnosis in a DSM-style state file keyed by symptom, with recent cases capped so the file stays readable.
- Create a narrow doctor Kanban card only when remediation work is needed; keep the card body scoped to the selected workers and the gathered evidence.
- Report the concise doctor summary to Discord with `hermes send --to <target> --subject "PM Swarm Doctor" ...` when a `doctor_discord_target`/`discord_target` is configured or the operator passes `--discord`.
- Provide `--no-discord` and `--dry-run` controls so operators can suppress or preview delivery.
- The Discord message should include findings, symptoms, remedies, ongoing issues, and PR/Codex state when available; avoid dumping the full Kanban event log into Discord.

Example config fields:

```json
{
  "doctor_dsm_path": "~/.hermes/state/<swarm>_doctor_dsm.json",
  "doctor_report_discord": true,
  "doctor_discord_target": "discord:<channel_or_thread>",
  "doctor_assignee": "default"
}
```

## Verification

1. Run help/usage for the new subcommand.
2. Run `--deep --dry-run` or doctor `--dry-run` against one known worker and inspect that it includes worker state, recent events, run history, symptoms, findings, remedies, and ongoing issues.
3. Run without `--dry-run` against a low-risk worker and confirm the PM card received the chunked comment(s) when PM posting is in scope.
4. If Discord reporting is in scope, run one live Discord send and verify the action reports `discord-sent:<target>`.
5. Confirm the DSM file updates with symptoms, findings, treatments, ongoing issues, and capped cases.
6. Confirm the normal recurring liveness mode still emits the existing `LIVENESS OK` / `LIVENESS PROBLEM` output and does not regress duplicate suppression.

## Pitfalls

- If a configured worker appears missing from Kanban, verify whether it was completed/archived first. Archived workers should usually be removed from the liveness config or ignored by future liveness scans after the doctor records an audit trail; do not respawn them or reopen already-merged PR work just because `worker_ids` is stale.
- Do not replace the existing recurring liveness monitor with a manual ping; this is an operator-requested diagnostic layer.
- Do not add LLM calls to the ping path.
- Do not rely on a single `hermes kanban show` text scrape if JSON output is available; prefer `show --json` and only use text output for compatibility gaps.
- Do not treat an `UNSTABLE` label as proof the PR is unstable; expose the exact evidence and route through doctor diagnosis.
- Do not claim implementation succeeded without a real dry-run and one live comment/post/Discord verification when posting or Discord delivery is in scope.
