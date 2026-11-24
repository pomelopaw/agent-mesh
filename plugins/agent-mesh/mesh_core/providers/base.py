"""
Provider adapter abstraction, shared local session turn lifecycle, and provider registry.
Eliminates boilerplate across providers and enables clean addition of new AI backends.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..security import validate_conversation_id
from ..storage import (
    CONVS_FILE,
    TASK_REGISTRY,
    TURN_EVENT,
    StateLock,
    api_session_path,
    load_api_session,
    load_json_file,
    save_api_session,
    save_json_file,
    utc_now,
)


@dataclass(frozen=True)
class ProviderMeta:
    type_id: str
    display_name: str
    tag_class: str
    uses_local_session: bool = True
    supports_cancel: bool = True


def append_user_turn(
    conversation_id: str,
    user_message: str,
    initial_status: str = "running",
    extra_session_fields: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Atomically append a user turn to the local session and update conversation cache."""
    cid = validate_conversation_id(conversation_id)
    now = utc_now()
    with StateLock():
        sess = load_api_session(cid)
        turns = list(sess.get("turns") or [])
        turns.append(
            {
                "role": "user",
                "step_index": len(turns),
                "created_at": now,
                "content": user_message,
            }
        )
        sess["turns"] = turns
        sess["status"] = initial_status
        sess["error"] = None
        if extra_session_fields:
            sess.update(extra_session_fields)
        save_api_session(cid, sess)

        convs = load_json_file(CONVS_FILE)
        rec = convs.get(cid, {"conversation_id": cid, "created_at": now})
        rec["updated_at"] = now
        rec["status"] = initial_status
        rec["fully_idle"] = False
        rec["step_count"] = len(turns)
        rec["last_prompt"] = user_message[:500]
        convs[cid] = rec
        save_json_file(CONVS_FILE, convs)
    return turns, len(turns)


def finalize_turn(
    conversation_id: str,
    reply_text: str = "",
    reasoning_text: str = "",
    error_msg: str | None = None,
    cancelled: bool = False,
    error_prefix: str = "[Error]",
    extra_session_fields: dict[str, Any] | None = None,
    extra_conv_fields: dict[str, Any] | None = None,
) -> None:
    """
    Atomically commit a provider turn result to both api_sessions/<cid>.json and
    conversations.json (updating cached step_count and latest_reply_preview) and
    wake any waiting callers.
    """
    cid = validate_conversation_id(conversation_id)
    now = utc_now()
    with StateLock():
        sess = load_api_session(cid)
        # If the task was already marked cancelled by user, do not overwrite with a late reply.
        if sess.get("status") == "cancelled" and not cancelled:
            return
        turns = list(sess.get("turns") or [])
        if cancelled:
            final_status = "cancelled"
            sess["status"] = "cancelled"
            sess["error"] = None
            content = "[Task cancelled by user]"
            turns.append(
                {
                    "role": "assistant",
                    "step_index": len(turns),
                    "created_at": now,
                    "content": content,
                    "cancelled": True,
                }
            )
            preview = content
        elif error_msg:
            final_status = "error"
            sess["status"] = "error"
            sess["error"] = error_msg
            content = f"{error_prefix} {error_msg}"
            turns.append(
                {
                    "role": "assistant",
                    "step_index": len(turns),
                    "created_at": now,
                    "content": content,
                }
            )
            preview = content[:300]
        else:
            final_status = "completed"
            sess["status"] = "completed"
            sess["error"] = None
            tobj: dict[str, Any] = {
                "role": "assistant",
                "step_index": len(turns),
                "created_at": now,
                "content": reply_text,
            }
            if reasoning_text:
                tobj["reasoning_content"] = reasoning_text
            turns.append(tobj)
            preview = reply_text[:300]

        if extra_session_fields:
            sess.update(extra_session_fields)
        sess["turns"] = turns
        save_api_session(cid, sess)

        convs = load_json_file(CONVS_FILE)
        rec = convs.get(cid, {"conversation_id": cid, "created_at": now})
        rec["updated_at"] = now
        rec["status"] = final_status
        rec["fully_idle"] = True
        rec["step_count"] = len(turns)
        rec["latest_reply_preview"] = preview
        if error_msg and not cancelled:
            rec["error"] = error_msg
        else:
            rec.pop("error", None)
        if extra_conv_fields:
            rec.update(extra_conv_fields)
        convs[cid] = rec
        save_json_file(CONVS_FILE, convs)

    with TURN_EVENT:
        TURN_EVENT.notify_all()


def read_local_session_summary(conversation_id: str, last_n: int = 20) -> dict[str, Any]:
    cid = validate_conversation_id(conversation_id)
    p = api_session_path(cid)
    if not p.exists():
        return {
            "exists": False,
            "path": str(p),
            "step_count": 0,
            "last_step_index": -1,
            "inferred_state": "starting",
            "latest_reply": None,
            "waiting_approval": False,
            "turns": [],
            "recent_steps": [],
        }
    data = load_api_session(cid)
    turns = data.get("turns") or []
    latest_reply: str | None = None
    latest_reply_step = -1
    latest_user_step = -1
    latest_reply_at = None
    latest_user_at = None
    for idx, t in enumerate(turns):
        if t.get("role") == "user":
            latest_user_step = idx
            latest_user_at = t.get("created_at")
        elif t.get("role") == "assistant":
            latest_reply = t.get("content")
            latest_reply_step = idx
            latest_reply_at = t.get("created_at")
    status = data.get("status") or ("completed" if latest_reply_step > latest_user_step else "running")
    return {
        "exists": True,
        "path": str(p),
        "step_count": len(turns),
        "last_step_index": len(turns) - 1,
        "latest_user_step": latest_user_step,
        "latest_reply_step": latest_reply_step,
        "latest_user_at": latest_user_at,
        "latest_reply_at": latest_reply_at,
        "latest_reply": latest_reply,
        "waiting_approval": status == "waiting_relay",
        "inferred_state": status,
        "error": data.get("error"),
        "turns": turns[-last_n:] if last_n > 0 else turns,
        "recent_steps": [],
    }


class ProviderAdapter(ABC):
    meta: ProviderMeta

    @abstractmethod
    def normalize_collaborator(
        self,
        raw: dict[str, Any],
        project_workspace: str,
        models_map: dict[str, Any],
        instances: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Normalize a stored collaborator entry for runtime use."""

    @abstractmethod
    def clean_collaborator_input(
        self,
        raw: dict[str, Any],
        idx: int,
        project_workspace: str,
    ) -> dict[str, Any] | None:
        """Validate and sanitize a collaborator entry before saving projects.json."""

    @abstractmethod
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
        """Start a new background conversation with this collaborator."""

    @abstractmethod
    def send_followup(
        self,
        conversation_id: str,
        rec: dict[str, Any],
        message: str,
        title: str | None,
        project_dir: str | None,
    ) -> dict[str, Any]:
        """Send a follow-up message to an existing conversation."""

    def read_transcript(
        self,
        rec: dict[str, Any],
        conversation_id: str,
        last_n: int = 20,
        include_full_latest: bool = True,
    ) -> dict[str, Any]:
        return read_local_session_summary(conversation_id, last_n=last_n)

    def cancel(self, conversation_id: str) -> bool:
        return TASK_REGISTRY.cancel(conversation_id)


PROVIDERS: dict[str, ProviderAdapter] = {}


def register_provider(adapter: ProviderAdapter) -> ProviderAdapter:
    PROVIDERS[adapter.meta.type_id] = adapter
    return adapter


def get_provider(type_id: str) -> ProviderAdapter | None:
    return PROVIDERS.get(type_id)


def list_provider_metas() -> list[dict[str, Any]]:
    return [
        {
            "type_id": p.meta.type_id,
            "display_name": p.meta.display_name,
            "tag_class": p.meta.tag_class,
            "supports_cancel": p.meta.supports_cancel,
        }
        for p in PROVIDERS.values()
    ]
