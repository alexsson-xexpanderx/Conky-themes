"""The looks for terminal programs: which look is on, the colours Conky-Mirage
took from the wallpaper, and terminal colours made from them.

Used by mirage_konsole.py (Konsole's colours) and mirafetch (the system
summary). Nothing here writes anything.
"""

# ---------------------------------------------------------------- USER CONFIGURATION

# Whose look the terminal follows: "panel" (ConkyTray's, which can itself be
# "Like Mirage"), "mirage" (the widget's), or a look of its own: "glass",
# "solid", "metro" or "pixel".
follow = "pixel"

# The terminal's background in each look: "auto" takes the panel's colour
# (or the wallpaper's), or give a colour of your own, like "#1e2030".
glass_background = "auto"
solid_background = "auto"
metro_background = "#03111a"
pixel_background = "auto"

# How much of the desktop shows through the terminal in each look, from
# 0 (none) to 0.9, and whether what shows through is blurred.
glass_see_through = 0.20
solid_see_through = 0.00
metro_see_through = 0.00
pixel_see_through = 0.00
blur = "yes"

# How colourful the terminal's colours are, from 0.5 (soft) to 1.5 (vivid).
colour_strength = 1.00

# Room around the text in Konsole, in pixels.
margin = 12

# What mirafetch shows beside the facts: "logo" (your system's), "wallpaper"
# or "none".
beside = "logo"

# ----------------------------------------------- DON'T EDIT BELOW IF YOU DO NOT KNOW WHAT YOU ARE DOING

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MIRAGE_DIR = os.path.join(ROOT, "Conky-Mirage")
TRAY_DIR = os.path.join(ROOT, "ConkyTray")
CACHE = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
LOOKS = ("glass", "solid", "metro", "pixel")
LOOK_NAMES = {"glass": "Glass", "solid": "Solid", "metro": "Metro", "pixel": "Pixel"}

sys.path.insert(0, MIRAGE_DIR)
try:
    import mirage as M                  # its colour maths (OKLab), so the colours agree
except ImportError:
    sys.exit("Mirage-Terminal needs Conky-Mirage in the folder next to it (%s)." % MIRAGE_DIR)

# A quiet dark palette for when Mirage has not made one yet.
FALLBACK = {"mode": "dark", "ink": (0.96, 0.96, 0.98), "accent": (0.72, 0.74, 1.0), "accent2": (0.9, 0.7, 0.8),
            "tint": (0.09, 0.09, 0.13), "solid": (0.10, 0.10, 0.14), "metro1": (0.55, 0.22, 0.40),
            "metro2": (0.18, 0.32, 0.58), "metro3": (0.56, 0.30, 0.16), "metro4": (0.10, 0.40, 0.42),
            "px_surface": (0.13, 0.12, 0.15), "px_primary_c": (0.30, 0.20, 0.36), "px_secondary_c": (0.22, 0.20, 0.26),
            "px_tertiary_c": (0.16, 0.26, 0.32), "px_on": (0.93, 0.91, 0.95), "px_primary": (0.84, 0.74, 1.0)}


def lua_setting(path, key):
    """A quoted setting from a USER CONFIGURATION block, or None."""
    try:
        with open(path, encoding="utf-8") as handle:
            m = re.search(r'^%s\s*=\s*"([^"]*)"' % re.escape(key), handle.read(), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


def current_look():
    """The look the terminal wears now, by `follow`."""
    forced = os.environ.get("MIRAGE_TERMINAL_LOOK", "")   # for trying a look, and for tests
    if forced in LOOKS:
        return forced
    mirage_style = lua_setting(os.path.join(MIRAGE_DIR, "mirage.lua"), "style")
    mirage_style = mirage_style if mirage_style in LOOKS else "glass"
    if follow in LOOKS:
        return follow
    if follow == "mirage":
        return mirage_style
    tray = lua_setting(os.path.join(TRAY_DIR, "tray.lua"), "style")
    return tray if tray in LOOKS else mirage_style


def palette():
    """The colours Mirage made from the wallpaper (its widget's, else the
    panel's), as 0-1 RGB triples, with "mode" and "wallpaper"."""
    found = dict(FALLBACK)
    # The newer of the two: with only one of the widgets running, the other's
    # is left from before the wallpaper last changed.
    paths = [os.path.join(CACHE, "conky-mirage", "theme.lua"), os.path.join(CACHE, "conky-tray", "theme", "theme.lua")]
    paths.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except OSError:
            continue
        # Top-level keys come first in Mirage's writer; nested tables repeat
        # some names, so the first of each is the one.
        seen = set()
        for name, r, g, b in re.findall(r"\b(\w+) = \{([\d.]+), ([\d.]+), ([\d.]+)\}", text):
            if name not in seen:
                seen.add(name)
                found[name] = (float(r), float(g), float(b))
        for name in ("mode", "wallpaper"):
            m = re.search(r'\b%s = "([^"]*)"' % name, text)
            if m:
                found[name] = m.group(1)
        break
    return found


def chosen(look, block):
    """The background chosen for this look: the terminal's own setting, else
    the colour chosen in ConkyTray's settings for the look's block (block is
    "apps", "launcher", ... for Metro and Pixel), else None for "auto"."""
    own = str(globals().get(look + "_background", "auto"))
    if re.fullmatch(r"#[0-9a-fA-F]{6}", own):
        return M.hexcolor(own)
    key = look + "_color" if look in ("glass", "solid") else "%s_%s" % (look, block)
    value = lua_setting(os.path.join(TRAY_DIR, "tray.lua"), key) or ""
    return M.hexcolor(value) if re.fullmatch(r"#[0-9a-fA-F]{6}", value) else None


def see_through(look):
    try:
        return max(0.0, min(0.9, float(globals().get(look + "_see_through", 0.0))))
    except (TypeError, ValueError):
        return 0.0


def oklch(rgb):
    return M.oklch(rgb)


def lch(L, C, h):
    return M.oklch_to_rgb(L, C, h)


def readable(hue, chroma, bg, target):
    """The colour of `hue` and `chroma` closest in lightness to the background
    that still reads against it at `target` contrast."""
    dark = M.luminance(bg) < 0.2
    steps = range(30, 99) if dark else range(80, 4, -1)
    rgb = None
    for step in steps:
        rgb = lch(step / 100, chroma, hue)
        if M.contrast(rgb, bg) >= target:
            return rgb
    return rgb


def toward(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def to_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, round(c * 255))) for c in rgb)


# The terminal's sixteen colours keep their meaning (red is still red) and
# are turned a little towards the wallpaper's accent so they belong together.
HUES = {"red": 25, "green": 145, "yellow": 92, "blue": 258, "magenta": 330, "cyan": 200}
ORDER = ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white")
CHROMA = {"glass": 0.14, "solid": 0.13, "metro": 0.10, "pixel": 0.11}


def scheme(look=None, P=None):
    """The terminal colours for a look: background, foreground, the sixteen
    ANSI colours (normal, bright, faint) and how see-through the window is."""
    look = look or current_look()
    P = P or palette()
    if look == "glass":
        bg, fg = chosen(look, None) or P["tint"], P["ink"]
    elif look == "solid":
        bg, fg = chosen(look, None) or P["solid"], P["ink"]
    elif look == "metro":
        # Metro's block colour, deepened: at its own mid lightness the
        # terminal's colours had no room and all came out pastel. A colour
        # chosen in the panel's settings is taken as it is.
        bg, fg = chosen(look, "apps"), (1.0, 1.0, 1.0)
        if bg is None:
            L, C, h = M.oklch(P["metro2"])
            bg = lch(min(L, 0.34), C * 0.9, h)
    else:
        bg, fg = chosen(look, "apps") or P["px_surface"], P["px_on"]
    # A chosen colour, or a light one: the text is what reads on it.
    if M.contrast(fg, bg) < 7:
        h = oklch(P.get("accent", fg))[2]
        fg = readable(h, 0.02, bg, 9)
    accent_h = oklch(P.get("px_primary" if look == "pixel" else "accent", fg))[2]
    try:
        strength = max(0.5, min(1.5, float(colour_strength)))
    except (TypeError, ValueError):
        strength = 1.0
    chroma = CHROMA[look] * strength
    normal, bright = {}, {}
    # On a dark or pale background the bright colours are lighter (or darker)
    # than the normal ones. On Metro's mid-tone blocks there is no room for
    # that, they all came out near white, so there they are stronger instead.
    mid = M.contrast(fg, bg) < 9
    for name, hue in HUES.items():
        h = M.harmonize(hue, accent_h, amount=0.2, limit=14)
        normal[name] = readable(h, chroma * (0.75 if mid else 1), bg, 4.6)
        bright[name] = readable(h, chroma * (1.5 if mid else 0.85), bg, 4.8 if mid else 7.5)
    bg_l, _, bg_h = oklch(bg)
    dark = M.luminance(bg) < 0.2
    # "Black" is a step off the background (what apps paint panels with);
    # bright black is the grey for comments and hints.
    normal["black"] = lch(max(0.0, min(1.0, bg_l + (0.10 if dark else -0.10))), 0.02, bg_h)
    bright["black"] = readable(bg_h, 0.02, bg, 3.2)
    normal["white"] = toward(fg, bg, 0.18)
    bright["white"] = fg
    return {
        "look": look, "bg": bg, "fg": fg,
        "bg_intense": lch(max(0.0, min(1.0, bg_l + (0.03 if dark else -0.03))), oklch(bg)[1], bg_h),
        "normal": [normal[n] for n in ORDER], "bright": [bright[n] for n in ORDER],
        "faint": [toward(normal[n], bg, 0.45) for n in ORDER],
        "opacity": 1.0 - see_through(look), "blur": str(blur).lower() == "yes" and see_through(look) > 0,
        "accent": P.get("px_primary" if look == "pixel" else "accent", fg), "palette": P,
    }
