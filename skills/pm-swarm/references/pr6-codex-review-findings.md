# PM Swarm PR review hardening notes

Session context: while publishing `pm-swarm` into `djm204/agent-workflow-skills`, GitHub Codex review found several reproducibility and edge-case issues. These are class-level pitfalls for future PM-swarm/plugin packaging work.

## Package the plugin, not just the skill docs

If a skill advertises plugin-provided interfaces such as `pm_swarm_create`, `hermes pm-swarm`, or `/pm-swarm`, the published skill package must include one of:

- a complete plugin template under `templates/plugin/`, plus an installer script under `scripts/`; or
- a concrete generation recipe that creates those plugin files.

A skill-only package that says “enable the plugin” is not reproducible for users who do not already have the local plugin.

Recommended package shape:

```text
skills/pm-swarm/
  SKILL.md
  scripts/install-pm-swarm-plugin.sh
  templates/plugin/plugin.yaml
  templates/plugin/__init__.py
  templates/plugin/core.py
  references/repeatable-user-local-plugin.md
```

The installer should copy the template into `~/.hermes/plugins/pm-swarm`, run `hermes plugins enable pm-swarm`, and tell the user to restart/reset for model tool registry reload.

## Topology edge cases Codex caught

- PM cards are parents of worker cards, so PM instructions must say to complete after posting shard planning/instructions. If PMs wait for worker handoffs before completing, workers never start.
- Slash-command parsers must not let `argparse` raise `SystemExit`; use a parser that raises an ordinary exception so the slash handler returns a friendly error.
- Idempotent retries need deterministic child idempotency keys, not just a root idempotency key. Also avoid calling `complete_task` on a reused root that is already `done`.
- Validate explicit board names before creating cards. A typo should fail fast instead of falling back to `default`.
- Reject `workspace_kind=worktree` with a single shared `workspace_path` for a swarm; otherwise parallel cards contend for the same checkout.
- Validate optional `max_runtime_seconds` as positive when present. Zero means immediately expired work.

## Review-loop hygiene

- Running `python3 -m py_compile` inside a package tree creates `__pycache__`; remove it before `git add`, or run compilation with `PYTHONDONTWRITEBYTECODE=1`/outside the tracked tree.
- If Codex comments are on a stale reviewed commit, verify current head and file contents before changing code. Reply/resolve stale threads only after a current-head implementation proves the issue is addressed.
- Eyes reaction means Codex accepted the trigger, not clean. Continue polling for an actual clean response or findings.
