# Codex rate-limit liveness for PM swarms

Session-derived pattern for GitHub/Codex review-gated issue swarms.

## Problem

Workers may block themselves when GitHub Codex review returns a usage/rate-limit message or goes silent after an `@codex review` trigger. A naive liveness check keeps reporting the blocker or duplicate-suppresses PM pings, but never nudges the worker after limits subside.

## Durable fix pattern

For liveness monitors that supervise Codex-gated worker cards:

1. Inspect blocked worker summaries for transient Codex/rate-limit language, e.g. `usage limit`, `rate limit`, `no fresh current-head all-clear`, or `blocked_by_usage_limit`.
2. Extract the referenced PR number from the worker summary.
3. Query the PR with `gh pr view <pr> --json state,headRefOid,comments,reviews`.
4. If a `chatgpt-codex-connector` review exists on the current `headRefOid` after the last usage-limit comment, unblock the worker without retriggering; it can resume fixing/merging.
5. Otherwise compare the latest usage-limit comment or latest `@codex review` trigger to a configurable retry window, e.g. 30 minutes.
6. When the retry window elapsed, post `@codex review`, unblock the worker with a clear reason, and run one dispatcher pass.
7. Keep non-transient blockers (iteration budget, approval-required, real check failures, unresolved review findings) blocked; do not mask them as rate-limit nudges.

## Pitfalls

- Do not scan full task bodies for rate-limit text. Card instructions often contain fallback-policy wording and cause false positives. Scan slim task state plus worker summaries/comments.
- Do not permanently switch the profile/provider because a rate-limit happened. Use fallback only while actively limited and return to the primary once clear.
- Do not count old Codex reviews as current-head all-clear. Match the review commit to the PR `headRefOid`.
- After unblocking, verify by checking Kanban state and the PR comments/reviews, not just script stdout.

## Verification recipe

Run the liveness script directly, then verify:

```bash
python3 ~/.hermes/scripts/<project>_liveness.py
hermes kanban --board <board> show <worker_id>
gh pr view <pr> --repo <owner/repo> --json comments,reviews,headRefOid
```

Expected outcome for retryable pauses:

- liveness output includes `codex-retriggered:PR#...` when a fresh trigger was needed;
- blocked worker cards get an `UNBLOCK:` comment explaining the retry/resume reason;
- worker status becomes `ready` or gets respawned/guarded by the dispatcher according to active PR safeguards.
