import { notify } from "./rpc.js";

export const state = {
  discoveredWorkspaceProjects: [],
  discoveredAgProjects: [],
  discoveredCodexProjects: [],
  discoveredCodexModels: [],
  codexDefaultModel: "",
  codexAvailable: false,
  discoveredInstances: [],
  agBridgeOnline: false,
  savedProjects: {},
  savedModels: {},
  activeProjectDir: "",
  pendingInboxCount: 0,

  // Project modal state
  draftCollaborators: [],
  addAgentType: "openai_compat",
  projectStep: "main", // "main" | "add_agent"
  editingProjectTitle: "",

  // Sessions modal state
  sessionsProjectKey: "",
  sessionsProjectTitle: "",
  sessionsPage: 1,
  sessionsTotalPages: 1,
  sessionsPageSize: 10,
  currentDetailConvId: "",
  currentDetailTitle: "",
  currentDetailReply: "",
};

export function el(tag, attrs, text) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "className") node.className = v;
      else if (v !== undefined && v !== null) node.setAttribute(k, v);
    }
  }
  if (text !== undefined) node.textContent = text;
  return node;
}

export function normPath(p) {
  return String(p || "").trim().replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
}

export function slugKey(name) {
  return String(name || "").trim().replace(/\s+/g, "-").toLowerCase();
}

export function formatTime(iso) {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    const pad = (n) => String(n).padStart(2, "0");
    return (
      d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) +
      " " + pad(d.getHours()) + ":" + pad(d.getMinutes())
    );
  } catch (_e) {
    return String(iso).slice(0, 16);
  }
}

export function buildWorkspaceItems() {
  const wsItems = [...state.discoveredWorkspaceProjects];
  if (
    state.activeProjectDir &&
    !wsItems.some((x) => normPath(x.path) === normPath(state.activeProjectDir))
  ) {
    const parts = state.activeProjectDir.replace(/\\/g, "/").split("/").filter(Boolean);
    wsItems.unshift({
      name: parts[parts.length - 1] || state.activeProjectDir,
      path: state.activeProjectDir,
    });
  }
  return wsItems;
}

/**
 * Guard any async UI action with try/catch and surface errors via ui/notify.
 */
export async function runSafe(fn) {
  try {
    return await fn();
  } catch (err) {
    await notify(err && err.message ? err.message : String(err), "error");
    return null;
  }
}

/**
 * Disable a button while an async action runs to prevent double-clicks.
 */
export async function withBusy(btn, fn) {
  if (btn && btn.disabled) return null;
  if (btn) btn.disabled = true;
  try {
    return await runSafe(fn);
  } finally {
    if (btn) btn.disabled = false;
  }
}
