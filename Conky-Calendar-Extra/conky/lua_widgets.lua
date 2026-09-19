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

-- How many gauges to draw: one per core we actually have a sensor for, plus
-- one for the GPU when it is enabled.
local function gauge_count()
  ensure_sensors()
  local available = #(cpu_sensors or {})
  local wanted = number_of_physical_CPU_cores
  if wanted <= 0 then wanted = available end
  return math.min(wanted, available) + (show_gpu and 1 or 0)
end

-- Column of ten blocks growing upwards from (x, height/2 + y_offset). Blocks are
-- highlighted in proportion to temperature, a full column meaning max_temperature.
local function vertical_bars(cr, x, height, y_offset, temperature)
  cairo_set_source_rgba(cr, r, g, b, transparency)

  local degrees_per_block = max_temperature / 10
  local filled_blocks = math.floor((temperature / degrees_per_block) + 0.5)

  for i = 1, 10 do
    if filled_blocks >= i then
      cairo_set_source_rgba(cr, r_c, g_c, b_c, transparency)
    end
    cairo_rectangle(cr, x, height / 2 + y_offset - i * 5, 15, 3)
    cairo_fill(cr)
    cairo_set_source_rgba(cr, r, g, b, transparency)
  end
end

-- Filled disc with a single character knocked out of it.
local function labelled_dot(cr, x, y, radius, label, shift_x, shift_y)
  cairo_arc(cr, x, y, radius, 0, 2 * math.pi)
  cairo_fill(cr)
  cairo_set_operator(cr, CAIRO_OPERATOR_CLEAR)
  cairo_move_to(cr, x + shift_x, y + shift_y)
  cairo_show_text(cr, label)
  cairo_set_operator(cr, CAIRO_OPERATOR_OVER)
end

local function draw_function(cr)
  local w, h = conky_window.width, conky_window.height
  local center_x, center_y = (w - x_rel_pos) / 2, (h - y_rel_pos) / 2
  local count = gauge_count()

  cairo_set_line_width(cr, 3)
  cairo_set_font_size(cr, 12)
  cairo_select_font_face(cr, "Dejavu Sans Condensed", CAIRO_FONT_SLANT_NORMAL, CAIRO_FONT_WEIGHT_NORMAL)

  -- Number of weeks per year
  create_circle(cr, w - x_rel_pos, h - y_rel_pos, 52.0, 2, 3.5, 225, 3,
                CAIRO_OPERATOR_OVER, 4, tonumber(os.date("%V")), '')

  -- Number of days in the current month
  create_circle(cr, w - x_rel_pos, h - y_rel_pos, days_in_current_month(), 2, 3.5, 200, 13,
                CAIRO_OPERATOR_CLEAR, -4.5, tonumber(os.date("%d")), '')

  -- Days
  local days = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
  create_circle(cr, w - x_rel_pos, h - y_rel_pos, 7, 2, 3.5, 150, 13,
                CAIRO_OPERATOR_CLEAR, -4, tonumber(os.date("%u")), days, 8.5)

  -- Month
  local months = {"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"}
  create_circle(cr, w - x_rel_pos, h - y_rel_pos, 12, 2, 3.5, 175, 13,
                CAIRO_OPERATOR_CLEAR, -4, tonumber(os.date("%m")), months, 5.5)

  -- Clock
  cairo_set_font_size(cr, 42)
  cairo_move_to(cr, center_x - 54, center_y)
  cairo_show_text(cr, os.date("%H:%M"))
  cairo_set_font_size(cr, 12)

  -- Free space
  cairo_set_operator(cr, CAIRO_OPERATOR_OVER)

  local drives = {{name = "R", path = "/", x = center_x - 60},
                  {name = "H", path = "/home", x = center_x + 60}}

  for _, drive in ipairs(drives) do
    local used_perc = 100 - number_or(conky_parse("${fs_free_perc " .. drive.path .. "}"), 0)
    create_circle_hdd(cr, drive.x, center_y - 80, 20, 3, 20, 3, used_perc)
    labelled_dot(cr, drive.x, center_y - 80, 14, drive.name, -4, 5)
  end

  -- Temperatures
  for i = 1, count do
    local x = center_x - ((15 * count) + 15 * (count - 1)) / 2 + 30 * (i - 1)

    if show_gpu and i == count then
      vertical_bars(cr, x, h - y_rel_pos, 75, gpu_temperature())
      labelled_dot(cr, x + 8, center_y + 90, 7, "G", -5, 4)
    else
      vertical_bars(cr, x, h - y_rel_pos, 75, cpu_temperature(i))
      labelled_dot(cr, x + 8, center_y + 90, 7, tostring(i - 1), -3, 4)
    end
  end
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
