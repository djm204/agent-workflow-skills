from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any

from hermes_cli import kanban_db as kb

BLACKBOARD_PREFIX = "[pm-swarm:blackboard] "
DEFAULT_PM_SKILLS = ["kanban-orchestrator"]
DEFAULT_VERIFIER_SKILLS = ["requesting-code-review"]
DEFAULT_SYNTHESIZER_SKILLS = ["humanizer"]


@dataclass(frozen=True)
class WorkerSpec:
    profile: str
    title: str
    body: str = ""
    skills: list[str] = field(default_factory=list)
    priority: int = 0
    max_runtime_seconds: int | None = None


@dataclass(frozen=True)
class PmSpec:
    profile: str
    title: str = ""
    body: str = ""
    skills: list[str] = field(default_factory=lambda: list(DEFAULT_PM_SKILLS))
    priority: int = 0
    max_runtime_seconds: int | None = None


def _require_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def _positive_int(value: Any, field_name: str, default: int) -> int:
    if value is None:
        value = default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a positive integer") from None
    if number < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return number


def _skills(value: Any, default: list[str] | None = None) -> list[str]:
    if value is None:
        return list(default or [])
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return [str(part).strip() for part in value if str(part).strip()]


def _optional_positive_int(value: Any, field_name: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a positive integer") from None
    if number < 1:
        raise ValueError(f"{field_name} must be a positive integer")
    return number


_SKILL_LIST_RE = re.compile(r"^[A-Za-z0-9_.-]+(?:,[A-Za-z0-9_.-]+)*$")


def _split_profile_title_skills(value: str, *, field_name: str, require_title: bool) -> tuple[str, str, list[str]]:
    """Parse compact CLI specs while preserving normal colons in titles.

    The historical form is ``profile:title[:skill,skill]``. Titles also often
    contain colons (for example ``dev:API: add retries``), so only treat the
    final colon-delimited segment as a skills suffix when it is an unambiguous
    comma-separated skill token list with no whitespace. Users who need a
    single skill can still use ``profile:title:skill-name``; colon-containing
    prose titles keep their colons.
    """
    profile, sep, rest = value.partition(":")
    if not sep:
        if require_title:
            raise ValueError(f"{field_name} must be profile:title or profile:title:skill,skill")
        return _require_text(profile, f"{field_name}.profile"), "", []

    title = rest.strip()
    skills: list[str] = []
    if ":" in rest:
        maybe_title, maybe_skills = rest.rsplit(":", 1)
        maybe_skills = maybe_skills.strip()
        if maybe_skills and _SKILL_LIST_RE.fullmatch(maybe_skills):
            title = maybe_title.strip()
            skills = _skills(maybe_skills)

    if require_title:
        _require_text(title, f"{field_name}.title")
    return _require_text(profile, f"{field_name}.profile"), title, skills


def parse_worker(value: str | dict[str, Any]) -> WorkerSpec:
    if isinstance(value, str):
        profile, title, skills = _split_profile_title_skills(value, field_name="worker", require_title=True)
        return WorkerSpec(
            profile=profile,
            title=title,
            body=title,
            skills=skills,
        )
    return WorkerSpec(
        profile=_require_text(value.get("profile"), "worker.profile"),
        title=_require_text(value.get("title"), "worker.title"),
        body=str(value.get("body") or value.get("title") or ""),
        skills=_skills(value.get("skills")),
        priority=int(value.get("priority") or 0),
        max_runtime_seconds=_optional_positive_int(value.get("max_runtime_seconds"), "worker.max_runtime_seconds"),
    )


def parse_pm(value: str | dict[str, Any]) -> PmSpec:
    if isinstance(value, str):
        profile, title, skills = _split_profile_title_skills(value, field_name="pm", require_title=False)
        return PmSpec(
            profile=profile,
            title=title,
            body=title,
            skills=skills or list(DEFAULT_PM_SKILLS),
        )
    return PmSpec(
        profile=_require_text(value.get("profile"), "pm.profile"),
        title=str(value.get("title") or ""),
        body=str(value.get("body") or value.get("title") or ""),
        skills=_skills(value.get("skills"), DEFAULT_PM_SKILLS),
        priority=int(value.get("priority") or 0),
        max_runtime_seconds=_optional_positive_int(value.get("max_runtime_seconds"), "pm.max_runtime_seconds"),
    )


def _swarm_context(root_id: str, goal: str) -> str:
    return (
        "\n\n## PM Swarm protocol\n"
        f"- Root/shared blackboard card: `{root_id}`.\n"
        "- Use Kanban comments on the root for cross-agent coordination.\n"
        "- Put machine-readable facts in completion metadata.\n"
        "- Do not duplicate sibling work; check parent/sibling handoffs first.\n"
        f"- Goal: {goal.strip()}\n"
    )


def _pm_context(root_id: str, goal: str, shard_index: int, capacity: int) -> str:
    return (
        _swarm_context(root_id, goal)
        + "\n## PM shard protocol\n"
        + f"- You are PM shard {shard_index}; own at most {capacity} worker tasks.\n"
        + "- Coordinate worker scope, prevent overlap, and post shard planning instructions on the root blackboard.\n"
        + "- Complete after your shard plan/instructions are posted so dependent workers can start; do not wait for worker handoffs before completing.\n"
    )


def _worker_context(root_id: str, pm_id: str, goal: str) -> str:
    return (
        _swarm_context(root_id, goal)
        + "\n## PM ownership\n"
        + f"- Your PM card is `{pm_id}`. Check it for shard instructions and handoffs.\n"
        + "- Coordinate through your PM and root blackboard before expanding scope.\n"
    )


def _chunks(items: list[WorkerSpec], size: int) -> list[list[WorkerSpec]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _selected_pms(pm_specs: list[PmSpec], count: int) -> list[PmSpec]:
    if not pm_specs:
        raise ValueError("at least one pm profile is required")
    return [pm_specs[i % len(pm_specs)] for i in range(count)]


def _child_key(root_key: str | None, suffix: str) -> str | None:
    return f"{root_key}:{suffix}" if root_key else None


def _profile_author() -> str:
    import os
    for env in ("HERMES_PROFILE_NAME", "HERMES_PROFILE"):
        value = os.environ.get(env)
        if value:
            return value
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "pm-swarm"
    except Exception:
        return "pm-swarm"


def _post_blackboard(conn, root_id: str, *, author: str, key: str, value: Any) -> int:
    payload = json.dumps({"key": key, "value": value}, ensure_ascii=False, sort_keys=True)
    return kb.add_comment(conn, root_id, author=author, body=BLACKBOARD_PREFIX + payload)


def latest_blackboard(conn, root_id: str) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    authors: dict[str, str] = {}
    for comment in kb.list_comments(conn, root_id):
        body = comment.body or ""
        if not body.startswith(BLACKBOARD_PREFIX):
            continue
        try:
            payload = json.loads(body[len(BLACKBOARD_PREFIX):])
        except json.JSONDecodeError:
            continue
        key = payload.get("key")
        if isinstance(key, str) and key:
            merged[key] = payload.get("value")
            authors[key] = comment.author
    if authors:
        merged["_authors"] = authors
    return merged


def create_pm_swarm_from_args(args: dict[str, Any]) -> dict[str, Any]:
    goal = _require_text(args.get("goal"), "goal")
    workers = [parse_worker(item) for item in (args.get("workers") or [])]
    pm_specs = [parse_pm(item) for item in (args.get("pm_profiles") or args.get("pms") or [])]
    verifier = _require_text(args.get("verifier"), "verifier")
    synthesizer = _require_text(args.get("synthesizer"), "synthesizer")
    capacity = _positive_int(args.get("pm_capacity"), "pm_capacity", 5)
    if not workers:
        raise ValueError("at least one worker is required")
    requested_workspace_kind = str(args.get("workspace_kind") or "scratch")
    if requested_workspace_kind == "scratch" and args.get("workspace_path"):
        raise ValueError("workspace_path cannot be shared across a PM swarm scratch workspace; use workspace_kind=dir for a shared directory or omit workspace_path")
    shards = _chunks(workers, capacity)
    selected_pms = _selected_pms(pm_specs, len(shards))
    board = args.get("board") or None
    if board and not kb.board_exists(str(board)):
        raise ValueError(f"unknown board {board!r}; create it first or omit --board")

    scope = kb.scoped_current_board(str(board)) if board else None
    if scope is None:
        class _NullScope:
            def __enter__(self): return None
            def __exit__(self, exc_type, exc, tb): return False
        scope = _NullScope()

    with scope:
        kb.init_db()
        with kb.connect_closing() as conn:
            created_by = str(args.get("created_by") or _profile_author())
            tenant = args.get("tenant") or None
            priority = int(args.get("priority") or 0)
            workspace_kind = str(args.get("workspace_kind") or "scratch")
            workspace_path = args.get("workspace_path") or None
            idempotency_key = args.get("idempotency_key") or None
            root_key = str(idempotency_key) if idempotency_key else None

            root = kb.create_task(
                conn,
                title=f"PM Swarm: {goal.splitlines()[0][:80]}",
                body=(
                    "PM Swarm planning/root card. This card is completed immediately so PM shards can start "
                    "while it remains the shared blackboard and audit anchor.\n\n"
                    f"Goal:\n{goal}"
                ),
                assignee=created_by,
                created_by=created_by,
                tenant=tenant,
                priority=priority,
                idempotency_key=root_key,
                workspace_kind=workspace_kind,
                workspace_path=workspace_path,
                skills=DEFAULT_PM_SKILLS,
            )

            existing = latest_blackboard(conn, root).get("topology")
            if isinstance(existing, dict) and existing.get("kind") == "pm_swarm_v1":
                return dict(existing)

            root_task = kb.get_task(conn, root)
            if root_task is None:
                raise ValueError(f"unknown root task {root}")
            if root_task.status != "done":
                kb.complete_task(
                    conn,
                    root,
                    summary="PM swarm topology planned; root remains the shared blackboard.",
                    metadata={
                        "kind": "pm_swarm_v1",
                        "goal": goal,
                        "worker_count": len(workers),
                        "pm_count": len(shards),
                        "pm_capacity": capacity,
                    },
                )

            pm_ids: list[str] = []
            worker_ids: list[str] = []
            assignments: dict[str, list[str]] = {}
            worker_index = 0
            for shard_index, (pm_spec, shard) in enumerate(zip(selected_pms, shards), start=1):
                pm_title = pm_spec.title or f"PM shard {shard_index}: {goal.splitlines()[0][:70]}"
                pm_body = (
                    (pm_spec.body or "Coordinate this worker shard.")
                    + _pm_context(root, goal, shard_index, capacity)
                    + "\nAssigned worker titles:\n"
                    + "\n".join(f"- {worker.title}" for worker in shard)
                )
                pm_id = kb.create_task(
                    conn,
                    title=pm_title,
                    body=pm_body,
                    assignee=pm_spec.profile,
                    created_by=created_by,
                    parents=[root],
                    tenant=tenant,
                    priority=pm_spec.priority or priority,
                    workspace_kind=workspace_kind,
                    workspace_path=workspace_path,
                    skills=pm_spec.skills or DEFAULT_PM_SKILLS,
                    max_runtime_seconds=pm_spec.max_runtime_seconds,
                    idempotency_key=_child_key(root_key, f"pm:{shard_index}"),
                )
                pm_ids.append(pm_id)
                assignments[pm_id] = []
                for worker in shard:
                    worker_index += 1
                    worker_id = kb.create_task(
                        conn,
                        title=worker.title,
                        body=(worker.body or worker.title) + _worker_context(root, pm_id, goal),
                        assignee=worker.profile,
                        created_by=created_by,
                        parents=[pm_id],
                        tenant=tenant,
                        priority=worker.priority or priority,
                        workspace_kind=workspace_kind,
                        workspace_path=workspace_path,
                        skills=worker.skills or None,
                        max_runtime_seconds=worker.max_runtime_seconds,
                        idempotency_key=_child_key(root_key, f"worker:{worker_index}"),
                    )
                    worker_ids.append(worker_id)
                    assignments[pm_id].append(worker_id)

            verifier_body = (
                "Review every PM shard handoff, worker handoff, and blackboard update. "
                "Gate the PM swarm: complete only with metadata {\"gate\": \"pass\"} "
                "when evidence is sufficient; otherwise block with exact missing work."
                + _swarm_context(root, goal)
            )
            verifier_id = kb.create_task(
                conn,
                title="Verify PM swarm outputs",
                body=verifier_body,
                assignee=verifier,
                created_by=created_by,
                parents=pm_ids + worker_ids,
                tenant=tenant,
                priority=priority,
                workspace_kind=workspace_kind,
                workspace_path=workspace_path,
                skills=DEFAULT_VERIFIER_SKILLS,
                idempotency_key=_child_key(root_key, "verifier"),
            )

            synthesizer_id = kb.create_task(
                conn,
                title="Synthesize PM swarm outputs",
                body=(
                    "Synthesize the verified PM-swarm outputs into the final deliverable. "
                    "Do not start until the verifier has passed the gate."
                    + _swarm_context(root, goal)
                ),
                assignee=synthesizer,
                created_by=created_by,
                parents=[verifier_id],
                tenant=tenant,
                priority=priority,
                workspace_kind=workspace_kind,
                workspace_path=workspace_path,
                skills=DEFAULT_SYNTHESIZER_SKILLS,
                idempotency_key=_child_key(root_key, "synthesizer"),
            )

            result = {
                "kind": "pm_swarm_v1",
                "root_id": root,
                "pm_ids": pm_ids,
                "worker_ids": worker_ids,
                "verifier_id": verifier_id,
                "synthesizer_id": synthesizer_id,
                "assignments": assignments,
                "goal": goal,
                "pm_capacity": capacity,
                "board": kb.get_current_board(),
            }
            _post_blackboard(conn, root, author=created_by, key="topology", value=result)
            return result


