"""Check the baked per-vertex lighting (84DFB280) of every level against the placements and models.

usage: python tools/lighting_check.py [level ...]

Per level:

* every lighting record of the level archive is named by exactly one model placement (the u32 at
  +166 after its InstanceProperties object, ``lighting.placement_lighting``);
* layout: the record's entry count equals the vertex count of the placed model's vertex-lit meshes
  (``lighting.lit_offsets``). Records that don't match are counted (their layout is unknown);
* seams: in matching records, vertices of one mesh at the same position should carry the same
  (sky, sun) values. The level passes when the share of such pairs that agree is at least 0.4 above
  that of random vertex pairs of the same mesh (a wrong mapping lands near the random rate). It is
  not 1: hard edges (different normals) are lit differently, and boxy maps (raven_sup,
  training_raven) have many of them.
"""
import collections
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _offline  # noqa: E402
from mag_importer.core import lighting as lit  # noqa: E402
from mag_importer.core.zrb import MODEL_TYPES, T_LIGHTING  # noqa: E402


def check_level(lib, lv, rng, flags):
    idx = lib.index
    ai = lv['archive']
    t0 = time.time()
    recs = {r[8]: (zi, ri) for zi, z in enumerate(idx.zrbs) if z.archive == ai
            for ri, r in enumerate(z.records) if r[7] == T_LIGHTING}
    per = lit.placement_lighting(lib.archive(ai).read(lv['clv']), set(recs))
    if len(per) != len(lv['mesh']):
        return 'placement count %d != index %d' % (len(per), len(lv['mesh'])), False
    refs = collections.Counter(h for h in per if h)
    ok = set(refs) == set(recs) and all(v == 1 for v in refs.values())

    def mflags(h, zi):
        if h not in flags:
            out = lib.material(h, zi)
            flags[h] = out[0].flags if out else None
        return flags[h]
    layouts, models = {}, {}
    match = miss = 0
    eq = tot = req = 0
    bad_models = collections.Counter()
    for mh, rh in zip(lv['mesh'].tolist(), per):
        if not rh:
            continue
        hit = idx.find_in_archive(mh, MODEL_TYPES, ai)
        if hit is None:
            miss += 1
            continue
        if mh not in layouts:
            m = lib.model_header(hit[1], hit[2])
            layouts[mh] = lit.lit_offsets(m, lambda h: mflags(h, hit[1]))
        offs, n = layouts[mh]
        z, r = lib.record(*recs[rh])
        data = lit.read_lighting(z, r)
        if len(data) != n or not offs:
            miss += 1
            bad_models[mh] += 1
            continue
        match += 1
        if mh not in models:
            _, geoms = lib.model(hit[1], hit[2])
            pairs = {}
            for mi in offs:
                g = geoms[mi]
                if g is None:
                    continue
                key = collections.defaultdict(list)
                for i, p in enumerate(np.round(g.positions, 4).tolist()):
                    key[tuple(p)].append(i)
                pr = np.array([(ix[0], j) for ix in key.values() for j in ix[1:]], np.int64).reshape(-1, 2)
                if len(pr):
                    pairs[mi] = (pr, len(g.positions))
            models[mh] = pairs
        for mi, (pr, V) in models[mh].items():
            v = data[offs[mi]:offs[mi] + V]
            eq += int((v[pr[:, 0]] == v[pr[:, 1]]).all(1).sum())
            rnd = rng.integers(0, V, len(pr))
            req += int((v[rnd] == v[pr[:, 0]]).all(1).sum())
            tot += len(pr)
    seam = eq / tot if tot else float('nan')
    rand = req / tot if tot else float('nan')
    ok &= bool(tot) and seam - rand >= 0.4
    msg = ('%4d records, %4d placements lit (%d refs off), layout match %4d / %4d (%4.1f %%), '
           'seams %.3f vs random %.3f (%d pairs), %.1fs'
           % (len(recs), sum(refs.values()), sum(v - 1 for v in refs.values()) + len(set(recs) - set(refs)),
              match, match + miss, 100.0 * match / max(match + miss, 1), seam, rand, tot, time.time() - t0))
    if bad_models:
        msg += '\n      unknown layout: ' + ', '.join('%08x x%d' % kv for kv in bad_models.most_common(6))
    return msg, ok


def main():
    lib = _offline.open_library()
    want = [a.lower() for a in sys.argv[1:]]
    rng = np.random.default_rng(1)
    flags = {}
    bad = 0
    for lv in lib.index.levels:
        if want and not any(w in lv['name'] for w in want):
            continue
        msg, ok = check_level(lib, lv, rng, flags)
        bad += not ok
        print('%-32s %s  %s' % (lv['name'], 'ok  ' if ok else 'FAIL', msg))
    print('lighting_check: %s' % ('all ok' if not bad else '%d level(s) failed' % bad))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
