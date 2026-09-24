# Roblox Studio companion plugin

Multi-Script 6.13.0 includes a diagnostic-only companion. Roblox Studio’s official live MCP server remains the sole native execution path.

## Improvements
- Bridge/plugin version compatibility and official native-tool count.
- Bounded sanitized heartbeat and readiness score.
- Yielding, non-destructive high/medium/low heuristic risk scan.
- Script, module, remote, animation, audio, particle and UI inventory.
- Selection summary and sanitized JSON report in Output.
- Optional idempotent project folders.

Enable **Game Settings → Security → Allow HTTP Requests** and **Assistant Settings → MCP Servers → Studio as MCP Server**. Copy the Lua file to `%LOCALAPPDATA%/Roblox/Plugins/` on Windows or `~/Documents/Roblox/Plugins/` on macOS.

Only `GET /status`, `GET /catalog`, and `POST /plugin/heartbeat` are allowed. There is no job polling, result endpoint, custom Roblox command layer, source upload, or secret handling. Findings are review prompts, not proof of bugs.
