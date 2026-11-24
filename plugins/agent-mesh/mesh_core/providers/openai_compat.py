"""
OpenAI-compatible reasoning / chat API provider adapter.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import urllib.error
import urllib.request
import uuid
from typing import Any
from urllib.parse import urlparse

from ..security import is_loopback_host, validate_api_base, validate_conversation_id
from ..storage import (
    CONVS_FILE,
    MODELS_FILE,
    TASK_REGISTRY,
    ActiveTaskHandle,
    StateLock,
    get_secret,
    load_json_file,
    save_json_file,
    utc_now,
)
from .base import (
    ProviderAdapter,
    ProviderMeta,
    append_user_turn,
    finalize_turn,
    read_local_session_summary,
)


def build_chat_completions_url(api_base: str) -> str:
    base = validate_api_base(api_base).rstrip("/")
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


def _close_connection_now(conn: http.client.HTTPConnection) -> None:
    sock = getattr(conn, "sock", None)
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass
    try:
        conn.close()
    except Exception:
        pass


def _make_cancellable_opener(
    handle: ActiveTaskHandle,
    bypass_proxy: bool,
) -> tuple[urllib.request.OpenerDirector, list[Any], threading.Lock]:
    """
    Build a urllib opener whose underlying HTTP/HTTPS connections are tracked before
    connect() so task cancellation can shut down the TCP socket immediately even while
    waiting for response headers (TTFB), while keeping native system proxy and TLS behavior.
    """
    conn_lock = threading.Lock()
    active_conns: set[http.client.HTTPConnection] = set()
    active_resps: list[Any] = []

    def _abort() -> None:
        with conn_lock:
            conns = list(active_conns)
            resps = list(active_resps)
        for conn in conns:
            _close_connection_now(conn)
        for resp in resps:
            try:
                resp.close()
            except Exception:
                pass

    handle.add_cancel_hook(_abort)

    class _TrackedHTTPConnection(http.client.HTTPConnection):
        def connect(self) -> None:
            if handle.cancel_event.is_set():
                raise InterruptedError("cancelled")
            with conn_lock:
                active_conns.add(self)
            try:
                super().connect()
            except BaseException:
                with conn_lock:
                    active_conns.discard(self)
                raise
            if handle.cancel_event.is_set():
                with conn_lock:
                    active_conns.discard(self)
                _close_connection_now(self)
                raise InterruptedError("cancelled")

        def close(self) -> None:
            with conn_lock:
                active_conns.discard(self)
            super().close()

    class _TrackedHTTPSConnection(http.client.HTTPSConnection):
        def connect(self) -> None:
            if handle.cancel_event.is_set():
                raise InterruptedError("cancelled")
            with conn_lock:
                active_conns.add(self)
            try:
                super().connect()
            except BaseException:
                with conn_lock:
                    active_conns.discard(self)
                raise
            if handle.cancel_event.is_set():
                with conn_lock:
                    active_conns.discard(self)
                _close_connection_now(self)
                raise InterruptedError("cancelled")

        def close(self) -> None:
            with conn_lock:
                active_conns.discard(self)
            super().close()

    class _TrackedHTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req: urllib.request.Request) -> Any:
            return self.do_open(_TrackedHTTPConnection, req)

    class _TrackedHTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req: urllib.request.Request) -> Any:
            return self.do_open(_TrackedHTTPSConnection, req, context=self._context)

    handlers: list[Any] = [_TrackedHTTPHandler(), _TrackedHTTPSHandler()]
    if bypass_proxy:
        handlers.append(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener(*handlers), active_resps, conn_lock


def _run_openai_compat_turn(
    conversation_id: str,
    api_base: str,
    api_key: str,
    model: str,
    system_prompt: str,
    user_message: str,
    thinking_level: str = "",
) -> None:
    handle = TASK_REGISTRY.register(conversation_id)
    try:
        url = build_chat_completions_url(api_base)
        turns, _ = append_user_turn(conversation_id, user_message, initial_status="running")

        messages: list[dict[str, str]] = []
        if system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt.strip()})
        for t in turns:
            if t.get("role") in ("user", "assistant") and not t.get("cancelled"):
                messages.append({"role": t["role"], "content": str(t.get("content") or "")})

        payload: dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        valid_effort = thinking_level.strip().lower()
        if valid_effort in ("minimal", "low", "medium", "high", "xhigh"):
            payload["reasoning_effort"] = valid_effort

        headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
        if api_key.strip():
            headers["Authorization"] = f"Bearer {api_key.strip()}"

        parsed_host = (urlparse(url).hostname or "").lower()
        opener, active_resp, resp_lock = _make_cancellable_opener(
            handle,
            bypass_proxy=is_loopback_host(parsed_host),
        )

        def _post(body_obj: dict[str, Any]) -> dict[str, Any]:
            if handle.cancel_event.is_set():
                raise InterruptedError("cancelled")
            req = urllib.request.Request(
                url,
                data=json.dumps(body_obj, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            resp = opener.open(req, timeout=180)
            with resp_lock:
                active_resp.append(resp)
            try:
                raw = resp.read()
                return json.loads(raw.decode("utf-8", errors="replace"))
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
                with resp_lock:
                    if resp in active_resp:
                        active_resp.remove(resp)

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
        except InterruptedError:
            return
        except urllib.error.HTTPError as exc:
            error_msg = f"API HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')[:500]}"
        except Exception as exc:
            if handle.cancel_event.is_set():
                return
            error_msg = f"API request failed: {exc}"

        if handle.cancel_event.is_set():
            return

        finalize_turn(
            conversation_id,
            reply_text=reply_text,
            reasoning_text=reasoning_text,
            error_msg=error_msg,
            error_prefix="[API Error]",
        )
    except Exception as exc:
        if not handle.cancel_event.is_set():
            finalize_turn(conversation_id, error_msg=str(exc), error_prefix="[API Error]")
    finally:
        TASK_REGISTRY.unregister(conversation_id, handle)


class OpenAICompatAdapter(ProviderAdapter):
    meta = ProviderMeta(
        type_id="openai_compat",
        display_name="API Model",
        tag_class="api",
        uses_local_session=True,
        supports_cancel=True,
    )

    def normalize_collaborator(
        self,
        raw: dict[str, Any],
        project_workspace: str,
        models_map: dict[str, Any],
        instances: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        mref = str(raw.get("model_ref") or "").strip()
        mcfg = models_map.get(mref) if mref else None
        return {
            "id": str(raw.get("id") or (f"model:{mref}" if mref else "api")),
            "type": "openai_compat",
            "model_ref": mref,
            "label": str((mcfg or {}).get("name") or raw.get("label") or mref or raw.get("default_model") or "API"),
            "model_id": str((mcfg or {}).get("model_id") or raw.get("model_id") or raw.get("default_model") or ""),
            "api_base": str((mcfg or {}).get("api_base") or raw.get("api_base") or ""),
            "thinking_level": str((mcfg or {}).get("thinking_level") or raw.get("thinking_level") or ""),
        }

    def clean_collaborator_input(
        self,
        raw: dict[str, Any],
        idx: int,
        project_workspace: str,
    ) -> dict[str, Any] | None:
        mref = str(raw.get("model_ref") or "").strip()
        if not mref:
            return None
        return {
            "id": str(raw.get("id") or f"model:{mref}"),
            "type": "openai_compat",
            "model_ref": mref,
            "label": str(raw.get("label") or mref),
        }

    def start_conversation(
        self,
        proj: dict[str, Any],
        collab: dict[str, Any],
        prompt: str,
        title: str | None,
        project_dir: str | None,
        model: str | None,
        profile: str | None,
    ) -> dict[str, Any]:
        rt = resolve_model_runtime(collab, proj["key"])
        api_base = validate_api_base(rt["api_base"])
        chosen_model = str(model or rt["model_id"] or "").strip()
        if not chosen_model:
            raise RuntimeError("Model configuration missing model_id")
        conv_id = str(uuid.uuid4())
        now = utc_now()
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
                "workspace_path": proj.get("workspace_path") or "",
                "api_base": api_base,
                "title": title or f"{proj['display_name']} · {collab.get('label') or chosen_model}",
                "model": chosen_model,
                "thinking_level": rt["thinking_level"],
                "created_at": now,
                "updated_at": now,
                "status": "running",
                "fully_idle": False,
                "step_count": 1,
                "latest_reply_preview": "",
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
            "brief_path": None,
            "stderr": "",
        }

    def send_followup(
        self,
        conversation_id: str,
        rec: dict[str, Any],
        message: str,
        title: str | None,
        project_dir: str | None,
    ) -> dict[str, Any]:
        cid = validate_conversation_id(conversation_id)
        collab_stub = {
            "model_ref": rec.get("model_ref") or "",
            "api_base": rec.get("api_base") or "",
            "model_id": rec.get("model") or "",
            "thinking_level": rec.get("thinking_level") or "",
        }
        rt = resolve_model_runtime(collab_stub, str(rec.get("project_key") or ""))
        api_base = validate_api_base(rt["api_base"])
        chosen_model = str(rt["model_id"] or rec.get("model") or "").strip()
        if not chosen_model:
            raise RuntimeError("API session missing model")
        pre_step = read_local_session_summary(cid, last_n=1).get("last_step_index", -1)
        with StateLock():
            convs = load_json_file(CONVS_FILE)
            cur = convs.get(cid, rec)
            cur["updated_at"] = utc_now()
            cur["status"] = "running"
            cur["fully_idle"] = False
            cur["last_prompt"] = message[:500]
            convs[cid] = cur
            save_json_file(CONVS_FILE, convs)
        threading.Thread(
            target=_run_openai_compat_turn,
            args=(cid, api_base, rt["api_key"], chosen_model, rt["system_prompt"], message, rt["thinking_level"]),
            daemon=True,
        ).start()
        return {
            "ok": True,
            "provider": "openai_compat",
            "conversation_id": cid,
            "previous_step_index": pre_step,
            "brief_path": None,
            "stderr": "",
        }
