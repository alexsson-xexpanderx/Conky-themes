#!/usr/bin/env python3
"""ConkyTray helper: everything the panel shows that conky cannot find out
for itself.

    tray.py daemon     the long-running helper tray.lua starts
    tray.py doctor     print what it finds: icon theme, apps, tray, windows
    tray.py screen     the screen's width, for start_conky_tray

The open windows come from a KWin script (tray_kwin.js) that calls back over
D-Bus; the tray icons from the StatusNotifierItem host protocol, whose
watcher lives in kded and so outlives Plasma's own panel; volume from
pactl/wpctl, the network from nmcli, the battery from /sys. The look --
palette and frosted glass -- is asked of Conky-Mirage's helper, so the panel
wears whichever of Mirage's four looks the widget does.

tray.lua and this talk through files in the cache (~/.cache/conky-tray):
tray.lua writes `layout` (its size, the panes to frost, the icon sizes) and
appends one line per click to `commands`; this writes `state.lua`, a Lua
table, and bumps `serial` when it changed. Menus are tray_menu.py, a GTK
layer-shell popup started per menu.

Standard library, dbus-python and PyGObject; Pillow for icons that are not
SVG; rsvg-convert for those that are.
"""

import configparser
import glob
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(HOME, ".cache"), "conky-tray")
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "conky-tray")
MIRAGE = os.path.join(os.path.dirname(HERE), "Conky-Mirage", "mirage.py")
BUS_NAME = "org.conkytray"
LAUNCHER_W, LAUNCHER_H = 900, 620      # the app menu's size, in KWin's units


def log(message):
    sys.stderr.write(time.strftime("%H:%M:%S ") + message.rstrip() + "\n")
    sys.stderr.flush()


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def detached(argv):
    """Start a program that outlives this one and is not our child to reap."""
    try:
        subprocess.Popen(["setsid", "-f"] + argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL).wait(timeout=5)
    except (OSError, subprocess.SubprocessError) as error:
        log("could not start %s: %s" % (argv[0], error))


def die_with_parent():
    """For a child that must not outlive the helper (the watchers): the
    kernel stops it when the helper goes, however the helper goes."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)    # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


def output_of(argv, timeout=3):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# ------------------------------------------------------------------ Lua out

def lua_string(s):
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif o < 32 or o == 127:
            out.append("\\%03d" % o)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def lua(value):
    """A Lua literal for a Python value, for state.lua."""
    if value is None:
        return "nil"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(round(value, 4))
    if isinstance(value, str):
        return lua_string(value)
    if isinstance(value, dict):
        return "{" + ", ".join("[%s] = %s" % (lua_string(str(k)), lua(v))
                               for k, v in value.items() if v is not None) + "}"
    return "{" + ", ".join(lua(v) for v in value) + "}"


# ------------------------------------------------------------------- icons

def data_dirs():
    home = os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local", "share")
    dirs = [home] + (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    dirs += [os.path.join(home, "flatpak", "exports", "share"), "/var/lib/flatpak/exports/share"]
    seen, out = set(), []
    for d in dirs:
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


class Theme:
    """One icon theme, which may be spread over several base directories."""

    def __init__(self, name, roots):
        self.name, self.roots = name, roots
        self.dirs, self.inherits = [], []
        parser = configparser.RawConfigParser(strict=False, interpolation=None)
        parser.optionxform = str
        try:
            parser.read(os.path.join(roots[0], "index.theme"), encoding="utf-8")
        except (configparser.Error, UnicodeDecodeError):
            return
        if not parser.has_section("Icon Theme"):
            return
        main = parser["Icon Theme"]
        self.inherits = [x.strip() for x in main.get("Inherits", "").split(",") if x.strip()]
        names = main.get("Directories", "") + "," + main.get("ScaledDirectories", "")
        for d in [x.strip() for x in names.split(",") if x.strip()]:
            if not parser.has_section(d):
                continue
            sec = parser[d]
            try:
                size = int(sec.get("Size", "0"))
                scale = int(sec.get("Scale", "1"))
            except ValueError:
                continue
            kind = sec.get("Type", "Threshold")
            self.dirs.append({"path": d, "size": size * scale, "type": kind,
                              "min": int(sec.get("MinSize", size)) * scale,
                              "max": int(sec.get("MaxSize", size)) * scale,
                              "threshold": int(sec.get("Threshold", "2")) * scale})


class Icons:
    """Icon theme lookup (the freedesktop rules, roughly) and rendering to
    PNGs of exactly the size the panel draws them at."""

    EXTS = (".png", ".svg", ".svgz", ".xpm")

    def __init__(self, cache):
        self.out = os.path.join(cache, "icons")
        os.makedirs(self.out, exist_ok=True)
        self.bases = [os.path.join(HOME, ".icons")] + [os.path.join(d, "icons") for d in data_dirs()]
        self.pixmaps = ["/usr/share/pixmaps"]
        self.themes, self.listings, self.found = {}, {}, {}
        self.theme_name = self.user_theme()

    @staticmethod
    def user_theme():
        parser = configparser.RawConfigParser(strict=False, interpolation=None)
        try:
            parser.read(os.path.join(HOME, ".config", "kdeglobals"), encoding="utf-8")
            return parser.get("Icons", "Theme", fallback="breeze")
        except (configparser.Error, UnicodeDecodeError):
            return "breeze"

    def theme(self, name):
        if name not in self.themes:
            roots = [os.path.join(b, name) for b in self.bases if os.path.isfile(os.path.join(b, name, "index.theme"))]
            roots += [os.path.join(b, name) for b in self.bases
                      if os.path.isdir(os.path.join(b, name)) and os.path.join(b, name) not in roots]
            self.themes[name] = Theme(name, roots) if roots and os.path.isfile(os.path.join(roots[0], "index.theme")) else None
        return self.themes[name]

    def chain(self, first):
        order, queue = [], [first]
        while queue:
            name = queue.pop(0)
            if name in order:
                continue
            theme = self.theme(name)
            if theme is None:
                continue
            order.append(name)
            queue.extend(theme.inherits)
        if "hicolor" not in order and self.theme("hicolor"):
            order.append("hicolor")
        return order

    def listing(self, path):
        if path not in self.listings:
            try:
                self.listings[path] = set(os.listdir(path))
            except OSError:
                self.listings[path] = set()
        return self.listings[path]

    @staticmethod
    def distance(d, size):
        if d["type"] == "Fixed":
            return abs(d["size"] - size)
        if d["type"] == "Scalable":
            if size < d["min"]:
                return d["min"] - size
            if size > d["max"]:
                return size - d["max"]
            return 0
        if size < d["size"] - d["threshold"]:
            return d["size"] - d["threshold"] - size
        if size > d["size"] + d["threshold"]:
            return size - d["size"] - d["threshold"]
        return 0

    def in_theme(self, theme, name, size):
        best, best_distance = None, None
        for d in theme.dirs:
            dist = self.distance(d, size)
            if best_distance is not None and dist >= best_distance:
                continue
            for root in theme.roots:
                files = self.listing(os.path.join(root, d["path"]))
                for ext in self.EXTS:
                    if name + ext in files:
                        best, best_distance = os.path.join(root, d["path"], name + ext), dist
                        break
                if best_distance == dist:
                    break
            if best_distance == 0:
                break
        return best

    def find(self, name, size, theme=None, extra=()):
        """The file for an icon name, or None. `extra` are directories to
        look in first (a tray icon's IconThemePath)."""
        if not name:
            return None
        if name.startswith("/"):
            return name if os.path.isfile(name) else None
        key = (name, size, theme, tuple(extra))
        if key in self.found:
            return self.found[key]
        result = None
        names = [name]
        # The spec's fallback: drop the last dash-part ("network-wireless-
        # signal-good" -> "network-wireless-signal" -> ...).
        while "-" in names[-1]:
            names.append(names[-1].rsplit("-", 1)[0])
        for n in names:
            for d in extra:
                for ext in self.EXTS:
                    if os.path.isfile(os.path.join(d, n + ext)):
                        result = os.path.join(d, n + ext)
                        break
                if result:
                    break
                hits = glob.glob(os.path.join(glob.escape(d), "*", "*", "*", n + ".*"))
                if hits:
                    result = sorted(hits)[-1]
                    break
            if result:
                break
            for t in self.chain(theme or self.theme_name):
                result = self.in_theme(self.theme(t), n, size)
                if result:
                    break
            if result:
                break
            for p in self.pixmaps:
                for ext in self.EXTS:
                    if os.path.isfile(os.path.join(p, n + ext)):
                        result = os.path.join(p, n + ext)
                        break
                if result:
                    break
            if result:
                break
        self.found[key] = result
        return result

    def render(self, path, size, color=None, accent=None):
        """A size x size PNG of an icon file, symbolic icons drawn in `color`."""
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return None
        key = hashlib.sha1(("%s|%s|%d|%s|%s" % (path, mtime, size, color, accent)).encode()).hexdigest()[:20]
        out = os.path.join(self.out, key + ".png")
        if os.path.exists(out):
            return out
        symbolic = "-symbolic" in os.path.basename(path)
        try:
            from PIL import Image
            if path.endswith((".svg", ".svgz")):
                data = (gzip.open(path) if path.endswith(".svgz") else open(path, "rb")).read().decode("utf-8", "replace")
                if color and "ColorScheme-" in data:
                    data = recolor(data, color, accent)
                    symbolic = False
                tmp = out + ".svg"
                with open(tmp, "w", encoding="utf-8") as handle:
                    handle.write(data)
                raw = out + ".raw.png"
                done = subprocess.run(["rsvg-convert", "-w", str(size), "-h", str(size), "--keep-aspect-ratio",
                                       "-o", raw, tmp], capture_output=True, timeout=10)
                os.remove(tmp)
                if done.returncode != 0:
                    return None
                image = Image.open(raw).convert("RGBA")
                os.remove(raw)
            else:
                image = Image.open(path).convert("RGBA")
            image = fit_square(image, size)
            if symbolic and color:
                image = tint(image, color)
            image.save(out)
            return out
        except Exception as error:                       # a broken icon must not stop the panel
            log("icon %s: %s" % (path, error))
            return None

    def pixmap(self, pixmaps, size):
        """A size x size PNG from a tray item's IconPixmap (ARGB32, network order)."""
        if not pixmaps:
            return None
        best = min(pixmaps, key=lambda p: (p[0] < size, abs(p[0] - size)))
        w, h, data = int(best[0]), int(best[1]), bytes(best[2])
        if w <= 0 or h <= 0 or len(data) < w * h * 4:
            return None
        key = hashlib.sha1(data + b"%d" % size).hexdigest()[:20]
        out = os.path.join(self.out, "px-" + key + ".png")
        if os.path.exists(out):
            return out
        from PIL import Image
        rgba = bytearray(len(data))
        rgba[0::4], rgba[1::4], rgba[2::4], rgba[3::4] = data[1::4], data[2::4], data[3::4], data[0::4]
        image = fit_square(Image.frombytes("RGBA", (w, h), bytes(rgba[:w * h * 4])), size)
        image.save(out)
        return out


def uptime():
    """The clock the panel reads too (/proc/uptime), so a position measured
    here can be carried forward there."""
    try:
        return float(open("/proc/uptime").read().split()[0])
    except (OSError, ValueError, IndexError):
        return time.monotonic()


def text_png(out_dir, text, px, bold, color, widest):
    """`text` drawn by Pango in `color` at `px` pixels, as a PNG: Pango finds
    a font for every character (a title in Japanese, an emoji), which cairo's
    toy text cannot. Returns (path, width, height, baseline)."""
    import gi
    gi.require_version("Pango", "1.0")
    gi.require_version("PangoCairo", "1.0")
    import cairo
    from gi.repository import Pango, PangoCairo
    key = hashlib.sha1(("%s|%.2f|%s|%s|%d" % (text, px, bold, color, widest)).encode()).hexdigest()[:20]
    out = os.path.join(out_dir, "t-" + key + ".png")
    probe = cairo.Context(cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1))
    layout = PangoCairo.create_layout(probe)
    options = cairo.FontOptions()
    options.set_antialias(cairo.ANTIALIAS_GRAY)
    options.set_hint_style(cairo.HINT_STYLE_SLIGHT)
    options.set_hint_metrics(cairo.HINT_METRICS_OFF)
    PangoCairo.context_set_font_options(layout.get_context(), options)
    layout.context_changed()
    desc = Pango.FontDescription.from_string("Noto Sans Medium" if bold else "Noto Sans")
    desc.set_absolute_size(px * Pango.SCALE)
    layout.set_font_description(desc)
    layout.set_single_paragraph_mode(True)
    layout.set_width(int(widest * Pango.SCALE))
    layout.set_ellipsize(Pango.EllipsizeMode.END)
    layout.set_text(text, -1)
    logical = layout.get_pixel_extents()[1]
    width, height = max(1, logical.width), max(1, logical.height)
    baseline = layout.get_baseline() / Pango.SCALE
    if not os.path.exists(out):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
        cr = cairo.Context(surface)
        r, g, b = hex_rgb(color)
        cr.set_source_rgb(r / 255, g / 255, b / 255)
        PangoCairo.show_layout(cr, layout)
        surface.write_to_png(out + ".tmp")
        os.replace(out + ".tmp", out)
    return out, width, height, baseline


def fit_square(image, size):
    from PIL import Image
    w, h = image.size
    if (w, h) != (size, size):
        k = size / max(w, h)
        image = image.resize((max(1, round(w * k)), max(1, round(h * k))), Image.LANCZOS)
        square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        square.paste(image, ((size - image.width) // 2, (size - image.height) // 2))
        image = square
    return image


def oklab(r, g, b):
    """sRGB (0-1) to OKLab, for telling colours apart the way eyes do."""
    def linear(c):
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = linear(r), linear(g), linear(b)
    l = (0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b) ** (1 / 3)
    m = (0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b) ** (1 / 3)
    s = (0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b) ** (1 / 3)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def tint(image, color):
    from PIL import Image
    r, g, b = hex_rgb(color)
    solid = Image.new("RGBA", image.size, (r, g, b, 255))
    solid.putalpha(image.getchannel("A"))
    return solid


def hex_rgb(color):
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def recolor(svg, color, accent):
    """Point a KDE icon's colour-scheme classes at the panel's colours, as
    Plasma does when it draws a symbolic icon."""
    for cls in ("Text", "ViewText", "ButtonText", "WindowText", "Foreground"):
        svg = re.sub(r"(\.ColorScheme-%s\s*\{[^}]*?color\s*:\s*)#[0-9a-fA-F]{3,8}" % cls, r"\g<1>" + color, svg)
    if accent:
        svg = re.sub(r"(\.ColorScheme-(?:Highlight|ViewFocus)\s*\{[^}]*?color\s*:\s*)#[0-9a-fA-F]{3,8}",
                     r"\g<1>" + accent, svg)
    return svg


# -------------------------------------------------------------------- apps

class Apps:
    """The installed applications, from their .desktop files, and which one
    a window belongs to."""

    def __init__(self):
        self.entries = {}
        self.by_lower, self.by_class, self.by_exec, self.by_tail = {}, {}, {}, {}
        self.scan()

    def scan(self):
        for base in reversed(data_dirs()):              # earlier directories win
            root = os.path.join(base, "applications")
            for path in glob.glob(os.path.join(glob.escape(root), "**", "*.desktop"), recursive=True):
                ident = os.path.relpath(path, root)[:-8].replace("/", "-")
                entry = self.parse(path)
                if entry:
                    self.entries[ident] = entry
        for ident, e in self.entries.items():
            self.by_lower[ident.lower()] = ident
            if e["wmclass"]:
                self.by_class.setdefault(e["wmclass"].lower(), []).append(ident)
            if e["exec"]:
                self.by_exec.setdefault(e["exec"].lower(), []).append(ident)
            self.by_tail.setdefault(ident.rsplit(".", 1)[-1].lower(), []).append(ident)

    @staticmethod
    def parse(path):
        parser = configparser.RawConfigParser(strict=False, interpolation=None)
        parser.optionxform = str
        try:
            parser.read(path, encoding="utf-8")
        except (configparser.Error, UnicodeDecodeError):
            return None
        if not parser.has_section("Desktop Entry"):
            return None
        e = parser["Desktop Entry"]
        if e.get("Type", "Application") != "Application":
            return None
        command = e.get("Exec", "").split()
        exe = ""
        for part in command:
            if "=" in part and not part.startswith("/"):
                continue                                  # env FOO=bar ...
            if part in ("env", "flatpak", "run"):
                continue
            if part.startswith("-"):
                continue
            exe = os.path.basename(part.strip('"'))
            break
        hidden = e.get("NoDisplay", "false").lower() == "true" or e.get("Hidden", "false").lower() == "true"
        desktops = set((os.environ.get("XDG_CURRENT_DESKTOP") or "KDE").split(":"))
        only = [x for x in e.get("OnlyShowIn", "").split(";") if x]
        never = [x for x in e.get("NotShowIn", "").split(";") if x]
        try_exec = e.get("TryExec", "")
        shown = not hidden and (not only or desktops & set(only)) and not desktops & set(never) and \
            (not try_exec or bool(shutil.which(try_exec)) or os.path.exists(try_exec))
        return {"name": e.get("Name", ""), "icon": e.get("Icon", ""), "exec": exe,
                "wmclass": e.get("StartupWMClass", ""), "path": path, "hidden": hidden,
                "shown": bool(shown), "generic": e.get("GenericName", ""), "comment": e.get("Comment", ""),
                "keywords": e.get("Keywords", ""), "categories": [c for c in e.get("Categories", "").split(";") if c]}

    def match(self, desktop, cls, name, pins=()):
        """The app a window belongs to. Where several apps could be it (Zen's
        window says "zen", its launcher is zen-browser, and there is a hidden
        "userapp-" file KDE wrote for /opt/zen-browser/zen), a pinned app
        wins, then one that shows in menus."""
        for key in (desktop, cls, name):
            if key and key.lower() in self.by_lower and key.lower() in [p.lower() for p in pins]:
                return self.by_lower[key.lower()]
        found = []
        for key in (desktop, cls, name):
            if key and key.lower() in self.by_lower:
                found.append(self.by_lower[key.lower()])
        for table in (self.by_class, self.by_tail, self.by_exec):
            for key in (cls, name):
                found += table.get(key.lower(), []) if key else []
        # A pinned app named for the window's class and something more
        # ("zen" -> "zen-browser") is very likely the one.
        for key in (cls, name):
            k = (key or "").lower()
            for p in pins:
                e = self.entries.get(p) or {}
                if k and (p.lower().startswith(k + "-") or e.get("exec", "").lower().startswith(k + "-")):
                    found.append(p)
        if not found:
            return None
        return sorted(found, key=lambda i: (i not in pins, self.entries[i]["hidden"],
                                            i.startswith("userapp-")))[0]

    def resolve(self, launcher):
        """A Plasma launcher URL ("applications:konsole.desktop",
        "preferred://filemanager") as a desktop file id."""
        if launcher.startswith("applications:"):
            ident = launcher[len("applications:"):]
        elif launcher.startswith("preferred://"):
            what = launcher[len("preferred://"):]
            mime = {"filemanager": "inode/directory", "browser": "x-scheme-handler/https",
                    "mail": "x-scheme-handler/mailto"}.get(what)
            ident = output_of(["xdg-mime", "query", "default", mime]).strip() if mime else ""
        elif launcher.startswith("file://"):
            ident = os.path.basename(launcher[7:])
        else:
            ident = launcher
        ident = ident[:-8] if ident.endswith(".desktop") else ident
        return ident if ident in self.entries else None


def plasma_launchers():
    """The pinned apps of Plasma's icon task manager, to start from."""
    path = os.path.join(HOME, ".config", "plasma-org.kde.plasma.desktop-appletsrc")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return []
    m = re.search(r"^launchers=(.*)$", text, re.M)
    return [x for x in m.group(1).split(",") if x] if m else []


def plasma_kickoff_icon():
    path = os.path.join(HOME, ".config", "plasma-org.kde.plasma.desktop-appletsrc")
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return None
    for block in re.split(r"\n(?=\[)", text):
        if "plugin=org.kde.plasma.kickoff" in block:
            ident = re.match(r"\[Containments\]\[(\d+)\]\[Applets\]\[(\d+)\]", block)
            if ident:
                conf = re.search(r"\[Containments\]\[%s\]\[Applets\]\[%s\]\[Configuration\]\[General\]\n(.*?)(?:\n\[|\Z)"
                                 % ident.groups(), text, re.S)
                if conf:
                    icon = re.search(r"^icon=(.*)$", conf.group(1), re.M)
                    if icon:
                        return icon.group(1).strip()
    return None


# ------------------------------------------------------------------ daemon

class Daemon:
    def __init__(self, args):
        import dbus
        import dbus.service
        import dbus.mainloop.glib
        from gi.repository import GLib
        self.dbus, self.GLib = dbus, GLib
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SessionBus()
        try:
            self.name = dbus.service.BusName(BUS_NAME, self.bus, do_not_queue=True)
        except dbus.exceptions.NameExistsException:
            log("another helper is running")
            raise SystemExit(0)
        self.cache = args.cache
        self.mirage = args.mirage
        # Apps the panel opens start where this runs, and conky runs in the
        # ConkyTray folder: a terminal opened there, not at home. PWD too,
        # which some programs believe over the real folder.
        os.chdir(HOME)
        os.environ["PWD"] = HOME
        os.makedirs(self.cache, exist_ok=True)
        os.makedirs(CONFIG, exist_ok=True)
        for name in ("daemon.log", "launcher.log", "mouse.log"):
            path = os.path.join(self.cache, name)
            try:
                if os.path.getsize(path) > 512 * 1024:
                    os.remove(path)
            except OSError:
                pass
        write_atomic(os.path.join(self.cache, "daemon.pid"), "%d\n" % os.getpid())
        self.loop = GLib.MainLoop()
        self.icons = Icons(self.cache)
        self.apps = Apps()
        self.layout, self.layout_text = {}, None
        self.windows, self.panels, self.recency, self.tick = [], [], {}, 0
        self.edges = set()             # screen edges a window reaches, from KWin
        self.groups = {}
        self.tray, self.tray_items = [], {}
        self.volume = self.network = self.battery = None
        self.theme, self.theme_running, self.theme_again = None, False, False
        self.theme_inputs, self.wallpaper_seen, self.last_theme = None, None, 0
        self.mirage_settings, self.mirage_mtime = {}, None
        self.serial, self.write_pending = 0, False
        self.menu = None
        # Clicks from before this helper started are not ours to carry out:
        # replaying them reopened every app clicked since the file began.
        try:
            self.commands_offset = os.path.getsize(os.path.join(self.cache, "commands"))
        except OSError:
            self.commands_offset = 0
        self.gone_checks = 0
        self.action_n = 0
        self.launching = {}            # app -> when it was started, until its window shows
        self.bluetooth = self.brightness = self.keyboard = self.devices = None
        self.sysbus = None
        self.bt_adapter, self.bt_devices, self.displays, self.layouts = None, [], [], []
        self.device_list, self.devices_known, self.device_popup = [], None, None
        self.soon_pending = set()
        self.windows_pending = False
        self.tray_raise = None         # a tray icon clicked, whose window may need bringing forward
        self.launcher_symbolic = False
        self.players, self.media, self.media_pick, self.media_files = {}, None, None, set()
        self.art_fetching, self.last_skip = set(), 0
        self.mic_apps, self.camera_apps, self.privacy = [], [], None
        self.capture, self.capture_failed = None, False
        self.notifier, self.notes, self.note_history, self.note_ids = None, {}, [], 0
        self.note_unread, self.notifications, self.notes_object = 0, None, None
        self.dnd = self.load_dnd()
        import collections
        self.counts = collections.Counter()   # what kept it busy, for the log
        self.cpu_seen = (time.monotonic(), os.times())
        self.theme_args = None
        self.launcher, self.launcher_glass, self.launcher_glass_running = None, None, False
        self.pins = self.load_pins()
        self.export()

    # ---------------------------------------------------------- plumbing

    def export(self):
        import dbus.service
        daemon = self

        class Tray(dbus.service.Object):
            @dbus.service.method("org.conkytray.Tray", in_signature="s")
            def Windows(self, text):
                daemon.got_windows(text)

            @dbus.service.method("org.conkytray.Tray", in_signature="ss")
            def Caption(self, ident, caption):
                daemon.got_caption(str(ident), str(caption))

            @dbus.service.method("org.conkytray.Tray", in_signature="s")
            def Edges(self, text):
                daemon.got_edges(str(text))

        self.tray_object = Tray(self.bus, "/Tray")

    def run(self):
        GLib = self.GLib
        try:
            from gi.repository import GLibUnix
            add_signal = GLibUnix.signal_add
        except ImportError:
            add_signal = GLib.unix_signal_add
        for sig in (signal.SIGTERM, signal.SIGINT):
            add_signal(GLib.PRIORITY_DEFAULT, sig, self.quit)
        GLib.idle_add(self.start)
        try:
            self.loop.run()
        finally:
            self.stop_kwin()
            for proc in getattr(self, "monitors", []) + [self.launcher, self.menu, self.capture, self.notifier]:
                if proc is not None and proc.returncode is None and proc.poll() is None:
                    proc.terminate()

    def quit(self, *unused):
        self.loop.quit()
        return False

    def start(self):
        GLib = self.GLib
        self.read_layout()
        self.serve_notifications()
        self.start_kwin()
        self.start_tray()
        self.watch_volume()
        self.watch_network()
        try:
            self.sysbus = self.dbus.SystemBus()
        except self.dbus.DBusException as error:
            log("no system bus, so no Bluetooth or devices: %s" % error)
            self.sysbus = None
        self.watch_bluetooth()
        self.watch_brightness()
        self.watch_keyboard()
        self.watch_devices()
        self.watch_media()
        self.watch_camera()
        self.read_microphone()
        self.read_battery()
        self.GLib.timeout_add(1500, lambda: (self.start_launcher(), False)[1])
        # Clicks and layout changes are announced by the kernel (inotify), so
        # the helper sleeps until something happens; the slow polls are only a
        # fallback should a notice be missed.
        reacts = {"commands": self.poll_commands, "layout": self.poll_layout}
        if not self.watch_cache(reacts):
            from gi.repository import Gio
            self.monitors_files = []
            for name, react in reacts.items():
                monitor = Gio.File.new_for_path(os.path.join(self.cache, name)).monitor_file(Gio.FileMonitorFlags.NONE, None)
                monitor.connect("changed", lambda m, f, o, event, react=react: react())
                self.monitors_files.append(monitor)
        GLib.timeout_add(1000, self.poll_commands)
        GLib.timeout_add(2000, self.poll_layout)
        GLib.timeout_add_seconds(3, self.poll_slow)
        GLib.timeout_add_seconds(30, self.poll_rare)
        self.changed()
        return False

    def watch_cache(self, reacts):
        """Have the kernel say when the panel has written one of these files,
        and react at once. Gio's file monitor does the same with a catch: after
        a notice about any other file in the folder (the helper's own state.lua,
        every icon it renders) it stops listening for 100ms, so a click often
        waited 50-100ms (measured) before the helper saw it."""
        import ctypes
        import struct
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        except (OSError, AttributeError):
            return False
        if fd < 0:
            return False
        IN_CLOSE_WRITE, IN_MOVED_TO = 0x8, 0x80
        if libc.inotify_add_watch(fd, os.fsencode(self.cache), IN_CLOSE_WRITE | IN_MOVED_TO) < 0:
            os.close(fd)
            return False

        def readable(*unused):
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                return True
            except OSError:
                return False
            names, i = set(), 0
            while i + 16 <= len(data):
                length = struct.unpack_from("iIII", data, i)[3]
                names.add(data[i + 16:i + 16 + length].split(b"\0", 1)[0].decode("utf-8", "replace"))
                i += 16 + length
            for name in names & set(reacts):
                reacts[name]()
            return True

        self.cache_watch = fd
        self.GLib.io_add_watch(fd, self.GLib.PRIORITY_DEFAULT, self.GLib.IOCondition.IN, readable)
        return True

    def changed(self):
        """Something the panel shows changed: write state.lua soon, once."""
        if not self.write_pending:
            self.write_pending = True
            self.GLib.timeout_add(30, self.write_state)

    # ------------------------------------------------------------ layout

    def read_layout(self):
        path = os.path.join(self.cache, "layout")
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            return False
        if text == self.layout_text:
            return False
        self.layout_text = text
        layout = {}
        for line in text.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                layout[k.strip()] = v.strip()
        old = self.layout
        self.layout = layout
        if any(old.get(k) != layout.get(k) for k in ("task_icon", "tray_icon", "ink", "tray_ink", "accent",
                                                     "launcher_icon", "launcher_ink")):
            self.rebuild_tasks()
            self.rebuild_tray()
            self.read_volume()
            self.read_network()
            self.read_battery()
            self.read_bluetooth()
            self.read_brightness()
            self.read_devices()
            self.update_privacy()
            self.update_bell()
        if any(old.get(k) != layout.get(k) for k in ("scale", "media_ink", "task_icon")):
            self.build_media()
        if old.get("visualizer") != layout.get("visualizer"):
            self.update_capture()
        self.request_theme()
        self.changed()
        self.maybe_device_popup()
        return True

    def lnum(self, key, default):
        try:
            return float(self.layout.get(key, default))
        except ValueError:
            return default

    def poll_layout(self):
        self.read_layout()
        return True

    def poll_slow(self):
        # Exit when the panel that started us has gone, so a helper never
        # outlives conky for long.
        pid = int(self.lnum("pid", 0))
        alive = pid > 0 and os.path.exists("/proc/%d" % pid)
        self.gone_checks = 0 if alive else self.gone_checks + 1
        if self.gone_checks >= 5:
            log("the panel has gone; stopping")
            self.quit()
            return False
        self.check_wallpaper()
        self.check_mirage()
        self.refresh_media()
        self.check_terminal()
        return True

    def poll_rare(self):
        self.read_battery()
        self.read_network()
        if time.time() - self.last_theme > 120:
            self.request_theme(force=True)
        self.check_busy()
        return True

    def check_terminal(self):
        """Konsole in the look (Mirage-Terminal, next to this folder): when
        what its colours come from changes (the look, the panel's colours,
        the palette Mirage took from the wallpaper) its tool brings Konsole up
        to date. Four timestamps a poll; the tool runs only on a change, and
        does nothing when its Konsole profile is not installed."""
        tool = os.path.join(os.path.dirname(HERE), "Mirage-Terminal", "mirage_konsole.py")
        if not os.path.isfile(tool):
            return
        stamps = []
        for path in (os.path.join(HERE, "tray.lua"), os.path.join(os.path.dirname(self.mirage), "mirage.lua"),
                     os.path.join(os.path.dirname(tool), "terminal_palette.py"),     # its `follow`
                     os.path.join(os.path.dirname(self.cache), "conky-mirage", "theme.lua"),
                     os.path.join(self.cache, "theme", "theme.lua")):
            try:
                stamps.append(os.path.getmtime(path))
            except OSError:
                stamps.append(None)
        if stamps != getattr(self, "terminal_seen", None):
            self.terminal_seen = stamps
            self.counts["terminal updates"] += 1
            detached([sys.executable, tool, "apply", "--quiet"])

    def check_busy(self):
        """If this has been busy, say what with, so it can be found."""
        then, before = self.cpu_seen
        now, times = time.monotonic(), os.times()
        used = (times.user + times.system) - (before.user + before.system)
        share = used / max(now - then, 1e-3)
        if share > 0.15:
            log("busy: %d%% of a core over %ds: %s" % (share * 100, now - then, dict(self.counts)))
        self.cpu_seen = (now, times)
        self.counts.clear()

    # ---------------------------------------------------------- commands

    def poll_commands(self):
        path = os.path.join(self.cache, "commands")
        try:
            size = os.path.getsize(path)
        except OSError:
            return True
        if size < self.commands_offset:
            self.commands_offset = 0
        if size == self.commands_offset:
            return True
        with open(path, "rb") as handle:
            handle.seek(self.commands_offset)
            data = handle.read()
        end = data.rfind(b"\n") + 1
        self.commands_offset += end
        for line in data[:end].decode("utf-8", "replace").splitlines():
            try:
                self.command(line.split("\t"))
            except Exception as error:                   # one bad click must not stop the helper
                log("command %r: %s" % (line, error))
        if self.commands_offset > 65536:
            try:
                os.remove(path)
            except OSError:
                pass
            self.commands_offset = 0
        return True

    def command(self, parts):
        what = parts[0]
        self.counts["clicks"] += 1
        self.close_menu()
        if not (what == "launcher" and parts[1] == "left") and self.launcher:
            self.tell_launcher({"cmd": "hide"})
        # A click ends with the box clicked, in the panel's pixels: x, y, w, h.
        rect = tuple(float(v) for v in parts[-4:]) if len(parts) >= 6 else (0.0, 0.0, 1.0, 1.0)
        if what == "task":
            self.task_click(parts[1], parts[2], rect)
        elif what == "tray":
            self.tray_click(parts[1], parts[2], rect)
        elif what == "scroll":
            self.scroll(parts[1], parts[2], parts[3])
        elif what == "launcher":
            self.launcher_click(parts[1], rect)
        elif what == "volume":
            self.volume_click(parts[1], rect)
        elif what == "network":
            self.network_click(parts[1], rect)
        elif what == "battery":
            if parts[1] == "left":
                detached(["systemsettings", "kcm_powerdevilprofilesconfig"])
        elif what == "clock":
            self.clock_click(parts[1], rect)
        elif what == "bluetooth":
            self.bluetooth_click(parts[1], rect)
        elif what == "brightness":
            self.brightness_click(parts[1], rect)
        elif what == "keyboard":
            self.keyboard_click(parts[1], rect)
        elif what == "devices":
            self.devices_menu(rect, passive=False)
        elif what == "media":
            self.media_click(parts[1], parts[2] if len(parts) >= 7 else "toggle", rect)
        elif what == "privacy":
            self.privacy_click(parts[1], rect)
        elif what == "notifications":
            self.notifications_click(parts[1], rect)
        # "close-menu", a click on the panel where nothing is, needs nothing more.

    # ------------------------------------------------------------- state

    def write_state(self):
        self.write_pending = False
        self.counts["state checks"] += 1
        ds = self.lnum("ds", 1.0)
        panel = self.panel_geometry()
        state = {
            "tasks": [{k: t.get(k) for k in ("key", "name", "icon", "pinned", "windows", "active", "attention",
                                             "minimized", "starting")} for t in self.tasks_list()],
            "tray": [{k: t[k] for k in ("id", "icon", "attention", "title", "symbolic")} for t in self.tray],
            "volume": self.volume, "network": self.network, "battery": self.battery,
            "bluetooth": self.bluetooth, "brightness": self.brightness, "keyboard": self.keyboard,
            "devices": self.devices, "media": self.media, "privacy": self.privacy,
            "notifications": self.notifications,
            "launcher": {"icon": self.launcher_icon(), "color": self.launcher_colour(),
                         "symbolic": self.launcher_symbolic},
            "theme": self.theme, "mirage": self.mirage_settings or None,
            "panel": {"x": panel[0], "y": panel[1], "h": panel[2], "side": self.layout.get("side")} if panel else None,
            "ds": ds,
            "attached": self.layout.get("side", "bottom") in self.edges,
        }
        text = "return " + lua(state) + "\n"
        path = os.path.join(self.cache, "state.lua")
        try:
            if open(path, encoding="utf-8").read() == text:
                return False
        except OSError:
            pass
        write_atomic(path, text)
        self.serial += 1
        write_atomic(os.path.join(self.cache, "serial"), "%d %d\n" % (os.getpid(), self.serial))
        return False

    def watch(self, proc, then):
        """Call then(exit code) when the child exits. GLib reaps it, so the
        Popen is told its code and kept until then: one dropped while its child
        still runs is reaped by subprocess's own cleanup at the next Popen, and
        GLib then warns that the child has gone. With the code set, it is also
        never signalled again, after the pid may have been reused."""
        def reaped(pid, status):
            try:
                proc.returncode = os.waitstatus_to_exitcode(status)
            except ValueError:
                proc.returncode = status
            then(proc.returncode)

        self.GLib.child_watch_add(proc.pid, reaped)

    # ------------------------------------------------------------- KWin

    def kwin(self):
        obj = self.bus.get_object("org.kde.KWin", "/Scripting")
        return self.dbus.Interface(obj, "org.kde.kwin.Scripting")

    def start_kwin(self):
        try:
            scripting = self.kwin()
            if scripting.isScriptLoaded("conkytray"):
                scripting.unloadScript("conkytray")
            scripting.loadScript(os.path.join(HERE, "tray_kwin.js"), "conkytray", signature="ss")
            scripting.start()
        except self.dbus.DBusException as error:
            log("KWin is not answering, so there is no window list: %s" % error)

    # ---------------------------------------------------------- app menu

    def start_launcher(self):
        """The app menu is its own program, kept running so it opens at once."""
        if self.launcher and self.launcher.poll() is None:
            return
        try:
            self.launcher = subprocess.Popen([sys.executable, os.path.join(HERE, "tray_launcher.py")],
                                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=open(os.path.join(self.cache, "launcher.log"), "a"),
                                             text=True, bufsize=1, preexec_fn=die_with_parent)
        except OSError as error:
            log("no app menu: %s" % error)
            self.launcher = None
            return
        proc = self.launcher

        def readable(fd, condition):
            if condition & (self.GLib.IO_HUP | self.GLib.IO_ERR):
                return False
            line = proc.stdout.readline()
            if not line:
                return False
            self.launcher_said(line.strip())
            return True

        self.GLib.io_add_watch(proc.stdout.fileno(), self.GLib.IO_IN | self.GLib.IO_HUP | self.GLib.IO_ERR, readable)

    def tell_launcher(self, spec):
        self.start_launcher()
        if not self.launcher:
            return
        try:
            self.launcher.stdin.write(json.dumps(spec) + "\n")
            self.launcher.stdin.flush()
        except (OSError, ValueError):
            self.launcher = None

    def launcher_said(self, line):
        what, _, rest = line.partition(" ")
        if what == "launch":
            self.launch(rest)
        elif what == "open":
            detached(["xdg-open", rest])
        elif what in ("pin", "unpin"):
            if what == "pin" and rest not in self.pins:
                self.pins.append(rest)
            elif what == "unpin" and rest in self.pins:
                self.pins.remove(rest)
            self.save_pins(self.pins)
            self.rebuild_tasks()
            self.changed()
        elif what == "power":
            self.session(rest)
        elif what == "settings":
            detached([sys.executable, os.path.join(HERE, "tray_settings.py")])
        elif what == "search":
            try:
                krunner = self.bus.get_object("org.kde.krunner", "/App")
                krunner.query(rest, dbus_interface="org.kde.krunner.App",
                              reply_handler=lambda *a: None, error_handler=lambda e: log(str(e)))
            except self.dbus.DBusException as error:
                log("no KRunner: %s" % error)

    def session(self, action):
        """Sleep, lock, and the logout prompts Plasma's own menu uses."""
        quiet = {"reply_handler": lambda *a: None, "error_handler": lambda e: log("%s: %s" % (action, e))}
        try:
            if action == "sleep":
                obj = self.bus.get_object("org.kde.Solid.PowerManagement",
                                          "/org/kde/Solid/PowerManagement/Actions/SuspendSession")
                obj.suspendToRam(dbus_interface="org.kde.Solid.PowerManagement.Actions.SuspendSession", **quiet)
            elif action == "lock":
                obj = self.bus.get_object("org.freedesktop.ScreenSaver", "/ScreenSaver")
                obj.Lock(dbus_interface="org.freedesktop.ScreenSaver", **quiet)
            elif action in ("logout", "reboot", "shutdown"):
                method = {"logout": "promptLogout", "reboot": "promptReboot", "shutdown": "promptShutDown"}[action]
                obj = self.bus.get_object("org.kde.LogoutPrompt", "/LogoutPrompt")
                getattr(obj, method)(dbus_interface="org.kde.LogoutPrompt", **quiet)
        except self.dbus.DBusException as error:
            log("%s: %s" % (action, error))

    def palette(self):
        """Mirage's palette as #rrggbb, read out of its theme.lua."""
        path = os.path.join(self.cache, "theme", "theme.lua")
        try:
            text = open(path, encoding="utf-8").read()
        except OSError:
            return {}
        out = {}
        for key, r, g, b in re.findall(r"\b(\w+) = \{([\d.]+), ([\d.]+), ([\d.]+)\}", text):
            out[key] = "#%02x%02x%02x" % tuple(max(0, min(255, round(float(v) * 255))) for v in (r, g, b))
        m = re.search(r'\bmode = "(\w+)"', text)
        if m:
            out["mode"] = m.group(1)
        return out

    def launcher_box(self):
        """The panel's app menu button in KWin's coordinates, or None while
        the panel's place on the screen is not known yet."""
        if not self.panel_geometry():
            return None
        try:
            rect = tuple(float(v) for v in self.layout.get("launcher_rect", "").split(","))
        except ValueError:
            return None
        return self.screen_rect(rect) if len(rect) == 4 else None

    def launcher_spot(self):
        """Where the app menu opens, in KWin's coordinates (its top left
        corner): beside the button, off the panel, on the panel's inner side."""
        box = self.launcher_box()
        if box is None:
            return None
        x, y, w, h = box
        side = self.layout.get("side", "bottom")
        if side == "bottom":
            return x, y - 6 - LAUNCHER_H
        if side == "top":
            return x, y + h + 6
        if side == "left":
            return x + w + 6, y
        return x - 6 - LAUNCHER_W, y

    def request_launcher_glass(self):
        """The menu's own frosted pane, cut from the wallpaper where it opens."""
        spot = self.launcher_spot()
        if spot is None or self.launcher_glass_running or not os.path.isfile(self.mirage):
            return
        ds = self.lnum("ds", 1.0)
        w, h = round(LAUNCHER_W * ds), round(LAUNCHER_H * ds)
        x, y = round(spot[0] * ds), round(spot[1] * ds)
        cache = os.path.join(self.cache, "launcher")
        os.makedirs(cache, exist_ok=True)
        s = self.lnum("scale", 1.0)
        m = self.mirage_settings
        argv = [sys.executable, self.mirage, "theme", "--cache", cache, "--at=%d,%d" % (x, y),
                "--window", "%dx%d" % (w, h), "--tiles", "0,0,%d,%d" % (w, h),
                "--radius", "%.2f" % (16 * ds), "--scale", "%.4f" % s,
                "--blur", "%.1f" % (m.get("blur", 34) * s), "--tint", "%.3f" % m.get("tint", 0.26)]
        self.launcher_glass_running = True
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self.launcher_glass_running = False
            return

        def done(code):
            self.launcher_glass_running = False
            path = os.path.join(cache, "glass_1.png")
            self.launcher_glass = path if os.path.isfile(path) else None

        self.watch(proc, done)

    def stop_kwin(self):
        try:
            self.kwin().unloadScript("conkytray")
        except Exception:
            pass

    def kwin_action(self, window_ids, action):
        """Do something to windows: KWin scripts cannot be called from
        outside, so each action is a one-off script."""
        body = {
            "activate": "w.minimized = false; workspace.activeWindow = w;",
            "minimize": "w.minimized = true;",
            "close": "w.closeWindow();",
        }[action]
        self.action_n += 1
        name = "conkytray-action-%d-%d" % (os.getpid(), self.action_n)
        path = os.path.join(self.cache, name + ".js")
        write_atomic(path, "var ids = %s;\nworkspace.windowList().forEach(function (w) {\n"
                           "    if (ids.indexOf(w.internalId.toString()) >= 0) { %s }\n});\n"
                     % (json.dumps(list(window_ids)), body))

        def cleanup():
            try:
                self.kwin().unloadScript(name)
            except Exception:
                pass
            try:
                os.remove(path)
            except OSError:
                pass
            return False

        # Started with Scripting.start(), which runs every loaded script that
        # has not run, and not through /Scripting/Script<id>: KWin numbers a
        # script by how many are loaded, so once an older one is unloaded a new
        # one can get the number of one still loaded, and run() then goes to
        # that one, already run. A click then did nothing, until a later one
        # happened to get a free number (measured: three in a row lost).
        try:
            scripting = self.kwin()
            scripting.loadScript(path, name, signature="ss")
            scripting.start(reply_handler=lambda: None, error_handler=lambda e: log(str(e)))
        except self.dbus.DBusException as error:
            log("KWin action failed: %s" % error)
        self.GLib.timeout_add(1500, cleanup)

    def got_windows(self, text):
        self.counts["window lists"] += 1
        try:
            data = json.loads(text)
        except ValueError:
            return
        self.windows = data.get("windows", [])
        self.panels = data.get("panels", [])
        for w in self.windows:
            if w.get("active") and self.recency.get(w["id"], -1) != self.tick:
                self.tick += 1
                self.recency[w["id"]] = self.tick
        # A burst of changes (activating a window changes two) is one rebuild.
        if not self.windows_pending:
            self.windows_pending = True
            self.GLib.timeout_add(50, self.windows_settled)

    def windows_settled(self):
        if not self.windows_pending:
            return False                    # a click settled them already
        self.windows_pending = False
        self.rebuild_tasks()
        self.raise_for_tray()
        panel = self.panel_geometry()
        if panel != getattr(self, "panel_seen", None):
            self.panel_seen = panel
            self.request_theme()
            self.launcher_glass = None
        self.changed()
        return False

    def got_edges(self, text):
        """The screen edges some window reaches: the panel along one of them
        stops floating (no gap round it, no rounded ends), as Plasma's does."""
        edges = {e for e in text.split(",") if e}
        if edges != self.edges:
            self.edges = edges
            self.changed()

    def got_caption(self, ident, caption):
        """Only menus show titles, so a new one is noted and nothing redrawn."""
        self.counts["title changes"] += 1
        for w in self.windows:
            if w["id"] == ident:
                w["caption"] = caption

    def panel_geometry(self):
        """Where our panel is on the screen, in pixels, from KWin."""
        pid = int(self.lnum("pid", 0))
        ds = self.lnum("ds", 1.0)
        for p in self.panels:
            if int(p.get("pid", -1)) == pid:
                return (int(round(p["x"] * ds)), int(round(p["y"] * ds)), int(round(p["h"] * ds)))
        return None

    # ------------------------------------------------------------ tasks

    def load_pins(self):
        path = os.path.join(CONFIG, "pinned")
        if os.path.exists(path):
            return [x.strip() for x in open(path, encoding="utf-8") if x.strip() and not x.startswith("#")]
        pins = [p for p in (self.apps.resolve(x) for x in plasma_launchers()) if p]
        if not pins:
            pins = [p for p in (self.apps.resolve("preferred://filemanager"),
                                self.apps.resolve("preferred://browser"),
                                self.apps.resolve("applications:org.kde.konsole.desktop")) if p]
        self.save_pins(pins)
        return pins

    def save_pins(self, pins):
        write_atomic(os.path.join(CONFIG, "pinned"),
                     "# Apps pinned to ConkyTray, one desktop file name per line.\n" + "".join(p + "\n" for p in pins))

    def rebuild_tasks(self):
        groups = {}
        order = []
        for pin in self.pins:
            groups[pin] = {"key": pin, "pinned": True, "list": []}
            order.append(pin)
        for w in self.windows:
            app = self.apps.match(w.get("desktop"), w.get("cls"), w.get("name"), self.pins)
            key = app or "wm:" + (w.get("cls") or w.get("name") or w.get("caption") or "?")
            if key not in groups:
                groups[key] = {"key": key, "pinned": False, "list": []}
                order.append(key)
            groups[key]["list"].append(w)
        size = int(self.lnum("task_icon", 0))
        for key in order:
            g = groups[key]
            entry = self.apps.entries.get(key)
            first = g["list"][0] if g["list"] else {}
            g["name"] = (entry or {}).get("name") or first.get("caption") or first.get("cls") or key
            g["windows"] = len(g["list"])
            g["active"] = any(w.get("active") for w in g["list"])
            g["attention"] = any(w.get("attention") for w in g["list"])
            g["minimized"] = bool(g["list"]) and all(w.get("minimized") for w in g["list"])
            if g["list"]:
                self.launching.pop(key, None)
            g["starting"] = key in self.launching
            g["icon"] = None
            if size > 0:
                names = [(entry or {}).get("icon"), (first.get("cls") or "").lower(),
                         (first.get("desktop") or "").lower(), "application-x-executable"]
                for name in names:
                    path = self.icons.find(name, size) if name else None
                    if path:
                        g["icon"] = self.icons.render(path, size, self.layout.get("ink"), self.layout.get("accent"))
                        if g["icon"]:
                            break
        self.groups = groups
        self.order = order

    def tasks_list(self):
        return [self.groups[k] for k in getattr(self, "order", []) if k in self.groups]

    def launch(self, key):
        entry = self.apps.entries.get(key)
        if not entry:
            return
        self.launching[key] = time.time()

        def settle():
            # Gave up waiting for a window (some apps only start a tray icon).
            if time.time() - self.launching.get(key, 0) >= 5.9:
                self.launching.pop(key, None)
                self.rebuild_tasks()
                self.changed()
            return False

        self.GLib.timeout_add(6000, settle)
        self.rebuild_tasks()
        self.changed()
        # kstart takes the name without ".desktop" (org.kde.dolphin); with it,
        # it finds nothing and starts nothing. If it fails, gtk-launch.
        launchers = [["kstart", "--application", key]] if shutil.which("kstart") else []
        launchers += [["gtk-launch", key]] if shutil.which("gtk-launch") else []
        launchers += [["kioclient", "exec", entry["path"]]] if shutil.which("kioclient") else []
        self.start_first(launchers)

    def start_first(self, launchers):
        """Run the first launcher; if it exits with an error, the next."""
        if not launchers:
            log("nothing to start apps with (kstart, gtk-launch, kioclient)")
            return
        argv = launchers[0]
        try:
            # No pipes: the app it starts may inherit them and block on a full one.
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            self.start_first(launchers[1:])
            return

        finished = [False]

        def done(code):
            finished[0] = True
            if code != 0:
                log("%s did not start it (exit status %d)" % (argv[0], code))
                self.start_first(launchers[1:])

        def stuck():
            # kstart hangs, rather than exiting, when it cannot find the app.
            if not finished[0]:
                log("%s hung; trying the next way" % argv[0])
                proc.kill()
            return False

        self.watch(proc, done)
        self.GLib.timeout_add_seconds(8, stuck)

    def task_click(self, button, key, rect):
        if self.windows_pending:
            self.windows_settled()          # decide on the newest window list
        g = self.groups.get(key)
        if not g:
            return
        windows = g["list"]
        if button == "left":
            if not windows:
                # A second click while it is still starting would start it twice.
                if time.time() - self.launching.get(key, 0) > 6:
                    self.launch(key)
                return
            active = [w for w in windows if w.get("active")]
            if active and len(windows) == 1:
                self.kwin_action([windows[0]["id"]], "minimize")
            elif active:
                i = windows.index(active[0])
                self.kwin_action([windows[(i + 1) % len(windows)]["id"]], "activate")
            else:
                target = max(windows, key=lambda w: self.recency.get(w["id"], 0))
                self.kwin_action([target["id"]], "activate")
        elif button == "middle":
            self.launch(key)
        elif button == "right":
            items = [{"header": True, "label": g["name"]}]
            if len(windows) > 1:
                for w in windows:
                    items.append({"id": "win:" + w["id"], "label": w.get("caption") or g["name"],
                                  "checked": bool(w.get("active"))})
                items.append({"separator": True})
            if key in self.apps.entries:
                items.append({"id": "launch", "label": "New window" if windows else "Open"})
                items.append({"id": "unpin" if g["pinned"] else "pin",
                              "label": "Unpin from the panel" if g["pinned"] else "Pin to the panel"})
            if windows:
                items.append({"separator": True})
                items.append({"id": "close", "label": "Close window" if len(windows) == 1 else
                              "Close all %d windows" % len(windows)})
            ids = [w["id"] for w in windows]

            def chosen(choice):
                if choice == "launch":
                    self.launch(key)
                elif choice == "pin" and key not in self.pins:
                    self.pins.append(key)
                    self.save_pins(self.pins)
                elif choice == "unpin" and key in self.pins:
                    self.pins.remove(key)
                    self.save_pins(self.pins)
                elif choice == "close":
                    self.kwin_action(ids, "close")
                elif choice.startswith("win:"):
                    self.kwin_action([choice[4:]], "activate")
                self.rebuild_tasks()
                self.changed()

            self.open_menu(items, rect, chosen)

    # ------------------------------------------------------------- tray

    SNI = "org.kde.StatusNotifierItem"
    CATEGORY = {"ApplicationStatus": 0, "Communications": 1, "SystemServices": 2, "Hardware": 3}

    def start_tray(self):
        dbus = self.dbus
        self.bus.add_signal_receiver(self.item_registered, "StatusNotifierItemRegistered",
                                     "org.kde.StatusNotifierWatcher")
        self.bus.add_signal_receiver(self.item_unregistered, "StatusNotifierItemUnregistered",
                                     "org.kde.StatusNotifierWatcher")
        self.bus.add_signal_receiver(self.watcher_owner, "NameOwnerChanged", "org.freedesktop.DBus",
                                     arg0="org.kde.StatusNotifierWatcher")
        try:
            watcher = self.bus.get_object("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher")
            dbus.Interface(watcher, "org.kde.StatusNotifierWatcher").RegisterStatusNotifierHost(BUS_NAME)
            items = watcher.Get("org.kde.StatusNotifierWatcher", "RegisteredStatusNotifierItems",
                                dbus_interface="org.freedesktop.DBus.Properties")
            for item in items:
                self.item_registered(str(item))
        except dbus.DBusException as error:
            log("no tray watcher, so no tray icons: %s" % error)

    def watcher_owner(self, name, old, new):
        if new:
            self.GLib.timeout_add(500, lambda: (self.start_tray(), False)[1])

    def item_registered(self, full):
        full = str(full)
        if full in self.tray_items:
            return
        cut = full.find("/")
        service, path = (full[:cut], full[cut:]) if cut > 0 else (full, "/StatusNotifierItem")
        item = {"full": full, "service": service, "path": path, "props": {}, "id": "i%d" % (len(self.tray_items) + 1)}
        while any(i["id"] == item["id"] for i in self.tray_items.values()):
            item["id"] = "i%d" % (int(item["id"][1:]) + 1)
        item["match"] = self.bus.add_signal_receiver(lambda *a, **k: self.item_changed(full), None, self.SNI,
                                                     service, path)
        self.tray_items[full] = item
        self.item_changed(full)

    def item_unregistered(self, full):
        item = self.tray_items.pop(str(full), None)
        if item:
            item["match"].remove()
            self.rebuild_tray()
            self.changed()

    def item_changed(self, full):
        item = self.tray_items.get(full)
        if not item:
            return
        try:
            obj = self.bus.get_object(item["service"], item["path"], introspect=False)
        except self.dbus.DBusException:
            return

        def got(props):
            if full in self.tray_items:
                item["props"] = dict(props)
                self.rebuild_tray()
                self.changed()

        obj.GetAll(self.SNI, dbus_interface="org.freedesktop.DBus.Properties",
                   reply_handler=got, error_handler=lambda e: None)

    def rebuild_tray(self):
        size = int(self.lnum("tray_icon", 0))
        out = []
        for item in self.tray_items.values():
            p = item["props"]
            status = str(p.get("Status", "Active"))
            if status == "Passive" or not p:
                continue
            attention = status == "NeedsAttention"
            icon = None
            if size > 0:
                themes = [str(p.get("IconThemePath", ""))] if p.get("IconThemePath") else []
                name = str(p.get("AttentionIconName" if attention else "IconName", "") or p.get("IconName", ""))
                path = self.icons.find(name, size, extra=[t for t in themes if t]) if name else None
                if path:
                    icon = self.icons.render(path, size, self.layout.get("tray_ink"), self.layout.get("accent"))
                if not icon:
                    pix = p.get("AttentionIconPixmap" if attention else "IconPixmap") or p.get("IconPixmap")
                    icon = self.icons.pixmap(list(pix) if pix else [], size)
            title = str(p.get("Title", "") or p.get("Id", ""))
            out.append({"id": item["id"], "icon": icon, "attention": attention, "title": title,
                        "symbolic": self.one_colour(icon),
                        "sort": (self.CATEGORY.get(str(p.get("Category", "")), 9), str(p.get("Id", "")).lower()),
                        "full": item["full"]})
        out.sort(key=lambda t: t["sort"])
        self.tray = out

    def one_colour(self, path):
        """Whether an icon is one plain colour, white, grey or black (Claude's
        and Telegram's tray icons are white): the panel then paints it in its
        own ink, so it reads on a light block too. Remembered per file."""
        if not path:
            return False
        known = getattr(self, "one_colour_seen", None)
        if known is None:
            known = self.one_colour_seen = {}
        if path not in known:
            plain = False
            try:
                from PIL import Image
                image = Image.open(path).convert("RGBA")
                pixels = image.get_flattened_data() if hasattr(image, "get_flattened_data") else image.getdata()
                solid = [px for px in pixels if px[3] > 160]
                if len(solid) >= 12:
                    lows = [min(px[:3]) for px in solid]
                    highs = [max(px[:3]) for px in solid]
                    # grey (every pixel's channels close) and all one shade
                    plain = (max(h - l for h, l in zip(highs, lows)) < 40
                             and max(highs) - min(highs) < 48)
            except Exception:
                plain = False
            known[path] = plain
        return known[path]

    def tray_item(self, ident):
        for t in self.tray:
            if t["id"] == ident:
                return self.tray_items.get(t["full"])
        return None

    def screen_rect(self, rect):
        """A box in the panel's pixels as KWin's coordinates (what tray items
        and menus want)."""
        ds = self.lnum("ds", 1.0)
        panel = self.panel_geometry() or (0, 0, 0)
        x, y, w, h = rect
        return ((panel[0] + x) / ds, (panel[1] + y) / ds, w / ds, h / ds)

    def screen_point(self, rect):
        x, y, w, h = self.screen_rect(rect)
        return int(x + w / 2), int(y + h / 2)

    def tray_click(self, button, ident, rect):
        item = self.tray_item(ident)
        if not item:
            return
        p = item["props"]
        obj = self.bus.get_object(item["service"], item["path"], introspect=False)
        x, y = self.screen_point(rect)
        menu_path = str(p.get("Menu", "") or "")
        ignore = {"reply_handler": lambda *a: None, "error_handler": lambda e: None}

        def show_menu():
            if menu_path and menu_path != "/":
                self.open_menu(None, rect, None, dbusmenu=(item["service"], menu_path))
            else:
                obj.ContextMenu(x, y, dbus_interface=self.SNI, **ignore)

        if button == "left":
            if p.get("ItemIsMenu"):
                show_menu()
            else:
                # Plasma hands the app a token with Activate that lets it bring
                # its window forward; a panel that is not Plasma cannot get
                # one, and KWin will not let the app do it without (ZapZap's
                # window stayed minimised or behind). So unless the app's
                # window is already in front (then Activate usually hides it),
                # the helper brings it forward itself once the app has answered.
                self.tray_raise = None
                if not any(w.get("active") for w in self.item_windows(item)):
                    now = time.monotonic()
                    self.tray_raise = {"item": ident, "from": now + 0.25, "until": now + 2.5}

                def answered(*unused):
                    self.GLib.timeout_add(260, self.raise_for_tray)

                # An item that has no Activate answers with an error; its
                # menu is then the next best thing.
                obj.Activate(x, y, dbus_interface=self.SNI, reply_handler=answered,
                             error_handler=lambda e: (setattr(self, "tray_raise", None), show_menu()))
        elif button == "middle":
            obj.SecondaryActivate(x, y, dbus_interface=self.SNI, **ignore)
        elif button == "right":
            show_menu()

    def item_windows(self, item):
        """The windows of the app behind a tray icon: by its process, or by its
        name, or for a flatpak (whose D-Bus may come through a proxy, another
        process) by its app id."""
        if "pid" not in item:
            item["pid"], item["names"] = 0, set()
            try:
                dbus_obj = self.bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
                item["pid"] = int(dbus_obj.GetConnectionUnixProcessID(item["service"],
                                                                      dbus_interface="org.freedesktop.DBus"))
            except (self.dbus.DBusException, ValueError, TypeError):
                pass
            ident = str(item["props"].get("Id") or "").lower()
            if ident:
                item["names"].add(ident)
            try:
                found = re.search(r"^name=(\S+)", open("/proc/%d/root/.flatpak-info" % item["pid"]).read(), re.M)
                if found:
                    item["names"].add(found.group(1).lower())
            except OSError:
                pass
        pid, names = item["pid"], item["names"]
        return [w for w in self.windows
                if (pid and w.get("pid") == pid)
                or names & {str(w.get(k) or "").lower() for k in ("cls", "desktop", "name")}]

    def raise_for_tray(self):
        """After a tray icon's Activate, and after each window list until a
        couple of seconds have passed: if the app now has a window and none of
        them is in front, bring forward its latest. Waits a moment first, for
        an app that shows a hidden window to show it, or hides one."""
        want = self.tray_raise
        if not want:
            return False
        now = time.monotonic()
        item = self.tray_item(want["item"])
        if not item or now > want["until"]:
            self.tray_raise = None
            return False
        if now < want["from"]:
            return False
        mine = self.item_windows(item)
        if any(w.get("active") for w in mine):
            self.tray_raise = None          # it managed by itself
        elif mine:
            self.tray_raise = None
            target = max(mine, key=lambda w: self.recency.get(w["id"], 0))
            self.kwin_action([target["id"]], "activate")
        return False

    # ------------------------------------------------------------ menus

    def palette_for_menu(self):
        return {k: self.layout.get("menu_" + k) for k in ("bg", "fg", "accent", "edge", "hover", "muted", "radius")}

    def open_menu(self, items, rect, chosen, dbusmenu=None, passive=False):
        """Show a menu beside the panel, by the box that was clicked."""
        self.close_menu()
        spec = {"side": self.layout.get("side", "bottom"), "rect": self.screen_rect(rect), "passive": passive,
                "placed": bool(self.panel_geometry()), "colors": self.palette_for_menu(), "items": items}
        if dbusmenu:
            spec["dbusmenu"] = {"service": dbusmenu[0], "path": dbusmenu[1]}
        try:
            proc = subprocess.Popen([sys.executable, os.path.join(HERE, "tray_menu.py"), json.dumps(spec)],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                    preexec_fn=die_with_parent)
        except OSError as error:
            log("no menu: %s" % error)
            return
        self.menu = proc

        def done(code):
            out = proc.stdout.read().strip() if proc.stdout else ""
            if self.menu is proc:
                self.menu = None
            if out and chosen:
                chosen(out)

        self.watch(proc, done)

    def close_menu(self):
        # returncode, not poll(): poll() would reap it under GLib's child watch,
        # which then warns that the child has gone.
        if self.menu and self.menu.returncode is None:
            try:
                self.menu.terminate()
            except OSError:
                pass
        self.menu = None

    def launcher_icon(self):
        """The button's logo, one colour on its tile, as Plasma's own: the one
        set in Plasma's app menu, else the Plasma logo."""
        size = int(self.lnum("launcher_icon", 0))
        if size <= 0:
            return None
        for name in (self.layout.get("launcher_name") or "", plasma_kickoff_icon() or "",
                     "start-here-kde-plasma-symbolic", "start-here-kde-symbolic", "start-here-symbolic",
                     "start-here-kde-plasma", "start-here-kde", "start-here"):
            if name and name != "auto":
                path = self.icons.find(name, size)
                if path and os.path.basename(path).startswith(name):
                    # One colour (Plasma's logo is): the panel paints it in
                    # whatever reads on the button.
                    self.launcher_symbolic = "symbolic" in os.path.basename(path)
                    return self.icons.render(path, size, self.layout.get("launcher_ink"), self.layout.get("accent"))
        return None

    # The colours the app menu's button may wear, by look. Metro leaves out
    # metro2, which the apps block next to it wears.
    BUTTON_COLOURS = {"metro": ("metro1", "metro3", "metro4"), "pixel": ("px_primary", "px_tertiary_c", "px_secondary_c"),
                      "glass": ("accent", "accent2"), "solid": ("accent", "accent2")}

    def launcher_colour(self):
        """The colour of the app menu's button: of those its look offers, the
        one furthest from the wallpaper behind it, so it never melts into it."""
        look = self.layout.get("look", "glass")
        try:
            rect = [int(float(v)) for v in self.layout.get("launcher_rect", "").split(",")]
            under = os.path.join(self.cache, "theme", "under.png")
            stamp = (look, tuple(rect), os.path.getmtime(under), self.theme and self.theme.get("stamp"))
        except (ValueError, OSError):
            return None
        if getattr(self, "button_pick", (None, None))[0] == stamp:
            return self.button_pick[1]
        pick = None
        try:
            from PIL import Image, ImageStat
            x, y, w, h = rect
            mean = ImageStat.Stat(Image.open(under).convert("RGB").crop((x, y, x + w, y + h))).mean
            behind = oklab(*[v / 255 for v in mean])
            palette = self.palette()
            far = -1
            for key in self.BUTTON_COLOURS.get(look, ("accent",)):
                colour = palette.get(key)
                if not colour:
                    continue
                lab = oklab(*[int(colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)])
                d = sum((p - q) ** 2 for p, q in zip(lab, behind))
                if d > far:
                    far, pick = d, colour
        except Exception as error:                      # a colour is not worth failing over
            # (Most likely the wallpaper image half written: next time, again.)
            log("button colour: %s" % error)
            return None
        self.button_pick = (stamp, pick)
        return pick

    def launcher_click(self, button, rect):
        if button == "left":
            if self.launcher_glass is None:
                self.request_launcher_glass()
            self.tell_launcher({"cmd": "toggle", "side": self.layout.get("side", "bottom"),
                                "rect": self.launcher_box() or self.screen_rect(rect),
                                "look": self.layout.get("look", "glass"), "palette": self.palette(),
                                "glass": self.launcher_glass, "pins": self.pins})
        elif button == "right":
            items = [{"id": "settings", "label": "System Settings"},
                     {"separator": True},
                     {"id": "lock", "label": "Lock the screen"},
                     {"id": "logout", "label": "Log out…"},
                     {"id": "reboot", "label": "Restart…"},
                     {"id": "shutdown", "label": "Shut down…"}]

            def chosen(choice):
                if choice == "settings":
                    detached(["systemsettings"])
                else:
                    self.session(choice)

            self.open_menu(items, rect, chosen)

    def clock_click(self, button, rect):
        if button == "right":
            items = [{"id": "settings", "label": "Date and time settings"}]
            self.open_menu(items, rect, lambda c: detached(["systemsettings", "kcm_clock"]))

    # ------------------------------------------------------------ volume

    def watch_volume(self):
        # Only the output itself ("on sink #3"), not every app playing sound
        # ("on sink-input #..."), which during playback is a steady stream.
        def wanted(line):
            # A recording stream starting or stopping: the microphone's light.
            if b"source-output" in line:
                self.soon("microphone", self.read_microphone, 300)
                return False
            if b" on server" in line and self.capture is not None:
                self.soon("capture", self.restart_capture, 600)    # perhaps a new default output
            return b" on sink #" in line or b" on server" in line

        self.status_watch(["pactl", "subscribe"], wanted, self.read_volume, 150)
        self.read_volume()

    def status_watch(self, argv, wanted, refresh, delay):
        """Run a monitor (pactl subscribe, nmcli monitor) and refresh when it
        reports something relevant, at most every `delay` ms."""
        GLib = self.GLib
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    preexec_fn=die_with_parent)
        except OSError:
            return
        pending = [False]

        def fire():
            pending[0] = False
            refresh()
            return False

        def readable(fd, condition):
            if condition & (GLib.IO_HUP | GLib.IO_ERR):
                return False
            line = proc.stdout.readline()
            if not line:
                return False
            if wanted(line) and not pending[0]:
                pending[0] = True
                GLib.timeout_add(delay, fire)
            return True

        GLib.io_add_watch(proc.stdout.fileno(), GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, readable)
        self.monitors = getattr(self, "monitors", []) + [proc]

    def read_volume(self):
        self.counts["volume checks"] += 1
        text = output_of(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"])
        m = re.search(r"Volume:\s*([\d.]+)", text)
        if not m:
            self.volume = None
        else:
            level = round(float(m.group(1)) * 100)
            muted = "MUTED" in text
            name = ("audio-volume-muted" if muted or level == 0 else "audio-volume-low" if level < 34 else
                    "audio-volume-medium" if level < 67 else "audio-volume-high")
            self.volume = {"level": level, "muted": muted, "icon": self.status_icon(name)}
        self.changed()

    def status_icon(self, name, *more):
        size = int(self.lnum("tray_icon", 0))
        if size <= 0:
            return None
        for base in (name,) + tuple(more):
            for n in (base + "-symbolic", base):
                path = self.icons.find(n, size)
                # Only the icon asked for, not the theme's nearest by name
                # ("network-bluetooth" falling back to "network").
                if path and os.path.basename(path).startswith(n):
                    return self.icons.render(path, size, self.layout.get("tray_ink"), self.layout.get("accent"))
        return None

    def volume_click(self, button, rect):
        if button in ("left", "middle"):
            subprocess.run(["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "toggle"], timeout=3)
            self.read_volume()
        elif button == "right":
            muted = self.volume and self.volume["muted"]
            items = [{"header": True, "label": "Volume %d%%" % self.volume["level"] if self.volume else "Sound"},
                     {"id": "mute", "label": "Unmute" if muted else "Mute"},
                     {"id": "settings", "label": "Sound settings"}]

            def chosen(choice):
                if choice == "mute":
                    self.volume_click("left", rect)
                elif choice == "settings":
                    detached(["systemsettings", "kcm_pulseaudio"])

            self.open_menu(items, rect, chosen)

    def scroll(self, target, ident, direction):
        if target == "volume":
            step = {"up": "5%+", "down": "5%-"}.get(direction)
            if step:
                subprocess.run(["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", step], timeout=3)
                self.read_volume()
        elif target == "brightness" and direction in ("up", "down"):
            self.set_brightness((self.brightness or {}).get("level", 50) + (5 if direction == "up" else -5))
        elif target == "keyboard" and direction in ("up", "down") and self.keyboard:
            self.set_layout((self.keyboard["index"] + (1 if direction == "down" else -1)) % len(self.layouts))
        elif target == "media" and direction in ("up", "down"):
            # A wheel or a touchpad sends a burst; one skip per burst.
            if time.monotonic() - self.last_skip > 0.6:
                self.last_skip = time.monotonic()
                self.player_call("Next" if direction == "down" else "Previous")
        elif target == "tray":
            item = self.tray_item(ident)
            if item and direction in ("up", "down"):
                obj = self.bus.get_object(item["service"], item["path"], introspect=False)
                obj.Scroll(120 if direction == "up" else -120, "vertical", dbus_interface=self.SNI,
                           reply_handler=lambda *a: None, error_handler=lambda e: None)

    def soon(self, key, refresh, delay=150):
        """Run `refresh` once, shortly: services announce changes in bursts."""
        if key in self.soon_pending:
            return
        self.soon_pending.add(key)

        def fire():
            self.soon_pending.discard(key)
            refresh()
            return False

        self.GLib.timeout_add(delay, fire)

    # --------------------------------------------------------- bluetooth

    PROPS = "org.freedesktop.DBus.Properties"
    MANAGER = "org.freedesktop.DBus.ObjectManager"

    def watch_bluetooth(self):
        if not self.sysbus:
            return
        for name, iface in (("PropertiesChanged", self.PROPS), ("InterfacesAdded", self.MANAGER),
                            ("InterfacesRemoved", self.MANAGER)):
            self.sysbus.add_signal_receiver(lambda *a, **k: self.soon("bluetooth", self.read_bluetooth),
                                            name, iface, "org.bluez")
        self.read_bluetooth()

    def read_bluetooth(self):
        if not self.sysbus:
            return
        self.counts["bluetooth checks"] += 1

        def failed(error):
            self.bluetooth = None
            self.changed()

        try:
            obj = self.sysbus.get_object("org.bluez", "/", introspect=False)
            obj.GetManagedObjects(dbus_interface=self.MANAGER, reply_handler=self.got_bluetooth,
                                  error_handler=failed)
        except self.dbus.DBusException as error:
            failed(error)

    def got_bluetooth(self, objects):
        adapter, powered, devices = None, False, []
        for path, interfaces in sorted(objects.items()):
            if "org.bluez.Adapter1" in interfaces and adapter is None:
                adapter = str(path)
                powered = bool(interfaces["org.bluez.Adapter1"].get("Powered", False))
            device = interfaces.get("org.bluez.Device1")
            if device and device.get("Paired"):
                devices.append({"path": str(path), "name": str(device.get("Alias") or device.get("Name") or "Device"),
                                "connected": bool(device.get("Connected")), "icon": str(device.get("Icon", ""))})
        self.bt_adapter, self.bt_devices = adapter, devices
        if adapter is None:
            self.bluetooth = None
        else:
            connected = sum(1 for d in devices if d["connected"])
            names = (("network-bluetooth-activated", "bluetooth-active", "network-bluetooth") if powered and connected
                     else ("network-bluetooth", "bluetooth-active") if powered
                     else ("network-bluetooth-inactive", "bluetooth-disabled", "network-bluetooth"))
            self.bluetooth = {"powered": powered, "connected": connected, "icon": self.status_icon(*names)}
        self.changed()

    def bluetooth_click(self, button, rect):
        if not self.bluetooth:
            return
        on = self.bluetooth["powered"]
        items = [{"header": True, "label": "Bluetooth is on" if on else "Bluetooth is off"},
                 {"id": "power", "label": "Turn Bluetooth off" if on else "Turn Bluetooth on"}]
        if on and self.bt_devices:
            items.append({"separator": True})
            for d in self.bt_devices:
                items.append({"id": "dev:" + d["path"], "label": d["name"], "checked": d["connected"],
                              "icon_name": d["icon"] or None})
        items += [{"separator": True}, {"id": "settings", "label": "Bluetooth settings"}]
        quiet = {"reply_handler": lambda *a: None, "error_handler": lambda e: log("Bluetooth: %s" % e)}

        def chosen(choice):
            if choice == "power" and self.bt_adapter:
                adapter = self.sysbus.get_object("org.bluez", self.bt_adapter, introspect=False)
                adapter.Set("org.bluez.Adapter1", "Powered", self.dbus.Boolean(not on, variant_level=1),
                            dbus_interface=self.PROPS, **quiet)
            elif choice.startswith("dev:"):
                device = next((d for d in self.bt_devices if d["path"] == choice[4:]), None)
                if device:
                    obj = self.sysbus.get_object("org.bluez", device["path"], introspect=False)
                    # Connecting can take a while; it answers when done.
                    getattr(obj, "Disconnect" if device["connected"] else "Connect")(
                        dbus_interface="org.bluez.Device1", timeout=40, **quiet)
            elif choice == "settings":
                detached(["systemsettings", "kcm_bluetooth"])

        self.open_menu(items, rect, chosen)

    # -------------------------------------------------------- brightness

    def watch_brightness(self):
        self.bus.add_signal_receiver(lambda *a, **k: self.soon("brightness", self.read_brightness),
                                     "PropertiesChanged", self.PROPS, "org.kde.ScreenBrightness")
        self.bus.add_signal_receiver(lambda *a, **k: self.soon("brightness", self.read_brightness),
                                     None, "org.kde.ScreenBrightness", "org.kde.ScreenBrightness")
        self.read_brightness()

    def read_brightness(self):
        """The screens whose brightness Plasma can set, and the first one's level."""
        self.counts["brightness checks"] += 1
        displays = []
        try:
            root = self.bus.get_object("org.kde.ScreenBrightness", "/org/kde/ScreenBrightness", introspect=False)
            for name in root.Get("org.kde.ScreenBrightness", "DisplaysDBusNames", dbus_interface=self.PROPS):
                obj = self.bus.get_object("org.kde.ScreenBrightness", "/org/kde/ScreenBrightness/" + str(name),
                                          introspect=False)
                props = obj.GetAll("org.kde.ScreenBrightness.Display", dbus_interface=self.PROPS)
                if int(props.get("MaxBrightness", 0)) > 0:
                    displays.append({"obj": obj, "label": str(props.get("Label", "")),
                                     "value": int(props["Brightness"]), "max": int(props["MaxBrightness"])})
        except self.dbus.DBusException:
            displays = []
        self.displays = displays
        if not displays:
            self.brightness = None
        else:
            level = round(100 * displays[0]["value"] / displays[0]["max"])
            self.brightness = {"level": level, "icon": self.status_icon("display-brightness", "brightness-high",
                                                                        "video-display-brightness", "video-display")}
        self.changed()

    def set_brightness(self, percent):
        """Every screen to `percent`; Plasma shows its own level indicator."""
        percent = max(1, min(100, int(percent)))
        for d in self.displays:
            d["obj"].SetBrightness(self.dbus.Int32(round(d["max"] * percent / 100)), self.dbus.UInt32(0),
                                   dbus_interface="org.kde.ScreenBrightness.Display",
                                   reply_handler=lambda *a: None, error_handler=lambda e: log("brightness: %s" % e))

    def brightness_click(self, button, rect):
        if not self.brightness:
            return
        level = self.brightness["level"]
        items = [{"header": True, "label": "Brightness %d%%" % level}]
        for p in (100, 75, 50, 25):
            items.append({"id": "set:%d" % p, "label": "%d%%" % p, "checked": abs(level - p) < 3})
        items += [{"separator": True}, {"id": "settings", "label": "Display settings"}]

        def chosen(choice):
            if choice.startswith("set:"):
                self.set_brightness(int(choice[4:]))
            elif choice == "settings":
                detached(["systemsettings", "kcm_kscreen"])

        self.open_menu(items, rect, chosen)

    # ---------------------------------------------------- keyboard layout

    def watch_keyboard(self):
        self.bus.add_signal_receiver(lambda *a, **k: self.soon("keyboard", self.read_keyboard, 50),
                                     None, "org.kde.KeyboardLayouts", "org.kde.keyboard", "/Layouts")
        self.read_keyboard()

    def keyboard_obj(self):
        return self.bus.get_object("org.kde.keyboard", "/Layouts", introspect=False)

    def read_keyboard(self):
        """The layout in use, shown as its short name ("us"), as Plasma's; only
        when there is more than one to switch between."""
        try:
            obj = self.keyboard_obj()
            current = int(obj.getLayout(dbus_interface="org.kde.KeyboardLayouts"))
            self.layouts = [(str(a), str(b), str(c)) for a, b, c in obj.getLayoutsList(dbus_interface="org.kde.KeyboardLayouts")]
        except (self.dbus.DBusException, ValueError):
            self.layouts, current = [], 0
        if len(self.layouts) < 2 or not 0 <= current < len(self.layouts):
            self.keyboard = None
        else:
            short, variant, name = self.layouts[current]
            self.keyboard = {"label": short, "name": name, "index": current}
        self.changed()

    def set_layout(self, index):
        try:
            self.keyboard_obj().setLayout(self.dbus.UInt32(index), dbus_interface="org.kde.KeyboardLayouts",
                                          reply_handler=lambda *a: None, error_handler=lambda e: log(str(e)))
        except self.dbus.DBusException as error:
            log("keyboard layout: %s" % error)

    def keyboard_click(self, button, rect):
        if not self.keyboard:
            return
        if button == "left":
            self.set_layout((self.keyboard["index"] + 1) % len(self.layouts))
        elif button == "right":
            items = [{"id": "layout:%d" % i, "label": name or short, "checked": i == self.keyboard["index"]}
                     for i, (short, variant, name) in enumerate(self.layouts)]
            items += [{"separator": True}, {"id": "settings", "label": "Keyboard settings"}]

            def chosen(choice):
                if choice.startswith("layout:"):
                    self.set_layout(int(choice[7:]))
                elif choice == "settings":
                    detached(["systemsettings", "kcm_keyboard"])

            self.open_menu(items, rect, chosen)

    # ------------------------------------------------------------- media

    MPRIS = "org.mpris.MediaPlayer2"
    MPRIS_PLAYER = "org.mpris.MediaPlayer2.Player"
    MPRIS_PATH = "/org/mpris/MediaPlayer2"
    PAUSED_FOR = 600          # a paused player stays shown this long, so it can be resumed

    def watch_media(self):
        """Music and video players (MPRIS): what plays, shown in the panel's
        free space, with play/pause, skip and the player's window."""
        self.bus.add_signal_receiver(self.media_owner, "NameOwnerChanged", "org.freedesktop.DBus")
        try:
            names = self.bus.list_names()
        except self.dbus.DBusException:
            names = []
        for name in names:
            if str(name).startswith(self.MPRIS + "."):
                self.player_added(str(name))

    def media_owner(self, name, old, new):
        name = str(name)
        if not name.startswith(self.MPRIS + "."):
            return
        if old and name in self.players:
            p = self.players.pop(name)
            for match in p["matches"]:
                match.remove()
            if self.media_pick == name:
                self.media_pick = None
            self.build_media()
        if new:
            self.player_added(name)

    def player_added(self, name):
        if name in self.players:
            return
        p = {"name": name, "props": {}, "identity": name.split(".")[3] if name.count(".") >= 3 else name,
             "desktop": "", "can_raise": False, "pos": 0.0, "at": uptime(), "since": 0.0, "paused_at": 0.0,
             "pid": 0, "matches": []}
        self.players[name] = p
        p["matches"].append(self.bus.add_signal_receiver(
            lambda iface, changed, invalid, n=name: self.player_changed(n, str(iface), changed),
            "PropertiesChanged", self.PROPS, name, self.MPRIS_PATH))
        p["matches"].append(self.bus.add_signal_receiver(
            lambda position, n=name: self.player_seeked(n, position), "Seeked", self.MPRIS_PLAYER, name,
            self.MPRIS_PATH))
        try:
            obj = self.bus.get_object(name, self.MPRIS_PATH, introspect=False)
            obj.GetAll(self.MPRIS, dbus_interface=self.PROPS,
                       reply_handler=lambda props, n=name: self.player_changed(n, self.MPRIS, props),
                       error_handler=lambda e: None)
            obj.GetAll(self.MPRIS_PLAYER, dbus_interface=self.PROPS,
                       reply_handler=lambda props, n=name: self.player_changed(n, self.MPRIS_PLAYER, props),
                       error_handler=lambda e: None)
            dbus_obj = self.bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
            p["pid"] = int(dbus_obj.GetConnectionUnixProcessID(name, dbus_interface="org.freedesktop.DBus"))
        except (self.dbus.DBusException, ValueError, TypeError):
            pass

    def player_changed(self, name, iface, changed):
        p = self.players.get(name)
        if not p:
            return
        if iface == self.MPRIS:
            p["identity"] = str(changed.get("Identity", p["identity"]) or p["identity"])
            p["desktop"] = str(changed.get("DesktopEntry", p["desktop"]) or "")
            p["can_raise"] = bool(changed.get("CanRaise", p["can_raise"]))
        elif iface == self.MPRIS_PLAYER:
            was = p["props"].get("PlaybackStatus")
            p["props"].update(changed)
            status = p["props"].get("PlaybackStatus")
            if status != was:
                if status == "Playing":
                    p["since"] = time.monotonic()
                elif was == "Playing":
                    p["paused_at"] = time.monotonic()
            if "PlaybackStatus" in changed or "Metadata" in changed or "Rate" in changed:
                self.read_position(name)
        self.soon("media", self.build_media, 120)

    def player_seeked(self, name, position):
        p = self.players.get(name)
        if p:
            p["pos"], p["at"] = int(position) / 1e6, uptime()
            self.soon("media", self.build_media, 120)

    def read_position(self, name):
        """MPRIS does not announce the position as it moves; it is asked for
        and carried forward from then at the player's rate."""
        def got(value):
            p = self.players.get(name)
            if p:
                p["pos"], p["at"] = int(value) / 1e6, uptime()
                self.soon("media", self.build_media, 120)

        try:
            obj = self.bus.get_object(name, self.MPRIS_PATH, introspect=False)
            obj.Get(self.MPRIS_PLAYER, "Position", dbus_interface=self.PROPS, reply_handler=got,
                    error_handler=lambda e: None)
        except self.dbus.DBusException:
            pass

    def refresh_media(self):
        """Every few seconds: a playing player's position again (it drifts, and
        some players never send Seeked), and a long-paused one let go."""
        for name, p in self.players.items():
            if p["props"].get("PlaybackStatus") == "Playing":
                self.read_position(name)
        if self.media and not self.media["playing"]:
            self.build_media()

    def player_entry(self, p):
        """The player's app (its desktop file), for its name and icon: Zen
        calls itself "Mozilla zen" over MPRIS, its app is "Zen Browser"."""
        ident = self.apps.match(p["desktop"], p["desktop"], p["identity"].lower(), self.pins)
        return self.apps.entries.get(ident or "") or {}

    def player_label(self, p):
        return self.player_entry(p).get("name") or p["identity"]

    def shown_player(self):
        """The player the panel shows: one picked from its menu, else the one
        that started playing last, else the one paused last (for a while)."""
        def usable(p):
            return bool(str(p["props"].get("Metadata", {}).get("xesam:title", "") or p["identity"]))

        players = [p for p in self.players.values() if usable(p)]
        if self.media_pick in self.players and usable(self.players[self.media_pick]):
            return self.players[self.media_pick]
        playing = [p for p in players if p["props"].get("PlaybackStatus") == "Playing"]
        if playing:
            return max(playing, key=lambda p: p["since"])
        paused = [p for p in players if p["props"].get("PlaybackStatus") == "Paused"
                  and time.monotonic() - p["paused_at"] < self.PAUSED_FOR]
        return max(paused, key=lambda p: p["paused_at"]) if paused else None

    def build_media(self):
        """What the panel draws for the shown player: its title and artist as
        pictures (see text_png), its cover or app icon, and where it is."""
        p = self.shown_player()
        if not p:
            if self.media is not None:
                self.media = None
                self.changed()
            self.prune_media()
            self.update_capture()
            return False
        meta = p["props"].get("Metadata", {}) or {}
        title = str(meta.get("xesam:title", "") or p["identity"]).strip()
        artists = meta.get("xesam:artist", []) or []
        if isinstance(artists, (str, self.dbus.String)):
            artists = [artists]
        artist = ", ".join(str(a) for a in artists if str(a).strip())
        # A browser plays a video with no artist; its site name is better than nothing.
        if not artist and meta.get("xesam:album"):
            artist = str(meta.get("xesam:album"))
        scale = self.lnum("scale", 1.0)
        ink = self.layout.get("media_ink") or self.layout.get("ink") or "#ffffff"
        folder = os.path.join(self.cache, "media")
        os.makedirs(folder, exist_ok=True)
        widest = round(600 * scale)
        media = {"title": title, "artist": artist, "player": self.player_label(p),
                 "playing": p["props"].get("PlaybackStatus") == "Playing",
                 "can_prev": bool(p["props"].get("CanGoPrevious", True)),
                 "can_next": bool(p["props"].get("CanGoNext", True)),
                 "length": int(meta.get("mpris:length", 0) or 0) / 1e6,
                 "pos": p["pos"], "at": p["at"], "rate": float(p["props"].get("Rate", 1.0) or 1.0)}
        try:
            media["title_png"], media["title_w"], media["title_h"], media["title_base"] = text_png(
                folder, title, 12.5 * scale, True, ink, widest)
            if artist:
                media["artist_png"], media["artist_w"], media["artist_h"], media["artist_base"] = text_png(
                    folder, artist, 10.5 * scale, False, ink, widest)
        except Exception as error:                       # no Pango: the panel shows the button alone
            log("media text: %s" % error)
        media["art"] = self.media_art(p, meta, round(30 * scale))
        # A new reading of the position is only news when it is not where the
        # last one, carried forward, says it should be: rewriting the state
        # every few seconds would lay the panel out again each time.
        old = self.media
        if old and {k: v for k, v in old.items() if k not in ("pos", "at")} == \
                {k: v for k, v in media.items() if k not in ("pos", "at")}:
            carried = old["pos"] + ((media["at"] - old["at"]) * old["rate"] if old["playing"] else 0)
            if abs(carried - media["pos"]) < 1.0:
                media["pos"], media["at"] = old["pos"], old["at"]
        if media != self.media:
            self.media = media
            self.changed()
        self.prune_media()
        self.update_capture()
        return False

    def media_art(self, p, meta, size):
        """The cover, square at `size`; a cover on the web is fetched once (by
        curl, so the helper never waits on the network). Else the player's icon."""
        url = str(meta.get("mpris:artUrl", "") or "")
        folder = os.path.join(self.cache, "media")
        source = None
        if url.startswith("file://"):
            import urllib.parse
            source = urllib.parse.unquote(url[7:])
        elif url.startswith(("http://", "https://")):
            source = os.path.join(folder, "web-" + hashlib.sha1(url.encode()).hexdigest()[:20])
            if not os.path.exists(source) and url not in self.art_fetching and shutil.which("curl"):
                self.art_fetching.add(url)
                try:
                    proc = subprocess.Popen(["curl", "-sfL", "-m", "8", "-o", source + ".part", url],
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, preexec_fn=die_with_parent)

                    def fetched(code, url=url, source=source):
                        self.art_fetching.discard(url)
                        if code == 0 and os.path.exists(source + ".part"):
                            os.replace(source + ".part", source)
                            self.build_media()

                    self.watch(proc, fetched)
                except OSError:
                    self.art_fetching.discard(url)
        if source and os.path.isfile(source):
            out = os.path.join(folder, "a-%s-%d.png" % (hashlib.sha1(("%s|%s" % (
                source, os.path.getmtime(source))).encode()).hexdigest()[:20], size))
            if os.path.exists(out):
                return out
            try:
                from PIL import Image
                image = Image.open(source).convert("RGBA")
                side = min(image.size)
                left, top = (image.width - side) // 2, (image.height - side) // 2
                image.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS).save(out)
                return out
            except Exception as error:
                log("cover %s: %s" % (source, error))
        # No cover: the player's own icon.
        for name in (self.player_entry(p).get("icon"), p["desktop"], p["identity"].lower(), "multimedia-player"):
            path = self.icons.find(name, size) if name else None
            if path:
                return self.icons.render(path, size)
        return None

    def prune_media(self):
        """Old titles and covers go: one set per song would pile up."""
        keep = {os.path.basename(str(v)) for k, v in (self.media or {}).items() if k.endswith(("_png", "art")) and v}
        folder = os.path.join(self.cache, "media")
        try:
            names = os.listdir(folder)
        except OSError:
            return
        for n in names:
            if n.endswith(".png") and n not in keep and n.startswith(("t-", "a-")):
                try:
                    os.remove(os.path.join(folder, n))
                except OSError:
                    pass
            elif n.startswith("web-") and not n.endswith(".part"):
                path = os.path.join(folder, n)
                try:
                    if time.time() - os.path.getmtime(path) > 86400:
                        os.remove(path)
                except OSError:
                    pass

    def player_call(self, method, name=None):
        p = self.players.get(name) if name else self.shown_player()
        if not p:
            return
        try:
            obj = self.bus.get_object(p["name"], self.MPRIS_PATH, introspect=False)
            getattr(obj, method)(dbus_interface=self.MPRIS_PLAYER if method != "Raise" else self.MPRIS,
                                 reply_handler=lambda *a: None, error_handler=lambda e: log("player: %s" % e))
        except self.dbus.DBusException as error:
            log("player: %s" % error)

    def show_player(self, p):
        """The player's window, brought forward by KWin (the player's own
        Raise would need the token only Plasma can hand out)."""
        mine = [w for w in self.windows if p["pid"] and w.get("pid") == p["pid"]]
        if mine:
            target = max(mine, key=lambda w: self.recency.get(w["id"], 0))
            self.kwin_action([target["id"]], "activate")
        elif p["can_raise"]:
            self.player_call("Raise", p["name"])

    def media_click(self, button, part, rect):
        """A click on the media box: `part` is its button ("previous",
        "toggle", "next") or "show", the cover and title."""
        p = self.shown_player()
        if not p:
            return
        if button == "left":
            props = p["props"]
            if part == "previous" and props.get("CanGoPrevious", True):
                self.player_call("Previous", p["name"])
            elif part == "next" and props.get("CanGoNext", True):
                self.player_call("Next", p["name"])
            elif part == "show":
                self.show_player(p)
            elif part == "toggle":
                self.player_call("PlayPause", p["name"])
        elif button == "middle":
            self.show_player(p)
        elif button == "right":
            props = p["props"]
            playing = props.get("PlaybackStatus") == "Playing"
            items = [{"header": True, "label": self.player_label(p)},
                     {"id": "toggle", "label": "Pause" if playing else "Play"}]
            if props.get("CanGoNext", True):
                items.append({"id": "next", "label": "Next"})
            if props.get("CanGoPrevious", True):
                items.append({"id": "previous", "label": "Previous"})
            items += [{"separator": True}, {"id": "show", "label": "Show %s" % self.player_label(p)}]
            others = [q for q in self.players.values() if q["props"].get("PlaybackStatus") in ("Playing", "Paused")]
            if len(others) > 1:
                items.append({"separator": True})
                for q in others:
                    items.append({"id": "pick:" + q["name"], "label": self.player_label(q), "checked": q is p})

            def chosen(choice):
                if choice == "toggle":
                    self.player_call("PlayPause", p["name"])
                elif choice in ("next", "previous"):
                    self.player_call(choice.capitalize(), p["name"])
                elif choice == "show":
                    self.show_player(p)
                elif choice.startswith("pick:"):
                    self.media_pick = choice[5:]
                    self.build_media()

            self.open_menu(items, rect, chosen)

    # ---------------------------------------------------- the sound bars

    VIZ_BARS, VIZ_RATE, VIZ_N = 14, 24000, 1024

    def levels_path(self):
        return os.path.join(os.environ.get("XDG_RUNTIME_DIR") or self.cache, "conky-tray-levels")

    def update_capture(self):
        """Listen to what plays only while the panel shows the sound bars and
        the player shown is playing: no recording, and no cost, otherwise."""
        want = self.layout.get("visualizer") == "1" and bool(self.media and self.media.get("playing"))
        running = self.capture is not None and self.capture.returncode is None
        if want and not running:
            self.start_capture()
        elif not want and running:
            self.stop_capture()

    def restart_capture(self):
        self.stop_capture()
        self.update_capture()

    def stop_capture(self):
        proc, self.capture = self.capture, None
        if proc is not None and proc.returncode is None:
            proc.terminate()
        try:
            os.remove(self.levels_path())
        except OSError:
            pass

    def start_capture(self):
        """What the default output plays (its monitor, as Plasma's own volume
        meters do), 24 kHz mono, read as it comes."""
        try:
            import numpy as np
        except ImportError:
            if not self.capture_failed:
                log("no sound bars: numpy is not installed")
                self.capture_failed = True
            return
        try:
            proc = subprocess.Popen(["parec", "--device=@DEFAULT_MONITOR@", "--format=s16le",
                                     "--rate=%d" % self.VIZ_RATE, "--channels=1", "--latency-msec=20",
                                     "--client-name=ConkyTray", "--stream-name=Sound bars"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    preexec_fn=die_with_parent)
        except OSError as error:
            if not self.capture_failed:
                log("no sound bars: %s" % error)
                self.capture_failed = True
            return
        self.capture = proc
        fd = proc.stdout.fileno()
        os.set_blocking(fd, False)
        n = self.VIZ_N
        self.viz_window = np.hanning(n).astype(np.float32)
        # Bands spaced evenly in pitch from 45 Hz to 11 kHz, each the bins of
        # the transform it covers, with a lift for the highs, which carry far
        # less energy than the lows in music.
        edges = [45 * (11000 / 45) ** (i / self.VIZ_BARS) for i in range(self.VIZ_BARS + 1)]
        hz = self.VIZ_RATE / n
        self.viz_bands = []
        for i in range(self.VIZ_BARS):
            lo = max(1, int(edges[i] / hz))
            hi = max(lo + 1, int(round(edges[i + 1] / hz)))
            self.viz_bands.append((lo, hi, 3.0 * math.log2(max(1.0, edges[i] / 100))))
        buf = {"data": bytearray(), "last": 0.0, "peak": -40.0}

        def readable(source, condition):
            if self.capture is not proc or condition & (self.GLib.IO_HUP | self.GLib.IO_ERR):
                return False
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                return True
            except OSError:
                return False
            if not data:
                return False
            buf["data"] += data
            del buf["data"][:-2 * n]
            now = time.monotonic()
            if now - buf["last"] >= 0.04 and len(buf["data"]) >= 2 * n:
                buf["last"] = now
                self.counts["sound frames"] += 1
                self.sound_levels(np, bytes(buf["data"]), buf)
            return True

        self.GLib.io_add_watch(fd, self.GLib.IO_IN | self.GLib.IO_HUP | self.GLib.IO_ERR, readable)
        self.watch(proc, lambda code: None)

    def sound_levels(self, np, data, buf):
        """One reading of the bars: each band's loudest bin in decibels, on a
        42 dB scale whose top follows the music's loudest, quickly up and
        slowly down, so quiet passages still move the bars."""
        samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        spectrum = np.abs(np.fft.rfft(samples * self.viz_window)) / (self.VIZ_N / 4)
        dbs = [20 * math.log10(float(spectrum[lo:hi].max()) + 1e-7) + lift for lo, hi, lift in self.viz_bands]
        loudest = max(dbs)
        buf["peak"] = max(loudest, buf["peak"] - 0.12, -40.0)
        floor = buf["peak"] - 42
        levels = [0.0] * len(dbs) if loudest < -80 else [min(1.0, max(0.0, (d - floor) / 42)) for d in dbs]
        write_atomic(self.levels_path(), "%.2f %s\n" % (uptime(), " ".join("%.2f" % v for v in levels)))

    # ------------------------------------------- microphone and camera in use

    # Recordings that are not someone listening: level meters.
    QUIET_RECORDERS = ("org.PulseAudio.pavucontrol", "org.kde.kmixd", "org.kde.plasma", "org.kde.kmix",
                       "org.kde.plasmashell", "org.kde.kded6")

    def read_microphone(self):
        """Apps recording a microphone right now (not a speaker's monitor, not a
        paused stream, not a volume meter), as Plasma's tray used to show."""
        self.counts["microphone checks"] += 1
        try:
            outputs = json.loads(output_of(["pactl", "-f", "json", "list", "source-outputs"]) or "[]")
        except ValueError:
            outputs = []
        apps = []
        if outputs:
            try:
                sources = {s.get("index"): s for s in json.loads(output_of(["pactl", "-f", "json", "list", "sources"])
                                                                   or "[]")}
            except ValueError:
                sources = {}
            for o in outputs:
                props = o.get("properties", {}) or {}
                source = sources.get(o.get("source"), {})
                monitor = (str(source.get("name", "")).endswith(".monitor")
                           or (source.get("properties", {}) or {}).get("device.class") == "monitor")
                app_id = str(props.get("application.id", ""))
                if o.get("corked") or monitor or app_id.startswith(self.QUIET_RECORDERS):
                    continue
                if "peak" in str(props.get("media.name", "")).lower():
                    continue
                pid = int(props.get("application.process.id", 0) or 0)
                apps.append({"name": self.app_name(pid, props.get("application.name")
                                                   or props.get("application.process.binary")), "pid": pid})
        self.mic_apps = apps
        self.update_privacy()

    def app_name(self, pid, fallback=None):
        """What to call the app behind a process: its name in the apps' row if
        one of its windows is open, else what it calls itself."""
        for g in self.groups.values():
            if any(pid and w.get("pid") == pid for w in g.get("list", [])):
                return g.get("name") or fallback
        if fallback:
            return str(fallback)
        try:
            return open("/proc/%d/comm" % pid).read().strip() or "An app"
        except OSError:
            return "An app"

    def watch_camera(self):
        """The kernel says when a camera is opened or closed (inotify on
        /dev/video*), and /dev when one is plugged in; who holds it open is only
        looked for then."""
        import ctypes
        import struct
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        except (OSError, AttributeError):
            return
        if fd < 0:
            return
        IN_OPEN, IN_CLOSE_WRITE, IN_CLOSE_NOWRITE, IN_CREATE = 0x20, 0x8, 0x10, 0x100
        watched = set()

        def add_cameras():
            for dev in glob.glob("/dev/video*"):
                if dev not in watched and libc.inotify_add_watch(fd, os.fsencode(dev),
                                                                 IN_OPEN | IN_CLOSE_WRITE | IN_CLOSE_NOWRITE) >= 0:
                    watched.add(dev)

        libc.inotify_add_watch(fd, b"/dev", IN_CREATE)
        add_cameras()

        def readable(*unused):
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                return True
            except OSError:
                return False
            i, camera, plugged = 0, False, False
            while i + 16 <= len(data):
                mask, length = struct.unpack_from("iIII", data, i)[1::2]
                name = data[i + 16:i + 16 + length].split(b"\0", 1)[0]
                i += 16 + length
                if mask & IN_CREATE:
                    plugged = plugged or name.startswith(b"video")
                else:
                    camera = True
            if plugged:
                add_cameras()
            if camera:
                # Apps open a camera for a moment to ask what it can do; only
                # one still open a second later counts.
                self.soon("camera", self.read_camera, 1200)
            return True

        self.camera_watch = fd
        self.GLib.io_add_watch(fd, self.GLib.PRIORITY_DEFAULT, self.GLib.IOCondition.IN, readable)

    def read_camera(self):
        self.counts["camera checks"] += 1
        devices = {os.path.realpath(d) for d in glob.glob("/dev/video*")}
        holders = []
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                fds = os.listdir("/proc/%s/fd" % pid)
            except OSError:
                continue
            for f in fds:
                try:
                    if os.readlink("/proc/%s/fd/%s" % (pid, f)) in devices:
                        holders.append(int(pid))
                        break
                except OSError:
                    continue
        apps = []
        for pid in holders:
            comm = ""
            try:
                comm = open("/proc/%d/comm" % pid).read().strip()
            except OSError:
                pass
            if comm in ("pipewire", "wireplumber"):
                # PipeWire holds the camera for an app that asked the desktop for it.
                apps += [{"name": n, "pid": 0} for n in self.pipewire_camera_users()] or [{"name": "An app", "pid": 0}]
            else:
                apps.append({"name": self.app_name(pid, comm), "pid": pid})
        self.camera_apps = apps
        self.update_privacy()

    def pipewire_camera_users(self):
        try:
            nodes = json.loads(output_of(["pw-dump"], timeout=5) or "[]")
        except ValueError:
            return []
        names = []
        for n in nodes:
            info = n.get("info") or {}
            props = info.get("props") or {}
            if props.get("media.class") == "Stream/Input/Video" and info.get("state") == "running":
                names.append(str(props.get("application.name") or props.get("node.name") or "An app"))
        return names

    def update_privacy(self):
        if not self.mic_apps and not self.camera_apps:
            privacy = None
        else:
            name = ("camera-web", "camera-video") if self.camera_apps else ("audio-input-microphone",
                                                                           "microphone-sensitivity-high")
            privacy = {"icon": self.status_icon(*name),
                       "microphone": len(self.mic_apps), "camera": len(self.camera_apps)}
        if privacy != self.privacy:
            self.privacy = privacy
            self.changed()

    def privacy_click(self, button, rect):
        if not self.privacy:
            return
        items = [{"header": True, "label": "In use right now"}]
        everyone = []
        for kind, apps in (("Camera", self.camera_apps), ("Microphone", self.mic_apps)):
            for a in apps:
                items.append({"id": "app:%d" % len(everyone), "label": "%s: %s" % (kind, a["name"])})
                everyone.append(a)
        if self.mic_apps:
            items += [{"separator": True}, {"id": "settings", "label": "Sound settings"}]

        def chosen(choice):
            if choice.startswith("app:"):
                a = everyone[int(choice[4:])]
                mine = [w for w in self.windows if a["pid"] and w.get("pid") == a["pid"]]
                if mine:
                    self.kwin_action([max(mine, key=lambda w: self.recency.get(w["id"], 0))["id"]], "activate")
            elif choice == "settings":
                detached(["systemsettings", "kcm_pulseaudio"])

        self.open_menu(items, rect, chosen)


    # ----------------------------------------------------- notifications

    NOTES = "org.freedesktop.Notifications"

    def serve_notifications(self):
        """Be the desktop's notification service. Plasma's lived in its
        panel's tray and went with it; with nobody serving, the bus started
        Plasma's stand-in, which waits for Plasma, and an app asking for a
        notification waited with it until the bus gave up (Claude desktop
        froze for 110s each time). A service already running is left alone,
        and Plasma may take the name back."""
        import dbus.service
        daemon, NOTES = self, self.NOTES

        class Notifications(dbus.service.Object):
            @dbus.service.method(NOTES, in_signature="", out_signature="as")
            def GetCapabilities(self):
                return ["body", "actions", "icon-static", "persistence"]

            @dbus.service.method(NOTES, in_signature="susssasa{sv}i", out_signature="u",
                                 sender_keyword="sender", byte_arrays=True)
            def Notify(self, app, replaces, icon, summary, body, actions, hints, timeout, sender=None):
                return daemon.notify(str(app), int(replaces), str(icon), str(summary), str(body),
                                     [str(a) for a in actions], dict(hints), int(timeout), sender)

            @dbus.service.method(NOTES, in_signature="u")
            def CloseNotification(self, ident):
                daemon.close_note(int(ident), 3)

            @dbus.service.method(NOTES, in_signature="", out_signature="ssss")
            def GetServerInformation(self):
                return ("ConkyTray", "Conky-themes", "1.0", "1.2")

            @dbus.service.signal(NOTES, signature="uu")
            def NotificationClosed(self, ident, reason):
                pass

            @dbus.service.signal(NOTES, signature="us")
            def ActionInvoked(self, ident, key):
                pass

        try:
            self.notes_name = dbus.service.BusName(NOTES, self.bus, allow_replacement=True, do_not_queue=True)
        except self.dbus.exceptions.NameExistsException:
            log("another notification service is running, so the panel shows none")
            return
        self.notes_object = Notifications(self.bus, "/org/freedesktop/Notifications")
        self.update_bell()

    def load_dnd(self):
        try:
            with open(os.path.join(CONFIG, "notifications.json"), encoding="utf-8") as handle:
                return bool(json.load(handle).get("dnd"))
        except (OSError, ValueError, AttributeError):
            return False

    def save_dnd(self):
        write_atomic(os.path.join(CONFIG, "notifications.json"), json.dumps({"dnd": self.dnd}) + "\n")

    def notify(self, app, replaces, icon, summary, body, actions, hints, timeout, sender):
        """A notification: shown as a pop-up (unless Do not disturb is on and
        it is not urgent) and kept behind the bell. Answers at once."""
        if replaces and replaces in self.notes:
            ident = replaces
        else:
            self.note_ids = self.note_ids % 0xFFFFFFF0 + 1
            ident = self.note_ids
        hints = {str(k): v for k, v in hints.items()}
        try:
            urgency = int(hints.get("urgency", 1))
        except (TypeError, ValueError):
            urgency = 1
        desktop = str(hints.get("desktop-entry", "") or "")
        desktop = desktop[:-8] if desktop.endswith(".desktop") else desktop
        entry = self.apps.entries.get(desktop) or {}
        name = app or entry.get("name") or "Notification"
        if timeout < 0:
            timeout = 0 if urgency == 2 else 6000
        elif timeout > 0:
            timeout = max(timeout, 2000)
        note = {"id": ident, "app": name, "summary": summary, "body": body,
                "icon": self.note_picture(ident, icon, hints, entry.get("icon", "")),
                "actions": list(zip(actions[0::2], actions[1::2])), "pid": self.sender_pid(sender),
                "desktop": desktop.lower(), "at": time.time()}
        self.notes[ident] = note
        if not hints.get("transient"):
            self.note_history = [n for n in self.note_history if n["id"] != ident]
            self.note_history.insert(0, note)
            del self.note_history[30:]
            if not (replaces and replaces == ident):
                self.note_unread += 1
        if not self.dnd or urgency == 2:
            self.tell_notifier({"cmd": "show", "id": ident, "app": name, "summary": summary, "body": body,
                                "icon": note["icon"], "actions": note["actions"], "timeout": timeout,
                                "urgency": urgency, "side": self.layout.get("side", "bottom"),
                                "colors": self.palette_for_menu()})
        self.counts["notifications"] += 1
        self.update_bell()
        return ident

    def sender_pid(self, sender):
        try:
            bus = self.bus.get_object("org.freedesktop.DBus", "/org/freedesktop/DBus")
            return int(bus.GetConnectionUnixProcessID(sender, dbus_interface="org.freedesktop.DBus"))
        except (self.dbus.DBusException, TypeError, ValueError):
            return 0

    def note_picture(self, ident, icon, hints, app_icon):
        """A picture for the pop-up: the image the app sent, else its icon,
        else the icon of its desktop file."""
        folder = os.path.join(self.cache, "notify")
        os.makedirs(folder, exist_ok=True)
        keep = {n.get("icon") for n in list(self.notes.values()) + self.note_history}
        for old in glob.glob(os.path.join(folder, "n*.png")):
            if old not in keep:
                try:
                    os.remove(old)
                except OSError:
                    pass
        data = hints.get("image-data") or hints.get("image_data") or hints.get("icon_data")
        if data:
            try:
                from PIL import Image
                w, h, stride, alpha, _bits, _channels, raw = data
                mode = "RGBA" if bool(alpha) else "RGB"
                image = Image.frombuffer(mode, (int(w), int(h)), bytes(raw), "raw", mode, int(stride), 1)
                image.thumbnail((96, 96))
                path = os.path.join(folder, "n%d-%d.png" % (ident, int(time.time() * 1000) % 100000))
                image.save(path)
                return path
            except Exception as error:                   # a bad picture must not lose the notification
                log("notification picture: %s" % error)
        for name in (str(hints.get("image-path", "") or ""), icon, app_icon):
            if not name:
                continue
            if name.startswith("file://"):
                import urllib.parse
                name = urllib.parse.unquote(name[7:])
            if os.path.isabs(name):
                if os.path.exists(name):
                    return self.icons.render(name, 96) or name
                continue
            found = self.icons.find(name, 96)
            if found:
                return self.icons.render(found, 96)
        return None

    def close_note(self, ident, reason):
        note = self.notes.pop(ident, None)
        if note is None:
            return
        self.tell_notifier({"cmd": "close", "id": ident})
        if self.notes_object:
            self.notes_object.NotificationClosed(self.dbus.UInt32(ident), self.dbus.UInt32(reason))

    def start_notifier(self):
        """The pop-ups are their own program (GTK), kept running."""
        if self.notifier and self.notifier.returncode is None:
            return
        try:
            self.notifier = subprocess.Popen([sys.executable, os.path.join(HERE, "tray_notify.py")],
                                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=open(os.path.join(self.cache, "notify.log"), "a"),
                                             text=True, bufsize=1, preexec_fn=die_with_parent)
        except OSError as error:
            log("no notification pop-ups: %s" % error)
            self.notifier = None
            return
        proc = self.notifier

        def readable(fd, condition):
            if condition & (self.GLib.IO_HUP | self.GLib.IO_ERR):
                return False
            line = proc.stdout.readline()
            if not line:
                return False
            self.notifier_said(line.strip())
            return True

        self.GLib.io_add_watch(proc.stdout.fileno(), self.GLib.IO_IN | self.GLib.IO_HUP | self.GLib.IO_ERR, readable)

        def gone(code):
            if self.notifier is proc:
                self.notifier = None

        self.watch(proc, gone)

    def tell_notifier(self, spec):
        self.start_notifier()
        if not self.notifier:
            return
        try:
            self.notifier.stdin.write(json.dumps(spec) + "\n")
            self.notifier.stdin.flush()
        except (OSError, ValueError):
            self.notifier = None

    def notifier_said(self, line):
        try:
            event = json.loads(line)
            ident = int(event.get("id", 0))
        except (ValueError, TypeError, AttributeError):
            return
        note = self.notes.get(ident)
        what = event.get("ev")
        if what == "action":
            if self.notes_object:
                self.notes_object.ActionInvoked(self.dbus.UInt32(ident), str(event.get("key", "")))
            self.raise_note_app(note)
        elif what == "clicked":
            self.raise_note_app(note)
        elif what == "closed":
            self.close_note(ident, int(event.get("reason", 2)) or 2)

    def note_windows(self, note):
        if not note:
            return []
        names = {note.get("desktop") or "", str(note.get("app") or "").lower()} - {""}
        return [w for w in self.windows
                if (note.get("pid") and w.get("pid") == note["pid"])
                or names & {str(w.get(k) or "").lower() for k in ("cls", "desktop", "name")}]

    def raise_note_app(self, note):
        """After a notification is clicked, its app's window, brought forward
        if the app has not done it itself: like a tray icon's app, it cannot
        without the token only Plasma gets."""
        if not note:
            return

        def later():
            mine = self.note_windows(note)
            if mine and not any(w.get("active") for w in mine):
                target = max(mine, key=lambda w: self.recency.get(w["id"], 0))
                self.kwin_action([target["id"]], "activate")
            return False

        self.GLib.timeout_add(350, later)

    def update_bell(self):
        if not self.notes_object:
            bell = None
        else:
            names = (("notification-disabled", "notifications-disabled") if self.dnd else
                     ("notification", "notifications", "preferences-desktop-notification-bell"))
            bell = {"icon": self.status_icon(*names), "unread": self.note_unread, "dnd": self.dnd}
        if bell != self.notifications:
            self.notifications = bell
            self.changed()

    def notifications_click(self, button, rect):
        if not self.notes_object:
            return
        history = list(self.note_history[:12])
        items = [{"header": True, "label": "Notifications" if history else "No notifications"}]
        for i, n in enumerate(history):
            text = " ".join((n["summary"] or n["body"] or "").split())
            items.append({"id": "n:%d" % i, "label": "%s: %s" % (n["app"], text) if text else n["app"]})
        items += [{"separator": True}, {"id": "dnd", "label": "Do not disturb", "checked": self.dnd}]
        if history:
            items.append({"id": "clear", "label": "Clear all"})
        self.note_unread = 0
        self.update_bell()

        def chosen(choice):
            if choice.startswith("n:"):
                self.raise_note_app(history[int(choice[2:])])
            elif choice == "dnd":
                self.dnd = not self.dnd
                self.save_dnd()
                self.update_bell()
            elif choice == "clear":
                self.note_history = []
                self.update_bell()

        self.open_menu(items, rect, chosen)

    # ----------------------------------------------------------- devices

    UDISKS = "org.freedesktop.UDisks2"

    def watch_devices(self):
        """USB drives and memory cards, from UDisks: an icon while one is
        plugged in, and a menu by itself when one arrives."""
        if not self.sysbus:
            return
        for name, iface in (("InterfacesAdded", self.MANAGER), ("InterfacesRemoved", self.MANAGER),
                            ("PropertiesChanged", self.PROPS)):
            self.sysbus.add_signal_receiver(lambda *a, **k: self.soon("devices", self.read_devices, 400),
                                            name, iface, self.UDISKS)
        self.read_devices()

    def read_devices(self):
        if not self.sysbus:
            return
        try:
            obj = self.sysbus.get_object(self.UDISKS, "/org/freedesktop/UDisks2", introspect=False)
            obj.GetManagedObjects(dbus_interface=self.MANAGER, reply_handler=self.got_devices,
                                  error_handler=lambda e: log("devices: %s" % e))
        except self.dbus.DBusException as error:
            log("devices: %s" % error)

    def got_devices(self, objects):
        drives = {str(p): i["org.freedesktop.UDisks2.Drive"] for p, i in objects.items()
                  if "org.freedesktop.UDisks2.Drive" in i}
        found = []
        for path, interfaces in sorted(objects.items()):
            fs = interfaces.get("org.freedesktop.UDisks2.Filesystem")
            block = interfaces.get("org.freedesktop.UDisks2.Block")
            if fs is None or block is None or block.get("HintIgnore") or block.get("HintSystem"):
                continue
            drive = drives.get(str(block.get("Drive")))
            if not drive or not (drive.get("Removable") or drive.get("MediaRemovable") or
                                 str(drive.get("ConnectionBus", "")) in ("usb", "sdio", "ieee1394")):
                continue
            name = str(block.get("IdLabel") or "").strip() or \
                " ".join(x for x in (str(drive.get("Vendor", "")).strip(), str(drive.get("Model", "")).strip()) if x) or "Drive"
            mounts = [bytes(m).rstrip(b"\0").decode("utf-8", "replace") for m in fs.get("MountPoints", [])]
            found.append({"path": str(path), "drive": str(block.get("Drive")), "name": name,
                          "size": int(block.get("Size", 0)), "mounts": mounts,
                          "power_off": bool(drive.get("CanPowerOff")), "eject": bool(drive.get("Ejectable"))})
        arrived = [d for d in found if self.devices_known is not None and d["path"] not in self.devices_known]
        self.devices_known = {d["path"] for d in found}
        self.device_list = found
        self.devices = {"count": len(found), "icon": self.status_icon("drive-removable-media", "media-removable",
                                                                       "drive-removable-media-usb")} if found else None
        self.changed()
        if arrived:
            # Its icon has to be on the panel first, for the menu to open by it.
            self.device_popup = time.time()
            self.GLib.timeout_add(2500, lambda: (self.maybe_device_popup(force=True), False)[1])

    def maybe_device_popup(self, force=False):
        if self.device_popup is None or not self.device_list:
            return
        try:
            rect = tuple(float(v) for v in self.layout.get("devices_rect", "").split(","))
        except ValueError:
            rect = ()
        if len(rect) != 4 and not force:
            return
        self.device_popup = None
        self.devices_menu(rect if len(rect) == 4 else None, passive=True)

    @staticmethod
    def human_size(n):
        for unit in ("bytes", "KB", "MB", "GB", "TB"):
            if n < 1000 or unit == "TB":
                return ("%d %s" if unit == "bytes" else "%.1f %s") % (n, unit)
            n /= 1000.0

    def devices_menu(self, rect, passive):
        """Each plugged-in drive: open it, or remove it safely. When a drive
        has just arrived the menu opens by itself and does not take the
        keyboard, and it goes away again unless the pointer is on it."""
        if not self.device_list:
            return
        items = []
        for d in self.device_list:
            if items:
                items.append({"separator": True})
            items.append({"header": True, "label": "%s, %s" % (d["name"], self.human_size(d["size"]))})
            items.append({"id": "open:" + d["path"], "label": "Open in the file manager", "icon_name": "system-file-manager"})
            items.append({"id": "remove:" + d["path"], "label": "Safely remove", "icon_name": "media-eject"})
        self.open_menu(items, rect or (0, 0, 1, 1), self.device_chosen, passive=passive)

    def device_chosen(self, choice):
        what, _, path = choice.partition(":")
        device = next((d for d in self.device_list if d["path"] == path), None)
        if not device:
            return
        fs = "org.freedesktop.UDisks2.Filesystem"
        obj = self.sysbus.get_object(self.UDISKS, path, introspect=False)
        options = self.dbus.Dictionary({}, signature="sv")
        if what == "open":
            if device["mounts"]:
                detached(["xdg-open", device["mounts"][0]])
            else:
                obj.Mount(options, dbus_interface=fs, timeout=60,
                          reply_handler=lambda where: detached(["xdg-open", str(where)]),
                          error_handler=lambda e: log("could not open %s: %s" % (device["name"], e)))
        elif what == "remove":
            # Every mounted part of the drive first, then the drive itself.
            parts = [d for d in self.device_list if d["drive"] == device["drive"] and d["mounts"]]

            def power_off():
                drive = self.sysbus.get_object(self.UDISKS, device["drive"], introspect=False)
                method = "PowerOff" if device["power_off"] else "Eject" if device["eject"] else None
                if method:
                    getattr(drive, method)(options, dbus_interface="org.freedesktop.UDisks2.Drive", timeout=60,
                                           reply_handler=lambda *a: None,
                                           error_handler=lambda e: log("could not remove %s: %s" % (device["name"], e)))

            def unmount_next(*unused):
                if not parts:
                    power_off()
                    return
                part = parts.pop()
                self.sysbus.get_object(self.UDISKS, part["path"], introspect=False).Unmount(
                    options, dbus_interface=fs, timeout=120, reply_handler=unmount_next,
                    error_handler=lambda e: log("could not unmount %s: %s" % (part["name"], e)))

            unmount_next()

    # ----------------------------------------------------------- network

    def watch_network(self):
        self.status_watch(["nmcli", "monitor"], lambda line: True, self.read_network, 300)
        self.read_network()

    def read_network(self):
        self.counts["network checks"] += 1
        text = output_of(["nmcli", "-t", "-f", "TYPE,STATE,CONNECTION", "device", "status"])
        kind, name = None, ""
        for line in text.splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[1] == "connected" and parts[0] in ("wifi", "ethernet"):
                kind, name = parts[0], ":".join(parts[2:])
                if kind == "ethernet":
                    break
        if not text:
            self.network = None
        elif kind == "wifi":
            strength = 0
            for line in output_of(["nmcli", "-t", "-f", "IN-USE,SIGNAL", "device", "wifi", "list", "--rescan", "no"]).splitlines():
                if line.startswith("*:"):
                    strength = int(line[2:] or 0)
            level = "excellent" if strength > 75 else "good" if strength > 50 else "ok" if strength > 25 else "weak"
            self.network = {"kind": kind, "name": name, "strength": strength,
                            "icon": self.status_icon("network-wireless-signal-" + level,
                                                     "network-wireless-connected-%d" % (min(100, strength // 25 * 25 + 25)),
                                                     "network-wireless")}
        elif kind == "ethernet":
            self.network = {"kind": kind, "name": name,
                            "icon": self.status_icon("network-wired-activated", "network-wired")}
        else:
            self.network = {"kind": "none", "name": "",
                            "icon": self.status_icon("network-offline", "network-disconnect", "network-wired-disconnected")}
        self.changed()

    def network_click(self, button, rect):
        if button == "left" or button == "right":
            items = [{"header": True, "label": self.network["name"] if self.network and self.network["name"]
                      else "Not connected"},
                     {"id": "settings", "label": "Network settings"}]
            self.open_menu(items, rect, lambda c: detached(["systemsettings", "kcm_networkmanagement"]))

    # ----------------------------------------------------------- battery

    def read_battery(self):
        self.battery = None
        for supply in sorted(glob.glob("/sys/class/power_supply/*")):
            try:
                if open(os.path.join(supply, "type")).read().strip() != "Battery":
                    continue
                if os.path.exists(os.path.join(supply, "scope")) and \
                        open(os.path.join(supply, "scope")).read().strip() == "Device":
                    continue                              # a mouse's battery, not the computer's
                level = int(open(os.path.join(supply, "capacity")).read().strip())
                status = open(os.path.join(supply, "status")).read().strip()
            except (OSError, ValueError):
                continue
            charging = status in ("Charging", "Full")
            step = min(100, int(round(level / 10.0)) * 10)
            name = "battery-%03d%s" % (step, "-charging" if charging else "")
            self.battery = {"level": level, "charging": charging,
                            "icon": self.status_icon(name, "battery-level-%d%s-symbolic" % (step, "-charging" if charging else ""),
                                                     "battery")}
            break
        self.changed()

    # ------------------------------------------------------------- theme

    def check_mirage(self):
        """Follow Conky-Mirage's look: read its settings when they change."""
        path = os.path.join(os.path.dirname(self.mirage), "mirage.lua")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            if self.mirage_settings:
                self.mirage_settings = {}
                self.changed()
            return
        if mtime == self.mirage_mtime:
            return
        self.mirage_mtime = mtime
        text = open(path, encoding="utf-8", errors="replace").read()
        settings = {}
        for key, pattern in (("style", r'^style\s*=\s*"(\w+)"'), ("radius", r"^corner_radius\s*=\s*([\d.]+)"),
                             ("transparency", r"^transparency\s*=\s*([\d.]+)"),
                             ("blur", r"^blur_radius\s*=\s*([\d.]+)"), ("tint", r"^glass_tint\s*=\s*([\d.]+)")):
            m = re.search(pattern, text, re.M)
            if m:
                settings[key] = m.group(1) if key == "style" else float(m.group(1))
        if settings != self.mirage_settings:
            self.mirage_settings = settings
            self.request_theme()
            self.changed()

    def check_wallpaper(self):
        try:
            shell = self.bus.get_object("org.kde.plasmashell", "/PlasmaShell", introspect=False)
            shell.wallpaper(self.dbus.UInt32(0), dbus_interface="org.kde.PlasmaShell",
                            reply_handler=self.got_wallpaper, error_handler=lambda e: None)
        except self.dbus.DBusException:
            pass

    def got_wallpaper(self, info):
        key = json.dumps({str(k): str(v) for k, v in dict(info).items()}, sort_keys=True)
        if key != self.wallpaper_seen:
            first = self.wallpaper_seen is None
            self.wallpaper_seen = key
            if not first:
                self.request_theme(force=True)

    def request_theme(self, force=False):
        """Ask Mirage's helper for the palette and the glass behind the panel."""
        if not os.path.isfile(self.mirage) or "size" not in self.layout:
            return
        if self.theme_running:
            self.theme_again = True
            return
        try:
            w, h = (int(x) for x in self.layout["size"].split("x"))
        except ValueError:
            return
        panel = self.panel_geometry()
        if panel is None:
            screen = re.search(r"(\d+)/\d+x(\d+)/\d+\+0\+0", output_of(["xrandr", "--listmonitors"]))
            panel = (0, int(screen.group(2)) - h, h) if screen else (0, 1080 - h, h)
        s = self.lnum("scale", 1.0)
        m = self.mirage_settings
        # "--at=": the panel's x can be -1, which alone reads as an option.
        argv = [sys.executable, self.mirage, "theme", "--cache", os.path.join(self.cache, "theme"),
                "--at=%d,%d" % panel[:2], "--window", "%dx%d" % (w, h),
                "--tiles", self.layout.get("tiles", ""),
                "--radius", "%.2f" % self.lnum("radius", 12), "--scale", "%.4f" % s,
                "--blur", "%.1f" % (m.get("blur", 34) * s), "--tint", "%.3f" % m.get("tint", 0.26)]
        # Starting Mirage's helper costs a Python and Pillow start-up even when
        # it then finds nothing to do, so not for the same request twice.
        if not force and argv == self.theme_args and time.time() - self.last_theme < 120:
            return
        self.theme_args = argv
        self.counts["theme runs"] += 1
        os.makedirs(os.path.join(self.cache, "theme"), exist_ok=True)
        self.theme_running = True
        self.last_theme = time.time()
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except OSError as error:
            log("Mirage's helper did not start: %s" % error)
            self.theme_running = False
            return

        def done(code):
            self.theme_running = False
            err = proc.stderr.read() if proc.stderr else ""
            if code != 0 and err:
                log("Mirage's helper: " + err.strip().splitlines()[-1])
            path = os.path.join(self.cache, "theme", "theme.lua")
            try:
                text = open(path, encoding="utf-8").read()
                ident = re.search(r"\bid = (\d+)", text)
                theme = {"dir": os.path.join(self.cache, "theme"), "id": int(ident.group(1)) if ident else 0,
                         "stamp": os.path.getmtime(path)}
            except (OSError, AttributeError):
                theme = None
            if theme != self.theme:
                self.theme = theme
                self.changed()
                self.request_launcher_glass()
            if self.theme_again:
                self.theme_again = False
                self.request_theme()

        self.watch(proc, done)


# ------------------------------------------------------------------ doctor

def doctor():
    icons = Icons(os.path.join(CACHE, "doctor"))
    print("icon theme:", icons.theme_name, "->", ", ".join(icons.chain(icons.theme_name)))
    for name in ("org.kde.konsole", "audio-volume-high-symbolic", "network-wireless-signal-good-symbolic",
                 "start-here-kde", "application-x-executable"):
        print("  %-40s %s" % (name, icons.find(name, 24)))
    apps = Apps()
    print("apps:", len(apps.entries))
    print("Plasma's pinned apps:", [apps.resolve(x) for x in plasma_launchers()])
    print("Mirage's helper:", MIRAGE if os.path.isfile(MIRAGE) else "not found")
    try:
        import dbus
        bus = dbus.SessionBus()
        watcher = bus.get_object("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher")
        items = watcher.Get("org.kde.StatusNotifierWatcher", "RegisteredStatusNotifierItems",
                            dbus_interface="org.freedesktop.DBus.Properties")
        print("tray items:", [str(i) for i in items])
    except Exception as error:
        print("tray: not available (%s)" % error)
    print("volume:", output_of(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"]).strip() or "no wpctl")
    print("network:", output_of(["nmcli", "-t", "-f", "TYPE,STATE,CONNECTION", "device", "status"]).strip().replace("\n", "; ")
          or "no nmcli")


def screen():
    """The primary screen's width in the units Wayland places windows in
    (pixels over the desktop's scale), which is how long a panel along it is;
    then its width in pixels, the scale, and the height in both units.
    Started at login, kscreen may not answer yet, so it is asked again for up
    to 20s rather than the panel being made for a guessed screen."""
    tries = 40 if shutil.which("kscreen-doctor") else 0
    for attempt in range(tries):
        if attempt:
            time.sleep(0.5)
        try:
            data = json.loads(output_of(["kscreen-doctor", "-j"]) or "{}")
            outputs = [o for o in data.get("outputs", []) if o.get("enabled")]
            outputs.sort(key=lambda o: o.get("priority", 99))
            if outputs:
                o = outputs[0]
                size = o.get("size") or {}
                scale = float(o.get("scale") or 1)
                width, height = size.get("width"), size.get("height")
                if o.get("rotation") in (2, 8):
                    width, height = height, width
                if width and height:
                    # Rounded up: a panel a fraction short of the screen's
                    # edge let the wallpaper show through at its end.
                    return (math.ceil(width / scale - 1e-6), int(width), scale,
                            math.ceil(height / scale - 1e-6), int(height))
        except (ValueError, TypeError):
            pass
    m = re.search(r"(\d+)/\d+x(\d+)/\d+\+0\+0", output_of(["xrandr", "--listmonitors"]))
    width, height = (int(m.group(1)), int(m.group(2))) if m else (1920, 1080)
    return width, width, 1.0, height, height


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=("daemon", "doctor", "screen"), nargs="?", default="daemon")
    parser.add_argument("--cache", default=CACHE)
    parser.add_argument("--mirage", default=MIRAGE, help="Conky-Mirage's mirage.py, for the look")
    args = parser.parse_args()
    if args.command == "doctor":
        doctor()
        return 0
    if args.command == "screen":
        print("%d %d %.4f %d %d" % screen())
        return 0
    Daemon(args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
