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
        # ConkyTray folder: a terminal opened there, not at home.
        os.chdir(HOME)
        os.makedirs(self.cache, exist_ok=True)
        os.makedirs(CONFIG, exist_ok=True)
        write_atomic(os.path.join(self.cache, "daemon.pid"), "%d\n" % os.getpid())
        self.loop = GLib.MainLoop()
        self.icons = Icons(self.cache)
        self.apps = Apps()
        self.layout, self.layout_text = {}, None
        self.windows, self.panels, self.recency, self.tick = [], [], {}, 0
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

    def quit(self, *unused):
        self.loop.quit()
        return False

    def start(self):
        GLib = self.GLib
        self.read_layout()
        self.start_kwin()
        self.start_tray()
        self.watch_volume()
        self.watch_network()
        self.read_battery()
        self.GLib.timeout_add(1500, lambda: (self.start_launcher(), False)[1])
        GLib.timeout_add(40, self.poll_commands)
        GLib.timeout_add(500, self.poll_layout)
        GLib.timeout_add_seconds(3, self.poll_slow)
        GLib.timeout_add_seconds(30, self.poll_rare)
        self.changed()
        return False

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
        if any(old.get(k) != layout.get(k) for k in ("task_icon", "tray_icon", "ink", "tray_ink", "accent", "launcher_icon")):
            self.rebuild_tasks()
            self.rebuild_tray()
            self.read_volume()
            self.read_network()
            self.read_battery()
        self.request_theme()
        self.changed()
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
        return True

    def poll_rare(self):
        self.read_battery()
        self.read_network()
        if time.time() - self.last_theme > 120:
            self.request_theme()
        return True

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
        self.close_menu()
        if not (what == "launcher" and parts[1] == "left") and self.launcher:
            self.tell_launcher({"cmd": "hide"})
        if what == "task":
            self.task_click(parts[1], parts[2], float(parts[3]), float(parts[4]))
        elif what == "tray":
            self.tray_click(parts[1], parts[2], float(parts[3]), float(parts[4]))
        elif what == "scroll":
            self.scroll(parts[1], parts[2], parts[3])
        elif what == "launcher":
            self.launcher_click(parts[1], float(parts[2]), float(parts[3]))
        elif what == "volume":
            self.volume_click(parts[1], float(parts[2]), float(parts[3]))
        elif what == "network":
            self.network_click(parts[1], float(parts[2]), float(parts[3]))
        elif what == "battery":
            if parts[1] == "left":
                detached(["systemsettings", "kcm_powerdevilprofilesconfig"])
        elif what == "clock":
            self.clock_click(parts[1], float(parts[2]), float(parts[3]))
        # "close-menu", a click on the panel where nothing is, needs nothing more.

    # ------------------------------------------------------------- state

    def write_state(self):
        self.write_pending = False
        ds = self.lnum("ds", 1.0)
        panel = self.panel_geometry()
        state = {
            "tasks": [{k: t.get(k) for k in ("key", "name", "icon", "pinned", "windows", "active", "attention",
                                             "minimized", "starting")} for t in self.tasks_list()],
            "tray": [{k: t[k] for k in ("id", "icon", "attention", "title")} for t in self.tray],
            "volume": self.volume, "network": self.network, "battery": self.battery,
            "launcher": {"icon": self.launcher_icon()},
            "theme": self.theme, "mirage": self.mirage_settings or None,
            "panel": {"x": panel[0], "y": panel[1], "h": panel[2]} if panel else None,
            "ds": ds,
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

    # ------------------------------------------------------------- KWin

    def kwin(self):
        obj = self.bus.get_object("org.kde.KWin", "/Scripting")
        return self.dbus.Interface(obj, "org.kde.kwin.Scripting")

    def start_kwin(self):
        try:
            scripting = self.kwin()
            if scripting.isScriptLoaded("conkytray"):
                scripting.unloadScript("conkytray")
            sid = scripting.loadScript(os.path.join(HERE, "tray_kwin.js"), "conkytray", signature="ss")
            script = self.bus.get_object("org.kde.KWin", "/Scripting/Script%d" % sid)
            self.dbus.Interface(script, "org.kde.kwin.Script").run()
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
                                             text=True, bufsize=1)
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

    def launcher_place(self):
        """Where the menu goes, in KWin's coordinates: its left edge over the
        panel's button, its bottom just above the panel."""
        ds = self.lnum("ds", 1.0)
        m = round(6 * self.lnum("scale", 1.0))
        panel = self.panel_geometry() or (0, 0, 0)
        _, above = self.screen_point(m, m)
        return (panel[0] + m) / ds, above if self.panel_geometry() else None

    def request_launcher_glass(self):
        """The menu's own frosted pane, cut from the wallpaper where it opens."""
        left, above = self.launcher_place()
        if above is None or self.launcher_glass_running or not os.path.isfile(self.mirage):
            return
        ds = self.lnum("ds", 1.0)
        w, h = round(LAUNCHER_W * ds), round(LAUNCHER_H * ds)
        x, y = round(left * ds), round((above - 6 - LAUNCHER_H) * ds)
        cache = os.path.join(self.cache, "launcher")
        os.makedirs(cache, exist_ok=True)
        s = self.lnum("scale", 1.0)
        m = self.mirage_settings
        argv = [sys.executable, self.mirage, "theme", "--cache", cache, "--at", "%d,%d" % (x, y),
                "--window", "%dx%d" % (w, h), "--tiles", "0,0,%d,%d" % (w, h),
                "--radius", "%.2f" % (16 * ds), "--scale", "%.4f" % s,
                "--blur", "%.1f" % (m.get("blur", 34) * s), "--tint", "%.3f" % m.get("tint", 0.26)]
        self.launcher_glass_running = True
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self.launcher_glass_running = False
            return

        def done(pid, status):
            self.launcher_glass_running = False
            path = os.path.join(cache, "glass_1.png")
            self.launcher_glass = path if os.path.isfile(path) else None

        self.GLib.child_watch_add(proc.pid, done)

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

        try:
            sid = self.kwin().loadScript(path, name, signature="ss")
            script = self.bus.get_object("org.kde.KWin", "/Scripting/Script%d" % sid)
            self.dbus.Interface(script, "org.kde.kwin.Script").run(reply_handler=lambda: None,
                                                                   error_handler=lambda e: log(str(e)))
        except self.dbus.DBusException as error:
            log("KWin action failed: %s" % error)
        self.GLib.timeout_add(1500, cleanup)

    def got_windows(self, text):
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
        self.rebuild_tasks()
        panel = self.panel_geometry()
        if panel != getattr(self, "panel_seen", None):
            self.panel_seen = panel
            self.request_theme()
        self.changed()

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

        def done(pid, status):
            finished[0] = True
            if status != 0:
                log("%s did not start it (exit status %d)" % (argv[0], status))
                self.start_first(launchers[1:])

        def stuck():
            # kstart hangs, rather than exiting, when it cannot find the app.
            if not finished[0] and proc.poll() is None:
                log("%s hung; trying the next way" % argv[0])
                proc.kill()
            return False

        self.GLib.child_watch_add(proc.pid, done)
        self.GLib.timeout_add_seconds(8, stuck)

    def task_click(self, button, key, cx, top):
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

            self.open_menu(items, cx, top, chosen)

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
                        "sort": (self.CATEGORY.get(str(p.get("Category", "")), 9), str(p.get("Id", "")).lower()),
                        "full": item["full"]})
        out.sort(key=lambda t: t["sort"])
        self.tray = out

    def tray_item(self, ident):
        for t in self.tray:
            if t["id"] == ident:
                return self.tray_items.get(t["full"])
        return None

    def screen_point(self, cx, top):
        """A point in the window as KWin's coordinates (what tray items and
        menus want), from window pixels."""
        ds = self.lnum("ds", 1.0)
        panel = self.panel_geometry() or (0, 0, 0)
        return int((panel[0] + cx) / ds), int((panel[1] + top) / ds)

    def tray_click(self, button, ident, cx, top):
        item = self.tray_item(ident)
        if not item:
            return
        p = item["props"]
        obj = self.bus.get_object(item["service"], item["path"], introspect=False)
        x, y = self.screen_point(cx, top)
        menu_path = str(p.get("Menu", "") or "")
        ignore = {"reply_handler": lambda *a: None, "error_handler": lambda e: None}

        def show_menu():
            if menu_path and menu_path != "/":
                self.open_menu(None, cx, top, None, dbusmenu=(item["service"], menu_path))
            else:
                obj.ContextMenu(x, y, dbus_interface=self.SNI, **ignore)

        if button == "left":
            if p.get("ItemIsMenu"):
                show_menu()
            else:
                # An item that has no Activate answers with an error; its
                # menu is then the next best thing.
                obj.Activate(x, y, dbus_interface=self.SNI, reply_handler=lambda *a: None,
                             error_handler=lambda e: show_menu())
        elif button == "middle":
            obj.SecondaryActivate(x, y, dbus_interface=self.SNI, **ignore)
        elif button == "right":
            show_menu()

    # ------------------------------------------------------------ menus

    def palette_for_menu(self):
        return {k: self.layout.get("menu_" + k) for k in ("bg", "fg", "accent", "edge", "hover", "muted", "radius")}

    def open_menu(self, items, cx, top, chosen, dbusmenu=None):
        """Show a menu above the panel, centred on window x `cx`."""
        self.close_menu()
        x, above = self.screen_point(cx, top)
        spec = {"x": x, "above": above if self.panel_geometry() else None,
                "colors": self.palette_for_menu(), "items": items}
        if dbusmenu:
            spec["dbusmenu"] = {"service": dbusmenu[0], "path": dbusmenu[1]}
        try:
            proc = subprocess.Popen([sys.executable, os.path.join(HERE, "tray_menu.py"), json.dumps(spec)],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        except OSError as error:
            log("no menu: %s" % error)
            return
        self.menu = proc

        def done(pid, status):
            out = proc.stdout.read().strip() if proc.stdout else ""
            if self.menu is proc:
                self.menu = None
            if out and chosen:
                chosen(out)

        self.GLib.child_watch_add(proc.pid, done)

    def close_menu(self):
        if self.menu and self.menu.poll() is None:
            try:
                self.menu.terminate()
            except OSError:
                pass
        self.menu = None

    def launcher_icon(self):
        size = int(self.lnum("launcher_icon", 0))
        if size <= 0:
            return None
        for name in (self.layout.get("launcher_name") or "", plasma_kickoff_icon() or "",
                     "start-here-kde-plasma", "start-here-kde", "start-here"):
            if name and name != "auto":
                path = self.icons.find(name, size)
                if path:
                    return self.icons.render(path, size, self.layout.get("tray_ink"), self.layout.get("accent"))
        return None

    def launcher_click(self, button, cx, top):
        if button == "left":
            left, above = self.launcher_place()
            if self.launcher_glass is None:
                self.request_launcher_glass()
            self.tell_launcher({"cmd": "toggle", "left": left, "above": above,
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

            self.open_menu(items, cx, top, chosen)

    def clock_click(self, button, cx, top):
        if button == "right":
            items = [{"id": "settings", "label": "Date and time settings"}]
            self.open_menu(items, cx, top, lambda c: detached(["systemsettings", "kcm_clock"]))

    # ------------------------------------------------------------ volume

    def watch_volume(self):
        self.status_watch(["pactl", "subscribe"], lambda line: b"sink" in line or b"server" in line,
                          self.read_volume, 80)
        self.read_volume()

    def status_watch(self, argv, wanted, refresh, delay):
        """Run a monitor (pactl subscribe, nmcli monitor) and refresh when it
        reports something relevant, at most every `delay` ms."""
        GLib = self.GLib
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
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
        for n in (name + "-symbolic", name) + tuple(more):
            path = self.icons.find(n, size)
            if path:
                return self.icons.render(path, size, self.layout.get("tray_ink"), self.layout.get("accent"))
        return None

    def volume_click(self, button, cx, top):
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
                    self.volume_click("left", cx, top)
                elif choice == "settings":
                    detached(["systemsettings", "kcm_pulseaudio"])

            self.open_menu(items, cx, top, chosen)

    def scroll(self, target, ident, direction):
        if target == "volume":
            step = {"up": "5%+", "down": "5%-"}.get(direction)
            if step:
                subprocess.run(["wpctl", "set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", step], timeout=3)
                self.read_volume()
        elif target == "tray":
            item = self.tray_item(ident)
            if item and direction in ("up", "down"):
                obj = self.bus.get_object(item["service"], item["path"], introspect=False)
                obj.Scroll(120 if direction == "up" else -120, "vertical", dbus_interface=self.SNI,
                           reply_handler=lambda *a: None, error_handler=lambda e: None)

    # ----------------------------------------------------------- network

    def watch_network(self):
        self.status_watch(["nmcli", "monitor"], lambda line: True, self.read_network, 300)
        self.read_network()

    def read_network(self):
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

    def network_click(self, button, cx, top):
        if button == "left" or button == "right":
            items = [{"header": True, "label": self.network["name"] if self.network and self.network["name"]
                      else "Not connected"},
                     {"id": "settings", "label": "Network settings"}]
            self.open_menu(items, cx, top, lambda c: detached(["systemsettings", "kcm_networkmanagement"]))

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
                self.request_theme()

    def request_theme(self):
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
        argv = [sys.executable, self.mirage, "theme", "--cache", os.path.join(self.cache, "theme"),
                "--at", "%d,%d" % panel[:2], "--window", "%dx%d" % (w, h),
                "--tiles", self.layout.get("tiles", ""),
                "--radius", "%.2f" % self.lnum("radius", 12), "--scale", "%.4f" % s,
                "--blur", "%.1f" % (m.get("blur", 34) * s), "--tint", "%.3f" % m.get("tint", 0.26)]
        os.makedirs(os.path.join(self.cache, "theme"), exist_ok=True)
        self.theme_running = True
        self.last_theme = time.time()
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        except OSError as error:
            log("Mirage's helper did not start: %s" % error)
            self.theme_running = False
            return

        def done(pid, status):
            self.theme_running = False
            err = proc.stderr.read() if proc.stderr else ""
            if status != 0 and err:
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

        self.GLib.child_watch_add(proc.pid, done)


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
    (pixels over the desktop's scale), which is how wide the panel is; then
    its width in pixels, and the scale."""
    try:
        data = json.loads(output_of(["kscreen-doctor", "-j"]) or "{}")
        outputs = [o for o in data.get("outputs", []) if o.get("enabled")]
        outputs.sort(key=lambda o: o.get("priority", 99))
        if outputs:
            o = outputs[0]
            size = o.get("size") or {}
            scale = float(o.get("scale") or 1)
            width = size.get("width")
            if o.get("rotation") in (2, 8):
                width = size.get("height")
            if width:
                return int(round(width / scale)), int(width), scale
    except (ValueError, TypeError):
        pass
    m = re.search(r"(\d+)/\d+x\d+/\d+\+0\+0", output_of(["xrandr", "--listmonitors"]))
    width = int(m.group(1)) if m else 1920
    return width, width, 1.0


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
        print("%d %d %.4f" % screen())
        return 0
    Daemon(args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
