--[[ ==========================================================================
  ConkyTray -- a panel along the bottom of the screen, in the look of
  Conky-Mirage: an app menu, your pinned and open apps, the tray,
  volume, network, battery and the clock.

  Start it with start_conky_tray. It needs a Wayland desktop that has the
  layer shell (KDE Plasma does).
============================================================================ ]]

--[[ USER CONFIGURATION ]]

-- The look. "mirage" wears whichever look Conky-Mirage is set to; or pick
-- "glass", "solid", "metro" or "pixel".
style = "mirage"

clock_format = "24h"          -- "24h" or "12h"
show_date    = "yes"          -- the date under the time
date_format  = "%a %d %b"     -- how the date is written, see "man strftime"

show_volume  = "yes"
show_network = "yes"
show_battery = "auto"         -- "auto" shows it when the computer has one

-- The button at the left end opens the app menu; right-click it to lock the
-- screen, log out or shut down. "auto" uses the icon of Plasma's app menu.
launcher_icon = "auto"

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
local shadow = nil

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
  if shadow then cairo_surface_destroy(shadow); shadow = nil end
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
  if th.shadow then shadow = load_png(t.dir .. "/" .. th.shadow) end
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
    for _, k in ipairs({"volume", "network", "battery", "launcher"}) do
      if state[k] and state[k].icon then wanted[state[k].icon] = true end
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
local BLOCK_METRO = {launcher = 1, tasks = 2, tray = 3, clock = 4}
local BLOCK_PIXEL = {launcher = "px_primary_c", tasks = "px_surface", tray = "px_secondary_c",
                     clock = "px_tertiary_c"}

local function block_colors(look, name)
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

local function pill(cr, x, y, w, h)
  local r = min(w, h) / 2
  cairo_new_path(cr)
  cairo_arc(cr, x + w - r, y + r, r, -pi / 2, pi / 2)
  cairo_arc(cr, x + r, y + h - r, r, pi / 2, 3 * pi / 2)
  cairo_close_path(cr)
end

local function shape(cr, look, x, y, w, h, r)
  if look == "pixel" then pill(cr, x, y, w, h) else squircle(cr, x, y, w, h, r) end
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
-- In the window's pixels. The window is 56 units tall: a bar of 44 with 6
-- above and below, so the shadow has room and the bar floats.

local layout, layout_key, layout_written = nil, nil, nil

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

local function status_list()
  local list = {}
  if yes(show_volume) and state.volume then list[#list + 1] = {kind = "volume", item = state.volume} end
  if yes(show_network) and state.network then list[#list + 1] = {kind = "network", item = state.network} end
  local b = tostring(show_battery):lower()
  if state.battery and (b == "auto" or b == "yes") then list[#list + 1] = {kind = "battery", item = state.battery} end
  return list
end

local function compute_layout(cr, W, H)
  local look = current_style()
  local u = H / 56
  local m = round(6 * u)
  local top, bh = m, H - 2 * m
  local gap = round(6 * u)
  local pad = round(4 * u)
  local L = {W = W, H = H, u = u, look = look, top = top, bh = bh, hits = {}, blocks = {}}
  L.radius = LOOK[look].radius * u
  L.split = LOOK[look].split
  L.task_cell, L.task_icon = round(46 * u), round(30 * u)
  L.tray_cell, L.tray_icon = round(32 * u), round(20 * u)
  L.launcher_icon = round(26 * u)

  local x = m
  -- The launcher.
  L.blocks[#L.blocks + 1] = {name = "launcher", x = x, w = bh}
  L.hits[#L.hits + 1] = {kind = "launcher", id = "launcher", x = x, w = bh}
  x = x + bh + gap
  -- Pinned and open apps.
  local n = #state.tasks
  if n > 0 then
    local w = 2 * pad + n * L.task_cell
    L.blocks[#L.blocks + 1] = {name = "tasks", x = x, w = w}
    for i, t in ipairs(state.tasks) do
      L.hits[#L.hits + 1] = {kind = "task", id = t.key, x = x + pad + (i - 1) * L.task_cell, w = L.task_cell, task = t}
    end
  end
  -- From the right: the clock, then the tray and the status icons.
  local hm, date = clock_strings()
  face(cr, font_medium, 14.5 * u)
  local tw = advance(cr, hm)
  if date then face(cr, font_regular, 10.5 * u); tw = max(tw, advance(cr, date)) end
  local cw = round(tw + 2 * 14 * u)
  local right = W - m
  L.blocks[#L.blocks + 1] = {name = "clock", x = right - cw, w = cw}
  L.hits[#L.hits + 1] = {kind = "clock", id = "clock", x = right - cw, w = cw}
  L.clock = {hm = hm, date = date}
  right = right - cw - gap
  local status = status_list()
  local count = #state.tray + #status
  if count > 0 then
    local w = 2 * pad + count * L.tray_cell
    local bx = right - w
    L.blocks[#L.blocks + 1] = {name = "tray", x = bx, w = w}
    local cx = bx + pad
    for _, t in ipairs(state.tray) do
      L.hits[#L.hits + 1] = {kind = "tray", id = t.id, x = cx, w = L.tray_cell, tray = t}
      cx = cx + L.tray_cell
    end
    for _, s in ipairs(status) do
      L.hits[#L.hits + 1] = {kind = s.kind, id = s.kind, x = cx, w = L.tray_cell, status = s.item}
      cx = cx + L.tray_cell
    end
  end
  -- Glass and solid are one pane from end to end.
  if not L.split then
    L.panes = {{x = m, y = top, w = W - 2 * m, h = bh}}
  else
    L.panes = {}
    for _, b in ipairs(L.blocks) do L.panes[#L.panes + 1] = {x = b.x, y = top, w = b.w, h = bh, block = b.name} end
  end
  for _, p in ipairs(L.panes) do p.key = string.format("%d,%d,%d,%d", p.x, p.y, p.w, p.h) end
  return L
end

-- What the helper needs to know: the size, the panes to frost, the icon
-- sizes and colours, and the menus' colours.
local function write_layout(L)
  if tray_preview then return end           -- a preview must not steer the running panel's helper
  local tiles = {}
  if L.look == "glass" or L.look == "solid" then
    for _, p in ipairs(L.panes) do tiles[#tiles + 1] = p.key end
  end
  local tasks = block_colors(L.look, "tasks")
  local tray = block_colors(L.look, "tray")
  local menu = block_colors(L.look, "tasks")
  local menu_bg
  if L.look == "glass" then
    menu_bg = menu.dark and mix(BASE.tint, BASE.shade or {0, 0, 0}, 0.45) or mix(BASE.tint, {1, 1, 1}, 0.55)
  else
    menu_bg = menu.bg
  end
  local lines = {
    "pid=" .. MY_PID,
    string.format("size=%dx%d", L.W, L.H),
    "look=" .. L.look,
    "height=" .. L.H,
    string.format("ds=%.4f", L.ds or 1),
    string.format("scale=%.4f", L.u),
    "tiles=" .. table.concat(tiles, ";"),
    string.format("radius=%.2f", min(L.radius, L.bh / 2)),
    "task_icon=" .. L.task_icon, "tray_icon=" .. L.tray_icon, "launcher_icon=" .. L.launcher_icon,
    "launcher_name=" .. tostring(launcher_icon),
    "ink=" .. color_hex(tasks.ink), "tray_ink=" .. color_hex(tray.ink), "accent=" .. color_hex(tray.accent),
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
  local x, y, w, hh = h.x + inset, L.top + inset, h.w - 2 * inset, L.bh - 2 * inset
  if L.look == "pixel" then pill(cr, x, y, w, hh)
  else squircle(cr, x, y, w, hh, L.look == "metro" and 0 or 8 * L.u) end
  source(cr, colors.ink, alpha)
  cairo_fill(cr)
end

local function draw_panel(cr, L)
  prepare(cr)
  local look = L.look
  -- The shadow, for the looks that have one.
  if shadow and (look == "glass" or look == "solid") then
    cairo_set_source_surface(cr, shadow, 0, 0)
    cairo_paint(cr)
  end
  -- The panes.
  for _, p in ipairs(L.panes) do
    local colors = block_colors(look, p.block or "tasks")
    local g = glass[p.key]
    if look == "glass" and g then
      cairo_set_source_surface(cr, g, p.x, p.y)
      cairo_paint(cr)
    else
      shape(cr, look, p.x, p.y, p.w, p.h, L.radius)
      source(cr, colors.bg, look == "glass" and 0.62 or 1)
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
    local block = (h.kind == "volume" or h.kind == "network" or h.kind == "battery") and "tray" or
                  (h.kind == "task" and "tasks") or h.kind
    local colors = block_colors(look, block)
    local cx, cy = h.x + h.w / 2, L.top + L.bh / 2
    local is_hover = hover and hover.kind == h.kind and hover.id == h.id
    if h.kind == "launcher" then
      if is_hover then cell_highlight(cr, L, h, colors, 0.10) end
      local s = icon(state.launcher and state.launcher.icon)
      if s then paint_icon(cr, s, cx - L.launcher_icon / 2, cy - L.launcher_icon / 2, L.launcher_icon)
      else draw_glyph_launcher(cr, cx, cy, L.launcher_icon, colors.ink) end
    elseif h.kind == "task" then
      local t = h.task
      if t.attention then cell_highlight(cr, L, h, {ink = BASE.bad or {1, 0.5, 0.5}}, 0.30)
      elseif t.active then cell_highlight(cr, L, h, colors, 0.16)
      elseif is_hover then cell_highlight(cr, L, h, colors, 0.09) end
      local s = icon(t.icon)
      local iy = cy - L.task_icon / 2 - round(2 * L.u)
      if s then
        local alpha = t.starting and 0.45 or t.minimized and 0.6 or 1
        paint_icon(cr, s, cx - L.task_icon / 2, iy, L.task_icon, alpha)
      end
      -- Under an open app, a line: longer for the one in front, two dots
      -- for an app with several windows.
      local n = t.windows or 0
      if n > 0 then
        local y, lh = L.top + L.bh - round(5.5 * L.u), max(2, round(2.5 * L.u))
        local alpha = t.minimized and 0.5 or 1
        source(cr, colors.accent, alpha)
        if t.active then
          pill(cr, cx - 9 * L.u, y, 18 * L.u, lh)
          cairo_fill(cr)
        elseif n > 1 then
          pill(cr, cx - 6 * L.u, y, 4.5 * L.u, lh); cairo_fill(cr)
          pill(cr, cx + 1.5 * L.u, y, 4.5 * L.u, lh); cairo_fill(cr)
        else
          pill(cr, cx - 3 * L.u, y, 6 * L.u, lh)
          cairo_fill(cr)
        end
      end
    elseif h.kind == "tray" or h.kind == "volume" or h.kind == "network" or h.kind == "battery" then
      local item = h.tray or h.status
      if item.attention then cell_highlight(cr, L, h, {ink = BASE.bad or {1, 0.5, 0.5}}, 0.30)
      elseif is_hover then cell_highlight(cr, L, h, colors, 0.10) end
      local s = icon(item.icon)
      if s then paint_icon(cr, s, cx - L.tray_icon / 2, cy - L.tray_icon / 2, L.tray_icon) end
    elseif h.kind == "clock" then
      if is_hover then cell_highlight(cr, L, h, colors, 0.08) end
      local c = L.clock
      source(cr, colors.ink, 0.96)
      if c.date then
        face(cr, font_medium, 14.5 * L.u)
        local w = advance(cr, c.hm)
        cairo_move_to(cr, round(cx - w / 2), round(L.top + L.bh * 0.50))
        cairo_show_text(cr, c.hm)
        face(cr, font_regular, 10.5 * L.u)
        source(cr, colors.ink, 0.62)
        w = advance(cr, c.date)
        cairo_move_to(cr, round(cx - w / 2), round(L.top + L.bh * 0.80))
        cairo_show_text(cr, c.date)
      else
        face(cr, font_medium, 15.5 * L.u)
        local w = advance(cr, c.hm)
        cairo_move_to(cr, round(cx - w / 2), round(cy + 5.5 * L.u))
        cairo_show_text(cr, c.hm)
      end
    end
    -- A press shows for a moment.
    if pressed and pressed.kind == h.kind and pressed.id == h.id and now_seconds() - pressed_at < 0.18 then
      cell_highlight(cr, L, h, colors, 0.14)
    end
  end
end

local cache, cache_key, cache_w, cache_h = nil, nil, 0, 0
local old_cache, fade_start = nil, nil
local FADE = 0.8

local function frame_key(L)
  local h = hover and (hover.kind .. ":" .. tostring(hover.id)) or ""
  local p = pressed and (now_seconds() - pressed_at < 0.18) and (pressed.kind .. ":" .. tostring(pressed.id)) or ""
  return table.concat({tostring(serial_seen), tostring(theme_key), L.look, L.W, L.H, h, p,
                       L.clock.hm, tostring(L.clock.date)}, "|")
end

function conky_tray()
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
  poll(now)

  local cr = cairo_create(surface)
  -- conky scales its surface to the desktop's scale; everything here is
  -- drawn in real pixels instead, so lines and icons land on whole pixels.
  cairo_scale(cr, 1 / ds, 1 / ds)
  layout = compute_layout(cr, W, H)
  layout.ds = ds
  write_layout(layout)
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
  cairo_destroy(cr)
  if owns then cairo_surface_destroy(surface) end
end

---------------- THE MOUSE ----------------

local function hit_at(x, y)
  if not layout then return nil end
  if y < layout.top or y > layout.top + layout.bh then return nil end
  for _, h in ipairs(layout.hits) do
    if x >= h.x and x < h.x + h.w then return h end
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
  local x, y = (event.x or 0) * ds, (event.y or 0) * ds
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
    pressed, pressed_at = h, now_seconds()
    local cx = h.x + h.w / 2
    if h.kind == "task" or h.kind == "tray" then
      send(h.kind, button, h.id, cx, layout.top)
    else
      send(h.kind, button, cx, layout.top)
    end
    return true
  elseif kind == "mouse_scroll" or kind == "scroll" then
    local h = hit_at(x, y)
    local direction = tostring(event.direction or "")
    if h and (h.kind == "volume" or h.kind == "tray") then
      send("scroll", h.kind, h.id, direction)
    end
    return true
  end
  return false
end
