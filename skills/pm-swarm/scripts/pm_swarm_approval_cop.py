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
import importlib.util
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_STATE = Path.home() / ".hermes/state/pm_swarm_approval_cop.json"
DEFAULT_DB = Path.home() / ".hermes/state.db"
DEFAULT_DASHBOARD = Path.home() / ".hermes/state/pm_swarm_approval_queue.md"
DEFAULT_QUEUE_JSON = Path.home() / ".hermes/state/pm_swarm_approval_queue.json"
APPROVAL_KEYWORDS = (
    "approval required",
    "approval-required",
    "approval needed",
    "approval-needed",
    "need approval",
    "needs approval",
    "requires approval",
    "approval layer",
    "push approval required",
    "push approval needed",
    "needs approval",
    "needed approval",
    "explicit approval",
    "human decision",
    "approve one over-cap",
    "over-cap current-head @codex review",
    "approve another codex trigger",
    "approve one extra trigger",
    "approve one more codex trigger",
    "approve one more @codex review",
    "approve one more codex review",
    "authorize one extra @codex review",
    "one extra @codex review pass",
    "approve extra trigger",
    "bypass-merge approval",
    "bypass merge approval",
    "blocked by approval",
    "blocked command",
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


def _discord_api(token: str, method: str, path: str, data: bytes | None = None) -> tuple[int, str]:
    req = urllib.request.Request(
        f"https://discord.com/api/v10{path}",
        data=data,
        headers={
            "Authorization": f"Bot {token}",
            "Content-Type": "application/json",
            "User-Agent": "HermesApprovalCop/1.0",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def _discord_bot_user_id(token: str) -> str:
    try:
        status, body = _discord_api(token, "GET", "/users/@me")
        if 200 <= status < 300:
            return str((json.loads(body) or {}).get("id") or "")
    except Exception:
        return ""
    return ""


def _discord_reaction_path(channel_id: str, message_id: str, emoji: str) -> str:
    return f"/channels/{channel_id}/messages/{message_id}/reactions/{urllib.parse.quote(emoji, safe='')}"


def add_approval_reactions(channel_id: str, message_id: str, token: str) -> str:
    statuses: list[str] = []
    for emoji in ("✅", "💯", "⛔", "🔥"):
        try:
            status, _body = _discord_api(token, "PUT", _discord_reaction_path(channel_id, message_id, emoji) + "/@me")
            statuses.append(f"{emoji}:{status}")
        except Exception as e:
            statuses.append(f"{emoji}:err:{compact(e, 60)}")
    return "reactions:" + ",".join(statuses)


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
                body = resp.read().decode("utf-8", "replace")
                if 200 <= resp.status < 300:
                    sent = json.loads(body or "{}")
                    message_id = str(sent.get("id") or "")
                    reaction_status = add_approval_reactions(send_to, message_id, token) if message_id else "reactions-skipped:no-message-id"
                    return f"discord-sent-with-mention:{target}:channel:{send_to}:message:{message_id}:{reaction_status}"
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
    workdir = resolve_git_workdir(Path(str(task.get("workspace_path") or fallback_cwd)).expanduser())
    # Prefer explicit worktree/workspace paths in handoff comments, including
    # dispatcher-created scratch worktrees under .hermes/kanban/workspaces.
    m_workspace = re.search(r'"workspace"\s*:\s*"(/home/pfkagent/\.hermes/kanban/workspaces/t_[0-9a-f]+/[\w./-]+)"', text)
    if not m_workspace:
        m_workspace = re.search(r"(/home/pfkagent/\.hermes/kanban/workspaces/t_[0-9a-f]+/[\w./-]+)", text)
    if m_workspace:
        # Doctor cards often quote the real worker worktree as a plain path
        # rather than a structured JSON workspace field. Prefer it over the
        # doctor's scratch card path so exact push commands can be tokenized.
        workdir = resolve_git_workdir(Path(m_workspace.group(1)))
    m_path = re.search(r"(/home/pfkagent/dev/(?:resolve-wt/issue-\d+(?:-[\w.-]+)?|frankenbeast)(?:/[\w./-]+)?|/tmp/pr\d+-wt)", text)
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
        workdir = resolve_git_workdir(p)
    else:
        # Kanban workspace_path is not necessarily the git worktree. If the
        # worker text names an issue and the standard resolve worktree exists,
        # use it so approval-cop can extract/approve the exact blocked command.
        m_issue = re.search(r"(?:issue|resolve issue)\s*#(\d+)", text, flags=re.I)
        if m_issue:
            candidate = Path(f"/home/pfkagent/dev/resolve-wt/issue-{m_issue.group(1)}")
            if candidate.exists():
                workdir = resolve_git_workdir(candidate)
    # Exact blocked command blocks are usually backtick quoted after a label.
    labels = [
        r"exact command attempted was blocked by approval:\s*([^\n\"`]+)",
        r"Exact blocked command:\s*`([^`]+)`",
        r"Exact blocked command:\s*```(?:bash)?\s*([^`]+?)\s*```",
        # Some handoffs put the command on the next plain line instead of in
        # backticks/fences, e.g. "Exact blocked command:\n  git push ...".
        r"Exact blocked command:\s*\n\s*([^\n`]+)",
        r"exact command is\s*`([^`]+)`",
        r"blocked by approval\s+(?:on\s+)?(?:the\s+)?exact command\s*`([^`]+)`",
        r"blocked by approval\s+for\s*`([^`]+)`",
        r"exact command blocked pending approval was:\s*```(?:bash)?\s*([^`]+?)\s*```",
        r"request approval for this exact [^:]+command:\s*```(?:bash)?\s*([^`]+?)\s*```",
        r"approval blocker is missing an allowlisted exact command.*?```(?:bash)?\s*([^`]+?)\s*```",
        r"blocked command(?:\s*\([^)]*\))?(?:\s+(?:requiring|needing)\s+approval)?(?:\s+was)?:\s*`([^`]+)`",
        r"blocked command(?:\s+(?:requiring|needing)\s+approval)?(?:\s+was)?:\s*```(?:bash)?\s*([^`]+?)\s*```",
        r"blocked command(?:\s*\([^)]*\))?(?:\s+(?:requiring|needing)\s+approval)?(?:\s+was)?:\s*\n\s*([^\n`]+)",
        r'"blocked_command"\s*:\s*"([^"]+)"',
        r"blocked command awaiting approval layer:\s*```(?:bash)?\s*([^`]+?)\s*```",
        r"approval-blocked command:\s*```(?:bash)?\s*([^`]+?)\s*```",
        r"approval-blocked command:\s*`([^`]+)`",
        r"required remote update commands:\s*-\s*`([^`]+)`",
        # Doctor summaries sometimes embed the exact over-cap Codex trigger in
        # prose rather than under an "Exact blocked command" label, e.g.
        # "Approve ... `gh pr comment 836 ... --body '@codex review'`".
        # Keep this narrow: Codex review triggers are still allowlisted separately
        # and budget policy is checked before any approval token is minted.
        r"(`gh pr comment \d+ --repo djm204/frankenbeast --body '@codex review'`)",
        r"(`CODEX_REVIEW_MAX_INVOCATIONS=\d+ bash /home/pfkagent/\.hermes/skills/codex-review-loop/scripts/codex-review-loop\.sh trigger --repo djm204/frankenbeast --pr \d+`)",
        r"(`git push --force-with-lease(?: [^`]+)?`)",
        r"(`git add [^`]+git push --force-with-lease[^`]+`)",
    ]
    command_candidates: list[tuple[int, str]] = []
    for pat in labels:
        for m in re.finditer(pat, text, flags=re.I | re.S):
            cmd = m.group(1).strip()
            if cmd.startswith("`") and cmd.endswith("`"):
                cmd = cmd[1:-1]
            # Preserve command block line structure so shell=True executes the
            # same approved script. Single-line backtick commands are collapsed
            # to keep existing exact command policy ids stable.
            lines = [line.rstrip() for line in cmd.splitlines() if line.strip()]
            cmd = "\n".join(lines) if len(lines) > 1 else " ".join(cmd.split())
            # A previously approved codex-review-loop trigger can become stale
            # after the trigger count reaches the approved cap, e.g. the exact
            # recorded command used MAX_INVOCATIONS=21 and execution failed with
            # "invocation cap reached (21/21)". With a fresh approve-all, mint a
            # new exact command for the next cap value instead of reusing the
            # permanently failing token.
            cap_reached = re.search(r"invocation cap reached \((\d+)/\1\)", text, flags=re.I)
            if cap_reached and is_codex_review_loop_trigger_command(cmd):
                next_cap = int(cap_reached.group(1)) + 1
                cmd = re.sub(r"CODEX_REVIEW_MAX_INVOCATIONS=\d+", f"CODEX_REVIEW_MAX_INVOCATIONS={next_cap}", cmd, count=1)
            command_candidates.append((m.start(), cmd))
    for _pos, cmd in sorted(command_candidates, key=lambda item: item[0], reverse=True):
        # A doctor/takeover card often runs from a scratch Kanban workspace while
        # the exact Codex-review command is a repo-scoped GitHub CLI side effect.
        # Execute/request that command from the configured repo cwd instead of
        # rejecting it because the scratch workspace is not a git repo. Budget
        # policy is enforced later before creating an approval token.
        candidate_workdir = fallback_cwd if is_codex_review_trigger_command(cmd) else workdir
        if is_allowed_command(cmd, candidate_workdir):
            return cmd, candidate_workdir

    # Some approval handoffs include a structured target branch and state that a
    # force-with-lease publish is required, but omit the exact shell line. Keep
    # this synthesis narrow: only publish the current HEAD to an issue branch
    # when the card text explicitly says force-with-lease is the remaining
    # approval blocker.
    lower_text = text.lower()
    if "force-with-lease" in lower_text and ("push" in lower_text or "publishing" in lower_text):
        branch_match = re.search(r'"target_branch"\s*:\s*"(resolve/issue-\d+[-/\w.]+)"', text)
        if not branch_match:
            branch_match = re.search(r"target branch\s*[:=]\s*`?(resolve/issue-\d+[-/\w.]+)`?", text, flags=re.I)
        if branch_match:
            cmd = f"git push --force-with-lease origin HEAD:{branch_match.group(1)}"
            if is_allowed_command(cmd, workdir):
                return cmd, workdir

    # Over-cap Codex review blockers often summarize the needed action in prose
    # instead of preserving an exact command. Normalize those into the same
    # approval-gated raw Codex trigger command so the durable ledger gets a token
    # instead of liveness repeating "approve another trigger" forever.
    pr_match = re.search(r"PR\s*#(\d+)", text, flags=re.I)
    lower_text = text.lower()
    ids = re.findall(r"`(\d{8,})`", text)
    ids.extend(re.findall(r'"(\d{8,})"\s*:', text))
    helper_requested = bool(ids) and any(
        helper_marker in lower_text
        for helper_marker in ("resolve-codex", "reply/resolve", "comment-ids", "codex threads", "codex review threads", "review threads")
    )
    if pr_match and "codex" in lower_text and "review" in lower_text and not helper_requested and any(
        marker in lower_text
        for marker in (
            "5 @codex review invocation cap",
            "5/5 trigger cap",
            "invocation cap 5/5",
            "invocation cap reached",
            "invocation cap is reached",
            "refused due invocation cap",
            "refused due to invocation cap",
            "reached (5/5)",
            "fresh @codex review",
            "approve another codex trigger",
            "one extra trigger",
            "one extra @codex review pass",
            "authorize one extra @codex review",
            "extra codex trigger",
            "override the cap",
            "max-invocations 6",
            "max-invocation cap has been reached",
            "max invocation cap has been reached",
            "default 5-trigger cap",
            "explicit approval for another @codex review",
            "another @codex review",
            "6th review",
            "fresh current-head codex clean pass",
        )
    ):
        cmd = f"gh pr comment {pr_match.group(1)} --repo djm204/frankenbeast --body '@codex review'"
        workdir = fallback_cwd
        if is_allowed_command(cmd, workdir):
            return cmd, workdir
    # Workers sometimes cannot include a long GitHub API reply/resolve script in
    # the blocked summary. If they recorded PR + exact Codex comment ids, create
    # a deterministic helper command that performs only those side effects after
    # approval. Accept both backticked ids and JSON object keys from fenced
    # `/tmp/resolve-codex-<pr>.json` handoffs.
    ids = re.findall(r"`(\d{8,})`", text)
    ids.extend(re.findall(r'"(\d{8,})"\s*:', text))
    lower_text = text.lower()
    if pr_match and ids and any(
        marker in lower_text
        for marker in (
            "resolve codex",
            "codex review comments",
            "codex review threads",
            "codex threads",
            "review threads",
            "inline comments",
            "resolve-codex",
        )
    ):
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


def resolve_git_workdir(path: Path) -> Path:
    """Resolve Kanban workspace containers to the actual checked-out git repo."""
    path = path.expanduser()
    if (path / ".git").exists():
        return path
    for child_name in ("repo", "frankenbeast"):
        child = path / child_name
        if (child / ".git").exists():
            return child
    try:
        if path.exists():
            for child in path.iterdir():
                if child.is_dir() and (child / ".git").exists():
                    return child
    except Exception:
        pass
    return path


def is_allowed_command(cmd: str, workdir: Path) -> bool:
    workdir = resolve_git_workdir(workdir)
    wd = str(workdir.resolve()) if workdir.exists() else str(workdir)
    if not (wd.startswith("/home/pfkagent/dev/resolve-wt/") or wd == "/home/pfkagent/dev/frankenbeast" or re.fullmatch(r"/tmp/pr\d+-wt", wd)):
        if not re.match(r"^/home/pfkagent/\.hermes/kanban/workspaces/t_[0-9a-f]+(?:/(?:repo|frankenbeast|[\w.-]+))?$", wd):
            return False
    # Allow only pre-recorded publish/amend commands, narrowly scoped approved
    # deletions, plus the local Codex review-comment helper that takes explicit
    # comment ids from the worker's blocked handoff and is still gated by
    # approval-cop.
    allowed_patterns = [
        r"^git push --force-with-lease(?:=[^\s]+)?(?: origin (HEAD:)?[-/\w.]+| origin HEAD:[-/\w.]+)?$",
        r"^cd /home/pfkagent/dev/resolve-wt/issue-\d+(?:-[\w.-]+)? && git push --force-with-lease(?:=[^\s]+)?(?: origin (HEAD:)?[-/\w.]+| origin HEAD:[-/\w.]+)?$",
        r"^git push origin (?:HEAD|[0-9a-f]{7,40}):resolve/issue-\d+[-/\w.]+$",
        r"^cd /home/pfkagent/dev/resolve-wt/issue-\d+ && git push origin (?:HEAD|[0-9a-f]{7,40}):resolve/issue-\d+[-/\w.]+$",
        r"^git status --short --branch && git push --force-with-lease(?:=[^\s]+)?(?: origin (HEAD:)?[-/\w.]+| origin HEAD:[-/\w.]+)?$",
        r"^git status --short && git add [-/\w. ]+ && git commit --amend --no-edit && git rev-parse HEAD && git push --force-with-lease(?:=[^\s]+)?(?: origin (HEAD:)?[-/\w.]+| origin HEAD:[-/\w.]+)?$",
        r"^cd /home/pfkagent/dev/resolve-wt/issue-\d+(?:-clean)? && PR_BRANCH=[-/\w.]+ && git fetch origin \"\$PR_BRANCH\" && OLD=\$\(git rev-parse FETCH_HEAD\) && echo \"old_remote=\$OLD new_head=\$\(git rev-parse HEAD\)\" && git push origin HEAD:\$PR_BRANCH --force-with-lease=refs/heads/\$PR_BRANCH:\$OLD$",
        r"^git add [-/\w. ]+ && git diff --cached --(?:stat|check) && git commit --amend --no-edit && git push --force-with-lease(?: origin [-/\w.]+)?$",
        r"^git add [-/\w. ]+ && git diff --cached --stat && git diff --cached --check && git commit -m \"[^\"]+\" && git push --force-with-lease origin HEAD:[-/\w.]+$",
        r"^set -euo pipefail\s+cd /home/pfkagent/dev/resolve-wt/issue-\d+\s+(?:# [^\n]+\s+)?git diff --check\s+git status --short\s+git add [-/\w. ]+\s+git commit --amend --no-edit\s+git push --force-with-lease(?: origin [-/\w.]+)?$",
        r"^set -euo pipefail\s+cd /home/pfkagent/dev/resolve-wt/issue-\d+\s+git status --short --branch\s+git diff --stat\s+git add [-/\w. ]+\s+git commit --amend --no-edit\s+git push --force-with-lease(?: origin [-/\w.]+)?$",
        r"^set -euo pipefail\s+cd /home/pfkagent/dev/resolve-wt/issue-\d+\s+git status --short --branch\s+git push origin HEAD:[-/\w.]+\s+gh pr create --repo djm204/frankenbeast --base main --head [-/\w.]+ --title \"[^\"]+\" --body \$'[^']+'$",
        r"^set -euo pipefail\s+current=\$\(git rev-parse --short=8 HEAD\)\s+backup=\"backup/issue-\d+-local-\$\{current\}\"\s+if ! git show-ref --verify --quiet \"refs/heads/\$backup\"; then\s+git branch \"\$backup\" HEAD\s+fi\s+git reset --hard origin/resolve/issue-\d+[-/\w.]+\s+git config user\.name 'David Mendez'\s+git config user\.email 'me@davidmendez\.dev'\s+printf 'backup=%s\\n' \"\$backup\"\s+git status --short --branch\s+git rev-parse HEAD\s+git config user\.name\s+git config user\.email$",
        r"^set -euo pipefail\s+REPO=/home/pfkagent/dev/frankenbeast\s+WT=/home/pfkagent/dev/resolve-wt/issue-\d+\s+BR=resolve/issue-\d+[-/\w.]+\s+cd \"\$REPO\"\s+git fetch origin main --prune\s+if \[ -d \"\$WT/\.git\" \] \|\| \[ -f \"\$WT/\.git\" \]; then\s+echo \"worktree exists\"\s+else\s+mkdir -p \"\$\(dirname \"\$WT\"\)\"\s+git worktree add -b \"\$BR\" \"\$WT\" origin/main\s+fi\s+cd \"\$WT\"\s+git config user\.name \"David Mendez\"\s+git config user\.email \"me@davidmendez\.dev\"\s+git status --short --branch(?:\s+printf .*?node -e \"const p=require\('\./package\.json'\); console\.log\(p\.packageManager\); console\.log\(JSON\.stringify\(p\.scripts,null,2\)\)\")?$",
        r"^set -euo pipefail\s+rm -f packages/franken-web/src/pages/beast-dispatch-page\.tsx \\\\\s+packages/franken-web/src/pages/beast-dispatch-page\.test\.tsx \\\\\s+packages/franken-web/tests/pages/beast-dispatch-page\.test\.tsx$",
        rf"^python3 {re.escape(str(Path(__file__).with_name('frankenbeast_codex_resolve_comments.py')))} --repo djm204/frankenbeast --pr \d+ --comment-ids( \d{{8,}})+ --retrigger$",
        # Canonical raw Codex review trigger. This is allowlisted as an exact
        # approval-gated side effect, but request_for_task suppresses it while
        # primary/Spark daily budget is exhausted.
        r"^gh pr comment \d+ --repo djm204/frankenbeast --body '@codex review'$",
        r"^CODEX_REVIEW_MAX_INVOCATIONS=\d+ bash /home/pfkagent/\.hermes/skills/codex-review-loop/scripts/codex-review-loop\.sh trigger --repo djm204/frankenbeast --pr \d+$",
    ]
    flat = " ".join(cmd.replace("\\\n", " ").split())
    if flat == (
        "set -euo pipefail rm -f "
        "packages/franken-web/src/pages/beast-dispatch-page.tsx "
        "packages/franken-web/src/pages/beast-dispatch-page.test.tsx "
        "packages/franken-web/tests/pages/beast-dispatch-page.test.tsx"
    ):
        return True
    return any(re.match(p, cmd, flags=re.S) or re.match(p, flat, flags=re.S) for p in allowed_patterns)


def is_raw_codex_review_command(cmd: str) -> bool:
    flat = " ".join(str(cmd or "").split())
    return bool(re.fullmatch(r"gh pr comment \d+ --repo djm204/frankenbeast --body '@codex review'", flat))


def is_codex_review_loop_trigger_command(cmd: str) -> bool:
    flat = " ".join(str(cmd or "").split())
    return bool(
        re.fullmatch(
            r"CODEX_REVIEW_MAX_INVOCATIONS=\d+ bash /home/pfkagent/\.hermes/skills/codex-review-loop/scripts/codex-review-loop\.sh trigger --repo djm204/frankenbeast --pr \d+",
            flat,
        )
    )


def is_codex_review_trigger_command(cmd: str) -> bool:
    return is_raw_codex_review_command(cmd) or is_codex_review_loop_trigger_command(cmd)


def codex_review_route_exhausted(config: dict[str, Any]) -> bool:
    """Return True when raw Codex review triggers should be parked now."""
    # Prefer the same live budget router liveness/refill uses. This catches
    # actual route=ollama/paused states even after a UTC date rollover makes a
    # manual `forced_exhausted_models: YYYY-MM-DD` marker look stale.
    sibling = Path(__file__).with_name("frankenbeast_worker_refill.py")
    try:
        if sibling.exists():
            spec = importlib.util.spec_from_file_location("frankenbeast_worker_refill_budget", sibling)
            if spec and spec.loader:
                refill = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(refill)  # type: ignore[union-attr]
                budget = refill.openai_budget_status(config)
                route = str(budget.get("route") or "primary")
                if route in {"ollama", "paused"}:
                    return True
                if budget.get("primary_daily_budget_exhausted") and budget.get("spark_daily_budget_exhausted"):
                    return True
                if budget.get("hard_stop") and route != "primary":
                    return True
    except Exception:
        pass

    guard = config.get("openai_weekly_budget_guard") or {}
    daily = guard.get("daily_pacing") or {}
    forced = daily.get("forced_exhausted_models") or {}
    today = datetime.now(timezone.utc).date().isoformat()
    if isinstance(forced, dict):
        primary = str(guard.get("primary_model") or "openai-codex/gpt-5.5").split("/")[-1]
        spark = str(guard.get("spark_model") or "openai-codex/gpt-5.3-codex-spark").split("/")[-1]
        primary_forced = forced.get(primary) or forced.get("gpt-5.5")
        spark_forced = forced.get(spark) or forced.get("gpt-5.3-codex-spark")
        if (primary_forced is True or str(primary_forced).lower() in {"true", "today", today}) and (
            spark_forced is True or str(spark_forced).lower() in {"true", "today", today}
        ):
            return True
    return False


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
        f"**Approval ledger:** `{DEFAULT_DASHBOARD}` (source of truth outside Discord)\n"
        f"**Action buttons / copy-paste controls:**\n"
        f"🟢 `APPROVE {rid}` / ✅ — allow once\n"
        f"🔵 `ALLOW ALWAYS {rid}` / 💯 — always allow this exact command+workdir\n"
        f"🔴 `DENY {rid}` / ⛔ — deny and leave worker blocked\n"
        f"🔥 `APPROVE ALL` — approve all pending approvals in the durable ledger\n"
        f"Reply with one action above."
    )


def missing_command_text(task_id: str, title: str, workdir: Path, config: dict[str, Any]) -> str:
    mention = str(config.get("approval_mention") or config.get("approval_mentions") or "").strip()
    prefix = f"{mention}\n" if mention else ""
    return (
        prefix
        + f"🚦 **ACTION REQUIRED: approval blocker needs exact command** for `{task_id}` — {title or 'worker'}\n"
        f"Approval was surfaced by PM/doctor/liveness, but approval-cop cannot safely ask for approval yet because no exact allowlisted command was extractable.\n"
        f"Inferred workdir: `{workdir}`\n"
        f"**Required worker/doctor action:** add a Kanban comment containing the exact blocked command in this form:\n"
        f"```text\nExact blocked command: `<command>`\n```\n"
        f"or a fenced block labelled as the exact approval command.\n"
        f"**Approval ledger:** `{DEFAULT_DASHBOARD}`\n"
        f"This alert is sent directly so approval blockers never stay hidden inside PM/doctor output."
    )


def record_discord_delivery(req: dict[str, Any], action: str) -> None:
    m = re.search(r":channel:(\d+):message:(\d+):", action)
    if not m:
        return
    req["discord_channel_id"] = m.group(1)
    req["discord_message_id"] = m.group(2)


def pending_requests(state: dict[str, Any]) -> list[dict[str, Any]]:
    reqs = state.get("requests") or {}
    pending = [r for r in reqs.values() if isinstance(r, dict) and r.get("status") == "requested"]
    return sorted(pending, key=lambda r: (str(r.get("created_at") or ""), str(r.get("id") or "")))


def render_approval_dashboard(state: dict[str, Any], generated_by: str = "approval-cop") -> tuple[str, dict[str, Any]]:
    pending = pending_requests(state)
    missing = state.get("missing_exact_command") or {}
    now = utcnow()
    lines = [
        "# PM Swarm Approval Queue",
        "",
        f"Generated: `{now}` by `{generated_by}`",
        "",
        "This file is the durable approval ledger; Discord is only a notification/reply channel.",
        "",
        "## Commands",
        "",
        "```bash",
        "# refresh this ledger from current approval state",
        "python3 /home/pfkagent/.hermes/scripts/pm_swarm_approval_cop.py /home/pfkagent/.hermes/scripts/frankenbeast_issue_swarm_liveness.json --dashboard-only",
        "# approve/deny/clear one token in the ledger (approval executes only when approved)",
        "python3 /home/pfkagent/.hermes/scripts/pm_swarm_approval_cop.py /home/pfkagent/.hermes/scripts/frankenbeast_issue_swarm_liveness.json --approve <token>",
        "python3 /home/pfkagent/.hermes/scripts/pm_swarm_approval_cop.py /home/pfkagent/.hermes/scripts/frankenbeast_issue_swarm_liveness.json --deny <token>",
        "python3 /home/pfkagent/.hermes/scripts/pm_swarm_approval_cop.py /home/pfkagent/.hermes/scripts/frankenbeast_issue_swarm_liveness.json --clear <token>",
        "```",
        "",
        f"## Pending approvals ({len(pending)})",
        "",
    ]
    queue = {"generated_at": now, "pending": [], "missing_exact_command": missing}
    if not pending:
        lines.append("_No pending approvals._")
    for req in pending:
        cmd = str(req.get("command") or "")
        short_cmd = compact(cmd, 220)
        item = {
            "id": req.get("id"),
            "task_id": req.get("task_id"),
            "title": req.get("title"),
            "workdir": req.get("workdir"),
            "created_at": req.get("created_at"),
            "last_sent_at": req.get("last_sent_at"),
            "reminder_count": req.get("reminder_count") or 0,
            "command": cmd,
        }
        queue["pending"].append(item)
        lines.extend([
            f"### `{req.get('id')}` — `{req.get('task_id')}`",
            "",
            f"- Title: {req.get('title') or ''}",
            f"- Status: `{req.get('status')}`",
            f"- Created: `{req.get('created_at')}`; last notification: `{req.get('last_sent_at')}`; reminders: `{req.get('reminder_count') or 0}`",
            f"- Workdir: `{req.get('workdir')}`",
            f"- Command summary: `{short_cmd}`",
            "",
            "```bash",
            cmd,
            "```",
            "",
            f"Clear without executing: `python3 /home/pfkagent/.hermes/scripts/pm_swarm_approval_cop.py /home/pfkagent/.hermes/scripts/frankenbeast_issue_swarm_liveness.json --clear {req.get('id')}`",
            "",
        ])
    if missing:
        lines.extend(["", f"## Needs exact command ({len(missing)})", ""])
        for task_id, info in sorted(missing.items()):
            lines.append(f"- `{task_id}` last prompted `{info.get('last_commented_at')}`")
    return "\n".join(lines).rstrip() + "\n", queue


def write_approval_dashboard(state: dict[str, Any], dashboard_path: Path = DEFAULT_DASHBOARD, queue_json_path: Path = DEFAULT_QUEUE_JSON, generated_by: str = "approval-cop") -> str:
    text, queue = render_approval_dashboard(state, generated_by=generated_by)
    dashboard_path.parent.mkdir(parents=True, exist_ok=True)
    dashboard_path.write_text(text)
    queue_json_path.parent.mkdir(parents=True, exist_ok=True)
    queue_json_path.write_text(json.dumps(queue, indent=2, sort_keys=True) + "\n")
    return f"approval-dashboard-updated:{dashboard_path}"


def mark_request(state: dict[str, Any], token: str, status: str, note: str = "") -> str:
    req = (state.get("requests") or {}).get(token)
    if not req:
        return f"approval-token-missing:{token}"
    if req.get("status") != "requested" and status in {"cleared", "denied"}:
        return f"approval-token-not-pending:{token}:{req.get('status')}"
    req["status"] = status
    req[f"{status}_at"] = utcnow()
    if note:
        req[f"{status}_note"] = note
    return f"approval-{status}:{req.get('task_id')}:{token}"


def approve_tokens(config: dict[str, Any], state: dict[str, Any], tokens: list[str], dry_run: bool = False, decision_source: str = "approval-ledger-cli") -> list[str]:
    actions: list[str] = []
    for token in tokens:
        req = (state.get("requests") or {}).get(token)
        if not req:
            actions.append(f"approval-token-missing:{token}")
            continue
        if req.get("status") != "requested":
            actions.append(f"approval-token-not-pending:{token}:{req.get('status')}")
            continue
        if dry_run:
            actions.append(f"dry-run:approval-approved:{req.get('task_id')}:{token}")
            continue
        req["status"] = "approved"
        req["decision_at"] = req.get("decision_at") or utcnow()
        req["decision_source"] = decision_source
        actions.extend(execute_request(config, req, dry_run=dry_run))
    return actions

def _parse_time(ts: Any) -> float:
    if isinstance(ts, (int, float)):
        return float(ts)
    if not ts:
        return 0.0
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def request_for_task(config: dict[str, Any], state: dict[str, Any], task_id: str, dry_run: bool = False) -> list[str]:
    board = config.get("board", "default")
    cwd = Path(config.get("workdir") or ".").expanduser()
    target = config.get("approval_discord_target") or config.get("doctor_discord_target")
    data = task_show(board, task_id, cwd)
    if not data:
        return [f"approval-cop-show-failed:{task_id}"]
    task = data.get("task") or {}
    live_status = str(task.get("status") or "").lower()
    if live_status in {"done", "completed", "closed", "merged", "archived", "cancelled", "terminal"}:
        # A terminal Kanban card is not a live approval blocker even if old
        # doctor comments still contain approval-required prose. Clear stale
        # ledger rows so approve-all stops surfacing ghosts.
        state.setdefault("missing_exact_command", {}).pop(task_id, None)
        state.setdefault("budget_parked_codex_review", {}).pop(task_id, None)
        return [f"approval-cop-skipped:terminal-task:{task_id}:{live_status}"]
    completed = config.get("completed_worker_ids") or {}
    terminated = config.get("terminated_workers") or {}
    deferred = config.get("deferred_worker_ids") or {}
    if task_id in completed or task_id in terminated or task_id in deferred:
        # Liveness may leave a stale needs-exact-command ledger entry after a
        # worker/doctor merged the PR, archived the original card, or explicitly
        # deferred a superseded duplicate. Treat that as config/ledger hygiene,
        # not as a live approval blocker, and never create a new approval prompt
        # for non-actionable work.
        state.setdefault("missing_exact_command", {}).pop(task_id, None)
        state.setdefault("budget_parked_codex_review", {}).pop(task_id, None)
        if task_id in completed:
            bucket = "completed-worker"
            reason = completed.get(task_id)
        elif task_id in terminated:
            bucket = "terminated-worker"
            reason = terminated.get(task_id)
        else:
            bucket = "deferred-worker"
            reason = deferred.get(task_id)
        return [f"approval-cop-skipped:{bucket}:{task_id}:{compact(reason or bucket, 180)}"]
    summary = str(data.get("latest_summary") or "")
    comments = [str(c.get("body") or "") for c in (data.get("comments") or []) if isinstance(c, dict)]
    haystack = "\n".join([summary] + comments).lower()
    if not any(k in haystack for k in APPROVAL_KEYWORDS):
        return [f"approval-cop-skipped:not-approval-blocked:{task_id}"]
    cmd, workdir = extract_command_and_workdir(data, cwd)
    if not cmd:
        missing = state.setdefault("missing_exact_command", {})
        last = float(missing.get(task_id, {}).get("last_commented_ts") or 0)
        last_alert = float(missing.get(task_id, {}).get("last_discord_ts") or 0)
        age = time.time() - last if last else 10**9
        alert_age = time.time() - last_alert if last_alert else 10**9
        remind_minutes = float(config.get("approval_missing_command_remind_minutes", 30) or 30)
        if age >= remind_minutes * 60:
            kanban_comment(
                board,
                task_id,
                "🚦 Approval Traffic Cop sees an approval-required blocker, but no exact allowlisted blocked command was recorded. "
                "Please add a comment with `Exact blocked command: `<command>``, the worktree, and the verification summary so approval-cop can request human approval safely.",
                cwd,
            )
            missing.setdefault(task_id, {})["last_commented_at"] = utcnow()
            missing.setdefault(task_id, {})["last_commented_ts"] = time.time()
        if alert_age >= remind_minutes * 60:
            action = send_discord(target, missing_command_text(task_id, str(task.get("title") or ""), workdir, config), cwd, dry_run=dry_run)
            entry = missing.setdefault(task_id, {})
            entry["last_discord_at"] = utcnow()
            entry["last_discord_ts"] = time.time()
            entry["last_discord_action"] = action
            return [f"approval-cop-needs-exact-command:{task_id}", action]
        return [f"approval-cop-needs-exact-command:{task_id}:alert-age-{int(alert_age)}s"]
    # If a later PM/doctor comment supplies an extractable exact command, clear
    # any stale "needs exact command" ledger entry for this worker immediately.
    # Budget parking is a separate policy state: the command exists and is
    # allowlisted, but it must not create an approval token while the Codex route
    # is exhausted.
    state.setdefault("missing_exact_command", {}).pop(task_id, None)
    if is_codex_review_trigger_command(cmd) and codex_review_route_exhausted(config):
        entry = state.setdefault("budget_parked_codex_review", {}).setdefault(task_id, {})
        entry["last_seen_at"] = utcnow()
        entry["last_command"] = cmd
        entry["workdir"] = str(workdir)
        entry["reason"] = "primary and Spark daily budget exhausted; raw @codex review is parked until budget route reopens"
        if not dry_run:
            kanban_comment(
                board,
                task_id,
                "🚦 Approval Traffic Cop found an exact raw `@codex review` command, but primary/Spark daily budget is exhausted. "
                "The command remains allowlisted for normal budget windows, but it is parked now so approval cannot bypass the budget guard.",
                cwd,
            )
        return [f"approval-cop-budget-parked-codex-review:{task_id}"]
    rid = request_id(task_id, cmd, workdir)
    pkey = policy_id(cmd, workdir)
    policies = state.setdefault("allow_always", {})
    reqs = state.setdefault("requests", {})
    existing = reqs.get(rid)
    if existing and existing.get("status") == "executed" and any(k in haystack for k in APPROVAL_KEYWORDS) and any(
        marker in haystack for marker in ("blocked again", "approval was blocked again", "blocked command was blocked again")
    ):
        # Same task/command can legitimately need a second approval after the
        # worker amends again. A deterministic task+command id would otherwise
        # point at the old executed token and leave the worker permanently
        # blocked while the dashboard says nothing is pending.
        rid = hashlib.sha256(f"repeat-approval-v1\n{task_id}\n{workdir}\n{cmd}\n{summary}".encode()).hexdigest()[:8]
        existing = reqs.get(rid)
    if existing and existing.get("status") in {"requested", "approved", "executed", "auto_approved", "execute_failed"}:
        status = existing.get("status")
        if status == "execute_failed" and existing.get("exit_code") == "missing_workdir":
            missing = state.setdefault("missing_exact_command", {})
            entry = missing.setdefault(task_id, {})
            entry["last_missing_workdir"] = existing.get("workdir")
            entry["last_missing_workdir_token"] = rid
            entry["last_commented_at"] = utcnow()
            entry["last_commented_ts"] = time.time()
            kanban_comment(
                board,
                task_id,
                f"🚦 Approval Traffic Cop will not re-request stale token `{rid}` because the approved command's recorded workdir is missing: `{existing.get('workdir')}`. Doctor/worker must post a fresh exact command using an existing worktree, or clear this blocker if live PR state shows the push already landed.",
                cwd,
            )
            return [f"approval-cop-stale-missing-workdir:{task_id}:{rid}"]
        if status == "requested":
            remind_minutes = float(config.get("approval_remind_minutes", 10) or 10)
            last_sent_ts = _parse_time(existing.get("last_sent_at") or existing.get("created_at"))
            age = time.time() - last_sent_ts if last_sent_ts else 10**9
            if age >= remind_minutes * 60:
                action = send_discord(existing.get("discord_target") or target, approval_text(existing), cwd, dry_run=dry_run)
                record_discord_delivery(existing, action)
                existing["last_sent_at"] = utcnow()
                existing["reminder_count"] = int(existing.get("reminder_count") or 0) + 1
                kanban_comment(board, task_id, f"🚦 Approval Traffic Cop re-sent pending Discord approval token `{rid}` for exact command: `{cmd}`", cwd)
                return [f"approval-reminded:{task_id}:{rid}", action]
            return [f"approval-cop-pending:{task_id}:{rid}:age-{int(age)}s"]
        return [f"approval-cop-existing:{task_id}:{rid}:{status}"]
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
    record_discord_delivery(req, action)
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
        # Batch usability: if the user replies `approve all` or 🔥, approve
        # every currently pending request in the durable ledger. This preserves
        # approve-once semantics but avoids losing approvals when Discord reply
        # context is truncated or a liveness digest omits sibling tokens.
        active_stripped = active.strip()
        if re.fullmatch(r"(?:APPROVE\s+ALL|🔥)", active_stripped, flags=re.I):
            # The user uses APPROVE ALL/🔥 as a ledger-wide approve-once control
            # for the PM-swarm approval cop. Do not require the token to be in
            # Discord reply context: bridge truncation, reposted prompts, or
            # liveness summaries can omit sibling pending tokens even though the
            # durable ledger is the source of truth.
            return "approved", mid, active
        if re.search(rf"(?:\b(?:ALLOW\s+ALWAYS|ALWAYS\s+ALLOW)\s+{rid}\b|💯\s*`?{rid_raw}`?)", active, flags=re.I):
            return "approved_always", mid, active
        if re.search(rf"(?:\b(?:APPROVE|ALLOW\s+ONCE|ALLOW)\s+{rid}\b|✅\s*`?{rid_raw}`?)", active, flags=re.I):
            return "approved", mid, active
        if re.search(rf"(?:\bDENY\s+{rid}\b|⛔\s*`?{rid_raw}`?)", active, flags=re.I):
            return "denied", mid, active
        # Usability: a bare APPROVE/ALLOW/DENY reply, or its emoji shortcut,
        # applies only to the token in the directly replied-to message, not every
        # token present in surrounding context.
        bare = re.fullmatch(r"(?:APPROVE|ALLOW(?:\s+ONCE)?|ALLOW\s+ALWAYS|ALWAYS\s+ALLOW|DENY|✅|💯|⛔)", active_stripped, flags=re.I)
        if bare:
            replied_match = re.search(r"\[Replying to:\s*.*?Token:\s*`?([0-9a-f]{8})`?", context, flags=re.I | re.S)
            if replied_match and replied_match.group(1).lower() == rid_raw.lower():
                active_upper = " ".join(active_stripped.upper().split())
                if active_upper.startswith("DENY") or active_stripped == "⛔":
                    verdict = "denied"
                elif active_upper in {"ALLOW ALWAYS", "ALWAYS ALLOW"} or active_stripped == "💯":
                    verdict = "approved_always"
                else:
                    verdict = "approved"
                return verdict, mid, active
    return None


def find_reaction_decision(req: dict[str, Any]) -> tuple[str, int, str] | None:
    """Poll Discord reactions on the approval request message.

    The bot pre-seeds ✅/💯/⛔/🔥 reactions. Prefer a single message fetch and
    inspect reaction counts, because fetching the user list for all four emojis
    every 5 seconds quickly hits Discord per-route rate limits. Since the bot
    seeds exactly one normal reaction per control, count > 1 means a human (or
    another non-seed actor) reacted and is enough to make the decision. Fall
    back to user-list inspection only for unseeded/ambiguous cases.
    """
    token = _load_discord_token()
    channel_id = str(req.get("discord_channel_id") or "")
    message_id = str(req.get("discord_message_id") or "")
    if not token or not channel_id or not message_id:
        return None
    checks = (("🔥", "approved_all"), ("💯", "approved_always"), ("✅", "approved"), ("⛔", "denied"))
    try:
        status, body = _discord_api(token, "GET", f"/channels/{channel_id}/messages/{message_id}")
        if 200 <= status < 300:
            msg = json.loads(body or "{}")
            reactions = msg.get("reactions") or []
            by_name = {
                str(((r or {}).get("emoji") or {}).get("name") or ""): (r or {})
                for r in reactions
                if isinstance(r, dict)
            }
            req["last_reaction_poll_at"] = utcnow()
            req["last_reaction_snapshot"] = {
                emoji: {
                    "count": int((by_name.get(emoji) or {}).get("count") or 0),
                    "count_details": (by_name.get(emoji) or {}).get("count_details") or {},
                    "me": bool((by_name.get(emoji) or {}).get("me")),
                }
                for emoji, _verdict in checks
                if by_name.get(emoji)
            }
            for emoji, verdict in checks:
                r = by_name.get(emoji)
                if not r:
                    continue
                details = r.get("count_details") if isinstance(r.get("count_details"), dict) else {}
                count = int(r.get("count") or 0)
                normal = int(details.get("normal") or 0)
                burst = int(details.get("burst") or 0)
                total = max(count, normal + burst)
                # Bot-seeded controls have exactly one normal reaction from us.
                # Any extra normal or burst reaction indicates a human click.
                if total > 1 or burst > 0 or (total > 0 and not bool(r.get("me"))):
                    return verdict, int(message_id), f"discord-reaction:{emoji}:count:{total}:burst:{burst}"
    except Exception as e:
        req["last_reaction_poll_error"] = compact(e, 200)

    bot_id = _discord_bot_user_id(token)
    for emoji, verdict in checks:
        try:
            status, body = _discord_api(token, "GET", _discord_reaction_path(channel_id, message_id, emoji) + "?limit=100")
            if not (200 <= status < 300):
                continue
            users = json.loads(body or "[]")
        except Exception as e:
            req["last_reaction_poll_error"] = compact(e, 200)
            continue
        for user in (users if isinstance(users, list) else []):
            uid = str((user or {}).get("id") or "")
            if uid and uid != bot_id:
                return verdict, int(message_id), f"discord-reaction:{emoji}:user:{uid}"
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
    if is_codex_review_trigger_command(cmd) and codex_review_route_exhausted(config):
        req["status"] = "budget_parked"
        req["budget_parked_at"] = utcnow()
        req["budget_parked_reason"] = "primary and Spark daily budget exhausted; raw @codex review is parked until budget route reopens"
        if not dry_run:
            kanban_comment(
                board,
                task_id,
                f"🚦 Approval Traffic Cop received approval for token `{req['id']}`, but did not execute it because primary/Spark daily budget is exhausted. The exact `@codex review` command is parked until the budget route reopens.",
                cwd,
            )
        return [f"approval-budget-parked-codex-review:{task_id}:{req['id']}"]
    if dry_run:
        return [f"dry-run:approved-would-execute:{task_id}:{req['id']}"]
    timeout_seconds = int(float(config.get("approval_command_timeout_seconds", 3600) or 3600))

    try:
        cp = subprocess.run(
            cmd,
            cwd=str(workdir),
            shell=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as e:
        req["executed_at"] = utcnow()
        req["exit_code"] = "missing_workdir"
        req["stdout_tail"] = ""
        req["stderr_tail"] = str(e)[-4000:]
        req["status"] = "execute_failed"
        kanban_comment(
            board,
            task_id,
            f"🚦 Approval Traffic Cop could not execute approved command token `{req['id']}` because the recorded workdir is missing: `{workdir}`. stderr: {compact(e, 900)}",
            cwd,
        )
        return [f"approval-execute-failed:{task_id}:{req['id']}:missing-workdir"]
    except subprocess.TimeoutExpired as e:
        req["executed_at"] = utcnow()
        req["exit_code"] = "timeout"
        req["stdout_tail"] = ((e.stdout or "") if isinstance(e.stdout, str) else "")[-4000:]
        req["stderr_tail"] = ((e.stderr or "") if isinstance(e.stderr, str) else "")[-4000:]
        req["status"] = "execute_timeout"
        kanban_comment(
            board,
            task_id,
            f"🚦 Approval Traffic Cop attempted approved command token `{req['id']}` but it exceeded the configured timeout of {timeout_seconds}s. "
            f"stdout: {compact(req.get('stdout_tail'), 500)} stderr: {compact(req.get('stderr_tail'), 900)}",
            cwd,
        )
        return [f"approval-execute-timeout:{task_id}:{req['id']}:timeout-{timeout_seconds}s"]
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
        decision = find_decision(req, db_path) or find_reaction_decision(req)
        if not decision:
            continue
        verdict, message_id, content = decision
        if dry_run:
            actions.append(f"dry-run:approval-{verdict}:{req['task_id']}:{rid}:message-{message_id}")
            continue
        req["decision_message_id"] = message_id
        req["decision_at"] = utcnow()
        req["decision_source"] = content
        notify_reaction_seen(config, req, verdict, content)
        if verdict == "approved_all":
            pending_ids = [str(r.get("id")) for r in pending_requests(state)]
            actions.append(f"approval-all-detected:{req['task_id']}:{rid}:message-{message_id}:count-{len(pending_ids)}")
            actions.extend(approve_tokens(config, state, pending_ids, dry_run=dry_run, decision_source=content))
            continue
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


def _float_config(config: dict[str, Any], key: str, default: float) -> float:
    try:
        return float(config.get(key, default))
    except Exception:
        return default


def sent_approval_message(actions: list[str]) -> bool:
    return any(
        a.startswith("approval-requested:")
        or a.startswith("approval-reminded:")
        or a.startswith("discord-sent-with-mention:")
        or a.startswith("discord-sent-fallback:")
        for a in actions
    )


def send_discord_notice(target: str | None, message: str) -> str:
    """Send a plain Discord status message without approval reactions."""
    if not target:
        return "discord-notice-skipped:no-target"
    channel_id, thread_id = _parse_discord_target(target)
    token = _load_discord_token()
    if not channel_id or not token:
        return "discord-notice-skipped:no-discord-target-or-token"
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
    status, body = _discord_api(token, "POST", f"/channels/{send_to}/messages", payload)
    if 200 <= status < 300:
        return f"discord-notice-sent:channel:{send_to}"
    return f"discord-notice-failed:{status}:{compact(body, 180)}"


def extract_action_request_id(action: str) -> str | None:
    parts = action.split(":")
    if len(parts) >= 3 and re.fullmatch(r"[0-9a-f]{8}", parts[2], flags=re.I):
        return parts[2]
    if len(parts) >= 4 and re.fullmatch(r"[0-9a-f]{8}", parts[3], flags=re.I):
        return parts[3]
    return None


def _reaction_emoji_from_source(source: str) -> str:
    m = re.search(r"discord-reaction:([^:]+):", source or "")
    return m.group(1) if m else "reaction"


def notify_reaction_seen(config: dict[str, Any], req: dict[str, Any], verdict: str, source: str) -> None:
    """Immediately acknowledge a reaction decision before running the approved command."""
    if not str(source or "").startswith("discord-reaction:"):
        return
    if req.get("reaction_seen_notice_at"):
        return
    target = config.get("approval_discord_target") or config.get("discord_target")
    mention = str(config.get("approval_mention") or "").strip()
    emoji = _reaction_emoji_from_source(source)
    rid = str(req.get("id") or "")
    task_id = str(req.get("task_id") or "")
    if verdict == "approved_all":
        action = "approving all pending ledger requests"
    elif verdict == "approved_always":
        action = "allowing this exact command always and executing it"
    elif verdict == "approved":
        action = "approving and executing this request"
    elif verdict == "denied":
        action = "denying this request"
    else:
        action = f"processing `{verdict}`"
    lines = [
        f"{mention}" if mention else "",
        f"👀 Saw `{emoji}` on approval token `{rid}` for `{task_id}` — {action} now.",
        f"Source: `{source}`",
    ]
    result = send_discord_notice(target, "\n".join(line for line in lines if line))
    req["reaction_seen_notice_at"] = utcnow()
    req["reaction_seen_notice_result"] = result


def notify_reaction_actions(config: dict[str, Any], actions: list[str], state: dict[str, Any]) -> None:
    """Tell Discord when a reaction was acted on, independent of Hermes bg watch notifications."""
    interesting = [
        a for a in actions
        if a.startswith("approval-executed:")
        or a.startswith("approval-execute-failed:")
        or a.startswith("approval-execute-timeout:")
        or a.startswith("approval-denied:")
        or a.startswith("approval-all-detected:")
    ]
    if not interesting:
        return
    reaction_related = False
    for action in interesting:
        rid = extract_action_request_id(action)
        req = (state.get("requests") or {}).get(rid or "") if rid else None
        source = str((req or {}).get("decision_source") or "")
        if action.startswith("approval-all-detected:") or source.startswith("discord-reaction:"):
            reaction_related = True
            break
    if not reaction_related:
        return
    target = config.get("approval_discord_target") or config.get("discord_target")
    mention = str(config.get("approval_mention") or "").strip()
    lines = [f"{mention}" if mention else "", "⚡ Reaction approval acted on:"]
    lines.extend(f"- `{a}`" for a in interesting[:8])
    if len(interesting) > 8:
        lines.append(f"- … {len(interesting) - 8} more")
    send_discord_notice(target, "\n".join(line for line in lines if line))


def post_send_poll_loop(
    config: dict[str, Any],
    state: dict[str, Any],
    db_path: Path,
    state_path: Path,
    dashboard_path: Path,
    queue_json_path: Path,
    dry_run: bool = False,
) -> list[str]:
    """After sending approval prompts, poll reactions frequently for a short window.

    Cron runs the liveness job every few minutes, but humans react immediately.
    This loop gives reaction-click approvals near-button latency without waiting
    for the next cron tick. It only runs after this invocation actually sent or
    resent an approval message.
    """
    interval = max(1.0, _float_config(config, "approval_post_send_poll_interval_seconds", 5.0))
    duration = max(0.0, _float_config(config, "approval_post_send_poll_duration_seconds", 120.0))
    if duration <= 0:
        return []
    deadline = time.time() + duration
    actions: list[str] = [f"approval-post-send-poll-start:interval-{int(interval)}s:duration-{int(duration)}s"]
    while time.time() < deadline:
        # Reload durable state every tick. Other approval paths (text reply,
        # ledger CLI, another watcher) may have already approved/denied/executed
        # a token; acting on a stale in-memory copy can duplicate side effects.
        if not dry_run:
            latest = load_json(state_path, state)
            if isinstance(latest, dict):
                state.clear()
                state.update(latest)
        if not any(req.get("status") == "requested" for req in (state.get("requests") or {}).values() if isinstance(req, dict)):
            break
        polled = poll(config, state, db_path, dry_run=dry_run)
        if polled:
            actions.extend(polled)
            actions.append(write_approval_dashboard(state, dashboard_path, queue_json_path))
            if not dry_run:
                save_json(state_path, state)
                notify_reaction_actions(config, polled, state)
            # A reaction click should act on the current tick; the sleep is only
            # spacing between scans, not a delay before first action.
        time.sleep(interval)
    actions.append("approval-post-send-poll-stop")
    return actions


def spawn_post_send_poll_watcher(config_path: Path, state_path: Path, db_path: Path, dashboard_path: Path, queue_json_path: Path) -> str:
    log_path = Path.home() / ".hermes/logs/pm_swarm_approval_cop_post_send_poll.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(Path(__file__)),
        str(config_path),
        "--poll-watch",
        "--state",
        str(state_path),
        "--db",
        str(db_path),
        "--dashboard",
        str(dashboard_path),
        "--queue-json",
        str(queue_json_path),
    ]
    with log_path.open("ab") as log:
        proc = subprocess.Popen(cmd, cwd=str(Path.cwd()), stdout=log, stderr=log, start_new_session=True)
    return f"approval-post-send-poll-spawned:pid-{proc.pid}:interval-5s:log-{log_path}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("config", type=Path)
    ap.add_argument("task_ids", nargs="*")
    ap.add_argument("--state", type=Path, default=DEFAULT_STATE)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--poll", action="store_true")
    ap.add_argument("--request", action="store_true")
    ap.add_argument("--approve", nargs="*", default=[])
    ap.add_argument("--deny", nargs="*", default=[])
    ap.add_argument("--clear", nargs="*", default=[])
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dashboard-only", action="store_true")
    ap.add_argument("--dashboard", type=Path, default=DEFAULT_DASHBOARD)
    ap.add_argument("--queue-json", type=Path, default=DEFAULT_QUEUE_JSON)
    ap.add_argument("--poll-watch", action="store_true", help="Poll reactions every configured interval for a short post-send window, then exit")
    ap.add_argument("--no-post-send-poll", action="store_true", help="Do not spawn the short post-send reaction poll watcher after sending prompts")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    config = load_json(args.config, {})
    state = load_json(args.state, {"requests": {}})
    actions: list[str] = []
    if args.poll_watch:
        actions.extend(post_send_poll_loop(config, state, args.db, args.state, args.dashboard, args.queue_json, dry_run=args.dry_run))
        actions.append(write_approval_dashboard(state, args.dashboard, args.queue_json))
        if not args.dry_run:
            save_json(args.state, state)
    elif args.list or args.dashboard_only:
        actions.append(write_approval_dashboard(state, args.dashboard, args.queue_json, generated_by="approval-cop-cli"))
        if args.list:
            pending = pending_requests(state)
            actions.extend(f"approval-pending:{r.get('task_id')}:{r.get('id')}" for r in pending)
    else:
        if args.clear:
            for token in args.clear:
                if token.lower() == "all":
                    for req in pending_requests(state):
                        actions.append(mark_request(state, str(req.get("id")), "cleared", "cleared by approval-ledger-cli"))
                else:
                    actions.append(mark_request(state, token, "cleared", "cleared by approval-ledger-cli"))
        if args.deny:
            for token in args.deny:
                if token.lower() == "all":
                    for req in pending_requests(state):
                        actions.append(mark_request(state, str(req.get("id")), "denied", "denied by approval-ledger-cli"))
                else:
                    actions.append(mark_request(state, token, "denied", "denied by approval-ledger-cli"))
        if args.approve:
            tokens = [str(req.get("id")) for req in pending_requests(state)] if args.approve == ["all"] else args.approve
            actions.extend(approve_tokens(config, state, tokens, dry_run=args.dry_run))
        if args.scan or (not args.poll and not args.request and not args.task_ids and not args.approve and not args.deny and not args.clear):
            actions.extend(scan(config, state, dry_run=args.dry_run))
        if args.request or args.task_ids:
            for task_id in args.task_ids:
                actions.extend(request_for_task(config, state, task_id, dry_run=args.dry_run))
        if args.poll or (not args.request and not args.task_ids and not args.approve and not args.deny and not args.clear):
            actions.extend(poll(config, state, args.db, dry_run=args.dry_run))
        actions.append(write_approval_dashboard(state, args.dashboard, args.queue_json))
        if sent_approval_message(actions) and not args.no_post_send_poll:
            if not args.dry_run:
                save_json(args.state, state)
                actions.append(spawn_post_send_poll_watcher(args.config, args.state, args.db, args.dashboard, args.queue_json))
            else:
                actions.extend(post_send_poll_loop(config, state, args.db, args.state, args.dashboard, args.queue_json, dry_run=True))
            actions.append(write_approval_dashboard(state, args.dashboard, args.queue_json))
    if not args.dry_run:
        save_json(args.state, state)
    # Watchdog pattern: stay quiet unless there is a new actionable event.
    interesting = [a for a in actions if not (a.startswith("approval-cop-existing") or a.startswith("approval-cop-skipped"))]
    if interesting:
        print("\n".join(interesting))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
