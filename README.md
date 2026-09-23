# Multi-Script 5.3.1

Multi-Script connects supported AI chats to Roblox Studio, Unity, Godot, Blender, Figma, and other MCP tools through a local bridge.

You describe what you want to build. The AI can inspect the connected project, use the available tools, make changes, and verify the result.

## What is included

- Browser extension for Notion AI, ChatGPT, DeepSeek, Gemini, Kimi, GLM, Qwen, Arena, and Meta AI
- Local multi-engine MCP bridge
- Built-in Roblox, Unity, and Godot launchers
- Support for Blender, Figma, Unreal, and other stdio MCP servers
- 500 reusable professional game-development skills, including 194 detailed Roblox/Unity/Godot production workflows
- Canvas/SVG UI and texture creation
- Per-provider prompt, validation, speed, and creative-routing settings

---


## Prompt-only Notion routing

Choose GPT-5.6 Sol, Claude Opus 5, Kimi K3, or future Claude Opus 5.5 from Multi-Script's own startup profile picker. On each new chat, Multi-Script sends one focused routing block before the shared system prompt. It never clicks Notion's direct model selector, making this compatible with Business Trials locked to Auto. Routing remains best-effort because only Notion controls the private backend model.

## Future-ready Claude Opus 5.5 profile

Anthropic released Claude Opus 5.5 on September 22, 2026. Notion had not announced it at validation time, so Multi-Script includes an honest future-ready profile: select **Claude Opus 5.5** under **Notion Auto preferred model**. It requests the real model once at the start of each new chat when Notion makes it available; until then it falls back to Standard Auto without imitation or false identity claims.

## Automatic ElevenLabs sound generation

When a user asks Multi-Script to create a sound effect, ambience, Foley, UI sound, audio loop, or musical element, the bridge can call ElevenLabs directly, save the real audio locally, and hand it to Roblox Studio, Unity, or Godot for import and runtime testing. The user does not need to open ElevenLabs for each request.

One-time setup:

```bash
python runtime/configure_elevenlabs.py
```

Enter the API key in the hidden terminal prompt and restart the bridge. The key stays in `runtime/.env` or the `ELEVENLABS_API_KEY` environment variable and is never stored in the browser extension. ElevenLabs account quota and licensing still apply.

## 200 engine-specific virtual tools

Multi-Script includes 50 virtual tools each for Roblox Studio, Unity, Godot, and Blender. They cover UI, animation, textures, graphics, shaders, lighting, VFX, models, rigging, audio, gameplay, networking, saves, optimization, testing, and production. Automatic mode silently matches the exact prompt to the strongest tools, uses their engine-specific expertise to improve the work, and then immediately performs the actual MCP changes.

Four compact gateway commands list, match, inspect, and run the catalog without wasting model context on 200 separate schemas. The internal augmentation stays hidden, explicit prompt details remain binding, and the real engine artifact remains the deliverable.

## Automatic capability stacks

Skills are an invisible quality multiplier. The user asks normally and Multi-Script performs the task normally; relevant techniques silently improve the design, implementation, polish, accessibility, performance, and testing. It does not make the user watch a skill-selection workflow. Complex orchestration remains available only when it genuinely helps. The real project result—not the skill call—is the deliverable.

Dedicated professional pipelines cover animation, textures and materials, art direction, UI/UX, VFX, and audio. Every one of the 800 skills is linked to at least four collaborators, producing 2,000 validated skill relationships.

## Professional handling of vague prompts

Multi-Script does not require the user to write a perfect specification. If a request is short, vague, or poorly structured, the agent uses `ms_prompt_rescue` to preserve the intent, infer conservative project-aware defaults, establish acceptance criteria, and create the smallest polished end-to-end result. It asks only when a decision is destructive, irreversible, paid, credentialed, or genuinely contradictory.

For larger requests, virtual tools cover game blueprints, vertical slices, system design, balance, multiplayer authority, save migrations, performance budgets, accessibility, content pipelines, playtests, and strict definition-of-done gates. These complement the 500 engine workflows rather than replacing real implementation in Roblox Studio, Unity, or Godot.

# Step-by-step installation

## Step 1 — Extract the complete ZIP

1. Download `Multi-Script-5.3.1.zip`.
2. Right-click it and choose **Extract All**.
3. Open the extracted `Multi-Script` folder.

Do not run Multi-Script from inside the ZIP preview. The launcher needs the complete folder structure.

The folder should contain:

```text
Multi-Script/
├── start.bat
├── MacOS_Start.command
├── extension/
├── runtime/
├── docs/
├── assets/
└── README.md
```

## Step 2 — Install Python

Multi-Script requires Python 3.9 or newer.

### Windows

The Windows launcher can offer to install Python through `winget` if it is missing. You may also install Python from [python.org](https://www.python.org/downloads/). During installation, enable **Add Python to PATH**.

### macOS or Linux

Install Python 3.9 or newer, then confirm it works:

```bash
python3 --version
```

## Step 3 — Start the Multi-Script bridge

### Windows

Double-click:

```text
start.bat
```

### macOS

1. Control-click `MacOS_Start.command`.
2. Choose **Open**.
3. If macOS blocks it, open **System Settings → Privacy & Security** and choose **Open Anyway**.

### Linux

Run:

```bash
bash MacOS_Start.command
```

Keep the terminal window open while using Multi-Script. You may minimize it.

A successful startup displays:

```text
Multi-Script Bridge v5.3.1
```

The bridge listens locally on `127.0.0.1:17613`.

## Step 4 — Install the browser extension

Use Chrome, Edge, Brave, or another compatible Chromium browser.

1. Open `chrome://extensions` or `edge://extensions`.
2. Enable **Developer mode**.
3. Click **Load unpacked**.
4. Select the extracted `Multi-Script/extension` folder.
5. Pin Multi-Script to the browser toolbar.

Do not select the ZIP itself and do not select the outer folder. Select the folder named `extension`.

## Step 5 — Connect Roblox Studio

1. Open Roblox Studio.
2. Open the place you want to edit.
3. Open **Assistant AI**.
4. Open the Assistant menu (`…`).
5. Choose **Manage MCP Servers** or **Assistant Settings → MCP Servers**.
6. Enable **Studio as MCP Server**.
7. Return to the bridge terminal and confirm Roblox Studio is connected.

If the terminal says Studio is open but not connected, reopen Roblox Assistant's MCP settings or toggle its MCP server off and on.

## Step 6 — Optional: connect Unity

Multi-Script is configured for CoplayDev Unity MCP v10.2.0.

1. Install Unity 2021.3 LTS or newer.
2. Install Python 3.10+ and `uv`/`uvx`.
3. In Unity Package Manager, choose **Add package from git URL**.
4. Enter:

```text
https://github.com/CoplayDev/unity-mcp.git?path=/MCPForUnity#v10.2.0
```

5. Open **Window → MCP for Unity**.
6. Keep the Unity project open.
7. Restart the Multi-Script bridge.
8. Open Multi-Script's **Engines** tab and confirm Unity is connected.

## Step 7 — Optional: connect Godot

The bundled launcher uses `@coding-solo/godot-mcp@0.1.1`.

1. Install Godot.
2. Install Node.js 18 or newer.
3. Restart the Multi-Script bridge.
4. Open the Godot project containing `project.godot`.
5. Check the **Engines** tab for the Godot server.

If Godot cannot be detected, configure `GODOT_PATH` in `runtime/config.json`.

## Step 8 — Optional: connect Blender, Figma, Unreal, or another MCP

Only add MCP servers you trust.

1. Install the desired MCP server from its official documentation.
2. Copy its exact stdio start command.
3. Open Multi-Script → **Engines**.
4. Enter a name and the exact start command.
5. Click **Add addon**.
6. Restart MCP servers from the same menu.

You can also use:

```bash
python runtime/configure_engine.py blender --command "<official stdio command>"
```

Replace `blender` with `unreal`, `unity`, `godot`, or `custom` when appropriate.

## Step 9 — Open a supported AI provider

Open a new chat on one of these supported sites:

- Notion AI
- ChatGPT
- DeepSeek
- Gemini
- Kimi
- GLM / Z.ai
- Qwen
- Arena
- Meta AI

The Multi-Script bar should appear near the chat input.

For Arena, use **Direct** mode.

## Step 10 — Configure Multi-Script

Open the Multi-Script menu and review:

### Agent

- **Prompt Skills** — literal, suggestions only, or automatic enhancement
- **Production Checks** — Fast, Balanced, or Rigorous
- **Creative Output** — Auto, direct engine, Figma first, or Canvas/SVG first
- **Quality Amplifier** — Off, Polished, or Ambitious
- **Usage Optimizer** — Off, Balanced, or Compact

Settings are saved independently for each AI provider.

### Engines

Confirm that the bridge, MCP server, and editor are connected. A running MCP process does not always mean its editor is attached.

### AI sites

Switch providers and view the general Speed, Smart, and Stability estimates.

### Help

Run **Test provider** or **Copy diagnostics** when troubleshooting.

## Step 11 — Notion AI setup

1. Open a new empty Notion AI chat.
2. Open Multi-Script's menu.
3. Under **Notion Auto preferred model**, choose:
   - Claude Opus 5
   - Kimi K3
   - GPT-5.6 Sol
   - Standard Auto
4. Click **Check selection**.
5. Click **Start**.

The preferred-model request is placed first, followed by the complete shared Multi-Script skills and tools prompt. It is sent once per new chat.

Notion controls its hidden backend routing. Multi-Script can request an eligible model but cannot prove or force Notion's private selection.

## Step 12 — Start building

Click **Start** in a new chat, wait for the ready message, and then describe the result you want.

Examples:

```text
Build a polished responsive Roblox shop UI with controller and touch support.
```

```text
Create a Blender prop, UV it, export it, import it into Roblox, and validate its scale and materials.
```

```text
Design this flow directly in Roblox Studio unless a Figma prototype would improve the result.
```

---

# Updating Multi-Script

1. Stop the old bridge.
2. Back up `runtime/config.json` if you added custom servers.
3. Extract the new release into a fresh folder.
4. Restore only your trusted custom MCP entries.
5. Reload Multi-Script on the browser's extensions page.
6. Restart the bridge.
7. Reload any open AI tabs.

# Troubleshooting

## The Multi-Script bar does not appear

- Confirm the extension is enabled.
- Reload the AI website.
- Confirm the URL is a supported host.
- On Notion, open an actual Notion AI chat rather than a normal page editor.

## Bridge offline

- Run `start.bat` or `MacOS_Start.command`.
- Keep the terminal open.
- Confirm another application is not blocking local port 17613.

## Roblox Studio is open but disconnected

- Load a place.
- Reopen Assistant MCP settings.
- Toggle Studio's MCP server off and on.
- Restart the Multi-Script bridge.

## Unity or Godot server is running but the editor is unavailable

- Confirm the editor project is open.
- Check that the editor-side MCP plugin is installed and enabled.
- Use **Engines → Refresh status**.
- Review the bridge terminal for the first actionable error.

## Provider is responding without using tools

- Confirm the session was started in a new chat.
- Check that Prompt Skills are not disabled unintentionally.
- Try Balanced production checks.
- Start a fresh session if the provider lost long-conversation context.

# Privacy and security

- The bridge binds to localhost only.
- Multi-Script stores settings locally in the browser.
- Multi-Script does not include its own analytics service.
- AI providers and third-party MCP servers have separate privacy policies.
- Never place API keys, passwords, tokens, or private keys in prompts or bug reports.
- Use source control and backups before allowing automated project changes.

Read:

- [`docs/PRIVACY.md`](docs/PRIVACY.md)
- [`docs/SECURITY.md`](docs/SECURITY.md)
- [`docs/SUPPORT.md`](docs/SUPPORT.md)
- [`docs/THIRD_PARTY_NOTICES.md`](docs/THIRD_PARTY_NOTICES.md)

# Folder guide

```text
Multi-Script/
├── README.md                    # this setup guide
├── start.bat                   # Windows launcher
├── MacOS_Start.command         # macOS/Linux launcher
├── extension/                  # browser extension
├── runtime/                    # bridge, engine launchers, config, skills
├── roblox-plugin/              # optional Studio companion plugin
├── docs/                       # installation, privacy, security, QA
├── assets/                     # product artwork
├── tools/                      # release validation and packaging
├── tests/                      # regression tests
└── vendor/                     # attributed third-party references
```

# Release verification

Maintainers can run:

```bash
python tools/release_check.py
```

The release also includes SHA-256 checksums and a machine-readable release manifest.

# License

Multi-Script is released under GPL-3.0-or-later. Third-party integrations retain their original licenses. See `LICENSE` and `docs/THIRD_PARTY_NOTICES.md`.


## Optional Roblox Studio companion plugin

`roblox-plugin/MultiScriptCompanion.server.lua` is a local Studio plugin that makes the official Studio MCP connection and native tool count visible inside Studio. It also provides a read-only project risk scan, a live bridge heartbeat, and an optional idempotent project workspace.

It does **not** replace Roblox's official Studio MCP server or claim to create undocumented native commands. The 60+ native Roblox tools still come from **Assistant Settings → MCP Servers → Enable Studio as MCP server**. See `docs/ROBLOX_PLUGIN.md` for installation.

## 151 direct Multi-Script tools

Version 5.3.1 includes 151 direct production commands for camera, input, controllers, combat, AI, quests, inventory/economy, procedural generation, shaders, lighting, environments, characters, rigging, localization, telemetry, live operations, store compliance, security, bug triage, and regression planning. Each tool produces an engine-ready execution contract and requires real MCP implementation plus verification.

## Public-release provider QA

Multi-Script tests every direct built-in tool call across all nine provider adapters. Arena runs in **Direct** mode. If Arena presents a CAPTCHA or bot check, Multi-Script hides its overlays and pauses so you can complete the challenge manually; it automatically continues once verification clears. Multi-Script never bypasses anti-bot protections.

