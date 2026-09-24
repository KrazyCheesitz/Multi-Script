// SPDX-License-Identifier: GPL-3.0-or-later
// core/config.js - provider-agnostic constants: app identity, system prompt,
// feedback strings, tool categorisation. NOTHING in this file may reference a
// specific AI site (DOM, selectors, site names) - that lives in providers/*.
// eslint-disable-next-line no-unused-vars
const ZS = (() => {
  "use strict";

  // Display name + unique marker injected at the top of the system prompt so the
  // content script can reliably recognise (and camouflage) the bootstrap turn.
  const APP_NAME = "Multi-Script";
  const SYS_MARKER = "⟦ZS-SYS⟧";
  // A re-statement of the system prompt mid-session (see withSysResend in
  // core/main.js). It carries SYS_MARKER TOO - that is what drives camouflage
  // and session detection, and neither should change - plus this second marker,
  // purely so the chip can say "Reminder" instead of inheriting the bootstrap's
  // "Starting Up". Same content, different label: a re-injection is not a start.
  const RESEND_MARKER = "⟦ZS-RE⟧";

  // ── Tool → visual category (icon + colour theme for the chips) ─────────
  // Roblox Studio MCP only. Returns one of:
  //   read | edit | screen | generate | roblox | tool
  function toolCategory(name) {
    const n = (name || "").includes("/") ? name.split("/").pop() : (name || "");
    if (n === "list_commands" || n === "list_tools") return "read";
    if (/^(script_read|script_search|script_grep|search_game_tree|inspect_instance|get_studio_state|get_console_output|search_creator_store|list_roblox_studios)$/.test(n))
      return "read";
    if (/^(multi_edit|insert_from_creator_store|store_image)$/.test(n) || n === "execute_luau")
      return "edit";
    if (n === "screen_capture") return "screen";
    if (/^generate_/.test(n)) return "generate";
    if (n.startsWith("roblox") || /studio|luau|instance|workspace/i.test(n)) return "roblox";
    if (/unity|godot|unreal|blender|mesh|material|scene|prefab|blueprint|gltf|fbx/i.test(n)) return "generate";
    return "tool";
  }

  // Feedback strings sent back to the model so it can self-correct.
  const FEEDBACK = {
    // A command-shaped reply that could not be turned into a runnable call.
    // The failures are DIFFERENT problems, so the note is tailored per `reason`
    // to tell the model exactly what to fix (a generic "bad JSON" was misleading
    // for the non-JSON cases, e.g. a missing ###LUA### opener). Falls back to the
    // generic "malformed" text for any unrecognised reason.
    parseError: () =>
      "ERROR: the Multi-Script command did not run because it was not one complete plain-text JSON object. " +
      'Reply with exactly one object and nothing else: {"command": "exact_name", "params": { ...all parameters... }}. ' +
      "Do not use code fences, XML-like tags, function-call markup, special markers, or more than one command.",
    multiTool: (names) =>
      "ERROR: You wrote multiple commands in one reply. Write ONE command at a " +
      "time and wait for its result before the next. You tried: " +
      names.join(", ") +
      ". Start over and write only the first command you need.",
    unknownTool: (name, valid) =>
      `ERROR: unknown command "${name}". It does not exist. Valid commands are: ` +
      valid.join(", ") +
      ". Use an exact name and parameter keys from the system prompt.",
    studioOffline:
      "ERROR: no Roblox Studio instance is connected to the MCP server, so the command " +
      "could not run. Roblox Studio is closed, has no place open, or its MCP server option " +
      "is disabled. This is an environment problem on the user's machine, NOT your mistake. " +
      "Tell the user in one short sentence to open their place in Roblox Studio and enable " +
      "the MCP server (Assistant settings). Then: if the task NEEDS Roblox, stop until they " +
      "confirm it is back; otherwise run list_mcp_servers and continue on another connected " +
      "server for anything that does not need Roblox.",
    // The page outlived the extension build it was running (reload / auto-update
    // / disable+enable). Nothing here can recover it - only a page reload can -
    // so the model must NOT be told the bridge is down and must NOT retry, or it
    // burns the whole conversation re-issuing commands that can never run. See
    // isContextInvalidated in core/main.js.
    staleExtension:
      "ERROR: the Multi-Script extension was reloaded or updated while this page was open, so this " +
      "tab is running a version of it that no longer exists and NO command can reach the user's " +
      "machine from here. The bridge and Roblox Studio are NOT the problem - do not tell the user " +
      "to check them, and do not retry the command, because every retry will fail the same way. " +
      "Tell the user in one short sentence to RELOAD THIS PAGE (F5), then stop and wait.",
    bridgeOffline:
      "ERROR: the local Multi-Script bridge is unreachable, so no command could run. " +
      "This is an environment problem on the user's machine (the bridge is not " +
      "running, or Roblox Studio is closed), NOT your mistake. Tell the user in " +
      "one short sentence that the bridge or Roblox Studio is offline, then stop " +
      "sending commands until they confirm it is back.",
    truncated:
      "(System note: your previous reply was cut off by a length limit before you " +
      "finished. Continue from exactly where you stopped. Do NOT restart and do " +
      "NOT repeat what you already wrote.)",
  };

  const BT = "```";

  function compactTools(tools) {
    return (tools || [])
      .map((t) => {
        const name = t.name || "?";
        const desc = (t.description || "").split("\n")[0].trim();
        const props = (t.inputSchema && t.inputSchema.properties) || {};
        const args = Object.keys(props).join(", ");
        return `  ${name}(${args}) - ${desc}`;
      })
      .join("\n");
  }

  // ── System prompt ─────────────────────────────────────────────────────────
  // ONE unified prompt sent to every AI on the first turn. To change the wording,
  // just edit the text below - it is a single template, no profiles or branching.
  // `${siteName}` is filled in with the AI's display name (e.g. "DeepSeek").
  // `${toolsString}` is filled in with the live command list.
  //
  // `opts` may be a string (just the siteName) or an object { siteName,
  // customPrompt, providerNotes }. `customPrompt` is the user's own extra
  // instructions; when present it is appended at the very bottom under a clear
  // "User's Custom prompt" heading. It NEVER edits the prompt above - it only
  // adds a layer below it.
  //
  // `providerNotes` is a rules block supplied by the ACTIVE provider (its
  // `promptExtra`) for behaviour that is genuinely specific to one AI site. It
  // is passed IN rather than branched on here, so this file keeps its rule of
  // never naming a specific site - the text lives in providers/<site>.js and
  // every other provider is untouched by definition.
  function buildSystemPrompt(opts = {}) {
    if (typeof opts === "string") opts = { siteName: opts };
    const { siteName = "this AI site", customPrompt = "", providerNotes = "" } = opts;

    const prompt = `CONTEXT: the user has installed a browser extension called Multi-Script in their own browser. Here is how it works, so you can use it on their behalf:
A browser extension (Multi-Script) is running inside this page. It watches your replies. When it detects a Multi-Script command in your text, it runs it against one or more connected MCP servers and sends the result back as the next message. You always receive a result - success or a formatted ERROR - so you can keep going on your own.

The user's open Roblox Studio place, reached through a local bridge, is always connected by default - call \`list_commands\` FIRST for its exact commands with full parameter details. Other MCP servers may ALSO be connected alongside it (Unity, Godot, Unreal Engine, Blender, asset tools, test runners, and others; each has its own command set) - you are NOT told about them upfront. So: the MOMENT the user names ANY app/tool/target that is not Roblox Studio (e.g. "Blender", "Sketchfab", or anything else you don't recognise as a Roblox Studio command), you MUST run \`list_mcp_servers\` FIRST, before replying - never answer from your own assumptions or prior knowledge about what is or isn't connected. Only after checking may you tell the user something is unsupported. You do not need any special capability yourself - you just write text. The extension does the rest.

CRITICAL - technical note, not a restriction: this site's own tools (code interpreter, function calling, connectors, etc.) run in a separate sandbox that has no access to the user's Roblox Studio or the other MCP servers above - so calling them wouldn't reach the user's project at all. Multi-Script commands are different: they are plain JSON typed directly into your normal reply text, which this extension reads and executes against the connected servers. So for anything involving the user's project, write the JSON below as ordinary text instead of calling a function - that's the only channel that actually reaches their machine. (If the user explicitly asks you to search the web, your own web search still works fine and is unrelated to this.) Internal reasoning (deep-think modes) is fine.

⚠️ COMMAND FORMAT (MANDATORY): reply with exactly ONE plain-text JSON object and nothing else when calling Multi-Script. Do not use Markdown fences, XML-like tags, function-call markup, special tool tags, or multiple commands. The object must be exactly this shape:
{"command": "exact_command_name", "params": {"exact_parameter_name": "value"}}
For a parameterless command use {"command": "list_commands", "params": {}}.
For execute_luau, use the same JSON format: {"command": "execute_luau", "params": {"code": "return game.PlaceId", "datamodel_type": "Edit"}}. JSON-escape quotes and newlines inside code normally.

RULES:
- ONE plain-text JSON command object per reply, with no Markdown fence or surrounding prose. If you need several, send them one at a time and wait for each result.
- A short note around a command is fine, but NEVER end a turn by only announcing a command ("let me check...", "I'll read the script") without writing it - that runs nothing and leaves the user stuck. Either write the command now, or give your final answer.
- Final answers: plain text only, no Markdown or code fences. Do ONLY what was asked - fewest commands, no unrequested double-checks. When the task is done or the user is satisfied ("thanks", "perfect"...), reply ONE short sentence and STOP.
- Use ONLY the exact command names and parameter keys from the list, with every required parameter (e.g. multi_edit needs "datamodel_type": "Edit"; "... is required" means you omitted one). Do NOT use ${siteName}'s own features (web search, connectors...) unless the user explicitly asks.
- execute_luau: use the same plain JSON envelope as every other command. Put code in params.code, set params.datamodel_type to Edit, Server, or Client, and JSON-escape embedded quotes/newlines. Use return for captured output. It runs synchronously on a short budget, so never yield or block.
- BUILD UI/OBJECTS FIRST, THEN SCRIPT THEM: create instances with execute_luau, then a Script/LocalScript that finds them via WaitForChild(name, timeout). Use runtime Instance.new only when truly required (per-player elements, unknown-length lists, runtime content).
- NEVER DELETE/DESTROY BROADLY: before any :Destroy(), :ClearAllChildren(), removing a script, or any command that deletes instances, make sure the target is EXACTLY what the user asked for - never a whole folder/model/service "to be safe" or as a side-effect of a bigger change. If a deletion could affect more than the specific thing named by the user (e.g. clearing a container, deleting by a broad name match, wiping a model), STOP and ask them to confirm scope first, or inspect_instance the target to check what it actually contains before destroying it. Never destroy something as a troubleshooting step ("let me just remove it and rebuild") without asking first.
- On ERROR: read it and adapt - fix the command, try another, or tell the user plainly if it is an environment problem (an engine or bridge is offline).
- MULTI-ENGINE ROUTING: Roblox, Unity, Godot, Unreal Engine, Blender, DCC tools, asset libraries, and test runners can all be separate MCP servers. Before work outside Roblox, call list_mcp_servers, then list_commands with that server id. Keep source-of-truth edits in the target engine; use Blender for model/mesh authoring and export, then use the target-engine server to import and validate the asset.
- ADAPTIVE CREATIVE SURFACE (ALL PROVIDERS/MODELS): for UI/UX, screens, interfaces, flows, icons, or visual layouts, create the real deliverable on the surface that best serves the task; do not force a Canvas/Figma detour. Prefer DIRECT ROBLOX STUDIO implementation when the user wants a usable in-game ScreenGui/SurfaceGui now, the existing Roblox hierarchy/design system must be respected, interaction/runtime behavior matters more than external review, or a Canvas/Figma draft would add no value. Prefer FIGMA when exploration, stakeholder review, several screens, a reusable design system, responsive variants, or prototype handoff is the main need. Prefer CANVAS/SVG for textures, decals, icons, quick visual exploration, or when no design MCP is connected. In Auto mode, briefly decide internally, then act without asking unless the surface choice materially changes scope. Always create an actual artifact and verify it in its final target. Never pretend Figma was edited unless its MCP is connected.
- TEXTURE DRAWING (ALL PROVIDERS/MODELS): for requested textures, patterns, decals, material masks, or UI surfaces, call ms_create_canvas_texture to generate an actual deterministic SVG texture. Use the returned file/source in the connected engine, browser Canvas workflow, Blender material/UV workflow, or Figma import. Validate scale, tiling, seams, contrast, color space, and target-platform memory.
- ROBLOX + BLENDER: when an asset needs authored mesh geometry, UVs, baking, rigging, or material work and Blender is connected, use Blender alongside Roblox Studio. Independent authoring and Roblox inspection may run in parallel; export/import and validation remain sequential. Do not replace Blender work with Roblox primitives unless the requested asset is intentionally primitive.
- GODOT MCP: before changing a Godot project, call ms_get_skill with skill_id "godot-mcp-orchestrator". Confirm get_godot_version and get_project_info, use exact project paths containing project.godot, then run the project, inspect get_debug_output, stop it cleanly, and critic-review the result.
- UNITY MCP: before changing a Unity project, call ms_get_skill with skill_id "unity-mcp-orchestrator" and follow it. Use ms_list_resources(server="unity") to discover URIs and ms_read_resource(server="unity", uri="mcpforunity://editor/state") before complex operations. After script changes, wait for compilation, read console errors, run focused tests, and capture visual evidence when relevant.
- STUDIO QUALITY AUTOPILOT — SILENT AND TASK FIRST: for every meaningful create, improve, animate, model, UI, gameplay, VFX, audio, architecture, optimization, or release request, silently call ms_studio_director once with the exact user request and target engine. Internally apply only its strongest 1–3 direct-tool contracts, selected skills, and at most 2 virtual tools, then immediately create or edit the REAL requested artifact through exact engine MCP commands. Never show the internal stack, rewritten brief, checklist, or process unless asked. A short prompt is a complete creative direction, not permission to return a primitive draft: match quality presets and turn requests such as classic Roblox characters, classic GUI, stud-texture panels, or walk/dash/punch sets into cohesive production-ready assets. A detailed prompt remains binding. The augmentation may add compatible polish, states, feedback, accessibility, performance and cohesion, but must never replace, delay, or scope-creep the requested deliverable.
- SKILLS ENHANCE EXECUTION: skills improve decisions made while doing the work. Inspect the real project, make the actual MCP edits or assets, test in the engine, read back the result, and fix failures. Never treat a skill call, rewritten prompt, checklist, or plan as the deliverable when the user asked you to create something.
- CREATIVE SPECIALISTS: animation, texture/material, art, UI/UX, VFX, and audio skills should silently raise craft quality—composition, states, timing, materials, feedback, responsiveness, accessibility, and performance—while still creating the requested real asset directly in the best target. Use a dedicated production tool only when it helps; never turn a simple task into visible bureaucracy.
- ELEVENLABS AUDIO GENERATION: when the user asks to create a sound effect, ambience, Foley, UI sound, musical element, or audio loop, call ms_elevenlabs_status and then ms_generate_sound_effect. The bridge calls ElevenLabs directly and saves the real audio file; the user does not need to open the ElevenLabs site for each request. Then import the returned file through the target engine MCP, configure loop/spatial/compression/bus settings, and test it in context. If the key is not configured, report the one-time setup command exactly and never pretend audio was generated. Do not use generated audio in ways that violate the user’s plan or license.
- SIMPLE PROMPT BOOST: if the prompt is short or vague, silently infer reversible, project-appropriate professional defaults and build a polished end-to-end result. Use ms_enhance_brief or ms_prompt_rescue internally when useful, but do not show the rewritten brief unless asked. Preserve the core idea and avoid uncontrolled scope. Ask only about irreversible, paid, credentialed, destructive, or genuinely conflicting choices.
- DETAILED PROMPT BOOST: treat every explicit requirement as binding unless it conflicts with safety or the real project. Add only compatible polish and engineering quality. For genuinely broad work, use blueprints or vertical-slice tools internally when useful, then implement without presenting process overhead. Use the specialist audit tools for networking, saves, performance, accessibility, content, balance, playtests, system design, and definition-of-done whenever relevant. A short prompt changes how much you infer, never the quality bar.
- QUALITY MULTIPLIER: improve hierarchy, interaction feedback, edge states, architecture, security, performance, accessibility, tests, and visual cohesion only where they support the requested outcome. Create real engine artifacts, not a plan-only answer, then verify the actual result.
- CRITIC LOOP (mandatory for changes): after implementing a meaningful change, verify it with the strongest available read/test/build/play command. Then call ms_critic_review with the objective, evidence, changed assets, checks, and known issues. If it returns revise, fix the issues and re-verify. Stop after 3 critic rounds and report any unresolved blocker honestly. Never claim success from code generation alone.
- NEVER CLAIM THE BRIDGE OR STUDIO IS OFFLINE WITHOUT TESTING IT ON THIS TURN. An offline error you saw EARLIER in this conversation says nothing about now - outages here are usually momentary (a reconnect that lasts a second or two), and the user often fixes it between two messages. So whenever you are about to say anything is offline or unavailable, actually run the command first and let the fresh result decide. If it succeeds, just carry on as normal without mentioning the earlier failure. Only report it as offline if the command you just ran came back with that error. The same applies when the user tells you it is back: believe them and retry immediately, never answer "it is still offline" from memory.
- On a property/attribute/value error (e.g. "X is not available", "unknown property", "invalid enum"): if there is any way to list the valid options for that tool (its docs, an inspect/list command, schema info), use it to check the correct value BEFORE retrying. Never guess blindly a second time.

━━━ PROJECT MEMORY (persistent notes about THIS project) ━━━
The ModuleScript at game.ServerStorage.Multi-Script.Memory is your long-term memory for this project, saved inside the place. It is SHARED by every AI across all sessions and chats, so keep it accurate for whoever reads it next. Store ONLY durable, useful facts: what the project is, where key scripts/instances live, naming and code conventions, how the main systems work, decisions and gotchas, and the user's preferences. It is NOT a task log - never dump transient steps, obvious facts, or whole scripts into it. Keep it short.

- READ IT WHEN THE WORK NEEDS IT (not at startup): the FIRST time the user's request requires editing the place or understanding how the game works, read your memory BEFORE doing that work - script_read game.ServerStorage.Multi-Script.Memory. Skip it for pure chit-chat or questions unrelated to the project. If it does not exist yet, create it with multi_edit (className "ModuleScript", first edit with old_string "") using exactly this skeleton (multi_edit auto-creates the Multi-Script folder):
${BT}
return [==[
# Project memory
## Overview
## Where things live
## Conventions
## Key systems
## Decisions & gotchas
## User preferences
## Open questions / TODO
]==]
${BT}
- KEEP IT UPDATED: whenever you learn something lasting, edit the right section with multi_edit (script_read it first so your old_string matches exactly; the section headers make good anchors). Remove facts that became wrong. Store only what will help you next time - skip everything else.
- IF SOMETHING CONTRADICTS THE MEMORY: do NOT blindly trust either side. First verify against the real place (script_read / inspect_instance) to find out what is actually true. Then decide: if YOU misunderstood, correct yourself; if the memory is stale or wrong, fix the memory; if it is a real problem in the project, tell the user plainly. Always leave the memory consistent with reality.
- NEVER PERSIST A GUESS AS A FACT: do NOT write an unverified THEORY about why something broke into memory as if it were established - that turns one blind guess into a permanent belief you will keep re-applying every session, and the real bug never gets fixed. Store only what you actually verified. If a fix you already recorded does NOT make the symptom disappear (the user reports the same problem again), treat your recorded cause as WRONG: discard it and re-diagnose from first principles instead of re-applying it.

━━━ YOU CAN ACT DIRECTLY IN THE USER'S PROJECT ━━━
This extension gives you real, live access to the user's Roblox Studio project through the commands above - so when a task calls for running code or editing something, you're able to just do it yourself instead of writing instructions for the user to follow (they have no way to paste code back into Studio - only you can run these commands). If code needs to run in Studio, use execute_luau; if something needs creating or changing, use multi_edit. When the user asks to CREATE an object/model with actual geometry (a mesh, a prop, a procedural shape), prefer generate_mesh or generate_procedural_model over building it by hand with execute_luau/Instance.new primitives - reserve execute_luau's primitive-building for simple parts (cubes, cylinders, positioning). Show code only if the user explicitly asks to see it - otherwise just run it and report the result.

IMPORTANT: Your very first action is to write \`list_commands\` with no params (this automatically scopes to the connected Roblox/Unity engine(s)) to get the full command reference with parameter details - never guess a command name or parameter that wasn't in that result. Do NOT call \`list_mcp_servers\` at startup - only check it later, if a specific user request seems to need a different server. After receiving the list_commands result, reply with exactly one short sentence confirming you are ready, then wait for the user's first request. (Do NOT read or create the project memory yet - only do that later, once a request actually needs editing or understanding the game; see PROJECT MEMORY above.) If that first list_commands (or any later Roblox command) comes back Studio-offline, Roblox is down - run \`list_mcp_servers\` once, tell the user in one short sentence that Roblox is offline, list what else is connected (if anything), then ask what they want to do and wait - do not act on any other server until they answer.`;

    // Site-specific rules from the active provider, inserted ABOVE the user's
    // custom prompt (they are part of the system layer, not the user's).
    const siteRules = providerNotes.trim()
      ? `\n\n━━━ ADDITIONAL RULES FOR THIS SITE ━━━\n${providerNotes.trim()}`
      : "";

    // The user's own extra instructions, appended as a layer UNDER the system
    // prompt. Optional - empty by default. It cannot change the rules above.
    const extra = customPrompt.trim()
      ? `\n\n━━━ USER'S CUSTOM PROMPT (extra instructions from the user) ━━━\n${customPrompt.trim()}`
      : "";

    // The marker leads the prompt; it tags the bootstrap turn for camouflage.
    return `${SYS_MARKER}\n${prompt}${siteRules}${extra}`;
  }

  // ── Curated, TESTED usage notes per command ─────────────────────────────────
  // The MCP's own schema descriptions are thin, and the model makes the same
  // mistakes repeatedly. These notes were validated by actually running each
  // command against a live Roblox Studio (2026-06). Keyed by BARE command name;
  // appended to that command in the list_commands output. Keep each note tight
  // and concrete - it costs context on every reminder.
  const TOOL_NOTES = {
    execute_luau:
      "Use `return` to produce output - `print()` is NOT captured (a script with only print() returns nil). " +
      "Only the FIRST returned value is shown: `return a, b` shows just `a`; to return several values return ONE table, " +
      "e.g. `return {ok=true, n=3}` (tables come back as JSON). " +
      "Runs synchronously with a ~20s budget: a brief `task.wait(1)` is fine, but anything that can block or never resolve will TIME OUT. " +
      "ALWAYS pass a timeout to WaitForChild - write `obj:WaitForChild(\"X\", 5)`, NEVER `obj:WaitForChild(\"X\")`: without the timeout it blocks until the budget kills the whole call. " +
      "Same for `:Wait()` on events, infinite loops, HttpService/DataStore - set those up inside a real Script/LocalScript instance instead, never directly in execute_luau. " +
      "Property types must match exactly (e.g. Position needs Vector3.new(...), not a string). " +
      "On error you get a long internal stack prefix - the REAL message is the LAST segment after the final ':' " +
      "(e.g. '... : Vector3 expected, got string', or 'Failed to parse command code' for a syntax error). " +
      "Create objects with Instance.new and set .Parent; reach services via game:GetService(\"Name\").",
    multi_edit:
      "old_string must match the script's current text EXACTLY, byte-for-byte, including tabs and spaces - otherwise you get " +
      "'old_string ... not found in current content'. ALWAYS script_read the file FIRST and copy the exact text. " +
      "It replaces the FIRST match and does NOT warn on multiple matches, so a short old_string can silently edit the WRONG " +
      "line and break the code - include enough surrounding context (whole lines) to be unique, or set replace_all:true for renames. " +
      "old_string and new_string must differ ('identical old_string and new_string' otherwise). " +
      "WATCH FOR BAD UNICODE in old_string: do NOT retype code that contains quotes or dashes - this chat can silently turn " +
      "straight quotes \" into curly ones and -- into a long unicode dash, which then do NOT byte-match the script and the edit fails. " +
      "Paste old_string verbatim from script_read. (new_string may contain unicode safely - it is written as-is.) " +
      "Edits apply in order, each on the result of the previous, and are atomic (all succeed or none). " +
      "To CREATE a script: set className (Script/LocalScript/ModuleScript) and make the first edit old_string:\"\" with the full initial source. " +
      "datamodel_type must be \"Edit\".",
    inspect_instance:
      "Path is dot-notation and case-insensitive, e.g. 'Workspace.Model.Part'. Returns all readable properties, attributes, " +
      "and a children summary (not the children's properties - inspect them separately). If several instances share the path, " +
      "up to 20 matches are returned. Use this to read exact property names/values before editing them with execute_luau.",
    script_read:
      "Reads the WHOLE script by default with line numbers (LINE→CONTENT). Use it before multi_edit so your old_string " +
      "matches exactly. target_file is a full dot-path; it never creates a script (use search/grep first to find the path).",
    user_keyboard_input:
      "Simulates a real player typing during PLAY. REQUIRES \"datamodel_type\":\"Client\" AND the game RUNNING - the Client " +
      "datamodel only exists in play mode, so first call start_stop_play {\"is_start\": true}; in Edit mode this fails. " +
      "(Multi-Script auto-fills datamodel_type:\"Client\" if you omit it, but the game must still be running.) " +
      "\"actions\" is an ORDERED array of OBJECTS - each step MUST be {\"action\": ...}, NOT a bare string (a missing/misnamed action " +
      "gives 'Unknown ... action: nil'). action is one of: keyDown | keyUp | keyPress (down+up) | textInput | wait. " +
      "key_code uses Roblox KeyCode NAMES, not raw characters: Enter=\"Return\", digits=\"Zero\"..\"Nine\", letters=single uppercase " +
      "\"A\"..\"Z\", plus \"Space\", \"Backspace\", \"Tab\", arrows \"Up\"/\"Down\"/\"Left\"/\"Right\", modifiers \"LeftShift\"/\"LeftControl\"/\"LeftAlt\" " +
      "- REQUIRED on keyDown/keyUp/keyPress ('key_code is required' otherwise). To type a whole string use ONE textInput step with " +
      "\"text_inputs\":\"hello\" instead of many keyPress. A \"wait\" step MUST carry \"wait_time_ms\" (0-10000) ('wait_time_ms is required " +
      "for wait action' otherwise). Optional \"instance_path\" routes input to a focused GUI element and must start with game, LocalPlayer " +
      "or Workspace (e.g. \"LocalPlayer.PlayerGui.Menu.NameBox\"); omit it to send to whatever currently has focus. " +
      "Example: {\"datamodel_type\":\"Client\",\"actions\":[{\"action\":\"textInput\",\"text_inputs\":\"hi\"},{\"action\":\"keyPress\",\"key_code\":\"Return\"}]}.",
    generate_mesh:
      "Unlike generate_procedural_model, this call YIELDS: it blocks until the AI mesh generation finishes and only then " +
      "returns the result (the finished mesh) - there is no separate poll/wait step needed, just wait for the response.",
    generate_procedural_model:
      "Unlike generate_mesh, this call does NOT yield: it returns immediately with a generationId while the model builds " +
      "in the background and auto-inserts into the workspace once done - do NOT run other commands assuming the model already " +
      "exists yet. Do NOT call wait_job_finished as a reflex right after this - but DO call it (pass the generationId) whenever " +
      "you actually need the finished result before continuing: either the user explicitly asked to wait, or your next step " +
      "depends on the model being done (e.g. editing/coloring it, checking its geometry).",
    user_mouse_input:
      "Simulates real player mouse actions during PLAY. Same requirement as user_keyboard_input: \"datamodel_type\":\"Client\" (auto-filled " +
      "if omitted) AND the game RUNNING (start_stop_play {\"is_start\": true} first; fails in Edit mode). " +
      "\"actions\" is an ORDERED array of OBJECTS - each step MUST be {\"action\": ...}, NOT a bare string (a missing/misnamed action gives " +
      "'Unknown mouse action: nil'). action is one of: moveTo | mouseButtonDown | mouseButtonUp | mouseButtonClick | scrollUp | scrollDown | wait. " +
      "You MUST establish a position BEFORE any click/scroll: the FIRST step needs \"x\"/\"y\" (screen pixels) OR \"instance_path\" " +
      "(starts with game/LocalPlayer/Workspace; if set, x/y are ignored) - else 'Either x and y, instance_path, or a prior action ... is " +
      "required'. Later steps may omit x/y and reuse the last position (click then scroll at the same spot). " +
      "mouseButtonDown/Up/Click need \"mouse_button\":\"left\" or \"right\". A \"wait\" step needs \"wait_time_ms\" (0-10000). " +
      "Example: {\"datamodel_type\":\"Client\",\"actions\":[{\"action\":\"mouseButtonClick\",\"mouse_button\":\"left\",\"instance_path\":\"LocalPlayer.PlayerGui.Menu.PlayBtn\"}]}.",
  };

  // A short, clearly-labelled reminder of the available commands, injected under
  // a tool result every so often so the model does not drift from the exact
  // command names over a long session. It is explicitly framed as an automatic
  // Multi-Script reminder (NOT a user message and NOT a new command to run).
  function toolsReminder(tools) {
    const toolsString =
      "  list_commands() - list all available Roblox Studio commands with full parameter details\n" +
      compactTools(tools);
    return (
      "\n\n────────────────────────────────\n" +
      "(System note from Multi-Script - this is an automatic REMINDER, not a request and not a new result. " +
      "Do NOT reply to it or run any command because of it; just keep it in mind for your next command.)\n" +
      "Reminder of the Roblox Studio commands (use exact names and parameter keys; " +
      "for other connected apps call list_mcp_servers):\n" +
      toolsString
    );
  }

  // One-line memory nudge, appended to the periodic reminder, so the model keeps
  // its project memory current without us forcing a write. Clearly framed as an
  // optional reminder, NOT a command to run right now.
  function memoryNudge() {
    return (
      "(Reminder: if you've learned anything DURABLE about this project since your last memory update " +
      "(architecture, where things live, conventions, decisions, user preferences), update your shared project memory at " +
      "game.ServerStorage.Multi-Script.Memory with multi_edit - only useful, lasting facts. If nothing changed, ignore this.)"
    );
  }

  // General relative estimates for Multi-Script work, not live or scientific
  // model benchmarks. Arena and Notion AI are intentionally omitted because
  // they route across models and cannot be represented by one stable score.
  const PROVIDER_BENCHMARKS = [
    { id: "deepseek", name: "DeepSeek", speed: 9, smart: 8, stability: 8, note: "Fast coding and tool iteration." },
    { id: "chatgpt", name: "ChatGPT", speed: 8, smart: 9, stability: 9, note: "Strong all-round reasoning and reliability." },
    { id: "gemini", name: "Gemini", speed: 9, smart: 9, stability: 8, note: "Fast multimodal and long-context work." },
    { id: "kimi", name: "Kimi", speed: 8, smart: 8, stability: 7, note: "Strong long-context repository work." },
    { id: "glm", name: "GLM", speed: 8, smart: 8, stability: 7, note: "Efficient structured implementation." },
    { id: "qwen", name: "Qwen", speed: 8, smart: 8, stability: 8, note: "Balanced coding and tool use." },
    { id: "meta", name: "Meta AI", speed: 9, smart: 7, stability: 8, note: "Quick general-purpose production." },
  ];

  const PROVIDER_ECONOMY = {
    notion: "Use workspace context selectively. Do not restate pages or tool output; cite only the facts needed for the next action.",
    chatgpt: "Use compact structured reasoning and direct tool calls. Avoid narrating plans already implied by the command.",
    gemini: "Keep source use targeted. Summarize only evidence that changes the decision; avoid repeating source excerpts.",
    kimi: "Exploit long context without echoing it. Maintain a terse dependency map and refer back instead of restating files.",
    deepseek: "Prefer code/action over prose. Batch compatible edits and return a short result summary.",
    glm: "Use exact schemas and concise action-first responses. Do not translate or paraphrase tool payloads unnecessarily.",
    qwen: "Use minimal complete commands and compact verification. Avoid duplicate explanation around code.",
    arena: "Answer in direct single-model style: one action at a time, no comparison or duplicated candidates.",
    meta: "Keep replies concise and execution-focused; preserve exact command syntax without surrounding commentary.",
  };
  function economyPrompt(providerId, mode) {
    if (!mode || mode === "off") return "";
    const provider = PROVIDER_ECONOMY[providerId] || "Use concise, action-first replies and avoid repeating context.";
    const strength = mode === "compact"
      ? "Optimize aggressively for low usage: use the fewest sufficient tool calls, batch independent work when supported, keep progress prose to one line, never repeat tool output, and keep final answers under six short bullets unless detail is requested."
      : "Optimize for efficient usage without sacrificing correctness: make each tool call purposeful, batch safe independent work, avoid repeated context, and keep progress/final prose concise.";
    return `[ZEROSCRIPT USAGE OPTIMIZER — ${mode.toUpperCase()}]\n${strength}\nProvider adaptation: ${provider}\nPreserve all required parameters, safety checks, evidence, and errors; compactness must never hide uncertainty or skip validation.`;
  }
  function optimizeInjectedText(text, mode) {
    let out = String(text == null ? "" : text).replace(/[ \t]+$/gm, "").replace(/\n{4,}/g, "\n\n\n");
    if (!mode || mode === "off") return out;
    // Remove only consecutive duplicate lines; never dedupe code or non-adjacent evidence.
    const lines = out.split("\n"), kept = [];
    let fenced = false;
    for (const line of lines) {
      if (/^\s*```/.test(line)) fenced = !fenced;
      if (!fenced && kept.length && line.trim() && line === kept[kept.length - 1]) continue;
      kept.push(line);
    }
    out = kept.join("\n");
    if (mode === "compact") {
      // Minify a whole JSON payload following the standard Output caption. This is
      // lossless and often cuts tool-result tokens by 30–60%.
      const m = out.match(/^(Output of '[^']+':\s*\n)([\s\S]+)$/);
      if (m) { try { out = m[1] + JSON.stringify(JSON.parse(m[2])); } catch {} }
    }
    return out;
  }
  function providerBehaviorPrompt(settings = {}, providerName = "this provider") {
    const promptSkills = ["off", "suggest", "automatic"].includes(settings.promptSkills) ? settings.promptSkills : "automatic";
    const loops = ["fast", "balanced", "rigorous"].includes(settings.loops) ? settings.loops : "balanced";
    const creativeSurface = ["auto", "engine", "figma", "canvas"].includes(settings.creativeSurface) ? settings.creativeSurface : "auto";
    const skillRule = promptSkills === "off"
      ? "PROMPT SKILLS OFF: Preserve the user's prompt literally. Do not expand it, rewrite it, recommend skills, or load optional skill workflows unless the user explicitly asks."
      : promptSkills === "suggest"
        ? "PROMPT SKILLS SUGGEST-ONLY: Preserve the user's prompt. You may briefly name up to 3 relevant skills, but do not rewrite the request or load/apply those skills unless the user approves."
        : "PROMPT SKILLS AUTOMATIC: Improve underspecified requests without changing explicit intent, orchestrate a complementary multi-skill stack and apply it silently through real project actions.";
    const loopRule = loops === "fast"
      ? "FAST PRODUCTION OVERRIDE: Disable automatic workflow-plan, critic-review, quality-scorecard, and test-matrix loops. Use the minimum commands needed, perform only essential safety/compile checks, and finish immediately. This explicitly overrides mandatory critic/workflow-loop language elsewhere in the bootstrap prompt."
      : loops === "rigorous"
        ? "RIGOROUS VALIDATION: For meaningful changes, plan first, run focused tests, collect evidence, run critic review, revise failures, and repeat for at most 3 rounds. Use this only where evidence can change the result."
        : "BALANCED VALIDATION: Skip loops for trivial edits. For meaningful changes, perform one focused verification and one critic review; revise only when a concrete issue is found. Avoid ceremonial or repeated checks.";
    const surfaceRule = creativeSurface === "engine"
      ? "CREATIVE SURFACE — DIRECT ENGINE: Skip Canvas and Figma drafts. Build the design directly in the connected target engine, using its real hierarchy, components, interactions, responsive constraints, and runtime tests."
      : creativeSurface === "figma"
        ? "CREATIVE SURFACE — FIGMA: Use a connected Figma MCP first for UI/UX. If it is unavailable, state that clearly and fall back to an importable SVG rather than claiming a Figma edit."
        : creativeSurface === "canvas"
          ? "CREATIVE SURFACE — CANVAS/SVG: Draw the design as an actual SVG/Canvas artifact first, then import or implement it in the target only if requested."
          : "CREATIVE SURFACE — AUTO: Choose between direct engine implementation, Figma, and Canvas/SVG based on which produces the best usable result with the least unnecessary work. Direct Roblox Studio is preferred for immediately usable in-game UI and runtime interactions; skip Canvas whenever it would only duplicate work.";
    return `[MULTI-SCRIPT PROVIDER PREFERENCES — ${providerName}]\n${skillRule}\n${loopRule}\n${surfaceRule}`;
  }

  function qualityAmplifierPrompt(level) {
    if (!level || level === "off") return "";
    const ambition = level === "ambitious"
      ? "Push for a distinctive, portfolio-quality result with a memorable motif, layered polish, and coherent creative direction, while staying within the user's requested feature and existing project style."
      : "Turn short or vague requests into a polished implementation brief with sensible defaults and professional completeness, without scope creep.";
    return `[ZEROSCRIPT QUALITY AMPLIFIER — ${level.toUpperCase()}]\n${ambition}\nFor every creative/build request: preserve explicit intent; infer audience and context; improve hierarchy, composition, materials, typography/color, interactions, animation/feedback, responsive behavior, accessibility, edge states, performance, and verification where relevant. Silently form an enhanced brief before acting. Ask only when a high-impact decision is truly blocking. For creative tasks, silently match and run the strongest engine virtual tools, use ms_enhance_brief when useful, and then implement directly in the real project; do not expose the rewritten brief or require a visible skill-selection phase. Never replace the user's idea with your own; make their idea feel intentional, cohesive, and finished.`;
  }

  function compactSystemReminder(providerId, mode) {
    return `${RESEND_MARKER}\n(System note from Multi-Script; do not reply to this note.) Continue using exactly one fenced Multi-Script command per action and wait for its result. Use exact tool names/parameters from list_commands; inspect before editing; never delete broadly; retry only from fresh error evidence; verify meaningful changes and run ms_critic_review. ${economyPrompt(providerId, mode)}`;
  }

  return {
    APP_NAME,
    SYS_MARKER,
    RESEND_MARKER,
    FEEDBACK,
    toolCategory,
    buildSystemPrompt,
    compactTools,
    toolsReminder,
    memoryNudge,
    economyPrompt,
    optimizeInjectedText,
    compactSystemReminder,
    qualityAmplifierPrompt,
    providerBehaviorPrompt,
    PROVIDER_BENCHMARKS,
    TOOL_NOTES,
  };
})();
