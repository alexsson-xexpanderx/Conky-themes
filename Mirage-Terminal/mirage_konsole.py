#!/usr/bin/env python3
"""Konsole in the look of Conky-Mirage and ConkyTray.

    ./mirage_konsole.py install     a "Mirage" profile, made Konsole's default
    ./mirage_konsole.py apply       its colours again, for the look and wallpaper now
    ./mirage_konsole.py uninstall   your old default profile back

The profile keeps your font and the rest of your old default profile; only the
colours are Mirage's. ConkyTray's helper runs `apply` whenever the look, the
wallpaper or the panel's colours change, and every open Konsole tab on the
Mirage profile changes with them.
"""

import os
import re
import shutil
import sys

import terminal_palette as T

DATA = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
CONFIG = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
KONSOLE = os.path.join(DATA, "konsole")
KONSOLERC = os.path.join(CONFIG, "konsolerc")
PROFILE = "Mirage"
SCHEME = "Mirage"
# The same colours under a second name. A tab already showing "Mirage" does
# not read the file again when told "Mirage", but told another name it reads
# that one (tried on Konsole 26.08). Konsole applies the change a moment
# later, so "the other, then back" in one go came to nothing: each update
# moves a tab to the name it is not on, both files holding the new colours.
FLIP = "Mirage-Switch"
TABS = os.path.join(T.CACHE, "mirage-terminal", "konsole-tabs")


def rgb(c):
    return "%d,%d,%d" % tuple(max(0, min(255, round(v * 255))) for v in c)


def scheme_text(s, description):
    lines = []

    def section(name, colour):
        lines.extend(["[%s]" % name, "Color=" + rgb(colour), ""])

    section("Background", s["bg"])
    section("BackgroundFaint", s["bg"])
    section("BackgroundIntense", s["bg_intense"])
    for i in range(8):
        section("Color%d" % i, s["normal"][i])
        section("Color%dFaint" % i, s["faint"][i])
        section("Color%dIntense" % i, s["bright"][i])
    section("Foreground", s["fg"])
    section("ForegroundFaint", T.toward(s["fg"], s["bg"], 0.45))
    section("ForegroundIntense", s["fg"])
    lines += ["[General]", "Blur=%s" % ("true" if s["blur"] else "false"), "ColorRandomization=false",
              "Description=" + description, "Opacity=%.2f" % s["opacity"], "Wallpaper=", ""]
    return "\n".join(lines)


def read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(tmp, path)


def default_profile():
    m = re.search(r"^DefaultProfile=(.*)$", read(KONSOLERC) or "", re.M)
    return m.group(1).strip() if m else None


def set_default_profile(name):
    text = read(KONSOLERC) or ""
    if re.search(r"^DefaultProfile=", text, re.M):
        text = re.sub(r"^DefaultProfile=.*$", "DefaultProfile=" + name, text, count=1, flags=re.M)
    elif "[Desktop Entry]" in text:
        text = text.replace("[Desktop Entry]", "[Desktop Entry]\nDefaultProfile=" + name, 1)
    else:
        text = "[Desktop Entry]\nDefaultProfile=%s\n\n" % name + text
    write(KONSOLERC, text)


def margin():
    try:
        return max(0, min(64, int(T.margin)))
    except (TypeError, ValueError):
        return 12


def sync_margin():
    """The profile's room round the text, as the setting has it. Konsole
    reads it when a window opens."""
    path = os.path.join(KONSOLE, PROFILE + ".profile")
    text = read(path)
    if text is None:
        return False
    want = "TerminalMargin=%d" % margin()
    if re.search(r"^%s$" % want, text, re.M):
        return False
    if re.search(r"^TerminalMargin=", text, re.M):
        text = re.sub(r"^TerminalMargin=.*$", want, text, count=1, flags=re.M)
    elif "[General]" in text:
        text = text.replace("[General]", "[General]\n" + want, 1)
    else:
        text += "\n[General]\n%s\n" % want
    write(path, text)
    return True


def profile_text(base):
    """Your old default profile with Mirage's colours: its font, cursor
    shape, scrolling and so on are kept. The cursor takes the text colour."""
    sections, current = {}, None
    for line in (base or "").splitlines():
        m = re.match(r"^\[(.+)\]$", line.strip())
        if m:
            current = sections.setdefault(m.group(1), {})
        elif current is not None and "=" in line:
            k, v = line.split("=", 1)
            current[k.strip()] = v.strip()
    sections.setdefault("Appearance", {})["ColorScheme"] = SCHEME
    general = sections.setdefault("General", {})
    general["Name"] = PROFILE
    general.setdefault("Parent", "FALLBACK/")
    general["TerminalMargin"] = str(margin())
    cursor = sections.setdefault("Cursor Options", {})
    cursor["UseCustomCursorColor"] = "false"
    out = []
    for name in sorted(sections):
        out.append("[%s]" % name)
        out += ["%s=%s" % kv for kv in sorted(sections[name].items())]
        out.append("")
    return "\n".join(out)


def sessions():
    """Konsole's open tabs: (an id for the tab, its profile's name, its
    terminal device). The id, Konsole's process and the tab's number, is never
    used twice while that Konsole runs."""
    try:
        import dbus
        bus = dbus.SessionBus()
        names = [str(n) for n in bus.list_names() if str(n).startswith("org.kde.konsole")]
    except Exception:
        return []
    found = []
    for name in names:
        try:
            xml = bus.get_object(name, "/Sessions").Introspect(dbus_interface="org.freedesktop.DBus.Introspectable")
        except Exception:
            continue
        for number in re.findall(r'<node name="(\d+)"', str(xml)):
            try:
                session = bus.get_object(name, "/Sessions/" + number)
                profile = str(session.profile(dbus_interface="org.kde.konsole.Session"))
                pid = int(session.processId(dbus_interface="org.kde.konsole.Session"))
                tty = os.readlink("/proc/%d/fd/0" % pid)
            except Exception:
                continue
            if tty.startswith("/dev/pts/"):
                found.append(("%s/%s" % (name, number), profile, tty))
    return found


def recolour_open_tabs():
    """Every open tab on the Mirage profile reads the colours again. The
    change is sent to the tab's terminal (Konsole's own way, as its
    konsoleprofile command does); Konsole keeps D-Bus profile changes for
    when its "security-sensitive D-Bus" setting is on, which it is not by default."""
    # A tab whose colours were changed this way has a nameless copy of its
    # profile from then on (Konsole reports ""), so the tabs done are kept,
    # each with the name it is on now.
    remembered = dict(line.split(" ", 1) for line in (read(TABS) or "").splitlines() if " " in line)
    done = {}
    for ident, profile, tty in sessions():
        if profile != PROFILE and not (profile == "" and ident in remembered):
            continue
        target = FLIP if remembered.get(ident, SCHEME) == SCHEME else SCHEME
        try:
            with open(tty, "w") as handle:
                handle.write("\033]50;ColorScheme=%s\007" % target)
            done[ident] = target
        except OSError:
            pass
    os.makedirs(os.path.dirname(TABS), exist_ok=True)
    write(TABS, "".join("%s %s\n" % kv for kv in sorted(done.items())))
    return len(done)


def apply(quiet=False):
    """The colours for the look and wallpaper now; nothing at all if they are
    what Konsole has, or if the Mirage profile is not installed."""
    if not os.path.exists(os.path.join(KONSOLE, PROFILE + ".profile")):
        if not quiet:
            print("The Mirage profile isn't installed. Run: %s install" % sys.argv[0])
        return False
    margin_changed = sync_margin()
    s = T.scheme()
    text = scheme_text(s, "Mirage (%s)" % T.LOOK_NAMES[s["look"]])
    path = os.path.join(KONSOLE, SCHEME + ".colorscheme")
    if read(path) == text:
        return margin_changed
    write(os.path.join(KONSOLE, FLIP + ".colorscheme"), text.replace("Description=Mirage (", "Description=Mirage, switching ("))
    write(path, text)
    tabs = recolour_open_tabs()
    if not quiet:
        print("Konsole is %s now (%d open tab%s changed)." % (T.LOOK_NAMES[s["look"]], tabs, "" if tabs == 1 else "s"))
    return True


def install():
    os.makedirs(KONSOLE, exist_ok=True)
    old = default_profile()
    if old != PROFILE + ".profile":
        if os.path.exists(KONSOLERC):
            shutil.copy2(KONSOLERC, KONSOLERC + ".before-mirage")
        state = os.path.join(KONSOLE, ".mirage-previous-default")
        write(state, (old or "") + "\n")
    base = read(os.path.join(KONSOLE, old)) if old and old != PROFILE + ".profile" else None
    if base is None:
        base = read(os.path.join(KONSOLE, PROFILE + ".profile"))
    write(os.path.join(KONSOLE, PROFILE + ".profile"), profile_text(base))
    set_default_profile(PROFILE + ".profile")
    try:
        os.remove(os.path.join(KONSOLE, SCHEME + ".colorscheme"))      # so apply writes it fresh
    except OSError:
        pass
    apply(quiet=True)
    print("Konsole's default profile is Mirage now (it was %s). New Konsole windows wear the look;"
          " windows already open keep theirs until you open new ones." % (old or "Konsole's own"))


def uninstall():
    state = read(os.path.join(KONSOLE, ".mirage-previous-default"))
    old = (state or "").strip()
    if default_profile() == PROFILE + ".profile":
        if old:
            set_default_profile(old)
        else:
            text = re.sub(r"^DefaultProfile=.*\n?", "", read(KONSOLERC) or "", flags=re.M)
            write(KONSOLERC, text)
    for name in (PROFILE + ".profile", SCHEME + ".colorscheme", FLIP + ".colorscheme", ".mirage-previous-default"):
        try:
            os.remove(os.path.join(KONSOLE, name))
        except OSError:
            pass
    print("The Mirage profile is gone; Konsole's default is %s again." % (old or "its own"))


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "apply"
    if command == "install":
        install()
    elif command == "apply":
        quiet = "--quiet" in sys.argv
        if not apply(quiet) and not quiet and os.path.exists(os.path.join(KONSOLE, PROFILE + ".profile")):
            print("Konsole already has the colours for the look now.")
    elif command == "uninstall":
        uninstall()
    else:
        print(__doc__.strip())
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
