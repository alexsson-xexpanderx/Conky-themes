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

# Every element the widget colours, in the groups the editor lists them under,
# as (key, name, role, what it is). As shipped, colour says what kind of thing
# something is rather than where it sits -- the dials, what is happening now,
# what is stored or standing, the cores, heat -- and `role` is which of those an
# element belongs to, so that a preset can colour all of them from a palette of
# five. Each one can still be changed on its own.
ELEMENTS = [
    ("Clock & calendar", [
        ("HTML_clock", "Clock", "base", "The time in the middle."),
        ("HTML_rings", "Rings", "base", "The month, day and weekday hoops, and their ticks."),
        ("HTML_labels", "Labels", "base", "Month names, day numbers and weekdays."),
        ("HTML_today", "Today", "accent", "Today's month, day and weekday, and their glow."),
        ("HTML_seconds", "Seconds", "second", "The ring round the cage; the minute so far is lit."),
        ("HTML_dust", "Dust", "base", "The drifting specks that give the scene depth."),
    ]),
    ("The machine", [
        ("HTML_cores", "Cores", "cool", "The orbiting CPU cores, while they run cool."),
        ("HTML_heat", "Heat", "warm", "What cores, cage, glow and GPU turn towards when hot."),
        ("HTML_cage", "Cage", "second", "The cage around the clock."),
        ("HTML_glow", "Glow", "second", "The glow behind the clock."),
    ]),
    ("Readouts", [
        ("HTML_tracks", "Tracks", "base", "The unlit part of each readout arc."),
        ("HTML_captions", "Captions", "base", "CPU, MEM and the rest, above each value."),
        ("HTML_cpu", "CPU", "accent", "CPU load: its arc and its value."),
        ("HTML_memory", "Memory", "accent", "Memory in use: its arc and its value."),
        ("HTML_gpu", "GPU", "cool", "GPU temperature, while it runs cool."),
        ("HTML_root", "Root", "second", "How full / is: its arc and its value."),
        ("HTML_home", "Home", "second", "How full /home is: its arc and its value."),
    ]),
]
ELEMENT = {key: (name, role, about)
           for _, items in ELEMENTS for key, name, role, about in items}

# Yes/No settings, as (key, label, the switch it depends on). One whose parent
# is off is kept as it is but has nothing to show, so it is drawn dimmed.
SWITCHES = [
    ("show_seconds", "Seconds", None),
    ("show_dust", "Dust", None),
    ("show_monitoring", "The machine", None),
    ("show_cage", "Cage", "show_monitoring"),
    ("show_glow", "Glow", "show_monitoring"),
    ("show_readouts", "Readouts", "show_monitoring"),
    ("enable_graphic_card_temperature_sensor", "GPU readout", "show_readouts"),
]
SWITCH_PARENT = {key: parent for key, _, parent in SWITCHES}

# The switch an element is drawn under; anything not listed is always drawn.
SHOWN_BY = {
    "HTML_seconds": "show_seconds", "HTML_dust": "show_dust",
    "HTML_cores": "show_monitoring", "HTML_heat": "show_monitoring",
    "HTML_cage": "show_cage", "HTML_glow": "show_glow",
    "HTML_tracks": "show_readouts", "HTML_captions": "show_readouts",
    "HTML_cpu": "show_readouts", "HTML_memory": "show_readouts",
    "HTML_root": "show_readouts", "HTML_home": "show_readouts",
    "HTML_gpu": "enable_graphic_card_temperature_sensor",
}

NUMBERS = [
    ("depth_fade", "Far side fade", 0.05, 1.0),
]

# Every element but Heat has an opacity of its own, named after its colour and
# edited under the picker while the element is selected. Heat is a colour that
# other elements turn as they run hot, not something drawn in its own right.
OPACITY_KEY = {key: "opacity_" + key[len("HTML_"):] for key in ELEMENT if key != "HTML_heat"}

# A preset is a palette of five, in the order of ROLES, and every element takes
# the one its role names. A preset's cool colour, the cores', has to stay clear
# of its base and accent, which are the clock and today's date, and of its
# second, which is the cage the cores orbit through.
ROLES = ("base", "accent", "second", "cool", "warm")
PRESETS = [
    ("Ice", ("#DCE6F5", "#3DDCFF", "#B07BFF", "#5B7BFF", "#FF5F8D")),
    ("Ember", ("#F5E8DC", "#FFB300", "#FF6B3D", "#8AA8D8", "#FF3D5A")),
    ("Nord", ("#ECEFF4", "#88C0D0", "#B48EAD", "#A3BE8C", "#BF616A")),
    ("Gruvbox", ("#EBDBB2", "#83A598", "#D3869B", "#B8BB26", "#FB4934")),
    ("Matrix", ("#D8F5DC", "#4DFF91", "#2FBF71", "#3FC8FF", "#FFD166")),
    ("Dracula", ("#F8F8F2", "#8BE9FD", "#BD93F9", "#50FA7B", "#FF5555")),
    ("Rosewater", ("#F2E9E1", "#F5C2E7", "#CBA6F7", "#89B4FA", "#F38BA8")),
    ("Mono", ("#E8E8E8", "#FFFFFF", "#9A9A9A", "#707070", "#C9C9C9")),
]

# Conky draws on a transparent window, so how the widget reads depends entirely
# on the wallpaper behind it. These let that be checked before committing.
BACKDROPS = [("Dark", "0B0E17"), ("Slate", "2B303B"), ("Grey", "6E7480"), ("Light", "D8DCE4")]

COLOUR_RE = {k: re.compile(r'^(\s*%s\s*=\s*")(#?[0-9A-Fa-f]{6})(")' % k, re.M)
             for k in ELEMENT}
NUMBER_RE = {k: re.compile(r'^(\s*%s\s*=\s*)(-?[0-9]*\.?[0-9]+)' % k, re.M)
             for k in [n for n, _, _, _ in NUMBERS] + list(OPACITY_KEY.values())}
SWITCH_RE = {k: re.compile(r'^(\s*%s\s*=\s*")([A-Za-z]+)(")' % k, re.M)
             for k, _, _ in SWITCHES}


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
    for key, pattern in SWITCH_RE.items():
        m = pattern.search(text)
        if m:
            values[key] = m.group(2).lower() == "yes"
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

    def replace_switch(m, wanted):
        if (m.group(2).lower() == "yes") == wanted:
            return m.group(0)
        return m.group(1) + ("Yes" if wanted else "No") + m.group(3)

    for key, pattern in COLOUR_RE.items():
        if key in values:
            text = pattern.sub(lambda m, c=values[key]: replace_colour(m, c), text, count=1)
    for key, pattern in NUMBER_RE.items():
        if key in values:
            text = pattern.sub(lambda m, n=values[key]: replace_number(m, n), text, count=1)
    for key, pattern in SWITCH_RE.items():
        if key in values:
            text = pattern.sub(lambda m, w=values[key]: replace_switch(m, w), text, count=1)
    return text


def switched_on(values, key):
    """Whether a switch is on, and every switch it depends on with it. No
    switch at all means always drawn; a switch missing from the file reads as
    off, because that is what the widget makes of it."""
    while key:
        if not values.get(key, False):
            return False
        key = SWITCH_PARENT.get(key)
    return True


# How long the preview keeps an element picked out before showing everything
# again, counted from when the picked-out frame appears.
SPOTLIGHT_MS = 1500


def spotlight_text(text, values, key):
    """The script as it would be with one element picked out, so the preview
    can show which element a colour belongs to. The widget does the picking
    out itself, drawing everything else as a faint ghost, once orrery_spotlight
    is set ahead of the script; here every opacity goes to full so that a faint
    element still shows, and its switches go on so that a hidden one shows
    anyway. Dust is also drawn larger, and Heat gets a machine running hot,
    because at idle nothing is drawn in it.

    This is only ever rendered, never saved; Apply writes candidate_text()."""
    picked = dict(values)
    for number in NUMBER_RE:
        if number in picked:
            picked[number] = 1.0
    switch = SHOWN_BY.get(key)
    while switch:
        picked[switch] = True
        switch = SWITCH_PARENT.get(switch)
    text = apply_settings(text, picked)
    if key == "HTML_dust":
        # A speck is under a pixel across in the preview; at full strength it
        # still reads as noise, so the specks are drawn larger as well.
        text = re.sub(r'^(\s*dust_size\s*=\s*)[-0-9.]+', r'\g<1>2.4', text, count=1, flags=re.M)
    if key == "HTML_heat":
        text = re.sub(r'^(\s*warm_above\s*=\s*)[-0-9.]+', r'\g<1>0', text, count=1, flags=re.M)
        text = re.sub(r'^(\s*max_temperature\s*=\s*)[-0-9.]+', r'\g<1>1', text, count=1,
                      flags=re.M)
    return 'orrery_spotlight = "%s"\n' % key[len("HTML_"):] + text


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


class ElementList(tk.Canvas):
    """Every element as a row -- swatch, name and opacity -- under its group
    heading. The swatch is laid over a checkerboard at the element's opacity,
    so a faint one looks faint. The selected row is outlined, a row whose
    element is switched off is dimmed, and a dot marks an element whose colour
    or opacity is not what is saved."""

    W = 206
    CHECKS = ("#3A4352", "#1C222C")
    HEAD, ROW = 30, 27

    def __init__(self, parent, groups, command):
        self.groups = groups                    # [(title, [key, ...]), ...]
        self.command = command
        height = sum(self.HEAD + self.ROW * len(keys) for _, keys in groups)
        super().__init__(parent, width=self.W, height=height, bg=BG,
                         highlightthickness=0, cursor="hand2")
        self.rows = []
        y = 0
        for _, keys in groups:
            y += self.HEAD
            for key in keys:
                self.rows.append((y, y + self.ROW, key))
                y += self.ROW
        self.hover = None
        self.state = None
        self.bind("<Button-1>", self._click)
        self.bind("<Motion>", lambda e: self._set_hover(self._key_at(e.y)))
        self.bind("<Leave>", lambda e: self._set_hover(None))

    def _key_at(self, y):
        for top, bottom, key in self.rows:
            if top <= y < bottom:
                return key
        return None

    def _set_hover(self, key):
        if key != self.hover:
            self.hover = key
            self._draw()

    def _click(self, event):
        key = self._key_at(event.y)
        if key:
            self.command(key)

    def show(self, values, saved, selected, visible):
        self.state = (values, saved, selected, visible)
        self._draw()

    def _draw(self):
        self.delete("all")
        if self.state is None:
            return
        values, saved, selected, visible = self.state
        y = 0
        for title, keys in self.groups:
            mid = y + self.HEAD - 10
            heading = self.create_text(1, mid, text=title.upper(), anchor="w", fill=FAINT,
                                       font=(FONT, 8, "bold"))
            self.create_line(self.bbox(heading)[2] + 10, mid, self.W, mid, fill=BORDER)
            y += self.HEAD
            for key in keys:
                shown, chosen = visible(key), key == selected
                if chosen or key == self.hover:
                    rounded(self, 1, y + 1, self.W - 1, y + self.ROW - 1, 7, fill=SURFACE,
                            outline=ACCENT if chosen else BORDER_LIT)
                colour = values[key]
                if not shown:
                    colour = mix(colour, BG, 0.65)
                opacity_key = OPACITY_KEY.get(key)
                alpha = values.get(opacity_key, 1.0) if opacity_key else 1.0
                top, bottom = y + 6, y + self.ROW - 6
                for col in range(4):
                    for half in range(2):
                        check = self.CHECKS[(col + half) % 2]
                        self.create_rectangle(
                            9 + col * 7, top + half * (bottom - top) / 2,
                            16 + col * 7, top + (half + 1) * (bottom - top) / 2,
                            fill=mix(check, colour, alpha), width=0)
                self.create_rectangle(9, top, 37, bottom, outline=BORDER)
                ink = TEXT if chosen else (MUTED if shown else FAINT)
                self.create_text(47, y + self.ROW / 2, text=ELEMENT[key][0], anchor="w",
                                 fill=ink, font=F_BODY)
                if opacity_key in values:
                    self.create_text(self.W - 22, y + self.ROW / 2, anchor="e", font=F_SMALL,
                                     text="%d%%" % round(alpha * 100),
                                     fill=MUTED if shown else FAINT)
                changed = (values[key].upper() != saved.get(key, values[key]).upper()
                           or (opacity_key in values
                               and abs(values[opacity_key] - saved.get(opacity_key, 0)) > 1e-9))
                if changed:
                    self.create_oval(self.W - 15, y + self.ROW / 2 - 3, self.W - 9,
                                     y + self.ROW / 2 + 3, fill=ACCENT, outline=ACCENT)
                y += self.ROW


class Switch(tk.Canvas):
    """An on/off toggle: a pill whose knob sits right, lit, when on. Drawn
    dimmed while a switch it depends on is off -- it can still be flipped,
    there is just nothing for it to show until the other one is on."""

    W, H = 34, 20

    def __init__(self, parent, command):
        super().__init__(parent, width=self.W, height=self.H, bg=SURFACE,
                         highlightthickness=0, cursor="hand2")
        self.value, self.live, self.hover = True, True, False
        self.command = command
        self.bind("<Button-1>", lambda e: self.command(not self.value))
        self.bind("<Enter>", lambda e: self._hover(True))
        self.bind("<Leave>", lambda e: self._hover(False))

    def _hover(self, on):
        self.hover = on
        self._draw()

    def show(self, value, live):
        self.value, self.live = value, live
        self._draw()

    def _draw(self):
        self.delete("all")
        h = self.H
        if self.value and self.live:
            track, edge, knob = ACCENT_DIM, ACCENT, ACCENT
        elif self.value:
            track, edge, knob = SUNKEN, BORDER_LIT, MUTED
        else:
            # BORDER disappears against the card, and an off switch still has to
            # read as a switch.
            track, edge = SUNKEN, MUTED if self.hover else BORDER_LIT
            knob = MUTED if self.live else FAINT
        rounded(self, 1, 1, self.W - 1, h - 1, (h - 2) / 2, fill=track, outline=edge)
        x = self.W - h / 2 if self.value else h / 2
        r = h / 2 - 4
        self.create_oval(x - r, h / 2 - r, x + r, h / 2 + r, fill=knob, outline=knob)


class PresetChip(tk.Canvas):
    """A preset shown as the colours it would set, plus its name."""

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
        # Five dots and the longest name, Rosewater, only just share the chip.
        for i, colour in enumerate(self.colours):
            x = 10 + i * 10
            self.create_oval(x - 4, self.H / 2 - 4, x + 4, self.H / 2 + 4,
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
        # Only what the file actually has is offered, since anything else has
        # no line to be written back to.
        self.colour_keys = [key for key in ELEMENT if key in self.saved]
        if not self.colour_keys:
            messagebox.showerror(
                "Nothing to edit",
                "No colour settings found in:\n%s\n\nIs that lua_orrery.lua? Copies from "
                "before every element had its own colour use five shared ones instead, "
                "and need the lua_orrery.lua that came with this editor." % script_path)
            self.destroy()
            raise SystemExit(1)

        self.values = dict(self.saved)
        self.selected = self.colour_keys[0]
        self.backdrop = BACKDROPS[0][1]
        self.phase = 0.0

        self.workdir = tempfile.mkdtemp(prefix="orrery-colours-")
        self.results = queue.Queue()
        self.render_seq = 0
        self.pending = None
        self.preview_image = None
        # The element the preview is picking out, if any. Choosing an element
        # sets it; it clears itself shortly after the picked-out frame shows,
        # and at once on any change or a click on the preview. The serial
        # lets a timer that has been overtaken tell that it has.
        self.spotlight = None
        self.spotlight_serial = 0
        self.rendering_spotlight = None

        self._build()
        self.after(60, self._drain)
        self.select(self.selected, highlight=False)
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

        sidebar = tk.Frame(root, bg=BG)
        sidebar.grid(row=0, column=0, sticky="n", padx=(0, 16))
        left = tk.Frame(root, bg=BG)
        left.grid(row=0, column=1, sticky="n", padx=(0, 16))
        right = tk.Frame(root, bg=BG)
        right.grid(row=0, column=2, sticky="n")

        # ---- what there is: every element, and the switches for the parts
        groups = [(title, [key for key, *_ in items if key in self.saved])
                  for title, items in ELEMENTS]
        self.element_list = ElementList(sidebar, [(t, keys) for t, keys in groups if keys],
                                        command=self.select)
        self.element_list.grid(row=0, column=0, sticky="w", pady=(4, 0))

        self._section(sidebar, "Show", 1)
        switches = self._card(sidebar, 2)
        switches.columnconfigure(0, weight=1)
        self.switches, self.switch_labels = {}, {}
        present = [s for s in SWITCHES if s[0] in self.saved]
        for i, (key, label, parent) in enumerate(present):
            depth, above = 0, parent
            while above:
                depth, above = depth + 1, SWITCH_PARENT.get(above)
            line = tk.Frame(switches, bg=SURFACE)
            line.grid(row=i, column=0, sticky="ew", pady=(0 if i == 0 else 7, 0))
            name = tk.Label(line, text=label, bg=SURFACE, fg=TEXT, font=F_BODY)
            name.pack(side="left", padx=(depth * 14, 0))
            switch = Switch(line, command=lambda on, k=key: self.set_switch(k, on))
            switch.pack(side="right")
            self.switches[key], self.switch_labels[key] = switch, name

        # ---- the selected element's colour
        row = 0
        row = self._section(left, "Colour", row)

        picker_card = self._card(left, row)
        row += 1
        about = tk.Frame(picker_card, bg=SURFACE)
        about.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        self.element_name = tk.Label(about, text="", bg=SURFACE, fg=TEXT, font=F_TITLE)
        self.element_name.pack(anchor="w")
        # Two lines whatever it says, so the picker below does not jump when
        # the selection changes.
        self.element_about = tk.Label(about, text="", bg=SURFACE, fg=MUTED, font=F_SMALL,
                                      height=2, anchor="nw", justify="left", wraplength=400)
        self.element_about.pack(anchor="w", fill="x")

        self.picker = ColourPicker(picker_card, command=self.picked)
        self.picker.configure(bg=SURFACE)
        self.picker.square.configure(bg=SURFACE)
        self.picker.strip.configure(bg=SURFACE)
        self.picker.grid(row=1, column=0, rowspan=2, sticky="nw")

        side = tk.Frame(picker_card, bg=SURFACE)
        side.grid(row=1, column=1, sticky="nw", padx=(14, 0))
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

        # The selected element's opacity, under its colour. Heat has none, so
        # for Heat the slider gives way to a line saying why.
        opacity = tk.Frame(picker_card, bg=SURFACE)
        opacity.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        self.opacity_controls = tk.Frame(opacity, bg=SURFACE)
        head = tk.Frame(self.opacity_controls, bg=SURFACE)
        head.pack(fill="x")
        tk.Label(head, text="Opacity", bg=SURFACE, fg=TEXT, font=F_BODY).pack(side="left")
        self.opacity_value = tk.Label(head, text="", bg=SURFACE, fg=ACCENT, font=F_MONO)
        self.opacity_value.pack(side="right")
        self.opacity_slider = Slider(self.opacity_controls, 0.0, 1.0, 1.0,
                                     command=self.set_opacity, width=400)
        self.opacity_slider.configure(bg=SURFACE)
        self.opacity_slider.pack(fill="x", pady=(4, 0))
        self.opacity_note = tk.Label(opacity, text="", bg=SURFACE, fg=MUTED, font=F_SMALL,
                                     justify="left", anchor="w", wraplength=400)

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

        row = self._section(left, "Depth", row)
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
        self.canvas.bind("<Button-1>", lambda e: self.end_spotlight(render=True))
        self.canvas_text = self.canvas.create_text(
            PREVIEW_SIZE // 2, PREVIEW_SIZE // 2, text="", fill=MUTED,
            width=PREVIEW_SIZE - 80, justify="center", font=F_BODY)

        caption = tk.Frame(right, bg=BG)
        caption.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        tk.Label(caption, text=os.path.basename(self.script_path), bg=BG, fg=MUTED,
                 font=F_MONO).pack(side="left")
        self.preview_caption = tk.Label(caption, text="preview", bg=BG, fg=FAINT, font=F_SMALL)
        self.preview_caption.pack(side="right")

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

    def visible(self, key):
        return switched_on(self.values, SHOWN_BY.get(key))

    def refresh(self):
        self.element_list.show(self.values, self.saved, self.selected, self.visible)
        name, _, about = ELEMENT[self.selected]
        self.element_name.configure(text=name)
        if not self.visible(self.selected):
            if self.spotlight == self.selected:
                about += "\nSwitched off under Show; drawn while it is picked out."
            else:
                about += "\nSwitched off under Show, so the preview leaves it out."
        self.element_about.configure(text=about)
        if self.spotlight:
            self.preview_caption.configure(
                text="picking out %s" % ELEMENT[self.spotlight][0], fg=ACCENT)
        else:
            self.preview_caption.configure(text="preview", fg=FAINT)
        self.canvas.configure(cursor="hand2" if self.spotlight else "")
        for key, switch in self.switches.items():
            live = switched_on(self.values, SWITCH_PARENT.get(key))
            switch.show(self.values.get(key, False), live)
            self.switch_labels[key].configure(fg=TEXT if live else FAINT)
        for key, _, _, _ in NUMBERS:
            self.slider_values[key].configure(text="%.2f" % self.values.get(key, 0))
        opacity_key = OPACITY_KEY.get(self.selected)
        if opacity_key in self.values:
            self.opacity_note.pack_forget()
            self.opacity_controls.pack(fill="x")
            value = self.values[opacity_key]
            self.opacity_slider.set(value)
            text = "%d%%" % round(value * 100)
            was = self.saved.get(opacity_key, value)
            if abs(was - value) > 1e-9:
                text += "  (saved %d%%)" % round(was * 100)
            self.opacity_value.configure(text=text)
        else:
            self.opacity_controls.pack_forget()
            self.opacity_note.configure(
                text="Heat has no opacity of its own: it is the colour the cores, the cage, "
                     "the glow and the GPU turn as they run hot." if self.selected == "HTML_heat"
                else "This file has no opacity setting for this element.")
            self.opacity_note.pack(fill="x")
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

    def select(self, key, highlight=True):
        """Choose the element the picker edits, and pick it out in the preview
        so it is plain which part of the widget that is."""
        self.selected = key
        self.picker.set_hex(self.values[key])
        if highlight:
            self.spotlight = key
            self.spotlight_serial += 1
            self.request_render()
        self.refresh()

    def end_spotlight(self, render=False):
        """Back to the whole widget. Any change does this, so that a colour is
        judged among the others rather than on an empty stage."""
        if self.spotlight is None:
            return
        self.spotlight = None
        self.spotlight_serial += 1
        self.refresh()
        if render:
            self.request_render()

    def _spotlight_shown(self):
        """The picked-out frame is on screen: give it a moment, then show
        everything again -- unless something has moved on in the meantime."""
        serial = self.spotlight_serial

        def expire():
            if serial == self.spotlight_serial:
                self.end_spotlight(render=True)

        self.after(SPOTLIGHT_MS, expire)

    def restore_one(self):
        """Put just the selected colour back to what is on disk, which is finer
        grained than Revert and is the thing you want after one bad guess."""
        was = self.saved.get(self.selected)
        if was and was.upper() != self.values[self.selected].upper():
            self.end_spotlight()
            self.values[self.selected] = was
            self.picker.set_hex(was)
            self.refresh()
            self.request_render()

    def picked(self, hex_colour):
        self.end_spotlight()
        self.values[self.selected] = hex_colour
        self.refresh()
        self.request_render()

    def commit_hex(self):
        raw = self.hex_entry.get().strip()
        if not raw.startswith("#"):
            raw = "#" + raw
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", raw):
            if raw.upper() != self.values[self.selected].upper():
                self.end_spotlight()
                self.values[self.selected] = raw.upper()
                self.picker.set_hex(raw.upper())
                self.refresh()
                self.request_render()
        else:
            self.refresh()  # put the old value back

    def set_opacity(self, value):
        key = OPACITY_KEY.get(self.selected)
        if key not in self.values:
            return
        self.end_spotlight()
        # The file keeps two decimals, so anything finer would only make the
        # element look changed when it is not.
        self.values[key] = round(value, 2)
        self.refresh()
        self.request_render()

    def set_number(self, key, value):
        self.end_spotlight()
        self.values[key] = value
        self.slider_values[key].configure(text="%.2f" % value)
        self.request_render()

    def set_backdrop(self, value):
        self.backdrop = value
        self.request_render()

    def set_phase(self, value):
        self.phase = value
        self.request_render()

    def set_switch(self, key, on):
        self.end_spotlight()
        self.values[key] = on
        self.refresh()
        self.request_render()

    def use_preset(self, colours):
        self.end_spotlight()
        palette = dict(zip(ROLES, colours))
        for key in self.colour_keys:
            self.values[key] = palette[ELEMENT[key][1]]
        self.picker.set_hex(self.values[self.selected])
        self.refresh()
        self.request_render()

    def revert(self):
        self.end_spotlight()
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

    def preview_text(self):
        if self.spotlight:
            return spotlight_text(self.original_text, self.values, self.spotlight)
        return self.candidate_text()

    def request_render(self):
        if self.pending is not None:
            self.after_cancel(self.pending)
        self.pending = self.after(DEBOUNCE_MS, self._start_render)

    def _start_render(self):
        self.pending = None
        self.render_seq += 1
        # Only the newest render is ever shown, so it is enough to remember
        # what that one is picking out.
        self.rendering_spotlight = self.spotlight
        threading.Thread(target=self._render,
                         args=(self.render_seq, self.preview_text(),
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
                if png and self.spotlight and self.rendering_spotlight == self.spotlight:
                    self._spotlight_shown()
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
