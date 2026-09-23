# Privacy

Multi-Script is a local bridge and browser extension.

## Data it handles
- Text sent to and received from supported AI chat pages
- MCP commands and results needed to operate connected local editors
- Local preferences such as menu appearance, provider modes, and Notion Auto routing choice
- Local diagnostic status such as server health and advertised tool counts

## Storage and transport
- Preferences use browser extension local storage.
- The bridge listens on loopback only (`127.0.0.1`) and writes local diagnostic logs.
- Multi-Script does not include its own analytics or telemetry service.
- Content sent to an AI provider remains subject to that provider's privacy policy.
- Third-party MCP servers may have their own network and telemetry behavior. Review them before enabling them.

## Permissions
Host permissions are limited to the supported AI sites plus localhost for the bridge. The extension uses `storage` to retain settings.

## Sensitive information
Do not place passwords, API keys, tokens, or private keys in prompts, screenshots, bug reports, or `runtime/config.json`. Use environment variables supported by the relevant MCP server.
