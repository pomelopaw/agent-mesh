# Agent Mesh

Multi-AI background orchestration hub (`kind: "host"`).

## What It Does

- **Custom OpenAI-Compatible APIs**: Configure reusable reasoning/chat models (DeepSeek, OpenAI, Ollama, OpenRouter, etc.) with thinking-effort levels (`minimal` / `low` / `medium` / `high` / `xhigh`). API keys are isolated in `~/.agent-mesh/secrets.json` and never exposed in tool responses.
- **Local Codex Desktop / CLI**: Auto-discovers installed Codex CLI, projects, and models; dispatches headless multi-turn tasks via official `codex app-server --stdio` (with `codex exec` fallback) without grabbing mouse or window focus.
- **Multi-Instance Desktop Collaboration**: Auto-discovers desktop assistant instances and supports headless CLI execution or relay inbox (`ag_check_inbox` / `ag_reply_inbox`) for dialogue-only accounts.
- **Optional Antigravity Integration**: Auto-detects an optional localhost Antigravity Sidecar bridge (`127.0.0.1:18765`) when present; stays fully online without Antigravity.
- **Paginated Session History**: Filter conversations by project, date (`YYYY-MM-DD`), collaborator, or search query (10 per page) with collapsible reasoning traces.

## Runtime & Dependencies

- Pure **Python 3 standard library** (`python3`), zero external runtime packages downloaded at startup, zero telemetry.

## Data Flow

- Local state is stored in `~/.agent-mesh` (overridable via `AGENT_MESH_STATE_DIR`).
- Network requests are sent **only** to user-configured API base URLs or localhost (`127.0.0.1`) optional bridge endpoints.
- Source repository: https://github.com/pomelopaw/agent-mesh
