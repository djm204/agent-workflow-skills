---
name: gemini-session-start
description: "Use at the beginning of a Gemini coding session or recovery handoff to establish compact context: git state, progress docs, recent tasks, and durable project lessons without wasting tokens."
version: 1.0.0
author: djm204, adapted for Gemini Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  gemini:
    tags: [workflow, session-start, context, recovery]
    related_skills: [gemini-task-start, systematic-debugging]
---

# Gemini Session Start Protocol

## Overview

Antigravity plugin hooks do not auto-run in Gemini, so invoke this skill explicitly when starting or resuming project work. The goal is a compact context brief, not a full repo audit.

## Context to Load

| Signal | Source | Action if missing |
|--------|--------|-------------------|
| Branch/status | `git status --short --branch` | note no git repo |
| Recent commits | `git log --oneline -5` | skip if no git repo |
| Progress docs | `tasks/*-progress.md`, `tasks/todo.md` | skip if absent |
| Lessons | `tasks/lessons.md`, project docs | skip if absent |
| Prior chat | `session_search` when user references it | ask only if unretrievable |

## Procedure

1. Read only the compact sources above.
2. Do not speculatively read full files unless the user's request needs detail.
3. Summarize branch, dirty work, active progress item, and immediate risk in a few lines.
4. Apply lessons silently; do not recite them unless relevant.
5. Move into the user's actual request.

## Output Shape

```text
Branch/status: ...
Active work: ...
Relevant context: ...
Next action: ...
```

Keep it short; this is a launch checklist, not a report.

## Verification Checklist

- [ ] Git state checked when in a repo.
- [ ] Progress docs scanned if present.
- [ ] No broad file reading without a task-specific reason.
- [ ] User's actual request remains the focus.
