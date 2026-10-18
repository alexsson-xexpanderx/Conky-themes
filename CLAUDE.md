# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A collection of five independent Conky desktop-widget themes. There is no build system, package
manager, test suite, or linter — each theme is a Conky config plus a Lua script that draws with
cairo, installed by copying files into `~/.conky/`.

- `Conky-Weather/` — OpenWeatherMap temperature + icon (Lua + a Python helper)
- `Conky-Revisited-2/` — battery/disk/CPU/RAM panel, in four layout variants
- `Conky-Calendar-Extra/` — circular calendar/clock + per-core CPU temperatures, in two looks
- `Conky_Orrery/` — animated software-3D armillary clock, plus a tkinter colour editor with a
  live preview. Grew out of Conky-Calendar-Extra and still shares its sensor code. It runs in
  place rather than being installed into `~/.conky/`.
- `Conky-Mirage/` — a bento of frosted-glass tiles (clock, weather now / 24h / 7 days, wind,
  humidity, UV, air quality, sun and moon, system) whose glass and palette are rendered from the
  wallpaper actually on screen. Lua paints; a Python helper (`mirage.py`, Pillow) finds the
  wallpaper, renders the glass and fetches Open-Meteo weather (no API key). Runs in place, like
  the orrery, and like it has a tkinter editor with a live preview (`mirage_settings.py`, for
  sizes and placement). `wallpapers/` holds twelve wallpapers drawn for its four looks.

The top-level `README.md` also advertises two themes that live in *other* repositories
(`conky-drawer-interactive`, `conky-pywal`) — they are not in this tree.

## Writing for users

Anything a user reads -- READMEs, GUI labels, tooltips, status messages, the comments in a
USER CONFIGURATION block, errors a widget prints -- is short, plain and sounds like a person
wrote it. No jargon a user would have to translate ("scale 1", "base size", "flex", "render",
"helper"), no em-dash asides, no bold-led bullet lists of features. Say what a control does
in the words someone would use to ask about it: "Space between the boxes", not "tile gap".
The user asked for this explicitly after the first versions of Mirage's README and settings
window read as machine-written. Technical detail belongs here, in CLAUDE.md, not there.

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

```bash
conky -c /abs/path/Conky-Mirage/start_conky_mirage
```

`Conky-Calendar-Extra` has no installer. Its `lua_load` is relative, but conky resolves that
against the **config file's own directory**, not the working directory — so `conky -c
/abs/path/start_conky` works from anywhere, while copying the config away from its `.lua` breaks it.
`start_conky_modernized` is the restyled variant. The same applies to `Conky_Orrery`, whose
`start_conky_orrery` and `lua_orrery.lua` must stay in the same directory, and to `Conky-Mirage`,
whose `start_conky_mirage`, `mirage.lua` and `mirage.py` must.

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

Conky-Mirage has its own harness, `Conky-Mirage/mirage_preview.lua`, which works differently
(overrides instead of a script copy, a synchronous helper, a still mode); see its section below.

A GUI cannot be grabbed off the whole screen either, but it can be pointed at a virtual X server
that *can* be captured — `Xvfb :99 -screen 0 1400x1000x24`, run the program with `DISPLAY=:99`,
then `ffmpeg -f x11grab -draw_mouse 0`. Without `-draw_mouse 0` the pointer is baked into the
middle of the grab. One X11 window on the real display *can* be captured, though: `import -window
<id>` with the window's frame id (`app.wm_frame()` from Tk) reads that window alone. Use it to see
what the user sees: Xvfb is not the same, and on the real display Mirage's settings window opens
26px taller (text draws larger there) and showed glitches Xvfb did not make obvious.

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

All seven configs use the 1.10+ Lua-table syntax (`conky.config = { ... }; conky.text = [[ ]]`).
Conky-Calendar-Extra was converted from the pre-1.10 `key value` / `TEXT` format; older copies of
that file found elsewhere will still be in the old format, and while conky can auto-convert them at
startup, that rescue path depends on the optional `old configuration syntax` build feature and emits
a syntax-error warning every launch. Convert instead, with `convert.lua` from conky's doc directory.

Settings get retired regularly, and transparency is where it bites: `own_window_transparent` is
deprecated and `own_window_argb_visual` has been removed outright ("ARGB is now always enabled when
available"). Both are gone from this repo — transparency is now `own_window_colour = '#00000000'`
alone. Conky 1.25 then renamed that to `own_window_color` and warns about the old spelling on every
launch; `start_conky_mirage` asks `conky --version` at load time (`conky_version` is *not* defined
while a config is evaluated) and sets whichever the binary wants, including the pre-1.24 pair.

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
  a solid ball instead of a cage. **Text is never depth-faded**, neither the hoop labels nor the
  clock: dimming the words cost more reading than it bought depth. Today's labels spent a third of
  a camera turn below half strength, and today's month at the back of its hoop came out at about a
  quarter, fainter than the cores in front of it. The hoop lines and ticks still recede and the
  type still shrinks with perspective, which is depth enough.
- **Sampling is throttled to 1Hz and eased.** `${cpu}`, `${memperc}` and `${fs_free_perc}` are
  read once a second, not once a frame, and every frame eases towards the last reading with a
  framerate-independent exponential (`1 - exp(-dt/tau)`). Reading them per frame would parse
  twenty `${...}` a second for numbers that do not change that fast. With `show_monitoring = "No"`
  nothing is sampled at all.

Animation time comes from `/proc/uptime`, which is the only sub-second wall clock available:
`os.time()` has one-second resolution and `os.clock()` measures CPU time consumed, so it crawls
while the widget sits idle. Wall time is uptime plus an offset derived from `os.time()`; since
that is truncated to the second the offset starts out up to a second wrong and is re-derived
whenever it drifts past 1.25s, which also covers suspend/resume. `dt` is clamped to 0.25s so a
frame that arrives late does not teleport anything that integrates over it.

`update_interval` in `start_conky_orrery` is what actually sets the frame rate; `target_fps` in
`lua_orrery.lua` only supplies the fallback frame duration and the warm-up threshold. Change both
together. The whole frame is redrawn every tick, so the frame rate is also the cost: 20fps with 24
bodies and the hoop labels measures ~8% of one core. The clock-and-calendar mode draws a frame in
about half the time (3.4ms against 6.5ms through the preview harness).

Two radii are in tension and were tuned against renders, not by eye. `R_CAGE` has to be larger
than half the clock's width or the cage sits entirely behind the text and the crossing effect
disappears; the rest of the radial budget (`R_YEAR` down to `ORBIT_INNER`) is sized outward from
it. `widget_size` is the outermost hoop, but readout values are written outside it, so the fit
test measures `CONTENT_DIAMETER`, not `BASE_DIAMETER` — sizing against the latter lets the corner
text run out of a small window. Without the readouts it is 680, not 640: the glow behind this
month's label reaches ~335 units at the widest point of its hoop once perspective is applied. That
figure was measured from renders over a full camera turn, not derived; derive it and you get 320.

Three of the four hoops carry the date as labels (months, day numbers, weekdays) and the middle
holds only the clock, so the labels are the readout and two rules keep them legible. Where a hoop
turns edge-on its divisions crowd into a knot, so a label fades by how much room its neighbour
leaves it; and no label may run into the clock, or a steeply tilted hoop writes across it. The live
label is exempt from both — it is *pushed* out along its own direction from the centre rather than
faded — because fading it would mean that every time a hoop came edge-on, the one thing worth
reading off it disappeared. Label tangents come from the neighbours either side rather than a
second projected sample: every anchor is projected already, so it is free and steadier over two
divisions than over a short chord.

The clock's keep-out is **its own ink box**, not a radius. `CLOCK_SAMPLE` is measured at
`CLOCK_SIZE` and each label's box, turned to its lean, is tested against it; a label fades over
`KEEPOUT_RAMP` as it closes in. It used to be a circle of radius 104 (+44 ramp) round the middle,
which is the wrong shape for a clock that is wide and short: a label just below the numerals,
nowhere near touching them, was inside it and vanished. A viewer of the demo video reported exactly
that, as APR disappearing "when it gets too close to the line it's following" — it was the circle,
not the line. Over a camera turn the circle hid 14% of otherwise-readable labels; the box hides 3%.
The live label is pushed to `LIVE_CLEARANCE` (32) from the box rather than to its edge, because a
day number pushed only to the edge read as part of the time.

Today's three labels are **not** kept off each other. Over a camera turn two of them overlap (by
bounding box) in about 23% of frames, before and after the box keep-out alike. Pushing a crowded one
on outward until clear was tried and taken out: whenever the first clear spot switched from one side
of another label to the far side, the label jumped up to 75 units between frames, which reads worse
than the overlap. A fix has to move them continuously — ease the offset, not the position.

Label sizes change every frame with perspective, so `cairo_text_extents` is called once per string
at `REFERENCE_SIZE` and the result scaled (`extent_for`). Measuring per label per frame is around
a thousand calls a second for a set of strings that never changes. Hinting makes the scaling very
slightly non-linear, which is a fraction of a pixel on centred text.

**Today's date is the only thing on a hoop drawn in today's colour** (`HTML_today`), and the reading is the
label itself, lit from behind with `put_glow`. Two other ways of marking it were tried and both
read as bugs to the user. A glowing bead riding the hoop is indistinguishable from an orbiting
body a few pixels away — in this widget a round glowing dot is a CPU core and nothing else.
Brighter ticks bounding the live division read as two stray dashes floating near the text, because
the ticks are on the hoop while the label is written outside it and half a division round; a
longer tick marking the hoop's zero went the same way. So every tick is identical and every other
label is the same dim grey. Only the seconds hoop, which carries no labels and is the one thing
that moves between frames, keeps a travelling head. Do not add a second lit thing to a labelled
hoop. The seconds hoop has no labels, so at the user's request it shows progress instead: a bar
across it where the minute starts and ends, and the minute so far lit from that bar to the head,
brightening into the old streak over the last `tail_degrees`. Before that it had neither, and a user
took it for an unexplained ring round the cage, which is also why it has `show_seconds`. The bar
is not the "longer tick" rejected above: that one marked a labelled hoop's zero for no reason a
reader could see, while this one is where the lit arc visibly starts.

Labels sit at division *centres* (`(i - 0.5) * TAU / count`) and the live mark snaps to the centre
of the division the value falls in, rather than tracking the value continuously. A label names the
sector after its tick, so a continuous mark sits almost on FRI by Thursday evening and almost on
OCT by late September — correct to the hour and wrong to the eye. The continuous value is still
what is passed in; only its presentation is quantised.

**Every element has its own colour setting** — seventeen `HTML_*` keys, from `HTML_clock` to
`HTML_home`, read into the `INK` table — because the user wanted to try colours element by element
in the editor. **And its own opacity** — `opacity_<element>`, read into `OPACITY`, for all but Heat,
which is a colour other elements turn rather than something drawn. They replaced four shared
opacity tiers. The setting is the element's main part; any other part (a readout's value, the
cage's vertices, the seconds hoop's unlit ring) is written as its designed alpha times
`scale_of(element)`, which is `OPACITY / DESIGNED` and so exactly 1 at the designed value — at
those values they rendered pixel-identically to the tiers they replaced, checked with fixed
sensors. As shipped the colours follow Catppuccin Mocha, chosen when the user asked for the widget
to be made beautiful: every colour at a similar soft brightness so nothing vibrates on a dark
desktop, cool colours for the time (clock, today, seconds, rings) and warm for the machine (mauve
cores, cream readouts, rose heat). The readouts are one colour nothing else uses because the user
wanted them told apart from the cores. The clock has its own face, `font_clock` (Noto Sans Light);
`extent_for` keys its cache on the family too, and the clock's keep-out box is measured in the
clock's face, since a narrow light clock kept clear by the box of a wide regular one would push
labels away for nothing. The editor's presets are still five-colour palettes by kind, giving each
element the palette colour its role names.
`heat_color(temperature, cool)` blends an element's own cool colour into `HTML_heat`; the cage and
the glow blend towards the same heat by the hottest core. The cores were given a colour of their
own because at idle two dozen dots in today's cyan made the date the hard thing to find, so a
preset's cool colour has to stay clear of its base and accent (the clock, the date) and of its
second (the cage the cores orbit through). The readouts are spaced evenly from the top from a
list built at draw time, so dropping the GPU takes the set from five to four and the layout
follows — do not reintroduce fixed corner angles. Beads use five stacked discs for a halo rather than a cairo gradient, because at up to forty
beads a frame the pattern allocation is not free; the nucleus is the one thing large enough for
those steps to show as rings, so it alone uses `put_glow` and a real radial gradient.

`show_monitoring = "No"` is the clock and the calendar alone: no cores, no cage, no readouts, no
sampling. It exists because the combined widget was reported as doing two jobs in one picture —
reading the time you do not want the machine, and the other way round. It also takes
`show_readouts` with it. The hoops still cross in front of and behind the numerals, so the
depth-sorting point above holds without the cage. `show_cage` and `show_glow` turn the cage and
the glow behind the clock off separately; `draw_cage` puts the glow down before it looks at
`show_cage`, because with the cage off the glow is the one thing left showing how hot the hottest
core is.

### The orrery's colour editor

`Conky_Orrery/orrery_colors.py` is a tkinter editor for every element's `HTML_*` colour and
`opacity_*`, the Yes/No switches for the parts that can be turned off, and `depth_fade`, with a
live preview. Standard library only — tkinter ships with Python and Tk 8.6 reads PNG unaided, so
there is nothing to install and no Pillow dependency even though Pillow happens to be present on
this machine.

`ELEMENTS` lists the colours in the groups the config block uses, each with a `role` that presets
go through (`ROLES`). `SWITCHES` names each switch's parent and `SHOWN_BY` the switch an element
hangs off; `switched_on` walks that chain, so a dependent switch and every element it hides are
drawn dimmed while a parent is off, and the description under the element's name says the
preview leaves it out.

**Choosing an element picks it out in the preview for a moment**, so it is plain which part of the
widget a colour belongs to. `spotlight_text` puts `orrery_spotlight = "<element>"` ahead of the
script, and the widget itself then draws every other element as a ghost — `emphasis()` gives each
primitive a factor through the `drawing` variable, and `share()` turns it into alpha, capped at full
first. It is done by alpha in the widget, not by colour in the editor, for a reason that was found
the hard way: fading the other colours into the backdrop made them dark, and anything passing in
front of the picked element at full strength cut a dark stroke through it — through the clock's
numerals, for one. With `orrery_spotlight` unset every factor is 1 and rendering is pixel-identical
to before; that was checked with fixed sensors at three poses. The editor also puts every opacity
at full so a faint element still shows, and its switches on so a hidden one shows anyway; Dust is
drawn larger (`dust_size`), since a speck is under a pixel in a 600px preview, and Heat gets a
machine running hot (`warm_above` 0, `max_temperature` 1), because at idle nothing is drawn in it.

The spotlight ends by itself `SPOTLIGHT_MS` after the picked-out frame appears — the user could not
find how to get the whole widget back — and at once on any edit or a click on the preview, so a
colour is judged among the others. `spotlight_serial` lets a timer that has been overtaken by a
newer selection tell that it has. The text only ever goes to the renderer, through
`preview_text()`; Apply writes `candidate_text()`, which never sees it. The editor only offers settings the file actually has, since anything else
has no line to be written back to; a `lua_orrery.lua` from before per-element colours has none of
them, and gets an error rather than an empty editor.

**Almost nothing in it is a ttk widget.** Buttons, sliders, switches, the segmented control, the
element list, the preset chips and the colour picker are all drawn on `tk.Canvas`, because the stock ttk themes
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

### Conky-Mirage: Lua paints, Python does everything slow

`mirage.lua` is the draw hook and never blocks. `mirage.py` is a one-shot the Lua starts detached
(`os.execute(cmd .. " &")`) every `HELPER_EVERY` (10s); it takes an `flock` on the cache's `lock`
so a slow network cannot let runs pile up. It writes into `~/.cache/conky-mirage/`: `theme.lua`
and `weather.lua` (Lua table literals, loaded with `loadfile(path, "t", {})` so a bad file can only
fail to load), per-tile `glass_N.png` and `shadow.png`, `under.png` (the raw wallpaper behind the
window, used only by previews), and `serial`, which it bumps when anything changed. The Lua reads
`serial` once a second and reloads only when it moved. A run with nothing to do costs ~20ms of
CPU; `urllib`/`ssl` are imported lazily because importing them cost more than the rest of the run.

The `require`/`conky_window_surface` block and `wall_clock` in `mirage.lua` are copied verbatim
from `lua_orrery.lua`; a fix to either belongs in both. Its sensor code is its own (one CPU
package temperature, not per-core), so the hwmon note above does not extend to it.

**Every setting lives in mirage.lua's USER CONFIGURATION** and reaches the helper as command-line
arguments (`helper_command`). Do not give `mirage.py` a config of its own. The tile rectangles are
passed too, so Python renders glass for exactly the layout Lua computed.

- **The helper finds the window by title** (`xwininfo -name conky-mirage`): `own_window_title` in
  `start_conky_mirage` and `TITLE` in `mirage.lua` must match. It asks X rather than computing
  from `alignment`/`gap_x` because the real window lands a pixel or two off what was requested
  (measured: 403×302 at 122,76 for a 400×300 request at 123,77), and glass cut from the wrong
  pixels shows. `output_backend = 'x11'` is required for the same reason.
- **A wallpaper change is caught by a probe, not the 10s run.** `watch_wallpaper` asks the
  desktop for its wallpaper every `PROBE_EVERY` (1.5s) -- `gdbus` on Plasma, `gsettings` on the
  GNOME family, `xfconf-query` on Xfce -- detached, writing to `wallpaper.probe` via a rename,
  read back on the next round. When the answer changes it calls `run_helper(now, true)`.
  Measured live on Plasma: re-rendered glass 2.2-2.5s after `setWallpaper`. Setters with
  nothing to ask (feh, swww, ...) and an explicit `wallpaper` path rely on the 10s run, whose
  signature includes the image's mtime.
- **Which wallpaper.** KDE first asks Plasma over D-Bus (`org.kde.PlasmaShell.wallpaper <screen>`,
  via `gdbus`): Plasma writes `plasma-org.kde.plasma.desktop-appletsrc` some time after a change,
  the D-Bus answer is live. The appletsrc parse is the fallback. A wallpaper package directory is
  resolved the way plasma-workspace's `findPreferredImageInPackage` does, `images_dark` when the
  colour scheme is dark. GNOME/Cinnamon/MATE (gsettings), Xfce (xfconf), feh, nitrogen, swaybg,
  swww, hyprpaper and pywal follow. A slideshow records no current image, so it falls back to
  tinted panes rather than show the wrong picture.
- **The theme signature includes `mirage.py`'s own mtime**, so editing the helper re-renders the
  glass on the next run; `VERSION` only needs bumping for changes that do not touch the file.
- **Glass is darkened by multiplying, not by mixing towards a tint.** Mixing towards grey flattened
  Saturn's golds and pinks to mud; multiplying keeps hue and saturation. Each tile is solved for
  7:1 (6:1 on light glass) against its 94th-percentile pixel, not the mean, then pulled 35% towards
  the darkest tile so neighbouring panes read as one material.
- **The palette is in OKLab.** Accents are scored on chroma over area. The temperature ramp runs
  from the wallpaper's coolest hue to its warmest *by way of the accent*, at least 170° wide (an
  80° ramp made 8° and 18° look the same), with stops spread so 5–25°C covers most of it. Stops
  are in Celsius; the Lua converts imperial readings before looking them up.

Drawing:

- **Tiles have a `static` part and a `live` part.** Static is drawn into a per-tile surface keyed
  by its `key()`; shadow, glass and every static layer are then flattened into one `base` surface,
  rebuilt only when some key changes (every frame during the intro and a palette crossfade, else
  about once a minute). Compositing ~20 window-sized layers per frame was most of the X server's
  cost. Measured at 10fps: conky ~0.9% of a core, Xwayland ~2.2% above its baseline.
- **Live drawing is clipped to its tile before any `cairo_push_group`.** A group takes the size of
  the current clip; unclipped, the icon's cloud cut-out allocated a window-sized pixmap per frame.
- **Gauge arcs use a linear gradient, not a mesh/conic one.** XRender has linear gradients; a mesh
  pattern is rasterised by cairo on the client and uploaded every frame for every ring.
- **The toy font API has no glyph fallback.** Noto Sans has no arrows, so ↑/↓ are drawn as
  chevrons and the location pin is a path. Check any new glyph with
  `fc-list ':charset=XXXX' family` first.
- **`string.format("%d", x)` errors in Lua 5.4 when x is a non-integral float**, which an idle
  network rate is: `human_rate` crashed the whole draw hook on a quiet connection until it
  floored. Floor before `%d`.
- **Open-Meteo is fetched with `timeformat=unixtime`**, so `daily[i].t` is the absolute instant of
  local midnight. Seconds since midnight is `now - t`, *without* `tz_offset`; the offset is only
  for formatting, as `os.date("!%H:%M", t + tz_offset)`. Adding it moved the sun three hours ahead.
- Captions are upper-cased by a UTF-8 `upper()` covering Latin-1 and Latin Extended-A, because
  day names follow the locale and `string.upper` mangles "sâmbătă".

- **`fit()` trims whole UTF-8 characters.** It used to drop bytes, which cut the two-byte "°"
  in half; cairo answers invalid UTF-8 by putting the context into an error state that silently
  drops *everything drawn after it* in that tile, so a trimmed "Dew point 6°" took the scale bar
  under it with it. Any new string surgery has to go by `utf8.offset`.
- **`disks`** takes mount points or devices; a device is resolved through `readlink -f` and
  `/proc/mounts` once a minute, because conky's `${fs_*}` only accept a mount point. Each disk
  is a gauge keyed `disk1`, `disk2`… in `target`/`shown`.
- **The 7-day forecast is a chart, not bars**: a column per day, a warm line through the highs
  and a cool line through the lows (`P.sun`, `P.rain`), numbers by their dots, a High/Low key.
  Bars failed twice with the user. The iOS layout -- low and high in columns at the edges, the
  day's bar on the week's scale between them, coloured by temperature -- read as a highlight
  (the warm end's pink) and as wrong (today 8-10° in a week of 8-19° put "now, 10°" near the
  left of the track, so it read "close to 8°": the eye takes a track to span its own row's
  numbers). Moving the numbers to the bar ends fixed the meaning and lost all alignment. Don't
  bring bars back.
- **Conky reloads its config by itself when the file changes, and comes back with a 1x1
  window** (conky 1.25 does not apply `minimum_width/height` again; reproduced by editing
  `start_conky_mirage` under a running copy). Editing that file under a running widget used to
  break it invisibly, and a second copy started next to the broken one then fought it: both
  share the cache and the window title, so each helper rendered glass for its own window and
  the other fell back to tinted panes, flickering every couple of seconds. Two guards:
  `check_owner` makes the newest copy win (each writes its pid to `owner`; an older copy that
  sees a live newer one exits), and `restart_fresh` makes a copy whose window is under 40px
  start itself again with its own `/proc/<pid>/cmdline` and cwd, then exit, at most once in
  30s. The settings window has one Save, which stops the widget *before* writing and starts it
  after, so conky never sees its file change underneath it. When testing, give a test copy its
  own title and `XDG_CACHE_HOME`, or it joins in the fight; and find test copies by
  `/proc/<pid>/cwd`, since a relative `-c` leaves nothing to tell them apart by.
  Editing `mirage.lua` is different and safe: conky re-runs the script in the same window,
  and a running widget picks the change up within a second (seen as the same pid rewriting
  `owner` at the edit's mtime).
- **The network graph is ported from the user's conky-dashboard-pixel**
  (`~/git/kde-plasma-themes/Retrograde/extras/conky-dashboard-pixel/lua/dashboard.lua`:
  `sample_network`, `record_network`, `section_network`), at the user's request because it
  carries their bug fixes; keep the two in step. It takes the busiest non-loopback interface
  rather than the default route (a VPN or no default route left the graph flat), restarts the
  count when the interface changes (no false burst), measures spans with the monotonic
  `/proc/uptime` (wall time steps after a suspend), averages each point over its whole second,
  draws both directions against one linear scale with a 32 KiB/s floor, and writes KiB/MiB to
  three figures. One deliberate difference: Mirage's "peak" label is the busiest second
  actually seen; the dashboard prints the scale's floor, so an idle link reads "peak 32 KiB/s".
- **Hourly marks are placed by measuring their labels**: a three-hour mark is skipped unless
  its temperature, hour and icon clear the previous mark (or "Now"). The old rule skipped marks
  within 40 minutes of now, which stopped being enough once "°C" doubled the labels' width.
- **Temperatures carry their unit** (`show_temperature_unit`). `degrees(v, unit)` takes the
  forecast's unit unless given one; the machine's sensors are always `"C"`, whatever `units` is.

#### Looks

`style` is one of four looks: `glass` (the helper's frosted PNGs), `solid` (flat panes in a
deep or pale tone of the wallpaper), `metro` (square, flat tiles in four bold colours with
everything on them white) and `pixel` (Material You: large corners, tonal containers, round
regular-weight numerals via `DISPLAY_FONT`). The helper's palette carries every look's
colours (`solid`, `metro1`-`4`, `px_*`), all derived from the wallpaper and blended through
a wallpaper change like the rest. `BASE` is the theme palette; `P` is what drawing reads, and
in the looks other than glass `palette_for(tile.name)` swaps it per tile -- a tile's `tint` is
its background, which is also what the cut-out effects (the wind hub, the dots' rings, the
today circle's numeral) are drawn in, so they stay right on any background. `METRO_TILE` and
`PIXEL_TILE` assign the colours by tile name so neighbours differ. Only glass and solid have a
shadow; Metro and Pixel are flat. `transparency` (0-0.9) scales every background's alpha and
the shadow's, never the content's. Corners and spacing stay user settings; the settings
window's look picker sets each look's designed values (`STYLE_FIT`) when chosen. Metro's
colours sit at OKLab L 0.45-0.52 so white text holds about 5.5:1; Pixel's tertiary container
takes the wallpaper's second accent, since a fixed hue rotation landed on olive for a warm one.

**`mirage.lua` is close to Lua's limit of 200 locals in one chunk** ("too many local
variables"). Self-contained sections are wrapped in `do ... end` with only the functions the
rest of the file calls declared outside (`palette_for`, `moon_phase`, `sample_network`,
`check_owner`/`restart_fresh`, `watch_wallpaper`). Add new helpers the same way rather than
as more top-level locals.

Small decisions the user made: the 7-day rain chance is shown for every day (a gap for a dry
day read as missing data; below 20% it is fainter) and named in the key, after High and Low, as
"Rain probability" with the same drop (the drops read as humidity twice; the key shrinks to 9.5
and then says just "Rain" before it gives the rain up where the tile is narrow), the 24-hour chart has no highlighted
"now" dot (the "Now" label is enough) and no night shading (a darker band from sunset to
sunrise, which the user read as rain and disliked), and the wind hub is 19 units across so the
unit clears its edge.

#### Wallpapers

`Conky-Mirage/wallpapers/make_wallpapers.py` (numpy + Pillow) draws twelve wallpapers, three
made for each look, as KDE packages in `wallpapers/<look>/Mirage<Look><Name>/` at 3440x1440
and 3840x2160; `--size` adds another size to each package. `wallpapers/preview.jpg` is all
twelve with the widget on them. They were picked by rendering every wallpaper in every look,
and the look a wallpaper suits follows from how that look takes its colours:

- Glass wants soft light behind the widget for the frosting to show (Afterglow, Silk,
  Orbit). Solid wants calm, flat scenery (Horizon, Dunes, Pines).
- Metro builds its tiles from the two main hues and those turned by 40 degrees, at OKLab L
  about 0.5, where orange and yellow come out olive and brown (Dunes, Canopée). Its three
  are magenta, violet, blue and teal (Shards, Stripes, Mosaic).
- Pixel's containers are dark or pale tones of the same two hues, so it wants gentle
  colours that are not orange (Bloom, light; Pebbles and Arches, dark).

Layout is in units of the screen's height and placed by the width's share where the shape
has to keep its place (the Stripes' angle, the Arches' spacing), or 16:9 versions crowd
into the widget's corner. Shapes are signed distances painted over their own box; colours
mix in OKLab, light adds in linear RGB, and grain plus dither keep gradients from banding.
Two traps: a layer's soft shadow must be masked to the ground already drawn or it smudges
the sky (Dunes), and a glow blurred on an 8-bit copy shows steps (Silk blurs in floating
point).

#### Sizes

Every size is a setting: `text_size`, `clock_size`, `temperature_size`, `caption_size`,
`widget_width`, `tile_gap`, `tile_padding`, `corner_radius`. Three rules make them safe:

- **`text_size` zooms each tile's contents and grows the rows to match**, rather than scaling
  fonts inside a fixed layout. A tile is drawn at `cs = s * text_size` in content units
  `cw × ch` (its pixel size over `cs`), and the row heights in `ROWS` are multiplied by
  `text_size`; widths are not, so a narrower content width is the only way text can run out,
  and that is handled where it can happen (`fit`, `fitted_size`, captions shrinking to `room`).
  Every tile is written against those content units, so no position in a tile needs to know
  the text size.
- **The big numerals hang from a fixed top** (cap height ≈ 0.72 of the size) and push what is
  under them down; row 1 grows by the larger of the two growths (`hero_growth`), and a numeral
  too wide for its tile shrinks rather than overflowing.
- **The window size follows from the sizes**, and conky's window comes from the config, which
  cannot run the layout. `mirage_natural_size()` (a global) gives the size at scale 1;
  `start_conky_mirage` holds it as `base_width, base_height`; `mirage_settings.py` writes it on
  Save; and `check_window` logs once, with the numbers to use, when the window's shape and
  the layout's disagree. The widget still draws in a mismatched window, scaled down to fit.

#### The preview harness and the settings editor

`mirage_preview.lua` loads `mirage.lua` with any `setting=value` overrides substituted into its
source. `mirage_cache_dir`, `mirage_preview_sync` and `mirage_preview_still` must be set
*before* that load, since the script reads them at load time — set after, a preview writes into
the running widget's own cache. It sizes the window from `mirage_natural_size()`, places it as
conky would (alignment and margins inside `_NET_WORKAREA`; middle alignments subtract the gap),
runs the helper synchronously into `MIRAGE_PREVIEW_CACHE` with the position passed as `--at`,
and composites over `under.png`. Its last line, `size WxH at X,Y scale S natural WxH`, is what
the editor parses. `MIRAGE_PREVIEW_STILL=1` skips the intro, the palette fade and gauge easing,
and draws two frames a second apart (CPU needs two samples) — about half a second a render.
`MIRAGE_PREVIEW_SIZE_ONLY=1` prints the natural size and exits, in ~10ms, for Save.
`mirage_preview_refresh` makes the next frame run the helper; the demo video was made by
switching `wallpaper` and setting it.

The demo video shows each look on its three wallpapers. A look is read once at load, so it
is four runs of the script (one per look) on one shared fake timeline, and the whole scene
dissolves over 22 frames where the look changes. For the runs to agree there, every run
starts at frame 0 and renders silently up to its first written frame; CPU, memory, network,
temperatures and GPU are closed-form functions of the fake uptime (the real ones differ run
to run); `os.date` is faked as well as `os.time`, because the static layers' keys ask
`os.date("%H%M")` with no time and so read the real clock (it put 17:42 before 17:41); and
`tile_gap` is 14 in every look so the tiles line up through a dissolve. The runs write the
widget alone and Python composites each frame onto the whole wallpaper: the mp4/webm are the
desktop at 1920 wide (the wallpapers' art is mostly away from the widget), the gif is the
widget's own rectangle, and `conky-mirage-close-up.mp4` is the widget with a 36px margin of
wallpaper at full size (888x996). Stills for posting live in `Conky-Mirage/previews/` (the
looks and all twelve wallpapers on the desktop, in several widths; `close-ups/` crops to the
widget). The user wants such files in the repo, not in ~/Pictures. Each wallpaper is held 105 frames, because the widget takes about
2.6s to finish recolouring after a change (half a second to notice, a poll, the 1.6s fade);
at 88 it never settled. `av1_vaapi` ignores `-qp` (use `-rc_mode CQP -global_quality 90`) and
pads the height to a multiple of 16 with black rows, so the webm is cropped to 1920x800.

`mirage_settings.py` follows the orrery editor's conventions (canvas-drawn controls, candidate
settings never written until Save, unchanged values left byte-identical, `.bak` copies,
`/proc/*/cmdline` to find and restart only Mirage's conky) with two differences. Renders run on
**one** long-lived worker thread that always takes the newest request, not a thread per
render: two renders at once would race for the helper's `flock`, and the loser would draw with
glass rendered for the previous layout. And the candidate settings go to the harness as
overrides, not as a rewritten copy of the script. A rewritten number keeps the comment after it
in its column.

The window is **resizable and maximisable**, never smaller than it opens (`minsize` of its
requested size), and the preview panel grows with it. The worker thread only renders, to a file
per render (`frame-<seq>.png`, older ones deleted); the editor scales "Whole widget" and "On
your screen" to the panel's current size itself, from the newest render and the full-size
desktop the worker made once (`Renderer.screen`), so a resize, debounced 120ms, never
re-renders. "Whole widget" never scales up past 100%.

The **Look** section picks `style` (a string, matched by `STYLE_RE` and written in place
like the numbers) and `transparency`. **Reload** drops the editor's copy of the desktop and its
scaled views and renders again, so a wallpaper changed while the window is open shows up; the
preview's helper re-renders its glass by itself, since its signature includes the wallpaper.

The sliders match the orrery editor's: no mark for the default (a short line at the default
read as a stray line on the track to the user; double-click still resets), and the lit part of
the track is drawn only once it reaches more than 18px from the handle's edge, since a few lit
pixels beside a handle near the start (40 of 720 for "From the top") read as a speck.

What a control does is shown in a **tooltip** (`Tooltip`), never in the status line: the status
line used to wrap onto two lines for a long hint and the whole window jumped. The status label
is a fixed width and never wraps. The margin sliders are named for where the widget is ("From
the right", "Move up" for a middle alignment, where conky's gap moves it up). The window icon
is drawn by `make_icon` with Pillow at start-up and set with `iconphoto`; `xprop`'s default
output shows `_NET_WM_ICON` as empty even when it is set, so check it with
`-f _NET_WM_ICON 32c ' = $0 x $1'`. Under Xvfb the editor sizes "auto" to the virtual screen; drive it with a script
that imports it, points `SCRIPT`/`CONFIG` at copies and stubs `mirage_pids`, as there is no
xdotool here.

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
