#!/usr/bin/env python3
"""
Agent Mesh Core Engine (v1.0.0)

Self-contained, zero-dependency cross-platform backend for multi-AI collaboration:
  1. Custom OpenAI-compatible reasoning/chat APIs (DeepSeek, OpenAI, Ollama, OpenRouter, etc.)
  2. Multi-instance desktop assistant collaboration (auto CLI execution + Free-account relay inbox)
  3. Optional Antigravity desktop integration (auto-detected via optional local Sidecar bridge)

All state is stored cleanly in ~/.agent-mesh (or AGENT_MESH_STATE_DIR) using atomic writes
and lightweight cross-process lockfiles, with API keys isolated in secrets.json.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

VERSION = "1.0.0"
DEFAULT_AG_BRIDGE_URL = "http://127.0.0.1:18765"

UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL)

TURN_EVENT = threading.Condition()
_LOCAL_LOCK = threading.RLock()


def resolve_state_dir() -> Path:
    custom = os.environ.get("AGENT_MESH_STATE_DIR", "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    return (Path.home() / ".agent-mesh").resolve()


STATE_DIR = resolve_state_dir()
PROJECTS_FILE = STATE_DIR / "projects.json"
MODELS_FILE = STATE_DIR / "models.json"
SECRETS_FILE = STATE_DIR / "secrets.json"
CONVS_FILE = STATE_DIR / "conversations.json"
API_SESSIONS_DIR = STATE_DIR / "api_sessions"
PROMPTS_DIR = STATE_DIR / "prompts"
DEFAULT_TOKEN_FILE = STATE_DIR / "bridge.token"
LOG_FILE = STATE_DIR / "agent_mesh.log"
LOCK_FILE = STATE_DIR / ".state.lock"


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def local_date_str(iso_ts: str | None) -> str:
    if not iso_ts:
        return ""
    try:
        dt = datetime.datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d")
    except Exception:
        return str(iso_ts)[:10]


def ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    API_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str) -> None:
    line = f"[{utc_now()}] {msg}\n"
    try:
        ensure_dirs()
        with LOG_FILE.open("a", encoding="utf-8", errors="replace") as f:
            f.write(line)
    except OSError:
        pass


class StateLock:
    """Thread-safe + cross-process atomic lockfile guard with stale-lock recovery."""

    def __init__(self, lock_path: Path = LOCK_FILE, timeout: float = 10.0) -> None:
        self.lock_path = lock_path
        self.timeout = timeout
        self._fd: int | None = None

    def __enter__(self) -> StateLock:
        _LOCAL_LOCK.acquire()
        ensure_dirs()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self._fd = os.open(
                    str(self.lock_path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
                os.write(self._fd, f"{os.getpid()}:{time.time()}\n".encode("ascii"))
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.lock_path.stat().st_mtime
                    if age > 30.0:
                        self.lock_path.unlink(missing_ok=True)
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    return self
                time.sleep(0.03)
            except OSError:
                return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
                try:
                    self.lock_path.unlink(missing_ok=True)
                except OSError:
                    pass
        finally:
            _LOCAL_LOCK.release()


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_json_file(path: Path, data: dict[str, Any], private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    if private and os.name != "nt":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05)


def mask_secret(val: str | None) -> str:
    s = (val or "").strip()
    if not s:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}...{s[-4:]}"


def get_secret(key: str) -> str:
    with StateLock():
        return str(load_json_file(SECRETS_FILE).get(key) or "")


def set_secret(key: str, value: str) -> None:
    with StateLock():
        data = load_json_file(SECRETS_FILE)
        val = value.strip()
        if val:
            data[key] = val
        else:
            data.pop(key, None)
        save_json_file(SECRETS_FILE, data, private=True)


def delete_secret(key: str) -> None:
    with StateLock():
        data = load_json_file(SECRETS_FILE)
        if key in data:
            data.pop(key, None)
            save_json_file(SECRETS_FILE, data, private=True)


def normalize_path(p: str | None) -> str:
    if not p:
        return ""
    s = p.strip().replace("/", "\\") if os.name == "nt" else p.strip()
    while len(s) > 3 and s.endswith(("\\", "/")):
        s = s[:-1]
    return s.lower() if os.name == "nt" else s


# ---- Cross-Platform Workbench & Binary Discovery (Zero Hardcoded Paths) ----
def resolve_workbench_data_dir() -> Path | None:
    candidates: list[Path] = []
    env_dir = os.environ.get("CLAUDE_CAGE_DATA_DIR", "").strip()
    if env_dir:
        candidates.append(Path(env_dir).expanduser())
    env_cfg = os.environ.get("CLAUDE_CAGE_CONFIG", "").strip()
    if env_cfg:
        candidates.append(Path(env_cfg).expanduser().resolve().parent)

    # Auto-detect when installed inside <DataDir>/plugins/<plugin-id>/mesh_engine.py
    here = Path(__file__).resolve()
    if len(here.parents) >= 3 and here.parents[1].name.lower() == "plugins":
        candidates.append(here.parents[2])

    # Standard OS data locations
    if os.name == "nt":
        for var in ("APPDATA", "LOCALAPPDATA"):
            base = os.environ.get(var, "").strip()
            if base:
                candidates.append(Path(base) / "claude-cage")
                candidates.append(Path(base) / "ClaudeCage")
                candidates.append(Path(base) / "Cage")
        candidates.append(Path.home() / ".claude-cage")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "claude-cage")
        candidates.append(Path.home() / "Library" / "Application Support" / "Claude Cage")
        candidates.append(Path.home() / ".claude-cage")
    else:
        xdg = os.environ.get("XDG_DATA_HOME", "").strip()
        if xdg:
            candidates.append(Path(xdg) / "claude-cage")
        candidates.append(Path.home() / ".local" / "share" / "claude-cage")
        candidates.append(Path.home() / ".claude-cage")

    for c in candidates:
        try:
            if c.exists() and c.is_dir():
                if any((c / name).exists() for name in ("projects.json", "claude-accounts.json", "config.json")):
                    return c
        except OSError:
            continue
    return None


def resolve_workbench_cli() -> str | None:
    env_bin = os.environ.get("CLAUDE_CAGE_BIN", "").strip()
    if env_bin and Path(env_bin).exists():
        return env_bin

    found = shutil.which("cage")
    if found:
        return found

    candidates: list[Path] = []
    data_dir = resolve_workbench_data_dir()
    if os.name == "nt":
        if data_dir:
            candidates.append(data_dir.parent / "Cage" / "resources" / "cage.exe")
            candidates.append(data_dir.parent / "resources" / "cage.exe")
        for var in ("LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(var, "").strip()
            if base:
                candidates.append(Path(base) / "Programs" / "Cage" / "resources" / "cage.exe")
                candidates.append(Path(base) / "Cage" / "resources" / "cage.exe")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Cage.app/Contents/Resources/cage"))
        candidates.append(Path.home() / "Applications" / "Cage.app" / "Contents/Resources/cage")
    else:
        candidates.append(Path("/usr/local/bin/cage"))
        candidates.append(Path("/usr/bin/cage"))
        candidates.append(Path.home() / ".local" / "bin" / "cage")

    for c in candidates:
        try:
            if c.exists() and c.is_file():
                return str(c)
        except OSError:
            continue
    return None


def list_local_workspace_projects() -> list[dict[str, Any]]:
    data_dir = resolve_workbench_data_dir()
    if not data_dir:
        return []
    pfile = data_dir / "projects.json"
    if not pfile.exists():
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
        if not Path(ws).exists():
            continue
        norm = normalize_path(ws)
        if norm in seen:
            continue
        seen.add(norm)
        name = Path(ws.rstrip("/\\")).name or ws
        items.append({"name": name, "path": ws, "folders": [ws]})
    return items


def list_local_desktop_instances() -> list[dict[str, Any]]:
    data_dir = resolve_workbench_data_dir()
    if not data_dir:
        return []
    afile = data_dir / "claude-accounts.json"
    if not afile.exists():
        return []
    data = load_json_file(afile)
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
        has_code_access = (
            cli_status == "ready"
            and (not code_unlinked)
            and raw_plan.lower() != "free"
        )
        plan_label = raw_plan.capitalize() if raw_plan else ("Free" if not has_code_access else "Pro")
        out.append(
            {
                "id": acc_id,
                "number": num,
                "email": email,
                "desktop_instance_id": str(acc.get("desktop_instance_id") or ""),
                "desktop_status": desktop_status,
                "cli_status": cli_status,
                "cli_ready": has_code_access,
                "has_code_access": has_code_access,
                "plan": raw_plan,
                "plan_label": plan_label,
            }
        )
    out.sort(key=lambda x: (x["number"] is None, x["number"] or 999, x["email"]))
    return out


# ---- Optional Antigravity Bridge Adapter ----
def ag_bridge_url() -> str:
    return (
        os.environ.get("AGENT_MESH_AG_BRIDGE_URL")
        or os.environ.get("CAGE_AG_BRIDGE_URL")
        or DEFAULT_AG_BRIDGE_URL
    ).rstrip("/")


def load_ag_bridge_token() -> str | None:
    for var in ("AGENT_MESH_AG_TOKEN", "CAGE_AG_BRIDGE_TOKEN"):
        v = os.environ.get(var, "").strip()
        if v:
            return v
    candidates: list[Path] = []
    for var in ("AGENT_MESH_AG_TOKEN_FILE", "CAGE_AG_BRIDGE_TOKEN_FILE"):
        p = os.environ.get(var, "").strip()
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
    # Minimal fallback if config.toml has syntax variations or Python < 3.11
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
    if os.name == "nt":
        local_app = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app:
            bin_root = Path(local_app) / "OpenAI" / "Codex" / "bin"
            try:
                if bin_root.exists():
                    exes = sorted(bin_root.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
                    candidates.extend(exes)
            except OSError:
                pass
        app_data = os.environ.get("APPDATA", "").strip()
        if app_data:
            candidates.append(Path(app_data) / "npm" / "codex.cmd")
            candidates.append(Path(app_data) / "npm" / "codex")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Codex.app/Contents/Resources/codex"))
        bin_root = Path.home() / "Library" / "Application Support" / "OpenAI" / "Codex" / "bin"
        try:
            if bin_root.exists():
                candidates.extend(sorted(bin_root.glob("*/codex"), key=lambda p: p.stat().st_mtime, reverse=True))
        except OSError:
            pass
    else:
        candidates.extend([Path.home() / ".local" / "bin" / "codex", Path("/usr/local/bin/codex")])

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

    # Primary source: Codex Desktop SQLite state (read-only)
    for db_path in db_files[:1]:
        con: sqlite3.Connection | None = None
        try:
            con = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=2.0)
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

    # Fallback: trusted project entries in ~/.codex/config.toml
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


def run_cmd(
    args: list[str],
    timeout: int = 60,
    env_override: dict[str, str] | None = None,
) -> dict[str, Any]:
    env = os.environ.copy()
    if env_override:
        env.update(env_override)
    try:
        proc = subprocess.run(
            args,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            env=env,
        )
        return {
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except FileNotFoundError:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": f"Executable not found: {args[0]}"}
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": -2,
            "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
            "stderr": f"Command timed out after {timeout}s",
        }
    except Exception as exc:
        return {"ok": False, "returncode": -3, "stdout": "", "stderr": str(exc)}


# ---- Models & Multi-Collaborator Project Normalization ----
def sanitize_models_for_view(models: dict[str, Any]) -> dict[str, Any]:
    secrets_map = load_json_file(SECRETS_FILE)
    out: dict[str, Any] = {}
    for k, rec in models.items():
        if not isinstance(rec, dict):
            continue
        item = dict(rec)
        item.pop("api_key", None)
        raw_key = str(secrets_map.get(f"model:{k}") or "")
        item["has_api_key"] = bool(raw_key)
        item["api_key_masked"] = mask_secret(raw_key)
        out[k] = item
    return out


def normalize_project_record(
    key: str,
    rec: dict[str, Any],
    models_map: dict[str, Any],
    instances: list[dict[str, Any]],
) -> dict[str, Any]:
    item = dict(rec)
    item.pop("api_key", None)
    item["key"] = key
    item["display_name"] = str(item.get("display_name") or key)
    item["host_workspace"] = str(item.get("host_workspace") or "")
    item["cage_workspace"] = str(item.get("cage_workspace") or item["host_workspace"])

    raw_collabs = item.get("collaborators")
    collabs: list[dict[str, Any]] = []

    if isinstance(raw_collabs, list):
        for c in raw_collabs:
            if not isinstance(c, dict):
                continue
            ctype = str(c.get("type") or "").strip()
            if ctype == "antigravity" and c.get("project_id"):
                collabs.append(
                    {
                        "id": str(c.get("id") or "antigravity"),
                        "type": "antigravity",
                        "label": str(c.get("label") or "Antigravity"),
                        "project_id": str(c.get("project_id")),
                        "model": str(c.get("model") or ""),
                        "thinking_level": str(c.get("thinking_level") or ""),
                    }
                )
            elif ctype == "openai_compat":
                mref = str(c.get("model_ref") or "").strip()
                mcfg = models_map.get(mref) if mref else None
                collabs.append(
                    {
                        "id": str(c.get("id") or (f"model:{mref}" if mref else "api")),
                        "type": "openai_compat",
                        "model_ref": mref,
                        "label": str((mcfg or {}).get("name") or c.get("label") or mref or c.get("default_model") or "API"),
                        "model_id": str((mcfg or {}).get("model_id") or c.get("model_id") or c.get("default_model") or ""),
                        "api_base": str((mcfg or {}).get("api_base") or c.get("api_base") or ""),
                        "thinking_level": str((mcfg or {}).get("thinking_level") or c.get("thinking_level") or ""),
                    }
                )
            elif ctype == "codex":
                ws_val = str(c.get("workspace_path") or item["host_workspace"]).strip()
                disp = str(c.get("label") or (Path(ws_val.rstrip("/\\")).name if ws_val else "") or "Codex")
                collabs.append(
                    {
                        "id": str(c.get("id") or "codex"),
                        "type": "codex",
                        "label": disp,
                        "project_id": str(c.get("project_id") or ""),
                        "workspace_path": ws_val,
                        "model": str(c.get("model") or ""),
                        "thinking_level": str(c.get("thinking_level") or ""),
                        "sandbox_mode": str(c.get("sandbox_mode") or "workspace-write"),
                    }
                )
            elif ctype == "claude_instance" and c.get("account_id"):
                acc_id = str(c.get("account_id"))
                acc = next((a for a in instances if a["id"] == acc_id), None)
                num = acc["number"] if acc else c.get("account_number")
                email = acc["email"] if acc else str(c.get("email") or acc_id[:8])
                cli_ready = bool(acc["cli_ready"]) if acc else False
                has_code = bool(acc.get("has_code_access", cli_ready)) if acc else bool(c.get("workspace_path"))
                ws_val = str(c.get("workspace_path") or (item["host_workspace"] if has_code else ""))
                collabs.append(
                    {
                        "id": str(c.get("id") or f"claude:{acc_id}"),
                        "type": "claude_instance",
                        "account_id": acc_id,
                        "account_number": num,
                        "email": email,
                        "cli_ready": cli_ready,
                        "has_code_access": has_code,
                        "workspace_path": ws_val if has_code else "",
                        "model": str(c.get("model") or ""),
                        "thinking_level": str(c.get("thinking_level") or ""),
                        "label": f"#{num} {email}" if num is not None else email,
                    }
                )
    else:
        legacy_prov = item.get("provider") or "antigravity"
        if legacy_prov == "openai_compat":
            mref = str(item.get("model_ref") or "").strip()
            mcfg = models_map.get(mref) if mref else None
            collabs.append(
                {
                    "id": f"model:{mref}" if mref else "api",
                    "type": "openai_compat",
                    "model_ref": mref,
                    "label": str((mcfg or {}).get("name") or mref or item.get("default_model") or "API"),
                    "model_id": str((mcfg or {}).get("model_id") or item.get("default_model") or ""),
                    "api_base": str((mcfg or {}).get("api_base") or item.get("api_base") or ""),
                    "thinking_level": str((mcfg or {}).get("thinking_level") or item.get("thinking_level") or ""),
                }
            )
        elif item.get("project_id"):
            collabs.append(
                {
                    "id": "antigravity",
                    "type": "antigravity",
                    "label": str(item.get("display_name") or "Antigravity"),
                    "project_id": str(item.get("project_id")),
                    "model": str(item.get("default_model") or ""),
                }
            )

    item["collaborators"] = collabs
    return item


def get_all_normalized_projects() -> dict[str, dict[str, Any]]:
    with StateLock():
        raw_projects = load_json_file(PROJECTS_FILE)
        models_map = load_json_file(MODELS_FILE)
    instances = list_local_desktop_instances()
    return {
        k: normalize_project_record(k, v, models_map, instances)
        for k, v in raw_projects.items()
        if isinstance(v, dict)
    }


def resolve_project_and_collaborator(
    project_key: str | None,
    project_dir: str | None,
    target: str | None,
    project_id_explicit: str | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if project_id_explicit:
        return (
            {"key": project_key or "explicit", "display_name": project_key or "explicit", "host_workspace": project_dir or ""},
            {"id": "antigravity", "type": "antigravity", "project_id": project_id_explicit, "label": "Antigravity", "model": ""},
        )

    projects = get_all_normalized_projects()
    matched_proj: dict[str, Any] | None = None

    if project_key and project_key in projects:
        matched_proj = projects[project_key]
    elif project_key:
        pk_low = project_key.lower()
        for k, p in projects.items():
            if k.lower() == pk_low or p["display_name"].lower() == pk_low:
                matched_proj = p
                break

    if not matched_proj and project_dir:
        norm_target = normalize_path(project_dir)
        base_name = Path(project_dir.rstrip("/\\")).name.lower()
        for k, p in projects.items():
            if p["cage_workspace"] and normalize_path(p["cage_workspace"]) == norm_target:
                matched_proj = p
                break
            if p["host_workspace"] and normalize_path(p["host_workspace"]) == norm_target:
                matched_proj = p
                break
            if k.lower() == base_name:
                matched_proj = p
                break

    if not matched_proj:
        raise ValueError(
            "No matching collaboration project found. Create a project in Agent Mesh first, or pass 'project' / 'project_id'."
        )

    collabs = matched_proj.get("collaborators") or []
    if not collabs:
        raise ValueError(f"Project '{matched_proj['key']}' has no collaborators configured.")

    if not target:
        return matched_proj, collabs[0]

    t_low = target.strip().lower()
    for c in collabs:
        if c["id"].lower() == t_low or c["type"].lower() == t_low:
            return matched_proj, c
    for c in collabs:
        hay = " ".join(
            str(x)
            for x in (
                c.get("id"),
                c.get("type"),
                c.get("label"),
                c.get("model_ref"),
                c.get("model_id"),
                c.get("email"),
                f"#{c.get('account_number')}" if c.get("account_number") is not None else "",
                f"claude#{c.get('account_number')}" if c.get("account_number") is not None else "",
            )
            if x
        ).lower()
        if t_low in hay:
            return matched_proj, c

    avail = ", ".join(f"{c['id']} ({c['label']})" for c in collabs)
    raise ValueError(f"Target '{target}' not found in project '{matched_proj['key']}'. Available: {avail}")


# ---- Transcript & Session Readers ----
def default_ag_transcript_path(conversation_id: str) -> Path:
    return (
        Path.home()
        / ".gemini"
        / "antigravity"
        / "brain"
        / conversation_id
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )


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


def api_session_path(conversation_id: str) -> Path:
    return API_SESSIONS_DIR / f"{conversation_id}.json"


def load_api_session(conversation_id: str) -> dict[str, Any]:
    data = load_json_file(api_session_path(conversation_id))
    return data if data else {"conversation_id": conversation_id, "turns": []}


def save_api_session(conversation_id: str, session_data: dict[str, Any]) -> None:
    ensure_dirs()
    save_json_file(api_session_path(conversation_id), session_data)


def read_local_session_summary(conversation_id: str, last_n: int = 20) -> dict[str, Any]:
    p = api_session_path(conversation_id)
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
    data = load_api_session(conversation_id)
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


def read_conv_transcript(
    rec: dict[str, Any],
    cid: str,
    last_n: int = 20,
    include_full_latest: bool = True,
) -> dict[str, Any]:
    prov = rec.get("provider") or "antigravity"
    if prov in ("openai_compat", "claude_instance", "codex"):
        return read_local_session_summary(cid, last_n=last_n)
    tpath = Path(rec.get("transcript_path") or default_ag_transcript_path(cid))
    return read_ag_transcript_summary(tpath, last_n=last_n, include_full_latest=include_full_latest)


def compute_effective_status(record: dict[str, Any], transcript: dict[str, Any]) -> str:
    inferred = transcript.get("inferred_state")
    if inferred in ("waiting_approval", "waiting_relay"):
        return inferred
    if record.get("status") == "completed" and record.get("fully_idle") is True:
        if transcript.get("exists") and inferred == "running":
            return "running"
        return "completed"
    if transcript.get("exists") and inferred in ("completed", "running", "error"):
        return inferred
    return record.get("status") or "unknown"


# ---- Execution Backends ----
def build_chat_completions_url(api_base: str) -> str:
    base = api_base.strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1") or base.endswith("/v1beta") or base.endswith("/v2"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def resolve_model_runtime(collab: dict[str, Any], proj_key: str) -> dict[str, str]:
    mref = str(collab.get("model_ref") or "").strip()
    if mref:
        with StateLock():
            models_map = load_json_file(MODELS_FILE)
        mcfg = models_map.get(mref) or {}
        if mcfg:
            return {
                "api_base": str(mcfg.get("api_base") or "").strip(),
                "model_id": str(mcfg.get("model_id") or "").strip(),
                "thinking_level": str(mcfg.get("thinking_level") or "").strip(),
                "system_prompt": str(mcfg.get("system_prompt") or "").strip(),
                "api_key": get_secret(f"model:{mref}"),
            }
    return {
        "api_base": str(collab.get("api_base") or "").strip(),
        "model_id": str(collab.get("model_id") or "").strip(),
        "thinking_level": str(collab.get("thinking_level") or "").strip(),
        "system_prompt": str(collab.get("system_prompt") or "").strip(),
        "api_key": get_secret(proj_key),
    }


def _run_openai_compat_turn(
    conversation_id: str,
    api_base: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    thinking_level: str = "",
) -> None:
    url = build_chat_completions_url(api_base)
    with StateLock():
        sess = load_api_session(conversation_id)
        turns = list(sess.get("turns") or [])
        turns.append({"role": "user", "step_index": len(turns), "created_at": utc_now(), "content": user_message})
        sess["turns"] = turns
        sess["status"] = "running"
        sess["error"] = None
        save_api_session(conversation_id, sess)

    messages: list[dict[str, str]] = []
    if system_prompt.strip():
        messages.append({"role": "system", "content": system_prompt.strip()})
    for t in turns:
        if t.get("role") in ("user", "assistant"):
            messages.append({"role": t["role"], "content": str(t.get("content") or "")})

    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
    valid_effort = thinking_level.strip().lower()
    if valid_effort in ("minimal", "low", "medium", "high", "xhigh"):
        payload["reasoning_effort"] = valid_effort

    headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
    if api_key.strip():
        headers["Authorization"] = f"Bearer {api_key.strip()}"

    parsed_host = (urlparse(url).hostname or "").lower()
    opener = (
        urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if parsed_host in ("127.0.0.1", "localhost", "::1")
        else urllib.request.build_opener()
    )

    def _post(body_obj: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            url,
            data=json.dumps(body_obj, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with opener.open(req, timeout=180) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))

    reply_text = ""
    reasoning_text = ""
    error_msg: str | None = None
    try:
        try:
            obj = _post(payload)
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace")[:500]
            if exc.code in (400, 422) and "reasoning_effort" in payload:
                fb = dict(payload)
                fb.pop("reasoning_effort", None)
                obj = _post(fb)
            else:
                raise RuntimeError(f"API HTTP {exc.code}: {err_body}") from exc
        choices = obj.get("choices") or []
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            reply_text = str(msg.get("content") or "").strip()
            reasoning_text = str(msg.get("reasoning_content") or "").strip()
        if not reply_text and reasoning_text:
            reply_text = reasoning_text
        if not reply_text:
            error_msg = f"Empty response from API: {json.dumps(obj, ensure_ascii=False)[:300]}"
    except urllib.error.HTTPError as exc:
        error_msg = f"API HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:500]}"
    except Exception as exc:
        error_msg = f"API request failed: {exc}"

    with StateLock():
        sess = load_api_session(conversation_id)
        turns = list(sess.get("turns") or [])
        if error_msg:
            sess["status"] = "error"
            sess["error"] = error_msg
            turns.append(
                {"role": "assistant", "step_index": len(turns), "created_at": utc_now(), "content": f"[API Error] {error_msg}"}
            )
        else:
            sess["status"] = "completed"
            sess["error"] = None
            tobj: dict[str, Any] = {
                "role": "assistant",
                "step_index": len(turns),
                "created_at": utc_now(),
                "content": reply_text,
            }
            if reasoning_text:
                tobj["reasoning_content"] = reasoning_text
            turns.append(tobj)
        sess["turns"] = turns
        save_api_session(conversation_id, sess)

        convs = load_json_file(CONVS_FILE)
        rec = convs.get(conversation_id, {"conversation_id": conversation_id, "created_at": utc_now()})
        rec["updated_at"] = utc_now()
        rec["status"] = "error" if error_msg else "completed"
        rec["fully_idle"] = True
        if error_msg:
            rec["error"] = error_msg
        convs[conversation_id] = rec
        save_json_file(CONVS_FILE, convs)

    with TURN_EVENT:
        TURN_EVENT.notify_all()


def _run_claude_instance_turn(
    conversation_id: str,
    account_id: str,
    account_email: str,
    user_message: str,
    cli_ready: bool,
    claude_session_id: str = "",
    model: str = "",
) -> None:
    with StateLock():
        sess = load_api_session(conversation_id)
        turns = list(sess.get("turns") or [])
        turns.append({"role": "user", "step_index": len(turns), "created_at": utc_now(), "content": user_message})
        sess["turns"] = turns
        sess["target_account_id"] = account_id
        sess["target_email"] = account_email
        sess["status"] = "running" if cli_ready else "waiting_relay"
        sess["error"] = None
        save_api_session(conversation_id, sess)

    if not cli_ready:
        with StateLock():
            convs = load_json_file(CONVS_FILE)
            rec = convs.get(conversation_id, {"conversation_id": conversation_id, "created_at": utc_now()})
            rec["updated_at"] = utc_now()
            rec["status"] = "waiting_relay"
            rec["fully_idle"] = False
            convs[conversation_id] = rec
            save_json_file(CONVS_FILE, convs)
        with TURN_EVENT:
            TURN_EVENT.notify_all()
        log(f"Instance {account_email} is Desktop-only (no Code CLI); queued in relay inbox: {conversation_id}")
        return

    cli_bin = resolve_workbench_cli()
    if not cli_bin:
        res = {"ok": False, "stdout": "", "stderr": "Workbench CLI binary not found on system"}
    else:
        cfg_dir = f"$HOME/.local/share/cage/claude-accounts/{account_id}/config"
        resume_part = f" --resume {shlex.quote(claude_session_id)}" if claude_session_id else ""
        model_part = f" --model {shlex.quote(model)}" if model else ""
        inner = (
            f'CLAUDE_CONFIG_DIR="{cfg_dir}" claude -p {shlex.quote(user_message)} '
            f"--output-format json{model_part}{resume_part}"
        )
        res = run_cmd([cli_bin, "run", "bash", "-lc", inner], timeout=180)

    reply_text = ""
    new_sid = claude_session_id
    error_msg: str | None = None
    if res["ok"]:
        raw_out = (res["stdout"] or "").strip()
        try:
            obj = json.loads(raw_out)
            reply_text = str(obj.get("result") or obj.get("content") or raw_out).strip()
            if obj.get("session_id"):
                new_sid = str(obj["session_id"])
        except Exception:
            reply_text = raw_out or "(empty response)"
    else:
        error_msg = (res.get("stderr") or res.get("stdout") or "Instance execution failed").strip()[:600]

    with StateLock():
        sess = load_api_session(conversation_id)
        turns = list(sess.get("turns") or [])
        if error_msg:
            sess["status"] = "error"
            sess["error"] = error_msg
            turns.append(
                {"role": "assistant", "step_index": len(turns), "created_at": utc_now(), "content": f"[Instance Error] {error_msg}"}
            )
        else:
            sess["status"] = "completed"
            sess["error"] = None
            if new_sid:
                sess["claude_session_id"] = new_sid
            turns.append({"role": "assistant", "step_index": len(turns), "created_at": utc_now(), "content": reply_text})
        sess["turns"] = turns
        save_api_session(conversation_id, sess)

        convs = load_json_file(CONVS_FILE)
        rec = convs.get(conversation_id, {"conversation_id": conversation_id, "created_at": utc_now()})
        rec["updated_at"] = utc_now()
        rec["status"] = "error" if error_msg else "completed"
        rec["fully_idle"] = True
        if new_sid:
            rec["claude_session_id"] = new_sid
        convs[conversation_id] = rec
        save_json_file(CONVS_FILE, convs)

    with TURN_EVENT:
        TURN_EVENT.notify_all()


def _run_codex_via_app_server(
    codex_bin: str,
    workspace_path: str,
    user_message: str,
    codex_thread_id: str,
    model: str,
    thinking_level: str,
    sandbox_mode: str,
    timeout: int = 300,
) -> tuple[bool, str, str, str, str | None]:
    """
    Run a turn via official `codex app-server --stdio` JSON-RPC 2.0 protocol.
    Threads created this way use Codex's native Desktop source ('vscode'), so they
    appear directly in the Codex Desktop sidebar in addition to Agent Mesh history.
    Returns: (supported, thread_id, reply_text, reasoning_text, error_msg)
    """
    try:
        proc = subprocess.Popen(
            [codex_bin, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        return False, codex_thread_id, "", "", str(exc)

    assert proc.stdin and proc.stdout and proc.stderr
    stderr_chunks: list[str] = []

    def _drain_stderr() -> None:
        try:
            for line in proc.stderr:  # type: ignore[union-attr]
                if len(stderr_chunks) < 40:
                    stderr_chunks.append(line)
        except Exception:
            pass

    threading.Thread(target=_drain_stderr, daemon=True).start()

    def _send(obj: dict[str, Any]) -> None:
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    def _recv_until(
        target_id: int | None = None,
        stop_methods: tuple[str, ...] = (),
        wait_sec: float = 30.0,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        collected: list[dict[str, Any]] = []
        deadline = time.monotonic() + wait_sec
        while time.monotonic() < deadline:
            line = proc.stdout.readline()  # type: ignore[union-attr]
            if not line:
                break
            s = line.strip()
            if not s or not s.startswith("{"):
                continue
            try:
                msg = json.loads(s)
            except json.JSONDecodeError:
                continue
            collected.append(msg)
            if target_id is not None and msg.get("id") == target_id:
                return msg, collected
            if msg.get("method") in stop_methods:
                return msg, collected
        return None, collected

    new_tid = codex_thread_id
    try:
        _send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "Codex Desktop", "title": "Agent Mesh", "version": VERSION},
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        init_res, _ = _recv_until(target_id=1, wait_sec=15.0)
        if not init_res or "error" in init_res:
            err = (init_res or {}).get("error") or "".join(stderr_chunks).strip()
            return False, new_tid, "", "", f"app-server init failed: {err}"

        _send({"jsonrpc": "2.0", "method": "initialized"})

        base_params: dict[str, Any] = {
            "sandbox": sandbox_mode,
            "approvalPolicy": "never",
        }
        if workspace_path and Path(workspace_path).exists():
            base_params["cwd"] = workspace_path
        if model.strip():
            base_params["model"] = model.strip()

        if codex_thread_id:
            resume_params = dict(base_params)
            resume_params["threadId"] = codex_thread_id
            _send({"jsonrpc": "2.0", "id": 2, "method": "thread/resume", "params": resume_params})
        else:
            _send({"jsonrpc": "2.0", "id": 2, "method": "thread/start", "params": base_params})

        th_res, _ = _recv_until(target_id=2, wait_sec=20.0)
        if not th_res or "error" in th_res:
            err = (th_res or {}).get("error") or "".join(stderr_chunks).strip()
            return True, new_tid, "", "", f"Codex thread start/resume failed: {err}"

        res_obj = th_res.get("result") or {}
        tid_val = (res_obj.get("thread") or {}).get("id") or res_obj.get("threadId")
        if isinstance(tid_val, str) and tid_val.strip():
            new_tid = tid_val.strip()
        if not new_tid:
            return True, "", "", "", "Codex app-server did not return a thread id"

        turn_params: dict[str, Any] = {
            "threadId": new_tid,
            "input": [{"type": "text", "text": user_message}],
        }
        effort = thinking_level.strip().lower()
        if effort in ("minimal", "low", "medium", "high", "xhigh"):
            turn_params["effort"] = effort
        if model.strip():
            turn_params["model"] = model.strip()

        _send({"jsonrpc": "2.0", "id": 3, "method": "turn/start", "params": turn_params})
        turn_ack, ev_pre = _recv_until(target_id=3, wait_sec=20.0)
        if not turn_ack or "error" in turn_ack:
            err = (turn_ack or {}).get("error") or "".join(stderr_chunks).strip()
            return True, new_tid, "", "", f"Codex turn/start failed: {err}"

        done_msg, ev_post = _recv_until(
            stop_methods=("turn/completed", "turn/failed", "error"),
            wait_sec=float(timeout),
        )
        all_events = ev_pre + ev_post
        delta_chunks: list[str] = []
        completed_reply = ""
        reasoning_parts: list[str] = []

        for ev in all_events:
            m = ev.get("method")
            params = ev.get("params") or {}
            if m == "item/agentMessage/delta":
                delta = params.get("delta")
                if isinstance(delta, str):
                    delta_chunks.append(delta)
            elif m == "item/completed":
                item = params.get("item") or {}
                itype = item.get("type")
                if itype in ("agentMessage", "agent_message") and isinstance(item.get("text"), str):
                    completed_reply = item["text"].strip()
                elif itype == "reasoning":
                    rtxt = item.get("text") or item.get("summary") or ""
                    if isinstance(rtxt, list):
                        rtxt = "\n".join(str(x) for x in rtxt if x)
                    if isinstance(rtxt, str) and rtxt.strip():
                        reasoning_parts.append(rtxt.strip())

        reply_text = completed_reply or "".join(delta_chunks).strip()
        if not done_msg:
            return True, new_tid, reply_text, "\n\n".join(reasoning_parts).strip(), f"Codex turn timed out after {timeout}s"

        if done_msg.get("method") in ("turn/failed", "error"):
            err_detail = json.dumps(done_msg.get("params") or done_msg, ensure_ascii=False)[:600]
            return True, new_tid, reply_text, "\n\n".join(reasoning_parts).strip(), f"Codex turn failed: {err_detail}"

        turn_info = (done_msg.get("params") or {}).get("turn") or {}
        if not reply_text:
            for item in turn_info.get("items") or []:
                if isinstance(item, dict) and item.get("type") in ("agentMessage", "agent_message"):
                    txt = str(item.get("text") or "").strip()
                    if txt:
                        reply_text = txt
        if turn_info.get("status") == "failed" and not reply_text:
            err_detail = json.dumps(turn_info.get("error") or turn_info, ensure_ascii=False)[:600]
            return True, new_tid, "", "\n\n".join(reasoning_parts).strip(), f"Codex turn failed: {err_detail}"
        if not reply_text:
            return True, new_tid, "", "\n\n".join(reasoning_parts).strip(), "Codex returned an empty response."

        return True, new_tid, reply_text, "\n\n".join(reasoning_parts).strip(), None
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _run_codex_turn(
    conversation_id: str,
    workspace_path: str,
    user_message: str,
    codex_thread_id: str = "",
    model: str = "",
    thinking_level: str = "",
    sandbox_mode: str = "workspace-write",
) -> None:
    with StateLock():
        sess = load_api_session(conversation_id)
        turns = list(sess.get("turns") or [])
        turns.append({"role": "user", "step_index": len(turns), "created_at": utc_now(), "content": user_message})
        sess["turns"] = turns
        sess["status"] = "running"
        sess["error"] = None
        save_api_session(conversation_id, sess)

    codex_bin = resolve_codex_cli()
    reply_text = ""
    reasoning_parts: list[str] = []
    new_tid = codex_thread_id
    error_msg: str | None = None

    if not codex_bin:
        error_msg = "Codex CLI not found on this machine. Install Codex Desktop or Codex CLI first."
    else:
        valid_sandbox = sandbox_mode if sandbox_mode in ("read-only", "workspace-write", "danger-full-access") else "workspace-write"
        # Primary path: official `codex app-server --stdio` so threads appear natively in Codex Desktop sidebar
        supported, app_tid, app_reply, app_reasoning, app_err = _run_codex_via_app_server(
            codex_bin=codex_bin,
            workspace_path=workspace_path,
            user_message=user_message,
            codex_thread_id=codex_thread_id,
            model=model,
            thinking_level=thinking_level,
            sandbox_mode=valid_sandbox,
            timeout=300,
        )
        if supported:
            new_tid = app_tid or new_tid
            reply_text = app_reply
            if app_reasoning:
                reasoning_parts.append(app_reasoning)
            error_msg = app_err
        else:
            # Fallback for older Codex CLI builds without `app-server`
            with tempfile.TemporaryDirectory(prefix="agent_mesh_codex_") as tmpdir:
                out_file = Path(tmpdir) / "last_message.txt"
                if codex_thread_id:
                    cmd = [codex_bin, "exec", "resume", codex_thread_id, "--json", "--skip-git-repo-check"]
                else:
                    cmd = [codex_bin, "exec", "--json", "--skip-git-repo-check", "-s", valid_sandbox]
                    if workspace_path and Path(workspace_path).exists():
                        cmd.extend(["-C", workspace_path])
                if model.strip():
                    cmd.extend(["-m", model.strip()])
                effort = thinking_level.strip().lower()
                if effort in ("minimal", "low", "medium", "high", "xhigh"):
                    cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])
                cmd.extend(["-o", str(out_file), "-"])

                try:
                    proc = subprocess.run(
                        cmd,
                        input=user_message,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=300,
                        shell=False,
                    )
                except subprocess.TimeoutExpired:
                    proc = None
                    error_msg = "Codex execution timed out after 300s"
                except Exception as exc:
                    proc = None
                    error_msg = f"Failed to start Codex CLI: {exc}"

                if proc is not None:
                    jsonl_reply = ""
                    for raw_line in (proc.stdout or "").splitlines():
                        line = raw_line.strip()
                        if not line or not line.startswith("{"):
                            continue
                        try:
                            ev = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        etype = ev.get("type")
                        if etype == "thread.started" and ev.get("thread_id"):
                            new_tid = str(ev["thread_id"])
                        elif etype == "item.completed":
                            item = ev.get("item") or {}
                            itype = item.get("type")
                            if itype == "agent_message" and isinstance(item.get("text"), str):
                                jsonl_reply = item["text"].strip()
                            elif itype == "reasoning":
                                rtxt = item.get("text") or item.get("summary") or ""
                                if isinstance(rtxt, list):
                                    rtxt = "\n".join(str(x) for x in rtxt if x)
                                if isinstance(rtxt, str) and rtxt.strip():
                                    reasoning_parts.append(rtxt.strip())

                    if out_file.exists():
                        file_reply = out_file.read_text(encoding="utf-8", errors="replace").strip()
                        if file_reply:
                            reply_text = file_reply
                    if not reply_text and jsonl_reply:
                        reply_text = jsonl_reply

                    if proc.returncode != 0 and not reply_text:
                        err_detail = (proc.stderr or proc.stdout or f"exit code {proc.returncode}").strip()
                        error_msg = err_detail[:800]
                    elif not reply_text:
                        error_msg = "Codex returned an empty response."

    reasoning_text = "\n\n".join(reasoning_parts).strip()

    with StateLock():
        sess = load_api_session(conversation_id)
        turns = list(sess.get("turns") or [])
        if error_msg:
            sess["status"] = "error"
            sess["error"] = error_msg
            turns.append(
                {"role": "assistant", "step_index": len(turns), "created_at": utc_now(), "content": f"[Codex Error] {error_msg}"}
            )
        else:
            sess["status"] = "completed"
            sess["error"] = None
            if new_tid:
                sess["codex_thread_id"] = new_tid
            tobj: dict[str, Any] = {
                "role": "assistant",
                "step_index": len(turns),
                "created_at": utc_now(),
                "content": reply_text,
            }
            if reasoning_text:
                tobj["reasoning_content"] = reasoning_text
            turns.append(tobj)
        sess["turns"] = turns
        save_api_session(conversation_id, sess)

        convs = load_json_file(CONVS_FILE)
        rec = convs.get(conversation_id, {"conversation_id": conversation_id, "created_at": utc_now()})
        rec["updated_at"] = utc_now()
        rec["status"] = "error" if error_msg else "completed"
        rec["fully_idle"] = True
        if new_tid:
            rec["codex_thread_id"] = new_tid
        if error_msg:
            rec["error"] = error_msg
        convs[conversation_id] = rec
        save_json_file(CONVS_FILE, convs)

    with TURN_EVENT:
        TURN_EVENT.notify_all()


# ---- High-Level Public Engine API (Called Directly by MCP Server) ----
def get_health(active_project_dir: str = "") -> dict[str, Any]:
    ensure_dirs()
    with StateLock():
        convs = load_json_file(CONVS_FILE)
        projects = load_json_file(PROJECTS_FILE)
        models = load_json_file(MODELS_FILE)
    ag_info = probe_antigravity_bridge()
    codex_info = probe_codex()
    instances = list_local_desktop_instances()
    return {
        "ok": True,
        "version": VERSION,
        "mode": "embedded",
        "time": utc_now(),
        "state_dir": str(STATE_DIR),
        "active_project_dir": active_project_dir or None,
        "agentapi_available": ag_info["available"],
        "antigravity_bridge_online": ag_info["bridge_online"],
        "codex_available": codex_info["available"],
        "codex_default_model": codex_info["default_model"] or None,
        "default_project_id": ag_info["default_project_id"],
        "tracked_conversations": len(convs),
        "configured_projects": len(projects),
        "configured_models": len(models),
        "claude_instances_count": len(instances),
    }


def list_projects(active_project_dir: str = "") -> dict[str, Any]:
    return {
        "ok": True,
        "active_project_dir": active_project_dir or None,
        "projects": get_all_normalized_projects(),
    }


def discover_resources(force_refresh: bool = False, active_project_dir: str = "") -> dict[str, Any]:
    ag_projects: list[dict[str, Any]] = []
    scanned = 0
    ag_online = False
    qs = "?refresh=1" if force_refresh else "?refresh=0"
    ag_res = ag_bridge_request("GET", f"/discover{qs}", timeout=25)
    if ag_res and ag_res.get("ok"):
        ag_online = True
        ag_projects = ag_res.get("antigravity_projects") or []
        scanned = int(ag_res.get("scanned_conversations") or 0)

    codex_info = probe_codex()
    return {
        "ok": True,
        "active_project_dir": active_project_dir or None,
        "antigravity_bridge_online": ag_online,
        "codex_available": codex_info["available"],
        "codex_default_model": codex_info["default_model"],
        "codex_default_effort": codex_info["default_effort"],
        "codex_models": codex_info["models"],
        "codex_projects": list_local_codex_projects(),
        "workspace_projects": list_local_workspace_projects(),
        "antigravity_projects": ag_projects,
        "desktop_instances": list_local_desktop_instances(),
        "scanned_conversations": scanned,
    }


def save_project(body: dict[str, Any], active_project_dir: str = "") -> dict[str, Any]:
    key = str(body.get("key") or "").strip()
    if not key:
        raise ValueError("key is required")
    display_name = str(body.get("display_name") or key).strip()
    ws = str(body.get("workspace_path") or body.get("host_workspace") or "").strip()
    if not ws and body.get("workspace_path") is None:
        ws = active_project_dir

    controller_in = body.get("controller")
    controller_clean: dict[str, Any] | None = None
    if isinstance(controller_in, dict) and str(controller_in.get("account_id") or "").strip():
        controller_clean = {
            "account_id": str(controller_in.get("account_id")).strip(),
            "account_number": controller_in.get("account_number"),
            "email": str(controller_in.get("email") or "").strip(),
        }

    collabs_in = body.get("collaborators")
    if isinstance(collabs_in, list):
        cleaned_collabs: list[dict[str, Any]] = []
        for idx, c in enumerate(collabs_in):
            if not isinstance(c, dict):
                continue
            ctype = str(c.get("type") or "").strip()
            if ctype == "antigravity" and str(c.get("project_id") or "").strip():
                cleaned_collabs.append(
                    {
                        "id": str(c.get("id") or f"antigravity:{idx+1}"),
                        "type": "antigravity",
                        "label": str(c.get("label") or "Antigravity"),
                        "project_id": str(c.get("project_id")).strip(),
                        "model": str(c.get("model") or "").strip(),
                        "thinking_level": str(c.get("thinking_level") or "").strip(),
                    }
                )
            elif ctype == "openai_compat" and str(c.get("model_ref") or "").strip():
                mref = str(c.get("model_ref")).strip()
                cleaned_collabs.append(
                    {
                        "id": str(c.get("id") or f"model:{mref}"),
                        "type": "openai_compat",
                        "model_ref": mref,
                        "label": str(c.get("label") or mref),
                    }
                )
            elif ctype == "codex":
                ws_c = str(c.get("workspace_path") or ws).strip()
                cleaned_collabs.append(
                    {
                        "id": str(c.get("id") or f"codex:{idx+1}"),
                        "type": "codex",
                        "label": str(c.get("label") or (Path(ws_c.rstrip("/\\")).name if ws_c else "") or "Codex"),
                        "project_id": str(c.get("project_id") or "").strip(),
                        "workspace_path": ws_c,
                        "model": str(c.get("model") or "").strip(),
                        "thinking_level": str(c.get("thinking_level") or "").strip(),
                        "sandbox_mode": str(c.get("sandbox_mode") or "workspace-write").strip(),
                    }
                )
            elif ctype == "claude_instance" and str(c.get("account_id") or "").strip():
                aid = str(c.get("account_id")).strip()
                cleaned_collabs.append(
                    {
                        "id": str(c.get("id") or f"claude:{aid}"),
                        "type": "claude_instance",
                        "account_id": aid,
                        "account_number": c.get("account_number"),
                        "email": str(c.get("email") or "").strip(),
                        "workspace_path": str(c.get("workspace_path") or "").strip(),
                        "model": str(c.get("model") or "").strip(),
                        "thinking_level": str(c.get("thinking_level") or "").strip(),
                        "label": str(c.get("label") or c.get("email") or aid[:8]),
                    }
                )
        if not cleaned_collabs:
            raise ValueError("Please add at least one AI collaborator")
        entry = {
            "key": key,
            "display_name": display_name,
            "host_workspace": ws,
            "cage_workspace": ws,
            "controller": controller_clean,
            "collaborators": cleaned_collabs,
            "updated_at": utc_now(),
        }
    else:
        pid = str(body.get("project_id") or "").strip()
        if not pid:
            raise ValueError("project_id or collaborators is required")
        entry = {
            "key": key,
            "display_name": display_name,
            "host_workspace": ws,
            "cage_workspace": ws,
            "collaborators": [
                {
                    "id": "antigravity",
                    "type": "antigravity",
                    "label": display_name,
                    "project_id": pid,
                    "model": str(body.get("default_model") or "").strip(),
                }
            ],
            "updated_at": utc_now(),
        }

    with StateLock():
        projects = load_json_file(PROJECTS_FILE)
        projects[key] = entry
        save_json_file(PROJECTS_FILE, projects)
    norm = get_all_normalized_projects().get(key, entry)
    return {"ok": True, "key": key, "project": norm}


def delete_project(key: str) -> dict[str, Any]:
    with StateLock():
        projects = load_json_file(PROJECTS_FILE)
        removed = projects.pop(key, None)
        save_json_file(PROJECTS_FILE, projects)
    delete_secret(key)
    return {"ok": True, "removed": removed is not None, "key": key}


def list_models() -> dict[str, Any]:
    with StateLock():
        models = sanitize_models_for_view(load_json_file(MODELS_FILE))
    return {"ok": True, "models": models}


def save_model(body: dict[str, Any]) -> dict[str, Any]:
    key = str(body.get("key") or "").strip()
    name = str(body.get("name") or key).strip()
    api_base = str(body.get("api_base") or "").strip()
    model_id = str(body.get("model_id") or "").strip()
    thinking_level = str(body.get("thinking_level") or "").strip().lower()
    system_prompt = str(body.get("system_prompt") or "").strip()
    if not key:
        key = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "-", name or model_id).strip("-").lower() or secrets.token_hex(4)
    if not api_base or not model_id:
        raise ValueError("api_base and model_id are required")
    if thinking_level not in ("", "minimal", "low", "medium", "high", "xhigh"):
        raise ValueError("invalid thinking_level")

    raw_api_key = str(body.get("api_key") or "").strip()
    if raw_api_key:
        set_secret(f"model:{key}", raw_api_key)

    entry = {
        "key": key,
        "name": name or model_id,
        "api_base": api_base,
        "model_id": model_id,
        "thinking_level": thinking_level,
        "system_prompt": system_prompt,
        "updated_at": utc_now(),
    }
    with StateLock():
        models = load_json_file(MODELS_FILE)
        models[key] = entry
        save_json_file(MODELS_FILE, models)
        sanitized = sanitize_models_for_view({key: entry})[key]
    return {"ok": True, "key": key, "model": sanitized}


def delete_model(key: str) -> dict[str, Any]:
    with StateLock():
        models = load_json_file(MODELS_FILE)
        removed = models.pop(key, None)
        save_json_file(MODELS_FILE, models)
    delete_secret(f"model:{key}")
    return {"ok": True, "removed": removed is not None, "key": key}


def create_conversation(
    prompt: str,
    title: str | None = None,
    project: str | None = None,
    target: str | None = None,
    project_id: str | None = None,
    project_dir: str | None = None,
    model: str | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    proj, collab = resolve_project_and_collaborator(
        project_key=project,
        project_dir=project_dir,
        target=target,
        project_id_explicit=project_id,
    )
    ctype = collab["type"]

    if ctype == "openai_compat":
        rt = resolve_model_runtime(collab, proj["key"])
        api_base = rt["api_base"]
        chosen_model = str(model or rt["model_id"] or "").strip()
        if not api_base or not chosen_model:
            raise RuntimeError("Model configuration missing api_base or model_id")
        conv_id = str(uuid.uuid4())
        with StateLock():
            convs = load_json_file(CONVS_FILE)
            convs[conv_id] = {
                "conversation_id": conv_id,
                "provider": "openai_compat",
                "project_key": proj["key"],
                "collaborator_id": collab["id"],
                "collaborator_label": collab.get("label") or chosen_model,
                "model_ref": collab.get("model_ref") or "",
                "project_id": "",
                "host_workspace": proj.get("host_workspace") or "",
                "api_base": api_base,
                "title": title or f"{proj['display_name']} · {collab.get('label') or chosen_model}",
                "model": chosen_model,
                "thinking_level": rt["thinking_level"],
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "status": "running",
                "fully_idle": False,
                "last_prompt": prompt[:500],
            }
            save_json_file(CONVS_FILE, convs)
        threading.Thread(
            target=_run_openai_compat_turn,
            args=(conv_id, api_base, rt["api_key"], chosen_model, rt["system_prompt"], prompt, rt["thinking_level"]),
            daemon=True,
        ).start()
        return {
            "ok": True,
            "provider": "openai_compat",
            "mode": "api",
            "conversation_id": conv_id,
            "project_key": proj["key"],
            "collaborator_id": collab["id"],
            "collaborator_label": collab.get("label"),
            "project_id": "",
            "model": chosen_model,
            "target_email": None,
            "brief_path": None,
            "stderr": "",
        }

    if ctype == "codex":
        ws_path = str(collab.get("workspace_path") or proj.get("host_workspace") or project_dir or "").strip()
        chosen_model = str(model or collab.get("model") or "").strip()
        thinking = str(collab.get("thinking_level") or "").strip()
        sandbox_mode = str(collab.get("sandbox_mode") or "workspace-write").strip()
        conv_id = str(uuid.uuid4())
        with StateLock():
            convs = load_json_file(CONVS_FILE)
            convs[conv_id] = {
                "conversation_id": conv_id,
                "provider": "codex",
                "project_key": proj["key"],
                "collaborator_id": collab["id"],
                "collaborator_label": f"Codex · {collab.get('label') or proj['display_name']}",
                "project_id": str(collab.get("project_id") or ""),
                "host_workspace": ws_path,
                "title": title or f"{proj['display_name']} · Codex",
                "model": chosen_model,
                "thinking_level": thinking,
                "sandbox_mode": sandbox_mode,
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "status": "running",
                "fully_idle": False,
                "last_prompt": prompt[:500],
            }
            save_json_file(CONVS_FILE, convs)
        threading.Thread(
            target=_run_codex_turn,
            args=(conv_id, ws_path, prompt, "", chosen_model, thinking, sandbox_mode),
            daemon=True,
        ).start()
        return {
            "ok": True,
            "provider": "codex",
            "mode": "codex_cli",
            "conversation_id": conv_id,
            "project_key": proj["key"],
            "collaborator_id": collab["id"],
            "collaborator_label": f"Codex · {collab.get('label') or proj['display_name']}",
            "project_id": str(collab.get("project_id") or ""),
            "model": chosen_model,
            "target_email": None,
            "brief_path": None,
            "stderr": "",
        }

    if ctype == "claude_instance":
        acc_id = str(collab["account_id"])
        email = str(collab.get("email") or acc_id[:8])
        cli_ready = bool(collab.get("cli_ready"))
        conv_id = str(uuid.uuid4())
        with StateLock():
            convs = load_json_file(CONVS_FILE)
            convs[conv_id] = {
                "conversation_id": conv_id,
                "provider": "claude_instance",
                "project_key": proj["key"],
                "collaborator_id": collab["id"],
                "collaborator_label": collab.get("label") or email,
                "target_account_id": acc_id,
                "target_number": collab.get("account_number"),
                "target_email": email,
                "project_id": "",
                "host_workspace": proj.get("host_workspace") or "",
                "title": title or f"邀请 {collab.get('label') or email}",
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "status": "running" if cli_ready else "waiting_relay",
                "fully_idle": False,
                "last_prompt": prompt[:500],
            }
            save_json_file(CONVS_FILE, convs)
        threading.Thread(
            target=_run_claude_instance_turn,
            args=(conv_id, acc_id, email, prompt, cli_ready, "", str(model or collab.get("model") or "")),
            daemon=True,
        ).start()
        return {
            "ok": True,
            "provider": "claude_instance",
            "mode": "cli_auto" if cli_ready else "desktop_relay_inbox",
            "conversation_id": conv_id,
            "project_key": proj["key"],
            "collaborator_id": collab["id"],
            "collaborator_label": collab.get("label") or email,
            "project_id": "",
            "model": str(model or collab.get("model") or ""),
            "target_email": email,
            "brief_path": None,
            "stderr": "",
        }

    # Antigravity collaborator via optional Sidecar bridge
    target_project_id = str(collab.get("project_id") or "")
    chosen_model = str(model or collab.get("model") or "").strip() or None
    payload = {
        "prompt": prompt,
        "title": title,
        "project_id": target_project_id,
        "project": proj["key"],
        "project_dir": proj.get("host_workspace") or project_dir or None,
        "model": chosen_model,
        "profile": profile,
    }
    res = ag_bridge_request("POST", "/conversations", body=payload, timeout=70)
    if not res:
        raise RuntimeError(
            f"Antigravity sidecar bridge is not reachable at {ag_bridge_url()}. "
            "Enable the optional Antigravity Sidecar in Antigravity Automations, or target an API model / desktop instance collaborator."
        )
    conv_id = res.get("conversation_id")
    if conv_id:
        with StateLock():
            convs = load_json_file(CONVS_FILE)
            convs[conv_id] = {
                "conversation_id": conv_id,
                "provider": "antigravity",
                "project_key": proj["key"],
                "collaborator_id": collab["id"],
                "collaborator_label": collab.get("label") or "Antigravity",
                "project_id": target_project_id,
                "host_workspace": proj.get("host_workspace") or "",
                "title": title or f"{proj['display_name']} · Antigravity",
                "model": chosen_model,
                "created_at": utc_now(),
                "updated_at": utc_now(),
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
        "target_email": None,
        "brief_path": res.get("brief_path"),
        "stderr": (res.get("command_result") or {}).get("stderr", ""),
    }


def send_message(
    conversation_id: str,
    message: str,
    title: str | None = None,
    project_dir: str | None = None,
) -> dict[str, Any]:
    with StateLock():
        convs = load_json_file(CONVS_FILE)
        rec = convs.get(conversation_id, {"conversation_id": conversation_id, "created_at": utc_now()})
    prov = rec.get("provider") or "antigravity"

    if prov == "openai_compat":
        collab_stub = {
            "model_ref": rec.get("model_ref") or "",
            "api_base": rec.get("api_base") or "",
            "model_id": rec.get("model") or "",
            "thinking_level": rec.get("thinking_level") or "",
        }
        rt = resolve_model_runtime(collab_stub, str(rec.get("project_key") or ""))
        api_base = rt["api_base"]
        chosen_model = str(rt["model_id"] or rec.get("model") or "").strip()
        if not api_base or not chosen_model:
            raise RuntimeError("API session missing api_base or model")
        pre_step = read_local_session_summary(conversation_id, last_n=1).get("last_step_index", -1)
        with StateLock():
            convs = load_json_file(CONVS_FILE)
            rec = convs.get(conversation_id, rec)
            rec["updated_at"] = utc_now()
            rec["status"] = "running"
            rec["fully_idle"] = False
            rec["last_prompt"] = message[:500]
            convs[conversation_id] = rec
            save_json_file(CONVS_FILE, convs)
        threading.Thread(
            target=_run_openai_compat_turn,
            args=(conversation_id, api_base, rt["api_key"], chosen_model, rt["system_prompt"], message, rt["thinking_level"]),
            daemon=True,
        ).start()
        return {
            "ok": True,
            "provider": "openai_compat",
            "conversation_id": conversation_id,
            "previous_step_index": pre_step,
            "brief_path": None,
            "stderr": "",
        }

    if prov == "codex":
        ws_path = str(rec.get("host_workspace") or project_dir or "").strip()
        chosen_model = str(rec.get("model") or "").strip()
        thinking = str(rec.get("thinking_level") or "").strip()
        sandbox_mode = str(rec.get("sandbox_mode") or "workspace-write").strip()
        codex_tid = str(rec.get("codex_thread_id") or "").strip()
        pre_step = read_local_session_summary(conversation_id, last_n=1).get("last_step_index", -1)
        with StateLock():
            convs = load_json_file(CONVS_FILE)
            rec = convs.get(conversation_id, rec)
            rec["updated_at"] = utc_now()
            rec["status"] = "running"
            rec["fully_idle"] = False
            rec["last_prompt"] = message[:500]
            convs[conversation_id] = rec
            save_json_file(CONVS_FILE, convs)
        threading.Thread(
            target=_run_codex_turn,
            args=(conversation_id, ws_path, message, codex_tid, chosen_model, thinking, sandbox_mode),
            daemon=True,
        ).start()
        return {
            "ok": True,
            "provider": "codex",
            "conversation_id": conversation_id,
            "previous_step_index": pre_step,
            "brief_path": None,
            "stderr": "",
        }

    if prov == "claude_instance":
        acc_id = str(rec.get("target_account_id") or "")
        email = str(rec.get("target_email") or acc_id[:8])
        acc = next((a for a in list_local_desktop_instances() if a["id"] == acc_id), None)
        cli_ready = bool(acc["cli_ready"]) if acc else False
        pre_step = read_local_session_summary(conversation_id, last_n=1).get("last_step_index", -1)
        with StateLock():
            convs = load_json_file(CONVS_FILE)
            rec = convs.get(conversation_id, rec)
            rec["updated_at"] = utc_now()
            rec["status"] = "running" if cli_ready else "waiting_relay"
            rec["fully_idle"] = False
            rec["last_prompt"] = message[:500]
            convs[conversation_id] = rec
            save_json_file(CONVS_FILE, convs)
        threading.Thread(
            target=_run_claude_instance_turn,
            args=(conversation_id, acc_id, email, message, cli_ready, str(rec.get("claude_session_id") or "")),
            daemon=True,
        ).start()
        return {
            "ok": True,
            "provider": "claude_instance",
            "conversation_id": conversation_id,
            "previous_step_index": pre_step,
            "brief_path": None,
            "stderr": "",
        }

    tpath = Path(rec.get("transcript_path") or default_ag_transcript_path(conversation_id))
    pre_step = read_ag_transcript_summary(tpath, last_n=1, include_full_latest=False).get("last_step_index", -1)
    res = ag_bridge_request(
        "POST",
        f"/conversations/{conversation_id}/messages",
        body={"message": message, "title": title, "project_dir": rec.get("host_workspace") or project_dir},
        timeout=70,
    )
    if not res:
        raise RuntimeError(
            f"Antigravity sidecar bridge is not reachable at {ag_bridge_url()}."
        )
    with StateLock():
        convs = load_json_file(CONVS_FILE)
        rec = convs.get(conversation_id, rec)
        rec["updated_at"] = utc_now()
        rec["status"] = "running" if res.get("ok") else "error"
        rec["fully_idle"] = False
        rec["last_prompt"] = message[:500]
        rec["last_brief_path"] = res.get("brief_path")
        convs[conversation_id] = rec
        save_json_file(CONVS_FILE, convs)
    return {
        "ok": bool(res.get("ok")),
        "provider": "antigravity",
        "conversation_id": conversation_id,
        "previous_step_index": pre_step,
        "brief_path": res.get("brief_path"),
        "stderr": (res.get("command_result") or {}).get("stderr", ""),
    }


def wait_reply(
    conversation_id: str,
    timeout_seconds: int = 25,
    after_step: int = -1,
) -> dict[str, Any]:
    timeout = min(max(int(timeout_seconds), 1), 120)
    deadline = time.monotonic() + timeout
    while True:
        with StateLock():
            convs = load_json_file(CONVS_FILE)
            record = convs.get(conversation_id, {"conversation_id": conversation_id})
        tr = read_conv_transcript(record, conversation_id, last_n=20, include_full_latest=True)
        eff = compute_effective_status(record, tr)
        latest_reply_step = tr.get("latest_reply_step", -1)
        has_new_reply = latest_reply_step > after_step and bool(tr.get("latest_reply"))

        if eff in ("waiting_approval", "waiting_relay"):
            return {
                "ok": True,
                "conversation_id": conversation_id,
                "done": False,
                "status": eff,
                "last_step_index": tr.get("last_step_index"),
                "latest_reply": tr.get("latest_reply"),
                "recent_tools": [
                    tname for s in (tr.get("recent_steps") or [])[-5:] for tname in (s.get("tool_names") or [])
                ],
            }

        if eff in ("completed", "error") and (
            after_step < 0 or has_new_reply or tr.get("last_step_index", -1) > after_step
        ):
            return {
                "ok": True,
                "conversation_id": conversation_id,
                "done": True,
                "status": eff,
                "last_step_index": tr.get("last_step_index"),
                "latest_reply": tr.get("latest_reply"),
                "recent_tools": [
                    tname for s in (tr.get("recent_steps") or [])[-5:] for tname in (s.get("tool_names") or [])
                ],
            }

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {
                "ok": True,
                "conversation_id": conversation_id,
                "done": False,
                "status": eff,
                "last_step_index": tr.get("last_step_index"),
                "latest_reply": tr.get("latest_reply"),
                "recent_tools": [
                    tname for s in (tr.get("recent_steps") or [])[-5:] for tname in (s.get("tool_names") or [])
                ],
            }
        with TURN_EVENT:
            TURN_EVENT.wait(timeout=min(0.45, remaining))


def get_history(conversation_id: str, last_n: int = 30) -> dict[str, Any]:
    n = min(max(int(last_n), 1), 100)
    with StateLock():
        convs = load_json_file(CONVS_FILE)
        record = convs.get(conversation_id, {"conversation_id": conversation_id})
    tr = read_conv_transcript(record, conversation_id, last_n=n, include_full_latest=True)
    eff = compute_effective_status(record, tr)
    return {
        "ok": True,
        "conversation_id": conversation_id,
        "status": eff,
        "conversation": record,
        "latest_reply": tr.get("latest_reply"),
        "turns": tr.get("turns") or [],
    }


def list_conversations(
    project: str | None = None,
    date: str | None = None,
    collaborator: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 10,
) -> dict[str, Any]:
    page = max(int(page), 1)
    page_size = min(max(int(page_size), 1), 100)
    query_filter = (q or "").strip().lower()

    projects = get_all_normalized_projects()
    proj_obj = projects.get(project) if project else None
    proj_ag_ids = (
        {
            c.get("project_id")
            for c in (proj_obj.get("collaborators") or [])
            if c.get("type") == "antigravity" and c.get("project_id")
        }
        if proj_obj
        else set()
    )

    with StateLock():
        convs = load_json_file(CONVS_FILE)

    items: list[dict[str, Any]] = []
    all_dates: set[str] = set()
    for cid, rec in convs.items():
        if project:
            matched = (
                rec.get("project_key") == project
                or rec.get("project_id") == project
                or (rec.get("project_id") and rec.get("project_id") in proj_ag_ids)
            )
            if not matched:
                continue

        ts = rec.get("updated_at") or rec.get("created_at") or ""
        d_str = local_date_str(ts)
        if d_str:
            all_dates.add(d_str)
        if date and d_str != date:
            continue
        if collaborator:
            cf = collaborator.lower()
            c_hay = (
                f"{rec.get('collaborator_id','')} {rec.get('provider','')} "
                f"{rec.get('collaborator_label','')} {rec.get('model','')} {rec.get('target_email','')}"
            ).lower()
            if cf not in c_hay:
                continue

        tr = read_conv_transcript(rec, cid, last_n=2, include_full_latest=False)
        item = dict(rec)
        item["date"] = d_str
        item["effective_status"] = compute_effective_status(rec, tr)
        item["step_count"] = tr.get("step_count", 0)
        item["latest_reply_preview"] = (tr.get("latest_reply") or "")[:300]

        if query_filter:
            q_hay = (
                f"{item.get('title','')} {item.get('last_prompt','')} "
                f"{item.get('latest_reply_preview','')} {item.get('collaborator_label','')}"
            ).lower()
            if query_filter not in q_hay:
                continue
        items.append(item)

    items.sort(key=lambda x: str(x.get("updated_at") or x.get("created_at") or ""), reverse=True)
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    start = (page - 1) * page_size
    paged_items = items[start : start + page_size]

    return {
        "ok": True,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "available_dates": sorted(all_dates, reverse=True),
        "items": paged_items,
        "conversations": {it["conversation_id"]: it for it in paged_items},
    }


def check_inbox(account: str | None = None) -> dict[str, Any]:
    with StateLock():
        convs = load_json_file(CONVS_FILE)
    pending_items: list[dict[str, Any]] = []
    for cid, rec in convs.items():
        if rec.get("provider") != "claude_instance":
            continue
        tr = read_local_session_summary(cid, last_n=5)
        eff = compute_effective_status(rec, tr)
        if eff != "waiting_relay":
            continue
        if account:
            af = account.lower()
            hay = f"{rec.get('target_account_id','')} {rec.get('target_email','')} #{rec.get('target_number','')}".lower()
            if af not in hay:
                continue
        pending_items.append(
            {
                "conversation_id": cid,
                "project_key": rec.get("project_key"),
                "title": rec.get("title"),
                "target_email": rec.get("target_email"),
                "target_number": rec.get("target_number"),
                "updated_at": rec.get("updated_at"),
                "last_prompt": rec.get("last_prompt"),
                "turns": tr.get("turns") or [],
            }
        )
    return {"ok": True, "pending": pending_items}


def reply_inbox(conversation_id: str, reply_text: str, responder: str = "") -> dict[str, Any]:
    text = reply_text.strip()
    if not conversation_id or not text:
        raise ValueError("conversation_id and reply are required")
    with StateLock():
        sess = load_api_session(conversation_id)
        turns = list(sess.get("turns") or [])
        turns.append(
            {
                "role": "assistant",
                "step_index": len(turns),
                "created_at": utc_now(),
                "content": text,
                "responder": responder,
            }
        )
        sess["turns"] = turns
        sess["status"] = "completed"
        save_api_session(conversation_id, sess)

        convs = load_json_file(CONVS_FILE)
        rec = convs.get(conversation_id, {"conversation_id": conversation_id, "created_at": utc_now()})
        rec["updated_at"] = utc_now()
        rec["status"] = "completed"
        rec["fully_idle"] = True
        convs[conversation_id] = rec
        save_json_file(CONVS_FILE, convs)

    with TURN_EVENT:
        TURN_EVENT.notify_all()
    return {"ok": True, "conversation_id": conversation_id, "status": "completed"}
