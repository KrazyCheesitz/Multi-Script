# Roblox Studio companion plugin

Multi-Script 5.1.0 includes `roblox-plugin/MultiScriptCompanion.server.lua`.

## What it does

- Shows whether the local Multi-Script bridge is online.
- Shows whether Roblox Studio and a place are connected to Roblox's official Studio MCP server.
- Displays the exact live count of native Roblox tools advertised to the bridge.
- Sends a local heartbeat with non-secret project metadata: place name/id, script/module counts, selection count, and run state.
- Scans script source for a small set of risky patterns and writes findings to Studio's Output.
- Optionally creates three idempotent folders: `ServerStorage/MultiScript`, `ReplicatedStorage/MultiScript`, and `TestService/MultiScriptTests`.

The plugin does not execute AI tool calls, upload project source, expose the bridge to the network, or manufacture undocumented Roblox tools. The native 60+ tool catalogue is owned by Roblox's official Studio MCP server.

## Install

1. Start Multi-Script with `start.bat` or `MacOS_Start.command`.
2. Copy `roblox-plugin/MultiScriptCompanion.server.lua` into the local Roblox Studio Plugins folder.
   - Windows: `%LOCALAPPDATA%/Roblox/Plugins/`
   - macOS: `~/Documents/Roblox/Plugins/`
3. Restart Roblox Studio.
4. Open a place.
5. In Studio, enable **Game Settings → Security → Allow HTTP Requests** so the plugin can read the loopback status endpoint.
6. Enable **Assistant Settings → MCP Servers → Studio as MCP Server**.
7. Open **Plugins → Multi-Script Companion**.

The bridge status service listens only on `127.0.0.1:17614` by default. Override it with `ZS_PLUGIN_PORT`; if you do, update `BRIDGE_URL` near the top of the plugin source too.

## Security model

- Loopback bind only; other computers cannot connect.
- `GET /status` returns versions, counts, and connection state.
- `POST /plugin/heartbeat` accepts at most 64 KiB and stores only allowlisted metadata.
- Tool execution remains on the existing browser-extension WebSocket path.
- No API keys, script source, or generated assets are accepted by the plugin endpoint.

## Troubleshooting

- **Bridge offline or HTTP Requests disabled:** start Multi-Script and enable HTTP Requests in Studio.
- **Proxy running, Studio not connected:** open a place and toggle Studio as MCP Server off and on.
- **Zero native tools:** wait up to 10 seconds, then use **Refresh live status**. If still zero, restart the bridge and toggle the official MCP setting.
- **Plugin missing:** confirm the `.server.lua` file is directly inside the local Plugins folder, then restart Studio.
