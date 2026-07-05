#!/usr/bin/env python3
"""Generic low-token liveness checker for Hermes PM-swarm / Kanban workflows.

Usage:
  pm_swarm_liveness.py /path/to/config.json

Designed for no_agent cron jobs. It prints a concise liveness-check report:
- `💓💓💓 LIVENESS CHECK 💓💓💓` banner first
- `✅ LIVENESS OK ...` when healthy
- `🚨 LIVENESS PROBLEM ...` when unhealthy and after triggering lightweight intervention

The script is deliberately deterministic and non-LLM so 5-minute checks do not waste tokens.
Repo/project-specific behavior lives in JSON config files, not in this script.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_STALE_MINUTES = 30
DEFAULT_PING_SUPPRESS_MINUTES = 30
DEFAULT_CODEX_RETRY_AFTER_MINUTES = 20
DEFAULT_CODEX_RETRY_MAX_ATTEMPTS = 3


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def run(cmd: list[str], cwd: Path, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def compact_error(cp: subprocess.CompletedProcess[str]) -> str:
    text = (cp.stderr.strip() or cp.stdout.strip()).replace("\n", " ")
    return text[:300]


def load_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def save_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def append_progress(progress_path: Path | None, line: str) -> None:
    if not progress_path:
        return
    try:
        text = progress_path.read_text() if progress_path.exists() else ""
        if "## Liveness log" not in text:
            text = text.rstrip() + "\n\n## Liveness log\n"
        progress_path.write_text(text.rstrip() + "\n" + line.rstrip() + "\n")
    except Exception:
        # Liveness should still report Discord output even if doc logging fails.
        pass


def task_snapshot(task_id: str, board: str, cwd: Path) -> tuple[datetime | None, str]:
    cp = run(["hermes", "kanban", "--board", board, "show", task_id], cwd, timeout=60)
    if cp.returncode != 0:
        return None, ""
    summary = ""
    m = re.search(r"Latest summary:\n(.+?)(?:\n\n|\Z)", cp.stdout, flags=re.S)
    if m:
        summary = " ".join(m.group(1).split())[:500]
    matches = re.findall(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]", cp.stdout)
    if not matches:
        return None, summary
    # Hermes CLI currently renders local wall-clock timestamps. Interpret using local timezone.
    latest = datetime.strptime(matches[-1], "%Y-%m-%d %H:%M").astimezone().astimezone(timezone.utc)
    return latest, summary


def kanban_comment(board: str, task_id: str, message: str, cwd: Path, action: str = "pm-pinged") -> str:
    cp = run(["hermes", "kanban", "--board", board, "comment", task_id, message[:1800]], cwd, timeout=60)
    if cp.returncode == 0:
        return action
    if action == "pm-pinged":
        prefix = "pm-ping-failed"
    elif action.startswith("worker-nudged:"):
        prefix = action.replace("worker-nudged:", "worker-nudge-failed:", 1)
    else:
        prefix = f"{action}-failed"
    return prefix + ":" + compact_error(cp)


def kanban_unblock(board: str, task_id: str, reason: str, cwd: Path) -> str:
    cp = run(["hermes", "kanban", "--board", board, "unblock", task_id, "--reason", reason[:1800]], cwd, timeout=60)
    if cp.returncode == 0:
        return f"worker-unblocked:{task_id}"
    return f"worker-unblock-failed:{task_id}:" + compact_error(cp)


def kanban_archive_worker(board: str, task_id: str, display_name: str, cwd: Path) -> str:
    """Archive a terminal worker card so it cannot be reused for another issue.

    In Kanban terms this is the deterministic equivalent of "killing" a done
    worker/session: the completed card leaves the active board, and the PM must
    create/dispatch a new one-issue worker for remaining issue work.
    """
    cp = run(["hermes", "kanban", "--board", board, "archive", task_id], cwd, timeout=60)
    if cp.returncode == 0:
        return f"worker-terminated:{display_name}:{task_id}"
    return f"worker-terminate-failed:{display_name}:{task_id}:" + compact_error(cp)


def gh_pr_comment(repo: str, pr: str, body: str, cwd: Path) -> str:
    cp = run(["gh", "pr", "comment", pr, "--repo", repo, "--body", body], cwd, timeout=60)
    if cp.returncode == 0:
        return f"codex-retriggered:PR#{pr}"
    return f"codex-retrigger-failed:PR#{pr}:" + compact_error(cp)


def dispatch_once(board: str, cwd: Path) -> str:
    cp = run(["hermes", "kanban", "--board", board, "dispatch"], cwd, timeout=120)
    if cp.returncode == 0:
        return "dispatch-run"
    return "dispatch-failed:" + compact_error(cp)


def worker_refill(cwd: Path) -> str:
    script = Path(__file__).with_name("frankenbeast_worker_refill.py")
    if not script.exists():
        return "worker-refill-unavailable"
    cp = run([sys.executable, str(script)], cwd, timeout=240)
    if cp.returncode == 0:
        text = ";".join(line.strip() for line in cp.stdout.splitlines() if line.strip())
        return "worker-refill:" + (text[:500] if text else "no-op")
    return "worker-refill-failed:" + compact_error(cp)


def doctor_worker(config_path: Path, task_id: str, cwd: Path) -> str:
    doctor = Path(__file__).with_name("pm_swarm_doctor.py")
    cp = run([sys.executable, str(doctor), str(config_path), task_id, "--pm-ping", "--doctor", "--discord"], cwd, timeout=240)
    if cp.returncode == 0:
        m = re.search(r"doctor-card-created:([^\s;]+)", cp.stdout)
        suffix = f":{m.group(1)}" if m else ""
        dispatch = run(["hermes", "kanban", "--board", "default", "dispatch", "--max", "5", "--json"], cwd, timeout=120)
        dispatch_suffix = ":dispatch" if dispatch.returncode == 0 else ":dispatch-failed"
        return f"doctor-dispatched:{task_id}{suffix}{dispatch_suffix}"
    return f"doctor-dispatch-failed:{task_id}:" + compact_error(cp)


def active_doctor_for(task_id: str, tasks: list[dict[str, Any]]) -> dict[str, Any] | None:
    needle = f"Doctor stalled/unstable workers {task_id}"
    for task in tasks:
        if str(task.get("status") or "") != "running":
            continue
        title = str(task.get("title") or "")
        body = str(task.get("body") or "")
        if needle in title or task_id in body:
            return task
    return None


def doctor_checkup(board: str, doctor_id: str, stalled_id: str, cwd: Path) -> str:
    msg = (
        f"CHECKUP REQUEST: liveness still sees worker {stalled_id} stranded in ready/todo with repeated active_pr guard churn. "
        "You are the active doctor for this stalled worker. Re-check live PR/Codex/CI/Kanban state now; either continue the concrete fix/merge path, "
        "replace the stale ready worker with the actual active closeout/doctor card in liveness, or block with an exact HITL decision. "
        "Comment a concise diagnosis and next action so liveness stops repeatedly nudging a ready card that is not doing work."
    )
    return kanban_comment(board, doctor_id, msg, cwd, action=f"doctor-checkup-requested:{stalled_id}:{doctor_id}")


def approval_cop_request(config_path: Path, task_id: str, cwd: Path) -> str:
    cop = Path(__file__).with_name("pm_swarm_approval_cop.py")
    cp = run([sys.executable, str(cop), str(config_path), task_id, "--request"], cwd, timeout=120)
    if cp.returncode == 0:
        lines = [line.strip() for line in (cp.stdout or "").splitlines() if line.strip()]
        if lines:
            return "approval-cop:" + ";".join(lines[:4])
        return f"approval-cop:no-new-request:{task_id}"
    return f"approval-cop-failed:{task_id}:" + compact_error(cp)


def approval_required_text(text: str) -> bool:
    lower = text.lower()
    return any(
        marker in lower
        for marker in (
            "approval required",
            "approval-required",
            "approval_blocked",
            "approval-blocked",
            "push approval required",
            "approval layer",
            "blocked by approval",
        )
    )


def kanban_diagnostics_by_task(board: str, cwd: Path) -> dict[str, list[dict[str, Any]]]:
    cp = run(["hermes", "kanban", "--board", board, "diagnostics", "--json"], cwd, timeout=60)
    if cp.returncode != 0:
        return {}
    try:
        raw = json.loads(cp.stdout or "[]")
    except Exception:
        return {}
    out: dict[str, list[dict[str, Any]]] = {}
    for item in raw if isinstance(raw, list) else []:
        tid = str(item.get("task_id") or "")
        if tid:
            out[tid] = [d for d in (item.get("diagnostics") or []) if isinstance(d, dict)]
    return out


def stranded_ready_age_seconds(diagnostics: dict[str, list[dict[str, Any]]], task_id: str) -> int | None:
    for diag in diagnostics.get(task_id) or []:
        if diag.get("kind") == "stranded_in_ready":
            data = diag.get("data") or {}
            try:
                return int(data.get("age_seconds"))
            except Exception:
                return None
    return None


def task_detail_json(task_id: str, board: str, cwd: Path) -> dict[str, Any]:
    cp = run(["hermes", "kanban", "--board", board, "show", task_id, "--json"], cwd, timeout=60)
    if cp.returncode != 0:
        return {}
    try:
        data = json.loads(cp.stdout or "{}")
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def recent_event_count(detail: dict[str, Any], kind: str, reason: str | None = None, window: int = 25) -> int:
    count = 0
    for event in (detail.get("events") or [])[-window:]:
        if event.get("kind") != kind:
            continue
        if reason is not None:
            payload = event.get("payload") or {}
            if str(payload.get("reason") or "") != reason:
                continue
        count += 1
    return count


def latest_notes(detail: dict[str, Any], window: int = 12) -> str:
    chunks: list[str] = []
    task = detail.get("task") or {}
    for key in ("title", "body", "result"):
        value = task.get(key)
        if value:
            chunks.append(str(value))
    summary = detail.get("latest_summary")
    if summary:
        chunks.append(str(summary))
    for event in (detail.get("events") or [])[-window:]:
        payload = event.get("payload") or {}
        note = payload.get("note") if isinstance(payload, dict) else None
        if note:
            chunks.append(str(note))
    for comment in (detail.get("comments") or [])[-3:]:
        body = comment.get("body")
        if body:
            chunks.append(str(body))
    return "\n".join(chunks)[-5000:]


def extract_pr_numbers_from_text(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for pr in re.findall(r"(?:PR|pull request)\s*#(\d+)", text, flags=re.I):
        if pr not in seen:
            seen.add(pr)
            out.append(pr)
    return out


def extract_issue_numbers_from_text(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for issue in re.findall(r"(?:issue|closes|fixes|resolves)\s*#(\d+)", text, flags=re.I):
        if issue not in seen:
            seen.add(issue)
            out.append(issue)
    return out


def live_terminal_reason(repo: str | None, task: dict[str, Any], summary: str, cwd: Path, detail: dict[str, Any] | None = None) -> str | None:
    """Return a terminal live-state reason for stale worker cards.

    This is the automated "kill stuck dead card" check: if GitHub says the
    issue is closed or the active PR is merged/closed, liveness should prune the
    old worker even if the Kanban card is stranded in todo/ready/blocked.
    """
    if not repo:
        return None
    notes = latest_notes(detail or {"task": task, "latest_summary": summary})
    prs = extract_pr_numbers_from_text(notes)
    for pr in reversed(prs):
        cp = run(["gh", "pr", "view", pr, "--repo", repo, "--json", "number,state,url,closed,closedAt,mergeCommit"], cwd, timeout=60)
        if cp.returncode != 0:
            continue
        try:
            data = json.loads(cp.stdout or "{}")
        except Exception:
            continue
        state = str(data.get("state") or "").upper()
        if state in {"MERGED", "CLOSED"} or data.get("closed"):
            merge = data.get("mergeCommit") or {}
            sha = (merge.get("oid") or "")[:12] if isinstance(merge, dict) else ""
            suffix = f" at {sha}" if sha else ""
            return f"PR #{pr} is {state or 'closed'} on live GitHub{suffix}"

    issue_nums = extract_issue_numbers_from_text(notes)
    for issue in reversed(issue_nums):
        cp = run(["gh", "issue", "view", issue, "--repo", repo, "--json", "number,state,url,closedAt"], cwd, timeout=60)
        if cp.returncode != 0:
            continue
        try:
            data = json.loads(cp.stdout or "{}")
        except Exception:
            continue
        state = str(data.get("state") or "").upper()
        if state == "CLOSED" or data.get("closed"):
            return f"issue #{issue} is CLOSED on live GitHub"
    return None


def current_head_codex_unresolved(repo: str | None, pr: str, cwd: Path) -> tuple[int | None, str | None]:
    if not repo:
        return None, None
    if "/" not in repo:
        return None, None
    owner, name = repo.split("/", 1)
    query = (
        "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
        "pullRequest(number:$number){headRefOid reviewThreads(first:100){nodes{id isResolved isOutdated "
        "comments(first:20){nodes{author{login} body path line commit{oid} createdAt}}}} "
        "latestReviews(first:10){nodes{author{login} submittedAt commit{oid} body}}}}}"
    )
    cp = run(
        ["gh", "api", "graphql", "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={pr}", "-f", f"query={query}"],
        cwd,
        timeout=60,
    )
    if cp.returncode != 0:
        return None, None
    try:
        pr_data = json.loads(cp.stdout or "{}")['data']['repository']['pullRequest']
    except Exception:
        return None, None
    head = str(pr_data.get("headRefOid") or "")
    latest_current_review: str | None = None
    for review in ((pr_data.get("latestReviews") or {}).get("nodes") or []):
        author = ((review.get("author") or {}).get("login") or "").lower()
        commit = ((review.get("commit") or {}).get("oid") or "")
        if author == "chatgpt-codex-connector" and head and commit == head:
            submitted = review.get("submittedAt")
            if submitted and (latest_current_review is None or submitted > latest_current_review):
                latest_current_review = str(submitted)
    unresolved = 0
    for thread in ((pr_data.get("reviewThreads") or {}).get("nodes") or []):
        if thread.get("isResolved"):
            continue
        comments = ((thread.get("comments") or {}).get("nodes") or [])
        if any(((c.get("author") or {}).get("login") or "").lower() == "chatgpt-codex-connector" and ((c.get("commit") or {}).get("oid") or "") == head for c in comments):
            unresolved += 1
    return unresolved, latest_current_review


def churn_probe_reason(status: str, task_id: str, detail: dict[str, Any], repo: str | None, cwd: Path) -> str | None:
    notes = latest_notes(detail)
    prs = extract_pr_numbers_from_text(notes)
    if status in {"ready", "todo"}:
        guarded = recent_event_count(detail, "respawn_guarded", "active_pr")
        if guarded >= 2:
            pr_text = f" for PR #{prs[-1]}" if prs else ""
            return f"repeated respawn_guarded(active_pr) ({guarded} recent events){pr_text}; likely broad shard/active-PR churn"
    if status == "running" and prs:
        text = notes.lower()
        waiting_on_old_evidence = any(phrase in text for phrase in ("waiting for codex", "still waiting", "zero unresolved", "no unresolved", "no new inline findings"))
        if waiting_on_old_evidence:
            unresolved, review_time = current_head_codex_unresolved(repo, prs[-1], cwd)
            if unresolved and unresolved > 0:
                return f"worker appears to be waiting on obsolete Codex evidence, but PR #{prs[-1]} has {unresolved} current-head unresolved Codex thread(s) from {review_time or 'latest review'}"
    return None


def count_open_issues(repo: str | None, cwd: Path, problems: list[str]) -> str:
    if not repo:
        return "n/a"
    auth = run(["gh", "auth", "status"], cwd, timeout=30)
    if auth.returncode != 0:
        problems.append("GitHub auth unavailable")
        return "unknown"
    cp = run(["gh", "issue", "list", "--repo", repo, "--state", "open", "--limit", "1000", "--json", "number"], cwd, timeout=60)
    if cp.returncode != 0:
        problems.append(f"failed to list GitHub issues for {repo}")
        return "unknown"
    try:
        return str(len(json.loads(cp.stdout or "[]")))
    except Exception:
        problems.append("failed to parse GitHub issue list")
        return "unknown"


def active_pr_gate(summary: str, repo: str | None, cwd: Path) -> str | None:
    """Detect PR gates for non-running workers and distinguish work from waiting."""
    if not repo or not summary:
        return None
    nums = re.findall(r"(?:PR|pull request)\s*#(\d+)", summary, flags=re.I)
    if not nums:
        return None
    pr = nums[-1]
    cp = run(
        [
            "gh",
            "pr",
            "view",
            pr,
            "--repo",
            repo,
            "--json",
            "number,state,headRefOid,mergeStateStatus,statusCheckRollup,reviewDecision,url,comments,reviews",
        ],
        cwd,
        timeout=60,
    )
    if cp.returncode != 0:
        return None
    try:
        data = json.loads(cp.stdout or "{}")
    except Exception:
        return None
    if data.get("state") != "OPEN":
        return None
    pending: list[str] = []
    failing: list[str] = []
    for check in data.get("statusCheckRollup") or []:
        status = str(check.get("status") or "").upper()
        conclusion = str(check.get("conclusion") or "").upper()
        name = check.get("name") or check.get("workflowName") or "check"
        if status and status != "COMPLETED":
            pending.append(f"{name}:{status}")
        elif conclusion and conclusion not in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            failing.append(f"{name}:{conclusion}")
    if pending:
        return f"PR #{pr} external gate pending ({', '.join(pending)[:160]})"
    if failing:
        return f"PR #{pr} check failure needs worker ({', '.join(failing)[:160]})"
    if data.get("reviewDecision"):
        return None

    head = str(data.get("headRefOid") or "")
    latest_trigger: datetime | None = None
    latest_bot_response: datetime | None = None
    bot_review_current_head = False
    comment_count = len(data.get("comments") or [])
    review_count = len(data.get("reviews") or [])

    for comment in data.get("comments") or []:
        body = str(comment.get("body") or "").strip().lower()
        created = parse_github_time(comment.get("createdAt"))
        author = ((comment.get("author") or {}).get("login") or "").lower()
        if body == "@codex review" and created:
            latest_trigger = max(latest_trigger, created) if latest_trigger else created
        if author == "chatgpt-codex-connector" and created:
            latest_bot_response = max(latest_bot_response, created) if latest_bot_response else created

    for review in data.get("reviews") or []:
        author = ((review.get("author") or {}).get("login") or "").lower()
        submitted = parse_github_time(review.get("submittedAt"))
        commit = ((review.get("commit") or {}).get("oid") or "")
        if author == "chatgpt-codex-connector" and submitted:
            latest_bot_response = max(latest_bot_response, submitted) if latest_bot_response else submitted
            if head and commit == head:
                bot_review_current_head = True

    if bot_review_current_head:
        return (
            f"PR #{pr} has current-head Codex review/comments; needs worker to inspect PR comments/reviews, "
            "address or explicitly resolve all actionable feedback, then merge if CI/Codex are clean; after merge/blocker self-reflect, update shared lessons, complete/block, and terminate this worker/session so the PM assigns the next issue to a fresh one-issue worker"
        )
    if latest_trigger and (not latest_bot_response or latest_bot_response < latest_trigger):
        return (
            f"PR #{pr} Codex loop was triggered and has not produced a current-head result; needs worker to poll PR comments/reviews, "
            "address feedback if present, or continue/retrigger the Codex loop instead of waiting idle; after merge/blocker self-reflect, update shared lessons, complete/block, and terminate this worker/session so the PM assigns the next issue to a fresh one-issue worker"
        )
    return (
        f"PR #{pr} review/Codex gate unresolved with {comment_count} comments and {review_count} reviews; "
        "needs worker to inspect existing PR comments/reviews first, address any feedback, otherwise trigger @codex review for the current head; after merge/blocker self-reflect, update shared lessons, complete/block, and terminate this worker/session so the PM assigns the next issue to a fresh one-issue worker"
    )


def parse_github_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def rate_limit_blocker(summary: str) -> bool:
    text = summary.lower()
    return any(
        phrase in text
        for phrase in (
            "usage limit",
            "rate limit",
            "rate-limit",
            "no fresh current-head all-clear",
            "blocked_by_usage_limit",
        )
    )


def codex_retry_nudge(summary: str, repo: str | None, cwd: Path, now: datetime, retry_after_minutes: int) -> tuple[str | None, list[str]]:
    """Find transient Codex rate-limit blockers that are safe to retry.

    Workers can remain blocked indefinitely after GitHub Codex returns a usage-limit
    response. When that blocker is old enough, retrigger `@codex review` and unblock
    the worker so it can resume the normal review/fix/merge loop. If Codex already
    produced a current-head review after the usage-limit message, just unblock.
    """
    if not repo or not summary or not rate_limit_blocker(summary):
        return None, []
    nums = re.findall(r"(?:PR|pull request)\s*#(\d+)", summary, flags=re.I)
    if not nums:
        return None, []
    pr = nums[-1]
    cp = run(
        ["gh", "pr", "view", pr, "--repo", repo, "--json", "number,state,headRefOid,comments,reviews,url"],
        cwd,
        timeout=60,
    )
    if cp.returncode != 0:
        return None, [f"codex-nudge-inspect-failed:PR#{pr}:" + compact_error(cp)]
    try:
        data = json.loads(cp.stdout or "{}")
    except Exception:
        return None, [f"codex-nudge-parse-failed:PR#{pr}"]
    if data.get("state") != "OPEN":
        return None, []

    head = str(data.get("headRefOid") or "")
    latest_limit: datetime | None = None
    latest_trigger: datetime | None = None
    for comment in data.get("comments") or []:
        body = str(comment.get("body") or "").lower()
        created = parse_github_time(comment.get("createdAt"))
        author = ((comment.get("author") or {}).get("login") or "").lower()
        if body.strip() == "@codex review" and created:
            latest_trigger = max(latest_trigger, created) if latest_trigger else created
        if author == "chatgpt-codex-connector" and "usage limit" in body and created:
            latest_limit = max(latest_limit, created) if latest_limit else created

    for review in data.get("reviews") or []:
        author = ((review.get("author") or {}).get("login") or "").lower()
        commit = ((review.get("commit") or {}).get("oid") or "")
        submitted = parse_github_time(review.get("submittedAt"))
        if author == "chatgpt-codex-connector" and head and commit == head and submitted:
            if not latest_limit or submitted > latest_limit:
                return (
                    f"Liveness retry: PR #{pr} now has a current-head GitHub Codex review after the prior usage-limit blocker. Resume active ownership now: read tasks/resolve-issues-shared-lessons.md first; inspect PR comments, reviews, and unresolved threads; address or explicitly resolve feedback; if no actionable feedback remains, merge when CI/Codex gates are clean instead of waiting idle. After merge/blocker, self-reflect, append concise reusable lessons, complete/block, and terminate this worker/session so the next issue gets a fresh one-issue worker/context.",
                    [],
                )

    last_gate = max([dt for dt in (latest_limit, latest_trigger) if dt], default=None)
    if last_gate and now - last_gate < timedelta(minutes=retry_after_minutes):
        return None, [f"codex-nudge-waiting:PR#{pr}"]

    action = gh_pr_comment(repo, pr, "@codex review", cwd)
    if action.startswith("codex-retriggered"):
        return (
            f"Liveness retry: prior blocker for PR #{pr} was a transient GitHub Codex usage/rate limit and the retry window has elapsed; @codex review was retriggered. Resume active ownership now: read tasks/resolve-issues-shared-lessons.md first; poll PR comments/reviews, address or explicitly resolve any feedback, and continue the Codex loop/merge path instead of waiting idle. After merge/blocker, self-reflect, append concise reusable lessons, complete/block, and terminate this worker/session so the next issue gets a fresh one-issue worker/context.",
            [action],
        )
    return None, [action]


def waiting_codex_review(summary: str) -> bool:
    text = summary.lower()
    return "codex" in text and any(
        phrase in text
        for phrase in (
            "has not produced a current-head result",
            "has not produced",
            "waiting on codex",
            "waiting for codex",
            "codex gate stalled",
            "codex review gate is stalled",
            "no clean response",
            "no terminal response",
        )
    )


def codex_silent_review_retry(
    summary: str,
    repo: str | None,
    cwd: Path,
    now: datetime,
    retry_after_minutes: int,
    max_attempts: int,
    state: dict[str, Any],
) -> tuple[str | None, list[str]]:
    """Retrigger truly silent `@codex review` requests on a bounded schedule."""
    if not repo or not summary or not waiting_codex_review(summary):
        return None, []
    nums = re.findall(r"(?:PR|pull request)\s*#(\d+)", summary, flags=re.I)
    if not nums:
        return None, []
    pr = nums[-1]
    cp = run(
        ["gh", "pr", "view", pr, "--repo", repo, "--json", "number,state,headRefOid,comments,reviews,url"],
        cwd,
        timeout=60,
    )
    if cp.returncode != 0:
        return None, [f"codex-silent-inspect-failed:PR#{pr}:" + compact_error(cp)]
    try:
        data = json.loads(cp.stdout or "{}")
    except Exception:
        return None, [f"codex-silent-parse-failed:PR#{pr}"]
    if data.get("state") != "OPEN":
        return None, []

    head = str(data.get("headRefOid") or "")
    latest_trigger: datetime | None = None
    latest_current_head_bot_response: datetime | None = None
    for comment in data.get("comments") or []:
        body = str(comment.get("body") or "").strip().lower()
        author = ((comment.get("author") or {}).get("login") or "").lower()
        created = parse_github_time(comment.get("createdAt"))
        if body == "@codex review" and created:
            latest_trigger = max(latest_trigger, created) if latest_trigger else created
        if author == "chatgpt-codex-connector" and created:
            latest_current_head_bot_response = max(latest_current_head_bot_response, created) if latest_current_head_bot_response else created

    for review in data.get("reviews") or []:
        author = ((review.get("author") or {}).get("login") or "").lower()
        submitted = parse_github_time(review.get("submittedAt"))
        commit = ((review.get("commit") or {}).get("oid") or "")
        if author == "chatgpt-codex-connector" and submitted and head and commit == head:
            latest_current_head_bot_response = max(latest_current_head_bot_response, submitted) if latest_current_head_bot_response else submitted

    key = f"PR#{pr}:{head}"
    retry_state = state.setdefault("codex_silent_review_retries", {})
    if latest_current_head_bot_response and (not latest_trigger or latest_current_head_bot_response >= latest_trigger):
        retry_state.pop(key, None)
        return (
            f"Liveness Codex retry check: PR #{pr} already has a current-head Codex response at {latest_current_head_bot_response.isoformat(timespec='seconds')}. Do not retrigger; inspect the review/comments, address or explicitly resolve actionable findings, then merge only when CI and current-head Codex are clean.",
            [f"codex-silent-current-head-response:PR#{pr}"],
        )

    if not latest_trigger:
        return None, []
    if now - latest_trigger < timedelta(minutes=retry_after_minutes):
        return None, [f"codex-silent-waiting:PR#{pr}"]

    rec = retry_state.setdefault(key, {"attempts": 0, "first_seen_at": now.isoformat()})
    attempts = int(rec.get("attempts") or 0)
    if attempts >= max_attempts:
        return None, [f"codex-silent-hitl-needed:PR#{pr}:attempts-{attempts}"]

    action = gh_pr_comment(repo, pr, "@codex review", cwd)
    if action.startswith("codex-retriggered"):
        rec["attempts"] = attempts + 1
        rec["last_retriggered_at"] = now.isoformat()
        rec["latest_trigger_at"] = latest_trigger.isoformat()
        return (
            f"Liveness Codex retry: PR #{pr} had no current-head Codex response for at least {retry_after_minutes}m after `@codex review`; retriggered attempt {attempts + 1}/{max_attempts}. Poll for a response, address feedback if posted, and do not wait idle. If all {max_attempts} attempts produce no review, escalate to HITL.",
            [f"{action}:attempt-{attempts + 1}-of-{max_attempts}"],
        )
    return None, [action]


def detect_rate_limit(tasks_text: str, extra_paths: list[Path]) -> bool:
    haystack = tasks_text.lower()
    for path in extra_paths:
        try:
            if path.exists() and path.is_file() and path.stat().st_size < 1_000_000:
                haystack += "\n" + path.read_text(errors="ignore").lower()
        except Exception:
            pass
    patterns = [
        "429",
        "too many requests",
        "usage limit exceeded",
        "rate limit exceeded",
        "ratelimiterror",
        "quota exceeded",
        "requests per minute",
        "tokens per minute",
        "exhausted credentials",
    ]
    return any(p in haystack for p in patterns)


def bullet_list(items: list[str], empty: str) -> str:
    if not items:
        return f"- {empty}"
    return "\n".join(f"- {item}" for item in items)


def slugify_agent_part(value: str, fallback: str) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return text or fallback


def extract_issue_or_pr(task: dict[str, Any], summary: str) -> str:
    haystack = "\n".join(
        str(value or "")
        for value in (
            task.get("title"),
            task.get("body"),
            task.get("result"),
            task.get("summary"),
            summary,
        )
    )
    issue_match = re.search(r"(?:issue|gh)[\s#:-]*(\d+)", haystack, flags=re.I)
    if issue_match:
        return issue_match.group(1)
    pr_match = re.search(r"(?:PR|pull request)[\s#:-]*(\d+)", haystack, flags=re.I)
    if pr_match:
        return f"pr{pr_match.group(1)}"
    hash_match = re.search(r"#(\d+)", haystack)
    if hash_match:
        return hash_match.group(1)
    return "unknownissue"


def agent_name(task: dict[str, Any], status: str, summary: str) -> str:
    base = slugify_agent_part(str(task.get("assignee") or task.get("title") or "worker"), "worker")
    issue = extract_issue_or_pr(task, summary)
    return f"{base}-{issue}-{slugify_agent_part(status, 'unknown')}"


def status_emoji(status: str) -> str:
    return {
        "running": "🟢",
        "done": "✅",
        "ready": "🟡",
        "todo": "📝",
        "blocked": "⛔",
        "unstable": "🚨",
    }.get(status, "❔")


def action_emoji(action: str) -> str:
    if action.startswith("worker-unblocked") or action.startswith("codex-retriggered"):
        return "🔓"
    if action.startswith("worker-terminated"):
        return "💀"
    if action.startswith("worker-nudged") or action.startswith("pm-pinged"):
        return "📣"
    if action.startswith("doctor-dispatched"):
        return "🩺"
    if action.startswith("dispatch-run"):
        return "🚚"
    if "failed" in action:
        return "❌"
    if "suppressed" in action or "waiting" in action:
        return "⏳"
    return "🛠️"


def problem_emoji(problem: str) -> str:
    text = problem.lower()
    if "unstable" in text:
        return "🚨"
    if "blocked" in text or "rate-limit" in text or "rate limit" in text:
        return "⛔"
    if "stale" in text or "not running" in text:
        return "⏰"
    if "failed" in text or "unavailable" in text:
        return "❌"
    if "open pr gate" in text or "codex" in text:
        return "🔎"
    return "⚠️"


def emoji_bullet_list(items: list[str], empty: str, emoji_fn) -> str:
    if not items:
        return f"- ✅ {empty}"
    return "\n".join(f"- {emoji_fn(item)} {item}" for item in items)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("LIVENESS PROBLEM: usage: pm_swarm_liveness.py /path/to/config.json")
        return 2

    config_path = Path(argv[1]).expanduser().resolve()
    cfg = load_json(config_path)
    project = cfg.get("project", config_path.stem)
    workdir = Path(cfg["workdir"]).expanduser().resolve()
    board = cfg.get("board", "default")
    tenant = cfg["tenant"]
    pm_id = cfg["pm_id"]
    workers = list(cfg.get("worker_ids") or [])
    completed_worker_ids_cfg = {str(x) for x in (cfg.get("completed_worker_ids") or {}).keys()}
    deferred_worker_ids_cfg = {str(x) for x in (cfg.get("deferred_worker_ids") or {}).keys()}
    excluded_worker_ids_cfg = completed_worker_ids_cfg | deferred_worker_ids_cfg
    if excluded_worker_ids_cfg:
        workers = [str(w) for w in workers if str(w) not in excluded_worker_ids_cfg]
    repo = cfg.get("repo")
    progress_path = Path(cfg["progress_path"]).expanduser().resolve() if cfg.get("progress_path") else None
    state_path = Path(cfg.get("state_path") or f"~/.hermes/state/{project}_liveness.json").expanduser().resolve()
    stale_minutes = int(cfg.get("stale_minutes", DEFAULT_STALE_MINUTES))
    ping_suppress_minutes = int(cfg.get("ping_suppress_minutes", DEFAULT_PING_SUPPRESS_MINUTES))
    doctor_ready_stale_minutes = int(cfg.get("doctor_ready_stale_minutes", 15))
    doctor_suppress_minutes = int(cfg.get("doctor_suppress_minutes", ping_suppress_minutes))
    doctor_checkup_minutes = int(cfg.get("doctor_checkup_minutes", 5))
    target_active_workers = int(cfg.get("target_active_workers", 0) or 0)
    auto_refill_workers = bool(cfg.get("auto_refill_workers", False))
    churn_probe_enabled = bool(cfg.get("churn_probe_enabled", True))
    codex_retry_after_minutes = int(cfg.get("codex_retry_after_minutes", DEFAULT_CODEX_RETRY_AFTER_MINUTES))
    codex_retry_max_attempts = int(cfg.get("codex_retry_max_attempts", DEFAULT_CODEX_RETRY_MAX_ATTEMPTS))
    intervention_text = cfg.get("intervention_text") or "PM: unblock/reclaim/reassign as appropriate, preserve project invariants, and record exact blockers/human decisions."
    rate_limit_policy = cfg.get("rate_limit_policy") or "If OpenAI/Codex is rate-limited, use the configured Ollama fallback temporarily; switch back to OpenAI primary when clear."
    extra_rate_limit_paths = [Path(p).expanduser().resolve() for p in cfg.get("rate_limit_scan_paths", [])]

    t0 = utcnow()
    problems: list[str] = []
    actions: list[str] = []
    state = load_state(state_path)
    terminated_workers = dict(state.get("terminated_workers") or {})
    doctor_interventions = dict(state.get("doctor_interventions") or {})
    doctor_checkups = dict(state.get("doctor_checkups") or {})
    workers_to_prune: list[str] = []
    worker_replacements: dict[str, str] = {}

    open_issue_count = count_open_issues(repo, workdir, problems)

    kb = run(["hermes", "kanban", "--board", board, "list", "--tenant", tenant, "--json"], workdir, timeout=60)
    tasks: list[dict[str, Any]] = []
    tasks_text = kb.stderr
    if kb.returncode != 0:
        problems.append(f"Kanban list failed for tenant {tenant}")
    else:
        try:
            raw = json.loads(kb.stdout or "[]")
            tasks = [t for t in raw if isinstance(t, dict)]
            # Do not scan full task bodies for rate-limit text: card instructions often
            # contain fallback policy language and would create false-positive interventions.
            slim = [
                {
                    "id": t.get("id"),
                    "status": t.get("status"),
                    "result": t.get("result"),
                    "current_step_key": t.get("current_step_key"),
                }
                for t in tasks
            ]
            tasks_text += "\n" + json.dumps(slim)
        except Exception:
            problems.append("Kanban JSON parse failed")

    by_id = {str(t.get("id")): t for t in tasks}
    kanban_diagnostics = kanban_diagnostics_by_task(board, workdir)
    stale_cutoff = t0 - timedelta(minutes=stale_minutes)
    worker_states: list[str] = []
    for wid in workers:
        task = by_id.get(wid)
        if not task:
            if wid in terminated_workers:
                workers_to_prune.append(wid)
                continue
            problems.append(f"worker {wid} missing from Kanban")
            continue
        status = str(task.get("status") or "unknown")
        latest, summary = task_snapshot(wid, board, workdir)
        latest_s = latest.strftime("%Y-%m-%d %H:%MZ") if latest else "unknown"
        display_name = agent_name(task, status, summary)
        worker_states.append(f"{status_emoji(status)} {display_name} ({wid})@{latest_s}")
        issues_remain = open_issue_count not in {"0", "n/a", "unknown"}
        detail_json = task_detail_json(wid, board, workdir) if issues_remain and (churn_probe_enabled or status in {"blocked", "ready", "todo", "unstable", "running"}) else {}
        terminal_reason = live_terminal_reason(repo, task, summary, workdir, detail_json)
        if terminal_reason and status in {"todo", "ready", "blocked", "unstable", "running"}:
            worker_states[-1] += f"[💀 auto-killed: {terminal_reason}]"
            msg = (
                f"LIVENESS AUTO-KILL {t0.isoformat(timespec='seconds')}: live GitHub terminal state detected ({terminal_reason}). "
                "This stale worker/card is removed from active liveness tracking; refill will create/keep a fresh one-issue worker instead."
            )
            actions.append(kanban_comment(board, wid, msg, workdir, action=f"worker-autokill-commented:{wid}"))
            archive_action = kanban_archive_worker(board, wid, display_name, workdir)
            actions.append(archive_action)
            terminated_workers[wid] = {
                "agent_name": display_name,
                "terminated_at": t0.isoformat(),
                "reason": terminal_reason,
            }
            cfg.setdefault("completed_worker_ids", {})[wid] = terminal_reason
            workers_to_prune.append(wid)
            continue
        churn_reason = churn_probe_reason(status, wid, detail_json, repo, workdir) if detail_json else None
        if churn_reason:
            problems.append(f"worker {wid} churn probe tripped: {churn_reason}; dispatching PM Swarm Doctor")
            last_doctor_at = doctor_interventions.get(wid)
            doctor_allowed = True
            if last_doctor_at:
                try:
                    doctor_allowed = t0 - datetime.fromisoformat(str(last_doctor_at)) > timedelta(minutes=doctor_suppress_minutes)
                except Exception:
                    doctor_allowed = True
            if doctor_allowed:
                actions.append(doctor_worker(config_path, wid, workdir))
                doctor_interventions[wid] = t0.isoformat()
            else:
                actions.append(f"doctor-suppressed-duplicate:{wid}")
        if status == "unstable":
            reason = f": {summary}" if summary else ""
            problems.append(
                f"worker {wid} unstable{reason}; run pm_swarm_doctor.py for deep diagnosis or create a doctor card before assuming the PR itself is unstable"
            )
        elif status == "blocked":
            reason = f": {summary}" if summary else ""
            unblock_reason, nudge_actions = codex_retry_nudge(summary, repo, workdir, t0, codex_retry_after_minutes)
            actions.extend(nudge_actions)
            if unblock_reason:
                actions.append(kanban_unblock(board, wid, unblock_reason, workdir))
                problems.append(f"worker {wid} nudged from Codex rate-limit blocker")
                continue
            silent_retry_reason, silent_retry_actions = codex_silent_review_retry(
                summary,
                repo,
                workdir,
                t0,
                codex_retry_after_minutes,
                codex_retry_max_attempts,
                state,
            )
            actions.extend(silent_retry_actions)
            if silent_retry_reason:
                actions.append(kanban_comment(board, wid, silent_retry_reason, workdir, action=f"worker-nudged:{wid}"))
                if not approval_required_text(summary) and "current-head Codex response" in silent_retry_reason:
                    # A current-head Codex response means the worker's old
                    # "waiting for Codex" blocker is stale. Doctor-as-treatment
                    # should now actively inspect/resolve findings or merge, not
                    # let liveness repeat a passive nudge forever.
                    last_doctor_at = doctor_interventions.get(wid)
                    doctor_allowed = True
                    if last_doctor_at:
                        try:
                            doctor_allowed = t0 - datetime.fromisoformat(str(last_doctor_at)) > timedelta(minutes=doctor_suppress_minutes)
                        except Exception:
                            doctor_allowed = True
                    if doctor_allowed:
                        actions.append(doctor_worker(config_path, wid, workdir))
                        doctor_interventions[wid] = t0.isoformat()
                    else:
                        actions.append(f"doctor-suppressed-duplicate:{wid}")
            if approval_required_text(summary):
                actions.append(approval_cop_request(config_path, wid, workdir))
            problems.append(f"worker {wid} blocked{reason}")
        elif status == "done":
            gate = active_pr_gate(summary, repo, workdir)
            if gate:
                problems.append(
                    f"worker {wid} is marked done but still has an open PR gate: {gate}. "
                    "This shard is not terminal; assign a fresh follow-up worker before verifier/synthesis proceeds"
                )
            elif issues_remain:
                action = kanban_archive_worker(board, wid, display_name, workdir)
                actions.append(action)
                if action.startswith("worker-terminated:"):
                    worker_states[-1] += "[💀 terminated/archived; fresh one-issue worker required]"
                    terminated_workers[wid] = {
                        "agent_name": display_name,
                        "terminated_at": t0.isoformat(),
                        "reason": "done while project issues remain; prevent cross-issue context bloat",
                    }
                    workers_to_prune.append(wid)
                else:
                    problems.append(
                        f"worker {wid} is done while project issues remain but automatic termination failed. "
                        "PM must kill/terminate the old worker session and create a fresh one-issue worker card named <name>-<issueNumberTheyAreWorkingOn>-<status>."
                    )
        elif status in {"ready", "todo"} and issues_remain:
            active_doctor = active_doctor_for(wid, tasks)
            active_doctor_id = str(active_doctor.get("id")) if active_doctor else ""
            gate = active_pr_gate(summary, repo, workdir)
            if gate and "needs worker" not in gate:
                worker_states[-1] += f"[{gate}]"
            else:
                detail = f": {gate}" if gate else ""
                if active_doctor_id:
                    problems.append(
                        f"worker {wid} stranded in {status} while active doctor {active_doctor_id} is running; requesting doctor checkup instead of repeatedly nudging stale ready card{detail}"
                    )
                    worker_replacements[wid] = active_doctor_id
                    last_checkup_at = doctor_checkups.get(wid)
                    checkup_allowed = True
                    if last_checkup_at:
                        try:
                            checkup_allowed = t0 - datetime.fromisoformat(str(last_checkup_at)) > timedelta(minutes=doctor_checkup_minutes)
                        except Exception:
                            checkup_allowed = True
                    if checkup_allowed:
                        actions.append(doctor_checkup(board, active_doctor_id, wid, workdir))
                        doctor_checkups[wid] = t0.isoformat()
                    else:
                        actions.append(f"doctor-checkup-suppressed-duplicate:{wid}:{active_doctor_id}")
                else:
                    title = str(task.get("title") or "worker")
                    assignee = str(task.get("assignee") or "unassigned")
                    notes = latest_notes(detail_json, window=8) if detail_json else ""
                    freeze_note = ""
                    if any(marker in notes.lower() for marker in ("closeout-freeze", "intentionally not actionable", "superseded")):
                        freeze_note = "; card contains closeout-freeze/superseded note"
                    problems.append(f"worker {wid} ({title}; assignee={assignee}) not running while issues remain ({status}){detail}{freeze_note}")
                    if gate and "needs worker" in gate:
                        nudge = (
                            f"LIVENESS WAKE-UP: {gate}. Resume active work now. First inspect the PR's existing comments, reviews, and unresolved review threads. "
                            "Before coding, read tasks/resolve-issues-shared-lessons.md and tasks/lessons.md. "
                            "If comments/reviews contain actionable feedback, address it in code or explicitly reply/resolve it. "
                            "If there is no actionable feedback and this shard is in Codex-review phase, trigger or continue the @codex review loop and poll for the result. "
                            "Do not sit idle waiting for Codex; determine the next concrete coding/review/merge action and execute it. "
                            "After a PR is merged or a blocker/approval gate is reached, self-reflect, append concise reusable shortcomings/lessons to tasks/resolve-issues-shared-lessons.md, then complete/block this card and terminate this worker/session. "
                            "One agent handles one issue only; PM must kill the old worker and assign the next issue to a fresh worker/context named <name>-<issueNumberTheyAreWorkingOn>-<status> to prevent context bloat."
                        )
                        actions.append(kanban_comment(board, wid, nudge, workdir, action=f"worker-nudged:{wid}"))
            stranded_age = stranded_ready_age_seconds(kanban_diagnostics, wid)
            if stranded_age is not None and stranded_age >= doctor_ready_stale_minutes * 60:
                last_doctor_at = doctor_interventions.get(wid)
                doctor_allowed = True
                if last_doctor_at:
                    try:
                        doctor_allowed = t0 - datetime.fromisoformat(str(last_doctor_at)) > timedelta(minutes=doctor_suppress_minutes)
                    except Exception:
                        doctor_allowed = True
                if doctor_allowed:
                    problems.append(
                        f"worker {wid} stranded in {status} for {stranded_age // 60}m; dispatching PM Swarm Doctor"
                    )
                    actions.append(doctor_worker(config_path, wid, workdir))
                    doctor_interventions[wid] = t0.isoformat()
                elif active_doctor_id:
                    # A doctor exists but the original card is still stranded: make that doctor actively report back.
                    last_checkup_at = doctor_checkups.get(wid)
                    checkup_allowed = True
                    if last_checkup_at:
                        try:
                            checkup_allowed = t0 - datetime.fromisoformat(str(last_checkup_at)) > timedelta(minutes=doctor_checkup_minutes)
                        except Exception:
                            checkup_allowed = True
                    if checkup_allowed:
                        actions.append(doctor_checkup(board, active_doctor_id, wid, workdir))
                        doctor_checkups[wid] = t0.isoformat()
                    else:
                        actions.append(f"doctor-checkup-suppressed-duplicate:{wid}:{active_doctor_id}")
                else:
                    actions.append(f"doctor-suppressed-duplicate:{wid}")
        elif status == "running" and latest and latest < stale_cutoff:
            problems.append(f"worker {wid} stale: no event since {latest_s}")

    rate_limited = detect_rate_limit(tasks_text, extra_rate_limit_paths)
    if rate_limited:
        problems.append("OpenAI/Codex rate-limit evidence observed; temporary Ollama fallback should be used")

    if any("not running" in p for p in problems) or any(a.startswith("worker-unblocked:") for a in actions):
        actions.append(dispatch_once(board, workdir))

    signature = "; ".join(sorted(problems))
    last_sig = state.get("last_problem_signature")
    last_ping = state.get("last_pm_ping_at")
    can_ping = True
    if problems and last_sig == signature and last_ping:
        try:
            last_dt = datetime.fromisoformat(str(last_ping))
            can_ping = t0 - last_dt > timedelta(minutes=ping_suppress_minutes)
        except Exception:
            can_ping = True

    if problems and can_ping:
        msg = (
            f"LIVENESS intervention requested for {project} at {t0.isoformat(timespec='seconds')}: "
            f"{'; '.join(problems)}. Workers: {', '.join(worker_states) or 'none configured'}. "
            f"{intervention_text} One-agent-per-issue invariant: after an issue PR is resolved and merged, PM must kill/terminate that worker/session and create a fresh worker for the next issue; name agents/cards as <name>-<issueNumberTheyAreWorkingOn>-<status>; do not long-live agents across issues. Fallback policy: {rate_limit_policy}"
        )
        actions.append(kanban_comment(board, pm_id, msg, workdir))
        state["last_pm_ping_at"] = t0.isoformat()
        state["last_problem_signature"] = signature
    elif problems:
        actions.append("pm-ping-suppressed-duplicate")

    if workers_to_prune:
        prune_set = set(workers_to_prune)
        kept_workers = [wid for wid in workers if wid not in prune_set]
        if kept_workers != workers:
            cfg["worker_ids"] = kept_workers
            save_json(config_path, cfg)
            actions.append("worker-ids-pruned:" + ",".join(wid for wid in workers if wid in prune_set))
            workers = kept_workers

    if worker_replacements:
        replaced: list[str] = []
        next_workers: list[str] = []
        for wid in workers:
            replacement = worker_replacements.get(wid)
            if replacement and replacement not in next_workers:
                next_workers.append(replacement)
                replaced.append(f"{wid}->{replacement}")
            elif wid not in next_workers:
                next_workers.append(wid)
        if next_workers != workers:
            cfg["worker_ids"] = next_workers
            save_json(config_path, cfg)
            actions.append("worker-ids-replaced:" + ",".join(replaced))
            workers = next_workers

    issues_remain_after_scan = open_issue_count not in {"0", "n/a", "unknown"}
    if auto_refill_workers and target_active_workers > 0 and issues_remain_after_scan:
        active_statuses = {"todo", "ready", "running", "blocked", "unstable"}
        active_tracked = [wid for wid in workers if str((by_id.get(wid) or {}).get("status") or "") in active_statuses]
        if len(active_tracked) < target_active_workers:
            problems.append(
                f"worker capacity below target: {len(active_tracked)}/{target_active_workers} active tracked workers while issues remain; invoking PM auto-refill"
            )
            actions.append(worker_refill(workdir))
            try:
                cfg = load_json(config_path)
                workers = list(cfg.get("worker_ids") or workers)
            except Exception:
                pass

    state["last_run_at"] = t0.isoformat()
    state["last_output_problem"] = bool(problems)
    state["terminated_workers"] = terminated_workers
    state["doctor_interventions"] = doctor_interventions
    state["doctor_checkups"] = doctor_checkups
    save_state(state_path, state)

    status_line = "🚨 LIVENESS PROBLEM" if problems else "✅ LIVENESS OK"
    output_parts = [
        "💓💓💓 LIVENESS CHECK 💓💓💓",
        status_line,
        f"📊 Open issues: {open_issue_count}",
        "",
        "👷 Workers:",
        bullet_list(worker_states, "✅ none configured"),
    ]
    if problems:
        output_parts.extend(["", "⚠️ Problems:", emoji_bullet_list(problems, "none", problem_emoji)])
    if actions:
        output_parts.extend(["", "🛠️ Actions:", emoji_bullet_list(actions, "none", action_emoji)])
    if rate_limited:
        output_parts.extend(["", f"☁️ Fallback: {rate_limit_policy}"])
    output = "\n".join(output_parts)

    append_progress(progress_path, f"- {t0.isoformat(timespec='seconds')}: {output}")
    print(output[:3500])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
