"""Check the sky mapping offline, for every level's sky dome.

usage: python tools/sky_check.py [--index PATH]

For each distinct dome: the planar UVs (``sky.dome_uv``) must cover the texture disc (rim near
radius 0.5, apex at the centre), and the world shader's mapping (``sky.direction_uv`` through the
elevation profile) must land within a small distance of the planar UV at every vertex above the
skirt. Prints the worst error in texels of the 1024-texel sky texture.
"""
import argparse
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _offline  # noqa: E402
from mag_importer.core import sky  # noqa: E402

TEXELS = 1024
LIMIT = 12.0  # texels; the rim ring is a polygon whose radius varies ~3 % with azimuth


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--index')
    a = ap.parse_args()
    lib = _offline.open_library(a.index)
    idx = lib.index
    seen = {}
    bad = 0
    for lv in idx.levels:
        hit = sky.sky_piece(idx, lv['archive'])
        name = lv['name'].replace('mag_', '').replace('_final', '')
        if hit is None:
            print('%-24s no sky piece' % name)
            bad += 1
            continue
        zi, ri = hit
        h = idx.zrbs[zi].records[ri][8]
        if h in seen:
            print('%-24s dome %08x (as %s)' % (name, h, seen[h]))
            continue
        seen[h] = name
        model, geoms = lib.model(zi, ri)
        p = np.concatenate([g.positions for g in geoms if g is not None]).astype(np.float64)
        uv = sky.dome_uv(p)
        prof = sky.elevation_profile(p)
        rad = np.hypot(uv[:, 0] - 0.5, uv[:, 1] - 0.5)
        d = p / np.linalg.norm(p, axis=1, keepdims=True)
        wuv = sky.direction_uv(prof, d)
        err = np.hypot(*(wuv - uv).T) * TEXELS
        el = np.degrees(np.arcsin(np.clip(d[:, 1], -1, 1)))
        upper = el > 0.5
        worst = float(err[upper].max())
        ok = worst < LIMIT and 0.48 < rad.max() < 0.53 and rad.min() < 0.01
        bad += not ok
        print('%-24s dome %08x  R %.3f  profile %d pts (%.1f..90 deg)  disc r %.3f..%.3f  '
              'world vs planar: median %.1f, worst %.1f texels above horizon  %s' % (
                  name, h, sky.dome_radius(p), len(prof), prof[0][0], rad.min(), rad.max(),
                  float(np.median(err[upper])), worst, 'OK' if ok else 'FAIL'))
    print('sky_check: %s' % ('FAIL (%d)' % bad if bad else 'OK'))
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
