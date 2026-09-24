# Release checklist

## Code and QA
- [ ] Set bridge, extension, adapter, changelog, and release-manifest versions
- [ ] Run `python tools/release_check.py`
- [ ] Run parser, provider, bridge, MCP, launcher, and browser fixture tests
- [ ] Confirm no generated files, logs, secrets, or credentials are packaged
- [ ] Test a clean install from the ZIP on Windows and one Chromium browser

## Product
- [ ] Verify bridge offline/online, editor attached/detached, and recovery states
- [ ] Verify provider settings persist independently
- [ ] Verify Notion routing request is sent once in a new chat
- [ ] Verify Fast mode disables optional loops
- [ ] Verify Canvas/Figma fallback and direct-engine routing

## Distribution
- [ ] Review INSTALL, PRIVACY, SECURITY, SUPPORT, LICENSE, and third-party notices
- [ ] Generate full and extension-only ZIPs
- [ ] Publish SHA-256 checksums
- [ ] Upload screenshots, store description, privacy disclosure, and release notes
- [ ] Tag `v6.8.0` only after smoke testing the exact uploaded archives
