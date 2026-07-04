---
name: pm-swarm
description: "Use when coordinating large Hermes work inside a profile with a persistent PM layer: top-level orchestrator creates PM Kanban cards, each PM owns up to five worker cards, then verifier and synthesizer gates finish the swarm."
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

Use this skill to create and operate a user-local `pm-swarm` Hermes plugin that adds a persistent PM-led topology inside Hermes Kanban.

This published skill includes a reproducible plugin template under:

`templates/plugin/`

Install that template into the active Hermes profile before using the CLI/tool interfaces:

```bash
bash "$HERMES_SKILL_DIR/scripts/install-pm-swarm-plugin.sh"
```

If `HERMES_SKILL_DIR` is not set, run the script from the installed skill directory, for example:

```bash
bash ~/.hermes/skills/devops/pm-swarm/scripts/install-pm-swarm-plugin.sh
```

The installer copies the plugin to:

`~/.hermes/plugins/pm-swarm`

The skill itself should live under:

`~/.hermes/skills/devops/pm-swarm/SKILL.md`

The topology state lives in the normal Hermes Kanban database, so the existing dispatcher, gateway, dashboard, `/kanban`, and `hermes kanban` flows can observe and run it.

Important repeatability rule: for user-specific Hermes orchestration extensions, prefer user-local plugins and skills under `~/.hermes/` over Hermes source/worktree edits. Source edits can be clobbered by `hermes update` and are not automatically active in the live profile. See `references/repeatable-user-local-plugin.md` for the durable pattern and verification steps.

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

Install the bundled plugin template first:

```bash
bash "$HERMES_SKILL_DIR/scripts/install-pm-swarm-plugin.sh"
```

After plugin installation and Hermes plugin loading/restart, the plugin exposes three repeatable interfaces:

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

1. Use an idempotency key for any mission that might be retried.
2. Give workers narrow titles/bodies so PMs can prevent overlap.
3. Use PM profiles that have Kanban access and the `kanban-orchestrator` skill available.
4. PM cards are deliberately dependencies of worker cards. This makes PMs start first, post shard planning/instructions, then complete so workers can run. PMs must not wait for worker handoffs before completing, otherwise the shard can deadlock.
5. The verifier depends on both PM and worker cards. It should pass only after PM planning and all worker handoffs are complete and evidence is sufficient.
6. The synthesizer depends on verifier and should not start until the verification gate passes.

## Verification Checklist

- [ ] Plugin template exists at `templates/plugin/plugin.yaml`, `templates/plugin/__init__.py`, and `templates/plugin/core.py`.
- [ ] Installer exists at `scripts/install-pm-swarm-plugin.sh`.
- [ ] Run `bash "$HERMES_SKILL_DIR/scripts/install-pm-swarm-plugin.sh"` from an installed skill copy.
- [ ] Plugin exists at `~/.hermes/plugins/pm-swarm/plugin.yaml` and `__init__.py`.
- [ ] `hermes plugins list --json` shows `pm-swarm` status `enabled`.
- [ ] `hermes pm-swarm --help` works in a fresh CLI invocation.
- [ ] A test creation with 6 workers creates 2 PM cards by default.
- [ ] First worker card has its PM card as parent.
- [ ] Verifier has all PM and worker cards as parents.
- [ ] Root blackboard has a `pm_swarm_v1` topology payload.
