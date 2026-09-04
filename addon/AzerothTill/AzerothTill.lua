--[[
AzerothTill — draws the payment code the server sends.

WHAT IT DOES AND WHAT IT REFUSES TO DO

    It paints black squares. That is the whole of it.

    The server encodes the QR with a library that has a test suite and a
    round-trip check; this receives the finished MODULES and renders them. A
    QR encoder written in Lua would be a second implementation of the same
    format, running on a player's machine where nothing checks it, and the two
    would drift apart silently -- with the failure showing up as a symbol that
    scans into the wrong payment.

    So the wire carries the bitmap, and everything this file can get wrong is
    visible the moment you look at the frame.

THE FRAME IS GATED ON `E`, AND THAT IS THE POINT

    A partially received symbol still scans. It scans into something else, or
    into nothing, and a player cannot tell which by looking. So nothing is
    drawn until the end marker arrives AND every chunk the header promised is
    present. A short symbol is an error message, never a picture.

    3.3.5a addon messages are capped at 255 bytes, so a symbol always arrives
    in several pieces. Losing one is ordinary, not exotic.

    /aztill  — show, hide, scale, or test the frame without a server.
]]

local ADDON_PREFIX = "AZTILL"

local DEFAULTS = {
    scale = 6,     -- physical pixels per module
    quiet = 4,     -- modules of white margin; 4 is what the QR standard asks
    point = "CENTER",
    x = 0,
    y = 0,
}

local incoming = nil          -- the symbol being received, if any
local textures = {}           -- the pool of black rectangles, reused
local frame, canvas, label, hint

-- ---------------------------------------------------------------------------
-- BASE64, WITHOUT A BIT LIBRARY
--
-- 3.3.5a's Lua has no `bit`, so this is arithmetic. The accumulator never
-- exceeds 2^14, well inside the range a double represents exactly, so there
-- is no rounding to reason about.
-- ---------------------------------------------------------------------------

local B64 = {}
do
    local alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    for index = 1, string.len(alphabet) do
        B64[string.sub(alphabet, index, index)] = index - 1
    end
end

local function decode64(text)
    local bytes, accumulator, held = {}, 0, 0
    for index = 1, string.len(text) do
        local value = B64[string.sub(text, index, index)]
        if value then
            accumulator = accumulator * 64 + value
            held = held + 6
            if held >= 8 then
                held = held - 8
                local divisor = 2 ^ held
                local byte = math.floor(accumulator / divisor)
                accumulator = accumulator - byte * divisor
                bytes[#bytes + 1] = byte
            end
        end
    end
    return bytes
end

local function isDark(bytes, size, x, y)
    local index = y * size + x
    local byte = bytes[math.floor(index / 8) + 1]
    if not byte then return false end
    local shift = 2 ^ (7 - (index % 8))
    return math.floor(byte / shift) % 2 == 1
end

-- ---------------------------------------------------------------------------
-- THE FRAME
-- ---------------------------------------------------------------------------

local function settings()
    AzerothTillDB = AzerothTillDB or {}
    for key, value in pairs(DEFAULTS) do
        if AzerothTillDB[key] == nil then AzerothTillDB[key] = value end
    end
    return AzerothTillDB
end

local function build()
    if frame then return end
    local db = settings()

    frame = CreateFrame("Frame", "AzerothTillFrame", UIParent)
    frame:SetPoint(db.point, UIParent, db.point, db.x, db.y)
    frame:SetWidth(200)
    frame:SetHeight(230)
    frame:SetMovable(true)
    frame:EnableMouse(true)
    frame:RegisterForDrag("LeftButton")
    frame:SetScript("OnDragStart", function() frame:StartMoving() end)
    frame:SetScript("OnDragStop", function()
        frame:StopMovingOrSizing()
        local point, _, _, x, y = frame:GetPoint()
        db.point, db.x, db.y = point, x, y
    end)
    frame:SetBackdrop({
        bgFile = "Interface\\Buttons\\WHITE8X8",
        edgeFile = "Interface\\Tooltips\\UI-Tooltip-Border",
        edgeSize = 12,
        insets = { left = 3, right = 3, top = 3, bottom = 3 },
    })
    -- The backdrop is the QUIET ZONE, so it is white and opaque. A translucent
    -- frame lets the game world through and a reader sees a moving background
    -- where the standard requires blank paper.
    frame:SetBackdropColor(1, 1, 1, 1)
    frame:SetBackdropBorderColor(0.4, 0.4, 0.4, 1)

    canvas = CreateFrame("Frame", nil, frame)
    canvas:SetPoint("TOP", frame, "TOP", 0, -10)

    label = frame:CreateFontString(nil, "OVERLAY", "GameFontNormalSmall")
    label:SetPoint("BOTTOM", frame, "BOTTOM", 0, 16)
    label:SetTextColor(0, 0, 0)

    hint = frame:CreateFontString(nil, "OVERLAY", "GameFontDisableSmall")
    hint:SetPoint("BOTTOM", frame, "BOTTOM", 0, 5)
    hint:SetText("drag to move  |  /aztill hide")
    hint:SetTextColor(0.3, 0.3, 0.3)

    local close = CreateFrame("Button", nil, frame, "UIPanelCloseButton")
    close:SetPoint("TOPRIGHT", frame, "TOPRIGHT", 2, 2)
    close:SetScript("OnClick", function() frame:Hide() end)

    frame:Hide()
end

local function releaseTextures()
    for index = 1, #textures do textures[index]:Hide() end
end

local function takeTexture(index)
    if not textures[index] then
        local texture = canvas:CreateTexture(nil, "OVERLAY")
        texture:SetTexture(0, 0, 0)
        textures[index] = texture
    end
    return textures[index]
end

--- Paint one symbol.
--
-- Dark modules are drawn as horizontal RUNS rather than one texture each. A
-- 45x45 symbol is 2,025 modules and roughly half are dark; as runs that is
-- about 250 rectangles. Both draw the same picture and the run version is what
-- keeps this from being felt on a frame that is only ever shown while a player
-- waits for a payment.
local function draw(size, bytes, caption)
    build()
    local db = settings()
    local scale = db.scale
    local quiet = db.quiet

    local side = (size + quiet * 2) * scale
    canvas:SetWidth(size * scale)
    canvas:SetHeight(size * scale)
    frame:SetWidth(side)
    frame:SetHeight(side + 34)
    canvas:ClearAllPoints()
    canvas:SetPoint("TOP", frame, "TOP", 0, -quiet * scale)

    releaseTextures()
    local used = 0
    for y = 0, size - 1 do
        local x = 0
        while x < size do
            if isDark(bytes, size, x, y) then
                local run = 1
                while x + run < size and isDark(bytes, size, x + run, y) do run = run + 1 end
                used = used + 1
                local texture = takeTexture(used)
                texture:ClearAllPoints()
                texture:SetPoint("TOPLEFT", canvas, "TOPLEFT", x * scale, -y * scale)
                texture:SetWidth(run * scale)
                texture:SetHeight(scale)
                texture:Show()
                x = x + run
            else
                x = x + 1
            end
        end
    end
    label:SetText(caption or "")
    frame:Show()
end

-- ---------------------------------------------------------------------------
-- THE WIRE
-- ---------------------------------------------------------------------------

local function reset(reason)
    if reason and incoming then
        DEFAULT_CHAT_FRAME:AddMessage("|cff33ff99[Till]|r code not drawn: " .. reason)
    end
    incoming = nil
end

local function complete()
    if not incoming or not incoming.ended then return end
    local parts = {}
    for index = 0, incoming.count - 1 do
        if not incoming.chunks[index] then
            return reset("part " .. (index + 1) .. " of " .. incoming.count .. " never arrived")
        end
        parts[index + 1] = incoming.chunks[index]
    end
    if incoming.ended ~= incoming.ref then
        return reset("the end marker names a different order")
    end
    local bytes = decode64(table.concat(parts))
    local needed = math.ceil(incoming.size * incoming.size / 8)
    if #bytes < needed then
        return reset("the code decoded short")
    end
    draw(incoming.size, bytes, incoming.ref)
    incoming = nil
end

local function onWire(message)
    local kind, rest = string.match(message, "^(%a)|(.*)$")
    if not kind then return end

    if kind == "H" then
        local ref, size, count = string.match(rest, "^([^|]+)|(%d+)|(%d+)$")
        if not ref then return end
        incoming = { ref = ref, size = tonumber(size), count = tonumber(count), chunks = {} }
    elseif kind == "D" then
        if not incoming then return end
        local index, data = string.match(rest, "^(%d+)|(.*)$")
        if index then incoming.chunks[tonumber(index)] = data end
    elseif kind == "E" then
        if not incoming then return end
        incoming.ended = rest
        complete()
    end
end

-- ---------------------------------------------------------------------------
-- EVENTS AND COMMANDS
-- ---------------------------------------------------------------------------

local listener = CreateFrame("Frame")
listener:RegisterEvent("CHAT_MSG_ADDON")
listener:SetScript("OnEvent", function(_, event, prefix, message)
    -- 3.3.5a passes event arguments as globals to this handler in some
    -- clients and as parameters in others, so both are read and the globals
    -- win only when the parameters are absent.
    prefix = prefix or arg1
    message = message or arg2
    if prefix == ADDON_PREFIX and message then onWire(message) end
end)

SLASH_AZTILL1 = "/aztill"
SlashCmdList["AZTILL"] = function(input)
    local command, value = string.match(input or "", "^(%a*)%s*(%S*)$")
    command = string.lower(command or "")
    local db = settings()

    if command == "hide" then
        build(); frame:Hide()
    elseif command == "show" then
        build(); frame:Show()
    elseif command == "scale" then
        local scale = tonumber(value)
        if scale and scale >= 2 and scale <= 12 then
            db.scale = scale
            DEFAULT_CHAT_FRAME:AddMessage("|cff33ff99[Till]|r module size " .. scale .. " px.")
        else
            DEFAULT_CHAT_FRAME:AddMessage("|cff33ff99[Till]|r /aztill scale 2-12")
        end
    elseif command == "test" then
        -- A symbol drawn from nothing but this file, so the frame can be
        -- placed and sized with no server and no payment. It is a checkerboard
        -- and is deliberately NOT a valid QR: a fake that scanned would be a
        -- fake somebody could pay.
        local size = 21
        local bytes = {}
        for index = 0, math.ceil(size * size / 8) - 1 do
            bytes[index + 1] = (index % 2 == 0) and 170 or 85
        end
        draw(size, bytes, "test pattern (not a real code)")
    else
        DEFAULT_CHAT_FRAME:AddMessage("|cff33ff99[Till]|r /aztill show | hide | scale <2-12> | test")
    end
end

-- ---------------------------------------------------------------------------
-- THE TESTING SEAM
--
-- One table, exposed on purpose. `harness/wire_check.lua` loads this file
-- unmodified, feeds these two the real wire, and reads the drawn rectangles
-- back off stubbed textures -- so what the gate compares is the picture a
-- player would see, not a copy of the decoder written to agree with it.
--
-- Nothing in the addon reads this table, and removing it breaks only the gate.
-- ---------------------------------------------------------------------------

_G.AzerothTill = {
    onWire = onWire,
    decode64 = decode64,
    isDark = isDark,
}
