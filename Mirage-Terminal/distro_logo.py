"""Your system's logo in each look, for mirafetch.

Slackware's is drawn after its own: a big S with a line turning up under
it, on a disc. Glass makes the disc frosted glass, Solid a flat raised pane,
Metro a square tile and Pixel a soft tonal disc. Other
systems get the first letter of their name the same way, until someone draws
theirs.
"""

import os
import re

from PIL import Image, ImageDraw, ImageFilter, ImageFont

import terminal_palette as T

M = T.M
FONTS = {True: ("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf", "DejaVu Sans:bold"),
         False: ("/usr/share/fonts/TTF/DejaVuSans.ttf", "DejaVu Sans")}
SUPER = 3                       # drawn this many times larger, then scaled down
SLACKWARE_BLUE = (0.36, 0.40, 0.69)


def os_release():
    found = {}
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    m = re.match(r'^(\w+)="?([^"\n]*)"?$', line.strip())
                    if m:
                        found[m.group(1)] = m.group(2)
            break
        except OSError:
            continue
    return found


def distro():
    """("slackware", "Slackware") for Slackware, else ("letter", the name)."""
    info = os_release()
    ids = (info.get("ID", "") + " " + info.get("ID_LIKE", "")).lower().split()
    name = info.get("NAME") or "Linux"
    return ("slackware" if "slackware" in ids else "letter"), name


def font(size, bold=True):
    path, name = FONTS[bold]
    if not os.path.exists(path):
        import subprocess
        try:
            path = subprocess.run(["fc-match", "-f", "%{file}", name], capture_output=True,
                                  text=True, timeout=2).stdout.strip() or path
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default(size)


def rgb8(c, alpha=255):
    return tuple(max(0, min(255, round(v * 255))) for v in c) + (alpha,)


def glyph(mask, letter, x0, y0, height, centre_x=None):
    """`letter` drawn white into `mask` with its ink `height` tall and its
    top at y0, its left at x0 (or centred on centre_x)."""
    probe = font(400)
    box = probe.getbbox(letter)
    size = max(8, round(400 * height / max(1, box[3] - box[1])))
    face = font(size)
    box = face.getbbox(letter)
    if centre_x is not None:
        x0 = centre_x - (box[2] - box[0]) / 2
    ImageDraw.Draw(mask).text((x0 - box[0], y0 - box[1]), letter, font=face, fill=255)


def mark(size, kind, letter, area, rounded_ends):
    """The logo's mark, white on black, inside `area` (x0, y0, x1, y1, as
    parts of `size`)."""
    mask = Image.new("L", (size, size), 0)
    ax0, ay0, ax1, ay1 = (v * size for v in area)
    w, h = ax1 - ax0, ay1 - ay0
    if kind != "slackware":
        glyph(mask, letter, 0, ay0 + h * 0.12, h * 0.76, centre_x=ax0 + w / 2)
        return mask
    # Slackware's: the S high and to the right, and under it a line that
    # turns up at its left end. In the mark's own box (0-1 both ways).
    glyph(mask, "S", 0, ay0, h * 0.84, centre_x=ax0 + w * 0.54)
    t = h * 0.046                                   # the line's weight
    left, right, bottom = ax0 + w * 0.0, ax0 + w * 1.0, ay0 + h
    up = ay0 + h * 0.70
    draw = ImageDraw.Draw(mask)
    if rounded_ends:
        draw.rounded_rectangle((left, up, left + t, bottom), radius=t / 2, fill=255)
        draw.rounded_rectangle((left, bottom - t, right, bottom), radius=t / 2, fill=255)
    else:
        draw.rectangle((left, up, left + t, bottom), fill=255)
        draw.rectangle((left, bottom - t, right, bottom), fill=255)
    return mask


def disc(size, inset=0.0):
    shape = Image.new("L", (size, size), 0)
    d = size * inset
    ImageDraw.Draw(shape).ellipse((d, d, size - 1 - d, size - 1 - d), fill=255)
    return shape


def readable_on(colour, ground, fallback, target=3.0):
    return colour if M.contrast(colour, ground) >= target else fallback


def nearest(P, names, to):
    """Of the palette's colours `names`, the one nearest in hue to `to`."""
    h = M.oklch(to)[2]

    def gap(name):
        return abs((M.oklch(P[name])[2] - h + 180) % 360 - 180)
    return min((n for n in names if n in P), key=gap)


def logo(look, s, width, height, wallpaper=None):
    """The logo for `look` (with `s`, the terminal's colours from
    terminal_palette.scheme), `width` x `height` pixels, see-through round
    it. It is square and centred whatever the box."""
    P = s["palette"]
    side = min(width, height)
    S = side * (SUPER if side < 400 else 2)          # big ones need less to look smooth
    kind, name = distro()
    letter = (name.strip()[:1] or "L").upper()
    dark = M.luminance(s["bg"]) < 0.2
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    if look == "metro":
        # A flat square tile in the Metro colour nearest the logo's own blue,
        # the mark white, the name at its foot as Mirage's tiles have.
        tile = P[nearest(P, ("metro1", "metro2", "metro3", "metro4"), SLACKWARE_BLUE)]
        out.paste(rgb8(tile), (0, 0, S, S))
        art = mark(S, kind, letter, (0.17, 0.08, 0.83, 0.74), False)
        out.paste((255, 255, 255, 255), (0, 0), art)
        if side >= 120:                                 # in blocks it would only be noise
            caption = font(round(S * 0.072), bold=False)
            faint = rgb8(T.toward((1, 1, 1), tile, 0.30))
            ImageDraw.Draw(out).text((S * 0.08, S * 0.93), name, font=caption, fill=faint, anchor="ls")

    elif look == "pixel":
        # A round tonal container, the mark in the primary colour on it,
        # its ends rounded. (Android's wavy "cookie" was tried; the user
        # wanted the plain circle.)
        ground = P.get("px_primary_c", s["bg"])
        ink = readable_on(P.get("px_primary", s["fg"]), ground, P.get("px_on", s["fg"]))
        out.paste(rgb8(ground), (0, 0), disc(S))
        art = mark(S, kind, letter, (0.22, 0.15, 0.78, 0.78), True)
        out.paste(rgb8(ink), (0, 0), art)

    elif look == "solid":
        # A flat raised disc a step off the background, with a soft shadow
        # as Mirage's solid boxes have, the mark in the accent colour.
        L, C, h = M.oklch(s["bg"])
        ground = T.lch(max(0, min(1, L + (0.07 if dark else -0.06))), C, h)
        inset = 0.035
        shadow = disc(S, inset).filter(ImageFilter.GaussianBlur(S * 0.022))
        shadow = shadow.transform(shadow.size, Image.AFFINE, (1, 0, 0, 0, 1, -S * 0.022))
        out.paste((0, 0, 0, 255), (0, 0), shadow.point(lambda v: v * (0.55 if dark else 0.28)))
        out.paste(rgb8(ground), (0, 0), disc(S, inset))
        ink = readable_on(s["accent"], ground, s["fg"], 3.2)
        art = mark(S, kind, letter, (0.22, 0.15, 0.78, 0.78), False)
        out.paste(rgb8(ink), (0, 0), art)

    else:
        # Frosted glass: the wallpaper blurred and tinted as Mirage's panes
        # are, light catching its upper half, a fine rim, and the mark in
        # the text colour lit from behind by the accent.
        inset = 0.02
        ground = rgb8(T.toward(P.get("tint", s["bg"]), s["fg"], 0.10))
        pane = Image.new("RGBA", (S, S), ground)
        if wallpaper:
            try:
                with Image.open(wallpaper) as picture:
                    w, h = picture.size
                    crop = min(w, h) * 0.6
                    piece = picture.convert("RGB").crop(((w - crop) / 2, (h - crop) / 2,
                                                         (w + crop) / 2, (h + crop) / 2)).resize((S // 4, S // 4))
                piece = piece.filter(ImageFilter.GaussianBlur(S / 4 * 0.10)).resize((S, S), Image.BILINEAR)
                pane = Image.blend(piece.convert("RGBA"), pane, 0.58)
            except (OSError, ValueError):
                pass
        shape = disc(S, inset)
        out.paste(pane, (0, 0), shape)
        sheen = Image.new("L", (S, S), 0)
        ImageDraw.Draw(sheen).ellipse((S * 0.10, S * 0.045, S * 0.90, S * 0.56), fill=255)
        fade = Image.linear_gradient("L").resize((S, S)).point(lambda v: max(0, 64 - v * 0.30) if dark else
                                                              max(0, 90 - v * 0.42))
        sheen = Image.composite(fade, Image.new("L", (S, S), 0), sheen.filter(ImageFilter.GaussianBlur(S * 0.01)))
        out.paste((255, 255, 255, 255), (0, 0), Image.composite(sheen, Image.new("L", (S, S), 0), shape))
        rim = Image.new("L", (S, S), 0)
        d = S * inset
        ImageDraw.Draw(rim).ellipse((d, d, S - 1 - d, S - 1 - d), outline=110 if dark else 90,
                                    width=max(2, round(S * 0.008)))
        out.paste(rgb8(s["fg"]), (0, 0), rim)
        art = mark(S, kind, letter, (0.22, 0.14, 0.78, 0.78), False)
        glow = art.filter(ImageFilter.GaussianBlur(S * 0.024)).point(lambda v: min(255, v * 1.25))
        out.paste(rgb8(s["accent"]), (0, 0), glow)
        out.paste(rgb8(s["fg"]), (0, 0), art)

    # Edges a touch softer than sharp: Konsole enlarges a picture by the
    # desktop's scaling without smoothing, which put steps in sharp ones.
    # Blurred premultiplied, so the rim takes no dark fringe.
    out = out.resize((side, side), Image.LANCZOS).convert("RGBa").filter(ImageFilter.GaussianBlur(0.45)).convert("RGBA")
    box = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    box.paste(out, ((width - side) // 2, (height - side) // 2))
    return box


if __name__ == "__main__":
    # ./distro_logo.py out.png [look] [size]: the logo alone, to look at.
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else "logo.png"
    look = sys.argv[2] if len(sys.argv) > 2 else T.current_look()
    size = int(sys.argv[3]) if len(sys.argv) > 3 else 320
    scheme = T.scheme(look)
    logo(look, scheme, size, size, scheme["palette"].get("wallpaper")).save(target)
