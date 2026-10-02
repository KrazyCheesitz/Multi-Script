# Security policy

## Supported version
Security fixes are provided for the latest published Multi-Script release.

## Reporting
Report security issues privately to the release maintainer. Do not open a public issue containing credentials, private workspace content, exploit details, or unredacted logs. Include the Multi-Script version, operating system, browser, affected provider/engine, reproduction steps, and sanitized diagnostics.

## Trust boundaries
- The bridge binds to loopback only. Do not expose port 17613 to a LAN or the public internet.
- Multi-Script can modify connected projects. Use source control and backups.
- Only install MCP servers from sources you trust. Review commands, environment variables, and licenses.
- Generated files and AI changes require normal code review and testing.
- Notion Auto routing requests cannot prove the hidden backend model identity.

## Roblox companion execution
- Companion execution is Off by default and must be enabled inside Roblox Studio for each session.
- Read, Project, and Full tiers limit tool exposure; the bridge and plugin both enforce the required tier.
- The loopback plugin session uses an ephemeral in-memory bearer token. Jobs cannot be enqueued through HTTP.
- The companion exposes only curated handlers and never evaluates arbitrary Lua. Full access can edit source and delete instances, so use it only for trusted prompts and keep source control enabled.
- Disable elevated tools in the widget when finished. Tokens are invalidated on disable or plugin unload.
