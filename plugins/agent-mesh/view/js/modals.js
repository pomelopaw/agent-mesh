import { getLang, statusLabel, t, thinkingLabel } from "./i18n.js";
import { renderMarkdown } from "./markdown.js";
import { getProviderMeta, PROVIDER_REGISTRY } from "./providers.js";
import { callTool, confirmAction, notify, rpc } from "./rpc.js";
import {
  buildWorkspaceItems,
  el,
  formatTime,
  normPath,
  runSafe,
  slugKey,
  state,
  withBusy,
} from "./state.js";

let _refreshAll = async () => {};
let _discoverResources = async () => {};
let _loadModels = async () => {};

export function bindModalCallbacks({ refreshAll, discoverResources, loadModels }) {
  _refreshAll = refreshAll;
  _discoverResources = discoverResources;
  _loadModels = loadModels;
}

// =========================================================================
// Modal 1: Project Modal (Step 1: Project Config <-> Step 2: Add Collaborator)
// =========================================================================
export function renderProviderTabs() {
  const bar = document.getElementById("provider-tabs");
  bar.textContent = "";
  for (const p of PROVIDER_REGISTRY) {
    const btn = el(
      "button",
      {
        type: "button",
        className: "seg-btn" + (state.addAgentType === p.type ? " active" : ""),
        "data-provider": p.type,
      },
      t(p.tabKey)
    );
    btn.addEventListener("click", () => setAddAgentType(p.type));
    bar.appendChild(btn);
  }
}

export function showProjectMainStep() {
  state.projectStep = "main";
  document.getElementById("project-main-view").classList.remove("hidden");
  document.getElementById("project-add-agent-view").classList.add("hidden");
  document.getElementById("project-main-footer").classList.remove("hidden");
  document.getElementById("project-add-agent-footer").classList.add("hidden");
  document.getElementById("back-to-project-btn").classList.add("hidden");
  document.getElementById("project-modal-title").textContent =
    state.editingProjectTitle || t("create_project_title");
}

export function showAddAgentStep() {
  state.projectStep = "add_agent";
  let defaultTab = "openai_compat";
  if (Object.keys(state.savedModels).length > 0) defaultTab = "openai_compat";
  else if (state.codexAvailable) defaultTab = "codex";
  else if (state.agBridgeOnline && state.discoveredAgProjects.length > 0) defaultTab = "antigravity";

  document.getElementById("project-main-view").classList.add("hidden");
  document.getElementById("project-add-agent-view").classList.remove("hidden");
  document.getElementById("project-main-footer").classList.add("hidden");
  document.getElementById("project-add-agent-footer").classList.remove("hidden");
  document.getElementById("back-to-project-btn").classList.remove("hidden");
  document.getElementById("project-modal-title").textContent = t("add_agent_title");

  setAddAgentType(defaultTab);
}

export function renderDraftAgents() {
  const box = document.getElementById("draft-agent-list");
  document.getElementById("agent-count").textContent = String(state.draftCollaborators.length);
  box.textContent = "";

  if (state.draftCollaborators.length === 0) {
    box.appendChild(el("div", { className: "hint-box" }, t("no_draft_agents")));
    return;
  }

  state.draftCollaborators.forEach((c, idx) => {
    const item = el("div", { className: "agent-item" });
    const main = el("div", { className: "agent-item-main" });
    const head = el("div", { className: "agent-item-head" });

    const meta = getProviderMeta(c.type);
    if (meta) {
      meta.renderDraftMain(c, head, main);
    } else {
      head.appendChild(el("span", { className: "tag" }, c.type));
      head.appendChild(el("span", null, c.label || c.id));
      main.appendChild(head);
    }
    item.appendChild(main);

    const rmBtn = el("button", { className: "ghost sm danger", type: "button" }, t("btn_remove"));
    rmBtn.addEventListener("click", () => {
      state.draftCollaborators.splice(idx, 1);
      renderDraftAgents();
    });
    item.appendChild(rmBtn);
    box.appendChild(item);
  });
}

function formatInstanceLabel(inst) {
  const isZh = getLang() === "zh";
  const plan = inst.plan_label || "";
  let cap = "";
  if (inst.is_free) {
    cap = "Free · " + (isZh ? "仅对话 / 协作信箱" : "Dialogue / Relay");
  } else if (plan && inst.code_cli_ready) {
    cap = plan + " · " + (isZh ? "桌面端 + Code 就绪" : "Desktop + Code Ready");
  } else if (plan) {
    cap = plan + " · " + (isZh ? "桌面端已就绪" : "Desktop Ready");
  } else if (inst.desktop_ready || inst.desktop_status === "ready") {
    cap = isZh ? "桌面端已就绪" : "Desktop Ready";
  } else if (inst.code_cli_ready) {
    cap = isZh ? "Code 已就绪" : "Code Ready";
  } else {
    cap = isZh ? "未登录" : "Signed Out";
  }
  return "#" + inst.number + "  " + inst.email + " (" + cap + ")";
}

export function syncWsCustomVisibility() {
  const wsSel = document.getElementById("ws-select");
  document.getElementById("ws-custom-wrap").classList.toggle("hidden", wsSel.value !== "__manual__");
}

export function syncControllerState() {
  const ctrlId = document.getElementById("ctrl-select").value;
  const inst = state.discoveredInstances.find((x) => x.id === ctrlId);
  const isFree = !!(inst && inst.is_free === true);
  document.getElementById("ctrl-nocode-warn").classList.toggle("hidden", !isFree);
  // Always keep workspace selection available regardless of CLI link state or plan tier
  document.getElementById("ctrl-ws-field").classList.remove("hidden");
}

export function currentPrimaryWorkspace() {
  const wsSel = document.getElementById("ws-select");
  return wsSel.value === "__manual__" ? document.getElementById("proj-ws").value.trim() : wsSel.value;
}

export function onWorkspaceChanged() {
  const wsSel = document.getElementById("ws-select");
  const val = wsSel.value;
  if (val !== "__manual__") {
    const wsItems = buildWorkspaceItems();
    const found = wsItems.find((x) => x.path === val);
    const disp = (found && found.name) || val.replace(/\\/g, "/").split("/").filter(Boolean).pop() || "";
    document.getElementById("proj-name").value = disp;
    document.getElementById("proj-key").value = slugKey(disp);
  }
  syncWsCustomVisibility();
}

function populateProjectModal(editingItem) {
  const ctrlSel = document.getElementById("ctrl-select");
  ctrlSel.textContent = "";
  if (state.discoveredInstances.length === 0) {
    ctrlSel.appendChild(el("option", { value: "" }, t("default_ctrl")));
  } else {
    for (const inst of state.discoveredInstances) {
      ctrlSel.appendChild(el("option", { value: inst.id }, formatInstanceLabel(inst)));
    }
  }

  const wsSel = document.getElementById("ws-select");
  wsSel.textContent = "";
  const wsItems = buildWorkspaceItems();
  for (let i = 0; i < wsItems.length; i++) {
    const w = wsItems[i];
    wsSel.appendChild(el("option", { value: w.path }, "#" + (i + 1) + "  " + w.name + "  (" + w.path + ")"));
  }
  wsSel.appendChild(el("option", { value: "__manual__" }, t("manual_ws_opt")));

  if (editingItem) {
    state.editingProjectTitle = t("edit_project_title");
    if (editingItem.controller && editingItem.controller.account_id) {
      ctrlSel.value = editingItem.controller.account_id;
    }
    const wsPath = editingItem.workspace_path || "";
    const wsMatch = wsItems.find((w) => normPath(w.path) === normPath(wsPath));
    if (wsMatch) {
      wsSel.value = wsMatch.path;
      document.getElementById("proj-ws").value = "";
    } else {
      wsSel.value = "__manual__";
      document.getElementById("proj-ws").value = wsPath;
    }
    document.getElementById("proj-name").value = editingItem.display_name || editingItem.key || "";
    document.getElementById("proj-key").value = editingItem.key || "";
    state.draftCollaborators = (editingItem.collaborators || []).map((c) => ({ ...c }));
  } else {
    state.editingProjectTitle = t("create_project_title");
    document.getElementById("proj-ws").value = "";
    const firstReadyInst =
      state.discoveredInstances.find((x) => x.code_cli_ready) ||
      state.discoveredInstances.find((x) => x.desktop_ready || !x.is_free);
    if (firstReadyInst) ctrlSel.value = firstReadyInst.id;
    const boundWs = new Set(Object.values(state.savedProjects).map((p) => normPath(p.workspace_path)));
    const firstUnbound = wsItems.find((w) => !boundWs.has(normPath(w.path)));
    if (firstUnbound) wsSel.value = firstUnbound.path;
    onWorkspaceChanged();
    state.draftCollaborators = [];
  }

  syncControllerState();
  syncWsCustomVisibility();
  renderDraftAgents();
  showProjectMainStep();
}

export async function openProjectModal(editingItem) {
  await runSafe(async () => {
    if (state.discoveredWorkspaceProjects.length === 0 && state.discoveredAgProjects.length === 0) {
      await _discoverResources(false);
    }
    populateProjectModal(editingItem);
    document.getElementById("project-backdrop").classList.remove("hidden");
  });
}

export function closeProjectModal() {
  document.getElementById("project-backdrop").classList.add("hidden");
  showProjectMainStep();
}

export async function saveProject(btn) {
  await withBusy(btn, async () => {
    const ctrlId = document.getElementById("ctrl-select").value;
    const ctrlInst = state.discoveredInstances.find((x) => x.id === ctrlId) || null;
    const controller = ctrlInst
      ? { account_id: ctrlInst.id, account_number: ctrlInst.number, label: "#" + ctrlInst.number }
      : null;

    const wsSel = document.getElementById("ws-select");
    const workspace_path =
      wsSel.value === "__manual__"
        ? document.getElementById("proj-ws").value.trim()
        : wsSel.value;
    const display_name = document.getElementById("proj-name").value.trim();
    const key = document.getElementById("proj-key").value.trim() || slugKey(display_name);

    if (!key) {
      await notify(t("err_need_key"), "error");
      return;
    }
    if (state.draftCollaborators.length === 0) {
      await notify(t("err_need_agent"), "error");
      return;
    }

    await callTool("mesh_save_project", {
      key,
      display_name: display_name || key,
      workspace_path,
      controller,
      collaborators: state.draftCollaborators,
    });
    closeProjectModal();
    await notify((display_name || key) + " (" + state.draftCollaborators.length + ")", "info");
    await _refreshAll(false);
  });
}

// ---- Step 2: Add-Agent Form Logic ----
export function setAddAgentType(type) {
  state.addAgentType = type;
  renderProviderTabs();
  for (const p of PROVIDER_REGISTRY) {
    const panel = document.getElementById(p.panelId);
    if (panel) panel.classList.toggle("hidden", p.type !== type);
  }
  populateAddAgentPanel();
}

export function syncCodexSandboxWarn() {
  const sb = document.getElementById("sub-codex-sandbox").value;
  document.getElementById("sub-codex-write-warn").classList.toggle("hidden", sb !== "workspace-write");
}

export function updateApiSelectionSummary() {
  const key = document.getElementById("sub-api-select").value;
  const summary = document.getElementById("sub-api-summary");
  const m = state.savedModels[key];
  if (!m) {
    summary.classList.add("hidden");
    return;
  }
  summary.classList.remove("hidden");
  const th = thinkingLabel(m.thinking_level || "");
  summary.textContent = m.api_base + " · " + m.model_id + " · " + th;
}

export function updateClaudeInstanceModeNote() {
  const aid = document.getElementById("sub-claude-inst").value;
  const inst = state.discoveredInstances.find((x) => x.id === aid);
  const note = document.getElementById("sub-claude-mode-note");
  const projField = document.getElementById("sub-claude-proj-field");
  if (!inst) {
    note.textContent = "";
    projField.classList.add("hidden");
    return;
  }
  const isFree = inst.is_free === true;
  projField.classList.toggle("hidden", isFree);
  const isZh = getLang() === "zh";
  note.textContent = inst.cli_ready
    ? (isZh ? "已检测到本地 CLI 与配置根目录，将直接后台执行。" : "Local CLI and config root detected; runs directly in background.")
    : (isZh ? "派发任务后将进入「协作信箱」，可在协作信箱或对应桌面窗口接单回复。" : "Tasks will queue in Relay Inbox for pickup via the Inbox drawer or secondary assistant.");
}

export function populateAddAgentPanel() {
  const primaryWs = currentPrimaryWorkspace();
  const primaryName = document.getElementById("proj-name").value.trim();

  if (state.addAgentType === "antigravity") {
    const sel = document.getElementById("sub-ag-select");
    const offlineWarn = document.getElementById("sub-ag-offline-warn");
    const emptyWarn = document.getElementById("sub-ag-empty-warn");
    sel.textContent = "";
    if (!state.agBridgeOnline) {
      sel.appendChild(el("option", { value: "" }, "—"));
      offlineWarn.classList.remove("hidden");
      emptyWarn.classList.add("hidden");
    } else if (state.discoveredAgProjects.length === 0) {
      sel.appendChild(el("option", { value: "" }, "—"));
      offlineWarn.classList.add("hidden");
      emptyWarn.classList.remove("hidden");
    } else {
      offlineWarn.classList.add("hidden");
      for (const ag of state.discoveredAgProjects) {
        const extra = ag.workspace ? " | " + ag.workspace : ag.sample_title ? " | " + ag.sample_title : "";
        sel.appendChild(el("option", { value: ag.project_id }, ag.name + extra));
      }
      const match =
        state.discoveredAgProjects.find((a) => a.workspace && normPath(a.workspace) === normPath(primaryWs)) ||
        state.discoveredAgProjects.find((a) => a.name && primaryName && a.name.toLowerCase() === primaryName.toLowerCase());
      if (match) sel.value = match.project_id;
      emptyWarn.classList.toggle("hidden", !!sel.value);
    }
    document.getElementById("sub-ag-model").value = "";
    document.getElementById("sub-ag-thinking").value = "";
    document.getElementById("sub-ag-custom-model").value = "";
    document.getElementById("sub-ag-custom-model-wrap").classList.add("hidden");
  } else if (state.addAgentType === "openai_compat") {
    const sel = document.getElementById("sub-api-select");
    const warn = document.getElementById("sub-api-empty-warn");
    const summary = document.getElementById("sub-api-summary");
    sel.textContent = "";
    const keys = Object.keys(state.savedModels);
    if (keys.length === 0) {
      sel.appendChild(el("option", { value: "" }, "—"));
      warn.classList.remove("hidden");
      summary.classList.add("hidden");
    } else {
      warn.classList.add("hidden");
      for (const k of keys) {
        const m = state.savedModels[k];
        const th = m.thinking_level ? " · " + thinkingLabel(m.thinking_level) : "";
        sel.appendChild(el("option", { value: k }, (m.name || k) + " (" + m.model_id + th + ")"));
      }
      updateApiSelectionSummary();
    }
  } else if (state.addAgentType === "codex") {
    const offlineWarn = document.getElementById("sub-codex-offline-warn");
    const fieldsWrap = document.getElementById("sub-codex-fields");
    offlineWarn.classList.toggle("hidden", state.codexAvailable);
    fieldsWrap.classList.toggle("hidden", !state.codexAvailable);
    if (state.codexAvailable) {
      const projSel = document.getElementById("sub-codex-proj");
      projSel.textContent = "";
      for (const cp of state.discoveredCodexProjects) {
        const extra = cp.workspace ? " (" + cp.workspace + ")" : "";
        projSel.appendChild(el("option", { value: cp.workspace || cp.project_id }, cp.name + extra));
      }
      if (primaryWs && !state.discoveredCodexProjects.some((cp) => normPath(cp.workspace) === normPath(primaryWs))) {
        projSel.appendChild(el("option", { value: primaryWs }, primaryWs));
      }
      if (projSel.options.length === 0) {
        projSel.appendChild(el("option", { value: primaryWs || "" }, primaryWs || t("follow_default")));
      }
      const match = state.discoveredCodexProjects.find((cp) => cp.workspace && normPath(cp.workspace) === normPath(primaryWs));
      if (match) projSel.value = match.workspace;
      else if (primaryWs) projSel.value = primaryWs;

      const mSel = document.getElementById("sub-codex-model");
      mSel.textContent = "";
      const defLabel = state.codexDefaultModel ? t("follow_default") + " (" + state.codexDefaultModel + ")" : t("follow_default");
      mSel.appendChild(el("option", { value: "" }, defLabel));
      for (const slug of state.discoveredCodexModels) {
        mSel.appendChild(el("option", { value: slug }, slug));
      }
      document.getElementById("sub-codex-thinking").value = "";
      // Safe default: read-only
      document.getElementById("sub-codex-sandbox").value = "read-only";
      syncCodexSandboxWarn();
    }
  } else if (state.addAgentType === "claude_instance") {
    const ctrlId = document.getElementById("ctrl-select").value;
    const others = state.discoveredInstances.filter((x) => x.id !== ctrlId);
    const instSel = document.getElementById("sub-claude-inst");
    const warn = document.getElementById("sub-claude-empty-warn");
    const fieldsWrap = document.getElementById("sub-claude-fields");
    instSel.textContent = "";

    if (others.length === 0) {
      instSel.appendChild(el("option", { value: "" }, "—"));
      warn.classList.remove("hidden");
      fieldsWrap.classList.add("hidden");
    } else {
      warn.classList.add("hidden");
      fieldsWrap.classList.remove("hidden");
      for (const inst of others) {
        instSel.appendChild(el("option", { value: inst.id }, formatInstanceLabel(inst)));
      }
      const projSel = document.getElementById("sub-claude-proj");
      projSel.textContent = "";
      const wsItems = buildWorkspaceItems();
      for (let i = 0; i < wsItems.length; i++) {
        const w = wsItems[i];
        projSel.appendChild(el("option", { value: w.path }, "#" + (i + 1) + "  " + w.name + "  (" + w.path + ")"));
      }
      if (primaryWs && wsItems.some((w) => normPath(w.path) === normPath(primaryWs))) {
        projSel.value = wsItems.find((w) => normPath(w.path) === normPath(primaryWs)).path;
      }
      document.getElementById("sub-claude-model").value = "";
      document.getElementById("sub-claude-thinking").value = "";
      updateClaudeInstanceModeNote();
    }
  }
}

export async function confirmAddAgent() {
  await runSafe(async () => {
    if (state.addAgentType === "antigravity") {
      const pid = document.getElementById("sub-ag-select").value;
      if (!pid) {
        await notify(state.agBridgeOnline ? t("ag_empty_warn") : t("ag_offline_warn"), "error");
        return;
      }
      const ag = state.discoveredAgProjects.find((a) => a.project_id === pid);
      let model = document.getElementById("sub-ag-model").value;
      if (model === "__custom__") {
        model = document.getElementById("sub-ag-custom-model").value.trim();
      }
      const thinking = document.getElementById("sub-ag-thinking").value;
      const existsIdx = state.draftCollaborators.findIndex((c) => c.type === "antigravity" && c.project_id === pid);
      const entry = {
        id: "antigravity:" + pid.slice(0, 8),
        type: "antigravity",
        label: (ag && ag.name) || "Antigravity",
        project_id: pid,
        model,
        thinking_level: thinking,
      };
      if (existsIdx >= 0) state.draftCollaborators[existsIdx] = entry;
      else state.draftCollaborators.push(entry);
    } else if (state.addAgentType === "openai_compat") {
      const mref = document.getElementById("sub-api-select").value;
      if (!mref || !state.savedModels[mref]) {
        await notify(t("api_empty_warn"), "error");
        return;
      }
      const m = state.savedModels[mref];
      const entry = {
        id: "model:" + mref,
        type: "openai_compat",
        model_ref: mref,
        label: m.name || mref,
        model_id: m.model_id,
        thinking_level: m.thinking_level || "",
      };
      const existsIdx = state.draftCollaborators.findIndex((c) => c.type === "openai_compat" && c.model_ref === mref);
      if (existsIdx >= 0) state.draftCollaborators[existsIdx] = entry;
      else state.draftCollaborators.push(entry);
    } else if (state.addAgentType === "codex") {
      if (!state.codexAvailable) {
        await notify(t("codex_offline_warn"), "error");
        return;
      }
      const wsPath = document.getElementById("sub-codex-proj").value || currentPrimaryWorkspace();
      const matchedCp = state.discoveredCodexProjects.find((cp) => cp.workspace === wsPath || cp.project_id === wsPath);
      const label = (matchedCp && matchedCp.name) || (wsPath ? wsPath.replace(/\\/g, "/").split("/").filter(Boolean).pop() : "Codex");
      const model = document.getElementById("sub-codex-model").value;
      const thinking = document.getElementById("sub-codex-thinking").value;
      const sandboxMode = document.getElementById("sub-codex-sandbox").value || "read-only";
      const entry = {
        id: "codex:" + slugKey(label || "default"),
        type: "codex",
        label: label || "Codex",
        project_id: (matchedCp && matchedCp.project_id) || "",
        workspace_path: wsPath,
        model,
        thinking_level: thinking,
        sandbox_mode: sandboxMode,
      };
      const existsIdx = state.draftCollaborators.findIndex((c) => c.type === "codex" && normPath(c.workspace_path) === normPath(wsPath));
      if (existsIdx >= 0) state.draftCollaborators[existsIdx] = entry;
      else state.draftCollaborators.push(entry);
    } else if (state.addAgentType === "claude_instance") {
      const aid = document.getElementById("sub-claude-inst").value;
      const inst = state.discoveredInstances.find((x) => x.id === aid);
      if (!inst) {
        await notify(t("claude_empty_warn"), "error");
        return;
      }
      const wsPath = inst.has_code_access ? (document.getElementById("sub-claude-proj").value || currentPrimaryWorkspace()) : "";
      const model = document.getElementById("sub-claude-model").value;
      const thinking = document.getElementById("sub-claude-thinking").value;
      const entry = {
        id: "claude:" + inst.id,
        type: "claude_instance",
        account_id: inst.id,
        account_number: inst.number,
        cli_ready: inst.cli_ready,
        has_code_access: inst.has_code_access,
        workspace_path: wsPath,
        model,
        thinking_level: thinking,
        label: "#" + inst.number,
      };
      const existsIdx = state.draftCollaborators.findIndex((c) => c.type === "claude_instance" && c.account_id === inst.id);
      if (existsIdx >= 0) state.draftCollaborators[existsIdx] = entry;
      else state.draftCollaborators.push(entry);
    }

    showProjectMainStep();
    renderDraftAgents();
  });
}

// =========================================================================
// Modal 2: Paginated Sessions Modal + Safe Markdown Detail + Task Cancel
// =========================================================================
export function showSessionsListView() {
  document.getElementById("sessions-list-view").classList.remove("hidden");
  document.getElementById("sessions-detail-view").classList.add("hidden");
  document.getElementById("back-to-list-btn").classList.add("hidden");
  document.getElementById("sessions-footer").classList.remove("hidden");
  document.getElementById("sessions-modal-title").textContent =
    (state.sessionsProjectTitle || t("all_sessions_title")) + " · " + t("btn_sessions");
}

function showSessionsDetailView(title) {
  document.getElementById("sessions-list-view").classList.add("hidden");
  document.getElementById("sessions-detail-view").classList.remove("hidden");
  document.getElementById("back-to-list-btn").classList.remove("hidden");
  document.getElementById("sessions-footer").classList.add("hidden");
  document.getElementById("sessions-modal-title").textContent = title || t("sessions_title");
}

export async function openSessionsModal(projectKey, title, collabs) {
  state.sessionsProjectKey = projectKey || "";
  state.sessionsProjectTitle = title || t("all_sessions_title");
  state.sessionsPage = 1;
  document.getElementById("filter-date").value = "";
  document.getElementById("filter-q").value = "";

  const colSel = document.getElementById("filter-collab");
  colSel.textContent = "";
  colSel.appendChild(el("option", { value: "" }, t("all_collabs")));
  if (collabs && collabs.length > 0) {
    for (const c of collabs) {
      colSel.appendChild(el("option", { value: c.id }, c.label || c.id));
    }
  } else {
    for (const p of PROVIDER_REGISTRY) {
      colSel.appendChild(el("option", { value: p.type }, t(p.tabKey)));
    }
  }

  showSessionsListView();
  document.getElementById("sessions-backdrop").classList.remove("hidden");
  await loadSessionsPage(1);
}

export function closeSessionsModal() {
  document.getElementById("sessions-backdrop").classList.add("hidden");
}

export async function loadSessionsPage(page) {
  await runSafe(async () => {
    state.sessionsPage = Math.max(1, page);
    const box = document.getElementById("sessions-list-box");
    box.textContent = "";
    box.appendChild(el("div", { className: "project-sub" }, t("loading_sessions")));

    const args = { page: state.sessionsPage, page_size: state.sessionsPageSize };
    if (state.sessionsProjectKey) args.project = state.sessionsProjectKey;
    const dateVal = document.getElementById("filter-date").value.trim();
    if (dateVal) args.date = dateVal;
    const collabVal = document.getElementById("filter-collab").value.trim();
    if (collabVal) args.collaborator = collabVal;
    const qVal = document.getElementById("filter-q").value.trim();
    if (qVal) args.q = qVal;

    const data = await callTool("mesh_list_conversations", args);
    const items = data.items || [];
    const total = data.total || 0;
    state.sessionsPage = data.page || 1;
    state.sessionsTotalPages = data.total_pages || 1;

    document.getElementById("page-info").textContent =
      total + " · " + state.sessionsPage + " / " + state.sessionsTotalPages;
    document.getElementById("prev-page-btn").disabled = state.sessionsPage <= 1;
    document.getElementById("next-page-btn").disabled = state.sessionsPage >= state.sessionsTotalPages;

    box.textContent = "";
    if (items.length === 0) {
      box.appendChild(el("div", { className: "empty-state" }, t("no_sessions")));
      return;
    }

    for (const c of items) {
      const row = el("div", { className: "session-row", tabindex: "0", role: "button" });
      const top = el("div", { className: "session-top" });
      top.appendChild(el("span", { className: "session-title" }, c.title || (c.conversation_id || "").slice(0, 8)));

      const st = c.effective_status || c.status || "unknown";
      const stCls = st === "completed" ? "ok" : st === "error" || st === "cancelled" ? "err" : "warn";
      top.appendChild(el("span", { className: "tag " + stCls }, statusLabel(st)));
      row.appendChild(top);

      const meta = el("div", { className: "session-meta" });
      const prov = c.provider || "antigravity";
      const pMeta = getProviderMeta(prov);
      const pCls = pMeta ? pMeta.tagClass : "ag";
      meta.appendChild(el("span", { className: "tag " + pCls }, c.collaborator_label || c.model || prov));
      if (c.updated_at || c.created_at) meta.appendChild(el("span", null, formatTime(c.updated_at || c.created_at)));
      meta.appendChild(el("span", null, "ID: " + (c.conversation_id || "").slice(0, 8)));
      row.appendChild(meta);

      const snippet = c.latest_reply_preview || c.last_prompt || "";
      if (snippet) row.appendChild(el("div", { className: "session-snippet" }, snippet));

      const openDetail = () => openSessionDetail(c.conversation_id, c.title || t("sessions_title"));
      row.addEventListener("click", openDetail);
      row.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openDetail();
        }
      });
      box.appendChild(row);
    }
  });
}

export async function openSessionDetail(convId, title) {
  await runSafe(async () => {
    state.currentDetailConvId = convId;
    state.currentDetailTitle = title || "";
    showSessionsDetailView(title);
    const turnsBox = document.getElementById("detail-turns-box");
    const metaEl = document.getElementById("detail-meta");
    const cancelBtn = document.getElementById("cancel-task-btn");
    cancelBtn.classList.add("hidden");
    turnsBox.textContent = "";
    turnsBox.appendChild(el("div", { className: "project-sub" }, t("loading_detail")));
    state.currentDetailReply = "";

    const data = await callTool("mesh_get_history", { conversation_id: convId, last_n: 50 });
    const conv = data.conversation || {};
    const turns = data.turns || [];
    state.currentDetailReply = data.latest_reply || "";

    metaEl.textContent =
      (conv.collaborator_label || conv.model || conv.provider || "") +
      " · " + statusLabel(data.status || "unknown") +
      " · " + turns.length;

    cancelBtn.classList.toggle("hidden", data.status !== "running");

    turnsBox.textContent = "";
    if (turns.length === 0) {
      turnsBox.appendChild(el("div", { className: "empty-state" }, t("no_turns")));
      return;
    }

    for (const turn of turns) {
      const isUser = turn.role === "user";
      const card = el("div", { className: "turn-card " + (isUser ? "user" : "assistant") });
      const head = el("div", { className: "turn-head" });
      head.appendChild(el("strong", null, isUser ? t("role_user") : (conv.collaborator_label || t("role_assistant"))));
      head.appendChild(el("span", null, formatTime(turn.created_at)));
      card.appendChild(head);

      if (turn.reasoning_content) {
        const det = el("details", { className: "thinking-box" });
        det.appendChild(el("summary", null, t("view_thinking")));
        det.appendChild(el("pre", { className: "turn-body mt-6" }, turn.reasoning_content));
        card.appendChild(det);
      }

      if (isUser) {
        card.appendChild(el("pre", { className: "turn-body" }, turn.content || ""));
      } else {
        card.appendChild(renderMarkdown(turn.content || ""));
      }
      turnsBox.appendChild(card);
    }
  });
}

export async function cancelCurrentTask(btn) {
  if (!state.currentDetailConvId) return;
  const ok = await confirmAction(t("cancel_task_title"), t("cancel_task_msg"), t("confirm_label"));
  if (!ok) return;
  await withBusy(btn, async () => {
    await callTool("mesh_cancel_conversation", { conversation_id: state.currentDetailConvId });
    await notify(t("task_cancelled_ok"), "info");
    await openSessionDetail(state.currentDetailConvId, state.currentDetailTitle);
  });
}

// =========================================================================
// Modal 3: Standalone Model Settings (Single Entry Point for API Models)
// =========================================================================
export function resetModelForm() {
  document.getElementById("model-form-title").textContent = t("add_model_heading");
  document.getElementById("m-key").value = "";
  document.getElementById("m-name").value = "";
  document.getElementById("m-id").value = "";
  document.getElementById("m-base").value = "";
  document.getElementById("m-thinking").value = "";
  document.getElementById("m-apikey").value = "";
  document.getElementById("m-apikey").placeholder = t("api_key_ph");
  document.getElementById("m-system").value = "";
}

function fillModelForm(key, m) {
  document.getElementById("model-form-title").textContent = t("edit_model_prefix") + (m.name || key);
  document.getElementById("m-key").value = key;
  document.getElementById("m-name").value = m.name || "";
  document.getElementById("m-id").value = m.model_id || "";
  document.getElementById("m-base").value = m.api_base || "";
  document.getElementById("m-thinking").value = m.thinking_level || "";
  document.getElementById("m-apikey").value = "";
  document.getElementById("m-apikey").placeholder = m.has_api_key
    ? (m.api_key_masked || "****") + t("api_key_keep_hint")
    : t("api_key_ph");
  document.getElementById("m-system").value = m.system_prompt || "";
}

export function renderSavedModelsList() {
  const box = document.getElementById("saved-models-list");
  box.textContent = "";
  const keys = Object.keys(state.savedModels);
  if (keys.length === 0) {
    box.appendChild(el("div", { className: "project-sub" }, t("no_saved_models")));
    return;
  }
  for (const key of keys) {
    const m = state.savedModels[key];
    const item = el("div", { className: "model-item" });
    const main = el("div", { className: "model-item-main" });
    const head = el("div", { className: "project-head" });
    head.appendChild(el("strong", null, m.name || key));
    head.appendChild(el("span", { className: "tag api" }, m.model_id));
    head.appendChild(el("span", { className: "tag" }, thinkingLabel(m.thinking_level || "")));
    if (m.has_api_key) head.appendChild(el("span", { className: "tag ok" }, t("tag_key_ok")));
    main.appendChild(head);
    main.appendChild(el("div", { className: "project-sub" }, m.api_base));
    item.appendChild(main);

    const acts = el("div", { className: "row-actions" });
    const editBtn = el("button", { className: "ghost sm", type: "button" }, t("btn_edit"));
    editBtn.addEventListener("click", () => fillModelForm(key, m));
    acts.appendChild(editBtn);

    const delBtn = el("button", { className: "ghost sm danger", type: "button" }, t("btn_delete"));
    delBtn.addEventListener("click", async () => {
      const ok = await confirmAction(t("delete_model_title"), (m.name || key) + "?", t("confirm_label"));
      if (!ok) return;
      await withBusy(delBtn, async () => {
        await callTool("mesh_delete_model", { key });
        await _loadModels();
        renderSavedModelsList();
        await _refreshAll(false);
      });
    });
    acts.appendChild(delBtn);

    item.appendChild(acts);
    box.appendChild(item);
  }
}

export async function openModelsModal() {
  await runSafe(async () => {
    await _loadModels();
    renderSavedModelsList();
    resetModelForm();
    document.getElementById("models-backdrop").classList.remove("hidden");
  });
}

export function closeModelsModal() {
  document.getElementById("models-backdrop").classList.add("hidden");
}

export async function saveModel(btn) {
  await withBusy(btn, async () => {
    const key = document.getElementById("m-key").value.trim();
    const name = document.getElementById("m-name").value.trim();
    const model_id = document.getElementById("m-id").value.trim();
    const api_base = document.getElementById("m-base").value.trim();
    const thinking_level = document.getElementById("m-thinking").value;
    const api_key = document.getElementById("m-apikey").value.trim();
    const system_prompt = document.getElementById("m-system").value.trim();

    if (!api_base || !model_id) {
      await notify(t("err_need_model_fields"), "error");
      return;
    }

    await callTool("mesh_save_model", {
      key,
      name: name || model_id,
      api_base,
      model_id,
      thinking_level,
      api_key,
      system_prompt,
    });
    await _loadModels();
    renderSavedModelsList();
    resetModelForm();
    await _refreshAll(false);
    await notify(name || model_id, "info");
  });
}

// =========================================================================
// Modal 4: Relay Inbox Modal
// =========================================================================
export async function loadInboxList() {
  await runSafe(async () => {
    const box = document.getElementById("inbox-list-box");
    box.textContent = "";
    box.appendChild(el("div", { className: "project-sub" }, t("loading")));

    const data = await callTool("mesh_check_inbox", {});
    const pending = data.pending || [];
    state.pendingInboxCount = pending.length;
    updateInboxBadge();

    box.textContent = "";
    if (pending.length === 0) {
      box.appendChild(el("div", { className: "empty-state" }, t("inbox_empty")));
      return;
    }

    for (const item of pending) {
      const card = el("div", { className: "inbox-card" });
      const top = el("div", { className: "session-top" });
      top.appendChild(el("span", { className: "session-title" }, item.title || item.conversation_id.slice(0, 8)));
      top.appendChild(el("span", { className: "tag warn" }, statusLabel("waiting_relay")));
      card.appendChild(top);

      const meta = el("div", { className: "session-meta" });
      if (item.collaborator_label) meta.appendChild(el("span", { className: "tag claude" }, item.collaborator_label));
      if (item.project_key) meta.appendChild(el("span", { className: "tag" }, item.project_key));
      if (item.updated_at) meta.appendChild(el("span", null, formatTime(item.updated_at)));
      card.appendChild(meta);

      const lastTurn = (item.turns || []).slice().reverse().find((x) => x.role === "user");
      const promptText = (lastTurn && lastTurn.content) || item.last_prompt || "";
      card.appendChild(el("pre", { className: "turn-body" }, promptText));

      const replyWrap = el("div", { className: "inbox-reply-row" });
      const ta = el("textarea", { placeholder: t("inbox_reply_ph") });
      replyWrap.appendChild(ta);

      const actRow = el("div", { className: "inbox-reply-actions" });
      const sendBtn = el("button", { type: "button", className: "primary sm" }, t("inbox_send_btn"));
      sendBtn.addEventListener("click", async () => {
        const replyVal = ta.value.trim();
        if (!replyVal) {
          await notify(t("err_need_reply"), "error");
          return;
        }
        await withBusy(sendBtn, async () => {
          await callTool("mesh_reply_inbox", {
            conversation_id: item.conversation_id,
            reply: replyVal,
            responder: item.collaborator_label || "panel",
          });
          await notify(t("inbox_sent_ok"), "info");
          await loadInboxList();
          await _refreshAll(false);
        });
      });
      actRow.appendChild(sendBtn);
      replyWrap.appendChild(actRow);
      card.appendChild(replyWrap);
      box.appendChild(card);
    }
  });
}

export function updateInboxBadge() {
  const badge = document.getElementById("inbox-badge");
  if (!badge) return;
  const count = state.pendingInboxCount || 0;
  badge.textContent = String(count);
  badge.classList.toggle("hidden", count <= 0);
}

export async function openInboxModal() {
  document.getElementById("inbox-backdrop").classList.remove("hidden");
  await loadInboxList();
}

export function closeInboxModal() {
  document.getElementById("inbox-backdrop").classList.add("hidden");
}

export async function copySelectedWorkspace() {
  await runSafe(async () => {
    const ws = currentPrimaryWorkspace();
    if (!ws) {
      await notify(t("no_path_copy"), "error");
      return;
    }
    await rpc("ui/copy", { text: ws });
    await notify(t("copied_path") + ws, "info");
  });
}
