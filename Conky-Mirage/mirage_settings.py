#!/usr/bin/env python3
"""Sizes for Conky Mirage, with a live preview.

    ./mirage_settings.py

Text sizes, the tiles and the window -- how big it is and where on the screen
it sits -- on the left; the widget on the right, fitted to the panel, at its
real size, or on your desktop. Nothing is written until Apply.

The preview is the real widget. Every change renders a frame of mirage.lua
through mirage_preview.lua with the candidate settings passed as overrides,
and the glass is cut from your wallpaper at the place the widget will sit, so
a size that is too big, or a corner that puts it over something, shows up
before anything is saved. There is no second implementation here that could
drift away from the first.

Apply writes the sizes into mirage.lua and the window into start_conky_mirage,
including the window size the new sizes need, which is the one thing that is
easy to get wrong by hand. The previous files are kept as .bak.

Standard library and Pillow, which Mirage needs anyway. Like the orrery's
colour editor, the controls are drawn on Tk canvases rather than taken from
ttk, whose themes cannot be pushed far from their Motif ancestry.
"""

import colorsys
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "mirage.lua")
CONFIG = os.path.join(HERE, "start_conky_mirage")
RENDERER = os.path.join(HERE, "mirage_preview.lua")
PREVIEW_CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
                             "conky-mirage-preview")

# The preview's size when the window opens; it grows with the window.
PREVIEW_W, PREVIEW_H = 660, 700
# Long enough that a slider drag does not ask for a render per pixel. A render
# takes about half a second; while one runs, only the newest request waits.
DEBOUNCE_MS = 160

BG = "#0D1117"
SURFACE = "#151B23"
SUNKEN = "#0A0F16"
BORDER = "#232C38"
BORDER_LIT = "#39465A"
TEXT = "#E6EDF3"
MUTED = "#8B97A6"
FAINT = "#5A6675"
ACCENT = "#A9B8FF"
ACCENT_DIM = "#2B3460"
WARN = "#E8B86D"

FONT = "DejaVu Sans"
F_TITLE = (FONT, 10, "bold")
F_BODY = (FONT, 10)
F_SMALL = (FONT, 9)
F_MONO = ("DejaVu Sans Mono", 10)

# (key, label, low, high, step, kind) -- kind says how a value is shown and
# written: "x" a multiplier shown as a percentage, "px" whole pixels.
TEXT_SETTINGS = [
    ("text_size", "All text", 0.80, 1.50, 0.05, "x"),
    ("clock_size", "Clock", 0.60, 1.60, 0.05, "x"),
    ("temperature_size", "Temperature", 0.60, 1.60, 0.05, "x"),
    ("caption_size", "Titles", 0.70, 1.60, 0.05, "x"),
]
TILE_SETTINGS = [
    ("widget_width", "Width", 560, 1100, 10, "px"),
    ("tile_gap", "Space between", 0, 40, 1, "px"),
    ("tile_padding", "Space inside", 12, 36, 1, "px"),
    ("corner_radius", "Rounded corners", 0, 48, 1, "px"),
]

# What each control does, shown when the pointer rests on it. Plain words:
# whoever reads these wants to know what happens if they move it.
RESET = "\nDouble-click the slider to put it back."
NOTES = {
    "text_size": "Makes all the writing bigger or smaller. The boxes grow to fit." + RESET,
    "clock_size": "Size of the big clock." + RESET,
    "temperature_size": "Size of the big temperature." + RESET,
    "caption_size": "Size of the small titles at the top of each box." + RESET,
    "widget_width": "How wide the whole widget is." + RESET,
    "tile_gap": "Space between the boxes." + RESET,
    "tile_padding": "Space between the edge of each box and what is in it." + RESET,
    "corner_radius": "How round the corners of the boxes are." + RESET,
    "transparency": "How much of your wallpaper shows through the boxes." + RESET,
    "scale": "Makes everything bigger or smaller at once." + RESET,
    "auto": "Picks a size that suits your screen.",
    "position": "Click where on the screen the widget should go.",
    "changed": "A dot means you changed this and haven't saved it yet.",
}
VIEW_NOTES = ["The whole widget, shrunk to fit here.",
              "Real size, pixel for pixel. Drag to look around.",
              "How it will look on your screen."]
PLACE_NAMES = {"top_left": "top left", "top_middle": "top middle", "top_right": "top right",
               "middle_left": "middle left", "middle_middle": "middle", "middle_right": "middle right",
               "bottom_left": "bottom left", "bottom_middle": "bottom middle",
               "bottom_right": "bottom right"}
LOOK_SETTINGS = [
    ("transparency", "See-through", 0.0, 0.90, 0.05, "x"),
]
# The looks, and the corners and spacing each one is designed with. Picking a
# look sets those too; they can still be changed afterwards.
STYLES = [("Glass", "glass"), ("Solid", "solid"), ("Metro", "metro"), ("Pixel", "pixel")]
STYLE_NOTES = ["A blurred piece of your wallpaper in each box.",
               "Plain boxes in colours from your wallpaper.",
               "Flat square tiles in bold colours, like Windows.",
               "Soft rounded colours, like Android phones."]
STYLE_FIT = {"glass": {"corner_radius": 24, "tile_gap": 14},
             "solid": {"corner_radius": 18, "tile_gap": 14},
             "metro": {"corner_radius": 0, "tile_gap": 8},
             "pixel": {"corner_radius": 30, "tile_gap": 12}}
LUA_SETTINGS = LOOK_SETTINGS + TEXT_SETTINGS + TILE_SETTINGS
KIND = {key: kind for key, _, _, _, _, kind in LUA_SETTINGS}

DEFAULTS = {
    "text_size": 1.0, "clock_size": 1.0, "temperature_size": 1.0, "caption_size": 1.0,
    "widget_width": 760, "tile_gap": 14, "tile_padding": 22, "corner_radius": 24,
    "scale": "auto", "position": "top_right", "margin_x": 40, "margin_y": 40,
    "style": "glass", "transparency": 0.0,
}

ALIGNMENTS = [["top_left", "top_middle", "top_right"],
              ["middle_left", "middle_middle", "middle_right"],
              ["bottom_left", "bottom_middle", "bottom_right"]]

LUA_RE = {key: re.compile(r'^(\s*%s\s*=\s*)(-?[0-9]*\.?[0-9]+)([ \t]*)' % key, re.M)
          for key, *_ in LUA_SETTINGS}
WALLPAPER_RE = re.compile(r'^\s*wallpaper\s*=\s*"([^"]*)"', re.M)
STYLE_RE = re.compile(r'^(\s*style\s*=\s*)"([A-Za-z]+)"', re.M)
SCALE_RE = re.compile(r'^(local scale = )("auto"|[0-9]*\.?[0-9]+)', re.M)
POSITION_RE = re.compile(r"^(local position = )'([a-z_]+)'", re.M)
MARGIN_RE = re.compile(r'^(local margin_x, margin_y = )(-?\d+), (-?\d+)', re.M)
BASE_RE = re.compile(r'^(local base_width, base_height = )(\d+), (\d+)', re.M)
SIZE_LINE = re.compile(r"size (\d+)x(\d+) at (-?\d+),(-?\d+) scale ([0-9.]+) natural (\d+)x(\d+)")


# --------------------------------------------------------------------- model

def read_settings(lua_text, config_text):
    """What the two files say now, so the editor starts where the user is."""
    values = {}
    for key, pattern in LUA_RE.items():
        m = pattern.search(lua_text)
        if m:
            values[key] = float(m.group(2)) if KIND[key] == "x" else int(round(float(m.group(2))))
    m = STYLE_RE.search(lua_text)
    if m:
        values["style"] = m.group(2).lower()
    m = SCALE_RE.search(config_text)
    if m:
        values["scale"] = "auto" if m.group(2) == '"auto"' else float(m.group(2))
    m = POSITION_RE.search(config_text)
    if m:
        values["position"] = m.group(2)
    m = MARGIN_RE.search(config_text)
    if m:
        values["margin_x"], values["margin_y"] = int(m.group(2)), int(m.group(3))
    return values


def formatted(key, value):
    return "%.2f" % value if KIND.get(key) == "x" else "%d" % round(value)


def replace_number(text, pattern, key, value):
    """Rewrite one value in place. A value that has not moved is left exactly
    as it is, so Apply with nothing changed writes a byte-identical file; one
    that has keeps the comment after it in its column."""
    m = pattern.search(text)
    if not m:
        return text
    if abs(float(m.group(2)) - value) < 1e-9:
        return text
    new = formatted(key, value)
    spaces = m.group(3)
    if text[m.end(3):m.end(3) + 2] == "--":
        spaces = " " * max(1, len(m.group(2)) + len(spaces) - len(new))
    return text[:m.start(2)] + new + spaces + text[m.end(3):]


def apply_lua(text, values):
    for key, pattern in LUA_RE.items():
        if key in values:
            text = replace_number(text, pattern, key, values[key])
    m = STYLE_RE.search(text)
    if m and "style" in values and m.group(2).lower() != values["style"]:
        text = text[:m.start(2)] + values["style"] + text[m.end(2):]
    return text


def apply_config(text, values, natural):
    """The window settings, and the window size the sizes need."""
    scale = values["scale"]
    m = SCALE_RE.search(text)
    if m:
        current = "auto" if m.group(2) == '"auto"' else float(m.group(2))
        if current != scale and not (scale != "auto" and current != "auto" and abs(current - scale) < 1e-9):
            new = '"auto"' if scale == "auto" else "%.2f" % scale
            text = text[:m.start(2)] + new + text[m.end(2):]
    m = POSITION_RE.search(text)
    if m and m.group(2) != values["position"]:
        text = text[:m.start(2)] + values["position"] + text[m.end(2):]
    m = MARGIN_RE.search(text)
    if m and (int(m.group(2)), int(m.group(3))) != (values["margin_x"], values["margin_y"]):
        text = text[:m.start(2)] + "%d, %d" % (values["margin_x"], values["margin_y"]) + text[m.end(3):]
    m = BASE_RE.search(text)
    if m and natural and (int(m.group(2)), int(m.group(3))) != natural:
        text = text[:m.start(2)] + "%d, %d" % natural + text[m.end(3):]
    return text


def overrides(values):
    numbers = ["%s=%s" % (key, formatted(key, values[key])) for key, *_ in LUA_SETTINGS if key in values]
    return numbers + (["style=" + values["style"]] if "style" in values else [])


def where(values):
    return "%s:%d,%d" % (values["position"], values["margin_x"], values["margin_y"])


def scale_arg(values):
    return "auto" if values["scale"] == "auto" else "%.2f" % values["scale"]


def natural_size(values):
    """The window size at scale 1 these settings need, asked of mirage.lua
    itself, so the layout is never computed twice in two languages."""
    env = dict(os.environ, MIRAGE_PREVIEW_SIZE_ONLY="1")
    done = subprocess.run(["lua", RENDERER, os.devnull, "-", "-"] + overrides(values),
                          capture_output=True, text=True, timeout=20, env=env, cwd=HERE)
    m = re.search(r"natural (\d+)x(\d+)", done.stdout)
    return (int(m.group(1)), int(m.group(2))) if m else None


def mirage_pids():
    """Conky processes running Mirage, found by reading /proc rather than by
    pattern-matching `ps` -- a pattern wide enough to catch the conky process
    also catches the shell that was asked to look for it."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % entry, "rb") as handle:
                parts = handle.read().split(b"\0")
        except OSError:
            continue
        if parts and parts[0] and os.path.basename(parts[0].decode("utf-8", "replace")) == "conky" \
                and any(b"start_conky_mirage" in part for part in parts):
            found.append(int(entry))
    return found


def primary_monitor():
    try:
        out = subprocess.run(["xrandr", "--listmonitors"], capture_output=True, text=True,
                             timeout=4).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    best = None
    for line in out.splitlines():
        m = re.search(r"(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)", line)
        if m:
            mon = tuple(int(m.group(i)) for i in (3, 4, 1, 2))     # x, y, w, h
            if best is None or "*" in line.split(":", 1)[-1].split()[0]:
                best = mon
    return best or (0, 0, 1920, 1080)


# ---------------------------------------------------------------- rendering

class Renderer(threading.Thread):
    """One render at a time, and only ever the newest request: a drag across
    a slider produces dozens of requests, and every one but the last is stale
    by the time the previous render finishes. Results go back through a queue
    because Tk must only be touched from its own thread."""

    def __init__(self, results, workdir, monitor, wallpaper_setting):
        super().__init__(daemon=True)
        self.results = results
        self.workdir = workdir
        self.monitor = monitor
        self.wallpaper_setting = wallpaper_setting
        self.cond = threading.Condition()
        self.request = None
        self.screen = None             # the wallpaper as the desktop shows it, made once

    def submit(self, seq, values, folded=False):
        with self.cond:
            self.request = (seq, dict(values), folded)
            self.cond.notify()

    def run(self):
        while True:
            with self.cond:
                while self.request is None:
                    self.cond.wait()
                seq, values, folded = self.request
                self.request = None
            try:
                self.results.put((seq, self.render(seq, values, folded), None))
            except Exception as problem:                  # shown in the preview
                self.results.put((seq, None, str(problem)))

    def ensure_screen(self, Image):
        """The whole desktop at full size, for the "On your screen" view. Made
        once; the editor scales it to whatever size the panel is."""
        if self.screen is None:
            sys.path.insert(0, HERE)
            import mirage
            x, y, w, h = self.monitor
            wallpaper = mirage.find_wallpaper(self.wallpaper_setting or "auto", 0,
                                              {"x": x, "y": y, "w": w, "h": h, "name": ""})
            if wallpaper and "unsupported" not in wallpaper:
                self.screen = mirage.fitted_screen(Image, wallpaper, w, h)
            else:
                self.screen = Image.new("RGB", (w, h), (20, 24, 32))

    def render(self, seq, values, folded=False):
        from PIL import Image
        self.ensure_screen(Image)
        # A file per render: the editor scales the views from the newest one
        # whenever the window changes size, after this thread has moved on.
        out = os.path.join(self.workdir, "frame-%d.png" % seq)
        env = dict(os.environ, MIRAGE_PREVIEW_STILL="1", MIRAGE_PREVIEW_CACHE=PREVIEW_CACHE,
                   MIRAGE_PREVIEW_COMPACT="1" if folded else "0")
        done = subprocess.run(["lua", RENDERER, out, where(values), scale_arg(values)] + overrides(values),
                              capture_output=True, text=True, timeout=90, env=env, cwd=HERE)
        m = SIZE_LINE.search(done.stdout)
        if done.returncode != 0 or not m:
            raise RuntimeError(done.stderr.strip() or done.stdout.strip() or "the renderer failed")
        for old in range(max(0, seq - 6), seq - 2):
            for name in ("frame-%d.png" % old, "frame-%d-alpha.png" % old):
                try:
                    os.remove(os.path.join(self.workdir, name))
                except OSError:
                    pass
        return {"seq": seq, "W": int(m.group(1)), "H": int(m.group(2)), "X": int(m.group(3)),
                "Y": int(m.group(4)), "scale": float(m.group(5)),
                "natural": (int(m.group(6)), int(m.group(7))),
                "actual": out, "alpha": out.replace(".png", "-alpha.png")}


# ------------------------------------------------------------------ drawing

def rounded(canvas, x1, y1, x2, y2, r, **kw):
    """A rounded rectangle: a polygon through the corner points with
    smooth=True, which at these radii is indistinguishable from real arcs."""
    points = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
              x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(points, smooth=True, **kw)


def mix(a, b, t):
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(x + (y - x) * t) for x, y in zip(pa, pb))


class Tooltip:
    """A small note beside a control, shown once the pointer has rested on it
    for a moment and gone as soon as it leaves or clicks. `text` can be a
    function of the pointer's x, for a control with parts that each need
    their own note."""

    DELAY_MS = 450

    def __init__(self, widget, text):
        self.widget, self.text = widget, text
        self.tip = self.job = self.shown_text = None
        widget.bind("<Enter>", lambda e: self._wait(e.x), add="+")
        widget.bind("<Motion>", self._motion, add="+")
        widget.bind("<Leave>", lambda e: self.hide(), add="+")
        widget.bind("<ButtonPress>", lambda e: self.hide(), add="+")

    def _text(self, x):
        return self.text(x) if callable(self.text) else self.text

    def _motion(self, event):
        if self.tip is None:
            self._wait(event.x)
        elif self._text(event.x) != self.shown_text:
            self.hide()
            self._wait(event.x)

    def _wait(self, x):
        if self.job:
            self.widget.after_cancel(self.job)
        self.job = self.widget.after(self.DELAY_MS, lambda: self._show(x))

    def _show(self, x):
        self.job = None
        text = self._text(x)
        if not text:
            return
        self.shown_text = text
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        tip.configure(bg=BORDER_LIT)
        tk.Label(tip, text=text, bg="#1C2430", fg=TEXT, font=F_SMALL, justify="left",
                 wraplength=270, padx=10, pady=7).pack(padx=1, pady=1)
        tip.update_idletasks()
        # Below the control, under the pointer, and never off the screen.
        left = self.widget.winfo_rootx() + max(0, min(x, self.widget.winfo_width())) - 16
        top = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        left = max(0, min(left, self.widget.winfo_screenwidth() - tip.winfo_reqwidth() - 4))
        if top + tip.winfo_reqheight() > self.widget.winfo_screenheight():
            top = self.widget.winfo_rooty() - tip.winfo_reqheight() - 6
        tip.geometry("+%d+%d" % (left, top))
        self.tip = tip

    def hide(self):
        if self.job:
            self.widget.after_cancel(self.job)
            self.job = None
        if self.tip is not None:
            self.tip.destroy()
            self.tip = None


def make_icon(path, size):
    """The window's icon: three sliders on a frosted tile in Mirage's own
    dusk colours, drawn four times larger and scaled down for clean edges."""
    from PIL import Image, ImageDraw, ImageFilter
    S = size * 4

    top, bottom = (46, 38, 98), (118, 56, 112)
    column = Image.linear_gradient("L").resize((1, S))
    rgb = [column.point(lambda v, a=a, b=b: int(a + (b - a) * v / 255)).resize((S, S))
           for a, b in zip(top, bottom)]
    tile = Image.merge("RGB", rgb).convert("RGBA")

    # A warm low sun behind the glass, as on the wallpaper Mirage was made on.
    glow = Image.new("L", (S, S), 0)
    ImageDraw.Draw(glow).ellipse((S * 0.05, S * 0.62, S * 0.95, S * 1.35), fill=150)
    glow = glow.filter(ImageFilter.GaussianBlur(S * 0.12))
    tile = Image.composite(Image.new("RGBA", (S, S), (255, 150, 110, 255)), tile, glow)
    # Light catching the top of the glass.
    sheen = Image.linear_gradient("L").resize((S, S)).point(lambda v: int(max(0, 70 - v * 0.55)))
    tile = Image.composite(Image.new("RGBA", (S, S), (255, 255, 255, 255)), tile, sheen)

    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=int(S * 0.23), fill=255)
    icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    icon.paste(tile, (0, 0), mask)

    draw = ImageDraw.Draw(icon)
    edge = Image.new("L", (S, S), 0)
    ImageDraw.Draw(edge).rounded_rectangle((2, 2, S - 3, S - 3), radius=int(S * 0.23),
                                           outline=110, width=max(2, int(S * 0.012)))
    icon.paste((255, 255, 255, 255), (0, 0), edge)

    knobs = [(0.40, (255, 196, 150)), (0.64, (196, 182, 255)), (0.50, (150, 214, 255))]
    for row, (at, colour) in enumerate(knobs):
        y = S * (0.33 + row * 0.17)
        h = S * 0.036
        draw.rounded_rectangle((S * 0.24, y - h / 2, S * 0.76, y + h / 2), radius=h / 2,
                               fill=(255, 255, 255, 120))
        draw.rounded_rectangle((S * 0.24, y - h / 2, S * at, y + h / 2), radius=h / 2,
                               fill=colour + (255,))
        r = S * 0.07
        cx = S * at
        shadow = Image.new("L", (S, S), 0)
        ImageDraw.Draw(shadow).ellipse((cx - r, y - r + S * 0.02, cx + r, y + r + S * 0.02), fill=110)
        shadow = shadow.filter(ImageFilter.GaussianBlur(S * 0.015))
        icon.paste((20, 10, 40, 255), (0, 0), shadow)
        draw = ImageDraw.Draw(icon)
        draw.ellipse((cx - r, y - r, cx + r, y + r), fill=(255, 255, 255, 255))
        inner = r * 0.62
        draw.ellipse((cx - inner, y - inner, cx + inner, y + inner), fill=colour + (255,))

    icon.resize((size, size), Image.LANCZOS).save(path)


class Button(tk.Canvas):
    """A flat rounded button with hover and press states."""

    def __init__(self, parent, text, command, primary=False, width=None):
        self.text, self.command, self.primary = text, command, primary
        w = width or (len(text) * 7 + 34)
        super().__init__(parent, width=w, height=34, bg=BG, highlightthickness=0, cursor="hand2")
        self.state = "normal"
        self.bind("<Enter>", lambda e: self._set("hover"))
        self.bind("<Leave>", lambda e: self._set("normal"))
        self.bind("<ButtonPress-1>", lambda e: self._set("press"))
        self.bind("<ButtonRelease-1>", self._release)
        self._draw()

    def _set(self, state):
        self.state = state
        self._draw()

    def _release(self, event):
        inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
        self._set("hover" if inside else "normal")
        if inside:
            self.command()

    def _draw(self):
        self.delete("all")
        w = int(self["width"])
        if self.primary:
            fill = {"normal": ACCENT_DIM, "hover": mix(ACCENT_DIM, ACCENT, 0.3),
                    "press": mix(ACCENT_DIM, ACCENT, 0.45)}[self.state]
            edge, ink = ACCENT, ACCENT
        else:
            fill = {"normal": SURFACE, "hover": "#1E2733", "press": "#242F3D"}[self.state]
            edge, ink = (BORDER_LIT if self.state != "normal" else BORDER), TEXT
        rounded(self, 1, 1, w - 1, 33, 8, fill=fill, outline=edge)
        self.create_text(w / 2, 17, text=self.text, fill=ink, font=F_BODY)


class Slider(tk.Canvas):
    """Track, filled portion and a round handle. Click to jump, drag to scrub;
    the mouse wheel and the arrow keys step. Values snap to `step`, and a
    double-click goes back to the shipped default."""

    H = 26

    def __init__(self, parent, low, high, step, value, default, command, width=250):
        super().__init__(parent, width=width, height=self.H, bg=SURFACE,
                         highlightthickness=0, cursor="hand2", takefocus=1)
        self.low, self.high, self.step = low, high, step
        self.value, self.default = value, default
        self.command = command
        self.w = width
        self.active, self.hover, self.enabled = False, False, True
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Double-Button-1>", lambda e: self.enabled and self.set(self.default, notify=True))
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self.bind("<Button-4>", lambda e: self._nudge(1))
        self.bind("<Button-5>", lambda e: self._nudge(-1))
        self.bind("<MouseWheel>", lambda e: self._nudge(1 if e.delta > 0 else -1))
        self.bind("<Left>", lambda e: self._nudge(-1))
        self.bind("<Right>", lambda e: self._nudge(1))
        self._draw()

    def enable(self, on):
        self.enabled = on
        self.configure(cursor="hand2" if on else "")
        self._draw()

    def _hover(self, on):
        self.hover = on
        self._draw()

    def _x(self, value):
        span = self.high - self.low or 1
        return 10 + (value - self.low) / span * (self.w - 20)

    def _from_x(self, x):
        t = (x - 10) / max(self.w - 20, 1)
        return self.low + max(0.0, min(1.0, t)) * (self.high - self.low)

    def _press(self, event):
        if not self.enabled:
            return
        self.focus_set()
        self.active = True
        self.set(self._from_x(event.x), notify=True)

    def _drag(self, event):
        if self.active:
            self.set(self._from_x(event.x), notify=True)

    def _release(self, _event):
        self.active = False
        self._draw()

    def _nudge(self, direction):
        if self.enabled:
            self.set(self.value + direction * self.step, notify=True)

    def set(self, value, notify=False):
        value = round((max(self.low, min(self.high, value)) - self.low) / self.step) * self.step + self.low
        value = round(value, 6)
        if abs(value - self.value) > 1e-9:
            self.value = value
            if notify:
                self.command(value)
        self._draw()

    def _draw(self):
        self.delete("all")
        mid = self.H / 2
        rounded(self, 10, mid - 2, self.w - 10, mid + 2, 2, fill=SUNKEN, outline=BORDER)
        x = self._x(self.value)
        lit = ACCENT if self.enabled else FAINT
        r = 8 if (self.active or self.hover) and self.enabled else 6.5
        # Only once the lit part reaches well out from under the handle: a
        # few lit pixels beside it, near the start of the track, read as a speck.
        if x - r > 18:
            rounded(self, 10, mid - 2, x, mid + 2, 2, fill=lit, outline=lit)
        ring = lit if (self.active or self.hover) and self.enabled else BORDER_LIT
        self.create_oval(x - r, mid - r, x + r, mid + r, fill=SURFACE, outline=ring, width=2)


class Segmented(tk.Canvas):
    """A pill split into segments -- a radio group that does not look like one."""

    H = 30

    def __init__(self, parent, options, value, command, width=250):
        super().__init__(parent, width=width, height=self.H, bg=BG, highlightthickness=0,
                         cursor="hand2")
        self.options, self.value, self.command, self.w = options, value, command, width
        self.hover_index = -1
        self.bind("<Configure>", self._resized)
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", lambda e: self._set_hover(self._index_at(e.x)))
        self.bind("<Leave>", lambda e: self._set_hover(-1))
        self._draw()

    def _resized(self, event):
        if event.width != self.w:
            self.w = event.width
            self._draw()

    def _index_at(self, x):
        return max(0, min(len(self.options) - 1, int(x // (self.w / len(self.options)))))

    def _set_hover(self, index):
        if index != self.hover_index:
            self.hover_index = index
            self._draw()

    def _click(self, event):
        _, value = self.options[self._index_at(event.x)]
        if value != self.value:
            self.value = value
            self._draw()
            self.command(value)

    def _draw(self):
        self.delete("all")
        rounded(self, 1, 1, self.w - 1, self.H - 1, 9, fill=SUNKEN, outline=BORDER)
        step = (self.w - 4) / len(self.options)
        for i, (label, value) in enumerate(self.options):
            x1 = 2 + i * step
            chosen = value == self.value
            if chosen:
                rounded(self, x1 + 1, 3, x1 + step - 1, self.H - 3, 7, fill=SURFACE, outline=ACCENT)
            ink = ACCENT if chosen else (TEXT if i == self.hover_index else MUTED)
            self.create_text(x1 + step / 2, self.H / 2, text=label, fill=ink, font=F_SMALL)


class Switch(tk.Canvas):
    """An on/off toggle: a pill whose knob sits right, lit, when on."""

    W, H = 34, 20

    def __init__(self, parent, value, command):
        super().__init__(parent, width=self.W, height=self.H, bg=SURFACE, highlightthickness=0,
                         cursor="hand2")
        self.value, self.hover, self.command = value, False, command
        self.bind("<Button-1>", lambda e: self.command(not self.value))
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self._draw()

    def _hover(self, on):
        self.hover = on
        self._draw()

    def show(self, value):
        self.value = value
        self._draw()

    def _draw(self):
        self.delete("all")
        h = self.H
        if self.value:
            track, edge, knob = ACCENT_DIM, ACCENT, ACCENT
        else:
            track, edge, knob = SUNKEN, MUTED if self.hover else BORDER_LIT, MUTED
        rounded(self, 1, 1, self.W - 1, h - 1, (h - 2) / 2, fill=track, outline=edge)
        x = self.W - h / 2 if self.value else h / 2
        r = h / 2 - 4
        self.create_oval(x - r, h / 2 - r, x + r, h / 2 + r, fill=knob, outline=knob)


def ink_on(colour):
    r, g, b = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
    return "#111318" if (r * 299 + g * 587 + b * 114) / 1000 > 150 else "#FFFFFF"


class ColourPicker(tk.Frame):
    """A saturation/value square over a hue strip, after the orrery's colour
    editor (Conky_Orrery/orrery_colors.py), smaller. The square is a
    PhotoImage filled a pixel at a time, rebuilt only when the hue moves.
    Not used here: ConkyTray's and Mirage-Terminal's settings windows take it."""

    SQ = 136
    STRIP = 12

    def __init__(self, parent, command):
        super().__init__(parent, bg=SURFACE)
        self.command = command
        self.h, self.s, self.v = 0.6, 0.5, 0.5
        self.square = tk.Canvas(self, width=self.SQ, height=self.SQ, bg=SURFACE, highlightthickness=1,
                                highlightbackground=BORDER, cursor="crosshair")
        self.square.grid(row=0, column=0, sticky="w")
        self.square.bind("<Button-1>", self._square_at)
        self.square.bind("<B1-Motion>", self._square_at)
        self.strip = tk.Canvas(self, width=self.SQ, height=self.STRIP, bg=SURFACE, highlightthickness=1,
                               highlightbackground=BORDER, cursor="sb_h_double_arrow")
        self.strip.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.strip.bind("<Button-1>", self._strip_at)
        self.strip.bind("<B1-Motion>", self._strip_at)
        self._image = tk.PhotoImage(width=self.SQ, height=self.SQ)
        self.square.create_image(0, 0, image=self._image, anchor="nw")
        for x in range(self.SQ):
            r, g, b = colorsys.hsv_to_rgb(x / (self.SQ - 1), 1.0, 1.0)
            self.strip.create_line(x, 0, x, self.STRIP, fill="#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255)))
        self._rebuild()

    def _rebuild(self):
        size, rows = self.SQ, []
        for y in range(size):
            value = 1.0 - y / (size - 1)
            row = []
            for x in range(size):
                r, g, b = colorsys.hsv_to_rgb(self.h, x / (size - 1), value)
                row.append("#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255)))
            rows.append("{" + " ".join(row) + "}")
        self._image.put(" ".join(rows))
        self._cursors()

    def _cursors(self):
        self.square.delete("cursor")
        x, y = self.s * (self.SQ - 1), (1.0 - self.v) * (self.SQ - 1)
        ink = "#000000" if self.v > 0.6 and self.s < 0.6 else "#FFFFFF"
        self.square.create_oval(x - 6, y - 6, x + 6, y + 6, outline=ink, width=2, tags="cursor")
        self.strip.delete("cursor")
        hx = self.h * (self.SQ - 1)
        self.strip.create_rectangle(hx - 2, 0, hx + 2, self.STRIP, outline="#FFFFFF", width=2, tags="cursor")

    def _square_at(self, event):
        self.s = max(0.0, min(1.0, event.x / (self.SQ - 1)))
        self.v = max(0.0, min(1.0, 1.0 - event.y / (self.SQ - 1)))
        self._cursors()
        self.command(self.hex())

    def _strip_at(self, event):
        self.h = max(0.0, min(1.0, event.x / (self.SQ - 1)))
        self._rebuild()
        self.command(self.hex())

    def hex(self):
        r, g, b = colorsys.hsv_to_rgb(self.h, self.s, self.v)
        return "#%02x%02x%02x" % (int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5))

    def set_hex(self, value):
        r, g, b = (int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        if s > 0.004:                  # a grey has no hue; leave the strip where it was
            self.h = h
        self.s, self.v = s, v
        self._rebuild()


class Swatch(tk.Canvas):
    """One colour to choose: filled with it, "Auto" or its code written on it,
    outlined when it is the one the picker is changing."""

    W, H = 112, 30

    def __init__(self, parent, command):
        super().__init__(parent, width=self.W, height=self.H, bg=SURFACE, highlightthickness=0, cursor="hand2")
        self.colour, self.auto, self.selected, self.hover = "#808080", True, False, False
        self.bind("<Button-1>", lambda e: command())
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))

    def _hover(self, on):
        self.hover = on
        self._draw()

    def show(self, colour, auto, selected):
        self.colour, self.auto, self.selected = colour, auto, selected
        self._draw()

    def _draw(self):
        self.delete("all")
        outline = ACCENT if self.selected else (BORDER_LIT if self.hover else BORDER)
        rounded(self, 2, 2, self.W - 2, self.H - 2, 7, fill=self.colour, outline=outline,
                   width=2 if self.selected else 1)
        self.create_text(self.W / 2, self.H / 2, text="Auto" if self.auto else self.colour.upper(),
                         fill=ink_on(self.colour), font=F_SMALL)


class PositionPicker(tk.Canvas):
    """The screen as a three-by-three grid of the places conky can anchor a
    window; the chosen one holds a small widget-shaped block."""

    W, H = 132, 84

    def __init__(self, parent, value, command):
        super().__init__(parent, width=self.W, height=self.H, bg=SURFACE, highlightthickness=0,
                         cursor="hand2")
        self.value, self.command = value, command
        self.hover = None
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._set_hover(None))
        self._draw()

    def _cell(self, x, y):
        col = max(0, min(2, int((x - 2) / ((self.W - 4) / 3))))
        row = max(0, min(2, int((y - 2) / ((self.H - 4) / 3))))
        return ALIGNMENTS[row][col]

    def _motion(self, event):
        self._set_hover(self._cell(event.x, event.y))

    def _set_hover(self, cell):
        if cell != self.hover:
            self.hover = cell
            self._draw()

    def _click(self, event):
        cell = self._cell(event.x, event.y)
        if cell != self.value:
            self.value = cell
            self._draw()
            self.command(cell)

    def _draw(self):
        self.delete("all")
        rounded(self, 1, 1, self.W - 1, self.H - 1, 8, fill=SUNKEN, outline=BORDER)
        cw, ch = (self.W - 4) / 3, (self.H - 4) / 3
        for r, row in enumerate(ALIGNMENTS):
            for c, name in enumerate(row):
                cx, cy = 2 + cw * (c + 0.5), 2 + ch * (r + 0.5)
                if name == self.value:
                    # The widget is taller than wide; so is its stand-in.
                    rounded(self, cx - 8, cy - 10, cx + 8, cy + 10, 4, fill=ACCENT_DIM, outline=ACCENT)
                elif name == self.hover:
                    rounded(self, cx - 8, cy - 10, cx + 8, cy + 10, 4, fill=SURFACE, outline=BORDER_LIT)
                else:
                    self.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, fill=FAINT, outline=FAINT)


# ----------------------------------------------------------------- the editor

class Editor(tk.Tk):
    def __init__(self):
        super().__init__(className="mirage-settings")
        self.title("Mirage settings")
        self.configure(bg=BG)
        self.workdir = tempfile.mkdtemp(prefix="mirage-settings-")
        self._set_icon()

        with open(SCRIPT, encoding="utf-8") as handle:
            self.lua_text = handle.read()
        with open(CONFIG, encoding="utf-8") as handle:
            self.config_text = handle.read()
        self.saved = read_settings(self.lua_text, self.config_text)
        missing = [key for key in DEFAULTS if key not in self.saved]
        if missing:
            messagebox.showerror(
                "Can't open the settings",
                "This copy of Mirage is older than this settings window. Get the latest "
                "Conky-Mirage and try again.\n\n(Missing: %s)" % ", ".join(missing))
            self.destroy()
            raise SystemExit(1)
        self.values = dict(self.saved)

        self.monitor = primary_monitor()
        wallpaper = WALLPAPER_RE.search(self.lua_text)
        self.results = queue.Queue()
        self.renderer = Renderer(self.results, self.workdir, self.monitor,
                                 wallpaper.group(1) if wallpaper else "auto")
        self.renderer.start()
        self.seq = 0
        self.pending = None
        self.view = "fit"
        self.folded = False            # preview the widget folded into its strip
        self.shown = None              # the last render's info
        self.image = None
        # The preview panel's size, which follows the window. The views are
        # scaled to it from the newest render, so resizing never re-renders.
        self.view_size = (PREVIEW_W, PREVIEW_H)
        self.view_cache = {}
        self.view_serial = 0
        self.desk_thumb = None
        self.resize_job = None

        self._build()
        # Free to grow, maximise included, but never smaller than it opens.
        self.update_idletasks()
        self.minsize(self.winfo_reqwidth(), self.winfo_reqheight())
        self.resizable(True, True)
        self.after(60, self._drain)
        self.refresh()
        self.request_render()

    # -------------------------------------------------------------- building

    def _set_icon(self):
        """Tk's own feather otherwise, which says nothing about what this is."""
        self.icons = []
        try:
            for size in (256, 64, 48, 32, 16):
                path = os.path.join(self.workdir, "icon-%d.png" % size)
                make_icon(path, size)
                self.icons.append(tk.PhotoImage(file=path))
            self.iconphoto(True, *self.icons)
        except Exception:                                  # an icon is not worth failing over
            pass

    def _section(self, parent, title, row):
        head = tk.Frame(parent, bg=BG)
        head.grid(row=row, column=0, sticky="ew", pady=(14 if row else 2, 6))
        tk.Label(head, text=title.upper(), bg=BG, fg=FAINT, font=(FONT, 8, "bold")).pack(side="left")
        tk.Frame(head, bg=BORDER, height=1).pack(side="left", fill="x", expand=True, padx=(10, 0),
                                                 pady=(1, 0))
        card = tk.Frame(parent, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER,
                        padx=14, pady=12)
        card.grid(row=row + 1, column=0, sticky="ew")
        card.columnconfigure(0, weight=1)
        return card

    def _slider_row(self, card, row, key, label, low, high, step, width=380, note=None):
        head = tk.Frame(card, bg=SURFACE)
        head.grid(row=row * 2, column=0, sticky="ew", pady=(0 if row == 0 else 8, 0))
        name = tk.Label(head, text=label, bg=SURFACE, fg=TEXT, font=F_BODY)
        name.pack(side="left")
        value = tk.Label(head, text="", bg=SURFACE, fg=ACCENT, font=F_MONO)
        value.pack(side="right")
        slider = Slider(card, low, high, step, self.values[key], DEFAULTS[key],
                        command=lambda v, k=key: self.set_value(k, v), width=width)
        slider.grid(row=row * 2 + 1, column=0, sticky="w")
        note = note or NOTES.get(key)
        if note:
            Tooltip(name, note)
            Tooltip(slider, note)
        Tooltip(value, lambda x, k=key: NOTES["changed"] if self.is_changed(k) else "")
        self.sliders[key], self.readouts[key], self.names[key] = slider, value, name

    def _build(self):
        root = tk.Frame(self, bg=BG, padx=16, pady=14)
        root.pack(fill="both", expand=True)
        left = tk.Frame(root, bg=BG)
        left.grid(row=0, column=0, sticky="n", padx=(0, 16))
        right = tk.Frame(root, bg=BG)
        right.grid(row=0, column=1, sticky="nsew")
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        self.sliders, self.readouts, self.names = {}, {}, {}
        card = self._section(left, "Look", 0)
        self.style_picker = Segmented(card, STYLES, self.values["style"], self.set_style, width=380)
        self.style_picker.configure(bg=SURFACE)
        self.style_picker.grid(row=0, column=0, sticky="w", pady=(0, 10))
        Tooltip(self.style_picker, lambda x: STYLE_NOTES[self.style_picker._index_at(x)] +
                "\nAlso sets the corners and spacing to suit it.")
        for i, (key, label, low, high, step, _) in enumerate(LOOK_SETTINGS):
            self._slider_row(card, i + 1, key, label, low, high, step)

        card = self._section(left, "Text size", 2)
        for i, (key, label, low, high, step, _) in enumerate(TEXT_SETTINGS):
            self._slider_row(card, i, key, label, low, high, step)

        card = self._section(left, "Boxes", 4)
        for i, (key, label, low, high, step, _) in enumerate(TILE_SETTINGS):
            self._slider_row(card, i, key, label, low, high, step)

        card = self._section(left, "Size and place", 6)
        head = tk.Frame(card, bg=SURFACE)
        head.grid(row=0, column=0, sticky="ew")
        overall = tk.Label(head, text="Overall size", bg=SURFACE, fg=TEXT, font=F_BODY)
        overall.pack(side="left")
        self.scale_readout = tk.Label(head, text="", bg=SURFACE, fg=ACCENT, font=F_MONO)
        self.scale_readout.pack(side="right")
        fit_label = tk.Label(head, text="Fit my screen", bg=SURFACE, fg=MUTED, font=F_SMALL)
        fit_label.pack(side="left", padx=(14, 6))
        self.auto_switch = Switch(head, self.values["scale"] == "auto", self.set_auto)
        self.auto_switch.pack(side="left")
        start = self.values["scale"] if self.values["scale"] != "auto" else 1.0
        self.scale_slider = Slider(card, 0.50, 2.00, 0.05, start, 1.0, self.set_scale, width=380)
        self.scale_slider.grid(row=1, column=0, sticky="w")
        self.sliders["scale"] = self.scale_slider
        Tooltip(overall, NOTES["scale"])
        Tooltip(self.scale_slider, lambda x: NOTES["scale"] if self.values["scale"] != "auto" else
                "Turn off \"Fit my screen\" to choose the size yourself.")
        Tooltip(fit_label, NOTES["auto"])
        Tooltip(self.auto_switch, NOTES["auto"])

        place = tk.Frame(card, bg=SURFACE)
        place.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        where_label = tk.Label(place, text="Where", bg=SURFACE, fg=TEXT, font=F_BODY)
        where_label.grid(row=0, column=0, sticky="w")
        self.picker = PositionPicker(place, self.values["position"], self.set_position)
        self.picker.grid(row=1, column=0, sticky="nw", pady=(6, 0))
        Tooltip(where_label, NOTES["position"])
        Tooltip(self.picker, NOTES["position"])
        margins = tk.Frame(place, bg=SURFACE)
        margins.grid(row=0, column=1, rowspan=2, sticky="nw", padx=(16, 0))
        margins.columnconfigure(0, weight=1)
        mon_w, mon_h = self.monitor[2], self.monitor[3]
        for i, (key, high) in enumerate((("margin_x", max(800, mon_w // 2)),
                                         ("margin_y", max(600, mon_h // 2)))):
            self._slider_row(margins, i, key, "", 0, high, 1, width=226,
                             note=lambda x, k=key: self.margin_note(k))

        # ---- preview
        top = tk.Frame(right, bg=BG)
        top.grid(row=0, column=0, sticky="ew", pady=(2, 10))
        top.columnconfigure(0, weight=1)
        self.views = Segmented(top, [("Whole widget", "fit"), ("Real size", "actual"),
                                     ("On your screen", "desktop")], self.view, self.set_view,
                               width=PREVIEW_W - 168)
        self.views.grid(row=0, column=0, sticky="ew")
        Tooltip(self.views, lambda x: VIEW_NOTES[self.views._index_at(x)])
        self.fold_picker = Segmented(top, [("Open", False), ("Folded", True)], self.folded,
                                     self.set_folded, width=160)
        self.fold_picker.grid(row=0, column=1, sticky="e", padx=(10, 0))
        Tooltip(self.fold_picker, "See the widget open, or folded into a narrow strip at the "
                                  "edge of the screen. The arrows in its top corner fold it.")
        frame = tk.Frame(right, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER)
        frame.grid(row=1, column=0, sticky="nsew")
        self.canvas = tk.Canvas(frame, width=PREVIEW_W, height=PREVIEW_H, bg=SUNKEN,
                                highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._canvas_resized)
        self.canvas.bind("<ButtonPress-1>", lambda e: self.canvas.scan_mark(e.x, e.y))
        self.canvas.bind("<B1-Motion>", self._pan)
        self.canvas.bind("<Button-4>", lambda e: self._scroll(-1))
        self.canvas.bind("<Button-5>", lambda e: self._scroll(1))
        self.canvas.bind("<MouseWheel>", lambda e: self._scroll(-1 if e.delta > 0 else 1))
        self.canvas_text = self.canvas.create_text(
            PREVIEW_W // 2, PREVIEW_H // 2, text="Loading the preview…", fill=MUTED,
            width=PREVIEW_W - 80, justify="center", font=F_BODY)

        info = tk.Frame(right, bg=BG)
        info.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.size_label = tk.Label(info, text="", bg=BG, fg=MUTED, font=F_SMALL)
        self.size_label.pack(side="left")
        reload_button = Button(info, "Reload", self.reload_preview, width=72)
        reload_button.pack(side="right")
        Tooltip(reload_button, "Draw the preview again, for example after you change "
                               "your wallpaper.")
        self.busy = tk.Label(info, text="", bg=BG, fg=FAINT, font=F_SMALL)
        self.busy.pack(side="right", padx=(0, 10))

        bar = tk.Frame(right, bg=BG)
        bar.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        # One line, never wrapped, and the same width whatever it says, so a
        # message can never make the window jump.
        self.status = tk.Label(bar, text="", bg=BG, fg=ACCENT, font=F_SMALL, anchor="w", width=34)
        self.status.pack(side="left")
        # One Save, which always restarts the widget. Conky reloads its config
        # by itself when the file changes, and comes back from that with a
        # 1x1 window, so a save that left the widget running was never safe.
        buttons = [
            (Button(bar, "Save", self.save, primary=True, width=76),
             "Save, and restart the widget so you see the change."),
            (Button(bar, "Defaults", self.defaults, width=84), "Go back to the original sizes."),
            (Button(bar, "Undo", self.revert, width=70), "Undo everything since you last saved."),
        ]
        for i, (button, note) in enumerate(buttons):
            button.pack(side="right", padx=(0, 0 if i == 0 else 8))
            Tooltip(button, note)

    # ------------------------------------------------------------- reactions

    def is_changed(self, key):
        value, was = self.values.get(key), self.saved.get(key, self.values.get(key))
        return isinstance(value, (int, float)) and abs(was - value) > 1e-9

    def margin_note(self, key):
        """The margins mean different things in different places, so they are
        named for where the widget is."""
        vertical, horizontal = self.values["position"].split("_")
        if key == "margin_x":
            return {"left": "Distance from the left edge of the screen.",
                    "right": "Distance from the right edge of the screen.",
                    "middle": "Moves the widget left from the middle."}[horizontal] + RESET
        return {"top": "Distance from the top of the screen.",
                "bottom": "Distance from the bottom of the screen.",
                "middle": "Moves the widget up from the middle."}[vertical] + RESET

    def refresh(self):
        for key, slider in self.sliders.items():
            if key == "scale":
                continue
            slider.set(self.values[key])
            value = self.values[key]
            text = "%d%%" % round(value * 100) if KIND.get(key) == "x" else "%d px" % value
            if abs(self.saved.get(key, value) - value) > 1e-9:
                text = "• " + text
            self.readouts[key].configure(text=text)
        auto = self.values["scale"] == "auto"
        self.auto_switch.show(auto)
        self.scale_slider.enable(not auto)
        if auto:
            used = self.shown["scale"] if self.shown else None
            self.scale_readout.configure(text="%d%%" % round(used * 100) if used else "")
        else:
            self.scale_slider.set(self.values["scale"])
            self.scale_readout.configure(text="%d%%" % round(self.values["scale"] * 100))
        vertical, horizontal = self.values["position"].split("_")
        self.names["margin_x"].configure(text={"left": "From the left", "right": "From the right",
                                               "middle": "Move left"}[horizontal])
        self.names["margin_y"].configure(text={"top": "From the top", "bottom": "From the bottom",
                                               "middle": "Move up"}[vertical])
        self.picker.value = self.values["position"]
        self.picker._draw()
        if self.style_picker.value != self.values["style"]:
            self.style_picker.value = self.values["style"]
            self.style_picker._draw()

    def set_style(self, value):
        self.values["style"] = value
        self.values.update(STYLE_FIT.get(value, {}))
        self.changed()
        self.status.configure(text="Corners and spacing set to suit this look.", fg=ACCENT)

    def reload_preview(self):
        """Start the preview from scratch: the wallpaper may have changed
        since the window opened, and the desktop view keeps a copy of it."""
        self.renderer.screen = None
        self.desk_thumb = None
        self.view_cache = {}
        self.request_render()

    def set_value(self, key, value):
        self.values[key] = value if KIND.get(key) == "x" else int(round(value))
        self.changed()

    def set_scale(self, value):
        self.values["scale"] = round(value, 2)
        self.changed()

    def set_auto(self, on):
        self.values["scale"] = "auto" if on else round(self.shown["scale"] if self.shown else 1.0, 2)
        self.changed()

    def set_position(self, value):
        self.values["position"] = value
        self.changed()

    def set_view(self, value):
        self.view = value
        self._show()

    def set_folded(self, value):
        self.folded = value
        self.request_render()

    def changed(self):
        self.status.configure(text="")
        self.refresh()
        self.request_render()

    def revert(self):
        self.values = dict(self.saved)
        self.changed()
        self.status.configure(text="Changes undone.", fg=ACCENT)

    def defaults(self):
        self.values = dict(DEFAULTS)
        self.changed()
        self.status.configure(text="Original look and sizes. Save to keep them.", fg=ACCENT)

    # ---------------------------------------------------------------- render

    def request_render(self):
        if self.pending is not None:
            self.after_cancel(self.pending)
        self.pending = self.after(DEBOUNCE_MS, self._start_render)

    def _start_render(self):
        self.pending = None
        self.seq += 1
        self.busy.configure(text="Updating the preview…")
        self.renderer.submit(self.seq, self.values, self.folded)

    def _drain(self):
        newest = None
        while True:
            try:
                newest = self.results.get_nowait()
            except queue.Empty:
                break
        if newest is not None:
            seq, info, problem = newest
            if seq == self.seq:
                self.busy.configure(text="")
            if problem:
                if seq == self.seq:
                    self.shown = None
                    self.canvas.delete("preview")
                    self.canvas.itemconfigure(self.canvas_text, state="normal",
                                              text="Couldn't draw the preview.\n\n" + problem)
            else:
                # A finished render is shown even if a newer one is on its
                # way: it is closer to what was asked than what is up now.
                self.shown = info
                self._show()
                self.refresh()
        self.after(60, self._drain)

    def _canvas_resized(self, event):
        size = (event.width, event.height)
        if size == self.view_size:
            return
        self.view_size = size
        self.canvas.coords(self.canvas_text, event.width // 2, event.height // 2)
        self.canvas.itemconfigure(self.canvas_text, width=max(200, event.width - 80))
        # A drag fires this many times a second; scale once it settles.
        if self.resize_job:
            self.after_cancel(self.resize_job)
        self.resize_job = self.after(120, self._show)

    def _desktop_view(self, info, width, height):
        """The whole screen scaled into the panel, the widget where it will sit."""
        from PIL import Image
        mx, my, mw, mh = self.monitor
        factor = min((width - 24) / mw, (height - 44) / mh)
        size = (max(1, round(mw * factor)), max(1, round(mh * factor)))
        if self.desk_thumb is None or self.desk_thumb[0] != size:
            screen = self.renderer.screen
            base = screen.resize(size, Image.LANCZOS) if screen is not None else \
                Image.new("RGB", size, (20, 24, 32))
            self.desk_thumb = (size, base)
        desk = self.desk_thumb[1].copy().convert("RGBA")
        widget = Image.open(info["alpha"]).convert("RGBA")
        small = widget.resize((max(1, round(info["W"] * factor)), max(1, round(info["H"] * factor))),
                              Image.LANCZOS)
        desk.alpha_composite(small, (max(0, round((info["X"] - mx) * factor)),
                                     max(0, round((info["Y"] - my) * factor))))
        return desk.convert("RGB")

    def _view_path(self, info):
        """The picture for the chosen view at the panel's current size, made
        once per render, view and size."""
        if self.view == "actual":
            return info["actual"]
        width, height = self.view_size
        key = (info["seq"], self.view, width, height)
        if self.view_cache.get("key") == key:
            return self.view_cache["path"]
        from PIL import Image
        if self.view == "fit":
            picture = Image.open(info["actual"]).convert("RGB")
            picture.thumbnail((max(60, width - 24), max(60, height - 24)), Image.LANCZOS)
        else:
            picture = self._desktop_view(info, width, height)
        self.view_serial += 1
        path = os.path.join(self.workdir, "view-%d.png" % self.view_serial)
        picture.save(path, compress_level=1)
        old = self.view_cache.get("path")
        if old:
            try:
                os.remove(old)
            except OSError:
                pass
        self.view_cache = {"key": key, "path": path}
        return path

    def _show(self):
        self.resize_job = None
        info = self.shown
        if not info:
            return
        try:
            path = self._view_path(info)
        except (OSError, ValueError) as failure:
            self.canvas.itemconfigure(self.canvas_text, text=str(failure), state="normal")
            return
        try:
            image = tk.PhotoImage(file=path)
        except tk.TclError as failure:
            self.canvas.itemconfigure(self.canvas_text, text=str(failure), state="normal")
            return
        self.image = image                                 # Tk will not hold this for us
        self.canvas.delete("preview")
        self.canvas.xview_moveto(0)
        self.canvas.yview_moveto(0)
        w, h = image.width(), image.height()
        pw, ph = self.view_size
        if self.view == "actual":
            # Real pixels: drag to look around one larger than the panel.
            x, y = max(0, (pw - w) // 2), max(0, (ph - h) // 2)
            self.canvas.configure(scrollregion=(0, 0, max(w, pw), max(h, ph)),
                                  cursor="fleur" if w > pw or h > ph else "")
        else:
            x, y = (pw - w) // 2, (ph - h) // 2
            self.canvas.configure(scrollregion=(0, 0, pw, ph), cursor="")
        self.canvas.create_image(x, y, image=image, anchor="nw", tags="preview")
        if self.view == "desktop":
            self.canvas.create_text(x + 2, y + h + 14, anchor="w", fill=FAINT, font=F_SMALL, tags="preview",
                                    text="Your screen, %d × %d" % (self.monitor[2], self.monitor[3]))
        self.canvas.itemconfigure(self.canvas_text, state="hidden")
        self.canvas.tag_lower("preview")
        mx, my, mw, mh = self.monitor
        off = info["X"] < mx or info["Y"] < my or info["X"] + info["W"] > mx + mw \
            or info["Y"] + info["H"] > my + mh
        if off:
            # Conky would put it there all the same, part of it off the
            # screen; say so rather than leave it to be found after Apply.
            self.size_label.configure(fg=WARN, text="Too big to fit on your screen. Make it smaller "
                                                    "or move it.")
        else:
            self.size_label.configure(fg=MUTED, text="%d × %d pixels, %s of your screen" % (
                info["W"], info["H"], PLACE_NAMES.get(self.values["position"], "")))

    def _pan(self, event):
        if self.view == "actual":
            self.canvas.scan_dragto(event.x, event.y, gain=1)

    def _scroll(self, direction):
        if self.view == "actual":
            self.canvas.yview_scroll(direction * 3, "units")

    # ---------------------------------------------------------------- saving

    def write(self):
        try:
            natural = natural_size(self.values)
        except (OSError, subprocess.SubprocessError) as problem:
            messagebox.showerror("Not saved", "Couldn't work out the widget's size, so nothing "
                                 "was saved.\n\n%s" % problem, parent=self)
            return False
        if not natural:
            messagebox.showerror("Not saved", "Couldn't work out the widget's size, so nothing "
                                 "was saved.", parent=self)
            return False
        lua = apply_lua(self.lua_text, self.values)
        config = apply_config(self.config_text, self.values, natural)
        try:
            for path, old, new in ((SCRIPT, self.lua_text, lua), (CONFIG, self.config_text, config)):
                if new != old:
                    shutil.copy2(path, path + ".bak")
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(new)
        except OSError as problem:
            messagebox.showerror("Not saved", "Couldn't save the settings.\n\n%s" % problem,
                                 parent=self)
            return False
        self.lua_text, self.config_text = lua, config
        self.saved = dict(self.values)
        self.refresh()
        return True

    def save(self):
        # Stop the widget before writing, so conky never sees its config
        # change underneath it; then start it again on the new settings.
        running = mirage_pids()
        for pid in running:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        saved = self.write()
        try:
            subprocess.Popen(["conky", "--pause=1", "--config=" + CONFIG], cwd=HERE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except FileNotFoundError:
            messagebox.showerror("Not started", "Your settings are saved, but conky couldn't be "
                                 "found to start the widget." if saved else
                                 "Conky couldn't be found to start the widget.", parent=self)
            return
        if saved:
            self.status.configure(text="Saved. The widget is restarting." if running
                                  else "Saved. The widget is starting.", fg=ACCENT)

    def destroy(self):
        shutil.rmtree(getattr(self, "workdir", ""), ignore_errors=True)
        super().destroy()


def main():
    for path in (SCRIPT, CONFIG, RENDERER):
        if not os.path.exists(path):
            sys.exit("%s is missing. It should be in %s." % (os.path.basename(path), HERE))
    if shutil.which("lua") is None:
        sys.exit("The preview needs Lua. Install the \"lua\" package and try again.")
    try:
        import PIL  # noqa: F401
    except ImportError:
        sys.exit("The preview needs Pillow. Install python3-pil (Debian, Ubuntu) or "
                 "python-pillow (Arch, Fedora) and try again.")
    Editor().mainloop()


if __name__ == "__main__":
    main()
