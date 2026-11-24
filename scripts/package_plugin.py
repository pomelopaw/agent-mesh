#!/usr/bin/env python3
"""
Validate and package plugins/agent-mesh for GitHub Release and Lee-xeo/cage-plugins PRs.
Checks:
  1. Python syntax compilation for all .py files
  2. plugin.json & i18n.json schema completeness
  3. Zero forbidden terms in MCP TOOLS names/descriptions/schemas
  4. Strict CSP compliance in view/index.html (no inline scripts or on*= handlers)
  5. Zero hardcoded local user/drive paths inside plugins/agent-mesh/
  6. Builds dist/agent-mesh-<version>.zip + .sha256
"""

from __future__ import annotations

import hashlib
import json
import py_compile
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "plugins" / "agent-mesh"
DIST_DIR = ROOT / "dist"

FORBIDDEN_TOOL_WORDS = ("cage", "host", "guest", "vm", "virtual", "sandbox", "container", "claude")
HARDCODED_PATTERNS = [
    re.compile(r"[A-Za-z]:[/\\]{1,2}Users[/\\]{1,2}[^\s/\\\"']+", re.IGNORECASE),
    re.compile(r"/Users/[^\s/\"']+", re.IGNORECASE),
    re.compile(r"[A-Za-z]:[/\\]{1,2}Cage(?:Data)?\b", re.IGNORECASE),
]


def main() -> None:
    errors: list[str] = []

    # 1. Compile all Python files
    for py_file in list(PLUGIN_DIR.rglob("*.py")) + list((ROOT / "sidecar").rglob("*.py")):
        try:
            py_compile.compile(str(py_file), doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"Python syntax error in {py_file}: {exc}")

    # 2. Validate plugin.json & i18n.json
    manifest = json.loads((PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    i18n = json.loads((PLUGIN_DIR / "i18n.json").read_text(encoding="utf-8"))
    version = manifest.get("version") or "0.0.0"

    if manifest.get("name") != "agent-mesh":
        errors.append(f"Expected plugin name 'agent-mesh', got {manifest.get('name')!r}")
    if manifest.get("kind") != "host":
        errors.append("Expected kind == 'host'")
    if "zh-CN" not in i18n or "zh-TW" not in i18n:
        errors.append("i18n.json must include zh-CN and zh-TW")

    # 3. Validate MCP TOOLS have zero forbidden words
    sys.path.insert(0, str(PLUGIN_DIR))
    import server as mcp_server  # noqa: E402

    for tool in mcp_server.TOOLS:
        blob = json.dumps(tool, ensure_ascii=False).lower()
        for word in FORBIDDEN_TOOL_WORDS:
            if re.search(rf"\b{re.escape(word)}\b", blob):
                errors.append(f"MCP tool '{tool['name']}' contains forbidden word '{word}'")

    # 4. Strict CSP check on view/index.html
    html = (PLUGIN_DIR / "view" / "index.html").read_text(encoding="utf-8")
    if re.search(r"\bon[a-z]+\s*=", html, re.IGNORECASE):
        errors.append("view/index.html contains inline event handler (on*=)")
    for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", html, re.IGNORECASE | re.DOTALL):
        attrs, body = m.group(1), m.group(2)
        if body.strip() or "src=" not in attrs.lower():
            errors.append("view/index.html contains inline <script> block")

    # 5. Zero hardcoded personal paths or old brand names inside plugins/agent-mesh/
    for f in PLUGIN_DIR.rglob("*"):
        if not f.is_file() or "__pycache__" in f.parts:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for pat in HARDCODED_PATTERNS:
            if pat.search(text):
                errors.append(f"Hardcoded/legacy pattern {pat.pattern!r} found in {f.relative_to(ROOT)}")

    if errors:
        print("VALIDATION FAILED:")
        for e in errors:
            print("  -", e)
        sys.exit(1)

    # 6. Build release zip + sha256
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = DIST_DIR / f"agent-mesh-{version}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(PLUGIN_DIR.rglob("*")):
            if not f.is_file() or "__pycache__" in f.parts or f.suffix in (".pyc", ".pyo"):
                continue
            arcname = f.relative_to(PLUGIN_DIR).as_posix()
            zf.write(f, arcname)

    sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    (DIST_DIR / f"agent-mesh-{version}.sha256").write_text(f"{sha}  {zip_path.name}\n", encoding="utf-8")

    print(f"VALIDATION PASSED ({len(mcp_server.TOOLS)} MCP tools)")
    print(f"Package : {zip_path} ({zip_path.stat().st_size} bytes)")
    print(f"SHA-256 : {sha}")


if __name__ == "__main__":
    main()
