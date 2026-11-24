import { t, thinkingLabel } from "./i18n.js";
import { el, state } from "./state.js";

export const PROVIDER_REGISTRY = [
  {
    type: "openai_compat",
    panelId: "sub-api-panel",
    tagClass: "api",
    tabKey: "tab_api",
    formatCardChip(c) {
      const m = c.model_ref ? state.savedModels[c.model_ref] : null;
      const name = (m && m.name) || c.label || c.model_id || "API";
      return { text: "API · " + name, title: (m && m.model_id) || c.model_id || name };
    },
    renderDraftMain(c, head, main) {
      const m = c.model_ref ? state.savedModels[c.model_ref] : null;
      head.appendChild(el("span", { className: "tag api" }, t("tab_api")));
      head.appendChild(el("span", null, (m && m.name) || c.label || c.model_ref));
      if (m && m.model_id) head.appendChild(el("span", { className: "tag" }, m.model_id));
      if (m && m.thinking_level) head.appendChild(el("span", { className: "tag" }, thinkingLabel(m.thinking_level)));
      main.appendChild(head);
      if (m && m.api_base) main.appendChild(el("div", { className: "agent-item-sub" }, m.api_base));
    },
  },
  {
    type: "codex",
    panelId: "sub-codex-panel",
    tagClass: "codex",
    tabKey: "tab_codex",
    formatCardChip(c) {
      const sb = c.sandbox_mode === "workspace-write" ? t("tag_write") : t("tag_readonly");
      const name = c.model || c.label || "Codex";
      return { text: "Codex · " + name + " (" + sb + ")", title: c.workspace_path || name };
    },
    renderDraftMain(c, head, main) {
      head.appendChild(el("span", { className: "tag codex" }, t("tab_codex")));
      head.appendChild(el("span", null, c.label || "Codex"));
      if (c.model) head.appendChild(el("span", { className: "tag" }, c.model));
      if (c.thinking_level) head.appendChild(el("span", { className: "tag" }, thinkingLabel(c.thinking_level)));
      const sbTag = c.sandbox_mode === "workspace-write" ? t("tag_write") : t("tag_readonly");
      head.appendChild(el("span", { className: "tag" }, sbTag));
      main.appendChild(head);
      if (c.workspace_path) main.appendChild(el("div", { className: "agent-item-sub" }, c.workspace_path));
    },
  },
  {
    type: "claude_instance",
    panelId: "sub-claude-panel",
    tagClass: "claude",
    tabKey: "tab_claude",
    formatCardChip(c) {
      const inst = state.discoveredInstances.find((x) => x.id === c.account_id);
      const disp = inst ? "#" + inst.number + " " + inst.email : (c.label || c.id);
      return { text: t("tab_claude") + " · " + disp, title: disp };
    },
    renderDraftMain(c, head, main) {
      const inst = state.discoveredInstances.find((x) => x.id === c.account_id);
      const disp = inst ? "#" + inst.number + " " + inst.email : (c.label || c.account_id);
      head.appendChild(el("span", { className: "tag claude" }, t("tab_claude")));
      head.appendChild(el("span", null, disp));
      if (c.model) head.appendChild(el("span", { className: "tag" }, c.model));
      if (c.thinking_level) head.appendChild(el("span", { className: "tag" }, thinkingLabel(c.thinking_level)));
      main.appendChild(head);
      main.appendChild(el("div", { className: "agent-item-sub" }, c.workspace_path || "Relay Inbox"));
    },
  },
  {
    type: "antigravity",
    panelId: "sub-ag-panel",
    tagClass: "ag",
    tabKey: "tab_ag",
    formatCardChip(c) {
      const ag = state.discoveredAgProjects.find((a) => a.project_id === c.project_id);
      const name = (ag && ag.name) || c.label || (c.project_id || "").slice(0, 8);
      return { text: "Antigravity · " + name, title: c.model || name };
    },
    renderDraftMain(c, head, main) {
      head.appendChild(el("span", { className: "tag ag" }, t("tab_ag")));
      head.appendChild(el("span", null, c.label || (c.project_id || "").slice(0, 8)));
      if (c.model) head.appendChild(el("span", { className: "tag" }, c.model));
      if (c.thinking_level) head.appendChild(el("span", { className: "tag" }, thinkingLabel(c.thinking_level)));
      main.appendChild(head);
      const ag = state.discoveredAgProjects.find((a) => a.project_id === c.project_id);
      main.appendChild(el("div", { className: "agent-item-sub" }, (ag && ag.workspace) || ("ID: " + c.project_id)));
    },
  },
];

export function getProviderMeta(type) {
  return PROVIDER_REGISTRY.find((p) => p.type === type) || null;
}

export function renderCollaboratorChip(c) {
  const meta = getProviderMeta(c.type);
  if (!meta) return el("span", { className: "tag" }, c.label || c.type);
  const info = meta.formatCardChip(c);
  return el("span", { className: "tag " + meta.tagClass, title: info.title || info.text }, info.text);
}
