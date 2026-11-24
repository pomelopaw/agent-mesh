(function () {
  "use strict";

  const THINKING_LABELS = {
    "": "默认",
    minimal: "极低",
    low: "低",
    medium: "中",
    high: "高",
    xhigh: "最高",
    max: "最高",
  };

  let rpcId = 1;
  const pending = new Map();

  let discoveredWorkspaceProjects = [];
  let discoveredAgProjects = [];
  let discoveredCodexProjects = [];
  let discoveredCodexModels = [];
  let codexDefaultModel = "";
  let codexAvailable = false;
  let discoveredInstances = [];
  let agBridgeOnline = false;
  let savedProjects = {};
  let savedModels = {};
  let activeProjectDir = "";

  // Draft collaborators while editing/creating a project
  let draftCollaborators = [];
  let addAgentType = "openai_compat";

  // Sessions Modal State
  let sessionsProjectKey = "";
  let sessionsProjectTitle = "全部历史会话";
  let sessionsPage = 1;
  let sessionsTotalPages = 1;
  const sessionsPageSize = 10;
  let currentDetailReply = "";

  function rpc(method, params) {
    return new Promise((resolve, reject) => {
      const id = rpcId++;
      pending.set(id, { resolve, reject });
      window.parent.postMessage({ jsonrpc: "2.0", id, method, params: params || {} }, "*");
    });
  }

  window.addEventListener("message", (event) => {
    const msg = event.data;
    if (event.source !== window.parent || !msg || typeof msg !== "object") return;
    if (msg.method === "ui/themeChanged" && msg.params && msg.params.tokens) {
      applyTheme(msg.params.tokens);
      return;
    }
    if (msg.id && pending.has(msg.id)) {
      const entry = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) entry.reject(new Error(msg.error.message || "RPC error"));
      else entry.resolve(msg.result);
    }
  });

  function applyTheme(tokens) {
    if (!tokens) return;
    const root = document.documentElement;
    for (const [k, v] of Object.entries(tokens)) {
      root.style.setProperty("--" + k, v);
    }
  }

  async function callTool(name, args) {
    const res = await rpc("tools/call", { name, arguments: args || {} });
    const text = (res && res.content && res.content[0] && res.content[0].text) || "{}";
    if (res && res.isError) throw new Error(text);
    return JSON.parse(text);
  }

  function el(tag, attrs, text) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const [k, v] of Object.entries(attrs)) {
        if (k === "className") node.className = v;
        else node.setAttribute(k, v);
      }
    }
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function normPath(p) {
    return String(p || "").trim().replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
  }

  function slugKey(name) {
    return String(name || "").trim().replace(/\s+/g, "-").toLowerCase();
  }

  function formatTime(iso) {
    if (!iso) return "";
    try {
      const d = new Date(iso);
      const pad = (n) => String(n).padStart(2, "0");
      return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + pad(d.getHours()) + ":" + pad(d.getMinutes());
    } catch (_e) {
      return String(iso).slice(0, 16);
    }
  }

  async function confirmAction(title, message) {
    try {
      const res = await rpc("ui/confirm", { title, message, confirmLabel: "确定", danger: true });
      return !!(res && (res.confirmed || res === true));
    } catch (_err) {
      return true;
    }
  }

  function buildWorkspaceItems() {
    const wsItems = [...discoveredWorkspaceProjects];
    if (activeProjectDir && !wsItems.some((x) => normPath(x.path) === normPath(activeProjectDir))) {
      const parts = activeProjectDir.replace(/\\/g, "/").split("/").filter(Boolean);
      wsItems.unshift({ name: parts[parts.length - 1] || activeProjectDir, path: activeProjectDir });
    }
    return wsItems;
  }

  // ---- Main Project Rows ----
  function renderCollaboratorTag(c) {
    if (c.type === "antigravity") {
      const ag = discoveredAgProjects.find((a) => a.project_id === c.project_id);
      const label = ag ? ag.name : (c.label || (c.project_id || "").slice(0, 8));
      const parts = ["Antigravity · " + label];
      if (c.model) parts.push(c.model);
      if (c.thinking_level) parts.push("思考:" + (THINKING_LABELS[c.thinking_level] || c.thinking_level));
      return el("span", { className: "tag ag" }, parts.join(" · "));
    }
    if (c.type === "openai_compat") {
      const m = c.model_ref ? savedModels[c.model_ref] : null;
      const name = (m && m.name) || c.label || c.model_id || "API";
      const thVal = (m && m.thinking_level) || c.thinking_level || "";
      const th = thVal ? " · 思考:" + (THINKING_LABELS[thVal] || thVal) : "";
      return el("span", { className: "tag api" }, "API · " + name + th);
    }
    if (c.type === "codex") {
      const parts = ["Codex · " + (c.label || "Codex")];
      if (c.model) parts.push(c.model);
      if (c.thinking_level) parts.push("思考:" + (THINKING_LABELS[c.thinking_level] || c.thinking_level));
      return el("span", { className: "tag codex" }, parts.join(" · "));
    }
    if (c.type === "claude_instance") {
      const parts = ["Claude " + (c.label || c.email)];
      if (c.model) parts.push(c.model);
      if (c.thinking_level) parts.push("思考:" + (THINKING_LABELS[c.thinking_level] || c.thinking_level));
      return el("span", { className: "tag claude" }, parts.join(" · "));
    }
    return el("span", { className: "tag" }, c.label || c.type);
  }

  function renderProjectList(totalConvs) {
    const listBox = document.getElementById("project-list");
    listBox.textContent = "";

    const keys = Object.keys(savedProjects);
    document.getElementById("summary-line").textContent =
      "共 " + keys.length + " 个协作项目 · " + Object.keys(savedModels).length + " 个模型 · " + discoveredInstances.length + " 个桌面实例 · " + (totalConvs || 0) + " 条会话";

    if (keys.length === 0) {
      const empty = el("div", { className: "empty-state" });
      empty.appendChild(el("p", null, "还没有创建协作项目。点击右上角「+ 创建项目」，为主控项目编组多个协作 AI Agent。"));
      const btn = el("button", { className: "primary" }, "+ 创建第一个项目");
      btn.addEventListener("click", () => openProjectModal(null));
      empty.appendChild(btn);
      listBox.appendChild(empty);
      return;
    }

    for (const key of keys) {
      const p = savedProjects[key];
      const collabs = p.collaborators || [];
      const row = el("div", { className: "project-row" });

      const main = el("div", { className: "project-main" });
      const head = el("div", { className: "project-head" });
      head.appendChild(el("span", { className: "project-name" }, p.display_name || key));

      if (p.controller && p.controller.email) {
        const cNum = p.controller.account_number != null ? "#" + p.controller.account_number + " " : "";
        head.appendChild(el("span", { className: "tag ctrl" }, "主控 " + cNum + p.controller.email));
      }

      for (const c of collabs) {
        head.appendChild(renderCollaboratorTag(c));
      }

      if (activeProjectDir && p.host_workspace && normPath(p.host_workspace) === normPath(activeProjectDir)) {
        head.appendChild(el("span", { className: "tag ok" }, "当前"));
      }
      main.appendChild(head);

      const subParts = [];
      if (p.host_workspace) subParts.push(p.host_workspace);
      subParts.push("Key: " + key);
      main.appendChild(el("div", { className: "project-sub" }, subParts.join("  ·  ")));
      row.appendChild(main);

      const actions = el("div", { className: "row-actions" });

      const sessBtn = el("button", { className: "sm" }, "会话");
      sessBtn.addEventListener("click", () => openSessionsModal(key, p.display_name || key, collabs));
      actions.appendChild(sessBtn);

      const editBtn = el("button", { className: "ghost sm" }, "编辑");
      editBtn.addEventListener("click", () => openProjectModal({ key, ...p }));
      actions.appendChild(editBtn);

      const delBtn = el("button", { className: "ghost sm danger" }, "删除");
      delBtn.addEventListener("click", async () => {
        const ok = await confirmAction("删除协作项目", "确定要删除项目「" + (p.display_name || key) + "」吗？（不会删除本地文件）");
        if (!ok) return;
        await callTool("ag_unbind_project", { key });
        await refreshAll(false);
      });
      actions.appendChild(delBtn);

      row.appendChild(actions);
      listBox.appendChild(row);
    }
  }

  // ---- Modal 1: Stepped Project Modal ----
  function renderDraftAgents() {
    const box = document.getElementById("draft-agent-list");
    document.getElementById("agent-count").textContent = String(draftCollaborators.length);
    box.textContent = "";

    if (draftCollaborators.length === 0) {
      box.appendChild(el("div", { className: "hint-box" }, "尚未添加协作 AI Agent，点击下方「+ 添加 AI Agent 协作」逐个添加。"));
      return;
    }

    draftCollaborators.forEach((c, idx) => {
      const item = el("div", { className: "agent-item" });
      const main = el("div", { className: "agent-item-main" });
      const head = el("div", { className: "agent-item-head" });

      if (c.type === "antigravity") {
        head.appendChild(el("span", { className: "tag ag" }, "Antigravity"));
        head.appendChild(el("span", null, c.label || (c.project_id || "").slice(0, 8)));
        if (c.model) head.appendChild(el("span", { className: "tag" }, c.model));
        if (c.thinking_level) head.appendChild(el("span", { className: "tag" }, "思考:" + (THINKING_LABELS[c.thinking_level] || c.thinking_level)));
        main.appendChild(head);
        const ag = discoveredAgProjects.find((a) => a.project_id === c.project_id);
        main.appendChild(el("div", { className: "agent-item-sub" }, (ag && ag.workspace) || ("Project ID: " + c.project_id)));
      } else if (c.type === "openai_compat") {
        const m = c.model_ref ? savedModels[c.model_ref] : null;
        head.appendChild(el("span", { className: "tag api" }, "API 模型"));
        head.appendChild(el("span", null, (m && m.name) || c.label || c.model_ref));
        if (m && m.model_id) head.appendChild(el("span", { className: "tag" }, m.model_id));
        if (m && m.thinking_level) head.appendChild(el("span", { className: "tag" }, "思考:" + (THINKING_LABELS[m.thinking_level] || m.thinking_level)));
        main.appendChild(head);
        if (m && m.api_base) main.appendChild(el("div", { className: "agent-item-sub" }, m.api_base));
      } else if (c.type === "codex") {
        head.appendChild(el("span", { className: "tag codex" }, "Codex"));
        head.appendChild(el("span", null, c.label || "Codex"));
        if (c.model) head.appendChild(el("span", { className: "tag" }, c.model));
        if (c.thinking_level) head.appendChild(el("span", { className: "tag" }, "思考:" + (THINKING_LABELS[c.thinking_level] || c.thinking_level)));
        if (c.sandbox_mode === "read-only") head.appendChild(el("span", { className: "tag" }, "只读"));
        main.appendChild(head);
        if (c.workspace_path) main.appendChild(el("div", { className: "agent-item-sub" }, "工作目录：" + c.workspace_path));
      } else if (c.type === "claude_instance") {
        head.appendChild(el("span", { className: "tag claude" }, "Claude 实例"));
        head.appendChild(el("span", null, c.label || c.email));
        if (c.model) head.appendChild(el("span", { className: "tag" }, c.model));
        if (c.thinking_level) head.appendChild(el("span", { className: "tag" }, "思考:" + (THINKING_LABELS[c.thinking_level] || c.thinking_level)));
        main.appendChild(head);
        const sub = c.workspace_path ? "项目：" + c.workspace_path : "无 Code 权限 · 纯对话接力模式";
        main.appendChild(el("div", { className: "agent-item-sub" }, sub));
      }

      item.appendChild(main);

      const rmBtn = el("button", { className: "ghost sm danger", type: "button" }, "移除");
      rmBtn.addEventListener("click", () => {
        draftCollaborators.splice(idx, 1);
        renderDraftAgents();
      });
      item.appendChild(rmBtn);
      box.appendChild(item);
    });
  }

  function syncWsCustomVisibility() {
    const wsSel = document.getElementById("ws-select");
    document.getElementById("ws-custom-wrap").classList.toggle("hidden", wsSel.value !== "__manual__");
  }

  function formatInstanceLabel(inst) {
    const plan = inst.plan_label || (inst.has_code_access ? "Pro" : "Free");
    const cap = inst.has_code_access ? plan + " · 支持 Code 项目" : plan + " · 无 Code 权限（仅对话）";
    return "#" + inst.number + "  " + inst.email + " (" + cap + ")";
  }

  function syncControllerState() {
    const ctrlId = document.getElementById("ctrl-select").value;
    const inst = discoveredInstances.find((x) => x.id === ctrlId);
    const hasCode = !inst || inst.has_code_access !== false;
    document.getElementById("ctrl-nocode-warn").classList.toggle("hidden", hasCode);
    document.getElementById("ctrl-ws-field").classList.toggle("hidden", !hasCode);
  }

  function populateProjectModal(editingItem) {
    const ctrlSel = document.getElementById("ctrl-select");
    ctrlSel.textContent = "";
    if (discoveredInstances.length === 0) {
      ctrlSel.appendChild(el("option", { value: "" }, "默认主控工作台"));
    } else {
      for (const inst of discoveredInstances) {
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
    wsSel.appendChild(el("option", { value: "__manual__" }, "+ 手动输入项目路径..."));

    if (editingItem) {
      document.getElementById("project-modal-title").textContent = "编辑协作项目";
      if (editingItem.controller && editingItem.controller.account_id) {
        ctrlSel.value = editingItem.controller.account_id;
      }
      const wsMatch = wsItems.find((w) => normPath(w.path) === normPath(editingItem.host_workspace));
      if (wsMatch) {
        wsSel.value = wsMatch.path;
        document.getElementById("proj-ws").value = "";
      } else {
        wsSel.value = "__manual__";
        document.getElementById("proj-ws").value = editingItem.host_workspace || "";
      }
      document.getElementById("proj-name").value = editingItem.display_name || editingItem.key || "";
      document.getElementById("proj-key").value = editingItem.key || "";
      draftCollaborators = (editingItem.collaborators || []).map((c) => ({ ...c }));
    } else {
      document.getElementById("project-modal-title").textContent = "创建协作项目";
      document.getElementById("proj-ws").value = "";
      const firstCodeInst = discoveredInstances.find((x) => x.has_code_access);
      if (firstCodeInst) ctrlSel.value = firstCodeInst.id;
      const boundWs = new Set(Object.values(savedProjects).map((p) => normPath(p.host_workspace)));
      const firstUnbound = wsItems.find((w) => !boundWs.has(normPath(w.path)));
      if (firstUnbound) wsSel.value = firstUnbound.path;
      onWorkspaceChanged();
      draftCollaborators = [];
    }

    syncControllerState();
    syncWsCustomVisibility();
    renderDraftAgents();
  }

  function onWorkspaceChanged() {
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

  async function openProjectModal(editingItem) {
    if (discoveredWorkspaceProjects.length === 0 && discoveredAgProjects.length === 0) {
      await discoverResources(false);
    }
    populateProjectModal(editingItem);
    document.getElementById("project-backdrop").classList.remove("hidden");
  }

  function closeProjectModal() {
    document.getElementById("project-backdrop").classList.add("hidden");
  }

  async function saveProject() {
    const ctrlId = document.getElementById("ctrl-select").value;
    const ctrlInst = discoveredInstances.find((x) => x.id === ctrlId) || null;
    const controller = ctrlInst
      ? { account_id: ctrlInst.id, account_number: ctrlInst.number, email: ctrlInst.email }
      : null;

    const wsSel = document.getElementById("ws-select");
    const ctrlHasCode = !ctrlInst || ctrlInst.has_code_access !== false;
    const workspace_path = !ctrlHasCode
      ? ""
      : wsSel.value === "__manual__"
        ? document.getElementById("proj-ws").value.trim()
        : wsSel.value;
    const display_name = document.getElementById("proj-name").value.trim();
    const key = document.getElementById("proj-key").value.trim() || slugKey(display_name);

    if (!key) {
      await rpc("ui/notify", { message: "请填写项目 Key", tone: "error" });
      return;
    }
    if (draftCollaborators.length === 0) {
      await rpc("ui/notify", { message: "请至少添加 1 个协作 AI Agent", tone: "error" });
      return;
    }

    await callTool("ag_save_project", {
      key,
      display_name: display_name || key,
      workspace_path,
      controller,
      collaborators: draftCollaborators,
    });
    closeProjectModal();
    await rpc("ui/notify", { message: "已保存项目：" + (display_name || key) + "（" + draftCollaborators.length + " 个协作者）", tone: "info" });
    await refreshAll(false);
  }

  // ---- Sub-Modal: Add One AI Agent Collaborator ----
  function setAddAgentType(type) {
    addAgentType = type;
    document.getElementById("agent-tab-api").classList.toggle("active", type === "openai_compat");
    document.getElementById("agent-tab-codex").classList.toggle("active", type === "codex");
    document.getElementById("agent-tab-claude").classList.toggle("active", type === "claude_instance");
    document.getElementById("agent-tab-ag").classList.toggle("active", type === "antigravity");
    document.getElementById("sub-api-panel").classList.toggle("hidden", type !== "openai_compat");
    document.getElementById("sub-codex-panel").classList.toggle("hidden", type !== "codex");
    document.getElementById("sub-claude-panel").classList.toggle("hidden", type !== "claude_instance");
    document.getElementById("sub-ag-panel").classList.toggle("hidden", type !== "antigravity");
    populateAddAgentPanel();
  }

  function currentPrimaryWorkspace() {
    const wsSel = document.getElementById("ws-select");
    return wsSel.value === "__manual__" ? document.getElementById("proj-ws").value.trim() : wsSel.value;
  }

  function populateAddAgentPanel() {
    const primaryWs = currentPrimaryWorkspace();
    const primaryName = document.getElementById("proj-name").value.trim();

    if (addAgentType === "antigravity") {
      const sel = document.getElementById("sub-ag-select");
      const offlineWarn = document.getElementById("sub-ag-offline-warn");
      const emptyWarn = document.getElementById("sub-ag-empty-warn");
      sel.textContent = "";
      if (!agBridgeOnline) {
        sel.appendChild(el("option", { value: "" }, "（未检测到 Antigravity 桥接服务）"));
        offlineWarn.classList.remove("hidden");
        emptyWarn.classList.add("hidden");
      } else if (discoveredAgProjects.length === 0) {
        sel.appendChild(el("option", { value: "" }, "（未扫描到 Antigravity 项目）"));
        offlineWarn.classList.add("hidden");
        emptyWarn.classList.remove("hidden");
      } else {
        offlineWarn.classList.add("hidden");
        for (const ag of discoveredAgProjects) {
          const extra = ag.workspace ? " | " + ag.workspace : ag.sample_title ? " | " + ag.sample_title : "";
          sel.appendChild(el("option", { value: ag.project_id }, ag.name + extra));
        }
        sel.appendChild(el("option", { value: "__none__" }, "列表里没有我的项目（需先在 Antigravity 创建）"));
        const match =
          discoveredAgProjects.find((a) => a.workspace && normPath(a.workspace) === normPath(primaryWs)) ||
          discoveredAgProjects.find((a) => a.name && primaryName && a.name.toLowerCase() === primaryName.toLowerCase());
        if (match) sel.value = match.project_id;
        emptyWarn.classList.toggle("hidden", sel.value !== "__none__" && sel.value !== "");
      }
      document.getElementById("sub-ag-model").value = "";
      document.getElementById("sub-ag-thinking").value = "";
      document.getElementById("sub-ag-custom-model").value = "";
      document.getElementById("sub-ag-custom-model-wrap").classList.add("hidden");
    } else if (addAgentType === "openai_compat") {
      const sel = document.getElementById("sub-api-select");
      const warn = document.getElementById("sub-api-empty-warn");
      const summary = document.getElementById("sub-api-summary");
      sel.textContent = "";
      const keys = Object.keys(savedModels);
      if (keys.length === 0) {
        sel.appendChild(el("option", { value: "" }, "（请先在右上角「模型设置」中添加模型）"));
        warn.classList.remove("hidden");
        summary.classList.add("hidden");
      } else {
        warn.classList.add("hidden");
        for (const k of keys) {
          const m = savedModels[k];
          const th = m.thinking_level ? " · 思考:" + (THINKING_LABELS[m.thinking_level] || m.thinking_level) : "";
          sel.appendChild(el("option", { value: k }, (m.name || k) + " (" + m.model_id + th + ")"));
        }
        updateApiSelectionSummary();
      }
    } else if (addAgentType === "codex") {
      const offlineWarn = document.getElementById("sub-codex-offline-warn");
      const fieldsWrap = document.getElementById("sub-codex-fields");
      offlineWarn.classList.toggle("hidden", codexAvailable);
      fieldsWrap.classList.toggle("hidden", !codexAvailable);
      if (codexAvailable) {
        const projSel = document.getElementById("sub-codex-proj");
        projSel.textContent = "";
        for (const cp of discoveredCodexProjects) {
          const extra = cp.workspace ? " (" + cp.workspace + ")" : "";
          projSel.appendChild(el("option", { value: cp.workspace || cp.project_id }, cp.name + extra));
        }
        if (primaryWs && !discoveredCodexProjects.some((cp) => normPath(cp.workspace) === normPath(primaryWs))) {
          projSel.appendChild(el("option", { value: primaryWs }, "使用主控项目目录 (" + primaryWs + ")"));
        }
        if (projSel.options.length === 0) {
          projSel.appendChild(el("option", { value: primaryWs || "" }, primaryWs ? "使用主控项目目录 (" + primaryWs + ")" : "跟随当前工作目录"));
        }
        const match = discoveredCodexProjects.find((cp) => cp.workspace && normPath(cp.workspace) === normPath(primaryWs));
        if (match) projSel.value = match.workspace;
        else if (primaryWs) projSel.value = primaryWs;

        const mSel = document.getElementById("sub-codex-model");
        mSel.textContent = "";
        const defLabel = codexDefaultModel ? "跟随 Codex 默认 (" + codexDefaultModel + ")" : "跟随 Codex 默认";
        mSel.appendChild(el("option", { value: "" }, defLabel));
        for (const slug of discoveredCodexModels) {
          mSel.appendChild(el("option", { value: slug }, slug));
        }
        document.getElementById("sub-codex-thinking").value = "";
        document.getElementById("sub-codex-sandbox").value = "workspace-write";
      }
    } else if (addAgentType === "claude_instance") {
      const ctrlId = document.getElementById("ctrl-select").value;
      const others = discoveredInstances.filter((x) => x.id !== ctrlId);
      const instSel = document.getElementById("sub-claude-inst");
      const warn = document.getElementById("sub-claude-empty-warn");
      const fieldsWrap = document.getElementById("sub-claude-fields");
      instSel.textContent = "";

      if (others.length === 0) {
        instSel.appendChild(el("option", { value: "" }, "（没有其他 Claude Desktop 实例）"));
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

  function updateApiSelectionSummary() {
    const key = document.getElementById("sub-api-select").value;
    const summary = document.getElementById("sub-api-summary");
    const m = savedModels[key];
    if (!m) {
      summary.classList.add("hidden");
      return;
    }
    summary.classList.remove("hidden");
    const th = THINKING_LABELS[m.thinking_level || ""] || m.thinking_level || "默认";
    summary.textContent = "已配置接口：" + m.api_base + " · 模型 ID：" + m.model_id + " · 思考等级：" + th + "（直接点「添加到项目」即可）";
  }

  function updateClaudeInstanceModeNote() {
    const aid = document.getElementById("sub-claude-inst").value;
    const inst = discoveredInstances.find((x) => x.id === aid);
    const note = document.getElementById("sub-claude-mode-note");
    const projField = document.getElementById("sub-claude-proj-field");
    if (!inst) {
      note.textContent = "";
      projField.classList.add("hidden");
      return;
    }
    const hasCode = inst.has_code_access !== false;
    projField.classList.toggle("hidden", !hasCode);
    note.textContent = hasCode
      ? "该账号为 " + (inst.plan_label || "Pro") + "，支持 Code 项目，主控派发任务后将全自动无鼠标后台执行并回传结果。"
      : "该账号为 Free（无 Claude Code 权限），不挂载本地代码项目；将作为纯对话协作实例加入，邀请后进入协作信箱在对应桌面窗口接单回复。";
  }

  function openAddAgentModal() {
    let defaultTab = "openai_compat";
    if (Object.keys(savedModels).length > 0) defaultTab = "openai_compat";
    else if (codexAvailable) defaultTab = "codex";
    else if (agBridgeOnline && discoveredAgProjects.length > 0) defaultTab = "antigravity";
    setAddAgentType(defaultTab);
    document.getElementById("add-agent-backdrop").classList.remove("hidden");
  }

  function closeAddAgentModal() {
    document.getElementById("add-agent-backdrop").classList.add("hidden");
  }

  async function confirmAddAgent() {
    if (addAgentType === "antigravity") {
      const pid = document.getElementById("sub-ag-select").value;
      if (!pid || pid === "__none__") {
        await rpc("ui/notify", { message: agBridgeOnline ? "请先在 Antigravity 中创建对应项目并点「重新扫描」" : "未检测到 Antigravity 桥接服务", tone: "error" });
        return;
      }
      const ag = discoveredAgProjects.find((a) => a.project_id === pid);
      let model = document.getElementById("sub-ag-model").value;
      if (model === "__custom__") {
        model = document.getElementById("sub-ag-custom-model").value.trim();
      }
      const thinking = document.getElementById("sub-ag-thinking").value;
      const existsIdx = draftCollaborators.findIndex((c) => c.type === "antigravity" && c.project_id === pid);
      const entry = {
        id: "antigravity:" + pid.slice(0, 8),
        type: "antigravity",
        label: (ag && ag.name) || "Antigravity",
        project_id: pid,
        model,
        thinking_level: thinking,
      };
      if (existsIdx >= 0) draftCollaborators[existsIdx] = entry;
      else draftCollaborators.push(entry);
    } else if (addAgentType === "openai_compat") {
      const mref = document.getElementById("sub-api-select").value;
      if (!mref || !savedModels[mref]) {
        await rpc("ui/notify", { message: "请先在右上角「模型设置」中添加模型", tone: "error" });
        return;
      }
      const m = savedModels[mref];
      const entry = {
        id: "model:" + mref,
        type: "openai_compat",
        model_ref: mref,
        label: m.name || mref,
        model_id: m.model_id,
        thinking_level: m.thinking_level || "",
      };
      const existsIdx = draftCollaborators.findIndex((c) => c.type === "openai_compat" && c.model_ref === mref);
      if (existsIdx >= 0) draftCollaborators[existsIdx] = entry;
      else draftCollaborators.push(entry);
    } else if (addAgentType === "codex") {
      if (!codexAvailable) {
        await rpc("ui/notify", { message: "未检测到本机 Codex CLI / Codex Desktop", tone: "error" });
        return;
      }
      const wsPath = document.getElementById("sub-codex-proj").value || currentPrimaryWorkspace();
      const matchedCp = discoveredCodexProjects.find((cp) => cp.workspace === wsPath || cp.project_id === wsPath);
      const label = (matchedCp && matchedCp.name) || (wsPath ? wsPath.replace(/\\/g, "/").split("/").filter(Boolean).pop() : "Codex");
      const model = document.getElementById("sub-codex-model").value;
      const thinking = document.getElementById("sub-codex-thinking").value;
      const sandboxMode = document.getElementById("sub-codex-sandbox").value || "workspace-write";
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
      const existsIdx = draftCollaborators.findIndex((c) => c.type === "codex" && normPath(c.workspace_path) === normPath(wsPath));
      if (existsIdx >= 0) draftCollaborators[existsIdx] = entry;
      else draftCollaborators.push(entry);
    } else if (addAgentType === "claude_instance") {
      const aid = document.getElementById("sub-claude-inst").value;
      const inst = discoveredInstances.find((x) => x.id === aid);
      if (!inst) {
        await rpc("ui/notify", { message: "没有可添加的其他 Claude Desktop 实例", tone: "error" });
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
        email: inst.email,
        cli_ready: inst.cli_ready,
        has_code_access: inst.has_code_access,
        workspace_path: wsPath,
        model,
        thinking_level: thinking,
        label: "#" + inst.number + " " + inst.email,
      };
      const existsIdx = draftCollaborators.findIndex((c) => c.type === "claude_instance" && c.account_id === inst.id);
      if (existsIdx >= 0) draftCollaborators[existsIdx] = entry;
      else draftCollaborators.push(entry);
    }

    closeAddAgentModal();
    renderDraftAgents();
  }

  // ---- Modal 2: Sessions Modal (Popup + Date Filter + 10/page + Full Detail) ----
  function showSessionsListView() {
    document.getElementById("sessions-list-view").classList.remove("hidden");
    document.getElementById("sessions-detail-view").classList.add("hidden");
    document.getElementById("back-to-list-btn").classList.add("hidden");
    document.getElementById("sessions-footer").classList.remove("hidden");
    document.getElementById("sessions-modal-title").textContent = sessionsProjectTitle + " · 会话记录";
  }

  function showSessionsDetailView(title) {
    document.getElementById("sessions-list-view").classList.add("hidden");
    document.getElementById("sessions-detail-view").classList.remove("hidden");
    document.getElementById("back-to-list-btn").classList.remove("hidden");
    document.getElementById("sessions-footer").classList.add("hidden");
    document.getElementById("sessions-modal-title").textContent = title || "会话详情";
  }

  async function openSessionsModal(projectKey, title, collabs) {
    sessionsProjectKey = projectKey || "";
    sessionsProjectTitle = title || "全部历史会话";
    sessionsPage = 1;
    document.getElementById("filter-date").value = "";
    document.getElementById("filter-q").value = "";

    const colSel = document.getElementById("filter-collab");
    colSel.textContent = "";
    colSel.appendChild(el("option", { value: "" }, "全部协作 AI"));
    if (collabs && collabs.length > 0) {
      for (const c of collabs) {
        colSel.appendChild(el("option", { value: c.id }, c.label || c.id));
      }
    } else {
      colSel.appendChild(el("option", { value: "openai_compat" }, "API 模型"));
      colSel.appendChild(el("option", { value: "codex" }, "Codex"));
      colSel.appendChild(el("option", { value: "claude_instance" }, "Claude 实例"));
      colSel.appendChild(el("option", { value: "antigravity" }, "Antigravity"));
    }

    showSessionsListView();
    document.getElementById("sessions-backdrop").classList.remove("hidden");
    await loadSessionsPage(1);
  }

  function closeSessionsModal() {
    document.getElementById("sessions-backdrop").classList.add("hidden");
  }

  async function loadSessionsPage(page) {
    sessionsPage = Math.max(1, page);
    const box = document.getElementById("sessions-list-box");
    box.textContent = "";
    box.appendChild(el("div", { className: "project-sub" }, "正在加载会话列表..."));

    const args = { page: sessionsPage, page_size: sessionsPageSize };
    if (sessionsProjectKey) args.project = sessionsProjectKey;
    const dateVal = document.getElementById("filter-date").value.trim();
    if (dateVal) args.date = dateVal;
    const collabVal = document.getElementById("filter-collab").value.trim();
    if (collabVal) args.collaborator = collabVal;
    const qVal = document.getElementById("filter-q").value.trim();
    if (qVal) args.q = qVal;

    const data = await callTool("ag_list_conversations", args);
    const items = data.items || [];
    const total = data.total || 0;
    sessionsPage = data.page || 1;
    sessionsTotalPages = data.total_pages || 1;

    document.getElementById("page-info").textContent =
      "共 " + total + " 条 · 第 " + sessionsPage + " / " + sessionsTotalPages + " 页（每页 " + sessionsPageSize + " 条）";
    document.getElementById("prev-page-btn").disabled = sessionsPage <= 1;
    document.getElementById("next-page-btn").disabled = sessionsPage >= sessionsTotalPages;

    box.textContent = "";
    if (items.length === 0) {
      box.appendChild(el("div", { className: "empty-state" }, "当前筛选条件下没有会话记录。"));
      return;
    }

    for (const c of items) {
      const row = el("div", { className: "session-row" });
      const top = el("div", { className: "session-top" });
      top.appendChild(el("span", { className: "session-title" }, c.title || (c.conversation_id || "").slice(0, 8)));

      const st = c.effective_status || c.status || "unknown";
      const stCls = st === "completed" ? "ok" : st === "error" ? "err" : "warn";
      const stMap = { completed: "已完成", running: "运行中", waiting_approval: "等待审批", waiting_relay: "待实例接力", error: "出错" };
      top.appendChild(el("span", { className: "tag " + stCls }, stMap[st] || st));
      row.appendChild(top);

      const meta = el("div", { className: "session-meta" });
      const prov = c.provider || "antigravity";
      const pCls = prov === "openai_compat" ? "api" : prov === "codex" ? "codex" : prov === "claude_instance" ? "claude" : "ag";
      meta.appendChild(el("span", { className: "tag " + pCls }, c.collaborator_label || c.model || prov));
      if (c.updated_at || c.created_at) meta.appendChild(el("span", null, formatTime(c.updated_at || c.created_at)));
      meta.appendChild(el("span", null, "ID: " + (c.conversation_id || "").slice(0, 8)));
      row.appendChild(meta);

      const snippet = c.latest_reply_preview || c.last_prompt || "";
      if (snippet) row.appendChild(el("div", { className: "session-snippet" }, snippet));

      row.addEventListener("click", () => openSessionDetail(c.conversation_id, c.title || "会话详情"));
      box.appendChild(row);
    }
  }

  async function openSessionDetail(convId, title) {
    showSessionsDetailView(title);
    const turnsBox = document.getElementById("detail-turns-box");
    const metaEl = document.getElementById("detail-meta");
    turnsBox.textContent = "";
    turnsBox.appendChild(el("div", { className: "project-sub" }, "正在加载完整对话..."));
    currentDetailReply = "";

    const data = await callTool("ag_get_history", { conversation_id: convId, last_n: 50 });
    const conv = data.conversation || {};
    const turns = data.turns || [];
    currentDetailReply = data.latest_reply || "";

    metaEl.textContent =
      (conv.collaborator_label || conv.model || conv.provider || "") +
      " · 状态：" + (data.status || "unknown") +
      " · 共 " + turns.length + " 条消息";

    turnsBox.textContent = "";
    if (turns.length === 0) {
      turnsBox.appendChild(el("div", { className: "empty-state" }, "暂无对话内容。"));
      return;
    }

    for (const t of turns) {
      const isUser = t.role === "user";
      const card = el("div", { className: "turn-card " + (isUser ? "user" : "assistant") });
      const head = el("div", { className: "turn-head" });
      head.appendChild(el("strong", null, isUser ? "主控提问" : (conv.collaborator_label || "协作 AI 回复")));
      head.appendChild(el("span", null, formatTime(t.created_at)));
      card.appendChild(head);

      if (t.reasoning_content) {
        const det = el("details", { className: "thinking-box" });
        det.appendChild(el("summary", null, "查看思考过程"));
        det.appendChild(el("pre", { className: "turn-body", style: "margin-top:6px;" }, t.reasoning_content));
        card.appendChild(det);
      }

      card.appendChild(el("pre", { className: "turn-body" }, t.content || ""));
      turnsBox.appendChild(card);
    }
  }

  // ---- Modal 3: Standalone Model Settings ----
  function resetModelForm() {
    document.getElementById("model-form-title").textContent = "添加新模型";
    document.getElementById("m-key").value = "";
    document.getElementById("m-name").value = "";
    document.getElementById("m-id").value = "";
    document.getElementById("m-base").value = "";
    document.getElementById("m-thinking").value = "";
    document.getElementById("m-apikey").value = "";
    document.getElementById("m-apikey").placeholder = "sk-...（仅存本机）";
    document.getElementById("m-system").value = "";
  }

  function fillModelForm(key, m) {
    document.getElementById("model-form-title").textContent = "编辑模型：" + (m.name || key);
    document.getElementById("m-key").value = key;
    document.getElementById("m-name").value = m.name || "";
    document.getElementById("m-id").value = m.model_id || "";
    document.getElementById("m-base").value = m.api_base || "";
    document.getElementById("m-thinking").value = m.thinking_level || "";
    document.getElementById("m-apikey").value = "";
    document.getElementById("m-apikey").placeholder = m.has_api_key
      ? "已配置 (" + (m.api_key_masked || "****") + ")，留空保持不变"
      : "sk-...（仅存本机）";
    document.getElementById("m-system").value = m.system_prompt || "";
  }

  function renderSavedModelsList() {
    const box = document.getElementById("saved-models-list");
    box.textContent = "";
    const keys = Object.keys(savedModels);
    if (keys.length === 0) {
      box.appendChild(el("div", { className: "project-sub" }, "暂未添加模型，请在下方直接填写添加。"));
      return;
    }
    for (const key of keys) {
      const m = savedModels[key];
      const item = el("div", { className: "model-item" });
      const main = el("div", { className: "model-item-main" });
      const head = el("div", { className: "project-head" });
      head.appendChild(el("strong", null, m.name || key));
      head.appendChild(el("span", { className: "tag api" }, m.model_id));
      head.appendChild(el("span", { className: "tag" }, "思考: " + (THINKING_LABELS[m.thinking_level || ""] || m.thinking_level)));
      if (m.has_api_key) head.appendChild(el("span", { className: "tag ok" }, "Key 已配"));
      main.appendChild(head);
      main.appendChild(el("div", { className: "project-sub" }, m.api_base));
      item.appendChild(main);

      const acts = el("div", { className: "row-actions" });
      const editBtn = el("button", { className: "ghost sm" }, "编辑");
      editBtn.addEventListener("click", () => fillModelForm(key, m));
      acts.appendChild(editBtn);

      const delBtn = el("button", { className: "ghost sm danger" }, "删除");
      delBtn.addEventListener("click", async () => {
        const ok = await confirmAction("删除模型", "确定要删除模型「" + (m.name || key) + "」吗？");
        if (!ok) return;
        await callTool("ag_delete_model", { key });
        await loadModels();
        renderSavedModelsList();
        await refreshAll(false);
      });
      acts.appendChild(delBtn);

      item.appendChild(acts);
      box.appendChild(item);
    }
  }

  async function loadModels() {
    const res = await callTool("ag_list_models", {});
    savedModels = res.models || {};
  }

  async function openModelsModal() {
    await loadModels();
    renderSavedModelsList();
    resetModelForm();
    document.getElementById("models-backdrop").classList.remove("hidden");
  }

  function closeModelsModal() {
    document.getElementById("models-backdrop").classList.add("hidden");
  }

  async function saveModel() {
    const key = document.getElementById("m-key").value.trim();
    const name = document.getElementById("m-name").value.trim();
    const model_id = document.getElementById("m-id").value.trim();
    const api_base = document.getElementById("m-base").value.trim();
    const thinking_level = document.getElementById("m-thinking").value;
    const api_key = document.getElementById("m-apikey").value.trim();
    const system_prompt = document.getElementById("m-system").value.trim();

    if (!api_base || !model_id) {
      await rpc("ui/notify", { message: "请填写 API Base URL 和模型 ID", tone: "error" });
      return;
    }

    await callTool("ag_save_model", {
      key,
      name: name || model_id,
      api_base,
      model_id,
      thinking_level,
      api_key,
      system_prompt,
    });
    await loadModels();
    renderSavedModelsList();
    resetModelForm();
    await refreshAll(false);
    await rpc("ui/notify", { message: "已保存模型：" + (name || model_id), tone: "info" });
  }

  // ---- Discovery & Global Refresh ----
  async function discoverResources(forceRefresh) {
    const rescanBtn = document.getElementById("sub-rescan-btn");
    const codexRescanBtn = document.getElementById("sub-codex-rescan-btn");
    if (rescanBtn) rescanBtn.textContent = "扫描中...";
    if (codexRescanBtn) codexRescanBtn.textContent = "扫描中...";
    try {
      const data = await callTool("ag_discover_projects", { refresh: !!forceRefresh });
      discoveredWorkspaceProjects = data.workspace_projects || [];
      discoveredAgProjects = data.antigravity_projects || [];
      discoveredCodexProjects = data.codex_projects || [];
      discoveredCodexModels = data.codex_models || [];
      codexDefaultModel = data.codex_default_model || "";
      codexAvailable = !!data.codex_available;
      discoveredInstances = data.desktop_instances || [];
      agBridgeOnline = !!data.antigravity_bridge_online;
      document.getElementById("codex-pill").classList.toggle("hidden", !codexAvailable);
      document.getElementById("ag-pill").classList.toggle("hidden", !agBridgeOnline);
      if (data.active_project_dir) activeProjectDir = data.active_project_dir;
    } finally {
      if (rescanBtn) rescanBtn.textContent = "重新扫描";
      if (codexRescanBtn) codexRescanBtn.textContent = "重新扫描";
    }
  }

  async function refreshAll(forceDiscover) {
    const pill = document.getElementById("status-pill");
    const statusText = document.getElementById("status-text");
    const dirLine = document.getElementById("active-dir-line");
    let totalConvs = 0;
    try {
      const health = await callTool("ag_health", {});
      activeProjectDir = health.active_project_dir || "";
      totalConvs = health.tracked_conversations || 0;
      agBridgeOnline = !!health.antigravity_bridge_online;
      codexAvailable = !!health.codex_available;
      pill.className = "status-pill ok";
      statusText.textContent = "在线";
      document.getElementById("codex-pill").classList.toggle("hidden", !codexAvailable);
      document.getElementById("ag-pill").classList.toggle("hidden", !agBridgeOnline);
      dirLine.textContent = activeProjectDir ? "当前：" + activeProjectDir : "";
    } catch (err) {
      pill.className = "status-pill err";
      statusText.textContent = "离线";
      document.getElementById("summary-line").textContent = String(err.message || err);
      return;
    }

    const [projRes] = await Promise.all([
      callTool("ag_list_projects", {}),
      loadModels(),
      discoverResources(!!forceDiscover),
    ]);
    savedProjects = projRes.projects || {};
    renderProjectList(totalConvs);
  }

  async function copySelectedWorkspace() {
    const ws = currentPrimaryWorkspace();
    if (!ws) {
      await rpc("ui/notify", { message: "当前没有可复制的路径", tone: "error" });
      return;
    }
    await rpc("ui/copy", { text: ws });
    await rpc("ui/notify", { message: "已复制路径：" + ws, tone: "info" });
  }

  async function init() {
    document.getElementById("refresh-btn").addEventListener("click", () => refreshAll(true));
    document.getElementById("open-project-btn").addEventListener("click", () => openProjectModal(null));
    document.getElementById("open-models-btn").addEventListener("click", openModelsModal);
    document.getElementById("open-all-sessions-btn").addEventListener("click", () => openSessionsModal("", "全部历史会话", []));

    // Project Modal
    document.getElementById("close-project-btn").addEventListener("click", closeProjectModal);
    document.getElementById("cancel-project-btn").addEventListener("click", closeProjectModal);
    document.getElementById("save-project-btn").addEventListener("click", saveProject);
    document.getElementById("copy-ws-btn").addEventListener("click", copySelectedWorkspace);
    document.getElementById("ctrl-select").addEventListener("change", syncControllerState);
    document.getElementById("ws-select").addEventListener("change", onWorkspaceChanged);
    document.getElementById("open-add-agent-btn").addEventListener("click", openAddAgentModal);
    document.getElementById("project-backdrop").addEventListener("click", (e) => {
      if (e.target.id === "project-backdrop") closeProjectModal();
    });

    // Add-Agent Sub-Modal
    document.getElementById("close-add-agent-btn").addEventListener("click", closeAddAgentModal);
    document.getElementById("cancel-add-agent-btn").addEventListener("click", closeAddAgentModal);
    document.getElementById("confirm-add-agent-btn").addEventListener("click", confirmAddAgent);
    document.getElementById("agent-tab-api").addEventListener("click", () => setAddAgentType("openai_compat"));
    document.getElementById("agent-tab-codex").addEventListener("click", () => setAddAgentType("codex"));
    document.getElementById("agent-tab-claude").addEventListener("click", () => setAddAgentType("claude_instance"));
    document.getElementById("agent-tab-ag").addEventListener("click", () => setAddAgentType("antigravity"));
    document.getElementById("sub-codex-rescan-btn").addEventListener("click", async () => {
      await discoverResources(true);
      populateAddAgentPanel();
    });
    document.getElementById("sub-ag-select").addEventListener("change", (e) => {
      document.getElementById("sub-ag-empty-warn").classList.toggle("hidden", !agBridgeOnline || (e.target.value !== "__none__" && e.target.value !== ""));
    });
    document.getElementById("sub-ag-model").addEventListener("change", (e) => {
      document.getElementById("sub-ag-custom-model-wrap").classList.toggle("hidden", e.target.value !== "__custom__");
    });
    document.getElementById("sub-api-select").addEventListener("change", updateApiSelectionSummary);
    document.getElementById("sub-claude-inst").addEventListener("change", updateClaudeInstanceModeNote);
    document.getElementById("sub-rescan-btn").addEventListener("click", async () => {
      await discoverResources(true);
      populateAddAgentPanel();
    });
    document.getElementById("add-agent-backdrop").addEventListener("click", (e) => {
      if (e.target.id === "add-agent-backdrop") closeAddAgentModal();
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
    document.getElementById("prev-page-btn").addEventListener("click", () => loadSessionsPage(sessionsPage - 1));
    document.getElementById("next-page-btn").addEventListener("click", () => loadSessionsPage(sessionsPage + 1));
    document.getElementById("copy-reply-btn").addEventListener("click", async () => {
      if (!currentDetailReply) return;
      await rpc("ui/copy", { text: currentDetailReply });
      await rpc("ui/notify", { message: "已复制最新回复", tone: "info" });
    });
    document.getElementById("sessions-backdrop").addEventListener("click", (e) => {
      if (e.target.id === "sessions-backdrop") closeSessionsModal();
    });

    // Models Modal
    document.getElementById("close-models-btn").addEventListener("click", closeModelsModal);
    document.getElementById("cancel-models-btn").addEventListener("click", closeModelsModal);
    document.getElementById("new-model-btn").addEventListener("click", resetModelForm);
    document.getElementById("save-model-btn").addEventListener("click", saveModel);
    document.getElementById("models-backdrop").addEventListener("click", (e) => {
      if (e.target.id === "models-backdrop") closeModelsModal();
    });

    // Escape key closes the topmost open modal cleanly
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (!document.getElementById("add-agent-backdrop").classList.contains("hidden")) {
        closeAddAgentModal();
      } else if (!document.getElementById("project-backdrop").classList.contains("hidden")) {
        closeProjectModal();
      } else if (!document.getElementById("sessions-backdrop").classList.contains("hidden")) {
        closeSessionsModal();
      } else if (!document.getElementById("models-backdrop").classList.contains("hidden")) {
        closeModelsModal();
      }
    });

    try {
      const info = await rpc("ui/initialize", { protocolVersion: 1 });
      if (info && info.theme && info.theme.tokens) {
        applyTheme(info.theme.tokens);
      }
    } catch (_err) {
      // standalone preview
    }
    await refreshAll(false);
  }

  init();
})();
