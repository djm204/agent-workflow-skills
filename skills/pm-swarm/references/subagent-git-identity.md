# Subagent Git identity enforcement

When PM-swarm or Kanban dispatches coding subagents, enforce commit identity in two layers:

1. Worker process environment:
   - `GIT_AUTHOR_NAME=David Mendez`
   - `GIT_AUTHOR_EMAIL=me@davidmendez.dev`
   - `GIT_COMMITTER_NAME=David Mendez`
   - `GIT_COMMITTER_EMAIL=me@davidmendez.dev`
2. Worker workspace local Git config, for any existing/reused worktree:
   - `git -C <workspace> config user.name 'David Mendez'`
   - `git -C <workspace> config user.email 'me@davidmendez.dev'`

Why both: environment variables are the hard guarantee for commits made by the running subagent; local repo config fixes tools and future commands that inspect or reuse the worktree outside that exact process.

Verification pattern:
- Run the repository's test wrapper first when it exists, e.g. `scripts/run_tests.sh ...`, because raw `python -m pytest` may not be installed in the ambient interpreter.
- Add/keep a narrow spawn probe or test that captures `_default_spawn` env and asserts the four Git identity variables.
- Add/keep a narrow workspace-config probe or test that calls the identity helper and asserts `git config user.name/user.email`.

Do not encode placeholder identities (`test`, `codex`, `AI Agent`) in PM-swarm worker setup paths.