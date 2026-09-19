-- Conky Calendar Extra - modernized layout
-- Based on the 2014-02-24 original by eXpander.

---------------- USER CONFIGURATION ----------------

-- How many CPU cores to show a temperature bar for; 0 means every sensor the
-- kernel publishes. There is one sensor per physical core, normally fewer than
-- the thread count htop shows.
number_of_physical_CPU_cores = 0

-- Show graphic card temperature? (Yes/No)
enable_graphic_card_temperature_sensor = "Yes"

-- Temperature a full bar represents, in degrees Celsius.
max_temperature = 100

-- Temperature at which readings start shifting from HTML_accent towards
-- HTML_warm. Anything cooler than this stays accent-coloured.
warm_above = 70

-- Diameter in pixels of the widget, tick numbers included. A floor, not a cap: the rings grow
-- past it when the temperature bars need more room, and everything shrinks
-- together if the conky window is smaller than the result.
widget_size = 500

-- Colors
HTML_base   = "#FFFFFF"   -- rings, ticks and text
HTML_accent = "#FF4081"   -- today, this month, this week, drive rings
HTML_warm   = "#FF7043"   -- hot end of the temperature ramp

-- Opacity, 0 to 1
opacity_track = 0.15      -- unfilled rings, ticks and bar tracks
opacity_label = 0.55      -- ring labels
opacity_text  = 0.85      -- clock and readouts
opacity_live  = 0.95      -- accent and temperature fills

-- Scaled relative position from middle. Positive x and y means left and up,
-- negative x and y means right and down.
x_rel_pos = 0
y_rel_pos = 0

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

local show_gpu = tostring(enable_graphic_card_temperature_sensor):lower() == "yes"

local function hex2rgb(hex)
  hex = hex:gsub("#", "")
  return {tonumber("0x" .. hex:sub(1, 2)) / 255,
          tonumber("0x" .. hex:sub(3, 4)) / 255,
          tonumber("0x" .. hex:sub(5, 6)) / 255}
end

local BASE   = hex2rgb(HTML_base)
local ACCENT = hex2rgb(HTML_accent)
local WARM   = hex2rgb(HTML_warm)

local function paint(cr, color, alpha)
  cairo_set_source_rgba(cr, color[1], color[2], color[3], alpha)
end

-- Readings stay accent-coloured until warm_above and only then shift towards
-- HTML_warm, so a machine sitting at idle never reads as a hot one. Keyed to
-- degrees rather than a fraction of max_temperature, so raising the ceiling
-- does not quietly move the point at which things start looking hot.
local function heat_color(temperature)
  local span = max_temperature - warm_above
  local t = span > 0 and (temperature - warm_above) / span or 1
  if t < 0 then t = 0 elseif t > 1 then t = 1 end
  return {ACCENT[1] + (WARM[1] - ACCENT[1]) * t,
          ACCENT[2] + (WARM[2] - ACCENT[2]) * t,
          ACCENT[3] + (WARM[3] - ACCENT[3]) * t}
end

-- Conky yields an empty string for a sensor or mount point that is not there.
-- Without this, one missing reading would abort the whole draw.
local function number_or(value, default)
  return tonumber(value) or default
end

local HWMON = "/sys/class/hwmon/hwmon"

local function read_first_line(path)
  local file = io.open(path, "r")
  if not file then return nil end
  local line = file:read("*l")
  file:close()
  return line
end

-- hwmon reports temperatures in millidegrees Celsius.
local function read_temperature(path)
  return number_or(read_first_line(path), 0) / 1000
end

-- Core labels are not contiguous: a hybrid Intel part numbers its cores
-- 0, 4, 8, ... 28 and then 32..47, and AMD labels chiplets Tccd1, Tccd2.
-- Collect whatever the chip actually reports and order it by that number.
local function scan_hwmon()
  local cores, gpu = {}, nil

  for chip = 0, 31 do
    local dir = HWMON .. chip .. "/"
    local name = read_first_line(dir .. "name")

    if name == "coretemp" or name == "k10temp" or name == "zenpower" then
      for index = 1, 99 do
        local label = read_first_line(dir .. "temp" .. index .. "_label")
        local number = label and (label:match("^Core (%d+)$") or label:match("^Tccd(%d+)$"))
        if number then
          cores[#cores + 1] = {order = tonumber(number), path = dir .. "temp" .. index .. "_input"}
        end
      end
    elseif gpu == nil and (name == "amdgpu" or name == "nvidia" or name == "radeon") then
      for index = 1, 99 do
        local label = read_first_line(dir .. "temp" .. index .. "_label")
        local path = dir .. "temp" .. index .. "_input"
        if (label == nil or label == "edge" or label == "junction") and read_first_line(path) then
          gpu = path
          break
        end
      end
    end
  end

  table.sort(cores, function(x, y) return x.order < y.order end)

  local paths = {}
  for i, core in ipairs(cores) do paths[i] = core.path end
  return paths, gpu
end

local cpu_sensors, gpu_sensor
local next_scan = 0

-- Conky may start before the sensor modules are up, so retry a failed scan
-- occasionally rather than reporting zero forever.
local function ensure_sensors()
  if cpu_sensors and #cpu_sensors > 0 then return end
  local now = os.time()
  if now < next_scan then return end
  next_scan = now + 30
  cpu_sensors, gpu_sensor = scan_hwmon()
end

-- position is 1-based over the cores that exist, not a kernel core number.
local function cpu_temperature(position)
  ensure_sensors()
  local path = cpu_sensors and cpu_sensors[position]
  if not path then return 0 end
  return read_temperature(path)
end

local function gpu_temperature()
  ensure_sensors()
  if gpu_sensor then return read_temperature(gpu_sensor) end
  -- Some NVIDIA setups expose no hwmon entry; fall back to the driver's tool.
  return number_or(conky_parse(
    "${exec nvidia-smi --query-gpu=temperature.gpu --format=csv,noheader,nounits | head -n 1}"), 0)
end

-- Day 0 of next month is the last day of this one.
local function days_in_current_month()
  local now = os.date("*t")
  return os.date("*t", os.time({year = now.year, month = now.month + 1, day = 0, hour = 12})).day
end

---------------- LAYOUT ----------------

-- Every length is written against this design size and multiplied at draw time.
local BASE_DIAMETER = 500
local INNER_RADIUS  = 118   -- usable space inside the innermost ring

-- Ring radii, the thickness of the two arc rings, and the radii the tick
-- numbers are written at (outside their ticks, inside the next ring).
-- Each band is spaced so the current tick, which is drawn 1.9x long, still
-- clears the numbers written outside it.
local R_WEEK, R_DAY, R_MONTH, R_DOW = 222, 188, 165, 138
local R_WEEK_LABEL, R_DAY_LABEL = 240, 206
local RING_THICKNESS = 20

-- A gauge is a slim rounded bar with its label underneath.
local GAUGE_WIDTH, GAUGE_BAR = 16, 7
local GAUGE_COLUMN_HEIGHT = 46
local GAUGE_LABEL_GAP, GAUGE_LABEL_HEIGHT = 11, 11
local GAUGE_HEIGHT = GAUGE_COLUMN_HEIGHT + GAUGE_LABEL_GAP + GAUGE_LABEL_HEIGHT
local GAUGE_PITCH_X = 20
local GAUGE_PITCH_Y = GAUGE_HEIGHT + 14
local GAUGE_TOP = 30

-- How far the rings must grow for a block of this size to clear the innermost
-- one. The block hangs GAUGE_TOP below the centre in *ring* units, because it
-- has to sit under the clock and date which scale with the rings, while its own
-- width and height are in *gauge* units. Growth therefore appears on both sides
-- of the comparison and is solved by iteration; it converges quickly because
-- GAUGE_TOP is much smaller than INNER_RADIUS.
local function growth_for(half_width, height)
  local growth = 1
  for _ = 1, 40 do
    local reach = math.sqrt(half_width ^ 2 + (GAUGE_TOP * growth + height) ^ 2)
    growth = math.max(1, reach / INNER_RADIUS)
  end
  return growth
end

-- Wrap the gauges into the grid that needs the least ring growth.
local function grid_for(count)
  local best
  for columns = 1, count do
    local rows = math.ceil(count / columns)
    local half_width = (GAUGE_PITCH_X * (columns - 1) + GAUGE_WIDTH) / 2
    local height = GAUGE_PITCH_Y * (rows - 1) + GAUGE_HEIGHT
    local growth = growth_for(half_width, height)
    if best == nil or growth < best.growth then
      best = {columns = columns, rows = rows, growth = growth}
    end
  end
  return best
end

-- Never draw a gauge with no sensor behind it; it would sit permanently empty.
local function gauge_count()
  ensure_sensors()
  local available = #(cpu_sensors or {})
  local wanted = number_of_physical_CPU_cores
  if wanted <= 0 then wanted = available end
  return math.min(wanted, available)
end

-- Rings and gauges deliberately do not share a scale: scaling both together
-- would enlarge the block by exactly the factor the rings grew by, so it would
-- never come to fit. The gauges hold their size and the rings grow around them.
local grid, ring_growth, laid_out_for

local function layout_for(count)
  if laid_out_for == count then return end
  laid_out_for = count
  grid = grid_for(count)
  ring_growth = grid.growth
end

local warned = false
local function warn_once(count, available, needed)
  if warned then return end
  warned = true
  io.stderr:write(string.format(
    "conky lua_widgets_modernized: %d gauges need a %dpx window; this one is %dpx, so the " ..
    "widget has been scaled down. Raise the size at the top of start_conky_modernized.\n",
    count, math.ceil(needed), math.floor(available)))
end

---------------- DRAWING ----------------

local extents
local function measure(cr, text)
  extents = extents or cairo_text_extents_t:create()
  cairo_text_extents(cr, text, extents)
  return extents
end

local function text_at(cr, x, y, text, size, color, alpha, weight)
  cairo_select_font_face(cr, "DejaVu Sans", CAIRO_FONT_SLANT_NORMAL,
                         weight or CAIRO_FONT_WEIGHT_NORMAL)
  cairo_set_font_size(cr, size)
  local e = measure(cr, text)
  paint(cr, color, alpha)
  cairo_move_to(cr, x - e.width / 2 - e.x_bearing, y)
  cairo_show_text(cr, text)
  cairo_new_path(cr)
end

-- Text laid along the ring. Labels past the halfway point are flipped so they
-- stay the right way up instead of hanging upside down at the bottom.
local function radial_text(cr, cx, cy, radius, angle, text, size, color, alpha)
  cairo_select_font_face(cr, "DejaVu Sans", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_NORMAL)
  cairo_set_font_size(cr, size)
  local e = measure(cr, text)
  local width, height = e.width, e.height
  local bearing = e.x_bearing

  cairo_save(cr)
  cairo_translate(cr, cx + math.cos(angle) * radius, cy + math.sin(angle) * radius)
  cairo_rotate(cr, angle + (math.sin(angle) > 0 and -math.pi / 2 or math.pi / 2))
  paint(cr, color, alpha)
  cairo_move_to(cr, -width / 2 - bearing, height / 2)
  cairo_show_text(cr, text)
  cairo_restore(cr)
  cairo_new_path(cr)
end

-- Ring of small radial ticks, the current one longer, in the accent colour and
-- with its number written outside it. Only that one is numbered.
local function tick_ring(cr, cx, cy, radius, count, current, length, width,
                         label_radius, font_size)
  cairo_set_line_cap(cr, CAIRO_LINE_CAP_ROUND)
  for i = 1, count do
    local angle = -math.pi / 2 + (i - 1) * 2 * math.pi / count
    local live = (i == current)
    local len = live and length * 1.9 or length
    local cos_a, sin_a = math.cos(angle), math.sin(angle)

    paint(cr, live and ACCENT or BASE, live and opacity_live or opacity_track)
    cairo_set_line_width(cr, live and width * 1.4 or width)
    cairo_new_path(cr)
    cairo_move_to(cr, cx + cos_a * (radius - len / 2), cy + sin_a * (radius - len / 2))
    cairo_line_to(cr, cx + cos_a * (radius + len / 2), cy + sin_a * (radius + len / 2))
    cairo_stroke(cr)

    if live then
      radial_text(cr, cx, cy, label_radius, angle, tostring(i), font_size,
                  ACCENT, opacity_live)
    end
  end
end

-- Ring of thick arc segments, each carrying a label.
local function label_ring(cr, cx, cy, radius, labels, current, thickness, font_size)
  local count = #labels
  local step = 2 * math.pi / count
  local gap = math.rad(2.4)

  cairo_set_line_cap(cr, CAIRO_LINE_CAP_BUTT)
  cairo_set_line_width(cr, thickness)
  for i = 1, count do
    local start = -math.pi / 2 + (i - 1) * step
    local live = (i == current)

    paint(cr, live and ACCENT or BASE, live and 0.85 or opacity_track)
    cairo_new_path(cr)
    cairo_arc(cr, cx, cy, radius, start + gap / 2, start + step - gap / 2)
    cairo_stroke(cr)

    radial_text(cr, cx, cy, radius, start + step / 2, labels[i], font_size,
                live and BASE or BASE, live and opacity_text or opacity_label)
  end
end

local function ellipse_path(cr, x, y, rx, ry, from, to)
  cairo_save(cr)
  cairo_translate(cr, x, y)
  cairo_scale(cr, rx, ry)
  cairo_arc(cr, 0, 0, 1, from or 0, to or 2 * math.pi)
  cairo_restore(cr)
end

-- Stacked-platters glyph for the root filesystem.
local function drive_icon(cr, x, y, size, line_width)
  local rx, ry = size * 0.42, size * 0.16
  local top, bottom = y - size * 0.32, y + size * 0.24

  cairo_set_line_width(cr, line_width)
  cairo_set_line_cap(cr, CAIRO_LINE_CAP_ROUND)

  cairo_new_path(cr)
  ellipse_path(cr, x, top, rx, ry)
  cairo_stroke(cr)

  cairo_new_path(cr)
  cairo_move_to(cr, x - rx, top)
  cairo_line_to(cr, x - rx, bottom)
  cairo_move_to(cr, x + rx, top)
  cairo_line_to(cr, x + rx, bottom)
  cairo_stroke(cr)

  cairo_new_path(cr)
  ellipse_path(cr, x, bottom, rx, ry, 0, math.pi)
  cairo_stroke(cr)

  cairo_new_path(cr)
  ellipse_path(cr, x, y - size * 0.04, rx, ry, 0, math.pi)
  cairo_stroke(cr)
end

-- House glyph for the home filesystem.
local function home_icon(cr, x, y, size, line_width)
  local half = size * 0.44
  local apex, eaves, base = y - size * 0.40, y - size * 0.04, y + size * 0.34
  local wall = half * 0.76
  local door = wall * 0.40

  cairo_set_line_width(cr, line_width)
  cairo_set_line_cap(cr, CAIRO_LINE_CAP_ROUND)
  cairo_set_line_join(cr, CAIRO_LINE_JOIN_ROUND)

  cairo_new_path(cr)
  cairo_move_to(cr, x - half, eaves)
  cairo_line_to(cr, x, apex)
  cairo_line_to(cr, x + half, eaves)
  cairo_stroke(cr)

  cairo_new_path(cr)
  cairo_move_to(cr, x - wall, eaves + size * 0.06)
  cairo_line_to(cr, x - wall, base)
  cairo_line_to(cr, x + wall, base)
  cairo_line_to(cr, x + wall, eaves + size * 0.06)
  cairo_stroke(cr)

  cairo_new_path(cr)
  cairo_move_to(cr, x - door, base)
  cairo_line_to(cr, x - door, base - size * 0.24)
  cairo_line_to(cr, x + door, base - size * 0.24)
  cairo_line_to(cr, x + door, base)
  cairo_stroke(cr)
end

local function rounded_rect(cr, x, y, w, h, r)
  cairo_new_path(cr)
  cairo_arc(cr, x + w - r, y + r, r, -math.pi / 2, 0)
  cairo_arc(cr, x + w - r, y + h - r, r, 0, math.pi / 2)
  cairo_arc(cr, x + r, y + h - r, r, math.pi / 2, math.pi)
  cairo_arc(cr, x + r, y + r, r, math.pi, 3 * math.pi / 2)
  cairo_close_path(cr)
end

-- Graphics-card glyph for the GPU: board, heatsink fins, fan and PCIe tab.
local function gpu_icon(cr, x, y, size, line_width)
  local half_w, half_h = size * 0.48, size * 0.30
  local left, right = x - half_w, x + half_w
  local top, bottom = y - half_h, y + half_h

  cairo_set_line_width(cr, line_width)
  cairo_set_line_cap(cr, CAIRO_LINE_CAP_ROUND)
  cairo_set_line_join(cr, CAIRO_LINE_JOIN_ROUND)

  -- board, rounded to sit with the other two glyphs
  rounded_rect(cr, left, top, half_w * 2, half_h * 2, half_h * 0.34)
  cairo_stroke(cr)

  -- fan, with its hub
  local fan_x, fan_r = x + half_w * 0.42, half_h * 0.50
  cairo_new_path(cr)
  cairo_arc(cr, fan_x, y, fan_r, 0, 2 * math.pi)
  cairo_stroke(cr)
  cairo_new_path(cr)
  cairo_arc(cr, fan_x, y, line_width * 0.65, 0, 2 * math.pi)
  cairo_fill(cr)

  -- heatsink fins
  for i = 0, 2 do
    local fin_x = left + half_w * (0.32 + i * 0.28)
    cairo_new_path(cr)
    cairo_move_to(cr, fin_x, top + half_h * 0.50)
    cairo_line_to(cr, fin_x, bottom - half_h * 0.50)
    cairo_stroke(cr)
  end

  -- PCIe tab
  cairo_new_path(cr)
  cairo_move_to(cr, left + half_w * 0.38, bottom)
  cairo_line_to(cr, left + half_w * 0.38, bottom + half_h * 0.42)
  cairo_line_to(cr, left + half_w * 1.02, bottom + half_h * 0.42)
  cairo_line_to(cr, left + half_w * 1.02, bottom)
  cairo_stroke(cr)
end

-- Dial: a progress ring with an icon and a readout inside it.
-- Largest size at which "text" still fits "max_width", so an unusually long
-- readout (a three-digit temperature) shrinks rather than running into the ring.
local function fitted_size(cr, text, preferred, max_width)
  cairo_select_font_face(cr, "DejaVu Sans", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_NORMAL)
  local size = preferred
  -- Hinting rounds glyph advances up, so scaling by the width ratio once can
  -- still come out too wide; undershoot slightly and re-measure.
  for _ = 1, 5 do
    cairo_set_font_size(cr, size)
    local width = measure(cr, text).width
    if width <= max_width or width <= 0 then return size end
    size = size * max_width / width * 0.98
  end
  return size
end

local function draw_dial(cr, x, y, radius, fraction, color, icon, label, scale)
  cairo_set_line_cap(cr, CAIRO_LINE_CAP_ROUND)
  cairo_set_line_width(cr, 5 * scale)

  paint(cr, BASE, opacity_track)
  cairo_new_path(cr)
  cairo_arc(cr, x, y, radius, 0, 2 * math.pi)
  cairo_stroke(cr)

  if fraction > 0 then
    if fraction > 1 then fraction = 1 end
    local start = -math.pi / 2
    paint(cr, color, opacity_live)
    cairo_new_path(cr)
    cairo_arc(cr, x, y, radius, start, start + 2 * math.pi * fraction)
    cairo_stroke(cr)
  end

  paint(cr, BASE, opacity_text)
  icon(cr, x, y - radius * 0.30, radius * 0.72, 1.7 * scale)

  -- the readout sits on a chord of the ring's clear interior, so how much room
  -- it has depends on how far down it is
  local clear = radius - 2.5 * scale
  local baseline = radius * 0.55
  local chord = 2 * math.sqrt(math.max(clear * clear - baseline * baseline, 1))
  text_at(cr, x, y + baseline, label,
          fitted_size(cr, label, 11 * scale, chord * 0.88), BASE, opacity_text)
end

-- One temperature bar with its label beneath.
local function draw_gauge(cr, x, top, scale, temperature, label)
  local bar = GAUGE_BAR * scale
  local height = GAUGE_COLUMN_HEIGHT * scale
  local top_y, bottom_y = top + bar / 2, top + height - bar / 2

  cairo_set_line_cap(cr, CAIRO_LINE_CAP_ROUND)
  cairo_set_line_width(cr, bar)

  paint(cr, BASE, opacity_track)
  cairo_new_path(cr)
  cairo_move_to(cr, x, top_y)
  cairo_line_to(cr, x, bottom_y)
  cairo_stroke(cr)

  local fraction = temperature / max_temperature
  if fraction > 1 then fraction = 1 end
  if fraction > 0 then
    paint(cr, heat_color(temperature), opacity_live)
    cairo_new_path(cr)
    cairo_move_to(cr, x, bottom_y)
    cairo_line_to(cr, x, bottom_y - (bottom_y - top_y) * fraction)
    cairo_stroke(cr)
  end

  text_at(cr, x, top + (GAUGE_COLUMN_HEIGHT + GAUGE_LABEL_GAP + GAUGE_LABEL_HEIGHT * 0.7) * scale,
          label, GAUGE_LABEL_HEIGHT * scale, BASE, opacity_label)
end

-- Rows are centred individually, so a short last row stays balanced.
local function draw_gauges(cr, center_x, center_y, scale, ring_scale, count)
  local top = center_y + GAUGE_TOP * ring_scale

  for row = 1, grid.rows do
    local first = (row - 1) * grid.columns + 1
    local last = math.min(first + grid.columns - 1, count)
    local in_row = last - first + 1
    local row_width = (GAUGE_PITCH_X * (in_row - 1) + GAUGE_WIDTH) * scale
    local left = center_x - row_width / 2 + GAUGE_WIDTH * scale / 2

    for column = 1, in_row do
      local index = first + column - 1
      local x = left + GAUGE_PITCH_X * (column - 1) * scale
      draw_gauge(cr, x, top, scale, cpu_temperature(index), tostring(index - 1))
    end

    top = top + GAUGE_PITCH_Y * scale
  end
end

local MONTHS = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"}
local WEEKDAYS = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}

local function draw_function(cr)
  local w, h = conky_window.width, conky_window.height
  local width, height = w - x_rel_pos, h - y_rel_pos
  local center_x, center_y = width / 2, height / 2

  local count = gauge_count()
  layout_for(count)

  local base = widget_size / BASE_DIAMETER
  local needed = BASE_DIAMETER * base * ring_growth
  local available = math.min(width, height)
  local fit = math.min(1, available / needed)
  if fit < 0.99 then warn_once(count, available, needed) end

  local gauge_scale = base * fit
  local scale = base * ring_growth * fit   -- everything positioned off the rings

  cairo_set_line_join(cr, CAIRO_LINE_JOIN_ROUND)

  -- Weeks of the year, then days of this month, as tick rings
  tick_ring(cr, center_x, center_y, R_WEEK * scale, 52, tonumber(os.date("%V")),
            7 * scale, 2 * scale, R_WEEK_LABEL * scale, 10.5 * scale)
  tick_ring(cr, center_x, center_y, R_DAY * scale, days_in_current_month(),
            tonumber(os.date("%d")), 9 * scale, 2.4 * scale, R_DAY_LABEL * scale, 10.5 * scale)

  -- Months and weekdays as labelled arcs
  label_ring(cr, center_x, center_y, R_MONTH * scale, MONTHS,
             tonumber(os.date("%m")), RING_THICKNESS * scale, 11 * scale)
  label_ring(cr, center_x, center_y, R_DOW * scale, WEEKDAYS,
             tonumber(os.date("%u")), RING_THICKNESS * scale, 11 * scale)

  -- Filesystems, and the GPU when it is enabled
  local dials = {{path = "/", icon = drive_icon}, {path = "/home", icon = home_icon}}
  if show_gpu then dials[#dials + 1] = {gpu = true, icon = gpu_icon} end

  -- Two dials keep their original spacing; a third tucks in further, both
  -- closer together and slightly smaller, so the outermost two keep clear of
  -- the innermost ring rather than crowding it.
  local spacing, radius = 116, 26
  if #dials > 2 then spacing, radius = 60, 23 end
  local dial_y = center_y - (48 + radius) * scale

  for i, dial in ipairs(dials) do
    local x = center_x + (i - (#dials + 1) / 2) * spacing * scale
    if dial.gpu then
      local temperature = gpu_temperature()
      local fraction = temperature / max_temperature
      draw_dial(cr, x, dial_y, radius * scale, fraction, heat_color(temperature),
                dial.icon, math.floor(temperature + 0.5) .. "°C", scale)
    else
      local used = 100 - number_or(conky_parse("${fs_free_perc " .. dial.path .. "}"), 0)
      draw_dial(cr, x, dial_y, radius * scale, used / 100, ACCENT,
                dial.icon, math.floor(used + 0.5) .. "%", scale)
    end
  end

  -- Clock. The date is not written out: the rings already carry the weekday,
  -- day, month and week, each highlighted in the accent colour.
  text_at(cr, center_x, center_y + 8 * scale, os.date("%H:%M"), 46 * scale,
          BASE, opacity_text, CAIRO_FONT_WEIGHT_NORMAL)

  -- Temperatures
  draw_gauges(cr, center_x, center_y, gauge_scale, scale, count)
end

function conky_start_widgets()
  if conky_window == nil then return end

  local cs, owns_surface = conky_window_surface()
  local cr = cairo_create(cs)

  -- Check that Conky has been running for at least 5s
  if number_or(conky_parse('${updates}'), 0) > 5 then
    draw_function(cr)
  end

  cairo_destroy(cr)
  if owns_surface then cairo_surface_destroy(cs) end
end
