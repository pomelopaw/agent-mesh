#!/usr/bin/env python3
"""
Cross-platform local installer for Agent Mesh into Claude Cage.
  - Auto-detects the Cage data directory (Windows / macOS / Linux)
  - Optionally migrates legacy antigravity-bridge state into ~/.agent-mesh
  - Copies plugins/agent-mesh into <CageData>/plugins/agent-mesh
  - Adapts Python command on Windows if python3 is not a real interpreter on PATH
  - Runs an end-to-end MCP stdio smoke test against the installed plugin
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_PLUGIN = ROOT / "plugins" / "agent-mesh"


def detect_cage_data_dir(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    candidates: list[Path] = []
    for var in ("CLAUDE_CAGE_DATA_DIR",):
        v = os.environ.get(var, "").strip()
        if v:
            candidates.append(Path(v).expanduser())
    cfg = os.environ.get("CLAUDE_CAGE_CONFIG", "").strip()
    if cfg:
        candidates.append(Path(cfg).expanduser().resolve().parent)

    if os.name == "nt":
        candidates.append(ROOT.parent / "CageData")
        candidates.append(ROOT.parent.parent / "CageData")
        for var in ("APPDATA", "LOCALAPPDATA"):
            b = os.environ.get(var, "").strip()
            if b:
                candidates.append(Path(b) / "claude-cage")
                candidates.append(Path(b) / "ClaudeCage")
        candidates.append(Path.home() / ".claude-cage")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "claude-cage")
        candidates.append(Path.home() / ".claude-cage")
    else:
        xdg = os.environ.get("XDG_DATA_HOME", "").strip()
        if xdg:
            candidates.append(Path(xdg) / "claude-cage")
        candidates.append(Path.home() / ".local" / "share" / "claude-cage")
        candidates.append(Path.home() / ".claude-cage")

    for c in candidates:
        if c.exists() and (c / "plugins").exists():
            return c
    for c in candidates:
        if c.exists() and any((c / f).exists() for f in ("projects.json", "claude-accounts.json", "config.json")):
            return c
    raise SystemExit("Could not locate Cage data directory. Pass --cage-data <path>.")


def migrate_legacy_state(legacy_state_dir: Path, target_state_dir: Path) -> list[str]:
    migrated: list[str] = []
    if not legacy_state_dir.exists():
        return migrated
    target_state_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("projects.json", "models.json", "secrets.json", "conversations.json", "bridge.token"):
        src = legacy_state_dir / fname
        dst = target_state_dir / fname
        if src.exists() and src.is_file() and not dst.exists():
            shutil.copy2(src, dst)
            migrated.append(fname)
    src_sess = legacy_state_dir / "api_sessions"
    dst_sess = target_state_dir / "api_sessions"
    if src_sess.exists() and src_sess.is_dir():
        dst_sess.mkdir(parents=True, exist_ok=True)
        for item in src_sess.glob("*.json"):
            dst = dst_sess / item.name
            if not dst.exists():
                shutil.copy2(item, dst)
                migrated.append(f"api_sessions/{item.name}")
    return migrated


def run_mcp_smoke_test(plugin_dir: Path, python_exe: str) -> None:
    server_py = plugin_dir / "server.py"
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "ag_health", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "ag_list_projects", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "ag_list_models", "arguments": {}}},
    ]
    payload = "\n".join(json.dumps(r) for r in reqs) + "\n"
    proc = subprocess.run(
        [python_exe, "-X", "utf8", str(server_py)],
        input=payload,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"MCP smoke test failed (rc={proc.returncode}): {proc.stderr}")
    lines = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) < 5:
        raise RuntimeError(f"Expected 5 MCP responses, got {len(lines)}: {proc.stdout}")
    tools = lines[1]["result"]["tools"]
    health = json.loads(lines[2]["result"]["content"][0]["text"])
    projects = json.loads(lines[3]["result"]["content"][0]["text"])
    models = json.loads(lines[4]["result"]["content"][0]["text"])
    if not health.get("ok"):
        raise RuntimeError(f"ag_health returned not ok: {health}")
    print(
        f"MCP smoke test    : OK ({len(tools)} tools, mode={health.get('mode')}, "
        f"projects={len(projects.get('projects', {}))}, models={len(models.get('models', {}))}, "
        f"ag_online={health.get('antigravity_bridge_online')})"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Install Agent Mesh plugin into local Claude Cage.")
    parser.add_argument("--cage-data", default="", help="Path to Cage data directory (auto-detected if omitted)")
    parser.add_argument("--migrate-from", default="", help="Optional legacy state directory to migrate from")
    parser.add_argument("--remove-legacy", action="store_true", help="Remove old antigravity-bridge plugin directory after install")
    args = parser.parse_args()

    cage_data = detect_cage_data_dir(args.cage_data or None)
    plugins_root = cage_data / "plugins"
    dst_dir = plugins_root / "agent-mesh"
    dst_dir.mkdir(parents=True, exist_ok=True)

    # Migrate legacy state if requested or auto-found next to repo
    state_dir = Path(os.environ.get("AGENT_MESH_STATE_DIR") or (Path.home() / ".agent-mesh")).expanduser().resolve()
    legacy_candidates = [Path(args.migrate_from)] if args.migrate_from else [ROOT.parent / "antigravity-bridge" / "state"]
    migrated: list[str] = []
    for lc in legacy_candidates:
        if lc.exists():
            migrated.extend(migrate_legacy_state(lc, state_dir))

    # Copy plugin in-place (Windows-safe when Cage holds handles)
    shutil.copytree(
        SRC_PLUGIN,
        dst_dir,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )

    # Adapt Python binary ONLY in the installed copy on Windows where `python3` may be absent/stub
    python_exe = sys.executable
    if os.name == "nt":
        mpath = dst_dir / "plugin.json"
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
        for srv in manifest.get("contributes", {}).get("mcpServers", []):
            cmd = srv.get("command") or []
            if cmd and cmd[0] == "python3":
                cmd[0] = python_exe
        mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Optionally remove old antigravity-bridge plugin folder so sidebar isn't duplicated
    removed_legacy = False
    if args.remove_legacy:
        old_dir = plugins_root / "antigravity-bridge"
        if old_dir.exists() and old_dir.is_dir():
            shutil.rmtree(old_dir, ignore_errors=True)
            removed_legacy = not old_dir.exists()

    print(f"Installed plugin  : {dst_dir}")
    print(f"State directory   : {state_dir}")
    print(f"Python executable : {python_exe}")
    if migrated:
        print(f"Migrated files    : {', '.join(migrated)}")
    if args.remove_legacy:
        print(f"Removed legacy    : {removed_legacy}")
    run_mcp_smoke_test(dst_dir, python_exe)


if __name__ == "__main__":
    main()
