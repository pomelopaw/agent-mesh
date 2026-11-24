# Agent Mesh

Multi-AI background orchestration hub (`kind: "host"`).

## What It Does

- **Custom OpenAI-Compatible APIs**: Configure reusable reasoning/chat models (DeepSeek, OpenAI, Ollama, OpenRouter, etc.) with thinking-effort levels (`minimal` / `low` / `medium` / `high` / `xhigh`). Only `https://` endpoints (and local loopback `http://127.0.0.1` / `localhost` / `[::1]`) are accepted. API keys are isolated in `~/.agent-mesh/secrets.json` (created with `0600` permissions on POSIX and restricted to the current user via ACL on Windows) and never returned in tool responses.
- **Local Codex Desktop / CLI**: Auto-discovers installed Codex CLI, projects, and models across Windows, macOS, and Linux; dispatches headless multi-turn tasks via official `codex app-server --stdio` (with `codex exec` fallback). Defaults to safe `read-only` sandbox mode; switch to `workspace-write` explicitly when edits are desired.
- **Multi-Instance Desktop Collaboration**: Supports multi-profile collaboration with an in-panel **Relay Inbox** (`mesh_check_inbox` / `mesh_reply_inbox`) and optional direct CLI execution via `AGENT_MESH_CLAUDE_BIN` + `AGENT_MESH_CLAUDE_CONFIG_ROOT`.
- **Optional Antigravity Integration**: Auto-detects an optional localhost Antigravity Sidecar bridge (`127.0.0.1:18765`) when present; stays fully online without Antigravity.
- **Paginated History, Safe Markdown & Task Cancellation**: Filter conversations by project, date (`YYYY-MM-DD`), collaborator, or search query (10 per page), read formatted replies and reasoning traces, and cancel running tasks anytime (`mesh_cancel_conversation`).

## Runtime & Dependencies

- Pure **Python 3 standard library** (`python3`), zero external runtime packages downloaded at startup, zero telemetry.

## Security & Data Flow

- Local state is stored in `~/.agent-mesh` (overridable via `AGENT_MESH_STATE_DIR`) with atomic lockfiles and log rotation.
- `prompt_file` / `message_file` inputs are strictly confined to the active workspace directory (blocking `..` traversal, symlink escapes, and files over 512 KiB).
- Network requests are sent **only** to user-configured API base URLs or localhost (`127.0.0.1`) optional bridge endpoints.
- Source repository: https://github.com/pomelopaw/agent-mesh
