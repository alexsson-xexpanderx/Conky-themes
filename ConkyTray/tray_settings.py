#!/usr/bin/env python3
"""ConkyTray's settings window: the look, the clock, what the panel shows and
how tall it is, with a preview of both ends of the panel at real size.

    ./tray_settings.py

It is built from Conky-Mirage's settings window (its buttons, sliders,
switches and tooltips are imported from ../Conky-Mirage/mirage_settings.py),
so the two look alike. Nothing is written until Save; settings left as they
were stay byte for byte as they were. tray.lua is picked up by the running
panel by itself; a new height needs the panel restarted, which Save does.
"""

import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "tray.lua")
CONFIG = os.path.join(HERE, "start_conky_tray")
RENDERER = os.path.join(HERE, "tray_preview.lua")
MIRAGE_DIR = os.path.join(os.path.dirname(HERE), "Conky-Mirage")

sys.path.insert(0, MIRAGE_DIR)
try:
    import mirage_settings as ms
except ImportError:
    sys.exit("The settings window is Conky-Mirage's, and Conky-Mirage should be in the folder "
             "next to ConkyTray (%s)." % MIRAGE_DIR)

BG, SURFACE, SUNKEN, BORDER = ms.BG, ms.SURFACE, ms.SUNKEN, ms.BORDER
TEXT, MUTED, FAINT, ACCENT = ms.TEXT, ms.MUTED, ms.FAINT, ms.ACCENT
FONT, F_BODY, F_SMALL, F_MONO = ms.FONT, ms.F_BODY, ms.F_SMALL, ms.F_MONO

PREVIEW_W = 1080
DEBOUNCE_MS = 160
END_GAP = 10                    # between the two ends of the panel in the preview
VIEW_H = 300                    # how much of each end of an upright panel the preview shows

STYLES = [("Like Mirage", "mirage"), ("Glass", "glass"), ("Solid", "solid"), ("Metro", "metro"),
          ("Pixel", "pixel")]
STYLE_NOTES = ["Whichever look Conky-Mirage has, so the two always match.",
               "A blurred piece of your wallpaper, one long pane.",
               "One flat pane in a deep or pale tone of your wallpaper.",
               "Square blocks in four bold colours from your wallpaper.",
               "Rounded blocks in soft tones of your wallpaper."]
CLOCKS = [("24 hour", "24h"), ("12 hour", "12h")]
EDGES = [("Bottom", "bottom"), ("Top", "top"), ("Left", "left"), ("Right", "right")]
DATES = ["%a %d %b", "%d %b", "%d/%m", "%Y-%m-%d"]

# The colours each look can be given, as tray.lua's settings, with what they
# are called here. "auto" is the look's own colour from the wallpaper.
COLOUR_KEYS = {
    "glass": [("glass_color", "Colour", "The tint laid over the glass.")],
    "solid": [("solid_color", "Colour", "The colour of the bar.")],
}
for _look in ("metro", "pixel"):
    COLOUR_KEYS[_look] = [(_look + "_launcher", "App menu button", "The block with the app menu's button."),
                          (_look + "_apps", "Apps", "The block with your apps. Menus take its colour too."),
                          (_look + "_media", "Now playing", "The block with the song or video playing."),
                          (_look + "_tray", "Tray", "The block with the tray icons, volume and so on."),
                          (_look + "_clock", "Clock", "The block with the time.")]
COLOURS = tuple(key for keys in COLOUR_KEYS.values() for key, _, _ in keys)
# Where each "auto" colour comes from in the palette the panel has now.
AUTO_FROM = {"glass_color": "tint", "solid_color": "solid",
             "metro_launcher": "launcher", "metro_apps": "metro2", "metro_media": "metro1", "metro_tray": "metro3",
             "metro_clock": "metro4", "pixel_launcher": "launcher", "pixel_apps": "px_surface",
             "pixel_media": "px_primary_c", "pixel_tray": "px_secondary_c", "pixel_clock": "px_tertiary_c"}
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

DEFAULTS = {"style": "mirage", "clock_format": "24h", "show_date": True, "date_format": "%a %d %b",
            "show_launcher": True, "show_tasks": True, "show_media": True, "show_visualizer": True,
            "show_tray": True, "show_clock": True,
            "show_privacy": True, "show_notifications": True,
            "show_devices": True, "show_keyboard": True, "show_bluetooth": True, "show_brightness": True,
            "show_volume": True, "show_network": True, "show_battery": True, "height": 56,
            "position": "bottom", "glass_transparency": 0.0}
DEFAULTS.update({key: "auto" for key in COLOURS})
STRINGS = ("style", "clock_format", "date_format") + COLOURS
NUMBERS = ("glass_transparency",)
SWITCHES = ("show_date", "show_launcher", "show_tasks", "show_media", "show_visualizer", "show_tray", "show_clock",
            "show_privacy", "show_notifications",
            "show_devices",
            "show_keyboard", "show_bluetooth", "show_brightness", "show_volume", "show_network", "show_battery")
HEIGHT_RE = re.compile(r"^(local height = )(\d+)", re.M)
POSITION_RE = re.compile(r'^(local position = )"(\w+)"', re.M)


def setting_re(key):
    return re.compile(r'^(%s\s*=\s*)"([^"]*)"([ \t]*)' % key, re.M)


def number_re(key):
    return re.compile(r'^(%s\s*=\s*)(-?[0-9]*\.?[0-9]+)([ \t]*)' % key, re.M)


def read_settings(lua_text, config_text):
    values = {}
    for key in STRINGS + SWITCHES:
        m = setting_re(key).search(lua_text)
        if m:
            raw = m.group(2)
            if key in SWITCHES:
                values[key] = raw.lower() not in ("no", "false", "off")
            else:
                values[key] = raw
    for key in NUMBERS:
        m = number_re(key).search(lua_text)
        if m:
            values[key] = float(m.group(2))
    m = HEIGHT_RE.search(config_text)
    if m:
        values["height"] = int(m.group(2))
    m = POSITION_RE.search(config_text)
    if m:
        values["position"] = m.group(2)
    return values


def as_text(key, value):
    if key in ("show_battery", "show_devices"):
        return "auto" if value else "no"
    if key in SWITCHES:
        return "yes" if value else "no"
    if key in NUMBERS:
        return "%.2f" % value
    return str(value)


def replaced(m, value):
    """The setting with its new value, the comment after it kept in its column."""
    spaces = m.group(3)
    if spaces:
        spaces = " " * max(1, len(m.group(2)) + len(spaces) - len(value))
    return m.group(1) + '"' + value + '"' + spaces


def apply_lua(text, values, saved):
    for key in STRINGS + SWITCHES:
        if key in values and values[key] != saved.get(key):
            text = setting_re(key).sub(lambda m, k=key: replaced(m, as_text(k, values[k])), text, count=1)
    for key in NUMBERS:
        if key in values and values[key] != saved.get(key):
            def number(m, k=key):
                new = as_text(k, values[k])
                spaces = m.group(3)
                if spaces:
                    spaces = " " * max(1, len(m.group(2)) + len(spaces) - len(new))
                return m.group(1) + new + spaces
            text = number_re(key).sub(number, text, count=1)
    return text


def apply_config(text, values, saved):
    if values.get("height") != saved.get("height"):
        text = HEIGHT_RE.sub(lambda m: m.group(1) + str(values["height"]), text, count=1)
    if values.get("position") != saved.get("position"):
        text = POSITION_RE.sub(lambda m: m.group(1) + '"%s"' % values["position"], text, count=1)
    return text


def overrides(values):
    return ["%s=%s" % (k, as_text(k, values[k])) for k in STRINGS + SWITCHES + NUMBERS if k in values]


def panel_palette():
    """The colours the panel wears now, from its helper's cache (what "auto"
    stands for in each look), and the look Mirage has, for "Like Mirage"."""
    cache = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache"),
                         "conky-tray")
    colours, look = {}, None
    try:
        with open(os.path.join(cache, "theme", "theme.lua"), encoding="utf-8") as handle:
            for name, r, g, b in re.findall(r"(\w+) = \{([\d.]+), ([\d.]+), ([\d.]+)\}", handle.read()):
                colours[name] = "#%02x%02x%02x" % tuple(min(255, round(float(v) * 255)) for v in (r, g, b))
    except OSError:
        pass
    try:
        with open(os.path.join(cache, "state.lua"), encoding="utf-8") as handle:
            state = handle.read()
        m = re.search(r'\["launcher"\] = \{[^}]*\["color"\] = "(#[0-9a-fA-F]{6})"', state)
        if m:
            colours["launcher"] = m.group(1)
        m = re.search(r'\["mirage"\] = \{[^}]*\["style"\] = "(\w+)"', state)
        if m:
            look = m.group(1)
    except OSError:
        pass
    return colours, look


ink_on, ColourPicker, Swatch = ms.ink_on, ms.ColourPicker, ms.Swatch


def screen_scale():
    """The screen's width and height in real pixels, and the desktop's scale."""
    out = subprocess.run(["python3", os.path.join(HERE, "tray.py"), "screen"], capture_output=True,
                         text=True, timeout=10).stdout.split()
    try:
        logical, real, real_h = int(out[0]), int(out[1]), int(out[4])
    except (IndexError, ValueError):
        logical, real, real_h = 1920, 1920, 1080
    return real, real / logical, real_h


def panel_pids():
    """The conky processes running this panel, from /proc (see CLAUDE.md on
    why not pgrep -f)."""
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % entry, "rb") as handle:
                parts = handle.read().split(b"\0")
        except OSError:
            continue
        if parts and os.path.basename(parts[0]) == b"conky" and any(b"start_conky_tray" in p for p in parts):
            pids.append(int(entry))
    return pids


class Renderer(threading.Thread):
    """One render at a time, always the newest request (as Mirage's)."""

    def __init__(self, results, workdir, width, scale, height):
        super().__init__(daemon=True)
        self.results, self.workdir, self.width, self.scale = results, workdir, width, scale
        self.screen_h = height
        self.cond = threading.Condition()
        self.request = None

    def submit(self, seq, values):
        with self.cond:
            self.request = (seq, dict(values))
            self.cond.notify()

    def run(self):
        while True:
            with self.cond:
                while self.request is None:
                    self.cond.wait()
                seq, values = self.request
                self.request = None
            try:
                self.results.put((seq, self.render(seq, values), None))
            except Exception as problem:
                self.results.put((seq, None, str(problem)))

    def render(self, seq, values):
        from PIL import Image
        height = round(values["height"] * self.scale)
        upright = values.get("position") in ("left", "right")
        size = (height, self.screen_h) if upright else (self.width, height)
        frame = os.path.join(self.workdir, "frame-%d.png" % seq)
        done = subprocess.run(["lua", RENDERER, frame, "size=%dx%d" % size, "scale=%.4f" % self.scale,
                               "position=" + values.get("position", "bottom")] + overrides(values),
                              capture_output=True, text=True, timeout=60, cwd=HERE)
        if done.returncode != 0 or not os.path.exists(frame):
            raise RuntimeError(done.stderr.strip() or "the preview could not be drawn")
        # The two ends: the middle of a panel is empty. A panel along the
        # bottom or top shows them one above the other, an upright one side
        # by side.
        panel = Image.open(frame).convert("RGB")
        os.remove(frame)
        if upright:
            h = min(VIEW_H, panel.height)
            view = Image.new("RGB", (2 * panel.width + 3 * END_GAP, h), (10, 15, 22))
            view.paste(panel.crop((0, 0, panel.width, h)), (0, 0))
            view.paste(panel.crop((0, panel.height - h, panel.width, panel.height)), (panel.width + 3 * END_GAP, 0))
        else:
            w = min(PREVIEW_W, panel.width)
            view = Image.new("RGB", (w, 2 * panel.height + END_GAP), (10, 15, 22))
            view.paste(panel.crop((0, 0, w, panel.height)), (0, 0))
            view.paste(panel.crop((panel.width - w, 0, panel.width, panel.height)), (0, panel.height + END_GAP))
        path = os.path.join(self.workdir, "view-%d.png" % seq)
        view.save(path, compress_level=1)
        for old in range(max(0, seq - 6), seq - 1):
            try:
                os.remove(os.path.join(self.workdir, "view-%d.png" % old))
            except OSError:
                pass
        return {"seq": seq, "path": path, "height": height}


class Editor(tk.Tk):
    def __init__(self):
        super().__init__(className="conkytray-settings")
        self.title("ConkyTray settings")
        self.configure(bg=BG)
        self.workdir = tempfile.mkdtemp(prefix="conky-tray-settings-")
        self._set_icon()
        with open(SCRIPT, encoding="utf-8") as handle:
            self.lua_text = handle.read()
        with open(CONFIG, encoding="utf-8") as handle:
            self.config_text = handle.read()
        self.saved = read_settings(self.lua_text, self.config_text)
        missing = [k for k in DEFAULTS if k not in self.saved]
        if missing:
            messagebox.showerror("Can't open the settings",
                                 "This copy of ConkyTray is older than this settings window.\n\n"
                                 "(Missing: %s)" % ", ".join(missing))
            self.destroy()
            raise SystemExit(1)
        self.values = dict(self.saved)
        self.screen_w, self.scale, self.screen_h = screen_scale()
        self.results = queue.Queue()
        self.renderer = Renderer(self.results, self.workdir, self.screen_w, self.scale, self.screen_h)
        self.renderer.start()
        self.seq, self.pending, self.image = 0, None, None
        self._build()
        self.resizable(False, False)
        self.after(60, self._drain)
        self.refresh()
        self.request_render()

    def _set_icon(self):
        self.icons = []
        try:
            for size in (256, 64, 32, 16):
                path = os.path.join(self.workdir, "icon-%d.png" % size)
                ms.make_icon(path, size)
                self.icons.append(tk.PhotoImage(file=path))
            self.iconphoto(True, *self.icons)
        except Exception:                                  # an icon is not worth failing over
            pass

    # -------------------------------------------------------------- building

    def _section(self, parent, title, column, row=0, span=1):
        box = tk.Frame(parent, bg=BG)
        box.grid(row=row, column=column, columnspan=span, sticky="nsew",
                 padx=(0 if column == 0 else 14, 0), pady=(0 if row == 0 else 14, 0))
        head = tk.Frame(box, bg=BG)
        head.pack(fill="x", pady=(2, 6))
        tk.Label(head, text=title.upper(), bg=BG, fg=FAINT, font=(FONT, 8, "bold")).pack(side="left")
        tk.Frame(head, bg=BORDER, height=1).pack(side="left", fill="x", expand=True, padx=(10, 0), pady=(1, 0))
        card = tk.Frame(box, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER, padx=14, pady=12)
        card.pack(fill="both", expand=True)
        return card

    def _switch_row(self, card, row, key, label, note, column=0):
        line = tk.Frame(card, bg=SURFACE)
        line.grid(row=row, column=column, sticky="ew", pady=(0 if row == 0 else 9, 0),
                  padx=(0 if column == 0 else 28, 0))
        name = tk.Label(line, text=label, bg=SURFACE, fg=TEXT, font=F_BODY)
        name.pack(side="left")
        switch = ms.Switch(line, self.values[key], lambda on, k=key: self.set_value(k, on))
        switch.pack(side="right")
        ms.Tooltip(name, note)
        ms.Tooltip(switch, note)
        self.switches[key] = switch

    def _build(self):
        root = tk.Frame(self, bg=BG, padx=16, pady=14)
        root.pack(fill="both", expand=True)

        # ---- preview
        tk.Label(root, text="PREVIEW", bg=BG, fg=FAINT, font=(FONT, 8, "bold")).grid(row=0, column=0, sticky="w")
        frame = tk.Frame(root, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER)
        frame.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        tallest = max(2 * round(96 * self.scale) + END_GAP, VIEW_H) + 24
        self.canvas = tk.Canvas(frame, width=PREVIEW_W + 24, height=tallest, bg=SUNKEN, highlightthickness=0)
        self.canvas.pack(fill="x", expand=True)
        self.canvas_text = self.canvas.create_text((PREVIEW_W + 24) // 2, tallest // 2, text="Loading the preview…",
                                                   fill=MUTED, font=F_BODY, width=PREVIEW_W - 80, justify="center")
        info = tk.Frame(root, bg=BG)
        info.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.size_label = tk.Label(info, text="Both ends of the panel, at real size", bg=BG, fg=MUTED, font=F_SMALL)
        self.size_label.pack(side="left")
        reload_button = ms.Button(info, "Reload", self.reload, width=72)
        reload_button.pack(side="right")
        ms.Tooltip(reload_button, "Draw the preview again, for example after you change your wallpaper "
                                  "or open other apps.")
        self.busy = tk.Label(info, text="", bg=BG, fg=FAINT, font=F_SMALL)
        self.busy.pack(side="right", padx=(0, 10))

        # ---- settings
        settings = tk.Frame(root, bg=BG)
        settings.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        settings.columnconfigure(0, weight=1)
        settings.columnconfigure(1, weight=1)
        self.switches = {}

        card = self._section(settings, "Look", 0)
        self.style_picker = ms.Segmented(card, STYLES, self.values["style"], lambda v: self.set_value("style", v),
                                         width=470)
        self.style_picker.configure(bg=SURFACE)
        self.style_picker.grid(row=0, column=0, sticky="w")
        ms.Tooltip(self.style_picker, lambda x: STYLE_NOTES[self.style_picker._index_at(x)])
        self.style_note = tk.Label(card, text="", bg=SURFACE, fg=MUTED, font=F_SMALL, anchor="w",
                                   justify="left", wraplength=460, width=64, height=2)
        self.style_note.grid(row=1, column=0, sticky="w", pady=(10, 0))
        where = tk.Frame(card, bg=SURFACE)
        where.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        where_name = tk.Label(where, text="Where", bg=SURFACE, fg=TEXT, font=F_BODY)
        where_name.pack(side="left")
        self.edge_picker = ms.Segmented(where, EDGES, self.values["position"],
                                        lambda v: self.set_value("position", v), width=360)
        self.edge_picker.configure(bg=SURFACE)
        self.edge_picker.pack(side="right")
        note = "Which edge of the screen the panel goes along. On the left or right it stands upright."
        ms.Tooltip(where_name, note)
        ms.Tooltip(self.edge_picker, note)
        head = tk.Frame(card, bg=SURFACE)
        head.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        height_name = tk.Label(head, text="Height", bg=SURFACE, fg=TEXT, font=F_BODY)
        self.height_name = height_name
        height_name.pack(side="left")
        self.height_value = tk.Label(head, text="", bg=SURFACE, fg=ACCENT, font=F_MONO)
        self.height_value.pack(side="right")
        self.height_slider = ms.Slider(card, 40, 96, 1, self.values["height"], DEFAULTS["height"],
                                       lambda v: self.set_value("height", int(round(v))), width=470)
        self.height_slider.grid(row=4, column=0, sticky="w")
        note = lambda x: ("How wide the panel is. " if self.values["position"] in ("left", "right") else
                          "How tall the panel is. ") + "Windows keep clear of it.\nDouble-click the slider to put it back."
        ms.Tooltip(height_name, note)
        ms.Tooltip(self.height_slider, note)

        card = self._section(settings, "Clock", 1)
        self.clock_picker = ms.Segmented(card, CLOCKS, self.values["clock_format"],
                                         lambda v: self.set_value("clock_format", v), width=336)
        self.clock_picker.configure(bg=SURFACE)
        self.clock_picker.grid(row=0, column=0, sticky="w")
        ms.Tooltip(self.clock_picker, "14:30 or 2:30 PM.")
        line = tk.Frame(card, bg=SURFACE)
        line.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        tk.Label(line, text="Date", bg=SURFACE, fg=TEXT, font=F_BODY).pack(side="left")
        self.switches["show_date"] = ms.Switch(line, self.values["show_date"], lambda on: self.set_value("show_date", on))
        self.switches["show_date"].pack(side="right")
        ms.Tooltip(self.switches["show_date"], "The date under the time.")
        dates = list(DATES)
        if self.values["date_format"] not in dates:
            dates.append(self.values["date_format"])
        self.date_picker = ms.Segmented(card, [(time.strftime(d), d) for d in dates], self.values["date_format"],
                                        lambda v: self.set_value("date_format", v), width=336)
        self.date_picker.configure(bg=SURFACE)
        self.date_picker.grid(row=2, column=0, sticky="w", pady=(8, 0))
        ms.Tooltip(self.date_picker, "How the date is written.")

        self._build_colours(settings)

        card = self._section(settings, "Shown", 0, row=2, span=2)
        # In the order they sit on the panel, four to a row.
        for i, (key, label, note) in enumerate((
                ("show_launcher", "App menu button", "The button that opens the app menu."),
                ("show_tasks", "Apps", "Your pinned apps and the ones that are open."),
                ("show_media", "Now playing", "The song or video playing, in the room between the apps and the "
                                              "tray. Click to pause, scroll to skip."),
                ("show_visualizer", "Sound bars", "Bars beside the song that move with the sound while it plays."),
                ("show_tray", "Tray icons", "The small icons apps put in the tray."),
                ("show_privacy", "Microphone and camera", "A red dot while an app uses your microphone or "
                                                          "camera. Click it to see which app."),
                ("show_notifications", "Notifications", "A bell with the notifications you have had. Click it "
                                                         "to see them again or to turn on Do not disturb."),
                ("show_devices", "Devices", "USB drives and memory cards. Shows while one is plugged in, and "
                                            "opens a small menu by itself when you plug one in."),
                ("show_keyboard", "Keyboard layout", "Which layout you type in. Click to switch. Shows when you "
                                                     "have more than one."),
                ("show_bluetooth", "Bluetooth", "Click to turn it on or off, or connect your devices."),
                ("show_brightness", "Brightness", "Scroll over it to change your screen's brightness."),
                ("show_volume", "Volume", "Scroll over it to change the volume, click to mute."),
                ("show_network", "Network", "Your Wi-Fi or cable. Click it for the settings."),
                ("show_battery", "Battery", "Only shows on a computer that has one."),
                ("show_clock", "Clock", "The time, and the date under it if that is on."))):
            card.columnconfigure(i % 4, weight=1)
            self._switch_row(card, i // 4, key, label, note, column=i % 4)

        # ---- buttons
        bar = tk.Frame(root, bg=BG)
        bar.grid(row=4, column=0, sticky="ew", pady=(14, 0))
        self.status = tk.Label(bar, text="", bg=BG, fg=ACCENT, font=F_SMALL, anchor="w", width=60)
        self.status.pack(side="left")
        buttons = [(ms.Button(bar, "Save", self.save, primary=True, width=76), "Save. The panel changes right away."),
                   (ms.Button(bar, "Defaults", self.defaults, width=84), "Go back to the original settings."),
                   (ms.Button(bar, "Undo", self.revert, width=70), "Undo everything since you last saved.")]
        for i, (button, note) in enumerate(buttons):
            button.pack(side="right", padx=(0, 0 if i == 0 else 8))
            ms.Tooltip(button, note)

    def _build_colours(self, settings):
        card = self._section(settings, "Colours", 0, row=1, span=2)
        card.columnconfigure(0, weight=1)
        self.colour_rows = tk.Frame(card, bg=SURFACE)
        self.colour_rows.grid(row=0, column=0, sticky="nw")
        side = tk.Frame(card, bg=SURFACE)
        side.grid(row=0, column=1, sticky="ne", padx=(24, 0))
        self.picker = ColourPicker(side, self._picked)
        self.picker.grid(row=0, column=0, rowspan=3, sticky="nw")
        self.hex_entry = tk.Entry(side, width=9, bg=SUNKEN, fg=TEXT, insertbackground=TEXT, relief="flat",
                                  font=F_MONO, highlightthickness=1, highlightbackground=BORDER,
                                  highlightcolor=ACCENT)
        self.hex_entry.grid(row=0, column=1, sticky="nw", padx=(14, 0), ipady=4)
        self.hex_entry.bind("<Return>", self._typed)
        self.hex_entry.bind("<FocusOut>", self._typed)
        ms.Tooltip(self.hex_entry, "The colour as a code, like #1f3a5f. Type one and press Enter.")
        auto = ms.Button(side, "Auto", self._auto, width=78)
        auto.configure(bg=SURFACE)
        auto.grid(row=1, column=1, sticky="nw", padx=(14, 0), pady=(10, 0))
        ms.Tooltip(auto, "Back to the colour from your wallpaper.")
        self.palette, self.mirage_look = panel_palette()
        self.colour_look, self.colour_key, self.swatches = None, None, {}
        self.see_slider = None

    def effective_look(self):
        style = self.values["style"]
        return style if style in COLOUR_KEYS else (self.mirage_look if self.mirage_look in COLOUR_KEYS else "glass")

    def auto_colour(self, key):
        source = AUTO_FROM.get(key, "")
        if source == "launcher" and "launcher" not in self.palette:
            source = "metro1" if key.startswith("metro") else "px_primary"
        return self.palette.get(source, "#5a6070")

    def shown_colour(self, key):
        value = self.values.get(key, "auto")
        return value.lower() if HEX_RE.match(value) else self.auto_colour(key)

    def _fill_colours(self, look):
        for child in self.colour_rows.winfo_children():
            child.destroy()
        self.swatches, self.see_slider = {}, None
        name = dict((v, n) for n, v in STYLES).get(look, look.title())
        tk.Label(self.colour_rows, text="%s's colours. Auto takes each from your wallpaper; text and icons turn "
                 "white or dark to stay readable." % name, bg=SURFACE, fg=MUTED, font=F_SMALL, anchor="w",
                 justify="left").grid(row=0, column=0, columnspan=6, sticky="w")
        for i, (key, label, note) in enumerate(COLOUR_KEYS[look]):
            cell = tk.Frame(self.colour_rows, bg=SURFACE)
            cell.grid(row=1, column=i, sticky="w", padx=(0, 14), pady=(12, 0))
            text = tk.Label(cell, text=label, bg=SURFACE, fg=TEXT, font=F_BODY, anchor="w")
            text.pack(anchor="w")
            swatch = Swatch(cell, lambda k=key: self._select(k))
            swatch.pack(anchor="w", pady=(6, 0))
            ms.Tooltip(text, note)
            ms.Tooltip(swatch, note + " Click it, then pick a colour on the right.")
            self.swatches[key] = swatch
        if look == "glass":
            line = tk.Frame(self.colour_rows, bg=SURFACE)
            line.grid(row=2, column=0, columnspan=6, sticky="w", pady=(16, 0))
            head = tk.Frame(line, bg=SURFACE)
            head.pack(fill="x")
            label = tk.Label(head, text="See-through", bg=SURFACE, fg=TEXT, font=F_BODY)
            label.pack(side="left")
            self.see_value = tk.Label(head, text="", bg=SURFACE, fg=ACCENT, font=F_MONO)
            self.see_value.pack(side="right")
            self.see_slider = ms.Slider(line, 0, 90, 1, round(self.values["glass_transparency"] * 100), 0,
                                        lambda v: self.set_value("glass_transparency", round(v) / 100), width=420)
            self.see_slider.pack(anchor="w")
            note = "How much of your wallpaper shows through the glass.\nDouble-click the slider to put it back."
            ms.Tooltip(label, note)
            ms.Tooltip(self.see_slider, note)
        keys = [key for key, _, _ in COLOUR_KEYS[look]]
        if self.colour_key not in keys:
            self.colour_key = keys[0]
        self.colour_look = look
        self._load_picker()

    def _load_picker(self):
        colour = self.shown_colour(self.colour_key)
        self.picker.set_hex(colour)
        self.hex_entry.delete(0, "end")
        self.hex_entry.insert(0, colour)

    def _select(self, key):
        self.colour_key = key
        self._load_picker()
        self.refresh()

    def _picked(self, colour):
        self.hex_entry.delete(0, "end")
        self.hex_entry.insert(0, colour)
        self.set_value(self.colour_key, colour)

    def _typed(self, event=None):
        text = self.hex_entry.get().strip()
        if not text.startswith("#"):
            text = "#" + text
        if HEX_RE.match(text) and text.lower() != self.shown_colour(self.colour_key):
            self.picker.set_hex(text.lower())
            self.set_value(self.colour_key, text.lower())

    def _auto(self):
        self.set_value(self.colour_key, "auto")
        self._load_picker()

    def _refresh_colours(self):
        look = self.effective_look()
        if look != self.colour_look:
            self._fill_colours(look)
        for key, swatch in self.swatches.items():
            auto = not HEX_RE.match(self.values.get(key, "auto"))
            swatch.show(self.shown_colour(key), auto, key == self.colour_key)
        if self.see_slider is not None:
            self.see_slider.set(round(self.values["glass_transparency"] * 100))
            mark = "• " if self.values["glass_transparency"] != self.saved.get("glass_transparency") else ""
            self.see_value.configure(text="%s%d%%" % (mark, round(self.values["glass_transparency"] * 100)))

    # ------------------------------------------------------------- reactions

    def set_value(self, key, value):
        self.values[key] = value
        self.status.configure(text="")
        self.refresh()
        self.request_render()

    def refresh(self):
        self.height_name.configure(text="Width" if self.values["position"] in ("left", "right") else "Height")
        for picker, key in ((self.style_picker, "style"), (self.clock_picker, "clock_format"),
                            (self.date_picker, "date_format"), (self.edge_picker, "position")):
            if picker.value != self.values[key]:
                picker.value = self.values[key]
                picker._draw()
        for key, switch in self.switches.items():
            if switch.value != self.values[key]:
                switch.show(self.values[key])
        self.date_picker.configure(cursor="hand2" if self.values["show_date"] else "")
        self.height_slider.set(self.values["height"])
        mark = "• " if self.values["height"] != self.saved["height"] else ""
        self.height_value.configure(text="%s%d px" % (mark, self.values["height"]))
        index = [v for _, v in STYLES].index(self.values["style"]) if self.values["style"] in [v for _, v in STYLES] else 0
        self.style_note.configure(text=STYLE_NOTES[index])
        self._refresh_colours()

    def revert(self):
        self.values = dict(self.saved)
        self.refresh()
        self._load_picker()
        self.request_render()
        self.status.configure(text="Changes undone.", fg=ACCENT)

    def defaults(self):
        self.values = dict(DEFAULTS)
        self.refresh()
        self._load_picker()
        self.request_render()
        self.status.configure(text="The original settings. Save to keep them.", fg=ACCENT)

    # ---------------------------------------------------------------- render

    def reload(self):
        self.palette, self.mirage_look = panel_palette()
        self.colour_look = None              # the "auto" colours may have changed with the wallpaper
        self.refresh()
        self.request_render()

    def request_render(self):
        if self.pending is not None:
            self.after_cancel(self.pending)
        self.pending = self.after(DEBOUNCE_MS, self._start_render)

    def _start_render(self):
        self.pending = None
        self.seq += 1
        self.busy.configure(text="Updating the preview…")
        self.renderer.submit(self.seq, self.values)

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
                    self.canvas.delete("preview")
                    self.canvas.itemconfigure(self.canvas_text, state="normal",
                                              text="Couldn't draw the preview.\n\n" + problem)
            else:
                self._show(info)
        self.after(60, self._drain)

    def _show(self, info):
        try:
            image = tk.PhotoImage(file=info["path"])
        except tk.TclError as failure:
            self.canvas.itemconfigure(self.canvas_text, text=str(failure), state="normal")
            return
        self.image = image
        self.canvas.delete("preview")
        cw, ch = max(int(self.canvas["width"]), self.canvas.winfo_width()), int(self.canvas["height"])
        self.canvas.create_image((cw - image.width()) // 2, (ch - image.height()) // 2, image=image,
                                 anchor="nw", tags="preview")
        self.canvas.itemconfigure(self.canvas_text, state="hidden")
        self.size_label.configure(text="Both ends of the panel, at real size: %d pixels %s" % (
            info["height"], "wide" if self.values["position"] in ("left", "right") else "tall"))

    # ---------------------------------------------------------------- saving

    def save(self):
        # The files as they are now, not as they were when the window opened:
        # writing back the old copy undid every change made to them since,
        # by hand or otherwise. Only the settings changed here are touched.
        try:
            with open(SCRIPT, encoding="utf-8") as handle:
                self.lua_text = handle.read()
            with open(CONFIG, encoding="utf-8") as handle:
                self.config_text = handle.read()
        except OSError as problem:
            messagebox.showerror("Not saved", "Couldn't read the settings.\n\n%s" % problem, parent=self)
            return
        lua = apply_lua(self.lua_text, self.values, self.saved)
        config = apply_config(self.config_text, self.values, self.saved)
        running = panel_pids()
        # A new height is a new window, and conky comes back from its config
        # changing underneath it with a broken window (see Mirage), so the
        # panel is stopped before the config is written and started after.
        # tray.lua alone it picks up by itself.
        restart = config != self.config_text or not running
        if restart:
            for pid in running:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        try:
            for path, old, new in ((SCRIPT, self.lua_text, lua), (CONFIG, self.config_text, config)):
                if new != old:
                    shutil.copy2(path, path + ".bak")
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(new)
        except OSError as problem:
            messagebox.showerror("Not saved", "Couldn't save the settings.\n\n%s" % problem, parent=self)
            return
        self.lua_text, self.config_text = lua, config
        self.saved = dict(self.values)
        self.saved.update(read_settings(lua, config))
        self.refresh()
        if restart:
            try:
                subprocess.Popen(["conky", "-c", CONFIG], cwd=HERE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
            except FileNotFoundError:
                messagebox.showerror("Not started", "Your settings are saved, but conky couldn't be found "
                                     "to start the panel.", parent=self)
                return
            self.status.configure(text="Saved. The panel is restarting." if running else
                                  "Saved. The panel is starting.", fg=ACCENT)
        else:
            self.status.configure(text="Saved.", fg=ACCENT)

    def destroy(self):
        shutil.rmtree(getattr(self, "workdir", ""), ignore_errors=True)
        super().destroy()


def main():
    for path in (SCRIPT, CONFIG, RENDERER):
        if not os.path.exists(path):
            sys.exit("%s is missing. It should be in %s." % (os.path.basename(path), HERE))
    if shutil.which("lua") is None:
        sys.exit("The preview needs Lua. Install the \"lua\" package and try again.")
    Editor().mainloop()


if __name__ == "__main__":
    main()
