# agent-workflow-skills

Claude Code plugin providing a full-cycle agent workflow: session context loading, requirements interviews, task tracking, ADRs, quality gates, and a Codex review loop.

## Skills

| Skill | Trigger | Purpose |
|-------|---------|---------|
| `session-start` | Auto (SessionStart hook) | Load git state, memory, WIP tasks, lessons |
| `task-start` | Before any implementation | Route to dive-in, recovery, or requirements interview |
| `task-end` | Before marking done | Quality gates, docs, ADR, 3× Codex review loop |

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
Quality gates → docs updates → ADR → ramp-up doc → PR → 3× `@codex review` loop (address all threads each round).
