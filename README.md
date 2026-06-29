# agent-workflow-skills

Claude Code plugin providing a full-cycle agent workflow: session context loading, requirements interviews, task tracking, ADRs, quality gates, and a Codex review loop.

## Skills

| Skill | Trigger | Purpose | Docs |
|-------|---------|---------|------|
| `session-start` | Auto (SessionStart hook) | Load git state, memory, WIP tasks, lessons | [docs](docs/skills/session-start.md) · [SKILL.md](skills/session-start/SKILL.md) |
| `task-start` | Before any implementation | Route to dive-in, recovery, or requirements interview | [docs](docs/skills/task-start.md) · [SKILL.md](skills/task-start/SKILL.md) |
| `task-end` | Before marking done | Quality gates, docs, ADR, Codex review loop (prefers the `codex-review-loop` skill; inline 3-round fallback) | [docs](docs/skills/task-end.md) · [SKILL.md](skills/task-end/SKILL.md) |

## Install

Add to `~/.claude/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "agent-workflow-skills": {
      "source": {
        "source": "git",
        "url": "https://github.com/djm204/agent-workflow-skills.git"
      }
    }
  }
}
```

Then install via Claude Code:
```
/plugins install agent-workflow@agent-workflow-skills
```

## Workflow Overview

### Session Start (auto)
Every session: git state + memory index + WIP tasks + active lessons → compact brief injected via hook. No tool calls needed.

### Task Start
- **Spec provided** → dive in, identify blast radius, plan, get approval
- **"continue"** → recovery: todo + git diff + memory → brief → confirm
- **No spec** → 7-question requirements interview → plan → ADR if needed → approval

### Task End
Quality gates → docs updates → ADR → ramp-up doc → PR → Codex review loop. The loop prefers a `codex-review-loop` skill if installed (the [codex-review](https://github.com/djm204/codex-review) plugin), and otherwise falls back to an inline 3× `@codex review` loop (address all threads each round).
