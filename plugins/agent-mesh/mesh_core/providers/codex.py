"""
Local Codex CLI / Desktop provider adapter.
Uses official `codex app-server --stdio` with a non-blocking stdout reader queue
so timeouts and task cancellation always trigger reliably, with `codex exec` fallback.
Defaults to safe `read-only` sandbox mode unless explicitly switched to `workspace-write`.
"""

from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..discovery import resolve_codex_cli
from ..security import validate_conversation_id
from ..storage import (
    CONVS_FILE,
    TASK_REGISTRY,
    VERSION,
    ActiveTaskHandle,
    StateLock,
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

DEFAULT_CODEX_SANDBOX = "read-only"
ALLOWED_CODEX_SANDBOXES = ("read-only", "workspace-write")


def _terminate_proc(proc: subprocess.Popen[Any]) -> None:
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        pass


def _run_codex_via_app_server(
    codex_bin: str,
    workspace_path: str,
    user_message: str,
    codex_thread_id: str,
    model: str,
    thinking_level: str,
    sandbox_mode: str,
    handle: ActiveTaskHandle,
    timeout: int = 300,
) -> tuple[bool, str, str, str, str | None]:
    """
    Run a turn via official `codex app-server --stdio` JSON-RPC 2.0 protocol.
    Uses a dedicated daemon stdout reader thread + Queue so timeout & cancellation
    never block on `proc.stdout.readline()`.
    Returns: (supported, thread_id, reply_text, reasoning_text, error_msg)
    """
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(
            [codex_bin, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            creationflags=creationflags,
        )
    except Exception as exc:
        return False, codex_thread_id, "", "", str(exc)

    assert proc.stdin and proc.stdout and proc.stderr
    handle.add_cancel_hook(lambda: _terminate_proc(proc))

    stderr_chunks: list[str] = []
    line_q: queue.Queue[str | None] = queue.Queue()

    def _drain_stderr() -> None:
        try:
            for line in proc.stderr:  # type: ignore[union-attr]
                if len(stderr_chunks) < 40:
                    stderr_chunks.append(line)
        except Exception:
            pass

    def _read_stdout() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line_q.put(line)
        except Exception:
            pass
        finally:
            line_q.put(None)

    threading.Thread(target=_drain_stderr, daemon=True).start()
    threading.Thread(target=_read_stdout, daemon=True).start()

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
        while True:
            if handle.cancel_event.is_set():
                return None, collected
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, collected
            try:
                line = line_q.get(timeout=min(0.25, remaining))
            except queue.Empty:
                continue
            if line is None:
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
                    "clientInfo": {"name": "agent-mesh", "title": "Agent Mesh", "version": VERSION},
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        init_res, _ = _recv_until(target_id=1, wait_sec=15.0)
        if handle.cancel_event.is_set():
            return True, new_tid, "", "", None
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
        if handle.cancel_event.is_set():
            return True, new_tid, "", "", None
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
        if handle.cancel_event.is_set():
            return True, new_tid, "", "", None
        if not turn_ack or "error" in turn_ack:
            err = (turn_ack or {}).get("error") or "".join(stderr_chunks).strip()
            return True, new_tid, "", "", f"Codex turn/start failed: {err}"

        done_msg, ev_post = _recv_until(
            stop_methods=("turn/completed", "turn/failed", "error"),
            wait_sec=float(timeout),
        )
        if handle.cancel_event.is_set():
            return True, new_tid, "", "", None

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
        _terminate_proc(proc)


def _run_codex_turn(
    conversation_id: str,
    workspace_path: str,
    user_message: str,
    codex_thread_id: str = "",
    model: str = "",
    thinking_level: str = "",
    sandbox_mode: str = DEFAULT_CODEX_SANDBOX,
) -> None:
    handle = TASK_REGISTRY.register(conversation_id)
    try:
        append_user_turn(conversation_id, user_message, initial_status="running")

        codex_bin = resolve_codex_cli()
        reply_text = ""
        reasoning_parts: list[str] = []
        new_tid = codex_thread_id
        error_msg: str | None = None

        if not codex_bin:
            error_msg = "Codex CLI not found on this machine. Install Codex Desktop or Codex CLI first."
        else:
            valid_sandbox = sandbox_mode if sandbox_mode in ALLOWED_CODEX_SANDBOXES else DEFAULT_CODEX_SANDBOX
            supported, app_tid, app_reply, app_reasoning, app_err = _run_codex_via_app_server(
                codex_bin=codex_bin,
                workspace_path=workspace_path,
                user_message=user_message,
                codex_thread_id=codex_thread_id,
                model=model,
                thinking_level=thinking_level,
                sandbox_mode=valid_sandbox,
                handle=handle,
                timeout=300,
            )
            if handle.cancel_event.is_set():
                return
            if supported:
                new_tid = app_tid or new_tid
                reply_text = app_reply
                if app_reasoning:
                    reasoning_parts.append(app_reasoning)
                error_msg = app_err
            else:
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

                    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    stdout_data = ""
                    stderr_data = ""
                    rc: int | None = None
                    try:
                        proc = subprocess.Popen(
                            cmd,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            shell=False,
                            creationflags=creationflags,
                        )
                        handle.add_cancel_hook(lambda: _terminate_proc(proc))
                        stdout_data, stderr_data = proc.communicate(input=user_message, timeout=300)
                        rc = proc.returncode
                    except subprocess.TimeoutExpired:
                        _terminate_proc(proc)
                        error_msg = "Codex execution timed out after 300s"
                    except Exception as exc:
                        error_msg = f"Failed to start Codex CLI: {exc}"

                    if handle.cancel_event.is_set():
                        return

                    if rc is not None:
                        jsonl_reply = ""
                        for raw_line in (stdout_data or "").splitlines():
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

                        if rc != 0 and not reply_text:
                            err_detail = (stderr_data or stdout_data or f"exit code {rc}").strip()
                            error_msg = err_detail[:800]
                        elif not reply_text:
                            error_msg = "Codex returned an empty response."

        if handle.cancel_event.is_set():
            return

        extra = {"codex_thread_id": new_tid} if new_tid else None
        finalize_turn(
            conversation_id,
            reply_text=reply_text,
            reasoning_text="\n\n".join(reasoning_parts).strip(),
            error_msg=error_msg,
            error_prefix="[Codex Error]",
            extra_session_fields=extra,
            extra_conv_fields=extra,
        )
    except Exception as exc:
        if not handle.cancel_event.is_set():
            finalize_turn(conversation_id, error_msg=str(exc), error_prefix="[Codex Error]")
    finally:
        TASK_REGISTRY.unregister(conversation_id, handle)


class CodexAdapter(ProviderAdapter):
    meta = ProviderMeta(
        type_id="codex",
        display_name="Codex",
        tag_class="codex",
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
        ws_val = str(raw.get("workspace_path") or project_workspace).strip()
        disp = str(raw.get("label") or (Path(ws_val.rstrip("/\\")).name if ws_val else "") or "Codex")
        raw_sb = str(raw.get("sandbox_mode") or DEFAULT_CODEX_SANDBOX).strip()
        sandbox_mode = raw_sb if raw_sb in ALLOWED_CODEX_SANDBOXES else DEFAULT_CODEX_SANDBOX
        return {
            "id": str(raw.get("id") or "codex"),
            "type": "codex",
            "label": disp,
            "project_id": str(raw.get("project_id") or ""),
            "workspace_path": ws_val,
            "model": str(raw.get("model") or ""),
            "thinking_level": str(raw.get("thinking_level") or ""),
            "sandbox_mode": sandbox_mode,
        }

    def clean_collaborator_input(
        self,
        raw: dict[str, Any],
        idx: int,
        project_workspace: str,
    ) -> dict[str, Any] | None:
        ws_c = str(raw.get("workspace_path") or project_workspace).strip()
        raw_sb = str(raw.get("sandbox_mode") or DEFAULT_CODEX_SANDBOX).strip()
        sandbox_mode = raw_sb if raw_sb in ALLOWED_CODEX_SANDBOXES else DEFAULT_CODEX_SANDBOX
        return {
            "id": str(raw.get("id") or f"codex:{idx + 1}"),
            "type": "codex",
            "label": str(raw.get("label") or (Path(ws_c.rstrip("/\\")).name if ws_c else "") or "Codex"),
            "project_id": str(raw.get("project_id") or "").strip(),
            "workspace_path": ws_c,
            "model": str(raw.get("model") or "").strip(),
            "thinking_level": str(raw.get("thinking_level") or "").strip(),
            "sandbox_mode": sandbox_mode,
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
        ws_path = str(collab.get("workspace_path") or proj.get("workspace_path") or project_dir or "").strip()
        chosen_model = str(model or collab.get("model") or "").strip()
        thinking = str(collab.get("thinking_level") or "").strip()
        raw_sb = str(collab.get("sandbox_mode") or DEFAULT_CODEX_SANDBOX).strip()
        sandbox_mode = raw_sb if raw_sb in ALLOWED_CODEX_SANDBOXES else DEFAULT_CODEX_SANDBOX
        conv_id = str(uuid.uuid4())
        now = utc_now()
        with StateLock():
            convs = load_json_file(CONVS_FILE)
            convs[conv_id] = {
                "conversation_id": conv_id,
                "provider": "codex",
                "project_key": proj["key"],
                "collaborator_id": collab["id"],
                "collaborator_label": f"Codex · {collab.get('label') or proj['display_name']}",
                "project_id": str(collab.get("project_id") or ""),
                "workspace_path": ws_path,
                "title": title or f"{proj['display_name']} · Codex",
                "model": chosen_model,
                "thinking_level": thinking,
                "sandbox_mode": sandbox_mode,
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
            "sandbox_mode": sandbox_mode,
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
        ws_path = str(rec.get("workspace_path") or rec.get("host_workspace") or project_dir or "").strip()
        chosen_model = str(rec.get("model") or "").strip()
        thinking = str(rec.get("thinking_level") or "").strip()
        raw_sb = str(rec.get("sandbox_mode") or DEFAULT_CODEX_SANDBOX).strip()
        sandbox_mode = raw_sb if raw_sb in ALLOWED_CODEX_SANDBOXES else DEFAULT_CODEX_SANDBOX
        codex_tid = str(rec.get("codex_thread_id") or "").strip()
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
            target=_run_codex_turn,
            args=(cid, ws_path, message, codex_tid, chosen_model, thinking, sandbox_mode),
            daemon=True,
        ).start()
        return {
            "ok": True,
            "provider": "codex",
            "conversation_id": cid,
            "previous_step_index": pre_step,
            "brief_path": None,
            "stderr": "",
        }
