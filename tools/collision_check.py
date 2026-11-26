"""Decode every collision mesh of each level and check it against the render geometry.

usage: python tools/collision_check.py [level ...]

Per level:

* every ``2A8BDF5A`` record in ``physics.zrb`` decodes (``core/collision.py``);
* every model placement names at most one collision record, and a mesh always the same one;
* exactly one unplaced record covers the terrain square (the terrain collision);
* piece collision lies on its model: for each placed (mesh, collision) pair, the median distance
  from collision vertices to the nearest model vertex, relative to the model's size; the level
  passes when the median over pairs is under 1 %;
* terrain collision lies on the leaf terrain tiles: height difference (game Y) between each
  sampled collision vertex and the nearest leaf tile vertex in x/z; passes under 0.5 m median.
"""
import collections
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _offline  # noqa: E402
from mag_importer.core import collision as col, level as lvl  # noqa: E402
from mag_importer.core.zrb import MODEL_TYPES, T_COLLISION, Zrb  # noqa: E402


def nearest(a, b, chunk=256):
    """Distance from each row of a to the nearest row of b (brute force, chunked)."""
    out = np.empty(len(a))
    for i in range(0, len(a), chunk):
        d = ((a[i:i + chunk, None, :] - b[None, :, :]) ** 2).sum(-1)
        j = d.argmin(1)
        out[i:i + chunk] = np.sqrt(d[np.arange(len(j)), j])
    return out


def nearest_xz_height(pts, ref, cell=4.0):
    """Height difference between pts and the nearest ref vertex in x/z (grid-bucketed)."""
    key = lambda p: (np.floor(p[:, 0] / cell).astype(np.int64), np.floor(p[:, 2] / cell).astype(np.int64))  # noqa: E731
    gx, gz = key(ref)
    buckets = collections.defaultdict(list)
    for i, k in enumerate(zip(gx.tolist(), gz.tolist())):
        buckets[k].append(i)
    px, pz = key(pts)
    out = np.full(len(pts), np.nan)
    for n, (x, z) in enumerate(zip(px.tolist(), pz.tolist())):
        cand = [i for dx in (-1, 0, 1) for dz in (-1, 0, 1) for i in buckets.get((x + dx, z + dz), ())]
        if not cand:
            continue
        c = ref[cand]
        j = ((c[:, [0, 2]] - pts[n, [0, 2]]) ** 2).sum(1).argmin()
        out[n] = abs(c[j, 1] - pts[n, 1])
    return out


def check_level(lib, lv, rng):
    ai = lv['archive']
    t0 = time.time()
    z = Zrb(lib.archive(ai).read(lv['clv'].rsplit('/', 1)[0] + '/resource_blocks/physics.zrb'))
    recs = {r.name: r for r in z.records if r.type == T_COLLISION}
    meshes = {h: col.read_collision(z, r) for h, r in recs.items()}
    t_dec = time.time() - t0
    pc = col.placement_collisions(lib.archive(ai).read(lv['clv']), set(recs))
    msgs, ok = [], True
    if len(pc) != len(lv['mesh']):
        return 'placement count %d != index %d' % (len(pc), len(lv['mesh'])), False
    m2c = collections.defaultdict(set)
    for m, c in zip(lv['mesh'].tolist(), pc):
        m2c[m].add(c)
    amb = sum(len(s) > 1 for s in m2c.values())
    ok &= amb == 0
    used = set(pc) - {0}
    square = lvl.terrain_square([lib.model_header(zi, ri).bounds for zi, ri in lv['terrain_lods'][-1]])
    tr = col.terrain_record(meshes, used, square)
    ok &= tr is not None
    ntri = sum(len(m.triangles) for m in meshes.values())
    msgs.append('%3d records %6d tris (%.1fs), %5d/%5d placements with collision, %d ambiguous meshes'
                % (len(recs), ntri, t_dec, sum(1 for c in pc if c), len(pc), amb))
    # pieces
    rel = []
    for mh, cs in m2c.items():
        c = next(iter(cs))
        if not c:
            continue
        hit = lib.index.find_in_archive(mh, MODEL_TYPES, ai)
        if hit is None:
            continue
        _, geoms = lib.model(hit[1], hit[2])
        P = np.concatenate([g.positions for g in geoms if g is not None]).astype(float)
        C = meshes[c].positions.astype(float)
        if not len(C) or not len(P):
            continue
        C = C[rng.choice(len(C), min(len(C), 150), replace=False)]
        P = P[rng.choice(len(P), min(len(P), 3000), replace=False)]
        rel.append(np.median(nearest(C, P)) / max(float(np.ptp(P, 0).max()), 1e-3))
    med = float(np.median(rel)) if rel else float('nan')
    ok &= bool(rel) and med < 0.01
    msgs.append('pieces: %d pairs, median rel. distance %.2e (90th pct %.2e)'
                % (len(rel), med, np.percentile(rel, 90) if rel else float('nan')))
    # terrain
    if tr is not None:
        ref = []
        for zi, ri in lv['terrain_lods'][0]:
            _, geoms = lib.model(zi, ri)
            ref.extend(g.positions for g in geoms if g is not None)
        ref = np.concatenate(ref).astype(float)
        T = meshes[tr].positions.astype(float)
        T = T[rng.choice(len(T), min(len(T), 3000), replace=False)]
        dh = nearest_xz_height(T, ref)
        dh = dh[np.isfinite(dh)]
        tmed = float(np.median(dh)) if len(dh) else float('nan')
        ok &= len(dh) > 0 and tmed < 0.5
        msgs.append('terrain %08x: %d tris, height vs leaf tiles median %.3f m, 95th pct %.3f m'
                    % (tr, len(meshes[tr].triangles), tmed, np.percentile(dh, 95) if len(dh) else float('nan')))
    else:
        msgs.append('terrain collision NOT found')
    return '\n      '.join(msgs), ok


def main():
    lib = _offline.open_library()
    want = [a.lower() for a in sys.argv[1:]]
    rng = np.random.default_rng(1)
    bad = 0
    for lv in lib.index.levels:
        if want and not any(w in lv['name'] for w in want):
            continue
        msg, ok = check_level(lib, lv, rng)
        bad += not ok
        print('%-32s %s  %s' % (lv['name'], 'ok  ' if ok else 'FAIL', msg))
    print('collision_check: %s' % ('all ok' if not bad else '%d level(s) failed' % bad))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
