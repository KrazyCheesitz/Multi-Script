# Install Multi-Script 5.3.1

## Requirements
- Windows 10/11 or macOS/Linux
- Python 3.9+
- Chromium-based browser with unpacked extensions enabled
- At least one supported editor/MCP connection

## 1. Extract everything
Do not run the launcher from inside the ZIP preview. Extract the complete `Multi-Script` folder first.

## 2. Start the local bridge
- Windows: double-click `start.bat`.
- macOS: control-click `MacOS_Start.command`, choose Open, and approve it once.
- Linux: run `bash MacOS_Start.command`.

Keep the terminal open. The bridge binds only to `127.0.0.1:17613`.

## 3. Load the browser extension
1. Open `chrome://extensions` or `edge://extensions`.
2. Enable **Developer mode**.
3. Choose **Load unpacked**.
4. Select the extracted `Multi-Script/extension` folder.
5. Pin Multi-Script to the toolbar.

For store submission, upload `Multi-Script-Extension-5.3.1.zip`; users installing the store build still need the local bridge package.

## 4. Connect an editor
Open Roblox Studio, Unity, or Godot with its MCP integration enabled. Multi-Script's Engines tab distinguishes a running server from an attached editor.

## 5. Verify
Open a supported AI site, open Multi-Script, and confirm the bridge is online. On Notion AI, use **Check selection** before starting a new chat.

## Updating
Stop the bridge, replace the extracted folder, reload the extension, then restart the bridge. Keep a backup of `runtime/config.json` if you added custom servers.

## Optional ElevenLabs audio setup

1. Create an ElevenLabs API key.
2. Run `python runtime/configure_elevenlabs.py`.
3. Paste the key into the hidden terminal prompt.
4. Restart the bridge.
5. Ask the AI to create a sound effect or ambience. Multi-Script generates the file directly and imports it through the connected engine workflow.

The key remains local and must not be committed or pasted into chat. ElevenLabs quota and licensing terms apply.
