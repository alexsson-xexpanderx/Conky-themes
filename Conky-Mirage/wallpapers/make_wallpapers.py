#!/usr/bin/env python3
"""Draw the wallpapers that come with Conky Mirage.

    ./make_wallpapers.py                     every wallpaper, at 3440x1440 and 3840x2160
    ./make_wallpapers.py --size 2560x1440    at your own screen size
    ./make_wallpapers.py --only orbit        just one of them

Each one is written as a KDE wallpaper package into the folder of the look
it was made for (glass, solid, metro, pixel), next to this script.
"""
import argparse
import json
import os

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- colour
# Gradients are mixed in OKLab, so a blend between two colours stays as
# bright and as colourful as its ends; light is added in linear RGB.

_M1 = np.array([[0.4122214708, 0.5363325363, 0.0514459929],
                [0.2119034982, 0.6806995451, 0.1073969566],
                [0.0883024619, 0.2817188376, 0.6299787005]])
_M2 = np.array([[0.2104542553, 0.7936177850, -0.0040720468],
                [1.9779984951, -2.4285922050, 0.4505937099],
                [0.0259040371, 0.7827717662, -0.8086757660]])
_M1_T, _M2_T = _M1.T.astype(np.float32), _M2.T.astype(np.float32)
_M1_INV_T = np.linalg.inv(_M1).T.astype(np.float32)
_M2_INV_T = np.linalg.inv(_M2).T.astype(np.float32)


def hex_rgb(code):
    code = code.lstrip("#")
    return np.array([int(code[i:i + 2], 16) / 255 for i in (0, 2, 4)], np.float32)


def to_linear(c):
    c = np.asarray(c, np.float32)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


def to_srgb(c):
    c = np.clip(c, 0, 1)
    return np.where(c <= 0.0031308, 12.92 * c, 1.055 * np.power(c, 1 / 2.4) - 0.055)


def lab_of(lin):
    return np.cbrt(lin @ _M1_T) @ _M2_T


def lin_of(lab):
    return np.maximum((lab @ _M2_INV_T) ** 3 @ _M1_INV_T, 0)


def ok(code):
    return lab_of(to_linear(hex_rgb(code)))


def lin(code):
    return to_linear(hex_rgb(code))


def ramp(t, stops):
    """Colour stops [(position, "#hex"), ...] looked up at t, in OKLab."""
    pos = [p for p, _ in stops]
    labs = [ok(c) for _, c in stops]
    return np.stack([np.interp(t, pos, [l[i] for l in labs]) for i in range(3)], -1).astype(np.float32)


def mix(a, b, w):
    w = np.asarray(w, np.float32)
    if w.ndim == a.ndim - 1:
        w = w[..., None]
    return a + (b - a) * w


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


# ---------------------------------------------------------------- canvas
# Everything is laid out in units of the screen's height, so a wallpaper
# keeps its shapes on any screen and only shows more or less at the sides.
# A = width / height. The widget sits top right.

class Canvas:
    def __init__(self, w, h, seed):
        self.w, self.h = w, h
        self.A = w / h
        self.px = 1 / h
        self.X = ((np.arange(w, dtype=np.float32) + 0.5) / h)[None, :]
        self.Y = ((np.arange(h, dtype=np.float32) + 0.5) / h)[:, None]
        self.rng = np.random.default_rng(seed)

    def fill(self, lab_row):
        return np.ascontiguousarray(np.broadcast_to(lab_row, (self.h, self.w, 3)), np.float32)

    def blob(self, cx, cy, rx, ry, angle=0.0, X=None, Y=None):
        """A soft elliptical weight, 1 at the centre."""
        X = self.X if X is None else X
        Y = self.Y if Y is None else Y
        ca, sa = np.cos(np.radians(angle)), np.sin(np.radians(angle))
        dx, dy = X - cx, Y - cy
        u, v = (dx * ca + dy * sa) / rx, (-dx * sa + dy * ca) / ry
        return np.exp(-(u * u + v * v)).astype(np.float32)

    def stars(self, img, count, top, bottom, brightness, size=(0.5, 1.3), avoid=None, tint="#dfe6ff"):
        """Add small soft stars, in linear light."""
        colour = lin(tint)
        for _ in range(count):
            x = self.rng.uniform(0, self.w)
            y = self.rng.uniform(top * self.h, bottom * self.h)
            if avoid and avoid(x / self.h, y / self.h):
                continue
            r = self.rng.uniform(*size) * self.h / 1440
            b = brightness * self.rng.uniform(0.25, 1) ** 2
            x0, y0 = int(x - 4 * r - 2), int(y - 4 * r - 2)
            x1, y1 = int(x + 4 * r + 3), int(y + 4 * r + 3)
            x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, self.w), min(y1, self.h)
            if x1 <= x0 or y1 <= y0:
                continue
            yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32) + 0.5
            spot = np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (r * r))[..., None]
            img[y0:y1, x0:x1] += spot * b * colour


class Wave:
    """A line across the screen: base + tilt * x + sum of sines, plus any
    sharp ridges (all in height units)."""

    def __init__(self, base, parts, tilt=0.0, ridges=()):
        self.base, self.parts, self.tilt, self.ridges = base, parts, tilt, ridges

    def at(self, x):
        y = self.base + self.tilt * x
        for amp, freq, phase in self.parts:
            y = y + amp * np.sin(2 * np.pi * freq * x + phase)
        for amp, freq, phase in self.ridges:
            # 1 - |sin| has sharp tops: peaks rather than rolling hills.
            y = y - amp * (1 - np.abs(np.sin(np.pi * freq * x + phase))) ** 1.6
        return y

    def shifted(self, dy):
        return Wave(self.base + dy, self.parts, self.tilt, self.ridges)


def below(cv, wave, soft=0.0):
    """How much of each pixel lies below the line, with an anti-aliased edge;
    soft > 0 gives a feathered edge that wide instead (for shadows)."""
    xs = cv.X[0]
    c = wave.at(xs)
    slope = np.gradient(c, xs)
    d = (cv.Y - c[None, :]) / np.sqrt(1 + slope[None, :] ** 2)
    if soft:
        return smoothstep(-soft, 0, d).astype(np.float32)
    return np.clip(0.5 + d / cv.px, 0, 1).astype(np.float32)


def depth_below(cv, wave, scale):
    """0 at the line, rising to 1 at `scale` below it."""
    return np.clip((cv.Y - wave.at(cv.X[0])[None, :]) / scale, 0, 1).astype(np.float32)


def blur(field, radius, step=4):
    """Gaussian blur of a 2-D float field, done at 1/step size."""
    h, w = field.shape
    hs, ws = h // step, w // step
    small = field[:hs * step, :ws * step].reshape(hs, step, ws, step).mean(axis=(1, 3))
    sigma = radius / step
    k = np.arange(-int(3 * sigma) - 1, int(3 * sigma) + 2)
    kernel = np.exp(-(k / sigma) ** 2 / 2)
    kernel /= kernel.sum()
    small = np.apply_along_axis(lambda r: np.convolve(r, kernel, mode="same"), 1, small)
    small = np.apply_along_axis(lambda c: np.convolve(c, kernel, mode="same"), 0, small)
    big = Image.fromarray(small.astype(np.float32), mode="F").resize((w, h), Image.BICUBIC)
    return np.asarray(big, np.float32)


def line_glow(cv, dist, width, strength):
    return (strength * np.exp(-(dist / width) ** 2)).astype(np.float32)


# ---------------------------------------------------------------- shapes
# Shapes are signed distances in height units (negative inside), drawn only
# over their own box so a few hundred of them stay cheap.

def region(cv, x0, y0, x1, y1):
    c0, c1 = max(int(x0 * cv.h), 0), min(int(x1 * cv.h) + 2, cv.w)
    r0, r1 = max(int(y0 * cv.h), 0), min(int(y1 * cv.h) + 2, cv.h)
    if c1 <= c0 or r1 <= r0:
        return None
    return (slice(r0, r1), slice(c0, c1)), cv.X[:, c0:c1], cv.Y[r0:r1, :]


def paint(cv, img, box, sdf, colour, alpha=1.0, shadow=None, grow=0.0):
    """Fill a shape. colour is "#hex", an OKLab triple, or f(X, Y) -> OKLab.
    shadow is (dx, dy, blur, strength, "#hex"), drawn first, under it."""
    x0, y0, x1, y1 = box
    if shadow:
        dx, dy, spread, strength, tone = shadow
        r = region(cv, x0 + min(dx, 0) - 2 * spread, y0 + min(dy, 0) - 2 * spread,
                   x1 + max(dx, 0) + 2 * spread, y1 + max(dy, 0) + 2 * spread)
        if r:
            sl, X, Y = r
            w = strength * (1 - smoothstep(-spread, spread, sdf(X - dx, Y - dy)))
            img[sl] = mix(img[sl], ok(tone), w)
    r = region(cv, x0 - 2 * cv.px, y0 - 2 * cv.px, x1 + 2 * cv.px, y1 + 2 * cv.px)
    if not r:
        return
    sl, X, Y = r
    cover = np.clip(0.5 - (sdf(X, Y) - grow * cv.px) / cv.px, 0, 1) * alpha
    if callable(colour):
        colour = colour(X, Y)
    elif isinstance(colour, str):
        colour = ok(colour)
    img[sl] = mix(img[sl], colour, cover)


def sd_circle(cx, cy, r):
    return lambda X, Y: np.sqrt((X - cx) ** 2 + (Y - cy) ** 2) - r


def sd_box(cx, cy, hw, hh, radius=0.0, angle=0.0):
    ca, sa = np.cos(np.radians(angle)), np.sin(np.radians(angle))

    def f(X, Y):
        u = np.abs((X - cx) * ca + (Y - cy) * sa) - (hw - radius)
        v = np.abs(-(X - cx) * sa + (Y - cy) * ca) - (hh - radius)
        outside = np.sqrt(np.maximum(u, 0) ** 2 + np.maximum(v, 0) ** 2)
        return outside + np.minimum(np.maximum(u, v), 0) - radius
    return f


def sd_capsule(ax, ay, bx, by, r):
    def f(X, Y):
        px, py, ex, ey = X - ax, Y - ay, bx - ax, by - ay
        h = np.clip((px * ex + py * ey) / (ex * ex + ey * ey), 0, 1)
        return np.sqrt((px - ex * h) ** 2 + (py - ey * h) ** 2) - r
    return f


def sd_convex(points):
    pts = np.array(points, np.float64)
    cx, cy = pts.mean(axis=0)
    planes = []
    for (ax, ay), (bx, by) in zip(pts, np.roll(pts, -1, axis=0)):
        nx, ny = by - ay, -(bx - ax)
        length = np.hypot(nx, ny)
        nx, ny = nx / length, ny / length
        if (cx - ax) * nx + (cy - ay) * ny > 0:
            nx, ny = -nx, -ny
        planes.append((ax, ay, nx, ny))

    def f(X, Y):
        d = None
        for ax, ay, nx, ny in planes:
            di = (X - ax) * nx + (Y - ay) * ny
            d = di if d is None else np.maximum(d, di)
        return d
    return f


def sd_cookie(cx, cy, r, lobes, depth, turn=0.0):
    """A circle with a scalloped edge, like Material You's rounded shapes."""
    def f(X, Y):
        dx, dy = X - cx, Y - cy
        dist = np.sqrt(dx * dx + dy * dy) + 1e-9
        theta = np.arctan2(dy, dx)
        edge = r * (1 + depth * np.cos(lobes * (theta - turn)))
        dedge = -r * depth * lobes * np.sin(lobes * (theta - turn))
        return (dist - edge) / np.sqrt(1 + (dedge / dist) ** 2)
    return f


def sd_arch(cx, cy, radius, half_width):
    """A band round (cx, cy), the half above it: one stripe of a rainbow."""
    def f(X, Y):
        band = np.abs(np.sqrt((X - cx) ** 2 + (Y - cy) ** 2) - radius) - half_width
        return np.maximum(band, Y - cy)
    return f


class Profile:
    """A skyline worked out once along the screen's columns."""

    def __init__(self, values):
        self.values = values

    def at(self, x):
        return self.values


def shade(code, dl=0.0, dc=1.0, dh=0.0):
    """A colour made lighter or darker (OKLab L), more or less colourful, or turned."""
    L, a, b = ok(code)
    c, h = np.hypot(a, b) * dc, np.arctan2(b, a) + np.radians(dh)
    return np.array([L + dl, c * np.cos(h), c * np.sin(h)], np.float32)


# ---------------------------------------------------------------- wallpapers

def afterglow(cv):
    """Dark, with soft coloured light pooling at the bottom: rose, amber, a
    violet haze, and a cool blue up where the widget sits."""
    A, X, Y = cv.A, cv.X, cv.Y
    # A gentle warp makes the pools of light flow instead of sitting round.
    WX = X + 0.07 * np.sin(2.4 * Y + 0.6) + 0.035 * np.sin(5.1 * Y + 1.7 * X)
    WY = Y + 0.06 * np.sin(1.9 * X + 1.2) + 0.025 * np.sin(4.3 * X - 2.6 * Y)
    img = cv.fill(ramp(Y, [(0, "#0b0a1a"), (0.6, "#120f2a"), (1, "#181236")]))
    pools = [
        (0.55 * A, 0.30, 0.75, 0.38, -10, "#33246e", 0.55),   # violet haze, middle
        (A - 0.30, 0.22, 0.55, 0.42, 25, "#1f6fa8", 0.70),    # cool blue behind the widget
        (A - 0.05, 0.62, 0.35, 0.30, 0, "#3a9fc4", 0.45),
        (0.12 * A, 1.02, 0.70, 0.36, 8, "#b8306b", 0.92),     # rose, bottom left
        (0.43 * A, 1.10, 0.62, 0.30, -6, "#ee8a3a", 0.85),    # amber, bottom middle
        (0.30 * A, 0.98, 0.25, 0.16, 0, "#ffb27a", 0.45),
        (0.66 * A, 1.06, 0.50, 0.26, 10, "#d0457a", 0.55),
        (0.30 * A, 0.18, 0.80, 0.30, 0, "#0a0918", 0.65),     # keeps the top calm
    ]
    for cx, cy, rx, ry, ang, colour, strength in pools:
        img = mix(img, ok(colour), strength * cv.blob(cx, cy, rx, ry, ang, WX, WY))
    out = lin_of(img)
    r2 = ((X - A / 2) / (A * 0.75)) ** 2 + ((Y - 0.45) / 0.9) ** 2
    out *= (1 - 0.22 * np.clip(r2, 0, 1))[..., None]
    cv.stars(out, int(110 * A), 0.0, 0.55, 0.06, avoid=lambda x, y: y > 0.5)
    return out


def horizon(cv):
    """Dusk over layered hills: a low sun on the left, the sky going from
    indigo to peach, and the widget up in the deep blue."""
    A, X, Y = cv.A, cv.X, cv.Y
    img = cv.fill(ramp(Y, [(0, "#0f1332"), (0.26, "#212458"), (0.44, "#463878"),
                            (0.56, "#8c5689"), (0.635, "#d9807f"), (0.69, "#f6b487")]))
    sx, sy, sr = 0.27 * A, 0.625, 0.068
    d = np.sqrt((X - sx) ** 2 + ((Y - sy) * 1.6) ** 2)
    img = mix(img, ok("#ffb98c"), 0.50 * np.exp(-(d / 0.20) ** 2))
    img = mix(img, ok("#f39a86"), 0.22 * np.exp(-(d / 0.55) ** 2) * smoothstep(0.35, 0.62, Y))
    out = lin_of(img)
    cv.stars(out, int(80 * A), 0.0, 0.40, 0.09, avoid=lambda x, y: y > 0.36)
    ds = np.sqrt((X - sx) ** 2 + (Y - sy) ** 2)
    sun = lin_of(ramp(np.clip((Y - (sy - sr)) / (2 * sr), 0, 1), [(0, "#fff6e4"), (1, "#ffc994")]))
    out = mix(out, sun, np.clip(0.5 + (sr - ds) / (1.5 * cv.px), 0, 1))
    img = lab_of(out)

    layers = [  # base, ridge share, amplitude, colour, haze at its foot
        (0.650, 0.75, 0.030, "#c88593", "#eaa596"),
        (0.700, 0.55, 0.045, "#9d6388", "#c98493"),
        (0.765, 0.35, 0.052, "#70467a", "#9a6489"),
        (0.845, 0.15, 0.058, "#462f61", "#664576"),
        (0.935, 0.00, 0.052, "#22182f", "#33233f"),
    ]
    rng = cv.rng
    for i, (base, peaky, amp, colour, haze) in enumerate(layers):
        f0 = 0.30 + 0.10 * i
        parts = [(amp * (1 - peaky) * w, f0 * m, rng.uniform(0, 2 * np.pi))
                 for w, m in ((1, 1), (0.45, 2.3), (0.18, 4.9), (0.06, 10.5))]
        ridges = [(amp * peaky * w, f0 * m, rng.uniform(0, np.pi)) for w, m in ((1.6, 1.4), (0.7, 3.1))]
        wave = Wave(base, parts, 0.0, ridges)
        body = mix(cv.fill(ok(colour)), ok(haze), 0.45 * smoothstep(0, 1, depth_below(cv, wave, 0.12)))
        img = mix(img, body, below(cv, wave))
    out = lin_of(img)
    r2 = ((X - A / 2) / (A * 0.8)) ** 2 + ((Y - 0.5) / 0.95) ** 2
    out *= (1 - 0.15 * np.clip(r2, 0, 1))[..., None]
    return out


def orbit(cv):
    """Night: a large dark planet rising from the bottom left, lit only along
    its edge, and the thin lines of its orbits crossing the sky."""
    A, X, Y = cv.A, cv.X, cv.Y
    d0 = np.sqrt(((X - 0.3 * A) / 1.4) ** 2 + ((Y - 0.9) / 1.0) ** 2)
    img = mix(cv.fill(ok("#04050b")), ok("#0d1530"), np.exp(-(d0 / 0.75) ** 2))
    img = mix(img, ok("#24194d"), 0.55 * cv.blob(A - 0.40, 0.28, 0.65, 0.42, 20))
    img = mix(img, ok("#0e3b52"), 0.45 * cv.blob(A - 0.10, 0.70, 0.40, 0.35))
    out = lin_of(img)

    px_, py_, R = 0.20 * A, 1.42, 0.98
    dx, dy = (X - px_) / R, (Y - py_) / R
    rr = dx * dx + dy * dy
    inside = rr < 1
    dz = np.sqrt(np.clip(1 - rr, 0, 1))
    L = np.array([0.40, -0.62, -0.68])     # from behind: only the edge is lit
    L = L / np.linalg.norm(L)
    lambert = np.clip(dx * L[0] + dy * L[1] + dz * L[2], 0, 1)
    planet = lin_of(ramp(lambert, [(0, "#05071a"), (0.12, "#0b1440"), (0.40, "#1f4a99"),
                                    (0.72, "#58bfe6"), (1, "#d2f6ff")]))
    edge = np.clip(0.5 + (1 - np.sqrt(rr)) * R / cv.px, 0, 1)
    # Atmosphere: a thin glow round the limb, bright on the lit side.
    dist = (np.sqrt(rr) - 1) * R
    lit = np.clip(dx * L[0] + dy * L[1] + 0.35, 0, 1)
    halo = np.where(dist > 0, np.exp(-dist / 0.018) * 0.55 + np.exp(-dist / 0.09) * 0.12, 0)
    out += (halo * lit)[..., None] * lin("#62c8ff")
    out += (np.where(dist > 0, np.exp(-dist / 0.05), 0) * (1 - lit) * 0.06)[..., None] * lin("#8a6cff")
    rim = np.exp(-np.clip(1 - np.sqrt(rr), 0, 1) * R / 0.006) * inside * lit
    planet = planet + (0.35 * rim)[..., None] * lin("#9fe8ff")
    out = mix(out, planet, edge)

    cv.stars(out, int(260 * A), 0.0, 1.0, 0.14,
             avoid=lambda x, y: ((x - px_) ** 2 + (y - py_) ** 2) < (R + 0.02) ** 2)

    # Orbits: thin ellipses round the planet, hidden where they pass behind it.
    behind = edge
    for a, b, ang, alpha, moon_at in ((1.55, 0.50, -9, 0.30, 2.25), (2.15, 0.78, -6, 0.20, None),
                                      (2.95, 1.10, -4, 0.13, 2.62)):
        ca, sa = np.cos(np.radians(ang)), np.sin(np.radians(ang))
        u = (X - px_) * ca + (Y - py_) * sa
        v = -(X - px_) * sa + (Y - py_) * ca
        f = (u / a) ** 2 + (v / b) ** 2 - 1
        grad = np.sqrt((2 * u / a ** 2) ** 2 + (2 * v / b ** 2) ** 2) + 1e-6
        dist = np.abs(f) / grad
        ring = line_glow(cv, dist, 0.75 * cv.px, alpha) + line_glow(cv, dist, 6 * cv.px, alpha * 0.08)
        out += (ring * (1 - behind))[..., None] * lin("#b9ccff")
        if moon_at is not None:
            mu, mv = a * np.cos(moon_at), b * np.sin(moon_at)
            mx, my = px_ + mu * ca - mv * sa, py_ + mu * sa + mv * ca
            md = np.sqrt((X - mx) ** 2 + (Y - my) ** 2)
            out += (np.exp(-(md / 0.035) ** 2) * 0.10)[..., None] * lin("#9fd8ff")
            out = mix(out, lin("#e8f4ff"), np.clip(0.5 + (0.0065 - md) / cv.px, 0, 1))
    return out


def silk(cv):
    """A single ribbon of light folding across a dark screen, magenta on the
    left turning to cyan on the right, passing under the widget."""
    A, X, Y = cv.A, cv.X, cv.Y
    img = cv.fill(ramp(Y, [(0, "#06060d"), (1, "#0c0a18")]))
    img = mix(img, ok("#1c1240"), 0.55 * cv.blob(0.30 * A, 0.75, 0.9, 0.45))
    img = mix(img, ok("#0b2a3a"), 0.45 * cv.blob(A - 0.35, 0.40, 0.7, 0.5))
    out = lin_of(img)

    # Colour along the ribbon, by x.
    t = np.clip(X / A, 0, 1)
    hue = lin_of(ramp(t, [(0, "#ff4d8d"), (0.35, "#c65bff"), (0.65, "#6f7dff"), (1, "#3fd6ff")]))[0]
    light = np.zeros((cv.h, cv.w), np.float32)
    n = 240
    xs = cv.X[0]
    # The ribbon's middle line, how wide it is, and how it turns over.
    centre = (0.74 - 0.36 * (xs / A) + 0.07 * np.sin(2 * np.pi * xs / A * 1.3 + 0.7)
              + 0.025 * np.sin(2 * np.pi * xs / A * 3.1 + 2.0))
    width = 0.10 + 0.13 * (0.5 + 0.5 * np.sin(2 * np.pi * xs / A * 1.1 + 0.4))
    twist = np.sin(2 * np.pi * (0.33 * xs / A * 2.2 + 0.15))
    for k in range(n):
        s = k / (n - 1)
        # Each thread is a slightly different wave; together they twist,
        # pinch and open like a ribbon.
        y = centre + (s - 0.5) * width * twist + 0.018 * np.sin(2 * np.pi * xs / A * 2.0 + 6 * s)
        slope = np.gradient(y, xs)
        lo = max(int((y.min() - 0.02) * cv.h), 0)
        hi = min(int((y.max() + 0.02) * cv.h) + 1, cv.h)
        if hi <= lo:
            continue
        dist = np.abs(cv.Y[lo:hi] - y[None, :]) / np.sqrt(1 + slope[None, :] ** 2)
        edge_bright = 0.55 + 0.45 * (abs(s - 0.5) * 2) ** 3
        light[lo:hi] += edge_bright * np.exp(-(dist / (0.85 * cv.px)) ** 2) * 0.050
    # Bloom: the same light, blurred, so the folds glow. Blurred small and in
    # floating point; an 8-bit copy left steps in the glow.
    glow = blur(light, cv.h * 0.025)
    out += (light + 1.3 * glow)[..., None] * hue[None, :, :]
    return out


def dunes(cv):
    """Light: pale sand under a pale sky, a soft sun, long dunes sweeping
    up towards the right."""
    A, X, Y = cv.A, cv.X, cv.Y
    img = cv.fill(ramp(Y, [(0, "#e9e2e4"), (0.35, "#f2e2d8"), (0.62, "#f8d8c0")]))
    sx, sy, sr = 0.30 * A, 0.30, 0.075
    out = lin_of(img)
    d = np.sqrt((X - sx) ** 2 + (Y - sy) ** 2)
    out += lin("#ffd9b0") * (0.22 * np.exp(-(d / 0.35) ** 2))[..., None]
    out = mix(out, lin("#fff6ec"), np.clip(0.5 + (sr - d) / cv.px, 0, 1))
    img = lab_of(out)

    layers = [  # base, tilt, amplitude, colour (crest), colour (deep)
        (0.60, -0.035, 0.030, "#efc8ad", "#e6b69a"),
        (0.66, -0.050, 0.040, "#e7b090", "#d99b7a"),
        (0.74, -0.060, 0.050, "#d98f6a", "#c87c58"),
        (0.84, -0.045, 0.055, "#bf6e4b", "#a85c3d"),
        (0.95, -0.030, 0.045, "#94503a", "#7c412f"),
    ]
    rng = cv.rng
    ground = np.zeros((cv.h, cv.w), np.float32)
    for i, (base, tilt, amp, crest, deep) in enumerate(layers):
        f0 = 0.22 + 0.07 * i
        parts = [(amp * w, f0 * m, rng.uniform(0, 2 * np.pi)) for w, m in ((1, 1), (0.35, 2.1), (0.12, 4.4))]
        base_adj = base + 0.035 * A * (-tilt)    # keep the middle of the screen at base
        wave = Wave(base_adj, parts, tilt)
        # A soft shadow on the dunes behind, never on the sky.
        img = mix(img, ok("#6a3526"), 0.10 * below(cv, wave.shifted(-0.003), soft=0.03)
                  * (1 - below(cv, wave)) * ground)
        depth = depth_below(cv, wave, 0.10)
        # The face towards the sun (left) is lit, the other side falls into shade.
        xs = cv.X[0]
        facing = np.clip(-np.gradient(wave.at(xs), xs) * 3.0, -1, 1)[None, :]
        body = mix(cv.fill(ok(crest)), ok(deep), smoothstep(0, 1, depth))
        body = mix(body, ok("#fff1e2"), 0.32 * np.clip(facing, 0, 1) * np.exp(-depth * 1.5))
        body = mix(body, ok("#7a3f2c"), 0.16 * np.clip(-facing, 0, 1) * np.exp(-depth * 1.5))
        cover = below(cv, wave)
        img = mix(img, body, cover)
        ground = np.maximum(ground, cover)
    return lin_of(img)


def pines(cv):
    """Misty pine ridges under a pale moon, in teal and green: flat layers for
    the flat boxes of the Solid look."""
    A, X, Y = cv.A, cv.X, cv.Y
    img = cv.fill(ramp(Y, [(0, "#0c1f2c"), (0.32, "#183f4d"), (0.56, "#4f8a8c"), (0.70, "#b5dccd")]))
    mx, my, mr = 0.24 * A, 0.27, 0.072
    d = np.sqrt((X - mx) ** 2 + (Y - my) ** 2)
    img = mix(img, ok("#cfeadf"), 0.30 * np.exp(-(d / 0.24) ** 2))
    out = lin_of(img)
    cv.stars(out, int(70 * A), 0.0, 0.40, 0.08, avoid=lambda x, y: y > 0.36)
    out = mix(out, lin("#eef6ea"), np.clip(0.5 + (mr - d) / (1.5 * cv.px), 0, 1))
    img = lab_of(out)

    xs = cv.X[0]
    rng = cv.rng
    layers = [  # base, tree height, spacing, colour, haze at its foot
        (0.63, 0.030, 0.010, "#86b7ad", "#b3d8cb"),
        (0.69, 0.046, 0.013, "#5d968f", "#86b8ae"),
        (0.76, 0.068, 0.018, "#3a6f6d", "#5a8f8a"),
        (0.85, 0.100, 0.025, "#1f4749", "#30605f"),
        (0.96, 0.140, 0.034, "#0c2427", "#132f33"),
    ]
    for i, (base, tall, spacing, colour, haze) in enumerate(layers):
        hill = Wave(base, [(0.018, 0.25 + 0.08 * i, rng.uniform(0, 6.3)),
                           (0.008, 0.9 + 0.2 * i, rng.uniform(0, 6.3))])
        ground = hill.at(xs)
        top = ground.copy()
        x = rng.uniform(0, spacing)
        while x < A + spacing:
            # Trees grow in stands, with clearings between them.
            if np.sin(x * (3.1 + i) + i * 1.7) + 0.6 * np.sin(x * 7.3 + i) > -0.55:
                h = tall * rng.uniform(0.55, 1.0)
                w = h * rng.uniform(0.26, 0.34)
                near = np.abs(xs - x) < w
                tip = hill.at(np.array([x]))[0] - h
                top[near] = np.minimum(top[near], tip + h * np.abs(xs[near] - x) / w)
            x += spacing * rng.uniform(0.7, 1.3)
        skyline = Profile(top)
        body = mix(cv.fill(ok(colour)), ok(haze), 0.5 * smoothstep(0, 1, depth_below(cv, Wave(base - 0.02, []), 0.14)))
        img = mix(img, body, below(cv, skyline))
    return lin_of(img)


def shards(cv):
    """Metro: a band of flat crystal facets rising from the bottom left,
    magenta into blue, on near-black."""
    A = cv.A
    img = cv.fill(ramp(cv.Y, [(0, "#0f1124"), (1, "#151834")]))
    rng = cv.rng
    start, end = np.array([-0.15, 1.10]), np.array([A + 0.10, 0.52])
    along = end - start
    length = np.linalg.norm(along)
    along /= length
    across = np.array([along[1], -along[0]])
    du, dv = 0.13, 0.085
    nu, nv = int(length / du) + 2, 5
    grid = np.zeros((nu + 1, nv + 1, 2))
    for i in range(nu + 1):
        for j in range(nv + 1):
            u = i * du + rng.uniform(-0.35, 0.35) * du * (0 < i < nu)
            v = (j - nv / 2) * dv + rng.uniform(-0.3, 0.3) * dv * (0 < j < nv)
            # The band swells in the middle and thins at both ends.
            v *= 0.55 + 0.75 * np.sin(np.pi * np.clip(u / length, 0, 1))
            grid[i, j] = start + u * along + v * across
    colours = ["#d81b60", "#ad1457", "#8e24aa", "#5e35b1", "#3949ab", "#1e5fd0", "#0288d1"]
    for i in range(nu):
        t = i / (nu - 1)
        base = colours[min(int(t * (len(colours) - 1) + rng.uniform(-0.6, 0.6)), len(colours) - 1)]
        for j in range(nv):
            a, b, c, d = grid[i, j], grid[i + 1, j], grid[i + 1, j + 1], grid[i, j + 1]
            tris = [(a, b, c), (a, c, d)] if rng.random() < 0.5 else [(a, b, d), (b, c, d)]
            edge = min(j, nv - 1 - j)
            for tri in tris:
                if edge == 0 and rng.random() < 0.45:
                    continue          # a ragged edge, not a ruled one
                p = np.array(tri)
                tilt = rng.uniform(-1, 1)      # each facet catches the light a little differently
                col = shade(base, dl=0.07 * tilt, dc=1 + 0.1 * rng.uniform(-1, 1), dh=rng.uniform(-6, 6))
                box = (p[:, 0].min(), p[:, 1].min(), p[:, 0].max(), p[:, 1].max())
                paint(cv, img, box, sd_convex(p), col, grow=0.6)
    return lin_of(img)


def stripes(cv):
    """Metro: bold flat bands crossing the bottom left corner on a dark
    ground, in magenta, violet and blues."""
    A = cv.A
    img = cv.fill(ramp(cv.Y, [(0, "#101223"), (1, "#141730")]))
    bands = [  # where it meets the bottom edge (share of the width), width, colour
        (0.10, 0.060, "#1565c0"),
        (0.22, 0.120, "#3949ab"),
        (0.39, 0.045, "#00897b"),
        (0.49, 0.095, "#7b1fa2"),
        (0.64, 0.150, "#c2185b"),
        (0.85, 0.040, "#e8467c"),
        (0.95, 0.075, "#5e35b1"),
    ]
    # The bands lean so the first runs from the bottom left corner to 62% of
    # the way along the top, which keeps the same look on wide and 16:9 screens.
    angle = -np.degrees(np.arctan2(1.0, 0.62 * A))
    for offset, width, colour in bands:
        cx, cy = offset * 0.55 * A, 1.0 + width / 2
        f = sd_box(cx, cy, 2.5, width / 2, 0.0, angle)
        paint(cv, img, (cx - 2.6, cy - 1.6, cx + 2.6, cy + 1.6), f, colour)
    return lin_of(img)


def mosaic(cv):
    """Metro: square tiles, thick at the bottom left and thinning out towards
    the widget, as if it were part of the same wall."""
    A = cv.A
    img = cv.fill(ramp(cv.Y, [(0, "#0f1122"), (1, "#14172e")]))
    rng = cv.rng
    pitch, gap = 0.092, 0.010
    size = pitch - gap
    cols, rows = int(A / pitch) + 1, int(1 / pitch) + 1
    taken = np.zeros((cols + 2, rows + 2), bool)
    palette = [("#00a3a3", 4), ("#00838f", 3), ("#006d75", 2), ("#7e57c2", 3), ("#5e35b1", 2), ("#c2185b", 1)]
    names = [c for c, _ in palette]
    weights = np.array([w for _, w in palette], float)
    weights /= weights.sum()
    for i in range(cols):
        for j in range(rows):
            if taken[i, j]:
                continue
            x, y = 0.012 + i * pitch, 1 - (j + 1) * pitch + gap * 0.5
            # Dense in the bottom left corner, thinning out towards the top right.
            reach = (x / A) * 0.9 + (1 - y) * 1.0
            if rng.random() > 1.35 - reach * 1.6:
                continue
            span = 2 if (rng.random() < 0.16 and not taken[i + 1, j] and not taken[i, j + 1]
                         and not taken[i + 1, j + 1] and i + 1 < cols and j + 1 < rows) else 1
            for a in range(span):
                for b in range(span):
                    taken[i + a, j + b] = True
            w = span * pitch - gap
            cx, cy = x + w / 2, y + size - w / 2
            colour = shade(rng.choice(names, p=weights), dl=rng.uniform(-0.03, 0.03) - 0.06 * reach)
            paint(cv, img, (cx - w / 2, cy - w / 2, cx + w / 2, cy + w / 2), sd_box(cx, cy, w / 2, w / 2), colour)
    return lin_of(img)


def bloom(cv):
    """Pixel, light: soft rounded shapes in peach and periwinkle on pale
    lilac, the way Android's own wallpapers look."""
    A = cv.A
    img = cv.fill(ramp(cv.Y, [(0, "#efebf7"), (1, "#e5e1f3")]))
    soft = (0.0, 0.010, 0.030, 0.22, "#9a8fbf")
    paint(cv, img, (A - 0.60, 0.45, A + 0.3, 1.4), sd_circle(A - 0.10, 0.98, 0.42), "#cdd6f6", shadow=soft)
    paint(cv, img, (0.17 * A - 0.45, 0.35, 0.17 * A + 0.45, 1.3), sd_cookie(0.17 * A, 0.80, 0.36, 9, 0.05, 0.3),
          "#f5c9b7", shadow=soft)
    paint(cv, img, (0.36 * A, 0.45, 0.62 * A, 1.2), sd_capsule(0.42 * A, 1.02, 0.56 * A, 0.66, 0.095), "#b9c6f1",
          shadow=soft)
    paint(cv, img, (0.36 * A - 0.12, 0.13, 0.36 * A + 0.12, 0.38), sd_cookie(0.36 * A, 0.25, 0.085, 4, 0.10, 0.4),
          "#f7d7cb", shadow=soft)
    paint(cv, img, (0.60 * A - 0.06, 0.20, 0.60 * A + 0.06, 0.32), sd_circle(0.60 * A, 0.26, 0.045), "#c9c2ef",
          shadow=soft)
    return lin_of(img)


def pebbles(cv):
    """Pixel, dark: smooth rounded stones in muted tones on deep slate,
    lifted off it by soft shadows."""
    A = cv.A
    img = cv.fill(ramp(cv.Y, [(0, "#1a1e2c"), (1, "#20263a")]))
    soft = (0.0, 0.014, 0.045, 0.60, "#0a0c13")
    stones = [  # x as a share of the width, y, half width, half height, angle, colour
        (0.10, 1.00, 0.30, 0.17, 4, "#3b5f80"),
        (0.29, 1.02, 0.22, 0.14, -6, "#7a5a8c"),
        (0.15, 0.80, 0.19, 0.105, -7, "#4f8079"),
        (0.25, 0.66, 0.11, 0.07, 9, "#9a6a86"),
        (0.19, 0.555, 0.06, 0.042, -5, "#8ea6cc"),
        (0.88, 1.03, 0.30, 0.15, -3, "#46607f"),
        (0.74, 1.04, 0.14, 0.09, 8, "#6c5a8a"),
    ]
    for fx, cy, hw, hh, ang, colour in stones:
        cx = fx * A
        reach = max(hw, hh) + 0.02
        top, bottom = shade(colour, dl=0.05), shade(colour, dl=-0.04)
        paint(cv, img, (cx - reach, cy - reach, cx + reach, cy + reach), sd_box(cx, cy, hw, hh, hh * 0.98, ang),
              lambda X, Y, cy=cy, hh=hh, top=top, bottom=bottom:
                  mix(np.broadcast_to(top, (Y.shape[0], X.shape[1], 3)), bottom,
                      np.clip((Y - (cy - hh)) / (2 * hh), 0, 1) + 0 * X),
              shadow=soft)
    return lin_of(img)


def arches(cv):
    """Pixel: nested arches like a soft rainbow, in rose and teal on deep
    green-blue, with a round sun beside them."""
    A = cv.A
    img = cv.fill(ramp(cv.Y, [(0, "#13262b"), (1, "#18303a")]))
    soft = (0.0, 0.008, 0.030, 0.45, "#08141a")
    def rainbow(cx, outer, colours, width):
        for k, colour in enumerate(colours):
            radius = outer - (k + 0.5) * width
            paint(cv, img, (cx - outer - 0.05, 1 - outer - 0.05, cx + outer + 0.05, 1.0), sd_arch(cx, 1.0, radius, width / 2),
                  colour, shadow=soft if k == 0 else None)
    big = 0.22 * A
    rainbow(big, 0.62, ["#f2b6c4", "#d08aa6", "#79b8ae", "#4b8b8b", "#efe2d3"], 0.085)
    rainbow(big + 0.98, 0.30, ["#79b8ae", "#f2b6c4", "#efe2d3"], 0.07)
    sun = big + 0.50
    paint(cv, img, (sun - 0.08, 0.33, sun + 0.08, 0.49), sd_circle(sun, 0.41, 0.06), "#f2b6c4", shadow=soft)
    return lin_of(img)


# key: (name, the look it was made for, how to draw it, seed)
WALLPAPERS = {
    "afterglow": ("Afterglow", "glass", afterglow, 11),
    "silk": ("Silk", "glass", silk, 3),
    "orbit": ("Orbit", "glass", orbit, 5),
    "horizon": ("Horizon", "solid", horizon, 23),
    "dunes": ("Dunes", "solid", dunes, 17),
    "pines": ("Pines", "solid", pines, 29),
    "shards": ("Shards", "metro", shards, 7),
    "stripes": ("Stripes", "metro", stripes, 13),
    "mosaic": ("Mosaic", "metro", mosaic, 19),
    "bloom": ("Bloom", "pixel", bloom, 31),
    "pebbles": ("Pebbles", "pixel", pebbles, 37),
    "arches": ("Arches", "pixel", arches, 41),
}


def finish(out, rng, grain=0.010):
    """Linear light to 8-bit sRGB, with fine grain so gradients never band."""
    srgb = to_srgb(out)
    h, w, _ = srgb.shape
    noise = rng.normal(0, grain, (h, w, 1)).astype(np.float32)
    dither = (rng.random((h, w, 3), np.float32) - rng.random((h, w, 3), np.float32)) / 255
    return np.clip((srgb + noise + dither) * 255 + 0.5, 0, 255).astype(np.uint8)


def write_package(key, sizes, out_dir):
    name, look, draw, seed = WALLPAPERS[key]
    title = f"Mirage {look.capitalize()} {name}"
    ident = title.replace(" ", "")
    folder = os.path.join(out_dir, look, ident)
    images = os.path.join(folder, "contents", "images")
    os.makedirs(images, exist_ok=True)
    first = None
    for w, h in sizes:
        cv = Canvas(w, h, seed)
        pixels = finish(draw(cv), np.random.default_rng(seed + 1000))
        im = Image.fromarray(pixels)
        im.save(os.path.join(images, f"{w}x{h}.jpg"), quality=92, subsampling=0, optimize=True)
        first = first or im
        print(f"{title}: {w}x{h}")
    thumb = first.resize((400, round(400 * first.height / first.width)), Image.LANCZOS)
    thumb.save(os.path.join(folder, "contents", "screenshot.jpg"), quality=90)
    meta = {"KPlugin": {"Authors": [{"Name": "alexsson"}], "Id": ident,
                        "License": "CC-BY-SA-4.0", "Name": title}}
    with open(os.path.join(folder, "metadata.json"), "w") as f:
        json.dump(meta, f, indent=4)
        f.write("\n")


def main():
    ap = argparse.ArgumentParser(description="Draw the Conky Mirage wallpapers.")
    ap.add_argument("--size", action="append", help="WIDTHxHEIGHT, can be given more than once")
    ap.add_argument("--only", action="append", choices=sorted(WALLPAPERS), help="just this one")
    ap.add_argument("--out", default=HERE, help="where to write the wallpaper folders")
    args = ap.parse_args()
    sizes = [tuple(int(v) for v in s.lower().split("x")) for s in (args.size or ["3440x1440", "3840x2160"])]
    for key in args.only or WALLPAPERS:
        write_package(key, sizes, args.out)


if __name__ == "__main__":
    main()
