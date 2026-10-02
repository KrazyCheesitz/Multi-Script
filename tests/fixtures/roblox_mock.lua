-- Minimal Roblox API mock for running engine_toolkit's generated Luau (tests only).
local Instances = {}
local function mkvec(x, y, z)
  local v = {X = x, Y = y, Z = z}
  v.Magnitude = math.sqrt(x*x + y*y + z*z)
  return setmetatable(v, {__typeof = "Vector3", __add = function(a, b) return mkvec(a.X+b.X, a.Y+b.Y, a.Z+b.Z) end,
    __sub = function(a, b) return mkvec(a.X-b.X, a.Y-b.Y, a.Z-b.Z) end,
    __mul = function(a, b) if type(b) == "number" then return mkvec(a.X*b, a.Y*b, a.Z*b) end return mkvec(a.X*b.X, a.Y*b.Y, a.Z*b.Z) end,
    __div = function(a, b) return mkvec(a.X/b, a.Y/b, a.Z/b) end})
end
Vector3 = {new = function(x, y, z) return mkvec(x or 0, y or 0, z or 0) end}
Vector2 = {new = function(x, y) return setmetatable({X = x, Y = y}, {__typeof = "Vector2"}) end}
local function mkcol(r, g, b) return setmetatable({R = r, G = g, B = b, ToHex = function() return string.format("%02x%02x%02x", r*255, g*255, b*255) end}, {__typeof = "Color3"}) end
Color3 = {new = mkcol, fromRGB = function(r, g, b) return mkcol(r/255, g/255, b/255) end,
  fromHex = function(h) h = h:gsub("#", ""); return mkcol(tonumber(h:sub(1,2),16)/255, tonumber(h:sub(3,4),16)/255, tonumber(h:sub(5,6),16)/255) end}
local function mkcf(x, y, z) local c = {X = x, Y = y, Z = z, Position = mkvec(x, y, z)}
  local mt = {__typeof = "CFrame"}
  mt.__sub = function(a, b) return mkcf(a.X-b.X, a.Y-b.Y, a.Z-b.Z) end
  mt.__add = function(a, b) return mkcf(a.X+b.X, a.Y+b.Y, a.Z+b.Z) end
  mt.__mul = function(a, b) return a end
  c.ToEulerAnglesXYZ = function() return 0, 0, 0 end
  return setmetatable(c, mt) end
CFrame = {new = function(a, b, c) if type(a) == "table" then return mkcf(a.X, a.Y, a.Z) end return mkcf(a or 0, b or 0, c or 0) end, Angles = function() return mkcf(0,0,0) end}
UDim2 = {new = function(a, b, c, d) return setmetatable({X = {Scale = a, Offset = b}, Y = {Scale = c, Offset = d}}, {__typeof = "UDim2"}) end}
UDim = {new = function(a, b) return setmetatable({Scale = a, Offset = b}, {__typeof = "UDim"}) end}
BrickColor = {new = function(n) return setmetatable({Name = n}, {__typeof = "BrickColor"}) end}
NumberRange = {new = function(a, b) return setmetatable({Min = a, Max = b}, {__typeof = "NumberRange"}) end}
local enumcache = {}
local function mkenum(etype, name) return setmetatable({Name = name, EnumType = etype}, {__typeof = "EnumItem", __tostring = function() return "Enum." .. etype .. "." .. name end}) end
Enum = setmetatable({}, {__index = function(t, etype)
  local e = setmetatable({}, {__index = function(_, name) enumcache[etype .. name] = enumcache[etype .. name] or mkenum(etype, name); return enumcache[etype .. name] end})
  rawset(t, etype, e); return e end})
local realtypeof = nil
function typeof(v)
  local mt = type(v) == "table" and getmetatable(v)
  if mt and mt.__typeof then return mt.__typeof end
  if type(v) == "table" and v.__isinstance then return "Instance" end
  return type(v)
end
math.clamp = math.clamp or function(x, a, b) return math.max(a, math.min(b, x)) end
math.deg = math.deg or function(x) return x * 180 / math.pi end
math.rad = math.rad or function(x) return x * math.pi / 180 end
table.unpack = table.unpack or unpack
local classes = {Part = {"BasePart", "PVInstance"}, MeshPart = {"BasePart"}, Model = {"PVInstance"}, Frame = {"GuiObject"}, TextLabel = {"GuiObject"}, Script = {"LuaSourceContainer"}, LocalScript = {"LuaSourceContainer"}, ModuleScript = {"LuaSourceContainer"}, PointLight = {"Light"}}
local defaults = {BasePart = {Position = mkvec(0,0,0), Size = mkvec(4,1,2), Anchored = false, Color = mkcol(0.6,0.6,0.6), Material = Enum.Material.Plastic, Transparency = 0, CanCollide = true, CFrame = mkcf(0,0,0), Shape = Enum.PartType.Block},
  GuiObject = {Position = UDim2.new(0,0,0,0), Size = UDim2.new(0,100,0,100), Visible = true, Text = ""}, LuaSourceContainer = {Source = ""}, Script = {Enabled = true, RunContext = Enum.RunContext.Legacy}, Light = {Brightness = 1}}
local function newInstance(class)
  local o = {ClassName = class, Name = class, Parent = nil, __isinstance = true, _kids = {}, _attrs = {}, _tags = {}}
  local isa = {[class] = true, Instance = true}
  for _, c in ipairs(classes[class] or {}) do isa[c] = true end
  for c in pairs(isa) do for k, v in pairs(defaults[c] or {}) do o[k] = v end end
  o.IsA = function(self, c) return isa[c] == true end
  o.GetChildren = function(self) local r = {}; for _, k in ipairs(self._kids) do r[#r+1] = k end; return r end
  o.FindFirstChild = function(self, n) for _, k in ipairs(self._kids) do if k.Name == n then return k end end end
  o.FindFirstChildOfClass = function(self, c) for _, k in ipairs(self._kids) do if k.ClassName == c then return k end end end
  o.GetDescendants = function(self) local r = {}; local function w(i) for _, k in ipairs(i._kids) do r[#r+1] = k; w(k) end end; w(self); return r end
  o.GetFullName = function(self) if self.Parent == nil or self.Parent == game then return self.Name end return self.Parent:GetFullName() .. "." .. self.Name end
  o.Destroy = function(self) self.Parent = nil; self._destroyed = true end
  o.GetAttribute = function(self, k) return self._attrs[k] end
  o.SetAttribute = function(self, k, v) self._attrs[k] = v end
  o.GetAttributes = function(self) return self._attrs end
  o.GetPivot = function(self) return self.CFrame or mkcf(0,0,0) end
  o.PivotTo = function(self, cf) self.CFrame = cf; self.Position = cf.Position end
  o.Clone = function(self) local c = newInstance(self.ClassName); c.Name = self.Name; return c end
  o.GetBoundingBox = function(self) return mkcf(0,0,0), mkvec(1,1,1) end
  return setmetatable(o, {__newindex = function(t, k, v)
    if k == "Parent" then
      local old = rawget(t, "Parent")
      if old then for i, kid in ipairs(old._kids) do if kid == t then table.remove(old._kids, i) break end end end
      rawset(t, "Parent", v)
      if v then v._kids[#v._kids+1] = t end
    else rawset(t, k, v) end end,
    __index = function(t, k) if k == "NotAProp" then error(k .. " is not a valid member of " .. tostring(rawget(t, "ClassName"))) end return nil end})
end
Instance = {new = function(class, parent) local o = newInstance(class); if parent then o.Parent = parent end; return o end}
game = newInstance("DataModel"); game.Name = "game"
local services = {}
function game:GetService(n)
  if n == "HttpService" then return {JSONDecode = function(_, s) return JSON_DECODE(s) end, JSONEncode = function(_, t) return JSON_ENCODE(t) end} end
  if n == "CollectionService" then return {AddTag = function(_, i, t) i._tags[#i._tags+1] = t end, RemoveTag = function() end, HasTag = function(_, i, t) for _, x in ipairs(i._tags) do if x == t then return true end end return false end,
    GetTags = function(_, i) return i._tags end, GetTagged = function() return {} end} end
  if n == "ChangeHistoryService" then return {SetWaypoint = function() end, Undo = function() end, Redo = function() end} end
  if n == "Selection" then return {Get = function() return {} end, Set = function() end} end
  if services[n] then return services[n] end
  local s = newInstance(n); s.Name = n; s.Parent = game; services[n] = s
  if n == "Lighting" then s.ClockTime = 12; s.Brightness = 2 end
  return s
end
workspace = game:GetService("Workspace")
workspace.Terrain = newInstance("Terrain")
debug = debug or {traceback = function() return "" end}
