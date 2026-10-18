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

STYLES = [("Like Mirage", "mirage"), ("Glass", "glass"), ("Solid", "solid"), ("Metro", "metro"),
          ("Pixel", "pixel")]
STYLE_NOTES = ["Whichever look Conky-Mirage has, so the two always match.",
               "A blurred piece of your wallpaper, one long pane.",
               "One flat pane in a deep or pale tone of your wallpaper.",
               "Square blocks in four bold colours from your wallpaper.",
               "Rounded blocks in soft tones of your wallpaper."]
CLOCKS = [("24 hour", "24h"), ("12 hour", "12h")]
DATES = ["%a %d %b", "%d %b", "%d/%m", "%Y-%m-%d"]

DEFAULTS = {"style": "mirage", "clock_format": "24h", "show_date": True, "date_format": "%a %d %b",
            "show_volume": True, "show_network": True, "show_battery": True, "height": 56}
STRINGS = ("style", "clock_format", "date_format")
SWITCHES = ("show_date", "show_volume", "show_network", "show_battery")
HEIGHT_RE = re.compile(r"^(local height = )(\d+)", re.M)


def setting_re(key):
    return re.compile(r'^(%s\s*=\s*)"([^"]*)"([ \t]*)' % key, re.M)


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
    m = HEIGHT_RE.search(config_text)
    if m:
        values["height"] = int(m.group(2))
    return values


def as_text(key, value):
    if key == "show_battery":
        return "auto" if value else "no"
    if key in SWITCHES:
        return "yes" if value else "no"
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
    return text


def apply_config(text, values, saved):
    if values.get("height") != saved.get("height"):
        text = HEIGHT_RE.sub(lambda m: m.group(1) + str(values["height"]), text, count=1)
    return text


def overrides(values):
    return ["%s=%s" % (k, as_text(k, values[k])) for k in STRINGS + SWITCHES if k in values]


def screen_scale():
    """The screen's width in real pixels and the desktop's scale."""
    out = subprocess.run(["python3", os.path.join(HERE, "tray.py"), "screen"], capture_output=True,
                         text=True, timeout=10).stdout.split()
    try:
        logical, real = int(out[0]), int(out[1])
    except (IndexError, ValueError):
        logical = real = 1920
    return real, real / logical


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

    def __init__(self, results, workdir, width, scale):
        super().__init__(daemon=True)
        self.results, self.workdir, self.width, self.scale = results, workdir, width, scale
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
        frame = os.path.join(self.workdir, "frame-%d.png" % seq)
        done = subprocess.run(["lua", RENDERER, frame, "size=%dx%d" % (self.width, height),
                               "scale=%.4f" % self.scale] + overrides(values),
                              capture_output=True, text=True, timeout=60, cwd=HERE)
        if done.returncode != 0 or not os.path.exists(frame):
            raise RuntimeError(done.stderr.strip() or "the preview could not be drawn")
        # The two ends, one above the other: the middle of a panel is empty.
        panel = Image.open(frame).convert("RGB")
        os.remove(frame)
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
        self.screen_w, self.scale = screen_scale()
        self.results = queue.Queue()
        self.renderer = Renderer(self.results, self.workdir, self.screen_w, self.scale)
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

    def _section(self, parent, title, column):
        box = tk.Frame(parent, bg=BG)
        box.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 14, 0))
        head = tk.Frame(box, bg=BG)
        head.pack(fill="x", pady=(2, 6))
        tk.Label(head, text=title.upper(), bg=BG, fg=FAINT, font=(FONT, 8, "bold")).pack(side="left")
        tk.Frame(head, bg=BORDER, height=1).pack(side="left", fill="x", expand=True, padx=(10, 0), pady=(1, 0))
        card = tk.Frame(box, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER, padx=14, pady=12)
        card.pack(fill="both", expand=True)
        return card

    def _switch_row(self, card, row, key, label, note):
        line = tk.Frame(card, bg=SURFACE)
        line.grid(row=row, column=0, sticky="ew", pady=(0 if row == 0 else 9, 0))
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
        tallest = 2 * round(96 * self.scale) + END_GAP + 24
        self.canvas = tk.Canvas(frame, width=PREVIEW_W + 24, height=tallest, bg=SUNKEN, highlightthickness=0)
        self.canvas.pack(fill="x", expand=True)
        self.canvas_text = self.canvas.create_text((PREVIEW_W + 24) // 2, tallest // 2, text="Loading the preview…",
                                                   fill=MUTED, font=F_BODY, width=PREVIEW_W - 80, justify="center")
        info = tk.Frame(root, bg=BG)
        info.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        self.size_label = tk.Label(info, text="Both ends of the panel, at real size", bg=BG, fg=MUTED, font=F_SMALL)
        self.size_label.pack(side="left")
        reload_button = ms.Button(info, "Reload", self.request_render, width=72)
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
        settings.columnconfigure(2, weight=1, minsize=200)
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
        head = tk.Frame(card, bg=SURFACE)
        head.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        height_name = tk.Label(head, text="Height", bg=SURFACE, fg=TEXT, font=F_BODY)
        height_name.pack(side="left")
        self.height_value = tk.Label(head, text="", bg=SURFACE, fg=ACCENT, font=F_MONO)
        self.height_value.pack(side="right")
        self.height_slider = ms.Slider(card, 40, 96, 1, self.values["height"], DEFAULTS["height"],
                                       lambda v: self.set_value("height", int(round(v))), width=470)
        self.height_slider.grid(row=3, column=0, sticky="w")
        note = "How tall the panel is. Windows keep clear of it.\nDouble-click the slider to put it back."
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

        card = self._section(settings, "Shown", 2)
        card.columnconfigure(0, weight=1)
        self._switch_row(card, 0, "show_volume", "Volume", "Scroll over it to change the volume, click to mute.")
        self._switch_row(card, 1, "show_network", "Network", "Your Wi-Fi or cable. Click it for the settings.")
        self._switch_row(card, 2, "show_battery", "Battery", "Only shows on a computer that has one.")

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

    # ------------------------------------------------------------- reactions

    def set_value(self, key, value):
        self.values[key] = value
        self.status.configure(text="")
        self.refresh()
        self.request_render()

    def refresh(self):
        for picker, key in ((self.style_picker, "style"), (self.clock_picker, "clock_format"),
                            (self.date_picker, "date_format")):
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

    def revert(self):
        self.values = dict(self.saved)
        self.refresh()
        self.request_render()
        self.status.configure(text="Changes undone.", fg=ACCENT)

    def defaults(self):
        self.values = dict(DEFAULTS)
        self.refresh()
        self.request_render()
        self.status.configure(text="The original settings. Save to keep them.", fg=ACCENT)

    # ---------------------------------------------------------------- render

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
        self.size_label.configure(text="Both ends of the panel, at real size: %d pixels tall" % info["height"])

    # ---------------------------------------------------------------- saving

    def save(self):
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
