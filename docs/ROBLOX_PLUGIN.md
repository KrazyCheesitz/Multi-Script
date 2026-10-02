# Roblox Studio companion plugin

Multi-Script 6.24.0 includes `roblox-plugin/MultiScriptCompanion.server.lua`, a redesigned Studio companion that adds 24 curated MCP tools without replacing Roblox Studio’s official MCP server.

## What it adds

The companion tools appear in Multi-Script only while the plugin is connected and you have explicitly enabled a permission tier:

| Tier | Tools | Access |
|---|---:|---|
| Off | 0 | Default. Diagnostics only. |
| Read | 7 | Project summary, selection, tree/search, properties, snapshots, and path validation. |
| Project | 16 | Read tools plus bounded create/edit/rename/tag/reparent/clone operations and undo checkpoints. Script source is excluded. |
| Full | 24 | Project tools plus script read/search/patch/replace, delete, undo, and redo. Requires a second confirmation click. |

The official Roblox Studio MCP catalog, Multi-Script’s 230 direct tools, 90 Roblox specialists, 199 Roblox skills, and these 24 companion tools are separate capability surfaces and are reported separately.

## Interface

The panel opens by itself the first time the plugin loads, and remembers whether you closed it. It
follows the extension's own monochrome design language, so Studio and the browser panel read as one
product:

- **Header** — the ✦ mark, the brand, and a version chip you can compare against the bridge.
- **Status** — one line per surface (bridge, Studio MCP, compatibility, elevated tools), each with a
  coloured dot: green when healthy, amber when it needs attention, red when it is down.
- **Tool access** — the four tier buttons. Read is the light primary action; the rest are secondary.
  Full still requires a second click to confirm.
- **Project** — a readiness bar that fills and tints by band (green ≥ 80, amber ≥ 50, red below),
  followed by the readiness, inventory and selection lines, then refresh / scan / diagnostics /
  catalog.

The panel is presentation only. Every tool, permission tier and safety property is unchanged by the
redesign.

## Install and enable

1. Start the Multi-Script bridge by clicking the terminal icon in the chat bar (run `Setup.bat` / `MacOS_Setup.command` once first), or run `python runtime/bridge.py`.
2. Copy `roblox-plugin/MultiScriptCompanion.server.lua` into the Studio Plugins folder.
3. Restart Studio and open a place.
4. Enable **Game Settings → Security → Allow HTTP Requests**.
5. Optional but recommended: enable **Assistant Settings → MCP Servers → Studio as MCP Server** for Roblox’s official native tools too.
6. The **Multi-Script Companion** panel opens automatically on first install. Reopen it any time from **Plugins → Multi-Script Companion** or the toolbar button.
7. Choose **Read**, **Project**, or **Full** in the widget. Leave it Off when you do not need companion execution.

Windows: `%LOCALAPPDATA%/Roblox/Plugins/`  
macOS: `~/Documents/Roblox/Plugins/`

## Security model

- Execution is Off by default and is never persisted across Studio sessions.
- The bridge listens on `127.0.0.1:17614`; it is not exposed to the LAN.
- Registration issues an ephemeral in-memory bearer token. Poll and result requests require it.
- There is no HTTP endpoint that can enqueue a job. Jobs enter only through Multi-Script’s existing extension-to-bridge tool channel.
- Both bridge and plugin verify the tool’s permission tier.
- The queue, request bodies, batches, source size, tree depth, result size, and wait time are bounded.
- Paths are confined to an allowlist of editable project services. Service deletion and cyclic reparenting are refused.
- Generic property editing cannot set `Source`, `Parent`, `ClassName`, or locked metadata. Source changes use dedicated Full-tier tools.
- Handlers are curated and allowlisted. The plugin never invokes `loadstring` or accepts arbitrary Lua to execute.
- Mutating operations create Studio undo waypoints where supported. Use source control for important projects.

## Diagnostics

The widget still provides live bridge/version status, official MCP status, a bounded heartbeat, selection summary, inventory, a yielding heuristic readiness scan, and a sanitized Output report. The report never includes source, tokens, prompts, or job payloads.

## Troubleshooting

- **Bridge offline:** start Multi-Script and enable Studio HTTP Requests.
- **Companion tools missing:** open the widget and explicitly enable a tier; tools disappear after disconnect or token expiry.
- **Permission error:** enable the tier named in the error. Full tools require the double-click confirmation.
- **Official MCP offline:** companion tools can still operate, but enable Studio as MCP Server to use Roblox’s native catalog too.
- **Version mismatch:** install the bridge, extension, and plugin from the same release.
