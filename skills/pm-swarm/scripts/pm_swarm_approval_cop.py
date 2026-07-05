#!/usr/bin/env python3
"""PM Swarm approval traffic cop.

Deterministic bridge for approval-gated Kanban workers:
- finds blocked approval-required workers
- sends a concise Discord approval request with a stable token
- watches Hermes session DB for user replies like `APPROVE <token>` / `DENY <token>`
- on approval, executes only the exact pre-recorded command in the exact workdir,
  then comments/unblocks/dispatches the worker so it can continue.

This intentionally avoids asking workers to route around approval denials. The
human approves the side effect in Discord; the traffic cop performs that exact
side effect and hands control back to the worker.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_STATE = Path.home() / ".hermes/state/pm_swarm_approval_cop.json"
DEFAULT_DB = Path.home() / ".hermes/state.db"
APPROVAL_KEYWORDS = (
    "approval required",
    "approval-required",
    "approval layer",
    "push approval required",
    "blocked by approval",
    "approval-blocked",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run(cmd: list[str], cwd: Path, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)


def compact(text: Any, limit: int = 700) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def task_show(board: str, task_id: str, cwd: Path) -> dict[str, Any] | None:
    cp = run(["hermes", "kanban", "--board", board, "show", task_id, "--json"], cwd)
    if cp.returncode != 0:
        return None
    try:
        return json.loads(cp.stdout or "{}")
    except Exception:
        return None


def kanban_comment(board: str, task_id: str, body: str, cwd: Path) -> str:
    cp = run(["hermes", "kanban", "--board", board, "comment", task_id, body[:1800]], cwd)
    if cp.returncode == 0:
        return f"commented:{task_id}"
    return f"comment-failed:{task_id}:{compact(cp.stderr or cp.stdout, 200)}"


def kanban_unblock(board: str, task_id: str, reason: str, cwd: Path) -> str:
    cp = run(["hermes", "kanban", "--board", board, "unblock", task_id, "--reason", reason[:700]], cwd)
    if cp.returncode == 0:
        return f"unblocked:{task_id}"
    return f"unblock-failed:{task_id}:{compact(cp.stderr or cp.stdout, 200)}"


def dispatch(board: str, cwd: Path) -> str:
    cp = run(["hermes", "kanban", "--board", board, "dispatch", "--max", "4", "--json"], cwd, timeout=180)
    if cp.returncode == 0:
        return "dispatch-run"
    return f"dispatch-failed:{compact(cp.stderr or cp.stdout, 200)}"


def _load_discord_token() -> str:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if token:
        return token
    env_path = Path.home() / ".hermes" / ".env"
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            if key.strip() == "DISCORD_BOT_TOKEN":
                return val.strip().strip('"').strip("'")
    except Exception:
        return ""
    return ""


def _parse_discord_target(target: str) -> tuple[str | None, str | None]:
    # Accept discord:<channel> or discord:<channel>:<thread>.
    parts = target.split(":")
    if len(parts) >= 2 and parts[0] == "discord":
        channel_id = parts[1] or None
        thread_id = parts[2] if len(parts) >= 3 and parts[2] else None
        return channel_id, thread_id
    return None, None


def _mentioned_user_ids(message: str) -> list[str]:
    ids: list[str] = []
    for uid in re.findall(r"<@!?(\d+)>", message):
        if uid not in ids:
            ids.append(uid)
    return ids


def send_discord(target: str | None, message: str, cwd: Path, dry_run: bool = False) -> str:
    if not target:
        return "discord-skipped:no-target"
    if dry_run:
        return f"dry-run: would send {target}"

    # Prefer direct Discord REST for approval requests so numeric <@USER_ID>
    # mentions are explicitly allowed and actually notify the human. `hermes
    # send` remains the fallback for non-Discord targets or token/config issues.
    channel_id, thread_id = _parse_discord_target(target)
    token = _load_discord_token()
    if channel_id and token:
        send_to = thread_id or channel_id
        payload = {
            "content": message[:1900],
            "allowed_mentions": {
                "parse": [],
                "users": _mentioned_user_ids(message),
                "roles": [],
                "replied_user": False,
            },
        }
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{send_to}/messages",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": "HermesApprovalCop/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if 200 <= resp.status < 300:
                    return f"discord-sent-with-mention:{target}"
                body = resp.read().decode("utf-8", "replace")
                return f"discord-failed:{resp.status}:{compact(body, 240)}"
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            # Fall through to hermes send in case this target needs gateway routing.
            fallback_reason = f"direct-http-{e.code}:{compact(body, 120)}"
        except Exception as e:
            fallback_reason = f"direct-http-error:{compact(str(e), 120)}"
    else:
        fallback_reason = "direct-http-skipped:no-discord-target-or-token"

    cp = run(["hermes", "send", "--to", target, "--subject", "Approval Traffic Cop", message[:1900]], cwd, timeout=60)
    if cp.returncode == 0:
        return f"discord-sent-fallback:{target}:{fallback_reason}"
    return f"discord-failed:{fallback_reason};{compact(cp.stderr or cp.stdout, 240)}"


def extract_command_and_workdir(data: dict[str, Any], fallback_cwd: Path) -> tuple[str | None, Path]:
    task = data.get("task") or {}
    comments = [c for c in data.get("comments") or [] if isinstance(c, dict)]
    text = "\n".join([str(task.get("body") or ""), str(data.get("latest_summary") or "")] + [str(c.get("body") or "") for c in comments])
    workdir = Path(str(task.get("workspace_path") or fallback_cwd)).expanduser()
    # Prefer explicit worktree path in handoff comments.
    m_path = re.search(r"(/home/pfkagent/dev/(?:resolve-wt/issue-\d+|frankenbeast)(?:/[\w./-]+)?)", text)
    if m_path:
        p = Path(m_path.group(1))
        # Trim file paths back to the worktree root when needed.
        parts = p.parts
        if "resolve-wt" in parts:
            idx = parts.index("resolve-wt")
            if len(parts) > idx + 1:
                p = Path(*parts[: idx + 2])
        elif p.name != "frankenbeast" and "frankenbeast" in parts:
            idx = parts.index("frankenbeast")
            p = Path(*parts[: idx + 1])
        workdir = p
    else:
        # Kanban workspace_path is not necessarily the git worktree. If the
        # worker text names an issue and the standard resolve worktree exists,
        # use it so approval-cop can extract/approve the exact blocked command.
        m_issue = re.search(r"(?:issue|resolve issue)\s*#(\d+)", text, flags=re.I)
        if m_issue:
            candidate = Path(f"/home/pfkagent/dev/resolve-wt/issue-{m_issue.group(1)}")
            if candidate.exists():
                workdir = candidate
    # Exact blocked command blocks are usually backtick quoted after a label.
    labels = [
        r"Exact blocked command:\s*`([^`]+)`",
        r"blocked command:\s*`([^`]+)`",
        r"blocked command(?:\s+(?:requiring|needing)\s+approval)?:\s*```(?:bash)?\s*([^`]+?)\s*```",
        r"approval-blocked command:\s*```(?:bash)?\s*([^`]+?)\s*```",
        r"approval-blocked command:\s*`([^`]+)`",
        r"required remote update commands:\s*-\s*`([^`]+)`",
        r"(`git push --force-with-lease(?: [^`]+)?`)",
        r"(`git add [^`]+git push --force-with-lease[^`]+`)",
    ]
    command_candidates: list[tuple[int, str]] = []
    for pat in labels:
        for m in re.finditer(pat, text, flags=re.I | re.S):
            cmd = m.group(1).strip()
            if cmd.startswith("`") and cmd.endswith("`"):
                cmd = cmd[1:-1]
            cmd = " ".join(cmd.split())
            command_candidates.append((m.start(), cmd))
    for _pos, cmd in sorted(command_candidates, key=lambda item: item[0], reverse=True):
        if is_allowed_command(cmd, workdir):
            return cmd, workdir
    # Workers sometimes cannot include a long GitHub API reply/resolve script in
    # the blocked summary. If they recorded PR + exact Codex comment ids, create
    # a deterministic helper command that performs only those side effects after
    # approval.
    pr_match = re.search(r"PR\s*#(\d+)", text, flags=re.I)
    ids = re.findall(r"`(\d{8,})`", text)
    if pr_match and ids and ("resolve codex" in text.lower() or "codex review comments" in text.lower() or "inline comments" in text.lower()):
        helper = Path(__file__).with_name("frankenbeast_codex_resolve_comments.py")
        unique_ids = []
        for cid in ids:
            if cid not in unique_ids:
                unique_ids.append(cid)
        cmd = (
            f"python3 {helper} --repo djm204/frankenbeast --pr {pr_match.group(1)} "
            f"--comment-ids {' '.join(unique_ids)} --retrigger"
        )
        workdir = fallback_cwd
        if is_allowed_command(cmd, workdir):
            return cmd, workdir
    return None, workdir


def is_allowed_command(cmd: str, workdir: Path) -> bool:
    wd = str(workdir.resolve()) if workdir.exists() else str(workdir)
    if not (wd.startswith("/home/pfkagent/dev/resolve-wt/") or wd == "/home/pfkagent/dev/frankenbeast"):
        return False
    # Allow only pre-recorded publish/amend commands, plus the local Codex
    # review-comment helper that takes explicit comment ids from the worker's
    # blocked handoff and is still gated by approval-cop.
    allowed_patterns = [
        r"^git push --force-with-lease(?: origin (HEAD:)?[-/\w.]+)?$",
        r"^git status --short --branch && git push --force-with-lease(?: origin (HEAD:)?[-/\w.]+)?$",
        r"^cd /home/pfkagent/dev/resolve-wt/issue-\d+(?:-clean)? && PR_BRANCH=[-/\w.]+ && git fetch origin \"\$PR_BRANCH\" && OLD=\$\(git rev-parse FETCH_HEAD\) && echo \"old_remote=\$OLD new_head=\$\(git rev-parse HEAD\)\" && git push origin HEAD:\$PR_BRANCH --force-with-lease=refs/heads/\$PR_BRANCH:\$OLD$",
        r"^git add [-/\w. ]+ && git diff --cached --stat && git commit --amend --no-edit && git push --force-with-lease origin [-/\w.]+$",
        r"^python3 /home/pfkagent/\.hermes/scripts/frankenbeast_codex_resolve_comments\.py --repo djm204/frankenbeast --pr \d+ --comment-ids( \d{8,})+ --retrigger$",
    ]
    return any(re.match(p, cmd) for p in allowed_patterns)


def request_id(task_id: str, cmd: str, workdir: Path) -> str:
    return hashlib.sha256(f"{task_id}\n{workdir}\n{cmd}".encode()).hexdigest()[:8]


def policy_id(cmd: str, workdir: Path) -> str:
    return hashlib.sha256(f"allow-always-v1\n{workdir}\n{cmd}".encode()).hexdigest()[:12]


def approval_text(req: dict[str, Any]) -> str:
    mention = str(req.get("mention") or "").strip()
    prefix = f"{mention}\n" if mention else ""
    rid = req["id"]
    return (
        prefix
        + f"🚦 **ACTION REQUIRED: approval needed** for `{req['task_id']}` — {req.get('title','worker')}\n"
        f"Token: `{rid}`\n"
        f"Workdir: `{req['workdir']}`\n"
        f"Command:\n```bash\n{req['command']}\n```\n"
        f"**Action buttons / copy-paste controls:**\n"
        f"🟢 `APPROVE {rid}` — allow once\n"
        f"🔵 `ALLOW ALWAYS {rid}` — always allow this exact command+workdir\n"
        f"🔴 `DENY {rid}` — deny and leave worker blocked\n"
        f"Reply with one action above."
    )


def request_for_task(config: dict[str, Any], state: dict[str, Any], task_id: str, dry_run: bool = False) -> list[str]:
    board = config.get("board", "default")
    cwd = Path(config.get("workdir") or ".").expanduser()
    target = config.get("approval_discord_target") or config.get("doctor_discord_target")
    data = task_show(board, task_id, cwd)
    if not data:
        return [f"approval-cop-show-failed:{task_id}"]
    task = data.get("task") or {}
    summary = str(data.get("latest_summary") or "")
    comments = [str(c.get("body") or "") for c in (data.get("comments") or []) if isinstance(c, dict)]
    haystack = "\n".join([summary] + comments).lower()
    if not any(k in haystack for k in APPROVAL_KEYWORDS):
        return [f"approval-cop-skipped:not-approval-blocked:{task_id}"]
    cmd, workdir = extract_command_and_workdir(data, cwd)
    if not cmd:
        return [f"approval-cop-skipped:no-allowed-command:{task_id}"]
    rid = request_id(task_id, cmd, workdir)
    pkey = policy_id(cmd, workdir)
    policies = state.setdefault("allow_always", {})
    reqs = state.setdefault("requests", {})
    existing = reqs.get(rid)
    if existing and existing.get("status") in {"requested", "approved", "executed", "auto_approved"}:
        return [f"approval-cop-existing:{task_id}:{rid}:{existing.get('status')}"]
    req = {
        "id": rid,
        "policy_key": pkey,
        "task_id": task_id,
        "title": task.get("title"),
        "status": "requested",
        "command": cmd,
        "workdir": str(workdir),
        "created_at": utcnow(),
        "created_ts": time.time(),
        "discord_target": target,
        "mention": config.get("approval_mention") or config.get("approval_mentions") or "",
    }
    reqs[rid] = req
    if pkey in policies:
        req["status"] = "auto_approved"
        req["auto_approved_at"] = utcnow()
        req["auto_approved_by_policy"] = pkey
        kanban_comment(board, task_id, f"🚦 Approval Traffic Cop auto-approved token `{rid}` by allow-always policy `{pkey}` for exact command/workdir.", cwd)
        return [f"approval-auto-approved:{task_id}:{rid}:{pkey}"] + execute_request(config, req, dry_run=dry_run)
    action = send_discord(target, approval_text(req), cwd, dry_run=dry_run)
    req["last_sent_at"] = utcnow()
    kanban_comment(board, task_id, f"🚦 Approval Traffic Cop requested Discord approval token `{rid}` for exact command: `{cmd}`", cwd)
    return [f"approval-requested:{task_id}:{rid}", action]


def scan(config: dict[str, Any], state: dict[str, Any], dry_run: bool = False) -> list[str]:
    actions: list[str] = []
    for task_id in config.get("worker_ids") or []:
        actions.extend(request_for_task(config, state, task_id, dry_run=dry_run))
    return actions


def recent_user_messages(db_path: Path, since_ts: float) -> list[tuple[int, float, str]]:
    if not db_path.exists():
        return []
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "select id,timestamp,content from messages where role='user' and timestamp >= ? order by id desc limit 200",
            (since_ts,),
        ).fetchall()
    finally:
        con.close()
    return [(int(r[0]), float(r[1]), str(r[2] or "")) for r in rows]


def decision_text_parts(content: str) -> tuple[str, str]:
    """Return (active user text, replied-to/context text) from Hermes Discord transcript payloads."""
    marker = "[New message]"
    if marker in content:
        context, active = content.split(marker, 1)
    else:
        context, active = "", content
    active = active.strip()
    # Discord messages in multi-user threads are prefixed like `[name] text`.
    m = re.match(r"^\[[^\]]+\]\s*(.*)$", active, flags=re.S)
    if m:
        active = m.group(1).strip()
    return active, context


def find_decision(req: dict[str, Any], db_path: Path) -> tuple[str, int, str] | None:
    rid_raw = req["id"]
    rid = re.escape(rid_raw)
    since = float(req.get("created_ts") or 0) - 5
    for mid, _ts, content in recent_user_messages(db_path, since):
        active, context = decision_text_parts(content)
        # Only treat the user's active text as the decision. The Discord bridge may
        # include replied-to/context messages containing `ALLOW ALWAYS <token>` rows;
        # those must never be re-parsed as fresh approvals.
        if re.search(rf"\bALLOW\s+ALWAYS\s+{rid}\b", active, flags=re.I):
            return "approved_always", mid, active
        if re.search(rf"\b(?:APPROVE|ALLOW\s+ONCE|ALLOW)\s+{rid}\b", active, flags=re.I):
            return "approved", mid, active
        if re.search(rf"\bDENY\s+{rid}\b", active, flags=re.I):
            return "denied", mid, active
        # Usability: a bare APPROVE/ALLOW/DENY reply applies only to the token in
        # the directly replied-to message, not every token present in surrounding
        # context.
        bare = re.fullmatch(r"(?:APPROVE|ALLOW(?:\s+ONCE)?|DENY)\b", active.strip(), flags=re.I)
        if bare:
            replied_match = re.search(r"\[Replying to:\s*.*?Token:\s*`?([0-9a-f]{8})`?", context, flags=re.I | re.S)
            if replied_match and replied_match.group(1).lower() == rid_raw.lower():
                return ("denied" if active.strip().upper().startswith("DENY") else "approved"), mid, active
    return None


def execute_request(config: dict[str, Any], req: dict[str, Any], dry_run: bool = False) -> list[str]:
    board = config.get("board", "default")
    cwd = Path(config.get("workdir") or ".").expanduser()
    workdir = Path(req["workdir"]).expanduser()
    cmd = req["command"]
    task_id = req["task_id"]
    if not is_allowed_command(cmd, workdir):
        req["status"] = "rejected_unsafe"
        return [f"approval-rejected-unsafe:{task_id}:{req['id']}"]
    if dry_run:
        return [f"dry-run:approved-would-execute:{task_id}:{req['id']}"]
    cp = subprocess.run(cmd, cwd=str(workdir), shell=True, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=900)
    req["executed_at"] = utcnow()
    req["exit_code"] = cp.returncode
    req["stdout_tail"] = (cp.stdout or "")[-4000:]
    req["stderr_tail"] = (cp.stderr or "")[-4000:]
    if cp.returncode == 0:
        req["status"] = "executed"
        kanban_comment(board, task_id, f"🚦 Approval Traffic Cop executed approved command token `{req['id']}` successfully. Worker may continue Codex/CI/merge closeout.", cwd)
        u = kanban_unblock(board, task_id, f"Approval token {req['id']} granted in Discord; exact blocked command executed successfully by traffic cop.", cwd)
        d = dispatch(board, cwd)
        return [f"approval-executed:{task_id}:{req['id']}", u, d]
    req["status"] = "execute_failed"
    kanban_comment(board, task_id, f"🚦 Approval Traffic Cop attempted approved command token `{req['id']}` but it failed with exit {cp.returncode}. stderr: {compact(cp.stderr, 900)} stdout: {compact(cp.stdout, 500)}", cwd)
    return [f"approval-execute-failed:{task_id}:{req['id']}:exit-{cp.returncode}"]


def poll(config: dict[str, Any], state: dict[str, Any], db_path: Path, dry_run: bool = False) -> list[str]:
    actions: list[str] = []
    for rid, req in list((state.get("requests") or {}).items()):
        status = req.get("status")
        if status == "approved" and not req.get("executed_at"):
            # Recovery path: execution may have been interrupted after recording approval.
            actions.extend(execute_request(config, req, dry_run=dry_run))
            continue
        if status != "requested":
            continue
        decision = find_decision(req, db_path)
        if not decision:
            continue
        verdict, message_id, content = decision
        if dry_run:
            actions.append(f"dry-run:approval-{verdict}:{req['task_id']}:{rid}:message-{message_id}")
            continue
        req["decision_message_id"] = message_id
        req["decision_at"] = utcnow()
        if verdict == "denied":
            req["status"] = "denied"
            actions.append(f"approval-denied:{req['task_id']}:{rid}")
            board = config.get("board", "default")
            cwd = Path(config.get("workdir") or ".").expanduser()
            kanban_comment(board, req["task_id"], f"🚦 Approval Traffic Cop recorded Discord denial for token `{rid}`; worker remains blocked.", cwd)
        else:
            if verdict == "approved_always":
                pkey = req.get("policy_key") or policy_id(req["command"], Path(req["workdir"]).expanduser())
                state.setdefault("allow_always", {})[pkey] = {
                    "created_at": utcnow(),
                    "request_id": rid,
                    "task_id": req["task_id"],
                    "command": req["command"],
                    "workdir": req["workdir"],
                    "decision_message_id": message_id,
                }
                req["policy_key"] = pkey
                req["allow_always_recorded"] = True
            req["status"] = "approved"
            actions.extend(execute_request(config, req, dry_run=dry_run))
    return actions


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("config", type=Path)
    ap.add_argument("task_ids", nargs="*")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--poll", action="store_true")
    ap.add_argument("--request", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    config = load_json(args.config, {})
    state = load_json(args.state, {"requests": {}})
    actions: list[str] = []
    if args.scan or (not args.poll and not args.request and not args.task_ids):
        actions.extend(scan(config, state, dry_run=args.dry_run))
    if args.request or args.task_ids:
        for task_id in args.task_ids:
            actions.extend(request_for_task(config, state, task_id, dry_run=args.dry_run))
    if args.poll or (not args.request and not args.task_ids):
        actions.extend(poll(config, state, args.db, dry_run=args.dry_run))
    save_json(args.state, state)
    # Watchdog pattern: stay quiet unless there is a new actionable event.
    interesting = [a for a in actions if not (a.startswith("approval-cop-existing") or a.startswith("approval-cop-skipped"))]
    if interesting:
        print("\n".join(interesting))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
