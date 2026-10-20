--[[ ==========================================================================
  ConkyTray -- a panel along the bottom of the screen, in the look of
  Conky-Mirage: an app menu, your pinned and open apps, the tray,
  volume, network, battery and the clock.

  Start it with start_conky_tray. It needs a Wayland desktop that has the
  layer shell (KDE Plasma does).
============================================================================ ]]

--[[ USER CONFIGURATION ]]

-- (Which edge of the screen the panel is on, and its size, are set in
-- start_conky_tray.)

-- The look. "mirage" wears whichever look Conky-Mirage is set to; or pick
-- "glass", "solid", "metro" or "pixel".
style = "pixel"

-- Which parts the panel shows, from one end to the other.
show_launcher = "yes"         -- the button for the app menu
show_tasks    = "yes"         -- your pinned and open apps
show_media    = "yes"         -- the song or video playing, in the room between
show_visualizer = "yes"       -- bars beside the song that move with the sound
show_tray     = "yes"         -- the apps' tray icons
show_clock    = "no"          -- the time, and the date under it

clock_format = "24h"          -- "24h" or "12h"
show_date    = "yes"          -- the date under the time
date_format  = "%a %d %b"     -- how the date is written, see "man strftime"

show_privacy    = "yes"       -- a warning while an app uses your microphone or camera
show_notifications = "yes"    -- a bell with the notifications you have had
show_devices    = "auto"      -- USB drives and cards, while one is plugged in
show_keyboard   = "yes"       -- the keyboard layout, when you have more than one
show_bluetooth  = "yes"
show_brightness = "yes"       -- for screens whose brightness can be set
show_volume  = "yes"
show_network = "yes"
show_battery = "auto"         -- "auto" shows it when the computer has one

-- The button at the left end opens the app menu; right-click it to lock the
-- screen, log out or shut down. "auto" uses the icon of Plasma's app menu.
launcher_icon = "auto"

-- Colours. "auto" takes them from your wallpaper, as the look does, or give
-- one like "#1f3a5f". Each look keeps its own, so you can switch between them.
glass_color        = "auto"      -- the tint of the glass
glass_transparency = 0.00        -- how much of your wallpaper shows through the glass, 0 to 0.9
solid_color        = "auto"
metro_launcher     = "auto"      -- Metro's blocks: the app menu button,
metro_apps         = "auto"      -- your apps,
metro_media        = "auto"      -- what is playing,
metro_tray         = "auto"      -- the tray
metro_clock        = "auto"      -- and the clock
pixel_launcher     = "auto"      -- Pixel's blocks, the same
pixel_apps         = "auto"
pixel_media        = "auto"
pixel_tray         = "auto"
pixel_clock        = "auto"

--[[ DON'T EDIT BELOW IF YOU DO NOT KNOW WHAT YOU ARE DOING ]]

require 'cairo'
pcall(require, 'cairo_xlib')

-- conky_surface() is conky's own surface, which it keeps; older builds need
-- an xlib surface made (and freed) on every draw.
local function conky_window_surface()
  if type(conky_surface) == "function" then
    return conky_surface(), false
  end
  return cairo_xlib_surface_create(conky_window.display, conky_window.drawable,
                                   conky_window.visual, conky_window.width,
                                   conky_window.height), true
end

local floor, min, max, abs, pi = math.floor, math.min, math.max, math.abs, math.pi
local function round(v) return floor(v + 0.5) end
local function clamp(v, low, high) return min(max(v, low), high) end
local function lerp(a, b, t) return a + (b - a) * t end
local function yes(value) value = tostring(value):lower(); return value == "yes" or value == "true" end

local HERE = debug.getinfo(1, "S").source:match("^@(.*)/[^/]*$") or "."
local CACHE = tray_cache_dir or
  ((os.getenv("XDG_CACHE_HOME") or ((os.getenv("HOME") or "/tmp") .. "/.cache")) .. "/conky-tray")

local function read_all(path)
  local f = io.open(path, "r")
  if not f then return nil end
  local text = f:read("a")
  f:close()
  return text
end

local function read_first_line(path)
  local f = io.open(path, "r")
  if not f then return nil end
  local line = f:read("l")
  f:close()
  return line
end

local function shell_quote(s) return "'" .. tostring(s):gsub("'", "'\\''") .. "'" end

local MY_PID = tonumber((read_first_line("/proc/self/stat") or ""):match("^(%d+)")) or 0

-- Sub-second time, from /proc/uptime: os.time() has whole seconds only.
local function now_seconds()
  return tonumber((read_first_line("/proc/uptime") or ""):match("^(%S+)") or "") or os.time()
end

os.execute("mkdir -p " .. shell_quote(CACHE))

---------------- PALETTE ----------------
-- BASE is Conky-Mirage's palette for the wallpaper, once the helper has
-- asked Mirage for it; until then, and without Mirage, a quiet dark one.

local FALLBACK = {
  mode = "dark", ink = {0.96, 0.96, 0.98}, accent = {0.72, 0.74, 1.00}, bad = {1.0, 0.55, 0.55},
  tint = {0.09, 0.09, 0.13}, shade = {0.0, 0.0, 0.02}, solid = {0.10, 0.10, 0.14},
  metro1 = {0.55, 0.22, 0.40}, metro2 = {0.18, 0.32, 0.58}, metro3 = {0.56, 0.30, 0.16},
  metro4 = {0.10, 0.40, 0.42},
  px_surface = {0.13, 0.12, 0.15}, px_primary_c = {0.30, 0.20, 0.36}, px_secondary_c = {0.22, 0.20, 0.26},
  px_tertiary_c = {0.16, 0.26, 0.32}, px_on = {0.93, 0.91, 0.95}, px_primary = {0.84, 0.74, 1.0},
}
local BASE = FALLBACK
local theme, theme_key = nil, nil
local glass = {}                -- "x,y,w,h" -> the frosted pane cut for it

local function color_hex(c)
  return string.format("#%02x%02x%02x", round(clamp(c[1], 0, 1) * 255),
                       round(clamp(c[2], 0, 1) * 255), round(clamp(c[3], 0, 1) * 255))
end

local function color_css(c, alpha)
  return string.format("rgba(%d,%d,%d,%.3f)", round(clamp(c[1], 0, 1) * 255),
                       round(clamp(c[2], 0, 1) * 255), round(clamp(c[3], 0, 1) * 255), alpha or 1)
end

local function mix(a, b, t) return {lerp(a[1], b[1], t), lerp(a[2], b[2], t), lerp(a[3], b[3], t)} end

local function load_png(path)
  if not path then return nil end
  local s = cairo_image_surface_create_from_png(path)
  if cairo_surface_status(s) ~= CAIRO_STATUS_SUCCESS then
    cairo_surface_destroy(s)
    return nil
  end
  return s
end

local function install_theme(t)
  local key = t and (tostring(t.id) .. ":" .. tostring(t.stamp)) or nil
  if key == theme_key then return false end
  theme_key = key
  for _, s in pairs(glass) do cairo_surface_destroy(s) end
  glass = {}
  local loaded = t and loadfile(t.dir .. "/theme.lua", "t", {})
  local ok, th = false, nil
  if loaded then ok, th = pcall(loaded) end
  if not ok or type(th) ~= "table" then
    BASE = FALLBACK
    return true
  end
  theme = th
  BASE = setmetatable(th, {__index = FALLBACK})
  for _, tile in ipairs(th.tiles or {}) do
    local s = load_png(t.dir .. "/" .. tile.file)
    if s then glass[string.format("%d,%d,%d,%d", tile.x, tile.y, tile.w, tile.h)] = s end
  end
  return true
end

---------------- STATE FROM THE HELPER ----------------

local state = {tasks = {}, tray = {}}
local serial_seen = nil
local next_poll, next_daemon_check = 0, 0
local icons = {}                -- png path -> surface

local function icon(path)
  if not path then return nil end
  local s = icons[path]
  if s == nil then
    s = load_png(path) or false
    icons[path] = s
  end
  return s or nil
end

-- The helper is a long-running Python program. Start it when it is not
-- there; it stops by itself once this conky has gone.
local function check_daemon()
  local pid = tonumber((read_first_line(CACHE .. "/daemon.pid") or ""):match("%d+") or "")
  if pid then
    local cmd = read_all("/proc/" .. pid .. "/cmdline") or ""
    if cmd:find("tray.py", 1, true) then return end
  end
  os.execute("python3 " .. shell_quote(HERE .. "/tray.py") .. " daemon --cache " .. shell_quote(CACHE) ..
             " >> " .. shell_quote(CACHE .. "/daemon.log") .. " 2>&1 &")
end

local function poll(now)
  if now >= next_daemon_check and not tray_preview then
    next_daemon_check = now + 3
    check_daemon()
  end
  if now < next_poll then return false end
  next_poll = now + 0.2
  local serial = read_first_line(CACHE .. "/serial")
  if serial == serial_seen then return false end
  serial_seen = serial
  local loaded = loadfile(CACHE .. "/state.lua", "t", {})
  local ok, s = false, nil
  if loaded then ok, s = pcall(loaded) end
  if ok and type(s) == "table" then
    state = s
    state.tasks, state.tray = state.tasks or {}, state.tray or {}
    -- Icons no longer shown are let go of.
    local wanted = {}
    for _, t in ipairs(state.tasks) do if t.icon then wanted[t.icon] = true end end
    for _, t in ipairs(state.tray) do if t.icon then wanted[t.icon] = true end end
    for _, k in ipairs({"volume", "network", "battery", "launcher", "bluetooth", "brightness", "devices",
                        "privacy", "notifications"}) do
      if state[k] and state[k].icon then wanted[state[k].icon] = true end
    end
    for _, k in ipairs({"title_png", "artist_png", "art"}) do
      if state.media and state.media[k] then wanted[state.media[k]] = true end
    end
    for path, s in pairs(icons) do
      if not wanted[path] then
        if s then cairo_surface_destroy(s) end
        icons[path] = nil
      end
    end
    install_theme(state.theme)
    return true
  end
  return false
end

local function send(...)
  local f = io.open(CACHE .. "/commands", "a")
  if not f then return end
  local parts = {}
  for i, v in ipairs({...}) do
    parts[i] = type(v) == "number" and string.format("%.1f", v) or tostring(v)
  end
  f:write(table.concat(parts, "\t"), "\n")
  f:close()
end

---------------- THE LOOK ----------------

local LOOK = {
  glass = {radius = 14, split = false},
  solid = {radius = 12, split = false},
  metro = {radius = 0, split = true},
  pixel = {radius = 99, split = true},   -- pills
}

local function current_style()
  local s = tostring(style):lower()
  if s == "mirage" then s = state.mirage and state.mirage.style or "glass" end
  return LOOK[s] and s or "glass"
end

-- Each block's colours: its background, its ink and its accent. Metro and
-- Pixel give the blocks colours of their own, in the order Mirage's boxes
-- take them; glass and solid are one pane.
local BLOCK_METRO = {launcher = 1, tasks = 2, tray = 3, clock = 4, media = 1}
-- The tray's own icons (not the apps'), drawn in the tray's block.
local STATUS_KIND = {volume = true, network = true, battery = true, bluetooth = true, brightness = true,
                     keyboard = true, devices = true, privacy = true, notifications = true}
local BLOCK_PIXEL = {launcher = "px_primary_c", tasks = "px_surface", tray = "px_secondary_c",
                     clock = "px_tertiary_c", media = "px_primary_c"}

-- A glyph that reads on `bg`: white on a dark colour, near-black on a light one.
local function ink_on(bg)
  local light = 0.2126 * bg[1] + 0.7152 * bg[2] + 0.0722 * bg[3]
  return light > 0.55 and {0.07, 0.07, 0.09} or {1, 1, 1}
end

-- The colour chosen for a look's block in USER CONFIGURATION, or nil for
-- "auto". Glass and solid have one colour for the whole bar.
local BLOCK_SETTING = {launcher = "launcher", tasks = "apps", media = "media", tray = "tray", clock = "clock"}
local GLASS_TINT = 0.62         -- how strongly a chosen colour tints the glass
local function chosen_color(look, name)
  local key = (look == "glass" or look == "solid") and look .. "_color" or look .. "_" .. (BLOCK_SETTING[name] or "")
  local hex = tostring(_G[key] or "")
  if not hex:match("^#%x%x%x%x%x%x$") then return nil end
  return {tonumber(hex:sub(2, 3), 16) / 255, tonumber(hex:sub(4, 5), 16) / 255, tonumber(hex:sub(6, 7), 16) / 255}
end
local function glass_see_through()
  return clamp(tonumber(glass_transparency) or 0, 0, 0.9)
end

local function block_colors(look, name)
  -- A colour chosen in the settings: the glyphs on it white or near-black,
  -- whichever reads.
  local chosen = (look == "metro" or look == "pixel") and chosen_color(look, name)
                 or ((look == "glass" or look == "solid") and name ~= "launcher" and chosen_color(look, name))
  if chosen then
    local under = chosen
    -- Glass is the frosted wallpaper with the colour laid over it: what the
    -- glyphs stand on is somewhere between the two.
    if look == "glass" then under = mix(BASE.tint, chosen, GLASS_TINT) end
    local ink = ink_on(under)
    return {bg = chosen, ink = ink, accent = ink, dark = ink[1] > 0.5}
  end
  -- The app menu's button stands on a colour of its own in every look, so it
  -- never melts into the wallpaper: of the colours its look offers, the one
  -- furthest from the wallpaper behind it, which the helper picks.
  if name == "launcher" then
    local hex = state.launcher and state.launcher.color
    local bg
    if type(hex) == "string" and #hex == 7 then
      bg = {tonumber(hex:sub(2, 3), 16) / 255, tonumber(hex:sub(4, 5), 16) / 255, tonumber(hex:sub(6, 7), 16) / 255}
    elseif look == "metro" then bg = BASE.metro1
    elseif look == "pixel" then bg = BASE.px_primary
    else bg = BASE.accent end
    return {bg = bg, ink = ink_on(bg), accent = ink_on(bg), dark = ink_on(bg)[1] > 0.5}
  end
  if look == "metro" then
    local bg = BASE["metro" .. BLOCK_METRO[name]]
    return {bg = bg, ink = {1, 1, 1}, accent = {1, 1, 1}, dark = true}
  elseif look == "pixel" then
    return {bg = BASE[BLOCK_PIXEL[name]], ink = BASE.px_on, accent = BASE.px_primary,
            dark = (BASE.mode or "dark") == "dark"}
  elseif look == "solid" then
    return {bg = BASE.solid, ink = BASE.ink, accent = BASE.accent, dark = (BASE.mode or "dark") == "dark"}
  end
  return {bg = BASE.tint, ink = BASE.ink, accent = BASE.accent, dark = (BASE.mode or "dark") == "dark"}
end

---------------- DRAWING HELPERS ----------------

local font_regular, font_medium = "Noto Sans", "Noto Sans Medium"
local font_options = nil

local function prepare(cr)
  if font_options == nil then
    font_options = cairo_font_options_create()
    cairo_font_options_set_antialias(font_options, CAIRO_ANTIALIAS_GRAY)
    cairo_font_options_set_hint_style(font_options, CAIRO_HINT_STYLE_SLIGHT)
    cairo_font_options_set_hint_metrics(font_options, CAIRO_HINT_METRICS_OFF)
  end
  cairo_set_font_options(cr, font_options)
end

local function face(cr, family, size)
  cairo_select_font_face(cr, family, CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_NORMAL)
  cairo_set_font_size(cr, size)
end

local extents = cairo_text_extents_t and cairo_text_extents_t:create() or nil
local function advance(cr, s)
  if extents then
    cairo_text_extents(cr, s, extents)
    return extents.x_advance
  end
  return #s * 8
end

local function source(cr, c, alpha) cairo_set_source_rgba(cr, c[1], c[2], c[3], alpha or 1) end

-- Mirage's corner: a squircle rather than a circular arc.
local function squircle(cr, x, y, w, h, r)
  r = min(r * 1.35, w / 2, h / 2)
  if r <= 0.5 then
    cairo_new_path(cr)
    cairo_rectangle(cr, x, y, w, h)
    return
  end
  local corners = {{x + w - r, y + r, 0}, {x + w - r, y + h - r, 90},
                   {x + r, y + h - r, 180}, {x + r, y + r, 270}}
  local e = 2 / 4.2
  cairo_new_path(cr)
  local first = true
  for _, c in ipairs(corners) do
    for i = 0, 14 do
      local a = math.rad(c[3] - 90 + 90 * i / 14)
      local ca, sa = math.cos(a), math.sin(a)
      local px = c[1] + r * (ca < 0 and -1 or 1) * abs(ca) ^ (e)
      local py = c[2] + r * (sa < 0 and -1 or 1) * abs(sa) ^ (e)
      if first then cairo_move_to(cr, px, py); first = false else cairo_line_to(cr, px, py) end
    end
  end
  cairo_close_path(cr)
end

-- A capsule, rounded along its longer side.
local function pill(cr, x, y, w, h)
  cairo_new_path(cr)
  if w >= h then
    local r = h / 2
    cairo_arc(cr, x + w - r, y + r, r, -pi / 2, pi / 2)
    cairo_arc(cr, x + r, y + r, r, pi / 2, 3 * pi / 2)
  else
    local r = w / 2
    cairo_arc(cr, x + r, y + r, r, pi, 2 * pi)
    cairo_arc(cr, x + r, y + h - r, r, 0, pi)
  end
  cairo_close_path(cr)
end

local function shape(cr, look, x, y, w, h, r)
  if look == "pixel" then pill(cr, x, y, w, h) else squircle(cr, x, y, w, h, r) end
end

-- A one-colour picture (a tray symbol, a line of text the helper drew) used
-- as a stencil and filled with `c`: it then reads on any block colour, chosen
-- or not, and a preview with other colours than the panel's is right too.
local function paint_mask(cr, s, x, y, size, c, alpha)
  local w = cairo_image_surface_get_width(s)
  cairo_save(cr)
  cairo_translate(cr, round(x), round(y))
  if size and w > 0 and w ~= size then cairo_scale(cr, size / w, size / w) end
  source(cr, c, alpha or 1)
  cairo_mask_surface(cr, s, 0, 0)
  cairo_restore(cr)
end

-- An icon at `size` pixels. The helper renders them at exactly the size the
-- panel wants, so this only scales for a moment after the size changed (and
-- in previews, which borrow the running panel's icons).
local function paint_icon(cr, s, x, y, size, alpha)
  local w = cairo_image_surface_get_width(s)
  cairo_save(cr)
  cairo_translate(cr, round(x), round(y))
  if size and w > 0 and w ~= size then cairo_scale(cr, size / w, size / w) end
  cairo_set_source_surface(cr, s, 0, 0)
  cairo_paint_with_alpha(cr, alpha or 1)
  cairo_restore(cr)
end

---------------- LAYOUT ----------------
-- In the window's pixels. Across the panel it is 56 units: a bar of 44 with
-- 6 on either side, so the bar floats. Along it, the
-- app menu's button comes first (left, or top) and the clock last.

local layout, layout_key, layout_written = nil, nil, nil

-- Which edge of the screen the panel is on, from start_conky_tray (a
-- preview says so itself). Only a left or right panel is upright.
local SIDE_SET = nil
do
  local config = read_all(HERE .. "/start_conky_tray") or ""
  SIDE_SET = config:match('\nlocal position = "(%a+)"') or config:match("\nlocal position = '(%a+)'")
end
-- The window reaches this far past the screen's edge (start_conky_tray's
-- `overhang`, in the desktop's units), and the panel is drawn inside the rest.
-- With a fractional scale the screen is not a whole number of units tall
-- (1440 / 1.15 = 1252.17), the panel ends on a whole one, and the last row
-- of pixels showed the wallpaper through. Previews draw the panel alone.
local OVERHANG = 1

local function panel_side(vertical)
  local side = tostring(tray_preview_position or SIDE_SET or ""):lower()
  if vertical then return side == "right" and "right" or "left" end
  return side == "top" and "top" or "bottom"
end

local function clock_strings()
  local t = os.time()
  local hm
  if tostring(clock_format):lower() == "12h" then
    hm = os.date("%I:%M", t):gsub("^0", "") .. " " .. os.date("%p", t)
  else
    hm = os.date("%H:%M", t)
  end
  return hm, yes(show_date) and os.date(tostring(date_format), t) or nil
end

local function shown(setting)
  local v = tostring(setting):lower()
  return v == "yes" or v == "auto" or v == "true"
end

-- The tray's own icons after the apps', in the order Plasma's tray has them.
local function status_list()
  local list = {}
  -- First, where it is seen: an app using the microphone or the camera.
  if yes(show_privacy) and state.privacy then list[#list + 1] = {kind = "privacy", item = state.privacy} end
  if shown(show_devices) and state.devices then list[#list + 1] = {kind = "devices", item = state.devices} end
  if yes(show_keyboard) and state.keyboard then list[#list + 1] = {kind = "keyboard", item = state.keyboard} end
  if yes(show_bluetooth) and state.bluetooth then list[#list + 1] = {kind = "bluetooth", item = state.bluetooth} end
  if yes(show_brightness) and state.brightness then list[#list + 1] = {kind = "brightness", item = state.brightness} end
  if yes(show_volume) and state.volume then list[#list + 1] = {kind = "volume", item = state.volume} end
  if yes(show_network) and state.network then list[#list + 1] = {kind = "network", item = state.network} end
  local b = tostring(show_battery):lower()
  if state.battery and (b == "auto" or b == "yes") then list[#list + 1] = {kind = "battery", item = state.battery} end
  -- Last, as in Plasma's tray: the bell.
  if yes(show_notifications) and state.notifications then
    list[#list + 1] = {kind = "notifications", item = state.notifications}
  end
  return list
end

local function words(text)
  local out = {}
  for w in tostring(text or ""):gmatch("%S+") do out[#out + 1] = w end
  return out
end

-- How the song playing fits the room left between the apps and the tray, in
-- content units u: its cover, its title over its artist, and three buttons
-- (previous, play or pause, next). As the room shrinks it drops the artist,
-- then fades the title's end, then is the three buttons alone, then play or
-- pause alone, then goes. An upright panel has the buttons, one above another.
local MEDIA_BUTTONS = {"previous", "toggle", "next"}
local VIZ_BARS, VIZ_STEP = 14, 4.5      -- the sound bars: how many, and a bar and its gap in units
local function media_fit(media, room, L)
  local u = L.u
  local cell, pad = round(26 * u), round(4 * u)
  local function only(buttons, c)
    local one = #buttons == 1
    local len = one and L.bh or #buttons * c + 2 * pad
    if room < len then return nil end
    return {mode = "buttons", len = len, buttons = buttons, cell = one and L.bh or c, first = one and 0 or pad}
  end
  if L.vertical or not media.title_png then
    return only(MEDIA_BUTTONS, L.vertical and round(32 * u) or cell) or only({"toggle"})
  end
  local art = media.art ~= nil
  local lead = round((art and 7 + 30 + 10 or 14) * u)
  local fixed = lead + round(6 * u) + 3 * cell + pad
  local cap = round(330 * u)
  local title = min(media.title_w or 0, cap)
  local both = max(title, min(media.artist_w or 0, cap))
  -- The sound bars, between the title and the buttons, go first when the
  -- room is short, before the artist.
  local viz = yes(show_visualizer) and round(VIZ_BARS * VIZ_STEP * u + 10 * u) or 0
  local natural = media.artist_png and both or title
  local fit
  if viz > 0 and room >= fixed + natural + viz then
    fit = {mode = media.artist_png and "two" or "one", tw = natural, viz = viz}
  elseif media.artist_png and room >= fixed + both then fit = {mode = "two", tw = both}
  elseif room >= fixed + title then fit = {mode = "one", tw = title}
  elseif room >= fixed + round(70 * u) then fit = {mode = "one", tw = room - fixed}
  else return only(MEDIA_BUTTONS, cell) or only({"toggle"}) end
  fit.len, fit.art, fit.lead, fit.buttons, fit.cell = fixed + fit.tw + (fit.viz or 0), art, lead, MEDIA_BUTTONS, cell
  fit.first = fit.len - 3 * cell - pad
  return fit
end

-- `win` is the whole window (w, h) and where the drawn part starts in it
-- (ox, oy): W x H is the part on the screen.
local function compute_layout(cr, W, H, win)
  local look = current_style()
  local vertical = H > W
  local thick, length = vertical and W or H, vertical and H or W
  local u = thick / 56
  local m = round(6 * u)
  local bh = thick - 2 * m                  -- the bar, across
  local gap = round(6 * u)
  local pad = round(4 * u)
  local L = {W = W, H = H, u = u, look = look, vertical = vertical, side = panel_side(vertical),
             across = m, bh = bh, hits = {}, blocks = {}}
  win = win or {w = W, h = H, ox = 0, oy = 0}
  L.win_w, L.win_h, L.ox, L.oy = win.w, win.h, win.ox, win.oy
  -- Across, the whole window, past the screen's edge too: what attached
  -- backgrounds cover.
  local edge_from = vertical and -win.ox or -win.oy
  local edge_to = vertical and win.w - win.ox or win.h - win.oy
  -- A window reaches the panel's edge (one is maximised): the panel stops
  -- floating. Only the backgrounds change, so nothing on it moves.
  L.attached = state.attached == true or tray_preview_attached == true
  L.radius = L.attached and 0 or LOOK[look].radius * u
  L.split = LOOK[look].split
  L.task_cell, L.task_icon = round(46 * u), round(30 * u)
  L.tray_cell, L.tray_icon = round(32 * u), round(20 * u)
  L.launcher_icon = round(26 * u)

  -- A box running from `a` for `len` along the bar, the bar's full width across.
  local function box(a, len)
    if vertical then return {x = m, y = a, w = bh, h = len} end
    return {x = a, y = m, w = len, h = bh}
  end
  local function add(list, a, len, fields)
    local r = box(a, len)
    for k, v in pairs(fields) do r[k] = v end
    r.cx, r.cy = r.x + r.w / 2, r.y + r.h / 2
    list[#list + 1] = r
    return r
  end

  local at = m
  if yes(show_launcher) then
    add(L.blocks, at, bh, {name = "launcher"})
    L.launcher_hit = add(L.hits, at, bh, {kind = "launcher", id = "launcher"})
    at = at + bh + gap
  end
  local n = yes(show_tasks) and #state.tasks or 0
  local free_from = at                      -- where the room after the apps begins
  if n > 0 then
    add(L.blocks, at, 2 * pad + n * L.task_cell, {name = "tasks"})
    for i, t in ipairs(state.tasks) do
      add(L.hits, at + pad + (i - 1) * L.task_cell, L.task_cell, {kind = "task", id = t.key, task = t})
    end
    free_from = at + 2 * pad + n * L.task_cell + gap
  end

  -- From the far end: the clock, then the tray and the status icons. On an
  -- upright panel the clock breaks onto short lines.
  local far = length - m
  if yes(show_clock) then
    local hm, date = clock_strings()
    local clock = {hm = hm, date = date}
    local clen
    if vertical then
      clock.time_lines, clock.date_lines = words(hm), date and words(date) or {}
      clen = round((12 + 17 * #clock.time_lines + 13 * #clock.date_lines + 8) * u)
    else
      face(cr, font_medium, 14.5 * u)
      local tw = advance(cr, hm)
      if date then face(cr, font_regular, 10.5 * u); tw = max(tw, advance(cr, date)) end
      clen = round(tw + 2 * 14 * u)
    end
    add(L.blocks, far - clen, clen, {name = "clock"})
    add(L.hits, far - clen, clen, {kind = "clock", id = "clock"})
    L.clock = clock
    far = far - clen - gap
  end
  local status = status_list()
  local tray = yes(show_tray) and state.tray or {}
  local count = #tray + #status
  local free_to = far                       -- and where it ends, before the tray
  if count > 0 then
    local len = 2 * pad + count * L.tray_cell
    local start = far - len
    free_to = start - gap
    add(L.blocks, start, len, {name = "tray"})
    local c = start + pad
    for _, t in ipairs(tray) do
      add(L.hits, c, L.tray_cell, {kind = "tray", id = t.id, tray = t})
      c = c + L.tray_cell
    end
    for _, st in ipairs(status) do
      local hit = add(L.hits, c, L.tray_cell, {kind = st.kind, id = st.kind, status = st.item})
      if st.kind == "devices" then L.devices_hit = hit end
      c = c + L.tray_cell
    end
  end
  -- What is playing, in the middle of the panel when there is room there,
  -- else as near it as the room allows.
  local media = yes(show_media) and state.media
  if media then
    local fit = media_fit(media, free_to - free_from, L)
    if fit then
      local start = clamp(round(length / 2 - fit.len / 2), free_from, free_to - fit.len)
      add(L.blocks, start, fit.len, {name = "media"})
      fit.text_x = start + (fit.lead or 0)
      if fit.viz then fit.viz_x = fit.text_x + fit.tw + round(8 * L.u) end
      -- The box (its cover and title bring the player up), and its buttons
      -- after it: hit_at looks from the last, so a click finds a button first.
      L.media_hit = add(L.hits, start, fit.len, {kind = "media", id = "show", media = media, fit = fit})
      for i, id in ipairs(fit.buttons) do
        add(L.hits, start + fit.first + (i - 1) * fit.cell, fit.cell, {kind = "media", id = id, media = media, fit = fit})
      end
    end
  end
  -- Glass and solid are one pane from end to end; attached, the whole window.
  if not L.split then
    L.panes = {L.attached and {x = -win.ox, y = -win.oy, w = win.w, h = win.h} or box(m, length - 2 * m)}
  elseif L.attached then
    -- Metro and Pixel attached: the blocks close up and reach the screen's
    -- edge, the apps' block across the empty middle to the tray (what is
    -- playing sits on it), so nothing is see-through.
    local function along(b) return vertical and b.y or b.x end
    local order, apps = {}, false
    for _, b in ipairs(L.blocks) do
      if b.name ~= "media" then order[#order + 1] = b end
      if b.name == "tasks" then apps = true end
    end
    if not apps then
      local after = L.launcher_hit and along(L.launcher_hit) + bh + gap or 0
      order[#order + 1] = vertical and {x = 0, y = after, name = "tasks"} or {x = after, y = 0, name = "tasks"}
    end
    table.sort(order, function(a, b) return along(a) < along(b) end)
    L.panes = {}
    for i, b in ipairs(order) do
      local a0 = i == 1 and 0 or along(b)
      local a1 = order[i + 1] and along(order[i + 1]) or length
      if a1 > a0 then
        L.panes[#L.panes + 1] = vertical and {x = edge_from, y = a0, w = edge_to - edge_from, h = a1 - a0, block = b.name}
                                         or {x = a0, y = edge_from, w = a1 - a0, h = edge_to - edge_from, block = b.name}
      end
    end
  else
    L.panes = {}
    for _, b in ipairs(L.blocks) do
      L.panes[#L.panes + 1] = {x = b.x, y = b.y, w = b.w, h = b.h, block = b.name}
    end
  end
  for _, p in ipairs(L.panes) do p.key = string.format("%d,%d,%d,%d", p.x, p.y, p.w, p.h) end
  -- Attached, what is playing sits on the apps' block and wears its colours.
  L.media_block = (L.attached and L.split) and "tasks" or "media"
  -- Glass is cut for the whole window, square, whether the panel floats or
  -- not: floating, it is clipped to the bar's shape here. So letting go of the
  -- edge needs no new glass, and is instant.
  L.glass_key = string.format("%d,%d,%d,%d", 0, 0, win.w, win.h)
  return L
end

-- What the helper needs to know: the size, the panes to frost, the icon
-- sizes and colours, and the menus' colours.
local function write_layout(L)
  if tray_preview then return end           -- a preview must not steer the running panel's helper
  local tiles = {}
  if L.look == "glass" or L.look == "solid" then tiles[1] = L.glass_key end
  local tasks = block_colors(L.look, "tasks")
  local tray = block_colors(L.look, "tray")
  local menu = block_colors(L.look, "tasks")
  local menu_bg
  if L.look == "glass" then
    menu_bg = menu.dark and mix(menu.bg, BASE.shade or {0, 0, 0}, 0.45) or mix(menu.bg, {1, 1, 1}, 0.55)
  else
    menu_bg = menu.bg
  end
  local lines = {
    "pid=" .. MY_PID,
    string.format("size=%dx%d", L.win_w, L.win_h),
    "look=" .. L.look,
    "side=" .. L.side,
    L.launcher_hit and string.format("launcher_rect=%d,%d,%d,%d", L.launcher_hit.x + L.ox, L.launcher_hit.y + L.oy,
                                     L.launcher_hit.w, L.launcher_hit.h) or "launcher_rect=",
    L.devices_hit and string.format("devices_rect=%d,%d,%d,%d", L.devices_hit.x + L.ox, L.devices_hit.y + L.oy,
                                    L.devices_hit.w, L.devices_hit.h) or "devices_rect=",
    "height=" .. L.win_h,
    string.format("ds=%.4f", L.ds or 1),
    string.format("scale=%.4f", L.u),
    "tiles=" .. table.concat(tiles, ";"),
    "radius=0.00",
    "task_icon=" .. L.task_icon, "tray_icon=" .. L.tray_icon, "launcher_icon=" .. L.launcher_icon,
    "launcher_name=" .. tostring(launcher_icon),
    "ink=" .. color_hex(tasks.ink), "tray_ink=" .. color_hex(tray.ink), "accent=" .. color_hex(tray.accent),
    "launcher_ink=" .. color_hex(block_colors(L.look, "launcher").ink),
    "media_ink=" .. color_hex(block_colors(L.look, L.media_block).ink),
    "visualizer=" .. ((L.media_hit and L.media_hit.fit.viz) and "1" or "0"),
    "menu_bg=" .. color_css(menu_bg, 0.97), "menu_fg=" .. color_css(menu.ink),
    "menu_muted=" .. color_css(menu.ink, 0.55), "menu_edge=" .. color_css(menu.ink, 0.14),
    "menu_hover=" .. color_css(menu.ink, 0.12), "menu_accent=" .. color_css(menu.accent),
    string.format("menu_radius=%d", L.look == "metro" and 0 or L.look == "pixel" and 18 or 12),
  }
  local text = table.concat(lines, "\n") .. "\n"
  if text == layout_written then return end
  layout_written = text
  local f = io.open(CACHE .. "/layout.tmp", "w")
  if not f then return end
  f:write(text)
  f:close()
  os.rename(CACHE .. "/layout.tmp", CACHE .. "/layout")
end

---------------- DRAWING ----------------

local hover = nil               -- the hit under the pointer
local pressed, pressed_at = nil, 0
-- This frame's time, read once. Declared here, above the drawing that uses
-- it: declared below, the drawing saw a global of that name, nil, and every
-- frame failed as soon as something had been clicked.
local frame_now = 0

local function draw_glyph_launcher(cr, cx, cy, size, c)
  -- Four rounded squares, for when there is no icon.
  local s = size * 0.36
  local g = size * 0.12
  source(cr, c, 0.9)
  for _, d in ipairs({{-1, -1}, {1, -1}, {-1, 1}, {1, 1}}) do
    squircle(cr, cx + (d[1] < 0 and -s - g / 2 or g / 2), cy + (d[2] < 0 and -s - g / 2 or g / 2), s, s, s * 0.3)
    cairo_fill(cr)
  end
end

local function cell_highlight(cr, L, h, colors, alpha)
  local inset = round(3 * L.u)
  local x, y, w, hh = h.x + inset, h.y + inset, h.w - 2 * inset, h.h - 2 * inset
  if L.look == "pixel" then pill(cr, x, y, w, hh)
  else squircle(cr, x, y, w, hh, L.look == "metro" and 0 or 8 * L.u) end
  source(cr, colors.ink, alpha)
  cairo_fill(cr)
end

-- How far into the bar the marks under open apps sit, from the side that
-- faces the screen's edge, and which way an icon steps away from them.
local function outer(L)
  local near = L.side == "top" or L.side == "left"
  local edge = near and (L.across + round(3 * L.u)) or (L.across + L.bh - round(5.5 * L.u))
  return edge, near and 1 or -1
end

-- A mark along the bar, `along` from the cell's centre, `len` long.
local function mark(cr, L, h, along, len)
  local edge = outer(L)
  local thick = max(2, round(2.5 * L.u))
  if L.vertical then pill(cr, edge, h.cy + along - len / 2, thick, len)
  else pill(cr, h.cx + along - len / 2, edge, len, thick) end
  cairo_fill(cr)
end

-- Play, or pause while playing: a triangle, or two bars.
local function draw_play_glyph(cr, L, cx, cy, size, playing, c)
  source(cr, c, 0.92)
  if playing then
    local bw = round(size * 0.32)
    for _, x in ipairs({cx - size * 0.42, cx + size * 0.1}) do
      if L.look == "metro" then cairo_new_path(cr); cairo_rectangle(cr, round(x), round(cy - size / 2), bw, round(size))
      else pill(cr, round(x), round(cy - size / 2), bw, round(size)) end
      cairo_fill(cr)
    end
  else
    cairo_new_path(cr)
    cairo_move_to(cr, cx - size * 0.36, cy - size * 0.52)
    cairo_line_to(cr, cx + size * 0.56, cy)
    cairo_line_to(cr, cx - size * 0.36, cy + size * 0.52)
    cairo_close_path(cr)
    cairo_fill(cr)
  end
end

-- Previous or next: a bar and a triangle pointing past it.
local function draw_skip_glyph(cr, L, cx, cy, size, forward, c, alpha)
  source(cr, c, alpha)
  local d = forward and 1 or -1
  cairo_new_path(cr)
  cairo_move_to(cr, cx - d * size * 0.48, cy - size * 0.5)
  cairo_line_to(cr, cx + d * size * 0.30, cy)
  cairo_line_to(cr, cx - d * size * 0.48, cy + size * 0.5)
  cairo_close_path(cr)
  cairo_fill(cr)
  local bw = max(1.5, size * 0.17)
  local bx = forward and cx + size * 0.32 or cx - size * 0.32 - bw
  if L.look == "metro" then cairo_new_path(cr); cairo_rectangle(cr, bx, cy - size * 0.5, bw, size)
  else pill(cr, bx, cy - size * 0.5, bw, size) end
  cairo_fill(cr)
end

local function draw_media_button(cr, L, h, colors, is_hover)
  local m = h.media
  local can = (h.id ~= "previous" or m.can_prev ~= false) and (h.id ~= "next" or m.can_next ~= false)
  if is_hover and can then cell_highlight(cr, L, h, colors, 0.12) end
  if h.id == "toggle" then
    draw_play_glyph(cr, L, h.cx, h.cy, 12 * L.u, m.playing, colors.ink)
  else
    -- One the player does not offer now (no next video) is drawn faint.
    draw_skip_glyph(cr, L, h.cx, h.cy, 10 * L.u, h.id == "next", colors.ink, can and 0.92 or 0.3)
  end
end

-- A line of text the helper drew (Pango finds a font for any script), fading
-- out at its end when it is longer than `room`.
local function draw_text_png(cr, s, x, y, room, c, alpha, u)
  if not s then return end
  local w, h = cairo_image_surface_get_width(s), cairo_image_surface_get_height(s)
  x, y = round(x), round(y)
  cairo_save(cr)
  if w <= room + 0.5 then
    source(cr, c, alpha)
    cairo_mask_surface(cr, s, x, y)
  else
    cairo_new_path(cr)
    cairo_rectangle(cr, x, y, room, h)
    cairo_clip(cr)
    cairo_push_group(cr)
    source(cr, c, 1)
    cairo_mask_surface(cr, s, x, y)
    cairo_pop_group_to_source(cr)
    local fade = min(26 * u, room / 3)
    local g = cairo_pattern_create_linear(x + room - fade, 0, x + room, 0)
    cairo_pattern_add_color_stop_rgba(g, 0, 0, 0, 0, alpha)
    cairo_pattern_add_color_stop_rgba(g, 1, 0, 0, 0, 0)
    cairo_mask(cr, g)
    cairo_pattern_destroy(g)
  end
  cairo_restore(cr)
end

local function draw_media(cr, L, h, colors, is_hover)
  local m, fit, u = h.media, h.fit, L.u
  local playing = m.playing
  if fit.mode == "buttons" then return end
  -- The cover and the title bring the player up: lit under the pointer up
  -- to where the buttons start.
  if is_hover then
    local sub = {x = h.x, y = h.y, w = h.w, h = h.h}
    if L.vertical then sub.h = fit.first else sub.w = fit.first end
    cell_highlight(cr, L, sub, colors, 0.07)
  end
  if fit.art then
    local s = icon(m.art)
    local size = round(30 * u)
    if s then
      local x, y = h.x + round(7 * u), round(h.cy - size / 2)
      cairo_save(cr)
      if L.look == "pixel" then pill(cr, x, y, size, size)
      else squircle(cr, x, y, size, size, L.look == "metro" and 0 or 5 * u) end
      cairo_clip(cr)
      paint_icon(cr, s, x, y, size, playing and 1 or 0.7)
      cairo_restore(cr)
    end
  end
  -- Two lines sit a little high, leaving the edge to the progress line.
  local two = fit.mode == "two"
  local base = two and h.cy - 2.5 * u or h.cy + 4 * u
  draw_text_png(cr, icon(m.title_png), fit.text_x, base - (m.title_base or 0), fit.tw, colors.ink,
                playing and 0.96 or 0.72, u)
  if two then
    draw_text_png(cr, icon(m.artist_png), fit.text_x, h.cy + 11 * u - (m.artist_base or 0), fit.tw, colors.ink,
                  playing and 0.62 or 0.46, u)
  end
end

-- How far into the song: a line along the edge the marks under open apps
-- use, under the title. Drawn every frame over the cached panel, since it moves.
local function draw_progress(cr, L)
  local h = L.media_hit
  if not h or h.fit.mode == "buttons" then return end
  local m = h.media
  local length = tonumber(m.length) or 0
  if length <= 0 then return end          -- a stream has no end to measure against
  local pos = tonumber(m.pos) or 0
  if m.playing then pos = pos + max(0, frame_now - (tonumber(m.at) or frame_now)) * (tonumber(m.rate) or 1) end
  local frac = clamp(pos / length, 0, 1)
  local colors = block_colors(L.look, L.media_block)
  local edge = outer(L)
  local thick = max(2, round(2 * L.u))
  local x, w = h.fit.text_x, h.fit.tw
  pill(cr, x, edge, w, thick)
  source(cr, colors.ink, 0.18)
  cairo_fill(cr)
  if frac > 0 then
    pill(cr, x, edge, max(thick, round(w * frac)), thick)
    source(cr, colors.accent, 0.95)
    cairo_fill(cr)
  end
end

-- The sound bars. The helper writes the levels of a few bands of what is
-- playing 25 times a second (to a file in the runtime folder, a RAM disk);
-- each frame eases towards them, quick to rise and slower to fall, so the
-- bars move smoothly between readings. Paused, or with no reading for a
-- moment, they settle into dots.
local VIZ_LEVELS = (os.getenv("XDG_RUNTIME_DIR") or CACHE) .. "/conky-tray-levels"
local viz_shown, viz_frame = {}, 0
local function viz_levels()
  if tray_preview then                   -- a still picture of bars, for the previews
    local out = {}
    for i = 1, VIZ_BARS do out[i] = 0.25 + 0.6 * abs(math.sin(i * 0.83)) * (1 - i / (VIZ_BARS * 1.6)) end
    return out
  end
  local line = read_first_line(VIZ_LEVELS)
  if not line then return nil end
  local values = {}
  for v in line:gmatch("%S+") do values[#values + 1] = tonumber(v) end
  if not values[1] or frame_now - values[1] > 0.6 then return nil end
  table.remove(values, 1)
  return values
end

local function draw_visualizer(cr, L)
  local h = L.media_hit
  if not h or not h.fit.viz_x then return end
  local m = h.media
  local target = m.playing and viz_levels() or nil
  local dt = viz_frame > 0 and clamp(frame_now - viz_frame, 0, 0.25) or 0.04
  viz_frame = frame_now
  for i = 1, VIZ_BARS do
    local want, have = target and clamp(target[i] or 0, 0, 1) or 0, viz_shown[i] or 0
    local tau = want > have and 0.035 or 0.14
    viz_shown[i] = have + (want - have) * (1 - math.exp(-dt / tau))
  end
  if tray_preview then viz_shown = target or viz_levels() end
  local u, colors = L.u, block_colors(L.look, L.media_block)
  local bw = max(2, round(2.6 * u))
  local step = VIZ_STEP * u
  local tallest = 24 * u
  source(cr, colors.accent, m.playing and 0.92 or 0.5)
  for i = 1, VIZ_BARS do
    local x = round(h.fit.viz_x + (i - 1) * step)
    local bar = max(bw, (viz_shown[i] or 0) * tallest)
    local y = h.cy - bar / 2
    if L.look == "metro" then cairo_new_path(cr); cairo_rectangle(cr, x, round(y), bw, round(bar))
    else pill(cr, x, y, bw, bar) end
    cairo_fill(cr)
  end
end

local function draw_panel(cr, L)
  prepare(cr)
  local look = L.look
  -- No shadow under the bar: Mirage's needs far more room to fade out than
  -- the few pixels round the bar, and cut off at the window's edge it showed
  -- as a dark band with straight edges. The bar's own edge line is enough.
  -- The panes.
  for _, p in ipairs(L.panes) do
    local colors = block_colors(look, p.block or "tasks")
    local g = glass[L.glass_key]
    local function outline()
      if L.attached then cairo_new_path(cr); cairo_rectangle(cr, p.x, p.y, p.w, p.h)
      else shape(cr, look, p.x, p.y, p.w, p.h, L.radius) end
    end
    local see = look == "glass" and glass_see_through() or 0
    local tint = look == "glass" and chosen_color("glass")
    if look == "glass" and g then
      cairo_save(cr)
      outline()
      cairo_clip(cr)
      cairo_set_source_surface(cr, g, -L.ox, -L.oy)
      cairo_paint_with_alpha(cr, 1 - see)
      if tint then
        source(cr, tint, GLASS_TINT * (1 - see))
        cairo_paint(cr)
      end
      cairo_restore(cr)
      -- The glass's own hairline runs round the window; floating, it is
      -- clipped away, so the bar gets one of its own.
      if not L.attached then
        outline()
        source(cr, colors.ink, 0.12)
        cairo_set_line_width(cr, 1)
        cairo_stroke(cr)
      end
    else
      outline()
      source(cr, colors.bg, look == "glass" and 0.62 * (1 - see) or 1)
      if look == "glass" or look == "solid" then
        cairo_fill_preserve(cr)
        source(cr, colors.ink, look == "glass" and 0.12 or 0.08)
        cairo_set_line_width(cr, 1)
        cairo_stroke(cr)
      else
        cairo_fill(cr)
      end
    end
  end
  -- What is on them.
  for _, h in ipairs(L.hits) do
    local block = STATUS_KIND[h.kind] and "tray" or (h.kind == "task" and "tasks")
                  or (h.kind == "media" and L.media_block) or h.kind
    local colors = block_colors(look, block)
    local cx, cy = h.cx, h.cy
    local is_hover = hover and hover.kind == h.kind and hover.id == h.id
    if h.kind == "launcher" then
      -- On one pane (glass, solid) the button gets its own tile; in Metro and
      -- Pixel its block already has the colour.
      if not L.split then
        local inset = round(4 * L.u)
        squircle(cr, h.x + inset, h.y + inset, h.w - 2 * inset, h.h - 2 * inset, 9 * L.u)
        source(cr, colors.bg, 1)
        cairo_fill(cr)
      end
      if is_hover then cell_highlight(cr, L, h, {ink = colors.ink}, 0.14) end
      local s = icon(state.launcher and state.launcher.icon)
      if s and state.launcher.symbolic then
        paint_mask(cr, s, cx - L.launcher_icon / 2, cy - L.launcher_icon / 2, L.launcher_icon, colors.ink)
      elseif s then paint_icon(cr, s, cx - L.launcher_icon / 2, cy - L.launcher_icon / 2, L.launcher_icon)
      else draw_glyph_launcher(cr, cx, cy, L.launcher_icon, colors.ink) end
    elseif h.kind == "task" then
      local t = h.task
      if t.attention then cell_highlight(cr, L, h, {ink = BASE.bad or {1, 0.5, 0.5}}, 0.30)
      elseif t.active then cell_highlight(cr, L, h, colors, 0.16)
      elseif is_hover then cell_highlight(cr, L, h, colors, 0.09) end
      local s = icon(t.icon)
      -- The icon steps away from the marks by two units.
      local _, away = outer(L)
      local ix, iy = cx - L.task_icon / 2, cy - L.task_icon / 2
      if L.vertical then ix = ix + away * round(2 * L.u) else iy = iy + away * round(2 * L.u) end
      if s then
        local alpha = t.starting and 0.45 or t.minimized and 0.6 or 1
        paint_icon(cr, s, ix, iy, L.task_icon, alpha)
      end
      -- By an open app, a line on the side facing the screen's edge: longer
      -- for the one in front, two dots for an app with several windows.
      local n = t.windows or 0
      if n > 0 then
        source(cr, colors.accent, t.minimized and 0.5 or 1)
        if t.active then
          mark(cr, L, h, 0, 18 * L.u)
        elseif n > 1 then
          mark(cr, L, h, -3.75 * L.u, 4.5 * L.u)
          mark(cr, L, h, 3.75 * L.u, 4.5 * L.u)
        else
          mark(cr, L, h, 0, 6 * L.u)
        end
      end
    elseif h.kind == "tray" or STATUS_KIND[h.kind] then
      local item = h.tray or h.status
      if item.attention then cell_highlight(cr, L, h, {ink = BASE.bad or {1, 0.5, 0.5}}, 0.30)
      elseif is_hover then cell_highlight(cr, L, h, colors, 0.10) end
      if h.kind == "keyboard" then
        -- The layout as its short name, as Plasma's tray writes it.
        local room = min(h.w, h.h) - 4 * L.u
        face(cr, font_medium, 12 * L.u)
        local w = advance(cr, item.label or "")
        if w > room then face(cr, font_medium, 12 * L.u * room / w); w = room end
        source(cr, colors.ink, 0.92)
        cairo_move_to(cr, round(cx - w / 2), round(cy + 4.3 * L.u))
        cairo_show_text(cr, item.label or "")
      else
        local s = icon(item.icon)
        if s and (STATUS_KIND[h.kind] or (h.tray and h.tray.symbolic)) then
          paint_mask(cr, s, cx - L.tray_icon / 2, cy - L.tray_icon / 2, L.tray_icon, colors.ink)
        elseif s then paint_icon(cr, s, cx - L.tray_icon / 2, cy - L.tray_icon / 2, L.tray_icon) end
      end
      if h.kind == "privacy" or (h.kind == "notifications" and (item.unread or 0) > 0) then
        -- The red dot that means recording, on any background: ringed in
        -- the block's own colour so it stands off the icon. The bell's,
        -- for notifications not yet seen, is in the accent colour.
        local r = 3.6 * L.u
        cairo_new_path(cr)
        cairo_arc(cr, cx + 6.5 * L.u, cy - 6.5 * L.u, r + 1.6 * L.u, 0, 2 * pi)
        source(cr, colors.bg, 1)
        cairo_fill(cr)
        cairo_arc(cr, cx + 6.5 * L.u, cy - 6.5 * L.u, r, 0, 2 * pi)
        source(cr, h.kind == "privacy" and {0.94, 0.22, 0.24} or colors.accent, 1)
        cairo_fill(cr)
      end
    elseif h.kind == "media" then
      if h.id == "show" then draw_media(cr, L, h, colors, is_hover)
      else draw_media_button(cr, L, h, colors, is_hover) end
    elseif h.kind == "clock" then
      if is_hover then cell_highlight(cr, L, h, colors, 0.08) end
      local c = L.clock
      source(cr, colors.ink, 0.96)
      if L.vertical then
        -- Short lines, each centred and shrunk to fit the bar if it must.
        local room = h.w - 6 * L.u
        local y = h.y + 12 * L.u
        for _, line in ipairs(c.time_lines) do
          face(cr, font_medium, 14.5 * L.u)
          local w = advance(cr, line)
          if w > room then face(cr, font_medium, 14.5 * L.u * room / w); w = room end
          y = y + 14 * L.u
          cairo_move_to(cr, round(cx - w / 2), round(y))
          cairo_show_text(cr, line)
          y = y + 3 * L.u
        end
        source(cr, colors.ink, 0.62)
        for _, line in ipairs(c.date_lines) do
          face(cr, font_regular, 10.5 * L.u)
          local w = advance(cr, line)
          if w > room then face(cr, font_regular, 10.5 * L.u * room / w); w = room end
          y = y + 13 * L.u
          cairo_move_to(cr, round(cx - w / 2), round(y))
          cairo_show_text(cr, line)
        end
      elseif c.date then
        face(cr, font_medium, 14.5 * L.u)
        local w = advance(cr, c.hm)
        cairo_move_to(cr, round(cx - w / 2), round(h.y + h.h * 0.50))
        cairo_show_text(cr, c.hm)
        face(cr, font_regular, 10.5 * L.u)
        source(cr, colors.ink, 0.62)
        w = advance(cr, c.date)
        cairo_move_to(cr, round(cx - w / 2), round(h.y + h.h * 0.80))
        cairo_show_text(cr, c.date)
      else
        face(cr, font_medium, 15.5 * L.u)
        local w = advance(cr, c.hm)
        cairo_move_to(cr, round(cx - w / 2), round(cy + 5.5 * L.u))
        cairo_show_text(cr, c.hm)
      end
    end
    -- A press shows for a moment.
    if pressed and pressed.kind == h.kind and pressed.id == h.id and frame_now - pressed_at < 0.18 then
      cell_highlight(cr, L, h, colors, 0.14)
    end
  end
end

local cache, cache_key, cache_w, cache_h = nil, nil, 0, 0
local frame_fast = nil          -- drawing 25 frames a second for the sound bars
local old_cache, fade_start = nil, nil
local FADE = 0.8


local function frame_key(L)
  local h = hover and (hover.kind .. ":" .. tostring(hover.id)) or ""
  local p = pressed and (frame_now - pressed_at < 0.18) and (pressed.kind .. ":" .. tostring(pressed.id)) or ""
  return L.key .. "|" .. h .. "|" .. p
end

-- How hard conky works, checked every 10s: if it ever uses more than a
-- sliver of a core, it says so in diag.log, with how often it drew.
local diag_at, diag_clock, diag_frames = nil, 0, 0
local function diagnose(now)
  diag_frames = diag_frames + 1
  if diag_at == nil then diag_at, diag_clock = now, os.clock() return end
  if now - diag_at < 10 then return end
  local share = (os.clock() - diag_clock) / (now - diag_at)
  if share > 0.10 and not tray_preview then
    local f = io.open(CACHE .. "/diag.log", "a")
    if f then
      f:write(string.format("%s conky used %d%% of a core over %ds, drawing %d times\n",
                            os.date("%H:%M:%S"), math.floor(share * 100), math.floor(now - diag_at), diag_frames))
      f:close()
    end
  end
  diag_at, diag_clock, diag_frames = now, os.clock(), 0
end

local function draw_frame()
  if conky_window == nil then return end
  local surface, owns = conky_window_surface()
  if not surface then return end
  local W, H = conky_window.width, conky_window.height
  if cairo_surface_get_type(surface) == CAIRO_SURFACE_TYPE_IMAGE then
    W, H = cairo_image_surface_get_width(surface), cairo_image_surface_get_height(surface)
  end
  if W < 20 or H < 10 then return end
  local ds = W / max(1, conky_window.width)
  local now = now_seconds()
  frame_now = now
  diagnose(now)
  poll(now)

  local cr = cairo_create(surface)
  -- conky scales its surface to the desktop's scale; everything here is
  -- drawn in real pixels instead, so lines and icons land on whole pixels.
  cairo_scale(cr, 1 / ds, 1 / ds)
  -- The layout is worked out again only when something it depends on
  -- changed: the size, the helper's state, the look, the minute.
  -- (The theme is the second field: a new one fades in, see below.)
  local inputs = table.concat({tostring(serial_seen), tostring(theme_key), W, H, current_style(),
                               os.date("%Y%m%d%H%M"), tostring(tray_preview_theme and tray_preview_theme.stamp)}, "|")
  if not layout or layout.inputs ~= inputs then
    -- The part of the window on the screen: all but the overhang, on the
    -- side of the screen's edge.
    local ov = tray_preview and 0 or round(OVERHANG * ds)
    local side = panel_side(H > W)
    local win = {w = W, h = H, ox = side == "left" and ov or 0, oy = side == "top" and ov or 0}
    local upright = side == "left" or side == "right"
    layout = compute_layout(cr, upright and W - ov or W, upright and H or H - ov, win)
    layout.ds, layout.inputs, layout.key = ds, inputs, inputs
    write_layout(layout)
  end
  -- A preview cuts its own glass for the panes it is drawing.
  if tray_preview then
    tray_preview_layout = layout
    if tray_preview_theme then install_theme(tray_preview_theme) end
  end

  local key = frame_key(layout)
  if cache == nil or cache_w ~= W or cache_h ~= H then
    if cache then cairo_surface_destroy(cache) end
    cache = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, W, H)
    cache_w, cache_h, cache_key = W, H, nil
  end
  if key ~= cache_key then
    -- A new look or palette fades in over the old one.
    local th = key:match("^[^|]*|([^|]*)")
    local was = cache_key and cache_key:match("^[^|]*|([^|]*)")
    if was and th ~= was and not tray_preview then
      if old_cache then cairo_surface_destroy(old_cache) end
      old_cache = cairo_image_surface_create(CAIRO_FORMAT_ARGB32, W, H)
      local c = cairo_create(old_cache)
      cairo_set_source_surface(c, cache, 0, 0)
      cairo_paint(c)
      cairo_destroy(c)
      fade_start = now
    end
    local c = cairo_create(cache)
    cairo_set_operator(c, CAIRO_OPERATOR_CLEAR)
    cairo_paint(c)
    cairo_set_operator(c, CAIRO_OPERATOR_OVER)
    cairo_translate(c, layout.ox, layout.oy)
    draw_panel(c, layout)
    cairo_destroy(c)
    cache_key = key
  end

  cairo_set_operator(cr, CAIRO_OPERATOR_CLEAR)
  cairo_paint(cr)
  local f = fade_start and clamp((now - fade_start) / FADE, 0, 1) or 1
  if old_cache and f < 1 then
    f = f * f * (3 - 2 * f)
    -- Adding the two, weighted, is an exact crossfade even where both are
    -- see-through.
    cairo_set_operator(cr, CAIRO_OPERATOR_ADD)
    cairo_set_source_surface(cr, old_cache, 0, 0)
    cairo_paint_with_alpha(cr, 1 - f)
    cairo_set_source_surface(cr, cache, 0, 0)
    cairo_paint_with_alpha(cr, f)
  else
    if old_cache then cairo_surface_destroy(old_cache); old_cache, fade_start = nil, nil end
    cairo_set_operator(cr, CAIRO_OPERATOR_OVER)
    cairo_set_source_surface(cr, cache, 0, 0)
    cairo_paint(cr)
  end
  cairo_set_operator(cr, CAIRO_OPERATOR_OVER)
  cairo_save(cr)
  cairo_translate(cr, layout.ox, layout.oy)
  draw_progress(cr, layout)
  draw_visualizer(cr, layout)
  cairo_restore(cr)
  -- While the bars move, 25 frames a second; else five, which is all the
  -- clock, hover and clicks need.
  local fast = layout.media_hit and layout.media_hit.fit.viz_x and state.media and state.media.playing and true or false
  if fast ~= frame_fast and not tray_preview and type(conky_set_update_interval) == "function" then
    frame_fast = fast
    conky_set_update_interval(fast and 0.04 or 0.2)
  end
  cairo_destroy(cr)
  -- Asked for a picture of what is drawn (a file named "dump" in the cache):
  -- this is the one way to see the panel, since nothing may grab the screen.
  if not tray_preview and io.open(CACHE .. "/dump", "r") then
    os.remove(CACHE .. "/dump")
    cairo_surface_write_to_png(surface, CACHE .. "/dump.png")
  end
  if owns then cairo_surface_destroy(surface) end
end

-- conky calls this for every frame. An error is written down rather than
-- left in a terminal nobody sees (the panel just goes blank), once each.
local errors_seen = {}
function conky_tray()
  local ok, err = pcall(draw_frame)
  if not ok and not errors_seen[tostring(err)] then
    errors_seen[tostring(err)] = true
    local f = io.open(CACHE .. "/errors.log", "a")
    if f then
      f:write(os.date("%H:%M:%S ") .. tostring(err) .. "\n" .. debug.traceback() .. "\n")
      f:close()
    end
  end
end

---------------- THE MOUSE ----------------

local function hit_at(x, y)
  if not layout then return nil end
  -- From the last: the media buttons lie on the media box.
  for i = #layout.hits, 1, -1 do
    local h = layout.hits[i]
    if x >= h.x and x < h.x + h.w and y >= h.y and y < h.y + h.h then return h end
  end
  return nil
end

local BUTTONS = {[1] = "left", [2] = "middle", [3] = "right"}

-- The first few mouse events go to a log, so it is easy to tell whether
-- the desktop delivers them at all.
local mouse_logged = 0
local function log_mouse(event)
  if mouse_logged >= 40 or event.type == "mouse_move" then return end
  mouse_logged = mouse_logged + 1
  local f = io.open(CACHE .. "/mouse.log", mouse_logged == 1 and "w" or "a")
  if not f then return end
  f:write(string.format("%s %s %s %s %s\n", tostring(event.type), tostring(event.button),
                        tostring(event.direction), tostring(event.x), tostring(event.y)))
  f:close()
end

-- conky calls this for the window's mouse events (lua_mouse_hook).
function conky_tray_mouse(event)
  log_mouse(event)
  if not layout then return false end
  local ds = layout.ds or 1
  local x, y = (event.x or 0) * ds - layout.ox, (event.y or 0) * ds - layout.oy
  local kind = event.type
  if kind == "mouse_move" or kind == "mouse_enter" then
    hover = hit_at(x, y)
    return false
  elseif kind == "mouse_leave" then
    hover = nil
    return false
  elseif kind == "button_down" then
    local button = BUTTONS[event.button] or tostring(event.button)
    local h = hit_at(x, y)
    if not h then
      send("close-menu")
      return true
    end
    pressed, pressed_at = h, frame_now
    -- The box clicked goes with it, so a menu can open beside it.
    -- In the window's pixels, which is what the helper places menus from.
    local hx, hy = h.x + layout.ox, h.y + layout.oy
    if h.kind == "task" or h.kind == "tray" or h.kind == "media" then
      send(h.kind, button, h.id, hx, hy, h.w, h.h)
    else
      send(h.kind, button, hx, hy, h.w, h.h)
    end
    return true
  elseif kind == "mouse_scroll" or kind == "scroll" then
    local h = hit_at(x, y)
    local direction = tostring(event.direction or "")
    if h and (h.kind == "volume" or h.kind == "tray" or h.kind == "brightness" or h.kind == "keyboard"
              or h.kind == "media") then
      send("scroll", h.kind, h.id, direction)
    end
    return true
  end
  return false
end
