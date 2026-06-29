# Skill: `session-start`

Human-facing reference. The authoritative, agent-facing spec is
[`skills/session-start/SKILL.md`](../../skills/session-start/SKILL.md).

## What it does

Establishes full working context at the start of a session with minimal token cost. A
SessionStart hook injects a compact brief — git state, the memory index, in-progress todos,
and recent lessons — and this skill tells the agent to **use that brief instead of
re-reading files**.

## When it activates

Automatically, at session start (driven by the SessionStart hook). Not invoked manually.

## What gets loaded

| Signal | Source | If missing |
|--------|--------|------------|
| Git branch + last commit | `git log` / `git status` | Note "no git repo" |
| Memory index | `MEMORY.md` (project or global) | Note "no memory" |
| WIP tasks | pending items in `tasks/todo.md` | Note "no todo" |
| Active lessons | last lines of `tasks/lessons.md` | Note "no lessons" |

## Expected behavior

- Don't speculatively re-read files the brief already summarized — drill into a memory file
  or todo only when the user's first message directly involves it.
- The first response should be terse: one line acknowledging branch + uncommitted work, one
  line surfacing relevant in-progress todos, then answer the user's actual message.
- Apply active lessons silently — don't list them back to the user.

## Related

Pairs with [`task-start`](task-start.md) (kick-off) and [`task-end`](task-end.md) (wrap-up).
