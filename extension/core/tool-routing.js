// SPDX-License-Identifier: GPL-3.0-or-later
// Pure exact-server tool-name routing shared by every provider.
// eslint-disable-next-line no-unused-vars
const ZSToolRouting = (() => {
  "use strict";
  const bare = (name) => String(name || "").split("/").pop().split(".").pop();
  function resolve(name, serverHint, tools) {
    const list = Array.isArray(tools) ? tools : [];
    const exact = list.find((t) => t && t.name === name);
    const matches = list.filter((t) => t && bare(t.name) === bare(name));
    if (serverHint) {
      const selected = matches.find((t) => t.server === serverHint);
      if (selected) return { ok: true, name: selected.name, server: selected.server, exact: false };
      return { ok: false, reason: "server", servers: [...new Set(matches.map((t) => t.server).filter(Boolean))] };
    }
    if (exact) return { ok: true, name: exact.name, server: exact.server || null, exact: true };
    if (matches.length === 1) return { ok: true, name: matches[0].name, server: matches[0].server || null, exact: false };
    if (matches.length > 1) return { ok: false, reason: "ambiguous", servers: [...new Set(matches.map((t) => t.server).filter(Boolean))] };
    return { ok: false, reason: "unknown", servers: [] };
  }
  return { bare, resolve };
})();
