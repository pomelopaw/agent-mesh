"""
Optional Antigravity desktop collaborator adapter (via local Sidecar bridge).
Strictly validates conversation UUIDs before constructing HTTP paths or transcript paths.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..discovery import ag_bridge_request, ag_bridge_url
from ..security import assert_within_dir, validate_conversation_id
from ..storage import CONVS_FILE, StateLock, load_json_file, save_json_file, utc_now
from .base import ProviderAdapter, ProviderMeta

USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)


def ag_brain_root() -> Path:
    return Path.home() / ".gemini" / "antigravity" / "brain"


def default_ag_transcript_path(conversation_id: str) -> Path:
    cid = validate_conversation_id(conversation_id)
    root = ag_brain_root()
    target = root / cid / ".system_generated" / "logs" / "transcript.jsonl"
    return assert_within_dir(target, root, "transcript_path")


def clean_user_content(raw: str) -> str:
    m = USER_REQ_RE.search(raw or "")
    if m:
        return m.group(1).strip()
    return (raw or "").strip()


def read_full_transcript_line(transcript_path: Path, target_step_index: int) -> dict[str, Any] | None:
    full_path = transcript_path.with_name("transcript_full.jsonl")
    if not full_path.exists():
        return None
    try:
        with full_path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if obj.get("step_index") == target_step_index:
                    return obj
    except OSError:
        return None
    return None


def read_ag_transcript_summary(
    transcript_path: Path,
    last_n: int = 20,
    include_full_latest: bool = True,
) -> dict[str, Any]:
    if not transcript_path.exists():
        return {
            "exists": False,
            "path": str(transcript_path),
            "step_count": 0,
            "last_step_index": -1,
            "inferred_state": "starting",
            "latest_reply": None,
            "waiting_approval": False,
            "turns": [],
            "recent_steps": [],
        }

    steps: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []
    latest_reply: str | None = None
    latest_reply_step = -1
    latest_reply_truncated = False
    latest_reply_at: str | None = None
    latest_user_step = -1
    latest_user_at: str | None = None
    waiting_approval = False
    last_step: dict[str, Any] | None = None

    try:
        with transcript_path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                step_index = obj.get("step_index")
                step_type = obj.get("type", "")
                status = obj.get("status", "")
                content = obj.get("content", "")
                created_at = obj.get("created_at")
                tool_calls = obj.get("tool_calls") or []
                truncated_fields = obj.get("truncated_fields") or []
                last_step = obj

                if step_type == "USER_INPUT":
                    latest_user_step = step_index if isinstance(step_index, int) else len(steps)
                    latest_user_at = created_at
                    turns.append(
                        {
                            "role": "user",
                            "step_index": step_index,
                            "created_at": created_at,
                            "content": clean_user_content(content if isinstance(content, str) else ""),
                        }
                    )
                elif step_type == "PLANNER_RESPONSE" and isinstance(content, str) and content.strip():
                    latest_reply = content
                    latest_reply_step = step_index if isinstance(step_index, int) else len(steps)
                    latest_reply_truncated = "content" in truncated_fields
                    latest_reply_at = created_at
                    turns.append(
                        {
                            "role": "assistant",
                            "step_index": step_index,
                            "created_at": created_at,
                            "content": content,
                            "has_tool_calls": bool(tool_calls),
                            "truncated": latest_reply_truncated,
                        }
                    )

                if status in ("WAITING", "PENDING", "AWAITING_APPROVAL", "REQUIRES_APPROVAL"):
                    waiting_approval = True
                elif step_type == "PLANNER_RESPONSE" and status == "DONE" and not tool_calls:
                    waiting_approval = False

                steps.append(
                    {
                        "step_index": step_index,
                        "type": step_type,
                        "status": status,
                        "created_at": created_at,
                        "tool_names": [tc.get("name") for tc in tool_calls if isinstance(tc, dict)],
                    }
                )
    except OSError as exc:
        return {"exists": True, "path": str(transcript_path), "inferred_state": "error", "error": str(exc)}

    if include_full_latest and latest_reply_truncated and latest_reply_step >= 0:
        full_obj = read_full_transcript_line(transcript_path, latest_reply_step)
        if full_obj and isinstance(full_obj.get("content"), str):
            latest_reply = full_obj["content"]
            for t in reversed(turns):
                if t.get("step_index") == latest_reply_step:
                    t["content"] = latest_reply
                    t["truncated"] = False
                    break

    last_step_index = (
        last_step.get("step_index")
        if isinstance(last_step, dict) and isinstance(last_step.get("step_index"), int)
        else len(steps) - 1
    )

    inferred_state = "running"
    if waiting_approval:
        inferred_state = "waiting_approval"
    elif isinstance(last_step, dict):
        ls_type = last_step.get("type", "")
        ls_status = last_step.get("status", "")
        ls_tools = last_step.get("tool_calls") or []
        if ls_type == "PLANNER_RESPONSE" and ls_status == "DONE" and not ls_tools:
            inferred_state = "completed"
        elif ls_status == "ERROR":
            inferred_state = "error"

    return {
        "exists": True,
        "path": str(transcript_path),
        "step_count": len(steps),
        "last_step_index": last_step_index,
        "latest_user_step": latest_user_step,
        "latest_reply_step": latest_reply_step,
        "latest_user_at": latest_user_at,
        "latest_reply_at": latest_reply_at,
        "latest_reply": latest_reply,
        "waiting_approval": waiting_approval,
        "inferred_state": inferred_state,
        "turns": turns[-last_n:] if last_n > 0 else turns,
        "recent_steps": steps[-last_n:] if last_n > 0 else [],
    }


class AntigravityAdapter(ProviderAdapter):
    meta = ProviderMeta(
        type_id="antigravity",
        display_name="Antigravity",
        tag_class="ag",
        uses_local_session=False,
        supports_cancel=True,
    )

    def normalize_collaborator(
        self,
        raw: dict[str, Any],
        project_workspace: str,
        models_map: dict[str, Any],
        instances: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        pid = str(raw.get("project_id") or "").strip()
        if not pid:
            return None
        return {
            "id": str(raw.get("id") or "antigravity"),
            "type": "antigravity",
            "label": str(raw.get("label") or "Antigravity"),
            "project_id": pid,
            "model": str(raw.get("model") or ""),
            "thinking_level": str(raw.get("thinking_level") or ""),
        }

    def clean_collaborator_input(
        self,
        raw: dict[str, Any],
        idx: int,
        project_workspace: str,
    ) -> dict[str, Any] | None:
        pid = str(raw.get("project_id") or "").strip()
        if not pid:
            return None
        return {
            "id": str(raw.get("id") or f"antigravity:{idx + 1}"),
            "type": "antigravity",
            "label": str(raw.get("label") or "Antigravity"),
            "project_id": pid,
            "model": str(raw.get("model") or "").strip(),
            "thinking_level": str(raw.get("thinking_level") or "").strip(),
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
        target_project_id = str(collab.get("project_id") or "")
        chosen_model = str(model or collab.get("model") or "").strip() or None
        payload = {
            "prompt": prompt,
            "title": title,
            "project_id": target_project_id,
            "project": proj["key"],
            "project_dir": proj.get("workspace_path") or project_dir or None,
            "model": chosen_model,
            "profile": profile,
        }
        res = ag_bridge_request("POST", "/conversations", body=payload, timeout=70)
        if not res:
            raise RuntimeError(
                f"Antigravity sidecar bridge is not reachable at {ag_bridge_url()}. "
                "Enable the optional Antigravity Sidecar, or target an API model / Codex / desktop instance collaborator."
            )
        raw_cid = res.get("conversation_id")
        conv_id = validate_conversation_id(str(raw_cid)) if raw_cid else None
        if conv_id:
            now = utc_now()
            with StateLock():
                convs = load_json_file(CONVS_FILE)
                convs[conv_id] = {
                    "conversation_id": conv_id,
                    "provider": "antigravity",
                    "project_key": proj["key"],
                    "collaborator_id": collab["id"],
                    "collaborator_label": collab.get("label") or "Antigravity",
                    "project_id": target_project_id,
                    "workspace_path": proj.get("workspace_path") or "",
                    "title": title or f"{proj['display_name']} · Antigravity",
                    "model": chosen_model,
                    "created_at": now,
                    "updated_at": now,
                    "status": "running" if res.get("ok") else "error",
                    "transcript_path": str(default_ag_transcript_path(conv_id)),
                    "last_prompt": prompt[:500],
                    "last_brief_path": res.get("brief_path"),
                }
                save_json_file(CONVS_FILE, convs)
        return {
            "ok": bool(res.get("ok")),
            "provider": "antigravity",
            "mode": "sidecar",
            "conversation_id": conv_id,
            "project_key": proj["key"],
            "collaborator_id": collab["id"],
            "collaborator_label": collab.get("label") or "Antigravity",
            "project_id": target_project_id,
            "model": chosen_model,
            "brief_path": res.get("brief_path"),
            "stderr": (res.get("command_result") or {}).get("stderr", ""),
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
        tpath = default_ag_transcript_path(cid)
        pre_step = read_ag_transcript_summary(tpath, last_n=1, include_full_latest=False).get("last_step_index", -1)
        res = ag_bridge_request(
            "POST",
            f"/conversations/{cid}/messages",
            body={
                "message": message,
                "title": title,
                "project_dir": rec.get("workspace_path") or rec.get("host_workspace") or project_dir,
            },
            timeout=70,
        )
        if not res:
            raise RuntimeError(f"Antigravity sidecar bridge is not reachable at {ag_bridge_url()}.")
        with StateLock():
            convs = load_json_file(CONVS_FILE)
            cur = convs.get(cid, rec)
            cur["updated_at"] = utc_now()
            cur["status"] = "running" if res.get("ok") else "error"
            cur["fully_idle"] = False
            cur["last_prompt"] = message[:500]
            cur["last_brief_path"] = res.get("brief_path")
            convs[cid] = cur
            save_json_file(CONVS_FILE, convs)
        return {
            "ok": bool(res.get("ok")),
            "provider": "antigravity",
            "conversation_id": cid,
            "previous_step_index": pre_step,
            "brief_path": res.get("brief_path"),
            "stderr": (res.get("command_result") or {}).get("stderr", ""),
        }

    def read_transcript(
        self,
        rec: dict[str, Any],
        conversation_id: str,
        last_n: int = 20,
        include_full_latest: bool = True,
    ) -> dict[str, Any]:
        cid = validate_conversation_id(conversation_id)
        tpath = default_ag_transcript_path(cid)
        return read_ag_transcript_summary(tpath, last_n=last_n, include_full_latest=include_full_latest)
