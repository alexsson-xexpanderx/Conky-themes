#!/usr/bin/env python3
"""Mirage-Terminal's settings window: which look the terminal wears, its
background and how see-through it is, Konsole's margin, and what mirafetch
shows, with a preview of a Konsole window running mirafetch.

    ./terminal_settings.py

Built from Conky-Mirage's settings window (its buttons, sliders, switches,
tooltips and colour picker come from ../Conky-Mirage/mirage_settings.py), so
it looks like it and ConkyTray's. Nothing is written until Save; settings
left as they were stay byte for byte as they were.
"""

import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.join(HERE, "terminal_palette.py")
sys.path.insert(0, HERE)
import terminal_palette as T   # noqa: E402  (exits with a message when Conky-Mirage is missing)

sys.path.insert(0, T.MIRAGE_DIR)
import mirage_settings as ms   # noqa: E402
import mirage_konsole as K   # noqa: E402

try:
    import terminal_preview as TP
except ImportError as problem:                      # Pillow, most likely
    sys.exit("The preview needs Pillow (python3-pillow): %s" % problem)

BG, SURFACE, SUNKEN, BORDER = ms.BG, ms.SURFACE, ms.SUNKEN, ms.BORDER
TEXT, MUTED, FAINT, ACCENT = ms.TEXT, ms.MUTED, ms.FAINT, ms.ACCENT
FONT, F_BODY, F_SMALL, F_MONO = ms.FONT, ms.F_BODY, ms.F_SMALL, ms.F_MONO

DEBOUNCE_MS = 140
PREVIEW_COLS, PREVIEW_ROWS = 128, 34
PREVIEW_FONT = 0.82             # of the profile's font size: a wider terminal than a real-size one would fit
SIDE_W = 410                    # the settings column

FOLLOWS = [("Panel", "panel"), ("Mirage", "mirage"), ("Glass", "glass"), ("Solid", "solid"),
           ("Metro", "metro"), ("Pixel", "pixel")]
FOLLOW_NOTES = ["The panel's look, whichever it is, so the terminal changes with ConkyTray.",
                "Conky-Mirage's look, whichever it is, so the terminal changes with the widget.",
                "Always Glass: see-through, with your desktop blurred behind it.",
                "Always Solid: one calm colour from your wallpaper.",
                "Always Metro: a deep tone of the panel's apps block, white text.",
                "Always Pixel: the soft tone of the panel's surface."]
BESIDE = [("Logo", "logo"), ("Wallpaper", "wallpaper"), ("Nothing", "none")]
BESIDE_NOTES = ["Your system's logo, drawn in the look.", "The wallpaper the colours come from.",
                "Only the facts."]

STRINGS = ("follow", "glass_background", "solid_background", "metro_background", "pixel_background",
           "blur", "beside")
NUMBERS = {"glass_see_through": "%.2f", "solid_see_through": "%.2f", "metro_see_through": "%.2f",
           "pixel_see_through": "%.2f", "colour_strength": "%.2f", "margin": "%d"}
DEFAULTS = {"follow": "panel", "glass_background": "auto", "solid_background": "auto",
            "metro_background": "auto", "pixel_background": "auto", "blur": True, "beside": "logo",
            "glass_see_through": 0.20, "solid_see_through": 0.0, "metro_see_through": 0.0,
            "pixel_see_through": 0.0, "colour_strength": 1.0, "margin": 12}
HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
RC_START = "# >>> mirafetch, from Mirage-Terminal's settings >>>"
RC_END = "# <<< mirafetch <<<"
BARE_CALL = re.compile(r"^\s*(\S*/)?mirafetch(\s+[^;&|#]*)?\s*$", re.M)
LOCK = threading.Lock()         # terminal_palette's settings are module globals; one user at a time


# ---------------------------------------------------------------- the file

def setting_re(key):
    return re.compile(r'^(%s\s*=\s*)"([^"]*)"([ \t]*)' % key, re.M)


def number_re(key):
    return re.compile(r'^(%s\s*=\s*)(-?[0-9]*\.?[0-9]+)([ \t]*)' % key, re.M)


def read_settings(text):
    values = {}
    for key in STRINGS:
        m = setting_re(key).search(text)
        if m:
            values[key] = m.group(2).lower() == "yes" if key == "blur" else m.group(2)
    for key, form in NUMBERS.items():
        m = number_re(key).search(text)
        if m:
            values[key] = int(float(m.group(2))) if form == "%d" else float(m.group(2))
    return values


def as_text(key, value):
    if key == "blur":
        return "yes" if value else "no"
    if key in NUMBERS:
        return NUMBERS[key] % value
    return str(value)


def apply_settings(text, values, saved):
    """The file with the settings changed here, the comment after each kept
    in its column; everything else as it was."""
    for key in STRINGS + tuple(NUMBERS):
        if key not in values or values[key] == saved.get(key):
            continue
        quoted = key in STRINGS

        def put(m, k=key, quoted=quoted):
            new = as_text(k, values[k])
            spaces = m.group(3)
            if spaces:
                spaces = " " * max(1, len(m.group(2)) + len(spaces) - len(new))
            return m.group(1) + ('"%s"' % new if quoted else new) + spaces
        text = (setting_re(key) if quoted else number_re(key)).sub(put, text, count=1)
    return text


def as_module_value(key, value):
    return as_text(key, value) if key in STRINGS else value


# ------------------------------------------------------- outside this folder

def konsole_installed():
    return os.path.exists(os.path.join(K.KONSOLE, K.PROFILE + ".profile")) and \
        K.default_profile() == K.PROFILE + ".profile"


def rc_file():
    """The shell's start-up file mirafetch can be added to, or None."""
    shell = os.path.basename(os.environ.get("SHELL", ""))
    home = os.path.expanduser("~")
    if shell == "bash":
        return os.path.join(home, ".bashrc")
    if shell == "zsh":
        return os.path.join(os.environ.get("ZDOTDIR") or home, ".zshrc")
    return None


def rc_has_mirafetch(path):
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return False
    return RC_START in text or bool(BARE_CALL.search(text))


def rc_set_mirafetch(path, on):
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        text = ""
    if os.path.exists(path) and not os.path.exists(path + ".before-mirafetch"):
        shutil.copy2(path, path + ".before-mirafetch")
    text = re.sub(r"\n?%s\n.*?%s\n?" % (re.escape(RC_START), re.escape(RC_END)), "\n", text, flags=re.S)
    text = BARE_CALL.sub("", text)
    text = text.rstrip("\n") + "\n" if text.strip() else ""
    if on:
        tool = os.path.join(HERE, "mirafetch")
        text += "\n%s\n[ -t 1 ] && [ -x \"%s\" ] && \"%s\"\n%s\n" % (RC_START, tool, tool, RC_END)
    with open(path + ".tmp", "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(path + ".tmp", path)


def make_icon(path, size):
    """The window's icon: a prompt on a frosted tile in Mirage's dusk colours,
    drawn four times larger and scaled down."""
    from PIL import Image, ImageDraw, ImageFilter
    S = size * 4
    top, bottom = (46, 38, 98), (118, 56, 112)
    column = Image.linear_gradient("L").resize((1, S))
    rgb = [column.point(lambda v, a=a, b=b: int(a + (b - a) * v / 255)).resize((S, S)) for a, b in zip(top, bottom)]
    tile = Image.merge("RGB", rgb).convert("RGBA")
    glow = Image.new("L", (S, S), 0)
    ImageDraw.Draw(glow).ellipse((S * 0.05, S * 0.62, S * 0.95, S * 1.35), fill=150)
    tile = Image.composite(Image.new("RGBA", (S, S), (255, 150, 110, 255)), tile,
                           glow.filter(ImageFilter.GaussianBlur(S * 0.12)))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, S - 1, S - 1), radius=int(S * 0.23), fill=255)
    icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    icon.paste(tile, (0, 0), mask)
    draw = ImageDraw.Draw(icon)
    w = S * 0.075
    draw.line([(S * 0.24, S * 0.32), (S * 0.44, S * 0.50), (S * 0.24, S * 0.68)], fill=(255, 255, 255, 255),
              width=round(w), joint="curve")
    draw.rounded_rectangle((S * 0.50, S * 0.64, S * 0.78, S * 0.64 + w), radius=w / 2, fill=(255, 196, 150, 255))
    icon.resize((size, size), Image.LANCZOS).save(path)


# ----------------------------------------------------------------- preview

def preview_fonts():
    return TP.Fonts(pt=TP.profile_font()[1] * PREVIEW_FONT)


def put_settings(values):
    """terminal_palette's settings set to `values`, for a preview. Under LOCK."""
    for key in STRINGS + tuple(NUMBERS):
        if key in values:
            setattr(T, key, as_module_value(key, values[key]))


class Renderer(threading.Thread):
    """One render at a time, always the newest request (as Mirage's)."""

    def __init__(self, results, workdir):
        super().__init__(daemon=True)
        self.results, self.workdir = results, workdir
        self.cond = threading.Condition()
        self.request = None
        self.known = None
        self.fonts = None

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
        if self.known is None:
            # The facts once: they do not change while the window is open.
            # Here the terminal is the one in the picture, not this window.
            self.fonts = preview_fonts()
            self.known = TP.preview_facts()
        with LOCK:
            put_settings(values)
            look = T.current_look()
            see = T.see_through(look)
            session = TP.session_text(look, PREVIEW_COLS, PREVIEW_ROWS, values["beside"], self.fonts, self.known)
            picture = TP.window(look, session, PREVIEW_COLS, PREVIEW_ROWS, int(values["margin"]), see,
                                bool(values["blur"]), self.fonts)
        path = os.path.join(self.workdir, "view-%d.png" % seq)
        picture.save(path, compress_level=1)
        for old in range(max(0, seq - 6), seq - 1):
            try:
                os.remove(os.path.join(self.workdir, "view-%d.png" % old))
            except OSError:
                pass
        return {"path": path, "look": look}


# ------------------------------------------------------------------ window

class Editor(tk.Tk):
    def __init__(self):
        super().__init__(className="mirage-terminal-settings")
        self.title("Mirage-Terminal settings")
        self.configure(bg=BG)
        self.workdir = tempfile.mkdtemp(prefix="mirage-terminal-settings-")
        self._set_icon()
        with open(SETTINGS, encoding="utf-8") as handle:
            self.text = handle.read()
        self.saved = read_settings(self.text)
        missing = [k for k in DEFAULTS if k not in self.saved]
        if missing:
            messagebox.showerror("Can't open the settings",
                                 "This copy of terminal_palette.py is older than this settings window.\n\n"
                                 "(Missing: %s)" % ", ".join(missing))
            self.destroy()
            raise SystemExit(1)
        self.rc = rc_file()
        self.has_konsole = shutil.which("konsole") is not None
        self.saved["konsole"] = konsole_installed()
        self.saved["on_open"] = bool(self.rc) and rc_has_mirafetch(self.rc)
        self.values = dict(self.saved)
        self._read_world()
        self.results = queue.Queue()
        self.renderer = Renderer(self.results, self.workdir)
        self.renderer.start()
        self.seq, self.pending, self.image = 0, None, None
        self.colour_look = None
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
                make_icon(path, size)
                self.icons.append(tk.PhotoImage(file=path))
            self.iconphoto(True, *self.icons)
        except Exception:                                  # an icon is not worth failing over
            pass

    def _read_world(self):
        """The looks "Panel" and "Mirage" stand for now, and each look's
        background when left on Auto."""
        with LOCK:
            put_settings(self.saved)
            self.follows = {}
            for follow in ("panel", "mirage"):
                T.follow = follow
                self.follows[follow] = T.current_look()
            self.auto_bg = {}
            for look in T.LOOKS:
                setattr(T, look + "_background", "auto")
                self.auto_bg[look] = T.to_hex(T.scheme(look)["bg"])

    # -------------------------------------------------------------- building

    def _section(self, parent, title, row):
        box = tk.Frame(parent, bg=BG)
        box.grid(row=row, column=0, sticky="nsew", pady=(0 if row == 0 else 12, 0))
        head = tk.Frame(box, bg=BG)
        head.pack(fill="x", pady=(2, 6))
        tk.Label(head, text=title.upper(), bg=BG, fg=FAINT, font=(FONT, 8, "bold")).pack(side="left")
        tk.Frame(head, bg=BORDER, height=1).pack(side="left", fill="x", expand=True, padx=(10, 0), pady=(1, 0))
        card = tk.Frame(box, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER, padx=14, pady=12)
        card.pack(fill="both", expand=True)
        card.columnconfigure(0, weight=1)
        return card

    def _switch_row(self, card, row, key, label, note, pady=9):
        line = tk.Frame(card, bg=SURFACE)
        line.grid(row=row, column=0, sticky="ew", pady=(pady, 0))
        name = tk.Label(line, text=label, bg=SURFACE, fg=TEXT, font=F_BODY)
        name.pack(side="left")
        switch = ms.Switch(line, self.values[key], lambda on, k=key: self.set_value(k, on))
        switch.pack(side="right")
        ms.Tooltip(name, note)
        ms.Tooltip(switch, note)
        self.switches[key] = switch
        return name, switch

    def _slider_row(self, card, row, label, low, high, step, value, default, command, note):
        head = tk.Frame(card, bg=SURFACE)
        head.grid(row=row, column=0, sticky="ew", pady=(10, 0))
        name = tk.Label(head, text=label, bg=SURFACE, fg=TEXT, font=F_BODY)
        name.pack(side="left")
        shown = tk.Label(head, text="", bg=SURFACE, fg=ACCENT, font=F_MONO)
        shown.pack(side="right")
        slider = ms.Slider(card, low, high, step, value, default, command, width=SIDE_W - 30)
        slider.grid(row=row + 1, column=0, sticky="w")
        note += "\nDouble-click the slider to put it back."
        ms.Tooltip(name, note)
        ms.Tooltip(slider, note)
        return slider, shown

    def _build(self):
        root = tk.Frame(self, bg=BG, padx=16, pady=14)
        root.pack(fill="both", expand=True)

        # ---- preview
        left = tk.Frame(root, bg=BG)
        left.grid(row=0, column=0, sticky="n")
        tk.Label(left, text="PREVIEW", bg=BG, fg=FAINT, font=(FONT, 8, "bold")).pack(anchor="w")
        frame = tk.Frame(left, bg=SURFACE, highlightthickness=1, highlightbackground=BORDER)
        frame.pack(pady=(6, 0))
        fonts = preview_fonts()
        self.preview_w = PREVIEW_COLS * fonts.cw + 2 * 32 + 2 * 28
        self.preview_h = PREVIEW_ROWS * fonts.ch + 2 * 32 + TP.TITLE_H + 2 * 28
        self.canvas = tk.Canvas(frame, width=self.preview_w, height=self.preview_h, bg=SUNKEN, highlightthickness=0)
        self.canvas.pack()
        self.canvas_text = self.canvas.create_text(self.preview_w // 2, self.preview_h // 2,
                                                   text="Loading the preview…", fill=MUTED, font=F_BODY)
        info = tk.Frame(left, bg=BG)
        info.pack(fill="x", pady=(6, 0))
        self.look_label = tk.Label(info, text="", bg=BG, fg=MUTED, font=F_SMALL)
        self.look_label.pack(side="left")
        reload_button = ms.Button(info, "Reload", self.reload, width=72)
        reload_button.pack(side="right")
        ms.Tooltip(reload_button, "Draw the preview again, for example after you change your wallpaper "
                                  "or the panel's look.")
        self.busy = tk.Label(info, text="", bg=BG, fg=FAINT, font=F_SMALL)
        self.busy.pack(side="right", padx=(0, 10))

        # ---- settings
        side = tk.Frame(root, bg=BG, width=SIDE_W)
        side.grid(row=0, column=1, sticky="n", padx=(16, 0))
        side.columnconfigure(0, weight=1)
        self.switches = {}

        card = self._section(side, "Look", 0)
        self.follow_picker = ms.Segmented(card, FOLLOWS, self.values["follow"],
                                          lambda v: self.set_value("follow", v), width=SIDE_W - 30)
        self.follow_picker.configure(bg=SURFACE)
        self.follow_picker.grid(row=0, column=0, sticky="w")
        ms.Tooltip(self.follow_picker, lambda x: FOLLOW_NOTES[self.follow_picker._index_at(x)])
        self.follow_note = tk.Label(card, text="", bg=SURFACE, fg=MUTED, font=F_SMALL, anchor="nw",
                                    justify="left", wraplength=SIDE_W - 34, height=2)
        self.follow_note.grid(row=1, column=0, sticky="ew", pady=(8, 0))

        self._build_background(side)

        card = self._section(side, "Konsole", 2)
        name, switch = self._switch_row(card, 0, "konsole", "Use in Konsole",
                                        "Make Mirage Konsole's default profile, with your font and the "
                                        "rest of the profile you had. Turn it off to go back to that one.", pady=0)
        if not self.has_konsole:
            switch.configure(cursor="")
            switch.unbind("<Button-1>")
            ms.Tooltip(name, "Konsole isn't installed.")
        self.margin_slider, self.margin_value = self._slider_row(
            card, 1, "Space around the text", 0, 32, 1, self.values["margin"], DEFAULTS["margin"],
            lambda v: self.set_value("margin", int(round(v))),
            "Room between the text and the edges of the window. New Konsole windows get it.")
        self.strength_slider, self.strength_value = self._slider_row(
            card, 3, "Colour strength", 50, 150, 5, round(self.values["colour_strength"] * 100), 100,
            lambda v: self.set_value("colour_strength", round(v) / 100),
            "How colourful the terminal's colours are: softer to the left, more vivid to the right.")

        card = self._section(side, "mirafetch", 3)
        line = tk.Frame(card, bg=SURFACE)
        line.grid(row=0, column=0, sticky="ew")
        tk.Label(line, text="Beside it", bg=SURFACE, fg=TEXT, font=F_BODY).pack(side="left")
        self.beside_picker = ms.Segmented(line, BESIDE, self.values["beside"],
                                          lambda v: self.set_value("beside", v), width=250)
        self.beside_picker.configure(bg=SURFACE)
        self.beside_picker.pack(side="right")
        ms.Tooltip(self.beside_picker, lambda x: BESIDE_NOTES[self.beside_picker._index_at(x)])
        name, switch = self._switch_row(card, 1, "on_open", "Show it when a terminal opens",
                                        "Run mirafetch every time you open a terminal. Adds it to the end of "
                                        "your %s." % ("~/" + os.path.basename(self.rc) if self.rc else
                                                      "shell's start-up file"), pady=12)
        if not self.rc:
            switch.configure(cursor="")
            switch.unbind("<Button-1>")
            ms.Tooltip(name, "Only for bash and zsh. Add mirafetch to your shell's start-up file yourself.")

        # ---- buttons
        bar = tk.Frame(root, bg=BG)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(14, 0))
        self.status = tk.Label(bar, text="", bg=BG, fg=ACCENT, font=F_SMALL, anchor="w", width=90)
        self.status.pack(side="left")
        buttons = [(ms.Button(bar, "Save", self.save, primary=True, width=76),
                    "Save. Open Konsole windows change right away."),
                   (ms.Button(bar, "Defaults", self.defaults, width=84), "Go back to the original settings."),
                   (ms.Button(bar, "Undo", self.revert, width=70), "Undo everything since you last saved.")]
        for i, (button, note) in enumerate(buttons):
            button.pack(side="right", padx=(0, 0 if i == 0 else 8))
            ms.Tooltip(button, note)

    def _build_background(self, side):
        card = self._section(side, "Background", 1)
        self.colour_note = tk.Label(card, text="", bg=SURFACE, fg=MUTED, font=F_SMALL, anchor="w", justify="left",
                                    wraplength=SIDE_W - 34)
        self.colour_note.grid(row=0, column=0, sticky="ew")
        row = tk.Frame(card, bg=SURFACE)
        row.grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.picker = ms.ColourPicker(row, self._picked)
        self.picker.grid(row=0, column=0, rowspan=3, sticky="nw")
        self.hex_entry = tk.Entry(row, width=9, bg=SUNKEN, fg=TEXT, insertbackground=TEXT, relief="flat",
                                  font=F_MONO, highlightthickness=1, highlightbackground=BORDER,
                                  highlightcolor=ACCENT)
        self.hex_entry.grid(row=0, column=1, sticky="nw", padx=(16, 0), ipady=4)
        self.colour_state = tk.Label(row, text="", bg=SURFACE, fg=MUTED, font=F_SMALL, anchor="w", justify="left",
                                     wraplength=150)
        self.colour_state.grid(row=1, column=1, sticky="nw", padx=(16, 0), pady=(8, 0))
        self.hex_entry.bind("<Return>", self._typed)
        self.hex_entry.bind("<FocusOut>", self._typed)
        ms.Tooltip(self.hex_entry, "The colour as a code, like #1e2030. Type one and press Enter.")
        auto = ms.Button(row, "Auto", self._auto, width=78)
        auto.configure(bg=SURFACE)
        auto.grid(row=2, column=1, sticky="nw", padx=(16, 0), pady=(10, 0))
        ms.Tooltip(auto, "Back to the panel's colour, or your wallpaper's.")
        self.see_slider, self.see_value = self._slider_row(
            card, 2, "See-through", 0, 90, 1, 0, 0,
            lambda v: self.set_value(self.effective_look() + "_see_through", round(v) / 100),
            "How much of your desktop shows through the terminal in this look.")
        self._switch_row(card, 4, "blur", "Blur what shows through",
                         "Blur the desktop behind a see-through terminal, as frosted glass. KDE's blur "
                         "effect has to be on.", pady=10)

    # ---------------------------------------------------------- the colours

    def effective_look(self):
        follow = self.values["follow"]
        return follow if follow in T.LOOKS else self.follows.get(follow, "glass")

    def shown_colour(self):
        look = self.effective_look()
        value = self.values.get(look + "_background", "auto")
        return value.lower() if HEX_RE.match(value) else self.auto_bg.get(look, "#202028")

    def _load_picker(self):
        colour = self.shown_colour()
        self.picker.set_hex(colour)
        self.hex_entry.delete(0, "end")
        self.hex_entry.insert(0, colour)

    def _picked(self, colour):
        self.hex_entry.delete(0, "end")
        self.hex_entry.insert(0, colour)
        self.set_value(self.effective_look() + "_background", colour)

    def _typed(self, event=None):
        text = self.hex_entry.get().strip()
        if not text.startswith("#"):
            text = "#" + text
        if HEX_RE.match(text) and text.lower() != self.shown_colour():
            self.picker.set_hex(text.lower())
            self.set_value(self.effective_look() + "_background", text.lower())

    def _auto(self):
        self.set_value(self.effective_look() + "_background", "auto")
        self._load_picker()

    # ------------------------------------------------------------- reactions

    def set_value(self, key, value):
        self.values[key] = value
        self.status.configure(text="")
        self.refresh()
        self.request_render()

    def refresh(self):
        for picker, key in ((self.follow_picker, "follow"), (self.beside_picker, "beside")):
            if picker.value != self.values[key]:
                picker.value = self.values[key]
                picker._draw()
        for key, switch in self.switches.items():
            if switch.value != self.values[key]:
                switch.show(self.values[key])
        index = [v for _, v in FOLLOWS].index(self.values["follow"]) if self.values["follow"] in dict(
            (v, n) for n, v in FOLLOWS) else 0
        look = self.effective_look()
        note = FOLLOW_NOTES[index]
        if self.values["follow"] in ("panel", "mirage"):
            note += " That is %s now." % T.LOOK_NAMES[look]
        self.follow_note.configure(text=note)
        if look != self.colour_look:
            self.colour_look = look
            self._load_picker()
        auto = not HEX_RE.match(self.values.get(look + "_background", "auto"))
        self.colour_note.configure(text="%s's background. Auto takes the panel's colour, or your wallpaper's; "
                                        "the text stays readable on any colour." % T.LOOK_NAMES[look])
        self.colour_state.configure(text="The panel's colour" if auto else "Your own colour")
        key = look + "_see_through"
        self.see_slider.set(round(self.values[key] * 100))
        self.see_slider.default = round(DEFAULTS[key] * 100)
        mark = "• " if self.values[key] != self.saved.get(key) else ""
        self.see_value.configure(text="%s%d%%" % (mark, round(self.values[key] * 100)))
        self.margin_slider.set(self.values["margin"])
        mark = "• " if self.values["margin"] != self.saved["margin"] else ""
        self.margin_value.configure(text="%s%d px" % (mark, self.values["margin"]))
        self.strength_slider.set(round(self.values["colour_strength"] * 100))
        mark = "• " if self.values["colour_strength"] != self.saved["colour_strength"] else ""
        self.strength_value.configure(text="%s%d%%" % (mark, round(self.values["colour_strength"] * 100)))

    def revert(self):
        self.values = dict(self.saved)
        self.colour_look = None
        self.refresh()
        self.request_render()
        self.status.configure(text="Changes undone.", fg=ACCENT)

    def defaults(self):
        self.values.update(DEFAULTS)
        self.colour_look = None
        self.refresh()
        self.request_render()
        self.status.configure(text="The original settings. Save to keep them.", fg=ACCENT)

    # ---------------------------------------------------------------- render

    def reload(self):
        self._read_world()
        self.colour_look = None
        TP.forget_desktop()
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
        self.canvas.create_image((self.preview_w - image.width()) // 2, (self.preview_h - image.height()) // 2,
                                 image=image, anchor="nw", tags="preview")
        self.canvas.itemconfigure(self.canvas_text, state="hidden")
        self.look_label.configure(text="Konsole in %s, running mirafetch, over your wallpaper"
                                       % T.LOOK_NAMES[info["look"]])

    # ---------------------------------------------------------------- saving

    def save(self):
        # The file as it is now, not as it was when the window opened, so a
        # change made to it meanwhile is kept. Only what changed here is written.
        try:
            with open(SETTINGS, encoding="utf-8") as handle:
                self.text = handle.read()
        except OSError as problem:
            messagebox.showerror("Not saved", "Couldn't read the settings.\n\n%s" % problem, parent=self)
            return
        text = apply_settings(self.text, self.values, self.saved)
        notes = []
        try:
            if text != self.text:
                shutil.copy2(SETTINGS, SETTINGS + ".bak")
                with open(SETTINGS, "w", encoding="utf-8") as handle:
                    handle.write(text)
            if self.rc and self.values["on_open"] != self.saved["on_open"]:
                rc_set_mirafetch(self.rc, self.values["on_open"])
                notes.append("mirafetch shows when a terminal opens." if self.values["on_open"] else
                             "mirafetch no longer shows when a terminal opens.")
        except OSError as problem:
            messagebox.showerror("Not saved", "Couldn't save the settings.\n\n%s" % problem, parent=self)
            return
        self.text = text
        tool = os.path.join(HERE, "mirage_konsole.py")
        if self.has_konsole:
            if self.values["konsole"] != self.saved["konsole"]:
                command = "install" if self.values["konsole"] else "uninstall"
                notes.insert(0, "Konsole uses Mirage now." if self.values["konsole"] else
                             "Konsole has its old profile back.")
            else:
                command = "apply" if self.values["konsole"] else None
            if command:
                done = subprocess.run([sys.executable, tool, command, "--quiet"], capture_output=True, text=True,
                                      timeout=30)
                if done.returncode != 0:
                    messagebox.showerror("Konsole wasn't changed", (done.stderr or done.stdout).strip()[-600:],
                                         parent=self)
        self.saved = dict(self.values)
        self.saved.update(read_settings(text))
        self._read_world()
        self.refresh()
        self.status.configure(text=" ".join(["Saved."] + notes), fg=ACCENT)

    def destroy(self):
        shutil.rmtree(getattr(self, "workdir", ""), ignore_errors=True)
        super().destroy()


def main():
    if not os.path.exists(SETTINGS):
        sys.exit("terminal_palette.py is missing. It should be in %s." % HERE)
    Editor().mainloop()


if __name__ == "__main__":
    main()
