# ConkyTray

A panel for the bottom of your screen, drawn by conky in the same four looks
as [Conky-Mirage](../Conky-Mirage): glass, solid, Metro and Pixel. It follows
whatever look Mirage is set to, and takes its colours from your wallpaper.

From left to right it has a button that opens KRunner, your pinned and open
apps, the tray icons, volume, network, battery (on a laptop) and the clock.
Windows keep clear of it, the way they do for Plasma's own panel.

It is new and still being worked on.

## What you need

- KDE Plasma on Wayland. The panel uses a Wayland feature (the layer shell)
  that Plasma has and X11 does not.
- conky 1.25 or newer, built with Wayland and Lua support.
- Python 3 with dbus-python, PyGObject and Pillow, GTK 3 with gtk-layer-shell
  (for the menus), and `rsvg-convert` (for the icons).
- Conky-Mirage in the folder next to this one, for the looks. Without it the
  panel still works, in plain dark colours.

## Start it

```bash
conky -c /path/to/ConkyTray/start_conky_tray
```

Your Plasma panel can stay while you try it: ConkyTray sits just above it.

## Using it

- **Apps:** click to open, to bring a window to the front, or to minimise it.
  With several windows, each click goes to the next. Middle-click opens a new
  window. Right-click to pin or unpin the app, or close its windows.
- **The button at the left:** click to find and start apps (KRunner).
  Right-click to lock the screen, log out, restart or shut down.
- **Tray icons:** click them as you would in Plasma's tray. Right-click for
  their menu.
- **Volume:** scroll over it to turn it up or down, click to mute.
- **Network:** click for the network settings.

The pinned apps are kept in `~/.config/conky-tray/pinned`, one per line. The
first time, they are copied from your Plasma panel.

## Settings

```bash
./tray_settings.py
```

The settings window looks like Mirage's. Pick the look (or "Like Mirage", to
always match it), the clock, the date and how it is written, which of
volume, network and battery to show, and how tall the panel is. The preview
shows both ends of the panel at real size. Save, and the panel changes right
away.

![Settings window](screenshot_settings.png)

The same settings are at the top of `tray.lua`, and the height is in
`start_conky_tray`, if you would rather edit them by hand.

## Replacing Plasma's panel

Once you are happy with it, start it with your desktop: add
`conky -c /path/to/ConkyTray/start_conky_tray` in System Settings under
Autostart. Then remove Plasma's panel (right-click it, "Show Panel
Configuration", and delete it there).

Plasma's tray has a few things ConkyTray does not have yet: Bluetooth, the
clipboard and the list of past notifications. Notifications themselves still
pop up as before.
