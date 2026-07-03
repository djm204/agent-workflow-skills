# Skill: `task-end`

Human-facing reference. The authoritative, agent-facing spec is
[`skills/task-end/SKILL.md`](../../skills/task-end/SKILL.md).

## What it does

Runs before declaring any task done and enforces a series of gates so "done" means verified,
documented, and reviewed — not just "code written".

## When it activates

Before reporting a task complete, committing, or merging.

## The gates (in order)

1. **Quality gates** — tests, type check, lint, coverage, and a staff-engineer self-review.
   Failures are fixed, not skipped.
2. **Documentation gates** — ADR for any architectural decision; update
   `docs/AGENT_RAMP_UP.md` on structural change; update README/API docs on interface change;
   always check off `tasks/todo.md` (+ a `## Review` section) and append to
   `tasks/lessons.md` if a correction occurred.
3. **Documentation janitor** — a drift sweep for docs the change *invalidated* rather than
   touched: stale descriptions, dead links and moved file paths, index/manifest entries gone
   missing or orphaned, and stale versions/dates. Findings are fixed, or flagged when out of
   scope — never left silent.
4. **Push** — open a PR if none exists, else push.
5. **Codex review loop** — drive a Codex review on the PR to a clean pass.

## Codex review loop (delegating)

This skill **prefers a dedicated loop skill** if one is installed: when a
`codex-review-loop` skill is available (e.g. from the
[`codex-review`](https://github.com/djm204/codex-review) plugin), `task-end` invokes it as
the source of truth for the mechanics — three-channel polling, the terminal "no major
issues" signal, and thread resolution.

Only if no such skill is installed does it fall back to the **inline 3-round loop**: comment
`@codex review`, address every thread (fix, reply, resolve via GraphQL), push, repeat up to
3 rounds (a 4th if a P1 lands in round 3) — never ship with open P1s.

## Related

Closes the loop opened by [`task-start`](task-start.md); shares the ADR convention with it.
