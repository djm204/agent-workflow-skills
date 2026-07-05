---
name: pm-swarm
description: "Use when coordinating large Hermes work inside this profile with a persistent PM layer: top-level orchestrator creates PM Kanban cards, each PM owns up to five worker cards, then verifier and synthesizer gates finish the swarm."
version: 1.0.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [kanban, orchestration, pm, swarm, multi-agent]
    related_skills: [kanban-orchestrator, kanban-worker, github-pr-workflow]
---

# PM Swarm

## Overview

This profile has a user-local `pm-swarm` plugin installed at:

`~/.hermes/plugins/pm-swarm`

It creates a persistent PM-led topology inside Hermes Kanban instead of modifying Hermes core or another project repo. The state lives in the normal Kanban database, so the existing dispatcher, gateway, dashboard, `/kanban`, and `hermes kanban` flows can observe and run it.

Important repeatability rule: for user-specific Hermes orchestration extensions, prefer user-local plugins and skills under `~/.hermes/` over Hermes source/worktree edits. Source edits can be clobbered by `hermes update` and are not automatically active in the live profile. See `references/repeatable-user-local-plugin.md` for the session-derived pattern and verification steps.

Default topology:

```text
root / shared blackboard card (completed immediately)
  ├─ PM shard 1 (ready)
  │   ├─ worker 1 (todo until PM shard completes)
  │   └─ ... up to 5 workers by default
  ├─ PM shard 2 (ready)
  │   └─ remaining workers
  └─ verifier (todo until all PMs and workers complete)
      └─ synthesizer (todo until verifier completes)
```

## When to Use

Use this when the user asks for a persistent PM layer within Hermes itself, especially for:

- issue-to-PR trains
- multi-worker repo work
- long-running coordination that should survive turns/sessions
- avoiding duplicate worker scope
- having PM agents supervise groups of workers rather than flat fan-out

Do not use this for quick one-turn subtasks; use `delegate_task` for those.

## Interfaces

The plugin exposes three repeatable interfaces after plugin loading/restart:

1. Model tool: `pm_swarm_create`
2. CLI command: `hermes pm-swarm ...`
3. Slash command: `/pm-swarm ...`

Enable the plugin with the Hermes CLI, not by hand-editing config:

```bash
hermes plugins enable pm-swarm
hermes plugins list --json
```

The list output should show `pm-swarm` with status `enabled`. If the current session was started before plugin installation or enablement, restart Hermes or `/reset` so the model tool registry reloads.

## CLI Recipe

```bash
hermes pm-swarm "Ship the feature" \
  --pm pm-profile \
  --worker coder-a:"Implement API" \
  --worker coder-b:"Implement UI" \
  --worker tester:"Write tests" \
  --verifier reviewer \
  --synthesizer writer \
  --idempotency-key feature-x
```

Useful options:

- `--pm-capacity 5` — workers per PM shard; default is 5.
- `--pm profile[:title[:skill,skill]]` — repeatable. If fewer PM profiles than shards are provided, profiles are reused round-robin.
- `--worker profile:title[:skill,skill]` — repeatable worker card.
- `--board slug` — create on a specific Kanban board.
- `--tenant name` — namespace the tasks.
- `--idempotency-key key` — recover existing topology instead of duplicating it.
- `--json` — emit machine-readable output.

## Tool Recipe

Call `pm_swarm_create` with:

```json
{
  "goal": "Ship the feature",
  "pm_profiles": ["pm-profile"],
  "workers": [
    "coder-a:Implement API:github-pr-workflow",
    "coder-b:Implement UI",
    "tester:Write tests"
  ],
  "verifier": "reviewer",
  "synthesizer": "writer",
  "pm_capacity": 5,
  "idempotency_key": "feature-x"
}
```

## Operating Rules

0. `pm-swarm` creates the durable project topology; it does not own recurring polling or watchdog loops. The top-level orchestrator owns runtime infrastructure such as cron monitors, timers, retries, status pings, and self-removal conditions. Load `orchestrator-runtime-infra` when a swarm must keep running/polling until the project is complete. For GitHub/Codex review-gated swarms, liveness monitors must distinguish persistent blockers from transient rate-limit pauses and actively retry/unblock rate-limit-paused workers once the retry window has elapsed; see `references/codex-rate-limit-liveness.md`.
0a. If PR count grows and workers are not merging, immediately switch to closeout mode: freeze new issue work, inventory every open PR from GitHub, add a central deterministic closeout watchdog, create one explicit closeout card per blocked PR, and keep verifier/synthesis gated until those cards finish. Do not rely only on passive worker nudges when the Codex loops are clearly not running. See `references/pr-closeout-chaos-control.md`.
0b. When the user wants to “ping the PM” for specific worker debug details, add or use a deterministic manual ping/doctor path on the liveness script rather than an LLM-heavy route. It should collect Kanban JSON, run history, worker log tail, PR/Codex gate details, and post chunked comments to the PM card with `--dry-run` support. If the user asks for a doctor/remediation loop, the doctor output must explicitly separate symptoms, findings, remedies/treatments, and ongoing issues; persist those in a DSM-style JSON/state file; and report the concise doctor summary to Discord through `hermes send` when a Discord target is configured. A worker stranded in `ready`/`todo` for a configured threshold (default 15 minutes) while issues remain is itself a doctor-triggering condition: liveness should create/idempotently recover a PM Swarm Doctor card and suppress duplicate doctor dispatches for a cooldown window instead of only nudging. See `references/manual-pm-worker-debug-ping.md`.
0c. Enforce one agent per issue for issue-to-PR swarms: when an issue PR is resolved and merged, the PM/liveness monitor must actively terminate/archive that worker card/session after its self-reflection and handoff are recorded, not just warn. Do not let workers continue into the next issue; create a fresh one-issue worker/card/context for each remaining issue to prevent context bloat and cross-issue contamination. Name agents/cards as `<name>-<issueNumberTheyAreWorkingOn>-<status>` (for example `resolver-493-running`) so liveness and Discord reports are quickly scannable. All PM/worker/verifier/synthesizer subagents must use Git identity `David Mendez <me@davidmendez.dev>` for commits; see `references/subagent-git-identity.md` for the two-layer env + local-config enforcement and verification pattern.
0d. When a liveness report says a configured worker is missing from Kanban, first check whether the card was intentionally completed/archived after reflection. If yes, treat the stale `worker_ids` entry in the liveness config/state as the defect: run the deterministic doctor on that task id for an audit trail, then remove the archived worker from future liveness scope rather than respawning it or creating broad remediation work. Liveness scripts should automatically prune worker ids that are already recorded under `terminated_workers` or that the script just auto-archived, and emit an action such as `worker-ids-pruned:<ids>` after rewriting the config. A doctor card for an archived/merged PR should close out config hygiene, not reopen the PR. See `references/archived-worker-liveness-cleanup.md` for the implementation and temp-config verification pattern.
0e. When a liveness report feels stuck or churning, actively collapse the swarm back to exactly one canonical worker per active PR/issue. Inspect Kanban runs/events and live PR/Codex state, archive superseded broad shards, block duplicate closeout cards with a non-auto-promoting blocker such as `--kind capability`, kill only the duplicate worker process, and replace stale `worker_ids` entries with a fresh focused card when the current worker is waiting on obsolete evidence. Do not keep nudging a `ready` card forever and do not let a dependency-style duplicate block auto-promote back into the same churn. See `references/stuck-worker-churn-control.md`.
0f. When the user says multiple workers feel stuck/idle, audit *all* liveness-tracked workers plus all active tenant Kanban cards and live open PR gates. During closeout freeze, broad root cards that point at merged PRs or should not start new issue work are liveness noise: replace them with focused closeout cards for each actionable open PR, keep true human blockers, dispatch the focused cards, verify real PIDs/heartbeats, and rewrite `worker_ids` to only those active closeout cards plus true blockers. See `references/all-worker-liveness-audit.md`.
0g. If workers are blocked only because a dangerous but intended Git publish/amend command needs approval, add a deterministic approval traffic-cop instead of forcing the user to run commands manually. The cop should send a Discord token with the exact command and worktree, watch Hermes session history for `APPROVE <token>` / `DENY <token>` (and, when possible, reply-context `APPROVE`), execute only an allowlisted exact approved command, then comment/unblock/dispatch the worker. Doctor/liveness should invoke the cop for `approval_required` blockers and stay quiet when approval is already pending or the worker is no longer blocked. See `references/approval-traffic-cop.md` for the durable implementation pattern and pitfalls.
0h. When a liveness-tracked worker is stuck in `ready`/`todo` with repeated `respawn_guarded(active_pr)` but an active doctor/focused closeout card is already running for that same PR, liveness must stop repeatedly nudging the stale ready card. Instead, request a bounded doctor checkup (for example every 5 minutes) on the active doctor card, asking it to re-check live PR/Codex/CI/Kanban state and either continue work, replace the stale worker id with the active card, or block with exact HITL. This keeps liveness accountable without token-wasting duplicate wakeups.
0i. If a continuing issue-swarm liveness loop reports `OK` with zero configured workers while open issues remain, treat it as a capacity outage: the PM must refill to the target concurrency (commonly 5) with fresh idempotent one-issue workers, update `worker_ids`, dispatch, verify real running workers, and install/keep a deterministic no-agent refill watchdog so completed workers are pruned then replaced. Do not revive broad historical shard cards. See `references/five-wide-auto-refill.md`.
0j. PM Swarm Doctor must be a treatment mechanism, not just a reporter. It should inspect live PR/Codex/CI/Kanban state, post a concrete `DOCTOR TREATMENT` on the target worker, unblock/dispatch safe blocked or idle cards, route exact approval-required commands through approval-cop, and create a fresh runnable takeover doctor card when code/PR work is required. Doctor card idempotency must not reuse a completed stale doctor forever. See `references/doctor-as-treatment.md`.
0k. Treat stale frozen/superseded `todo`/`ready` cards that remain in liveness `worker_ids` as config hygiene defects, not harmless noise. Liveness reports must surface the card title, assignee, status, and freeze/superseded context explicitly; if a card is intentionally deferred, remove it from active liveness capacity, store it in a deferred/ignore map, patch refill so it is not re-added, and immediately refill target concurrency with a fresh one-issue worker. See `references/liveness-deferred-worker-hygiene.md`.
1. Use an idempotency key for any mission that might be retried.
2. Give workers narrow titles/bodies so PMs can prevent overlap.
3. Use PM profiles that have Kanban access and the `kanban-orchestrator` skill available.
4. PM cards are deliberately dependencies of worker cards. This makes PMs start first, post shard planning/instructions, then complete so workers can run. PMs must not wait for worker handoffs before completing, otherwise the shard can deadlock.
5. The verifier depends on both PM and worker cards. It should pass only after PM planning and all worker handoffs are complete and evidence is sufficient.
6. The synthesizer depends on verifier and should not start until the verification gate passes.
7. If publishing this skill to a repo, ship the actual plugin template plus installer with the skill package; do not advertise `pm_swarm_create`, `hermes pm-swarm`, or `/pm-swarm` unless users can reproduce/install the plugin from the published artifact.
8. Treat `workspace_kind=worktree` plus a shared `workspace_path` as invalid for multi-card swarms; each card needs an isolated checkout/path.
9. Validate optional runtime limits as positive integers. A zero runtime limit creates immediately-expiring worker cards.
10. Validate explicit board names before creating tasks; a misspelled board must fail fast rather than silently falling back to `default`.

## Verification Checklist

## Integration Points

- Liveness scripts should call the cop for blocked workers with approval-required summaries, but stay quiet when a request is already pending or the worker is no longer blocked.
- PM Swarm Doctor should call the cop when diagnostics include `approval_required`.
- A no-agent cron watchdog can run every minute: scan, poll, execute, and emit output only for new approval events.
- Updated approval traffic cop now supports `ALLOW ALWAYS <token>` to create a persistent allow‑always policy for the exact command+workdir, reducing repeated approval requests.
- Ensure `approval_mention` uses a numeric Discord ID (`<@USER_ID>`) to guarantee the human is notified.
- The cop only executes commands that match the exact allowlist; any deviation will be rejected.

- [ ] Plugin exists at `~/.hermes/plugins/pm-swarm/plugin.yaml` and `__init__.py`.
- [ ] If publishing/installing from a skill repo, the skill contains `templates/plugin/{plugin.yaml,__init__.py,core.py}` and `scripts/install-pm-swarm-plugin.sh` or an equivalent concrete plugin generation recipe.
- [ ] `hermes plugins list --json` shows `pm-swarm` status `enabled`.
- [ ] `hermes pm-swarm --help` works in a fresh CLI invocation.
- [ ] Invalid slash args return a friendly `pm-swarm error` instead of raising `SystemExit`.
- [ ] A zero or negative `max_runtime_seconds` in object specs fails fast before creating cards.
- [ ] Explicit `--board` values are validated instead of silently falling back to `default`.
- [ ] Shared `workspace_path` with `workspace_kind=worktree` is rejected for swarms to avoid worker checkout collisions.
- [ ] A test creation with 6 workers creates 2 PM cards by default.
- [ ] Reusing an `idempotency_key` after a partial run does not duplicate child cards or re-complete an already-done root.
- [ ] First worker card has its PM card as parent.
- [ ] Verifier has all PM and worker cards as parents.
- [ ] Root blackboard has a `pm_swarm_v1` topology payload.

See `references/pr6-codex-review-findings.md` for the review findings that hardened this workflow.
