#!/usr/bin/env python3
"""Notification pop-ups for ConkyTray.

    tray_notify.py

Kept running by tray.py, which is the desktop's notification service
(org.freedesktop.Notifications) and tells this program what to show, one
JSON object a line on stdin:

    {"cmd": "show", "id": 3, "app": "...", "summary": "...", "body": "...",
     "icon": "/path.png" or null, "actions": [["key", "Label"], ...],
     "timeout": ms (0 stays until dismissed), "side": "bottom", "colors": {...}}
    {"cmd": "close", "id": 3}

and hears back the same way on stdout:

    {"ev": "action", "id": 3, "key": "default"}   a button, or the pop-up itself
    {"ev": "clicked", "id": 3}                     the pop-up, which has no default action
    {"ev": "closed", "id": 3, "reason": 1}         1 timed out, 2 dismissed

Each pop-up is a layer-shell surface of its own (GTK 3 and gtk-layer-shell),
in the corner at the panel's tray end. It never takes the keyboard. Newer
ones sit nearest the panel and push older ones away from it.
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

SHADOW = 14                     # room round each pop-up for its shadow
GAP = 10                        # from the panel, and between pop-ups (before the shadows)
WIDTH = 380
ICON = 40
MOST = 4                        # pop-ups on screen at once; older ones go to the bell only
STYLED = set()


def say(**event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def style(colors):
    key = json.dumps(colors, sort_keys=True)
    if key in STYLED:
        return
    STYLED.add(key)
    bg = colors.get("bg") or "rgba(30,30,38,0.97)"
    fg = colors.get("fg") or "rgb(240,240,245)"
    edge = colors.get("edge") or "rgba(255,255,255,0.12)"
    hover = colors.get("hover") or "rgba(255,255,255,0.10)"
    muted = colors.get("muted") or "rgba(240,240,245,0.55)"
    accent = colors.get("accent") or fg
    radius = float(colors.get("radius") or 12)
    css = """
    window { background: transparent; }
    .note { background-color: %(bg)s; color: %(fg)s; border: 1px solid %(edge)s;
            border-radius: %(r)dpx; padding: 12px 12px 12px 14px; margin: %(sh)dpx;
            box-shadow: 0 4px %(sh)dpx rgba(0,0,0,0.35); }
    .note.critical { border-color: %(accent)s; }
    .note .app { color: %(muted)s; font-size: 90%%; }
    .note .summary { color: %(fg)s; font-weight: bold; }
    .note .body { color: %(fg)s; }
    .note button { background: none; border: 1px solid %(edge)s; box-shadow: none; outline: none;
                   padding: 3px 12px; min-height: 0; border-radius: %(r2)dpx; color: %(fg)s; }
    .note button:hover { background-color: %(hover)s; }
    .note button label { color: %(fg)s; }
    .note button.close { border: none; padding: 0 6px; }
    .note button.close label { color: %(muted)s; }
    """ % {"bg": bg, "fg": fg, "edge": edge, "hover": hover, "muted": muted, "accent": accent,
           "r": radius, "r2": max(0, radius - 5), "sh": SHADOW}
    provider = Gtk.CssProvider()
    provider.load_from_data(css.encode())
    Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                             Gtk.STYLE_PROVIDER_PRIORITY_USER + 10)


class Popup(Gtk.Window):
    def __init__(self, stack, spec):
        super().__init__()
        self.stack, self.spec, self.ident = stack, spec, spec["id"]
        self.timer, self.closing = None, False
        visual = self.get_screen().get_rgba_visual()
        if visual:
            self.set_visual(visual)
        self.set_app_paintable(True)
        self.connect("draw", self.clear)
        GtkLayerShell.init_for_window(self)
        GtkLayerShell.set_layer(self, GtkLayerShell.Layer.OVERLAY)
        GtkLayerShell.set_namespace(self, "conkytray-notification")
        GtkLayerShell.set_keyboard_mode(self, GtkLayerShell.KeyboardMode.NONE)
        # The corner at the panel's tray end. Margins count from the edge of
        # the room the panel leaves, so these sit just off it.
        side = spec.get("side", "bottom")
        self.vertical_edge = GtkLayerShell.Edge.TOP if side == "top" else GtkLayerShell.Edge.BOTTOM
        across = GtkLayerShell.Edge.LEFT if side == "left" else GtkLayerShell.Edge.RIGHT
        GtkLayerShell.set_anchor(self, self.vertical_edge, True)
        GtkLayerShell.set_anchor(self, across, True)
        GtkLayerShell.set_margin(self, across, max(0, GAP - SHADOW + 4))
        style(spec.get("colors") or {})
        self.build()
        self.connect("size-allocate", lambda *a: self.stack.arrange())

    @staticmethod
    def clear(widget, cr):
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(0)
        cr.paint()
        cr.set_operator(2)
        return False

    def build(self):
        spec = self.spec
        events = Gtk.EventBox()
        events.set_visible_window(False)
        events.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK |
                          Gdk.EventMask.BUTTON_RELEASE_MASK)
        events.connect("enter-notify-event", self.pointer_in)
        events.connect("leave-notify-event", self.pointer_out)
        events.connect("button-release-event", self.pressed)
        self.add(events)
        frame = Gtk.Box(spacing=12)
        frame.get_style_context().add_class("note")
        if spec.get("urgency") == 2:
            frame.get_style_context().add_class("critical")
        frame.set_size_request(WIDTH, -1)
        events.add(frame)
        image = self.icon(spec.get("icon"))
        if image:
            image.set_valign(Gtk.Align.START)
            frame.pack_start(image, False, False, 0)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        frame.pack_start(column, True, True, 0)
        head = Gtk.Box(spacing=6)
        column.pack_start(head, False, False, 0)
        app = Gtk.Label(label="%s · %s" % (spec.get("app") or "Notification", time.strftime("%H:%M")), xalign=0)
        app.set_ellipsize(3)
        app.get_style_context().add_class("app")
        head.pack_start(app, True, True, 0)
        close = Gtk.Button(label="×")
        close.get_style_context().add_class("close")
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.connect("clicked", lambda b: self.dismiss(2))
        head.pack_end(close, False, False, 0)
        for text, cls, lines in ((spec.get("summary"), "summary", 2), (spec.get("body"), "body", 5)):
            if text:
                label = Gtk.Label(label=text, xalign=0)
                label.set_line_wrap(True)
                label.set_line_wrap_mode(2)          # words, then characters
                label.set_lines(lines)
                label.set_ellipsize(3)
                label.set_max_width_chars(40)
                label.get_style_context().add_class(cls)
                column.pack_start(label, False, False, 0)
        buttons = [(k, l) for k, l in spec.get("actions") or [] if k != "default" and l]
        if buttons:
            row = Gtk.Box(spacing=6)
            row.set_margin_top(6)
            for key, label in buttons[:3]:
                button = Gtk.Button(label=label)
                button.connect("clicked", lambda b, k=key: self.act(k))
                row.pack_start(button, False, False, 0)
            column.pack_start(row, False, False, 0)

    @staticmethod
    def icon(path):
        if not path:
            return None
        try:
            # Twice the size, shown at half: sharp on a scaled screen.
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(path, ICON * 2, ICON * 2)
        except GLib.Error:
            return None
        surface = Gdk.cairo_surface_create_from_pixbuf(pixbuf, 2, None)
        return Gtk.Image.new_from_surface(surface)

    def start_timer(self, ms=None):
        self.stop_timer()
        ms = self.spec.get("timeout", 6000) if ms is None else ms
        if ms > 0:
            self.timer = GLib.timeout_add(ms, lambda: (self.dismiss(1), False)[1])

    def stop_timer(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None

    def pointer_in(self, widget, event):
        if event.detail != Gdk.NotifyType.INFERIOR:
            self.stop_timer()
        return False

    def pointer_out(self, widget, event):
        if event.detail != Gdk.NotifyType.INFERIOR and self.spec.get("timeout", 6000) > 0:
            self.start_timer(max(2500, min(self.spec.get("timeout", 6000), 4000)))
        return False

    def pressed(self, widget, event):
        if event.button == 1:
            if any(k == "default" for k, _ in self.spec.get("actions") or []):
                self.act("default")
            else:
                say(ev="clicked", id=self.ident)
                self.dismiss(2)
        elif event.button in (2, 3):
            self.dismiss(2)
        return True

    def act(self, key):
        say(ev="action", id=self.ident, key=key)
        self.dismiss(2)

    def dismiss(self, reason, quiet=False):
        if self.closing:
            return
        self.closing = True
        self.stop_timer()
        if not quiet:
            say(ev="closed", id=self.ident, reason=reason)
        self.stack.remove(self)
        self.destroy()


class Stack:
    def __init__(self):
        self.popups = []              # newest first

    def show(self, spec):
        old = self.find(spec["id"])
        if old:                       # replaces it in place, as the spec asks
            index = self.popups.index(old)
            old.closing = True
            old.stop_timer()
            old.destroy()
            self.popups.pop(index)
        else:
            index = 0
        popup = Popup(self, spec)
        self.popups.insert(index, popup)
        for extra in self.popups[MOST:]:
            extra.dismiss(0, quiet=True)          # still behind the bell
        popup.show_all()
        popup.start_timer()
        self.arrange()

    def find(self, ident):
        for p in self.popups:
            if p.ident == ident:
                return p
        return None

    def remove(self, popup):
        if popup in self.popups:
            self.popups.remove(popup)
            self.arrange()

    def arrange(self):
        offset = GAP - SHADOW
        for p in self.popups:
            GtkLayerShell.set_margin(p, p.vertical_edge, max(0, offset))
            height = p.get_allocated_height() if p.get_mapped() else p.get_preferred_height()[1]
            offset += height - 2 * SHADOW + GAP


def main():
    if not GtkLayerShell.is_supported():
        sys.stderr.write("this desktop has no layer shell, so no notification pop-ups\n")
        return 1
    stack = Stack()

    def readable(source, condition):
        if condition & (GLib.IO_HUP | GLib.IO_ERR):
            Gtk.main_quit()
            return False
        line = sys.stdin.readline()
        if not line:
            Gtk.main_quit()
            return False
        try:
            spec = json.loads(line)
        except ValueError:
            return True
        if spec.get("cmd") == "show":
            stack.show(spec)
        elif spec.get("cmd") == "close":
            popup = stack.find(spec.get("id"))
            if popup:
                popup.dismiss(3, quiet=True)
        return True

    GLib.io_add_watch(sys.stdin.fileno(), GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, readable)
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
