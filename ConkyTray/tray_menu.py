#!/usr/bin/env python3
"""A menu above ConkyTray's panel.

    tray_menu.py '<json>'

On Wayland a popup has to belong to a window of the same program, and the
panel's window is conky's, so the menu is a layer-shell surface of its own,
placed over the panel (GTK 3 and gtk-layer-shell). It takes the keyboard
when it opens, which KWin grants an on-demand layer surface, so a click
anywhere else takes the keyboard away and closes it; Escape closes it too.

The JSON says where (`x`, the centre in KWin's coordinates, and `above`,
the top of the panel), the colours, and either `items` -- the chosen item's
id is printed -- or `dbusmenu`, a tray icon's own menu, which is read from
and answered to the app directly.
"""

import json
import sys
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("GtkLayerShell", "0.1")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, GtkLayerShell  # noqa: E402

SHADOW = 14                     # room round the menu for its shadow


def dbusmenu_items(service, path):
    """A tray icon's menu (com.canonical.dbusmenu) as items, and the object
    to send the click to."""
    import dbus
    bus = dbus.SessionBus()
    menu = dbus.Interface(bus.get_object(service, path), "com.canonical.dbusmenu")
    try:
        menu.AboutToShow(dbus.Int32(0))
    except dbus.DBusException:
        pass
    _, layout = menu.GetLayout(dbus.Int32(0), dbus.Int32(-1), dbus.Array([], signature="s"))

    def convert(node):
        ident, props, children = node
        props = dict(props)
        if not bool(props.get("visible", True)):
            return None
        if str(props.get("type", "")) == "separator":
            return {"separator": True}
        label = str(props.get("label", "")).replace("__", "\0").replace("_", "").replace("\0", "_")
        item = {"id": int(ident), "label": label, "enabled": bool(props.get("enabled", True))}
        if str(props.get("toggle-type", "")) in ("checkmark", "radio"):
            item["checked"] = int(props.get("toggle-state", 0)) == 1
        if str(props.get("icon-name", "")):
            item["icon_name"] = str(props["icon-name"])
        elif props.get("icon-data"):
            item["icon_data"] = bytes(props["icon-data"])
        kids = [k for k in (convert(c) for c in children) if k]
        if kids or str(props.get("children-display", "")) == "submenu":
            item["children"] = kids
        return item

    root = convert(layout) or {}
    return root.get("children", []), menu


class Menu(Gtk.Window):
    def __init__(self, spec, items, on_choose):
        super().__init__()
        self.spec, self.on_choose = spec, on_choose
        self.had_focus, self.opened, self.done = False, time.monotonic(), False
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_app_paintable(True)
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_namespace(self, "conkytray-menu")
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.ON_DEMAND)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.BOTTOM, True)
        GtkLayerShell.set_anchor(self, GtkLayerShell.Edge.LEFT, True)
        # Placed from the screen's edge, not from the top of the panels' space.
        GtkLayerShell.set_exclusive_zone(self, -1)
        self.style(spec.get("colors") or {})
        self.frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.frame.get_style_context().add_class("menu")
        self.add(self.frame)
        self.show_page(items, None)
        self.connect("focus-in-event", self.focus_in)
        self.connect("focus-out-event", self.focus_out)
        self.connect("key-press-event", self.key)
        self.connect("draw", self.clear)

    @staticmethod
    def clear(widget, cr):
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(0)              # CLEAR: the window round the menu stays see-through
        cr.paint()
        cr.set_operator(2)
        return False

    def style(self, colors):
        bg = colors.get("bg") or "rgba(30,30,38,0.97)"
        fg = colors.get("fg") or "rgb(240,240,245)"
        edge = colors.get("edge") or "rgba(255,255,255,0.12)"
        hover = colors.get("hover") or "rgba(255,255,255,0.10)"
        muted = colors.get("muted") or "rgba(240,240,245,0.55)"
        accent = colors.get("accent") or fg
        radius = float(colors.get("radius") or 12)
        css = """
        window { background: transparent; }
        .menu { background-color: %(bg)s; color: %(fg)s; border: 1px solid %(edge)s;
                border-radius: %(r)dpx; padding: 6px; margin: %(sh)dpx;
                box-shadow: 0 4px %(sh)dpx rgba(0,0,0,0.35); }
        .menu button { background: none; border: none; box-shadow: none; outline: none;
                       padding: 5px 12px; min-height: 0; border-radius: %(r2)dpx; }
        .menu button:hover { background-color: %(hover)s; }
        .menu button label { color: %(fg)s; }
        .menu button:disabled label { color: %(muted)s; }
        .menu .mark { color: %(accent)s; min-width: 16px; }
        .menu .header { color: %(muted)s; font-weight: bold; padding: 4px 12px 4px 12px; }
        .menu separator { background-color: %(edge)s; min-height: 1px; margin: 4px 6px; }
        """ % {"bg": bg, "fg": fg, "edge": edge, "hover": hover, "muted": muted, "accent": accent,
               "r": radius, "r2": max(0, radius - 5), "sh": SHADOW}
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode())
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_USER + 10)

    def show_page(self, items, back):
        for child in self.frame.get_children():
            self.frame.remove(child)
        if back is not None:
            self.frame.pack_start(self.row({"label": "‹  " + back[1]}, lambda: self.show_page(*back[0])),
                                  False, False, 0)
            self.frame.pack_start(Gtk.Separator(), False, False, 0)
        if not items:
            label = Gtk.Label(label="Nothing here", xalign=0)
            label.get_style_context().add_class("header")
            self.frame.pack_start(label, False, False, 0)
        for item in items:
            if item.get("separator"):
                self.frame.pack_start(Gtk.Separator(), False, False, 0)
            elif item.get("header"):
                label = Gtk.Label(label=item.get("label", ""), xalign=0)
                label.set_max_width_chars(48)
                label.set_ellipsize(3)
                label.get_style_context().add_class("header")
                self.frame.pack_start(label, False, False, 0)
            elif "children" in item:
                page = (items, back)
                self.frame.pack_start(self.row(item, lambda i=item, p=page: self.show_page(
                    i["children"], (p, i.get("label", "")))), False, False, 0)
            else:
                self.frame.pack_start(self.row(item, lambda i=item: self.choose(i)), False, False, 0)
        self.frame.show_all()
        self.place()

    def row(self, item, action):
        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        box = Gtk.Box(spacing=8)
        mark = Gtk.Label(label="✓" if item.get("checked") else "")
        mark.get_style_context().add_class("mark")
        box.pack_start(mark, False, False, 0)
        image = None
        if item.get("icon_name"):
            image = Gtk.Image.new_from_icon_name(item["icon_name"], Gtk.IconSize.MENU)
        elif item.get("icon_data"):
            try:
                loader = GdkPixbuf.PixbufLoader()
                loader.write(item["icon_data"])
                loader.close()
                pixbuf = loader.get_pixbuf().scale_simple(16, 16, GdkPixbuf.InterpType.BILINEAR)
                image = Gtk.Image.new_from_pixbuf(pixbuf)
            except GLib.Error:
                image = None
        if image:
            box.pack_start(image, False, False, 0)
        label = Gtk.Label(label=item.get("label", ""), xalign=0)
        label.set_max_width_chars(60)
        label.set_ellipsize(3)
        box.pack_start(label, True, True, 0)
        if "children" in item:
            box.pack_end(Gtk.Label(label="›"), False, False, 0)
        button.add(box)
        button.set_sensitive(item.get("enabled", True))
        button.connect("clicked", lambda b: action())
        return button

    def place(self):
        """Centre the menu over the point it was opened from, kept on the
        screen, its bottom just above the panel."""
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geometry = monitor.get_geometry()
        width = self.get_preferred_width()[1]
        x = float(self.spec.get("x") or geometry.width / 2)
        left = int(min(max(x - width / 2, 4 - SHADOW), geometry.width - width + SHADOW - 4))
        above = self.spec.get("above")
        bottom = int(geometry.height - above + 6 - SHADOW) if above is not None else 64
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.LEFT, max(0, left))
        GtkLayerShell.set_margin(self, GtkLayerShell.Edge.BOTTOM, max(0, bottom))
        self.resize(1, 1)

    def choose(self, item):
        if self.done:
            return
        self.done = True
        self.hide()
        self.on_choose(item)
        Gtk.main_quit()

    def finish(self):
        if not self.done:
            self.done = True
            Gtk.main_quit()

    def focus_in(self, *unused):
        self.had_focus = True
        return False

    def focus_out(self, *unused):
        # Someone clicked somewhere else. (A focus-out right as the menu
        # maps, before it ever had the keyboard, is not that.)
        if self.had_focus and time.monotonic() - self.opened > 0.15:
            self.finish()
        return False

    def key(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self.finish()
            return True
        return False


def main():
    spec = json.loads(sys.argv[1])
    if not GtkLayerShell.is_supported():
        sys.stderr.write("this desktop has no layer shell, so no menus\n")
        return 1
    if spec.get("dbusmenu"):
        import dbus
        try:
            items, menu = dbusmenu_items(spec["dbusmenu"]["service"], spec["dbusmenu"]["path"])
        except dbus.DBusException as error:
            sys.stderr.write("no menu: %s\n" % error)
            return 1

        def on_choose(item):
            try:
                menu.Event(dbus.Int32(item["id"]), "clicked", dbus.Int32(0, variant_level=1),
                           dbus.UInt32(int(time.time()) & 0xFFFFFFFF))
            except dbus.DBusException as error:
                sys.stderr.write("the app did not take the click: %s\n" % error)
    else:
        items = spec.get("items") or []

        def on_choose(item):
            print(item.get("id", ""), flush=True)

    window = Menu(spec, items, on_choose)
    window.show_all()
    # Nothing to wait for if it never got the keyboard: give up after a while
    # rather than linger over the panel.
    GLib.timeout_add_seconds(60, lambda: (window.finish(), False)[1])
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
