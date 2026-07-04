---
name: task-end
description: Run before marking any task done. Enforces quality gates, documentation updates, a documentation-drift janitor sweep, ADR completion, and the 3-round Gemini review loop.
---

# Task End Protocol

Do not report a task complete until every gate below is checked. Work through them in order.

## Quality Gates Matrix

| Gate | Command (auto-detect stack) | Pass condition | On fail |
|------|-----------------------------|----------------|---------|
| Tests | `npm test` / `pytest` / `cargo test` / `go test ./...` | 0 failures | Fix, do not skip |
| Type check | `npx tsc --noEmit` / `mypy` / `cargo check` | 0 errors | Fix, do not skip |
| Lint | `npm run lint` / `ruff` / `clippy` | 0 errors | Fix, do not skip |
| Coverage | project threshold (check package.json / pyproject.toml) | At or above threshold | Fix or get explicit exception |
| Self-review | Ask: "Would a staff engineer approve this?" | No hacks, no regressions, minimal impact | Refactor |

## Documentation Gates Matrix

| Artifact | When required | Location |
|----------|---------------|----------|
| ADR | Any architectural decision was made | `docs/adr/NNN-name.md` (sequential) |
| Ramp-up doc update | Architecture, structure, or key files changed | `docs/AGENT_RAMP_UP.md` |
| Public API / README | Public interface changed | Inline with the changed code |
| tasks/todo.md | Always | Mark items `[x]`, add `## Review` section with outcome |
| tasks/lessons.md | Any correction received during this task | Append the pattern + why + how to apply |

## Ramp-Up Doc Sections (keep current)

`docs/AGENT_RAMP_UP.md` must always contain:

| Section | Content |
|---------|---------|
| Project goal + architecture | What it is, why it exists, major components |
| Key file map | Where important things live |
| Active decisions + constraints | Non-obvious choices, forbidden patterns, linked ADRs |
| Current work in progress | What's being built, what's blocked, what's next |

## Documentation Janitor (drift sweep)

The gates above ensure docs *you touched* got updated. The janitor is the opposite pass:
sweep for docs that went **stale, dangling, or orphaned** because of this change — the rot a
per-change checklist misses. Run it after the doc gates, before pushing.

Scope the sweep to what this change could have invalidated (files changed + docs that
reference them). Do not audit the whole tree unless the change is repo-wide.

| Check | What to look for | Action |
|-------|------------------|--------|
| Drift | Docs describing code/behavior/flags this change altered (READMEs, ramp-up, ADRs, inline docs, code comments, examples) | Update to match reality, or flag if out of scope |
| Dead references | Broken internal links, moved/renamed/deleted file paths, stale anchors, dangling `[[wikilinks]]`, code snippets citing removed symbols | Fix the reference or remove it |
| Index reconciliation | New public modules/commands/skills missing from their index (key-file map, README TOC, marketplace/plugin manifest); index entries pointing at things that no longer exist | Add missing entries, prune orphaned ones |
| Stale metadata | Version numbers, dates, counts, "as of" statements, badges rendered wrong by this change | Update to current values |
| Duplication | The same fact now stated in two places that disagree after the change | Collapse to one source of truth, link the rest |

Report what the sweep found (fixed vs. deferred). If a doc is stale but fixing it is out of
scope, note it explicitly rather than silently leaving it — silence reads as "verified
current."

## Completion Steps (in order)

1. Run all quality gates — fix any failures before continuing
2. Self-review: "Would a staff engineer approve this?" — refactor if no
3. Update `tasks/todo.md`: check off items, add `## Review` section
4. Append to `tasks/lessons.md` if any corrections happened this session
5. Write ADR if architectural decision was made (check task-start interview notes)
6. Update `docs/AGENT_RAMP_UP.md` if structure or key files changed
7. Update any public API docs or README if public interface changed
8. Run the **Documentation Janitor** drift sweep — fix or flag every finding
9. **Push PR** (`gh pr create` if no PR exists, else `git push`)

## Codex Review Loop (mandatory)

**Prefer a dedicated loop skill if one is available.** Before running the fallback below,
check whether a `codex-review-loop` or `gemini-review-loop` skill is installed. If it is, **invoke it** to drive the review on the PR and skip the inline fallback —
it is the source of truth for the mechanics (three-channel polling, the terminal
"no major issues" signal, thread resolution) and stays current as the review connector changes.

Only if no such skill is available, fall back to the inline procedure below.

### Fallback: inline 3-round loop

Repeat the following 3 times before reporting done:

```
Round N:
  1. Comment "@codex review" on the PR
  2. Wait for Codex to post review threads
  3. For each unresolved thread:
     a. Read the full comment
     b. Fix the issue in code
     c. Reply to the thread with the fix commit hash + explanation
     d. Resolve the thread via GraphQL (resolveReviewThread mutation)
  4. Push fixes: git push origin <branch>
```

| Round | Trigger | Action if comments remain |
|-------|---------|--------------------------|
| 1 | After PR created/pushed | Address all comments, resolve threads, push |
| 2 | After round 1 fixes pushed | Address all new comments, resolve, push |
| 3 | After round 2 fixes pushed | Address all new comments, resolve, push |
| Done | All threads resolved after round 3 | Report PR URL as complete |

If Codex raises a P1 issue in round 3, address it and do a 4th round — never ship with open P1s.
