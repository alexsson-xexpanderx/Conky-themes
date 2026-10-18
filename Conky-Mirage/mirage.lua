--[[ ==========================================================================
  Conky-Mirage

  A weather and system panel made of frosted glass cut from your own
  wallpaper. Every tile is the patch of wallpaper behind it, blurred and
  tinted, and every colour is lifted from the wallpaper -- change it, and the
  widget re-tints itself to match within a few seconds.

  The heavy lifting happens out of the frame loop: mirage.py (run detached
  every few seconds) finds the wallpaper, renders the glass and fetches the
  forecast from Open-Meteo, which needs no API key. This script only paints
  what it left in the cache, plus the live parts: an animated weather icon and
  the system gauges.

  Start with:   conky -c /path/to/Conky-Mirage/start_conky_mirage
============================================================================ ]]

---------------- USER CONFIGURATION ----------------

-- Where to get the weather for. "auto" finds it from your internet
-- connection. Or write a city ("Cluj-Napoca"), a city and country
-- ("Paris, FR"), or coordinates ("46.77, 23.59, Home").
location = "auto"

units = "metric"                 -- "metric" (°C, km/h) or "imperial" (°F, mph)
show_temperature_unit = "Yes"    -- "Yes" shows 10°C, "No" shows 10°
air_quality_scale = "european"   -- "european" or "us"
weather_refresh_minutes = 15     -- how often to get a new forecast

clock_format = "24h"             -- "24h" or "12h"
week_starts_on = "monday"        -- "monday" or "sunday"

-- The look: "glass" (a blurred piece of your wallpaper in each box), "solid"
-- (plain boxes in your wallpaper's colours), "metro" (flat square tiles in
-- bold colours) or "pixel" (soft rounded tones, like Android phones). Easiest
-- to pick in ./mirage_settings.py, which also sets matching corners and spacing.
style = "metro"
transparency = 0.00              -- how see-through the boxes are, from 0 to 0.9
wallpaper = "auto"               -- "auto", or the full path to your wallpaper picture
glass_tone = "auto"              -- "auto", "dark" or "light" glass
accent_color = "auto"            -- "auto" picks it from your wallpaper, or a colour like "#FF8800"
blur_radius = 34                 -- how blurry the glass is
glass_tint = 0.26                -- how much the glass hides the wallpaper, from 0 to 1
vibrancy = 1.35                  -- how colourful the glass is
shadow_strength = 0.45           -- how dark the shadow under the boxes is

-- Which boxes to show. The others move together to fill the gap.
show_clock   = "Yes"
show_now     = "Yes"             -- the weather right now
show_hourly  = "Yes"             -- the next 24 hours
show_week    = "Yes"             -- the next 7 days
show_details = "Yes"             -- wind, humidity, UV and air quality
show_sky     = "Yes"             -- sunrise, sunset and the moon
show_system  = "Yes"             -- CPU, memory, GPU, disks and network

-- The disks to show, one ring each. Use where they're mounted ("/", "/home")
-- or the device name ("/dev/nvme0n1p3"). A disk that isn't mounted is skipped.
disks = {"/", "/dev/nvme0n1p3"}
network_interface = "auto"       -- "auto" uses the busiest connection, or a name like "wlan0"
show_gpu = "Yes"                 -- "No" hides the GPU ring

-- Fonts. Any installed font works. Inter, Outfit or Manrope look great if you
-- have them; set all three lines to that font.
font_regular = "Noto Sans"
font_medium  = "Noto Sans Medium"
font_display = "Noto Sans Light" -- the big clock and temperature

-- Sizes. Easiest to change with ./mirage_settings.py, which shows a preview.
-- If you change them here instead, look at what Conky prints when it starts:
-- it tells you what to put in start_conky_mirage.
text_size        = 1.20          -- all text (the boxes grow to fit)
clock_size       = 1.00          -- the big clock
temperature_size = 1.00          -- the big temperature
caption_size     = 1.00          -- the small titles at the top of each box
widget_width     = 760           -- how wide the whole widget is
tile_gap         = 8             -- space between the boxes
tile_padding     = 22            -- space inside each box
corner_radius    = 0             -- how round the corners are

-- How smooth the animations are, in frames per second. Lower uses less CPU.
-- If you change it, set fps in start_conky_mirage to the same number.
target_fps = 10
animate = "Yes"                  -- "No" turns the animations off

---------------- DON'T EDIT BELOW IF YOU DO NOT KNOW WHAT YOU ARE DOING ----------------

require 'cairo'
-- Conky moved cairo_xlib_surface_create into its own module; older builds
-- still export it from 'cairo', so a missing module here is harmless.
pcall(require, 'cairo_xlib')

-- Newer Conky hands out a cached surface for its own window and keeps ownership
-- of it; older builds need an xlib surface made (and freed) on every draw. The
-- second return value says whether this code is responsible for destroying it.
local function conky_window_surface()
  if type(conky_surface) == "function" then
    return conky_surface(), false
  end
  return cairo_xlib_surface_create(conky_window.display, conky_window.drawable,
                                   conky_window.visual, conky_window.width,
                                   conky_window.height), true
end

local sin, cos, abs, floor, ceil = math.sin, math.cos, math.abs, math.floor, math.ceil
local sqrt, max, min, pi = math.sqrt, math.max, math.min, math.pi
local TAU = 2 * pi

local function yes(value) return tostring(value):lower() == "yes" end
local function clamp(v, low, high)
  if v < low then return low elseif v > high then return high end
  return v
end
local function lerp(a, b, t) return a + (b - a) * t end
local function ease_out(t) t = clamp(t, 0, 1); return 1 - (1 - t) ^ 3 end
local function round(v) return floor(v + 0.5) end
local function number_or(value, default) return tonumber(value) or default end

local ANIMATE = yes(animate)
local TEXT = clamp(number_or(text_size, 1), 0.6, 2)
local CLOCK = clamp(number_or(clock_size, 1), 0.5, 2)
local TEMPERATURE = clamp(number_or(temperature_size, 1), 0.5, 2)
local CAPTION = clamp(number_or(caption_size, 1), 0.6, 2)
local TWELVE_HOUR = tostring(clock_format):lower() == "12h"
local SUNDAY_FIRST = tostring(week_starts_on):lower() == "sunday"
local STYLE = tostring(style):lower()
if STYLE == "tint" then STYLE = "solid" end            -- the old name for plain panes
if STYLE ~= "solid" and STYLE ~= "metro" and STYLE ~= "pixel" then STYLE = "glass" end
local GLASS = STYLE == "glass"
-- How much of each box's background is drawn: 1 is as designed.
local OPAQUE = 1 - clamp(number_or(transparency, 0), 0, 0.9)
-- Pixel's big numbers are in the regular weight: Material's clock is round and
-- solid where the glass look's is light and thin.
local DISPLAY_FONT = STYLE == "pixel" and font_regular or font_display

local SCRIPT_DIR = debug.getinfo(1, "S").source:match("^@(.*)/[^/]*$") or "."
local CACHE = mirage_cache_dir or
  ((os.getenv("XDG_CACHE_HOME") or ((os.getenv("HOME") or "/tmp") .. "/.cache")) .. "/conky-mirage")
-- Must match own_window_title in start_conky_mirage: it is how the helper
-- finds where on the screen the widget is.
local TITLE = "conky-mirage"

local function read_first_line(path)
  local file = io.open(path, "r")
  if not file then return nil end
  local line = file:read("*l")
  file:close()
  return line
end

local function read_all(path)
  local file = io.open(path, "r")
  if not file then return nil end
  local text = file:read("*a")
  file:close()
  return text
end

-- The cache files are Lua tables written by mirage.py. They are loaded into
-- an empty environment, so even a corrupted one can only fail to load.
local function load_table(path)
  local chunk = loadfile(path, "t", {})
  if not chunk then return nil end
  local ok, value = pcall(chunk)
  if ok and type(value) == "table" then return value end
  return nil
end

---------------- A SUB-SECOND WALL CLOCK ----------------
-- Copied from lua_orrery.lua. os.time() has one-second resolution and
-- os.clock() counts CPU time, so neither can drive an animation; /proc/uptime
-- is monotonic and sub-second, and an offset from os.time() makes it wall time.

local frame_counter = 0
local epoch_offset = nil

local function wall_clock()
  local uptime = tonumber((read_first_line("/proc/uptime") or ""):match("^(%S+)") or "")
  if uptime == nil then
    frame_counter = frame_counter + 1
    return os.time() + (frame_counter / target_fps) % 1
  end
  if epoch_offset == nil or abs(uptime + epoch_offset - os.time()) > 1.25 then
    epoch_offset = os.time() - uptime
  end
  return uptime + epoch_offset
end

---------------- PALETTE ----------------
-- P is what everything draws with. It is the helper's palette once one has
-- arrived, and a blend of the old and new ones while a wallpaper change fades.

local FALLBACK = {
  mode = "dark",
  ink = {0.96, 0.96, 0.98}, accent = {0.72, 0.74, 1.00}, accent2 = {0.98, 0.72, 0.80},
  tint = {0.09, 0.09, 0.13}, shade = {0.0, 0.0, 0.02}, cloud = {0.94, 0.94, 0.97},
  sun = {1.00, 0.80, 0.46}, rain = {0.52, 0.74, 1.00},
  good = {0.55, 0.85, 0.62}, fair = {0.92, 0.82, 0.42}, poor = {1.0, 0.66, 0.40},
  bad = {1.00, 0.55, 0.55}, severe = {0.86, 0.56, 0.95},
  temperature = {{-20, 0.68, 0.72, 1.0}, {-8, 0.6, 0.74, 1.0}, {2, 0.47, 0.78, 1.0},
                 {10, 0.38, 0.83, 0.86}, {17, 0.66, 0.84, 0.48}, {23, 0.92, 0.78, 0.40},
                 {29, 1.0, 0.66, 0.42}, {35, 1.0, 0.56, 0.48}, {42, 1.0, 0.5, 0.56}},
  -- the other looks' colours, also from the helper
  solid = {0.13, 0.13, 0.18},
  metro1 = {0.23, 0.36, 0.72}, metro2 = {0.52, 0.27, 0.62}, metro3 = {0.13, 0.48, 0.58},
  metro4 = {0.62, 0.28, 0.36},
  px_surface = {0.14, 0.14, 0.18}, px_primary_c = {0.22, 0.24, 0.40}, px_secondary_c = {0.20, 0.21, 0.28},
  px_tertiary_c = {0.30, 0.22, 0.34}, px_on = {0.90, 0.90, 0.96}, px_primary = {0.74, 0.78, 1.00},
}
local COLOR_KEYS = {"ink", "accent", "accent2", "tint", "shade", "cloud", "sun", "rain",
                    "good", "fair", "poor", "bad", "severe", "solid", "metro1", "metro2", "metro3",
                    "metro4", "px_surface", "px_primary_c", "px_secondary_c", "px_tertiary_c",
                    "px_on", "px_primary"}

-- BASE is the theme's palette; P is the one being drawn with, which in the
-- looks other than glass differs from tile to tile (palette_for, below).
local BASE = FALLBACK
local P = BASE
local palette_version = 0

local function blend_palette(a, b, t)
  local out = {mode = t < 0.5 and a.mode or b.mode, temperature = {}}
  for _, key in ipairs(COLOR_KEYS) do
    local x, y = a[key] or FALLBACK[key], b[key] or FALLBACK[key]
    out[key] = {lerp(x[1], y[1], t), lerp(x[2], y[2], t), lerp(x[3], y[3], t)}
  end
  local from, to = a.temperature or FALLBACK.temperature, b.temperature or FALLBACK.temperature
  for i, stop in ipairs(to) do
    local o = from[i] or stop
    out.temperature[i] = {stop[1], lerp(o[2], stop[2], t), lerp(o[3], stop[3], t), lerp(o[4], stop[4], t)}
  end
  return out
end

local function mixc(a, b, t)
  return {lerp(a[1], b[1], t), lerp(a[2], b[2], t), lerp(a[3], b[3], t)}
end

local palette_for
do
  -- Which colour each box takes. Metro alternates four bold colours so that
  -- neighbours differ; Pixel gives each box a tonal role, the way Android's
  -- Material You colours its widgets.
  local METRO_TILE = {clock = 1, now = 2, hourly = 3, week = 4, wind = 3, humidity = 2,
                      uv = 1, air = 4, sky = 2, system = 1}
  local PIXEL_TILE = {clock = "px_primary_c", now = "px_tertiary_c", hourly = "px_surface",
                      week = "px_surface", wind = "px_secondary_c", humidity = "px_secondary_c",
                      uv = "px_secondary_c", air = "px_secondary_c", sky = "px_tertiary_c",
                      system = "px_surface"}
  local WHITE = {1, 1, 1}
  -- On Metro's coloured tiles everything is white, temperatures included.
  local METRO_TEMPERATURE = {{-50, 1, 1, 1}, {60, 1, 1, 1}}

  local palette_cache, palette_cache_version = {}, -1

  -- The palette a box draws with. `tint` is its background, which also serves
  -- for anything drawn to look cut out of it (the wind hub, the dots' rings).
  function palette_for(name)
    if STYLE == "glass" then return BASE end
    if palette_cache_version ~= palette_version then
      palette_cache, palette_cache_version = {}, palette_version
    end
    local cached = palette_cache[name]
    if cached then return cached end
    local p = setmetatable({}, {__index = BASE})
    if STYLE == "solid" then
      p.tint = BASE.solid
    elseif STYLE == "metro" then
      local fill = BASE["metro" .. (METRO_TILE[name] or 1)]
      p.tint, p.mode = fill, "dark"
      p.ink, p.cloud, p.accent = {0.99, 0.99, 1}, {0.99, 0.99, 1}, WHITE
      p.accent2 = mixc(WHITE, fill, 0.35)
      p.sun, p.rain = {1, 0.9, 0.7}, {0.76, 0.88, 1}
      p.temperature = METRO_TEMPERATURE
    elseif STYLE == "pixel" then
      p.tint = BASE[PIXEL_TILE[name] or "px_surface"]
      p.ink, p.accent = BASE.px_on, BASE.px_primary
    end
    palette_cache[name] = p
    return p
  end
end

local function source(cr, c, alpha)
  cairo_set_source_rgba(cr, c[1], c[2], c[3], alpha or 1)
end

-- Opacity tiers for ink. Hierarchy comes from these, not from more colours.
local STRONG, SOFT, MUTED, FAINT, HAIR = 0.96, 0.74, 0.54, 0.30, 0.12

local function ink(cr, alpha) source(cr, P.ink, alpha) end

local weather = nil            -- the helper's forecast, once it has arrived

-- Temperatures are coloured on a fixed scale in Celsius, so 20° looks the
-- same whatever the units -- the stops come harmonised from the helper.
local function temperature_color(value)
  if weather and weather.imperial then value = (value - 32) * 5 / 9 end
  local stops = P.temperature
  if value <= stops[1][1] then return {stops[1][2], stops[1][3], stops[1][4]} end
  for i = 2, #stops do
    local b = stops[i]
    if value <= b[1] then
      local a = stops[i - 1]
      local t = (value - a[1]) / (b[1] - a[1])
      return {lerp(a[2], b[2], t), lerp(a[3], b[3], t), lerp(a[4], b[4], t)}
    end
  end
  local last = stops[#stops]
  return {last[2], last[3], last[4]}
end

---------------- TYPE ----------------

local font_options = nil

-- Greyscale antialiasing: subpixel rendering on a transparent window leaves
-- colour fringes once the compositor blends it over the desktop. Metrics are
-- unhinted so text measures the same at every scale.
local function prepare(cr)
  if font_options == nil then
    font_options = cairo_font_options_create()
    cairo_font_options_set_antialias(font_options, CAIRO_ANTIALIAS_GRAY)
    cairo_font_options_set_hint_style(font_options, CAIRO_HINT_STYLE_SLIGHT)
    cairo_font_options_set_hint_metrics(font_options, CAIRO_HINT_METRICS_OFF)
  end
  cairo_set_font_options(cr, font_options)
  cairo_set_line_cap(cr, CAIRO_LINE_CAP_ROUND)
  cairo_set_line_join(cr, CAIRO_LINE_JOIN_ROUND)
end

local function face(cr, family, size)
  cairo_select_font_face(cr, family, CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_NORMAL)
  cairo_set_font_size(cr, size)
end

local extents = nil
local function advance(cr, text)
  extents = extents or cairo_text_extents_t:create()
  cairo_text_extents(cr, text, extents)
  return extents.x_advance
end

-- align: 0 left, 0.5 centre, 1 right. Returns the width drawn.
local function text(cr, s, x, y, align)
  local w = advance(cr, s)
  cairo_move_to(cr, x - w * (align or 0), y)
  cairo_show_text(cr, s)
  return w
end

-- Small capitals with letter-spacing: the one typographic device that marks
-- a caption as a caption without making it louder.
local function tracked(cr, s, x, y, spacing, align)
  local width, chars = 0, {}
  for ch in s:gmatch(utf8.charpattern) do
    local a = advance(cr, ch)
    chars[#chars + 1] = {ch, a}
    width = width + a + spacing
  end
  width = width - spacing
  x = x - width * (align or 0)
  for _, c in ipairs(chars) do
    cairo_move_to(cr, x, y)
    cairo_show_text(cr, c[1])
    x = x + c[2] + spacing
  end
  return width
end

-- string.upper only knows ASCII, and day names and places come in the
-- user's language. This also covers Latin-1 and Latin Extended-A, which is
-- every accented letter in most European languages.
local function upper(s)
  local out = {}
  for _, c in utf8.codes(s) do
    if (c >= 0x61 and c <= 0x7A) or (c >= 0xE0 and c <= 0xFE and c ~= 0xF7) then c = c - 0x20
    elseif c >= 0x100 and c <= 0x17F and c ~= 0x131 and c ~= 0x138 and c ~= 0x149 then
      -- Pairs are upper/lower in that order, but the run 0x139-0x148 and
      -- 0x179-0x17E is shifted by one.
      local odd_lower = (c >= 0x139 and c <= 0x148) or (c >= 0x179 and c <= 0x17E)
      if (c % 2 == 1) ~= odd_lower then c = c - 1 end
    end
    out[#out + 1] = utf8.char(c)
  end
  return table.concat(out)
end

local function initial(s)
  return upper(s:match(utf8.charpattern) or "")
end

local function tracked_width(cr, s, spacing)
  local width, count = 0, 0
  for ch in s:gmatch(utf8.charpattern) do
    width, count = width + advance(cr, ch), count + 1
  end
  return width + spacing * max(count - 1, 0)
end

-- The width a tile's contents have, for anything that must not run out of
-- it. Set before each tile draws.
local room = 300

local function caption(cr, s, x, y, align, alpha)
  local ok, shouted = pcall(upper, s)
  s = ok and shouted or s
  -- A large caption in a narrow tile shrinks rather than running out of it.
  local size = 10 * CAPTION
  face(cr, font_medium, size)
  local width = tracked_width(cr, s, 1.5 * CAPTION)
  local most = room - 2 * (tonumber(tile_padding) or 22)
  if width > most then
    size = size * most / width
    face(cr, font_medium, size)
  end
  ink(cr, alpha or MUTED)
  return tracked(cr, s, x, y, 1.5 * size / 10, align)
end

-- The largest size up to `size` at which `s` is no wider than `width`.
local function fitted_size(cr, family, s, size, width)
  face(cr, family, size)
  local w = advance(cr, s)
  if w > width and w > 0 then
    size = size * width / w
    face(cr, family, size)
  end
  return size
end

-- Fit a string into a width, trimming it a character at a time. Whole
-- characters: half of a two-byte "°" is invalid UTF-8, and cairo answers
-- invalid text by putting the context into an error state that silently
-- drops everything drawn after it.
local function fit(cr, s, width)
  if advance(cr, s) <= width then return s end
  while #s > 0 and advance(cr, s .. "…") > width do
    local last = utf8.offset(s, -1)
    if not last then break end
    s = s:sub(1, last - 1)
  end
  return s .. "…"
end

local SHOW_UNIT = yes(show_temperature_unit)

-- The number alone, with a real minus sign rather than a hyphen.
local function signed(value)
  local v = round(value)
  return v < 0 and ("−" .. -v) or tostring(v)
end

-- The unit a temperature is in: the forecast's, unless one is given -- the
-- machine's sensors always report Celsius, whatever the forecast uses.
local function unit_of(unit)
  return "°" .. (SHOW_UNIT and (unit or (weather and weather.imperial and "F" or "C")) or "")
end

local function degrees(value, unit)
  return signed(value) .. unit_of(unit)
end

local function clock_text(t, offset)
  if offset then
    if TWELVE_HOUR then
      local h = tonumber(os.date("!%I", t + offset))
      return h .. os.date("!:%M", t + offset)
    end
    return os.date("!%H:%M", t + offset)
  end
  if TWELVE_HOUR then return tostring(tonumber(os.date("%I", t))) .. os.date(":%M", t) end
  return os.date("%H:%M", t)
end

local function hour_label(t, offset)
  if TWELVE_HOUR then
    local h = tonumber(os.date("!%I", t + offset))
    return h .. os.date("!%p", t + offset):sub(1, 1):lower()
  end
  return os.date("!%H", t + offset)
end

---------------- SHAPES ----------------

-- The same polygon as superellipse_polygon in mirage.py, so a tinted pane
-- drawn here lines up with glass rendered there to the pixel.
local function squircle(cr, x, y, w, h, r)
  r = min(r * 1.35, w / 2, h / 2)
  local corners = {{x + w - r, y + r, 0}, {x + w - r, y + h - r, 90},
                   {x + r, y + h - r, 180}, {x + r, y + r, 270}}
  local e = 2 / 4.2
  cairo_new_path(cr)
  local first = true
  for _, c in ipairs(corners) do
    for i = 0, 14 do
      local a = math.rad(c[3] - 90 + 90 * i / 14)
      local ca, sa = cos(a), sin(a)
      local px = c[1] + r * (ca < 0 and -1 or 1) * abs(ca) ^ e
      local py = c[2] + r * (sa < 0 and -1 or 1) * abs(sa) ^ e
      if first then cairo_move_to(cr, px, py); first = false else cairo_line_to(cr, px, py) end
    end
  end
  cairo_close_path(cr)
end

-- A rounded bar along its longer side.
local function capsule(cr, x, y, w, h)
  cairo_new_path(cr)
  if w >= h then
    local r = h / 2
    cairo_arc(cr, x + w - r, y + r, r, -pi / 2, pi / 2)
    cairo_arc(cr, x + r, y + r, r, pi / 2, 3 * pi / 2)
  else
    local r = w / 2
    cairo_arc(cr, x + r, y + r, r, pi, TAU)
    cairo_arc(cr, x + r, y + h - r, r, 0, pi)
  end
  cairo_close_path(cr)
end

local function disc(cr, x, y, r)
  cairo_new_path(cr)
  cairo_arc(cr, x, y, r, 0, TAU)
end

local function glow(cr, x, y, r, c, alpha)
  local g = cairo_pattern_create_radial(x, y, 0, x, y, r)
  cairo_pattern_add_color_stop_rgba(g, 0, c[1], c[2], c[3], alpha)
  cairo_pattern_add_color_stop_rgba(g, 0.4, c[1], c[2], c[3], alpha * 0.35)
  cairo_pattern_add_color_stop_rgba(g, 1, c[1], c[2], c[3], 0)
  cairo_set_source(cr, g)
  disc(cr, x, y, r)
  cairo_fill(cr)
  cairo_pattern_destroy(g)
end

---------------- WEATHER ICONS ----------------
-- Drawn, not loaded: they take the palette like everything else, scale to any
-- size, and the big one moves. Every icon is laid out on a 24-unit grid.

local function with_cutout(cr, back, front_path, gap)
  -- Draw `back`, then knock a gap round where the front shape will go, so a
  -- cloud reads as in front of the sun rather than pasted onto it.
  cairo_push_group(cr)
  back()
  cairo_set_operator(cr, CAIRO_OPERATOR_CLEAR)
  front_path()
  cairo_set_line_width(cr, gap)
  cairo_stroke_preserve(cr)
  cairo_fill(cr)
  cairo_set_operator(cr, CAIRO_OPERATOR_OVER)
  cairo_pop_group_to_source(cr)
  cairo_paint(cr)
end

local function cloud_path(cr, cx, cy, size)
  local u = size / 24
  local function blob(x, y, r)
    cairo_new_sub_path(cr)
    cairo_arc(cr, cx + (x - 12) * u, cy + (y - 12) * u, r * u, 0, TAU)
  end
  cairo_new_path(cr)
  blob(7.6, 14.4, 4.4)
  blob(12.6, 10.8, 6.0)
  blob(17.6, 14.0, 4.8)
  cairo_new_sub_path(cr)
  cairo_rectangle(cr, cx + (7.6 - 12) * u, cy + (13.0 - 12) * u, 10.0 * u, 5.8 * u)
end

local function fill_cloud(cr, cx, cy, size, shade)
  local u = size / 24
  local top, bottom = mixc(P.cloud, P.tint, shade or 0), mixc(P.cloud, P.tint, (shade or 0) + 0.22)
  local g = cairo_pattern_create_linear(0, cy - 7 * u, 0, cy + 7 * u)
  cairo_pattern_add_color_stop_rgba(g, 0, top[1], top[2], top[3], 1)
  cairo_pattern_add_color_stop_rgba(g, 1, bottom[1], bottom[2], bottom[3], 1)
  cloud_path(cr, cx, cy, size)
  cairo_set_source(cr, g)
  cairo_fill(cr)
  cairo_pattern_destroy(g)
end

local function sun_icon(cr, cx, cy, size, t, halo)
  local u = size / 24
  if halo then glow(cr, cx, cy, 13 * u, P.sun, 0.30 + 0.05 * sin(t * 1.3)) end
  cairo_set_line_width(cr, 1.8 * u)
  source(cr, P.sun, 1)
  local turn = t * 0.10
  cairo_new_path(cr)
  for i = 0, 7 do
    local a = turn + i * TAU / 8
    cairo_move_to(cr, cx + cos(a) * 7.6 * u, cy + sin(a) * 7.6 * u)
    cairo_line_to(cr, cx + cos(a) * 10.0 * u, cy + sin(a) * 10.0 * u)
  end
  cairo_stroke(cr)
  local hot = mixc(P.sun, {1, 1, 1}, 0.45)
  local g = cairo_pattern_create_radial(cx - 1.5 * u, cy - 1.5 * u, 0, cx, cy, 5.2 * u)
  cairo_pattern_add_color_stop_rgb(g, 0, hot[1], hot[2], hot[3])
  cairo_pattern_add_color_stop_rgb(g, 1, P.sun[1], P.sun[2], P.sun[3])
  cairo_set_source(cr, g)
  disc(cr, cx, cy, 5.2 * u)
  cairo_fill(cr)
  cairo_pattern_destroy(g)
end

-- A crescent: the disc, minus a second disc clipped to it (even-odd inside a
-- clip needs no intermediate surface).
local function moon_icon(cr, cx, cy, size, t, halo)
  local u = size / 24
  local moon = mixc(P.cloud, P.sun, 0.18)
  if halo then glow(cr, cx, cy, 12 * u, moon, 0.20 + 0.04 * sin(t * 1.1)) end
  cairo_save(cr)
  disc(cr, cx, cy, 7.4 * u)
  cairo_clip(cr)
  cairo_new_path(cr)
  cairo_arc(cr, cx, cy, 7.4 * u, 0, TAU)
  cairo_new_sub_path(cr)
  cairo_arc(cr, cx + 4.4 * u, cy - 3.4 * u, 6.4 * u, 0, TAU)
  cairo_set_fill_rule(cr, CAIRO_FILL_RULE_EVEN_ODD)
  source(cr, moon, 1)
  cairo_fill(cr)
  cairo_restore(cr)
  if halo then
    source(cr, moon, 0.55 + 0.45 * (0.5 + 0.5 * sin(t * 2.1)))
    disc(cr, cx + 7.5 * u, cy - 7 * u, 0.8 * u); cairo_fill(cr)
    source(cr, moon, 0.55 + 0.45 * (0.5 + 0.5 * sin(t * 1.7 + 2)))
    disc(cr, cx + 10 * u, cy - 1.8 * u, 0.55 * u); cairo_fill(cr)
  end
end

local function celestial(cr, day, cx, cy, size, t, halo)
  if day then sun_icon(cr, cx, cy, size, t, halo) else moon_icon(cr, cx, cy, size, t, halo) end
end

-- Precipitation falls from under the cloud. Animated, each streak loops on
-- its own phase; static, they sit at staggered heights so they still read as
-- falling.
local function precipitation(cr, kind, level, cx, cy, size, t, live)
  local u = size / 24
  local count = (level or 2) >= 3 and 5 or ((level or 2) == 1 and 3 or 4)
  local spread = 11
  for i = 1, count do
    local x0 = cx + (-spread / 2 + spread * (i - 0.5) / count) * u + 0.6 * u
    local phase = live and ((t * (kind == "snow" and 0.45 or 1.15) + i * 0.37) % 1) or ((i * 0.37) % 1) * 0.6 + 0.2
    local fade = live and sin(phase * pi) or 1
    local snowy = kind == "snow" or (kind == "sleet" and i % 2 == 0)
    if snowy then
      local sway = live and sin(t * 1.4 + i) * 0.8 * u or 0
      source(cr, P.cloud, 0.95 * fade)
      disc(cr, x0 + sway - phase * 1.2 * u, cy + (7.2 + phase * 6) * u, 1.05 * u)
      cairo_fill(cr)
    else
      local len = kind == "drizzle" and 1.1 or 2.6
      local y = cy + (7.0 + phase * 6) * u
      local x = x0 - phase * 2.0 * u
      source(cr, P.rain, 0.95 * fade)
      cairo_set_line_width(cr, 1.5 * u)
      cairo_new_path(cr)
      cairo_move_to(cr, x, y)
      cairo_line_to(cr, x - 0.7 * u * len / 2.6, y + len * u)
      cairo_stroke(cr)
    end
  end
end

local function bolt(cr, cx, cy, size, t, live)
  local u = size / 24
  local flash = 1
  if live then
    local p = t % 4.2
    flash = (p < 0.12 or (p > 0.22 and p < 0.32)) and 1 or 0.55
    if flash == 1 then glow(cr, cx, cy + 6 * u, 10 * u, P.sun, 0.35) end
  end
  local pts = {{13.4, 14.6}, {9.8, 19.6}, {12.4, 19.6}, {10.8, 23.6}, {15.4, 17.6}, {12.8, 17.6}, {14.6, 14.6}}
  cairo_new_path(cr)
  for i, p in ipairs(pts) do
    local x, y = cx + (p[1] - 12) * u, cy + (p[2] - 12) * u
    if i == 1 then cairo_move_to(cr, x, y) else cairo_line_to(cr, x, y) end
  end
  cairo_close_path(cr)
  source(cr, P.sun, flash)
  cairo_fill(cr)
end

local function weather_icon(cr, kind, level, day, cx, cy, size, t, live)
  local u = size / 24
  t = live and t or 0
  local drift = live and sin(t * 0.5) * 0.7 * u or 0
  local gap = 1.6 * u
  day = day ~= false and day ~= 0

  if kind == "clear" then
    celestial(cr, day, cx, cy, size, t, size >= 40)

  elseif kind == "few" or kind == "partly" or kind == "showers" then
    local few = kind == "few"
    local bx, by = few and cx - 1.5 * u or cx - 3.6 * u, few and cy - 1.5 * u or cy - 3.8 * u
    local bs = few and size * 0.9 or size * 0.74
    local fx, fy, fs = few and cx + 4.2 * u or cx + 1.2 * u, few and cy + 4.4 * u or cy + 1.8 * u, few and size * 0.58 or size * 0.9
    if kind == "showers" then fy = fy - 2.2 * u end
    with_cutout(cr, function() celestial(cr, day, bx, by, bs, t, size >= 40) end,
                function() cloud_path(cr, fx + drift, fy, fs) end, gap)
    fill_cloud(cr, fx + drift, fy, fs)
    if kind == "showers" then precipitation(cr, "rain", level, fx + drift, fy, fs, t, live) end

  elseif kind == "cloudy" then
    with_cutout(cr, function() fill_cloud(cr, cx + 3.4 * u - drift * 0.6, cy - 3.0 * u, size * 0.72, 0.30) end,
                function() cloud_path(cr, cx - 1.0 * u + drift, cy + 1.6 * u, size * 0.94) end, gap)
    fill_cloud(cr, cx - 1.0 * u + drift, cy + 1.6 * u, size * 0.94)

  elseif kind == "fog" then
    fill_cloud(cr, cx + drift, cy - 2.6 * u, size * 0.86, 0.08)
    cairo_set_line_width(cr, 1.6 * u)
    for i, row in ipairs({{-7.5, 5.5, 6.2}, {-5.0, 7.5, 9.0}}) do
      local slide = live and sin(t * 0.7 + i * 1.7) * 1.2 * u or 0
      source(cr, P.cloud, 0.8)
      cairo_new_path(cr)
      cairo_move_to(cr, cx + row[1] * u + slide, cy + row[3] * u)
      cairo_line_to(cr, cx + row[2] * u + slide, cy + row[3] * u)
      cairo_stroke(cr)
    end

  else   -- drizzle, rain, sleet, snow, thunder
    local shade = kind == "thunder" and 0.28 or 0.06
    if kind == "thunder" then bolt(cr, cx + drift, cy - 2.4 * u, size * 0.95, t, live) end
    if kind ~= "thunder" or level >= 3 then
      precipitation(cr, kind == "thunder" and "rain" or kind, kind == "thunder" and 1 or level,
                    cx + drift, cy - 2.4 * u, size * 0.95, t, live)
    end
    fill_cloud(cr, cx + drift, cy - 2.4 * u, size * 0.95, shade)
  end
end

-- A location pin, drawn because the toy font API has no glyph fallback.
local function pin(cr, x, y, size)
  local u = size / 12
  cairo_new_path(cr)
  cairo_arc(cr, x, y - 2 * u, 3.6 * u, pi * 0.8, pi * 2.2)
  cairo_line_to(cr, x, y + 4.6 * u)
  cairo_close_path(cr)
  cairo_new_sub_path(cr)
  cairo_arc_negative(cr, x, y - 2 * u, 1.4 * u, TAU, 0)
  cairo_fill(cr)
end

-- A raindrop, to say what a bare percentage is: a point on top, round below.
local function droplet(cr, x, y, size)
  local r = size * 0.36
  cairo_new_path(cr)
  cairo_move_to(cr, x, y - size * 0.5)
  cairo_curve_to(cr, x + r * 0.45, y - size * 0.18, x + r, y + size * 0.02, x + r, y + size * 0.18)
  cairo_arc(cr, x, y + size * 0.18, r, 0, pi)
  cairo_curve_to(cr, x - r, y + size * 0.02, x - r * 0.45, y - size * 0.18, x, y - size * 0.5)
  cairo_close_path(cr)
  cairo_fill(cr)
end

local function chevron(cr, x, y, size, up)
  local d = up and -1 or 1
  cairo_new_path(cr)
  cairo_move_to(cr, x - size, y - d * size * 0.5)
  cairo_line_to(cr, x, y + d * size * 0.5)
  cairo_line_to(cr, x + size, y - d * size * 0.5)
  cairo_stroke(cr)
end

---------------- ASTRONOMY ----------------

local moon_phase
do
  local SYNODIC = 29.530588853
  local NEW_MOON = 947182440      -- 2000-01-06 18:14 UTC

  function moon_phase(t)
    local age = ((t - NEW_MOON) / 86400) % SYNODIC
    local phase = age / SYNODIC
    local lit = (1 - cos(phase * TAU)) / 2
    local name
    if age < 1.0 or age > SYNODIC - 1.0 then name = "New moon"
    elseif age < 6.38 then name = "Waxing crescent"
    elseif age < 8.38 then name = "First quarter"
    elseif age < 13.77 then name = "Waxing gibbous"
    elseif age < 15.77 then name = "Full moon"
    elseif age < 21.15 then name = "Waning gibbous"
    elseif age < 23.15 then name = "Last quarter"
    else name = "Waning crescent" end
    return phase, lit, name
  end
end

-- The moon as it looks tonight: a lit limb, closed by the terminator, which is
-- half an ellipse whose width runs from the limb (new) through a straight line
-- (quarter) to the far limb (full). South of the equator it is mirrored.
local function moon_disc(cr, cx, cy, r, phase, south)
  local moon = mixc(P.cloud, P.sun, 0.14)
  source(cr, P.ink, 0.10)
  disc(cr, cx, cy, r)
  cairo_fill(cr)
  local side = phase < 0.5 and 1 or -1
  if south then side = -side end
  local k = cos(phase * TAU)
  cairo_new_path(cr)
  if side == 1 then cairo_arc(cr, cx, cy, r, -pi / 2, pi / 2)
  else cairo_arc_negative(cr, cx, cy, r, -pi / 2, pi / 2) end
  for i = 0, 24 do
    local a = pi / 2 - pi * i / 24
    cairo_line_to(cr, cx + side * k * r * cos(a), cy + r * sin(a))
  end
  cairo_close_path(cr)
  source(cr, moon, 1)
  cairo_fill(cr)
end

---------------- SYSTEM SAMPLING ----------------
-- Read once a second straight from /proc and /sys -- no ${exec}, no
-- subprocess on the frame path -- and eased towards every frame.

local function first_matching(pattern)
  local pipe = io.popen("ls -d " .. pattern .. " 2>/dev/null")
  if not pipe then return nil end
  local out = pipe:read("*l")
  pipe:close()
  return out
end

local hw = nil
local function discover_hardware()
  hw = {cpu_temp = nil, gpu_busy = nil, gpu_temp = nil, vram_used = nil, vram_total = nil,
        battery = nil, nvidia = false}
  for chip = 0, 31 do
    local dir = "/sys/class/hwmon/hwmon" .. chip .. "/"
    local name = read_first_line(dir .. "name")
    if name == nil then break end
    if hw.cpu_temp == nil and (name == "k10temp" or name == "coretemp" or name == "zenpower"
        or name == "cpu_thermal" or name == "x86_pkg_temp") then
      local fallback = nil
      for i = 1, 32 do
        local label = read_first_line(dir .. "temp" .. i .. "_label")
        local input = dir .. "temp" .. i .. "_input"
        if label == "Tctl" or label == "Tdie" or label == "Package id 0" then hw.cpu_temp = input; break end
        if fallback == nil and read_first_line(input) then fallback = input end
      end
      hw.cpu_temp = hw.cpu_temp or fallback
    elseif hw.gpu_temp == nil and (name == "amdgpu" or name == "radeon" or name == "nouveau" or name == "i915" or name == "xe") then
      for i = 1, 8 do
        local label = read_first_line(dir .. "temp" .. i .. "_label")
        if (label == nil or label == "edge") and read_first_line(dir .. "temp" .. i .. "_input") then
          hw.gpu_temp = dir .. "temp" .. i .. "_input"
          break
        end
      end
    end
  end
  local busy = first_matching("/sys/class/drm/card*/device/gpu_busy_percent")
  if busy then
    hw.gpu_busy = busy
    local dev = busy:match("^(.*)/gpu_busy_percent$")
    hw.vram_used, hw.vram_total = dev .. "/mem_info_vram_used", dev .. "/mem_info_vram_total"
  end
  local want_gpu = tostring(show_gpu):lower()
  if hw.gpu_busy == nil and want_gpu ~= "no" and os.execute("command -v nvidia-smi >/dev/null 2>&1") then
    hw.nvidia = true
  end
  if want_gpu == "no" then hw.gpu_busy, hw.nvidia = nil, false end
  local bat = first_matching("/sys/class/power_supply/BAT*")
  if bat then hw.battery = bat end
end

local target = {cpu = 0, mem = 0, gpu = 0, bat = 0}
local shown = {cpu = 0, mem = 0, gpu = 0, bat = 0}
local info = {cpu_temp = nil, gpu_temp = nil, mem_used = 0, mem_total = 0, vram = nil,
              disks = {}, charging = false, uptime = 0}

-- The `disks` setting resolved to mount points. A device is looked up in
-- /proc/mounts, through any symlink (/dev/disk/by-label/...) first; conky's
-- ${fs_*} variables only take a mount point.
local function mounted_disks()
  local mounts = {}
  for line in (read_all("/proc/mounts") or ""):gmatch("[^\n]+") do
    local device, point = line:match("^(%S+)%s+(%S+)")
    if device then
      point = point:gsub("\\040", " ")
      mounts[#mounts + 1] = {device, point}
    end
  end
  local found = {}
  for _, wanted in ipairs(type(disks) == "table" and disks or {disks or "/"}) do
    wanted = tostring(wanted)
    local point = nil
    if wanted:match("^/dev/") then
      local pipe = io.popen("readlink -f '" .. wanted:gsub("'", "") .. "' 2>/dev/null")
      local real = pipe and pipe:read("*l") or wanted
      if pipe then pipe:close() end
      for _, m in ipairs(mounts) do
        if m[1] == wanted or m[1] == real then point = m[2]; break end
      end
    else
      point = wanted
    end
    if point then
      local name = point == "/" and "Root" or (point:match("([^/]+)/*$") or point)
      found[#found + 1] = {point = point, label = name:sub(1, 1):upper() .. name:sub(2), free = nil}
    end
  end
  return found
end
local next_nvidia = 0
local prev_cpu = nil
local sampled_at = -1e9
local next_disk = 0

---------------- NETWORK ----------------
-- Ported from conky-dashboard-pixel (Retrograde/extras), whose version carries
-- the fixes; keep the two in step. What it gets right:
--   * The busiest interface other than loopback, not the default route. With
--     a VPN, or no default route at all, the route named an interface that
--     carried nothing and the graph lay flat. network_interface = "auto"
--     takes the busiest; a name takes that interface only.
--   * Counting starts again when the interface changes, so a switch is not
--     drawn as a burst; a counter that resets (interface down and up) reads
--     as zero, not as a negative rate.
--   * Spans come from /proc/uptime, which only goes forward. Wall time steps
--     after a suspend or a clock sync, and a step showed up as a spike.
--   * Each point is the average over its whole second, from the counters as
--     of the previous point.

local NET_SAMPLES = 96              -- a point a second
local NET_FLOOR = 32 * 1024         -- the least a full-height graph means, so idle stays flat
local net = {name = nil, down = {}, up = {}, mark = nil}

local sample_network
do
  local function monotonic()
    return tonumber((read_first_line("/proc/uptime") or ""):match("^([%d%.]+)")) or os.time()
  end

  local function read_network()
    local txt = read_all("/proc/net/dev")
    if not txt then return nil end
    local wanted = tostring(network_interface)
    local best_name, best_rx, best_tx = nil, 0, 0
    for line in txt:gmatch("[^\n]+") do
      local iface, rest = line:match("^%s*([%w%-%.@_]+):%s*(.+)$")
      if iface and iface ~= "lo" then
        local f = {}
        for n in rest:gmatch("%d+") do f[#f + 1] = tonumber(n) end
        -- Field 1 is bytes received, field 9 bytes sent.
        if #f >= 9 then
          if wanted == "auto" then
            if f[1] + f[9] > best_rx + best_tx then best_name, best_rx, best_tx = iface, f[1], f[9] end
          elseif iface == wanted then
            best_name, best_rx, best_tx = iface, f[1], f[9]
          end
        end
      end
    end
    return best_name, best_rx, best_tx
  end

  function sample_network()
    local name, rx, tx = read_network()
    if not name then return end
    local now = monotonic()
    local mark = net.mark
    net.name = name
    if not mark or mark.name ~= name then
      net.mark = {name = name, rx = rx, tx = tx, t = now}
      return
    end
    local span = now - mark.t
    if span < 0.05 then return end    -- too short to measure; keep the old mark
    net.mark = {name = name, rx = rx, tx = tx, t = now}
    for key, value in pairs({down = max(0, (rx - mark.rx) / span), up = max(0, (tx - mark.tx) / span)}) do
      local list = net[key]
      list[#list + 1] = value
      if #list > NET_SAMPLES then table.remove(list, 1) end
    end
  end
end

-- Bytes per second in the largest unit that keeps the number small, to three
-- figures: past 100 MiB/s the tenth is noise.
local function format_rate(bps)
  if bps >= 1048576 then
    local mib = bps / 1048576
    return string.format(mib >= 100 and "%.0f MiB/s" or "%.1f MiB/s", mib)
  end
  if bps >= 1024 then return string.format("%.0f KiB/s", bps / 1024) end
  return string.format("%.0f B/s", bps)
end

-- The busiest second a series holds. The graph's scale never goes below
-- NET_FLOOR, but the label says what actually happened: the dashboard printed
-- the floor, so an idle connection read "peak 32 KiB/s".
local function peak_of(series)
  local peak = 0
  for _, value in ipairs(series) do
    if value > peak then peak = value end
  end
  return peak
end

local function sample(now)
  if now - sampled_at < 1 then return end
  sampled_at = now
  if hw == nil then discover_hardware() end

  local stat = read_first_line("/proc/stat") or ""
  local fields = {}
  for n in stat:gmatch("%d+") do fields[#fields + 1] = tonumber(n) end
  if #fields >= 5 then
    local total, idle = 0, fields[4] + (fields[5] or 0)
    for i = 1, math.min(#fields, 8) do total = total + fields[i] end
    if prev_cpu then
      local dt, di = total - prev_cpu[1], idle - prev_cpu[2]
      if dt > 0 then target.cpu = clamp(100 * (dt - di) / dt, 0, 100) end
    end
    prev_cpu = {total, idle}
  end

  local mem = read_all("/proc/meminfo") or ""
  local total = tonumber(mem:match("MemTotal:%s+(%d+)")) or 1
  local avail = tonumber(mem:match("MemAvailable:%s+(%d+)")) or 0
  target.mem = 100 * (total - avail) / total
  info.mem_used, info.mem_total = (total - avail) / 1048576, total / 1048576

  if hw.cpu_temp then info.cpu_temp = number_or(read_first_line(hw.cpu_temp), 0) / 1000 end
  if hw.gpu_temp then info.gpu_temp = number_or(read_first_line(hw.gpu_temp), 0) / 1000 end
  if hw.gpu_busy then
    target.gpu = number_or(read_first_line(hw.gpu_busy), 0)
    local used, size = tonumber(read_first_line(hw.vram_used) or ""), tonumber(read_first_line(hw.vram_total) or "")
    if used and size and size > 0 then info.vram = {used / 1073741824, size / 1073741824} end
  elseif hw.nvidia then
    -- nvidia-smi takes tens of milliseconds, so it runs detached and leaves
    -- its answer for the next sample rather than stalling a frame.
    local line = read_first_line(CACHE .. "/nvidia")
    local busy, temp, used, size = (line or ""):match("(%d+),%s*(%d+),%s*(%d+),%s*(%d+)")
    if busy then
      target.gpu, info.gpu_temp = tonumber(busy), tonumber(temp)
      info.vram = {tonumber(used) / 1024, tonumber(size) / 1024}
    end
    if now >= next_nvidia then
      next_nvidia = now + 3
      os.execute("nvidia-smi --query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total " ..
                 "--format=csv,noheader,nounits > '" .. CACHE .. "/nvidia' 2>/dev/null &")
    end
  end
  if hw.battery then
    target.bat = number_or(read_first_line(hw.battery .. "/capacity"), 0)
    info.charging = (read_first_line(hw.battery .. "/status") or "") == "Charging"
  end

  if now >= next_disk then
    -- Mounts change rarely; look again once a minute, the usage every ten
    -- seconds.
    if next_disk == 0 or now - (info.disks_found or 0) > 60 then
      local fresh = mounted_disks()
      for i, disk in ipairs(fresh) do
        local old = info.disks[i]
        if old and old.point == disk.point then disk.free = old.free end
      end
      info.disks, info.disks_found = fresh, now
    end
    next_disk = now + 10
    for i, disk in ipairs(info.disks) do
      local used = tonumber(conky_parse("${fs_used_perc " .. disk.point .. "}"))
      if used then target["disk" .. i] = used end
      local free = conky_parse("${fs_free " .. disk.point .. "}")
      disk.free = free ~= "" and free or nil
    end
  end

  info.uptime = tonumber((read_first_line("/proc/uptime") or ""):match("^(%S+)")) or 0

  sample_network()
end

-- Exponential approach, framerate independent.
local function ease(current, goal, dt, tau)
  return current + (goal - current) * (1 - math.exp(-dt / tau))
end

local function ease_all(dt)
  for key, value in pairs(target) do
    -- A still preview is two frames; it shows the readings, not the way there.
    shown[key] = mirage_preview_still and value or ease(shown[key] or 0, value, dt, 0.5)
  end
end

---------------- LAYOUT ----------------
-- Designed at 1440p; everything is drawn in these units and scaled to the
-- window conky actually made. Rows are listed top to bottom; a number is a
-- tile's width, and a tile without one takes what is left of its row.

-- Heights are in content units, which text_size zooms: every tile's contents
-- are drawn text_size larger and the tile grows to match, so a larger text
-- size never runs anything into anything else. Widths are not zoomed; text
-- that would run out of a narrower tile shrinks or trims instead. The widths
-- below are for the design width of 760 and stretch with widget_width.
local MARGIN = 28
local GAP = clamp(number_or(tile_gap, 14), 0, 60)
local WIDTH = clamp(number_or(widget_width, 760), 480, 1600)
local RADIUS = clamp(number_or(corner_radius, 24), 0, 60)

-- How much taller the big numerals make their tiles: the growth of their
-- cap height, which is about 0.72 of the size in Noto Sans and its kin.
local CAP = 0.72
local CLOCK_GROWTH = 74 * (CLOCK - 1) * CAP
local TEMPERATURE_GROWTH = 78 * (TEMPERATURE - 1) * CAP

local function hero_growth()
  local grow = -math.huge
  if yes(show_clock) then grow = max(grow, CLOCK_GROWTH) end
  if yes(show_now) then grow = max(grow, TEMPERATURE_GROWTH) end
  return max(grow, -40)
end

local ROWS = {
  {height = 200, grows = hero_growth, {"clock", 316, show = show_clock}, {"now", show = show_now}},
  {height = 150, {"hourly", show = show_hourly}},
  {height = 296, {"week", 430, show = show_week},
                 {grid = {"wind", "humidity", "uv", "air"}, show = show_details}},
  {height = 180, {"sky", 316, show = show_sky}, {"system", show = show_system}},
}

local layout = nil

local function compute_layout(win_w, win_h)
  local rows = {}
  for _, row in ipairs(ROWS) do
    local items = {}
    for _, item in ipairs(row) do
      if yes(item.show) then items[#items + 1] = item end
    end
    if #items > 0 then
      local height = (row.height + (row.grows and row.grows() or 0)) * TEXT
      rows[#rows + 1] = {height = height, items = items}
    end
  end

  local base_h = 2 * MARGIN + GAP * math.max(#rows - 1, 0)
  for _, row in ipairs(rows) do base_h = base_h + row.height end
  local base_w = WIDTH + 2 * MARGIN
  local s = min(win_w / base_w, win_h / base_h)
  local ox = (win_w - base_w * s) / 2

  local tiles = {}
  -- cw, ch and cs are the tile in content units and the scale to draw them
  -- at: the layout's scale times text_size.
  local function add(name, bx, by, bw, bh)
    local x0, y0 = round(ox + bx * s), round(by * s)
    local x1, y1 = round(ox + (bx + bw) * s), round((by + bh) * s)
    tiles[#tiles + 1] = {name = name, x = x0, y = y0, w = x1 - x0, h = y1 - y0,
                         s = s, cs = s * TEXT, cw = (x1 - x0) / (s * TEXT), ch = (y1 - y0) / (s * TEXT)}
    tiles[#tiles].key = string.format("%d,%d,%d,%d", x0, y0, x1 - x0, y1 - y0)
  end

  local y = MARGIN
  for _, row in ipairs(rows) do
    local fixed, flex = 0, 0
    local stretch = WIDTH / 760
    for _, item in ipairs(row.items) do
      if item[2] and #row.items > 1 then fixed = fixed + item[2] * stretch else flex = flex + 1 end
    end
    if flex == 0 then flex, row.items[#row.items].stretch = 1, true end
    local spare = WIDTH - fixed - GAP * (#row.items - 1)
    local x = MARGIN
    for _, item in ipairs(row.items) do
      local w = (item[2] and #row.items > 1 and not item.stretch) and item[2] * stretch or spare / flex
      if item.stretch then w = w + item[2] * stretch end
      if item.grid then
        local cw, ch = (w - GAP) / 2, (row.height - GAP) / 2
        for i, name in ipairs(item.grid) do
          local col, line = (i - 1) % 2, floor((i - 1) / 2)
          add(name, x + col * (cw + GAP), y + line * (ch + GAP), cw, ch)
        end
      else
        add(item[1], x, y, w, row.height)
      end
      x = x + w + GAP
    end
    y = y + row.height + GAP
  end
  return {W = win_w, H = win_h, s = s, tiles = tiles, base_w = base_w, base_h = base_h}
end

-- The widget's size at scale 1 with these settings, which the window has to
-- match. Global, for mirage_preview.lua and mirage_settings.py.
function mirage_natural_size()
  local probe = compute_layout(1000, 1000)
  return round(probe.base_w), round(probe.base_h)
end

---------------- TILES ----------------
-- Each tile draws in its own units, (0,0) at its top-left corner. `static`
-- is drawn into a cached surface and only redrawn when its key changes;
-- `live` is drawn every frame.

local TILES = {}
local PAD = clamp(number_or(tile_padding, 22), 10, 40)
local data_version = 0
local weather_error = nil

local function place_offset() return weather and weather.tz_offset or 0 end

local function no_weather(cr, w, h, title)
  caption(cr, title, PAD, 30)
  face(cr, font_regular, 13)
  ink(cr, MUTED)
  local message = weather_error and ("Weather unavailable: " .. weather_error.error) or "Fetching the forecast…"
  text(cr, fit(cr, message, w - 2 * PAD), PAD, h / 2 + 6)
end

-- clock ---------------------------------------------------------------------

TILES.clock = {
  key = function() return os.date("%Y%m%d%H%M") end,
  static = function(cr, w, h)
    local now = os.time()
    caption(cr, os.date("%A", now), PAD, 30, 0, SOFT)

    -- The numerals hang from a fixed top, so a larger clock pushes the date
    -- down rather than up into the caption; and one too wide for the tile
    -- shrinks to fit it.
    local hm = clock_text(now)
    local size = fitted_size(cr, DISPLAY_FONT, hm, 74 * CLOCK, w - 2 * PAD + 6 - (TWELVE_HOUR and 34 or 0))
    local baseline = 104 - 74 * CAP + size * CAP
    ink(cr, STRONG)
    local tw = text(cr, hm, PAD - 3, baseline)
    if TWELVE_HOUR then
      face(cr, font_medium, 13)
      ink(cr, MUTED)
      text(cr, os.date("%p", now), PAD - 3 + tw + 6, baseline)
    end

    face(cr, font_regular, 15)
    ink(cr, SOFT)
    local date = tostring(tonumber(os.date("%d", now))) .. os.date(" %B", now)
    local dw = text(cr, date, PAD, baseline + 26)
    face(cr, font_regular, 12)
    ink(cr, FAINT)
    text(cr, fit(cr, "  ·  week " .. tonumber(os.date("%V", now)), w - 2 * PAD - dw), PAD + dw, baseline + 26)

    -- The week at a glance, today picked out in the accent.
    local today = os.date("*t", now)
    local wday = (today.wday - (SUNDAY_FIRST and 1 or 2)) % 7      -- 0 = first column
    local x0, x1 = PAD + 2, w - PAD - 2
    local step = (x1 - x0) / 6
    for i = 0, 6 do
      local t = now + (i - wday) * 86400
      local x = x0 + i * step
      local is_today = i == wday
      face(cr, font_medium, 9.5)
      ink(cr, is_today and SOFT or FAINT)
      text(cr, initial(os.date("%a", t)), x, h - 40, 0.5)
      if is_today then
        source(cr, P.accent, 1)
        disc(cr, x, h - 23, 11.5)
        cairo_fill(cr)
        source(cr, P.tint, 1)
      else
        ink(cr, SOFT)
      end
      face(cr, is_today and font_medium or font_regular, 12.5)
      text(cr, tostring(tonumber(os.date("%d", t))), x, h - 18.5, 0.5)
    end
  end,
}

-- now -----------------------------------------------------------------------

-- The icon takes about a third of the tile's width, up to its design size,
-- and the text starts after it.
local function now_geometry(w, h)
  local size = clamp((w - 2 * PAD) * 0.31, 64, 118)
  return PAD + size * 0.6, h / 2 + 4, size, PAD + size * 1.4
end

TILES.now = {
  key = function() return (weather and tostring(weather.fetched) or "none") .. os.date("%H%M") end,
  static = function(cr, w, h)
    if not weather then return no_weather(cr, w, h, "Weather") end
    local c = weather.current
    local ix, iy, isize, x = now_geometry(w, h)
    -- A soft light behind the icon, in the colour of the weather.
    local wet = c.kind ~= "clear" and c.kind ~= "few" and c.kind ~= "partly"
    glow(cr, ix, iy - 2, isize * 0.81, wet and P.rain or (c.is_day == 1 and P.sun or P.accent2), 0.20)
    ink(cr, MUTED)
    pin(cr, PAD + 3 * CAPTION, 30 - 4 * CAPTION, 11 * CAPTION)
    local name = weather.place ~= "" and weather.place or string.format("%.2f, %.2f", weather.lat, weather.lon)
    caption(cr, name, PAD + 13 * CAPTION, 30)
    if os.time() - weather.fetched > 3 * 60 * weather_refresh_minutes then
      face(cr, font_regular, 10.5)
      ink(cr, FAINT)
      text(cr, "updated " .. clock_text(weather.fetched), w - PAD, 30, 1)
    end

    -- The block of temperature and description is centred in whatever height
    -- the row has beyond what the temperature itself needs.
    -- With the unit shown, the digits stay large and "°C" sits small against
    -- their top, the way a thermometer's scale would print it.
    local number = SHOW_UNIT and signed(c.temp) or degrees(c.temp)
    local unit = SHOW_UNIT and unit_of() or nil
    local small = 0
    if unit then
      face(cr, DISPLAY_FONT, 78 * TEMPERATURE * 0.36)
      small = advance(cr, unit) + 4
    end
    local size = fitted_size(cr, DISPLAY_FONT, number, 78 * TEMPERATURE, w - x - PAD + 4 - small)
    local top = 112 - 78 * CAP + (h - 200 - TEMPERATURE_GROWTH) / 2
    local baseline = top + size * CAP
    ink(cr, STRONG)
    local nw = text(cr, number, x - 4, baseline)
    if unit then
      local unit_size = size * 0.36
      face(cr, DISPLAY_FONT, unit_size)
      ink(cr, SOFT)
      text(cr, unit, x - 4 + nw + 3, top + unit_size * CAP + 2)
    end

    face(cr, font_regular, 17)
    ink(cr, STRONG)
    text(cr, fit(cr, c.desc, w - x - PAD), x, baseline + 30)

    local today = weather.daily[1]
    face(cr, font_regular, 13)
    ink(cr, MUTED)
    local line = "Feels " .. degrees(c.feels)
    if today then line = line .. "  ·  H " .. degrees(today.hi) .. "  L " .. degrees(today.lo) end
    text(cr, fit(cr, line, w - x - PAD), x, baseline + 54)
  end,
  live = function(cr, w, h, now)
    if not weather then return end
    local c = weather.current
    local ix, iy, isize = now_geometry(w, h)
    weather_icon(cr, c.kind, c.level, c.is_day == 1, ix, iy, isize, now, ANIMATE)
  end,
}

-- hourly --------------------------------------------------------------------

-- Monotone cubic through the points (Fritsch-Carlson): smooth, but never
-- overshooting into a high or low that is not in the forecast.
local function monotone_path(cr, xs, ys, start_new)
  local n = #xs
  if n < 2 then return end
  local d, m = {}, {}
  for i = 1, n - 1 do d[i] = (ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]) end
  m[1], m[n] = d[1], d[n - 1]
  for i = 2, n - 1 do
    if d[i - 1] * d[i] <= 0 then m[i] = 0 else m[i] = (d[i - 1] + d[i]) / 2 end
  end
  for i = 1, n - 1 do
    if d[i] == 0 then m[i], m[i + 1] = 0, 0
    else
      local a, b = m[i] / d[i], m[i + 1] / d[i]
      local s = a * a + b * b
      if s > 9 then
        local t = 3 / sqrt(s)
        m[i], m[i + 1] = t * a * d[i], t * b * d[i]
      end
    end
  end
  if start_new then cairo_move_to(cr, xs[1], ys[1]) else cairo_line_to(cr, xs[1], ys[1]) end
  for i = 1, n - 1 do
    local h = (xs[i + 1] - xs[i]) / 3
    cairo_curve_to(cr, xs[i] + h, ys[i] + m[i] * h, xs[i + 1] - h, ys[i + 1] - m[i + 1] * h, xs[i + 1], ys[i + 1])
  end
end


TILES.hourly = {
  key = function() return (weather and tostring(weather.fetched) or "none") .. os.date("%H%M") end,
  static = function(cr, w, h)
    if not weather then return no_weather(cr, w, h, "Next 24 hours") end
    local now, off = os.time(), place_offset()
    local cw = caption(cr, "Next 24 hours", PAD, 30)
    if weather.summary and weather.summary ~= "" then
      face(cr, font_regular, 12.5)
      ink(cr, SOFT)
      text(cr, fit(cr, weather.summary, w - 2 * PAD - cw - 24), w - PAD, 30, 1)
    end

    local x0, x1 = PAD + 14, w - PAD - 14
    local t0, t1 = now, now + 24 * 3600
    local function X(t) return x0 + (x1 - x0) * (t - t0) / (t1 - t0) end
    local pts = {{t = now, temp = weather.current.temp}}
    local hours = {}
    for _, hr in ipairs(weather.hourly) do
      if hr.t > now and hr.t <= t1 + 3600 then pts[#pts + 1] = hr end
      if hr.t + 3600 > now and hr.t < t1 then hours[#hours + 1] = hr end
    end
    if #pts < 3 then return end

    local lo, hi = math.huge, -math.huge
    for _, p in ipairs(pts) do lo, hi = min(lo, p.temp), max(hi, p.temp) end
    local span = max(hi - lo, 4)
    local mid = (hi + lo) / 2
    lo, hi = mid - span / 2, mid + span / 2
    local top, bottom = 84, 113
    local function Y(v) return bottom - (bottom - top) * (v - lo) / (hi - lo) end

    -- Chance of rain, as low bars rising from the floor of the chart.
    cairo_save(cr)
    cairo_rectangle(cr, x0, 44, x1 - x0, h - 44)
    cairo_clip(cr)
    for _, hr in ipairs(hours) do
      if hr.pop >= 10 then
        local bh = 2 + 18 * hr.pop / 100
        source(cr, P.rain, 0.25 + 0.5 * hr.pop / 100)
        capsule(cr, X(hr.t + 1800) - 1.5, 122 - bh, 3, bh)
        cairo_fill(cr)
      end
    end
    cairo_restore(cr)

    local xs, ys = {}, {}
    for _, p in ipairs(pts) do xs[#xs + 1] = X(p.t); ys[#ys + 1] = Y(p.temp) end

    local grad = cairo_pattern_create_linear(x0, 0, x1, 0)
    for _, p in ipairs(pts) do
      local c = temperature_color(p.temp)
      local at = clamp((X(p.t) - x0) / (x1 - x0), 0, 1)
      cairo_pattern_add_color_stop_rgb(grad, at, c[1], c[2], c[3])
    end

    cairo_save(cr)
    cairo_rectangle(cr, x0 - 4, 40, x1 - x0 + 8, h)
    cairo_clip(cr)
    -- Area under the curve: the temperature gradient, fading downwards.
    cairo_push_group(cr)
    cairo_new_path(cr)
    monotone_path(cr, xs, ys, true)
    cairo_line_to(cr, xs[#xs], 122)
    cairo_line_to(cr, xs[1], 122)
    cairo_close_path(cr)
    cairo_set_source(cr, grad)
    cairo_fill(cr)
    cairo_pop_group_to_source(cr)
    local fade = cairo_pattern_create_linear(0, top - 6, 0, 122)
    cairo_pattern_add_color_stop_rgba(fade, 0, 0, 0, 0, 0.30)
    cairo_pattern_add_color_stop_rgba(fade, 1, 0, 0, 0, 0.0)
    cairo_mask(cr, fade)
    cairo_pattern_destroy(fade)

    cairo_new_path(cr)
    monotone_path(cr, xs, ys, true)
    cairo_set_source(cr, grad)
    cairo_set_line_width(cr, 2.4)
    cairo_stroke(cr)
    cairo_restore(cr)
    cairo_pattern_destroy(grad)

    -- Every third hour: icon above, temperature on the curve, hour below.
    -- A mark is drawn only where its labels clear the ones before it, the
    -- "now" labels included. They are measured, not guessed at: with the
    -- unit shown "10°C" is twice the width of "10°", and a mark an hour after
    -- now printed on top of the current temperature.
    face(cr, font_medium, 12)
    local temp_right = x0 + advance(cr, degrees(weather.current.temp)) / 2
    face(cr, font_medium, 11)
    local hour_right = x0 + advance(cr, "Now") / 2
    local icon_x = x0 - 100
    for _, hr in ipairs(weather.hourly) do
      local local_hour = tonumber(os.date("!%H", hr.t + off))
      if hr.t > now and hr.t < t1 - 1200 and local_hour % 3 == 0 then
        local x = X(hr.t)
        local temperature, hour = degrees(hr.temp), hour_label(hr.t, off)
        face(cr, font_regular, 12)
        local tw = advance(cr, temperature)
        face(cr, font_regular, 11)
        local hw = advance(cr, hour)
        if x - tw / 2 >= temp_right + 6 and x - hw / 2 >= hour_right + 6 and x - icon_x >= 24 then
          weather_icon(cr, hr.kind, hr.level, hr.day == 1, x, 53, 18, 0, false)
          face(cr, font_regular, 12)
          ink(cr, SOFT)
          text(cr, temperature, x, Y(hr.temp) - 8, 0.5)
          face(cr, font_regular, 11)
          ink(cr, MUTED)
          text(cr, hour, x, h - 15, 0.5)
          ink(cr, HAIR)
          disc(cr, x, Y(hr.temp), 1.6)
          cairo_fill(cr)
          temp_right, hour_right, icon_x = x + tw / 2, x + hw / 2, x
        end
      end
    end

    face(cr, font_medium, 11)
    ink(cr, SOFT)
    text(cr, "Now", x0, h - 15, 0.5)
    face(cr, font_medium, 12)
    ink(cr, STRONG)
    text(cr, degrees(weather.current.temp), x0, ys[1] - 9, 0.5)
  end,
}

-- week ----------------------------------------------------------------------

-- The week as a chart: a column per day, and two smooth lines through the
-- columns, one joining every day's high and one every day's low, with the
-- numbers above and below their dots. Bars were tried twice and misread both
-- times: drawn on the week's scale between the day's numbers, a bar's ends
-- were taken to be those numbers; with the numbers riding the bar ends
-- instead, nothing lined up. Two lines against one shared scale need no
-- explaining, and every number sits in its day's column.
TILES.week = {
  key = function() return (weather and tostring(weather.fetched) or "none") .. os.date("%Y%m%d%H") end,
  static = function(cr, w, h)
    if not weather then return no_weather(cr, w, h, "7-day forecast") end
    local cw = caption(cr, "7-day forecast", PAD, 30)
    local off = place_offset()
    local today_key = os.date("!%Y%m%d", os.time() + off)
    local days = {}
    for _, d in ipairs(weather.daily) do
      if os.date("!%Y%m%d", d.t + off) >= today_key and #days < 7 then days[#days + 1] = d end
    end
    if #days == 0 then return end

    local warm, cool = P.sun, P.rain

    -- The key: which line is which.
    face(cr, font_regular, 11)
    local key_low, key_high = "Low", "High"
    local kw = advance(cr, key_low) + advance(cr, key_high) + 44
    if kw <= w - 2 * PAD - cw - 10 then
      local x = w - PAD
      ink(cr, MUTED)
      x = x - advance(cr, key_low)
      text(cr, key_low, x, 30)
      source(cr, cool, 1)
      disc(cr, x - 9, 26, 3.4)
      cairo_fill(cr)
      x = x - 22 - advance(cr, key_high)
      ink(cr, MUTED)
      text(cr, key_high, x, 30)
      source(cr, warm, 1)
      disc(cr, x - 9, 26, 3.4)
      cairo_fill(cr)
    end

    local n = #days
    local col = (w - 2 * PAD) / n
    local function X(i) return PAD + col * (i - 0.5) end

    -- Today's column, lifted a little from the rest.
    local today = os.date("!%Y%m%d", days[1].t + off) == today_key
    if today then
      ink(cr, 0.06)
      squircle(cr, X(1) - col / 2 + 2, 38, col - 4, h - 44, 12)
      cairo_fill(cr)
    end

    local name_y, icon_y = 56, 79
    local top, bottom = 114, h - 66
    local lo, hi = math.huge, -math.huge
    for _, d in ipairs(days) do lo, hi = min(lo, d.lo), max(hi, d.hi) end
    local span = max(hi - lo, 1)
    local function Y(v) return top + (bottom - top) * (hi - v) / span end

    -- Day names share one size, the largest at which the longest still fits
    -- its column, so a narrow tile shrinks them all rather than clipping one.
    local names, name_size = {}, 13
    for i, d in ipairs(days) do
      names[i] = (i == 1 and today) and "Today" or os.date("!%a", d.t + off)
      face(cr, (i == 1 and today) and font_medium or font_regular, 13)
      local nw = advance(cr, names[i])
      if nw > col - 4 then name_size = min(name_size, 13 * (col - 4) / nw) end
    end

    local xs, his, los = {}, {}, {}
    for i, d in ipairs(days) do
      xs[i], his[i], los[i] = X(i), Y(d.hi), Y(d.lo)
      local is_today = i == 1 and today
      face(cr, is_today and font_medium or font_regular, name_size)
      ink(cr, is_today and STRONG or SOFT)
      text(cr, names[i], X(i), name_y, 0.5)
      weather_icon(cr, d.kind, d.level, true, X(i), icon_y, 24, 0, false)
      -- The chance of rain, for every day: a gap for a dry one looked like
      -- something missing. Below 20% it is drawn fainter.
      do
        face(cr, font_medium, 10.5)
        source(cr, P.rain, d.pop >= 20 and 1 or 0.5)
        local label = round(d.pop) .. "%"
        local lw = advance(cr, label)
        local x0 = X(i) - (lw + 9) / 2
        droplet(cr, x0 + 3, h - 19.5, 8)
        text(cr, label, x0 + 9, h - 16)
      end
    end

    -- The band between the lines, warm at the top fading to cool.
    if n >= 2 then
      cairo_new_path(cr)
      monotone_path(cr, xs, his, true)
      local rx, rl = {}, {}
      for i = n, 1, -1 do rx[#rx + 1], rl[#rl + 1] = xs[i], los[i] end
      monotone_path(cr, rx, rl, false)
      cairo_close_path(cr)
      local g = cairo_pattern_create_linear(0, top, 0, bottom)
      cairo_pattern_add_color_stop_rgba(g, 0, warm[1], warm[2], warm[3], 0.16)
      cairo_pattern_add_color_stop_rgba(g, 1, cool[1], cool[2], cool[3], 0.10)
      cairo_set_source(cr, g)
      cairo_fill(cr)
      cairo_pattern_destroy(g)

      cairo_set_line_width(cr, 2.2)
      cairo_new_path(cr)
      monotone_path(cr, xs, his, true)
      source(cr, warm, 0.95)
      cairo_stroke(cr)
      cairo_new_path(cr)
      monotone_path(cr, xs, los, true)
      source(cr, cool, 0.95)
      cairo_stroke(cr)
    end

    for i, d in ipairs(days) do
      for _, point in ipairs({{his[i], warm}, {los[i], cool}}) do
        source(cr, P.tint, 0.95)
        disc(cr, xs[i], point[1], 4.6)
        cairo_fill(cr)
        source(cr, point[2], 1)
        disc(cr, xs[i], point[1], 3.1)
        cairo_fill(cr)
      end
      face(cr, font_regular, 12.5)
      ink(cr, STRONG)
      text(cr, degrees(d.hi), xs[i], his[i] - 9, 0.5)
      ink(cr, MUTED)
      text(cr, degrees(d.lo), xs[i], los[i] + 19, 0.5)
    end
  end,
}

-- details -------------------------------------------------------------------

local function value_block(cr, value, unit, sub, w)
  local unit_w = 0
  if unit then
    face(cr, font_regular, 13)
    unit = fit(cr, unit, w - 2 * PAD - 30)
    unit_w = advance(cr, unit) + 4
  end
  fitted_size(cr, DISPLAY_FONT, value, 32, w - 2 * PAD + 1 - unit_w)
  ink(cr, STRONG)
  local vw = text(cr, value, PAD - 1, 74)
  if unit then
    face(cr, font_regular, 13)
    ink(cr, MUTED)
    text(cr, unit, PAD - 1 + vw + 4, 74)
  end
  if sub then
    face(cr, font_regular, 12)
    ink(cr, SOFT)
    text(cr, fit(cr, sub, w - 2 * PAD), PAD, 96)
  end
end

-- A scale bar with a marker. `stops` colour it; nil leaves a plain fill up to
-- the marker instead.
local function scale_bar(cr, w, h, fraction, stops, fill)
  local x0, x1, y = PAD, w - PAD, h - 26
  fraction = clamp(fraction, 0, 1)
  if stops then
    local g = cairo_pattern_create_linear(x0, 0, x1, 0)
    for i, c in ipairs(stops) do
      cairo_pattern_add_color_stop_rgba(g, (i - 1) / (#stops - 1), c[1], c[2], c[3], 0.85)
    end
    cairo_set_source(cr, g)
    capsule(cr, x0, y - 2.5, x1 - x0, 5)
    cairo_fill(cr)
    cairo_pattern_destroy(g)
  else
    ink(cr, 0.10)
    capsule(cr, x0, y - 2.5, x1 - x0, 5)
    cairo_fill(cr)
    source(cr, fill, 0.95)
    capsule(cr, x0, y - 2.5, max(5, (x1 - x0) * fraction), 5)
    cairo_fill(cr)
  end
  local x = clamp(lerp(x0, x1, fraction), x0 + 3, x1 - 3)
  source(cr, P.tint, 0.95)
  disc(cr, x, y, 5.6)
  cairo_fill(cr)
  source(cr, P.ink, 1)
  disc(cr, x, y, 3.8)
  cairo_fill(cr)
end

local CARDINALS = {"N", "NE", "E", "SE", "S", "SW", "W", "NW"}

local function compass_geometry(w, h)
  local r = min(w / 2 - 16, (h - 44) / 2 - 2)
  return w / 2, 44 + (h - 44) / 2 - 1, r
end

TILES.wind = {
  key = function() return weather and tostring(weather.fetched) or "none" end,
  static = function(cr, w, h)
    local cw = caption(cr, "Wind", PAD, 30)
    if not weather then return end
    local c = weather.current
    if c.gust > c.wind + 3 then
      face(cr, font_regular, 11)
      ink(cr, MUTED)
      local gusts = "gusts " .. round(c.gust)
      if advance(cr, gusts) <= w - 2 * PAD - cw - 8 then text(cr, gusts, w - PAD, 30, 1) end
    end

    -- Compass: a tick every 10°, longer at the cardinals, and only north
    -- lettered -- the ticks say where the rest are.
    local cx, cy, r = compass_geometry(w, h)
    cairo_set_line_width(cr, 1)
    for i = 0, 35 do
      local a = i * TAU / 36 - pi / 2
      local long = i % 9 == 0
      ink(cr, long and MUTED or HAIR)
      cairo_new_path(cr)
      cairo_move_to(cr, cx + cos(a) * (r - (long and 5 or 3)), cy + sin(a) * (r - (long and 5 or 3)))
      cairo_line_to(cr, cx + cos(a) * r, cy + sin(a) * r)
      cairo_stroke(cr)
    end
    face(cr, font_medium, 9)
    source(cr, P.accent, 1)
    text(cr, "N", cx, cy - r + 17, 0.5)
  end,
  live = function(cr, w, h, now)
    if not weather then return end
    local c = weather.current
    local cx, cy, r = compass_geometry(w, h)
    -- The needle points the way the wind is blowing; gusts set it swaying.
    local gusty = clamp((c.gust - c.wind) / max(c.wind, 5), 0, 1)
    local sway = ANIMATE and (sin(now * 1.3) * 0.6 + sin(now * 3.1) * 0.4) * 0.06 * (0.3 + gusty) or 0
    local a = math.rad(c.wind_dir + 180) - pi / 2 + sway
    local ca, sa = cos(a), sin(a)
    local tip, tail = r - 9, r - 11
    cairo_set_line_width(cr, 1.6)
    source(cr, P.accent, 0.9)
    cairo_new_path(cr)
    cairo_move_to(cr, cx - ca * tail, cy - sa * tail)
    cairo_line_to(cr, cx + ca * (tip - 6), cy + sa * (tip - 6))
    cairo_stroke(cr)
    cairo_new_path(cr)
    cairo_move_to(cr, cx + ca * tip, cy + sa * tip)
    cairo_line_to(cr, cx + ca * (tip - 8) - sa * 4.2, cy + sa * (tip - 8) + ca * 4.2)
    cairo_line_to(cr, cx + ca * (tip - 8) + sa * 4.2, cy + sa * (tip - 8) - ca * 4.2)
    cairo_close_path(cr)
    source(cr, P.accent, 1)
    cairo_fill(cr)
    cairo_set_line_width(cr, 1.4)
    disc(cr, cx - ca * tail, cy - sa * tail, 2.6)
    cairo_stroke(cr)

    -- Hub: the speed, on a disc of glass the needle passes under.
    local hub = 19
    source(cr, P.tint, 0.92)
    disc(cr, cx, cy, hub)
    cairo_fill(cr)
    ink(cr, HAIR)
    cairo_set_line_width(cr, 1)
    disc(cr, cx, cy, hub)
    cairo_stroke(cr)
    face(cr, font_regular, 15)
    ink(cr, STRONG)
    text(cr, tostring(round(c.wind)), cx, cy + 2.5, 0.5)
    face(cr, font_regular, 7.5)
    ink(cr, MUTED)
    text(cr, weather.units.wind, cx, cy + 11, 0.5)
  end,
}

TILES.humidity = {
  key = function() return weather and tostring(weather.fetched) or "none" end,
  static = function(cr, w, h)
    caption(cr, "Humidity", PAD, 30)
    if not weather then return end
    local c = weather.current
    value_block(cr, tostring(round(c.humidity)), "%", "Dew point " .. degrees(c.dew), w)
    scale_bar(cr, w, h, c.humidity / 100, nil, P.rain)
  end,
}

local function uv_band(uv)
  if uv < 3 then return "Low", P.good
  elseif uv < 6 then return "Moderate", P.fair
  elseif uv < 8 then return "High", P.poor
  elseif uv < 11 then return "Very high", P.bad end
  return "Extreme", P.severe
end

TILES.uv = {
  key = function() return weather and tostring(weather.fetched) or "none" end,
  static = function(cr, w, h)
    caption(cr, "UV index", PAD, 30)
    if not weather then return end
    local uv = weather.current.uv
    local band = uv_band(uv)
    local today = weather.daily[1]
    value_block(cr, tostring(round(uv)), band,
                today and ("Peak " .. round(today.uv) .. " today") or nil, w)
    scale_bar(cr, w, h, uv / 11, {P.good, P.fair, P.poor, P.bad, P.severe})
  end,
}

local function aqi_band(value, scale)
  local bands = scale == "US" and {{50, "Good"}, {100, "Moderate"}, {150, "Sensitive"}, {200, "Unhealthy"},
                                   {300, "Very poor"}, {1e9, "Hazardous"}}
                               or {{20, "Good"}, {40, "Fair"}, {60, "Moderate"}, {80, "Poor"},
                                   {100, "Very poor"}, {1e9, "Extreme"}}
  for _, b in ipairs(bands) do if value <= b[1] then return b[2] end end
end

TILES.air = {
  key = function() return weather and tostring(weather.fetched) or "none" end,
  static = function(cr, w, h)
    if weather and weather.aqi then
      caption(cr, "Air quality", PAD, 30)
      local a = weather.aqi
      value_block(cr, tostring(round(a.value)), aqi_band(a.value, a.scale),
                  string.format("PM2.5  %.0f µg/m³", a.pm25 or 0), w)
      local top = a.scale == "US" and 300 or 100
      scale_bar(cr, w, h, a.value / top, {P.good, P.fair, P.poor, P.bad, P.severe})
    else
      caption(cr, "Pressure", PAD, 30)
      if not weather then return end
      local trend = weather.pressure_trend or 0
      local word = trend > 1 and "Rising" or (trend < -1 and "Falling" or "Steady")
      value_block(cr, tostring(round(weather.current.pressure)), "hPa", word, w)
      scale_bar(cr, w, h, (weather.current.pressure - 970) / 70, nil, P.accent)
    end
  end,
}

-- sky -----------------------------------------------------------------------

TILES.sky = {
  key = function() return (weather and tostring(weather.fetched) or "none") .. os.date("%H%M") end,
  static = function(cr, w, h)
    local cw = caption(cr, "Sun & moon", PAD, 30)
    local now = os.time()
    local phase, lit, name = moon_phase(now)
    local south = weather and weather.lat < 0
    moon_disc(cr, w - PAD - 8, 26, 8, phase, south)
    face(cr, font_regular, 11.5)
    ink(cr, MUTED)
    text(cr, fit(cr, name .. "  " .. round(lit * 100) .. "%", w - 2 * PAD - 22 - cw - 14), w - PAD - 22, 30, 1)

    if not weather or not weather.daily[1] then return end
    local off = place_offset()
    local today_key = os.date("!%Y%m%d", now + off)
    local day = weather.daily[1]
    for _, d in ipairs(weather.daily) do
      if os.date("!%Y%m%d", d.t + off) == today_key then day = d end
    end
    local midnight = day.t
    local rise, set = day.rise - midnight, day.set - midnight
    local noon = (rise + set) / 2
    local c0 = cos(TAU * (rise - noon) / 86400)

    local x0, x1, horizon, A = PAD, w - PAD, 112, 52
    local function X(sec) return x0 + (x1 - x0) * sec / 86400 end
    local function Y(sec)
      local v = (cos(TAU * (sec - noon) / 86400) - c0) / (1 - c0)
      if v < 0 then v = v * 0.32 end
      return horizon - A * v
    end

    -- The sun's height through the day: lit above the horizon, a faint
    -- trace below it.
    cairo_new_path(cr)
    for i = 0, 96 do
      local sec = 86400 * i / 96
      if i == 0 then cairo_move_to(cr, X(sec), Y(sec)) else cairo_line_to(cr, X(sec), Y(sec)) end
    end
    ink(cr, HAIR)
    cairo_set_line_width(cr, 1.2)
    cairo_stroke(cr)

    cairo_new_path(cr)
    cairo_move_to(cr, X(rise), horizon)
    for i = 0, 64 do
      local sec = rise + (set - rise) * i / 64
      cairo_line_to(cr, X(sec), Y(sec))
    end
    cairo_close_path(cr)
    local g = cairo_pattern_create_linear(0, horizon - A, 0, horizon)
    cairo_pattern_add_color_stop_rgba(g, 0, P.sun[1], P.sun[2], P.sun[3], 0.22)
    cairo_pattern_add_color_stop_rgba(g, 1, P.sun[1], P.sun[2], P.sun[3], 0.02)
    cairo_set_source(cr, g)
    cairo_fill_preserve(cr)
    cairo_pattern_destroy(g)
    cairo_new_path(cr)
    for i = 0, 64 do
      local sec = rise + (set - rise) * i / 64
      if i == 0 then cairo_move_to(cr, X(sec), Y(sec)) else cairo_line_to(cr, X(sec), Y(sec)) end
    end
    source(cr, P.sun, 0.55)
    cairo_set_line_width(cr, 1.6)
    cairo_stroke(cr)

    ink(cr, FAINT)
    cairo_rectangle(cr, x0, horizon, x1 - x0, 1)
    cairo_fill(cr)

    -- day.t is the absolute instant of local midnight, so no offset here.
    local sec = (now - midnight) % 86400
    local above = sec > rise and sec < set
    if above then
      cairo_new_path(cr)
      for i = 0, 48 do
        local s2 = rise + (sec - rise) * i / 48
        if i == 0 then cairo_move_to(cr, X(s2), Y(s2)) else cairo_line_to(cr, X(s2), Y(s2)) end
      end
      source(cr, P.sun, 1)
      cairo_set_line_width(cr, 2.2)
      cairo_stroke(cr)
    end
    TILES.sky.sun = {X(sec), Y(sec), above}

    face(cr, font_regular, 11.5)
    ink(cr, SOFT)
    cairo_set_line_width(cr, 1.3)
    local function marker(x, label, up)
      local lw = advance(cr, label)
      local lx = clamp(x, x0 + lw / 2 + 8, x1 - lw / 2)
      chevron(cr, lx - lw / 2 - 6, horizon + 20, 3, up)
      text(cr, label, lx + 2, horizon + 24, 0.5)
    end
    marker(X(rise), clock_text(day.rise, off), true)
    marker(X(set), clock_text(day.set, off), false)

    local length = day.set - day.rise
    face(cr, font_regular, 11.5)
    ink(cr, MUTED)
    local remaining
    if now < day.rise then remaining = "sunrise in " .. floor((day.rise - now) / 3600) .. "h " .. floor((day.rise - now) % 3600 / 60) .. "m"
    elseif now < day.set then remaining = "sunset in " .. floor((day.set - now) / 3600) .. "h " .. floor((day.set - now) % 3600 / 60) .. "m"
    else remaining = "the sun has set" end
    text(cr, string.format("%dh %dm of daylight · %s", floor(length / 3600), floor(length % 3600 / 60), remaining),
         w / 2, h - 14, 0.5)
  end,
  live = function(cr, w, h, now)
    local sun = TILES.sky.sun
    if not sun then return end
    if sun[3] then
      glow(cr, sun[1], sun[2], 16, P.sun, ANIMATE and 0.42 + 0.08 * sin(now * 1.6) or 0.45)
      source(cr, mixc(P.sun, {1, 1, 1}, 0.35), 1)
      disc(cr, sun[1], sun[2], 5)
      cairo_fill(cr)
    else
      ink(cr, MUTED)
      disc(cr, sun[1], sun[2], 3.6)
      cairo_fill(cr)
    end
  end,
}

-- system --------------------------------------------------------------------

-- The arc of a gauge. Its gradient is linear across the ring rather than
-- conic: cairo's X backend draws linear gradients in the X server, but has to
-- rasterise a mesh pattern itself and upload it, every frame, for every ring.
local function ring_arc(cr, cx, cy, r, width, fraction, c0, c1)
  fraction = clamp(fraction, 0, 1)
  if fraction < 0.004 then return end
  local a0 = -pi / 2
  local g = cairo_pattern_create_linear(cx - r, cy - r, cx + r, cy + r)
  cairo_pattern_add_color_stop_rgb(g, 0, c0[1], c0[2], c0[3])
  cairo_pattern_add_color_stop_rgb(g, 1, c1[1], c1[2], c1[3])
  cairo_set_source(cr, g)
  cairo_set_line_width(cr, width)
  cairo_new_path(cr)
  cairo_arc(cr, cx, cy, r, a0, a0 + TAU * fraction)
  cairo_stroke(cr)
  cairo_pattern_destroy(g)
end

local function duration(seconds)
  local d, hr, m = floor(seconds / 86400), floor(seconds % 86400 / 3600), floor(seconds % 3600 / 60)
  if d > 0 then return d .. "d " .. hr .. "h" end
  if hr > 0 then return hr .. "h " .. m .. "m" end
  return m .. "m"
end

-- {caption, key into `shown`, line under the caption}
local function gauges()
  local list = {
    {"CPU", "cpu", info.cpu_temp and degrees(info.cpu_temp, "C") or nil},
    {"Memory", "mem", string.format("%.0f/%.0f G", info.mem_used, info.mem_total)},
  }
  if hw and (hw.gpu_busy or hw.nvidia) then
    list[#list + 1] = {"GPU", "gpu", info.gpu_temp and degrees(info.gpu_temp, "C") or nil}
  end
  for i, disk in ipairs(info.disks) do
    list[#list + 1] = {disk.label, "disk" .. i, disk.free and (disk.free .. " free") or nil}
  end
  if hw and hw.battery then
    list[#list + 1] = {info.charging and "Charging" or "Battery", "bat", nil}
  end
  return list
end

local function gauge_at(w, count, i)
  local span = (w - 2 * PAD) / count
  return PAD + span * (i - 0.5), 68, min(23, span / 2 - 12)
end

local RING_WIDTH = 4.2
-- The graph's left and right ends and its axis. The readings sit either side
-- of it, each with its peak on a second line underneath.
local function graph_box(w, h) return PAD + 104, w - PAD - 104, h - 28 end
local NET_HALF = 11

-- What does not move -- captions, tracks, the graph's baseline -- is static,
-- so the live part of the tile is only arcs, numbers and the graph itself.
TILES.system = {
  key = function()
    local names = {}
    for _, g in ipairs(gauges()) do names[#names + 1] = g[1] end
    return table.concat(names, ",") .. floor(info.uptime / 60)
  end,
  static = function(cr, w, h)
    caption(cr, "System", PAD, 30)
    face(cr, font_regular, 11)
    ink(cr, MUTED)
    text(cr, "up " .. duration(info.uptime), w - PAD, 30, 1)

    local list = gauges()
    for i, g in ipairs(list) do
      local cx, cy, r = gauge_at(w, #list, i)
      cairo_set_line_width(cr, RING_WIDTH)
      ink(cr, 0.10)
      disc(cr, cx, cy, r)
      cairo_stroke(cr)
      face(cr, font_medium, 9)
      ink(cr, MUTED)
      tracked(cr, upper(g[1]), cx, cy + r + 17, 1.2, 0.5)
    end

    local gx0, gx1, gy = graph_box(w, h)
    ink(cr, HAIR)
    cairo_rectangle(cr, gx0, gy, gx1 - gx0, 1)
    cairo_fill(cr)
    cairo_set_line_width(cr, 1.4)
    source(cr, P.accent, 1)
    chevron(cr, PAD + 4, gy - 3, 3.2, false)
    source(cr, P.accent2, 1)
    chevron(cr, w - PAD - 4, gy - 3, 3.2, true)
  end,
  live = function(cr, w, h, now)
    local list = gauges()
    for i, g in ipairs(list) do
      local cx, cy, r = gauge_at(w, #list, i)
      local value = shown[g[2]] or 0
      local hot = clamp((value - 75) / 25, 0, 1)
      ring_arc(cr, cx, cy, r, RING_WIDTH, value / 100, P.accent, mixc(P.accent2, P.bad, hot))
      -- The number and a smaller % sign, centred together inside the ring;
      -- "100%" shrinks rather than touching a small ring.
      local number, size = tostring(round(value)), 13
      face(cr, font_regular, 8.5)
      local sign = advance(cr, "%") + 1
      face(cr, font_regular, size)
      local nw = advance(cr, number)
      local room = 2 * r - RING_WIDTH - 6
      if nw + sign > room then
        size = size * (room - sign) / nw
        face(cr, font_regular, size)
        nw = advance(cr, number)
      end
      local x = cx - (nw + sign) / 2
      ink(cr, STRONG)
      text(cr, number, x, cy + 4.5)
      face(cr, font_regular, 8.5)
      ink(cr, MUTED)
      text(cr, "%", x + nw + 1, cy + 4.5)
      if g[3] then
        face(cr, font_regular, 10)
        ink(cr, FAINT)
        text(cr, g[3], cx, cy + r + 30, 0.5)
      end
    end

    -- Network, drawn as conky-dashboard-pixel draws it: download above the
    -- axis, upload below, both against one linear scale -- the larger of the
    -- two peaks -- so their heights compare. (Each to its own peak drew
    -- 31 MiB/s up as tall as 629 KiB/s down.) A quiet upload stays close to
    -- the axis, which is what it is. Newest on the right, scrolling smoothly
    -- between points.
    local gx0, gx1, gy = graph_box(w, h)
    local down_peak, up_peak = peak_of(net.down), peak_of(net.up)
    local scale = max(down_peak, up_peak, NET_FLOOR)
    local step = (gx1 - gx0) / (NET_SAMPLES - 1)
    local slide = ANIMATE and clamp(now - sampled_at, 0, 1) or 0
    local function trace(series, downward)
      local n = #series
      if n < 2 then return nil end
      local x0 = gx1 - (n - 1 + slide) * step
      cairo_new_path(cr)
      for i = 1, n do
        local v = clamp(series[i] / scale, 0, 1)
        local x, y = x0 + (i - 1) * step, downward and gy + v * NET_HALF or gy - v * NET_HALF
        if i == 1 then cairo_move_to(cr, x, y) else cairo_line_to(cr, x, y) end
      end
      return x0, x0 + (n - 1) * step
    end
    local function area(series, color, downward)
      local x0, x1 = trace(series, downward)
      if not x0 then return end
      -- Fill first, fading out towards the axis, then the trace at full
      -- weight so the shape reads even where the fill has all but gone.
      cairo_line_to(cr, x1, gy)
      cairo_line_to(cr, x0, gy)
      cairo_close_path(cr)
      local far = downward and gy + NET_HALF or gy - NET_HALF
      local g = cairo_pattern_create_linear(0, far, 0, gy)
      cairo_pattern_add_color_stop_rgba(g, 0, color[1], color[2], color[3], 0.45)
      cairo_pattern_add_color_stop_rgba(g, 1, color[1], color[2], color[3], 0.04)
      cairo_set_source(cr, g)
      cairo_fill(cr)
      cairo_pattern_destroy(g)
      trace(series, downward)
      source(cr, color, 0.95)
      cairo_set_line_width(cr, 1.6)
      cairo_set_line_join(cr, CAIRO_LINE_JOIN_ROUND)
      cairo_stroke(cr)
    end
    cairo_save(cr)
    cairo_rectangle(cr, gx0, gy - NET_HALF - 2, gx1 - gx0, 2 * NET_HALF + 4)
    cairo_clip(cr)
    area(net.down, P.accent, false)
    area(net.up, P.accent2, true)
    cairo_restore(cr)

    -- The readings are the graph's newest point, a whole second's average,
    -- and under each the busiest second the graph holds.
    face(cr, font_regular, 11.5)
    ink(cr, SOFT)
    text(cr, format_rate(net.down[#net.down] or 0), PAD + 13, gy + 1)
    text(cr, format_rate(net.up[#net.up] or 0), w - PAD - 13, gy + 1, 1)
    face(cr, font_regular, 9.5)
    ink(cr, FAINT)
    text(cr, "peak " .. format_rate(down_peak), PAD + 13, gy + 14)
    text(cr, "peak " .. format_rate(up_peak), w - PAD - 13, gy + 14, 1)
  end,
}

---------------- THE HELPER ----------------

local function shell_quote(s)
  return "'" .. tostring(s):gsub("'", "'\\''") .. "'"
end

local function choice(value, allowed, default)
  value = tostring(value):lower()
  for _, a in ipairs(allowed) do if value == a then return a end end
  return default
end

local function helper_command()
  local rects = {}
  for _, t in ipairs(layout.tiles) do rects[#rects + 1] = t.key end
  local s = layout.s
  local parts = {
    "python3", shell_quote(SCRIPT_DIR .. "/mirage.py"), "tick",
    "--cache", shell_quote(CACHE), "--title", shell_quote(TITLE),
    "--window", string.format("%dx%d", layout.W, layout.H),
    "--tiles", shell_quote(table.concat(rects, ";")),
    "--radius", string.format("%.2f", RADIUS * s), "--scale", string.format("%.4f", s),
    "--wallpaper", shell_quote(wallpaper),
    "--mode", choice(glass_tone, {"auto", "dark", "light"}, "auto"),
    "--blur", string.format("%.1f", blur_radius * s),
    "--tint", string.format("%.3f", clamp(number_or(glass_tint, 0.26), 0, 0.95)),
    "--vibrancy", string.format("%.3f", number_or(vibrancy, 1.35)),
    "--shadow", string.format("%.3f", number_or(shadow_strength, 0.45)),
    "--accent", shell_quote(accent_color), "--location", shell_quote(location),
    "--units", choice(units, {"metric", "imperial"}, "metric"),
    "--aqi", tostring(air_quality_scale):lower() == "us" and "us" or "european",
    "--interval", string.format("%.1f", number_or(weather_refresh_minutes, 15)),
    "--clock", TWELVE_HOUR and "12h" or "24h",
    SHOW_UNIT and "--show-unit" or nil,
  }
  if mirage_preview_at then parts[#parts + 1] = "--at " .. shell_quote(mirage_preview_at) end
  return table.concat(parts, " ")
end

local next_helper = 0
-- Seconds between helper runs, which is how soon a new wallpaper is noticed.
-- A run with nothing to do costs about 30ms of CPU.
local HELPER_EVERY = 10
local cache_made = false

local function run_helper(now, soon)
  -- The preview harness sets this to make the next frame run the helper,
  -- as a wallpaper change would within HELPER_EVERY on a real desktop.
  if mirage_preview_refresh then mirage_preview_refresh, soon = nil, true end
  if soon then next_helper = min(next_helper, now) end
  if now < next_helper then return end
  next_helper = now + HELPER_EVERY
  if not cache_made then
    os.execute("mkdir -p " .. shell_quote(CACHE))
    cache_made = true
  end
  local command = helper_command() .. " >/dev/null 2>>" .. shell_quote(CACHE .. "/helper.err")
  if mirage_preview_sync then os.execute(command) else os.execute(command .. " &") end
end

-- Noticing a new wallpaper. The helper's run every HELPER_EVERY seconds
-- notices one in the end, but starting Python is the expensive part of a
-- run. On the desktops that can simply be asked -- Plasma over D-Bus, the
-- GNOME family through gsettings, Xfce through xfconf -- that answer costs a
-- few milliseconds, so it is asked every PROBE_EVERY seconds, detached, and
-- read back on the next round; when it changes, the helper runs at once. A
-- wallpaper change then shows within two or three seconds, not ten.
local watch_wallpaper
do
  local PROBE_EVERY = 1.5
  local probe_command, probe_seen, next_probe = nil, nil, 0

  local function wallpaper_probe()
    if tostring(wallpaper) ~= "auto" then return false end
    local desktop = (os.getenv("XDG_CURRENT_DESKTOP") or ""):lower()
    if desktop:find("kde") or desktop:find("plasma") then
      return "gdbus call --session --dest org.kde.plasmashell --object-path /PlasmaShell " ..
             "--method org.kde.PlasmaShell.wallpaper 0"
    elseif desktop:find("cinnamon") then
      return "gsettings get org.cinnamon.desktop.background picture-uri"
    elseif desktop:find("mate") then
      return "gsettings get org.mate.background picture-filename"
    elseif desktop:find("gnome") or desktop:find("unity") or desktop:find("budgie") or desktop:find("pantheon") then
      return "gsettings get org.gnome.desktop.background picture-uri; " ..
             "gsettings get org.gnome.desktop.background picture-uri-dark; " ..
             "gsettings get org.gnome.desktop.interface color-scheme"
    elseif desktop:find("xfce") then
      return "xfconf-query -c xfce4-desktop -l -v | grep last-image"
    end
    return false     -- feh, swww, hyprpaper...: the regular helper run finds those
  end

  function watch_wallpaper(now)
    if mirage_preview_sync or now < next_probe then return end
    next_probe = now + PROBE_EVERY
    if probe_command == nil then probe_command = wallpaper_probe() end
    if not probe_command then return end
    local answer = read_all(CACHE .. "/wallpaper.probe")
    if answer and answer ~= "" then
      if probe_seen and answer ~= probe_seen then run_helper(now, true) end
      probe_seen = answer
    end
    -- Written aside and renamed, so a read never sees half an answer.
    local tmp, out = shell_quote(CACHE .. "/wallpaper.probe.tmp"), shell_quote(CACHE .. "/wallpaper.probe")
    os.execute("(" .. probe_command .. ") > " .. tmp .. " 2>/dev/null && mv -f " .. tmp .. " " .. out .. " &")
  end
end

---------------- THEME AND GLASS ----------------

local theme = nil              -- the helper's theme, once loaded
local glass = {}               -- tile key -> surface, for the current theme
local glass_old = {}           -- the previous theme's, while it fades out
local shadow, shadow_old = nil, nil
local fade_start, fade_from = nil, nil
local FADE = 1.6
local pending_theme = nil

local function destroy_all(set)
  for _, surface in pairs(set) do cairo_surface_destroy(surface) end
end

-- PNGs are decoded once and copied into surfaces of the window's own kind,
-- so painting them each frame is a blit inside the X server.
local function upload(target_surface, path)
  local image = cairo_image_surface_create_from_png(path)
  if cairo_surface_status(image) ~= CAIRO_STATUS_SUCCESS then
    cairo_surface_destroy(image)
    return nil
  end
  local w, h = cairo_image_surface_get_width(image), cairo_image_surface_get_height(image)
  local copy = cairo_surface_create_similar(target_surface, CAIRO_CONTENT_COLOR_ALPHA, w, h)
  local cr = cairo_create(copy)
  cairo_set_source_surface(cr, image, 0, 0)
  cairo_set_operator(cr, CAIRO_OPERATOR_SOURCE)
  cairo_paint(cr)
  cairo_destroy(cr)
  cairo_surface_destroy(image)
  return copy
end

local function install_theme(target_surface, now)
  local new = pending_theme
  pending_theme = nil
  -- A preview is a single settled frame; it has no time for a fade.
  if mirage_preview_still then fade_start = nil end
  local surfaces, new_shadow = {}, nil
  if GLASS and new.glass then
    for _, t in ipairs(new.tiles or {}) do
      local s = upload(target_surface, CACHE .. "/" .. t.file)
      if s then surfaces[string.format("%d,%d,%d,%d", t.x, t.y, t.w, t.h)] = s end
    end
    if new.shadow then new_shadow = upload(target_surface, CACHE .. "/" .. new.shadow) end
  end
  destroy_all(glass_old)
  if shadow_old then cairo_surface_destroy(shadow_old) end
  glass_old, shadow_old = glass, shadow
  glass, shadow = surfaces, new_shadow
  fade_from = BASE
  fade_start = now
  theme = new
  if mirage_preview_still then
    fade_start = nil
    destroy_all(glass_old)
    glass_old = {}
    if shadow_old then cairo_surface_destroy(shadow_old); shadow_old = nil end
    BASE = blend_palette(theme, theme, 1)
    P = BASE
    palette_version = palette_version + 1
  end
end

local function update_palette(now)
  if not fade_start then return end
  local t = clamp((now - fade_start) / FADE, 0, 1)
  local e = t * t * (3 - 2 * t)
  BASE = blend_palette(fade_from, theme, e)
  P = BASE
  palette_version = palette_version + 1
  if t >= 1 then
    fade_start = nil
    destroy_all(glass_old)
    glass_old = {}
    if shadow_old then cairo_surface_destroy(shadow_old); shadow_old = nil end
  end
end

---------------- ONE COPY, AND A WINDOW THAT SURVIVES A RELOAD ----------------
-- Two copies of Mirage share the cache and the window title, so each one's
-- helper renders glass for its own window, and the other, finding glass that
-- does not fit its tiles, falls back to plain panes: the pair flicker between
-- glass and tint every few seconds. So the newest copy wins. Each writes its
-- pid to `owner` as it starts, and a copy that finds another live owner there
-- steps down.
--
-- And conky reloads its config by itself when the file changes -- the
-- settings window saving it, or an edit by hand -- after which its window is
-- 1x1: conky 1.25 does not apply minimum_width/height again. Nothing here can
-- resize the window, so a copy that finds itself in one starts a fresh copy
-- with its own command line and steps down; the edit then simply shows.

local check_owner, restart_fresh
do
  local MY_PID = tonumber((read_first_line("/proc/self/stat") or ""):match("^(%d+)"))
  local owner_claimed = false

  local function is_conky(pid)
    local cmd = read_all("/proc/" .. pid .. "/cmdline") or ""
    return cmd:find("conky", 1, true) ~= nil
  end

  local function claim_ownership()
    local file = io.open(CACHE .. "/owner", "w")
    if file then
      file:write(tostring(MY_PID), "\n")
      file:close()
    end
  end

  local function step_down(why)
    io.stderr:write("conky-mirage: " .. why .. "\n")
    os.exit(0)
  end

  function check_owner()
    if mirage_preview_sync or not MY_PID then return end
    if not owner_claimed then
      claim_ownership()
      owner_claimed = true
      return
    end
    local owner = tonumber(read_first_line(CACHE .. "/owner") or "")
    if owner and owner ~= MY_PID and is_conky(owner) then
      step_down("a newer copy of Mirage is running (pid " .. owner .. "), so this one is closing")
    elseif owner ~= MY_PID then
      claim_ownership()          -- the owner has gone; carry on as the one copy
    end
  end

  function restart_fresh()
    if mirage_preview_sync or not MY_PID then return end
    -- Not more than once in half a minute, in case a fresh start comes up
    -- just as small: then it is a conky problem, and looping would not help.
    local last = tonumber(read_first_line(CACHE .. "/restarted") or "") or 0
    if os.time() - last < 30 then return end
    local file = io.open(CACHE .. "/restarted", "w")
    if file then file:write(os.time(), "\n"); file:close() end

    local args = {}
    for arg in (read_all("/proc/" .. MY_PID .. "/cmdline") or ""):gmatch("[^%z]+") do
      args[#args + 1] = shell_quote(arg)
    end
    local pipe = io.popen("readlink /proc/" .. MY_PID .. "/cwd")
    local cwd = pipe and pipe:read("*l") or "/"
    if pipe then pipe:close() end
    if #args == 0 then return end
    os.execute("cd " .. shell_quote(cwd) .. " && (sleep 0.5; exec " .. table.concat(args, " ") ..
               ") >/dev/null 2>&1 &")
    step_down("conky reloaded its settings and lost the window size, so Mirage is starting afresh")
  end
end

local serial_seen = nil
local next_poll = 0
local first_poll = nil

-- If the helper never writes anything, it is not running at all -- no
-- python3, say -- and whatever it printed on the way out is the explanation.
local function helper_failure()
  local last = nil
  for line in (read_all(CACHE .. "/helper.err") or ""):gmatch("[^\n]+") do last = line end
  return last and {error = last:gsub("^%w*Error: ", "")} or {error = "mirage.py didn't run. Is Python 3 installed?"}
end

local function poll_cache(now)
  if now < next_poll then return end
  next_poll = now + 1
  check_owner()
  first_poll = first_poll or now
  local serial = read_first_line(CACHE .. "/serial")
  if serial == nil and not weather_error and now - first_poll > 20 then
    weather_error = helper_failure()
    data_version = data_version + 1
  end
  if serial == nil or serial == serial_seen then return end
  serial_seen = serial
  local w = load_table(CACHE .. "/weather.lua")
  if w and (not weather or w.fetched ~= weather.fetched) then
    weather = w
    data_version = data_version + 1
  end
  weather_error = not weather and load_table(CACHE .. "/weather_error.lua") or nil
  data_version = data_version + 1
  local th = load_table(CACHE .. "/theme.lua")
  if th and (not theme or th.id ~= theme.id) then pending_theme = th end
end

---------------- DRAWING ----------------

local start_time = nil

local function fade_amount(now)
  if not fade_start then return 1 end
  local t = clamp((now - fade_start) / FADE, 0, 1)
  return t * t * (3 - 2 * t)
end

local function plain_pane(cr, tile, alpha)
  local s = tile.s
  squircle(cr, tile.x, tile.y, tile.w, tile.h, RADIUS * s)
  source(cr, P.tint, (P.mode == "dark" and 0.58 or 0.62) * alpha)
  cairo_fill_preserve(cr)
  ink(cr, 0.12 * alpha)
  cairo_set_line_width(cr, 1)
  cairo_stroke(cr)
end

local function draw_glass(cr, tile, alpha, f)
  local new, old = glass[tile.key], glass_old[tile.key]
  if not new and not old then return plain_pane(cr, tile, alpha) end
  if fade_start and f < 1 then
    if old then
      cairo_set_source_surface(cr, old, tile.x, tile.y)
      cairo_paint_with_alpha(cr, alpha)
    else
      plain_pane(cr, tile, alpha)
    end
    if new then
      cairo_set_source_surface(cr, new, tile.x, tile.y)
      cairo_paint_with_alpha(cr, alpha * f)
    end
  elseif new then
    cairo_set_source_surface(cr, new, tile.x, tile.y)
    cairo_paint_with_alpha(cr, alpha)
  else
    plain_pane(cr, tile, alpha)
  end
end

-- A box's background in the looks drawn here rather than by the helper.
local function draw_background(cr, tile, alpha, f)
  if GLASS then return draw_glass(cr, tile, alpha * OPAQUE, f) end
  squircle(cr, tile.x, tile.y, tile.w, tile.h, RADIUS * tile.s)
  source(cr, P.tint, alpha * OPAQUE)
  if STYLE == "solid" then
    -- A faint edge, so a box reads as a box on a busy wallpaper.
    cairo_fill_preserve(cr)
    ink(cr, 0.08 * alpha)
    cairo_set_line_width(cr, 1)
    cairo_stroke(cr)
  else
    cairo_fill(cr)                -- Metro and Pixel are flat: no edge, no shadow
  end
end

local function draw_static(cr, target_surface, tile, spec, alpha)
  local key = (spec.key and spec.key() or "") .. "|" .. data_version .. "|" .. palette_version
  if tile.cache_key ~= key then
    if tile.cache == nil then
      tile.cache = cairo_surface_create_similar(target_surface, CAIRO_CONTENT_COLOR_ALPHA, tile.w, tile.h)
    end
    local c = cairo_create(tile.cache)
    cairo_set_operator(c, CAIRO_OPERATOR_CLEAR)
    cairo_paint(c)
    cairo_set_operator(c, CAIRO_OPERATOR_OVER)
    prepare(c)
    cairo_scale(c, tile.cs, tile.cs)
    room = tile.cw
    spec.static(c, tile.cw, tile.ch, tile)
    cairo_destroy(c)
    tile.cache_key = key
  end
  cairo_set_source_surface(cr, tile.cache, tile.x, tile.y)
  cairo_paint_with_alpha(cr, alpha)
end

-- Everything that does not move -- shadow, glass and each tile's static
-- layer -- is flattened into one surface, rebuilt only when one of them
-- changes: every frame during the intro and a palette fade, otherwise about
-- once a minute when the clock turns over. Compositing a score of layers
-- the size of the window ten times a second was most of what the widget cost
-- the X server.
local base, base_key = nil, nil

local function appear_of(i, intro)
  return ease_out((intro - 0.15 - (i - 1) * 0.07) / 0.9)
end

local function rise(appear, tile)
  return round((1 - appear) * 16 * tile.s)
end

local function compose_base(target_surface, intro, f)
  if base == nil then
    base = cairo_surface_create_similar(target_surface, CAIRO_CONTENT_COLOR_ALPHA, layout.W, layout.H)
  end
  local cr = cairo_create(base)
  cairo_set_operator(cr, CAIRO_OPERATOR_CLEAR)
  cairo_paint(cr)
  cairo_set_operator(cr, CAIRO_OPERATOR_OVER)
  prepare(cr)

  local shadow_alpha = ease_out((intro - 0.1) / 1.2) * OPAQUE
  if STYLE == "metro" or STYLE == "pixel" then shadow_alpha = 0 end
  if shadow_old and f < 1 then
    cairo_set_source_surface(cr, shadow_old, 0, 0)
    cairo_paint_with_alpha(cr, shadow_alpha * (1 - f))
  end
  if shadow then
    cairo_set_source_surface(cr, shadow, 0, 0)
    cairo_paint_with_alpha(cr, shadow_alpha * (shadow_old and f or 1))
  end

  for i, tile in ipairs(layout.tiles) do
    -- Tiles arrive one after another, rising into place.
    local appear = appear_of(i, intro)
    if appear > 0 then
      local spec = TILES[tile.name]
      P = palette_for(tile.name)
      cairo_save(cr)
      cairo_translate(cr, 0, rise(appear, tile))
      draw_background(cr, tile, appear, f)
      if spec.static then draw_static(cr, target_surface, tile, spec, appear) end
      cairo_restore(cr)
    end
  end
  P = BASE
  cairo_destroy(cr)
end

local function draw_frame(cr, target_surface, now)
  prepare(cr)
  local intro = now - start_time
  local f = fade_amount(now)

  local settled = not fade_start and appear_of(#layout.tiles, intro) >= 1
  local key = nil
  if settled then
    local parts = {theme and theme.id or 0, palette_version, data_version}
    for _, tile in ipairs(layout.tiles) do
      local spec = TILES[tile.name]
      parts[#parts + 1] = spec.key and spec.key() or ""
    end
    key = table.concat(parts, "|")
  end
  if key == nil or key ~= base_key then
    compose_base(target_surface, intro, f)
    base_key = key
  end
  cairo_set_source_surface(cr, base, 0, 0)
  cairo_paint(cr)

  for i, tile in ipairs(layout.tiles) do
    local spec = TILES[tile.name]
    local appear = appear_of(i, intro)
    if spec.live and appear > 0 then
      cairo_save(cr)
      cairo_translate(cr, 0, rise(appear, tile))
      -- Clipped to the tile, which also keeps any group pushed inside -- the
      -- icon's cut-outs -- the size of the tile rather than of the window.
      cairo_rectangle(cr, tile.x, tile.y, tile.w, tile.h)
      cairo_clip(cr)
      if appear < 1 then cairo_push_group(cr) end
      cairo_save(cr)
      cairo_translate(cr, tile.x, tile.y)
      cairo_scale(cr, tile.cs, tile.cs)
      room = tile.cw
      P = palette_for(tile.name)
      spec.live(cr, tile.cw, tile.ch, now, tile)
      P = BASE
      cairo_restore(cr)
      if appear < 1 then
        cairo_pop_group_to_source(cr)
        cairo_paint_with_alpha(cr, appear)
      end
      cairo_restore(cr)
    end
  end
end

local function drop_layout()
  if not layout then return end
  for _, tile in ipairs(layout.tiles) do
    if tile.cache then cairo_surface_destroy(tile.cache) end
  end
  if base then cairo_surface_destroy(base) end
  base, base_key = nil, nil
end

local last_now = nil
local size_warned = false

-- The window's shape comes from base_width/base_height in start_conky_mirage
-- and the layout's from the sizes above. If they disagree the widget still
-- draws, scaled down to fit, but not at the size or in the place intended.
local function check_window(W, H)
  if size_warned or mirage_preview_sync then return end
  size_warned = true
  local bw, bh = layout.base_w, layout.base_h
  if abs(W / H - bw / bh) > 0.015 then
    io.stderr:write(string.format(
      "conky-mirage: the sizes in mirage.lua don't fit the window. Open ./mirage_settings.py " ..
      "and click Save, or put this line in start_conky_mirage:\n" ..
      "    local base_width, base_height = %d, %d\n", round(bw), round(bh)))
  end
end

function conky_mirage()
  if conky_window == nil then return end
  -- conky_window is not usable for the first few updates; wait about a
  -- second whatever the frame rate.
  if number_or(conky_parse("${updates}"), 0) <= target_fps then return end

  local now = wall_clock()
  local dt = clamp(now - (last_now or now), 0, 0.25)
  last_now = now
  start_time = start_time or (mirage_preview_still and now - 60) or now

  local W, H = conky_window.width, conky_window.height
  if W < 40 or H < 40 then return restart_fresh() end
  local resized = not layout or layout.W ~= W or layout.H ~= H
  if resized then
    drop_layout()
    layout = compute_layout(W, H)
    check_window(W, H)
  end

  run_helper(now, resized)
  watch_wallpaper(now)
  poll_cache(now)
  sample(now)
  ease_all(dt)

  local cs, owns_surface = conky_window_surface()
  if pending_theme then install_theme(cs, now) end
  update_palette(now)

  local cr = cairo_create(cs)
  draw_frame(cr, cs, now)
  cairo_destroy(cr)
  if owns_surface then cairo_surface_destroy(cs) end
end
