#!/usr/bin/env python3
"""Manual PM-swarm worker doctor/debug tool.

Usage:
  pm_swarm_doctor.py /path/to/config.json --all --dry-run
  pm_swarm_doctor.py /path/to/config.json t_worker1 t_worker2 --pm-ping
  pm_swarm_doctor.py /path/to/config.json t_worker1 --doctor --discord

This is intentionally deterministic and low-token. It gives an operator or PM a
high-detail status/debug dump for specific workers, comments exact treatments on
the target worker, unblocks/dispatches when safe, and can create a narrow
doctor/takeover Kanban card to actively repair stalled, broken, blocked, or
unstable workers. It also maintains a lightweight DSM-style symptom/treatment
JSON database for repeated blockers, and reports findings, symptoms, remedies,
and ongoing issues to Discord via `hermes send`.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_EVENT_LIMIT = 12
DEFAULT_COMMENT_LIMIT = 5
DEFAULT_RUN_LIMIT = 5


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def run(cmd: list[str], cwd: Path, timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def compact(text: Any, limit: int = 240) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


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
    return f"{base}-{extract_issue_or_pr(task, summary)}-{slugify_agent_part(status, 'unknown')}"


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True))


def hermes_comment(board: str, task_id: str, body: str, cwd: Path, dry_run: bool) -> str:
    if dry_run:
        return f"dry-run: would comment {task_id}"
    cp = run(["hermes", "kanban", "--board", board, "comment", task_id, body[:1800]], cwd)
    if cp.returncode == 0:
        return f"commented:{task_id}"
    return f"comment-failed:{task_id}:{compact(cp.stderr or cp.stdout, 200)}"


def send_discord(target: str | None, message: str, cwd: Path, dry_run: bool) -> str:
    if not target:
        return "discord-skipped:no-target"
    if dry_run:
        return f"dry-run: would send Discord report to {target}"
    cp = run(["hermes", "send", "--to", target, "--subject", "PM Swarm Doctor", message[:1900]], cwd, timeout=60)
    if cp.returncode == 0:
        return f"discord-sent:{target}"
    return f"discord-failed:{target}:{compact(cp.stderr or cp.stdout, 240)}"


def kanban_show(board: str, task_id: str, cwd: Path) -> dict[str, Any] | None:
    cp = run(["hermes", "kanban", "--board", board, "show", task_id, "--json"], cwd)
    if cp.returncode != 0:
        return None
    try:
        data = json.loads(cp.stdout or "{}")
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def kanban_list(board: str, tenant: str, cwd: Path) -> list[dict[str, Any]]:
    cp = run(["hermes", "kanban", "--board", board, "list", "--tenant", tenant, "--json"], cwd)
    if cp.returncode != 0:
        return []
    try:
        data = json.loads(cp.stdout or "[]")
    except Exception:
        return []
    return [item for item in data if isinstance(item, dict)]


def latest_epoch(items: list[dict[str, Any]]) -> int | None:
    values = [int(item.get("created_at")) for item in items if isinstance(item.get("created_at"), int)]
    return max(values) if values else None


def fmt_epoch(value: int | None) -> str:
    if not value:
        return "unknown"
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return "unknown"


def extract_pr(summary: str, comments: list[dict[str, Any]], extra_text: str = "") -> str | None:
    haystack = summary + "\n" + extra_text + "\n" + "\n".join(str(c.get("body") or "") for c in comments[-5:])
    matches = re.findall(r"(?:PR|pull request)\s*#(\d+)|github\.com/[^/]+/[^/]+/pull/(\d+)", haystack, flags=re.I)
    nums = [a or b for a, b in matches if a or b]
    return nums[-1] if nums else None


def parse_github_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def unresolved_codex_threads(repo: str | None, pr: str | None, cwd: Path) -> list[dict[str, Any]]:
    if not repo or not pr or "/" not in repo:
        return []
    owner, name = repo.split("/", 1)
    query = """
query($owner:String!, $name:String!, $number:Int!) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$number) {
      reviewThreads(first: 100) {
        nodes {
          isResolved
          path
          line
          comments(first: 20) {
            nodes { author { login } body createdAt }
          }
        }
      }
    }
  }
}
"""
    cp = run(
        [
            "gh", "api", "graphql",
            "-f", f"owner={owner}",
            "-f", f"name={name}",
            "-F", f"number={int(pr)}",
            "-f", f"query={query}",
        ],
        cwd,
        timeout=60,
    )
    if cp.returncode != 0:
        return []
    try:
        raw = json.loads(cp.stdout or "{}")
        nodes = (((raw.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get("reviewThreads", {}).get("nodes") or []
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for node in nodes:
        if node.get("isResolved"):
            continue
        codex_comments = []
        for comment in (((node.get("comments") or {}).get("nodes")) or []):
            author = ((comment.get("author") or {}).get("login") or "").lower()
            if author == "chatgpt-codex-connector":
                codex_comments.append(comment)
        if not codex_comments:
            continue
        latest = codex_comments[-1]
        out.append({"path": node.get("path"), "line": node.get("line"), "created_at": latest.get("createdAt"), "body": compact(latest.get("body"), 220)})
    return out


def inspect_pr(repo: str | None, pr: str | None, cwd: Path) -> dict[str, Any] | None:
    if not repo or not pr:
        return None
    cp = run(
        [
            "gh",
            "pr",
            "view",
            pr,
            "--repo",
            repo,
            "--json",
            "number,state,url,headRefName,headRefOid,mergeStateStatus,isDraft,reviewDecision,statusCheckRollup,comments,reviews",
        ],
        cwd,
    )
    if cp.returncode != 0:
        return {"number": pr, "error": compact(cp.stderr or cp.stdout)}
    try:
        data = json.loads(cp.stdout or "{}")
    except Exception:
        return {"number": pr, "error": "failed to parse gh pr view JSON"}
    checks: list[str] = []
    for check in data.get("statusCheckRollup") or []:
        name = check.get("name") or check.get("workflowName") or "check"
        status = check.get("status") or ""
        conclusion = check.get("conclusion") or ""
        checks.append(f"{name}:{status or conclusion}")
    latest_codex_comment = None
    latest_usage_limit_at: datetime | None = None
    latest_codex_trigger_at: datetime | None = None
    usage_limit_seen = False
    for comment in data.get("comments") or []:
        author = ((comment.get("author") or {}).get("login") or "").lower()
        body = str(comment.get("body") or "")
        created = parse_github_time(comment.get("createdAt"))
        if body.strip().lower() == "@codex review" and created:
            latest_codex_trigger_at = max(latest_codex_trigger_at, created) if latest_codex_trigger_at else created
        if author == "chatgpt-codex-connector":
            latest_codex_comment = compact(body, 180)
            if "usage limit" in body.lower() or "rate limit" in body.lower():
                usage_limit_seen = True
                if created:
                    latest_usage_limit_at = max(latest_usage_limit_at, created) if latest_usage_limit_at else created
    current_head = str(data.get("headRefOid") or "")
    current_head_codex_review = False
    latest_current_head_codex_at: datetime | None = None
    for review in data.get("reviews") or []:
        author = ((review.get("author") or {}).get("login") or "").lower()
        commit = ((review.get("commit") or {}).get("oid") or "")
        submitted = parse_github_time(review.get("submittedAt"))
        if author == "chatgpt-codex-connector" and current_head and commit == current_head:
            current_head_codex_review = True
            if submitted:
                latest_current_head_codex_at = max(latest_current_head_codex_at, submitted) if latest_current_head_codex_at else submitted
    usage_limit_stale = bool(latest_usage_limit_at and latest_current_head_codex_at and latest_current_head_codex_at > latest_usage_limit_at)
    unresolved = unresolved_codex_threads(repo, str(data.get("number") or pr), cwd)
    return {
        "number": data.get("number") or pr,
        "url": data.get("url"),
        "state": data.get("state"),
        "merge_state": data.get("mergeStateStatus"),
        "review_decision": data.get("reviewDecision"),
        "head": compact(current_head, 12),
        "checks": checks[:8],
        "usage_limit_seen": usage_limit_seen,
        "usage_limit_stale": usage_limit_stale,
        "latest_usage_limit_at": iso_or_none(latest_usage_limit_at),
        "latest_codex_trigger_at": iso_or_none(latest_codex_trigger_at),
        "latest_current_head_codex_at": iso_or_none(latest_current_head_codex_at),
        "current_head_codex_review": current_head_codex_review,
        "unresolved_codex_threads": len(unresolved),
        "unresolved_codex_thread_samples": unresolved[:5],
        "latest_codex_comment": latest_codex_comment,
    }


def diagnose(task_id: str, data: dict[str, Any], repo: str | None, cwd: Path) -> dict[str, Any]:
    task = data.get("task") or {}
    comments = [c for c in data.get("comments") or [] if isinstance(c, dict)]
    events = [e for e in data.get("events") or [] if isinstance(e, dict)]
    runs = [r for r in data.get("runs") or [] if isinstance(r, dict)]
    status = str(task.get("status") or "unknown")
    summary = str(data.get("latest_summary") or "")
    event_text = "\n".join(json.dumps(e.get("payload"), sort_keys=True) for e in events[-10:] if e.get("payload") is not None)
    extra_text = "\n".join(str(value or "") for value in (task.get("title"), task.get("body"), task.get("result"), event_text))
    pr = extract_pr(summary, comments, extra_text=extra_text)
    pr_data = inspect_pr(repo, pr, cwd)

    symptoms: list[str] = []
    treatments: list[str] = []
    text = "\n".join([summary] + [str(c.get("body") or "") for c in comments[-3:]]).lower()
    event_kinds = [str(e.get("kind") or "") for e in events]
    run_statuses = [str(r.get("status") or r.get("outcome") or "") for r in runs]

    if status == "unstable":
        symptoms.append("unstable_status")
        treatments.append("Create/assign a doctor card to inspect latest run, comments, PR gate, and convert the card to running/blocked/done with evidence.")
    if status == "blocked":
        symptoms.append("blocked_status")
        treatments.append("Read latest summary/comment for exact blocker; unblock only after adding a concrete next action or human-decision requirement.")
    if status in {"ready", "todo"} and pr:
        symptoms.append("ready_or_todo_with_open_pr_gate")
        treatments.append("Dispatch or nudge a focused worker for the active PR gate instead of leaving the shard idle.")
    if "usage limit" in text or "rate limit" in text or (pr_data and pr_data.get("usage_limit_seen")):
        if pr_data and pr_data.get("usage_limit_stale"):
            symptoms.append("stale_codex_usage_limit")
            treatments.append("Ignore the old usage-limit comment because newer current-head Codex evidence exists; unblock/dispatch the worker to inspect findings and merge/fix normally.")
        elif pr_data and not pr_data.get("current_head_codex_review"):
            symptoms.append("codex_usage_retry_needed")
            treatments.append("Treat the usage-limit blocker as retryable/stale after the configured window: retrigger @codex review, unblock/dispatch, and require fresh current-head Codex evidence.")
        else:
            symptoms.append("codex_usage_or_rate_limit")
            treatments.append("Re-check unresolved threads and current-head Codex evidence before deciding whether to retrigger, unblock, or keep waiting.")
    if (
        "approval-required" in text
        or "approval required" in text
        or "approval layer" in text
        or "command blocked" in text
    ):
        symptoms.append("approval_required")
        treatments.append("Escalate the exact command/decision to the human; do not churn retries until approval is granted.")
    if "timed_out" in run_statuses or "timed_out" in event_kinds:
        symptoms.append("iteration_budget_exhausted")
        treatments.append("Split the remaining PR/issue work into a narrow follow-up card with current evidence and a fresh context.")
    if "reclaimed" in event_kinds or "reclaimed" in run_statuses:
        symptoms.append("stale_claim_reclaimed")
        treatments.append("Check whether respawn was guarded by active_pr; if so create/nudge a closeout worker for that PR, otherwise dispatch normally.")
    if "respawn_guarded" in event_kinds:
        symptoms.append("respawn_guarded")
        treatments.append("Do not repeatedly reclaim; inspect guard reason and create a specific closeout/doctor task if the guard is stale or wrong.")
    if pr_data and pr_data.get("current_head_codex_review") and status in {"ready", "todo", "blocked", "unstable"}:
        symptoms.append("current_head_codex_available_but_worker_idle")
        treatments.append("Wake worker to inspect Codex review/comments, fix or resolve findings, then merge if CI is clean.")
    if pr_data and int(pr_data.get("unresolved_codex_threads") or 0) > 0:
        symptoms.append("current_head_codex_findings")
        treatments.append("Actively address current unresolved Codex threads: patch code or reply/resolve non-actionable comments, push, then retrigger @codex review.")
    if not symptoms:
        symptoms.append("no_obvious_blocker")
        treatments.append("Ask PM for live context and compare Kanban status against active PR/CI/Codex state before intervening.")

    findings: list[str] = []
    ongoing_issues: list[str] = []
    if pr_data:
        checks = ", ".join(pr_data.get("checks") or []) or "unknown"
        findings.append(
            f"PR #{pr_data.get('number')} is {pr_data.get('state')} merge_state={pr_data.get('merge_state')} checks={checks} current_head_codex={pr_data.get('current_head_codex_review')}"
        )
        if pr_data.get("usage_limit_stale"):
            ongoing_issues.append("Older GitHub Codex usage-limit comment is stale because a newer current-head Codex review exists.")
        elif pr_data.get("usage_limit_seen"):
            ongoing_issues.append("GitHub Codex review usage/rate limit is visible and no current-head Codex review has superseded it yet.")
        if pr_data.get("current_head_codex_review") and status in {"ready", "todo", "blocked", "unstable"}:
            ongoing_issues.append("Current-head Codex evidence exists while worker card is not completing/merging; needs closeout action.")
        if int(pr_data.get("unresolved_codex_threads") or 0) > 0:
            samples = "; ".join(
                f"{s.get('path')}:{s.get('line')} {compact(s.get('body'), 120)}"
                for s in (pr_data.get("unresolved_codex_thread_samples") or [])[:3]
            )
            ongoing_issues.append(f"{pr_data.get('unresolved_codex_threads')} unresolved Codex thread(s) require repair. {samples}".strip())
    if "respawn_guarded" in event_kinds:
        ongoing_issues.append("Repeated respawn_guarded(active_pr) events indicate the PM is suppressing normal respawn while an active PR exists.")
    if "reclaimed" in event_kinds or "reclaimed" in run_statuses:
        findings.append("A stale worker claim was reclaimed; previous worker context likely stopped before closeout.")
    if "timed_out" in run_statuses or "timed_out" in event_kinds:
        findings.append("At least one run exhausted its iteration budget.")

    return {
        "task_id": task_id,
        "title": task.get("title"),
        "agent_name": agent_name(task, status, summary),
        "status": status,
        "assignee": task.get("assignee"),
        "latest_summary": summary,
        "latest_event_at": fmt_epoch(latest_epoch(events)),
        "latest_comment_at": fmt_epoch(latest_epoch(comments)),
        "symptoms": symptoms,
        "findings": findings,
        "ongoing_issues": ongoing_issues,
        "treatments": treatments,
        "pr": pr_data,
        "recent_runs": [
            {
                "id": r.get("id"),
                "status": r.get("status"),
                "outcome": r.get("outcome"),
                "error": compact(r.get("error"), 160),
                "summary": compact(r.get("summary"), 200),
                "started_at": fmt_epoch(r.get("started_at")),
                "ended_at": fmt_epoch(r.get("ended_at")),
            }
            for r in runs[-DEFAULT_RUN_LIMIT:]
        ],
        "recent_events": [
            {
                "kind": e.get("kind"),
                "at": fmt_epoch(e.get("created_at")),
                "payload": compact(json.dumps(e.get("payload"), sort_keys=True) if e.get("payload") is not None else "", 180),
            }
            for e in events[-DEFAULT_EVENT_LIMIT:]
        ],
        "recent_comments": [
            {"author": c.get("author"), "at": fmt_epoch(c.get("created_at")), "body": compact(c.get("body"), 260)}
            for c in comments[-DEFAULT_COMMENT_LIMIT:]
        ],
    }


def render_report(project: str, diagnostics: list[dict[str, Any]], actions: list[str]) -> str:
    lines = [f"PM SWARM DOCTOR REPORT: {project}", f"generated_at: {utcnow().isoformat(timespec='seconds')}", ""]
    for item in diagnostics:
        lines.extend(
            [
                f"Worker {item['task_id']} ({item.get('agent_name')}): {item.get('title')}",
                f"  status: {item.get('status')} assignee: {item.get('assignee')} latest_event: {item.get('latest_event_at')} latest_comment: {item.get('latest_comment_at')}",
                f"  summary: {compact(item.get('latest_summary'), 500)}",
                f"  symptoms: {', '.join(item.get('symptoms') or [])}",
                "  findings:",
            ]
        )
        lines.extend(f"    - {f}" for f in (item.get("findings") or ["No specific live finding beyond card state."]))
        lines.extend(
            [
                "  ongoing issues:",
            ]
        )
        lines.extend(f"    - {issue}" for issue in (item.get("ongoing_issues") or ["None detected."]))
        lines.extend(
            [
                "  treatments:",
            ]
        )
        lines.extend(f"    - {t}" for t in item.get("treatments") or [])
        if item.get("pr"):
            lines.append(f"  pr: {json.dumps(item['pr'], sort_keys=True)}")
        lines.append("  recent runs:")
        lines.extend(f"    - {json.dumps(r, sort_keys=True)}" for r in item.get("recent_runs") or [])
        lines.append("  recent events:")
        lines.extend(f"    - {json.dumps(e, sort_keys=True)}" for e in item.get("recent_events") or [])
        lines.append("  recent comments:")
        lines.extend(f"    - {json.dumps(c, sort_keys=True)}" for c in item.get("recent_comments") or [])
        lines.append("")
    if actions:
        lines.append("Actions:")
        lines.extend(f"- {a}" for a in actions)
    return "\n".join(lines).rstrip()


def update_dsm(path: Path, diagnostics: list[dict[str, Any]], dry_run: bool) -> str:
    if dry_run:
        return f"dry-run: would update DSM {path}"
    dsm = load_json(path, {"version": 1, "symptoms": {}, "cases": []})
    counts = Counter()
    for item in diagnostics:
        for symptom in item.get("symptoms") or []:
            counts[symptom] += 1
            entry = dsm.setdefault("symptoms", {}).setdefault(symptom, {"seen": 0, "treatments": {}})
            entry["seen"] = int(entry.get("seen") or 0) + 1
            for treatment in item.get("treatments") or []:
                treatments = entry.setdefault("treatments", {})
                treatments[treatment] = int(treatments.get(treatment) or 0) + 1
        dsm.setdefault("cases", []).append(
            {
                "at": utcnow().isoformat(timespec="seconds"),
                "task_id": item.get("task_id"),
                "status": item.get("status"),
                "symptoms": item.get("symptoms"),
                "findings": item.get("findings"),
                "ongoing_issues": item.get("ongoing_issues"),
                "treatments": item.get("treatments"),
                "summary": compact(item.get("latest_summary"), 500),
            }
        )
    dsm["cases"] = dsm.get("cases", [])[-200:]
    dsm["updated_at"] = utcnow().isoformat(timespec="seconds")
    save_json(path, dsm)
    return f"dsm-updated:{path}:{dict(counts)}"


def create_doctor_card(cfg: dict[str, Any], diagnostics: list[dict[str, Any]], report: str, dry_run: bool) -> str:
    workdir = Path(cfg["workdir"]).expanduser().resolve()
    board = cfg.get("board", "default")
    tenant = cfg["tenant"]
    pm_id = cfg["pm_id"]
    assignee = cfg.get("doctor_assignee") or cfg.get("pm_assignee") or "default"
    ids = ",".join(str(d["task_id"]) for d in diagnostics)
    title = f"Doctor stalled/unstable workers {ids}"
    body = (
        "You are the PM-swarm doctor/takeover agent. Do not stop at diagnosis. Your job is to actively repair/unstick the listed worker card(s) without broadening scope. "
        "Read the PM/root cards, target worker cards, live PR/CI/Codex state, worktree state, and DSM below. Apply the smallest safe treatment now: fix current Codex/CI findings in the relevant worktree, reply/resolve threads, push, retrigger @codex review, unblock+dispatch when safe, create a fresh one-issue follow-up only if context/iteration budget is exhausted, or escalate exact human approval. "
        "If another worker is actively fixing the same PR, verify live progress and post exact treatment instructions instead of duplicating code edits; otherwise take over the narrow closeout yourself. "
        "Enforce one agent per issue: after an issue PR is resolved and merged, PM must kill/terminate the old worker/session and assign any remaining issue to a fresh worker/context; do not long-live agents across issues. "
        "After treatment, update the DSM with symptom/treatment lessons and comment evidence on the PM card.\n\n"
        + report[:5000]
    )
    if dry_run:
        return f"dry-run: would create doctor card '{title}'"
    cp = run(
        [
            "hermes",
            "kanban",
            "--board",
            board,
            "create",
            title,
            "--body",
            body,
            "--assignee",
            assignee,
            "--parent",
            pm_id,
            "--tenant",
            tenant,
            "--priority",
            "95",
            "--skill",
            "kanban-orchestrator",
            "--skill",
            "github-pr-workflow",
            "--skill",
            "github-issues",
            "--skill",
            "codex-review-loop",
            "--skill",
            "resolve-issues",
            "--idempotency-key",
            f"doctor:{tenant}:{ids}:{int(utcnow().timestamp() // 1800)}",
            "--json",
        ],
        workdir,
        timeout=90,
    )
    if cp.returncode != 0:
        return "doctor-card-failed:" + compact(cp.stderr or cp.stdout, 300)
    try:
        task_id = (json.loads(cp.stdout or "{}") or {}).get("id")
    except Exception:
        task_id = None
    return f"doctor-card-created:{task_id or compact(cp.stdout, 120)}"


def approval_cop_request(config_path: Path, task_id: str, cwd: Path, dry_run: bool) -> str:
    cop = Path(__file__).with_name("pm_swarm_approval_cop.py")
    cmd = [sys.executable, str(cop), str(config_path), task_id, "--request"]
    if dry_run:
        cmd.append("--dry-run")
    cp = run(cmd, cwd, timeout=120)
    if cp.returncode == 0:
        lines = [line.strip() for line in (cp.stdout or "").splitlines() if line.strip()]
        if lines:
            return "approval-cop:" + ";".join(lines[:4])
        return f"approval-cop:no-new-request:{task_id}"
    return f"approval-cop-failed:{task_id}:" + compact(cp.stderr or cp.stdout, 240)


def kanban_unblock(board: str, task_id: str, reason: str, cwd: Path, dry_run: bool) -> str:
    if dry_run:
        return f"dry-run: would unblock {task_id}"
    cp = run(["hermes", "kanban", "--board", board, "unblock", task_id, "--reason", reason[:1800]], cwd, timeout=60)
    if cp.returncode == 0:
        return f"worker-unblocked:{task_id}"
    return f"worker-unblock-failed:{task_id}:" + compact(cp.stderr or cp.stdout, 220)


def dispatch_once(board: str, cwd: Path, dry_run: bool) -> str:
    if dry_run:
        return "dry-run: would dispatch"
    cp = run(["hermes", "kanban", "--board", board, "dispatch", "--max", "5", "--json"], cwd, timeout=120)
    if cp.returncode == 0:
        return "dispatch-run"
    return "dispatch-failed:" + compact(cp.stderr or cp.stdout, 220)


def gh_pr_comment(repo: str | None, pr: Any, body: str, cwd: Path, dry_run: bool) -> str:
    if not repo or not pr:
        return "codex-retrigger-skipped:no-pr"
    if dry_run:
        return f"dry-run: would comment @codex review on PR #{pr}"
    cp = run(["gh", "pr", "comment", str(pr), "--repo", repo, "--body", body], cwd, timeout=60)
    if cp.returncode == 0:
        return f"codex-retriggered:PR#{pr}"
    return f"codex-retrigger-failed:PR#{pr}:" + compact(cp.stderr or cp.stdout, 220)


def apply_treatments(board: str, diagnostics: list[dict[str, Any]], cwd: Path, dry_run: bool, repo: str | None = None) -> list[str]:
    actions: list[str] = []
    should_dispatch = False
    for item in diagnostics:
        tid = str(item.get("task_id"))
        symptoms = set(item.get("symptoms") or [])
        status = str(item.get("status") or "")
        pr = item.get("pr") or {}
        samples = pr.get("unresolved_codex_thread_samples") or []
        sample_text = "; ".join(
            f"{s.get('path')}:{s.get('line')} {compact(s.get('body'), 120)}" for s in samples[:3]
        )
        treatment = (
            f"DOCTOR TREATMENT {utcnow().isoformat(timespec='seconds')}: "
            f"symptoms={','.join(sorted(symptoms)) or 'none'}. "
        )
        if "current_head_codex_findings" in symptoms:
            treatment += (
                f"PR #{pr.get('number')} has {pr.get('unresolved_codex_threads')} unresolved current Codex thread(s). "
                f"Fix/reply/resolve them now, push, retrigger @codex review, then merge only after CI + current-head Codex clean. "
                f"Samples: {sample_text}"
            )
        elif "stale_codex_usage_limit" in symptoms:
            treatment += (
                f"PR #{pr.get('number')} has newer current-head Codex evidence after an older usage-limit comment. "
                "Treat the rate-limit blocker as stale: inspect findings/threads, fix or resolve, then merge if CI and Codex are clean."
            )
        elif "codex_usage_retry_needed" in symptoms:
            treatment += (
                f"PR #{pr.get('number')} still lacks current-head Codex evidence after a usage-limit response. "
                "Doctor is retriggering @codex review now and unblocking/dispatching the worker to continue polling/fixing instead of waiting on stale state."
            )
            actions.append(gh_pr_comment(repo, pr.get("number"), "@codex review", cwd, dry_run))
        elif "current_head_codex_available_but_worker_idle" in symptoms:
            treatment += (
                f"PR #{pr.get('number')} has current-head Codex evidence while card is idle. Inspect comments/reviews, address findings, and close out/merge if gates are clean."
            )
        elif status in {"ready", "todo", "unstable"}:
            treatment += "Card is not actively running; dispatching focused worker/doctor to continue narrow PR/issue closeout."
        elif status == "blocked" and "approval_required" not in symptoms:
            treatment += "Blocked state has no human-approval symptom; unblocking for focused doctor/worker continuation with exact PR/Codex/CI checks."
        else:
            treatment += "No deterministic code change is safe from the doctor script; posted exact live treatment so active worker/doctor acts on it."
        actions.append(hermes_comment(board, tid, treatment, cwd, dry_run))
        if status == "blocked" and "approval_required" not in symptoms:
            actions.append(kanban_unblock(board, tid, "Doctor found no human approval requirement; resume focused PR/Codex/CI closeout with posted treatment.", cwd, dry_run))
            should_dispatch = True
        if status in {"ready", "todo", "unstable"}:
            should_dispatch = True
    if should_dispatch:
        actions.append(dispatch_once(board, cwd, dry_run))
    return actions


def render_discord_report(project: str, diagnostics: list[dict[str, Any]], actions: list[str]) -> str:
    lines = [f"🩺 PM Swarm Doctor: {project}", f"generated_at: {utcnow().isoformat(timespec='seconds')}"]
    for item in diagnostics:
        lines.append(f"\nWorker `{item.get('agent_name') or item.get('task_id')}` (`{item.get('task_id')}`) — {compact(item.get('title'), 120)}")
        lines.append(f"status: `{item.get('status')}` latest_event: {item.get('latest_event_at')}")
        lines.append(f"symptoms: {', '.join(item.get('symptoms') or ['none'])}")
        lines.append("findings:")
        for finding in (item.get("findings") or ["No specific live finding beyond card state."])[:4]:
            lines.append(f"- {compact(finding, 220)}")
        lines.append("remedies:")
        for treatment in (item.get("treatments") or [])[:5]:
            lines.append(f"- {compact(treatment, 220)}")
        lines.append("ongoing issues:")
        for issue in (item.get("ongoing_issues") or ["None detected."])[:4]:
            lines.append(f"- {compact(issue, 220)}")
        if item.get("pr"):
            pr = item["pr"]
            lines.append(f"PR: {pr.get('url')} merge_state={pr.get('merge_state')} current_head_codex={pr.get('current_head_codex_review')}")
    if actions:
        lines.append("\nactions: " + "; ".join(compact(a, 180) for a in actions))
    return "\n".join(lines)[:1900]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deep-debug and doctor PM-swarm worker cards.")
    parser.add_argument("config", help="PM-swarm liveness JSON config")
    parser.add_argument("workers", nargs="*", help="Worker task ids to inspect; defaults to problematic workers unless --all is set")
    parser.add_argument("--all", action="store_true", help="Inspect every configured worker id")
    parser.add_argument("--pm-ping", action="store_true", help="Comment the PM card with the doctor report")
    parser.add_argument("--doctor", action="store_true", help="Create/idempotently recover a doctor Kanban card for the selected workers")
    parser.add_argument("--discord", action="store_true", help="Send findings/symptoms/remedies/ongoing issues to the configured Discord target")
    parser.add_argument("--no-discord", action="store_true", help="Suppress configured default Discord reporting")
    parser.add_argument("--dry-run", action="store_true", help="Do not write comments, DSM, or doctor cards")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of human text")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).expanduser().resolve()
    cfg = load_json(config_path, {})
    if not cfg:
        print(f"DOCTOR PROBLEM: failed to load config {config_path}")
        return 2

    project = cfg.get("project", config_path.stem)
    workdir = Path(cfg["workdir"]).expanduser().resolve()
    board = cfg.get("board", "default")
    tenant = cfg["tenant"]
    pm_id = cfg["pm_id"]
    configured_workers = [str(w) for w in (cfg.get("worker_ids") or [])]
    dsm_path = Path(cfg.get("doctor_dsm_path") or f"~/.hermes/state/{project}_doctor_dsm.json").expanduser().resolve()
    discord_target = cfg.get("doctor_discord_target") or cfg.get("discord_target")
    discord_default = bool(cfg.get("doctor_report_discord", False))

    selected = [str(w) for w in args.workers]
    if args.all:
        selected = configured_workers
    if not selected:
        # Default to currently non-terminal/non-running-ish configured workers.
        tasks = {str(t.get("id")): t for t in kanban_list(board, tenant, workdir)}
        selected = [wid for wid in configured_workers if str((tasks.get(wid) or {}).get("status") or "") in {"blocked", "ready", "todo", "unstable"}]
    if not selected:
        print("DOCTOR OK: no selected/problematic workers")
        return 0

    diagnostics: list[dict[str, Any]] = []
    actions: list[str] = []
    for wid in selected:
        shown = kanban_show(board, wid, workdir)
        if not shown:
            diagnostics.append({"task_id": wid, "status": "missing", "symptoms": ["kanban_show_failed"], "treatments": ["Verify worker id and board/tenant config."]})
            continue
        diagnostics.append(diagnose(wid, shown, cfg.get("repo"), workdir))

    report = render_report(project, diagnostics, actions=[])
    actions.append(update_dsm(dsm_path, diagnostics, args.dry_run))
    actions.extend(apply_treatments(board, diagnostics, workdir, args.dry_run, cfg.get("repo")))
    for item in diagnostics:
        if "approval_required" in (item.get("symptoms") or []):
            actions.append(approval_cop_request(config_path, str(item.get("task_id")), workdir, args.dry_run))
    if args.pm_ping:
        actions.append(hermes_comment(board, pm_id, "PM DOCTOR DEBUG PING\n\n" + report[:1700], workdir, args.dry_run))
    if args.doctor:
        actions.append(create_doctor_card(cfg, diagnostics, report, args.dry_run))
    if (args.discord or discord_default) and not args.no_discord:
        actions.append(send_discord(discord_target, render_discord_report(project, diagnostics, actions), workdir, args.dry_run))

    if args.json:
        print(json.dumps({"project": project, "workers": selected, "diagnostics": diagnostics, "actions": actions}, indent=2, sort_keys=True))
    else:
        print(render_report(project, diagnostics, actions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
