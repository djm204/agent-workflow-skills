---
name: task-start
description: Run before any implementation task. Routes to the correct kick-off path based on whether a spec was provided, whether this is a resumption, or whether we need a requirements interview.
---

# Task Start Protocol

## Route Selection Matrix

| Condition | Path | First action |
|-----------|------|--------------|
| Implementation file(s) provided | Dive in | Identify affected files, understand blast radius |
| User says "continue" or session crashed | Recovery | See Recovery Protocol below |
| No spec provided (new task) | Interview | See Requirements Interview below |

---

## Path A — Spec Provided (Dive In)

1. Read the provided file(s)
2. Identify all files that will change (blast radius)
3. Check for existing tests covering the affected area
4. Write plan to `tasks/todo.md` with checkable items
5. Get explicit user approval before writing code

---

## Path B — Recovery Protocol

Run these steps in order:

1. **Read `tasks/todo.md`** → find the last `[x]` checked item and all open `[ ]` items
2. **Run `git diff HEAD` + `git log --oneline -5`** → identify what actually changed vs. what was planned
3. **Check memory systems** (in order):
   - Local MEMORY.md / project memory files
   - `~/.claude/` or `~/.gemini/` session artifacts if available
4. **Produce a recovery brief:**
   ```
   Last completed: [item from todo]
   Git shows: [files changed, summary]
   Memory says: [relevant context if found]
   Discrepancy: [anything that doesn't match]
   ```
5. **Ask: "Continue from [last item] or replan?"** — do not assume, always confirm

---

## Path C — Requirements Interview

Ask these questions. Do not start coding until all are answered.

| # | Question | Why it matters |
|---|----------|----------------|
| 1 | What are we building and why? | Establishes goal and motivation |
| 2 | What are the acceptance criteria? | Defines explicit pass/fail for done |
| 3 | What is explicitly OUT of scope (MVP boundary)? | Prevents gold-plating |
| 4 | Stack / framework / patterns to follow or avoid? | Prevents wrong-tool choices |
| 5 | Autonomy: full-auto, check-in at steps, or plan-only? | Sets collaboration mode |
| 6 | Test expectations: unit / integration / e2e, TDD required? | Sets quality bar upfront |
| 7 | Does this involve an architectural decision? | ADR required if yes (non-negotiable) |

### After interview is complete:

1. Write a plan to `tasks/todo.md` with checkable items grouped by phase
2. Write an ADR to `docs/adr/NNN-name.md` if question 7 = yes
3. Present the plan to the user and get explicit approval
4. Only then begin implementation

---

## ADR Checklist (mandatory for architectural decisions)

File: `docs/adr/NNN-name.md` (sequential number, kebab-case name)

```markdown
# ADR-NNN: Title

## Status
Proposed | Accepted | Deprecated | Superseded by ADR-NNN

## Context
What situation or constraint led to this decision?

## Decision
What did we decide?

## Consequences
What becomes easier? What becomes harder? What are the trade-offs?
```

Link the ADR from `docs/AGENT_RAMP_UP.md` under "Active Decisions".
