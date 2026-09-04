--[[
wire_check.lua — runs the ADDON's real code outside the game and prints the
picture it would draw.

Nothing here reimplements the addon. It stubs the handful of client functions
`AzerothTill.lua` calls, loads that file unmodified, feeds it the wire messages
the till produced, and then reads the drawn rectangles back off the stubbed
textures. What is compared upstream is therefore the picture the player sees,
which covers the base64 decode, the bit indexing AND the run coalescing --
three things that can each be wrong on their own and two of which would still
produce a plausible-looking square.

    lua5.1 wire_check.lua <addon.lua> <wire.txt>

`wire.txt` is one addon message per line. The output is one line per module
row, `0` for light and `1` for dark, which is what `h_wire.py` compares against
`qrcodegen`'s own matrix.
]]

local addonPath, wirePath = ...
if not addonPath or not wirePath then
    io.stderr:write("usage: wire_check.lua <addon.lua> <wire.txt>\n")
    os.exit(2)
end

-- ---------------------------------------------------------------------------
-- THE STUBS
--
-- Deliberately dumb. A stub that computed anything would be a third
-- implementation, and a check whose expectation comes from the same idea as
-- the thing it checks is a check that agrees with a defect.
-- ---------------------------------------------------------------------------

local drawn = {}

local function noop() end

local function newTexture()
    local texture = {
        shown = false, width = 0, height = 0, offsetX = 0, offsetY = 0,
    }
    function texture:SetTexture() end
    function texture:ClearAllPoints() end
    function texture:SetPoint(_, _, _, x, y) self.offsetX, self.offsetY = x, y end
    function texture:SetWidth(w) self.width = w end
    function texture:SetHeight(h) self.height = h end
    function texture:Show() self.shown = true; drawn[#drawn + 1] = self end
    function texture:Hide() self.shown = false end
    return texture
end

local function newFontString()
    local fs = {}
    function fs:SetPoint() end
    function fs:SetText(text) fs.text = text end
    function fs:SetTextColor() end
    return fs
end

local function newFrame()
    local frame = {}
    local methods = {
        "SetPoint", "SetWidth", "SetHeight", "SetMovable", "EnableMouse",
        "RegisterForDrag", "SetBackdrop", "SetBackdropColor",
        "SetBackdropBorderColor", "Show", "Hide", "ClearAllPoints",
        "StartMoving", "StopMovingOrSizing", "RegisterEvent",
    }
    for _, name in ipairs(methods) do frame[name] = noop end
    function frame:SetScript(which, handler) frame["script_" .. which] = handler end
    function frame:GetPoint() return "CENTER", nil, "CENTER", 0, 0 end
    function frame:CreateTexture() return newTexture() end
    function frame:CreateFontString() return newFontString() end
    return frame
end

_G.UIParent = newFrame()
_G.CreateFrame = function() return newFrame() end
_G.DEFAULT_CHAT_FRAME = { AddMessage = function(_, text) io.stderr:write(text .. "\n") end }
_G.SlashCmdList = {}

local chunk = assert(loadfile(addonPath))
chunk()

local addon = _G.AzerothTill
if not addon or not addon.onWire then
    io.stderr:write("the addon did not expose its wire handler for checking\n")
    os.exit(3)
end

-- ---------------------------------------------------------------------------
-- FEED IT THE WIRE
-- ---------------------------------------------------------------------------

local size = nil
for line in io.lines(wirePath) do
    if line ~= "" then
        local declared = string.match(line, "^H|[^|]+|(%d+)|%d+$")
        if declared then size = tonumber(declared) end
        addon.onWire(line)
    end
end

if not size then
    io.stderr:write("the wire carried no header\n")
    os.exit(4)
end
if #drawn == 0 then
    io.stderr:write("the addon drew nothing\n")
    os.exit(5)
end

-- ---------------------------------------------------------------------------
-- READ THE PICTURE BACK OFF THE RECTANGLES
-- ---------------------------------------------------------------------------

local scale = _G.AzerothTillDB and _G.AzerothTillDB.scale or 6

local grid = {}
for y = 0, size - 1 do
    grid[y] = {}
    for x = 0, size - 1 do grid[y][x] = 0 end
end

for _, rectangle in ipairs(drawn) do
    local x = rectangle.offsetX / scale
    local y = -rectangle.offsetY / scale
    local run = rectangle.width / scale
    if x ~= math.floor(x) or y ~= math.floor(y) or run ~= math.floor(run) then
        io.stderr:write("a rectangle did not land on the module grid\n")
        os.exit(6)
    end
    if rectangle.height ~= scale then
        io.stderr:write("a rectangle was not one module tall\n")
        os.exit(7)
    end
    for offset = 0, run - 1 do
        if grid[y] == nil or grid[y][x + offset] == nil then
            io.stderr:write("a rectangle fell outside the symbol\n")
            os.exit(8)
        end
        grid[y][x + offset] = 1
    end
end

for y = 0, size - 1 do
    local row = {}
    for x = 0, size - 1 do row[x + 1] = grid[y][x] end
    print(table.concat(row))
end
