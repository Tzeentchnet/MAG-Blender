"""Check the terrain detail-layer decode against tile borders (offline, no Blender).

usage: python tools/terrain_check.py <level> [<level> ...]

For every pair of neighbouring leaf tiles it compares the edge texels of the layer weight maps
(slot 8) in *layer* space: each channel is credited to the colour layer texture it selects, so
tiles with different layer sets are compared correctly. A right channel order and orientation
give a small mean difference; the table also shows the V/U-flipped orientation and the other
channel orders, which must all be worse. It also checks that the detail UV of every layer is
world-continuous (``s * uv0 + offset`` agrees with ``s * world / tile size`` modulo 1).
"""
import itertools
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _offline  # noqa: E402
from mag_importer.core import materials, textures  # noqa: E402
from mag_importer.core.zrb import T_MATERIAL  # noqa: E402


def main():
    lib = _offline.open_library()
    idx = lib.index
    tex = idx.texture_hashes()
    cache = {}

    def weights(h):
        if h not in cache:
            t, z = lib.texture_info(h)
            cache[h] = textures.decode_rgba(t, z.data[t.data_at:t.data_at + t.size]).astype(np.float32) / 255.0
        return cache[h]

    for level in sys.argv[1:]:
        lv = _offline.level_entry(lib, level)
        tiles, uv_err, tile_size = {}, 0.0, set()
        for zi, ri in lv['terrain']:
            m, geoms = lib.model(zi, ri)
            for me, g in zip(m.meshes, geoms):
                hit = idx.find(me.material, (T_MATERIAL,), zi)
                if hit is None or g is None or len(g.positions) < 3:
                    continue
                z, r = lib.record(hit[1], hit[2])
                t = materials.parse_material(z.payload(r), r.name, tex).terrain
                if t is None:
                    continue
                P = np.asarray(g.positions, np.float64)
                u = np.asarray(g.uvs[0], np.float64)
                span = np.ptp(P[:, 0]) / max(np.ptp(u[:, 0]), 1e-9)
                L = 2.0 ** round(np.log2(span))  # leaf size: 64 or 128 m
                tile_size.add(int(L))
                tiles[(int(round(np.mean(P[:, 0] / L - u[:, 0]))),
                       int(round(np.mean(u[:, 1] + P[:, 2] / L))))] = t
                for s, c, d in t.uv:
                    eu = (s * u[:, 0] + c - s * P[:, 0] / L + 0.5) % 1.0 - 0.5
                    ev = (s * u[:, 1] + d + s * P[:, 2] / L + 0.5) % 1.0 - 0.5
                    uv_err = max(uv_err, np.abs(eu).max(), np.abs(ev).max())
        print('%s: %d layered tiles, tile size %s m, worst detail-UV continuity error %.4f' % (
            level, len(tiles), sorted(tile_size), uv_err))

        def seam(perm, flip):
            diffs = []
            for (i, j), t in tiles.items():
                for nb, axis in ((tiles.get((i, j - 1)), 0), (tiles.get((i + 1, j)), 1)):
                    if nb is None:
                        continue
                    a, b = weights(t.weights), weights(nb.weights)
                    ea, eb = (0, -1) if flip else (-1, 0)
                    ra = a[ea] if axis == 0 else a[:, ea]
                    rb = b[eb] if axis == 0 else b[:, eb]
                    la, lb = {}, {}
                    for c, h in zip(perm, t.colors):
                        la[h] = la.get(h, 0) + ra[:, c]
                    for c, h in zip(perm, nb.colors):
                        lb[h] = lb.get(h, 0) + rb[:, c]
                    diffs.append(np.mean([np.abs(la.get(h, 0) - lb.get(h, 0)).mean() for h in set(la) | set(lb)]))
            return float(np.mean(diffs)) if diffs else float('nan')
        base = seam((0, 1, 2, 3), False)
        # orders that only swap channels whose layer texture is the same on every tile tie exactly
        others = [seam(p, False) for p in itertools.permutations(range(4)) if p != (0, 1, 2, 3)]
        ties = sum(abs(o - base) < 1e-9 for o in others)
        worse = [o for o in others if abs(o - base) >= 1e-9]
        flipped = seam((0, 1, 2, 3), True)
        ok = min(others) >= base - 1e-9 and base < flipped and uv_err < 0.01
        print('  weight seam: RGBA->layers 0-3 %.4f, other orders %s (%d tie), flipped %.4f -> %s' % (
            base, '%.4f-%.4f' % (min(worse), max(worse)) if worse else 'n/a', ties, flipped,
            'OK' if ok else 'FAIL'))


if __name__ == '__main__':
    main()
