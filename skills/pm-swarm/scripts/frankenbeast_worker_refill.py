#!/usr/bin/env python3
"""Keep the frankenbeast issue swarm filled with fresh one-issue workers.

No-agent watchdog: prints only when it creates/updates/dispatches work.
"""
from __future__ import annotations

import fcntl
import json
import pathlib
import re
import subprocess
from typing import Any

REPO = "djm204/frankenbeast"
WORKDIR = pathlib.Path("/home/pfkagent/dev/frankenbeast")
CONFIG = pathlib.Path("/home/pfkagent/.hermes/scripts/frankenbeast_issue_swarm_liveness.json")
TENANT = "frankenbeast-issues"
BOARD = "default"
PM_ID = "t_bdaa2232"
TARGET = 5
ACTIVE_STATUSES = {"todo", "ready", "running", "blocked", "unstable"}


def run(cmd: list[str], timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(WORKDIR), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)


def issue_priority(issue: dict[str, Any]) -> tuple[int, int]:
    labels = {label.get("name", "") for label in issue.get("labels", [])}
    if "P0" in labels:
        p = 0
    elif "P1" in labels:
        p = 1
    elif "P2" in labels:
        p = 2
    elif "P3" in labels:
        p = 3
    else:
        p = 4
    # Prefer newer/higher issue numbers within priority. Security stays naturally represented by labels in body.
    return (p, -int(issue["number"]))


def issue_num_from_managed_title(title: str) -> int | None:
    m = re.match(r"resolve issue #(\d+):", title or "", flags=re.I)
    return int(m.group(1)) if m else None


def create_worker(issue: dict[str, Any]) -> str | None:
    n = int(issue["number"])
    title = issue["title"]
    labels = ", ".join(label.get("name", "") for label in issue.get("labels", []))
    short = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48]
    branch = f"resolve/issue-{n}-{short}"[:90]
    body = f"""Resolve GitHub issue #{n} only: {title}

Issue: {issue['url']}
Labels: {labels}

Operating invariants:
- One issue = one branch/worktree = one PR. Do not work on other issues.
- Before coding, read tasks/resolve-issues-shared-lessons.md and tasks/lessons.md if present.
- Use git identity David Mendez <me@davidmendez.dev> for commits.
- Create/use branch `{branch}` and an isolated worktree if needed.
- Implement the smallest correct fix for issue #{n}; add/update targeted tests/docs.
- Run relevant tests plus package-level typecheck/build/lint as appropriate.
- Open a PR that closes only issue #{n}.
- Trigger the real GitHub Codex gate with `@codex review`; address/reply/resolve findings and retrigger until current-head clean.
- Merge only when CI is green and current-head Codex is clean.
- After merge or a real blocker, self-reflect, append durable lessons to tasks/resolve-issues-shared-lessons.md if useful, complete/block this card, and stop. Do not take a second issue.
"""
    cmd = [
        "hermes", "kanban", "--board", BOARD, "create", f"resolve issue #{n}: {title[:90]}",
        "--tenant", TENANT, "--parent", PM_ID, "--priority", "95", "--assignee", "default",
        "--idempotency-key", f"frankenbeast-fresh-issue-{n}-5wide",
        "--skill", "resolve-issues", "--skill", "github-pr-workflow", "--skill", "github-issues",
        "--skill", "codex-review-loop", "--skill", "issue-pr-swarm-orchestration",
        "--max-runtime", "3h", "--body", body, "--json",
    ]
    cp = run(cmd, timeout=120)
    if cp.returncode != 0:
        print(f"refill-create-failed issue #{n}: {cp.stdout[:500]}")
        return None
    try:
        data = json.loads(cp.stdout)
        return data.get("id") or (data.get("task") or {}).get("id")
    except Exception:
        m = re.search(r"t_[0-9a-f]+", cp.stdout)
        return m.group(0) if m else None


def main() -> int:
    lock_path = pathlib.Path("/home/pfkagent/.hermes/state/frankenbeast_worker_refill.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        return main_locked()


def main_locked() -> int:
    cfg = json.loads(CONFIG.read_text())
    target = int(cfg.get("target_active_workers") or TARGET)
    if not cfg.get("auto_refill_workers", True):
        return 0

    tasks_cp = run(["hermes", "kanban", "--board", BOARD, "list", "--tenant", TENANT, "--json"], timeout=60)
    if tasks_cp.returncode != 0:
        print("refill-kanban-list-failed:" + tasks_cp.stdout[:500])
        return 1
    tasks = json.loads(tasks_cp.stdout or "[]")
    by_id = {str(t.get("id")): t for t in tasks if isinstance(t, dict)}

    deferred_ids = {str(x) for x in (cfg.get("deferred_worker_ids") or {}).keys()}
    completed_ids = {str(x) for x in (cfg.get("completed_worker_ids") or {}).keys()}
    excluded_ids = deferred_ids | completed_ids
    deferred_issue_nums = {
        int(m.group(1))
        for value in (cfg.get("deferred_worker_ids") or {}).values()
        for m in [re.search(r"issue\s+#(\d+)", str(value), flags=re.I)]
        if m
    }
    worker_ids = [str(x) for x in (cfg.get("worker_ids") or []) if str(x) not in excluded_ids]
    active_ids = []
    active_managed_ids = []
    managed_issue_nums = set()
    for t in tasks:
        status = str(t.get("status") or "")
        tid = str(t.get("id") or "")
        num = issue_num_from_managed_title(str(t.get("title") or ""))
        if num is not None and tid in deferred_ids:
            deferred_issue_nums.add(num)
        if num is not None and status in ACTIVE_STATUSES and tid not in excluded_ids:
            managed_issue_nums.add(num)
            active_managed_ids.append(tid)
        if tid in worker_ids and status in ACTIVE_STATUSES:
            active_ids.append(tid)

    # Capacity is based on every active one-issue refill card, not only the
    # currently tracked liveness ids. This prevents the liveness-triggered refill
    # and the standalone refill cron from both creating workers during the same
    # short window, then treating one of them as invisible on future ticks.
    tracked_plus_untracked = active_ids + [tid for tid in active_managed_ids if tid not in active_ids]
    visible_active_ids = tracked_plus_untracked[:target]
    needed = max(0, target - len(tracked_plus_untracked))
    created: list[str] = []
    if needed:
        issues_cp = run(["gh", "issue", "list", "--repo", REPO, "--state", "open", "--limit", "100", "--json", "number,title,labels,url"], timeout=60)
        if issues_cp.returncode != 0:
            print("refill-issue-list-failed:" + issues_cp.stdout[:500])
            return 1
        issues = sorted(json.loads(issues_cp.stdout or "[]"), key=issue_priority)
        for issue in issues:
            n = int(issue["number"])
            if n in managed_issue_nums or n in deferred_issue_nums:
                continue
            tid = create_worker(issue)
            if tid:
                active_ids.append(tid)
                created.append(f"#{n}:{tid}")
                managed_issue_nums.add(n)
            if len(active_ids) >= target:
                break

    tracked_plus_untracked = active_ids + [tid for tid in active_managed_ids if tid not in active_ids]
    visible_active_ids = tracked_plus_untracked[:target]

    if visible_active_ids != worker_ids:
        cfg["worker_ids"] = visible_active_ids
        cfg["target_active_workers"] = target
        cfg["auto_refill_workers"] = True
        CONFIG.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n")

    dispatched = ""
    if created:
        d = run(["hermes", "kanban", "--board", BOARD, "dispatch", "--max", str(target), "--json"], timeout=180)
        dispatched = d.stdout.strip()[:1000]
        comment = (
            f"PM AUTO-REFILL: active tracked workers were below target {target}; created "
            + ", ".join(created)
            + f". Current liveness worker_ids: {', '.join(visible_active_ids)}. Keep five one-issue workers moving while open issues remain."
        )
        run(["hermes", "kanban", "--board", BOARD, "comment", PM_ID, comment], timeout=60)

    if created or visible_active_ids != worker_ids:
        print("refill-active-workers=" + ",".join(visible_active_ids))
        if len(tracked_plus_untracked) > target:
            overflow = tracked_plus_untracked[target:]
            print("refill-over-target-active-managed=" + ",".join(overflow))
        if created:
            print("refill-created=" + ",".join(created))
            print("refill-dispatch=" + dispatched)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
