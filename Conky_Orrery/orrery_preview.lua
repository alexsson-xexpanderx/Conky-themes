--[[ ==========================================================================
  Render one frame of lua_orrery.lua to a PNG, without conky.

    lua orrery_preview.lua <script> <out.png> <size> [phase] [background]

  <script>      a copy of lua_orrery.lua, normally one the colour editor has
                written candidate colours into
  <size>        width and height of the output, in pixels
  [phase]       seconds to advance the camera by, for looking at the assembly
                from a different angle; defaults to 0
  [background]  RRGGBB to composite onto, or "none" for a transparent PNG

  This exists because the widget cannot be screenshotted on a Wayland session --
  a compositor will not let an X11 client grab the screen -- and because
  spawning conky to look at a colour change is far slower than drawing one
  frame. It works by giving the script the handful of globals conky would have
  provided and calling its draw hook against an image surface.

  Three of those need care:

  * The animation clock comes from /proc/uptime, so io.open is wrapped to serve
    a synthetic one. Without it every frame would land at the same instant and
    the eased readouts would never leave zero.
  * os.time() is overridden to agree with that synthetic clock, or the script
    would decide the two had drifted apart and resynchronise every frame.
  * hwmon reads are *not* intercepted. They fall through to the real files, so
    the preview shows the machine's own core count and temperatures rather than
    invented ones.
============================================================================ ]]

local CONKY_LIB_DIRS = {
  "/usr/lib64/conky", "/usr/lib/conky",
  "/usr/lib/x86_64-linux-gnu/conky", "/usr/local/lib/conky",
}
for _, dir in ipairs(CONKY_LIB_DIRS) do
  package.cpath = dir .. "/lib?.so;" .. package.cpath
end

local ok, err = pcall(require, "cairo")
if not ok then
  io.stderr:write("cannot load conky's cairo bindings: " .. tostring(err) .. "\n")
  io.stderr:write("looked in: " .. table.concat(CONKY_LIB_DIRS, " ") .. "\n")
  os.exit(1)
end

local SCRIPT = assert(arg[1], "usage: orrery_preview.lua <script> <out.png> <size> [phase] [bg]")
local OUT    = assert(arg[2], "missing output path")
local SIZE   = tonumber(arg[3]) or 560
local PHASE  = tonumber(arg[4]) or 0
local BG     = arg[5] or "0B0E17"

-- Settling: dt is clamped to 0.25s inside the widget, so this many steps of
-- that length is how much widget time passes before the frame that gets kept.
-- The readouts ease with a time constant of 0.45s, so three seconds is
-- comfortably enough for them to arrive at their real values.
local SETTLE_STEPS, SETTLE_DT = 14, 0.25

-- Anchor the clock to midday today. The date is therefore real -- which matters,
-- because the lit day, month and weekday are what the accent colour is judged
-- on -- while the camera angle depends only on PHASE, so two renders differ by
-- exactly the colours that were changed and nothing else.
local function midday_today()
  local now = os.date("*t")
  return os.time({year = now.year, month = now.month, day = now.day, hour = 12, min = 0, sec = 0})
end

local clock = midday_today() + PHASE
local uptime = 100000 + PHASE

local real_open = io.open
io.open = function(path, mode)
  if path == "/proc/uptime" then
    local served = false
    return {
      read = function()
        if served then return nil end
        served = true
        return string.format("%.2f 1.0", uptime)
      end,
      close = function() end,
    }
  end
  return real_open(path, mode)
end

local real_time = os.time
os.time = function(t)
  if t then return real_time(t) end
  return math.floor(clock)
end

---------------- the globals conky would have supplied ----------------

conky_window = {width = SIZE, height = SIZE}

local surface = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, SIZE, SIZE)
function conky_surface() return surface end

local function slurp(path)
  local f = real_open(path, "r")
  if not f then return "" end
  local s = f:read("*a")
  f:close()
  return s
end

-- Real figures where they are cheap to get. Load average stands in for ${cpu}:
-- the real thing needs two samples of /proc/stat a moment apart, and a tenth of
-- a second of latency on every keystroke in a colour picker is not worth an
-- exact percentage on a preview.
local cores = 0
for line in slurp("/proc/cpuinfo"):gmatch("[^\n]+") do
  if line:match("^processor") then cores = cores + 1 end
end
local load1 = tonumber(slurp("/proc/loadavg"):match("^(%S+)")) or 0
local CPU = math.min(100, load1 / math.max(cores, 1) * 100)

local meminfo = slurp("/proc/meminfo")
local memtotal = tonumber(meminfo:match("MemTotal:%s+(%d+)")) or 1
local memavail = tonumber(meminfo:match("MemAvailable:%s+(%d+)")) or 0
local MEM = 100 * (memtotal - memavail) / memtotal

local free_cache = {}
local function free_percent(mount)
  if free_cache[mount] == nil then
    local pipe = io.popen("df -P '" .. mount:gsub("'", "'\\''") .. "' 2>/dev/null | tail -1")
    local used = pipe and tonumber((pipe:read("*a") or ""):match("(%d+)%%"))
    if pipe then pipe:close() end
    free_cache[mount] = used and (100 - used) or 100
  end
  return free_cache[mount]
end

function conky_parse(s)
  if s == "${updates}" then return "999999" end
  if s == "${cpu cpu0}" then return string.format("%.1f", CPU) end
  if s == "${memperc}" then return string.format("%.1f", MEM) end
  local mount = s:match("^%${fs_free_perc ([^}]+)}$")
  if mount then return tostring(free_percent(mount)) end
  -- Anything else, including the nvidia-smi fallback, returns empty; the widget
  -- funnels every reading through number_or() and copes with that.
  return ""
end

---------------- draw ----------------

local loaded, load_err = loadfile(SCRIPT)
if not loaded then
  io.stderr:write("cannot load " .. SCRIPT .. ": " .. tostring(load_err) .. "\n")
  os.exit(1)
end
loaded()

if type(conky_start_widgets) ~= "function" then
  io.stderr:write(SCRIPT .. " defines no conky_start_widgets\n")
  os.exit(1)
end

local function clear()
  local cr = cairo_create(surface)
  cairo_set_operator(cr, CAIRO_OPERATOR_SOURCE)
  if BG == "none" then
    cairo_set_source_rgba(cr, 0, 0, 0, 0)
  else
    local hex = BG:gsub("#", "")
    cairo_set_source_rgba(cr,
      tonumber(hex:sub(1, 2), 16) / 255,
      tonumber(hex:sub(3, 4), 16) / 255,
      tonumber(hex:sub(5, 6), 16) / 255, 1)
  end
  cairo_paint(cr)
  cairo_destroy(cr)
end

-- Conky repaints the whole window every tick, so the surface is cleared every
-- step too. Without that, fourteen frames of glow would pile up and the render
-- would come out far brighter than the real widget.
for _ = 1, SETTLE_STEPS do
  clear()
  local drawn, draw_err = pcall(conky_start_widgets)
  if not drawn then
    io.stderr:write("draw failed: " .. tostring(draw_err) .. "\n")
    os.exit(1)
  end
  clock = clock + SETTLE_DT
  uptime = uptime + SETTLE_DT
end

local status = cairo_surface_write_to_png(surface, OUT)
cairo_surface_destroy(surface)
if status ~= 0 and status ~= "0" and status ~= nil then
  io.stderr:write("could not write " .. OUT .. " (cairo status " .. tostring(status) .. ")\n")
  os.exit(1)
end
