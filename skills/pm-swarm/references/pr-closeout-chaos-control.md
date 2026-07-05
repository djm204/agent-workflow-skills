# PR closeout chaos control

Use this pattern when a PM swarm has accumulated many open PRs and workers are not reliably merging them.

## Trigger signals

- Open PR count is growing or staying high despite workers reporting progress.
- User says the PM is not orchestrating, PRs are not merging, or Codex review loops are not running.
- Cards are marked done while their PRs remain open.
- Many PRs are waiting on Codex usage windows, unresolved Codex threads, dirty local worktrees, or CI registration.

## Recovery pattern

1. Switch the swarm to closeout mode.
   - Comment on the root blackboard and PM cards: no new issue work; reduce the open PR queue first.
   - Block or freeze cards that would start the next issue after a successful merge.
   - Keep verifier/synthesizer gated until PR closeout cards complete.

2. Inventory every open PR from GitHub, not from worker summaries.
   - PR number, title, branch, head SHA, merge state, CI/check state.
   - Current-head Codex status: latest trigger, usage-limit response, clean comment, unresolved current-head Codex threads/reviews.
   - Local worktree state for the PR branch; distinguish tracked dirty changes from untracked progress docs.

3. Create a central closeout watchdog owned by the top-level orchestrator.
   - Prefer a deterministic script/no-agent cron job for repeatable polling.
   - Run every 10 minutes for urgent cleanup, with a bounded repeat count or self-removal condition.
   - Deliver status to the user's expected channel when configured.
   - The watchdog may trigger `@codex review` when a retry window has elapsed and may merge only when all gates are objectively clean.

4. Merge only under objective gates.
   - CI/checks green for the current head.
   - Fresh current-head Codex all-clear after the latest trigger.
   - Zero unresolved current-head Codex-authored review threads.
   - No tracked dirty local fix waiting to be pushed for that branch.
   - Do not merge release-please or other special PRs unless explicitly in scope.

5. Route blocked PRs to explicit closeout cards.
   - One closeout card per PR with actionable Codex findings or local dirty fixes.
   - Body must say: close this PR only, do not start new issues, read shared lessons, fix/reply/resolve, push, retrigger Codex, merge only after gates are clean, then self-reflect/update lessons/stop.
   - Link closeout cards as parents of verifier/synthesizer so false completion cannot pass.

6. Report in human-readable queue form.
   - Open PR count now vs. before.
   - Merged PRs.
   - Remaining PRs with owner card and exact blocker/waiting state.
   - Watchdog job id/schedule/delivery.

## Pitfalls

- Passive worker nudges are not enough when review loops are not running; add a central watchdog.
- Do not treat a prior usage-limit comment as permanent. Retry after the configured window, but check for existing actionable comments/threads before triggering.
- Do not let untracked task/progress docs make a PR look dirty; dirty means tracked PR code changes still need commit/push.
- Do not let a card marked done pass verification if its PR is still open; create/link a fresh closeout follow-up card.
