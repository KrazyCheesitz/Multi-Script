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
