"""
High-level orchestration API for Agent Mesh:
  - Health, project & model CRUD
  - Multi-provider conversation dispatch, follow-up, wait, cancel, history, pagination
  - Relay inbox polling and submission
"""

from __future__ import annotations

import re
import secrets
import time
from pathlib import Path
from typing import Any

from .discovery import (
    ag_bridge_request,
    list_local_codex_projects,
    list_local_desktop_instances,
    list_local_workspace_projects,
    probe_antigravity_bridge,
    probe_codex,
)
from .providers import (
    finalize_turn,
    get_provider,
    list_provider_metas,
    read_local_session_summary,
)
from .security import validate_account_id, validate_api_base, validate_conversation_id
from .storage import (
    CONVS_FILE,
    MODELS_FILE,
    PROJECTS_FILE,
    SECRETS_FILE,
    TURN_EVENT,
    VERSION,
    StateLock,
    delete_secret,
    ensure_dirs,
    load_api_session,
    load_json_file,
    local_date_str,
    mask_secret,
    normalize_path,
    save_api_session,
    save_json_file,
    set_secret,
    utc_now,
)


# ---- Models & Projects ----
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
    legacy_ws = ""
    for legacy_key in [k for k in list(item.keys()) if k.endswith("_workspace")]:
        val = str(item.pop(legacy_key, "") or "").strip()
        if val and not legacy_ws:
            legacy_ws = val
    ws = str(item.pop("workspace_path", "") or legacy_ws or "").strip()
    item["key"] = key
    item["display_name"] = str(item.get("display_name") or key)
    item["workspace_path"] = ws

    ctrl = item.get("controller")
    if isinstance(ctrl, dict) and str(ctrl.get("account_id") or "").strip():
        try:
            ctrl_aid = validate_account_id(str(ctrl["account_id"]))
            ctrl_num = ctrl.get("account_number")
            item["controller"] = {
                "account_id": ctrl_aid,
                "account_number": ctrl_num,
                "label": str(ctrl.get("label") or (f"#{ctrl_num}" if ctrl_num is not None else ctrl_aid[:8])),
            }
        except ValueError:
            item["controller"] = None
    else:
        item["controller"] = None

    raw_collabs = item.get("collaborators")
    collabs: list[dict[str, Any]] = []

    if isinstance(raw_collabs, list):
        for c in raw_collabs:
            if not isinstance(c, dict):
                continue
            ctype = str(c.get("type") or "").strip()
            adapter = get_provider(ctype)
            if adapter is None:
                continue
            norm_c = adapter.normalize_collaborator(c, ws, models_map, instances)
            if norm_c is not None:
                collabs.append(norm_c)
    else:
        legacy_prov = str(item.get("provider") or "antigravity").strip()
        adapter = get_provider(legacy_prov)
        if adapter is not None:
            norm_c = adapter.normalize_collaborator(item, ws, models_map, instances)
            if norm_c is not None:
                collabs.append(norm_c)

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
            {
                "key": project_key or "explicit",
                "display_name": project_key or "explicit",
                "workspace_path": project_dir or "",
            },
            {
                "id": "antigravity",
                "type": "antigravity",
                "project_id": project_id_explicit,
                "label": "Antigravity",
                "model": "",
            },
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
            if p["workspace_path"] and normalize_path(p["workspace_path"]) == norm_target:
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
                f"#{c.get('account_number')}" if c.get("account_number") is not None else "",
                f"instance#{c.get('account_number')}" if c.get("account_number") is not None else "",
            )
            if x
        ).lower()
        if t_low in hay:
            return matched_proj, c

    avail = ", ".join(f"{c['id']} ({c['label']})" for c in collabs)
    raise ValueError(f"Target '{target}' not found in project '{matched_proj['key']}'. Available: {avail}")


# ---- Transcript & Effective Status ----
def read_conv_transcript(
    rec: dict[str, Any],
    cid: str,
    last_n: int = 20,
    include_full_latest: bool = True,
) -> dict[str, Any]:
    valid_cid = validate_conversation_id(cid)
    prov = str(rec.get("provider") or "antigravity")
    adapter = get_provider(prov)
    if adapter is not None:
        return adapter.read_transcript(rec, valid_cid, last_n=last_n, include_full_latest=include_full_latest)
    return read_local_session_summary(valid_cid, last_n=last_n)


def compute_effective_status(record: dict[str, Any], transcript: dict[str, Any]) -> str:
    if record.get("status") == "cancelled":
        return "cancelled"
    inferred = transcript.get("inferred_state")
    if inferred in ("waiting_approval", "waiting_relay", "cancelled"):
        return inferred
    if record.get("status") == "completed" and record.get("fully_idle") is True:
        if transcript.get("exists") and inferred == "running":
            return "running"
        return "completed"
    if transcript.get("exists") and inferred in ("completed", "running", "error"):
        return inferred
    return str(record.get("status") or "unknown")


def _count_pending_inbox(convs: dict[str, Any]) -> int:
    count = 0
    for rec in convs.values():
        if isinstance(rec, dict) and rec.get("provider") == "claude_instance" and rec.get("status") == "waiting_relay":
            count += 1
    return count


# ---- High-Level Public API ----
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
        "active_project_dir": active_project_dir or None,
        "agentapi_available": ag_info["available"],
        "antigravity_bridge_online": ag_info["bridge_online"],
        "codex_available": codex_info["available"],
        "codex_default_model": codex_info["default_model"] or None,
        "default_project_id": ag_info["default_project_id"],
        "tracked_conversations": len(convs),
        "configured_projects": len(projects),
        "configured_models": len(models),
        "desktop_instances_count": len(instances),
        "pending_inbox_count": _count_pending_inbox(convs),
        "providers": list_provider_metas(),
    }


def list_projects(active_project_dir: str = "") -> dict[str, Any]:
    return {
        "ok": True,
        "active_project_dir": active_project_dir or None,
        "projects": get_all_normalized_projects(),
    }


def discover_resources(force_refresh: bool = False, active_project_dir: str = "") -> dict[str, Any]:
    """App-only local resource discovery (never exposed to model)."""
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
    ws = str(body.get("workspace_path") or "").strip()
    if not ws and body.get("workspace_path") is None:
        ws = active_project_dir

    controller_in = body.get("controller")
    controller_clean: dict[str, Any] | None = None
    if isinstance(controller_in, dict) and str(controller_in.get("account_id") or "").strip():
        ctrl_aid = validate_account_id(str(controller_in["account_id"]))
        ctrl_num = controller_in.get("account_number")
        controller_clean = {
            "account_id": ctrl_aid,
            "account_number": ctrl_num,
            "label": str(controller_in.get("label") or (f"#{ctrl_num}" if ctrl_num is not None else ctrl_aid[:8])).strip(),
        }

    collabs_in = body.get("collaborators")
    if isinstance(collabs_in, list):
        cleaned_collabs: list[dict[str, Any]] = []
        for idx, c in enumerate(collabs_in):
            if not isinstance(c, dict):
                continue
            ctype = str(c.get("type") or "").strip()
            adapter = get_provider(ctype)
            if adapter is None:
                continue
            cleaned = adapter.clean_collaborator_input(c, idx, ws)
            if cleaned is not None:
                cleaned_collabs.append(cleaned)
        if not cleaned_collabs:
            raise ValueError("Please add at least one AI collaborator")
        entry = {
            "key": key,
            "display_name": display_name,
            "workspace_path": ws,
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
            "workspace_path": ws,
            "controller": None,
            "collaborators": [
                {
                    "id": "antigravity",
                    "type": "antigravity",
                    "label": display_name,
                    "project_id": pid,
                    "model": str(body.get("default_model") or "").strip(),
                    "thinking_level": "",
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
    api_base = validate_api_base(str(body.get("api_base") or ""))
    model_id = str(body.get("model_id") or "").strip()
    thinking_level = str(body.get("thinking_level") or "").strip().lower()
    system_prompt = str(body.get("system_prompt") or "").strip()
    if not key:
        key = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "-", name or model_id).strip("-").lower() or secrets.token_hex(4)
    if not model_id:
        raise ValueError("model_id is required")
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
    adapter = get_provider(str(collab["type"]))
    if adapter is None:
        raise RuntimeError(f"Unsupported collaborator provider: {collab['type']}")
    return adapter.start_conversation(
        proj=proj,
        collab=collab,
        prompt=prompt,
        title=title,
        project_dir=project_dir,
        model=model,
        profile=profile,
    )


def send_message(
    conversation_id: str,
    message: str,
    title: str | None = None,
    project_dir: str | None = None,
) -> dict[str, Any]:
    cid = validate_conversation_id(conversation_id)
    with StateLock():
        convs = load_json_file(CONVS_FILE)
        rec = convs.get(cid, {"conversation_id": cid, "created_at": utc_now()})
    prov = str(rec.get("provider") or "antigravity")
    adapter = get_provider(prov)
    if adapter is None:
        raise RuntimeError(f"Unsupported conversation provider: {prov}")
    return adapter.send_followup(
        conversation_id=cid,
        rec=rec,
        message=message,
        title=title,
        project_dir=project_dir,
    )


def cancel_conversation(conversation_id: str) -> dict[str, Any]:
    cid = validate_conversation_id(conversation_id)
    with StateLock():
        convs = load_json_file(CONVS_FILE)
        rec = convs.get(cid)
    if not isinstance(rec, dict):
        raise ValueError(f"Conversation not found: {cid}")
    prov = str(rec.get("provider") or "antigravity")
    adapter = get_provider(prov)
    was_active = adapter.cancel(cid) if adapter is not None else False

    if adapter is not None and adapter.meta.uses_local_session:
        finalize_turn(cid, cancelled=True)
    else:
        now = utc_now()
        with StateLock():
            convs = load_json_file(CONVS_FILE)
            cur = convs.get(cid, rec)
            cur["status"] = "cancelled"
            cur["fully_idle"] = True
            cur["updated_at"] = now
            cur["latest_reply_preview"] = "[Task cancelled by user]"
            convs[cid] = cur
            save_json_file(CONVS_FILE, convs)
        with TURN_EVENT:
            TURN_EVENT.notify_all()

    return {
        "ok": True,
        "conversation_id": cid,
        "status": "cancelled",
        "was_running": was_active,
    }


def wait_reply(
    conversation_id: str,
    timeout_seconds: int = 25,
    after_step: int = -1,
) -> dict[str, Any]:
    cid = validate_conversation_id(conversation_id)
    timeout = min(max(int(timeout_seconds), 1), 120)
    deadline = time.monotonic() + timeout
    while True:
        with StateLock():
            convs = load_json_file(CONVS_FILE)
            record = convs.get(cid, {"conversation_id": cid})
        tr = read_conv_transcript(record, cid, last_n=20, include_full_latest=True)
        eff = compute_effective_status(record, tr)
        latest_reply_step = tr.get("latest_reply_step", -1)
        has_new_reply = latest_reply_step > after_step and bool(tr.get("latest_reply"))

        if eff in ("waiting_approval", "waiting_relay"):
            return {
                "ok": True,
                "conversation_id": cid,
                "done": False,
                "status": eff,
                "last_step_index": tr.get("last_step_index"),
                "latest_reply": tr.get("latest_reply"),
                "recent_tools": [
                    tname for s in (tr.get("recent_steps") or [])[-5:] for tname in (s.get("tool_names") or [])
                ],
            }

        if eff == "cancelled":
            return {
                "ok": True,
                "conversation_id": cid,
                "done": True,
                "status": "cancelled",
                "last_step_index": tr.get("last_step_index"),
                "latest_reply": tr.get("latest_reply"),
                "recent_tools": [],
            }

        if eff in ("completed", "error") and (
            after_step < 0 or has_new_reply or tr.get("last_step_index", -1) > after_step
        ):
            return {
                "ok": True,
                "conversation_id": cid,
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
                "conversation_id": cid,
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
    cid = validate_conversation_id(conversation_id)
    n = min(max(int(last_n), 1), 100)
    with StateLock():
        convs = load_json_file(CONVS_FILE)
        record = convs.get(cid, {"conversation_id": cid})
    tr = read_conv_transcript(record, cid, last_n=n, include_full_latest=True)
    eff = compute_effective_status(record, tr)
    return {
        "ok": True,
        "conversation_id": cid,
        "status": eff,
        "conversation": record,
        "latest_reply": tr.get("latest_reply"),
        "turns": tr.get("turns") or [],
    }


def _hydrate_conversation_summary(cid: str, rec: dict[str, Any]) -> dict[str, Any]:
    """
    Use cached step_count and latest_reply_preview when settled to avoid N+1 file I/O.
    Only read transcript live for active/running sessions or legacy records missing cache.
    """
    item = dict(rec)
    prov = str(rec.get("provider") or "antigravity")
    status = str(rec.get("status") or "unknown")
    has_cache = "step_count" in rec and "latest_reply_preview" in rec
    is_settled = status in ("completed", "error", "cancelled") and rec.get("fully_idle") is True

    if has_cache and is_settled and prov != "antigravity":
        item["effective_status"] = status
        return item

    try:
        tr = read_conv_transcript(rec, cid, last_n=2, include_full_latest=False)
        item["effective_status"] = compute_effective_status(rec, tr)
        item["step_count"] = tr.get("step_count", rec.get("step_count", 0))
        item["latest_reply_preview"] = (tr.get("latest_reply") or rec.get("latest_reply_preview") or "")[:300]
    except Exception:
        item["effective_status"] = status
        item.setdefault("step_count", 0)
        item.setdefault("latest_reply_preview", "")
    return item


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

    candidates: list[tuple[str, dict[str, Any], str]] = []
    all_dates: set[str] = set()
    for cid, rec in convs.items():
        if not isinstance(rec, dict):
            continue
        if project:
            matched = (
                rec.get("project_key") == project
                or rec.get("project_id") == project
                or (rec.get("project_id") and rec.get("project_id") in proj_ag_ids)
            )
            if not matched:
                continue

        ts = str(rec.get("updated_at") or rec.get("created_at") or "")
        d_str = local_date_str(ts)
        if d_str:
            all_dates.add(d_str)
        if date and d_str != date:
            continue
        if collaborator:
            cf = collaborator.lower()
            c_hay = (
                f"{rec.get('collaborator_id', '')} {rec.get('provider', '')} "
                f"{rec.get('collaborator_label', '')} {rec.get('model', '')}"
            ).lower()
            if cf not in c_hay:
                continue
        candidates.append((cid, rec, d_str))

    # Sort before pagination so non-search queries only hydrate the current page!
    candidates.sort(
        key=lambda tup: str(tup[1].get("updated_at") or tup[1].get("created_at") or ""),
        reverse=True,
    )

    if query_filter:
        matched_items: list[dict[str, Any]] = []
        for cid, rec, d_str in candidates:
            item = _hydrate_conversation_summary(cid, rec)
            item["date"] = d_str
            q_hay = (
                f"{item.get('title', '')} {item.get('last_prompt', '')} "
                f"{item.get('latest_reply_preview', '')} {item.get('collaborator_label', '')}"
            ).lower()
            if query_filter in q_hay:
                matched_items.append(item)
        total = len(matched_items)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        start = (page - 1) * page_size
        paged_items = matched_items[start : start + page_size]
    else:
        total = len(candidates)
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        start = (page - 1) * page_size
        paged_items = []
        for cid, rec, d_str in candidates[start : start + page_size]:
            item = _hydrate_conversation_summary(cid, rec)
            item["date"] = d_str
            paged_items.append(item)

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
        if not isinstance(rec, dict) or rec.get("provider") != "claude_instance":
            continue
        if rec.get("status") not in ("waiting_relay", "running"):
            continue
        try:
            tr = read_local_session_summary(cid, last_n=5)
        except ValueError:
            continue
        eff = compute_effective_status(rec, tr)
        if eff != "waiting_relay":
            continue
        if account:
            af = account.lower()
            hay = (
                f"{rec.get('target_account_id', '')} {rec.get('collaborator_label', '')} "
                f"#{rec.get('target_number', '')}"
            ).lower()
            if af not in hay:
                continue
        pending_items.append(
            {
                "conversation_id": cid,
                "project_key": rec.get("project_key"),
                "title": rec.get("title"),
                "collaborator_label": rec.get("collaborator_label"),
                "target_number": rec.get("target_number"),
                "updated_at": rec.get("updated_at"),
                "last_prompt": rec.get("last_prompt"),
                "turns": tr.get("turns") or [],
            }
        )
    pending_items.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)
    return {"ok": True, "pending": pending_items}


def reply_inbox(conversation_id: str, reply_text: str, responder: str = "") -> dict[str, Any]:
    cid = validate_conversation_id(conversation_id)
    text = reply_text.strip()
    if not text:
        raise ValueError("reply is required")
    now = utc_now()
    with StateLock():
        sess = load_api_session(cid)
        turns = list(sess.get("turns") or [])
        turns.append(
            {
                "role": "assistant",
                "step_index": len(turns),
                "created_at": now,
                "content": text,
                "responder": responder,
            }
        )
        sess["turns"] = turns
        sess["status"] = "completed"
        sess["error"] = None
        save_api_session(cid, sess)

        convs = load_json_file(CONVS_FILE)
        rec = convs.get(cid, {"conversation_id": cid, "created_at": now})
        rec["updated_at"] = now
        rec["status"] = "completed"
        rec["fully_idle"] = True
        rec["step_count"] = len(turns)
        rec["latest_reply_preview"] = text[:300]
        convs[cid] = rec
        save_json_file(CONVS_FILE, convs)

    with TURN_EVENT:
        TURN_EVENT.notify_all()
    return {"ok": True, "conversation_id": cid, "status": "completed"}
