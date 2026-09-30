--!strict
-- Multi-Script Companion for Roblox Studio
-- Diagnostic/readiness companion only. Real execution remains exclusively on
-- Roblox Studio's official live MCP server and its currently advertised tools.

local HttpService = game:GetService("HttpService")
local Selection = game:GetService("Selection")
local RunService = game:GetService("RunService")
local ServerStorage = game:GetService("ServerStorage")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local TestService = game:GetService("TestService")

local PLUGIN_VERSION = "6.17.3"
local DIAGNOSTIC_VERSION = 2
local BRIDGE_URL = "http://127.0.0.1:17614"
local POLL_SECONDS = 5
local MAX_ISSUES = 100
local SCAN_YIELD_EVERY = 150

local toolbar = plugin:CreateToolbar("Multi-Script")
local toggleButton = toolbar:CreateButton("Multi-Script Companion", "Show MCP health and project readiness", "rbxassetid://4458901886")
toggleButton.ClickableWhenViewportHidden = true
local widgetInfo = DockWidgetPluginGuiInfo.new(Enum.InitialDockState.Right, false, false, 420, 640, 340, 420)
local widget = plugin:CreateDockWidgetPluginGui("MultiScriptCompanion_v6", widgetInfo)
widget.Title = "Multi-Script Companion"

local root = Instance.new("ScrollingFrame")
root.Name = "Root"
root.Size = UDim2.fromScale(1, 1)
root.BackgroundColor3 = Color3.fromRGB(24, 26, 32)
root.BorderSizePixel = 0
root.ScrollBarThickness = 6
root.AutomaticCanvasSize = Enum.AutomaticSize.Y
root.CanvasSize = UDim2.new()
root.Parent = widget
local padding = Instance.new("UIPadding")
padding.PaddingTop = UDim.new(0, 12); padding.PaddingBottom = UDim.new(0, 12); padding.PaddingLeft = UDim.new(0, 12); padding.PaddingRight = UDim.new(0, 12); padding.Parent = root
local list = Instance.new("UIListLayout")
list.Padding = UDim.new(0, 8); list.SortOrder = Enum.SortOrder.LayoutOrder; list.Parent = root

local function label(text: string, height: number, bold: boolean?): TextLabel
	local item = Instance.new("TextLabel")
	item.Size = UDim2.new(1, 0, 0, height); item.BackgroundTransparency = 1; item.Text = text
	item.TextColor3 = Color3.fromRGB(226, 230, 240); item.TextSize = bold and 18 or 13
	item.Font = bold and Enum.Font.GothamBold or Enum.Font.Gotham; item.TextWrapped = true
	item.TextXAlignment = Enum.TextXAlignment.Left; item.TextYAlignment = Enum.TextYAlignment.Top; item.Parent = root
	return item
end
local function button(text: string): TextButton
	local item = Instance.new("TextButton")
	item.Size = UDim2.new(1, 0, 0, 36); item.BackgroundColor3 = Color3.fromRGB(52, 91, 196)
	item.AutoButtonColor = true; item.Text = text; item.TextColor3 = Color3.new(1, 1, 1); item.TextSize = 13; item.Font = Enum.Font.GothamSemibold; item.Parent = root
	local corner = Instance.new("UICorner"); corner.CornerRadius = UDim.new(0, 6); corner.Parent = item
	return item
end
local function color(item: TextLabel, state: string)
	item.TextColor3 = state == "good" and Color3.fromRGB(101, 214, 143) or state == "warn" and Color3.fromRGB(244, 190, 80) or state == "bad" and Color3.fromRGB(245, 112, 120) or Color3.fromRGB(226, 230, 240)
end

label("Multi-Script Companion", 26, true)
local bridgeStatus = label("Bridge: checking…", 38)
local studioStatus = label("Studio MCP: checking…", 52)
local compatibilityStatus = label("Compatibility: checking versions…", 38)
local readinessStatus = label("Readiness: scan not run", 46)
local projectStatus = label("Project inventory: waiting", 46)
local selectionStatus = label("Selection: none", 34)
local refreshButton = button("Refresh live status")
local scanButton = button("Run non-destructive readiness scan")
local diagnosticsButton = button("Print sanitized diagnostic report")
local catalogButton = button("Print official native tool catalog")
local prepareButton = button("Create optional Multi-Script folders")
local catalogStatus = label("Native catalog not loaded", 42)
local guidance = label("This plugin is diagnostic-only. It never polls for AI jobs, executes tool calls, reads secrets, uploads source, or manufactures Roblox commands. All project mutations remain on Roblox Studio's official live MCP tools.", 86)
guidance.TextColor3 = Color3.fromRGB(166, 176, 200)

local alive = true
local busy = false
local lastStatus: {[string]: any}? = nil
local lastScan = { score = 0, issueCount = 0, high = 0, medium = 0, low = 0, scanned = 0, unreadable = 0, durationMs = 0 }

local function inventory()
	local result = { scripts = 0, modules = 0, remotes = 0, animations = 0, sounds = 0, particleEmitters = 0, screenGuis = 0, descendants = 0 }
	for _, instance in game:GetDescendants() do
		result.descendants += 1
		if instance:IsA("ModuleScript") then result.modules += 1
		elseif instance:IsA("LuaSourceContainer") then result.scripts += 1
		elseif instance:IsA("RemoteEvent") or instance:IsA("RemoteFunction") then result.remotes += 1
		elseif instance:IsA("Animation") then result.animations += 1
		elseif instance:IsA("Sound") then result.sounds += 1
		elseif instance:IsA("ParticleEmitter") then result.particleEmitters += 1
		elseif instance:IsA("ScreenGui") then result.screenGuis += 1 end
	end
	return result
end

local function readinessScore(): number
	local score = lastScan.score
	if lastStatus then
		local roblox = lastStatus.roblox or {}
		if roblox.connected then score = math.min(100, score + 10)
		elseif roblox.alive then score = math.max(0, score - 10)
		else score = math.max(0, score - 25) end
	end
	return score
end

local function heartbeat()
	local inv = inventory()
	local body = HttpService:JSONEncode({ pluginVersion = PLUGIN_VERSION, diagnosticVersion = DIAGNOSTIC_VERSION, placeId = game.PlaceId, placeName = string.sub(game.Name, 1, 120), isRunning = RunService:IsRunning(), selectionCount = #Selection:Get(), scriptCount = inv.scripts, moduleCount = inv.modules, readinessScore = readinessScore(), issueCount = lastScan.issueCount, highIssueCount = lastScan.high, scanDurationMs = lastScan.durationMs })
	pcall(function() HttpService:RequestAsync({ Url = BRIDGE_URL .. "/plugin/heartbeat", Method = "POST", Headers = { ["Content-Type"] = "application/json" }, Body = body }) end)
end

local function updateSelection()
	local selected = Selection:Get()
	if #selected == 0 then selectionStatus.Text = "Selection: none"; return end
	local first = selected[1]
	selectionStatus.Text = string.format("Selection: %d instance(s) · first: %s (%s)", #selected, string.sub(first:GetFullName(), 1, 90), first.ClassName)
end

local function refresh()
	if busy then return end
	busy = true
	local ok, response = pcall(function() return HttpService:RequestAsync({ Url = BRIDGE_URL .. "/status", Method = "GET", Headers = { ["Accept"] = "application/json" } }) end)
	if ok and response.Success then
		local decodedOk, data = pcall(function() return HttpService:JSONDecode(response.Body) end)
		if decodedOk and type(data) == "table" then
			lastStatus = data
			local roblox = data.roblox or {}; local nativeTools = tonumber(roblox.nativeTools) or 0; local bridgeVersion = tostring(data.bridgeVersion or "?")
			bridgeStatus.Text = string.format("Bridge: online · v%s · loopback only", bridgeVersion); color(bridgeStatus, "good")
			if bridgeVersion == PLUGIN_VERSION then compatibilityStatus.Text = "Compatibility: plugin and bridge versions match"; color(compatibilityStatus, "good")
			else compatibilityStatus.Text = string.format("Compatibility: plugin v%s / bridge v%s · update the older component", PLUGIN_VERSION, bridgeVersion); color(compatibilityStatus, "warn") end
			if roblox.connected then studioStatus.Text = string.format("Studio MCP: connected to a loaded place · %d official native tools", nativeTools); color(studioStatus, nativeTools > 0 and "good" or "warn")
			elseif roblox.alive then studioStatus.Text = string.format("Studio MCP proxy: alive, but no usable place · %d cached tools", nativeTools); color(studioStatus, "warn")
			else studioStatus.Text = "Studio MCP: offline · start bridge and enable Studio as MCP Server"; color(studioStatus, "bad") end
			heartbeat()
		else bridgeStatus.Text = "Bridge: invalid local response"; color(bridgeStatus, "bad") end
	else
		bridgeStatus.Text = "Bridge: offline, or Studio HTTP Requests are disabled"; color(bridgeStatus, "bad")
		studioStatus.Text = "Enable Game Settings → Security → Allow HTTP Requests, then start Multi-Script"; color(studioStatus, "warn")
		compatibilityStatus.Text = "Compatibility: unavailable until the local bridge responds"; color(compatibilityStatus, "warn")
	end
	updateSelection(); busy = false
end

local RISK_PATTERNS = {
	{ pattern = "loadstring", label = "dynamic loadstring execution", severity = "high" },
	{ pattern = "require%s*%(%s*%d+", label = "numeric asset require; verify ownership and supply chain", severity = "high" },
	{ pattern = "OnServerInvoke", label = "RemoteFunction server handler; validate input, timeout, and failure behavior", severity = "medium" },
	{ pattern = "OnServerEvent", label = "RemoteEvent server handler; validate input, authority, and rate limits", severity = "medium" },
	{ pattern = "GetAsync%s*%(", label = "DataStore GetAsync; verify pcall, budgets, retry, and cancellation", severity = "medium" },
	{ pattern = "SetAsync%s*%(", label = "DataStore SetAsync; prefer conflict-safe UpdateAsync where contested", severity = "medium" },
	{ pattern = "while%s+true%s+do", label = "unbounded loop; verify yield, cancellation, and teardown", severity = "medium" },
	{ pattern = "WaitForChild%s*%([^,%)]*%)", label = "WaitForChild without explicit timeout", severity = "low" },
	{ pattern = "task%.spawn%s*%(", label = "spawned task; verify ownership and cancellation", severity = "low" },
}

local function scanProject()
	if busy then return end
	busy = true; scanButton.Text = "Scanning…"; local started = os.clock(); local issues = {}; local counts = { high = 0, medium = 0, low = 0 }; local scanned = 0; local unreadable = 0; local visited = 0
	for _, instance in game:GetDescendants() do
		visited += 1
		if instance:IsA("LuaSourceContainer") then
			scanned += 1
			local sourceOk, source = pcall(function() return instance.Source end)
			if sourceOk then
				for _, entry in RISK_PATTERNS do
					if string.find(source, entry.pattern) then
						counts[entry.severity] += 1
						table.insert(issues, { path = instance:GetFullName(), severity = entry.severity, message = entry.label })
						if #issues >= MAX_ISSUES then break end
					end
				end
			else unreadable += 1 end
		end
		if #issues >= MAX_ISSUES then break end
		if visited % SCAN_YIELD_EVERY == 0 then task.wait() end
	end
	local durationMs = math.floor((os.clock() - started) * 1000 + 0.5); local score = math.max(0, 100 - counts.high * 12 - counts.medium * 4 - counts.low - unreadable * 2)
	lastScan = { score = score, issueCount = #issues, high = counts.high, medium = counts.medium, low = counts.low, scanned = scanned, unreadable = unreadable, durationMs = durationMs }
	readinessStatus.Text = string.format("Readiness: %d/100 · %d high · %d medium · %d low", score, counts.high, counts.medium, counts.low); color(readinessStatus, counts.high > 0 and "bad" or counts.medium > 0 and "warn" or "good")
	projectStatus.Text = string.format("Project scan: %d scripts · %d unreadable · %d ms · capped at %d findings", scanned, unreadable, durationMs, MAX_ISSUES)
	if #issues > 0 then
		local lines = {}; for _, issue in issues do table.insert(lines, string.format("[%s] %s — %s", string.upper(issue.severity), issue.path, issue.message)) end
		warn("[Multi-Script] Non-destructive readiness findings (review context; heuristics are not proof of a bug):\n- " .. table.concat(lines, "\n- "))
	else color(projectStatus, "good") end
	scanButton.Text = "Run non-destructive readiness scan"; busy = false; heartbeat()
end

local function printCatalog()
	local ok, response = pcall(function() return HttpService:RequestAsync({ Url = BRIDGE_URL .. "/catalog", Method = "GET", Headers = { ["Accept"] = "application/json" } }) end)
	if not ok or not response.Success then catalogStatus.Text = "Catalog unavailable · check bridge and HTTP Requests"; color(catalogStatus, "warn"); return end
	local decodedOk, data = pcall(function() return HttpService:JSONDecode(response.Body) end)
	if not decodedOk or type(data) ~= "table" then catalogStatus.Text = "Invalid catalog response"; color(catalogStatus, "bad"); return end
	local tools = data.tools or {}; catalogStatus.Text = string.format("Official native: %d · Multi-Script direct: %d · Roblox specialists: %d virtual + %d skills", #tools, tonumber(data.multiScriptDirectTools) or 0, tonumber(data.robloxVirtualTools) or 0, tonumber(data.robloxSkills) or 0); color(catalogStatus, #tools > 0 and "good" or "warn")
	local names = {}; for _, tool in tools do table.insert(names, tostring(tool.name or "?")) end
	print("[Multi-Script] Official Roblox Studio MCP catalog (controlled by Roblox):\n- " .. table.concat(names, "\n- "))
end

local function diagnosticReport()
	local inv = inventory(); local roblox = lastStatus and lastStatus.roblox or {}
	local report = { product = "Multi-Script Companion", pluginVersion = PLUGIN_VERSION, diagnosticVersion = DIAGNOSTIC_VERSION, bridgeVersion = lastStatus and lastStatus.bridgeVersion or nil, studioMcpConnected = roblox and roblox.connected == true, officialNativeToolCount = roblox and roblox.nativeTools or 0, placeId = game.PlaceId, placeName = string.sub(game.Name, 1, 120), isRunning = RunService:IsRunning(), selectionCount = #Selection:Get(), inventory = inv, readiness = lastScan, privacy = "No source, secrets, assets, or AI jobs are included." }
	print("[Multi-Script] Sanitized diagnostic report:\n" .. HttpService:JSONEncode(report))
end

local function getOrCreateFolder(parent: Instance, name: string): Folder
	local existing = parent:FindFirstChild(name)
	if existing and existing:IsA("Folder") then return existing end
	local folder = Instance.new("Folder"); folder.Name = name; folder:SetAttribute("MultiScriptManaged", true); folder.Parent = parent; return folder
end
local function prepareFolders()
	getOrCreateFolder(ServerStorage, "MultiScript"); getOrCreateFolder(ReplicatedStorage, "MultiScript"); getOrCreateFolder(TestService, "MultiScriptTests")
	projectStatus.Text = "Optional workspace ready · only missing folders were created"; color(projectStatus, "good"); heartbeat()
end

toggleButton.Click:Connect(function() widget.Enabled = not widget.Enabled; if widget.Enabled then task.spawn(refresh) end end)
refreshButton.Activated:Connect(function() task.spawn(refresh) end)
scanButton.Activated:Connect(function() task.spawn(scanProject) end)
diagnosticsButton.Activated:Connect(diagnosticReport)
catalogButton.Activated:Connect(function() task.spawn(printCatalog) end)
prepareButton.Activated:Connect(prepareFolders)
Selection.SelectionChanged:Connect(updateSelection)
widget:GetPropertyChangedSignal("Enabled"):Connect(function() toggleButton:SetActive(widget.Enabled) end)
plugin.Unloading:Connect(function() alive = false end)
task.spawn(function() while alive do if widget.Enabled then refresh() end task.wait(POLL_SECONDS) end end)
