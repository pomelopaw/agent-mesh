"""
Multi-instance desktop assistant collaborator provider adapter.
Supports:
  1. Direct CLI execution (shell=False, strict account_id validation, neutral env
     AGENT_MESH_CLAUDE_CONFIG_ROOT) with workspace_path, model, thinking_level,
     and session resume.
  2. Relay Inbox mode (`waiting_relay`) out of the box when direct CLI is unavailable
     or the target profile is desktop-only.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

from ..discovery import (
    list_local_desktop_instances,
    resolve_claude_cli,
    resolve_claude_config_root,
)
from ..security import assert_within_dir, validate_account_id, validate_conversation_id
from ..storage import (
    CONVS_FILE,
    TASK_REGISTRY,
    TURN_EVENT,
    StateLock,
    load_json_file,
    log,
    save_json_file,
    utc_now,
)
from .base import (
    ProviderAdapter,
    ProviderMeta,
    append_user_turn,
    finalize_turn,
    read_local_session_summary,
)


def _terminate_proc(proc: subprocess.Popen[Any]) -> None:
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        pass


def _run_claude_instance_turn(
    conversation_id: str,
    account_id: str,
    account_label: str,
    user_message: str,
    cli_ready: bool,
    claude_session_id: str = "",
    workspace_path: str = "",
    model: str = "",
    thinking_level: str = "",
) -> None:
    handle = TASK_REGISTRY.register(conversation_id)
    try:
        safe_acc_id = validate_account_id(account_id)
        cli_bin = resolve_claude_cli() if cli_ready else None
        cfg_root = resolve_claude_config_root() if cli_ready else None
        can_run_direct = bool(cli_ready and cli_bin and cfg_root)

        append_user_turn(
            conversation_id,
            user_message,
            initial_status="running" if can_run_direct else "waiting_relay",
            extra_session_fields={
                "target_account_id": safe_acc_id,
                "target_label": account_label,
                "workspace_path": workspace_path,
                "model": model,
                "thinking_level": thinking_level,
            },
        )

        if not can_run_direct:
            with TURN_EVENT:
                TURN_EVENT.notify_all()
            log(f"Profile {safe_acc_id} queued in relay inbox for conversation {conversation_id}")
            return

        assert cli_bin is not None and cfg_root is not None
        cfg_dir = assert_within_dir(cfg_root / safe_acc_id / "config", cfg_root, "config_dir")

        cmd = [cli_bin, "-p", user_message, "--output-format", "json"]
        if model.strip():
            cmd.extend(["--model", model.strip()])
        effort = thinking_level.strip().lower()
        if effort in ("low", "medium", "high", "max"):
            cmd.extend(["--effort", effort])
        if claude_session_id.strip():
            cmd.extend(["--resume", claude_session_id.strip()])

        env = os.environ.copy()
        env["CLAUDE_CONFIG_DIR"] = str(cfg_dir)

        cwd: str | None = None
        if workspace_path and Path(workspace_path).is_dir():
            cwd = workspace_path

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        reply_text = ""
        new_sid = claude_session_id
        error_msg: str | None = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=cwd,
                env=env,
                shell=False,
                creationflags=creationflags,
            )
            handle.add_cancel_hook(lambda: _terminate_proc(proc))
            stdout_data, stderr_data = proc.communicate(timeout=180)
            if handle.cancel_event.is_set():
                return
            if proc.returncode == 0:
                raw_out = (stdout_data or "").strip()
                try:
                    obj = json.loads(raw_out)
                    reply_text = str(obj.get("result") or obj.get("content") or raw_out).strip()
                    if obj.get("session_id"):
                        new_sid = str(obj["session_id"])
                except Exception:
                    reply_text = raw_out or "(empty response)"
            else:
                error_msg = (stderr_data or stdout_data or f"CLI exited with code {proc.returncode}").strip()[:600]
        except subprocess.TimeoutExpired:
            _terminate_proc(proc)
            error_msg = "Instance execution timed out after 180s"
        except Exception as exc:
            if handle.cancel_event.is_set():
                return
            error_msg = str(exc)

        if handle.cancel_event.is_set():
            return

        extra = {"claude_session_id": new_sid} if new_sid else None
        finalize_turn(
            conversation_id,
            reply_text=reply_text,
            error_msg=error_msg,
            error_prefix="[Instance Error]",
            extra_session_fields=extra,
            extra_conv_fields=extra,
        )
    except Exception as exc:
        if not handle.cancel_event.is_set():
            finalize_turn(conversation_id, error_msg=str(exc), error_prefix="[Instance Error]")
    finally:
        TASK_REGISTRY.unregister(conversation_id, handle)


class ClaudeInstanceAdapter(ProviderAdapter):
    meta = ProviderMeta(
        type_id="claude_instance",
        display_name="Desktop Instance",
        tag_class="claude",
        uses_local_session=True,
        supports_cancel=True,
    )

    def normalize_collaborator(
        self,
        raw: dict[str, Any],
        project_workspace: str,
        models_map: dict[str, Any],
        instances: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        raw_aid = str(raw.get("account_id") or "").strip()
        if not raw_aid:
            return None
        try:
            acc_id = validate_account_id(raw_aid)
        except ValueError:
            return None
        acc = next((a for a in instances if a["id"] == acc_id), None)
        num = acc["number"] if acc else raw.get("account_number")
        cli_ready = bool(acc["cli_ready"]) if acc else False
        has_code = bool(acc.get("has_code_access", cli_ready)) if acc else bool(raw.get("workspace_path"))
        ws_val = str(raw.get("workspace_path") or (project_workspace if has_code else ""))
        display_label = str(raw.get("label") or (f"Instance #{num}" if num is not None else f"Instance {acc_id[:8]}"))
        return {
            "id": str(raw.get("id") or f"claude:{acc_id}"),
            "type": "claude_instance",
            "account_id": acc_id,
            "account_number": num,
            "cli_ready": cli_ready,
            "has_code_access": has_code,
            "workspace_path": ws_val if has_code else "",
            "model": str(raw.get("model") or ""),
            "thinking_level": str(raw.get("thinking_level") or ""),
            "label": display_label,
        }

    def clean_collaborator_input(
        self,
        raw: dict[str, Any],
        idx: int,
        project_workspace: str,
    ) -> dict[str, Any] | None:
        raw_aid = str(raw.get("account_id") or "").strip()
        if not raw_aid:
            return None
        aid = validate_account_id(raw_aid)
        num = raw.get("account_number")
        label = str(raw.get("label") or (f"Instance #{num}" if num is not None else f"Instance {aid[:8]}")).strip()
        return {
            "id": str(raw.get("id") or f"claude:{aid}"),
            "type": "claude_instance",
            "account_id": aid,
            "account_number": num,
            "workspace_path": str(raw.get("workspace_path") or "").strip(),
            "model": str(raw.get("model") or "").strip(),
            "thinking_level": str(raw.get("thinking_level") or "").strip(),
            "label": label,
        }

    def start_conversation(
        self,
        proj: dict[str, Any],
        collab: dict[str, Any],
        prompt: str,
        title: str | None,
        project_dir: str | None,
        model: str | None,
        profile: str | None,
    ) -> dict[str, Any]:
        acc_id = validate_account_id(str(collab["account_id"]))
        label = str(collab.get("label") or f"Instance {acc_id[:8]}")
        cli_ready = bool(collab.get("cli_ready"))
        ws_path = str(collab.get("workspace_path") or proj.get("workspace_path") or project_dir or "").strip()
        chosen_model = str(model or collab.get("model") or "").strip()
        thinking = str(collab.get("thinking_level") or "").strip()
        conv_id = str(uuid.uuid4())
        now = utc_now()
        can_run_direct = bool(cli_ready and resolve_claude_cli() and resolve_claude_config_root())

        with StateLock():
            convs = load_json_file(CONVS_FILE)
            convs[conv_id] = {
                "conversation_id": conv_id,
                "provider": "claude_instance",
                "project_key": proj["key"],
                "collaborator_id": collab["id"],
                "collaborator_label": label,
                "target_account_id": acc_id,
                "target_number": collab.get("account_number"),
                "project_id": "",
                "workspace_path": ws_path,
                "model": chosen_model,
                "thinking_level": thinking,
                "title": title or f"Invite {label}",
                "created_at": now,
                "updated_at": now,
                "status": "running" if can_run_direct else "waiting_relay",
                "fully_idle": False,
                "step_count": 1,
                "latest_reply_preview": "",
                "last_prompt": prompt[:500],
            }
            save_json_file(CONVS_FILE, convs)

        threading.Thread(
            target=_run_claude_instance_turn,
            args=(conv_id, acc_id, label, prompt, cli_ready, "", ws_path, chosen_model, thinking),
            daemon=True,
        ).start()
        return {
            "ok": True,
            "provider": "claude_instance",
            "mode": "cli_auto" if can_run_direct else "desktop_relay_inbox",
            "conversation_id": conv_id,
            "project_key": proj["key"],
            "collaborator_id": collab["id"],
            "collaborator_label": label,
            "project_id": "",
            "model": chosen_model,
            "thinking_level": thinking,
            "brief_path": None,
            "stderr": "",
        }

    def send_followup(
        self,
        conversation_id: str,
        rec: dict[str, Any],
        message: str,
        title: str | None,
        project_dir: str | None,
    ) -> dict[str, Any]:
        cid = validate_conversation_id(conversation_id)
        acc_id = validate_account_id(str(rec.get("target_account_id") or ""))
        label = str(rec.get("collaborator_label") or f"Instance {acc_id[:8]}")
        acc = next((a for a in list_local_desktop_instances() if a["id"] == acc_id), None)
        cli_ready = bool(acc["cli_ready"]) if acc else False
        can_run_direct = bool(cli_ready and resolve_claude_cli() and resolve_claude_config_root())
        ws_path = str(rec.get("workspace_path") or rec.get("host_workspace") or project_dir or "").strip()
        chosen_model = str(rec.get("model") or "").strip()
        thinking = str(rec.get("thinking_level") or "").strip()
        pre_step = read_local_session_summary(cid, last_n=1).get("last_step_index", -1)

        with StateLock():
            convs = load_json_file(CONVS_FILE)
            cur = convs.get(cid, rec)
            cur["updated_at"] = utc_now()
            cur["status"] = "running" if can_run_direct else "waiting_relay"
            cur["fully_idle"] = False
            cur["last_prompt"] = message[:500]
            convs[cid] = cur
            save_json_file(CONVS_FILE, convs)

        threading.Thread(
            target=_run_claude_instance_turn,
            args=(
                cid,
                acc_id,
                label,
                message,
                cli_ready,
                str(rec.get("claude_session_id") or ""),
                ws_path,
                chosen_model,
                thinking,
            ),
            daemon=True,
        ).start()
        return {
            "ok": True,
            "provider": "claude_instance",
            "conversation_id": cid,
            "previous_step_index": pre_step,
            "brief_path": None,
            "stderr": "",
        }
