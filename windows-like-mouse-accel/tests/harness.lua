-- Runs a generated winaccel plugin under a stub of libinput's Lua plugin API
-- (doc/user/lua-plugins.rst, libinput 1.32), in the same restricted sandbox.
--
-- usage: lua harness.lua PLUGIN VID PID UDEVPROPS < packets
--   UDEVPROPS: comma separated, e.g. ID_INPUT_MOUSE
--   each input line "dx dy" is one evdev frame (a zero is omitted from the frame,
--   like the kernel does); each output line is the frame libinput would receive.

local plugin_path, vid, pid, props = arg[1], tonumber(arg[2]), tonumber(arg[3]), arg[4]

local evdev = {
    REL_X = (2 << 16) | 0,
    REL_Y = (2 << 16) | 1,
    BTN_LEFT = (1 << 16) | 0x110,
    BTN_RIGHT = (1 << 16) | 0x111,
}

local callbacks, device_callbacks, registered = {}, {}, false
local logs = {}

local libinput = {}
function libinput:register(versions) registered = true return 1 end
function libinput:connect(name, fn) callbacks[name] = fn end
function libinput:log_info(msg) logs[#logs + 1] = msg end
function libinput:log_debug(msg) end
function libinput:log_error(msg) io.stderr:write("plugin error log: " .. msg .. "\n") end
function libinput:now() return 0 end

local udev = {}
for p in string.gmatch(props, "[^,]+") do udev[p] = true end

local device = {}
function device:usages()
    return { [evdev.REL_X] = true, [evdev.REL_Y] = true, [evdev.BTN_LEFT] = true, [evdev.BTN_RIGHT] = true }
end
function device:udev_properties() return udev end
function device:info() return { bustype = 3, vid = vid, pid = pid } end
function device:name() return "stub mouse" end
function device:connect(name, fn) device_callbacks[name] = fn end

-- The globals libinput exposes to plugins, nothing else (no io, os, require, ...).
local env = {
    assert = assert, error = error, ipairs = ipairs, next = next, pairs = pairs,
    tonumber = tonumber, pcall = pcall, select = select, print = print,
    tostring = tostring, type = type, xpcall = xpcall, table = table,
    string = string, math = math, _VERSION = _VERSION,
    libinput = libinput, evdev = evdev,
}
local f = assert(io.open(plugin_path))
local chunk = assert(load(f:read("a"), "=" .. plugin_path, "t", env))
f:close()
chunk()
assert(registered, "plugin did not register")
callbacks["new-evdev-device"](device)

local handler = device_callbacks["evdev-frame"]
io.write(handler and "attached\n" or "not-attached\n")
if not handler then return end

local out = {}
for line in io.lines() do
    local dx, dy = line:match("(-?%d+) (-?%d+)")
    dx, dy = math.tointeger(tonumber(dx)), math.tointeger(tonumber(dy))
    local frame = {}
    if dx ~= 0 then frame[#frame + 1] = { usage = evdev.REL_X, value = dx } end
    frame[#frame + 1] = { usage = evdev.BTN_LEFT, value = 1 }
    if dy ~= 0 then frame[#frame + 1] = { usage = evdev.REL_Y, value = dy } end
    local result = handler(device, frame, 0) or frame
    local ox, oy, buttons = 0, 0, 0
    for _, e in ipairs(result) do
        -- libinput reads values with luaL_checkinteger into an int32_t
        assert(math.type(e.value) == "integer", "non-integer event value")
        assert(e.value >= -2147483648 and e.value <= 2147483647, "value out of int32 range")
        if e.usage == evdev.REL_X then ox = ox + e.value
        elseif e.usage == evdev.REL_Y then oy = oy + e.value
        elseif e.usage == evdev.BTN_LEFT then buttons = buttons + 1 end
    end
    assert(buttons == 1, "button event lost or duplicated")
    out[#out + 1] = ox .. " " .. oy
end
io.write(table.concat(out, "\n"), "\n")
