# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A collection of three independent Conky desktop-widget themes. There is no build system, package
manager, test suite, or linter — each theme is a Conky config plus a Lua script that draws with
cairo, installed by copying files into `~/.conky/`.

- `Conky-Weather/` — OpenWeatherMap temperature + icon (Lua + a Python helper)
- `Conky-Revisited-2/` — battery/disk/CPU/RAM panel, in four layout variants
- `Conky-Calendar-Extra/` — circular calendar/clock + per-core CPU temperatures

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

`Conky-Calendar-Extra` has no installer. Its `lua_load` is relative, but conky resolves that
against the **config file's own directory**, not the working directory — so `conky -c
/abs/path/start_conky` works from anywhere, while copying the config away from its `.lua` breaks it.
`start_conky_modernized` is the restyled variant.

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
seconds before concluding a change did nothing.

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

### Conky-Calendar-Extra ships two looks

`lua_widgets.lua` + `start_conky` is the original dial; `lua_widgets_modernized.lua` +
`start_conky_modernized` is a restyled copy driven from the same sensor and scaling code (the
`require`/`conky_window_surface`, hwmon and `days_in_current_month` blocks were sliced out of the
original verbatim, so fixes to those must be applied to both). The modernized one draws positively
with colour and alpha instead of knocking holes with `CAIRO_OPERATOR_CLEAR`, and anchors the gauge
block in **ring** units rather than gauge units so it clears the clock and date — which makes the
fit an implicit equation, solved by the fixed-point iteration in `growth_for` (it converges because
`GAUGE_TOP` is much smaller than `INNER_RADIUS`).

The modernized variant draws its filesystem and GPU readouts with one `draw_dial` helper — ring,
cairo-drawn icon, readout — so a dial is defined by a fill fraction, a colour and an icon rather than
by what it measures. Icons (`drive_icon`, `home_icon`, `gpu_icon`) are stroked line art; note they
build paths under a scaled CTM but **stroke after restoring it**, since stroking while scaled would
distort the line width.

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
