#!/usr/bin/env python3
"""Colour editor for Conky Orrery, with a live preview.

    ./orrery_colors.py [path/to/lua_orrery.lua]

Pick colours on the left, watch the widget redraw on the right, and only write
to lua_orrery.lua when you press Apply.

The preview is the real thing. Every render writes the candidate settings into a
throwaway copy of the script and draws a frame of *that* through
orrery_preview.lua, so what is on screen is the file Apply is about to save --
there is no second implementation of the widget here that could drift away from
the first.

Only the standard library is used. tkinter ships with Python and Tk 8.6 reads
PNG on its own, so there is nothing to install.

Almost every control is drawn on a Canvas rather than taken from ttk. The stock
themes cannot be pushed far from their 1990s Motif ancestry -- their scales in
particular still draw a hatched grip -- and this sits next to a preview whose
whole job is to be looked at. Canvas drawing is a few hundred lines but it is
plain geometry, and it means the editor can wear the same palette as the thing
it edits.
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
DEFAULT_SCRIPT = os.path.join(HERE, "lua_orrery.lua")
RENDERER = os.path.join(HERE, "orrery_preview.lua")

PREVIEW_SIZE = 600
# Long enough that dragging a slider does not queue a render per pixel, short
# enough to feel immediate. A render takes about a tenth of a second.
DEBOUNCE_MS = 130

BG = "#0D1117"
SURFACE = "#151B23"
SUNKEN = "#0A0F16"
BORDER = "#232C38"
BORDER_LIT = "#39465A"
TEXT = "#E6EDF3"
MUTED = "#8B97A6"
FAINT = "#5A6675"
ACCENT = "#3DDCFF"
ACCENT_DIM = "#174E5E"

FONT = "DejaVu Sans"
F_TITLE = (FONT, 10, "bold")
F_BODY = (FONT, 10)
F_SMALL = (FONT, 9)
F_MONO = ("DejaVu Sans Mono", 10)

# key, label, and what the colour actually controls -- worth spelling out,
# because in this widget colour encodes the kind of a thing rather than its
# position, and that is not guessable from the name.
COLOURS = [
    ("HTML_base", "Rings &\nclock"),
    ("HTML_accent", "Today,\nCPU, RAM"),
    ("HTML_second", "Cage &\ndisks"),
    ("HTML_warm", "Heat"),
]

NUMBERS = [
    ("opacity_track", "Unlit rings", 0.0, 1.0),
    ("opacity_label", "Month and day labels", 0.0, 1.0),
    ("opacity_text", "Clock and readout values", 0.0, 1.0),
    ("opacity_live", "Today and the cores", 0.0, 1.0),
    ("depth_fade", "Far side fade", 0.05, 1.0),
]

# base, accent, second, warm
PRESETS = [
    ("Ice", ("#DCE6F5", "#3DDCFF", "#B07BFF", "#FF5F8D")),
    ("Ember", ("#F5E8DC", "#FFB300", "#FF6B3D", "#FF3D5A")),
    ("Nord", ("#ECEFF4", "#88C0D0", "#B48EAD", "#BF616A")),
    ("Gruvbox", ("#EBDBB2", "#83A598", "#D3869B", "#FB4934")),
    ("Matrix", ("#D8F5DC", "#4DFF91", "#2FBF71", "#FFD166")),
    ("Dracula", ("#F8F8F2", "#8BE9FD", "#BD93F9", "#FF5555")),
    ("Rosewater", ("#F2E9E1", "#F5C2E7", "#CBA6F7", "#F38BA8")),
    ("Mono", ("#E8E8E8", "#FFFFFF", "#9A9A9A", "#C9C9C9")),
]

# Conky draws on a transparent window, so how the widget reads depends entirely
# on the wallpaper behind it. These let that be checked before committing.
BACKDROPS = [("Dark", "0B0E17"), ("Slate", "2B303B"), ("Grey", "6E7480"), ("Light", "D8DCE4")]

COLOUR_RE = {k: re.compile(r'^(\s*%s\s*=\s*")(#?[0-9A-Fa-f]{6})(")' % k, re.M)
             for k, _ in COLOURS}
NUMBER_RE = {k: re.compile(r'^(\s*%s\s*=\s*)(-?[0-9]*\.?[0-9]+)' % k, re.M)
             for k, _, _, _ in NUMBERS}


# --------------------------------------------------------------------- model

def read_settings(text):
    """Pull the current values out of the script, so the editor starts where
    the user's file actually is rather than at some assumed default."""
    values = {}
    for key, pattern in COLOUR_RE.items():
        m = pattern.search(text)
        if m:
            hexval = m.group(2)
            values[key] = hexval if hexval.startswith("#") else "#" + hexval
    for key, pattern in NUMBER_RE.items():
        m = pattern.search(text)
        if m:
            values[key] = float(m.group(2))
    return values


def apply_settings(text, values):
    """Rewrite only the values, leaving every comment, blank line and column of
    alignment in the file exactly as it was.

    A setting whose value has not actually changed is left completely alone,
    down to the case of its hex digits. Normalising as we pass would mean
    pressing Apply always produced a diff, and the point of editing in place is
    that the file stays recognisably the user's."""

    def replace_colour(m, wanted):
        if m.group(2).lstrip("#").upper() == wanted.lstrip("#").upper():
            return m.group(0)
        return m.group(1) + wanted.upper() + m.group(3)

    def replace_number(m, wanted):
        try:
            if abs(float(m.group(2)) - wanted) < 1e-9:
                return m.group(0)
        except ValueError:
            pass
        # Two decimals is how the file is already written, and the same width
        # for every value in range, so the comment after it keeps its column.
        return m.group(1) + ("%.2f" % wanted)

    for key, pattern in COLOUR_RE.items():
        if key in values:
            text = pattern.sub(lambda m, c=values[key]: replace_colour(m, c), text, count=1)
    for key, pattern in NUMBER_RE.items():
        if key in values:
            text = pattern.sub(lambda m, n=values[key]: replace_number(m, n), text, count=1)
    return text


def orrery_pids():
    """Conky processes running the orrery, found by reading /proc rather than
    by pattern-matching `ps` output -- a pattern wide enough to catch the conky
    process also catches the shell that was asked to look for it."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % entry, "rb") as handle:
                parts = handle.read().split(b"\0")
        except OSError:
            continue
        if not parts or not parts[0]:
            continue
        if os.path.basename(parts[0].decode("utf-8", "replace")) != "conky":
            continue
        if any(b"start_conky_orrery" in part for part in parts):
            found.append(int(entry))
    return found


# ------------------------------------------------------------------ drawing

def rounded(canvas, x1, y1, x2, y2, r, **kw):
    """A rounded rectangle. Tk's canvas has no such primitive, but a polygon
    through the corner points with smooth=True bends them into quadratic
    splines, which at these radii is indistinguishable from real arcs."""
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kw)


def mix(a, b, t):
    """Blend two #rrggbb colours."""
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(x + (y - x) * t) for x, y in zip(pa, pb))


def ink_on(hex_colour):
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return "#0D1117" if (r * 299 + g * 587 + b * 114) / 1000 > 140 else TEXT


class Button(tk.Canvas):
    """A flat rounded button with hover and press states."""

    def __init__(self, parent, text, command, primary=False, width=None):
        self.text = text
        self.command = command
        self.primary = primary
        w = width or (len(text) * 7 + 34)
        super().__init__(parent, width=w, height=34, bg=BG, highlightthickness=0,
                         cursor="hand2")
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
            fill = {"normal": ACCENT_DIM, "hover": mix(ACCENT_DIM, ACCENT, 0.35),
                    "press": mix(ACCENT_DIM, ACCENT, 0.5)}[self.state]
            edge, ink = ACCENT, ACCENT
        else:
            fill = {"normal": SURFACE, "hover": "#1E2733", "press": "#242F3D"}[self.state]
            edge, ink = (BORDER_LIT if self.state != "normal" else BORDER), TEXT
        rounded(self, 1, 1, w - 1, 33, 8, fill=fill, outline=edge)
        self.create_text(w / 2, 17, text=self.text, fill=ink, font=F_BODY)


class Slider(tk.Canvas):
    """Track, filled portion and a round handle. Click anywhere to jump,
    drag to scrub, and the handle grows slightly while it is held."""

    H = 26

    def __init__(self, parent, low, high, value, command, width=250):
        super().__init__(parent, width=width, height=self.H, bg=BG,
                         highlightthickness=0, cursor="hand2")
        self.low, self.high = low, high
        self.value = value
        self.command = command
        self.w = width
        self.active = False
        self.hover = False
        self.bind("<Button-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self._draw()

    def _hover(self, on):
        self.hover = on
        self._draw()

    def _pos(self):
        span = self.high - self.low or 1
        margin = 10
        return margin + (self.value - self.low) / span * (self.w - 2 * margin)

    def _from_x(self, x):
        margin = 10
        t = (x - margin) / max(self.w - 2 * margin, 1)
        return self.low + max(0.0, min(1.0, t)) * (self.high - self.low)

    def _press(self, event):
        self.active = True
        self.set(self._from_x(event.x), notify=True)

    def _drag(self, event):
        if self.active:
            self.set(self._from_x(event.x), notify=True)

    def _release(self, _event):
        self.active = False
        self._draw()

    def set(self, value, notify=False):
        value = max(self.low, min(self.high, value))
        if abs(value - self.value) > 1e-9:
            self.value = value
            if notify:
                self.command(value)
        self._draw()

    def _draw(self):
        self.delete("all")
        mid = self.H / 2
        rounded(self, 10, mid - 2, self.w - 10, mid + 2, 2, fill=SUNKEN, outline=BORDER)
        x = self._pos()
        if x > 12:
            rounded(self, 10, mid - 2, x, mid + 2, 2, fill=ACCENT, outline=ACCENT)
        r = 8 if (self.active or self.hover) else 6.5
        ring = ACCENT if (self.active or self.hover) else BORDER_LIT
        self.create_oval(x - r, mid - r, x + r, mid + r, fill=SURFACE, outline=ring, width=2)


class Segmented(tk.Canvas):
    """A pill split into segments -- a radio group that does not look like one."""

    H = 30

    def __init__(self, parent, options, value, command, width=250):
        super().__init__(parent, width=width, height=self.H, bg=BG,
                         highlightthickness=0, cursor="hand2")
        self.options = options
        self.value = value
        self.command = command
        self.w = width
        self.hover_index = -1
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._set_hover(-1))
        self._draw()

    def _index_at(self, x):
        step = self.w / len(self.options)
        return max(0, min(len(self.options) - 1, int(x // step)))

    def _motion(self, event):
        self._set_hover(self._index_at(event.x))

    def _set_hover(self, index):
        if index != self.hover_index:
            self.hover_index = index
            self._draw()

    def _click(self, event):
        label, value = self.options[self._index_at(event.x)]
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
                rounded(self, x1 + 1, 3, x1 + step - 1, self.H - 3, 7,
                        fill=SURFACE, outline=ACCENT)
            ink = ACCENT if chosen else (TEXT if i == self.hover_index else MUTED)
            self.create_text(x1 + step / 2, self.H / 2, text=label, fill=ink, font=F_SMALL)


class Swatch(tk.Canvas):
    """One of the four colours, as a selectable chip."""

    W, H = 106, 70

    def __init__(self, parent, label, command):
        super().__init__(parent, width=self.W, height=self.H, bg=BG,
                         highlightthickness=0, cursor="hand2")
        self.label = label
        self.command = command
        self.colour = "#000000"
        self.selected = False
        self.hover = False
        self.bind("<Button-1>", lambda e: self.command())
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))

    def _hover(self, on):
        self.hover = on
        self._draw()

    def configure_colour(self, colour, selected):
        self.colour = colour
        self.selected = selected
        self._draw()

    def _draw(self):
        self.delete("all")
        edge = ACCENT if self.selected else (BORDER_LIT if self.hover else BORDER)
        rounded(self, 1, 1, self.W - 1, self.H - 1, 9,
                fill=SURFACE, outline=edge, width=2 if self.selected else 1)
        rounded(self, 8, 8, self.W - 8, 32, 6, fill=self.colour, outline=BORDER)
        ink = TEXT if self.selected else MUTED
        for i, line in enumerate(self.label.split("\n")):
            self.create_text(self.W / 2, 44 + i * 13, text=line, fill=ink, font=F_SMALL)


class PresetChip(tk.Canvas):
    """A preset shown as the four colours it would set, plus its name."""

    W, H = 142, 34

    def __init__(self, parent, name, colours, command):
        super().__init__(parent, width=self.W, height=self.H, bg=BG,
                         highlightthickness=0, cursor="hand2")
        self.name = name
        self.colours = colours
        self.hover = False
        self.bind("<Button-1>", lambda e: command())
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))
        self._draw()

    def _hover(self, on):
        self.hover = on
        self._draw()

    def _draw(self):
        self.delete("all")
        rounded(self, 1, 1, self.W - 1, self.H - 1, 8, fill=SURFACE,
                outline=BORDER_LIT if self.hover else BORDER)
        for i, colour in enumerate(self.colours):
            x = 11 + i * 13
            self.create_oval(x - 5, self.H / 2 - 5, x + 5, self.H / 2 + 5,
                             fill=colour, outline=BORDER)
        self.create_text(self.W - 10, self.H / 2, text=self.name, anchor="e",
                         fill=TEXT if self.hover else MUTED, font=F_SMALL)


class ColourPicker(tk.Frame):
    """A saturation/value square over a hue strip.

    The square is a PhotoImage built a pixel at a time in Python, which sounds
    ruinous and is not: 190x190 costs about twenty milliseconds, and it is only
    rebuilt when the hue moves."""

    SQ = 186
    STRIP = 14

    def __init__(self, parent, command):
        super().__init__(parent, bg=BG)
        self.command = command
        self.h, self.s, self.v = 0.5, 0.8, 1.0

        self.square = tk.Canvas(self, width=self.SQ, height=self.SQ, bg=BG,
                                highlightthickness=1, highlightbackground=BORDER,
                                cursor="crosshair")
        self.square.grid(row=0, column=0, sticky="w")
        self.square.bind("<Button-1>", self._square_at)
        self.square.bind("<B1-Motion>", self._square_at)

        self.strip = tk.Canvas(self, width=self.SQ, height=self.STRIP, bg=BG,
                               highlightthickness=1, highlightbackground=BORDER,
                               cursor="sb_h_double_arrow")
        self.strip.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.strip.bind("<Button-1>", self._strip_at)
        self.strip.bind("<B1-Motion>", self._strip_at)

        self._sv_image = tk.PhotoImage(width=self.SQ, height=self.SQ)
        self.square.create_image(0, 0, image=self._sv_image, anchor="nw")
        self._draw_strip()
        self._rebuild_square()

    # hue strip is drawn as one vertical line per pixel column; cheap enough
    # that it does not need an image
    def _draw_strip(self):
        for x in range(self.SQ):
            r, g, b = colorsys.hsv_to_rgb(x / (self.SQ - 1), 1.0, 1.0)
            self.strip.create_line(x, 0, x, self.STRIP,
                                   fill="#%02x%02x%02x" % (int(r * 255), int(g * 255),
                                                           int(b * 255)))
        self.strip.create_line(0, 0, 0, self.STRIP, fill="#FFFFFF", width=2, tags="cursor")

    def _rebuild_square(self):
        size = self.SQ
        rows = []
        for y in range(size):
            value = 1.0 - y / (size - 1)
            row = []
            for x in range(size):
                r, g, b = colorsys.hsv_to_rgb(self.h, x / (size - 1), value)
                row.append("#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255)))
            rows.append("{" + " ".join(row) + "}")
        self._sv_image.put(" ".join(rows))
        self._draw_cursors()

    def _draw_cursors(self):
        self.square.delete("cursor")
        x = self.s * (self.SQ - 1)
        y = (1.0 - self.v) * (self.SQ - 1)
        ink = "#000000" if self.v > 0.6 and self.s < 0.6 else "#FFFFFF"
        self.square.create_oval(x - 7, y - 7, x + 7, y + 7, outline=ink, width=2, tags="cursor")
        self.strip.delete("cursor")
        hx = self.h * (self.SQ - 1)
        self.strip.create_rectangle(hx - 2, 0, hx + 2, self.STRIP,
                                    outline="#FFFFFF", width=2, tags="cursor")

    def _square_at(self, event):
        self.s = max(0.0, min(1.0, event.x / (self.SQ - 1)))
        self.v = max(0.0, min(1.0, 1.0 - event.y / (self.SQ - 1)))
        self._draw_cursors()
        self.command(self.hex())

    def _strip_at(self, event):
        self.h = max(0.0, min(1.0, event.x / (self.SQ - 1)))
        self._rebuild_square()
        self.command(self.hex())

    def hex(self):
        r, g, b = colorsys.hsv_to_rgb(self.h, self.s, self.v)
        return "#%02X%02X%02X" % (int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5))

    def set_hex(self, value):
        r, g, b = (int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))
        h, s, v = colorsys.rgb_to_hsv(r, g, b)
        # A grey has no meaningful hue; keep the strip where the user left it
        # rather than snapping it to red every time they pick white.
        if s > 0.004:
            self.h = h
        self.s, self.v = s, v
        self._rebuild_square()


# ----------------------------------------------------------------- the editor

class Editor(tk.Tk):
    def __init__(self, script_path):
        super().__init__()
        self.script_path = script_path
        self.title("Conky Orrery colours")
        self.configure(bg=BG)
        self.resizable(False, False)

        with open(script_path, encoding="utf-8") as handle:
            self.original_text = handle.read()

        self.saved = read_settings(self.original_text)
        if not self.saved:
            messagebox.showerror(
                "Nothing to edit",
                "No colour settings found in:\n%s\n\nIs that lua_orrery.lua?" % script_path)
            self.destroy()
            raise SystemExit(1)

        self.values = dict(self.saved)
        self.selected = COLOURS[0][0]
        self.backdrop = BACKDROPS[0][1]
        self.phase = 0.0

        self.workdir = tempfile.mkdtemp(prefix="orrery-colours-")
        self.results = queue.Queue()
        self.render_seq = 0
        self.pending = None
        self.preview_image = None

        self._build()
        self.after(60, self._drain)
        self.select(self.selected)
        self.request_render()

    # -------------------------------------------------------------- building

    def _section(self, parent, title, row):
        head = tk.Frame(parent, bg=BG)
        head.grid(row=row, column=0, sticky="ew", pady=(14, 6))
        tk.Label(head, text=title.upper(), bg=BG, fg=FAINT, font=(FONT, 8, "bold")).pack(
            side="left")
        line = tk.Frame(head, bg=BORDER, height=1)
        line.pack(side="left", fill="x", expand=True, padx=(10, 0), pady=(1, 0))
        return row + 1

    def _card(self, parent, row, pad=12):
        card = tk.Frame(parent, bg=SURFACE, highlightthickness=1,
                        highlightbackground=BORDER, padx=pad, pady=pad)
        card.grid(row=row, column=0, sticky="ew")
        return card

    def _build(self):
        root = tk.Frame(self, bg=BG, padx=16, pady=14)
        root.pack(fill="both", expand=True)

        left = tk.Frame(root, bg=BG)
        left.grid(row=0, column=0, sticky="n", padx=(0, 16))
        right = tk.Frame(root, bg=BG)
        right.grid(row=0, column=1, sticky="n")

        row = 0
        row = self._section(left, "Colours", row)

        chips = tk.Frame(left, bg=BG)
        chips.grid(row=row, column=0, sticky="w")
        self.swatches = {}
        for i, (key, label) in enumerate(COLOURS):
            chip = Swatch(chips, label, command=lambda k=key: self.select(k))
            chip.grid(row=0, column=i, padx=(0 if i == 0 else 8, 0))
            self.swatches[key] = chip
        row += 1

        picker_card = self._card(left, row)
        row += 1
        self.picker = ColourPicker(picker_card, command=self.picked)
        self.picker.configure(bg=SURFACE)
        self.picker.square.configure(bg=SURFACE)
        self.picker.strip.configure(bg=SURFACE)
        self.picker.grid(row=0, column=0, rowspan=2, sticky="nw")

        side = tk.Frame(picker_card, bg=SURFACE)
        side.grid(row=0, column=1, sticky="nw", padx=(14, 0))
        tk.Label(side, text="HEX", bg=SURFACE, fg=FAINT, font=(FONT, 8, "bold")).pack(anchor="w")
        self.hex_entry = tk.Entry(side, width=9, bg=SUNKEN, fg=TEXT, font=F_MONO,
                                  relief="flat", insertbackground=ACCENT,
                                  highlightthickness=1, highlightbackground=BORDER,
                                  highlightcolor=ACCENT)
        self.hex_entry.pack(anchor="w", pady=(4, 0), ipady=4, ipadx=4)
        self.hex_entry.bind("<Return>", lambda e: self.commit_hex())
        self.hex_entry.bind("<FocusOut>", lambda e: self.commit_hex())

        tk.Label(side, text="SAVED", bg=SURFACE, fg=FAINT,
                 font=(FONT, 8, "bold")).pack(anchor="w", pady=(16, 0))
        self.saved_swatch = tk.Canvas(side, width=104, height=30, bg=SURFACE,
                                      highlightthickness=0, cursor="hand2")
        self.saved_swatch.pack(anchor="w", pady=(4, 0))
        self.saved_swatch.bind("<Button-1>", lambda e: self.restore_one())

        row = self._section(left, "Presets", row)
        presets = tk.Frame(left, bg=BG)
        presets.grid(row=row, column=0, sticky="w")
        self.preset_chips = []
        for i, (name, colours) in enumerate(PRESETS):
            chip = PresetChip(presets, name, colours,
                              command=lambda c=colours: self.use_preset(c))
            self.preset_chips.append(chip)
            chip.grid(
                row=i // 3, column=i % 3, padx=(0 if i % 3 == 0 else 7, 0), pady=(0, 7))
        row += 1

        row = self._section(left, "Opacity and depth", row)
        sliders = self._card(left, row, pad=14)
        row += 1
        self.sliders = {}
        self.slider_values = {}
        for i, (key, label, low, high) in enumerate(NUMBERS):
            head = tk.Frame(sliders, bg=SURFACE)
            head.grid(row=i * 2, column=0, sticky="ew", pady=(0 if i == 0 else 8, 0))
            tk.Label(head, text=label, bg=SURFACE, fg=TEXT, font=F_BODY).pack(side="left")
            value = tk.Label(head, text="", bg=SURFACE, fg=ACCENT, font=F_MONO)
            value.pack(side="right")
            self.slider_values[key] = value

            slider = Slider(sliders, low, high, self.values.get(key, low),
                            command=lambda v, k=key: self.set_number(k, v), width=404)
            slider.configure(bg=SURFACE)
            slider.grid(row=i * 2 + 1, column=0, sticky="ew")
            self.sliders[key] = slider

        # ---- preview side
        frame = tk.Frame(right, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER,
                         padx=10, pady=10)
        frame.grid(row=0, column=0)
        self.canvas = tk.Canvas(frame, width=PREVIEW_SIZE, height=PREVIEW_SIZE,
                                bg=SUNKEN, highlightthickness=0)
        self.canvas.pack()
        self.canvas_text = self.canvas.create_text(
            PREVIEW_SIZE // 2, PREVIEW_SIZE // 2, text="", fill=MUTED,
            width=PREVIEW_SIZE - 80, justify="center", font=F_BODY)

        caption = tk.Frame(right, bg=BG)
        caption.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        tk.Label(caption, text=os.path.basename(self.script_path), bg=BG, fg=MUTED,
                 font=F_MONO).pack(side="left")
        tk.Label(caption, text="preview", bg=BG, fg=FAINT, font=F_SMALL).pack(side="right")

        view = tk.Frame(right, bg=SURFACE, highlightthickness=1,
                        highlightbackground=BORDER, padx=14, pady=12)
        view.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        view.columnconfigure(0, weight=1)
        tk.Label(view, text="Backdrop", bg=SURFACE, fg=TEXT, font=F_BODY).grid(
            row=1, column=0, sticky="w")
        seg = self.segmented = Segmented(view, [(n, h) for n, h in BACKDROPS], self.backdrop,
                                         command=self.set_backdrop, width=592)
        seg.configure(bg=SURFACE)
        seg.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 12))

        tk.Label(view, text="Viewing angle", bg=SURFACE, fg=TEXT, font=F_BODY).grid(
            row=3, column=0, sticky="w")
        angle = self.angle_slider = Slider(view, 0, 150, 0, command=self.set_phase, width=592)
        angle.configure(bg=SURFACE)
        angle.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        bar = tk.Frame(right, bg=BG)
        bar.grid(row=3, column=0, sticky="ew", pady=(16, 0))
        self.status = tk.Label(bar, text="", bg=BG, fg=ACCENT, font=F_SMALL)
        self.status.pack(side="left")
        Button(bar, "Apply and restart conky", self.apply_and_restart).pack(side="right")
        Button(bar, "Apply", self.apply, primary=True, width=92).pack(side="right", padx=8)
        self.revert_button = Button(bar, "Revert", self.revert, width=86)
        self.revert_button.pack(side="right")

        self.refresh()

    # ------------------------------------------------------------- reactions

    def refresh(self):
        for key, _ in COLOURS:
            self.swatches[key].configure_colour(self.values[key], key == self.selected)
        for key, _, _, _ in NUMBERS:
            self.slider_values[key].configure(text="%.2f" % self.values.get(key, 0))
        colour = self.values[self.selected]
        was = self.saved.get(self.selected, colour)
        self.saved_swatch.delete("all")
        rounded(self.saved_swatch, 1, 1, 103, 29, 7, fill=was, outline=BORDER)
        if was.upper() != colour.upper():
            self.saved_swatch.create_text(52, 15, text=was.upper(), fill=ink_on(was),
                                          font=F_SMALL)
        else:
            self.saved_swatch.create_text(52, 15, text="unchanged", fill=ink_on(was),
                                          font=F_SMALL)
        if self.hex_entry.get().strip().upper() != colour.upper():
            self.hex_entry.delete(0, "end")
            self.hex_entry.insert(0, colour.upper())

    def select(self, key):
        self.selected = key
        self.picker.set_hex(self.values[key])
        self.refresh()

    def restore_one(self):
        """Put just the selected colour back to what is on disk, which is finer
        grained than Revert and is the thing you want after one bad guess."""
        was = self.saved.get(self.selected)
        if was and was.upper() != self.values[self.selected].upper():
            self.values[self.selected] = was
            self.picker.set_hex(was)
            self.refresh()
            self.request_render()

    def picked(self, hex_colour):
        self.values[self.selected] = hex_colour
        self.refresh()
        self.request_render()

    def commit_hex(self):
        raw = self.hex_entry.get().strip()
        if not raw.startswith("#"):
            raw = "#" + raw
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", raw):
            if raw.upper() != self.values[self.selected].upper():
                self.values[self.selected] = raw.upper()
                self.picker.set_hex(raw.upper())
                self.refresh()
                self.request_render()
        else:
            self.refresh()  # put the old value back

    def set_number(self, key, value):
        self.values[key] = value
        self.slider_values[key].configure(text="%.2f" % value)
        self.request_render()

    def set_backdrop(self, value):
        self.backdrop = value
        self.request_render()

    def set_phase(self, value):
        self.phase = value
        self.request_render()

    def use_preset(self, colours):
        for (key, _), colour in zip(COLOURS, colours):
            self.values[key] = colour
        self.picker.set_hex(self.values[self.selected])
        self.refresh()
        self.request_render()

    def revert(self):
        self.values = dict(self.saved)
        for key, _, _, _ in NUMBERS:
            self.sliders[key].set(self.values.get(key, 0))
        self.picker.set_hex(self.values[self.selected])
        self.refresh()
        self.status.configure(text="reverted")
        self.request_render()

    # ---------------------------------------------------------------- render

    def candidate_text(self):
        return apply_settings(self.original_text, self.values)

    def request_render(self):
        if self.pending is not None:
            self.after_cancel(self.pending)
        self.pending = self.after(DEBOUNCE_MS, self._start_render)

    def _start_render(self):
        self.pending = None
        self.render_seq += 1
        threading.Thread(target=self._render,
                         args=(self.render_seq, self.candidate_text(),
                               self.backdrop, self.phase), daemon=True).start()

    def _render(self, seq, text, backdrop, phase):
        # Alternating filenames, because Tk keeps the PNG mapped while it is
        # displayed and overwriting the one on screen can be read half-written.
        script = os.path.join(self.workdir, "candidate.lua")
        png = os.path.join(self.workdir, "preview-%d.png" % (seq % 2))
        try:
            with open(script, "w", encoding="utf-8") as handle:
                handle.write(text)
            done = subprocess.run(
                ["lua", RENDERER, script, png, str(PREVIEW_SIZE), "%.2f" % phase, backdrop],
                capture_output=True, text=True, timeout=30)
            if done.returncode != 0 or not os.path.exists(png):
                self.results.put((seq, None, done.stderr.strip() or "the renderer failed"))
            else:
                self.results.put((seq, png, None))
        except FileNotFoundError:
            self.results.put((seq, None,
                              "lua was not found on PATH.\nInstall it to see the preview."))
        except subprocess.TimeoutExpired:
            self.results.put((seq, None, "the renderer timed out"))
        except Exception as problem:                      # pragma: no cover
            self.results.put((seq, None, str(problem)))

    def _drain(self):
        newest = None
        while True:
            try:
                newest = self.results.get_nowait()
            except queue.Empty:
                break
        if newest is not None:
            seq, png, problem = newest
            if seq == self.render_seq:                    # ignore overtaken renders
                self._show(png, problem)
        self.after(60, self._drain)

    def _show(self, png, problem):
        if problem:
            self.canvas.delete("preview")
            self.canvas.itemconfigure(self.canvas_text, text=problem, state="normal")
            return
        try:
            image = tk.PhotoImage(file=png)
        except tk.TclError as failure:
            self.canvas.itemconfigure(self.canvas_text, text=str(failure), state="normal")
            return
        self.preview_image = image                        # Tk will not hold this for us
        self.canvas.delete("preview")
        self.canvas.create_image(0, 0, image=image, anchor="nw", tags="preview")
        self.canvas.itemconfigure(self.canvas_text, state="hidden")
        self.canvas.tag_lower("preview")

    # ---------------------------------------------------------------- saving

    def write(self):
        try:
            shutil.copy2(self.script_path, self.script_path + ".bak")
            with open(self.script_path, "w", encoding="utf-8") as handle:
                handle.write(self.candidate_text())
        except OSError as problem:
            messagebox.showerror("Could not save", str(problem), parent=self)
            return False
        self.original_text = self.candidate_text()
        self.saved = dict(self.values)
        return True

    def apply(self):
        if not self.write():
            return
        if orrery_pids():
            self.status.configure(text="saved - restart conky")
        else:
            self.status.configure(text="saved")

    def apply_and_restart(self):
        if not self.write():
            return
        running = orrery_pids()
        for pid in running:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        try:
            subprocess.Popen(
                ["conky", "--pause=1",
                 "--config=" + os.path.join(HERE, "start_conky_orrery")],
                cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
        except FileNotFoundError:
            messagebox.showerror("conky not found", "Saved, but conky is not on PATH.",
                                 parent=self)
            return
        self.status.configure(text="saved - conky restarted")


def main():
    script = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SCRIPT
    if not os.path.exists(script):
        sys.exit("no such file: %s" % script)
    if not os.path.exists(RENDERER):
        sys.exit("orrery_preview.lua is missing from %s" % HERE)
    Editor(os.path.abspath(script)).mainloop()


if __name__ == "__main__":
    main()
