# Approval-cop `blocked command was` extraction

Session learning: Bob-the-Builder encountered approval-required workers where the exact command was present, but approval-cop could not extract it because the wording did not match the existing parser labels. Known variants include fenced command blocks introduced by `The blocked command was:` and approval summaries that say a side effect is `blocked by approval for` or `blocked by approval on the exact command` followed by a backticked command.

## Durable fix pattern

When approval-cop reports `approval-cop-needs-exact-command:<task_id>` but Kanban comments visibly contain an exact command:

1. Inspect `hermes kanban --board <board> show <task_id> --json` and find the latest approval-required comment.
2. Extend parser labels to accept optional filler words and approval-summary phrasing where the command is backticked after `blocked by approval` instead of after a `blocked command:` label:
   - `blocked command ... was:\n\n```bash ... ````
   - `blocked command ... was: `inline command``
   - `blocked by approval for `inline command``
   - `blocked by approval on the exact command `inline command``
3. Preserve multi-line command structure when extracting fenced shell blocks; do not collapse command blocks that rely on `cd`, `set -euo pipefail`, or line comments.
4. Add the narrowest allowlist pattern for the exact intended command shape. For issue worktree amend/publish handoffs, the safe shape is commonly:
   - `set -euo pipefail`
   - `cd /home/pfkagent/dev/resolve-wt/issue-<n>`
   - optional comment line
   - `git diff --check`
   - `git status --short`
   - `git add <explicit file list>`
   - `git commit --amend --no-edit`
   - `git push --force-with-lease origin HEAD`
5. Run approval-cop in dry-run first and require `approval-requested:<task_id>:<token>` before the real request.
6. Run the real request only through approval-cop so it creates a durable pending token; do not execute the Git command directly.
7. Mark Bob items done for the HITL-pending case once the durable token exists, `missing_exact_command` is clear, a Kanban `BOB TREATMENT` comment names the token/workdir, and a fresh Bob scan no longer lists the item open.

## Verification recipe

Use an ad-hoc verifier under `/tmp` with `tempfile.mkstemp(prefix="hermes-verify-", dir="/tmp", suffix=".py")`. The verifier should:

- import/compile both the live `~/.hermes/scripts/pm_swarm_approval_cop.py` and the workflow-skills copy when applicable;
- construct approval-gated shell text as inert Python fixture data, not as a shell command or heredoc;
- drive `extract_command_and_workdir()` with a Kanban-show fixture containing the discovered wording variant plus the exact command;
- assert the exact command is extracted, the issue worktree is selected, and `is_allowed_command()` accepts the exact shape;
- self-delete with `Path(__file__).unlink()` when possible.

This is ad-hoc verification, not canonical suite green.
