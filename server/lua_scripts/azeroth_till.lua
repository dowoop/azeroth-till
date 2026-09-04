--[[
azeroth_till.lua — the gold till, inside the world server.

WHAT THIS IS
    A player types `#till 250`. This asks the till service to open an order,
    prints the payment instruction as a system message, and sends the QR to
    the player's addon if they have one. When the payment lands on Ootle, this
    claims the delivery, mails the gold, and says so.

    It holds no key, knows no address, and does no arithmetic about money. The
    till service does all three; this file is the part of it that can speak to
    a player and put gold in their mailbox, which is the part that must live
    in the world server and nothing else here does.

THREE THINGS THAT LOOK LIKE STYLE AND ARE NOT

  1. THE PLAYER IS RE-FOUND BY GUID IN EVERY CALLBACK. `HttpRequest` is
     asynchronous. The `player` that started an order may have logged out,
     been kicked, or crashed before the answer arrives, and the pointer that
     was valid at the call is not valid at the callback. `GetPlayerByGUID`
     returns nil for someone who left, and nil is a normal outcome here.

  2. THE RESPONSE IS LINES, NOT JSON. ALE ships no JSON decoder, and writing
     one to run in-process with the world on text off a socket is a parser
     nothing gates. The till speaks `text/plain` for exactly this caller: one
     `string.match` per line, and an unrecognised line is ignored rather than
     interpreted.

  3. DELIVERY IS CLAIMED BEFORE IT IS MAILED, AND ACKNOWLEDGED AFTER. Those
     are three separate steps on purpose and the till's `claim_deliveries`
     explains why: mailing first pays a player twice when an acknowledgement
     is lost, and acknowledging first loses the gold of a player who paid.
     Claiming moves the order to `delivering`; if this server dies with the
     mail unsent, the order sits there and `/stuck` shows a person, rather
     than either failure happening quietly.

INSTALL
    Copy this file and `azeroth_till_config.lua` into the world server's
    `lua_scripts/` directory. The config file carries the token and is not in
    version control; `azeroth_till_config.lua.example` is.
]]

local ADDON_PREFIX = "AZTILL"

-- CHAT_MSG_WHISPER. The channel an addon message rides on decides what the
-- client reports in `CHAT_MSG_ADDON`'s distribution argument, and whisper is
-- the only one that means "the server, to you alone".
local CHAT_MSG_WHISPER = 6

-- MAIL_STATIONERY_GM: the envelope a player recognises as coming from the
-- server rather than from another player.
local MAIL_STATIONERY_GM = 61

local PLAYER_EVENT_ON_CHAT = 18

local DEFAULTS = {
    url = "http://host.docker.internal:8099",
    token = "",
    trigger = "#till",
    poll_ms = 5000,
    claim_limit = 5,
    sender_guid = 0,
    mail_subject = "Azeroth Till",
}

-- READ LAZILY, NEVER CACHED AT LOAD. ALE loads every file in `lua_scripts`
-- and the order is not something this file may assume: reading the config at
-- load time would work or not depending on a filename. Reading it at use time
-- cannot.
local function config()
    local supplied = _G.AzerothTill_Config or {}
    local merged = {}
    for key, value in pairs(DEFAULTS) do
        if supplied[key] ~= nil then merged[key] = supplied[key] else merged[key] = value end
    end
    return merged
end

local function headers(cfg)
    return { ["X-Till-Token"] = cfg.token, ["Accept"] = "text/plain" }
end

local function say(player, text)
    if player then player:SendBroadcastMessage("|cff33ff99[Till]|r " .. text) end
end

-- Every line of a response, with its kind split off the front. An empty or
-- malformed line yields nothing and the caller skips it.
local function lines(body)
    local out = {}
    for line in tostring(body or ""):gmatch("[^\r\n]+") do
        local kind, rest = line:match("^(%u+)%s?(.*)$")
        if kind then out[#out + 1] = { kind = kind, rest = rest } end
    end
    return out
end

-- ---------------------------------------------------------------------------
-- ORDERING
-- ---------------------------------------------------------------------------

local function requestOrder(player, gold)
    local cfg = config()
    local guid = player:GetGUID()
    local body = string.format(
        '{"account_id":%d,"char_guid":%d,"char_name":"%s","gold":%d}',
        player:GetAccountId(), player:GetGUIDLow(), player:GetName(), gold)

    HttpRequest("POST", cfg.url .. "/order", body, "application/json", headers(cfg),
        function(status, response)
            local who = GetPlayerByGUID(guid)
            if not who then return end
            if status ~= 200 then
                for _, line in ipairs(lines(response)) do
                    if line.kind == "ERR" then say(who, line.rest) end
                end
                if status == 0 then
                    say(who, "The till did not answer. It may not be running.")
                end
                return
            end
            local sent = 0
            for _, line in ipairs(lines(response)) do
                if line.kind == "CHAT" then
                    say(who, line.rest)
                elseif line.kind == "WIRE" then
                    -- The symbol, for a client that has the addon. A client
                    -- without it never sees these: an addon message is not
                    -- rendered by the default UI, so this is silent rather
                    -- than noise, and the CHAT lines above are the whole
                    -- instruction on their own.
                    who:SendAddonMessage(ADDON_PREFIX, line.rest, CHAT_MSG_WHISPER, who)
                    sent = sent + 1
                end
            end
            if sent > 0 then
                say(who, "A code is on screen if you have the AzerothTill addon.")
            end
        end)
end

local function onChat(event, player, msg)
    local cfg = config()
    local trigger = cfg.trigger
    if msg:sub(1, #trigger) ~= trigger then return end

    local rest = msg:sub(#trigger + 1):match("^%s*(.-)%s*$")
    if rest == "" then
        say(player, "Usage: " .. trigger .. " <gold>   — pays in Ootle XTR, gold arrives by mail.")
        return false
    end
    local gold = tonumber(rest)
    -- REFUSED HERE RATHER THAN SENT ON. `250.5` and `1e9` both survive
    -- `tonumber`, and the till would refuse them -- but only after a network
    -- round trip that tells the player nothing useful about what they typed.
    if not gold or gold ~= math.floor(gold) or gold <= 0 then
        say(player, "That is not a whole number of gold.")
        return false
    end
    requestOrder(player, gold)
    return false
end

-- ---------------------------------------------------------------------------
-- DELIVERY
-- ---------------------------------------------------------------------------

-- Refs this server is mid-way through delivering. It stops a slow claim being
-- claimed twice by two overlapping polls; it is NOT the guard against double
-- payment, which is the till's `delivering` state and survives a restart.
local inFlight = {}

local function acknowledge(ref, name, gold)
    local cfg = config()
    HttpRequest("POST", cfg.url .. "/delivered",
        string.format('{"ref":"%s"}', ref), "application/json", headers(cfg),
        function(status, response)
            inFlight[ref] = nil
            if status ~= 200 then
                -- The gold IS in their mailbox; only the bookkeeping failed.
                -- Said out loud in the server log because the order will sit
                -- in `delivering` until a person looks at it, and a silent
                -- line here is how that becomes a mystery later.
                print(string.format(
                    "[azeroth_till] %s was mailed but the till did not record it (status %d): %s",
                    ref, status, tostring(response)))
                return
            end
            local who = GetPlayerByName(name)
            if who then say(who, string.format("%d gold delivered for %s -- check your mailbox.", gold, ref)) end
        end)
end

local function deliver(ref, guidLow, copper, gold, name)
    if inFlight[ref] then return end
    inFlight[ref] = true
    local cfg = config()
    SendMail(cfg.mail_subject,
        string.format("Your order %s: %d gold, paid in Ootle XTR. Thank you.", ref, gold),
        guidLow, cfg.sender_guid, MAIL_STATIONERY_GM, 0, copper, 0)
    print(string.format("[azeroth_till] mailed %d copper to guid %d for %s", copper, guidLow, ref))
    acknowledge(ref, name, gold)
end

local function poll()
    local cfg = config()
    if cfg.token == "" then return end
    HttpRequest("POST", cfg.url .. "/claim",
        string.format('{"limit":%d}', cfg.claim_limit), "application/json", headers(cfg),
        function(status, response)
            if status ~= 200 then return end
            for _, line in ipairs(lines(response)) do
                if line.kind == "DELIVER" then
                    local ref, guidLow, copper, gold, name =
                        line.rest:match("^(%S+)%s+(%d+)%s+(%d+)%s+(%d+)%s+(%S+)$")
                    if ref then
                        deliver(ref, tonumber(guidLow), tonumber(copper), tonumber(gold), name)
                    end
                end
            end
        end)
end

RegisterPlayerEvent(PLAYER_EVENT_ON_CHAT, onChat)
CreateLuaEvent(poll, config().poll_ms, 0)

print("[azeroth_till] loaded; trigger is " .. config().trigger .. ", till at " .. config().url)
