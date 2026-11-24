"""
Security validators and boundary enforcement for Agent Mesh.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from urllib.parse import urlparse

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
ACCOUNT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_INPUT_FILE_BYTES = 512 * 1024  # 512 KiB
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def validate_conversation_id(conversation_id: str) -> str:
    """Ensure conversation_id is a strict canonical UUID to prevent path or URL injection."""
    cid = str(conversation_id or "").strip()
    if not UUID_RE.fullmatch(cid):
        raise ValueError(f"Invalid conversation_id format (expected UUID): {cid!r}")
    return cid


def validate_account_id(account_id: str) -> str:
    """Validate desktop assistant account ID against a strict safe character whitelist."""
    aid = str(account_id or "").strip()
    if not ACCOUNT_ID_RE.fullmatch(aid):
        raise ValueError(f"Invalid account_id format: {aid!r}")
    return aid


def is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower().strip("[]")
    if h in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def validate_api_base(api_base: str) -> str:
    """
    Validate OpenAI-compatible API base URL:
      - Must be a valid URL without whitespace, control characters, or embedded credentials.
      - Allows 'https://' for any host.
      - Allows 'http://' ONLY for local loopback addresses (localhost / 127.0.0.1 / ::1).
    """
    url = str(api_base or "").strip()
    if not url:
        raise ValueError("api_base is required")
    if any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise ValueError("api_base must not contain whitespace or control characters")
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").strip()
    if not host:
        raise ValueError("api_base must include a valid hostname")
    if parsed.username or parsed.password:
        raise ValueError("api_base must not embed credentials in the URL")
    if scheme == "https":
        return url
    if scheme == "http" and is_loopback_host(host):
        return url
    raise ValueError(
        "api_base must use https:// (http:// is only permitted for localhost / 127.0.0.1 / ::1)"
    )


def assert_within_dir(target: Path, base_dir: Path, label: str = "path") -> Path:
    """Resolve target and assert it lies strictly inside base_dir."""
    resolved_base = base_dir.resolve()
    resolved_target = target.resolve()
    if not resolved_target.is_relative_to(resolved_base):
        raise PermissionError(f"{label} escapes allowed directory: {target}")
    return resolved_target


def resolve_safe_project_file(
    file_path: str,
    active_project_dir: str,
    label: str = "input",
    max_bytes: int = MAX_INPUT_FILE_BYTES,
) -> Path:
    """
    Resolve a user/model-supplied file path strictly inside active_project_dir.
    Blocks directory traversal ('..'), symlink escapes, absolute paths outside
    active_project_dir, and oversized files.
    """
    raw = str(file_path or "").strip()
    if not raw:
        raise ValueError(f"{label}_file is empty")
    if not active_project_dir or not str(active_project_dir).strip():
        raise PermissionError(
            f"{label}_file cannot be used because no active workspace directory is set."
        )

    workspace_root = Path(active_project_dir).expanduser()
    try:
        resolved_root = workspace_root.resolve(strict=True)
    except OSError as exc:
        raise PermissionError(f"Active workspace directory is not accessible: {exc}") from exc
    if not resolved_root.is_dir():
        raise PermissionError("Active workspace path is not a directory")

    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = resolved_root / candidate

    try:
        resolved_file = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(f"{label}_file does not exist: {raw}") from exc

    if not resolved_file.is_relative_to(resolved_root):
        raise PermissionError(
            f"{label}_file must reside within the active workspace directory (outside access blocked)."
        )
    if not resolved_file.is_file():
        raise RuntimeError(f"{label}_file is not a regular file: {raw}")

    size = resolved_file.stat().st_size
    if size > max_bytes:
        raise ValueError(
            f"{label}_file exceeds maximum allowed size ({size} bytes > {max_bytes} bytes)."
        )
    return resolved_file
