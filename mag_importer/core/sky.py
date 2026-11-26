"""Level sky dome (``sky.zrb``).

Every level archive has a ``sky.zrb`` holding one level piece (Edge static, ``80DC0132``), its
material and two textures. The dome is small (about 5 m across its horizontal radius R) and is
drawn around the camera. Its vertex stream has a position and a normal but **no UVs**: the colour
texture (slot 3, 1024x1024) is a top-down picture of the sky, a disc inscribed in the square
whose rim is the horizon. The importer maps it with a planar projection of the dome's own
positions::

    uv = 0.5 + 0.5 * (x, z) / R          R = max |x|, |z| over the dome (game axes, Y up)

The dome is an upper shell plus a short skirt down to about -20 degrees, rotationally symmetric to
within ~3 %, so the texture radius ``s = |(x, z)| / R`` is a function of elevation alone. That
profile (``elevation_profile``) lets a world shader reproduce the mapping from a view direction.
Slot 255 is a 64x64 noise shared by every sky (dither); it is not used.

Not verified against the game's shader: the mirror orientation (the sign of z), and whether R is
fixed per dome or taken from the material values (+0x1F1 holds 1, 1, 0, 0, then 60, 60, 20000 or
5000, 200 or 400, then two colour triples; they look like fog and tint parameters).
"""
import numpy as np

from .zrb import T_LEVEL_MODEL


def sky_piece(index, archive):
    """(zi, ri) of the sky dome piece of a level archive, or None."""
    for zi, z in enumerate(index.zrbs):
        if z.archive == archive and z.name.endswith('/sky.zrb'):
            for ri, r in enumerate(z.records):
                if r[7] == T_LEVEL_MODEL:
                    return zi, ri
    return None


def dome_radius(positions):
    """Horizontal half-extent R of the dome (game axes)."""
    p = np.asarray(positions, np.float64)
    return float(np.abs(p[:, [0, 2]]).max())


def dome_uv(positions, radius=None):
    """Planar sky UVs of dome vertices, in the game's convention (V down, as stored in vertex
    streams; the Blender side flips V like every other UV set)."""
    p = np.asarray(positions, np.float64)
    r = radius or dome_radius(p)
    return (0.5 + 0.5 * p[:, [0, 2]] / r).astype(np.float32)


def elevation_profile(positions, radius=None, step=1.0):
    """[(elevation in degrees, s)] sorted by elevation, with s = |(x, z)| / R the texture radius of
    the dome at that elevation (averaged over the dome in ``step``-degree bins), clamped so it
    never grows with elevation, and ending in (90, 0)."""
    p = np.asarray(positions, np.float64)
    r = radius or dome_radius(p)
    h = np.hypot(p[:, 0], p[:, 2])
    el = np.degrees(np.arctan2(p[:, 1], h))
    s = h / r
    keep = h > 1e-6 * r  # the apex has no azimuth
    bins = np.round(el[keep] / step).astype(int)
    out = []
    for b in np.unique(bins):
        sel = bins == b
        out.append([float(el[keep][sel].mean()), float(s[keep][sel].mean())])
    for k in range(1, len(out)):  # monotonic: higher never maps further out
        out[k][1] = min(out[k][1], out[k - 1][1])
    if out[-1][0] < 90.0:
        out.append([90.0, 0.0])
    return [tuple(v) for v in out]


def profile_s(profile, elevation_deg):
    """Texture radius at an elevation (degrees), linear between profile points, clamped."""
    e = np.asarray(elevation_deg, np.float64)
    xs = np.array([a for a, _ in profile])
    ys = np.array([b for _, b in profile])
    return np.interp(e, xs, ys)


def direction_uv(profile, d):
    """Planar sky UVs (game convention) for unit view directions ``d`` (n, 3) in game axes, through
    the elevation profile: what the world shader computes."""
    d = np.asarray(d, np.float64)
    h = np.hypot(d[:, 0], d[:, 2])
    el = np.degrees(np.arctan2(d[:, 1], h))
    s = profile_s(profile, el)
    with np.errstate(invalid='ignore', divide='ignore'):
        k = np.where(h > 1e-9, s / h, 0.0)
    return np.stack([0.5 + 0.5 * d[:, 0] * k, 0.5 + 0.5 * d[:, 2] * k], 1)

