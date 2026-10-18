--[[ ==========================================================================
  Render tray.lua to a PNG, without conky.

    lua tray_preview.lua <out.png> [size=WxH] [scale=S] [setting=value ...]

  Draws the panel from what the running panel's helper last wrote (its
  apps, tray icons and so on), with any setting from tray.lua's USER
  CONFIGURATION overridden, e.g. style=pixel clock_format=12h. Conky-Mirage's
  helper cuts the glass for exactly these panes, into a cache of the
  preview's own, and the panel is laid over the wallpaper behind it.

  size is the panel in real pixels and scale the desktop's (3440x65 at 1.15
  on a 3440x1440 screen at 115%); like conky's, the surface carries the
  scale. The last line printed is "size WxH".
============================================================================ ]]

for _, dir in ipairs({"/usr/lib64/conky", "/usr/lib/conky", "/usr/lib/x86_64-linux-gnu/conky"}) do
  package.cpath = dir .. "/lib?.so;" .. package.cpath
end
require "cairo"

local HERE = debug.getinfo(1, "S").source:match("^@(.*)/[^/]*$") or "."
local OUT = assert(arg[1], "usage: tray_preview.lua <out.png> [size=WxH] [scale=S] [setting=value ...]")
local W, H, SCALE = 3440, 65, 1.15
local overrides = {}
for i = 2, #arg do
  local key, value = arg[i]:match("^([%w_]+)=(.*)$")
  if key == "size" then
    local w, h = value:match("^(%d+)x(%d+)$")
    W, H = tonumber(w) or W, tonumber(h) or H
  elseif key == "scale" then
    SCALE = tonumber(value) or SCALE
  elseif key then
    overrides[key] = value
  end
end

local function command(text)
  local pipe = io.popen(text .. " 2>/dev/null")
  local out = pipe and pipe:read("a") or ""
  if pipe then pipe:close() end
  return out
end
local function shell_quote(s) return "'" .. tostring(s):gsub("'", "'\\''") .. "'" end

local home_cache = os.getenv("XDG_CACHE_HOME") or ((os.getenv("HOME") or "/tmp") .. "/.cache")
tray_cache_dir = os.getenv("TRAY_CACHE") or (home_cache .. "/conky-tray")
local PREVIEW = os.getenv("TRAY_PREVIEW_CACHE") or (home_cache .. "/conky-tray-preview")
tray_preview = true

local f = assert(io.open(HERE .. "/tray.lua", "r"))
local code = f:read("a")
f:close()
for key, value in pairs(overrides) do
  local literal = tonumber(value) and value or string.format("%q", value)
  literal = literal:gsub("%%", "%%%%")      -- a date format's % would read as a capture
  local count
  code, count = code:gsub("\n" .. key .. "%s*=%s*[^\n]-(%s*%-%-[^\n]*)\n", "\n" .. key .. " = " .. literal .. "%1\n", 1)
  if count == 0 then
    code, count = code:gsub("\n" .. key .. "%s*=%s*[^\n]*\n", "\n" .. key .. " = " .. literal .. "\n", 1)
  end
  if count == 0 then io.stderr:write("no setting named " .. key .. "\n") end
end
assert(load(code, "@" .. HERE .. "/tray.lua"))()

local surface = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, W, H)
cairo_surface_set_device_scale(surface, SCALE, SCALE)
conky_window = {width = W / SCALE, height = H / SCALE}
function conky_surface() return surface end

conky_tray()

-- The glass for these panes, where the running panel is (or would be).
local mirage = HERE .. "/../Conky-Mirage/mirage.py"
local L = tray_preview_layout
if L and io.open(mirage, "r") then
  local x, y = 0, nil
  local loaded = loadfile(tray_cache_dir .. "/state.lua", "t", {})
  local ok, state = false, nil
  if loaded then ok, state = pcall(loaded) end
  if ok and type(state) == "table" and state.panel then
    x = state.panel.x
    y = state.panel.y + (state.panel.h or H) - H
  end
  if not y then
    local mon_h = tonumber(command("xrandr --listmonitors"):match("%d+/%d+x(%d+)/")) or 1080
    y = mon_h - H
  end
  local tiles = {}
  if L.look == "glass" or L.look == "solid" then
    for _, p in ipairs(L.panes) do tiles[#tiles + 1] = p.key end
  end
  os.execute("mkdir -p " .. shell_quote(PREVIEW))
  os.execute(string.format("python3 %s theme --cache %s --at %d,%d --window %dx%d --tiles %s " ..
                           "--radius %.2f --scale %.4f --blur %.1f --tint 0.260 >/dev/null 2>&1",
                           shell_quote(mirage), shell_quote(PREVIEW), x, y, W, H, shell_quote(table.concat(tiles, ";")),
                           math.min(L.radius, L.bh / 2), L.u, 34 * L.u))
  tray_preview_theme = {dir = PREVIEW, id = 0, stamp = os.time() .. "." .. os.clock()}
end

conky_tray()

local out = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, W, H)
local cr = cairo_create(out)
local under = cairo_image_surface_create_from_png(PREVIEW .. "/under.png")
if cairo_surface_status(under) == CAIRO_STATUS_SUCCESS then
  cairo_set_source_surface(cr, under, 0, 0)
else
  cairo_set_source_rgb(cr, 0.12, 0.12, 0.16)
end
cairo_paint(cr)
-- The panel's surface is scaled; paint its pixels one for one.
cairo_surface_set_device_scale(surface, 1, 1)
cairo_set_source_surface(cr, surface, 0, 0)
cairo_paint(cr)
cairo_destroy(cr)
cairo_surface_write_to_png(out, OUT)
print(string.format("size %dx%d", W, H))
