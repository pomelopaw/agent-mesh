"""
Atomic state persistence, fail-closed locking, private secret storage,
rotating logging, and active task cancellation registry for Agent Mesh.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .security import assert_within_dir, validate_conversation_id

VERSION = "1.0.1"

TURN_EVENT = threading.Condition()
_LOCAL_LOCK = threading.RLock()
_LOG_LOCK = threading.Lock()
_LOG_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB


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


def log(msg: str) -> None:
    line = f"[{utc_now()}] {msg}\n"
    with _LOG_LOCK:
        try:
            ensure_dirs()
            if LOG_FILE.exists() and LOG_FILE.stat().st_size >= _LOG_MAX_BYTES:
                backup = LOG_FILE.with_suffix(".log.1")
                try:
                    if backup.exists():
                        backup.unlink()
                    os.replace(LOG_FILE, backup)
                except OSError:
                    pass
            with LOG_FILE.open("a", encoding="utf-8", errors="replace", newline="\n") as f:
                f.write(line)
        except OSError:
            pass


class StateLock:
    """
    Thread-safe + cross-process atomic lockfile guard with stale-lock recovery.
    Strictly fail-closed: raises TimeoutError / RuntimeError if the lock cannot
    be acquired instead of proceeding unlocked.
    """

    def __init__(self, lock_path: Path = LOCK_FILE, timeout: float = 10.0) -> None:
        self.lock_path = lock_path
        self.timeout = timeout
        self._fd: int | None = None
        self._acquired_local = False

    def __enter__(self) -> StateLock:
        _LOCAL_LOCK.acquire()
        self._acquired_local = True
        try:
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
                        raise TimeoutError(
                            f"Timed out after {self.timeout}s waiting for state lock: {self.lock_path}"
                        )
                    time.sleep(0.03)
                except OSError as exc:
                    raise RuntimeError(f"Failed to acquire state lock: {exc}") from exc
        except BaseException:
            self._release_local()
            raise

    def _release_local(self) -> None:
        if self._acquired_local:
            self._acquired_local = False
            _LOCAL_LOCK.release()

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
            self._release_local()


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _harden_windows_acl(path: Path) -> None:
    """Best-effort restriction of a private file to the current Windows user only."""
    if os.name != "nt":
        return
    username = os.environ.get("USERNAME", "").strip()
    if not username:
        return
    domain = os.environ.get("USERDOMAIN", "").strip()
    principal = f"{domain}\\{username}" if domain else username
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:(R,W)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            shell=False,
            creationflags=creationflags,
        )
    except Exception:
        pass


def save_json_file(path: Path, data: dict[str, Any], private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
    raw_bytes = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")

    if private and os.name != "nt":
        flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
        fd = os.open(str(tmp), flags, 0o600)
        try:
            if hasattr(os, "fchmod"):
                try:
                    os.fchmod(fd, 0o600)
                except OSError:
                    pass
            with os.fdopen(fd, "wb") as f:
                f.write(raw_bytes)
                f.flush()
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
    else:
        tmp.write_bytes(raw_bytes)
        if private and os.name == "nt":
            _harden_windows_acl(tmp)

    for attempt in range(5):
        try:
            os.replace(tmp, path)
            if private and os.name == "nt":
                _harden_windows_acl(path)
            return
        except PermissionError:
            if attempt == 4:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
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


def api_session_path(conversation_id: str) -> Path:
    cid = validate_conversation_id(conversation_id)
    ensure_dirs()
    return assert_within_dir(API_SESSIONS_DIR / f"{cid}.json", API_SESSIONS_DIR, "session_path")


def load_api_session(conversation_id: str) -> dict[str, Any]:
    p = api_session_path(conversation_id)
    data = load_json_file(p)
    return data if data else {"conversation_id": validate_conversation_id(conversation_id), "turns": []}


def save_api_session(conversation_id: str, session_data: dict[str, Any]) -> None:
    p = api_session_path(conversation_id)
    save_json_file(p, session_data)


# ---- Active Task Cancellation Registry ----
class ActiveTaskHandle:
    def __init__(self, conversation_id: str) -> None:
        self.conversation_id = conversation_id
        self.cancel_event = threading.Event()
        self._lock = threading.Lock()
        self._hooks: list[Callable[[], None]] = []

    def add_cancel_hook(self, fn: Callable[[], None]) -> None:
        run_now = False
        with self._lock:
            if self.cancel_event.is_set():
                run_now = True
            else:
                self._hooks.append(fn)
        if run_now:
            try:
                fn()
            except Exception:
                pass

    def cancel(self) -> None:
        with self._lock:
            self.cancel_event.set()
            hooks = list(self._hooks)
            self._hooks.clear()
        for fn in hooks:
            try:
                fn()
            except Exception:
                pass


class TaskRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, ActiveTaskHandle] = {}

    def register(self, conversation_id: str) -> ActiveTaskHandle:
        cid = validate_conversation_id(conversation_id)
        handle = ActiveTaskHandle(cid)
        with self._lock:
            old = self._tasks.get(cid)
            self._tasks[cid] = handle
        if old is not None:
            old.cancel()
        return handle

    def get(self, conversation_id: str) -> ActiveTaskHandle | None:
        with self._lock:
            return self._tasks.get(conversation_id)

    def is_cancelled(self, conversation_id: str) -> bool:
        with self._lock:
            h = self._tasks.get(conversation_id)
            return bool(h and h.cancel_event.is_set())

    def cancel(self, conversation_id: str) -> bool:
        with self._lock:
            h = self._tasks.get(conversation_id)
        if h is None:
            return False
        h.cancel()
        return True

    def unregister(self, conversation_id: str, handle: ActiveTaskHandle | None = None) -> None:
        with self._lock:
            cur = self._tasks.get(conversation_id)
            if cur is not None and (handle is None or cur is handle):
                self._tasks.pop(conversation_id, None)


TASK_REGISTRY = TaskRegistry()
