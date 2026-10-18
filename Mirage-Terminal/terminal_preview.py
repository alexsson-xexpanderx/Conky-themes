#!/usr/bin/env python3
"""A picture of a Konsole window in the look, running mirafetch, over your
wallpaper. The settings window shows it; it can also be run by itself:

    ./terminal_preview.py out.png [glass|solid|metro|pixel]

What is drawn is what mirafetch really prints, read back the way Konsole
reads it (colours, the cursor moves, the picture escape), so it shows the
real thing rather than a copy of it.
"""

import base64
import importlib.machinery
import importlib.util
import io
import os
import re
import subprocess
import sys
import unicodedata

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import terminal_palette as T   # noqa: E402

M = T.M
TITLE_H = 30
CORNER = 9
FONT_FALLBACK = ("DejaVu Sans Mono", 12.0)


def load_mirafetch():
    loader = importlib.machinery.SourceFileLoader("mirafetch", os.path.join(HERE, "mirafetch"))
    spec = importlib.util.spec_from_loader("mirafetch", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


mirafetch = load_mirafetch()


# ------------------------------------------------------------------ the font

def profile_font():
    """The family and size the Mirage profile uses (Konsole's own default
    when there is none)."""
    data = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    try:
        with open(os.path.join(data, "konsole", "Mirage.profile"), encoding="utf-8") as handle:
            m = re.search(r"^Font=([^,\n]+),([\d.]+)", handle.read(), re.M)
        if m:
            return m.group(1), float(m.group(2))
    except OSError:
        pass
    return FONT_FALLBACK


def font_file(family, bold):
    try:
        out = subprocess.run(["fc-match", "-f", "%{file}", family + (":bold" if bold else "")],
                             capture_output=True, text=True, timeout=3).stdout.strip()
        if out:
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    return "/usr/share/fonts/TTF/DejaVuSansMono%s.ttf" % ("-Bold" if bold else "")


class Fonts:
    """The terminal's font at `pt` points on a 96 dpi screen, and the size
    of a character cell in it."""

    def __init__(self, family=None, pt=None):
        found = profile_font()
        family, pt = family or found[0], pt or found[1]
        px = round(pt * 96 / 72)
        self.regular = ImageFont.truetype(font_file(family, False), px)
        self.bold = ImageFont.truetype(font_file(family, True), px)
        ascent, descent = self.regular.getmetrics()
        self.cw = max(1, round(self.regular.getlength("M")))
        self.ch = ascent + descent
        self.ascent = ascent


# ---------------------------------------------------------------- the screen

TOKEN = re.compile(r"\033\[([0-9;?]*)([A-Za-z])|\033\](.*?)(?:\007|\033\\)|\033([78])|([^\033])", re.S)


class Screen:
    """Enough of a terminal to read mirafetch: colours, bold and faint, the
    cursor moves it uses, and pictures sent in line."""

    def __init__(self, cols):
        self.cols = cols
        self.rows = [[]]
        self.x = self.y = 0
        self.saved = (0, 0)
        self.images = []                         # (column, row, cols, rows, PIL image, keep its shape)
        self.reset()

    def reset(self):
        self.fg = self.bg = None
        self.bold = self.faint = False

    def line(self, y):
        while len(self.rows) <= y:
            self.rows.append([])
        return self.rows[y]

    def put(self, ch):
        if ch == "\n":
            self.x, self.y = 0, self.y + 1
            self.line(self.y)
            return
        if ch == "\r":
            self.x = 0
            return
        wide = unicodedata.east_asian_width(ch) in "WF"
        if self.x >= self.cols:
            self.x, self.y = 0, self.y + 1
        row = self.line(self.y)
        while len(row) <= self.x + (1 if wide else 0):
            row.append(None)
        row[self.x] = (ch, self.fg, self.bg, self.bold, self.faint)
        if wide:
            row[self.x + 1] = ("", self.fg, self.bg, self.bold, self.faint)
        self.x += 2 if wide else 1

    def sgr(self, params):
        codes = [int(p) if p.isdigit() else 0 for p in params.split(";")] if params else [0]
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                self.reset()
            elif c == 1:
                self.bold = True
            elif c == 2:
                self.faint = True
            elif c == 22:
                self.bold = self.faint = False
            elif 30 <= c <= 37:
                self.fg = ("normal", c - 30)
            elif 90 <= c <= 97:
                self.fg = ("bright", c - 90)
            elif 40 <= c <= 47:
                self.bg = ("normal", c - 40)
            elif 100 <= c <= 107:
                self.bg = ("bright", c - 100)
            elif c == 39:
                self.fg = None
            elif c == 49:
                self.bg = None
            elif c in (38, 48) and i + 4 < len(codes) and codes[i + 1] == 2:
                colour = tuple(v / 255 for v in codes[i + 2:i + 5])
                if c == 38:
                    self.fg = colour
                else:
                    self.bg = colour
                i += 4
            i += 1

    def picture(self, text):
        m = re.match(r"1337;File=([^:]*):(.*)$", text, re.S)
        if not m:
            return
        args = dict(kv.split("=", 1) for kv in m.group(1).split(";") if "=" in kv)
        try:
            image = Image.open(io.BytesIO(base64.b64decode(m.group(2)))).convert("RGBA")
            cols, rows = int(args.get("width", "0")), int(args.get("height", "0"))
        except (ValueError, OSError):
            return
        if cols and rows:
            self.images.append((self.x, self.y, cols, rows, image, args.get("preserveAspectRatio", "1") != "0"))

    def feed(self, text):
        for m in TOKEN.finditer(text):
            params, final, osc, esc, ch = m.groups()
            if ch is not None:
                self.put(ch)
            elif final == "m":
                self.sgr(params)
            elif final == "A":
                self.y = max(0, self.y - int(params or 1))
            elif final == "B":
                self.y += int(params or 1)
                self.line(self.y)
            elif final == "C":
                self.x = min(self.cols - 1, self.x + int(params or 1))
            elif final == "G":
                self.x = max(0, int(params or 1) - 1)
            elif esc == "7":
                self.saved = (self.x, self.y)
            elif esc == "8":
                self.x, self.y = self.saved
            elif osc is not None:
                self.picture(osc)


# ------------------------------------------------------------------ drawing

BLOCKS = {"▀": ((0, 0, 1, .5),), "▄": ((0, .5, 1, 1),), "█": ((0, 0, 1, 1),), "▌": ((0, 0, .5, 1),),
          "▐": ((.5, 0, 1, 1),), "▖": ((0, .5, .5, 1),), "▗": ((.5, .5, 1, 1),), "▘": ((0, 0, .5, .5),),
          "▝": ((.5, 0, 1, .5),)}
# Box lines as the edges of the cell they reach: left, right, up, down.
LINES = {"─": "lr", "━": "lr", "│": "ud", "├": "udr", "┤": "udl", "╭": "rd", "╮": "ld", "╰": "ru", "╯": "lu"}


def rgb8(c, alpha=255):
    return tuple(max(0, min(255, round(v * 255))) for v in c) + (alpha,)


def colour_of(value, s, bold, faint, default):
    if value is None:
        c = default
    elif isinstance(value[0], str):
        kind, n = value
        c = s["bright" if bold or kind == "bright" else "normal"][n]
        if faint:
            c = s["faint"][n]
    else:
        c = value
    if faint and (value is None or not isinstance(value[0], str)):
        c = T.toward(c, s["bg"], 0.45)
    return c


def draw_line_char(draw, ch, x, y, cw, chh, ink):
    heavy = ch == "━"
    w = max(2, round(chh / 9)) if heavy else max(1, round(cw / 10))
    cx, cy = x + cw // 2, y + chh // 2
    ends = LINES[ch]
    if ch in "╭╮╰╯":
        r = min(cw, chh) // 2
        sx = 1 if "r" in ends else -1
        sy = 1 if "d" in ends else -1
        ox, oy = cx + sx * r, cy + sy * r                  # the arc's centre
        start = {"rd": 180, "ld": 270, "ru": 90, "lu": 0}[ends]
        draw.arc((ox - r, oy - r, ox + r, oy + r), start, start + 90, fill=ink, width=w)
        if sx > 0:
            draw.rectangle((ox, cy - w // 2, x + cw, cy - w // 2 + w - 1), fill=ink)
        else:
            draw.rectangle((x, cy - w // 2, ox, cy - w // 2 + w - 1), fill=ink)
        if sy > 0:
            draw.rectangle((cx - w // 2, oy, cx - w // 2 + w - 1, y + chh), fill=ink)
        else:
            draw.rectangle((cx - w // 2, y, cx - w // 2 + w - 1, oy), fill=ink)
        return
    if "l" in ends:
        draw.rectangle((x, cy - w // 2, cx, cy - w // 2 + w - 1), fill=ink)
    if "r" in ends:
        draw.rectangle((cx, cy - w // 2, x + cw, cy - w // 2 + w - 1), fill=ink)
    if "u" in ends:
        draw.rectangle((cx - w // 2, y, cx - w // 2 + w - 1, cy), fill=ink)
    if "d" in ends:
        draw.rectangle((cx - w // 2, cy, cx - w // 2 + w - 1, y + chh), fill=ink)


def terminal(screen, s, fonts, rows, alpha):
    """The terminal's text area: the background at `alpha` where it is the
    default one (as Konsole makes only that see-through), the rest opaque."""
    cw, chh = fonts.cw, fonts.ch
    W, H = screen.cols * cw, rows * chh
    out = Image.new("RGBA", (W, H), rgb8(s["bg"], round(alpha * 255)))
    draw = ImageDraw.Draw(out)
    plain_bg = rgb8(s["bg"])
    for y, row in enumerate(screen.rows[:rows]):
        for x, cell in enumerate(row):
            if cell and cell[2] is not None:
                c = rgb8(colour_of(cell[2], s, False, False, s["bg"]))
                if c != plain_bg:
                    draw.rectangle((x * cw, y * chh, (x + 1) * cw - 1, (y + 1) * chh - 1), fill=c)
    for col, row, cols, nrows, image, keep in screen.images:
        box = (cols * cw, nrows * chh)
        if keep:
            fitted = ImageOps.contain(image, box, Image.LANCZOS)
        else:
            fitted = image.resize(box, Image.LANCZOS)
        if row < rows:
            out.alpha_composite(fitted, (col * cw, row * chh))
    draw = ImageDraw.Draw(out)
    for y, row in enumerate(screen.rows[:rows]):
        for x, cell in enumerate(row):
            if not cell or not cell[0] or cell[0] == " ":
                continue
            ch, fg, bg, bold, faint = cell
            ink = rgb8(colour_of(fg, s, bold, faint, s["fg"]))
            px, py = x * cw, y * chh
            if ch in BLOCKS:
                for a, b, c, d in BLOCKS[ch]:
                    draw.rectangle((px + round(a * cw), py + round(b * chh), px + round(c * cw) - 1,
                                    py + round(d * chh) - 1), fill=ink)
            elif ch in LINES:
                draw_line_char(draw, ch, px, py, cw, chh, ink)
            else:
                draw.text((px, py + fonts.ascent), ch, font=fonts.bold if bold else fonts.regular, fill=ink,
                          anchor="ls")
    return out


_desktops = {}


def forget_desktop():
    _desktops.clear()


def desktop(wallpaper, size):
    """The wallpaper cut to `size`, kept for the next picture of that size."""
    try:
        key = (wallpaper, os.path.getmtime(wallpaper), size)
    except (OSError, TypeError):
        key = (None, 0, size)
    if key not in _desktops:
        try:
            with Image.open(wallpaper) as picture:
                picture.draft("RGB", (size[0] * 2, size[1] * 2))
                _desktops[key] = ImageOps.fit(picture.convert("RGB"), size, Image.LANCZOS).convert("RGBA")
        except (OSError, ValueError, AttributeError, TypeError):
            ground = Image.linear_gradient("L").resize(size)
            _desktops[key] = Image.merge("RGB", [ground.point(lambda v, a=a: a + v // 6)
                                                 for a in (24, 26, 40)]).convert("RGBA")
    return _desktops[key].copy()


def window(look, session, cols, rows, margin, see_through, blur, fonts=None, pad=28):
    """Konsole showing `session` (what was printed), over the wallpaper."""
    fonts = fonts or Fonts()
    s = mirafetch.colours(look)
    screen = Screen(cols)
    screen.feed(session)
    text = terminal(screen, s, fonts, rows, 1.0 - see_through)
    ww, wh = text.width + 2 * margin, text.height + 2 * margin + TITLE_H
    W, H = ww + 2 * pad, wh + 2 * pad
    canvas = desktop(s["palette"].get("wallpaper"), (W, H))
    x0, y0 = pad, pad
    shape = Image.new("L", (W, H), 0)
    ImageDraw.Draw(shape).rounded_rectangle((x0, y0, x0 + ww - 1, y0 + wh - 1), radius=CORNER, fill=255)
    shadow = shape.filter(ImageFilter.GaussianBlur(12)).point(lambda v: v * 0.55)
    shadow = shadow.transform(shadow.size, Image.AFFINE, (1, 0, 0, 0, 1, -5))
    canvas.paste((0, 0, 0, 255), (0, 0), shadow)
    if see_through > 0 and blur:
        canvas.paste(canvas.filter(ImageFilter.GaussianBlur(16)), (0, 0), shape)
    body = Image.new("RGBA", (ww, wh - TITLE_H), rgb8(s["bg"], round((1 - see_through) * 255)))
    body.alpha_composite(text, (margin, margin))
    pane = canvas.copy()
    pane.alpha_composite(body, (x0, y0 + TITLE_H))
    # The title bar, in a plain dark grey like Breeze's.
    bar = ImageDraw.Draw(pane)
    bar.rectangle((x0, y0, x0 + ww - 1, y0 + TITLE_H - 1), fill=(39, 42, 46, 255))
    title = "%s : ~ — Konsole" % (os.environ.get("USER") or "you")
    small = ImageFont.truetype(font_file("DejaVu Sans", False), 13)
    bar.text((x0 + ww / 2, y0 + TITLE_H / 2), title, font=small, fill=(220, 223, 228, 255), anchor="mm")
    # Breeze's buttons: close, maximise, minimise, from the right.
    cy, grey = y0 + TITLE_H / 2, (200, 204, 210, 255)
    cx = x0 + ww - 20
    bar.line((cx - 4, cy - 4, cx + 4, cy + 4), fill=grey, width=2)
    bar.line((cx - 4, cy + 4, cx + 4, cy - 4), fill=grey, width=2)
    cx -= 26
    bar.line((cx - 5, cy + 2, cx, cy - 3, cx + 5, cy + 2), fill=grey, width=2)
    cx -= 26
    bar.line((cx - 5, cy - 2, cx, cy + 3, cx + 5, cy - 2), fill=grey, width=2)
    canvas.paste(pane, (0, 0), shape)
    edge = Image.new("L", (W, H), 0)
    ImageDraw.Draw(edge).rounded_rectangle((x0, y0, x0 + ww - 1, y0 + wh - 1), radius=CORNER, outline=70)
    canvas.paste((255, 255, 255, 255), (0, 0), edge)
    return canvas.convert("RGB")


def konsole_version():
    try:
        out = subprocess.run(["konsole", "--version"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return "Konsole"
    m = re.search(r"(\d+\.\d+(\.\d+)?)", out)
    return "Konsole " + m.group(1) if m else "Konsole"


def preview_facts():
    """mirafetch's facts, the terminal being the Konsole in the picture
    rather than whatever runs this."""
    return [(k, konsole_version() if k == "Terminal" else v) for k, v in mirafetch.facts()]


def session_text(look, cols, rows, beside, fonts, known=None):
    """A short terminal session: mirafetch, then a listing, then the prompt."""
    host = os.uname().nodename.split(".")[0]
    prompt = "%s@%s:~$ " % (os.environ.get("USER") or "you", host)
    out = prompt + "mirafetch\n"
    out += mirafetch.compose(look, cols, rows, tty=True, images=True, cell=(fonts.cw, fonts.ch), beside=beside,
                             known=known if known is not None else preview_facts())
    listing = [("01;34", "Documents"), ("01;34", "Music"), ("01;34", "Pictures"), ("0", "notes.txt"),
               ("01;32", "build.sh"), ("01;35", "photo.jpg"), ("01;31", "backup.tar.gz"), ("01;36", "latest")]
    out += prompt + "ls\n" + "  ".join("\033[%sm%s\033[0m" % kv for kv in listing) + "\n"
    out += prompt + "█"
    return out


def render(path, look, cols=100, rows=28, beside="logo", margin=12, see_through=None, blur=None, known=None):
    fonts = Fonts()
    see_through = T.see_through(look) if see_through is None else see_through
    blur = str(T.blur).lower() == "yes" if blur is None else blur
    session = session_text(look, cols, rows, beside, fonts, known)
    window(look, session, cols, rows, margin, see_through, blur, fonts).save(path)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__.strip())
        sys.exit(1)
    chosen = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] in T.LOOKS else T.current_look()
    render(sys.argv[1], chosen, beside=T.beside, margin=int(T.margin))
