import { applyDomI18n, getLang, setLocale, t } from "./i18n.js";
import {
  bindModalCallbacks,
  cancelCurrentTask,
  closeInboxModal,
  closeModelsModal,
  closeProjectModal,
  closeSessionsModal,
  confirmAddAgent,
  copySelectedWorkspace,
  loadInboxList,
  loadSessionsPage,
  onWorkspaceChanged,
  openInboxModal,
  openModelsModal,
  openProjectModal,
  openSessionsModal,
  populateAddAgentPanel,
  renderProviderTabs,
  resetModelForm,
  saveModel,
  saveProject,
  showAddAgentStep,
  showProjectMainStep,
  showSessionsListView,
  syncCodexSandboxWarn,
  syncControllerState,
  updateApiSelectionSummary,
  updateClaudeInstanceModeNote,
  updateInboxBadge,
} from "./modals.js";
import { renderCollaboratorChip } from "./providers.js";
import { applyTheme, callTool, confirmAction, notify, rpc } from "./rpc.js";
import { el, normPath, runSafe, state, withBusy } from "./state.js";

function renderProjectList(totalConvs) {
  const listBox = document.getElementById("project-list");
  listBox.textContent = "";

  const keys = Object.keys(state.savedProjects);
  const isZh = getLang() === "zh";
  document.getElementById("summary-line").textContent = isZh
    ? "共 " + keys.length + " 个协作项目 · " + Object.keys(state.savedModels).length + " 个模型 · " + state.discoveredInstances.length + " 个桌面实例 · " + (totalConvs || 0) + " 条会话"
    : keys.length + " projects · " + Object.keys(state.savedModels).length + " models · " + state.discoveredInstances.length + " profiles · " + (totalConvs || 0) + " sessions";

  if (keys.length === 0) {
    const empty = el("div", { className: "empty-state" });
    empty.appendChild(el("p", null, t("empty_projects")));
    const btn = el("button", { className: "primary", type: "button" }, t("create_first_project"));
    btn.addEventListener("click", () => openProjectModal(null));
    empty.appendChild(btn);
    listBox.appendChild(empty);
    return;
  }

  for (const key of keys) {
    const p = state.savedProjects[key];
    const collabs = p.collaborators || [];
    const row = el("div", { className: "project-row" });

    const main = el("div", { className: "project-main" });

    // Row 1: Project Name + Controller + Active badge
    const head = el("div", { className: "project-head" });
    head.appendChild(el("span", { className: "project-name" }, p.display_name || key));

    if (p.controller && p.controller.account_id) {
      const inst = state.discoveredInstances.find((x) => x.id === p.controller.account_id);
      const ctrlText = inst
        ? "#" + inst.number + " " + inst.email
        : (p.controller.label || (p.controller.account_number != null ? "#" + p.controller.account_number : ""));
      if (ctrlText) {
        head.appendChild(el("span", { className: "tag ctrl" }, t("tag_controller") + " " + ctrlText));
      }
    }

    if (state.activeProjectDir && p.workspace_path && normPath(p.workspace_path) === normPath(state.activeProjectDir)) {
      head.appendChild(el("span", { className: "tag ok" }, t("tag_current")));
    }
    main.appendChild(head);

    // Row 2: Compact Collaborator Chips
    if (collabs.length > 0) {
      const collabRow = el("div", { className: "project-collabs" });
      for (const c of collabs) {
        collabRow.appendChild(renderCollaboratorChip(c));
      }
      main.appendChild(collabRow);
    }

    // Row 3: Workspace path + Key
    const subParts = [];
    if (p.workspace_path) subParts.push(p.workspace_path);
    subParts.push("Key: " + key);
    main.appendChild(el("div", { className: "project-sub", title: p.workspace_path || key }, subParts.join("  ·  ")));
    row.appendChild(main);

    const actions = el("div", { className: "row-actions" });

    const sessBtn = el("button", { className: "sm", type: "button" }, t("btn_sessions"));
    sessBtn.addEventListener("click", () => openSessionsModal(key, p.display_name || key, collabs));
    actions.appendChild(sessBtn);

    const editBtn = el("button", { className: "ghost sm", type: "button" }, t("btn_edit"));
    editBtn.addEventListener("click", () => openProjectModal({ key, ...p }));
    actions.appendChild(editBtn);

    const delBtn = el("button", { className: "ghost sm danger", type: "button" }, t("btn_delete"));
    delBtn.addEventListener("click", async () => {
      const ok = await confirmAction(
        t("delete_proj_title"),
        (p.display_name || key) + "?",
        t("confirm_label")
      );
      if (!ok) return;
      await withBusy(delBtn, async () => {
        await callTool("mesh_unbind_project", { key });
        await refreshAll(false);
      });
    });
    actions.appendChild(delBtn);

    row.appendChild(actions);
    listBox.appendChild(row);
  }
}

async function loadModels() {
  const res = await callTool("mesh_list_models", {});
  state.savedModels = res.models || {};
}

async function discoverResources(forceRefresh) {
  const rescanBtn = document.getElementById("sub-rescan-btn");
  const codexRescanBtn = document.getElementById("sub-codex-rescan-btn");
  if (rescanBtn) {
    rescanBtn.disabled = true;
    rescanBtn.textContent = t("scanning_btn");
  }
  if (codexRescanBtn) {
    codexRescanBtn.disabled = true;
    codexRescanBtn.textContent = t("scanning_btn");
  }
  try {
    const data = await callTool("mesh_discover_projects", { refresh: !!forceRefresh });
    state.discoveredWorkspaceProjects = data.workspace_projects || [];
    state.discoveredAgProjects = data.antigravity_projects || [];
    state.discoveredCodexProjects = data.codex_projects || [];
    state.discoveredCodexModels = data.codex_models || [];
    state.codexDefaultModel = data.codex_default_model || "";
    state.codexAvailable = !!data.codex_available;
    state.discoveredInstances = data.desktop_instances || [];
    state.agBridgeOnline = !!data.antigravity_bridge_online;
    document.getElementById("codex-pill").classList.toggle("hidden", !state.codexAvailable);
    document.getElementById("ag-pill").classList.toggle("hidden", !state.agBridgeOnline);
    if (data.active_project_dir) state.activeProjectDir = data.active_project_dir;
  } finally {
    if (rescanBtn) {
      rescanBtn.disabled = false;
      rescanBtn.textContent = t("rescan_btn");
    }
    if (codexRescanBtn) {
      codexRescanBtn.disabled = false;
      codexRescanBtn.textContent = t("rescan_btn");
    }
  }
}

async function refreshAll(forceDiscover) {
  const pill = document.getElementById("status-pill");
  const statusText = document.getElementById("status-text");
  const dirLine = document.getElementById("active-dir-line");
  let totalConvs = 0;
  try {
    const health = await callTool("mesh_health", {});
    state.activeProjectDir = health.active_project_dir || "";
    totalConvs = health.tracked_conversations || 0;
    state.agBridgeOnline = !!health.antigravity_bridge_online;
    state.codexAvailable = !!health.codex_available;
    state.pendingInboxCount = health.pending_inbox_count || 0;
    pill.className = "status-pill ok";
    statusText.textContent = t("status_online");
    document.getElementById("codex-pill").classList.toggle("hidden", !state.codexAvailable);
    document.getElementById("ag-pill").classList.toggle("hidden", !state.agBridgeOnline);
    dirLine.textContent = state.activeProjectDir ? state.activeProjectDir : "";
    dirLine.title = state.activeProjectDir || "";
    updateInboxBadge();
  } catch (err) {
    pill.className = "status-pill err";
    statusText.textContent = t("status_offline");
    document.getElementById("summary-line").textContent = String(err && err.message ? err.message : err);
    return;
  }

  await runSafe(async () => {
    const [projRes] = await Promise.all([
      callTool("mesh_list_projects", {}),
      loadModels(),
      discoverResources(!!forceDiscover),
    ]);
    state.savedProjects = projRes.projects || {};
    renderProjectList(totalConvs);
  });
}

async function init() {
  bindModalCallbacks({ refreshAll, discoverResources, loadModels });
  applyDomI18n();
  renderProviderTabs();

  // Topbar & Footer
  const refreshBtn = document.getElementById("refresh-btn");
  refreshBtn.addEventListener("click", () => withBusy(refreshBtn, () => refreshAll(true)));
  document.getElementById("open-project-btn").addEventListener("click", () => openProjectModal(null));
  document.getElementById("open-models-btn").addEventListener("click", openModelsModal);
  document.getElementById("open-inbox-btn").addEventListener("click", openInboxModal);
  document.getElementById("open-all-sessions-btn").addEventListener("click", () =>
    openSessionsModal("", t("all_sessions_title"), [])
  );

  // Project Modal (Main + In-Modal Add-Agent Step)
  document.getElementById("close-project-btn").addEventListener("click", closeProjectModal);
  document.getElementById("cancel-project-btn").addEventListener("click", closeProjectModal);
  document.getElementById("back-to-project-btn").addEventListener("click", showProjectMainStep);
  document.getElementById("cancel-add-agent-btn").addEventListener("click", showProjectMainStep);

  const saveProjBtn = document.getElementById("save-project-btn");
  saveProjBtn.addEventListener("click", () => saveProject(saveProjBtn));
  document.getElementById("copy-ws-btn").addEventListener("click", copySelectedWorkspace);
  document.getElementById("ctrl-select").addEventListener("change", syncControllerState);
  document.getElementById("ws-select").addEventListener("change", onWorkspaceChanged);
  document.getElementById("open-add-agent-btn").addEventListener("click", showAddAgentStep);
  document.getElementById("confirm-add-agent-btn").addEventListener("click", confirmAddAgent);

  document.getElementById("sub-codex-sandbox").addEventListener("change", syncCodexSandboxWarn);
  document.getElementById("sub-codex-rescan-btn").addEventListener("click", () =>
    runSafe(async () => {
      await discoverResources(true);
      populateAddAgentPanel();
    })
  );
  document.getElementById("sub-ag-select").addEventListener("change", (e) => {
    document.getElementById("sub-ag-empty-warn").classList.toggle(
      "hidden",
      !state.agBridgeOnline || !!e.target.value
    );
  });
  document.getElementById("sub-ag-model").addEventListener("change", (e) => {
    document.getElementById("sub-ag-custom-model-wrap").classList.toggle("hidden", e.target.value !== "__custom__");
  });
  document.getElementById("sub-api-select").addEventListener("change", updateApiSelectionSummary);
  document.getElementById("sub-claude-inst").addEventListener("change", updateClaudeInstanceModeNote);
  document.getElementById("sub-rescan-btn").addEventListener("click", () =>
    runSafe(async () => {
      await discoverResources(true);
      populateAddAgentPanel();
    })
  );
  document.getElementById("project-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "project-backdrop") closeProjectModal();
  });

  // Sessions Modal
  document.getElementById("close-sessions-btn").addEventListener("click", closeSessionsModal);
  document.getElementById("back-to-list-btn").addEventListener("click", showSessionsListView);
  document.getElementById("apply-filter-btn").addEventListener("click", () => loadSessionsPage(1));
  document.getElementById("filter-q").addEventListener("keydown", (e) => {
    if (e.key === "Enter") loadSessionsPage(1);
  });
  document.getElementById("filter-date").addEventListener("change", () => loadSessionsPage(1));
  document.getElementById("filter-collab").addEventListener("change", () => loadSessionsPage(1));
  document.getElementById("clear-date-btn").addEventListener("click", () => {
    document.getElementById("filter-date").value = "";
    loadSessionsPage(1);
  });
  document.getElementById("prev-page-btn").addEventListener("click", () => loadSessionsPage(state.sessionsPage - 1));
  document.getElementById("next-page-btn").addEventListener("click", () => loadSessionsPage(state.sessionsPage + 1));

  const cancelTaskBtn = document.getElementById("cancel-task-btn");
  cancelTaskBtn.addEventListener("click", () => cancelCurrentTask(cancelTaskBtn));

  document.getElementById("copy-reply-btn").addEventListener("click", () =>
    runSafe(async () => {
      if (!state.currentDetailReply) return;
      await rpc("ui/copy", { text: state.currentDetailReply });
      await notify(t("copied_reply"), "info");
    })
  );
  document.getElementById("sessions-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "sessions-backdrop") closeSessionsModal();
  });

  // Models Modal
  document.getElementById("close-models-btn").addEventListener("click", closeModelsModal);
  document.getElementById("cancel-models-btn").addEventListener("click", closeModelsModal);
  document.getElementById("new-model-btn").addEventListener("click", resetModelForm);
  const saveModelBtn = document.getElementById("save-model-btn");
  saveModelBtn.addEventListener("click", () => saveModel(saveModelBtn));
  document.getElementById("models-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "models-backdrop") closeModelsModal();
  });

  // Inbox Modal
  document.getElementById("close-inbox-btn").addEventListener("click", closeInboxModal);
  document.getElementById("close-inbox-footer-btn").addEventListener("click", closeInboxModal);
  const refreshInboxBtn = document.getElementById("refresh-inbox-btn");
  refreshInboxBtn.addEventListener("click", () => withBusy(refreshInboxBtn, loadInboxList));
  document.getElementById("inbox-backdrop").addEventListener("click", (e) => {
    if (e.target.id === "inbox-backdrop") closeInboxModal();
  });

  // Escape key closes topmost modal or steps back inside Project Modal
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (!document.getElementById("project-backdrop").classList.contains("hidden")) {
      if (state.projectStep === "add_agent") showProjectMainStep();
      else closeProjectModal();
    } else if (!document.getElementById("sessions-backdrop").classList.contains("hidden")) {
      closeSessionsModal();
    } else if (!document.getElementById("models-backdrop").classList.contains("hidden")) {
      closeModelsModal();
    } else if (!document.getElementById("inbox-backdrop").classList.contains("hidden")) {
      closeInboxModal();
    }
  });

  try {
    const info = await rpc("ui/initialize", { protocolVersion: 1 });
    if (info && info.locale) {
      setLocale(info.locale);
      renderProviderTabs();
    }
    if (info && info.theme) {
      applyTheme(info.theme);
    }
  } catch (_err) {
    // standalone preview
  }

  await refreshAll(false);
}

init();
