#!/usr/bin/env python3
"""
Conky-Mirage helper: the wallpaper, the glass and the weather.

mirage.lua runs this as a one-shot every few seconds, detached, with every
setting on the command line -- the USER CONFIGURATION block in mirage.lua is
the only place anything is configured. Each run does two things and exits:

  theme    Work out which wallpaper is on screen and exactly where the widget
           sits on it. When either has changed, derive a palette from the
           wallpaper and render each tile as frosted glass: the patch of
           wallpaper behind it, blurred, tinted until text on it is legible,
           and edged with a hairline highlight.
  weather  When the forecast is older than the refresh interval, fetch a new
           one from Open-Meteo, which needs no API key.

Results land in the cache directory as Lua tables and PNGs, and `serial` is
bumped so the widget knows to reload. A run that finds nothing to do touches
no image library and costs a few milliseconds, which is what makes running it
this often affordable.

Standard library plus Pillow. `./mirage.py doctor` prints what it detects.
"""

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time

VERSION = 3          # bump to force every cache to re-render
USER_AGENT = "conky-mirage/1.0 (+https://github.com/alexsson-xexpanderx/Conky-themes)"


# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------

def log(cache, message):
    """Append to mirage.log, keeping it from growing without bound."""
    path = os.path.join(cache, "mirage.log")
    try:
        if os.path.getsize(path) > 200_000:
            os.replace(path, path + ".old")
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message.rstrip() + "\n")


def write_atomically(path, data):
    """Write via a temporary file and rename, so the widget never reads half a file."""
    tmp = path + ".tmp"
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(tmp, mode, **({} if mode == "wb" else {"encoding": "utf-8"})) as handle:
        handle.write(data)
    os.replace(tmp, path)


def run(command, timeout=4):
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
        return done.stdout if done.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def lua_string(text):
    out = []
    for ch in text:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append("\\%03d" % ord(ch))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def lua_value(value):
    """Serialise to a Lua literal. Strings come from web APIs, so escape fully."""
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return "0"
        return repr(round(value, 5))
    if isinstance(value, str):
        return lua_string(value)
    if isinstance(value, (list, tuple)):
        return "{" + ", ".join(lua_value(v) for v in value) + "}"
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            if item is None:
                continue
            name = key if IDENTIFIER.match(str(key)) else "[" + lua_string(str(key)) + "]"
            parts.append(name + " = " + lua_value(item))
        return "{" + ", ".join(parts) + "}"
    raise TypeError(type(value))


def bump_serial(cache):
    path = os.path.join(cache, "serial")
    try:
        with open(path, encoding="utf-8") as handle:
            serial = int(handle.read().strip() or 0)
    except (OSError, ValueError):
        serial = 0
    write_atomically(path, str(serial + 1) + "\n")


def load_state(cache):
    try:
        with open(os.path.join(cache, "state.json"), encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_state(cache, state):
    write_atomically(os.path.join(cache, "state.json"), json.dumps(state, indent=1))


# --------------------------------------------------------------------------
# colour science: OKLab, because "the same lightness" has to mean the same to
# the eye across hues, or a palette lifted from a wallpaper comes out with one
# colour glaring and the next one muddy
# --------------------------------------------------------------------------

def to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def to_gamma(c):
    c = max(0.0, min(1.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def rgb_to_oklab(rgb):
    r, g, b = (to_linear(c) for c in rgb)
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l, m, s = (math.copysign(abs(x) ** (1 / 3), x) for x in (l, m, s))
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def oklab_to_linear(lab):
    L, a, b = lab
    l = (L + 0.3963377774 * a + 0.2158037573 * b) ** 3
    m = (L - 0.1055613458 * a - 0.0638541728 * b) ** 3
    s = (L - 0.0894841775 * a - 1.2914855480 * b) ** 3
    return (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
            -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
            -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)


def oklch_to_rgb(L, C, h):
    """OKLCH to sRGB, pulling chroma in until the colour exists in sRGB."""
    def linear(c):
        return oklab_to_linear((L, c * math.cos(math.radians(h)), c * math.sin(math.radians(h))))
    lin = linear(C)
    if any(x < -1e-4 or x > 1 + 1e-4 for x in lin):
        low, high = 0.0, C
        for _ in range(20):
            mid = (low + high) / 2
            if any(x < -1e-4 or x > 1 + 1e-4 for x in linear(mid)):
                high = mid
            else:
                low = mid
        lin = linear(low)
    return tuple(to_gamma(x) for x in lin)


def oklch(rgb):
    L, a, b = rgb_to_oklab(rgb)
    return L, math.hypot(a, b), math.degrees(math.atan2(b, a)) % 360


def luminance(rgb):
    r, g, b = (to_linear(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def hue_distance(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def harmonize(hue, towards, amount=0.3, limit=28):
    """Rotate a hue part of the way towards another, Material You style."""
    delta = ((towards - hue + 540) % 360) - 180
    return (hue + max(-limit, min(limit, delta * amount))) % 360


def mix(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


def hexcolor(text):
    text = text.strip().lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", text):
        return None
    return tuple(int(text[i:i + 2], 16) / 255 for i in (0, 2, 4))


# --------------------------------------------------------------------------
# where is the widget, and on which monitor
# --------------------------------------------------------------------------

def window_geometry(title):
    out = run(["xwininfo", "-name", title])
    if not out:
        return None
    values = {}
    for key, pattern in (("x", r"Absolute upper-left X:\s+(-?\d+)"),
                         ("y", r"Absolute upper-left Y:\s+(-?\d+)"),
                         ("w", r"Width:\s+(\d+)"), ("h", r"Height:\s+(\d+)")):
        found = re.search(pattern, out)
        if not found:
            return None
        values[key] = int(found.group(1))
    return values


def monitors():
    out = run(["xrandr", "--listmonitors"])
    found = []
    for line in out.splitlines():
        m = re.search(r"(\d+)/\d+x(\d+)/\d+\+(-?\d+)\+(-?\d+)\s+(\S+)\s*$", line)
        if m:
            w, h, x, y = (int(m.group(i)) for i in range(1, 5))
            found.append({"x": x, "y": y, "w": w, "h": h, "name": m.group(5),
                          "primary": "*" in line.split(":", 1)[-1].split()[0]})
    return found


def monitor_for(window, screens):
    """The monitor holding the widget's centre; Plasma numbers the primary 0."""
    if not screens:
        return None, 0
    cx, cy = window["x"] + window["w"] / 2, window["y"] + window["h"] / 2
    best, best_index = screens[0], 0
    best_distance = float("inf")
    for index, mon in enumerate(screens):
        dx = max(mon["x"] - cx, 0, cx - (mon["x"] + mon["w"]))
        dy = max(mon["y"] - cy, 0, cy - (mon["y"] + mon["h"]))
        if dx * dx + dy * dy < best_distance:
            best, best_index, best_distance = mon, index, dx * dx + dy * dy
    return best, best_index


# --------------------------------------------------------------------------
# which wallpaper is on screen, and how it is fitted
# --------------------------------------------------------------------------

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".jxl", ".avif", ".tif", ".tiff", ".svg")


def strip_uri(path):
    path = path.strip().strip("'\"")
    if path.startswith("file://"):
        from urllib.parse import unquote
        path = unquote(path[7:])
    return os.path.expanduser(path)


def parse_kde_ini(path):
    """KDE's nested [A][B][C] groups, which configparser mangles."""
    sections, current = {}, None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.rstrip("\n")
                if line.startswith("["):
                    current = tuple(re.findall(r"\[([^\]]*)\]", line))
                    sections.setdefault(current, {})
                elif current is not None and "=" in line:
                    key, value = line.split("=", 1)
                    sections[current][key.strip()] = value
    except OSError:
        pass
    return sections


def kde_is_dark():
    globals_ = parse_kde_ini(os.path.expanduser("~/.config/kdeglobals"))
    background = globals_.get(("Colors:Window",), {}).get("BackgroundNormal", "")
    parts = [int(p) for p in background.split(",")[:3] if p.strip().isdigit()]
    if len(parts) == 3:
        return luminance(tuple(p / 255 for p in parts)) < 0.3
    return "dark" in globals_.get(("General",), {}).get("ColorScheme", "").lower()


def package_image(directory, target_w, target_h):
    """Pick the image Plasma would pick from a wallpaper package.

    Mirrors plasma-workspace's findPreferredImageInPackage: aspect ratio
    dominates, then size, and an image that would have to be scaled up is
    penalised twice as hard as one scaled down.
    """
    folders = ["images"]
    if os.path.isdir(os.path.join(directory, "contents", "images_dark")) and kde_is_dark():
        folders.insert(0, "images_dark")
    for folder in folders:
        root = os.path.join(directory, "contents", folder)
        try:
            names = os.listdir(root)
        except OSError:
            continue
        best, best_distance, best_bigger = None, None, False
        target_ratio = target_w / max(target_h, 1)
        for name in names:
            m = re.match(r"^(\d+)x(\d+)\.", name)
            if not m or not name.lower().endswith(IMAGE_EXTENSIONS):
                continue
            w, h = int(m.group(1)), int(m.group(2))
            delta = w - target_w
            delta = delta if delta >= 0 else -delta * 2
            distance = abs(w / h - target_ratio) * 25000 + delta
            bigger = w >= target_w and h >= target_h
            if best is None or (bigger and (not best_bigger or distance < best_distance)) \
                    or (not best_bigger and distance < best_distance):
                best, best_distance, best_bigger = os.path.join(root, name), distance, bigger
        if best:
            return best
        images = sorted(n for n in names if n.lower().endswith(IMAGE_EXTENSIONS))
        if images:
            return os.path.join(root, images[-1])
    return None


KDE_FILL = {"0": "stretch", "1": "fit", "2": "crop", "3": "tile",
            "4": "tile", "5": "tile", "6": "center"}


def kde_live_wallpaper(screen_index):
    """Ask Plasma itself. Its config file is only written some time after a
    change, while this answers at once, and costs a few milliseconds."""
    out = run(["gdbus", "call", "--session", "--dest", "org.kde.plasmashell", "--object-path",
               "/PlasmaShell", "--method", "org.kde.PlasmaShell.wallpaper", str(screen_index)], timeout=2)
    image = re.search(r"'Image': <'([^']*)'>", out)
    if not image:
        return None
    fill = re.search(r"'FillMode': <(\d+)>", out)
    color = re.search(r"'Color': <\(uint32 (\d+),\)>", out)
    argb = int(color.group(1)) if color else 0
    return {"image": strip_uri(image.group(1)), "fill": fill.group(1) if fill else "2",
            "background": tuple(((argb >> shift) & 255) / 255 for shift in (16, 8, 0))}


def kde_wallpaper(screen_index, target_w, target_h):
    live = kde_live_wallpaper(screen_index)
    if live and live["image"]:
        image = live["image"]
        if os.path.isdir(image):
            image = package_image(image, target_w, target_h)
        if image and os.path.isfile(image):
            return {"path": image, "fill": KDE_FILL.get(live["fill"], "crop"), "background": live["background"]}

    config = parse_kde_ini(os.path.expanduser("~/.config/plasma-org.kde.plasma.desktop-appletsrc"))
    activity = parse_kde_ini(os.path.expanduser("~/.config/kactivitymanagerdrc")) \
        .get(("main",), {}).get("currentActivity", "")

    desktops = []
    for section, values in config.items():
        if len(section) == 2 and section[0] == "Containments" and values.get("location", "0") == "0" \
                and values.get("plugin", "") in ("org.kde.plasma.folder", "org.kde.desktopcontainment"):
            desktops.append((section[1], values))
    if not desktops:
        return None

    def rank(entry):
        _, values = entry
        return (values.get("lastScreen") == str(screen_index),
                not activity or values.get("activityId") == activity)
    cid, values = max(desktops, key=rank)

    plugin = values.get("wallpaperplugin", "org.kde.image")
    general = config.get(("Containments", cid, "Wallpaper", plugin, "General"), {})
    color = general.get("Color", "0,0,0")
    try:
        background = tuple(int(c) / 255 for c in color.split(",")[:3])
    except ValueError:
        background = (0, 0, 0)

    if plugin == "org.kde.color":
        return {"solid": background}
    if plugin != "org.kde.image":
        # A slideshow does not record which image it is showing, so glass
        # would show the wrong picture half the time. Say so and let the
        # widget fall back to tinted tiles.
        return {"unsupported": plugin}

    image = strip_uri(general.get("Image", ""))
    if not image:
        for fallback in ("/usr/share/wallpapers/Next", "/usr/share/wallpapers/Flow"):
            if os.path.isdir(fallback):
                image = fallback
                break
    if os.path.isdir(image):
        image = package_image(image, target_w, target_h)
    if not image or not os.path.isfile(image):
        return None
    return {"path": image, "fill": KDE_FILL.get(general.get("FillMode", "2"), "crop"),
            "background": background}


GNOME_FILL = {"zoom": "crop", "scaled": "fit", "stretched": "stretch", "centered": "center",
              "wallpaper": "tile", "spanned": "crop", "none": "crop"}


def gsettings(schema, key):
    out = run(["gsettings", "get", schema, key]).strip()
    return out.strip("'\"") if out else ""


def gnome_wallpaper():
    for schema in ("org.gnome.desktop.background", "org.cinnamon.desktop.background"):
        uri = ""
        if gsettings("org.gnome.desktop.interface", "color-scheme") == "prefer-dark":
            uri = gsettings(schema, "picture-uri-dark")
        uri = uri or gsettings(schema, "picture-uri")
        path = strip_uri(uri) if uri else ""
        if path.endswith(".xml"):
            # A timed slideshow; its first <file> is at least the right set.
            try:
                with open(path, encoding="utf-8", errors="replace") as handle:
                    m = re.search(r"<file>([^<]+)</file>", handle.read())
                path = m.group(1).strip() if m else ""
            except OSError:
                path = ""
        if path and os.path.isfile(path):
            fill = GNOME_FILL.get(gsettings(schema, "picture-options"), "crop")
            return {"path": path, "fill": fill, "background": (0, 0, 0)}
    path = gsettings("org.mate.background", "picture-filename")
    if path and os.path.isfile(path):
        return {"path": path, "fill": "crop", "background": (0, 0, 0)}
    return None


def xfce_wallpaper(monitor_name):
    listing = run(["xfconf-query", "-c", "xfce4-desktop", "-l"])
    keys = [k for k in listing.split() if k.endswith("/last-image")]
    keys.sort(key=lambda k: (monitor_name not in k, "workspace0" not in k))
    for key in keys:
        path = run(["xfconf-query", "-c", "xfce4-desktop", "-p", key]).strip()
        if path and os.path.isfile(path):
            style = run(["xfconf-query", "-c", "xfce4-desktop", "-p",
                         key.replace("last-image", "image-style")]).strip()
            fill = {"1": "center", "2": "tile", "3": "stretch", "4": "fit", "5": "crop"}.get(style, "crop")
            return {"path": path, "fill": fill, "background": (0, 0, 0)}
    return None


def process_args(name):
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                args = handle.read().split(b"\0")
        except OSError:
            continue
        if args and os.path.basename(args[0].decode(errors="replace")) == name:
            return [a.decode(errors="replace") for a in args if a]
    return None


def standalone_wallpaper():
    """Setters used outside the big desktops: feh, nitrogen, swaybg, swww, hyprpaper, pywal."""
    fehbg = os.path.expanduser("~/.fehbg")
    if os.path.isfile(fehbg):
        with open(fehbg, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "feh" in line and "--bg" in line:
                    try:
                        words = shlex.split(line)
                    except ValueError:
                        continue
                    mode = next((w for w in words if w.startswith("--bg-")), "--bg-fill")
                    images = [w for w in words if os.path.isfile(os.path.expanduser(w))]
                    if images:
                        fill = {"--bg-fill": "crop", "--bg-scale": "stretch", "--bg-max": "fit",
                                "--bg-center": "center", "--bg-tile": "tile"}.get(mode, "crop")
                        return {"path": os.path.expanduser(images[0]), "fill": fill, "background": (0, 0, 0)}
    nitrogen = os.path.expanduser("~/.config/nitrogen/bg-saved.cfg")
    if os.path.isfile(nitrogen):
        with open(nitrogen, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        m = re.search(r"^file=(.+)$", text, re.M)
        if m and os.path.isfile(m.group(1).strip()):
            return {"path": m.group(1).strip(), "fill": "crop", "background": (0, 0, 0)}
    args = process_args("swaybg")
    if args and "-i" in args:
        path = args[args.index("-i") + 1]
        mode = args[args.index("-m") + 1] if "-m" in args else "fill"
        fill = {"fill": "crop", "stretch": "stretch", "fit": "fit", "center": "center", "tile": "tile"}.get(mode, "crop")
        if os.path.isfile(path):
            return {"path": path, "fill": fill, "background": (0, 0, 0)}
    out = run(["swww", "query"])
    m = re.search(r"image:\s*(.+)$", out, re.M)
    if m and os.path.isfile(m.group(1).strip()):
        return {"path": m.group(1).strip(), "fill": "crop", "background": (0, 0, 0)}
    hyprpaper = os.path.expanduser("~/.config/hypr/hyprpaper.conf")
    if os.path.isfile(hyprpaper):
        with open(hyprpaper, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                m = re.match(r"\s*wallpaper\s*=\s*[^,]*,\s*(.+)$", line)
                if m and os.path.isfile(os.path.expanduser(m.group(1).strip())):
                    return {"path": os.path.expanduser(m.group(1).strip()), "fill": "crop", "background": (0, 0, 0)}
    wal = os.path.expanduser("~/.cache/wal/wal")
    if os.path.isfile(wal):
        with open(wal, encoding="utf-8", errors="replace") as handle:
            path = handle.read().strip()
        if os.path.isfile(path):
            return {"path": path, "fill": "crop", "background": (0, 0, 0)}
    return None


def find_wallpaper(setting, screen_index, monitor):
    if setting and setting != "auto":
        path = strip_uri(setting)
        if os.path.isdir(path):
            path = package_image(path, monitor["w"], monitor["h"])
        if path and os.path.isfile(path):
            return {"path": path, "fill": "crop", "background": (0, 0, 0), "source": "setting"}
        return None
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    order = []
    if "kde" in desktop or "plasma" in desktop:
        order.append(("plasma", lambda: kde_wallpaper(screen_index, monitor["w"], monitor["h"])))
    if any(d in desktop for d in ("gnome", "cinnamon", "mate", "budgie", "unity", "pantheon")):
        order.append(("gnome", gnome_wallpaper))
    if "xfce" in desktop:
        order.append(("xfce", lambda: xfce_wallpaper(monitor.get("name", ""))))
    order.append(("standalone", standalone_wallpaper))
    if not any(name == "plasma" for name, _ in order) and \
            os.path.isfile(os.path.expanduser("~/.config/plasma-org.kde.plasma.desktop-appletsrc")):
        order.append(("plasma", lambda: kde_wallpaper(screen_index, monitor["w"], monitor["h"])))
    for name, finder in order:
        found = finder()
        if found:
            found["source"] = name
            return found
    return None


# --------------------------------------------------------------------------
# palette and glass
# --------------------------------------------------------------------------

def fitted_screen(Image, wallpaper, width, height):
    """The wallpaper as the desktop draws it on a width x height monitor."""
    if "solid" in wallpaper:
        return Image.new("RGB", (width, height), tuple(int(c * 255) for c in wallpaper["solid"]))
    source = wallpaper["path"]
    if source.lower().endswith(".svg"):
        png = os.path.join(os.environ.get("MIRAGE_CACHE", "/tmp"), "wallpaper-svg.png")
        for command in (["rsvg-convert", "-w", str(width), "-h", str(height), "-a", "-o", png, source],
                        ["magick", "-background", "none", source, "-resize", f"{width}x{height}^", png]):
            subprocess.run(command, capture_output=True, timeout=30)
            if os.path.isfile(png):
                source = png
                break
    img = Image.open(source)
    img.draft("RGB", (width, height))    # lets JPEG decode at a reduced size
    img = img.convert("RGB")
    iw, ih = img.size
    background = tuple(int(c * 255) for c in wallpaper.get("background", (0, 0, 0)))
    fill = wallpaper.get("fill", "crop")
    if fill == "stretch":
        return img.resize((width, height), Image.LANCZOS)
    if fill == "crop":
        scale = max(width / iw, height / ih)
        bw, bh = width / scale, height / scale
        box = ((iw - bw) / 2, (ih - bh) / 2, (iw + bw) / 2, (ih + bh) / 2)
        return img.resize((width, height), Image.LANCZOS, box=box)
    canvas = Image.new("RGB", (width, height), background)
    if fill == "tile":
        for ty in range(0, height, ih):
            for tx in range(0, width, iw):
                canvas.paste(img, (tx, ty))
        return canvas
    if fill == "fit":
        scale = min(width / iw, height / ih)
        img = img.resize((max(1, round(iw * scale)), max(1, round(ih * scale))), Image.LANCZOS)
    canvas.paste(img, ((width - img.size[0]) // 2, (height - img.size[1]) // 2))
    return canvas


def cluster_colors(Image, img, count=14):
    """Dominant colours with their share of the image, via Pillow's median cut."""
    small = img.copy()
    small.thumbnail((220, 220), Image.BILINEAR)
    # Image.Quantize arrived in Pillow 9.1; Ubuntu 22.04 still ships 9.0.
    median_cut = getattr(getattr(Image, "Quantize", None), "MEDIANCUT", None)
    quantized = small.quantize(colors=count, method=median_cut if median_cut is not None else 0)
    palette = quantized.getpalette()
    total = small.size[0] * small.size[1]
    colors = []
    for share, index in quantized.getcolors(count * 4) or []:
        rgb = tuple(palette[index * 3 + i] / 255 for i in range(3))
        colors.append((share / total, rgb))
    colors.sort(reverse=True)
    return colors


def choose_accents(colors, dark, forced=None):
    """The wallpaper's most characterful colours, re-lit to sit on the glass.

    Vividness counts for more than area -- a small saturated sun says more
    about a wallpaper than a large field of near-black -- but a speck of
    colour that is barely there is not allowed to define the whole widget.
    """
    lit, max_chroma = (0.80, 0.15) if dark else (0.52, 0.16)
    candidates = []
    for share, rgb in colors:
        L, C, h = oklch(rgb)
        if C < 0.035 or share < 0.004:
            continue
        light_ok = min(1.0, max(0.15, (L - 0.12) / 0.4))
        candidates.append((C ** 1.4 * share ** 0.35 * light_ok, C, h))
    candidates.sort(reverse=True)

    if forced:
        _, fc, fh = oklch(forced)
        primary = (fh, max(fc, 0.08))
    elif candidates:
        primary = (candidates[0][2], candidates[0][1])
    else:
        primary = (265.0, 0.05)    # a grey wallpaper: a quiet periwinkle reads as neutral

    secondary = None
    for _, c, h in candidates:
        if hue_distance(h, primary[0]) >= 45:
            secondary = (h, c)
            break
    if secondary is None:
        secondary = ((primary[0] + 55) % 360, primary[1])

    def relight(hue, chroma, lightness=lit):
        return oklch_to_rgb(lightness, max(0.075, min(max_chroma, chroma * 1.1)), hue)

    return relight(*primary), relight(*secondary), primary[0], secondary[0], \
        [(round(h), round(c, 3)) for _, c, h in candidates[:8]]


SEMANTIC = {        # hue in OKLCH, before harmonising
    "sun": 82, "rain": 238, "good": 150, "fair": 95, "poor": 60, "bad": 25, "severe": 350,
}


def temperature_ramp(hues, accent_hue, dark):
    """Cold to hot, in the wallpaper's own colours.

    The ends are the wallpaper's coolest and warmest hues -- the ones nearest a
    sky blue and a sunset orange -- and the ramp runs between them the way
    round that passes the accent, so a lavender-and-coral wallpaper gets a
    lavender, pink, coral ramp rather than one through cyan and green. Where
    the wallpaper has no colour near either end, the semantic hue is used,
    pulled a little towards it, so cold still reads as cold.
    """
    def anchor(semantic):
        near = min(hues, key=lambda h: hue_distance(h, semantic)) if hues else semantic
        if hue_distance(near, semantic) <= 70:
            return near
        return harmonize(semantic, near, 0.3, 25)

    cold, hot = anchor(245), anchor(50)
    if hue_distance(cold, hot) < 60:            # one-colour wallpaper: open the ramp up
        cold, hot = harmonize(245, cold, 0.3, 25), harmonize(50, hot, 0.3, 25)
    up = (hot - cold) % 360                     # going round by increasing hue
    span = up if (accent_hue - cold) % 360 <= up else up - 360
    if abs(span) > 200:
        span = span - 360 if span > 0 else span + 360
    # A ramp narrower than this cannot tell a cool day from a mild one.
    if abs(span) < 170:
        widen = (170 - abs(span)) / 2
        direction = 1 if span >= 0 else -1
        cold -= direction * widen
        span += direction * 2 * widen

    L = 0.80 if dark else 0.56
    stops = []
    # Spread so that everyday temperatures, 5 to 25°, cover most of the hues.
    for t, f in ((-25, -0.25), (-10, 0.0), (0, 0.10), (8, 0.25), (15, 0.45),
                 (21, 0.62), (27, 0.80), (33, 1.0), (42, 1.18)):
        hue = (cold + span * max(0.0, min(1.0, f))) % 360
        if f > 1:
            hue = (hue + (span / abs(span) if span else 1) * 14) % 360
        chroma = 0.14 if 0 <= f <= 1 else (0.07 if f < 0 else 0.16)
        lightness = L + (0.06 if f < 0 else 0)
        stops.append((t, oklch_to_rgb(lightness, chroma, hue)))
    return stops


def build_palette(colors, local_lab, dark, accent_override, mode_reason):
    accent, accent2, hue1, hue2, notes = choose_accents(colors, dark, accent_override)
    L_text = 0.97 if dark else 0.24

    def semantic(hue, lightness=None, chroma=0.13, pull=0.22):
        target = hue1 if hue_distance(hue, hue1) < hue_distance(hue, hue2) else hue2
        return oklch_to_rgb(lightness or (0.80 if dark else 0.55), chroma, harmonize(hue, target, pull))

    ink = oklch_to_rgb(L_text, 0.012 if dark else 0.02, hue1)
    local_L, local_a, local_b = local_lab
    local_h = math.degrees(math.atan2(local_b, local_a)) % 360
    local_c = math.hypot(local_a, local_b)
    if dark:
        # Darker than what is behind it, but not by much: the pane should read
        # as frosted wallpaper, not as a dark card laid on top of it.
        tint = oklch_to_rgb(max(0.13, min(0.30, local_L * 0.82)), min(0.07, local_c * 1.1 + 0.015), local_h)
        shade = oklch_to_rgb(0.08, min(0.04, local_c), local_h)
    else:
        tint = oklch_to_rgb(0.965, min(0.025, local_c * 0.5), local_h)
        shade = oklch_to_rgb(0.30, min(0.04, local_c), local_h)

    hues = [h for h, c in notes if c >= 0.05] + [hue1, hue2]
    stops = temperature_ramp(hues, hue1, dark)

    # The other looks' colours, from the same hues. Metro's are bold but dark
    # enough for white text (about 5.5:1); Pixel's are Material You's tonal
    # roles built on the accent hue: a surface, three containers, the text on
    # them and the primary colour. The third container takes the wallpaper's
    # second accent, as Material You's tertiary colour does; a fixed turn of
    # the hue wheel landed on muddy olive for a warm accent.
    def tone(L, C, h):
        return oklch_to_rgb(L, C, h % 360)
    looks = {
        "solid": tone(0.24, min(0.05, local_c * 0.9 + 0.012), local_h) if dark
        else tone(0.955, min(0.02, local_c * 0.5), local_h),
        "metro1": tone(0.50, 0.13, hue1), "metro2": tone(0.47, 0.12, hue2),
        "metro3": tone(0.52, 0.12, hue1 + 40), "metro4": tone(0.45, 0.11, hue2 - 40),
    }
    if dark:
        looks.update({"px_surface": tone(0.24, 0.018, hue1), "px_primary_c": tone(0.33, 0.085, hue1),
                      "px_secondary_c": tone(0.30, 0.035, hue1 + 15),
                      "px_tertiary_c": tone(0.33, 0.08, hue2),
                      "px_on": tone(0.93, 0.015, hue1), "px_primary": tone(0.84, 0.10, hue1)})
    else:
        looks.update({"px_surface": tone(0.965, 0.012, hue1), "px_primary_c": tone(0.90, 0.06, hue1),
                      "px_secondary_c": tone(0.93, 0.03, hue1 + 15),
                      "px_tertiary_c": tone(0.90, 0.055, hue2),
                      "px_on": tone(0.22, 0.02, hue1), "px_primary": tone(0.48, 0.14, hue1)})
    return {
        **looks,
        "mode": "dark" if dark else "light",
        "mode_reason": mode_reason,
        "ink": ink,
        "accent": accent,
        "accent2": accent2,
        "tint": tint,
        "shade": shade,
        "cloud": oklch_to_rgb(0.94, 0.012, hue1) if dark else oklch_to_rgb(0.62, 0.02, hue1),
        "sun": semantic(SEMANTIC["sun"], 0.86 if dark else 0.72, 0.15, 0.12),
        "rain": semantic(SEMANTIC["rain"], 0.78 if dark else 0.56, 0.12, 0.2),
        "good": semantic(SEMANTIC["good"], chroma=0.12),
        "fair": semantic(SEMANTIC["fair"], chroma=0.13),
        "poor": semantic(SEMANTIC["poor"], chroma=0.14),
        "bad": semantic(SEMANTIC["bad"], chroma=0.15),
        "severe": semantic(SEMANTIC["severe"], chroma=0.15),
        "temperature": [[t, *rgb] for t, rgb in stops],
        "candidates": notes,
    }


def flat(img):
    """Pixel values as a flat sequence, across Pillow versions."""
    getter = getattr(img, "get_flattened_data", None) or img.getdata
    return list(getter())


def superellipse_polygon(x, y, w, h, r, steps=14, exponent=4.2):
    """A rounded rectangle with continuous-curvature corners.

    Each corner is a quarter superellipse over a 1.35x larger radius, which
    is what keeps the curve from starting abruptly where the edge ends -- the
    difference between an Apple-style squircle and a plain rounded rect. The
    Lua side builds the identical polygon, so tints line up with the glass.
    """
    r = min(r * 1.35, w / 2, h / 2)
    points = []
    corners = ((x + w - r, y + r, 0), (x + w - r, y + h - r, 90),
               (x + r, y + h - r, 180), (x + r, y + r, 270))
    for cx, cy, start in corners:
        for i in range(steps + 1):
            a = math.radians(start - 90 + 90 * i / steps)
            ca, sa = math.cos(a), math.sin(a)
            px = cx + r * math.copysign(abs(ca) ** (2 / exponent), ca)
            py = cy + r * math.copysign(abs(sa) ** (2 / exponent), sa)
            points.append((px, py))
    return points


def render_theme(args, wallpaper, monitor, window, tiles, cache):
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageEnhance

    os.environ["MIRAGE_CACHE"] = cache
    s = args.scale
    screen = fitted_screen(Image, wallpaper, monitor["w"], monitor["h"])

    # The wallpaper behind the window, with room around it for the blur to
    # draw from. Pillow's blur extends edge pixels, which is exactly what the
    # desktop does at the edge of the screen.
    blur = max(2, args.blur)
    margin = int(blur * 3)
    wx, wy = window["x"] - monitor["x"], window["y"] - monitor["y"]
    box = (max(0, wx - margin), max(0, wy - margin),
           min(monitor["w"], wx + window["w"] + margin), min(monitor["h"], wy + window["h"] + margin))
    region = screen.crop(box)
    ox, oy = wx - box[0], wy - box[1]                     # window origin inside region

    under = Image.new("RGB", (window["w"], window["h"]))
    under.paste(region, (-ox, -oy))

    # Blur at half size: smoother, and a quarter of the work.
    half = region.resize((max(1, region.size[0] // 2), max(1, region.size[1] // 2)), Image.BILINEAR)
    half = half.filter(ImageFilter.GaussianBlur(blur / 2))
    blurred = half.resize(region.size, Image.BICUBIC)
    blurred = ImageEnhance.Color(blurred).enhance(args.vibrancy)
    frosted = Image.new("RGB", (window["w"], window["h"]))
    frosted.paste(blurred, (-ox, -oy))

    # Light or dark glass: decided by what is actually behind the widget.
    local = under.resize((64, max(1, round(64 * window["h"] / max(window["w"], 1)))), Image.BOX)
    pixels = flat(local)
    labs = [rgb_to_oklab(tuple(c / 255 for c in p)) for p in pixels]
    local_lab = tuple(sum(v[i] for v in labs) / len(labs) for i in range(3))
    if args.mode in ("dark", "light"):
        dark, reason = args.mode == "dark", "setting"
    else:
        dark = local_lab[0] < 0.68
        reason = "wallpaper lightness %.2f" % local_lab[0]

    colors = cluster_colors(Image, screen)
    palette = build_palette(colors, local_lab, dark, hexcolor(args.accent) if args.accent != "auto" else None,
                            reason)
    ink, tint = palette["ink"], palette["tint"]

    # How dark (or, on light glass, how pale) each tile has to be. Text must
    # hold its contrast against the brightest part of what is behind it, not
    # the average -- a sun or a bright cloud under one corner of a tile is
    # exactly where unreadable text would end up. The pane is darkened by
    # multiplying rather than by mixing in grey, which keeps the wallpaper's
    # own hue and saturation: it reads as the wallpaper seen through smoked
    # glass, not as a card laid over it.
    target_contrast = 7.0 if dark else 6.0
    tint_amount = max(0.0, min(0.9, args.tint))

    def shade(rgb, m):
        if dark:
            return tuple(c * m for c in rgb)
        return tuple(1 - (1 - c) * m for c in rgb)

    amounts = []
    for (tx, ty, tw, th) in tiles:
        patch = frosted.crop((tx, ty, tx + tw, ty + th)).resize((48, 48), Image.BOX)
        lumas = sorted((luminance(tuple(c / 255 for c in p)), p) for p in flat(patch))
        pick = lumas[int(len(lumas) * (0.94 if dark else 0.06))][1]
        worst = mix(tuple(c / 255 for c in pick), tint, tint_amount)
        low, high = 0.25, 0.94
        if contrast(ink, shade(worst, high)) < target_contrast:
            for _ in range(18):
                mid = (low + high) / 2
                if contrast(ink, shade(worst, mid)) >= target_contrast:
                    low = mid
                else:
                    high = mid
            high = low
        amounts.append(high)
    # Tiles that differ a lot read as different materials, so pull each part
    # of the way towards the darkest.
    darkest = min(amounts) if amounts else 0.9
    amounts = [m + (darkest - m) * 0.35 for m in amounts]

    tint_rgb = tuple(int(c * 255) for c in tint)
    ss = 4
    radius = args.radius
    images = []
    for index, ((tx, ty, tw, th), amount) in enumerate(zip(tiles, amounts)):
        glass = frosted.crop((tx, ty, tx + tw, ty + th))
        glass = Image.blend(glass, Image.new("RGB", glass.size, tint_rgb), tint_amount)
        if dark:
            glass = glass.point(lambda v, m=amount: int(v * m))
        else:
            glass = glass.point(lambda v, m=amount: int(255 - (255 - v) * m))

        # Sheen: light falling on the top of the pane.
        sheen = Image.linear_gradient("L").resize((1, 256)).point(
            lambda v: int(max(0, 1 - v / 255 / 0.55) ** 1.6 * (16 if dark else 60)))
        sheen = sheen.resize((tw, th), Image.BILINEAR)
        glass = Image.composite(Image.new("RGB", glass.size, (255, 255, 255)), glass, sheen)

        # Grain: breaks up 8-bit banding in the gradients, and makes the pane
        # read as a material rather than a flat fill.
        grain = 0.09 if dark else 0.05
        noise = Image.effect_noise((tw, th), 64).point(lambda v, g=grain: 128 + int((v - 128) * g))
        glass = ImageChops.add(glass, Image.merge("RGB", (noise,) * 3), 1, -128)

        mask = Image.new("L", (tw * ss, th * ss), 0)
        draw = ImageDraw.Draw(mask)
        draw.polygon([(px * ss, py * ss) for px, py in
                      superellipse_polygon(0, 0, tw, th, radius)], fill=255)
        mask = mask.resize((tw, th), Image.LANCZOS)

        # Hairline edge, brighter along the top where the light catches it.
        ring = Image.new("L", (tw * ss, th * ss), 0)
        draw = ImageDraw.Draw(ring)
        outer = superellipse_polygon(0, 0, tw, th, radius)
        inset = max(1.0, 1.0 * s)
        inner = superellipse_polygon(inset, inset, tw - 2 * inset, th - 2 * inset, max(0, radius - inset))
        draw.polygon([(px * ss, py * ss) for px, py in outer], fill=255)
        draw.polygon([(px * ss, py * ss) for px, py in inner], fill=0)
        ring = ring.resize((tw, th), Image.LANCZOS)
        top, bottom = (0.26, 0.07) if dark else (0.85, 0.35)
        fade = Image.linear_gradient("L").resize((tw, th)).point(
            lambda v: int(255 * (top + (bottom - top) * min(1, v / 255 / 0.7))))
        edge = ImageChops.multiply(ring, fade)
        glass = Image.composite(Image.new("RGB", glass.size, (255, 255, 255)), glass, edge)

        glass.putalpha(mask)
        name = f"glass_{index + 1}.png"
        glass.save(os.path.join(cache, name), optimize=False, compress_level=1)
        images.append({"file": name, "x": tx, "y": ty, "w": tw, "h": th, "tint": round(amount, 3)})

    # One shadow under everything, so neighbouring tiles' shadows merge
    # instead of stacking up in the gaps.
    shadow_mask = Image.new("L", (window["w"], window["h"]), 0)
    draw = ImageDraw.Draw(shadow_mask)
    drop = 5 * s
    for (tx, ty, tw, th) in tiles:
        draw.polygon(superellipse_polygon(tx, ty + drop, tw, th, radius), fill=255)
    shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(16 * s))
    strength = args.shadow * (1.0 if dark else 0.55)
    shadow_mask = shadow_mask.point(lambda v: int(v * strength))
    shade_rgb = tuple(int(c * 255) for c in palette["shade"])
    shadow = Image.new("RGB", shadow_mask.size, shade_rgb)
    shadow.putalpha(shadow_mask)
    shadow.save(os.path.join(cache, "shadow.png"), compress_level=1)
    under.save(os.path.join(cache, "under.png"), compress_level=1)

    theme = dict(palette)
    theme.update({
        "glass": True,
        "tiles": images,
        "shadow": "shadow.png",
        "wallpaper": wallpaper.get("path") or "solid colour",
        "source": wallpaper.get("source", ""),
        "id": int(time.time() * 1000) % 2_000_000_000,
    })
    return theme


def tint_only_theme(reason, mode):
    """No wallpaper to work from: a quiet neutral palette over plain tint."""
    dark = mode != "light"
    colors = [(1.0, (0.45, 0.45, 0.55))]
    palette = build_palette(colors, (0.25 if dark else 0.9, 0.0, -0.01), dark, None, reason)
    palette.update({"glass": False, "tiles": [], "wallpaper": "", "source": reason,
                    "id": int(time.time() * 1000) % 2_000_000_000})
    return palette


def theme_step(args, cache, state):
    tiles = parse_tiles(args.tiles)
    window = None
    if args.at:
        x, y = (int(v) for v in args.at.split(","))
        w, h = (int(v) for v in args.window.split("x"))
        window = {"x": x, "y": y, "w": w, "h": h}
    else:
        window = window_geometry(args.title)
    screens = monitors()
    if window is None:
        reason = "widget window not found (is conky running on X11/XWayland?)"
        signature = hashlib.sha1(json.dumps([VERSION, "nowindow", args.mode]).encode()).hexdigest()
        if state.get("theme_signature") != signature or args.force:
            write_theme(cache, tint_only_theme(reason, args.mode))
            state["theme_signature"] = signature
            log(cache, "theme: " + reason)
            return True
        return False

    monitor, index = monitor_for(window, screens)
    if monitor is None:
        monitor = {"x": 0, "y": 0, "w": window["x"] + window["w"], "h": window["y"] + window["h"], "name": ""}
    wallpaper = find_wallpaper(args.wallpaper, index, monitor)

    fingerprint = None
    if wallpaper and "path" in wallpaper:
        try:
            st = os.stat(wallpaper["path"])
            fingerprint = [wallpaper["path"], st.st_mtime_ns, st.st_size, wallpaper.get("fill")]
        except OSError:
            wallpaper = None
    elif wallpaper:
        fingerprint = [repr(sorted(wallpaper.items()))]

    # The script's own mtime is in the signature so that updating it re-renders.
    signature = hashlib.sha1(json.dumps([
        VERSION, os.path.getmtime(os.path.abspath(__file__)), fingerprint, monitor, window, tiles, args.radius, args.mode, args.blur, args.tint,
        args.accent, args.vibrancy, args.shadow, args.scale]).encode()).hexdigest()
    if state.get("theme_signature") == signature and not args.force \
            and os.path.isfile(os.path.join(cache, "theme.lua")):
        return False

    started = time.time()
    if wallpaper is None or "unsupported" in wallpaper:
        why = "wallpaper plugin %s is not supported" % wallpaper["unsupported"] if wallpaper \
            else "could not find the wallpaper; set `wallpaper` in mirage.lua"
        theme = tint_only_theme(why, args.mode)
    else:
        try:
            theme = render_theme(args, wallpaper, monitor, window, tiles, cache)
        except ImportError:
            theme = tint_only_theme("Pillow is not installed (pip install pillow)", args.mode)
        except (OSError, ValueError) as error:
            theme = tint_only_theme("could not read the wallpaper: %s" % error, args.mode)
    theme["window"] = window
    write_theme(cache, theme)
    state["theme_signature"] = signature
    log(cache, "theme: %s (%s) in %.2fs, %s glass, accent %s" % (
        theme.get("wallpaper") or "-", theme.get("source", ""), time.time() - started,
        theme["mode"], "#%02x%02x%02x" % tuple(int(c * 255) for c in theme["accent"])))
    return True


def write_theme(cache, theme):
    def rgb(value):
        return [round(c, 4) for c in value] if isinstance(value, tuple) else value
    clean = {k: rgb(v) for k, v in theme.items()}
    write_atomically(os.path.join(cache, "theme.lua"), "return " + lua_value(clean) + "\n")


def parse_tiles(text):
    tiles = []
    for part in (text or "").split(";"):
        numbers = [int(round(float(v))) for v in part.split(",") if v.strip()]
        if len(numbers) == 4 and numbers[2] > 0 and numbers[3] > 0:
            tiles.append(tuple(numbers))
    return tiles


# --------------------------------------------------------------------------
# weather
# --------------------------------------------------------------------------

WMO = {
    0: ("Clear", "clear", 0), 1: ("Mostly clear", "few", 0), 2: ("Partly cloudy", "partly", 0),
    3: ("Overcast", "cloudy", 0), 45: ("Fog", "fog", 0), 48: ("Freezing fog", "fog", 0),
    51: ("Light drizzle", "drizzle", 1), 53: ("Drizzle", "drizzle", 2), 55: ("Heavy drizzle", "drizzle", 3),
    56: ("Freezing drizzle", "sleet", 1), 57: ("Freezing drizzle", "sleet", 2),
    61: ("Light rain", "rain", 1), 63: ("Rain", "rain", 2), 65: ("Heavy rain", "rain", 3),
    66: ("Freezing rain", "sleet", 2), 67: ("Freezing rain", "sleet", 3),
    71: ("Light snow", "snow", 1), 73: ("Snow", "snow", 2), 75: ("Heavy snow", "snow", 3),
    77: ("Snow grains", "snow", 1), 80: ("Light showers", "showers", 1), 81: ("Showers", "showers", 2),
    82: ("Heavy showers", "showers", 3), 85: ("Snow showers", "snow", 2), 86: ("Heavy snow showers", "snow", 3),
    95: ("Thunderstorm", "thunder", 2), 96: ("Thunderstorm with hail", "thunder", 3),
    99: ("Thunderstorm with hail", "thunder", 3),
}
WET_KINDS = {"drizzle": "Drizzle", "rain": "Rain", "showers": "Showers", "sleet": "Sleet",
             "snow": "Snow", "thunder": "Thunderstorms"}


def describe(code):
    return WMO.get(int(code or 0), ("Unknown", "cloudy", 0))


def fetch_json(url, timeout=15):
    # Imported here: urllib.request pulls in ssl, which costs more than the
    # whole of a run that has nothing to fetch.
    import urllib.request
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def urlencode(query):
    from urllib.parse import urlencode as encode
    return encode(query)


def locate(spec, state):
    """Resolve the location setting to coordinates, caching the answer."""
    spec = (spec or "auto").strip()
    cached = state.get("location")
    if cached and cached.get("spec") == spec and \
            (spec != "auto" or time.time() - cached.get("time", 0) < 6 * 3600):
        return cached

    place = None
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*(?:,\s*(.+))?", spec)
    if m:
        place = {"lat": float(m.group(1)), "lon": float(m.group(2)), "name": (m.group(3) or "").strip(),
                 "country": ""}
        if not place["name"]:
            try:
                data = fetch_json("https://api.bigdatacloud.net/data/reverse-geocode-client?" +
                                  urlencode({"latitude": place["lat"], "longitude": place["lon"],
                                                          "localityLanguage": "en"}))
                place["name"] = data.get("city") or data.get("locality") or ""
                place["country"] = data.get("countryCode", "")
            except Exception:
                pass
    elif spec.lower() == "auto":
        for url, parse in (
                ("https://ipinfo.io/json", lambda d: (*d["loc"].split(","), d.get("city"), d.get("country"))),
                ("https://ipapi.co/json/", lambda d: (d["latitude"], d["longitude"], d.get("city"),
                                                      d.get("country_code"))),
                ("http://ip-api.com/json/", lambda d: (d["lat"], d["lon"], d.get("city"), d.get("countryCode")))):
            try:
                lat, lon, city, country = parse(fetch_json(url, timeout=8))
                place = {"lat": float(lat), "lon": float(lon), "name": city or "", "country": country or ""}
                break
            except Exception:
                continue
    else:
        name, _, country = spec.partition(",")
        data = fetch_json("https://geocoding-api.open-meteo.com/v1/search?" +
                          urlencode({"name": name.strip(), "count": 10, "language": "en"}))
        results = data.get("results") or []
        country = country.strip().upper()
        if country:
            results = [r for r in results if r.get("country_code", "").upper() == country or
                       r.get("country", "").upper() == country] or results
        if results:
            r = results[0]
            place = {"lat": r["latitude"], "lon": r["longitude"], "name": r.get("name", name),
                     "country": r.get("country_code", "")}
    if place is None:
        if cached:
            return cached
        raise RuntimeError("could not resolve location %r" % spec)
    place.update({"spec": spec, "time": time.time()})
    state["location"] = place
    return place


TWELVE_HOUR = False


def clock_text(epoch, offset):
    moment = time.gmtime(epoch + offset)
    if TWELVE_HOUR:
        return "%d:%s" % (int(time.strftime("%I", moment)), time.strftime("%M %p", moment).lower())
    return time.strftime("%H:%M", moment)


def summarize(current, hours, minutely, offset, units, now, unit=""):
    """One plain sentence about the next day, the way a person would put it."""
    wet_threshold = 0.012 if units == "imperial" else 0.3
    current_kind = describe(current["code"])[1]
    raining = current_kind in WET_KINDS or current.get("precip", 0) > 0
    clauses = []

    if minutely:
        if raining:
            for t, p in minutely:
                if t > now and p <= 0.0 and all(q <= 0.0 for tt, q in minutely if tt >= t):
                    clauses.append("%s stopping around %s" % (WET_KINDS.get(current_kind, "Rain"),
                                                             clock_text(t, offset)))
                    break
        else:
            for t, p in minutely:
                if t > now and p > 0.05:
                    clauses.append("Rain starting around %s" % clock_text(t, offset))
                    break

    upcoming = [h for h in hours if h["t"] >= now - 1800][:24]
    if not clauses and upcoming:
        if raining:
            for h in upcoming:
                if h["pop"] < 30 and h["precip"] < wet_threshold / 3:
                    clauses.append("%s easing around %s" % (WET_KINDS.get(current_kind, "Rain"),
                                                           clock_text(h["t"], offset)))
                    break
            else:
                clauses.append("%s for most of the day" % WET_KINDS.get(current_kind, "Rain"))
        else:
            for h in upcoming[1:16]:
                if h["pop"] >= 50 or h["precip"] >= wet_threshold:
                    kind = describe(h["code"])[1]
                    clauses.append("%s likely around %s" % (WET_KINDS.get(kind, "Rain"),
                                                            clock_text(h["t"], offset)))
                    break
            else:
                if all(h["pop"] < 25 for h in upcoming):
                    clauses.append("Dry for the next 24 hours")

    if upcoming:
        day = upcoming[:14]
        warmest = max(day, key=lambda h: h["temp"])
        coolest = min(upcoming, key=lambda h: h["temp"])
        if warmest["t"] > now + 3600 and warmest["temp"] > current["temp"] + 1:
            clauses.append("High of %d°%s at %s" % (round(warmest["temp"]), unit, clock_text(warmest["t"], offset)))
        elif coolest["temp"] < current["temp"] - 1:
            hour = int(time.strftime("%H", time.gmtime(coolest["t"] + offset)))
            when = "tonight" if hour >= 18 or hour < 9 else "at " + clock_text(coolest["t"], offset)
            clauses.append("Low of %d°%s %s" % (round(coolest["temp"]), unit, when))
    return " · ".join(clauses[:2])


def weather_step(args, cache, state, force=False):
    now = time.time()
    key = [args.location, args.units, args.aqi, args.clock, args.show_unit]
    fresh = state.get("weather_key") == key and now - state.get("weather_time", 0) < args.interval * 60
    retry_at = state.get("weather_retry", 0)
    if fresh and not force and os.path.isfile(os.path.join(cache, "weather.lua")):
        return False
    if now < retry_at and not force and state.get("weather_key") == key:
        return False

    try:
        place = locate(args.location, state)
        imperial = args.units == "imperial"
        query = {
            "latitude": place["lat"], "longitude": place["lon"], "timezone": "auto",
            "timeformat": "unixtime", "forecast_days": 8,
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,"
                       "weather_code,cloud_cover,pressure_msl,wind_speed_10m,wind_direction_10m,"
                       "wind_gusts_10m,uv_index,dew_point_2m,visibility",
            "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code,is_day,"
                      "pressure_msl",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,"
                     "precipitation_probability_max,precipitation_sum,uv_index_max,wind_speed_10m_max",
            "minutely_15": "precipitation", "forecast_minutely_15": 8,
        }
        if imperial:
            query.update({"temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
                          "precipitation_unit": "inch"})
        data = fetch_json("https://api.open-meteo.com/v1/forecast?" + urlencode(query))
        offset = int(data.get("utc_offset_seconds", 0))
        c = data["current"]

        def num(value, default=0.0):
            return default if value is None else float(value)

        current = {
            "temp": num(c.get("temperature_2m")), "feels": num(c.get("apparent_temperature")),
            "humidity": num(c.get("relative_humidity_2m")), "code": int(num(c.get("weather_code"))),
            "is_day": int(num(c.get("is_day"), 1)), "wind": num(c.get("wind_speed_10m")),
            "wind_dir": num(c.get("wind_direction_10m")), "gust": num(c.get("wind_gusts_10m")),
            "pressure": num(c.get("pressure_msl")), "uv": num(c.get("uv_index")),
            "cloud": num(c.get("cloud_cover")), "dew": num(c.get("dew_point_2m")),
            "precip": num(c.get("precipitation")), "visibility": num(c.get("visibility")),
            "time": int(num(c.get("time"), now)),
        }
        desc, kind, level = describe(current["code"])
        current.update({"desc": desc, "kind": kind, "level": level})

        h = data["hourly"]
        hours = []
        for i, t in enumerate(h["time"]):
            if t < now - 2 * 3600 or t > now + 50 * 3600:
                continue
            code = int(num(h["weather_code"][i]))
            d, k, lv = describe(code)
            hours.append({"t": int(t), "temp": num(h["temperature_2m"][i]),
                          "pop": num(h["precipitation_probability"][i]),
                          "precip": num(h["precipitation"][i]), "code": code, "kind": k, "level": lv,
                          "day": int(num(h["is_day"][i], 1)), "pressure": num(h["pressure_msl"][i])})

        d = data["daily"]
        days = []
        for i, t in enumerate(d["time"]):
            code = int(num(d["weather_code"][i]))
            desc_d, k, lv = describe(code)
            days.append({"t": int(t), "code": code, "kind": k, "level": lv, "desc": desc_d,
                         "hi": num(d["temperature_2m_max"][i]), "lo": num(d["temperature_2m_min"][i]),
                         "pop": num(d["precipitation_probability_max"][i]),
                         "precip": num(d["precipitation_sum"][i]),
                         "rise": int(num(d["sunrise"][i])), "set": int(num(d["sunset"][i])),
                         "uv": num(d["uv_index_max"][i]), "wind": num(d["wind_speed_10m_max"][i])})

        minutely = []
        m15 = data.get("minutely_15") or {}
        for t, p in zip(m15.get("time") or [], m15.get("precipitation") or []):
            if p is not None:
                minutely.append((int(t), float(p)))

        # Pressure trend over the last three hours, the barometer's one job.
        trend = 0.0
        past = [x for x in hours if x["t"] <= now]
        if len(past) >= 4:
            trend = past[-1]["pressure"] - past[-4]["pressure"]

        aqi = None
        try:
            field = "us_aqi" if args.aqi == "us" else "european_aqi"
            air = fetch_json("https://air-quality-api.open-meteo.com/v1/air-quality?" + urlencode({
                "latitude": place["lat"], "longitude": place["lon"], "current": field + ",pm2_5",
                "timezone": "auto", "timeformat": "unixtime"}), timeout=10)
            value = (air.get("current") or {}).get(field)
            if value is not None:
                aqi = {"value": float(value), "scale": "US" if args.aqi == "us" else "EU",
                       "pm25": num((air.get("current") or {}).get("pm2_5"))}
        except Exception as error:
            log(cache, "air quality unavailable: %s" % error)

        weather = {
            "fetched": int(now), "place": place.get("name") or "", "country": place.get("country") or "",
            "lat": place["lat"], "lon": place["lon"], "tz_offset": offset,
            "imperial": imperial,
            "units": {"temp": "°F" if imperial else "°C", "wind": "mph" if imperial else "km/h",
                      "precip": "in" if imperial else "mm"},
            "current": current, "hourly": hours, "daily": days, "aqi": aqi,
            "pressure_trend": trend,
            "summary": summarize(current, hours, minutely, offset, args.units, now,
                                 ("F" if imperial else "C") if args.show_unit else ""),
        }
        write_atomically(os.path.join(cache, "weather.lua"), "return " + lua_value(weather) + "\n")
        state["weather_time"] = now
        state["weather_key"] = key
        state.pop("weather_retry", None)
        state.pop("weather_error", None)
        log(cache, "weather: %s %.1f%s %s" % (weather["place"], current["temp"],
                                              weather["units"]["temp"], desc))
        return True
    except Exception as error:   # network down, API change: keep the old data, retry soon
        state["weather_retry"] = now + 300
        state["weather_error"] = str(error)[:200]
        write_atomically(os.path.join(cache, "weather_error.lua"),
                         "return " + lua_value({"error": str(error)[:120], "time": int(now)}) + "\n")
        log(cache, "weather failed: %s" % error)
        return True


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("tick", "theme", "weather", "doctor"), nargs="?", default="tick")
    parser.add_argument("--cache", default=os.path.join(os.environ.get(
        "XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "conky-mirage"))
    parser.add_argument("--title", default="conky-mirage")
    parser.add_argument("--window", default="0x0", help="WxH, used with --at")
    parser.add_argument("--at", default="", help="X,Y of the window, instead of asking X")
    parser.add_argument("--tiles", default="", help="x,y,w,h;... in window pixels")
    parser.add_argument("--radius", type=float, default=22)
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--wallpaper", default="auto")
    parser.add_argument("--mode", default="auto", choices=("auto", "dark", "light"))
    parser.add_argument("--blur", type=float, default=36)
    parser.add_argument("--tint", type=float, default=0.35, help="least tint, 0..1")
    parser.add_argument("--vibrancy", type=float, default=1.35)
    parser.add_argument("--shadow", type=float, default=0.45)
    parser.add_argument("--accent", default="auto")
    parser.add_argument("--location", default="auto")
    parser.add_argument("--units", default="metric", choices=("metric", "imperial"))
    parser.add_argument("--aqi", default="european", choices=("european", "us"))
    parser.add_argument("--interval", type=float, default=15, help="weather refresh, minutes")
    parser.add_argument("--clock", default="24h", choices=("24h", "12h"))
    parser.add_argument("--show-unit", action="store_true", help='"12°C" rather than "12°"')
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    cache = os.path.expanduser(args.cache)
    os.makedirs(cache, exist_ok=True)
    global TWELVE_HOUR
    TWELVE_HOUR = args.clock == "12h"

    if args.command == "doctor":
        window = window_geometry(args.title)
        screens = monitors()
        print("desktop    :", os.environ.get("XDG_CURRENT_DESKTOP", "?"))
        print("window     :", window or "not found (is conky-mirage running?)")
        print("monitors   :", screens or "xrandr unavailable")
        monitor, index = monitor_for(window, screens) if window else (screens[0] if screens else None, 0)
        if monitor:
            print("wallpaper  :", find_wallpaper(args.wallpaper, index, monitor))
        try:
            import PIL
            print("pillow     :", PIL.__version__)
        except ImportError:
            print("pillow     : MISSING -- install python3-pillow for the glass")
        print("cache      :", cache)
        return 0

    # One run at a time: a slow network must not let runs pile up.
    lock = open(os.path.join(cache, "lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0

    state = load_state(cache)
    changed = False
    try:
        if args.command in ("tick", "theme"):
            changed |= theme_step(args, cache, state)
        if args.command in ("tick", "weather"):
            changed |= weather_step(args, cache, state, force=args.command == "weather")
    except Exception as error:
        log(cache, "error: %r" % error)
        raise
    finally:
        save_state(cache, state)
        if changed:
            bump_serial(cache)
    return 0


if __name__ == "__main__":
    sys.exit(main())
