// SPDX-License-Identifier: GPL-3.0-or-later
// Notion AI provider adapter. Notion ships several AI surfaces (full-page chat,
// home chat and side-panel chat), so this adapter uses semantic attributes and
// visible-control fallbacks rather than one brittle class name.
// eslint-disable-next-line no-unused-vars
const ZSProvider = (() => {
  "use strict";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const storageGet = (key) => { try { return globalThis.localStorage.getItem(key); } catch { return null; } };
  const storageSet = (key, value) => { try { globalThis.localStorage.setItem(key, value); return true; } catch { return false; } };
  const storageRemove = (key) => { try { globalThis.localStorage.removeItem(key); } catch {} };
  let diag = () => {};
  const timings = {
    GEN_IDLE_MS: 1800, REASON_IDLE_MS: 12000, WARMUP_MS: 45000,
    REASON_NOREPLY_MS: 90000, STABLE_MS: 9000, RESPONSE_TIMEOUT_MS: 300000,
  };

  const visible = (el) => {
    if (!el || !el.isConnected) return false;
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden" || cs.opacity === "0") return false;
    const r = el.getBoundingClientRect();
    return !!(el.getClientRects().length || r.width || r.height);
  };
  function queryAllDeep(selector, root=document) {
    const out = [], seen = new Set(), roots = [root];
    while (roots.length) {
      const cur = roots.shift(); if (!cur || seen.has(cur)) continue; seen.add(cur);
      try {
        for (const el of cur.querySelectorAll(selector)) out.push(el);
        for (const el of cur.querySelectorAll("*")) if (el.shadowRoot) roots.push(el.shadowRoot);
      } catch {}
    }
    return [...new Set(out)];
  }
  let editorCache = null;
  function editorCandidates() {
    const selectors = [
      "textarea[placeholder*='Ask' i]", "textarea[placeholder*='message' i]", "textarea[placeholder*='Do anything' i]",
      "[contenteditable='true'][data-placeholder*='Ask' i]", "[contenteditable='true'][data-placeholder*='message' i]",
      "[contenteditable='true'][data-placeholder*='Do anything' i]", "[contenteditable='true'][aria-placeholder*='Do anything' i]",
      "[contenteditable='true'][aria-label*='Ask' i]", "[contenteditable='true'][aria-label*='message' i]",
      "[contenteditable='true'][aria-label*='Do anything' i]", "[contenteditable][role='textbox']",
      "[role='textbox'][data-content-editable-root='true']", "[contenteditable][data-slate-editor='true']",
      "[contenteditable]:not([contenteditable='false'])", "[role='textbox']", ".ProseMirror", "textarea",
    ];
    const found = new Set(queryAllDeep(selectors.join(",")));
    // The current notion.ai home composer paints “Do anything with AI…” in a
    // sibling/overlay instead of an attribute on the editable node. Walk from
    // that visible label back to its local editable control.
    for (const hint of queryAllDeep("div,span,p")) {
      const text = (hint.textContent || "").trim();
      if (!/^do anything with ai[….]*$/i.test(text)) continue;
      let cur = hint;
      for (let depth = 0; cur && depth < 5; depth += 1, cur = cur.parentElement) {
        const editable = cur.matches && cur.matches("textarea,[contenteditable='true'],[role='textbox']") ? cur :
          cur.querySelector && cur.querySelector("textarea,[contenteditable='true'],[role='textbox']");
        if (editable) { found.add(editable); break; }
      }
    }
    return [...found].filter((e) => visible(e) && !e.closest("#zs-root,.zs-bar,.zs-chip"));
  }
  function getEditor() {
    if (visible(editorCache) && editorCache.getAttribute("contenteditable") !== "false" && !editorCache.disabled) return editorCache;
    const all = editorCandidates();
    const scored = all.map((e) => {
      const meta = `${e.getAttribute("placeholder") || ""} ${e.getAttribute("data-placeholder") || ""} ${e.getAttribute("aria-placeholder") || ""} ${e.getAttribute("aria-label") || ""}`;
      const ai = e.closest("[role='dialog'],[data-testid*='ai' i],[data-testid*='agent' i],[class*='ai-chat' i],[class*='notion-ai' i],[class*='agent-chat' i],aside");
      const r = e.getBoundingClientRect();
      const homePrompt = /do anything with ai/i.test(meta) || !![...((e.parentElement && e.parentElement.querySelectorAll("div,span,p")) || [])].find((x) => /do anything with ai/i.test(x.textContent || ""));
      return { e, score: (homePrompt ? 20000 : 0) + (/ask|message|chat|agent/i.test(meta) ? 10000 : 0) + (ai ? 5000 : 0) + Math.max(0, r.bottom) };
    }).sort((a, b) => b.score - a.score);
    editorCache = scored[0] ? scored[0].e : null;
    return editorCache;
  }
  const editorText = () => { const e = getEditor(); return e ? ("value" in e ? e.value : e.textContent || "") : ""; };
  function chatRoot() {
    const e = getEditor(); if (!e) return document;
    let cur = e.parentElement;
    while (cur && cur !== document.documentElement) {
      const meta = `${cur.getAttribute("role") || ""} ${cur.getAttribute("data-testid") || ""} ${typeof cur.className === "string" ? cur.className : ""}`.toLowerCase();
      const surface = cur.tagName === "ASIDE" || cur.getAttribute("role") === "dialog" ||
        (/(agent|ai).*(panel|chat|conversation)|(panel|chat|conversation).*(agent|ai)/.test(meta) && !/composer|input/.test(meta));
      if (surface) return cur;
      cur = cur.parentElement;
    }
    return e.closest("main") || document;
  }

  const turnSelector = [
    "[data-message-author-role]", "[data-role='user']", "[data-role='assistant']",
    "[data-testid*='chat-message' i]", "[data-testid*='agent-message' i]", "[data-testid*='user-message' i]", "[data-testid*='message' i]",
    "[data-message-id]", "[data-author]", "[data-testid*='turn' i]", "[data-testid*='response' i]", "[data-testid*='prompt' i]", "article[data-role]", "[class*='chatMessage']", "[class*='agentMessage']", "[class*='message-row']",
  ].join(",");
  function roleOf(el) {
    if (!el) return "";
    const raw = [el.getAttribute("data-message-author-role"), el.getAttribute("data-role"),
      el.getAttribute("data-testid"), el.getAttribute("data-author"), el.getAttribute("aria-label"), el.className]
      .filter(Boolean).join(" ").toLowerCase();
    if (el.dataset && el.dataset.zsRoleFallback) return el.dataset.zsRoleFallback;
    if (/assistant|notion-ai|ai-response|agent-message|response|bot/.test(raw)) return "assistant";
    if (/\buser\b|human|prompt/.test(raw)) return "user";
    return "";
  }
  function allItems() {
    const root = chatRoot();
    let raw = [...root.querySelectorAll(turnSelector)].filter(visible);
    // notion.ai sometimes removes role metadata from streamed responses. Recover
    // command-bearing response blocks so their JSON reaches the shared parser.
    if (!raw.some((x) => roleOf(x) === "assistant")) {
      for (const code of root.querySelectorAll("pre,code")) {
        if (!visible(code) || code.closest("form,[data-testid*='composer' i],[contenteditable='true']")) continue;
        if (!/"(?:command|tool)"\s*:\s*"|###\s*lua|###mcp_tool###/i.test(code.textContent || "")) continue;
        let item = code;
        for (let depth = 0; item.parentElement && depth < 4; depth += 1) item = item.parentElement;
        item.dataset.zsRoleFallback = "assistant"; raw.push(item);
      }
    }
    raw = [...new Set(raw)];
    // Remove nested matches; each turn must be represented once.
    return raw.filter((x) => !raw.some((y) => y !== x && y.contains(x) && roleOf(y) === roleOf(x)));
  }
  const isUserItem = (x) => roleOf(x) === "user";
  const isAssistantItem = (x) => roleOf(x) === "assistant";
  const assistantItems = () => allItems().filter(isAssistantItem);
  const assistantCount = () => assistantItems().length;
  const userCount = () => allItems().filter(isUserItem).length;
  const lastAssistant = () => assistantItems().slice(-1)[0] || null;
  const itemKey = (x) => x && (x.getAttribute("data-message-id") || x.getAttribute("data-block-id") || x.id) || null;
  const lastAssistantId = () => itemKey(lastAssistant());

  const BLOCK = /^(P|DIV|PRE|LI|UL|OL|BLOCKQUOTE|H[1-6]|TABLE|TR|SECTION|ARTICLE)$/;
  function textWithout(root, excludeSel) {
    let out = "";
    const br = () => { if (out && !out.endsWith("\n")) out += "\n"; };
    const walk = (n) => {
      if (n.nodeType === 3) { out += n.nodeValue || ""; return; }
      if (n.nodeType !== 1 || (excludeSel && n.matches && n.matches(excludeSel))) return;
      if (n.tagName === "BR") { out += "\n"; return; }
      const block = BLOCK.test(n.tagName); if (block) br();
      for (const c of n.childNodes) walk(c);
      if (block) br();
    };
    if (root) walk(root);
    return out.replace(/\n{3,}/g, "\n\n").trim();
  }
  const itemText = (x) => textWithout(x, ".zs-chip");
  const classifyText = (x, excludeSel) => textWithout(x, excludeSel);
  const streamLen = (x) => itemText(x === undefined ? lastAssistant() : x).length;
  function readAssistant() {
    const item = lastAssistant();
    return item ? { present: true, reply: itemText(item), thinking: "", item } :
      { present: false, reply: "", thinking: "", item: null };
  }
  const snapshot = () => { const x = lastAssistant(); return { th: 0, rp: x ? streamLen(x) : 0 }; };

  function composerContainer() {
    const e = getEditor(); if (!e) return null;
    const semantic = e.closest("form,[data-testid*='composer' i],[class*='composer' i]");
    if (semantic) return semantic;
    let cur = e.parentElement;
    for (let depth = 0; cur && depth < 5; depth += 1, cur = cur.parentElement) {
      if (cur.querySelector && cur.querySelector("button,[role='button']")) return cur;
    }
    return e.parentElement;
  }
  function sendButton() {
    const e = getEditor(), local = composerContainer();
    const roots = [local, chatRoot()].filter(Boolean);
    for (const root of roots) {
      const candidates = [...root.querySelectorAll("button,[role='button']")].filter(visible);
      const hit = candidates.find((b) => {
        const label = `${b.getAttribute("aria-label") || ""} ${b.getAttribute("title") || ""} ${b.getAttribute("data-testid") || ""} ${b.textContent || ""}`;
        return (/send|submit|ask notion|arrow.?up/i.test(label) || b.getAttribute("type") === "submit") && !/stop|cancel/i.test(label);
      });
      if (hit) return hit;
      if (root === local && e) {
        const er = e.getBoundingClientRect();
        const fallback = candidates.filter((b) => {
          const label = `${b.getAttribute("aria-label") || ""} ${b.getAttribute("title") || ""} ${b.getAttribute("data-testid") || ""} ${b.textContent || ""}`;
          const r = b.getBoundingClientRect();
          return !/stop|cancel|attach|plus|settings|option|filter|microphone|voice/i.test(label) &&
            r.left >= er.left + er.width * 0.55 && r.bottom >= er.top && r.top <= er.bottom + 24;
        }).sort((a, b) => b.getBoundingClientRect().right - a.getBoundingClientRect().right)[0];
        if (fallback) return fallback;
      }
    }
    return null;
  }
  function stopButton() {
    const root = chatRoot();
    return [...root.querySelectorAll("button")].filter(visible).find((b) =>
      /stop|cancel response/i.test(`${b.getAttribute("aria-label") || ""} ${b.getAttribute("data-testid") || ""} ${b.textContent || ""}`)) || null;
  }
  let maxLen = 0, grewAt = 0, streamItem = null;
  function sample() {
    const x = lastAssistant(), n = streamLen(x), now = Date.now();
    if (x !== streamItem || n < maxLen - 300) { streamItem = x; maxLen = n; grewAt = now; }
    else if (n > maxLen) { maxLen = n; grewAt = now; }
  }
  function isGenerating() { sample(); return !!stopButton() || (maxLen > 0 && Date.now() - grewAt < timings.GEN_IDLE_MS); }
  const isBusyNow = isGenerating, isHardGenerating = isGenerating;
  const turnHalted = () => false;

  let locked = false;
  function setInputLock(on) {
    locked = on; const e = getEditor(); if (!e) return;
    if (on) { e.setAttribute("aria-disabled", "true"); e.dataset.zsLocked = "1"; e.style.pointerEvents = "none"; }
    else { e.removeAttribute("aria-disabled"); delete e.dataset.zsLocked; e.style.pointerEvents = ""; }
  }
  function setText(e, text) {
    e.focus();
    if (e.tagName === "TEXTAREA" || e.tagName === "INPUT") {
      const proto = e.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, "value").set;
      setter.call(e, text); e.dispatchEvent(new Event("input", { bubbles: true }));
    } else {
      const sel = window.getSelection(), range = document.createRange();
      range.selectNodeContents(e); sel.removeAllRanges(); sel.addRange(range);
      e.dispatchEvent(new InputEvent("beforeinput", { bubbles: true, cancelable: true, inputType: "insertText", data: text }));
      const inserted = document.execCommand("insertText", false, text);
      if (!inserted || (e.textContent || "") !== text) e.textContent = text;
      e.dispatchEvent(new InputEvent("input", { bubbles: true, inputType: "insertText", data: text }));
    }
  }
  async function typeAndSend(text) {
    const e = getEditor(); if (!e) throw new Error("Notion AI input box not found");
    const relock = locked; if (relock) { e.removeAttribute("aria-disabled"); e.style.pointerEvents = ""; }
    try {
      setText(e, String(text).slice(0, 90000)); await sleep(120);
      const b = sendButton();
      if (b && !b.disabled && b.getAttribute("aria-disabled") !== "true") b.click();
      else {
        e.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true }));
        e.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true }));
      }
      const deadline = Date.now() + 2500;
      while (Date.now() < deadline && getEditor() === e && editorText().trim()) await sleep(100);
      if (getEditor() === e && editorText().trim()) throw new Error("Notion Agent did not accept the message. Click inside the Agent composer, reload Notion, and try again.");
    } finally { if (relock) setInputLock(true); }
  }
  function stopGeneration() { const b = stopButton(); if (b) b.click(); }

  const chatIsEmpty = () => allItems().length === 0;
  const isFreshChat = () => chatIsEmpty() && !!getEditor();
  function composerFrame() { return composerContainer(); }
  const coverTarget = composerFrame;
  function barMount() {
    const box = composerFrame(); if (!box || !box.parentElement) return null;
    return { parent: box.parentElement, before: box, inside: false };
  }
  function enforceComposer() { return { ready: !!getEditor() }; }
  async function ensureComposerReady(reason) {
    const profile = getAutoRoutingProfile();
    diag("mode_ready", { reason, provider: "notion", autoRoutingProfile: profile, routing: "prompt-only" });
    return { ready: !!getEditor(), profile, routing: { method: "prompt-only", directPickerInteraction: false } };
  }
  // Notion can reuse the same URL for multiple Agent chats. Track the visible
  // non-empty -> empty transition so every New chat gets a distinct bootstrap
  // identity even when the pathname does not change.
  const notionPageToken = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 9)}`;
  let notionChatEpoch = 0, notionHadTurns = false;
  function conversationKey() {
    const empty = chatIsEmpty();
    if (!empty) notionHadTurns = true;
    else if (notionHadTurns) { notionChatEpoch += 1; notionHadTurns = false; }
    return `${location.pathname}${location.search}${location.hash}|${notionPageToken}|${notionChatEpoch}`;
  }
  async function openAIChat() {
    const controls = queryAllDeep("button,a,[role='button'],[role='link']").filter(visible);
    const b = controls.find((x) => /notion ai|ask ai|open ai|chat with ai/i.test(`${x.getAttribute("aria-label") || ""} ${x.getAttribute("title") || ""} ${x.textContent || ""}`));
    if (!b) return false;
    b.click();
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) { if (getEditor()) return true; await sleep(100); }
    return !!getEditor();
  }
  async function openNewChat() {
    const b = queryAllDeep("button,a").filter(visible).find((x) =>
      /new chat|new conversation|start over/i.test(`${x.getAttribute("aria-label") || ""} ${x.textContent || ""}`));
    if (!b) return false; b.click(); await sleep(500); return true;
  }

  function scanError() {
    const root = chatRoot();
    for (const el of root.querySelectorAll("[role='alert'],[data-testid*='error' i]")) {
      const t = (el.innerText || "").trim(); if (visible(el) && t && t.length < 500) return t;
    }
    return getEditor() ? null : "Notion AI chat input is not open. Open Notion AI chat and try again.";
  }
  const isTooLongMsg = (t) => /too long|context limit|maximum length/i.test(t || "");
  const isBusyMsg = (t) => /try again|temporarily unavailable|rate limit|too many requests/i.test(t || "");
  const findContinueBtn = () => [...document.querySelectorAll("button")].filter(visible).find((b) => /continue generating|continue/i.test((b.textContent || "").trim())) || null;
  function clickContinueBtn() { const b = findContinueBtn(); if (!b) return false; b.click(); return true; }

  function installSendHooks(h) {
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
      const ed = getEditor(); if (!ed || !(ed === e.target || ed.contains(e.target))) return;
      if (!editorText().trim() || h.isBlocked()) return;
      if (!h.isStarted()) { h.onBlockedAttempt(); return; }
      h.onUserMessage(assistantCount());
    }, true);
    document.addEventListener("click", (e) => {
      const b = e.target && e.target.closest && e.target.closest("button"); if (!b) return;
      if (b === stopButton()) { h.onNativeStop(); return; }
      if (b !== sendButton() || h.isBlocked() || !editorText().trim()) return;
      if (!h.isStarted()) { h.onBlockedAttempt(); return; }
      h.onUserMessage(assistantCount());
    }, true);
  }

  function selfTest() {
    const editor = getEditor(), send = sendButton(), stop = stopButton(), root = chatRoot();
    const result = {
      provider: "notion", url: location.href, editorFound: !!editor,
      editorKind: editor ? `${editor.tagName.toLowerCase()}${editor.getAttribute("contenteditable") === "true" ? ":contenteditable" : ""}` : null,
      chatRoot: root === document ? "document" : (root.getAttribute("data-testid") || root.getAttribute("role") || root.tagName.toLowerCase()),
      sendButtonFound: !!send, stopButtonFound: !!stop,
      userTurns: userCount(), assistantTurns: assistantCount(),
      activeAutoProfile: getAutoRoutingProfile(), profilePromptReady: !!getStartupProfilePrompt(), routing: "prompt-only",
    };
    result.ready = result.editorFound && (result.sendButtonFound || editor.getAttribute("contenteditable") === "true" || editor.tagName === "TEXTAREA");
    result.recommendation = result.ready ? "Provider controls detected." : "Open Notion Agent chat, click the composer, then reload the page and run the self-test again.";
    return result;
  }

  const SHAPE = /"(?:command|tool)"\s*:\s*"|###\s*lua|###mcp_tool###|<\/?\s*[|｜].*dsml/i;
  function findToolBlockSpot(item) {
    if (!item) return null; let spot = null;
    for (const el of item.querySelectorAll("pre,code,p")) {
      if (el.closest(".zs-chip") || !SHAPE.test(el.textContent || "")) continue;
      el.classList.add("zs-tool-hide"); spot ||= { parent: el.parentElement, ref: el };
    }
    return spot;
  }
  const AUTO_ROUTING_PROFILES = {
    opus55: { label: "Claude Opus 5.5", model: "Claude Opus 5.5", availability: "awaiting-notion", description: "Future-ready request for the real Claude Opus 5.5 through Notion Auto, with honest fallback." },
    opus: { label: "Claude Opus 5", model: "Claude Opus 5", availability: "supported", description: "Requests the real Claude Opus 5 through Notion Auto for each new chat." },
    gpt: { label: "GPT-5.6 Sol", model: "GPT-5.6 Sol", availability: "supported", description: "Requests the real GPT-5.6 Sol through Notion Auto for each new chat." },
    kimi: { label: "Kimi K3", model: "Kimi K3", availability: "supported", description: "Requests the real Kimi K3 through Notion Auto for each new chat." },
    luna: { label: "GPT-6 Luna", model: "GPT-6 Luna", availability: "awaiting-notion", description: "Future-ready request for the real GPT-6 Luna through Notion Auto, with honest fallback." },
  };
  function getAutoRoutingProfile() { return storageGet("zs.notion.autoRoutingProfile") || ""; }
  function setAutoRoutingProfile(id) {
    if (!id || !AUTO_ROUTING_PROFILES[id]) storageRemove("zs.notion.autoRoutingProfile");
    else storageSet("zs.notion.autoRoutingProfile", id);
    // Auto-routing and a direct picker preference are mutually exclusive. A stale
    // direct preference otherwise reopens an unavailable picker before every send.
    storageRemove("zs.notion.modelPreference");
    return getAutoRoutingProfile();
  }
  function getStartupProfilePrompt() {
    const x = AUTO_ROUTING_PROFILES[getAutoRoutingProfile()];
    if (!x) return "";
    return `[NOTION AUTO MODEL REQUEST — PROCESS BEFORE THE NEXT BLOCK]
Preferred model: ${x.model}

ROUTING
- Route this new chat to the exact real ${x.model} backend model if this workspace, plan and chat are eligible.
- Keep that same real model for the full conversation and for all instructions that follow.
- If it is unavailable, continue with Notion Auto's best eligible real model; never imitate ${x.model} through role-play.


HANDOFF
- This block is routing and execution metadata, not the user's task. Do not answer or acknowledge it separately.
- Apply the complete Multi-Script operating block immediately after this one.
- Never claim ${x.model} was selected unless Notion itself exposes or confirms that selection.`;
  }
  function autoRoutingProfiles() {
    return Object.entries(AUTO_ROUTING_PROFILES).map(([id, x]) => ({ id, label: x.label, description: x.description, model: x.model, availability: x.availability || "supported" }));
  }
  function routingProfileDiagnostics() {
    const id = getAutoRoutingProfile(), x = AUTO_ROUTING_PROFILES[id];
    const prompt = x ? getStartupProfilePrompt() : "";
    return { selected: id || "standard", label: x ? x.label : "Standard Auto", requestedModel: x ? x.model : null, promptReady: !!prompt, promptCharacters: prompt.length, strategy: "one prompt-only Auto routing request", directPickerInteraction: false, placement: "immediately after the Multi-Script session marker", includesSharedSystem: true, availability: x ? (x.availability || "supported") : "auto", fallback: x ? "Standard Auto without model imitation" : null, applies: "once at the start of each new chat" };
  }

  const PROMPT_EXTRA = `- You are running inside Notion AI through a browser extension. For project actions, emit Multi-Script JSON commands as plain fenced-code text; do not use Notion's native connectors or page-editing actions as a substitute, because those do not reach the local game-engine MCP servers.\n- Route by intent: use Multi-Script commands for local game-engine/project actions; use Notion's native page/database/search capabilities for explicit workspace tasks; for hybrid tasks, read the Notion brief first and then execute engine changes through Multi-Script. Never substitute a Notion page edit for a requested engine action.
- Treat Notion pages and workspace text as reference material unless the user explicitly asks you to edit that Notion content. Keep workspace summaries concise, preserve citations/links, and never expose private workspace context in broader outputs without explicit approval.
- The short preferred-model routing request appears before this shared prompt. Do not emulate a named model. Use the real model if Notion Auto routed to it, then apply these common instructions.
- COMMON NOTION EXECUTION RULES: preserve exact workspace and repository context; inspect before editing; use only discovered tool schemas; keep Multi-Script skills and connected tools available; follow the selected prompt-skills, creative-surface, usage, and validation settings; verify meaningful changes with concrete evidence; never claim a model identity that Notion does not expose.`;

  return {
    id: "notion", displayName: "Notion AI", timings, supportsVision: false, sendCharBudget: 90000,
    persistentBarWhenNoEditor: true, noEditorMessage: "Notion AI chat is not open yet.",
    reliableCounts: false, chipAtItemLevel: true, coverMaxH: 280,
    init({ diag: d } = {}) {
      if (d) diag = d;
      try {
        document.documentElement.dataset.zsNotionVer = "6.13.0";
        new MutationObserver(() => { if (editorCache && !editorCache.isConnected) editorCache = null; }).observe(document.documentElement, { childList: true, subtree: true });
      } catch {}
    },
    allItems, isUserItem, isAssistantItem, itemText, classifyText, assistantCount,
    userCount, lastAssistant, lastAssistantId, itemKey, readAssistant, streamLen, snapshot,
    getEditor, editorText, chatIsEmpty, isFreshChat, composerFrame, coverTarget, barMount,
    setInputLock, typeAndSend, stopGeneration, isGenerating, isBusyNow, isHardGenerating,
    enforceComposer, ensureComposerReady, turnHalted, findContinueBtn, clickContinueBtn,
    scanError, isTooLongMsg, isBusyMsg, openAIChat, openNewChat, conversationKey, installSendHooks,
    findToolBlockSpot, getStartupProfilePrompt, getAutoRoutingProfile,
    setAutoRoutingProfile, autoRoutingProfiles, routingProfileDiagnostics, selfTest, promptExtra: PROMPT_EXTRA,
  };
})();
