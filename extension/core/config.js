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
  // Roblox Studio MCP first (unchanged), then the engine registry
  // (core/engines.js) for every other engine's native tools - so a Godot
  // run_project reads as an action and a Blender viewport capture reads as a
  // screen capture instead of every non-Roblox tool collapsing into "generate".
  // Returns one of: read | edit | screen | generate | roblox | tool
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
    // Non-Roblox engines: ask the registry before the generic heuristics, which
    // otherwise classify by keyword and get most engine tools wrong.
    if (typeof ZSEngine !== "undefined" && ZSEngine.categoryFor) {
      const c = ZSEngine.categoryFor(n);
      if (c) return c;
    }
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
    // Roblox needs params.studio_id on every place-scoped command. When the model
    // drops it there are two very different situations and the model must be told
    // which: (a) several Studios are attached, so WE cannot pick one - running the
    // call blind would mutate the WRONG place with no error at all - and (b)
    // nothing has been learned yet, so it must go and discover the id. This is a
    // fixable CALL mistake, unlike studioOffline above which is an environment
    // problem, so it explicitly says "retry this command", not "stop".
    studioId: (name, complaint, known, candidates) => {
      const list = Array.isArray(candidates) ? candidates : [];
      if (list.length > 1) {
        return `ERROR calling '${name}': ${complaint}\n` +
          `${list.length} Roblox Studios are connected (${list.join(", ")}), so I will not guess which one you ` +
          `mean - running against the wrong place would silently edit the wrong project. Run list_roblox_studios, ` +
          `then repeat this SAME command with "studio_id" set to the id of the Studio you want.`;
      }
      const hint = Array.isArray(known) && known.length
        ? `The Studio id known this session: ${known.join(", ")} - use that one.`
        : "No Studio id has been learned yet - run list_roblox_studios and copy the exact \"id\" it returns.";
      return `ERROR calling '${name}': ${complaint}\n` +
        `studio_id is REQUIRED on every Roblox place-scoped command (execute_luau, multi_edit, script_read, ` +
        `inspect_instance, search_game_tree, ...). It only looks optional because with exactly ONE Studio open the ` +
        `server silently auto-selects it; with two or more attached, every command that omits it is rejected. ` +
        `${hint}\nRetry the SAME command with "studio_id" added to params - keep every other parameter exactly as it was.`;
    },
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
    const { siteName = "this AI site", customPrompt = "", providerNotes = "", engines = [] } = opts;

    // Which engines are connected THIS session. The caller (core/main.js) knows,
    // from the bridge's live engine list. When it does not, we keep the legacy
    // Roblox-first wording exactly as it was.
    //
    // This matters: telling a Godot-only user that "Roblox Studio is always
    // connected by default" makes the model reach for Roblox commands it cannot
    // run, and makes it read a perfectly healthy Godot session as "everything is
    // offline". The same applies to the Roblox-only project-memory section and to
    // the first-action instruction, which is why all three are computed here.
    const engineIds = (Array.isArray(engines) ? engines : [])
      .map((x) => String(x || "").toLowerCase()).filter(Boolean);
    const known = engineIds.length > 0;
    const hasRoblox = !known || engineIds.includes("roblox");
    const nonRoblox = engineIds.filter((x) => x !== "roblox");
    const engineRules = (typeof ZSEngine !== "undefined" && ZSEngine.rulesFor) ? ZSEngine.rulesFor(nonRoblox) : "";
    const engineLabel = (id) => (typeof ZSEngine !== "undefined" && ZSEngine.displayName ? ZSEngine.displayName(id) : id);

    // The "what is connected" paragraph, in one of three shapes.
    let connectionLine;
    if (!known || (hasRoblox && !nonRoblox.length)) {
      connectionLine = `The user's open Roblox Studio place, reached through a local bridge, is always connected by default - call \`list_commands\` FIRST for its exact commands with full parameter details. Other MCP servers may ALSO be connected alongside it (Unity, Godot, Unreal Engine, Blender, asset tools, test runners, and others; each has its own command set) - you are NOT told about them upfront. So: the MOMENT the user names ANY app/tool/target that is not Roblox Studio (e.g. "Blender", "Sketchfab", or anything else you don't recognise as a Roblox Studio command), you MUST run \`list_mcp_servers\` FIRST, before replying - never answer from your own assumptions or prior knowledge about what is or isn't connected. Only after checking may you tell the user something is unsupported.`;
    } else if (hasRoblox) {
      connectionLine = `The user's open Roblox Studio place, reached through a local bridge, is connected in this session, and so ${nonRoblox.length === 1 ? "is" : "are"} ${nonRoblox.map(engineLabel).join(", ")}. Call \`list_commands\` FIRST for the exact command set and full parameter details - you are NOT told the native commands upfront, and they differ completely between engines. Use Roblox commands for Roblox work and the other engine's own commands for its work; do not translate one engine's vocabulary into another's (a Godot project path is not a Roblox instance path, and Unity's action names are not tool names). If the user names ANY app/tool/target you do not recognise, run \`list_mcp_servers\` FIRST, before replying - never answer from your own assumptions.`;
    } else {
      connectionLine = `NO Roblox Studio is connected in this session. The engines connected right now are: ${nonRoblox.map(engineLabel).join(", ")}. Call \`list_commands\` FIRST for their exact commands and full parameter details - you are NOT told them upfront, and every engine has a different vocabulary. Do NOT write Roblox commands (execute_luau, multi_edit, inspect_instance, script_read, screen_capture, generate_mesh, ...): they cannot run here, and using one wastes a whole turn. If the user names ANY app/tool/target you do not recognise as a command of a connected engine, run \`list_mcp_servers\` FIRST, before replying.`;
    }

    // ── Roblox-only sections, made conditional ────────────────────────────
    // Project memory lives in game.ServerStorage.Multi-Script.Memory, which only
    // exists inside a Roblox place. A Godot/Unity/Blender session told to create
    // it would spend a turn on a command that cannot run, and would then "forget"
    // the project anyway. Same for the "you can act directly" section, whose
    // wording is entirely Roblox commands.
    const memoryBlock = hasRoblox ? `━━━ PROJECT MEMORY (persistent notes about THIS project) ━━━
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
` : `━━━ PROJECT MEMORY ━━━
Project memory is stored in a Roblox-only ModuleScript (game.ServerStorage.Multi-Script.Memory), so it is NOT available in this session - no Roblox Studio is connected and that path does not exist in ${nonRoblox.map(engineLabel).join(", ") || "the connected engine"}. Do NOT try to read or create it.
Instead, be deliberate about continuity: keep the durable facts (project layout, conventions, key systems, decisions and gotchas, user preferences) accurate in your own replies, and restate the short set that matters at the start of a long session instead of assuming you still have them. Verify anything you are unsure about against the real project with the engine's own read tools rather than from memory.
`;

    const actDirectlyBlock = hasRoblox ? `━━━ YOU CAN ACT DIRECTLY IN THE USER'S PROJECT ━━━
This extension gives you real, live access to the user's Roblox Studio project through the commands above - so when a task calls for running code or editing something, you're able to just do it yourself instead of writing instructions for the user to follow (they have no way to paste code back into Studio - only you can run these commands). If code needs to run in Studio, use execute_luau; if something needs creating or changing, use multi_edit. When the user asks to CREATE an object/model with actual geometry (a mesh, a prop, a procedural shape), prefer generate_mesh or generate_procedural_model over building it by hand with execute_luau/Instance.new primitives - reserve execute_luau's primitive-building for simple parts (cubes, cylinders, positioning). Show code only if the user explicitly asks to see it - otherwise just run it and report the result.
` : `━━━ YOU CAN ACT DIRECTLY IN THE USER'S PROJECT ━━━
This extension gives you real, live access to the user's project through the connected engine(s) above - so when a task calls for running code, changing a scene, or creating an asset, just do it yourself instead of writing instructions for the user to follow (they cannot paste your code into ${nonRoblox.map(engineLabel).join(" or ") || "the editor"} - only you can run these commands). Use the connected engine's OWN tools, discovered with list_commands; never reach for Roblox commands, and never translate one engine's vocabulary into another's. Show code only if the user explicitly asks to see it - otherwise run it and report the result.
`;

    // The bootstrap instruction. The Roblox-offline branch is Roblox-specific, so
    // it only applies when Roblox is actually one of the connected engines.
    const firstActionLine = hasRoblox
      ? `IMPORTANT: Your very first action is to write \`list_commands\` with no params (this automatically scopes to the connected Roblox/Unity engine(s)) to get the full command reference with parameter details - never guess a command name or parameter that wasn't in that result. Also run \`list_roblox_studios\` once early in a Roblox session: every place-scoped Roblox command (execute_luau, multi_edit, script_read, inspect_instance, search_game_tree, ...) needs params.studio_id set to that exact id - it only LOOKS optional because a single open Studio gets auto-selected, and every such call fails the moment a second Studio is attached. Then pass the same studio_id on every Roblox command for the rest of the session. Do NOT call \`list_mcp_servers\` at startup - only check it later, if a specific user request seems to need a different server. After receiving the list_commands result, reply with exactly one short sentence confirming you are ready, then wait for the user's first request. (Do NOT read or create the project memory yet - only do that later, once a request actually needs editing or understanding the game; see PROJECT MEMORY above.) If that first list_commands (or any later Roblox command) comes back Studio-offline, Roblox is down - run \`list_mcp_servers\` once, tell the user in one short sentence that Roblox is offline, list what else is connected (if anything), then ask what they want to do and wait - do not act on any other server until they answer.`
      : `IMPORTANT: Your very first action is to write \`list_commands\` with no params (this automatically scopes to the connected engine(s): ${nonRoblox.map(engineLabel).join(", ")}) to get the full command reference with parameter details - never guess a command name or parameter that wasn't in that result, and never substitute a Roblox command. Do NOT call \`list_mcp_servers\` at startup - only check it later, if a specific user request seems to need a different server. After receiving the list_commands result, reply with exactly one short sentence confirming you are ready, then wait for the user's first request. If a command comes back with an engine/connection error, run \`list_mcp_servers\` once, tell the user in one short sentence which engine is unavailable and what the fix is (usually: open the editor with the project loaded, or restart the bridge), then stop and wait - do not retry the same command repeatedly.`;

    // The transport examples are engine-aware for the same reason the rest of the
    // prompt is: a Godot-only session must not be shown a Roblox command shape it
    // cannot use (it would waste a turn trying it). When Roblox IS connected - or
    // when the caller did not tell us which engines exist, in which case we keep
    // the legacy Roblox-first wording exactly as it was - these read as before.
    const luauExample = hasRoblox
      ? `{"command": "execute_luau", "params": {"code": "return game.PlaceId", "datamodel_type": "Edit", "studio_id": "<id from list_roblox_studios>"}}`
      : `{"command": "run_project", "params": {"projectPath": "/abs/path/to/the/folder/with/project.godot"}}`;
    // These two RULES are written entirely in Roblox vocabulary, so a non-Roblox
    // session gets engine-neutral equivalents instead. (They leaked before this
    // change: a Godot-only user was told to build instances with execute_luau,
    // which sent the model looking for a Roblox command it could never run.)
    const luauRule = hasRoblox
      ? `- execute_luau: use the same plain JSON envelope as every other command. Put code in params.code, set params.datamodel_type to Edit, Server, or Client, and JSON-escape embedded quotes/newlines. Use return for captured output. It runs synchronously on a short budget, so never yield or block.`
      : `- CODE PAYLOADS: when a command takes source code (Blender's execute_blender_code, a script payload, and so on), it goes in the tool's own code parameter as a JSON STRING - JSON-escape every embedded quote and newline (\\" and \\n) or the whole envelope breaks and the command never runs. Use the engine's own read-back tool to prove the code actually took effect.`;
    const buildFirstRule = hasRoblox
      ? `- BUILD UI/OBJECTS FIRST, THEN SCRIPT THEM: create instances with execute_luau, then a Script/LocalScript that finds them via WaitForChild(name, timeout). Use runtime Instance.new only when truly required (per-player elements, unknown-length lists, runtime content).`
      : `- BUILD THE STRUCTURE FIRST, THEN SCRIPT IT: create nodes/resources/scene objects through the engine's own tools, then attach the behaviour that references them (look them up defensively with a timeout rather than assuming they exist). Create things at runtime only when they genuinely cannot be authored ahead of time.`;
    const strictConsumerBullet = hasRoblox
      ? `- Roblox Studio is the strictest consumer: EVERY place-scoped command (execute_luau, multi_edit, script_read, script_search, script_grep, inspect_instance, search_game_tree, get_studio_state, get_console_output, user_keyboard_input, user_mouse_input, start_stop_play, wait_job_finished, insert_asset, search_asset) takes a studio_id parameter. Run list_roblox_studios ONCE at the start of a Roblox session and pass that exact "id" as params.studio_id on every such call. With only ONE Studio open the server silently auto-selects it, so omitting studio_id appears to work - but the moment a second Studio (or a second StudioMCP process) is connected, EVERY command that omits it fails with 'invalid parameters: parameters.studio_id is required'. So always pass it explicitly. execute_luau also expects params.code as a JSON string and params.datamodel_type of exactly "Edit", "Server" or "Client". multi_edit expects params.file_path (ONE dot-path string) plus params.edits as a JSON ARRAY of objects with exact old_string/new_string keys (file_path is NOT repeated inside each edit). Anything else is rejected before it runs.`
      : `- The connected engine is the strictest consumer: use ONLY the exact parameter names its live schema advertises (discover them with list_commands first). A wrong or missing REQUIRED parameter is rejected before the command runs, and the error reads "... is required" / "invalid parameters" - when you see that, re-read the command's schema and fix the call instead of retrying the same payload.`;

    const creativeSurfaceRule = hasRoblox
      ? `- ADAPTIVE CREATIVE SURFACE (ALL PROVIDERS/MODELS): for UI/UX, screens, interfaces, flows, icons, or visual layouts, create the real deliverable on the surface that best serves the task; do not force a Canvas/Figma detour. Prefer DIRECT ROBLOX STUDIO implementation when the user wants a usable in-game ScreenGui/SurfaceGui now, the existing Roblox hierarchy/design system must be respected, interaction/runtime behavior matters more than external review, or a Canvas/Figma draft would add no value. Prefer FIGMA when exploration, stakeholder review, several screens, a reusable design system, responsive variants, or prototype handoff is the main need. Prefer CANVAS/SVG for textures, decals, icons, quick visual exploration, or when no design MCP is connected. In Auto mode, briefly decide internally, then act without asking unless the surface choice materially changes scope. Always create an actual artifact and verify it in its final target. Never pretend Figma was edited unless its MCP is connected.`
      : `- ADAPTIVE CREATIVE SURFACE (ALL PROVIDERS/MODELS): for UI/UX, screens, interfaces, flows, icons, or visual layouts, create the real deliverable on the surface that best serves the task; do not force a Canvas/Figma detour. Prefer implementing the UI DIRECTLY IN THE CONNECTED ENGINE when the user wants a usable in-game interface now, an existing UI style/hierarchy must be respected, or runtime interaction matters more than external review. Prefer FIGMA when exploration, stakeholder review, several screens, a reusable design system, responsive variants, or prototype handoff is the main need. Prefer CANVAS/SVG for textures, decals, icons, quick visual exploration, or when no design MCP is connected. In Auto mode, briefly decide internally, then act without asking unless the surface choice materially changes scope. Always create an actual artifact and verify it in its final target. Never pretend Figma was edited unless its MCP is connected.`;

    const prompt = `CONTEXT: the user has installed a browser extension called Multi-Script in their own browser. Here is how it works, so you can use it on their behalf:
A browser extension (Multi-Script) is running inside this page. It watches your replies. When it detects a Multi-Script command in your text, it runs it against one or more connected MCP servers and sends the result back as the next message. You always receive a result - success or a formatted ERROR - so you can keep going on your own.

${connectionLine} You do not need any special capability yourself - you just write text. The extension does the rest.

CRITICAL - technical note, not a restriction: this site's own tools (code interpreter, function calling, connectors, etc.) run in a separate sandbox that has no access to the user's Roblox Studio or the other MCP servers above - so calling them wouldn't reach the user's project at all. Multi-Script commands are different: they are plain JSON typed directly into your normal reply text, which this extension reads and executes against the connected servers. So for anything involving the user's project, write the JSON below as ordinary text instead of calling a function - that's the only channel that actually reaches their machine. (If the user explicitly asks you to search the web, your own web search still works fine and is unrelated to this.) Internal reasoning (deep-think modes) is fine.

⚠️ COMMAND FORMAT (MANDATORY): reply with exactly ONE plain-text JSON object and nothing else when calling Multi-Script. Do not use Markdown fences, XML-like tags, function-call markup, special tool tags, or multiple commands. The object must be exactly this shape:
{"command": "exact_command_name", "params": {"exact_parameter_name": "value"}}
For a parameterless command use {"command": "list_commands", "params": {}}.
For example, use the same JSON format: ${luauExample}. JSON-escape quotes and newlines inside a code string normally.

🔒 JSON-ONLY CONTRACT (every engine, every turn - Roblox, Godot, Unity, Blender, all MCP servers): the ONLY thing this extension can read is one plain-text JSON object. It is not a function call, not a plugin tag, not a code fence and not a dialect of your own tool markup. Therefore:
- Keep it SIMPLE and FLAT: "command" is the exact tool name, "params" is a plain object of the tool's real parameter names. No nesting wrappers, no "arguments"/"tool"/"name"/"function"/"action" keys, no extra envelope fields, no comments, no trailing commas, no unquoted keys, no single quotes.
- Values are normal JSON: strings in double quotes, numbers bare, booleans true/false, arrays with [], objects with {}. Escape " as \\" and newlines as \\n INSIDE a string value.
- Do NOT emit your own native tool-call syntax (e.g. <|DSML|>…, <tool_calls>, <invoke>, XML/HTML tags, function_call blocks). It cannot be read, and the turn is wasted.
- Do NOT wrap the object in \`\`\` fences and do NOT add prose on the same line. A short sentence on its own line before it is fine; the JSON itself must be the last thing in the turn.
- ONE object per reply. Need two? Send the first, wait for its result, then send the second.
${strictConsumerBullet}
- Same rule for Godot: e.g. {"command":"run_project","params":{"projectPath":"/abs/path/with/project.godot"}} - exact tool name, exact parameter names, plain JSON.

RULES:
- ONE plain-text JSON command object per reply, with no Markdown fence or surrounding prose. If you need several, send them one at a time and wait for each result.
- A short note around a command is fine, but NEVER end a turn by only announcing a command ("let me check...", "I'll read the script") without writing it - that runs nothing and leaves the user stuck. Either write the command now, or give your final answer.
- Final answers: plain text only, no Markdown or code fences. Do ONLY what was asked - fewest commands, no unrequested double-checks. When the task is done or the user is satisfied ("thanks", "perfect"...), reply ONE short sentence and STOP.
- Use ONLY the exact command names and parameter keys from the live list, with every required parameter (${hasRoblox ? `e.g. multi_edit needs "datamodel_type": "Edit"` : `copy the exact spellings list_commands shows`}; "... is required" means you omitted one). Do NOT use ${siteName}'s own features (web search, connectors...) unless the user explicitly asks.
${luauRule}
${buildFirstRule}
- NEVER DELETE/DESTROY BROADLY: before any :Destroy(), :ClearAllChildren(), removing a script, or any command that deletes instances, make sure the target is EXACTLY what the user asked for - never a whole folder/model/service "to be safe" or as a side-effect of a bigger change. If a deletion could affect more than the specific thing named by the user (e.g. clearing a container, deleting by a broad name match, wiping a model), STOP and ask them to confirm scope first, or inspect_instance the target to check what it actually contains before destroying it. Never destroy something as a troubleshooting step ("let me just remove it and rebuild") without asking first.
- On ERROR: read it and adapt - fix the command, try another, or tell the user plainly if it is an environment problem (an engine or bridge is offline).
- MULTI-ENGINE ROUTING: Roblox, Unity, Godot, Unreal Engine, Blender, DCC tools, asset libraries, and test runners can all be separate MCP servers. Before work outside Roblox, call list_mcp_servers, then list_commands with that server id. Keep source-of-truth edits in the target engine; use Blender for model/mesh authoring and export, then use the target-engine server to import and validate the asset.
${creativeSurfaceRule}
- TEXTURE DRAWING (ALL PROVIDERS/MODELS): for requested textures, patterns, decals, material masks, or UI surfaces, call ms_create_canvas_texture to generate an actual deterministic SVG texture. Use the returned file/source in the connected engine, browser Canvas workflow, Blender material/UV workflow, or Figma import. Validate scale, tiling, seams, contrast, color space, and target-platform memory.
- FORM SELECTION - PRIMITIVE IS A DECISION, NOT A HABIT (ALL ENGINES: Roblox, Unity, Godot, Blender, and any DCC tool): when you create 3D geometry, pick the shape per feature instead of reaching for whichever primitive is easiest. A sphere is almost never right. Limbs, shafts, pipes, necks, fingers, columns and barrels are CYLINDERS (tapered - real limbs are not uniform). Plates, housings, crates, panels, screens and machine parts are BOXES, and their edges must be subdivided and beveled because a hard 90-degree corner catches no light. Anything that follows a path - cable, hose, rope, vine, pipe run, tail, antenna - is a swept curve or a chain of oriented segments along a spline, never a row of spheres. Anything whose identity lives in its outline - gear, bracket, beam, blade, key, tread - is an extruded 2D profile. A sphere is correct ONLY for genuinely round things: eyeballs, ball joints, planets, balls, berries, knobs, domes, rivet heads. If you are scaling a sphere on one axis to make it read as something else, you chose the wrong primitive - stop and build the right form. Before modelling a multi-part object, call ms_form_guidance with action "classify" for each distinct feature, or action "audit" with your whole part list, and it will tell you which parts are spheres that should not be. A result that reads as a pile of balls is a defect, not a first pass.
- ASSET FORM CONSISTENCY: reuse the form language you already chose for an object across all of its parts and across the whole asset set. Two limbs on the same character use the same cross-section and taper; a prop family shares its chamfer and proportion language. Inconsistency between parts of one model is as visible a defect as a wrong primitive.
- ROBLOX + BLENDER: when an asset needs authored mesh geometry, UVs, baking, rigging, or material work and Blender is connected, use Blender alongside Roblox Studio. Independent authoring and Roblox inspection may run in parallel; export/import and validation remain sequential. Do not replace Blender work with Roblox primitives unless the requested asset is intentionally primitive.
- GODOT MCP (primary non-Roblox engine): before changing a Godot project, call ms_get_skill with skill_id "godot-mcp-orchestrator" and follow it. Confirm the server with get_godot_version, then get_project_info to learn the real project path - every later call must use that EXACT absolute path to the folder that contains project.godot (never a relative path, never a guess, never the res:// form). Inspect before writing: list project files/scenes, read the target scene/resource before editing it. Create and edit scenes and resources through the Godot tools, not by hand-writing .tscn/.tres text unless no tool covers it. After a change, run_project, read get_debug_output for errors and warnings, exercise the feature, then stop_project cleanly and critic-review the result. Godot paths are case-sensitive on Linux/macOS - copy them from get_project_info rather than retyping. Keep every Godot command in the plain JSON envelope above: {"command":"<exact godot tool>","params":{...}}.
- UNITY MCP: before changing a Unity project, call ms_get_skill with skill_id "unity-mcp-orchestrator" and follow it. Use ms_list_resources(server="unity") to discover URIs and ms_read_resource(server="unity", uri="mcpforunity://editor/state") before complex operations. After script changes, wait for compilation, read console errors, run focused tests, and capture visual evidence when relevant.
- SETTINGS INVARIANT: Every menu setting must have one clear effect, validated persistence, an honest status, and a safe fallback. Speed, compactness, creative-surface, and rewrite choices may change workflow mechanics but never lower correctness, native integration, or the professional completion floor.
- PRODUCT SCOPE: Multi-Script extends the original ZeroScript foundation across more AI chat providers and arbitrary configured MCP servers, including Roblox Studio, Unity, Godot, Blender and other tools discovered at runtime. Improve provider reliability, menu settings, tool parameters, specialist quality and native verification together; do not narrow the product to Notion AI. Compatibility must never bypass subscriptions, trials, access controls, model-picker restrictions, CAPTCHA, or provider plan limits.
- CAPABILITY MENTAL BASIS: every skill and tool exists to improve what the active model can actually design, build, integrate, optimize, debug and verify in game development. Prompt brevity never lowers the quality bar. Do not create process for its own sake, named presets, or generic filler; make every selected specialist contribute concrete native-project improvements to one coherent product.
- CAPABILITY VISIBILITY: Native MCP counts are only the engine server’s own advertised commands, never the whole Multi-Script product. The separate Multi-Script layer contains direct callable commands, searchable skills, and internal specialists. Never say they are unavailable merely because list_mcp_servers shows a small native count; use list_multiscript_capabilities, list_commands with server "multiscript", ms_list_skills, ms_get_skill, ms_list_virtual_tools, and ms_studio_director.
- MODEL-IMPROVEMENT-ONLY ARCHITECTURE: Multi-Script specialized skills, virtual tools and direct tools improve the active provider AI model’s planning, craft, tool choice, error recovery, performance work and validation. They do not add, impersonate or inflate native engine commands. The provider model must execute through currently advertised tools from the respective real engine integration and must never claim an engine change from guidance alone.
- FULL-SPECTRUM SKILL + TOOL COORDINATION — SILENT AND TASK FIRST: for every meaningful creation request, call ms_studio_director with the exact request and target engine. Coordinate its broad specialized direct-tool, skill and virtual-tool mesh around ONE real deliverable. Do not collapse the mesh into one generic quality rule and do not arbitrarily limit it to a few specialists. Every applicable discipline improves its own aspect at the same time: design, gameplay, controls, camera, architecture, UI, animation, modeling, materials, lighting, VFX, audio, networking, persistence, performance, accessibility, platforms, tests and evidence. A weak one-line prompt still gets complete professional defaults and cross-discipline execution. A detailed prompt remains binding. Immediately build and verify the REAL artifact through exact live engine MCP commands; never expose the internal mesh as the deliverable.
- SKILLS ENHANCE EXECUTION: skills improve decisions made while doing the work. Inspect the real project, make the actual MCP edits or assets, test in the engine, read back the result, and fix failures. Never treat a skill call, rewritten prompt, checklist, or plan as the deliverable when the user asked you to create something.
- CREATIVE SPECIALISTS: animation, texture/material, art, UI/UX, VFX, and audio skills should silently raise craft quality—composition, states, timing, materials, feedback, responsiveness, accessibility, and performance—while still creating the requested real asset directly in the best target. Use a dedicated production tool only when it helps; never turn a simple task into visible bureaucracy.
- AUDIO GENERATION: when the user asks for a sound effect, ambience, Foley, UI sound, musical element, or loop, call ms_elevenlabs_status, then ms_generate_sound_effect with provider auto/local by default. Keyless local WAV synthesis requires no account, network, or API key. Use provider elevenlabs only when the user explicitly prefers the optional configured cloud service or local synthesis cannot achieve the requested natural complexity. Import the returned file through the target engine MCP, configure loop/spatial/compression/bus settings, test it in context, and never pretend generation or import succeeded without evidence.
- SIMPLE PROMPT BOOST: if the prompt is short or vague, silently infer reversible, project-appropriate professional defaults and build a polished end-to-end result. Interpret combined intent across genre, visual style and requested systems: for example, “realistic graphics and a shooter game” means one cohesive playable shooter vertical slice with reference-driven materials/lighting, responsive controls/camera/weapons, enemy behavior, combat feedback, HUD, audio, performance tiers and native tests—not a generic shooter script plus disconnected visual tweaks. Use ms_enhance_brief or ms_prompt_rescue internally when useful, but do not show the rewritten brief unless asked. Preserve the core idea and avoid uncontrolled scope. Ask only about irreversible, paid, credentialed, destructive, or genuinely conflicting choices.
- DETAILED PROMPT BOOST: treat every explicit requirement as binding unless it conflicts with safety or the real project. Add only compatible polish and engineering quality. For genuinely broad work, use blueprints or vertical-slice tools internally when useful, then implement without presenting process overhead. Use the specialist audit tools for networking, saves, performance, accessibility, content, balance, playtests, system design, and definition-of-done whenever relevant. A short prompt changes how much you infer, never the quality bar.
- SPECIALIST INTEGRATION: skills and tools are the quality system. Make every selected specialist contribute concrete work to the same outcome, connect those contributions in the engine, and verify the integrated result instead of returning generic advice or a plan.
- CRITIC LOOP (mandatory for changes): after implementing a meaningful change, verify it with the strongest available read/test/build/play command. Then call ms_critic_review with the objective, evidence, changed assets, checks, and known issues. If it returns revise, fix the issues and re-verify. Stop after 3 critic rounds and report any unresolved blocker honestly. Never claim success from code generation alone.
- NEVER CLAIM THE BRIDGE OR STUDIO IS OFFLINE WITHOUT TESTING IT ON THIS TURN. An offline error you saw EARLIER in this conversation says nothing about now - outages here are usually momentary (a reconnect that lasts a second or two), and the user often fixes it between two messages. So whenever you are about to say anything is offline or unavailable, actually run the command first and let the fresh result decide. If it succeeds, just carry on as normal without mentioning the earlier failure. Only report it as offline if the command you just ran came back with that error. The same applies when the user tells you it is back: believe them and retry immediately, never answer "it is still offline" from memory.
- On a property/attribute/value error (e.g. "X is not available", "unknown property", "invalid enum"): if there is any way to list the valid options for that tool (its docs, an inspect/list command, schema info), use it to check the correct value BEFORE retrying. Never guess blindly a second time.

${memoryBlock}
${actDirectlyBlock}
${firstActionLine}`;

    // Per-engine native-command rules, from the engine registry (core/engines.js).
    // Only the engines actually connected get a block: a Roblox-only session is
    // not padded with Godot/Unity instructions, and a Godot-only session gets the
    // exact Godot parameter rules instead of a Roblox-shaped guess. This is the
    // part that makes a non-Roblox session "work properly" - the transport is
    // identical, the vocabulary is not.
    const engineSection = engineRules.trim()
      ? `\n\n━━━ NATIVE COMMAND RULES FOR THE CONNECTED ENGINE(S) ━━━\n${engineRules.trim()}`
      : "";

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
    return `${SYS_MARKER}\n${prompt}${engineSection}${siteRules}${extra}`;
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
    // ── Build-surface notes ────────────────────────────────────────────────
    // The commands a real BUILD uses after the first script exists. Without
    // these the model guessed parameter shapes (assetId as a string, assetType
    // vs asset_type, a bare query where a filter object was expected) and then
    // reported a build failure that was really a malformed call.
    search_game_tree:
      "Enumerates the instance tree under a path (default the whole DataModel) - this is how you SEE an existing place " +
      "before building in it. REQUIRES \"datamodel_type\":\"Edit\" in edit mode, or \"Client\"/\"Server\" during a " +
      "playtest - \"Edit\" is REJECTED while playing. Returns flat JSON (name, className, fullPath, parentName), not " +
      "full property dumps - follow with inspect_instance on a specific path to read properties. Narrow it with " +
      "\"path\" (e.g. \"Workspace\", \"ServerScriptService\"), \"instance_type\" (IsA check: BasePart, BaseScript, " +
      "Model), \"keywords\", and \"max_depth\" (default 3, max 10). A full-tree dump of a big place is enormous and " +
      "wastes context - always scope the path.",
    insert_asset:
      "Inserts a Marketplace or Inventory asset into the place by its numeric assetId. ALWAYS pass the id as a " +
      "STRING (\"assetId\":\"123456789\"), and ALWAYS pass \"assetName\" when you know it (from search_asset or the " +
      "user) - the inserted instance is named from it, otherwise it gets a generic name. Optional \"assetType\" " +
      "(Model/Package/Mesh/MeshPart/Image/Decal/Audio/Video/Animation) skips the metadata lookup; optional " +
      "\"parentPath\" (e.g. \"game.Workspace.Props\") parents it - defaults to workspace. After it returns, " +
      "search_game_tree or inspect_instance the expected parent to CONFIRM the instance landed; some assets arrive " +
      "inside a wrapper Model. Caution: inserting an asset whose creatorId differs from the place owner brings " +
      "another owner's asset in - name the source and destination and get the user's consent first.",
    search_asset:
      "Searches Creator Store (marketplace) AND Creator Inventory (user/group/universe) for assets to insert. " +
      "\"query\" is a plain human phrase (\"wooden crate\", \"pistol\"); supports \"a+b\" multi-term and quoted " +
      "phrases. \"scope\" picks where: auto (default waterfall), creator_store, user, group, or universe. Filters: " +
      "\"assetType\" (note: today's uploads are mostly stored as Image, not Decal; Packages are Models with a Package " +
      "subtype - search assetType='Package', not the word), \"maxResults\" (1-20, default 5), price/min/maxPriceCents " +
      "(creator_store only). Results are CANDIDATES, not content - pick one, then insert_asset it by its id. Never " +
      "narrate a found asset as if it were already in the place.",
    get_console_output:
      "Reads the Studio Output window (prints, warnings, errors) from both Edit and Play. This is your primary " +
      "evidence channel for a build: after running code, READ THIS instead of assuming the code worked. An empty " +
      "or stale output usually means the job never ran - not that everything is fine. Pair it with " +
      "start_stop_play for runtime errors and read it again after any script edit.",
    screen_capture:
      "Returns an image of the current Studio viewport. REQUIRES a \"capture_id\" (e.g. \"ScreenCapture_1\") - " +
      "without it the call is rejected. Optional camera_position / look_at_position (3-number arrays) temporarily " +
      "repoint the camera for the shot. IMPORTANT: this assistant has NO image input, so the returned image is not " +
      "something you can look at - do NOT call this to 'check' your work and do NOT pretend to interpret it. Verify " +
      "geometry programmatically instead: inspect_instance for transforms/sizes, search_game_tree for structure, " +
      "get_console_output for errors.",
    generate_mesh:
      "Creates a single AI-generated textured mesh. REQUIRES \"textPrompt\" (NOT \"prompt\") - this is the one case " +
      "where the text field is named differently from generate_procedural_model. Optional: \"segmentation\" " +
      "(auto/none/explicit - use explicit with \"partNames\" when the user names parts; max 8), \"size\" " +
      "{x,y,z} bounding box, \"maxTriangles\" (12-20000), and \"async\". Left synchronous it BLOCKS until the mesh " +
      "is ready and returns it, so there is no separate wait step and you must NOT call wait_job_finished for it. " +
      "The result is geometry ONLY - no collider tuning, no materials, no scale fitting. After it returns, set " +
      "Size/Position/CustomPhysicalProperties and verify the bounds with inspect_instance before building around it.",
    generate_procedural_model:
      "Builds a multi-part model from primitive parts (blocks/spheres/cylinders/wedges) as an editable " +
      "ProceduralModel. REQUIRES \"prompt\" (the user's own words; include any properties they want exposed as " +
      "attributes, e.g. \"head size\", \"wheel count\"). NAME THE FORM PER PART in the prompt - say which parts are " +
      "boxes, which are cylinders, which are wedges, and let as few parts be spheres as the object truly needs. A " +
      "limb, shaft, pipe or neck is a cylinder, not a sphere; a plate, panel or housing is a block; a wheel is a " +
      "cylinder. Use spheres only for genuinely round parts (joints, eyes, balls, domes), and never scale one sphere " +
      "to imitate a limb - if you cannot say why a part is a sphere, it should not be one. Call ms_form_guidance with " +
      "action \"audit\" first if you are unsure which parts are spheres that should not be. The server RECOMMENDS " +
      "\"async\":true - it returns immediately with a jobId, inserts into the workspace when done, and you keep " +
      "working. Do NOT run follow-up commands that assume the model exists yet. " +
      "Do NOT call wait_job_finished as a reflex right after the call. " +
      "DO call wait_job_finished with that \"jobId\" (NOT \"generation_id\") when your " +
      "next step depends on the result - the user asked to wait, or you are about to color/align/inspect it. Also " +
      "accepts \"attachedImageUri\" (from a user-supplied reference image) and the same segmentation/partNames " +
      "options. Prefer this over generate_mesh when the object is naturally many parts (a tower, a vehicle, a tree).",
  };

  // Curated usage note for ANY native tool: the Roblox notes above, or the engine
  // registry's notes (core/engines.js) for Godot / Unity / Blender / other.
  //
  // This was Roblox-only, which is why every non-Roblox engine got zero curated
  // guidance and the model re-made the same avoidable mistakes: `path` instead of
  // Godot's `projectPath`, a Unity ACTION passed as if it were a tool name, an
  // unescaped quote inside a Blender code payload. The notes are attached by
  // list_commands and by the periodic reminder, so they are the cheapest place to
  // fix a whole class of tool-call failures.
  function toolNote(name) {
    const bare = (name || "").includes("/") ? name.split("/").pop() : (name || "");
    const hasEngine = typeof ZSEngine !== "undefined";
    // LEAD WITH THE SHAPE. A model skimming a note for "what do I write" will
    // happily skip prose about a parameter's SEMANTICS and then omit the
    // parameter itself - which is exactly how `multi_edit` produced
    // "parameters.edits[0].old_string is required" (observed live) while its note
    // talked at length about how old_string must match. A copyable envelope
    // cannot be skimmed past, so it goes first and the prose explains it after.
    const tpl = (hasEngine && ZSEngine.templateFor) ? ZSEngine.templateFor(bare) : "";
    const prefix = tpl ? "REQUIRED SHAPE (copy this, fill the values): " + tpl + "\n" : "";
    // SHORT suffix - the full studio_id explanation is stated ONCE in the prompt,
    // not repeated on all 16 place-scoped tools (that bloat is what pushed the
    // shape out of reading position in the first place).
    const suffix = (hasEngine && ZSEngine.needsStudioId && ZSEngine.needsStudioId(bare))
      ? ZSEngine.ROBLOX_STUDIO_ID_SUFFIX
      : "";
    const body = (hasEngine && ZSEngine.noteFor) ? (TOOL_NOTES[bare] || ZSEngine.noteFor(bare)) : TOOL_NOTES[bare];
    if (!body && !prefix && !suffix) return "";
    return prefix + (body || "") + suffix;
  }

  // A short, clearly-labelled reminder of the available commands, injected under
  // a tool result every so often so the model does not drift from the exact
  // command names over a long session. It is explicitly framed as an automatic
  // Multi-Script reminder (NOT a user message and NOT a new command to run).
  function toolsReminder(tools) {
    const toolsString =
      "  list_multiscript_capabilities() - compact index of direct commands, skills, specialists, and native sources\n" +
      "  list_commands({server:\"multiscript\"}) - list all Multi-Script direct commands with schemas\n" +
      "  list_commands() - list live native commands plus Multi-Script direct commands\n" +
      compactTools(tools);
    return (
      "\n\n────────────────────────────────\n" +
      "(System note from Multi-Script - this is an automatic REMINDER, not a request and not a new result. " +
      "Do NOT reply to it or run any command because of it; just keep it in mind for your next command.)\n" +
      "Reminder of the Roblox Studio commands (use exact names and parameter keys; " +
      "for other connected apps call list_mcp_servers):\n" +
      toolsString +
      "\n" + jsonContractReminder()
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
    { id: "claude", name: "Claude", speed: 8, smart: 9, stability: 9, note: "Strong coding, long-form reasoning, and careful tool use." },
    { id: "gemini", name: "Gemini", speed: 9, smart: 9, stability: 8, note: "Fast multimodal and long-context work." },
    { id: "kimi", name: "Kimi", speed: 8, smart: 8, stability: 7, note: "Strong long-context repository work." },
    { id: "glm", name: "GLM", speed: 8, smart: 8, stability: 7, note: "Efficient structured implementation." },
    { id: "qwen", name: "Qwen", speed: 8, smart: 8, stability: 8, note: "Balanced coding and tool use." },
    { id: "meta", name: "Meta AI", speed: 9, smart: 7, stability: 8, note: "Quick general-purpose production." },
  ];

  const PROVIDER_ECONOMY = {
    notion: "Use workspace context selectively. Do not restate pages or tool output; cite only the facts needed for the next action.",
    chatgpt: "Use compact structured reasoning and direct tool calls. Avoid narrating plans already implied by the command.",
    claude: "Use concise action-first replies. Keep extended reasoning internal and emit the next exact Multi-Script command without restating the whole plan.",
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
      ? "PROMPT REWRITING OFF: Preserve the user's submitted wording literally and do not display, substitute, or ask approval for a rewritten prompt. This controls rewriting only: still apply the full relevant Multi-Script skill/tool mesh silently to improve planning, craft, implementation, integration, optimization and verification."
      : promptSkills === "suggest"
        ? "PROMPT REWRITING SUGGEST-ONLY: Preserve the submitted wording and, only when useful, offer a separate optional improved prompt without blocking execution. Apply the full relevant skill/tool mesh regardless of whether the rewrite is accepted."
        : "PROMPT REWRITING AUTOMATIC: Silently clarify underspecified reversible details without changing explicit intent, while the always-on complementary skill/tool mesh improves the real project execution.";
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
    return `[MULTI-SCRIPT PROVIDER PREFERENCES — ${providerName}]\nALWAYS-ON CAPABILITY RULE: Prompt rewriting and skill/tool enhancement are independent. The selected rewrite mode never disables specialist execution quality.\n${skillRule}\n${loopRule}\n${surfaceRule}`;
  }

  function skillToolCoveragePrompt(level) {
    const depth = ["focused","full","maximum"].includes(level) ? level : "full";
    const rule = depth === "maximum"
      ? "Use the broadest relevant specialist mesh and deep craft passes while preserving the requested scope."
      : depth === "focused"
        ? "Use complete core discipline coverage plus every specialist directly connected to the requested artifact."
        : "Use broad professional studio coverage across the requested craft and all supporting engineering, performance, accessibility and validation disciplines.";
    return `[MULTI-SCRIPT SKILL + TOOL MESH — ${depth.toUpperCase()}]
${rule}
Skills, direct tools and engine virtual tools are the mechanism that improves the result. For every build request, silently coordinate specialists across design, implementation, polish, optimization and validation. Each applicable specialist must contribute to the same real artifact; do not collapse their concrete contributions into generic advice or stop at a rewritten brief. A weak one-line prompt still receives project-aware professional defaults, complete states, coherent art/interaction/audio, safe architecture, performance budgets, platform and accessibility behavior, tests and final engine evidence. Preserve explicit intent, ask only about irreversible blockers, execute through live native tools, integrate all contributions and correct defects.`;
  }

  function chatOnlyPrompt(providerName) {
    return `[MULTI-SCRIPT CHAT STUDIO — NO ENGINE CONNECTED]
This session intentionally started without Roblox Studio or another MCP engine. This overrides the earlier first-action requirement to call list_commands: do NOT call list_commands, list_mcp_servers, native engine tools, or claim project edits. Reply once that the Multi-Script chat studio is ready, then use the complete specialist mesh to produce professional designs, implementation-ready code, animation/VFX/texture specifications, SVG/Canvas-ready artwork, architecture, tests and handoff instructions directly in chat. Preserve the user's idea and make weak prompts production-grade, but label what still requires import or native-engine verification. Provider: ${providerName}.`;
  }

  // ── Autonomy (one-shot / unattended operation) ────────────────────────────
  // The user pastes a design spec and walks away. Guided mode behaves as before
  // (it may stop at a natural checkpoint or ask about a genuinely ambiguous
  // choice). One-shot mode turns the same spec into an uninterrupted build:
  // every reversible decision is made from project-aware professional defaults,
  // the agent keeps working through many tool calls, and it only ever stops for
  // the categories that are NOT safely reversible.
  const AUTONOMY_LEVELS = {
    guided: {
      label: "Guided",
      hint: "Normal behaviour - may pause at a checkpoint or ask about a real ambiguity.",
    },
    oneShot: {
      label: "One-shot (unattended)",
      hint: "Runs a pasted spec start-to-finish with no questions and no manual steps.",
    },
  };
  const AUTONOMY_IDS = Object.keys(AUTONOMY_LEVELS);

  function autonomyPrompt(level, providerName = "this provider") {
    if (level !== "oneShot") return "";
    return `[MULTI-SCRIPT ONE-SHOT / UNATTENDED MODE — ${providerName}]
The user is NOT watching this conversation. They gave a design specification and expect a finished, verified result when they return. Operate accordingly:
- NO QUESTIONS. Never end a turn asking for clarification, preference, confirmation, or permission for anything that is reversible, conventional, or inferable from the project. Decide it yourself using project-aware professional defaults, state the decision in one short line, and continue.
- NO DEAD TURNS. Never end a turn by announcing what you are about to do. If work remains, your turn MUST contain exactly one plain-text JSON command that performs the next real step.
- KEEP GOING UNTIL DONE. A spec typically needs many commands in sequence. Do not summarise progress as if the task were finished, do not stop after a first pass, and do not ask whether to continue. Inspect, implement, integrate, verify, fix what the evidence shows is broken, then continue to the next part of the spec.
- DRIVE THE WHOLE SPEC. Track the specification's parts yourself and complete every one of them: systems, UI, art/audio, data, states (win/fail/restart), platforms/inputs, performance, and validation. If the spec is vague, infer the missing detail rather than asking.
- RECOVER SILENTLY. If a command errors, times out, or returns nothing useful, read the error and try a corrected approach on your own. Do not stop to report a hiccup the user cannot act on. Only report a blocker that genuinely needs a human.
- STOP ONLY FOR THE IRREVERSIBLE. Ask (or stop) only when a choice is destructive or irreversible, costs money, needs credentials you do not have, or is genuinely contradictory. Everything else: decide and continue.
- FINISH WITH EVIDENCE. When the spec is complete, verify the result through the real engine tools (run/read back/console output), then give ONE short completion report: what was built, where it lives in the project, how it was verified, and anything honestly unfinished. No process narration, no checklist theatre.`;
  }

  // A short, unambiguous restatement of the JSON-only transport, appended to the
  // periodic command reminder. Models drift toward their own native tool-call
  // markup on long sessions; this re-anchors the one format the extension reads.
  function jsonContractReminder() {
    return (
      "(Reminder: every Multi-Script call is exactly ONE plain-text JSON object - " +
      '{"command":"exact_name","params":{...}} - with no code fence, no XML/DSML/function-call ' +
      "markup, no wrapper keys, and nothing after it. One command per reply.)"
    );
  }

  // Accurate, reason-specific feedback for a command the extension refuses to
  // run. Before this, every blocked command got the same "it timed out and is
  // unavailable" text - which was FALSE for permanently-disabled commands and
  // implied a retry could succeed, so the model retried instead of routing
  // around it. Each reason now says what is actually true and what to do next.
  //
  //   permanently-off : ZS never runs it here (e.g. subagent). No retry helps.
  //   no-vision       : the provider cannot accept images (screen_capture).
  //   unknown-block   : blocked for a reason we do not model - stay truthful
  //                     and do NOT claim a cause we cannot know.
  function blockedFeedback(bareName, provider) {
    const VISION = "screen_capture";
    const visionOk = !!(provider && provider.supportsVision);
    if (bareName === VISION && !visionOk) {
      return `ERROR: '${bareName}' cannot be used here - this assistant has no image input, so a viewport image would not be readable. Do NOT call it again. Get the same information programmatically instead: inspect_instance (properties/transforms), search_game_tree (structure), script_read (source), get_console_output (runtime errors).`;
    }
    if (PERMANENTLY_BLOCKED.has(bareName)) {
      return `ERROR: '${bareName}' is permanently disabled in Multi-Script and will NEVER run here - retrying it only wastes a turn. Do NOT call it again. Achieve the goal with the normal commands (execute_luau, multi_edit, inspect_instance, search_game_tree, script_read, get_console_output).`;
    }
    return `ERROR: the '${bareName}' command is not available in this environment. Do NOT call it again - complete the task yourself using the other commands (execute_luau, multi_edit, etc.).`;
  }

  // Commands the extension refuses outright, regardless of engine or provider.
  // Kept here (not only in main.js) so the refusal text and the block list
  // cannot drift apart - the drift is exactly what produced the misleading
  // "timed out" message for `subagent`.
  const PERMANENTLY_BLOCKED = new Set(["subagent"]);

  function sanitizeAutonomy(level) {
    return AUTONOMY_IDS.includes(level) ? level : "guided";
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
    toolNote,
    buildSystemPrompt,
    compactTools,
    toolsReminder,
    memoryNudge,
    economyPrompt,
    optimizeInjectedText,
    compactSystemReminder,
    skillToolCoveragePrompt,
    providerBehaviorPrompt,
    chatOnlyPrompt,
    AUTONOMY_LEVELS,
    AUTONOMY_IDS,
    autonomyPrompt,
    sanitizeAutonomy,
    jsonContractReminder,
    blockedFeedback,
    PERMANENTLY_BLOCKED,
    PROVIDER_BENCHMARKS,
    TOOL_NOTES,
  };
})();
