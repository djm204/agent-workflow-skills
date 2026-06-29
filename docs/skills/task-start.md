# Skill: `task-start`

Human-facing reference. The authoritative, agent-facing spec is
[`skills/task-start/SKILL.md`](../../skills/task-start/SKILL.md).

## What it does

Runs before any implementation task and routes the kick-off down the right path based on how
much is already specified — so work starts with a clear plan and explicit approval rather
than ad-hoc coding.

## When it activates

Before starting implementation work — new feature, bugfix, or resuming after a crash.

## The three routes

| Condition | Path | Kick-off |
|-----------|------|----------|
| Implementation file(s) provided | **Dive in** | Read them, map the blast radius, check existing tests, write a `tasks/todo.md` plan, get approval. |
| User says "continue" / session crashed | **Recovery** | Read `tasks/todo.md` + `git diff`/`log` + memory, produce a recovery brief, and confirm "continue or replan?" |
| No spec (new task) | **Interview** | Ask the 7 requirements questions, then plan (+ ADR if architectural) and get approval. |

## The requirements interview (Path C)

Goal, acceptance criteria, out-of-scope/MVP boundary, stack/patterns, autonomy level, test
expectations, and whether an architectural decision is involved. The last one is the trigger
for a mandatory ADR.

## Outputs

- A checkable plan in `tasks/todo.md` (grouped by phase for the interview path).
- An ADR in `docs/adr/NNN-name.md` when an architectural decision was made (template in the
  SKILL.md), linked from `docs/AGENT_RAMP_UP.md`.
- Explicit user approval **before** writing code.

## Related

Pairs with [`session-start`](session-start.md) (orientation) and [`task-end`](task-end.md)
(the closing gate). The ADR convention is shared with `task-end`.
