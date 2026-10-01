--[[ ==========================================================================
  Conky Orrery -- an animated 3D armillary clock.

  A third look for Conky-Calendar-Extra. Where lua_widgets.lua and
  lua_widgets_modernized.lua draw flat rings, this one builds the same
  information as a rotating orrery: four nested hoops for the year, the month,
  the hours and the seconds, a geodesic cage around the clock, and one orbiting
  body per CPU core that circles faster the hotter that core runs.

  There is no OpenGL here and conky offers none. Everything is a software 3D
  renderer written against cairo's 2D API: points are rotated by a 3x3 matrix,
  divided through by depth for perspective, collected into a flat list and
  painted back to front. That is what makes near hoops pass in front of far
  ones, and what lets the near half of the cage cross over the clock numerals.
============================================================================ ]]

---------------- USER CONFIGURATION ----------------

-- Frames per second the animation is written for. The conky config is what
-- actually sets the rate (`update_interval = 1/fps`), so change both together.
-- This value only decides how much time a frame is worth on systems where
-- /proc/uptime cannot be read, and how long the warm-up wait lasts.
target_fps = 20

-- Overall speed. 1 is the designed pace, 0.5 half as fast, 0 freezes the
-- assembly into a still (the clock and readouts keep updating).
motion = 1.0

-- Diameter in pixels of the whole widget, outer readouts included. Everything
-- scales from this; if the conky window is smaller, the widget shrinks to fit.
widget_size = 640

-- Show the machine as well as the time? (Yes/No) "Yes" puts a body in orbit
-- for each CPU core, a cage round the clock that swells with CPU load, and the
-- readouts around the outside. "No" is the clock and the calendar on their own:
-- the core, temperature, filesystem and readout settings below then have no
-- effect, and nothing is sampled at all.
show_monitoring = "Yes"

-- How many CPU cores to put in orbit; 0 means every sensor the kernel
-- publishes. There is one sensor per physical core, normally fewer than the
-- thread count htop shows. A 32-core machine makes a busy sky -- cap it here.
number_of_physical_CPU_cores = 0

-- Show a graphics card readout? (Yes/No)
enable_graphic_card_temperature_sensor = "Yes"

-- Temperature a body at the outermost orbit represents, in degrees Celsius.
max_temperature = 100

-- Temperature at which the cores and the GPU start shifting towards
-- HTML_heat. Anything cooler than this keeps its own colour.
warm_above = 70

-- Filesystems for the outer readouts.
root_filesystem = "/"
home_filesystem = "/home"

-- Camera. The whole assembly turns steadily and rocks up and down on a
-- different period, so the view never repeats exactly.
camera_turn_seconds = 150   -- one full revolution
camera_rock_seconds = 47    -- one full up-and-down
camera_pitch = 17           -- degrees above the equator, at rest
camera_rock = 11            -- degrees the pitch swings either side

-- Ambient dust gives the scene depth through parallax. (Yes/No)
show_dust = "No"
dust_count = 80
dust_size = 1.1           -- radius of a speck, in design units

-- The innermost hoop, just outside the cage: the seconds hand. Its bright head
-- sweeps round once a minute, and it is the one thing that moves every frame.
-- (Yes/No)
show_seconds = "Yes"

-- The readouts around the outside. (Yes/No)
show_readouts = "Yes"

-- The cage round the clock, which swells with CPU load, and the soft glow
-- behind the numerals. Each can go on its own. (Yes/No)
show_cage = "No"
show_glow = "No"

-- Colours, one for every element, so any of them can be changed on its own --
-- orrery_colors.py edits them all with a live preview. As shipped they are a
-- palette of five, where a colour means a kind of thing and not a place:
-- white for the dials, cyan for what is happening now, raspberry for what is
-- stored or standing, violet for the cores and pink for heat. The editor's
-- presets keep to that; changing a single element is entirely up to you.

-- The clock and the calendar
HTML_clock    = "#DCE6F5"   -- the time in the middle
HTML_rings    = "#DCE6F5"   -- the month, day and weekday hoops and their ticks
HTML_labels   = "#DCE6F5"   -- month names, day numbers and weekdays
HTML_today    = "#3DDCFF"   -- today's month, day and weekday, and their glow
HTML_seconds  = "#bf4272"   -- the seconds hoop and its sweeping head
HTML_dust     = "#DCE6F5"   -- the drifting specks

-- The machine
HTML_cores    = "#9C7CFF"   -- the orbiting cores, while they run cool
HTML_heat     = "#FF5F8D"   -- what cores, cage, glow and GPU turn when hot
HTML_cage     = "#bf4272"   -- the cage round the clock
HTML_glow     = "#bf4272"   -- the glow behind the clock

-- The readouts
HTML_tracks   = "#DCE6F5"   -- the unlit part of each arc
HTML_captions = "#DCE6F5"   -- the names above the values
HTML_cpu      = "#7CFFAC"
HTML_memory   = "#7CFFAC"
HTML_gpu      = "#7CFFAC"   -- while it runs cool
HTML_root     = "#7CFFAC"
HTML_home     = "#7CFFAC"

-- Opacity, 0 to 1: how solidly each element is drawn, by the same names as the
-- colours, and edited next to them in orrery_colors.py. Heat has none of its
-- own; it is a colour other elements turn. Where an element has more than one
-- part -- a readout's arc and its value, the seconds hoop's lit arc and the
-- rest of it -- this is its main part, and the others keep their share of it.
opacity_clock    = 0.92
opacity_rings    = 0.16
opacity_labels   = 0.52
opacity_today    = 1.00
opacity_seconds  = 1.00
opacity_dust     = 0.30
opacity_cores    = 1.00
opacity_cage     = 0.16
opacity_glow     = 0.21
opacity_tracks   = 0.16
opacity_captions = 0.52
opacity_cpu      = 0.35
opacity_memory   = 0.35
opacity_gpu      = 0.35
opacity_root     = 0.35
opacity_home     = 0.35

-- Depth cueing: how much of its brightness the far side of the assembly keeps.
-- 1 disables the effect and flattens the picture; 0.15 is a deep fade.
depth_fade = 0.22

font_name = "DejaVu Sans"

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

local sin, cos, atan2 = math.sin, math.cos, math.atan
local sqrt, floor, pi = math.sqrt, math.floor, math.pi
local TAU = 2 * pi
local RAD = pi / 180

local show_monitoring_now = tostring(show_monitoring):lower() == "yes"
local show_gpu = tostring(enable_graphic_card_temperature_sensor):lower() == "yes"
local show_dust_now = tostring(show_dust):lower() == "yes"
local show_seconds_now = tostring(show_seconds):lower() == "yes"
local show_readouts_now = show_monitoring_now and tostring(show_readouts):lower() == "yes"
local show_cage_now = show_monitoring_now and tostring(show_cage):lower() == "yes"
local show_glow_now = show_monitoring_now and tostring(show_glow):lower() == "yes"

local function clamp(v, low, high)
  if v < low then return low elseif v > high then return high end
  return v
end

local function hex2rgb(hex)
  hex = hex:gsub("#", "")
  return {tonumber("0x" .. hex:sub(1, 2)) / 255,
          tonumber("0x" .. hex:sub(3, 4)) / 255,
          tonumber("0x" .. hex:sub(5, 6)) / 255}
end

-- Every element's colour, by the name after HTML_ in the settings above. One
-- that has gone missing from the file is drawn white rather than stopping the
-- whole widget from drawing.
local INK = {}
for _, name in ipairs({"clock", "rings", "labels", "today", "seconds", "dust",
                        "cores", "heat", "cage", "glow", "tracks", "captions",
                        "cpu", "memory", "gpu", "root", "home"}) do
  INK[name] = hex2rgb(_G["HTML_" .. name] or "#FFFFFF")
end

-- Every element's opacity, from the settings above, falling back to the shipped
-- value for one that is missing. A part drawn at some other strength than the
-- element's main part -- the cage's vertices, a readout's value -- is written as
-- its designed alpha times scale_of(), which is exactly 1 at the shipped value,
-- so every part keeps the proportions it was designed with.
local DESIGNED = {clock = 0.92, rings = 0.16, labels = 0.52, today = 1.00, seconds = 1.00,
                  dust = 0.30, cores = 1.00, cage = 0.16, glow = 0.21, tracks = 0.16,
                  captions = 0.52, cpu = 1.00, memory = 1.00, gpu = 1.00, root = 1.00,
                  home = 1.00}
local OPACITY = {}
for name, designed in pairs(DESIGNED) do
  OPACITY[name] = tonumber(_G["opacity_" .. name]) or designed
end

local function scale_of(name)
  return OPACITY[name] / DESIGNED[name]
end

local function mix(a, b, t)
  return {a[1] + (b[1] - a[1]) * t,
          a[2] + (b[2] - a[2]) * t,
          a[3] + (b[3] - a[3]) * t}
end

-- Readings keep their own colour until warm_above and only then shift towards
-- HTML_heat, so a machine sitting at idle never reads as a hot one. Keyed to
-- degrees rather than a fraction of max_temperature, so raising the ceiling
-- does not quietly move the point at which things start looking hot.
--
-- The cores' cool colour is not today's. They used to idle in the same cyan as
-- the date, so two dozen glowing dots around the clock matched the three labels
-- worth reading, and the date was the harder of the two to find.
local function heat_color(temperature, cool)
  local span = max_temperature - warm_above
  local t = span > 0 and (temperature - warm_above) / span or 1
  return mix(cool, INK.heat, clamp(t, 0, 1))
end

-- orrery_colors.py's preview sets orrery_spotlight to one element's name, to
-- show which part of the widget a colour belongs to; conky never sets it. Every
-- other element is then drawn as a faint ghost of itself. That is done here, by
-- alpha, rather than in the editor by sinking the other colours into the
-- backdrop: a sunk colour is dark, and drawn at strength in front of the picked
-- element it painted it out -- a ring crossing the clock cut a dark stroke
-- through the numerals.
local SPOTLIGHT = orrery_spotlight
local GHOST = 0.08

-- Heat is never drawn on its own; it is what these turn as they run hot.
local SHOWS_HEAT = {cores = true, cage = true, glow = true, gpu = true}

local function emphasis(name)
  if SPOTLIGHT == nil or SPOTLIGHT == name then return 1 end
  if SPOTLIGHT == "heat" and SHOWS_HEAT[name] then return 1 end
  return GHOST
end

-- The alpha to draw at, given an element's emphasis: unchanged at 1, and for a
-- ghost that share of it, capped at full strength first.
local function share(alpha, e)
  return e < 1 and math.min(alpha, 1) * e or alpha
end

-- Conky yields an empty string for a sensor or mount point that is not there.
-- Without this, one missing reading would abort the whole draw.
local function number_or(value, default)
  return tonumber(value) or default
end

---------------- SENSORS ----------------
-- This block is a verbatim slice of lua_widgets_modernized.lua. Conky shares one
-- Lua state across every script it loads and resolves lua_load against the
-- config file's own directory, so there is no import to share it with; a fix
-- here has to be applied to all three scripts.

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

-- Never put a body in orbit with no sensor behind it; it would never move.
local function body_count()
  ensure_sensors()
  local available = #(cpu_sensors or {})
  local wanted = number_of_physical_CPU_cores
  if wanted <= 0 then wanted = available end
  return math.min(wanted, available)
end

---------------- A SUB-SECOND WALL CLOCK ----------------
-- Animation needs a smooth, monotonic clock and Lua offers neither: os.time()
-- has one-second resolution, and os.clock() measures CPU time consumed rather
-- than time passed, so it crawls while the widget is idle. /proc/uptime is a
-- monotonic wall clock in centiseconds, which is exactly the missing piece.
--
-- Wall time is then uptime plus a fixed offset. The offset is derived from
-- os.time(), which is truncated to the second, so it starts out up to a second
-- wrong; it is re-derived whenever it drifts past a quarter second, which
-- corrects it within the first few frames and again after a suspend/resume.

local frame_counter = 0
local epoch_offset = nil
local previous_now = nil

local function wall_clock()
  local uptime = tonumber((read_first_line("/proc/uptime") or ""):match("^(%S+)") or "")

  if uptime == nil then
    -- No procfs: fall back to counting frames, which stays smooth but slows
    -- down along with conky if it cannot keep up with target_fps.
    frame_counter = frame_counter + 1
    return os.time() + (frame_counter / target_fps) % 1
  end

  if epoch_offset == nil or math.abs(uptime + epoch_offset - os.time()) > 1.25 then
    epoch_offset = os.time() - uptime
  end
  return uptime + epoch_offset
end

---------------- SAMPLING ----------------
-- The draw hook runs target_fps times a second but the numbers behind it do not
-- change that fast, and each ${...} costs a parse (and sometimes a subprocess).
-- Values are read once a second and eased towards from every frame, which is
-- both cheaper and better looking: readouts glide instead of stepping.

local SAMPLE_INTERVAL = 1.0

local sampled_at = -1e9
local target = {cpu = 0, mem = 0, root = 0, home = 0, gpu = 0, temps = {}}
local shown  = {cpu = 0, mem = 0, root = 0, home = 0, gpu = 0, temps = {}}

local function used_percent(mount)
  return 100 - number_or(conky_parse("${fs_free_perc " .. mount .. "}"), 100)
end

local function sample(now, bodies)
  if now - sampled_at < SAMPLE_INTERVAL then return end
  sampled_at = now

  target.cpu  = number_or(conky_parse("${cpu cpu0}"), 0)
  target.mem  = number_or(conky_parse("${memperc}"), 0)
  target.root = used_percent(root_filesystem)
  target.home = used_percent(home_filesystem)
  if show_gpu then target.gpu = gpu_temperature() end

  for i = 1, bodies do target.temps[i] = cpu_temperature(i) end
end

-- Exponential approach, framerate independent: tau is the time constant in
-- seconds, so the same easing looks the same at 10fps and at 60.
local function ease(current, goal, dt, tau)
  return current + (goal - current) * (1 - math.exp(-dt / tau))
end

local function ease_all(dt, bodies)
  shown.cpu  = ease(shown.cpu,  target.cpu,  dt, 0.45)
  shown.mem  = ease(shown.mem,  target.mem,  dt, 0.45)
  shown.root = ease(shown.root, target.root, dt, 0.45)
  shown.home = ease(shown.home, target.home, dt, 0.45)
  shown.gpu  = ease(shown.gpu,  target.gpu,  dt, 0.8)
  for i = 1, bodies do
    shown.temps[i] = ease(shown.temps[i] or target.temps[i] or 0,
                          target.temps[i] or 0, dt, 0.8)
  end
end

---------------- THE 3D ENGINE ----------------
-- Row-major 3x3 matrices held as nine numbers in a flat table. Only a handful
-- are built per frame -- one for the camera, one per hoop and one per orbit --
-- and every point then costs a single matrix-vector multiply.

local function mat_identity()
  return {1, 0, 0, 0, 1, 0, 0, 0, 1}
end

local function mat_mul(a, b)
  return {
    a[1]*b[1] + a[2]*b[4] + a[3]*b[7], a[1]*b[2] + a[2]*b[5] + a[3]*b[8], a[1]*b[3] + a[2]*b[6] + a[3]*b[9],
    a[4]*b[1] + a[5]*b[4] + a[6]*b[7], a[4]*b[2] + a[5]*b[5] + a[6]*b[8], a[4]*b[3] + a[5]*b[6] + a[6]*b[9],
    a[7]*b[1] + a[8]*b[4] + a[9]*b[7], a[7]*b[2] + a[8]*b[5] + a[9]*b[8], a[7]*b[3] + a[8]*b[6] + a[9]*b[9],
  }
end

local function rot_x(a)
  local c, s = cos(a), sin(a)
  return {1, 0, 0, 0, c, -s, 0, s, c}
end

local function rot_y(a)
  local c, s = cos(a), sin(a)
  return {c, 0, s, 0, 1, 0, -s, 0, c}
end

local function rot_z(a)
  local c, s = cos(a), sin(a)
  return {c, -s, 0, s, c, 0, 0, 0, 1}
end

-- Distance from the eye to the centre of the assembly, in design units. The
-- hoops reach out to about 272, so this is a long lens: enough perspective for
-- the near side to read as nearer, not enough to bow the hoops out of shape.
local FOCAL = 1240

-- Set once per frame by draw_function.
local camera = mat_identity()
local model = mat_identity()      -- camera * (whatever the current object is)
local centre_x, centre_y, unit = 0, 0, 1

local function set_model(m)
  model = m and mat_mul(camera, m) or camera
end

-- Returns screen x, screen y, view-space depth, and the perspective factor.
-- Depth grows away from the eye, so the painter sorts on it descending.
local function project(x, y, z)
  local vx = model[1]*x + model[2]*y + model[3]*z
  local vy = model[4]*x + model[5]*y + model[6]*z
  local vz = model[7]*x + model[8]*y + model[9]*z
  local s = FOCAL / (FOCAL + vz)
  return centre_x + vx * s * unit, centre_y + vy * s * unit, vz, s
end

-- How much brightness something keeps at this depth. Straight linear fade
-- across the depth of the assembly: the far side recedes, the near side is
-- full strength. This is what stops a wireframe reading as a flat tangle.
-- Text is the exception and never goes through here -- see draw_hoop_labels.
local DEPTH_REFERENCE = 272

-- `reference` is the half-depth of the object being drawn. Fading everything
-- against the whole scene would leave a small object -- the cage is barely a
-- third of the scene's depth -- spanning only the middle of the ramp, so its
-- back would come out nearly as bright as its front and it would read as a
-- solid ball rather than as a cage. Objects that need the effect pass their
-- own radius instead.
local function fade_within(vz, reference)
  local t = clamp(0.5 - vz / (2 * reference), 0, 1)
  return depth_fade + (1 - depth_fade) * t
end

local function fade(vz)
  return fade_within(vz, DEPTH_REFERENCE)
end

---------------- THE PAINTER'S LIST ----------------
-- Primitives are accumulated into one flat list, sorted back to front and
-- painted in that order. The tables are pooled and reused forever: at 20fps a
-- fresh table per primitive would mean tens of thousands of allocations a
-- second, and the garbage collector pauses would show up as stutter.
--
-- Sorting only the live prefix is the awkward part, because table.sort has no
-- range form and truncating the list would throw the pool away. Instead the
-- unused tail is given a depth of -infinity, which parks it past the near end
-- of a descending sort, and the draw loop stops at the live count.

local SEGMENT, DOT, LABEL, GLOW = 1, 2, 3, 4

local pool = {}
local live = 0

local function by_depth(a, b) return a.depth > b.depth end

-- The element whatever is being put down belongs to, for emphasis().
local drawing = nil

local function slot(depth)
  live = live + 1
  local p = pool[live]
  if p == nil then p = {}; pool[live] = p end
  p.depth = depth
  p.e = emphasis(drawing)
  return p
end

-- A line between two points already in view space.
local function put_segment(x1, y1, x2, y2, depth, colour, alpha, width)
  local p = slot(depth)
  p.kind = SEGMENT
  p.x1, p.y1, p.x2, p.y2 = x1, y1, x2, y2
  p.r, p.g, p.b, p.a, p.w = colour[1], colour[2], colour[3], alpha, width
  return p
end

-- A disc, optionally wrapped in a halo. glow is how many times the radius the
-- halo reaches; 0 is a plain dot.
local function put_dot(x, y, depth, radius, colour, alpha, glow)
  local p = slot(depth)
  p.kind = DOT
  p.x1, p.y1, p.w = x, y, radius
  p.r, p.g, p.b, p.a = colour[1], colour[2], colour[3], alpha
  p.glow = glow or 0
  return p
end

-- A genuine radial gradient. Only the nucleus uses one -- at its size the
-- stacked discs below would show as rings -- and there is exactly one per
-- frame, so the cairo pattern it allocates does not matter.
local function put_glow(x, y, depth, radius, colour, alpha)
  local p = slot(depth)
  p.kind = GLOW
  p.x1, p.y1, p.w = x, y, radius
  p.r, p.g, p.b, p.a = colour[1], colour[2], colour[3], alpha
  return p
end

local function put_label(x, y, depth, text, size, angle, colour, alpha, bold)
  local p = slot(depth)
  p.kind = LABEL
  p.x1, p.y1, p.w = x, y, size
  p.text, p.rot = text, angle or 0
  p.r, p.g, p.b, p.a = colour[1], colour[2], colour[3], alpha
  p.bold = bold or false
  return p
end

-- Halo rings, outermost first. Five flat discs of falling opacity stand in for
-- a radial gradient. A bead is a few pixels across, so five steps are enough to
-- pass for a smooth falloff, and unlike a gradient this allocates no cairo
-- pattern per bead per frame -- which at up to forty beads and twenty frames a
-- second is the difference between free and not. The nucleus is large enough
-- for the steps to show as rings, so it uses put_glow instead.
local HALO_RADIUS = {4.0, 3.2, 2.5, 1.9, 1.4}
local HALO_ALPHA  = {0.035, 0.055, 0.085, 0.130, 0.205}

local extents
local function measure(cr, text)
  extents = extents or cairo_text_extents_t:create()
  cairo_text_extents(cr, text, extents)
  return extents
end

local function select_font(cr, size, bold)
  cairo_select_font_face(cr, font_name, CAIRO_FONT_SLANT_NORMAL,
                         bold and CAIRO_FONT_WEIGHT_BOLD or CAIRO_FONT_WEIGHT_NORMAL)
  cairo_set_font_size(cr, size)
end

-- Labels ride the hoops, so their size changes every frame with perspective and
-- no two frames ask for the same one. Measuring each of them every frame would be
-- around a thousand cairo_text_extents calls a second for a set of strings that
-- never changes, so each string is measured once at a large reference size and
-- the result scaled. Hinting makes that very slightly non-linear -- a fraction
-- of a pixel at these sizes -- which is invisible on centred text.
local REFERENCE_SIZE = 96
local extent_cache = {}

local function extent_for(cr, text, size, bold)
  local key = (bold and "b\0" or "n\0") .. text
  local cached = extent_cache[key]
  if cached == nil then
    select_font(cr, REFERENCE_SIZE, bold)
    local e = measure(cr, text)
    cached = {e.width / REFERENCE_SIZE, e.height / REFERENCE_SIZE, e.x_bearing / REFERENCE_SIZE}
    extent_cache[key] = cached
  end
  return cached[1] * size, cached[2] * size, cached[3] * size
end

local function paint_primitive(cr, p)
  local kind = p.kind
  local a = share(p.a, p.e)

  if kind == SEGMENT then
    cairo_set_source_rgba(cr, p.r, p.g, p.b, a)
    cairo_set_line_width(cr, p.w)
    cairo_new_path(cr)
    cairo_move_to(cr, p.x1, p.y1)
    cairo_line_to(cr, p.x2, p.y2)
    cairo_stroke(cr)

  elseif kind == DOT then
    if p.glow > 0 then
      for i = 1, 5 do
        cairo_set_source_rgba(cr, p.r, p.g, p.b, a * HALO_ALPHA[i])
        cairo_new_path(cr)
        cairo_arc(cr, p.x1, p.y1, p.w * HALO_RADIUS[i] * p.glow, 0, TAU)
        cairo_fill(cr)
      end
    end
    cairo_set_source_rgba(cr, p.r, p.g, p.b, a)
    cairo_new_path(cr)
    cairo_arc(cr, p.x1, p.y1, p.w, 0, TAU)
    cairo_fill(cr)

  elseif kind == GLOW then
    local g = cairo_pattern_create_radial(p.x1, p.y1, 0, p.x1, p.y1, p.w)
    cairo_pattern_add_color_stop_rgba(g, 0.00, p.r, p.g, p.b, a)
    cairo_pattern_add_color_stop_rgba(g, 0.45, p.r, p.g, p.b, a * 0.34)
    cairo_pattern_add_color_stop_rgba(g, 1.00, p.r, p.g, p.b, 0)
    cairo_set_source(cr, g)
    cairo_new_path(cr)
    cairo_arc(cr, p.x1, p.y1, p.w, 0, TAU)
    cairo_fill(cr)
    cairo_pattern_destroy(g)

  else
    local width, height, bearing = extent_for(cr, p.text, p.w, p.bold)
    select_font(cr, p.w, p.bold)
    cairo_save(cr)
    cairo_translate(cr, p.x1, p.y1)
    if p.rot ~= 0 then cairo_rotate(cr, p.rot) end
    cairo_set_source_rgba(cr, p.r, p.g, p.b, a)
    cairo_move_to(cr, -width / 2 - bearing, height / 2)
    cairo_show_text(cr, p.text)
    cairo_restore(cr)
    cairo_new_path(cr)
  end
end

local function flush(cr)
  for i = live + 1, #pool do
    pool[i].depth = -math.huge
  end
  table.sort(pool, by_depth)

  cairo_set_line_cap(cr, CAIRO_LINE_CAP_ROUND)
  for i = 1, live do
    paint_primitive(cr, pool[i])
  end
  live = 0
end

---------------- GEOMETRY ----------------
-- The cage is a geodesic sphere: an icosahedron with every face split into
-- four, giving 42 vertices and 120 edges pushed out onto the unit sphere. It is
-- built once at load, not per frame.

local function icosphere()
  local t = (1 + sqrt(5)) / 2
  local verts = {
    {-1, t, 0}, {1, t, 0}, {-1, -t, 0}, {1, -t, 0},
    {0, -1, t}, {0, 1, t}, {0, -1, -t}, {0, 1, -t},
    {t, 0, -1}, {t, 0, 1}, {-t, 0, -1}, {-t, 0, 1},
  }
  local faces = {
    {1,12,6}, {1,6,2},  {1,2,8},   {1,8,11}, {1,11,12},
    {2,6,10}, {6,12,5}, {12,11,3}, {11,8,7}, {8,2,9},
    {4,10,5}, {4,5,3},  {4,3,7},   {4,7,9},  {4,9,10},
    {5,10,6}, {3,5,12}, {7,3,11},  {9,7,8},  {10,9,2},
  }

  -- Split each edge once, reusing the midpoint the neighbouring face made so
  -- the two halves of a shared edge stay welded together.
  local midpoints = {}
  local function midpoint(a, b)
    local key = (a < b) and (a .. ":" .. b) or (b .. ":" .. a)
    local found = midpoints[key]
    if found then return found end
    local va, vb = verts[a], verts[b]
    verts[#verts + 1] = {(va[1] + vb[1]) / 2, (va[2] + vb[2]) / 2, (va[3] + vb[3]) / 2}
    midpoints[key] = #verts
    return #verts
  end

  local split = {}
  for _, f in ipairs(faces) do
    local a, b, c = f[1], f[2], f[3]
    local ab, bc, ca = midpoint(a, b), midpoint(b, c), midpoint(c, a)
    split[#split + 1] = {a, ab, ca}
    split[#split + 1] = {b, bc, ab}
    split[#split + 1] = {c, ca, bc}
    split[#split + 1] = {ab, bc, ca}
  end

  for _, v in ipairs(verts) do
    local length = sqrt(v[1]^2 + v[2]^2 + v[3]^2)
    v[1], v[2], v[3] = v[1] / length, v[2] / length, v[3] / length
  end

  local seen, edges = {}, {}
  for _, f in ipairs(split) do
    for i = 1, 3 do
      local a, b = f[i], f[i % 3 + 1]
      local key = (a < b) and (a .. ":" .. b) or (b .. ":" .. a)
      if not seen[key] then
        seen[key] = true
        edges[#edges + 1] = {a, b}
      end
    end
  end

  return verts, edges
end

local CAGE_VERTS, CAGE_EDGES = icosphere()

---------------- SCENE ----------------

-- Design-unit radii, outermost first. Every length below is written against a
-- 640px widget and multiplied by `unit` at projection time.
-- Each labelled hoop writes its text just outside itself, so the gap to the
-- next hoop out has to clear a line of type.
local R_YEAR, R_MONTH, R_DOW, R_SECOND = 274, 228, 182, 148
local L_YEAR, L_MONTH, L_DOW = 292, 246, 200
local R_CAGE = 96
local ORBIT_INNER, ORBIT_OUTER = 104, 132
local R_READOUT = 318

-- How finely a hoop is broken into straight segments. Each segment is sorted
-- on its own, which is what lets one hoop pass through another.
local HOOP_STEPS = 96

local MONTHS = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"}
local WEEKDAYS = {"MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"}

local DAYS = {}
for i = 1, 31 do DAYS[i] = tostring(i) end

-- A hoop lies in its own XZ plane, tilted about X and then swung about Y, so
-- the four of them sit at four attitudes and weave through each other as the
-- camera comes round. Tilt and swing both drift, slowly and on periods that do
-- not divide into each other, so the assembly never falls back into the same
-- pose twice.
local HOOPS = {
  {radius = R_YEAR,   tilt = 0,   swing = 0,   drift = 0.9,  wander = 71,  colour = INK.rings,   weight = 2.0, element = "rings"},
  {radius = R_MONTH,  tilt = 62,  swing = 35,  drift = -1.3, wander = 89,  colour = INK.rings,   weight = 1.6, element = "rings"},
  {radius = R_DOW,    tilt = 108, swing = 70,  drift = 1.7,  wander = 103, colour = INK.rings,   weight = 1.6, element = "rings"},
  {radius = R_SECOND, tilt = 145, swing = 110, drift = -2.1, wander = 127, colour = INK.seconds, weight = 1.3, element = "seconds"},
}

local function hoop_matrix(hoop, t)
  local tilt = (hoop.tilt + hoop.drift * 2.2 * sin(TAU * t / hoop.wander)) * RAD
  local swing = (hoop.swing + hoop.drift * 360 * t / 900) * RAD
  return mat_mul(rot_y(swing), rot_x(tilt))
end

-- One hoop: a broken circle of segments, a tick per division, a longer index
-- tick at the division the hoop counts from, and a lit bead at the live value.
-- `value` is continuous -- 14.62 days into the month, 37.5 seconds into the
-- minute -- so the bead creeps rather than stepping. The tail is given in
-- degrees rather than in divisions, because a tail one division long is a
-- visible streak on the twelve-part year hoop and an invisible nub on the
-- sixty-part seconds hoop.
local function draw_hoop(hoop, t, divisions, value, lit_colour, bead_size, tick_length,
                         tail_degrees, snap)
  set_model(hoop_matrix(hoop, t))
  drawing = hoop.element
  -- The rings are drawn at their own opacity. The seconds hoop's opacity is
  -- its lit parts', and its unlit ring keeps the share of that it was given.
  local lit = OPACITY[hoop.element] or 1
  local base = hoop.element == "rings" and OPACITY.rings or 0.16 * scale_of(hoop.element)

  local radius = hoop.radius
  local px, py, pz = project(radius, 0, 0)

  for i = 1, HOOP_STEPS do
    local angle = i * TAU / HOOP_STEPS
    local x, y, z = project(radius * cos(angle), 0, radius * sin(angle))
    put_segment(px, py, x, y, (pz + z) / 2, hoop.colour,
                base * hoop.weight * fade((pz + z) / 2), 1.5 * unit)
    px, py, pz = x, y, z
  end

  -- Every tick is identical. Marking the live division with brighter ticks was
  -- tried and removed: the ticks sit on the hoop while the label is written
  -- outside it and half a division round, so the pair read as two stray dashes
  -- floating near the text rather than as a bracket around it. On a labelled
  -- hoop the lit label is the whole reading, and nothing else on the hoop is
  -- drawn in today's colour. A marked zero went the same way -- a longer
  -- tick nobody can account for is worse than no tick at all.
  for i = 0, divisions - 1 do
    local angle = i * TAU / divisions
    local ca, sa = cos(angle), sin(angle)
    local inner = radius - tick_length / 2
    local outer = radius + tick_length / 2
    local x1, y1, z1 = project(inner * ca, 0, inner * sa)
    local x2, y2, z2 = project(outer * ca, 0, outer * sa)
    local depth = (z1 + z2) / 2
    put_segment(x1, y1, x2, y2, depth, hoop.colour,
                base * 2.4 * hoop.weight * 0.8 * fade(depth), 1.6 * unit)
  end

  -- The live mark. On a labelled hoop it snaps to the middle of the division it
  -- is in, which is exactly where that division's label is written, so the mark
  -- and the lit label sit together and read as one thing: this day, this month,
  -- this weekday. Letting it travel continuously instead is what a dial would
  -- do, but it put the mark almost on FRI by Thursday evening and almost on OCT
  -- by late September -- correct to the hour, and wrong to the eye.
  -- The unlabelled hoop has no division to light and nothing written on it, so
  -- it keeps a travelling head instead, with the minute so far lit behind it
  -- from a bar marking where the minute starts and ends. That is
  -- what makes the seconds sweep read as a sweep, and it is the only hoop where
  -- anything moves between one frame and the next.
  if not snap then
    local fraction = (value % divisions) / divisions
    local angle = TAU * fraction

    -- Where the minute starts and ends: a bar across the hoop, longer than any
    -- tick. Without it there was no telling how far into the minute the head
    -- was, or how much of it was left.
    local x1, y1, z1 = project(radius - 12, 0, 0)
    local x2, y2, z2 = project(radius + 12, 0, 0)
    local bar = (z1 + z2) / 2
    put_segment(x1, y1, x2, y2, bar, lit_colour, lit * fade(bar), 2.8 * unit)

    -- The minute so far, lit from the bar round to the head, so a quarter of
    -- the hoop lit is a quarter of the minute gone. The last few degrees
    -- brighten and thicken into a streak, so the head still reads as the thing
    -- that moves.
    local SPAN = tail_degrees * RAD
    local steps = math.max(1, math.ceil(HOOP_STEPS * fraction))
    local px, py, pz = project(radius, 0, 0)
    for i = 1, steps do
      local a = angle * i / steps
      local x, y, z = project(radius * cos(a), 0, radius * sin(a))
      local depth = (pz + z) / 2
      local streak = clamp(1 - (angle - a) / SPAN, 0, 1)
      put_segment(px, py, x, y, depth, lit_colour,
                  lit * fade(depth) * (0.66 + 0.34 * streak),
                  (2.4 + 1.2 * streak) * unit)
      px, py, pz = x, y, z
    end

    local x, y, z, s = project(radius * cos(angle), 0, radius * sin(angle))
    put_dot(x, y, z, bead_size * s * unit, lit_colour, lit * fade(z), 0.8)
  end
end

-- The labels ride the hoop, each turned to the screen-space tangent so the ring
-- of text leans with it. Sampling a second point a little further round and
-- taking the angle between the two projections is what gives the lean;
-- computing it from the 3D tangent would ignore perspective.
--
-- This is where the date is actually read. The current entry is drawn larger,
-- bold and in today's colour, so month, day and weekday stand out of their
-- rings at a glance and the middle of the widget is left to the clock alone.
-- `count` can be shorter than `labels` -- February uses 28 of the 31 day
-- strings -- so the divisions always match the month in front of you.

-- The clock keeps a box of its own, and a label fades only as it closes on
-- that box. This used to be a circle of radius 104 round the middle, which is
-- the wrong shape for a clock that is wide and short: a label sitting just below
-- the numerals, nowhere near touching them, was inside it and vanished. The
-- box is measured from the widest time the clock shows rather than the current
-- one, so it does not shift as the minutes turn over.
local CLOCK_SIZE, CLOCK_SAMPLE = 56, "00:00"
-- How far outside the clock's box a label starts to fade, in design units.
local KEEPOUT_RAMP = 16
-- Today's labels are moved instead of faded, and kept this far off: pushed
-- only to the edge of the box, a day number beside the clock read as part of
-- the time.
local LIVE_CLEARANCE = 32

-- Half the clock's ink box, in pixels. It scales with the widget, so
-- draw_function sets it every frame.
local clock_half_w, clock_half_h = 0, 0

-- How far a box centred on x, y lies from the clock's, along whichever axis
-- separates the two; negative once they overlap.
local function apart_from_clock(x, y, half_w, half_h)
  return math.max(math.abs(x - centre_x) - (half_w + clock_half_w),
                  math.abs(y - centre_y) - (half_h + clock_half_h))
end

local label_points = {}

local function draw_hoop_labels(cr, hoop, t, radius, labels, count, current, size)
  set_model(hoop_matrix(hoop, t))

  local ramp = KEEPOUT_RAMP * unit

  for i = 1, count do
    -- Half a division round, because a label names the sector that follows its
    -- tick, not the tick itself. Writing THU on the Wednesday/Thursday boundary
    -- puts the pointer almost on FRI by Thursday evening, which reads as the
    -- wrong day; centred in its sector, the pointer spends all of Thursday
    -- travelling past THU, which is what it means.
    local angle = (i - 0.5) * TAU / count
    local x, y, z, s = project(radius * cos(angle), 0, radius * sin(angle))
    local point = label_points[i]
    if point == nil then point = {}; label_points[i] = point end
    point[1], point[2], point[3], point[4] = x, y, z, s
  end

  for i = 1, count do
    local point = label_points[i]
    local before = label_points[(i - 2) % count + 1]
    local after = label_points[i % count + 1]
    local live = (i == current)
    local scale = (live and size * 1.4 or size) * point[4] * unit
    local width, height = extent_for(cr, labels[i], scale, live)

    -- The tangent comes from the neighbours either side rather than from a
    -- second sample point: every anchor is projected already, so it is free,
    -- and averaging across two divisions is steadier than a short chord.
    local lean = atan2(after[2] - before[2], after[1] - before[1])
    -- Keep every label the right way up instead of letting half the ring hang
    -- upside down at the far side.
    if lean > pi / 2 then lean = lean - pi elseif lean < -pi / 2 then lean = lean + pi end

    -- Declutter, in two parts. Where a hoop turns edge-on its divisions crowd
    -- into a knot, so a label fades out as its neighbour stops leaving it room
    -- to be read -- which means a hoop's labels quietly come and go as it
    -- precesses, and only the legible ones are ever on screen. The current
    -- value is exempt: when the rest of a ring has faded it is the one thing
    -- still worth showing, and it is drawn larger and on top in any case.
    local room = 1
    if not live then
      local gap = sqrt((after[1] - point[1])^2 + (after[2] - point[2])^2)
      room = clamp(gap / (width * 1.35), 0, 1)
      room = room * room
    end

    -- The second part protects the clock, which a steeply tilted hoop would
    -- otherwise run its labels straight across. What is compared is the
    -- label's box, turned to its lean, against the clock's. The current value
    -- is not faded out here but pushed out instead: fading it would mean that
    -- every time a hoop came edge-on the one thing worth reading off it --
    -- today's date -- was the thing that disappeared. Sliding it out along its
    -- own direction from the middle keeps it on the hoop's projected line,
    -- which is where the eye expects it.
    local x, y = point[1], point[2]
    local ca, sa = math.abs(cos(lean)), math.abs(sin(lean))
    local half_w = (ca * width + sa * height) / 2
    local half_h = (sa * width + ca * height) / 2
    local clear = 1

    if live then
      local gap = LIVE_CLEARANCE * unit
      if apart_from_clock(x, y, half_w, half_h) < gap then
        local dx, dy = x - centre_x, y - centre_y
        local distance = sqrt(dx * dx + dy * dy)
        local ux, uy
        if distance > 1 then
          ux, uy = dx / distance, dy / distance
        else
          -- Dead centre, so its own direction says nothing: borrow the
          -- neighbour's, which points along the hoop.
          local ax, ay = after[1] - centre_x, after[2] - centre_y
          local along = sqrt(ax * ax + ay * ay)
          if along > 1 then ux, uy = ax / along, ay / along else ux, uy = 0, -1 end
        end
        -- Out to where the line from the middle leaves the clock's box, grown
        -- by this label and the gap, through whichever edge it meets first.
        local least = math.min(
          math.abs(ux) > 1e-6 and (clock_half_w + half_w + gap) / math.abs(ux) or math.huge,
          math.abs(uy) > 1e-6 and (clock_half_h + half_h + gap) / math.abs(uy) or math.huge)
        x, y = centre_x + ux * least, centre_y + uy * least
      end
    else
      clear = clamp(apart_from_clock(x, y, half_w, half_h) / ramp, 0, 1)
    end

    -- Text is not depth-faded. The far side of a hoop already recedes through
    -- its line and its ticks, and the type shrinks with perspective; dimming
    -- the words as well bought a little depth at the cost of the reading.
    -- Today's month at the back of its hoop came out at about a quarter
    -- strength, fainter than the orbiting cores in front of it, so the one
    -- thing the hoop is there to say was the hardest thing on it to find.
    local alpha = (live and OPACITY.today or OPACITY.labels) * room * clear
    drawing = live and "today" or "labels"
    if alpha > 0.02 then
      -- The highlight belongs to the label rather than sitting beside it as a
      -- separate mark, so there is nothing on screen to mistake for a core: the
      -- thing that is lit up *is* the day, the month, the weekday.
      if live then
        put_glow(x, y, point[3] + 1, scale * 2.1, INK.today, 0.34 * scale_of("today"))
      end
      put_label(x, y, point[3], labels[i], scale, lean,
                live and INK.today or INK.labels, alpha, live)
    end
  end
end

-- The cage around the clock. It turns on its own axis, and breathes with CPU
-- load so a busy machine visibly swells; its colour carries the hottest core.
-- Projected cage vertices, reused between frames for the same reason the
-- primitive pool is.
local screen = {}

local function draw_cage(cr, t, hottest)
  local load = clamp(shown.cpu / 100, 0, 1)
  local radius = R_CAGE * (1 + 0.20 * load + 0.03 * sin(TAU * t / 6.3))
  local heat = clamp((hottest - warm_above) / math.max(max_temperature - warm_above, 1), 0, 1)

  -- The glow is drawn whether or not the cage is: with the cage switched off it
  -- is the one thing left saying how hot the hottest core is.
  if show_glow_now then
    drawing = "glow"
    -- A soft nucleus behind the numerals: almost all halo and hardly any disc,
    -- so the clock reads against a glow rather than against a painted ball.
    local x, y = centre_x, centre_y
    put_glow(x, y, R_CAGE + 1, radius * 1.55 * unit, mix(INK.glow, INK.heat, heat),
             (0.21 + 0.15 * load) * scale_of("glow"))
  end
  if not show_cage_now then return end

  drawing = "cage"
  local colour = mix(INK.cage, INK.heat, heat)
  set_model(mat_mul(rot_y(t * 0.28 * motion), rot_x(t * 0.17 * motion + 0.6)))

  for i, v in ipairs(CAGE_VERTS) do
    local x, y, z, s = project(v[1] * radius, v[2] * radius, v[3] * radius)
    local projected = screen[i]
    if projected == nil then projected = {}; screen[i] = projected end
    projected[1], projected[2], projected[3], projected[4] = x, y, z, s
  end

  for _, e in ipairs(CAGE_EDGES) do
    local a, b = screen[e[1]], screen[e[2]]
    local depth = (a[3] + b[3]) / 2
    put_segment(a[1], a[2], b[1], b[2], depth, colour,
                (0.16 * 1.45 + 0.16 * load) * scale_of("cage") * fade_within(depth, radius),
                1.0 * unit)
  end

  for _, v in ipairs(screen) do
    put_dot(v[1], v[2], v[3], 1.4 * v[4] * unit, colour,
            0.42 * scale_of("cage") * fade_within(v[3], radius), 0)
  end
end

---------------- ORBITING BODIES ----------------
-- One per CPU core. The orbit is fixed; the speed is not -- a body goes round
-- faster the hotter its core is, so the whole sky quickens under load. Phase is
-- integrated frame by frame rather than computed from the clock, because a
-- body whose speed just changed must carry on from where it is instead of
-- jumping to where a constant-speed body would have been.

local ORBIT_SLOWEST, ORBIT_FASTEST = 34, 5.5   -- seconds per revolution

local orbits = {}

local function ensure_orbits(count)
  for i = #orbits + 1, count do
    local spread = (count > 1) and (i - 1) / (count - 1) or 0.5
    orbits[i] = {
      radius = ORBIT_INNER + spread * (ORBIT_OUTER - ORBIT_INNER),
      phase = (i * 0.6180339) % 1 * TAU,
      -- A golden angle between orbital planes, so no two bodies share one and
      -- the set never settles into a visible pattern however many cores there
      -- are. The plane itself never moves -- only the body's position in it --
      -- so this is built once instead of twice a body per frame.
      plane = mat_mul(rot_y((i - 1) * 137.507 * RAD),
                      rot_x(((i * 47.3) % 116 - 58) * RAD)),
    }
  end
end

local function draw_bodies(t, dt, count)
  ensure_orbits(count)
  drawing = "cores"

  for i = 1, count do
    local orbit = orbits[i]
    local temperature = shown.temps[i] or 0
    local heat = clamp(temperature / max_temperature, 0, 1)
    local period = ORBIT_SLOWEST + (ORBIT_FASTEST - ORBIT_SLOWEST) * heat
    local omega = TAU / period * motion

    orbit.phase = (orbit.phase + omega * dt) % TAU

    set_model(orbit.plane)
    local colour = heat_color(temperature, INK.cores)
    local radius = orbit.radius

    -- Tail length follows speed, so a hot core draws a long comet and an idle
    -- one a short spark.
    local TAIL = 7
    local span = clamp(omega * 0.5, 0.10, 0.85)
    for k = TAIL, 1, -1 do
      local a = orbit.phase - span * k / TAIL
      local x, y, z, s = project(radius * cos(a), 0, radius * sin(a))
      put_dot(x, y, z, 2.0 * s * unit, colour,
              OPACITY.cores * fade(z) * (1 - k / (TAIL + 1)) * 0.55, 0)
    end

    local x, y, z, s = project(radius * cos(orbit.phase), 0, radius * sin(orbit.phase))
    put_dot(x, y, z, 2.9 * s * unit, colour, OPACITY.cores * fade(z), 0.8 + 0.6 * heat)
  end
end

---------------- DUST ----------------
-- Fixed points in the scene, turning with the camera. They carry no data; they
-- are there so the eye gets parallax and reads the assembly as a volume rather
-- than as overlapping circles.

local dust = {}

local function ensure_dust()
  if #dust > 0 then return end
  -- A fixed seed, so the same machine draws the same sky every restart.
  local seed = 20240224
  local function rand()
    seed = (seed * 1103515245 + 12345) % 2147483648
    return seed / 2147483648
  end
  for i = 1, dust_count do
    -- Sampled on a shell, not in a box: an even spread over the sphere needs
    -- z uniform and the angle uniform, not two uniform angles.
    local z = rand() * 2 - 1
    local a = rand() * TAU
    local r = 150 + rand() * 160
    local ring = sqrt(1 - z * z)
    dust[i] = {r * ring * cos(a), r * ring * sin(a), r * z, 0.3 + rand() * 0.7}
  end
end

local function draw_dust(t)
  ensure_dust()
  set_model(rot_y(t * 0.04 * motion))
  drawing = "dust"
  for _, d in ipairs(dust) do
    local x, y, z, s = project(d[1], d[2], d[3])
    put_dot(x, y, z, (dust_size or 1.1) * d[4] * s * unit, INK.dust,
            OPACITY.dust * d[4] * fade(z), 0)
  end
end

---------------- READOUTS ----------------
-- A flat frame around the 3D assembly: four quadrant arcs with their values
-- written into the corners the circle leaves empty. These are drawn straight to
-- cairo after the painter's list has been flushed, because they are a HUD and
-- must never be occluded by the scene behind them.

local function text_at(cr, x, y, text, size, colour, alpha, bold)
  local width, _, bearing = extent_for(cr, text, size, bold)
  select_font(cr, size, bold)
  cairo_set_source_rgba(cr, colour[1], colour[2], colour[3], alpha)
  cairo_move_to(cr, x - width / 2 - bearing, y)
  cairo_show_text(cr, text)
  cairo_new_path(cr)
end

-- Angular gap left between one readout and the next.
local READOUT_GAP = 16 * RAD

local function draw_readout(cr, middle, span, fraction, colour, caption, value, name)
  local radius = R_READOUT * unit
  local from = middle - span / 2
  -- Drawn straight to cairo rather than through the painter, so a ghost is
  -- worked out here; outside the editor's preview every factor is 1.
  local mine, tracks, captions = emphasis(name), emphasis("tracks"), emphasis("captions")

  cairo_set_line_cap(cr, CAIRO_LINE_CAP_ROUND)
  cairo_set_line_width(cr, 3.5 * unit)

  cairo_set_source_rgba(cr, INK.tracks[1], INK.tracks[2], INK.tracks[3],
                        share(OPACITY.tracks, tracks))
  cairo_new_path(cr)
  cairo_arc(cr, centre_x, centre_y, radius, from, from + span)
  cairo_stroke(cr)

  if fraction > 0 then
    cairo_set_source_rgba(cr, colour[1], colour[2], colour[3], share(OPACITY[name], mine))
    cairo_new_path(cr)
    cairo_arc(cr, centre_x, centre_y, radius, from, from + span * clamp(fraction, 0, 1))
    cairo_stroke(cr)
  end

  local tx = centre_x + cos(middle) * (R_READOUT + 34) * unit
  local ty = centre_y + sin(middle) * (R_READOUT + 34) * unit
  text_at(cr, tx, ty - 9 * unit, caption, 10 * unit, INK.captions,
          share(OPACITY.captions, captions))
  text_at(cr, tx, ty + 15 * unit, value, 21 * unit, colour,
          share(0.92 * scale_of(name), mine), true)
end

-- Reused between frames rather than rebuilt, like everything else here.
local readouts = {}

local function readout(index, caption, fraction, colour, value, name)
  local entry = readouts[index]
  if entry == nil then entry = {}; readouts[index] = entry end
  entry.caption, entry.fraction, entry.colour, entry.value = caption, fraction, colour, value
  entry.name = name
end

-- Both filesystems get a slot, and the GPU keeps its own when there is a sensor
-- for it, so the set is five or four depending on the machine. They are spaced
-- evenly from the top rather than pinned to the four corners, which is what
-- lets the count vary without the layout having to be redesigned for each one.
--
-- As shipped, colour says what kind of thing a readout is, never where it sits:
-- cyan for what the machine is doing this second, raspberry for how full its
-- disks are, and the heat ramp for anything measured in degrees. So CPU and MEM
-- match each other, ROOT and HOME match each other, and the GPU is the only one
-- that changes colour as its value moves. Each still has a setting of its own,
-- so a pair can be split.
local function draw_readouts(cr)
  local count = 0

  count = count + 1
  readout(count, "CPU", shown.cpu / 100, INK.cpu, floor(shown.cpu + 0.5) .. "%", "cpu")
  count = count + 1
  readout(count, "MEM", shown.mem / 100, INK.memory, floor(shown.mem + 0.5) .. "%", "memory")
  if show_gpu then
    count = count + 1
    readout(count, "GPU", shown.gpu / max_temperature, heat_color(shown.gpu, INK.gpu),
            floor(shown.gpu + 0.5) .. "°C", "gpu")
  end
  count = count + 1
  readout(count, "ROOT", shown.root / 100, INK.root, floor(shown.root + 0.5) .. "%", "root")
  count = count + 1
  readout(count, "HOME", shown.home / 100, INK.home, floor(shown.home + 0.5) .. "%", "home")

  local step = TAU / count
  for i = 1, count do
    local entry = readouts[i]
    draw_readout(cr, -pi / 2 + (i - 1) * step, step - READOUT_GAP,
                 entry.fraction, entry.colour, entry.caption, entry.value, entry.name)
  end
end

---------------- FRAME ----------------

-- Every length above is written against this width and multiplied by `unit`.
local BASE_DIAMETER = 640
-- What the widget actually spans. The readout captions and values reach well
-- outside the hoops. Without them the outermost ink is the glow behind this
-- month's label, which reaches about 335 units when the label sits at the
-- widest point of its hoop -- measured from renders over a full turn of the
-- camera, since perspective takes it past the hoop's own radius.
local CONTENT_DIAMETER = show_readouts_now and 2 * (R_READOUT + 52) or 2 * 340

local warned = false
local function warn_once(available, needed)
  if warned then return end
  warned = true
  io.stderr:write(string.format(
    "conky lua_orrery: the widget wants a %dpx window; this one is %dpx, so it has been " ..
    "scaled down. Raise the size at the top of start_conky_orrery.\n",
    math.ceil(needed), floor(available)))
end

local function draw_function(cr, now, dt)
  local w, h = conky_window.width, conky_window.height
  local width, height = w - x_rel_pos, h - y_rel_pos
  centre_x, centre_y = width / 2, height / 2

  -- widget_size is the diameter of the outermost hoop, but the readout values
  -- are written outside it, so when they are shown what has to fit is wider
  -- than the number the user set.
  local needed = widget_size * CONTENT_DIAMETER / BASE_DIAMETER
  local available = math.min(width, height)
  local fit = math.min(1, available / needed)
  if fit < 0.99 then warn_once(available, needed) end
  unit = widget_size / BASE_DIAMETER * fit

  -- With the monitoring off nothing is sampled: no ${...} is parsed and hwmon
  -- is never scanned, so the clock and calendar cost only their drawing.
  local bodies = 0
  if show_monitoring_now then
    bodies = body_count()
    sample(now, bodies)
    ease_all(dt, bodies)
  end

  -- Animation runs on `t`, which is wall time scaled by `motion`, so setting
  -- motion to 0 parks the assembly without stopping the clock.
  local t = now * motion

  local yaw = TAU * t / camera_turn_seconds
  local pitch = (camera_pitch + camera_rock * sin(TAU * t / camera_rock_seconds)) * RAD
  local roll = 2.2 * sin(TAU * t / 83) * RAD
  camera = mat_mul(rot_z(roll), mat_mul(rot_x(pitch), rot_y(yaw)))

  local when = os.date("*t", floor(now))
  local seconds = when.sec + (now - floor(now))
  local minutes = when.min + seconds / 60
  local hours = when.hour + minutes / 60
  local days = days_in_current_month()
  local day_fraction = hours / 24

  local hottest = 0
  for i = 1, bodies do
    local temperature = shown.temps[i] or 0
    if temperature > hottest then hottest = temperature end
  end

  -- Back to front is the sorter's job, not the caller's: these run in whatever
  -- order reads best here and land in the right place anyway.
  if show_dust_now then draw_dust(t) end

  -- The clock is drawn centred on the middle of the widget, ink box and all,
  -- and that box is what the labels are kept off.
  local clock_w, clock_h = extent_for(cr, CLOCK_SAMPLE, CLOCK_SIZE * unit, false)
  clock_half_w, clock_half_h = clock_w / 2, clock_h / 2

  -- Monday-first, to match WEEKDAYS; os.date numbers Sunday 1.
  local weekday = (when.wday == 1) and 7 or (when.wday - 1)

  draw_hoop(HOOPS[1], t, 12, (when.month - 1) + (when.day - 1 + day_fraction) / days,
            INK.today, 3.4, 9, 7, true)
  draw_hoop_labels(cr, HOOPS[1], t, L_YEAR, MONTHS, 12, when.month, 11.5)

  draw_hoop(HOOPS[2], t, days, (when.day - 1) + day_fraction, INK.today, 3.2, 7, 9, true)
  draw_hoop_labels(cr, HOOPS[2], t, L_MONTH, DAYS, days, when.day, 10)

  draw_hoop(HOOPS[3], t, 7, (weekday - 1) + day_fraction, INK.today, 3.4, 8, 11, true)
  draw_hoop_labels(cr, HOOPS[3], t, L_DOW, WEEKDAYS, 7, weekday, 12)

  -- The seconds hoop carries no labels: it is the one element moving fast
  -- enough to watch, and is there to be read as a sweep rather than a value.
  if show_seconds_now then
    draw_hoop(HOOPS[4], t, 60, seconds, INK.seconds, 3.4, 5, 26, false)
  end

  if show_monitoring_now then
    draw_cage(cr, t, hottest)
    draw_bodies(t, dt, bodies)
  end

  -- The clock sits on the plane through the centre of the scene, so the near
  -- half of the cage and any hoop swinging towards the eye cross in front of
  -- the numerals while the far half stays behind them. That one line is the
  -- whole reason the renderer sorts text along with everything else.
  local x, y = project(0, 0, 0)
  drawing = "clock"
  put_label(x, y, 0, os.date("%H:%M", floor(now)), CLOCK_SIZE * unit, 0,
            INK.clock, OPACITY.clock, false)

  flush(cr)

  if show_readouts_now then draw_readouts(cr) end
end

local last_now = nil

function conky_start_widgets()
  if conky_window == nil then return end

  -- Conky needs a moment before conky_window is usable and before ${cpu} means
  -- anything. The threshold is in updates, so it is written against target_fps
  -- to come out at about a second whatever rate the config runs at.
  if number_or(conky_parse('${updates}'), 0) <= target_fps then return end

  local now = wall_clock()
  -- A frame that arrives late -- the machine was busy, or the widget was on a
  -- hidden workspace -- must not teleport everything that integrates over dt.
  local dt = clamp(now - (last_now or now), 0, 0.25)
  last_now = now

  local cs, owns_surface = conky_window_surface()
  local cr = cairo_create(cs)

  draw_function(cr, now, dt)

  cairo_destroy(cr)
  if owns_surface then cairo_surface_destroy(cs) end
end
