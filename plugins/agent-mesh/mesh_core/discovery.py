"""
Cross-platform local discovery for workspace folders, desktop assistant instances,
Codex CLI/Desktop, and optional Antigravity Sidecar bridge.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .storage import DEFAULT_TOKEN_FILE, load_json_file, normalize_path

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

DEFAULT_AG_BRIDGE_URL = "http://127.0.0.1:18765"


# ---- Optional Antigravity Bridge Adapter ----
def ag_bridge_url() -> str:
    return (
        os.environ.get("AGENT_MESH_AG_BRIDGE_URL")
        or DEFAULT_AG_BRIDGE_URL
    ).rstrip("/")


def load_ag_bridge_token() -> str | None:
    v = os.environ.get("AGENT_MESH_AG_TOKEN", "").strip()
    if v:
        return v
    candidates: list[Path] = []
    p = os.environ.get("AGENT_MESH_AG_TOKEN_FILE", "").strip()
    if p:
        candidates.append(Path(p).expanduser())
    candidates.append(DEFAULT_TOKEN_FILE)
    for cand in candidates:
        try:
            if cand.exists() and cand.is_file():
                tok = cand.read_text(encoding="utf-8", errors="replace").strip()
                if tok:
                    return tok
        except OSError:
            continue
    return None


def ag_bridge_request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any] | None:
    """Call optional Antigravity Sidecar bridge on localhost. Returns None if unavailable."""
    token = load_ag_bridge_token()
    if not token:
        return None
    url = ag_bridge_url() + path
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method=method,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            parsed = json.loads(resp.read().decode("utf-8", errors="replace"))
            return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def probe_antigravity_bridge() -> dict[str, Any]:
    res = ag_bridge_request("GET", "/health", timeout=2)
    if res and res.get("ok"):
        return {
            "available": bool(res.get("agentapi_available", True)),
            "bridge_online": True,
            "default_project_id": res.get("default_project_id"),
            "port": res.get("port"),
        }
    return {
        "available": False,
        "bridge_online": False,
        "default_project_id": None,
        "port": None,
    }


# ---- Local Workbench Metadata Discovery (UI/app-only, never exposed to model) ----
def _resolve_workbench_data_dir() -> Path | None:
    candidates: list[Path] = []
    env_dir = os.environ.get("AGENT_MESH_WORKBENCH_DATA_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir).expanduser())

    # Auto-detect when installed inside <DataDir>/plugins/<plugin-id>/mesh_core/discovery.py
    here = Path(__file__).resolve()
    for parent in here.parents:
        if parent.name.lower() == "plugins" and parent.parent != parent:
            candidates.append(parent.parent)
            break

    for c in candidates:
        try:
            if c.exists() and c.is_dir():
                if any((c / name).exists() for name in ("projects.json", "claude-accounts.json", "config.json")):
                    return c
        except OSError:
            continue
    return None


def list_local_workspace_projects() -> list[dict[str, Any]]:
    data_dir = _resolve_workbench_data_dir()
    if not data_dir:
        return []
    pfile = data_dir / "projects.json"
    try:
        if not pfile.exists():
            return []
    except OSError:
        return []
    data = load_json_file(pfile)
    seen: set[str] = set()
    items: list[dict[str, Any]] = []

    groups = data.get("groups") or []
    if groups:
        for grp in groups:
            if not isinstance(grp, dict):
                continue
            ws = str(grp.get("workspace") or "").strip()
            if not ws:
                continue
            norm = normalize_path(ws)
            if norm in seen:
                continue
            seen.add(norm)
            name = str(grp.get("name") or Path(ws.rstrip("/\\")).name or ws)
            items.append({"name": name, "path": ws, "folders": grp.get("folders") or [ws]})
        return items

    for p in data.get("paths") or []:
        if not isinstance(p, str) or not p.strip():
            continue
        ws = p.strip()
        try:
            if not Path(ws).exists():
                continue
        except OSError:
            continue
        norm = normalize_path(ws)
        if norm in seen:
            continue
        seen.add(norm)
        name = Path(ws.rstrip("/\\")).name or ws
        items.append({"name": name, "path": ws, "folders": [ws]})
    return items


def resolve_claude_cli() -> str | None:
    env_bin = os.environ.get("AGENT_MESH_CLAUDE_BIN", "").strip()
    if env_bin:
        try:
            if Path(env_bin).is_file():
                return env_bin
        except OSError:
            pass
    found = shutil.which("claude")
    if found:
        return found
    return None


def resolve_claude_config_root() -> Path | None:
    env_root = os.environ.get("AGENT_MESH_CLAUDE_CONFIG_ROOT", "").strip()
    if not env_root:
        return None
    try:
        p = Path(env_root).expanduser().resolve()
        if p.is_dir():
            return p
    except OSError:
        pass
    return None


def list_local_desktop_instances() -> list[dict[str, Any]]:
    """
    Discover local multi-instance desktop assistant profiles for UI selection.
    Only exposed via app-only discovery tool (`mesh_discover_projects`).
    """
    data_dir = _resolve_workbench_data_dir()
    if not data_dir:
        return []
    afile = data_dir / "claude-accounts.json"
    try:
        if not afile.exists():
            return []
    except OSError:
        return []
    data = load_json_file(afile)
    direct_cli = bool(resolve_claude_cli() and resolve_claude_config_root())
    out: list[dict[str, Any]] = []
    for acc in data.get("accounts") or []:
        if not isinstance(acc, dict):
            continue
        acc_id = str(acc.get("id") or "").strip()
        if not acc_id:
            continue
        num = acc.get("number")
        email = str(acc.get("email") or acc.get("label") or acc_id[:8]).strip()
        cli_status = str(acc.get("status") or "unknown")
        desktop_status = str(acc.get("desktop_status") or "unknown")
        code_unlinked = bool(acc.get("code_unlinked", False))
        raw_plan = str(acc.get("plan") or "").strip()
        is_free = raw_plan.lower() == "free"
        desktop_ready = desktop_status == "ready"
        code_cli_ready = (
            cli_status == "ready"
            and (not code_unlinked)
            and (not is_free)
        )
        # Never fabricate "Free" when only Desktop is signed in and plan is unknown.
        has_code_access = not is_free
        plan_label = raw_plan.capitalize() if raw_plan else ""
        out.append(
            {
                "id": acc_id,
                "number": num,
                "email": email,
                "desktop_status": desktop_status,
                "desktop_ready": desktop_ready,
                "cli_status": cli_status,
                "code_cli_ready": code_cli_ready,
                "cli_ready": bool(code_cli_ready and direct_cli),
                "has_code_access": has_code_access,
                "is_free": is_free,
                "plan": raw_plan,
                "plan_label": plan_label,
            }
        )
    out.sort(key=lambda x: (x["number"] is None, x["number"] or 999, x["email"]))
    return out


# ---- Local Codex CLI & Desktop Auto-Discovery ----
def resolve_codex_home() -> Path:
    custom = os.environ.get("CODEX_HOME", "").strip()
    if custom:
        return Path(custom).expanduser()
    return Path.home() / ".codex"


def read_codex_config() -> dict[str, Any]:
    cfg_path = resolve_codex_home() / "config.toml"
    try:
        if not cfg_path.exists():
            return {}
        raw = cfg_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    if tomllib is not None:
        try:
            parsed = tomllib.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    out: dict[str, Any] = {}
    for key in ("model", "model_reasoning_effort", "model_catalog_json", "CODEX_CLI_PATH"):
        m = re.search(rf"^\s*{re.escape(key)}\s*=\s*['\"]([^'\"]+)['\"]", raw, re.MULTILINE)
        if m:
            out[key] = m.group(1)
    return out


def _find_codex_cli_in_config(cfg: dict[str, Any]) -> str | None:
    def _walk(obj: Any) -> str | None:
        if isinstance(obj, dict):
            val = obj.get("CODEX_CLI_PATH")
            if isinstance(val, str) and val.strip():
                try:
                    if Path(val.strip()).exists():
                        return val.strip()
                except OSError:
                    pass
            for v in obj.values():
                found = _walk(v)
                if found:
                    return found
        return None

    return _walk(cfg)


def _glob_safe(base: Path, pattern: str) -> list[Path]:
    try:
        if not base.exists():
            return []
        return sorted(base.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []


def resolve_codex_cli() -> str | None:
    for var in ("AGENT_MESH_CODEX_BIN", "CODEX_CLI_PATH"):
        val = os.environ.get(var, "").strip()
        if val:
            try:
                if Path(val).exists():
                    return val
            except OSError:
                pass

    found = shutil.which("codex")
    if found:
        return found

    cfg = read_codex_config()
    from_cfg = _find_codex_cli_in_config(cfg)
    if from_cfg:
        return from_cfg

    candidates: list[Path] = []
    home = Path.home()
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app:
            candidates.extend(_glob_safe(Path(local_app) / "OpenAI" / "Codex" / "bin", "*/codex.exe"))
        app_data = os.environ.get("APPDATA", "").strip()
        if app_data:
            candidates.append(Path(app_data) / "npm" / "codex.cmd")
            candidates.append(Path(app_data) / "npm" / "codex")
    elif sys.platform == "darwin":
        candidates.extend(
            [
                Path("/opt/homebrew/bin/codex"),
                Path("/usr/local/bin/codex"),
                Path("/Applications/Codex.app/Contents/Resources/codex"),
                home / ".local" / "bin" / "codex",
                home / ".volta" / "bin" / "codex",
            ]
        )
        candidates.extend(_glob_safe(home / "Library" / "Application Support" / "OpenAI" / "Codex" / "bin", "*/codex"))
        candidates.extend(_glob_safe(home / ".nvm" / "versions" / "node", "*/bin/codex"))
        candidates.extend(_glob_safe(home / "Library" / "Application Support" / "fnm" / "node-versions", "*/installation/bin/codex"))
    else:
        candidates.extend(
            [
                home / ".local" / "bin" / "codex",
                Path("/usr/local/bin/codex"),
                Path("/usr/bin/codex"),
                home / ".volta" / "bin" / "codex",
            ]
        )
        candidates.extend(_glob_safe(home / ".nvm" / "versions" / "node", "*/bin/codex"))
        candidates.extend(_glob_safe(home / ".local" / "share" / "fnm" / "node-versions", "*/installation/bin/codex"))

    for c in candidates:
        try:
            if c.exists() and c.is_file():
                return str(c)
        except OSError:
            continue
    return None


def list_local_codex_projects() -> list[dict[str, Any]]:
    codex_home = resolve_codex_home()
    try:
        if not codex_home.exists():
            return []
        db_files = sorted(codex_home.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for db_path in db_files[:1]:
        con: sqlite3.Connection | None = None
        try:
            encoded_path = quote(db_path.as_posix(), safe="/:")
            con = sqlite3.connect(f"file:{encoded_path}?mode=ro", uri=True, timeout=2.0)
            rows = con.execute(
                "SELECT p.id, p.name, r.path "
                "FROM projects p "
                "LEFT JOIN project_roots r ON r.project_id = p.id AND r.position = 0 "
                "ORDER BY p.position ASC, p.updated_at_ms DESC"
            ).fetchall()
            for pid, name, root_path in rows:
                ws = str(root_path or "").strip()
                norm = normalize_path(ws) if ws else str(pid)
                if norm in seen:
                    continue
                seen.add(norm)
                disp = str(name or (Path(ws.rstrip("/\\")).name if ws else "") or str(pid)[:8])
                items.append({"project_id": str(pid), "name": disp, "workspace": ws})
        except Exception:
            pass
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass

    if items:
        return items

    cfg = read_codex_config()
    raw_projs = cfg.get("projects")
    if isinstance(raw_projs, dict):
        for path_key in raw_projs.keys():
            ws = str(path_key).strip()
            try:
                if not ws or not Path(ws).exists():
                    continue
            except OSError:
                continue
            norm = normalize_path(ws)
            if norm in seen:
                continue
            seen.add(norm)
            items.append({"project_id": norm, "name": Path(ws.rstrip("/\\")).name or ws, "workspace": ws})
    return items


def list_local_codex_models() -> dict[str, Any]:
    codex_home = resolve_codex_home()
    cfg = read_codex_config()
    default_model = str(cfg.get("model") or "").strip()
    default_effort = str(cfg.get("model_reasoning_effort") or "").strip().lower()

    models: list[str] = []
    seen: set[str] = set()

    def _add(slug: str) -> None:
        s = slug.strip()
        if not s or s in seen or s == "codex-auto-review":
            return
        seen.add(s)
        models.append(s)

    if default_model:
        _add(default_model)

    catalog_paths: list[Path] = []
    cfg_cat = str(cfg.get("model_catalog_json") or "").strip()
    if cfg_cat:
        catalog_paths.append(Path(cfg_cat).expanduser())
    catalog_paths.append(codex_home / "models_cache.json")

    for cp in catalog_paths:
        try:
            if not cp.exists():
                continue
            data = json.loads(cp.read_text(encoding="utf-8", errors="replace"))
            raw_list = data.get("models") if isinstance(data, dict) else (data if isinstance(data, list) else [])
            for item in raw_list:
                if isinstance(item, dict):
                    slug = str(item.get("slug") or item.get("id") or item.get("name") or "").strip()
                    if slug:
                        _add(slug)
                elif isinstance(item, str):
                    _add(item)
        except Exception:
            continue

    return {
        "default_model": default_model,
        "default_effort": default_effort,
        "models": models,
    }


def probe_codex() -> dict[str, Any]:
    cli = resolve_codex_cli()
    minfo = list_local_codex_models()
    return {
        "available": bool(cli),
        "cli_path": cli,
        "default_model": minfo["default_model"],
        "default_effort": minfo["default_effort"],
        "models": minfo["models"],
    }
