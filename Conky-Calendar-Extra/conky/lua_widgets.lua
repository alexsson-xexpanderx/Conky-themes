-- 2014-02-24 by eXpander

---------------- USER CONFIGURATION ----------------

-- How many CPU cores to show a temperature bar for; 0 means every sensor the
-- kernel publishes. Bars are filled from the cores actually reported, in order,
-- however they happen to be numbered -- a hybrid CPU labels them 0, 4, 8, ...
-- rather than 0, 1, 2, 3. There is one sensor per physical core, so this is
-- normally fewer than the thread count htop shows, and asking for more than
-- exist simply draws the ones that do.
number_of_physical_CPU_cores = 0

-- Show graphic card temperature? (Yes/No)
-- Read from the amdgpu, nvidia or radeon hwmon entry, falling back to
-- "nvidia-smi" only when the driver publishes no sensor.
enable_graphic_card_temperature_sensor = "No"

-- Temperature a full bar represents, in degrees Celsius.
max_temperature = 70

-- Diameter in pixels of the outermost ring. Raise it to make the whole widget
-- bigger. It is a floor, not a cap: the rings grow past it on their own when
-- the temperature bars need more room, and everything shrinks together if the
-- conky window in start_conky is smaller than the result.
widget_size = 450

-- Colors
HTML_colors = "#000000"
HTML_colors_current = "#FFFFFF"
transparency = 0.5 -- From 0 to 1

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
  return tonumber("0x" .. hex:sub(1, 2)) / 255,
         tonumber("0x" .. hex:sub(3, 4)) / 255,
         tonumber("0x" .. hex:sub(5, 6)) / 255
end

local r, g, b = hex2rgb(HTML_colors)
local r_c, g_c, b_c = hex2rgb(HTML_colors_current)

-- Conky yields an empty string for a sensor or mount point that is not there.
-- Without this, one missing reading would abort the whole draw and the widget
-- would simply vanish.
local function number_or(value, default)
  return tonumber(value) or default
end

-- lm-sensors only formats what the kernel already exposes under /sys/class/hwmon,
-- so read that directly instead of spawning sensors|grep|awk|tr per core per
-- redraw. Lua has no directory listing in its standard library, hence probing
-- fixed index ranges rather than globbing.
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

-- Ring of separate arc segments centred on (w, h), used for the disk gauges.
-- Segments up to "current" percent are drawn in the highlight color.
local function create_circle_hdd(cr, w, h, elements, distance_between_blocks, radius, line_width, current)
  cairo_set_line_width(cr, line_width)
  cairo_set_source_rgba(cr, r, g, b, transparency)
  cairo_new_path(cr)

  local number_of_arcs = (360 - (elements * distance_between_blocks)) / elements
  local start_angle = 270
  local percent_per_element = 100.0 / elements
  local charged_elements = current / percent_per_element

  for i = 1, elements do
    if charged_elements >= i then
      cairo_set_source_rgba(cr, r_c, g_c, b_c, transparency)
    end
    cairo_arc(cr, w, h, radius, start_angle * math.pi / 180, (start_angle + number_of_arcs) * math.pi / 180)
    cairo_stroke(cr)
    start_angle = start_angle + number_of_arcs + distance_between_blocks
    cairo_set_source_rgba(cr, r, g, b, transparency)
  end
end

-- Ring of arc segments centred on (w/2, h/2), each carrying a rotated label.
-- The segment matching "current" is highlighted.
--   days     -- table of labels, or '' to label each segment with its index
--   operator -- cairo operator used while drawing the labels
local function create_circle(cr, w, h, elements, distance_between_blocks, two_number_degree,
                             radius, line_width, operator, radius_shift_for_text, current,
                             days, shift_days_distance)
  cairo_set_line_width(cr, line_width)
  cairo_set_source_rgba(cr, r, g, b, transparency)
  cairo_new_path(cr)

  local number_of_arcs = (360 - (elements * distance_between_blocks)) / elements
  local start_angle = 270

  -- Segments
  for i = 1, elements do
    if i == current then
      cairo_set_source_rgba(cr, r_c, g_c, b_c, transparency)
    end
    cairo_arc(cr, w / 2, h / 2, radius, start_angle * math.pi / 180, (start_angle + number_of_arcs) * math.pi / 180)
    cairo_stroke(cr)
    start_angle = start_angle + number_of_arcs + distance_between_blocks
    cairo_set_source_rgba(cr, r, g, b, transparency)
  end

  -- Labels
  start_angle = 270
  cairo_set_operator(cr, operator)

  local text_radius = radius + radius_shift_for_text
  local has_labels = days ~= ""

  for i = 1, elements do
    if i == current then
      cairo_set_source_rgba(cr, r_c, g_c, b_c, transparency)
    end

    local label
    if has_labels then label = days[i] else label = tostring(i) end

    -- Wider labels start further into their segment so they stay centred.
    local text_offset, extra_rotation
    if has_labels then
      text_offset = math.abs(number_of_arcs - shift_days_distance) / 2
      extra_rotation = 4
    elseif #label == 2 then
      text_offset = (number_of_arcs - two_number_degree) / 2
      extra_rotation = 0
    elseif #label == 1 then
      text_offset = (number_of_arcs - distance_between_blocks) / 2
      extra_rotation = 0
    end

    if text_offset then
      local text_angle = (start_angle + text_offset) * (math.pi / 180.0)
      local rotation = (text_offset + (number_of_arcs + distance_between_blocks) * (i - 1) + extra_rotation) * math.pi / 180.0

      cairo_move_to(cr, w / 2 + (text_radius * math.cos(text_angle)),
                        h / 2 + (text_radius * math.sin(text_angle)))
      cairo_rotate(cr, rotation)
      cairo_show_text(cr, label)
      cairo_rotate(cr, -rotation)
    end

    start_angle = start_angle + number_of_arcs + distance_between_blocks
    cairo_set_source_rgba(cr, r, g, b, transparency)
  end

  cairo_close_path(cr)
  cairo_set_operator(cr, CAIRO_OPERATOR_OVER)
end

-- Every length below is expressed against this design size and multiplied by
-- the scale worked out in layout_for(); angles are of course scale-free.
local BASE_DIAMETER = 450   -- outermost ring at scale 1, i.e. the original look
local INNER_RADIUS  = 130   -- usable space inside the innermost ring, with margin

-- A gauge is a 10-block column 15 wide and 48 tall with a labelled dot beneath
-- it, and the block of them starts GAUGE_TOP below the centre of the rings.
-- The dot hangs GAUGE_DOT_GAP under the column so the circles keep clear of the
-- bars; DOT_RADIUS_MAX budgets the height a dot may take, which is what lets the
-- gauge box be measured before the labels are known.
local GAUGE_WIDTH = 15
local GAUGE_COLUMN_HEIGHT = 48
local GAUGE_DOT_GAP = 16
local DOT_RADIUS_MAX = 10
local GAUGE_DOT_Y = GAUGE_COLUMN_HEIGHT + GAUGE_DOT_GAP + DOT_RADIUS_MAX
local GAUGE_HEIGHT = GAUGE_DOT_Y + DOT_RADIUS_MAX
local GAUGE_PITCH_X = 30
local GAUGE_PITCH_Y = GAUGE_HEIGHT + 8
local GAUGE_TOP = 25

-- Wrap the gauges into the grid whose furthest corner sits closest to the
-- centre, so the rings have to grow as little as possible. For 32 gauges this
-- picks 16 x 2; for the original 4 it picks a single row, leaving the classic
-- layout untouched.
local function grid_for(count)
  local best
  for columns = 1, count do
    local rows = math.ceil(count / columns)
    local width = GAUGE_PITCH_X * (columns - 1) + GAUGE_WIDTH
    local height = GAUGE_PITCH_Y * (rows - 1) + GAUGE_HEIGHT
    local reach = math.sqrt((width / 2) ^ 2 + (GAUGE_TOP + height) ^ 2)
    if best == nil or reach < best.reach then
      best = {columns = columns, rows = rows, reach = reach}
    end
  end
  return best
end

-- The kernel publishes one sensor per physical core, which on a hybrid CPU is
-- fewer than the thread count htop lists: 8 P-cores plus 16 E-cores is 24
-- sensors but 32 threads. Drawing a gauge with no sensor behind it would leave
-- it permanently empty, so the request is capped at what was actually found.
local function gauge_count()
  ensure_sensors()
  local available = #(cpu_sensors or {})
  local wanted = number_of_physical_CPU_cores
  if wanted <= 0 then wanted = available end
  return math.min(wanted, available) + (show_gpu and 1 or 0)
end

-- Rings and gauges deliberately do not share a scale. Scaling both together
-- would enlarge the block by exactly the factor the rings grew by, so it would
-- never come to fit; instead the gauges keep the size asked for and the rings
-- grow around them.
local grid, ring_growth, laid_out_for

local function layout_for(count)
  if laid_out_for == count then return end
  laid_out_for = count
  grid = grid_for(count)
  ring_growth = math.max(1, grid.reach / INNER_RADIUS)
end

local warned = false
local function warn_once(count, available, needed)
  if warned then return end
  warned = true
  io.stderr:write(string.format(
    "conky lua_widgets: %d gauges need a %dpx window; this one is %dpx, so the widget " ..
    "has been scaled down. Raise minimum_width/minimum_height in start_conky.\n",
    count, math.ceil(needed), math.floor(available)))
end

-- Reusing one extents struct avoids allocating per label per redraw.
local extents
local function measure(cr, text)
  extents = extents or cairo_text_extents_t:create()
  cairo_text_extents(cr, text, extents)
  return extents
end

-- Filled disc of exactly the radius asked for, with its label knocked out of
-- the middle. The radius is the caller's business: sizing each disc to its own
-- label makes a row of them visibly uneven.
local function labelled_dot(cr, x, y, radius, label)
  local size = measure(cr, label)

  cairo_arc(cr, x, y, radius, 0, 2 * math.pi)
  cairo_fill(cr)
  cairo_set_operator(cr, CAIRO_OPERATOR_CLEAR)
  cairo_move_to(cr, x - size.width / 2 - size.x_bearing, y + size.height / 2)
  cairo_show_text(cr, label)
  cairo_set_operator(cr, CAIRO_OPERATOR_OVER)
end

-- One radius for every gauge dot, wide enough for the widest label in the set,
-- so "0" and "31" sit in identical circles.
local function gauge_dot_radius(cr, scale, count)
  local widest = 0
  for index = 1, count do
    local label = (show_gpu and index == count) and "G" or tostring(index - 1)
    widest = math.max(widest, measure(cr, label).width)
  end
  -- capped so a dot can never exceed the height budgeted for it above
  return math.min(DOT_RADIUS_MAX * scale, math.max(7 * scale, widest / 2 + 3 * scale))
end

-- One gauge, with (left, top) the corner of its 15x72 box at this scale.
local function draw_gauge(cr, left, top, scale, dot_radius, temperature, label)
  local degrees_per_block = max_temperature / 10
  local filled_blocks = math.floor((temperature / degrees_per_block) + 0.5)

  cairo_set_source_rgba(cr, r, g, b, transparency)
  for i = 1, 10 do
    if filled_blocks >= i then
      cairo_set_source_rgba(cr, r_c, g_c, b_c, transparency)
    end
    cairo_rectangle(cr, left, top + (50 - i * 5) * scale, GAUGE_WIDTH * scale, 3 * scale)
    cairo_fill(cr)
    cairo_set_source_rgba(cr, r, g, b, transparency)
  end

  labelled_dot(cr, left + 8 * scale, top + GAUGE_DOT_Y * scale, dot_radius, label)
end

-- Rows are centred individually, so a short last row stays balanced.
local function draw_gauges(cr, center_x, center_y, scale, count)
  cairo_set_font_size(cr, 12 * scale)
  local dot_radius = gauge_dot_radius(cr, scale, count)

  local top = center_y + GAUGE_TOP * scale
  for row = 1, grid.rows do
    local first = (row - 1) * grid.columns + 1
    local last = math.min(first + grid.columns - 1, count)
    local in_row = last - first + 1
    local row_width = (GAUGE_PITCH_X * (in_row - 1) + GAUGE_WIDTH) * scale
    local left = center_x - row_width / 2

    for column = 1, in_row do
      local index = first + column - 1
      local x = left + GAUGE_PITCH_X * (column - 1) * scale
      if show_gpu and index == count then
        draw_gauge(cr, x, top, scale, dot_radius, gpu_temperature(), "G")
      else
        draw_gauge(cr, x, top, scale, dot_radius, cpu_temperature(index), tostring(index - 1))
      end
    end

    top = top + GAUGE_PITCH_Y * scale
  end
end

local function draw_function(cr)
  local w, h = conky_window.width, conky_window.height
  local width, height = w - x_rel_pos, h - y_rel_pos
  local center_x, center_y = width / 2, height / 2

  -- Never draw larger than the window; shrinking everything by one factor keeps
  -- the layout intact where clipping would not.
  local count = gauge_count()
  layout_for(count)

  local base = widget_size / BASE_DIAMETER
  local needed = BASE_DIAMETER * base * ring_growth
  local available = math.min(width, height)
  local fit = math.min(1, available / needed)
  if fit < 0.99 then warn_once(count, available, needed) end

  local gauge_scale = base * fit
  local scale = base * ring_growth * fit   -- everything positioned off the rings

  cairo_set_line_width(cr, 3 * scale)
  cairo_set_font_size(cr, 12 * scale)
  cairo_select_font_face(cr, "Dejavu Sans Condensed", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_NORMAL)

  -- Number of weeks per year
  create_circle(cr, width, height, 52.0, 2, 3.5, 225 * scale, 3 * scale,
                CAIRO_OPERATOR_OVER, 4 * scale, tonumber(os.date("%V")), '')

  -- Number of days in the current month
  create_circle(cr, width, height, days_in_current_month(), 2, 3.5, 200 * scale, 13 * scale,
                CAIRO_OPERATOR_CLEAR, -4.5 * scale, tonumber(os.date("%d")), '')

  -- Days
  local days = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
  create_circle(cr, width, height, 7, 2, 3.5, 150 * scale, 13 * scale,
                CAIRO_OPERATOR_CLEAR, -4 * scale, tonumber(os.date("%u")), days, 8.5)

  -- Month
  local months = {"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"}
  create_circle(cr, width, height, 12, 2, 3.5, 175 * scale, 13 * scale,
                CAIRO_OPERATOR_CLEAR, -4 * scale, tonumber(os.date("%m")), months, 5.5)

  -- Clock
  cairo_set_font_size(cr, 42 * scale)
  local clock = os.date("%H:%M")
  local clock_size = measure(cr, clock)
  cairo_move_to(cr, center_x - clock_size.width / 2 - clock_size.x_bearing, center_y)
  cairo_show_text(cr, clock)
  cairo_set_font_size(cr, 12 * scale)

  -- Free space
  cairo_set_operator(cr, CAIRO_OPERATOR_OVER)

  local drives = {{name = "R", path = "/", x = center_x - 60 * scale},
                  {name = "H", path = "/home", x = center_x + 60 * scale}}

  for _, drive in ipairs(drives) do
    local used_perc = 100 - number_or(conky_parse("${fs_free_perc " .. drive.path .. "}"), 0)
    create_circle_hdd(cr, drive.x, center_y - 80 * scale, 20, 3, 20 * scale, 3 * scale, used_perc)
    labelled_dot(cr, drive.x, center_y - 80 * scale, 14 * scale, drive.name)
  end

  -- Temperatures
  draw_gauges(cr, center_x, center_y, gauge_scale, count)
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
