"""User-local PM Swarm plugin for Hermes.

This plugin intentionally lives under ~/.hermes/plugins so it survives Hermes
source updates. It builds on the built-in Kanban board/dispatcher rather than
modifying Hermes core or creating a second scheduler.
"""
from __future__ import annotations

import argparse
import json
import shlex
from typing import Any

from .core import create_pm_swarm_from_args


class _SlashArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        if status:
            raise ValueError((message or f"argument parsing failed with status {status}").strip())
        raise ValueError((message or "argument parsing exited").strip())


PM_SWARM_SCHEMA = {
    "name": "pm_swarm_create",
    "description": (
        "Create a persistent PM-led Kanban swarm inside this Hermes instance. "
        "Use for large multi-agent work where a top-level orchestrator wants "
        "durable PM cards supervising worker cards. Default topology is one PM "
        "per five workers; state lives in Hermes Kanban, so gateway/dispatcher/" 
        "dashboard/kanban commands keep working."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {"type": "string", "description": "Overall mission/final outcome."},
            "workers": {
                "type": "array",
                "description": "Worker specs. Each item may be an object or 'profile:title[:skill,skill]'.",
                "items": {
                    "oneOf": [
                        {"type": "string"},
                        {
                            "type": "object",
                            "properties": {
                                "profile": {"type": "string"},
                                "title": {"type": "string"},
                                "body": {"type": "string"},
                                "skills": {"type": "array", "items": {"type": "string"}},
                                "priority": {"type": "integer"},
                                "max_runtime_seconds": {"type": "integer"},
                            },
                            "required": ["profile", "title"],
                        },
                    ]
                },
            },
            "pm_profiles": {
                "type": "array",
                "description": "PM specs. Strings use 'profile[:title[:skill,skill]]'. Profiles repeat round-robin if fewer than shards.",
                "items": {
                    "oneOf": [
                        {"type": "string"},
                        {
                            "type": "object",
                            "properties": {
                                "profile": {"type": "string"},
                                "title": {"type": "string"},
                                "body": {"type": "string"},
                                "skills": {"type": "array", "items": {"type": "string"}},
                                "priority": {"type": "integer"},
                                "max_runtime_seconds": {"type": "integer"},
                            },
                            "required": ["profile"],
                        },
                    ]
                },
            },
            "verifier": {"type": "string", "description": "Profile assigned to verification gate."},
            "synthesizer": {"type": "string", "description": "Profile assigned to final synthesis."},
            "pm_capacity": {"type": "integer", "description": "Workers per PM shard. Default 5."},
            "tenant": {"type": "string"},
            "board": {"type": "string", "description": "Optional Kanban board slug."},
            "created_by": {"type": "string"},
            "idempotency_key": {"type": "string", "description": "Dedup key to avoid duplicate topology creation."},
            "priority": {"type": "integer"},
            "workspace_kind": {"type": "string", "enum": ["scratch", "dir", "worktree"]},
            "workspace_path": {"type": "string"},
        },
        "required": ["goal", "workers", "pm_profiles", "verifier", "synthesizer"],
    },
}


def _tool_handler(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        return json.dumps({"success": True, "data": create_pm_swarm_from_args(args)}, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001 - plugin tool must return JSON, not crash host
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)


def _setup_cli(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("goal", help="Overall PM swarm goal / final outcome")
    parser.add_argument(
        "--worker",
        action="append",
        default=[],
        metavar="PROFILE:TITLE[:SKILL,SKILL]",
        help="Worker card (repeatable)",
    )
    parser.add_argument(
        "--pm",
        action="append",
        default=[],
        metavar="PROFILE[:TITLE[:SKILL,SKILL]]",
        help="PM profile/card spec (repeatable; profiles are reused round-robin if needed)",
    )
    parser.add_argument("--verifier", required=True, help="Verifier profile")
    parser.add_argument("--synthesizer", required=True, help="Synthesizer profile")
    parser.add_argument("--pm-capacity", type=int, default=5, help="Workers per PM shard (default: 5)")
    parser.add_argument("--tenant", default=None)
    parser.add_argument("--board", default=None)
    parser.add_argument("--created-by", default=None)
    parser.add_argument("--idempotency-key", default=None)
    parser.add_argument("--priority", type=int, default=0)
    parser.add_argument("--workspace-kind", default="scratch", choices=["scratch", "dir", "worktree"])
    parser.add_argument("--workspace-path", default=None)
    parser.add_argument("--json", action="store_true", help="Emit JSON output")


def _cli_handler(args: argparse.Namespace) -> int:
    payload = {
        "goal": args.goal,
        "workers": args.worker,
        "pm_profiles": args.pm,
        "verifier": args.verifier,
        "synthesizer": args.synthesizer,
        "pm_capacity": args.pm_capacity,
        "tenant": args.tenant,
        "board": args.board,
        "created_by": args.created_by,
        "idempotency_key": args.idempotency_key,
        "priority": args.priority,
        "workspace_kind": args.workspace_kind,
        "workspace_path": args.workspace_path,
    }
    try:
        result = create_pm_swarm_from_args(payload)
    except Exception as exc:  # noqa: BLE001 - CLI should print clean error
        print(f"pm-swarm: {exc}")
        return 1
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(f"PM swarm root: {result['root_id']}")
        print("PMs: " + ", ".join(result["pm_ids"]))
        for pm_id, worker_ids in result["assignments"].items():
            print(f"  {pm_id}: " + ", ".join(worker_ids))
        print("Workers: " + ", ".join(result["worker_ids"]))
        print(f"Verifier: {result['verifier_id']}")
        print(f"Synthesizer: {result['synthesizer_id']}")
    return 0


def _slash_handler(raw_args: str) -> str:
    raw = (raw_args or "").strip()
    if not raw:
        return (
            "Usage: /pm-swarm '{\"goal\": \"...\", \"workers\": [...], "
            "\"pm_profiles\": [\"pm\"], \"verifier\": \"reviewer\", "
            "\"synthesizer\": \"writer\"}'"
        )
    try:
        if (
            len(raw) >= 2
            and raw[0] == raw[-1]
            and raw[0] in {"'", '"'}
            and raw[1:-1].lstrip().startswith("{")
        ):
            raw = raw[1:-1].strip()
        if raw.startswith("{"):
            payload = json.loads(raw)
        else:
            # Lightweight convenience wrapper around the CLI grammar.
            tokens = shlex.split(raw)
            parser = _SlashArgumentParser(prog="/pm-swarm", add_help=False)
            _setup_cli(parser)
            ns = parser.parse_args(tokens)
            payload = {
                "goal": ns.goal,
                "workers": ns.worker,
                "pm_profiles": ns.pm,
                "verifier": ns.verifier,
                "synthesizer": ns.synthesizer,
                "pm_capacity": ns.pm_capacity,
                "tenant": ns.tenant,
                "board": ns.board,
                "created_by": ns.created_by,
                "idempotency_key": ns.idempotency_key,
                "priority": ns.priority,
                "workspace_kind": ns.workspace_kind,
                "workspace_path": ns.workspace_path,
            }
        result = create_pm_swarm_from_args(payload)
    except Exception as exc:  # noqa: BLE001
        return f"pm-swarm error: {exc}"
    return json.dumps(result, indent=2, ensure_ascii=False)


def register(ctx):
    ctx.register_tool(
        name="pm_swarm_create",
        toolset="pm-swarm",
        schema=PM_SWARM_SCHEMA,
        handler=_tool_handler,
        description="Create persistent PM-led Kanban swarms",
        emoji="🧭",
    )
    ctx.register_cli_command(
        name="pm-swarm",
        help="Create a persistent PM-led Kanban swarm",
        setup_fn=_setup_cli,
        handler_fn=_cli_handler,
        description="Create durable PM + worker + verifier + synthesizer Kanban topology",
    )
    ctx.register_command(
        name="pm-swarm",
        handler=_slash_handler,
        description="Create a persistent PM-led Kanban swarm",
        args_hint="<json-or-cli-args>",
    )

