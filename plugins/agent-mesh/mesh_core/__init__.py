"""
Agent Mesh Core Package (v1.0.0)
Modular, zero-dependency cross-platform backend for multi-AI collaboration.
"""

from __future__ import annotations

from .orchestrator import (
    cancel_conversation,
    check_inbox,
    create_conversation,
    delete_model,
    delete_project,
    discover_resources,
    get_health,
    get_history,
    list_conversations,
    list_models,
    list_projects,
    reply_inbox,
    save_model,
    save_project,
    send_message,
    wait_reply,
)
from .security import resolve_safe_project_file, validate_account_id, validate_api_base, validate_conversation_id
from .storage import VERSION, ensure_dirs

__all__ = [
    "VERSION",
    "cancel_conversation",
    "check_inbox",
    "create_conversation",
    "delete_model",
    "delete_project",
    "discover_resources",
    "ensure_dirs",
    "get_health",
    "get_history",
    "list_conversations",
    "list_models",
    "list_projects",
    "reply_inbox",
    "resolve_safe_project_file",
    "save_model",
    "save_project",
    "send_message",
    "validate_account_id",
    "validate_api_base",
    "validate_conversation_id",
    "wait_reply",
]
