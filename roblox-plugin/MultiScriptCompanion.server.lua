--!nocheck
-- Multi-Script Companion for Roblox Studio
-- Secure, opt-in companion MCP tools. No arbitrary code execution.

local HttpService = game:GetService("HttpService")
local Selection = game:GetService("Selection")
local RunService = game:GetService("RunService")
local CollectionService = game:GetService("CollectionService")
local ChangeHistoryService = game:GetService("ChangeHistoryService")

local PLUGIN_VERSION = "6.24.0"
local DIAGNOSTIC_VERSION = 3
local BRIDGE_URL = "http://127.0.0.1:17614"
local STATUS_SECONDS = 5
local POLL_SECONDS = 0.35
local MAX_BATCH, MAX_TREE, MAX_SOURCE, MAX_OUTPUT = 200, 500, 500000, 900000

local ROOT_NAMES = {Workspace=true, ReplicatedStorage=true, ServerStorage=true, StarterGui=true, StarterPlayer=true, Lighting=true, SoundService=true, Teams=true, TestService=true}
local CREATE_CLASSES = {
	Folder=true, Model=true, Configuration=true, Part=true, MeshPart=true, WedgePart=true, TrussPart=true,
	Attachment=true, WeldConstraint=true, Motor6D=true, ObjectValue=true, StringValue=true, BoolValue=true,
	IntValue=true, NumberValue=true, Vector3Value=true, CFrameValue=true, Color3Value=true, RemoteEvent=true,
	RemoteFunction=true, BindableEvent=true, BindableFunction=true, ScreenGui=true, Frame=true, TextLabel=true,
	TextButton=true, ImageLabel=true, ImageButton=true, UIListLayout=true, UIGridLayout=true, UIPadding=true,
	UICorner=true, UIStroke=true, Sound=true, Animation=true, ParticleEmitter=true, Beam=true, Trail=true,
	Highlight=true, ProximityPrompt=true, ClickDetector=true, SpawnLocation=true, Team=true,
}
local BLOCKED_PROPERTIES = {Parent=true, Source=true, ClassName=true, Archivable=true, RobloxLocked=true}
local PERMISSION_RANK = {off=0, read=1, project=2, full=3}
local TOOL_PERMISSION = {
	msrb_project_summary="read", msrb_get_selection="read", msrb_get_tree="read", msrb_find_instances="read",
	msrb_get_properties="read", msrb_export_snapshot="read", msrb_validate_paths="read",
	msrb_read_script="full", msrb_search_scripts="full", msrb_dependency_graph="full",
	msrb_set_selection="project", msrb_create_instances="project", msrb_set_properties="project",
	msrb_set_attributes="project", msrb_update_tags="project", msrb_reparent_instances="project",
	msrb_clone_instances="project", msrb_bulk_rename="project", msrb_checkpoint="project",
	msrb_patch_script="full", msrb_replace_script="full", msrb_delete_instances="full",
	msrb_undo="full", msrb_redo="full",
}

local alive, busy = true, false
local executionMode, sessionToken = "off", nil
local fullConfirmUntil = 0
local lastStatus = nil
local lastScan = {score=0, issueCount=0, high=0, medium=0, low=0, scanned=0, unreadable=0, durationMs=0}
local pluginId = plugin:GetSetting("MultiScriptPluginId")
if type(pluginId) ~= "string" or #pluginId < 8 then
	pluginId = HttpService:GenerateGUID(false)
	plugin:SetSetting("MultiScriptPluginId", pluginId)
end

local toolbar = plugin:CreateToolbar("Multi-Script")
local toggleButton = toolbar:CreateButton("Multi-Script Companion", "Secure MCP tools, health, and project readiness", "rbxassetid://4458901886")
toggleButton.ClickableWhenViewportHidden = true
local widget = plugin:CreateDockWidgetPluginGui("MultiScriptCompanion_v8", DockWidgetPluginGuiInfo.new(Enum.InitialDockState.Right, false, false, 460, 720, 380, 460))
widget.Title = "Multi-Script Companion"
-- A panel nobody can find is a panel that does not work. Open on first install
-- (so the toolbar button has something to toggle), then remember the choice.
local savedOpen = plugin:GetSetting("MultiScriptWidgetOpen")
widget.Enabled = savedOpen == nil and true or savedOpen == true
toggleButton:SetActive(widget.Enabled)

-- ─────────────────────────────────────────────────────────────────────────────
-- Interface
--
-- The panel mirrors the Multi-Script extension's monochrome design language: the
-- same surface ramp (#06060a -> #1a1a21), hairline 1px borders, the same ink ramp
-- (bright label -> quiet metadata), the same status colours, and the same two
-- button weights - a LIGHT primary carrying dark ink, and a dark secondary with a
-- hairline border. Roblox has no CSS, so these tokens are the single source of
-- truth and every element below is built from them.
-- ─────────────────────────────────────────────────────────────────────────────
local T = {
	mono0=Color3.fromRGB(6,6,10), mono1=Color3.fromRGB(11,11,15),
	mono2=Color3.fromRGB(18,18,23), mono3=Color3.fromRGB(26,26,33),
	ink=Color3.fromRGB(244,244,246), ink2=Color3.fromRGB(200,200,212),
	ink3=Color3.fromRGB(150,150,164), ink4=Color3.fromRGB(106,106,120),
	accent=Color3.fromRGB(216,216,226),
	good=Color3.fromRGB(126,240,184), warn=Color3.fromRGB(232,195,122), bad=Color3.fromRGB(247,156,156),
}
local HAIRLINE = 0.90
local THIN = utf8.char(0x2009) -- approximates the extension's letter-spacing

local function corner(parent, radius)
	local c = Instance.new("UICorner"); c.CornerRadius = UDim.new(0, radius or 9); c.Parent = parent; return c
end
local function hairline(parent, transparency)
	local s = Instance.new("UIStroke"); s.Color = T.accent; s.Transparency = transparency or HAIRLINE
	s.Thickness = 1; s.ApplyStrokeMode = Enum.ApplyStrokeMode.Border; s.Parent = parent; return s
end
local function pad(parent, px)
	local p = Instance.new("UIPadding")
	p.PaddingTop = UDim.new(0, px); p.PaddingBottom = UDim.new(0, px)
	p.PaddingLeft = UDim.new(0, px); p.PaddingRight = UDim.new(0, px); p.Parent = parent; return p
end
local function stack(parent, gap)
	local l = Instance.new("UIListLayout"); l.Padding = UDim.new(0, gap or 8)
	l.SortOrder = Enum.SortOrder.LayoutOrder; l.Parent = parent; return l
end

local root = Instance.new("ScrollingFrame")
root.Name = "Root"; root.Size = UDim2.fromScale(1, 1)
root.BackgroundColor3 = T.mono0; root.BorderSizePixel = 0
root.ScrollBarThickness = 4; root.ScrollBarImageColor3 = T.mono3
root.AutomaticCanvasSize = Enum.AutomaticSize.Y; root.CanvasSize = UDim2.new()
root.Parent = widget
pad(root, 14); stack(root, 10)
-- The panel's own vertical gradient: the extension's
-- linear-gradient(180deg, --mono-1, --mono-0 78%), reproduced with a UIGradient.
local rootGrad = Instance.new("UIGradient")
rootGrad.Rotation = 90; rootGrad.Color = ColorSequence.new(T.mono1, T.mono0); rootGrad.Parent = root

-- Header: mark + brand + version chip, exactly like the extension's bar.
local header = Instance.new("Frame")
header.Name = "Header"; header.BackgroundTransparency = 1
header.Size = UDim2.new(1, 0, 0, 44); header.LayoutOrder = 1; header.Parent = root
local mark = Instance.new("TextLabel")
mark.BackgroundTransparency = 1; mark.Size = UDim2.new(0, 18, 1, 0)
mark.Font = Enum.Font.GothamBold; mark.TextSize = 15; mark.TextColor3 = T.accent
mark.Text = "\u{2726}"; mark.TextXAlignment = Enum.TextXAlignment.Left; mark.Parent = header
local brand = Instance.new("TextLabel")
brand.BackgroundTransparency = 1; brand.Size = UDim2.new(0, 150, 1, 0); brand.Position = UDim2.new(0, 22, 0, 0)
brand.Font = Enum.Font.GothamBold; brand.TextSize = 16; brand.TextColor3 = T.ink
brand.Text = "Multi-Script"; brand.TextXAlignment = Enum.TextXAlignment.Left; brand.Parent = header
local versionChip = Instance.new("TextLabel")
versionChip.BackgroundColor3 = T.mono2; versionChip.Size = UDim2.new(0, 56, 0, 18)
versionChip.Position = UDim2.new(1, -56, 0.5, -9); versionChip.BorderSizePixel = 0
versionChip.Font = Enum.Font.GothamSemibold; versionChip.TextSize = 10; versionChip.TextColor3 = T.ink3
versionChip.Text = "v"..PLUGIN_VERSION; versionChip.Parent = header
corner(versionChip, 6); hairline(versionChip, 0.94)

-- A card is the panel's only container: mono-1 fill, 9px radius, hairline border,
-- and it grows to fit its contents.
local function card(order)
	local f = Instance.new("Frame")
	f.Name = "Card"; f.BackgroundColor3 = T.mono1; f.BorderSizePixel = 0
	f.Size = UDim2.new(1, 0, 0, 0); f.AutomaticSize = Enum.AutomaticSize.Y
	f.LayoutOrder = order; f.Parent = root
	corner(f, 9); hairline(f); pad(f, 12); stack(f, 8)
	return f
end

-- Section micro-label: uppercase, small, letter-spaced, quietest ink.
local function micro(parent, order, text)
	local l = Instance.new("TextLabel")
	l.BackgroundTransparency = 1; l.Size = UDim2.new(1, 0, 0, 13)
	l.Font = Enum.Font.GothamBold; l.TextSize = 10; l.TextColor3 = T.ink4
	l.TextXAlignment = Enum.TextXAlignment.Left; l.TextYAlignment = Enum.TextYAlignment.Center
	l.Text = (text:upper():gsub(".", function(c) return c..THIN end))
	l.LayoutOrder = order; l.Parent = parent; return l
end

-- Status colours are applied through color(), which also tints the row's dot.
-- The pairing lives in DOT_FOR so that the status logic never has to know a dot
-- exists - every existing color(x, state) call lights its dot for free.
local DOT_FOR = {}
local function statusRow(parent, order, text, height)
	local row = Instance.new("Frame")
	row.BackgroundTransparency = 1; row.Size = UDim2.new(1, 0, 0, height)
	row.LayoutOrder = order; row.Parent = parent
	local dot = Instance.new("Frame")
	dot.Size = UDim2.new(0, 7, 0, 7); dot.Position = UDim2.new(0, 1, 0, 5)
	dot.BackgroundColor3 = T.ink4; dot.BorderSizePixel = 0; dot.Parent = row
	local dc = Instance.new("UICorner"); dc.CornerRadius = UDim.new(1, 0); dc.Parent = dot
	local lbl = Instance.new("TextLabel")
	lbl.BackgroundTransparency = 1; lbl.Size = UDim2.new(1, -15, 1, 0); lbl.Position = UDim2.new(0, 15, 0, 0)
	lbl.Font = Enum.Font.Gotham; lbl.TextSize = 13; lbl.TextColor3 = T.ink2
	lbl.TextWrapped = true; lbl.TextXAlignment = Enum.TextXAlignment.Left
	lbl.TextYAlignment = Enum.TextYAlignment.Top; lbl.Text = text; lbl.Parent = row
	DOT_FOR[lbl] = dot
	return lbl, dot
end

-- Primary = the extension's "Start" weight: light fill, dark ink, bold.
local function primaryButton(parent, order, text, height)
	local b = Instance.new("TextButton")
	b.Size = UDim2.new(1, 0, 0, height or 34); b.BackgroundColor3 = T.ink; b.BorderSizePixel = 0
	b.AutoButtonColor = true; b.Font = Enum.Font.GothamBold; b.TextSize = 13
	b.TextColor3 = T.mono0; b.Text = text; b.LayoutOrder = order; b.Parent = parent
	corner(b, 9); return b
end
-- Secondary = a dark surface with a hairline border and light ink.
local function secondaryButton(parent, order, text, height)
	local b = Instance.new("TextButton")
	b.Size = UDim2.new(1, 0, 0, height or 32); b.BackgroundColor3 = T.mono3; b.BorderSizePixel = 0
	b.AutoButtonColor = true; b.Font = Enum.Font.GothamSemibold; b.TextSize = 12
	b.TextColor3 = T.ink2; b.Text = text; b.LayoutOrder = order; b.Parent = parent
	corner(b, 9); hairline(b, 0.92); return b
end
-- Two secondary buttons sharing one row.
local function buttonPair(parent, order, textA, textB, height)
	local row = Instance.new("Frame")
	row.BackgroundTransparency = 1; row.Size = UDim2.new(1, 0, 0, height or 32)
	row.LayoutOrder = order; row.Parent = parent
	local function half(x, text)
		local b = Instance.new("TextButton")
		b.Size = UDim2.new(0.5, -3, 1, 0); b.Position = UDim2.new(x, x > 0 and 3 or 0, 0, 0)
		b.BackgroundColor3 = T.mono3; b.BorderSizePixel = 0; b.AutoButtonColor = true
		b.Font = Enum.Font.GothamSemibold; b.TextSize = 12; b.TextColor3 = T.ink2
		b.Text = text; b.Parent = row; corner(b, 9); hairline(b, 0.92); return b
	end
	return half(0, textA), half(0.5, textB)
end

-- ── Status ────────────────────────────────────────────────────────────────
local statusCard = card(2)
local bridgeStatus = statusRow(statusCard, 1, "Bridge: checking\u{2026}", 38)
local studioStatus = statusRow(statusCard, 2, "Studio MCP: checking\u{2026}", 52)
local compatibilityStatus = statusRow(statusCard, 3, "Compatibility: checking versions\u{2026}", 38)
local permissionStatus = statusRow(statusCard, 4, "Elevated companion tools: OFF (safe default)", 44)

-- ── Tool access ───────────────────────────────────────────────────────────
local accessCard = card(3)
micro(accessCard, 1, "Tool access")
local readButton = primaryButton(accessCard, 2, "Enable Read tools (7)")
local projectButton = secondaryButton(accessCard, 3, "Enable Project tools (16)")
local fullButton = secondaryButton(accessCard, 4, "Enable Full tools (24) \u{2014} click twice")
local disableButton = secondaryButton(accessCard, 5, "Disable elevated tools")

-- ── Project ───────────────────────────────────────────────────────────────
local projectCard = card(4)
micro(projectCard, 1, "Project")
local barTrack = Instance.new("Frame")
barTrack.BackgroundColor3 = T.mono3; barTrack.BorderSizePixel = 0
barTrack.Size = UDim2.new(1, 0, 0, 4); barTrack.LayoutOrder = 2; barTrack.Parent = projectCard
corner(barTrack, 2)
local readinessBar = Instance.new("Frame")
readinessBar.BackgroundColor3 = T.ink2; readinessBar.BorderSizePixel = 0
readinessBar.Size = UDim2.new(0, 0, 1, 0); readinessBar.Parent = barTrack
corner(readinessBar, 2)
local readinessStatus = statusRow(projectCard, 3, "Readiness: scan not run", 34)
local projectStatus = statusRow(projectCard, 4, "Project inventory: waiting", 46)
local selectionStatus = statusRow(projectCard, 5, "Selection: none", 34)
local refreshButton, diagnosticsButton = buttonPair(projectCard, 6, "Refresh status", "Print diagnostics")
-- Full width: the "non-destructive" reassurance is part of the label and must not
-- be clipped by a half-width button.
local scanButton = secondaryButton(projectCard, 7, "Run non-destructive readiness scan")
local catalogButton = secondaryButton(projectCard, 8, "Print tool catalog")

-- ── Footnote ──────────────────────────────────────────────────────────────
local footnote = Instance.new("Frame")
footnote.BackgroundTransparency = 1; footnote.Size = UDim2.new(1, 0, 0, 0)
footnote.AutomaticSize = Enum.AutomaticSize.Y; footnote.LayoutOrder = 5; footnote.Parent = root
stack(footnote, 8); pad(footnote, 2)
local rule = Instance.new("Frame")
rule.BackgroundColor3 = T.accent; rule.BackgroundTransparency = 0.94
rule.BorderSizePixel = 0; rule.Size = UDim2.new(1, 0, 0, 1); rule.LayoutOrder = 1; rule.Parent = footnote
local guidance = Instance.new("TextLabel")
guidance.BackgroundTransparency = 1; guidance.Size = UDim2.new(1, 0, 0, 0)
guidance.AutomaticSize = Enum.AutomaticSize.Y; guidance.LayoutOrder = 2
guidance.Font = Enum.Font.Gotham; guidance.TextSize = 11; guidance.TextColor3 = T.ink4
guidance.TextWrapped = true; guidance.TextXAlignment = Enum.TextXAlignment.Left
guidance.TextYAlignment = Enum.TextYAlignment.Top; guidance.Parent = footnote
guidance.Text = "Elevated tools are OFF by default. Read can inspect metadata; Project can make bounded non-source changes; Full can read and edit source, delete, and undo. Every tool is allowlisted and permission-checked twice. No loadstring, arbitrary Lua evaluation, remote upload, or HTTP job injection is supported."

-- Fills the readiness bar and tints it by band, so the panel reads at a glance.
local function setReadiness(value)
	local v = tonumber(value) or 0
	readinessBar.Size = UDim2.new(math.max(0, math.min(100, v)) / 100, 0, 1, 0)
	readinessBar.BackgroundColor3 = v >= 80 and T.good or v >= 50 and T.warn or T.bad
end

-- The single status setter every status line already calls. It tints the text AND
-- lights the paired dot (see DOT_FOR), so state is legible at a glance instead of
-- having to be read out of a sentence.
local function color(item, state)
	item.TextColor3 = state == "good" and T.good or state == "warn" and T.warn
		or state == "bad" and T.bad or T.ink2
	local dot = DOT_FOR[item]
	if dot then
		dot.BackgroundColor3 = item.TextColor3
		dot.BackgroundTransparency = (state == nil) and 0.55 or 0
	end
end

local function request(method, route, body, authorized)
	local headers={Accept="application/json"}
	if body ~= nil then headers["Content-Type"]="application/json" end
	if authorized and sessionToken then headers.Authorization="Bearer "..sessionToken end
	return HttpService:RequestAsync({Url=BRIDGE_URL..route, Method=method, Headers=headers, Body=body and HttpService:JSONEncode(body) or nil})
end
local function decodeResponse(response)
	if not response or not response.Success then return nil end
	local ok,data=pcall(function() return HttpService:JSONDecode(response.Body) end)
	return ok and data or nil
end
local function safeName(value)
	local s=tostring(value or ""):sub(1,100)
	if s=="" or s:find("[%z\1-\31]") then error("invalid instance name") end
	return s
end
local function pathOf(inst) return inst:GetFullName() end
local function splitPath(path)
	local clean=tostring(path or ""):gsub("^game[%.%/]?",""):gsub("/",".")
	local parts={}; for part in clean:gmatch("[^%.]+") do table.insert(parts,part) end; return parts
end
local function resolve(path)
	local parts=splitPath(path); if #parts==0 or not ROOT_NAMES[parts[1]] then return nil end
	local ok,current=pcall(function() return game:GetService(parts[1]) end); if not ok then return nil end
	for i=2,#parts do current=current:FindFirstChild(parts[i]); if not current then return nil end end
	return current
end
local function isRoot(inst) return inst.Parent==game and ROOT_NAMES[inst.Name]==true end
local function bounded(value, fallback, low, high) value=tonumber(value) or fallback; return math.max(low,math.min(high,math.floor(value))) end
local function listBound(value, max) if type(value)~="table" then return {} end; local out={}; for i=1,math.min(#value,max) do out[i]=value[i] end; return out end

local function encodeValue(value, depth)
	depth=depth or 0; if depth>4 then return "<depth-limit>" end
	local kind=typeof(value)
	if kind=="nil" or kind=="boolean" or kind=="number" or kind=="string" then return value end
	if kind=="Instance" then return {__type="Instance",path=pathOf(value),className=value.ClassName} end
	if kind=="Vector2" then return {__type="Vector2",x=value.X,y=value.Y} end
	if kind=="Vector3" then return {__type="Vector3",x=value.X,y=value.Y,z=value.Z} end
	if kind=="Color3" then return {__type="Color3",r=value.R,g=value.G,b=value.B} end
	if kind=="CFrame" then return {__type="CFrame",components={value:GetComponents()}} end
	if kind=="UDim" then return {__type="UDim",scale=value.Scale,offset=value.Offset} end
	if kind=="UDim2" then return {__type="UDim2",x={scale=value.X.Scale,offset=value.X.Offset},y={scale=value.Y.Scale,offset=value.Y.Offset}} end
	if kind=="BrickColor" then return {__type="BrickColor",name=value.Name} end
	if kind=="EnumItem" then return {__type="Enum",value=tostring(value)} end
	if kind=="table" then local out={}; local n=0; for k,v in pairs(value) do n+=1; if n>200 then break end; out[tostring(k)]=encodeValue(v,depth+1) end; return out end
	return tostring(value)
end
local function decodeValue(value)
	if type(value)~="table" or type(value.__type)~="string" then return value end
	local t=value.__type
	if t=="Vector2" then return Vector2.new(tonumber(value.x) or 0,tonumber(value.y) or 0) end
	if t=="Vector3" then return Vector3.new(tonumber(value.x) or 0,tonumber(value.y) or 0,tonumber(value.z) or 0) end
	if t=="Color3" then return Color3.new(tonumber(value.r) or 0,tonumber(value.g) or 0,tonumber(value.b) or 0) end
	if t=="CFrame" and type(value.components)=="table" and #value.components==12 then return CFrame.new(table.unpack(value.components)) end
	if t=="UDim" then return UDim.new(tonumber(value.scale) or 0,tonumber(value.offset) or 0) end
	if t=="UDim2" then local x=value.x or {}; local y=value.y or {}; return UDim2.new(tonumber(x.scale) or 0,tonumber(x.offset) or 0,tonumber(y.scale) or 0,tonumber(y.offset) or 0) end
	if t=="BrickColor" then return BrickColor.new(tostring(value.name or "Medium stone grey")) end
	if t=="Instance" then return resolve(value.path) end
	if t=="Null" then return nil end
	if t=="Enum" then local enumName,item=tostring(value.value or ""):match("^Enum%.([%w_]+)%.([%w_]+)$"); if enumName and Enum[enumName] then return Enum[enumName][item] end end
	error("unsupported tagged value: "..t)
end
local function descriptor(inst) return {path=pathOf(inst),name=inst.Name,className=inst.ClassName,tags=CollectionService:GetTags(inst),attributes=encodeValue(inst:GetAttributes())} end
local function inventory()
	local result={scripts=0,modules=0,remotes=0,animations=0,sounds=0,particleEmitters=0,screenGuis=0,descendants=0}
	for _,inst in game:GetDescendants() do result.descendants+=1; if inst:IsA("ModuleScript") then result.modules+=1 elseif inst:IsA("LuaSourceContainer") then result.scripts+=1 elseif inst:IsA("RemoteEvent") or inst:IsA("RemoteFunction") then result.remotes+=1 elseif inst:IsA("Animation") then result.animations+=1 elseif inst:IsA("Sound") then result.sounds+=1 elseif inst:IsA("ParticleEmitter") then result.particleEmitters+=1 elseif inst:IsA("ScreenGui") then result.screenGuis+=1 end end
	return result
end
local function waypoint(labelText) pcall(function() ChangeHistoryService:SetWaypoint("Multi-Script: "..tostring(labelText):sub(1,100)) end) end
local function setProperties(inst, props)
	local changed={}; if type(props)~="table" then return changed end
	for name,value in pairs(props) do
		if type(name)~="string" or not name:match("^[%a_][%w_]*$") or BLOCKED_PROPERTIES[name] then error("blocked property: "..tostring(name)) end
		local ok,err=pcall(function() inst[name]=decodeValue(value) end); if not ok then error("cannot set "..pathOf(inst).."."..name..": "..tostring(err)) end; table.insert(changed,name)
	end
	return changed
end
local function exactReplace(source, old, new, replaceAll)
	-- Patch is a REPLACE, so an empty needle has no meaning here. Say what to use
	-- instead rather than a bare "cannot be empty" - the companion has a dedicated
	-- whole-source tool, and a caller that wanted to create or overwrite a script
	-- wants that one.
	if old=="" then error("old_string cannot be empty: msrb_patch_script replaces an exact match, so use msrb_replace_script to set a whole script source, or supply the exact existing text to patch") end
	local out,count,pos={},0,1
	while true do local a,b=string.find(source,old,pos,true); if not a then table.insert(out,string.sub(source,pos)); break end; table.insert(out,string.sub(source,pos,a-1)); table.insert(out,new); count+=1; pos=b+1; if not replaceAll then table.insert(out,string.sub(source,pos)); break end end
	return table.concat(out),count
end

local handlers={}
handlers.msrb_project_summary=function(a)
	local data={placeId=game.PlaceId,placeName=game.Name,isRunning=RunService:IsRunning(),selectionCount=#Selection:Get(),inventory=inventory()}
	if a.include_services then data.services={}; for name in pairs(ROOT_NAMES) do local ok,s=pcall(function() return game:GetService(name) end); if ok then data.services[name]=#s:GetDescendants() end end end; return data
end
handlers.msrb_get_selection=function() local out={}; for _,inst in Selection:Get() do table.insert(out,descriptor(inst)) end; return {selection=out,count=#out} end
handlers.msrb_get_tree=function(a)
	local rootInst=resolve(a.path); if not rootInst then error("path not found") end; local depth=bounded(a.depth,3,0,8); local limit=bounded(a.limit,200,1,MAX_TREE); local out={}; local function walk(inst,d) if #out>=limit then return end; local row=descriptor(inst); row.depth=d; table.insert(out,row); if d<depth then for _,child in inst:GetChildren() do walk(child,d+1); if #out>=limit then break end end end end; walk(rootInst,0); return {root=pathOf(rootInst),instances=out,truncated=#out>=limit}
end
handlers.msrb_find_instances=function(a)
	local rootInst=a.root and resolve(a.root) or game:GetService("Workspace"); if not rootInst then error("root not found") end; local limit=bounded(a.limit,100,1,MAX_TREE); local out={}; local needle=a.name and tostring(a.name):lower() or nil
	local candidates={rootInst}; for _,d in rootInst:GetDescendants() do table.insert(candidates,d) end
	for _,inst in candidates do if (not needle or inst.Name:lower():find(needle,1,true)) and (not a.class_name or inst:IsA(tostring(a.class_name))) and (not a.tag or CollectionService:HasTag(inst,tostring(a.tag))) then table.insert(out,descriptor(inst)); if #out>=limit then break end end end; return {matches=out,count=#out,truncated=#out>=limit}
end
handlers.msrb_get_properties=function(a)
	local result={}; for _,path in listBound(a.paths,100) do local inst=resolve(path); if inst then local row=descriptor(inst); row.properties={}; for _,name in listBound(a.properties,80) do local ok,v=pcall(function() return inst[tostring(name)] end); row.properties[tostring(name)]=ok and encodeValue(v) or "<unreadable>" end; table.insert(result,row) else table.insert(result,{path=path,error="not found"}) end end; return {instances=result}
end
handlers.msrb_read_script=function(a) local inst=resolve(a.path); if not inst or not inst:IsA("LuaSourceContainer") then error("script not found") end; local source=inst.Source; local cap=bounded(a.max_chars,MAX_SOURCE,1,200000); return {path=pathOf(inst),className=inst.ClassName,source=source:sub(1,cap),length=#source,truncated=#source>cap} end
handlers.msrb_search_scripts=function(a)
	local rootInst=a.root and resolve(a.root) or game; if not rootInst then error("root not found") end; local query=tostring(a.query or ""); if query=="" then error("query is required") end; local limit=bounded(a.limit,100,1,MAX_TREE); local out={}; for _,inst in rootInst:GetDescendants() do if inst:IsA("LuaSourceContainer") then local ok,source=pcall(function() return inst.Source end); local found=inst.Name:lower():find(query:lower(),1,true); if ok and not found then local success,index=pcall(string.find,source,query,1,a.plain~=false); found=success and index end; if found then table.insert(out,{path=pathOf(inst),className=inst.ClassName,length=ok and #source or nil}); if #out>=limit then break end end end end; return {matches=out,count=#out,truncated=#out>=limit}
end
handlers.msrb_dependency_graph=function(a)
	local rootInst=a.root and resolve(a.root) or game; if not rootInst then error("root not found") end; local limit=bounded(a.limit,200,1,MAX_TREE); local out={}; for _,inst in rootInst:GetDescendants() do if inst:IsA("LuaSourceContainer") then local ok,source=pcall(function() return inst.Source end); if ok then local deps={}; for expr in source:gmatch("require%s*%(([^%)]+)%)") do table.insert(deps,expr:sub(1,240)); if #deps>=50 then break end end; if #deps>0 then table.insert(out,{path=pathOf(inst),requires=deps}); if #out>=limit then break end end end end end; return {scripts=out,count=#out,truncated=#out>=limit}
end
handlers.msrb_export_snapshot=function(a)
	local tree=handlers.msrb_get_tree({path=a.root,depth=a.depth,limit=a.limit}); local props=listBound(a.properties,40); for _,row in tree.instances do local inst=resolve(row.path); row.properties={}; for _,name in props do local ok,v=pcall(function() return inst[tostring(name)] end); if ok then row.properties[tostring(name)]=encodeValue(v) end end end; return tree
end
handlers.msrb_validate_paths=function(a) local out={}; for _,path in listBound(a.paths,MAX_BATCH) do local inst=resolve(path); table.insert(out,{path=path,exists=inst~=nil,className=inst and inst.ClassName or nil}) end; return {paths=out} end
handlers.msrb_set_selection=function(a) local selected={}; for _,path in listBound(a.paths,100) do local inst=resolve(path); if not inst then error("path not found: "..tostring(path)) end; table.insert(selected,inst) end; Selection:Set(selected); return {count=#selected} end
handlers.msrb_create_instances=function(a)
	local made={}; waypoint("before create"); for _,spec in listBound(a.instances,100) do local class=tostring(spec.class_name or ""); if not CREATE_CLASSES[class] then error("class is not allowlisted: "..class) end; local parent=resolve(spec.parent); if not parent then error("parent not found") end; local inst=Instance.new(class); inst.Name=spec.name and safeName(spec.name) or class; setProperties(inst,spec.properties); if type(spec.attributes)=="table" then for k,v in pairs(spec.attributes) do inst:SetAttribute(tostring(k):sub(1,100),decodeValue(v)) end end; for _,tag in listBound(spec.tags,50) do CollectionService:AddTag(inst,tostring(tag):sub(1,100)) end; inst.Parent=parent; table.insert(made,descriptor(inst)) end; waypoint("created instances"); return {created=made,count=#made}
end
handlers.msrb_set_properties=function(a) local out={}; waypoint("before properties"); for _,change in listBound(a.changes,MAX_BATCH) do local inst=resolve(change.path); if not inst then error("path not found") end; table.insert(out,{path=pathOf(inst),properties=setProperties(inst,change.properties)}) end; waypoint("set properties"); return {changed=out,count=#out} end
handlers.msrb_set_attributes=function(a) local n=0; waypoint("before attributes"); for _,change in listBound(a.changes,MAX_BATCH) do local inst=resolve(change.path); if not inst then error("path not found") end; for k,v in pairs(change.attributes or {}) do inst:SetAttribute(tostring(k):sub(1,100),decodeValue(v)); n+=1 end end; waypoint("set attributes"); return {changed=n} end
handlers.msrb_update_tags=function(a) local n=0; waypoint("before tags"); for _,change in listBound(a.changes,MAX_BATCH) do local inst=resolve(change.path); if not inst then error("path not found") end; for _,tag in listBound(change.add,50) do CollectionService:AddTag(inst,tostring(tag):sub(1,100)); n+=1 end; for _,tag in listBound(change.remove,50) do CollectionService:RemoveTag(inst,tostring(tag):sub(1,100)); n+=1 end end; waypoint("updated tags"); return {changed=n} end
handlers.msrb_reparent_instances=function(a) local out={}; waypoint("before reparent"); for _,move in listBound(a.moves,100) do local inst,parent=resolve(move.path),resolve(move.parent); if not inst or not parent then error("path or parent not found") end; if isRoot(inst) or parent==inst or parent:IsDescendantOf(inst) then error("service/cyclic reparent refused") end; inst.Parent=parent; table.insert(out,pathOf(inst)) end; waypoint("reparented instances"); return {moved=out,count=#out} end
handlers.msrb_clone_instances=function(a) local out={}; waypoint("before clone"); for _,spec in listBound(a.clones,100) do local inst,parent=resolve(spec.path),resolve(spec.parent); if not inst or not parent or isRoot(inst) then error("path or parent invalid") end; local clone=inst:Clone(); if spec.name then clone.Name=safeName(spec.name) end; clone.Parent=parent; table.insert(out,descriptor(clone)) end; waypoint("cloned instances"); return {cloned=out,count=#out} end
handlers.msrb_bulk_rename=function(a) local out={}; waypoint("before rename"); for _,change in listBound(a.changes,MAX_BATCH) do local inst=resolve(change.path); if not inst or isRoot(inst) then error("path invalid") end; inst.Name=safeName(change.name); table.insert(out,pathOf(inst)) end; waypoint("renamed instances"); return {renamed=out,count=#out} end
handlers.msrb_patch_script=function(a)
	local inst=resolve(a.path); if not inst or not inst:IsA("LuaSourceContainer") then error("script not found") end; local source=inst.Source; local total=0; waypoint("before script patch"); for _,edit in listBound(a.edits,50) do local count; source,count=exactReplace(source,tostring(edit.old_string or ""),tostring(edit.new_string or ""),edit.replace_all==true); if count==0 then error("exact old_string was not found") end; total+=count; if #source>MAX_SOURCE then error("patched source exceeds 500000 characters") end end; inst.Source=source; waypoint("patched script"); return {path=pathOf(inst),replacements=total,length=#source}
end
handlers.msrb_replace_script=function(a) local inst=resolve(a.path); if not inst or not inst:IsA("LuaSourceContainer") then error("script not found") end; local source=tostring(a.source or ""); if #source>MAX_SOURCE then error("source exceeds 500000 characters") end; waypoint("before source replacement"); inst.Source=source; waypoint("replaced script"); return {path=pathOf(inst),length=#source} end
handlers.msrb_delete_instances=function(a) local removed={}; waypoint("before delete"); for _,path in listBound(a.paths,100) do local inst=resolve(path); if not inst or isRoot(inst) then error("missing path or service deletion refused") end; table.insert(removed,pathOf(inst)); inst:Destroy() end; waypoint("deleted instances"); return {deleted=removed,count=#removed} end
handlers.msrb_checkpoint=function(a) waypoint(a.label or "checkpoint"); return {created=true} end
handlers.msrb_undo=function() ChangeHistoryService:Undo(); return {undone=true} end
handlers.msrb_redo=function() ChangeHistoryService:Redo(); return {redone=true} end

local function updatePermissionStatus()
	if executionMode=="off" then permissionStatus.Text="Elevated companion tools: OFF (safe default)"; color(permissionStatus,"warn") else permissionStatus.Text=string.format("Elevated companion tools: %s · authenticated loopback session",string.upper(executionMode)); color(permissionStatus,executionMode=="full" and "warn" or "good") end
end
local function registerMode(mode)
	local ok,response=pcall(function() return request("POST","/plugin/register",{pluginVersion=PLUGIN_VERSION,pluginId=pluginId,executionEnabled=true,permissionMode=mode},false) end)
	local data=ok and decodeResponse(response) or nil
	if not data or type(data.token)~="string" then permissionStatus.Text="Could not enable tools · start Multi-Script and allow HTTP Requests"; color(permissionStatus,"bad"); return end
	executionMode=mode; sessionToken=data.token; fullConfirmUntil=0; updatePermissionStatus()
end
local function disableExecution()
	if sessionToken then pcall(function() request("POST","/plugin/unregister",{},true) end) end
	executionMode="off"; sessionToken=nil; fullConfirmUntil=0; updatePermissionStatus()
end
local function executeJob(job)
	local tool=tostring(job.tool or ""); local required=TOOL_PERMISSION[tool]; if not required or not handlers[tool] then error("tool is not allowlisted") end
	if PERMISSION_RANK[executionMode]<PERMISSION_RANK[required] or tostring(job.permission or "")~=required then error("permission tier mismatch") end
	return handlers[tool](type(job.arguments)=="table" and job.arguments or {})
end
local function pollOnce()
	if executionMode=="off" or not sessionToken then return end
	local ok,response=pcall(function() return request("GET","/plugin/next",nil,true) end)
	if not ok or response.StatusCode==403 then sessionToken=nil; permissionStatus.Text="Elevated tools disconnected · re-enable a tier"; color(permissionStatus,"warn"); return end
	local data=decodeResponse(response); local job=data and data.job; if type(job)~="table" then return end
	local success,result=pcall(executeJob,job)
	local payload={id=job.id,result=success and {ok=true,data=encodeValue(result)} or {ok=false,error=tostring(result):sub(1,2000)}}
	pcall(function() request("POST","/plugin/result",payload,true) end)
end

local function readinessScore() local score=lastScan.score; if lastStatus then local roblox=lastStatus.roblox or {}; if roblox.connected then score=math.min(100,score+10) elseif roblox.alive then score=math.max(0,score-10) else score=math.max(0,score-25) end end; return score end
local function heartbeat()
	local inv=inventory(); pcall(function() request("POST","/plugin/heartbeat",{pluginVersion=PLUGIN_VERSION,diagnosticVersion=DIAGNOSTIC_VERSION,placeId=game.PlaceId,placeName=game.Name:sub(1,120),isRunning=RunService:IsRunning(),selectionCount=#Selection:Get(),scriptCount=inv.scripts,moduleCount=inv.modules,readinessScore=readinessScore(),issueCount=lastScan.issueCount,highIssueCount=lastScan.high,scanDurationMs=lastScan.durationMs},false) end)
end
local function updateSelection() local selected=Selection:Get(); if #selected==0 then selectionStatus.Text="Selection: none" else selectionStatus.Text=string.format("Selection: %d instance(s) · first: %s (%s)",#selected,pathOf(selected[1]):sub(1,90),selected[1].ClassName) end end
local function refresh()
	if busy then return end; busy=true
	local ok,response=pcall(function() return request("GET","/status",nil,false) end); local data=ok and decodeResponse(response) or nil
	if data then lastStatus=data; local roblox=data.roblox or {}; local nativeTools=tonumber(roblox.nativeTools) or 0; local bridgeVersion=tostring(data.bridgeVersion or "?"); bridgeStatus.Text=string.format("Bridge: online · v%s · loopback only",bridgeVersion); color(bridgeStatus,"good"); if bridgeVersion==PLUGIN_VERSION then compatibilityStatus.Text="Compatibility: plugin and bridge versions match"; color(compatibilityStatus,"good") else compatibilityStatus.Text=string.format("Compatibility: plugin v%s / bridge v%s · update the older component",PLUGIN_VERSION,bridgeVersion); color(compatibilityStatus,"warn") end; if roblox.connected then studioStatus.Text=string.format("Studio MCP: connected · %d official native + %d companion tools",nativeTools,tonumber(data.companionTools) or 24); color(studioStatus,"good") elseif roblox.alive then studioStatus.Text=string.format("Studio MCP proxy alive, but no usable place · %d cached tools",nativeTools); color(studioStatus,"warn") else studioStatus.Text="Official Studio MCP offline · companion tools can still work when enabled"; color(studioStatus,"warn") end; heartbeat() else bridgeStatus.Text="Bridge: offline, or Studio HTTP Requests are disabled"; color(bridgeStatus,"bad"); studioStatus.Text="Enable Game Settings → Security → Allow HTTP Requests, then start Multi-Script"; color(studioStatus,"warn"); compatibilityStatus.Text="Compatibility unavailable until the bridge responds"; color(compatibilityStatus,"warn") end
	updateSelection(); setReadiness(readinessScore()); busy=false
end

local RISK_PATTERNS={{pattern="loadstring",label="dynamic loadstring execution",severity="high"},{pattern="require%s*%(%s*%d+",label="numeric asset require",severity="high"},{pattern="OnServerInvoke",label="RemoteFunction handler validation",severity="medium"},{pattern="OnServerEvent",label="RemoteEvent handler validation",severity="medium"},{pattern="SetAsync%s*%(",label="consider conflict-safe UpdateAsync",severity="medium"},{pattern="while%s+true%s+do",label="unbounded loop",severity="medium"},{pattern="WaitForChild%s*%([^,%)]*%)",label="WaitForChild without timeout",severity="low"}}
local function scanProject()
	if busy then return end; busy=true; scanButton.Text="Scanning…"; local started=os.clock();local issues={}; local counts={high=0,medium=0,low=0}; local scanned,unreadable,visited=0,0,0
	for _,inst in game:GetDescendants() do visited+=1; if inst:IsA("LuaSourceContainer") then scanned+=1; local ok,source=pcall(function() return inst.Source end); if ok then for _,entry in RISK_PATTERNS do if source:find(entry.pattern) then counts[entry.severity]+=1; table.insert(issues,{path=pathOf(inst),severity=entry.severity,message=entry.label}); if #issues>=100 then break end end end else unreadable+=1 end end; if #issues>=100 then break end; if visited%150==0 then task.wait() end end
	local duration=math.floor((os.clock()-started)*1000+.5); local score=math.max(0,100-counts.high*12-counts.medium*4-counts.low-unreadable*2); lastScan={score=score,issueCount=#issues,high=counts.high,medium=counts.medium,low=counts.low,scanned=scanned,unreadable=unreadable,durationMs=duration}; setReadiness(score); readinessStatus.Text=string.format("Readiness: %d/100 · %d high · %d medium · %d low",score,counts.high,counts.medium,counts.low); color(readinessStatus,counts.high>0 and "bad" or counts.medium>0 and "warn" or "good"); projectStatus.Text=string.format("Project scan: %d scripts · %d unreadable · %d ms",scanned,unreadable,duration); if #issues>0 then local lines={}; for _,issue in issues do table.insert(lines,string.format("[%s] %s — %s",issue.severity:upper(),issue.path,issue.message)) end; warn("[Multi-Script] Readiness findings:\n- "..table.concat(lines,"\n- ")) end; scanButton.Text="Run non-destructive readiness scan"; busy=false; heartbeat()
end
local function printCatalog()
	local ok,response=pcall(function() return request("GET","/catalog",nil,false) end); local data=ok and decodeResponse(response) or nil; if not data then projectStatus.Text="Catalog unavailable · check bridge and HTTP Requests"; color(projectStatus,"warn"); return end; projectStatus.Text=string.format("Tools: %d official native · %d direct · %d opt-in companion",tonumber(data.nativeToolCount) or 0,tonumber(data.multiScriptDirectTools) or 0,tonumber(data.companionPluginTools) or 0); color(projectStatus,"good"); local names={}; for _,tool in data.tools or {} do table.insert(names,tostring(tool.name or "?")) end; print("[Multi-Script] Official Roblox Studio MCP catalog:\n- "..table.concat(names,"\n- "))
end
local function diagnosticReport() local inv=inventory(); local roblox=lastStatus and lastStatus.roblox or {}; print("[Multi-Script] Sanitized diagnostic report:\n"..HttpService:JSONEncode({product="Multi-Script Companion",pluginVersion=PLUGIN_VERSION,diagnosticVersion=DIAGNOSTIC_VERSION,bridgeVersion=lastStatus and lastStatus.bridgeVersion or nil,studioMcpConnected=roblox.connected==true,officialNativeToolCount=roblox.nativeTools or 0,companionMode=executionMode,placeId=game.PlaceId,placeName=game.Name:sub(1,120),isRunning=RunService:IsRunning(),selectionCount=#Selection:Get(),inventory=inv,readiness=lastScan,privacy="No source, secrets, bearer token, or job payloads are included."})) end

readButton.Activated:Connect(function() task.spawn(function() registerMode("read") end) end)
projectButton.Activated:Connect(function() task.spawn(function() registerMode("project") end) end)
fullButton.Activated:Connect(function() if os.clock()>fullConfirmUntil then fullConfirmUntil=os.clock()+8; fullButton.Text="Confirm Full tools — click again within 8s"; task.delay(8,function() if os.clock()>fullConfirmUntil then fullButton.Text="Enable Full tools (24) — click twice" end end) else fullButton.Text="Enable Full tools (24) — click twice"; task.spawn(function() registerMode("full") end) end end)
disableButton.Activated:Connect(function() task.spawn(disableExecution) end)
toggleButton.Click:Connect(function() widget.Enabled=not widget.Enabled; if widget.Enabled then task.spawn(refresh) end end)refreshButton.Activated:Connect(function() task.spawn(refresh) end)
scanButton.Activated:Connect(function() task.spawn(scanProject) end)
diagnosticsButton.Activated:Connect(diagnosticReport)
catalogButton.Activated:Connect(function() task.spawn(printCatalog) end)
Selection.SelectionChanged:Connect(updateSelection)
widget:GetPropertyChangedSignal("Enabled"):Connect(function()
	toggleButton:SetActive(widget.Enabled)
	pcall(function() plugin:SetSetting("MultiScriptWidgetOpen", widget.Enabled) end)
	if widget.Enabled then task.spawn(refresh) end
end)
plugin.Unloading:Connect(function() alive=false; disableExecution() end)
updatePermissionStatus()
task.spawn(function() while alive do if widget.Enabled then refresh() end; task.wait(STATUS_SECONDS) end end)
task.spawn(function() while alive do if executionMode~="off" then pollOnce() end; task.wait(POLL_SECONDS) end end)
