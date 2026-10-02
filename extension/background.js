// SPDX-License-Identifier: GPL-3.0-or-later
// background.js - service worker.
// Owns ONE resilient WebSocket to the local bridge (ws://127.0.0.1:PORT).
// Keeping the socket here (not in the content script) avoids https→ws mixed
// content issues and centralises reconnect / timeout logic.
//
// Contract with content.js: every sendMessage ALWAYS gets a response object,
// even when the bridge is offline. The agentic loop must never hang waiting.

const PORT = 17613;
const URL = `ws://127.0.0.1:${PORT}`;

// ── Terminal panel data channel ─────────────────────────────────────────────
// The chat bar's terminal icon opens a LIVE terminal panel showing what the
// bridge is doing. There is no external launcher any more: the panel talks to
// the bridge over the socket the extension already owns, and falls back to the
// bridge's loopback HTTP port when the socket is down, so the panel can still
// prove whether the bridge is alive rather than just guessing.
const PLUGIN_PORT = PORT + 1;
const LOG_URL = `http://127.0.0.1:${PLUGIN_PORT}`;

// Cursor into the bridge's log ring. Held per worker so a reconnect resumes
// exactly where it stopped instead of replaying or skipping lines.
let logCursor = 0;

async function fetchLogs(since = logCursor, timeout = 6000) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeout);
  try {
    const res = await fetch(`${LOG_URL}/logs?since=${encodeURIComponent(since)}`, {
      signal: ctl.signal, cache: "no-store",
    });
    if (!res.ok) return { ok: false, kind: "http", status: res.status };
    const body = await res.json();
    if (typeof body.newest === "number") logCursor = Math.max(logCursor, body.newest);
    return { ok: true, ...body, via: "http" };
  } catch (e) {
    return { ok: false, kind: (e && e.name === "AbortError") ? "timeout" : "unreachable",
             error: String((e && e.message) || e) };
  } finally {
    clearTimeout(timer);
  }
}

// The log frame arrives either as a backlog (bridge_log) or as a push
// (bridge_log_push). Both feed the same consumer in the content script, which is
// the single place that owns rendering - this worker stays a transport.
function forwardLog(target, payload) {
  if (!target) return;
  // `target` is a tabId for chrome.tabs.sendMessage, or "all" for a broadcast.
  if (target === "all") {
    for (const url of PROVIDER_URLS) {
      chrome.tabs.query({ url }, (tabs) => {
        for (const t of tabs || []) {
          chrome.tabs.sendMessage(t.id, payload, () => void chrome.runtime.lastError);
        }
      });
    }
    return;
  }
  try {
    chrome.tabs.sendMessage(target, payload, () => void chrome.runtime.lastError);
  } catch {}
}

// Ask every provider content script to repaint the terminal icon and refresh an
// open terminal panel. Used when bridge state changes outside a status push, so
// an open panel never shows a stale picture.
function broadcastToProviders(payload) {
  for (const url of PROVIDER_URLS) {
    chrome.tabs.query({ url }, (tabs) => {
      for (const t of tabs || []) {
        chrome.tabs.sendMessage(t.id, payload, () => void chrome.runtime.lastError);
      }
    });
  }
}


// ── Native host: the terminal icon can START the bridge ─────────────────────
// A browser extension cannot spawn a process. A Native Messaging host that the
// user registered once (runtime/install_native_host.py) can, and the browser only
// lets THIS extension talk to it. Every call is one request / one reply
// (sendNativeMessage), which survives the MV3 worker being suspended.
//
// The host can run exactly one program: runtime/bridge.py in its own folder.
const NATIVE_HOST = "com.multiscript.bridge";
let hostMissingUntil = 0;      // after a "not installed" answer, stop asking for a while
let lastAutoStartAt = 0;
const AUTOSTART_COOLDOWN_MS = 45000;

function nativeCall(cmd, extra = {}, timeout = 30000) {
  return new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; clearTimeout(t); resolve(v); } };
    const t = setTimeout(() => finish({ ok: false, code: "host_timeout", error: "The launcher did not answer in time." }), timeout);
    try {
      chrome.runtime.sendNativeMessage(NATIVE_HOST, { cmd, ...extra }, (reply) => {
        const err = chrome.runtime.lastError;
        if (err) {
          const m = String(err.message || err);
          const forbidden = /forbidden/i.test(m);
          const missing = /not found|not registered|No such native/i.test(m);
          if (missing || forbidden) hostMissingUntil = Date.now() + 5 * 60 * 1000;
          finish({
            ok: false,
            code: forbidden ? "host_forbidden" : (missing ? "host_missing" : "host_error"),
            error: m, extensionId: chrome.runtime.id,
          });
          return;
        }
        hostMissingUntil = 0;
        finish(reply && typeof reply === "object" ? reply : { ok: false, code: "host_error", error: "empty reply" });
      });
    } catch (e) {
      finish({ ok: false, code: "host_error", error: String((e && e.message) || e), extensionId: chrome.runtime.id });
    }
  });
}

function storageGet(keys) {
  return new Promise((resolve) => { try { chrome.storage.local.get(keys, (r) => resolve(r || {})); } catch { resolve({}); } });
}
function storageSet(obj) {
  return new Promise((resolve) => { try { chrome.storage.local.set(obj, () => resolve()); } catch { resolve(); } });
}

// Called when the socket is down. Safe to call often: it is rate-limited, skipped
// when the user pressed Stop, when auto-start is off, or when the host is not
// installed - and the host itself no-ops if the port is already listening.
async function maybeAutoStart(reason) {
  const now = Date.now();
  if (connected || now - lastAutoStartAt < AUTOSTART_COOLDOWN_MS || now < hostMissingUntil) return;
  const prefs = await storageGet(["msTerminalAutoStart", "msBridgeUserStopped"]);
  if (prefs.msTerminalAutoStart === false || prefs.msBridgeUserStopped === true) return;
  lastAutoStartAt = now;
  broadcastToProviders({ type: "bridge_host_event", event: "starting", reason });
  const r = await nativeCall("start", { wait: 12 }, 40000);
  broadcastToProviders({ type: "bridge_host_event", event: r.ok ? "started" : "failed", result: r });
  if (r.ok) { reconnectDelay = RECONNECT_MIN; connect(); }
}

// Chat sites where a Multi-Script provider content script runs. Status pushes go
// to every tab matching these. Add the new provider's URL pattern here (and in
// manifest.json content_scripts + host_permissions) when integrating another AI.
const PROVIDER_URLS = ["https://chat.deepseek.com/*", "https://chatgpt.com/*", "https://chat.openai.com/*", "https://claude.ai/*", "https://www.claude.ai/*", "https://claude.com/*", "https://www.claude.com/*", "https://gemini.google.com/*", "https://www.kimi.ai/*", "https://kimi.ai/*", "https://chat.z.ai/*", "https://chat.qwen.ai/*", "https://arena.ai/*", "https://www.meta.ai/*", "https://meta.ai/*", "https://notion.ai/*", "https://www.notion.ai/*", "https://notion.com/*", "https://www.notion.com/*", "https://app.notion.com/*", "https://notion.so/*", "https://www.notion.so/*"];

const RECONNECT_MIN = 1000;
const RECONNECT_MAX = 5000;
const HEARTBEAT_MS = 10000;
// If no message (incl. pong) arrives within this window while we believe we're
// connected, the socket is half-open: force a reconnect instead of letting
// pending requests slowly time out.
const STALE_SOCKET_MS = 25000;
const REQUEST_TIMEOUT_DEFAULT = 130000; // a bit above the 120s tool timeout

let ws = null;
let connected = false;
let reconnectDelay = RECONNECT_MIN;
let reconnectTimer = null;
let heartbeatTimer = null;
let lastMessageAt = 0; // timestamp of the last frame received from the bridge
let nextId = 1;
const pending = new Map(); // id -> {resolve, timer}
let toolsCache = [];
let mcpAlive = false;
let serversCache = [];
let enginesCache = [];
// true/false = a PLACE is loaded and usable in Roblox Studio; null = unknown.
// The MCP process stays alive when Studio is closed or its MCP option is off,
// so this is probed separately (bridge "studio_status").
let studioConnected = null;
// true/false = a Roblox Studio app is connected to the MCP server at all; null =
// unknown. studioApp=true with studioConnected=false means "Studio open but no
// place"; studioApp=false means "Studio closed OR its MCP option disabled".
let studioApp = null;
// true/false = a Roblox Studio WINDOW/PROCESS exists on this machine (checked
// bridge-side via tasklist); null = unknown/old bridge. Distinguishes the two
// studioApp=false sub-cases the UI must word differently: Studio genuinely not
// launched ("open Roblox Studio") vs Studio OPEN but its MCP plugin never
// registered with the bridge - the documented fix for the latter is opening
// Assistant Settings > MCP Servers inside Studio (validated live 3x), which
// "open Roblox Studio" wording completely fails to convey.
let studioProc = null;

function log(...a) {
  console.log("[zs-bg]", ...a);
}

// ── WebSocket lifecycle ─────────────────────────────────────────────────
function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  clearTimeout(reconnectTimer);
  try {
    ws = new WebSocket(URL);
  } catch (e) {
    log("WebSocket ctor failed", e);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    connected = true;
    failedAttempts = 0;
    reconnectDelay = RECONNECT_MIN;
    lastMessageAt = Date.now();
    log("connected to bridge");
    startHeartbeat();
    broadcastStatus();
  };

  ws.onmessage = (ev) => {
    lastMessageAt = Date.now();
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    handleBridgeMessage(msg);
  };

  ws.onclose = () => {
    connected = false;
    mcpAlive = false;
    studioConnected = null;
    studioApp = null;
    studioProc = null;
    serversCache = [];
    enginesCache = [];
    stopHeartbeat();
    failAllPending("bridge connection closed");
    broadcastStatus();
    scheduleReconnect();
  };

  ws.onerror = () => {
    // onclose will follow; nothing to do here but avoid an unhandled error.
    try { ws.close(); } catch {}
  };
}

let failedAttempts = 0;
function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  // Two quiet misses first (the bridge may simply be mid-restart), then ask the
  // native host - if the user installed it - to start the bridge.
  if (++failedAttempts === 2) maybeAutoStart("socket-down");
  reconnectTimer = setTimeout(connect, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 1.7, RECONNECT_MAX);
}

function startHeartbeat() {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    if (connected) {
      // Half-open socket: the WS still reports OPEN but nothing comes through.
      // The pong (and every other frame) refreshes lastMessageAt; if it has
      // gone stale, drop the dead socket so onclose triggers a reconnect.
      if (lastMessageAt && Date.now() - lastMessageAt > STALE_SOCKET_MS) {
        log("socket stale, forcing reconnect");
        try { ws.close(); } catch {}
        return;
      }
      // Keeps the MV3 service worker alive AND detects a half-open socket.
      send({ type: "ping" }).catch(() => {});
      refreshStudioStatus();
    }
  }, HEARTBEAT_MS);
}

function stopHeartbeat() {
  clearInterval(heartbeatTimer);
  heartbeatTimer = null;
}

// Resolve once the socket is OPEN, or false after `timeout` ms.
function waitForConnection(timeout = 8000) {
  return new Promise((resolve) => {
    if (connected && ws && ws.readyState === WebSocket.OPEN) return resolve(true);
    connect(); // nudge a (re)connection - important after a worker wake-up
    const t0 = Date.now();
    const iv = setInterval(() => {
      if (connected && ws && ws.readyState === WebSocket.OPEN) {
        clearInterval(iv);
        resolve(true);
      } else if (Date.now() - t0 > timeout) {
        clearInterval(iv);
        resolve(false);
      }
    }, 100);
  });
}

// ── request/response over the socket ────────────────────────────────────
async function send(obj, timeout = REQUEST_TIMEOUT_DEFAULT) {
  // The MV3 service worker can be suspended; the first message after a wake-up
  // arrives before the socket has re-opened. Wait for it instead of failing -
  // otherwise Kimi wrongly hears "bridge offline".
  if (!connected || !ws || ws.readyState !== WebSocket.OPEN) {
    await waitForConnection(8000);
  }
  return new Promise((resolve) => {
    if (!connected || !ws || ws.readyState !== WebSocket.OPEN) {
      resolve({ ok: false, kind: "disconnected", error: "bridge not connected" });
      return;
    }
    const id = nextId++;
    const payload = { ...obj, id };
    const timer = setTimeout(() => {
      if (pending.has(id)) {
        pending.delete(id);
        resolve({ ok: false, kind: "timeout", error: "bridge did not respond in time" });
      }
    }, timeout);
    pending.set(id, { resolve, timer });
    try {
      ws.send(JSON.stringify(payload));
    } catch (e) {
      clearTimeout(timer);
      pending.delete(id);
      resolve({ ok: false, kind: "disconnected", error: String(e) });
    }
  });
}

// Ask the bridge whether a Roblox Studio instance is actually connected to the
// MCP server. Broadcasts only on change so the UI updates promptly but quietly.
let studioProbing = false;
async function refreshStudioStatus() {
  if (studioProbing || !connected) return;
  studioProbing = true;
  try {
    const r = await send({ type: "studio_status" }, 12000);
    const v = r && r.ok && typeof r.studio === "boolean" ? r.studio : null;
    if (v !== studioConnected) {
      studioConnected = v;
      broadcastStatus();
    }
  } finally {
    studioProbing = false;
  }
}

function handleBridgeMessage(msg) {
  if ("studio" in msg && (typeof msg.studio === "boolean" || msg.studio === null)) {
    studioConnected = msg.studio;
  }
  if ("studio_app" in msg && (typeof msg.studio_app === "boolean" || msg.studio_app === null)) {
    studioApp = msg.studio_app;
  }
  if ("studio_proc" in msg && (typeof msg.studio_proc === "boolean" || msg.studio_proc === null)) {
    studioProc = msg.studio_proc;
  }
  if (Array.isArray(msg.engines)) enginesCache = msg.engines;
  if (msg.type === "studio_status") {
    resolvePending(msg.id, { ok: true, studio: studioConnected });
    broadcastStatus();
    return;
  }
  if (msg.type === "connected") {
    mcpAlive = !!msg.mcp_alive;
    if (Array.isArray(msg.tools)) toolsCache = msg.tools;
    if (Array.isArray(msg.servers)) serversCache = msg.servers;
    broadcastStatus();
    // The log ring lives in the bridge process, which restarts independently of
    // us. On every (re)connect the cursor is reset so the panel re-reads the
    // backlog and shows the CURRENT run rather than resuming a stale cursor
    // from a previous process that happened to have higher seq numbers.
    logCursor = 0;
    // Tell any open panel to drop its own cursor too. The panel keeps an
    // independent cursor so an idle poll is incremental; if a bridge restart
    // reset only ours, the panel would keep asking for `since=<stale high seq>`
    // and the log would look frozen. "reset" means "forget what you have and
    // re-read", which is exactly right for a new process.
    broadcastToProviders({ type: "ms-log-reset" });
    return;
  }
  if (msg.type === "bridge_log") {
    if (typeof msg.newest === "number") logCursor = Math.max(logCursor, msg.newest);
    resolvePending(msg.id, { ok: true, lines: msg.lines || [], newest: msg.newest,
                             service: msg.service || null });
    return;
  }
  if (msg.type === "bridge_log_push") {
    const lines = Array.isArray(msg.lines) ? msg.lines : [];
    if (lines.length) logCursor = Math.max(logCursor, Number(lines[lines.length - 1].seq) || logCursor);
    // Push frames are fire-and-forget: they are broadcast to every tab so a
    // panel that was just opened is already filling in without asking again.
    broadcastToProviders({ type: "ms-log-push", lines });
    return;
  }
  if (msg.type === "bridge_log_cleared") {
    logCursor = 0;
    broadcastToProviders({ type: "ms-log-cleared" });
    resolvePending(msg.id, { ok: true });
    return;
  }
  if (msg.type === "pong") {
    resolvePending(msg.id, { ok: true });
    return;
  }
  if (msg.type === "tools") {
    if (Array.isArray(msg.tools)) toolsCache = msg.tools;
    if (Array.isArray(msg.servers)) serversCache = msg.servers;
    mcpAlive = !!msg.mcp_alive;
    resolvePending(msg.id, { ok: true, tools: toolsCache });
    broadcastStatus();
    return;
  }
  if (msg.type === "elevenlabs_settings") {
    resolvePending(msg.id, { ok: !!msg.ok, status: msg.status || null, error: msg.error });
    return;
  }
  if (msg.type === "tool_result") {
    resolvePending(msg.id, msg.ok
      ? { ok: true, text: msg.text, images: msg.images || [] }
      : { ok: false, kind: msg.kind, error: msg.error });
    return;
  }
  if (msg.type === "mcp_status") {
    mcpAlive = !!msg.alive;
    if (Array.isArray(msg.tools)) toolsCache = msg.tools;
    if (Array.isArray(msg.servers)) serversCache = msg.servers;
    resolvePending(msg.id, { ok: !!msg.ok, alive: msg.alive, error: msg.error });
    broadcastStatus();
    return;
  }
  if (msg.type === "server_changed") {
    // The bridge acks, then restarts itself to reload config.json. The socket
    // will drop right after this - the content script shows a spinner until the
    // reconnect lands and a fresh status arrives.
    resolvePending(msg.id, { ok: !!msg.ok, error: msg.error, restarting: !!msg.restarting });
    return;
  }
  if (msg.type === "error") {
    resolvePending(msg.id, { ok: false, error: msg.error });
    return;
  }
}

function resolvePending(id, value) {
  const p = pending.get(id);
  if (!p) return;
  clearTimeout(p.timer);
  pending.delete(id);
  p.resolve(value);
}

function failAllPending(reason) {
  for (const [, p] of pending) {
    clearTimeout(p.timer);
    p.resolve({ ok: false, kind: "disconnected", error: reason });
  }
  pending.clear();
}

// ── Surface vision: browser tabs ─────────────────────────────────────────
// Why this lives in the service worker: chrome.tabs is unavailable to a content
// script, and a page cannot enumerate or drive its siblings. The worker is the
// only place with the right privilege, so the model reaches tabs through these
// six narrow verbs rather than arbitrary script execution.
//
// Safety posture (deliberate, and reflected in the tool notes the model reads):
//   * surface_list   - read-only metadata; the ONLY discovery entry point.
//   * surface_read   - read-only text of a tab the model already listed.
//   * surface_open   - creates a tab ONLY for an http/https URL. No schemes that
//                      can execute (javascript:, data:, file:, chrome://, etc.).
//   * surface_type   - writes into a composer-like field, then optionally submits.
//   * surface_focus  - visibility only, never content.
//   * surface_close  - closes a tab, refused for the tab hosting the agent.
// Anything not listed above is simply not exposed.
const SURFACE_MAX_TEXT = 12000; // per-tab readback cap, keeps the prompt small
const SURFACE_MIN_TEXT = 200;   // below this a read is not worth a round trip; raised, not rejected.
const SURFACE_DEFAULT_TEXT = 6000; // a middle default so an unqualified read is neither tiny nor huge.

function surfaceDeny(reason) {
  return { ok: false, error: reason };
}

// Normalise the id the model passed. Tabs are addressed by integer id only -
// never by title or URL - so a stale or hallucinated id fails loudly instead of
// silently hitting the wrong tab.
function surfaceTabId(msg) {
  const raw = msg && (msg.tab_id !== undefined ? msg.tab_id : msg.id);
  const n = typeof raw === "number" ? raw : parseInt(String(raw == null ? "" : raw), 10);
  return Number.isInteger(n) && n >= 0 ? n : null;
}

function surfaceTabDescriptor(t, opts) {
  const o = opts || {};
  return {
    id: t.id,
    windowId: t.windowId,
    active: !!t.active,
    pinned: !!t.pinned,
    discarded: !!t.discarded,
    status: t.status || null,
    title: (t.title || "").slice(0, 200),
    url: (t.url || "").slice(0, 500),
    // host is what the model usually reasons about; keep it pre-extracted so it
    // does not have to parse URLs itself.
    host: (() => { try { return new URL(t.url || "").host; } catch (_) { return ""; } })(),
    favIconUrl: o.icons === false ? undefined : (t.favIconUrl || undefined),
  };
}

async function surfaceList(msg) {
  try {
    const all = await chrome.tabs.query({});
    let rows = all.map((t) => surfaceTabDescriptor(t, { icons: msg && msg.include_icons !== false }));
    const filter = (msg && msg.host) ? String(msg.host).toLowerCase() : "";
    if (filter) rows = rows.filter((r) => r.host.toLowerCase().includes(filter));
    // Deterministic order: current window first, then by window, then by index.
    const currentWin = rows.length ? rows.find((r) => r.active) : null;
    rows.sort((a, b) => (a.windowId - b.windowId) || (a.id - b.id));
    return {
      ok: true,
      count: rows.length,
      currentWindowId: currentWin ? currentWin.windowId : null,
      activeTabId: currentWin ? currentWin.id : null,
      tabs: rows,
      note: "Use these integer ids with surface_read / surface_focus / surface_type / surface_close.",
    };
  } catch (e) {
    return surfaceDeny(`could not enumerate tabs: ${e && e.message ? e.message : e}`);
  }
}

async function surfaceFocus(msg) {
  const id = surfaceTabId(msg);
  if (id === null) return surfaceDeny("a numeric tab_id is required (get one from surface_list)");
  try {
    const t = await chrome.tabs.get(id);
    if (!t) return surfaceDeny(`no tab with id ${id}`);
    await chrome.tabs.update(id, { active: true });
    // Focus the owning window too, otherwise the tab is "active" in a window the
    // user cannot see and the switch appears to do nothing.
    if (t.windowId !== undefined) {
      try { await chrome.windows.update(t.windowId, { focused: true }); } catch (_) { /* window may be gone */ }
    }
    return { ok: true, focused: surfaceTabDescriptor(t), note: "Tab is now active and its window is focused." };
  } catch (e) {
    return surfaceDeny(`could not focus tab ${id}: ${e && e.message ? e.message : e}`);
  }
}

async function surfaceOpen(msg) {
  const raw = msg && msg.url ? String(msg.url).trim() : "";
  if (!raw) return surfaceDeny("url is required");
  let parsed;
  try { parsed = new URL(raw); } catch (_) { return surfaceDeny(`not a valid absolute url: ${raw}`); }
  // Only the two web schemes. javascript:/data:/file:/chrome:/about: can execute
  // or reach the local machine, so they are refused outright.
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return surfaceDeny(`refusing to open a '${parsed.protocol}' url; only http: and https: are allowed`);
  }
  try {
    const created = await chrome.tabs.create({ url: parsed.toString(), active: msg && msg.active !== false });
    return { ok: true, opened: surfaceTabDescriptor(created), note: "New tab created. Call surface_read to see its content." };
  } catch (e) {
    return surfaceDeny(`could not open ${parsed.toString()}: ${e && e.message ? e.message : e}`);
  }
}

async function surfaceClose(msg) {
  const id = surfaceTabId(msg);
  if (id === null) return surfaceDeny("a numeric tab_id is required");
  try {
    const t = await chrome.tabs.get(id);
    if (!t) return surfaceDeny(`no tab with id ${id}`);
    await chrome.tabs.remove(id);
    return { ok: true, closed: { id, title: (t.title || "").slice(0, 200), url: (t.url || "").slice(0, 500) } };
  } catch (e) {
    return surfaceDeny(`could not close tab ${id}: ${e && e.message ? e.message : e}`);
  }
}

// Read a tab's visible text. Uses chrome.scripting.executeScript so it works on
// any http/https page without pre-declared host permissions, and returns only
// text - never HTML, never cookies, never form values the page did not render.
async function surfaceRead(msg) {
  const id = surfaceTabId(msg);
  if (id === null) return surfaceDeny("a numeric tab_id is required (get one from surface_list)");
  // Honour a caller-supplied readback size, CLAMPED into the range that is
  // actually safe - never rejected. The schema previously advertised
  // minimum:500 while this function ignored the value entirely and always used
  // SURFACE_MAX_TEXT, so a caller asking for a small read was turned away at the
  // bridge for a limit the worker was never going to look at. Bound it instead:
  // anything below the floor is raised to it, anything above the ceiling is
  // lowered to it, and an absent/blank value means "use the default".
  const requestedLimit = Number(msg && (msg.limit ?? msg.maxChars));
  const limit = Number.isFinite(requestedLimit) && requestedLimit > 0
    ? Math.max(SURFACE_MIN_TEXT, Math.min(SURFACE_MAX_TEXT, Math.floor(requestedLimit)))
    : SURFACE_DEFAULT_TEXT;
  try {
    const t = await chrome.tabs.get(id);
    if (!t) return surfaceDeny(`no tab with id ${id}`);
    const url = t.url || "";
    if (!/^https?:/i.test(url)) {
      return surfaceDeny(`tab ${id} is '${url.slice(0, 40)}' - only http/https tabs can be read`);
    }
    const results = await chrome.scripting.executeScript({
      target: { tabId: id },
      func: (limit) => {
        // Prefer the readable article/main region when one exists; fall back to
        // body. Strip chrome and script/style so the model gets prose, not code.
        const pick = document.querySelector("article, main, [role=main]") || document.body;
        let text = "";
        if (pick) {
          const clone = pick.cloneNode(true);
          clone.querySelectorAll("script,style,noscript,svg,canvas,iframe").forEach((n) => n.remove());
          text = (clone.innerText || clone.textContent || "").replace(/\n{3,}/g, "\n\n").trim();
        }
        return {
          title: document.title || "",
          url: location.href,
          selection: String(window.getSelection ? window.getSelection() : "").slice(0, 2000),
          text: text.slice(0, limit),
          truncated: text.length > limit,
        };
      },
      args: [limit],
    });
    const r = Array.isArray(results) && results[0] ? results[0].result : null;
    if (!r) return surfaceDeny(`tab ${id} returned no content (it may be blank or still loading)`);
    return {
      ok: true,
      id,
      title: r.title,
      url: r.url,
      selection: r.selection || "",
      text: r.text || "",
      truncated: !!r.truncated,
      textLimit: limit,
    };
  } catch (e) {
    // executeScript is refused on chrome://, the Web Store and PDF viewers; say so.
    return surfaceDeny(`could not read tab ${id}: ${e && e.message ? e.message : e}. Some pages (browser internals, the Web Store, PDF viewers) cannot be read.`);
  }
}

// Type into the focused composer of a tab, then optionally submit. Mirrors how
// a person would: focus the field, set its value the way the page expects
// (native setter + input event so frameworks register it), then Enter or the
// page's own send button.
async function surfaceType(msg) {
  const id = surfaceTabId(msg);
  if (id === null) return surfaceDeny("a numeric tab_id is required (get one from surface_list)");
  const text = msg && msg.text !== undefined ? String(msg.text) : "";
  if (!text) return surfaceDeny("text is required");
  if (text.length > 20000) return surfaceDeny("text is too long (20000 character limit)");
  const shouldSubmit = !(msg && msg.submit === false);
  try {
    const t = await chrome.tabs.get(id);
    if (!t) return surfaceDeny(`no tab with id ${id}`);
    if (!/^https?:/i.test(t.url || "")) {
      return surfaceDeny(`tab ${id} is not an http/https page; it cannot be typed into`);
    }
    const results = await chrome.scripting.executeScript({
      target: { tabId: id },
      func: (value, submit) => {
        const isEditable = (el) => el && (
          el.tagName === "TEXTAREA" ||
          (el.tagName === "INPUT" && /^(text|search|email|url|tel|password)$/i.test(el.type || "text")) ||
          el.isContentEditable === true
        );
        // Ordered candidate list: the element the page already focused, then the
        // biggest visible composer-like field. This is heuristic on purpose - we
        // cannot know each site's DOM - but it degrades honestly when it fails.
        const candidates = [];
        const push = (el) => { if (el && !candidates.includes(el)) candidates.push(el); };
        push(document.activeElement);
        const sel = 'textarea,[contenteditable="true"],[contenteditable=""],input[type="text"],input[type="search"],input:not([type])';
        const found = Array.from(document.querySelectorAll(sel));
        const visible = found.filter((el) => {
          const r = el.getBoundingClientRect();
          const s = getComputedStyle(el);
          return r.width > 40 && r.height > 10 && s.visibility !== "hidden" && s.display !== "none";
        });
        visible.sort((a, b) => (b.getBoundingClientRect().height * b.getBoundingClientRect().width) -
                               (a.getBoundingClientRect().height * a.getBoundingClientRect().width));
        visible.forEach(push);
        const field = candidates.find(isEditable);
        if (!field) return { ok: false, error: "no editable field was found on this page" };

        field.focus();
        if (field.isContentEditable) {
          // Contenteditable sites (most chat UIs) need a real beforeinput/input
          // pair, not just textContent, or their framework state stays empty.
          field.textContent = "";
          const dt = new DataTransfer();
          dt.setData("text/plain", value);
          field.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: "insertText", data: value, dataTransfer: dt }));
          document.execCommand ? document.execCommand("insertText", false, value) : (field.textContent = value);
          if (field.textContent !== value && !field.textContent) field.textContent = value;
          field.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: value, dataTransfer: dt }));
        } else {
          // Native setter so React/Vue-controlled inputs notice the change.
          const proto = field.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
          const setter = Object.getOwnPropertyDescriptor(proto, "value");
          if (setter && setter.set) setter.set.call(field, value);
          else field.value = value;
          field.dispatchEvent(new Event("input", { bubbles: true }));
          field.dispatchEvent(new Event("change", { bubbles: true }));
        }

        let submitted = false;
        if (submit) {
          const enter = () => {
            const opts = { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true, composed: true };
            field.dispatchEvent(new KeyboardEvent("keydown", opts));
            field.dispatchEvent(new KeyboardEvent("keypress", opts));
            field.dispatchEvent(new KeyboardEvent("keyup", opts));
          };
          enter();
          // Some composers need the button rather than Enter. Look for a nearby
          // send control and click it if the field still holds our text.
          setTimeout(() => {
            try {
              const current = field.isContentEditable ? field.textContent : field.value;
              if (current && current.trim() === value.trim()) {
                const btns = Array.from(document.querySelectorAll('button,[role="button"],input[type="submit"]'));
                const send = btns.find((b) => {
                  const label = ((b.getAttribute("aria-label") || "") + " " + (b.getAttribute("data-testid") || "") + " " + (b.textContent || "") + " " + (b.title || "")).toLowerCase();
                  return /send|submit|ask|go\b/.test(label) && !b.disabled;
                });
                if (send) { send.click(); submitted = true; }
              } else { submitted = true; }
            } catch (_) { /* best effort */ }
          }, 120);
        }
        return {
          ok: true,
          field: field.tagName.toLowerCase() + (field.isContentEditable ? "[contenteditable]" : ""),
          typed: value.length,
          submitRequested: submit,
        };
      },
      args: [text, shouldSubmit],
    });
    const r = Array.isArray(results) && results[0] ? results[0].result : null;
    if (!r || !r.ok) {
      return surfaceDeny(`could not type into tab ${id}: ${(r && r.error) || "no result"}`);
    }
    if (shouldSubmit) {
      // Give the page's own JS a beat to actually send before we report success.
      await new Promise((res) => setTimeout(res, 400));
    }
    return {
      ok: true,
      id,
      title: (t.title || "").slice(0, 200),
      url: (t.url || "").slice(0, 500),
      field: r.field,
      typed: r.typed,
      submitted: shouldSubmit,
      note: shouldSubmit ? "Text entered and send was triggered. Call surface_read to confirm the page reacted." : "Text entered but NOT submitted (submit=false).",
    };
  } catch (e) {
    return surfaceDeny(`could not type into tab ${id}: ${e && e.message ? e.message : e}`);
  }
}

// ── status push to any open DeepSeek tab + popup ─────────────────────────
function statusObj() {
  return { type: "zs-status", connected, mcpAlive, studio: studioConnected, studioApp, studioProc, tools: toolsCache.length, servers: serversCache, engines: enginesCache };
}

function broadcastStatus() {
  chrome.runtime.sendMessage(statusObj()).catch(() => {});
  chrome.tabs.query({ url: PROVIDER_URLS }, (tabs) => {
    for (const t of tabs) chrome.tabs.sendMessage(t.id, statusObj()).catch(() => {});
  });
}

// ── messages from content.js / popup.js ─────────────────────────────────
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    switch (msg.type) {
      case "status":
        if (!connected) connect(); // self-heal after a worker wake-up
        sendResponse(statusObj());
        break;
      case "list_tools": {
        // Prefer a live refresh; fall back to cache so the loop never stalls.
        // 10s, not 25s: a catalogue request only blocks this long when one of the
        // MCP servers is dead (typically Roblox in a degraded, Blender-only
        // session), and in that exact case we already hold a perfectly good cached
        // catalogue. Waiting the full 25s just froze the boot for no new data.
        const r = await send({ type: "list_tools" }, 10000);
        if (r.ok) sendResponse({ ok: true, tools: r.tools });
        else sendResponse({ ok: toolsCache.length > 0, tools: toolsCache, error: r.error });
        break;
      }
      case "elevenlabs_status": {
        sendResponse(await send({ type: "elevenlabs_status" }, 10000));
        break;
      }
      case "elevenlabs_configure": {
        sendResponse(await send({ type: "elevenlabs_configure", api_key: msg.api_key }, 10000));
        break;
      }
      case "elevenlabs_clear": {
        sendResponse(await send({ type: "elevenlabs_clear" }, 10000));
        break;
      }
      case "call_tool": {
        const timeout = (msg.timeout || 120000) + 10000;
        const r = await send(
          { type: "call_tool", name: msg.name, arguments: msg.arguments, timeout: msg.timeout },
          timeout
        );
        sendResponse(r);
        break;
      }
      case "restart_mcp": {
        const r = await send({ type: "restart_mcp" }, 30000);
        sendResponse(r);
        break;
      }
      case "add_server": {
        const r = await send({
          type: "add_server", server_id: msg.server_id,
          command: msg.command, args: msg.args, env: msg.env,
        }, 15000);
        sendResponse(r);
        break;
      }
      case "remove_server": {
        const r = await send({ type: "remove_server", server_id: msg.server_id }, 15000);
        sendResponse(r);
        break;
      }
      // ── Terminal panel (the chat bar's terminal icon) ────────────────────
      // `terminal_snapshot` is the ONE call the panel makes when it opens: it
      // returns the log backlog AND the service picture together, so the panel
      // paints completely on first frame instead of in two racing halves.
      case "terminal_snapshot": {
        const since = Number(msg.since) || 0;
        if (connected) {
          // Preferred path: the live socket. Ask the bridge for the backlog; new
          // lines then arrive unprompted as bridge_log_push.
          try {
            const r = await send({ type: "bridge_log", since, limit: msg.limit || 400 }, 9000);
            if (r && r.ok) { sendResponse({ ...r, via: "ws" }); break; }
          } catch {}
        }
        // Fallback: the bridge's loopback HTTP port. Works when the socket is
        // down but the process is alive, which is exactly when a panel is most
        // useful - it can then show real logs instead of just "disconnected".
        sendResponse(await fetchLogs(since));
        break;
      }
      case "terminal_clear":
        if (connected) {
          try {
            const r = await send({ type: "bridge_log_clear" }, 6000);
            if (r && r.ok) { sendResponse(r); break; }
          } catch {}
        }
        // No live socket to tell: clearing is still honest locally, the panel
        // just drops what it is showing.
        logCursor = 0;
        sendResponse({ ok: true, local: true });
        break;
      case "terminal_probe":
        // Liveness only, so the panel can say "bridge up, socket reconnecting"
        // instead of a flat "offline". Never mutates anything.
        sendResponse(await fetchLogs(Number(msg.since) || 0, 4000));
        break;
      // ── Native host (terminal icon starts / stops / watches the bridge) ──
      case "host_info":
        sendResponse(await nativeCall("ping", {}, 8000));
        break;
      case "host_start": {
        await storageSet({ msBridgeUserStopped: false });
        lastAutoStartAt = Date.now();
        const r = await nativeCall("start", { wait: 12 }, 40000);
        if (r.ok) { reconnectDelay = RECONNECT_MIN; connect(); }
        sendResponse(r);
        break;
      }
      case "host_stop": {
        // Remember the intent: a bridge the user stopped must stay stopped, not be
        // resurrected by auto-start on the next failed reconnect.
        await storageSet({ msBridgeUserStopped: true });
        const r = await nativeCall("stop", {}, 30000);
        sendResponse(r);
        break;
      }
      case "host_restart": {
        await storageSet({ msBridgeUserStopped: false });
        lastAutoStartAt = Date.now();
        const r = await nativeCall("restart", { wait: 12 }, 50000);
        if (r.ok) { reconnectDelay = RECONNECT_MIN; setTimeout(connect, 600); }
        sendResponse(r);
        break;
      }
      case "host_processes":
        sendResponse(await nativeCall("processes", {}, 25000));
        break;
      case "host_tail":
        sendResponse(await nativeCall("tail", { lines: msg.lines || 60 }, 8000));
        break;
      case "host_prefs": {
        if (msg.set && typeof msg.autoStart === "boolean") await storageSet({ msTerminalAutoStart: msg.autoStart });
        const p = await storageGet(["msTerminalAutoStart", "msBridgeUserStopped"]);
        sendResponse({ ok: true, autoStart: p.msTerminalAutoStart !== false, userStopped: p.msBridgeUserStopped === true,
                       extensionId: chrome.runtime.id, hostMissing: Date.now() < hostMissingUntil });
        break;
      }
      case "reconnect":
        reconnectDelay = RECONNECT_MIN;
        connect();
        sendResponse({ ok: true });
        break;
      // ── Surface vision (tabs) ───────────────────────────────────────────
      // The content script cannot see other tabs; chrome.tabs lives here. These
      // handlers are intentionally narrow: list (read-only), activate (focus),
      // read (returns the visible TEXT of a tab), type (fills+submits a composer).
      // Every one of them requires an explicit id from a prior `surface_list`,
      // so the model can never blind-fire at a tab it has not looked at.
      case "surface_list":
        sendResponse(await surfaceList(msg));
        break;
      case "surface_focus":
        sendResponse(await surfaceFocus(msg));
        break;
      case "surface_read":
        sendResponse(await surfaceRead(msg));
        break;
      case "surface_open":
        sendResponse(await surfaceOpen(msg));
        break;
      case "surface_type":
        sendResponse(await surfaceType(msg));
        break;
      case "surface_close":
        sendResponse(await surfaceClose(msg));
        break;
      default:
        sendResponse({ ok: false, error: "unknown message" });
    }
  })();
  return true; // async sendResponse
});

// Wake/keepalive hooks.
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);

connect();
