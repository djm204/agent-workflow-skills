---
name: session-start
description: Auto-runs at session start to establish full context with minimal token cost. Loads memory, git state, WIP tasks, and active lessons.
---

# Session Start Protocol

The SessionStart hook already ran and injected a compact brief. Use it. Do not re-read files that were summarized there unless you need detail on a specific item.

## What the hook loaded

| Signal | Source | Action if missing |
|--------|--------|-------------------|
| Git branch + last commit | `git log/status` | Skip, note no git repo |
| Memory index | MEMORY.md (project or global) | Skip, note no memory |
| WIP tasks | tasks/todo.md (pending items) | Skip, note no todo |
| Active lessons | tasks/lessons.md (last 20 lines) | Skip, note no lessons |

## When to load more detail

Only drill into a memory file or todo item if the user's first message directly involves it. Do not speculatively read files — the brief is sufficient to orient.

## Output

After the hook context loads, your first response should:
1. Acknowledge branch + any uncommitted work (one line)
2. Surface any in-progress todo items the user should know about (one line)
3. Apply active lessons immediately — do not list them back to the user

That's it. No verbose recap. Answer the user's actual message.
