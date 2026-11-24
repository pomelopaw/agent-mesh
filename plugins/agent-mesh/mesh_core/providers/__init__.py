"""
Provider registry initialization for Agent Mesh.
"""

from __future__ import annotations

from .antigravity import AntigravityAdapter
from .base import (
    PROVIDERS,
    ProviderAdapter,
    ProviderMeta,
    append_user_turn,
    finalize_turn,
    get_provider,
    list_provider_metas,
    read_local_session_summary,
    register_provider,
)
from .claude_instance import ClaudeInstanceAdapter
from .codex import CodexAdapter
from .openai_compat import OpenAICompatAdapter

register_provider(OpenAICompatAdapter())
register_provider(CodexAdapter())
register_provider(ClaudeInstanceAdapter())
register_provider(AntigravityAdapter())

__all__ = [
    "PROVIDERS",
    "ProviderAdapter",
    "ProviderMeta",
    "append_user_turn",
    "finalize_turn",
    "get_provider",
    "list_provider_metas",
    "read_local_session_summary",
    "register_provider",
]
