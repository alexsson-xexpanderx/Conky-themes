#!/usr/bin/env python3
"""ConkyTray's app menu, opened by the button at the left end of the panel.

    tray_launcher.py           (started and kept running by tray.py)

Laid out like Plasma's Kickoff: who you are and a search box along the top,
Favorites, All Applications and the categories down the left, the apps in a
grid, Applications and Places, and the session buttons along the bottom. It
wears the panel's look, in the palette Conky-Mirage made for the wallpaper.

Reading every app and building the grid takes about a second, so tray.py
keeps this running, hidden, and it only shows itself. tray.py sends one
JSON object per line on stdin:

    {"cmd": "toggle", "left": .., "above": .., "look": .., "palette": {..},
     "glass": "<png or null>", "pins": [..]}
    {"cmd": "hide"}

and this answers one line per action on stdout, for tray.py to carry out:
"launch <app>", "open <path or url>", "pin <app>", "unpin <app>",
"power <sleep|lock|logout|reboot|shutdown>", "settings", "search <text>".

Like the menus, it is a GTK 3 layer-shell surface that takes the keyboard
when it opens, so a click anywhere else closes it.
"""

import configparser
import json
import os
import pwd
import re
import sqlite3
import sys
import time
from xml.etree import ElementTree

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, GtkLayerShell, Pango  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tray  # noqa: E402

WIDTH, HEIGHT = tray.LAUNCHER_W, tray.LAUNCHER_H
SIDEBAR = 236
COLUMNS = 5
ICON = 48

# The freedesktop main categories, under the names and icons Plasma's menu
# gives them. Settings go with System, as in Plasma's menu.
CATEGORIES = [
    ("Development", ["Development"], "applications-development"),
    ("Education", ["Education"], "applications-education"),
    ("Games", ["Game"], "applications-games"),
    ("Graphics", ["Graphics"], "applications-graphics"),
    ("Help", ["Documentation", "Help"], "help-browser"),
    ("Internet", ["Network"], "applications-internet"),
    ("Multimedia", ["AudioVideo", "Audio", "Video"], "applications-multimedia"),
    ("Office", ["Office"], "applications-office"),
    ("Science", ["Science"], "applications-science"),
    ("System", ["System", "Settings"], "applications-system"),
    ("Utilities", ["Utility"], "applications-utilities"),
]
FAVORITES_FILE = os.path.join(tray.CONFIG, "favorites")


def out(line):
    print(line, flush=True)


# ------------------------------------------------------------------ data

def user_name():
    try:
        entry = pwd.getpwuid(os.getuid())
        full = entry.pw_gecos.split(",")[0].strip()
        return full or entry.pw_name
    except KeyError:
        return os.environ.get("USER", "")


def avatar():
    user = os.environ.get("USER") or pwd.getpwuid(os.getuid()).pw_name
    for path in (os.path.expanduser("~/.face.icon"), os.path.expanduser("~/.face"),
                 "/var/lib/AccountsService/icons/" + user):
        if os.path.isfile(path):
            return path
    return None


def kde_favorites(apps):
    """Plasma's Kickoff favourites, in Kickoff's order: the links live in the
    activity manager's database, the order in kactivitymanagerd-statsrc."""
    found = []
    db = os.path.expanduser("~/.local/share/kactivitymanagerd/resources/database")
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=1)
        rows = con.execute("SELECT targettedResource FROM ResourceLink WHERE initiatingAgent = "
                           "'org.kde.plasma.favorites.applications'").fetchall()
        con.close()
        found = [r[0] for r in rows]
    except sqlite3.Error:
        pass
    order = []
    try:
        text = open(os.path.expanduser("~/.config/kactivitymanagerd-statsrc"), encoding="utf-8").read()
        kickoff = None
        applets = open(os.path.expanduser("~/.config/plasma-org.kde.plasma.desktop-appletsrc"), encoding="utf-8").read()
        m = re.search(r"\[Containments\]\[\d+\]\[Applets\]\[(\d+)\]\n(?:[^\[]*\n)*?plugin=org\.kde\.plasma\.kickoff", applets)
        if m:
            kickoff = m.group(1)
        for block in re.findall(r"\[Favorites-org\.kde\.plasma\.kickoff\.favorites\.instance-(\d+)-([^\]]+)\]\nordering=(.*)", text):
            if kickoff is None or block[0] == kickoff:
                if not order or block[1] == "global":
                    order = block[2].split(",")
    except OSError:
        pass
    result = []
    for item in [x for x in order if x in found] + [x for x in found if x not in order]:
        ident = apps.resolve(item)
        if ident and ident not in result:
            result.append(ident)
    return result


def load_favorites(apps):
    if os.path.exists(FAVORITES_FILE):
        return [x.strip() for x in open(FAVORITES_FILE, encoding="utf-8") if x.strip() and not x.startswith("#")]
    favorites = kde_favorites(apps)
    save_favorites(favorites)
    return favorites


def save_favorites(favorites):
    os.makedirs(tray.CONFIG, exist_ok=True)
    tray.write_atomic(FAVORITES_FILE, "# Favourites in ConkyTray's app menu, one desktop file name per line.\n" +
                      "".join(f + "\n" for f in favorites))


def merged_menus(apps):
    """The extra submenus programs install (Wine, CrossOver, Chrome's web
    apps), from menus/applications-merged: name, icon and the apps in them."""
    dirs = [os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "menus")]
    dirs += [os.path.join(d, "menus") for d in (os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg").split(":")]
    menus = {}
    for d in dirs:
        merged = os.path.join(d, "applications-merged")
        try:
            files = sorted(os.listdir(merged))
        except OSError:
            continue
        for name in files:
            if not name.endswith(".menu"):
                continue
            try:
                root = ElementTree.parse(os.path.join(merged, name)).getroot()
            except (ElementTree.ParseError, OSError):
                continue
            for sub in root.findall("Menu"):
                key = (sub.findtext("Name") or "").strip()
                if not key:
                    continue
                menu = menus.setdefault(key, {"directory": None, "files": set(), "categories": set()})
                menu["directory"] = menu["directory"] or (sub.findtext("Directory") or "").strip()
                for include in sub.iter("Include"):
                    for f in include.iter("Filename"):
                        menu["files"].add((f.text or "").strip()[:-8] if (f.text or "").strip().endswith(".desktop")
                                          else (f.text or "").strip())
                    for c in include.iter("Category"):
                        menu["categories"].add((c.text or "").strip())
    result = []
    for key, menu in menus.items():
        title, icon = key, "folder"
        if menu["directory"]:
            for base in tray.data_dirs():
                path = os.path.join(base, "desktop-directories", menu["directory"])
                if os.path.isfile(path):
                    parser = configparser.RawConfigParser(strict=False, interpolation=None)
                    parser.optionxform = str
                    try:
                        parser.read(path, encoding="utf-8")
                        title = parser.get("Desktop Entry", "Name", fallback=title)
                        icon = parser.get("Desktop Entry", "Icon", fallback=icon)
                    except (configparser.Error, UnicodeDecodeError):
                        pass
                    break
        members = [i for i, e in apps.entries.items() if e["shown"] and
                   (i in menu["files"] or menu["categories"] & set(e["categories"]))]
        if members:
            result.append((title, members, icon))
    return result


def places():
    home = os.path.expanduser("~")
    dirs = {}
    try:
        for line in open(os.path.join(home, ".config", "user-dirs.dirs"), encoding="utf-8"):
            m = re.match(r'XDG_(\w+)_DIR="(.*)"', line.strip())
            if m:
                dirs[m.group(1)] = m.group(2).replace("$HOME", home)
    except OSError:
        pass
    list_ = [("Home", home, "user-home")]
    for key, title, icon in (("DESKTOP", "Desktop", "user-desktop"), ("DOCUMENTS", "Documents", "folder-documents"),
                             ("DOWNLOAD", "Downloads", "folder-download"), ("MUSIC", "Music", "folder-music"),
                             ("PICTURES", "Pictures", "folder-pictures"), ("VIDEOS", "Videos", "folder-videos")):
        path = dirs.get(key)
        if path and path != home and os.path.isdir(path):
            list_.append((title, path, icon))
    list_ += [("Trash", "trash:/", "user-trash"), ("Root folder", "/", "drive-harddisk")]
    return list_


# ------------------------------------------------------------------ look

def rgba(color, alpha=1.0):
    color = (color or "#808080").lstrip("#")
    r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
    return "rgba(%d,%d,%d,%.3f)" % (r, g, b, alpha)


def look_css(look, p, glass):
    """The menu's style sheet for one of the four looks."""
    ink = p.get("ink", "#f4f4f8")
    tiles = []
    if look == "metro":
        bg, fg, radius, item = rgba(p.get("metro2")), "#ffffff", 0, 0
        hover, selected, field = rgba("#ffffff", 0.14), rgba("#ffffff", 0.22), rgba("#ffffff", 0.16)
        edge, muted = rgba("#ffffff", 0.0), rgba("#ffffff", 0.72)
        tiles = [p.get("metro1"), p.get("metro3"), p.get("metro4")]
        background = "background-color: %s;" % bg
    elif look == "pixel":
        fg = p.get("px_on", ink)
        bg, radius, item = rgba(p.get("px_surface")), 28, 20
        hover, selected, field = rgba(fg, 0.08), rgba(p.get("px_secondary_c")), rgba(p.get("px_primary_c"))
        edge, muted = rgba(fg, 0.0), rgba(fg, 0.62)
        background = "background-color: %s;" % bg
    elif look == "solid":
        fg, radius, item = ink, 14, 10
        hover, selected, field = rgba(ink, 0.08), rgba(ink, 0.14), rgba(ink, 0.08)
        edge, muted = rgba(ink, 0.10), rgba(ink, 0.6)
        background = "background-color: %s;" % rgba(p.get("solid"))
    else:
        fg, radius, item = ink, 16, 10
        hover, selected, field = rgba(ink, 0.10), rgba(ink, 0.16), rgba(ink, 0.10)
        edge, muted = rgba(ink, 0.16), rgba(ink, 0.6)
        background = "background-color: %s;" % rgba(p.get("tint"), 0.94)
        if glass and os.path.isfile(glass):
            background += " background-image: url('file://%s'); background-size: 100%% 100%%;" % glass
    css = """
    window { background: transparent; }
    .launcher { %(background)s color: %(fg)s; border-radius: %(r)dpx; border: 1px solid %(edge)s;
                font-family: "Noto Sans"; }
    .launcher label { color: %(fg)s; }
    .launcher .muted { color: %(muted)s; }
    .launcher button { background: none; border: none; box-shadow: none; outline: none; color: %(fg)s;
                       border-radius: %(i)dpx; padding: 6px 10px; min-height: 0; text-shadow: none;
                       -gtk-icon-shadow: none; }
    .launcher button:hover { background-color: %(hover)s; }
    .launcher button.selected { background-color: %(selected)s; }
    .launcher .top { padding: 14px 16px 10px 16px; }
    .launcher .user { font-size: 12pt; }
    .launcher .avatar { background-color: %(selected)s; border-radius: 99px; min-width: 32px; min-height: 32px; }
    .launcher entry { background: %(field)s; color: %(fg)s; border: none; box-shadow: none;
                      border-radius: %(f)dpx; padding: 6px 12px; min-height: 22px; caret-color: %(fg)s; }
    .launcher entry image { color: %(muted)s; }
    .launcher entry selection { background-color: %(selected)s; }
    .launcher .side { padding: 4px 6px 6px 12px; }
    .launcher .side button { padding: 5px 10px; }
    .launcher .side label { font-size: 10.5pt; }
    .launcher .grid { padding: 4px 12px 8px 4px; }
    .launcher .grid flowboxchild { padding: 0; background: none; outline: none; }
    .launcher .app { padding: 10px 4px 8px 4px; }
    .launcher .app label { font-size: 9.5pt; }
    .launcher .bottom { padding: 8px 14px 12px 14px; }
    .launcher .bottom button label { font-size: 10pt; }
    .launcher separator { background-color: %(edge2)s; min-height: 1px; margin: 6px 10px; }
    .launcher .context { %(background)s border: 1px solid %(edge2)s; border-radius: %(i)dpx; padding: 4px; }
    .launcher scrollbar { background: none; border: none; }
    .launcher scrollbar slider { background-color: %(selected)s; min-width: 4px; border-radius: 4px; border: none; }
    .launcher .empty { font-size: 11pt; }
    """ % {"background": background, "fg": fg, "r": radius, "edge": edge, "muted": muted, "i": item,
           "hover": hover, "selected": selected, "field": field, "f": max(item, 8) if look != "pixel" else 99,
           "edge2": rgba(fg, 0.12)}
    # Metro's apps are tiles, each in one of the other three colours.
    for n, colour in enumerate(tiles):
        css += """
    .launcher .app.tile%d { background-color: %s; border-radius: 0; margin: 3px; }
    .launcher .app.tile%d:hover { background-image: linear-gradient(%s, %s); }
    """ % (n, rgba(colour), n, rgba("#ffffff", 0.14), rgba("#ffffff", 0.14))
    return css


# ------------------------------------------------------------------ window

class Launcher(Gtk.Window):
    def __init__(self):
        super().__init__()
        self.apps = tray.Apps()
        self.favorites = [f for f in load_favorites(self.apps) if f in self.apps.entries]
        self.pins = []
        self.section = "favorites"
        self.query = ""
        self.hidden_at = 0.0
        self.had_focus = False
        self.shown_at = 0.0
        self.provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), self.provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_USER + 10)
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_app_paintable(True)
        # TRAY_LAUNCHER_PLAIN=1 makes it an ordinary window, which a virtual
        # X screen can show and capture; otherwise it is a layer surface.
        self.layered = not os.environ.get("TRAY_LAUNCHER_PLAIN")
        if self.layered:
            GtkLayerShell.init_for_window(self)
            GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
            GtkLayerShell.set_namespace(self, "conkytray-launcher")
            GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.ON_DEMAND)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.BOTTOM, True)
            GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.LEFT, True)
            # Placed from the screen's edge, not from the top of the space
            # panels keep for themselves (where a margin would otherwise count
            # from, putting the menu that much too high).
            GtkLayerShell.set_exclusive_zone(self, -1)
        else:
            self.set_decorated(False)
        self.set_default_size(WIDTH, HEIGHT)
        self.set_size_request(WIDTH, HEIGHT)
        self.connect("draw", self.clear)
        self.connect("focus-in-event", self.focus_in)
        self.connect("focus-out-event", self.focus_out)
        self.connect("key-press-event", self.key)
        self.build()

    # ------------------------------------------------------------ building

    @staticmethod
    def clear(widget, cr):
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(0)
        cr.paint()
        cr.set_operator(2)
        return False

    def icon_image(self, name, size):
        if name and name.startswith("/"):
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(name, size, size, True)
                return Gtk.Image.new_from_pixbuf(pixbuf)
            except GLib.Error:
                name = "application-x-executable"
        image = Gtk.Image.new_from_icon_name(name or "application-x-executable", Gtk.IconSize.DIALOG)
        image.set_pixel_size(size)
        return image

    def build(self):
        self.overlay = Gtk.Overlay()
        self.add(self.overlay)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.get_style_context().add_class("launcher")
        self.overlay.add(box)

        # Who you are, and the search.
        top = Gtk.Box(spacing=10)
        top.get_style_context().add_class("top")
        face = avatar()
        if face:
            try:
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(face, 32, 32, True)
                top.pack_start(Gtk.Image.new_from_pixbuf(pixbuf), False, False, 0)
            except GLib.Error:
                face = None
        name = user_name()
        if not face:
            initial = Gtk.Label(label=(name[:1] or "?").upper())
            initial.get_style_context().add_class("avatar")
            top.pack_start(initial, False, False, 0)
        user = Gtk.Label(label=name, xalign=0)
        user.get_style_context().add_class("user")
        user.set_size_request(SIDEBAR - 58, -1)
        user.set_ellipsize(Pango.EllipsizeMode.END)
        top.pack_start(user, False, False, 0)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search…")
        self.search.connect("search-changed", self.search_changed)
        self.search.connect("activate", self.search_activate)
        self.search.connect("key-press-event", self.search_key)
        top.pack_start(self.search, True, True, 0)
        settings = Gtk.Button()
        settings.add(self.icon_image("configure", 18))
        settings.set_tooltip_text("ConkyTray settings")
        settings.connect("clicked", lambda b: self.act("settings"))
        top.pack_start(settings, False, False, 0)
        box.pack_start(top, False, False, 0)

        # The sections down the left, the apps (or places) on the right.
        body = Gtk.Box()
        box.pack_start(body, True, True, 0)
        side_scroll = Gtk.ScrolledWindow()
        side_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        side_scroll.set_size_request(SIDEBAR, -1)
        self.side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.side.get_style_context().add_class("side")
        side_scroll.add(self.side)
        body.pack_start(side_scroll, False, False, 0)

        self.stack = Gtk.Stack()
        body.pack_start(self.stack, True, True, 0)
        self.grid_scroll = Gtk.ScrolledWindow()
        self.grid_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.grid = Gtk.FlowBox()
        self.grid.get_style_context().add_class("grid")
        self.grid.set_valign(Gtk.Align.START)
        self.grid.set_homogeneous(True)
        self.grid.set_min_children_per_line(COLUMNS)
        self.grid.set_max_children_per_line(COLUMNS)
        self.grid.set_selection_mode(Gtk.SelectionMode.NONE)
        self.grid.set_filter_func(self.visible)
        self.grid.set_sort_func(self.order)
        self.grid_scroll.add(self.grid)
        self.stack.add_named(self.grid_scroll, "apps")
        self.empty = Gtk.Label(label="")
        self.empty.get_style_context().add_class("muted")
        self.empty.get_style_context().add_class("empty")
        self.stack.add_named(self.empty, "empty")
        self.places_box = Gtk.FlowBox()
        self.places_box.get_style_context().add_class("grid")
        self.places_box.set_valign(Gtk.Align.START)
        self.places_box.set_homogeneous(True)
        self.places_box.set_min_children_per_line(COLUMNS)
        self.places_box.set_max_children_per_line(COLUMNS)
        self.places_box.set_selection_mode(Gtk.SelectionMode.NONE)
        for i, (title, path, icon) in enumerate(places()):
            self.places_box.add(self.tile(title, icon, lambda p=path: self.act("open " + p), i))
        self.stack.add_named(self.places_box, "places")

        # The apps, built once; sections and searches only filter and sort.
        self.children = {}
        # A favourite is shown even if its file asks not to be (Plasma's own
        # menu does the same): the user chose it.
        shown = sorted((i for i, e in self.apps.entries.items() if e["shown"] or i in self.favorites),
                       key=lambda i: self.apps.entries[i]["name"].casefold())
        self.alpha = {ident: n for n, ident in enumerate(shown)}
        for n, ident in enumerate(shown):
            e = self.apps.entries[ident]
            button = self.tile(e["name"], e["icon"], lambda i=ident: self.launch(i), n)
            button.connect("button-press-event", lambda w, ev, i=ident: self.app_press(w, ev, i))
            tip = e.get("generic") or e.get("comment")
            if tip and tip != e["name"]:
                button.set_tooltip_text(tip)
            child = Gtk.FlowBoxChild()
            child.add(button)
            child.ident = ident
            self.children[ident] = child
            self.grid.add(child)

        self.sections = [("favorites", "Favorites", "bookmarks", None),
                         ("all", "All Applications", "applications-all", None), None]
        groups = []
        allocated = set()
        for title, members, icon in merged_menus(self.apps):
            groups.append((title, set(members), icon))
            allocated |= set(members)
        for title, cats, icon in CATEGORIES:
            members = {i for i, e in self.apps.entries.items() if e["shown"] and i not in allocated and
                       set(cats) & set(e["categories"])}
            if members:
                groups.append((title, members, icon))
        placed = set().union(*[g[1] for g in groups]) if groups else set()
        other = {i for i in self.alpha if i not in placed and self.apps.entries[i]["shown"]}
        if other:
            groups.append(("Lost & Found", other, "applications-other"))
        groups.sort(key=lambda g: (g[0] == "Lost & Found", g[0].casefold()))
        for title, members, icon in groups:
            self.sections.append(("cat:" + title, title, icon, members))
        self.section_buttons = {}
        for entry in self.sections:
            if entry is None:
                self.side.pack_start(Gtk.Separator(), False, False, 0)
                continue
            key, title, icon, _ = entry
            button = Gtk.Button()
            row = Gtk.Box(spacing=10)
            row.pack_start(self.icon_image(icon, 18), False, False, 0)
            row.pack_start(Gtk.Label(label=title, xalign=0), True, True, 0)
            button.add(row)
            button.connect("clicked", lambda b, k=key: self.choose(k))
            self.side.pack_start(button, False, False, 0)
            self.section_buttons[key] = button
        self.members = {e[0]: e[3] for e in self.sections if e}

        # Applications and Places; the session.
        bottom = Gtk.Box(spacing=4)
        bottom.get_style_context().add_class("bottom")
        self.tab_apps = self.bottom_button("Applications", "view-list-icons", lambda: self.choose("favorites"))
        self.tab_places = self.bottom_button("Places", "compass", lambda: self.choose("places"))
        bottom.pack_start(self.tab_apps, False, False, 0)
        bottom.pack_start(self.tab_places, False, False, 0)
        for title, icon, action in reversed((("Lock", "system-lock-screen", "lock"), ("Log out", "system-log-out", "logout"),
                                             ("Sleep", "system-suspend", "sleep"), ("Restart", "system-reboot", "reboot"),
                                             ("Shut down", "system-shutdown", "shutdown"))):
            bottom.pack_end(self.bottom_button(title, icon, lambda a=action: self.act("power " + a)), False, False, 0)
        box.pack_start(bottom, False, False, 0)

        # A small menu for an app, on right-click, drawn inside the window.
        self.context = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.context.get_style_context().add_class("context")
        self.context.set_halign(Gtk.Align.START)
        self.context.set_valign(Gtk.Align.START)
        self.context.set_no_show_all(True)
        self.overlay.add_overlay(self.context)
        self.connect("button-press-event", self.overlay_press)
        self.refresh()

    def tile(self, title, icon, action, n):
        button = Gtk.Button()
        button.get_style_context().add_class("app")
        button.get_style_context().add_class("tile%d" % (n % 3))
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        column.pack_start(self.icon_image(icon, ICON), False, False, 0)
        label = Gtk.Label(label=title, justify=Gtk.Justification.CENTER)
        label.set_line_wrap(True)
        label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_lines(2)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_max_width_chars(13)
        label.set_width_chars(13)
        column.pack_start(label, False, False, 0)
        button.add(column)
        button.connect("clicked", lambda b: action())
        return button

    def bottom_button(self, title, icon, action):
        button = Gtk.Button()
        row = Gtk.Box(spacing=6)
        row.pack_start(self.icon_image(icon, 16), False, False, 0)
        row.pack_start(Gtk.Label(label=title), False, False, 0)
        button.add(row)
        button.connect("clicked", lambda b: action())
        return button

    # ------------------------------------------------------------ filtering

    def visible(self, child):
        ident = child.ident
        if self.query:
            return self.score(ident) > 0
        if self.section == "favorites":
            return ident in self.favorites
        if self.section == "all":
            return True
        return ident in (self.members.get(self.section) or ())

    def order(self, a, b):
        if self.query:
            ka, kb = (-self.score(a.ident), self.alpha[a.ident]), (-self.score(b.ident), self.alpha[b.ident])
        elif self.section == "favorites":
            # Every app is sorted, shown or not; those not in favourites go last.
            rank = {f: n for n, f in enumerate(self.favorites)}
            ka, kb = rank.get(a.ident, 10 ** 6), rank.get(b.ident, 10 ** 6)
        else:
            ka, kb = self.alpha[a.ident], self.alpha[b.ident]
        return -1 if ka < kb else 1 if ka > kb else 0

    def score(self, ident):
        cache = self.scores
        if ident not in cache:
            e = self.apps.entries[ident]
            q = self.query
            name = e["name"].casefold()
            words = name.split()
            if name.startswith(q):
                s = 100
            elif any(w.startswith(q) for w in words):
                s = 80
            elif q in name:
                s = 60
            elif q in e.get("generic", "").casefold() or q in e.get("keywords", "").casefold():
                s = 40
            elif q in ident.casefold() or q in e.get("exec", "").casefold():
                s = 30
            elif q in e.get("comment", "").casefold():
                s = 10
            else:
                s = 0
            cache[ident] = s
        return cache[ident]

    def refresh(self):
        self.scores = {}
        self.grid.invalidate_filter()
        self.grid.invalidate_sort()
        for key, button in self.section_buttons.items():
            context = button.get_style_context()
            if key == self.section and not self.query:
                context.add_class("selected")
            else:
                context.remove_class("selected")
        places = self.section == "places" and not self.query
        for tab, on in ((self.tab_apps, not places), (self.tab_places, places)):
            if on:
                tab.get_style_context().add_class("selected")
            else:
                tab.get_style_context().remove_class("selected")
        if places:
            self.stack.set_visible_child_name("places")
            return
        count = sum(1 for c in self.children.values() if self.visible(c))
        if count:
            self.stack.set_visible_child_name("apps")
        else:
            self.empty.set_text("Nothing found. Press Enter to search with KRunner." if self.query else
                                "No favourites yet. Right-click an app to add it." if self.section == "favorites"
                                else "Nothing here.")
            self.stack.set_visible_child_name("empty")
        self.grid_scroll.get_vadjustment().set_value(0)

    def choose(self, section):
        self.section = section
        if self.query:
            self.search.set_text("")
        self.hide_context()
        self.refresh()

    def search_changed(self, entry):
        self.query = entry.get_text().strip().casefold()
        self.refresh()

    def first_visible(self):
        best = None
        for child in self.children.values():
            if self.visible(child):
                if best is None or self.order(child, best) < 0:
                    best = child
        return best

    def search_activate(self, entry):
        if not self.query:
            return
        first = self.first_visible()
        if first is not None:
            self.launch(first.ident)
        else:
            self.act("search " + entry.get_text().strip())

    def search_key(self, entry, event):
        if event.keyval == Gdk.KEY_Down:
            first = self.first_visible()
            if first is not None and self.stack.get_visible_child_name() == "apps":
                first.get_child().grab_focus()
                return True
        return False

    # ------------------------------------------------------------ actions

    def act(self, line):
        out(line)
        self.close()

    def launch(self, ident):
        self.act("launch " + ident)

    def app_press(self, widget, event, ident):
        if event.button != 3:
            return False
        self.hide_context()
        for child in self.context.get_children():
            self.context.remove(child)
        favorite = ident in self.favorites
        pinned = ident in self.pins
        items = [("Remove from favourites" if favorite else "Add to favourites", lambda: self.toggle_favorite(ident)),
                 ("Unpin from the panel" if pinned else "Pin to the panel",
                  lambda: (out(("unpin " if pinned else "pin ") + ident), self.toggle_pin(ident)))]
        for title, action in items:
            button = Gtk.Button(label=title)
            button.get_child().set_xalign(0)
            button.connect("clicked", lambda b, a=action: (a(), self.hide_context()))
            self.context.pack_start(button, False, False, 0)
        x, y = widget.translate_coordinates(self.overlay, int(event.x), int(event.y))
        self.context.set_margin_start(max(4, min(x, WIDTH - 220)))
        self.context.set_margin_top(max(4, min(y, HEIGHT - 90)))
        self.context.show_all()
        return True

    def toggle_favorite(self, ident):
        if ident in self.favorites:
            self.favorites.remove(ident)
        else:
            self.favorites.append(ident)
        save_favorites(self.favorites)
        self.refresh()

    def toggle_pin(self, ident):
        if ident in self.pins:
            self.pins.remove(ident)
        else:
            self.pins.append(ident)

    def hide_context(self):
        self.context.hide()

    def overlay_press(self, widget, event):
        # A click anywhere but on the small menu closes it.
        if self.context.get_visible():
            target = Gtk.get_event_widget(event)
            if target is None or (target is not self.context and not target.is_ancestor(self.context)):
                self.hide_context()
        return False

    # ------------------------------------------------------------ showing

    def command(self, spec):
        what = spec.get("cmd")
        sys.stderr.write("%s %s (shown: %s)\n" % (time.strftime("%H:%M:%S"), what, self.get_visible()))
        sys.stderr.flush()
        if what == "hide":
            self.close()
        elif what == "toggle":
            if self.get_visible():
                self.close()
            elif time.monotonic() - self.hidden_at < 0.4:
                # The click on the panel's button that took the keyboard
                # away and closed the menu: it was meant to close it.
                pass
            else:
                self.open(spec)
        elif what == "quit":
            Gtk.main_quit()

    def open(self, spec):
        self.pins = list(spec.get("pins") or [])
        self.provider.load_from_data(look_css(spec.get("look", "glass"), spec.get("palette") or {},
                                              spec.get("glass")).encode())
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geometry = monitor.get_geometry()
        left = int(spec.get("left") or 6)
        above = spec.get("above")
        bottom = int(geometry.height - above + 6) if above is not None else 70
        if self.layered:
            GtkLayerShell.set_margin(self, GtkLayerShell.Edge.LEFT, max(0, left))
            GtkLayerShell.set_margin(self, GtkLayerShell.Edge.BOTTOM, max(0, bottom))
        self.section, self.query = spec.get("section", "favorites"), ""
        self.search.set_text("")
        self.hide_context()
        self.refresh()
        self.had_focus, self.shown_at = False, time.monotonic()
        self.show_all()
        self.context.hide()
        self.present()
        self.search.grab_focus()

    def close(self):
        if self.get_visible():
            self.hide()
            self.hidden_at = time.monotonic()

    def focus_in(self, *unused):
        self.had_focus = True
        return False

    def focus_out(self, *unused):
        if self.layered and self.had_focus and time.monotonic() - self.shown_at > 0.15:
            self.close()
        return False

    def key(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            if self.context.get_visible():
                self.hide_context()
            elif self.query:
                self.search.set_text("")
            else:
                self.close()
            return True
        # Typing anywhere searches.
        char = Gdk.keyval_to_unicode(event.keyval)
        if char and chr(char).isprintable() and not self.search.has_focus() and \
                not event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD1_MASK):
            self.search.grab_focus_without_selecting()
            self.search.set_text(self.search.get_text() + chr(char))
            self.search.set_position(-1)
            return True
        return False


def main():
    if not GtkLayerShell.is_supported() and not os.environ.get("TRAY_LAUNCHER_PLAIN"):
        sys.stderr.write("this desktop has no layer shell, so no app menu\n")
        return 1
    launcher = Launcher()
    launcher.realize()

    def readable(fd, condition):
        if condition & (GLib.IO_HUP | GLib.IO_ERR):
            Gtk.main_quit()
            return False
        line = sys.stdin.readline()
        if not line:
            Gtk.main_quit()
            return False
        try:
            launcher.command(json.loads(line))
        except (ValueError, TypeError) as error:
            sys.stderr.write("bad command: %s\n" % error)
        return True

    GLib.io_add_watch(sys.stdin.fileno(), GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, readable)
    if "--show" in sys.argv:                          # to look at it on its own
        launcher.open({"look": sys.argv[sys.argv.index("--show") + 1] if len(sys.argv) > sys.argv.index("--show") + 1
                       else "glass"})
    out("ready")
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
