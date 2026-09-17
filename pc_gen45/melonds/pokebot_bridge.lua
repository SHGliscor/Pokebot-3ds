-- Pokebot Gen4/5 melonDS bridge v0p1
--
-- Requires the Pokebot melonDS-Lua build, which adds memory.read_block().
-- IPC is deliberately local-file based: no firewall, network adapter or
-- localhost socket setup is required.

local memory = require("core.memory")
local emu = require("core.emu")

local source = debug.getinfo(1, "S").source or ""
local script_path = string.sub(source, 1, 1) == "@" and string.sub(source, 2) or source
local script_dir = string.match(script_path, "^(.*)[/\\]") or "."
local IPC_DIR = script_dir .. "/../../pokebot_ipc"
local COMMAND = IPC_DIR .. "/command.tsv"
local RESPONSE = IPC_DIR .. "/response.bin"

local last_seq = -1
local pulses = {}

local function split_tabs(line)
    local out = {}
    for field in string.gmatch(line, "([^\t]+)") do
        table.insert(out, field)
    end
    return out
end

local function respond(seq, fields, payload)
    local f = io.open(RESPONSE, "wb")
    if not f then
        return
    end
    local header = tostring(seq) .. "\tOK"
    for _, value in ipairs(fields or {}) do
        header = header .. "\t" .. tostring(value)
    end
    f:write(header .. "\n")
    if payload then
        f:write(payload)
    end
    f:close()
end

local function fail(seq, message)
    local f = io.open(RESPONSE, "wb")
    if not f then
        return
    end
    f:write(tostring(seq) .. "\tERR\t" .. tostring(message) .. "\n")
    f:close()
end

local function process_pulses()
    for key, frames in pairs(pulses) do
        frames = frames - 1
        if frames <= 0 then
            emu.setInput(key, false)
            pulses[key] = nil
        else
            pulses[key] = frames
        end
    end
end

local function process_command()
    local f = io.open(COMMAND, "rb")
    if not f then
        return
    end
    local line = f:read("*l")
    f:close()
    if not line or line == "" then
        return
    end

    local parts = split_tabs(line)
    local seq = tonumber(parts[1])
    local cmd = parts[2]
    if not seq or not cmd or seq == last_seq then
        return
    end
    last_seq = seq

    local ok, err = pcall(function()
        if cmd == "PING" then
            respond(seq, {"Pokebot-melonDS-v0p1"})
            return
        end

        if cmd == "READ" then
            local addr = tonumber(parts[3])
            local len = tonumber(parts[4])
            if not addr or not len or len < 1 or len > 0x400000 then
                error("bad READ")
            end
            local data = memory.read_block(addr, len)
            respond(seq, {"BIN", #data}, data)
            return
        end

        if cmd == "KEY" then
            local key = assert(parts[3], "missing key")
            local pressed = tonumber(parts[4]) == 1
            emu.setInput(key, pressed)
            respond(seq, {"KEY"})
            return
        end

        if cmd == "PULSE" then
            local key = assert(parts[3], "missing key")
            local frames = tonumber(parts[4]) or 2
            if frames < 1 or frames > 600 then
                error("bad pulse length")
            end
            emu.setInput(key, true)
            pulses[key] = frames
            respond(seq, {"PULSE", frames})
            return
        end

        if cmd == "FAST_FORWARD" then
            local enabled = tonumber(parts[3]) == 1
            emu.setFastForward(enabled)
            respond(seq, {"FAST_FORWARD", enabled and 1 or 0})
            return
        end

        if cmd == "RELEASE_ALL" then
            pulses = {}
            emu.resetInput()
            respond(seq, {"RELEASED"})
            return
        end

        if cmd == "RESET" then
            pulses = {}
            emu.resetInput()
            emu.setFastForward(false)
            emu.reset()
            respond(seq, {"RESET"})
            return
        end

        error("unknown command " .. tostring(cmd))
    end)

    if not ok then
        fail(seq, err)
    end
end

emu.onFrame(function()
    process_pulses()
    process_command()
end)

print("[Pokebot] Gen4/5 bridge v0p1 active")
print("[Pokebot] IPC directory: " .. IPC_DIR)
