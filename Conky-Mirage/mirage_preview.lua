--[[ ==========================================================================
  Render mirage.lua to a PNG, without conky.

    lua mirage_preview.lua <out.png> [where] [scale] [setting=value ...]

  <out.png>   written twice: as given, composited over the wallpaper exactly as
              it sits behind the widget, and as <out>-alpha.png on transparency
  [where]     where the window would be on screen, which is where the glass is
              cut from: "x,y", or an alignment and margins as conky takes them,
              "top_right:40,40". "-" or nothing reads start_conky_mirage.
  [scale]     the widget's scale: a number, "auto", or "-" for start_conky_mirage's
  setting=value  overrides a USER CONFIGURATION line, e.g. units=imperial

  The window is sized from the layout -- mirage_natural_size() times the scale --
  so size settings passed as overrides are previewed at their true size. The
  last line printed says where it went:
      size 816x924 at 2584,40 scale 1.000 natural 816x924

  MIRAGE_PREVIEW_STILL=1 skips the intro and the palette fade and draws two
  frames a second apart instead of fifteen, which is what mirage_settings.py
  wants: a settled frame, quickly. MIRAGE_PREVIEW_SIZE_ONLY=1 draws nothing
  and prints only "natural WxH", the size these settings make at scale 1.

  The widget cannot be screenshotted on a Wayland session -- a compositor will
  not let an X11 client grab the screen -- so this gives the script the
  globals conky would have provided and calls its draw hook against an image
  surface. The helper runs synchronously instead of detached, into a cache of
  its own, so a preview never disturbs a running widget.

  Time is synthetic: frames are drawn 0.25s apart until the intro and the
  palette fade have finished, clearing between them as conky does.
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
  os.exit(1)
end

local HERE = debug.getinfo(1, "S").source:match("^@(.*)/[^/]*$") or "."
local OUT = assert(arg[1], "usage: mirage_preview.lua <out.png> [x,y] [scale] [setting=value ...]")

local function command(text)
  local pipe = io.popen(text .. " 2>/dev/null")
  local out = pipe and pipe:read("a") or ""
  if pipe then pipe:close() end
  return out
end

local listing = command("xrandr --listmonitors")
local mon_w, mon_h, mon_x, mon_y = listing:match("%+%*%S+%s+(%d+)/%d+x(%d+)/%d+%+(%-?%d+)%+(%-?%d+)")
if not mon_w then
  mon_w, mon_h, mon_x, mon_y = listing:match("%s(%d+)/%d+x(%d+)/%d+%+(%-?%d+)%+(%-?%d+)")
end
mon_w, mon_h = tonumber(mon_w) or 1920, tonumber(mon_h) or 1080
mon_x, mon_y = tonumber(mon_x) or 0, tonumber(mon_y) or 0

-- Conky places its window inside the work area -- the monitor less any
-- panels -- so a widget aligned to the bottom sits above the panel.
local area = {mon_x, mon_y, mon_w, mon_h}
do
  local wx, wy, ww, wh = command("xprop -root _NET_WORKAREA"):match("=%s*(%d+),%s*(%d+),%s*(%d+),%s*(%d+)")
  wx, wy, ww, wh = tonumber(wx), tonumber(wy), tonumber(ww), tonumber(wh)
  if wx then
    local x0, y0 = math.max(mon_x, wx), math.max(mon_y, wy)
    local x1, y1 = math.min(mon_x + mon_w, wx + ww), math.min(mon_y + mon_h, wy + wh)
    if x1 > x0 and y1 > y0 then area = {x0, y0, x1 - x0, y1 - y0} end
  end
end

-- What start_conky_mirage says, for anything not given on the command line.
local config = {scale = "auto", position = "top_right", margin_x = 40, margin_y = 40}
do
  local f = io.open(HERE .. "/start_conky_mirage", "r")
  local text = f and f:read("a") or ""
  if f then f:close() end
  config.scale = text:match("\nlocal scale = \"?([%w%.]+)\"?") or config.scale
  config.position = text:match("\nlocal position = ['\"]([%w_]+)['\"]") or config.position
  local mx, my = text:match("\nlocal margin_x, margin_y = (%-?%d+), (%-?%d+)")
  config.margin_x, config.margin_y = tonumber(mx) or 40, tonumber(my) or 40
end

local overrides = {}
for i = 4, #arg do
  local key, value = arg[i]:match("^([%w_]+)=(.*)$")
  if key then overrides[key] = value end
end

---------------- synthetic time ----------------

local clock = os.time()
local uptime = 100000

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

---------------- load the script, with any overrides ----------------

-- These are read as the script loads, so they come first. The cache is a
-- separate one: a preview must never touch a running widget's.
mirage_cache_dir = (os.getenv("MIRAGE_PREVIEW_CACHE") or "/tmp/conky-mirage-preview")
mirage_preview_sync = true
mirage_preview_still = os.getenv("MIRAGE_PREVIEW_STILL") == "1" or nil

local path = HERE .. "/mirage.lua"
local file = assert(real_open(path, "r"))
local code = file:read("a")
file:close()
for key, value in pairs(overrides) do
  local literal = tonumber(value) and value or string.format("%q", value)
  local count
  code, count = code:gsub("\n" .. key .. "%s*=%s*[^\n]-(%s*%-%-[^\n]*)\n", "\n" .. key .. " = " .. literal .. "%1\n", 1)
  if count == 0 then
    code, count = code:gsub("\n" .. key .. "%s*=%s*[^\n]*\n", "\n" .. key .. " = " .. literal .. "\n", 1)
  end
  if count == 0 then io.stderr:write("no setting named " .. key .. "\n") end
end
assert(load(code, "@" .. path))()

---------------- size and place the window ----------------

local base_w, base_h = mirage_natural_size()
-- MIRAGE_PREVIEW_SIZE_ONLY=1: just say how big these settings make it.
if os.getenv("MIRAGE_PREVIEW_SIZE_ONLY") == "1" then
  print(string.format("natural %dx%d", base_w, base_h))
  os.exit(0)
end

-- The same rule as scale = "auto" in start_conky_mirage.
local wanted = arg[3] and arg[3] ~= "-" and arg[3] or config.scale
local SCALE = tonumber(wanted) or math.min(math.max(mon_h / 1440, 0.85), (mon_h - 80) / base_h)
SCALE = math.max(0.45, math.min(SCALE, 3))
local W, H = math.floor(base_w * SCALE + 0.5), math.floor(base_h * SCALE + 0.5)

-- Conky's own placement: from the edges of the work area, or centred and
-- then moved by the margin.
local function place(alignment, gx, gy)
  local vertical, horizontal = alignment:match("^(%a+)_(%a+)$")
  local x, y
  if horizontal == "left" then x = area[1] + gx
  elseif horizontal == "right" then x = area[1] + area[3] - W - gx
  else x = area[1] + math.floor(area[3] / 2 - W / 2) - gx end
  if vertical == "top" then y = area[2] + gy
  elseif vertical == "bottom" then y = area[2] + area[4] - H - gy
  else y = area[2] + math.floor(area[4] / 2 - H / 2) - gy end
  return x, y
end

local AT = arg[2]
if not AT or AT == "-" then
  AT = string.format("%s:%d,%d", config.position, config.margin_x, config.margin_y)
end
if AT:match("^%a") then
  local alignment, mx, my = AT:match("^([%a_]+):?(%-?%d*),?(%-?%d*)$")
  local gx = math.floor((tonumber(mx) or config.margin_x) * SCALE + 0.5)
  local gy = math.floor((tonumber(my) or config.margin_y) * SCALE + 0.5)
  AT = string.format("%d,%d", place(alignment or "top_right", gx, gy))
end
mirage_preview_at = AT

---------------- the globals conky would have supplied ----------------

conky_window = {width = W, height = H}
local surface = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, W, H)
function conky_surface() return surface end

function conky_parse(s)
  if s == "${updates}" then return "999999" end
  local mount = s:match("^%${fs_used_perc ([^}]+)}$")
  if mount then return command("df -P '" .. mount .. "' | tail -1"):match("(%d+)%%") or "0" end
  mount = s:match("^%${fs_free ([^}]+)}$")
  if mount then
    local kb = tonumber(command("df -Pk '" .. mount .. "' | tail -1"):match("%S+%s+%d+%s+%d+%s+(%d+)") or "")
    if kb then return string.format("%.0fGiB", kb / 1048576) end
  end
  return ""
end


---------------- draw ----------------

local function frame()
  local cr = cairo_create(surface)
  cairo_set_operator(cr, CAIRO_OPERATOR_CLEAR)
  cairo_paint(cr)
  cairo_destroy(cr)
  conky_mirage()
end

local STEPS, DT = tonumber(os.getenv("MIRAGE_PREVIEW_STEPS") or "") or 14, 0.25
-- A still needs no intro, but two samples a second apart give the gauges
-- something to show.
if mirage_preview_still then STEPS, DT = 1, 1.0 end
for _ = 1, STEPS do
  frame()
  clock = clock + DT
  uptime = uptime + DT
end
frame()

local alpha_out = OUT:gsub("%.png$", "") .. "-alpha.png"
cairo_surface_write_to_png(surface, alpha_out)

local under = cairo_image_surface_create_from_png(mirage_cache_dir .. "/under.png")
local composite = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, W, H)
local cr = cairo_create(composite)
if cairo_surface_status(under) == CAIRO_STATUS_SUCCESS then
  cairo_set_source_surface(cr, under, 0, 0)
else
  cairo_set_source_rgb(cr, 0.1, 0.1, 0.14)
end
cairo_paint(cr)
cairo_set_source_surface(cr, surface, 0, 0)
cairo_paint(cr)
cairo_destroy(cr)
cairo_surface_write_to_png(composite, OUT)
print(string.format("size %dx%d at %s scale %.3f natural %dx%d", W, H, AT, SCALE, base_w, base_h))
