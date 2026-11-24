let rpcId = 1;
const pending = new Map();

export function applyTheme(theme) {
  if (!theme || typeof theme !== "object") return;
  const root = document.documentElement;
  const scheme = theme.scheme || (theme.tokens && theme.tokens.scheme);
  if (scheme === "dark" || scheme === "light") {
    root.setAttribute("data-scheme", scheme);
  }
  const tokens = theme.tokens || theme;
  if (tokens && typeof tokens === "object") {
    for (const [k, v] of Object.entries(tokens)) {
      if (typeof v === "string" && k !== "scheme") {
        root.style.setProperty("--" + k, v);
      }
    }
  }
}

window.addEventListener("message", (event) => {
  const msg = event.data;
  if (event.source !== window.parent || !msg || typeof msg !== "object") return;
  if (msg.method === "ui/themeChanged" && msg.params) {
    applyTheme(msg.params);
    return;
  }
  if (msg.id && pending.has(msg.id)) {
    const entry = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) entry.reject(new Error(msg.error.message || "RPC error"));
    else entry.resolve(msg.result);
  }
});

export function rpc(method, params) {
  return new Promise((resolve, reject) => {
    const id = rpcId++;
    pending.set(id, { resolve, reject });
    window.parent.postMessage({ jsonrpc: "2.0", id, method, params: params || {} }, "*");
  });
}

export async function callTool(name, args) {
  const res = await rpc("tools/call", { name, arguments: args || {} });
  const text = (res && res.content && res.content[0] && res.content[0].text) || "{}";
  if (res && res.isError) throw new Error(text);
  return JSON.parse(text);
}

export async function notify(message, tone = "info") {
  try {
    await rpc("ui/notify", { message: String(message || ""), tone });
  } catch (_err) {
    // ignore outside host preview
  }
}

/**
 * Strictly fail-closed confirmation dialog: returns false on cancellation, timeout, or error.
 */
export async function confirmAction(title, message, confirmLabel = "Confirm") {
  try {
    const res = await rpc("ui/confirm", { title, message, confirmLabel, danger: true });
    return !!(res && (res.confirmed === true || res === true));
  } catch (_err) {
    return false;
  }
}
