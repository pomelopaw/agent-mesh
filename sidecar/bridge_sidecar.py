#!/usr/bin/env python3
"""
Optional Antigravity Sidecar Bridge for Agent Mesh (v1.0.0)

Exposes Antigravity's local `agentapi` CLI over localhost HTTP (127.0.0.1:18765) with
a shared bearer token stored in ~/.agent-mesh/bridge.token (or AGENT_MESH_AG_TOKEN_FILE).
Zero third-party dependencies, zero hardcoded paths.
"""

from __future__ import annotations

import concurrent.futures
import datetime
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = int(os.environ.get("AGENT_MESH_AG_PORT", "18765"))

UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
BAT_UNSAFE_RE = re.compile(r'[\r\n"&|<>^%!]')
META_CACHE: dict[str, dict[str, Any]] = {}


def resolve_state_dir() -> Path:
    custom = os.environ.get("AGENT_MESH_STATE_DIR", "").strip()
    if custom:
        return Path(custom).expanduser().resolve()
    return (Path.home() / ".agent-mesh").resolve()


STATE_DIR = resolve_state_dir()
PROMPTS_DIR = STATE_DIR / "prompts"
TOKEN_FILE = Path(
    os.environ.get("AGENT_MESH_AG_TOKEN_FILE")
    or os.environ.get("CAGE_AG_BRIDGE_TOKEN_FILE")
    or str(STATE_DIR / "bridge.token")
).expanduser()


def utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def load_or_create_token() -> str:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text(encoding="utf-8", errors="replace").strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(tok + "\n", encoding="utf-8")
    if os.name != "nt":
        try:
            os.chmod(TOKEN_FILE, 0o600)
        except OSError:
            pass
    return tok


def resolve_agentapi() -> str | None:
    found = shutil.which("agentapi")
    if found:
        return found
    for name in ("agentapi.BAT", "agentapi.cmd", "agentapi"):
        cand = Path.home() / ".gemini" / "antigravity" / "bin" / name
        if cand.exists():
            return str(cand)
    return None


def resolve_agentapi_cmd_prefix() -> list[str] | None:
    """
    Unwrap Windows agentapi.BAT -> [<language_server.exe>, "agentapi"] so subprocess.run(..., shell=False)
    passes multi-line UTF-8/markdown prompts directly inline without cmd.exe mangling or temp files.
    """
    bin_path = resolve_agentapi()
    if not bin_path:
        return None
    if os.name == "nt" and bin_path.lower().endswith((".bat", ".cmd")):
        try:
            text = Path(bin_path).read_text(encoding="utf-8", errors="replace")
            m = re.search(r'"([^"]+language_server[^"]*\.exe)"\s+agentapi\b', text, re.IGNORECASE)
            if m and Path(m.group(1)).is_file():
                return [m.group(1), "agentapi"]
        except OSError:
            pass
    return [bin_path]


def run_cmd(args: list[str], timeout: int = 60, env_override: dict[str, str] | None = None) -> dict[str, Any]:
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
    except Exception as exc:
        return {"ok": False, "returncode": -1, "stdout": "", "stderr": str(exc)}


def uri_to_local_path(uri: str) -> str:
    s = uri.strip()
    if s.lower().startswith("file:///"):
        rest = unquote(s[8:])
        if len(rest) >= 2 and rest[1] == ":":
            return rest.replace("/", "\\")
        return "/" + rest
    if s.lower().startswith("file://"):
        return unquote(s[7:])
    return s


def _walk_collect(obj: Any, out: dict[str, list[Any]]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            kl = k.lower()
            if kl in ("projectid", "project_id") and isinstance(v, str) and v.strip():
                out["project_ids"].append(v.strip())
            elif kl in ("projectname", "project_name") and isinstance(v, str) and v.strip():
                out["project_names"].append(v.strip())
            elif kl in ("title", "summary") and isinstance(v, str) and v.strip():
                out["titles"].append(v.strip())
            elif kl in ("workspaceuri", "workspace_uri", "workspacepath", "workspace_path", "repositorypath", "cwd", "dir", "path"):
                if isinstance(v, str) and v.strip():
                    out["workspaces"].append(uri_to_local_path(v))
            elif kl in ("workspaceuris", "workspace_uris", "workspacepaths", "workspace_paths", "workspaces"):
                if isinstance(v, list):
                    for item in v:
                        if isinstance(item, str) and item.strip():
                            out["workspaces"].append(uri_to_local_path(item))
                        else:
                            _walk_collect(item, out)
                    continue
            _walk_collect(v, out)
    elif isinstance(obj, list):
        for item in obj:
            _walk_collect(item, out)


def parse_conv_meta(raw_stdout: str) -> dict[str, Any] | None:
    text = (raw_stdout or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    found: dict[str, list[Any]] = {"project_ids": [], "project_names": [], "titles": [], "workspaces": []}
    _walk_collect(parsed, found)
    return {
        "project_id": found["project_ids"][0] if found["project_ids"] else None,
        "project_name": found["project_names"][0] if found["project_names"] else None,
        "title": found["titles"][0] if found["titles"] else None,
        "workspaces": list(dict.fromkeys(found["workspaces"])),
    }


def fetch_meta(agentapi_bin: str, cid: str, force: bool = False) -> tuple[str, dict[str, Any] | None]:
    if not force and cid in META_CACHE:
        return cid, META_CACHE[cid]
    res = run_cmd([agentapi_bin, "get-conversation-metadata", cid], timeout=12)
    if not res["ok"]:
        return cid, None
    parsed = parse_conv_meta(res["stdout"])
    if parsed:
        META_CACHE[cid] = parsed
    return cid, parsed


def discover_antigravity_projects(force: bool = False) -> dict[str, Any]:
    agentapi_bin = resolve_agentapi()
    brain_dir = Path.home() / ".gemini" / "antigravity" / "brain"
    conv_ids: list[str] = []
    if brain_dir.exists() and brain_dir.is_dir():
        dirs: list[tuple[float, str]] = []
        for child in brain_dir.iterdir():
            if child.is_dir() and UUID_RE.fullmatch(child.name):
                try:
                    mtime = child.stat().st_mtime
                except OSError:
                    mtime = 0.0
                dirs.append((mtime, child.name))
        dirs.sort(reverse=True)
        conv_ids = [cid for _, cid in dirs[:30]]

    projects_map: dict[str, dict[str, Any]] = {}
    if agentapi_bin and conv_ids:
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futs = [pool.submit(fetch_meta, agentapi_bin, cid, force) for cid in conv_ids]
            for fut in concurrent.futures.as_completed(futs):
                _cid, info = fut.result()
                if not info or not info.get("project_id"):
                    continue
                pid = info["project_id"]
                ws_list = info.get("workspaces") or []
                primary_ws = ws_list[0] if ws_list else ""
                name = info.get("project_name") or (Path(primary_ws.rstrip("/\\")).name if primary_ws else "") or pid[:8]
                if pid not in projects_map:
                    projects_map[pid] = {
                        "project_id": pid,
                        "name": name,
                        "workspace": primary_ws,
                        "workspaces": ws_list,
                        "sample_title": info.get("title") or "",
                        "conversation_count": 1,
                    }
                else:
                    entry = projects_map[pid]
                    entry["conversation_count"] += 1
                    if not entry["workspace"] and primary_ws:
                        entry["workspace"] = primary_ws
                    if not entry["sample_title"] and info.get("title"):
                        entry["sample_title"] = info["title"]

    ag_list = sorted(projects_map.values(), key=lambda x: (x["name"].lower(), x["project_id"]))
    return {"ok": True, "antigravity_projects": ag_list, "scanned_conversations": len(conv_ids)}


def prepare_prompt_arg(text: str, cmd_prefix: list[str], workspace_hint: str | None = None) -> tuple[str, str | None]:
    cleaned = text.replace("\r\n", "\n").strip()
    is_bat = cmd_prefix[0].lower().endswith((".bat", ".cmd"))
    needs_file = (is_bat and (len(cleaned) > 600 or bool(BAT_UNSAFE_RE.search(cleaned)))) or (len(cleaned) > 24000)
    if not needs_file:
        return cleaned, None
    target_dir = PROMPTS_DIR
    if workspace_hint:
        ws = Path(workspace_hint)
        if ws.exists() and ws.is_dir():
            cand = ws / ".agent-mesh" / "prompts"
            try:
                cand.mkdir(parents=True, exist_ok=True)
                target_dir = cand
            except OSError:
                target_dir = PROMPTS_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)
    brief_path = target_dir / f"task-{stamp}.md"
    brief_path.write_text(cleaned + "\n", encoding="utf-8")
    return f"Please read the full instructions in {brief_path} and execute them.", str(brief_path)


def extract_conversation_id(stdout: str, stderr: str) -> str | None:
    try:
        parsed = json.loads(stdout)
        cid = parsed.get("response", {}).get("newConversation", {}).get("conversationId")
        if isinstance(cid, str) and UUID_RE.fullmatch(cid):
            return cid
    except Exception:
        pass
    for text in (stdout, stderr):
        m = UUID_RE.search(text or "")
        if m:
            return m.group(0)
    return None


class SidecarServer(ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], token: str) -> None:
        self.auth_token = token
        super().__init__(addr, SidecarHandler)


class SidecarHandler(BaseHTTPRequestHandler):
    server: SidecarServer

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if secrets.compare_digest(auth, f"Bearer {self.server.auth_token}"):
            return True
        self._send_json(401, {"ok": False, "error": "unauthorized"})
        return False

    def _read_json(self) -> dict[str, Any] | None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            self._send_json(400, {"ok": False, "error": "invalid json"})
            return None

    def do_GET(self) -> None:
        if not self._check_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)
        if path == "/health":
            self._send_json(
                200,
                {
                    "ok": True,
                    "version": "1.0.0",
                    "time": utc_now(),
                    "port": self.server.server_port,
                    "default_project_id": os.environ.get("ANTIGRAVITY_PROJECT_ID") or None,
                    "agentapi_available": bool(resolve_agentapi()),
                },
            )
            return
        if path == "/discover":
            force = (qs.get("refresh") or ["0"])[0] in ("1", "true", "yes")
            self._send_json(200, discover_antigravity_projects(force=force))
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:
        if not self._check_auth():
            return
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        body = self._read_json()
        if body is None:
            return
        cmd_prefix = resolve_agentapi_cmd_prefix()
        if not cmd_prefix:
            self._send_json(503, {"ok": False, "error": "agentapi not found"})
            return

        if path == "/conversations":
            prompt = str(body.get("prompt") or "").strip()
            if not prompt:
                self._send_json(400, {"ok": False, "error": "prompt is required"})
                return
            cmd = [*cmd_prefix, "new-conversation"]
            if body.get("model"):
                cmd.append(f"--model={body['model']}")
            if body.get("title"):
                safe_title = re.sub(r"[\r\n]+", " ", str(body["title"])).strip()
                if cmd_prefix[0].lower().endswith((".bat", ".cmd")):
                    safe_title = re.sub(r'["&|<>^%!]+', " ", safe_title).strip()
                if safe_title:
                    cmd.append(f"--title={safe_title}")
            if body.get("profile"):
                cmd.append(f"--profile={body['profile']}")
            prompt_arg, brief_path = prepare_prompt_arg(prompt, cmd_prefix, body.get("project_dir"))
            cmd.append(prompt_arg)
            pid = str(body.get("project_id") or "").strip()
            env_ov = {"ANTIGRAVITY_PROJECT_ID": pid} if pid else None
            res = run_cmd(cmd, timeout=60, env_override=env_ov)
            cid = extract_conversation_id(res["stdout"], res["stderr"])
            self._send_json(
                200 if res["ok"] else 500,
                {
                    "ok": res["ok"],
                    "conversation_id": cid,
                    "project_id": pid,
                    "brief_path": brief_path,
                    "command_result": res,
                },
            )
            return

        if path.startswith("/conversations/") and path.endswith("/messages"):
            cid = path[len("/conversations/") : -len("/messages")].strip("/")
            message = str(body.get("message") or "").strip()
            if not cid or not message:
                self._send_json(400, {"ok": False, "error": "conversation_id and message are required"})
                return
            cmd = [*cmd_prefix, "send-message"]
            if body.get("title"):
                safe_title = re.sub(r"[\r\n]+", " ", str(body["title"])).strip()
                if cmd_prefix[0].lower().endswith((".bat", ".cmd")):
                    safe_title = re.sub(r'["&|<>^%!]+', " ", safe_title).strip()
                if safe_title:
                    cmd.append(f"--title={safe_title}")
            msg_arg, brief_path = prepare_prompt_arg(message, cmd_prefix, body.get("project_dir"))
            cmd.extend([cid, msg_arg])
            res = run_cmd(cmd, timeout=60)
            self._send_json(
                200 if res["ok"] else 500,
                {"ok": res["ok"], "conversation_id": cid, "brief_path": brief_path, "command_result": res},
            )
            return

        self._send_json(404, {"ok": False, "error": "not found"})


def main() -> None:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    token = load_or_create_token()
    server = SidecarServer((DEFAULT_HOST, DEFAULT_PORT), token)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
