# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A collection of four independent Conky desktop-widget themes. There is no build system, package
manager, test suite, or linter — each theme is a Conky config plus a Lua script that draws with
cairo, installed by copying files into `~/.conky/`.

- `Conky-Weather/` — OpenWeatherMap temperature + icon (Lua + a Python helper)
- `Conky-Revisited-2/` — battery/disk/CPU/RAM panel, in four layout variants
- `Conky-Calendar-Extra/` — circular calendar/clock + per-core CPU temperatures, in two looks
- `Conky_Orrery/` — animated software-3D armillary clock, plus a tkinter colour editor with a
  live preview. Grew out of Conky-Calendar-Extra and still shares its sensor code; it is the
  only theme here that is not installed into `~/.conky/` and the only one with a GUI.

The top-level `README.md` also advertises two themes that live in *other* repositories
(`conky-drawer-interactive`, `conky-pywal`) — they are not in this tree.

## Install and run

Each theme installs to `~/.conky/` and is then run from there. Conky must be started from the
installed location, not the repo.

```bash
cd Conky-Weather && sh install.sh && ~/.conky/Conky-Weather/start_weather.sh &
```

```bash
cd Conky-Revisited-2 && sh install.sh && cd ~/.conky/Conky_Revisited_2/Conky_Square_Vertical && conky -c start_conky_General_VERTICAL
```

```bash
cd Conky-Calendar-Extra/conky && conky -c start_conky
```

```bash
cd Conky_Orrery && conky -c start_conky_orrery
```

`Conky-Calendar-Extra` has no installer. Its `lua_load` is relative, but conky resolves that
against the **config file's own directory**, not the working directory — so `conky -c
/abs/path/start_conky` works from anywhere, while copying the config away from its `.lua` breaks it.
`start_conky_modernized` is the restyled variant. The same applies to `Conky_Orrery`, whose
`start_conky_orrery` and `lua_orrery.lua` must stay in the same directory.

## The edit/run gotcha

`install.sh` **copies** into `~/.conky/`, and the `lua_load` lines in Conky-Weather and
Conky-Revisited-2 point at absolute `~/.conky/...` paths. Editing a `settings.lua` in the repo has
no effect on a running widget until you re-run `install.sh`. When iterating, either re-install after
every change, or edit the installed copy and port the change back. Each Revisited-2 variant's conky
file carries a commented-out `lua_load = 'settings.lua'` for running in place.

## Verifying a change

There is nothing to test but the rendering. Run conky in the foreground and read stderr — Lua errors
surface there and the widget simply draws nothing:

```bash
pkill conky; cd ~/.conky/Conky-Weather && conky -c conky_config
```

Conky-Revisited-2 and Conky-Calendar-Extra skip drawing until `${updates} > 5`, so allow a few
seconds before concluding a change did nothing. `lua_orrery.lua` waits for `${updates} >
target_fps` instead, because at its `update_interval` of 1/20s five updates is a quarter of a
second and conky_window is not ready that early — the threshold has to be written in updates but
mean about a second.

Do not `pkill conky` on this machine: the user runs their own widgets. Start a test instance,
note its pid, and kill that pid alone.

The KDE Wayland session here will not let an X11 client grab the screen, so `import`, `xwd` and
`ffmpeg -f x11grab` all come back blank — a screenshot of a running conky cannot be taken from
this session. Render instead: drive the script's own `draw_function` against a
`cairo_image_surface_create` surface with a stub `conky_surface()`, which is also far faster to
iterate on. Plain `lua` loads the bindings with `package.cpath = "/usr/lib64/conky/lib?.so"` —
note the `lib` prefix, since the module is `libcairo.so` rather than `cairo.so`.

For the orrery that harness is already written and shipped as
`Conky_Orrery/orrery_preview.lua`; use it rather than rebuilding one:

```bash
cd Conky_Orrery && lua orrery_preview.lua lua_orrery.lua /tmp/out.png 900 0
```

It intercepts `io.open` for `/proc/uptime` and overrides `os.time`, because the animation clock
comes from those and without a synthetic one every frame lands at the same instant and the eased
readouts never leave zero. hwmon reads are deliberately *not* intercepted, so a render shows the
machine's real core count and temperatures. It draws 14 settling frames at dt 0.25s before the one
it keeps — the readouts ease with a 0.45s time constant, and dt is clamped to 0.25s inside the
widget, so that is the cheapest way to arrive at settled values. It clears the surface between
those frames, because conky repaints the whole window each tick and without clearing 14 frames of
glow pile up and the render comes out far brighter than the real thing.

A GUI cannot be screenshotted on the Wayland session either, but unlike conky it can be pointed at
a virtual X server that *can* be captured — `Xvfb :99 -screen 0 1400x1000x24`, run the program with
`DISPLAY=:99`, then `ffmpeg -f x11grab -draw_mouse 0`. Without `-draw_mouse 0` the pointer is baked
into the middle of the grab.

Do not use `pkill -f` to clean up test processes here. The Bash tool wraps each command in a shell
whose own command line contains the pattern, so `pkill -f 'conky -c start_conky_orrery'` kills the
wrapper and the call returns 144. Collect pids first (`pgrep -x`, or read `/proc/*/cmdline`) and
kill those.

## Architecture

Every theme follows the same three-layer shape:

1. **Conky config** (`conky_config`, `start_conky*`) declares window properties plus
   `lua_load = <script>` and `lua_draw_hook_pre = 'start_widgets'`. `conky.text` is empty — nothing
   is drawn by Conky's own text engine.
2. **`conky_start_widgets()`** is the entry point Conky calls each `update_interval`. It builds a
   `cairo_xlib_surface_create` from `conky_window`, creates a context, calls `draw_function(cr)`,
   and destroys both. This boilerplate is near-identical in all themes.
3. **`draw_function(cr)`** lays out the whole widget in absolute pixel coordinates derived from
   `conky_window.width/height`, calling small `draw_*` helpers. There is no layout engine; positions
   are hand-tuned constants and nudge offsets.

All live data enters through `conky_parse('${...}')` — either Conky's own variables (`${memperc}`,
`${battery_percent BAT0}`, `${fs_free_perc /}`) or `${exec ...}` shelling out to `sensors`,
`nvidia-smi`, or `openweather.py`. Every one of these runs on every redraw, so anything obtainable
from Lua directly (dates, calendar arithmetic) belongs in Lua rather than an `${exec}`.

### Conky config syntax

All six configs use the 1.10+ Lua-table syntax (`conky.config = { ... }; conky.text = [[ ]]`).
Conky-Calendar-Extra was converted from the pre-1.10 `key value` / `TEXT` format; older copies of
that file found elsewhere will still be in the old format, and while conky can auto-convert them at
startup, that rescue path depends on the optional `old configuration syntax` build feature and emits
a syntax-error warning every launch. Convert instead, with `convert.lua` from conky's doc directory.

Settings get retired regularly, and transparency is where it bites: `own_window_transparent` is
deprecated and `own_window_argb_visual` has been removed outright ("ARGB is now always enabled when
available"). Both are gone from this repo — transparency is now `own_window_colour = '#00000000'`
alone.

Verify config changes by actually running conky for ~12s and reading the log, not by a headless
check. `DISPLAY= conky -c <file>` only exercises the settings parsed before X initialises; the
removal notice for `own_window_argb_visual` and every Lua error arrive *after* that point, so a
headless run reports "clean" on a config that fails in practice. `draw_function` also only starts
once `${updates} > 5`, so a run shorter than ~6s never executes the drawing code at all.

### The Lua cairo bindings moved

Two breaking changes hit every `settings.lua`/`lua_widgets.lua` in this repo on Conky 1.24:

- `cairo_xlib_surface_create` is no longer in the `cairo` module. It lives in a separate
  `cairo_xlib` module (`/usr/lib64/conky/libcairo_xlib.so`), so `require 'cairo'` alone leaves it
  `nil` and every draw dies with `attempt to call a nil value`. Each script now also does
  `pcall(require, 'cairo_xlib')` — pcall because older builds have no such module and still export
  the function from `cairo`.
- `cairo_xlib_surface_create` is itself deprecated in favour of `conky_surface()`. The two differ in
  ownership, which matters: `conky_surface()` returns the *same cached surface* on every call and
  conky owns it, so calling `cairo_surface_destroy` on it is wrong, whereas an xlib surface is
  created per draw and must be freed. `conky_window_surface()` in each script returns the surface
  plus an `owns_surface` flag that drives whether the destroy happens.

`conky_window` is nil on the very first draw hook and only becomes a table a few updates in — that
is what the `if conky_window == nil then return end` guard is for; don't remove it.

### Conky-Calendar-Extra ships two looks, and Conky_Orrery is a third descended from them

`lua_widgets.lua` + `start_conky` is the original dial; `lua_widgets_modernized.lua` +
`start_conky_modernized` is a restyled copy. `Conky_Orrery/lua_orrery.lua` is an animated 3D one
that started here and now lives in its own top-level folder. All three are driven from the same
sensor and scaling code (the `require`/`conky_window_surface`, hwmon and `days_in_current_month`
blocks were sliced out of the original verbatim, so **fixes to those must be applied to all three,
across two directories**). There is no shared module and cannot easily be one: conky shares a
single Lua state across every script it loads, and resolves `lua_load` against the config file's
directory while `dofile` would resolve against the working directory. The modernized one draws positively
with colour and alpha instead of knocking holes with `CAIRO_OPERATOR_CLEAR`, and anchors the gauge
block in **ring** units rather than gauge units so it clears the clock and date — which makes the
fit an implicit equation, solved by the fixed-point iteration in `growth_for` (it converges because
`GAUGE_TOP` is much smaller than `INNER_RADIUS`).

The modernized variant draws its filesystem and GPU readouts with one `draw_dial` helper — ring,
cairo-drawn icon, readout — so a dial is defined by a fill fraction, a colour and an icon rather than
by what it measures. Icons (`drive_icon`, `home_icon`, `gpu_icon`) are stroked line art; note they
build paths under a scaled CTM but **stroke after restoring it**, since stroking while scaled would
distort the line width.

### Conky_Orrery/lua_orrery.lua renders 3D in software

Conky exposes no 3D and none is used. Points are turned by a row-major 3x3 matrix held as nine
numbers, divided through by depth (`FOCAL / (FOCAL + z)`) for perspective, and painted back to
front. Four things about it are load-bearing:

- **Everything is depth-sorted together, text included.** The clock is submitted at depth 0, the
  plane through the centre of the scene, so the near half of the cage draws over the numerals and
  the far half behind them. That is the whole visual point of the variant; moving the clock out of
  the sorted list and drawing it last would throw it away.
- **The primitive tables are pooled and never freed.** At 20fps and ~1000 primitives a frame,
  allocating fresh tables would be tens of thousands of allocations a second and the GC pauses
  show as stutter. `table.sort` has no range form and truncating the list would discard the pool,
  so the unused tail is parked at `-math.huge`, which sorts past the near end of a descending
  sort, and the draw loop stops at the live count. The same reasoning applies to the per-body
  orbital matrices and the cage's projected vertices, which are built once rather than per frame.
- **Depth fade is per object, not per scene.** `fade_within(vz, reference)` takes the half-depth
  of the thing being drawn. Fading the 96-unit cage against the 272-unit scene leaves it spanning
  only the middle of the ramp, so its back comes out nearly as bright as its front and it reads as
  a solid ball instead of a cage.
- **Sampling is throttled to 1Hz and eased.** `${cpu}`, `${memperc}` and `${fs_free_perc}` are
  read once a second, not once a frame, and every frame eases towards the last reading with a
  framerate-independent exponential (`1 - exp(-dt/tau)`). Reading them per frame would parse
  twenty `${...}` a second for numbers that do not change that fast.

Animation time comes from `/proc/uptime`, which is the only sub-second wall clock available:
`os.time()` has one-second resolution and `os.clock()` measures CPU time consumed, so it crawls
while the widget sits idle. Wall time is uptime plus an offset derived from `os.time()`; since
that is truncated to the second the offset starts out up to a second wrong and is re-derived
whenever it drifts past 1.25s, which also covers suspend/resume. `dt` is clamped to 0.25s so a
frame that arrives late does not teleport anything that integrates over it.

`update_interval` in `start_conky_orrery` is what actually sets the frame rate; `target_fps` in
`lua_orrery.lua` only supplies the fallback frame duration and the warm-up threshold. Change both
together. The whole frame is redrawn every tick, so the frame rate is also the cost: 20fps with 24
bodies and the hoop labels measures ~8% of one core.

Two radii are in tension and were tuned against renders, not by eye. `R_CAGE` has to be larger
than half the clock's width or the cage sits entirely behind the text and the crossing effect
disappears; the rest of the radial budget (`R_YEAR` down to `ORBIT_INNER`) is sized outward from
it. `widget_size` is the outermost hoop, but readout values are written outside it, so the fit
test measures `CONTENT_DIAMETER`, not `BASE_DIAMETER` — sizing against the latter lets the corner
text run out of a small window.

Three of the four hoops carry the date as labels (months, day numbers, weekdays) and the middle
holds only the clock, so the labels are the readout and two rules keep them legible. Where a hoop
turns edge-on its divisions crowd into a knot, so a label fades by how much room its neighbour
leaves it; and no label may be drawn within `CLOCK_KEEPOUT` of the middle, or a steeply tilted
hoop writes across the clock. The live label is exempt from both — it is *pushed* out of the
keep-out along its own direction from the centre rather than faded — because fading it would mean
that every time a hoop came edge-on, the one thing worth reading off it disappeared. Label
tangents come from the neighbours either side rather than a second projected sample: every anchor
is projected already, so it is free and steadier over two divisions than over a short chord.

Label sizes change every frame with perspective, so `cairo_text_extents` is called once per string
at `REFERENCE_SIZE` and the result scaled (`extent_for`). Measuring per label per frame is around
a thousand calls a second for a set of strings that never changes. Hinting makes the scaling very
slightly non-linear, which is a fraction of a pixel on centred text.

**Today's date is the only thing on a hoop drawn in the accent colour**, and the reading is the
label itself, lit from behind with `put_glow`. Two other ways of marking it were tried and both
read as bugs to the user. A glowing bead riding the hoop is indistinguishable from an orbiting
body a few pixels away — in this widget a round glowing dot is a CPU core and nothing else.
Brighter ticks bounding the live division read as two stray dashes floating near the text, because
the ticks are on the hoop while the label is written outside it and half a division round; a
longer tick marking the hoop's zero went the same way. So every tick is identical and every other
label is the same dim grey. Only the seconds hoop, which carries no labels and is the one thing
that moves between frames, keeps a travelling head. Do not add a second lit thing to a labelled
hoop.

Labels sit at division *centres* (`(i - 0.5) * TAU / count`) and the live mark snaps to the centre
of the division the value falls in, rather than tracking the value continuously. A label names the
sector after its tick, so a continuous mark sits almost on FRI by Thursday evening and almost on
OCT by late September — correct to the hour and wrong to the eye. The continuous value is still
what is passed in; only its presentation is quantised.

Readout colour encodes kind, not position: accent for live load (CPU, MEM), second for disk use
(ROOT, HOME), the heat ramp for degrees (GPU). The readouts are spaced evenly from the top from a
list built at draw time, so dropping the GPU takes the set from five to four and the layout
follows — do not reintroduce fixed corner angles. Beads use five stacked discs for a halo rather than a cairo gradient, because at up to forty
beads a frame the pattern allocation is not free; the nucleus is the one thing large enough for
those steps to show as rings, so it alone uses `put_glow` and a real radial gradient.

### The orrery's colour editor

`Conky_Orrery/orrery_colors.py` is a tkinter editor for the four `HTML_*` colours, the four
`opacity_*` values and `depth_fade`, with a live preview. Standard library only — tkinter ships
with Python and Tk 8.6 reads PNG unaided, so there is nothing to install and no Pillow dependency
even though Pillow happens to be present on this machine.

**Almost nothing in it is a ttk widget.** Buttons, sliders, the segmented control, the swatch and
preset chips and the colour picker are all drawn on `tk.Canvas`, because the stock ttk themes
cannot be pushed far from their Motif ancestry — `clam`'s scale still draws a hatched grip — and
the editor sits beside a preview whose whole job is to be looked at. `rounded()` draws a rounded
rectangle as a `create_polygon` with `smooth=True`, which at these radii is indistinguishable from
real arcs. Each control redraws itself wholesale on hover, press and value change; at these sizes
that is far below a frame's worth of work.

The colour picker is a saturation/value square over a hue strip. The square is a `PhotoImage`
filled a pixel at a time from `colorsys`, which sounds ruinous and is not: 186×186 costs about
20ms, and it is only rebuilt when the hue moves — dragging within the square just moves the ring.
`set_hex` deliberately leaves the hue strip alone when the incoming colour has no saturation,
because a grey has no meaningful hue and snapping the strip to red every time someone picks white
is worse than remembering where they left it.

The preview is not a reimplementation. Each redraw writes the candidate settings into a throwaway
copy of `lua_orrery.lua` and renders *that* through `orrery_preview.lua`, so what is on screen is
byte-for-byte the file Apply is about to write. Keep it that way; a separate drawing path in
Python would drift from the Lua within a release.

Three details are load-bearing:

- **Unchanged settings are left completely alone**, down to the case of their hex digits.
  `apply_settings` compares before substituting and returns the original match when the value has
  not moved, so pressing Apply with nothing changed produces a byte-identical file. Numbers are
  written `%.2f`, which is how the file is already written and the same width for every value in
  range, so the comment after each one keeps its column.
- **Renders run on a worker thread** and results come back through a `queue.Queue` drained by an
  `after()` poll, because Tk is not thread-safe. Each render carries a sequence number and
  overtaken results are dropped, so a fast drag cannot paint a stale frame last.
- **The PNG filename alternates** between two names. Tk keeps the image mapped while it is
  displayed, and overwriting the file currently on screen can be read half-written.

`orrery_pids()` finds conky processes by reading `/proc/*/cmdline` rather than shelling out to
`pgrep -f`, for the same reason the note above gives: a pattern wide enough to match the conky
process also matches whatever was asked to look for it.

### Conky-Calendar-Extra scales itself

Nothing in `draw_function` is a fixed pixel count any more: constants are written against a 450px
design size (`BASE_DIAMETER`) and multiplied at draw time. Two scales exist and they are **not
interchangeable**:

- `scale` sizes the rings and everything positioned off them (clock, disk gauges, ring labels).
- `gauge_scale` sizes the temperature gauge block.

They are separate because the gauge block must fit inside the innermost ring, and scaling both by
one factor changes both sides of that comparison equally — the block would never come to fit however
large the widget grew. Instead the gauges hold the user's size and `ring_growth` expands the rings
around them.

`grid_for(count)` picks the wrap that minimises the distance from the ring centre to the block's
furthest corner, which is what decides `ring_growth`. It returns a single row up to 6 gauges (so the
original layout is bit-identical for the default), and 16×2 for 32.

Conky sizes its window larger than `minimum_width`/`minimum_height` (a 1000 request came back as
1149), so scale off `conky_window.width`/`height` at draw time, never off the configured minimum.
The window is centred with `alignment = 'middle_middle'`; `top_middle` pins it to the top of the
screen, which on a 1440px-tall display left the widget ~146px above centre.

Dot radii are the caller's decision and every gauge dot uses one radius from `gauge_dot_radius`,
sized for the widest label in the set. Sizing each disc to its own label makes a row visibly uneven.
The gauge box is derived, not hardcoded: `GAUGE_HEIGHT` and `GAUGE_PITCH_Y` fall out of
`GAUGE_COLUMN_HEIGHT` + `GAUGE_DOT_GAP` + `DOT_RADIUS_MAX`, and `gauge_dot_radius` is capped at
`DOT_RADIUS_MAX` so a dot can never exceed the height `grid_for` measured the block against.

When the window cannot hold the result everything is scaled down by one shared factor rather than
clipped, and `warn_once` logs the window size to set. Verify layout changes by rendering rather than
by eye: `cairo_image_surface_create` + `cairo_surface_write_to_png` from `/usr/lib64/conky/libcairo.so`
draws the widget headlessly, with a stub `conky_surface()` returning the image surface.

### Cairo operators carry meaning

Windows are ARGB/transparent, so `CAIRO_OPERATOR_CLEAR` is used deliberately to knock holes through
already-drawn shapes (this is how labels and battery blocks appear as cut-outs), and
`CAIRO_OPERATOR_SOURCE` to paint. In Conky-Revisited-2 this is parameterised: `operator` and
`operator_transpose` are two-element tables indexed by the user's `mode` setting, which is what makes
`mode` invert the entire look. PNG compositing needs `CAIRO_OPERATOR_OVER` instead — the recent
"Force over operator" / "Fix layer" commits fixed exactly this in the weather icon path.

### `USER CONFIGURATION` convention

Each Lua script opens with a user-editable block (colors as `#RRGGBB` strings converted by a local
`hex2rgb`, transparencies, font sizes, layout toggles), followed by a
`DON'T EDIT BELOW IF YOU DO NOT KNOW WHAT YOU ARE DOING` marker. Keep new tunables above that line
and document them in the theme's own README.

## Per-theme notes

**Conky-Revisited-2** ships four full copies of `settings.lua` (Square/Circle × Vertical/Horizontal),
each ~400 lines with its own `draw_square`/`draw_circle` and diverging helpers. There is no shared
module — a fix to `hex2rgb`, `draw_battery`, `draw_folder`, `draw_cpu`, or `draw_ram` must be applied
to all four, and each variant's conky file has its own hard-coded `minimum_width`/`minimum_height`.
`drives`, `number_of_cpus`, and `battery` change the widget's total height, so the conky file's
`minimum_height` must be adjusted by hand to match.

**Conky-Weather** requires `pip3 install pyowm` and an OpenWeatherMap API key. `settings.lua` holds
`api_key`/`city`/`country_code` and is committed with `YOUR_API_KEY` placeholders — keep them
placeholders. `openweather.py` is a one-shot CLI (`--get_temp_c`, `--get_temp_f`,
`--get_weather_icon`) invoked separately for each value, so each redraw costs two OWM API calls;
`update_interval = 15` is low relative to free-tier limits. Icon names returned by the API map
directly to filenames in `PNG/` (`01d`, `10n`, …), and `draw_weather_icon` resolves `$HOME` by
shelling out rather than using the conky-relative path.

**Conky-Calendar-Extra** spawns no subprocess on its normal draw path: clock and calendar values
come from `os.date`, and temperatures are read directly from `/sys/class/hwmon/hwmon*/temp*_input`
(millidegrees C), the same data `lm-sensors` formats. `sensors-detect` still matters for loading the
kernel modules, but the `sensors` binary is never invoked. `scan_hwmon` discovers inputs by label and
**must not assume contiguous core numbers** — a hybrid Intel part labels cores 0, 4, 8 … 28 then
32–47, and AMD uses `Tccd1`/`Tccd2`, so the old `grep 'Core N:'` for N in 0..3 silently returned
nothing for 3 of 4 bars. Cores are sorted by reported number and addressed positionally. The GPU
input comes from an `amdgpu`/`nvidia`/`radeon` hwmon entry, with an `nvidia-smi` `${exec}` as the
only fallback. Readings are funnelled through
`number_or(value, default)`: Conky returns an empty string for an absent sensor or mount point, and
without the fallback one `nil` would abort the draw hook and blank the entire widget rather than a
single bar. `number_of_physical_CPU_cores` must be set by the user; `temperature_gauges` derives the
real gauge count from it (plus one when the GPU is enabled) and drives the horizontal spacing.
Everything except the USER CONFIGURATION knobs and `conky_start_widgets` is file-local, because
Conky shares one Lua state across every script it loads.

## Locale

`Conky-Weather/settings.lua` asserts `os.setlocale("en_US.utf8", "numeric")` so decimal temperatures
parse with `.` rather than `,`. Removing it breaks number parsing under comma-decimal locales.
