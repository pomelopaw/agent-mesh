#!/usr/bin/env python3
"""
Agent Mesh MCP Server (v1.0.0)
Speaks newline-delimited JSON-RPC 2.0 over UTF-8 stdin/stdout with zero external dependencies.
Dispatches tool calls concurrently on a thread pool so long-running wait calls never block
UI interactions, inbox replies, or task cancellation.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sys
import threading
from pathlib import Path
from typing import Any

# Ensure sibling mesh_core package is importable regardless of current working directory
sys.path.insert(0, str(Path(__file__).resolve().parent))
import mesh_core as engine  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "agent-mesh", "version": engine.VERSION}

ACTIVE_PROJECT_DIR: str = ""
_STDOUT_LOCK = threading.Lock()


def resolve_text_input(text: str | None, file_path: str | None, label: str) -> str:
    if text and text.strip():
        return text.strip()
    if file_path and file_path.strip():
        safe_path = engine.resolve_safe_project_file(
            file_path=file_path.strip(),
            active_project_dir=ACTIVE_PROJECT_DIR,
            label=label,
        )
        content = safe_path.read_text(encoding="utf-8", errors="replace").strip()
        if not content:
            raise RuntimeError(f"{label}_file is empty: {safe_path.name}")
        return content
    raise RuntimeError(f"Provide either '{label}' or '{label}_file'.")


TOOLS: list[dict[str, Any]] = [
    {
        "name": "mesh_health",
        "description": "Check Agent Mesh engine status, active workspace directory, Codex and optional Antigravity connectivity, pending relay inbox count, and counts of configured projects and models.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
    {
        "name": "mesh_list_projects",
        "description": "List all configured collaboration projects and every AI collaborator attached to each project (API models, Codex, desktop assistant instances, and Antigravity).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
    {
        "name": "mesh_discover_projects",
        "description": "Discover local workspace projects, Codex projects and models, available desktop assistant profiles, and optional Antigravity projects for the panel UI.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "refresh": {"type": "boolean", "description": "Set true to rescan all sources bypassing cache."},
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "mesh_save_project",
        "description": "Create or update a collaboration project with a primary controller profile and one or more selected AI collaborators.",
        "inputSchema": {
            "type": "object",
            "required": ["key", "collaborators"],
            "properties": {
                "key": {"type": "string", "description": "Unique project key."},
                "display_name": {"type": "string", "description": "Friendly display name."},
                "workspace_path": {"type": "string", "description": "Workspace folder path."},
                "controller": {"type": "object", "description": "Optional primary controller metadata."},
                "collaborators": {
                    "type": "array",
                    "description": "List of collaborator entries (openai_compat, codex, antigravity, or desktop assistant profile).",
                    "items": {"type": "object"},
                },
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "mesh_bind_project",
        "description": "Quickly bind a project key to an Antigravity project UUID.",
        "inputSchema": {
            "type": "object",
            "required": ["key", "project_id"],
            "properties": {
                "key": {"type": "string", "description": "Short project identifier."},
                "project_id": {"type": "string", "description": "Target Antigravity project UUID."},
                "display_name": {"type": "string", "description": "Optional friendly project name."},
                "workspace_path": {"type": "string", "description": "Optional workspace folder path."},
                "default_model": {"type": "string", "description": "Optional default model tier or ID."},
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "mesh_unbind_project",
        "description": "Remove a saved collaboration project by its key.",
        "inputSchema": {
            "type": "object",
            "required": ["key"],
            "properties": {
                "key": {"type": "string", "description": "Project key to remove."},
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "mesh_list_models",
        "description": "List saved reusable API model configurations (without exposing secret keys).",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
    {
        "name": "mesh_save_model",
        "description": "Create or update a reusable API model configuration with HTTPS or local loopback base URL, model ID, thinking level, and optional API key.",
        "inputSchema": {
            "type": "object",
            "required": ["api_base", "model_id"],
            "properties": {
                "key": {"type": "string", "description": "Optional unique model key."},
                "name": {"type": "string", "description": "Friendly model display name."},
                "api_base": {"type": "string", "description": "API base URL (https:// or http://127.0.0.1 / localhost)."},
                "model_id": {"type": "string", "description": "Model identifier sent to the API."},
                "api_key": {"type": "string", "description": "Optional API key (leave empty when editing to keep existing key)."},
                "thinking_level": {
                    "type": "string",
                    "enum": ["", "minimal", "low", "medium", "high", "xhigh"],
                    "description": "Reasoning or thinking effort level.",
                },
                "system_prompt": {"type": "string", "description": "Optional default system instructions."},
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "mesh_delete_model",
        "description": "Delete a saved API model configuration by key.",
        "inputSchema": {
            "type": "object",
            "required": ["key"],
            "properties": {
                "key": {"type": "string", "description": "Model key to remove."},
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"visibility": ["app"]}},
    },
    {
        "name": "mesh_create_conversation",
        "description": "Start a new background task conversation with a collaborator attached to a project without touching the mouse or window focus. Use 'target' to choose a specific collaborator when a project has multiple AI members.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Concise task prompt (use prompt_file for prompts longer than 3000 bytes)."},
                "prompt_file": {"type": "string", "description": "Relative path inside the active workspace folder to a UTF-8 file containing a longer prompt (max 512 KiB)."},
                "title": {"type": "string", "description": "Short task conversation title."},
                "project": {"type": "string", "description": "Optional project key. Omit to auto-match the active project directory."},
                "target": {"type": "string", "description": "Optional collaborator selector when a project has multiple collaborators (e.g. 'codex', model name/key, 'antigravity', or '#2')."},
                "project_id": {"type": "string", "description": "Optional explicit Antigravity project UUID override."},
                "model": {"type": "string", "description": "Optional model override."},
                "profile": {"type": "string", "description": "Optional profile name."},
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
    {
        "name": "mesh_send_message",
        "description": "Send a follow-up message to an existing conversation ID. Call mesh_wait_reply with after_step set to previous_step_index to wait for the new reply.",
        "inputSchema": {
            "type": "object",
            "required": ["conversation_id"],
            "properties": {
                "conversation_id": {"type": "string", "description": "Target conversation UUID."},
                "message": {"type": "string", "description": "Follow-up message text (use message_file for messages longer than 3000 bytes)."},
                "message_file": {"type": "string", "description": "Relative path inside the active workspace folder to a UTF-8 file containing a longer message (max 512 KiB)."},
                "title": {"type": "string", "description": "Optional short message title."},
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
    {
        "name": "mesh_wait_reply",
        "description": "Wait for the latest reply on a conversation. Returns when the turn completes, pauses for approval or relay, is cancelled, or reaches timeout_seconds.",
        "inputSchema": {
            "type": "object",
            "required": ["conversation_id"],
            "properties": {
                "conversation_id": {"type": "string", "description": "Target conversation UUID."},
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120, "description": "Max seconds to wait before returning current progress (default 25)."},
                "after_step": {"type": "integer", "description": "Optional step index from previous_step_index to wait for a newer turn."},
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
    {
        "name": "mesh_cancel_conversation",
        "description": "Cancel a currently running background conversation task and terminate its active worker process or request.",
        "inputSchema": {
            "type": "object",
            "required": ["conversation_id"],
            "properties": {
                "conversation_id": {"type": "string", "description": "Target conversation UUID to cancel."},
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
    {
        "name": "mesh_get_history",
        "description": "Read full recent turns, reasoning, execution status, and latest reply of a conversation.",
        "inputSchema": {
            "type": "object",
            "required": ["conversation_id"],
            "properties": {
                "conversation_id": {"type": "string", "description": "Target conversation UUID."},
                "last_n": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Number of recent turns to return (default 30)."},
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
    {
        "name": "mesh_list_conversations",
        "description": "List collaboration conversations with optional project, date (YYYY-MM-DD), collaborator, search query, and pagination (default 10 per page).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Optional project key filter."},
                "date": {"type": "string", "description": "Optional date filter in YYYY-MM-DD format."},
                "collaborator": {"type": "string", "description": "Optional collaborator ID, type, or model filter."},
                "q": {"type": "string", "description": "Optional keyword search across titles and previews."},
                "page": {"type": "integer", "minimum": 1, "description": "Page number starting at 1."},
                "page_size": {"type": "integer", "minimum": 1, "maximum": 100, "description": "Items per page (default 10)."},
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
    {
        "name": "mesh_check_inbox",
        "description": "Check pending collaboration invitations waiting for a desktop assistant profile to pick up and reply.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "account": {"type": "string", "description": "Optional instance number or label filter (e.g. '#2')."},
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
    {
        "name": "mesh_reply_inbox",
        "description": "Submit a reply to a pending collaboration invitation conversation so the requesting assistant receives the answer.",
        "inputSchema": {
            "type": "object",
            "required": ["conversation_id", "reply"],
            "properties": {
                "conversation_id": {"type": "string", "description": "Pending invitation conversation UUID."},
                "reply": {"type": "string", "description": "Reply content to send back."},
                "responder": {"type": "string", "description": "Optional responder label."},
            },
            "additionalProperties": False,
        },
        "_meta": {"ui": {"visibility": ["model", "app"]}},
    },
]


def handle_tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "mesh_health":
        return engine.get_health(ACTIVE_PROJECT_DIR)

    if name == "mesh_list_projects":
        return engine.list_projects(ACTIVE_PROJECT_DIR)

    if name == "mesh_discover_projects":
        return engine.discover_resources(
            force_refresh=bool(args.get("refresh")),
            active_project_dir=ACTIVE_PROJECT_DIR,
        )

    if name == "mesh_save_project":
        return engine.save_project(args, active_project_dir=ACTIVE_PROJECT_DIR)

    if name == "mesh_bind_project":
        return engine.save_project(
            {
                "key": args["key"],
                "project_id": args["project_id"],
                "display_name": args.get("display_name") or args["key"],
                "workspace_path": args.get("workspace_path") or ACTIVE_PROJECT_DIR,
                "default_model": args.get("default_model") or "",
            },
            active_project_dir=ACTIVE_PROJECT_DIR,
        )

    if name == "mesh_unbind_project":
        return engine.delete_project(str(args["key"]))

    if name == "mesh_list_models":
        return engine.list_models()

    if name == "mesh_save_model":
        return engine.save_model(args)

    if name == "mesh_delete_model":
        return engine.delete_model(str(args["key"]))

    if name == "mesh_create_conversation":
        prompt = resolve_text_input(args.get("prompt"), args.get("prompt_file"), "prompt")
        return engine.create_conversation(
            prompt=prompt,
            title=args.get("title"),
            project=args.get("project"),
            target=args.get("target"),
            project_id=args.get("project_id"),
            project_dir=ACTIVE_PROJECT_DIR or None,
            model=args.get("model"),
            profile=args.get("profile"),
        )

    if name == "mesh_send_message":
        conv_id = engine.validate_conversation_id(str(args["conversation_id"]))
        message = resolve_text_input(args.get("message"), args.get("message_file"), "message")
        return engine.send_message(
            conversation_id=conv_id,
            message=message,
            title=args.get("title"),
            project_dir=ACTIVE_PROJECT_DIR or None,
        )

    if name == "mesh_wait_reply":
        return engine.wait_reply(
            conversation_id=engine.validate_conversation_id(str(args["conversation_id"])),
            timeout_seconds=int(args.get("timeout_seconds", 25)),
            after_step=int(args.get("after_step", -1)),
        )

    if name == "mesh_cancel_conversation":
        return engine.cancel_conversation(
            conversation_id=engine.validate_conversation_id(str(args["conversation_id"])),
        )

    if name == "mesh_get_history":
        return engine.get_history(
            conversation_id=engine.validate_conversation_id(str(args["conversation_id"])),
            last_n=int(args.get("last_n", 30)),
        )

    if name == "mesh_list_conversations":
        return engine.list_conversations(
            project=args.get("project"),
            date=args.get("date"),
            collaborator=args.get("collaborator"),
            q=args.get("q"),
            page=int(args.get("page", 1)),
            page_size=int(args.get("page_size", 10)),
        )

    if name == "mesh_check_inbox":
        return engine.check_inbox(account=args.get("account"))

    if name == "mesh_reply_inbox":
        return engine.reply_inbox(
            conversation_id=engine.validate_conversation_id(str(args["conversation_id"])),
            reply_text=str(args["reply"]),
            responder=str(args.get("responder") or ""),
        )

    raise RuntimeError(f"Unknown tool: {name}")


def send_json(obj: dict[str, Any]) -> None:
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with _STDOUT_LOCK:
        sys.stdout.write(line)
        sys.stdout.flush()


def _execute_tool_call(req_id: Any, tool_name: str, tool_args: dict[str, Any]) -> None:
    try:
        result = handle_tool_call(tool_name, tool_args)
        send_json(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, indent=2)}],
                    "isError": False,
                },
            }
        )
    except Exception as exc:
        send_json(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {exc}"}],
                    "isError": True,
                },
            }
        )


def handle_request(msg: dict[str, Any], executor: concurrent.futures.ThreadPoolExecutor) -> None:
    req_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}

    if req_id is None:
        return

    if method == "initialize":
        send_json(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                },
            }
        )
        return

    if method == "ping":
        send_json({"jsonrpc": "2.0", "id": req_id, "result": {}})
        return

    if method == "tools/list":
        send_json({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
        return

    if method == "tools/call":
        tool_name = str(params.get("name") or "")
        tool_args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        executor.submit(_execute_tool_call, req_id, tool_name, tool_args)
        return

    send_json(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }
    )


def main() -> None:
    global ACTIVE_PROJECT_DIR
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project-dir", default="")
    known, _ = parser.parse_known_args()
    ACTIVE_PROJECT_DIR = known.project_dir.strip()
    if ACTIVE_PROJECT_DIR == "${PROJECT_DIR}":
        ACTIVE_PROJECT_DIR = ""

    engine.ensure_dirs()

    with concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="mesh-rpc") as executor:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError as exc:
                sys.stderr.write(f"Invalid JSON-RPC input: {exc}\n")
                sys.stderr.flush()
                continue
            if isinstance(msg, dict):
                handle_request(msg, executor)


if __name__ == "__main__":
    main()
