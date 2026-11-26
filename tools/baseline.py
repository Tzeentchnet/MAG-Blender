"""Regression baselines: per-map counts and per-model decode digests, compared with a stored copy.

usage: python tools/baseline.py [--write] [--no-models] [--file PATH] [level ...]

Without ``--write`` it recomputes everything and lists every difference from the stored baseline
(``tools/baselines/baseline.json`` by default); the exit code is 1 when something changed.
``--write`` stores the current results instead (merged into the file, so a run limited to some
levels or without models keeps the other entries).

Per level (offline, no Blender):

* placements, unique pieces, particle emitters and missing pieces (as in the import report);
* terrain tile count of every terrain LOD (finest first) and the leaf count;
* backdrop placements (world box centre more than ``BACKDROP_MARGIN`` outside the terrain square,
  the same rule as ``build.build_map``);
* foliage batches, table entries, models and instances; the sky dome piece hash.
* baked lighting records, placements that name one, and records whose length fits the placed
  model's vertex-lit meshes.

Materials (whole disc, skipped when levels are named): counts of the decoded header features
(alpha-tested, translucent, water, glass, terrain, UV transforms, detail scales), materials per
technique, and a digest of every material's decode (header fields, bindings, constants).

Per ZRB: a digest of every decoded model in it (positions, normals, UVs, colours, indices,
joints, weights, rounded to 1e-4), plus counts of models, meshes and decode failures. A digest
change names the ZRB and the first model whose geometry changed.
"""
import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _offline  # noqa: E402
from mag_importer.core import catalog, foliage, level as lvl, lighting, materials, models, sky  # noqa: E402
from mag_importer.core.zrb import MODEL_TYPES, T_FOLIAGE_BATCH, T_LIGHTING, T_MATERIAL  # noqa: E402

DEFAULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'baselines', 'baseline.json')


# -- levels ---------------------------------------------------------------------------------------
def level_stats(lib, lv):
    idx = lib.index
    ai = lv['archive']
    mesh = [int(h) for h in lv['mesh']]
    hits = {h: idx.find_in_archive(h, MODEL_TYPES, ai) for h in set(mesh)}
    emit = sum(1 for h in mesh if hits[h] is None and h in idx.fx)
    miss = sum(1 for h in mesh if hits[h] is None and h not in idx.fx)
    lods = lv.get('terrain_lods') or []
    out = dict(placements=len(mesh), pieces=sum(1 for v in hits.values() if v is not None),
               emitters=emit, missing=miss, terrain_leaves=len(lv.get('terrain') or []),
               terrain_lods=[len(l) for l in lods])
    # backdrop, as build.build_map decides it
    coarse = lods[-1] if lods else (lv.get('terrain') or [])
    backdrop = 0
    if coarse:
        square = lvl.terrain_square([lib.model_header(zi, ri).bounds for zi, ri in coarse])
        centre = {}
        for h, hit in hits.items():
            if hit is None:
                continue
            m = lib.model_header(hit[1], hit[2])
            centre[h] = np.array(m.bounds[:3], float) if m.meshes else None
        for h, trs in zip(mesh, lv['trs']):
            c = centre.get(h)
            if c is None:
                continue
            G = lvl.trs_matrix(trs)
            w = G[:3, :3] @ c + G[:3, 3]
            backdrop += bool(lvl.outside_distance(w[0], w[2], square) > lvl.BACKDROP_MARGIN)
    out['backdrop'] = backdrop
    # foliage
    recs = [(zi, ri) for zi, z in enumerate(idx.zrbs) if z.archive == ai
            for ri, r in enumerate(z.records) if r[7] == T_FOLIAGE_BATCH]
    batches = [foliage.read_batch(*lib.record(zi, ri)) for zi, ri in recs]
    table = {}
    if batches:
        table = foliage.batch_table(lib.archive(ai).read(lv['clv']), batches[0].name)
    placed, lost = foliage.level_foliage(batches, table) if batches else ({}, 0)
    out['foliage'] = dict(batches=len(batches), table=len(table), unplaced=lost,
                          models={'%08x' % m: len(p.scales) for m, p in sorted(placed.items())})
    # baked lighting: records, placements naming one, and records whose length fits the model
    lrecs = {r[8]: (zi, ri) for zi, z in enumerate(idx.zrbs) if z.archive == ai
             for ri, r in enumerate(z.records) if r[7] == T_LIGHTING}
    per = lighting.placement_lighting(lib.archive(ai).read(lv['clv']), set(lrecs))
    flags, sizes, fits = {}, {}, 0

    def mflags(h, zi):
        if h not in flags:
            m = lib.material(h, zi)
            flags[h] = m[0].flags if m else None
        return flags[h]
    for h, rh in zip(mesh, per):
        if rh and hits.get(h) is not None:
            if h not in sizes:
                zi, ri = hits[h][1:]
                sizes[h] = lighting.lit_offsets(lib.model_header(zi, ri), lambda mh: mflags(mh, zi))[1]
            z, r = lib.record(*lrecs[rh])
            fits += len(lighting.read_lighting(z, r)) == sizes[h]
    out['lighting'] = dict(records=len(lrecs), placements=sum(1 for h in per if h), fit=fits)
    sp = sky.sky_piece(idx, ai)
    out['sky'] = '%08x' % idx.zrbs[sp[0]].records[sp[1]][8] if sp else None
    return out


# -- models ---------------------------------------------------------------------------------------
def _feed(h, a):
    if a is None:
        h.update(b'-')
        return
    a = np.asarray(a)
    if a.dtype.kind == 'f':
        a = np.nan_to_num(np.round(a.astype(np.float64) * 1e4), nan=-2.0 ** 62, posinf=2.0 ** 62,
                          neginf=-2.0 ** 61).clip(-2.0 ** 62, 2.0 ** 62).astype(np.int64)
    else:
        a = a.astype(np.int64)
    h.update(str(a.shape).encode())
    h.update(np.ascontiguousarray(a).tobytes())


def model_digest(z, r):
    """(digest hex, meshes, failed meshes) of one model record; digest None if it doesn't parse."""
    try:
        m = models.parse_model(z.payload(r), r.type, r.name)
    except Exception:  # noqa: BLE001 - a parse failure is a result to record
        return None, 0, 0
    h = hashlib.sha1()
    _feed(h, np.array(m.bounds))
    fails = 0
    for g in models.decode_model_geometry(z, r, m):
        if g is None:
            fails += 1
            h.update(b'fail')
            continue
        for a in (g.positions, g.normals, g.colors, g.indices, g.joints, g.weights):
            _feed(h, a)
        for uv in g.uvs:
            _feed(h, uv)
    return h.hexdigest()[:16], len(m.meshes), fails


def zrb_models(lib, zi):
    z = lib.zrb(zi)
    per = {}
    meshes = fails = bad = 0
    for r in z.records:
        if r.type not in MODEL_TYPES:
            continue
        d, n, f = model_digest(z, r)
        per['%08x' % r.name] = d
        meshes += n
        fails += f
        bad += d is None
    agg = hashlib.sha1(json.dumps(sorted(per.items())).encode()).hexdigest()[:16]
    return dict(models=len(per), meshes=meshes, mesh_fails=fails, parse_fails=bad, digest=agg,
                per_model=per)


def material_stats(lib):
    tex = lib.texture_hashes()
    seen, per = set(), {}
    for zi, z in enumerate(lib.index.zrbs):
        for ri, r in enumerate(z.records):
            if r[7] == T_MATERIAL and r[8] not in seen:
                seen.add(r[8])
                zz, rr = lib.record(zi, ri)
                per[r[8]] = materials.parse_material(zz.payload(rr), r[8], tex)
    techs = {}
    h = hashlib.sha1()
    for name in sorted(per):
        m = per[name]
        techs['%08x' % m.technique] = techs.get('%08x' % m.technique, 0) + 1
        h.update(json.dumps([name, m.parsed, m.technique, m.render, m.group, m.alpha_ref, m.flags,
                             sorted(m.bindings.items()),
                             [(c.mask, c.dest, c.type, [round(v, 4) for v in c.values]) for c in m.constants]]).encode())
    ms = list(per.values())
    return dict(count=len(ms), parsed=sum(m.parsed for m in ms), alpha_tested=sum(m.alpha_test for m in ms),
                translucent=sum(m.translucent for m in ms), water=sum(m.water for m in ms),
                glass=sum(m.glass for m in ms), terrain=sum(m.terrain is not None for m in ms),
                uv_transforms=sum(bool(m.uv_transform()) for m in ms),
                detail_scales=sum(bool(m.detail_scale(s)) for m in ms for s in (16, 17)),
                techniques=techs, digest=h.hexdigest()[:16])


# -- compare --------------------------------------------------------------------------------------
def diff(old, new, path=''):
    """Lines describing every difference between two JSON-like values."""
    if isinstance(old, dict) and isinstance(new, dict):
        out = []
        for k in sorted(set(old) | set(new)):
            p = '%s.%s' % (path, k) if path else k
            if k not in old:
                out.append('%s: new %s' % (p, _short(new[k])))
            elif k not in new:
                out.append('%s: gone (was %s)' % (p, _short(old[k])))
            else:
                out.extend(diff(old[k], new[k], p))
        return out
    return [] if old == new else ['%s: %s -> %s' % (path, _short(old), _short(new))]


def _short(v):
    s = json.dumps(v)
    return s if len(s) < 80 else s[:77] + '...'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('levels', nargs='*')
    ap.add_argument('--write', action='store_true')
    ap.add_argument('--no-models', action='store_true')
    ap.add_argument('--file', default=DEFAULT_FILE)
    a = ap.parse_args()
    lib = _offline.open_library()
    want = [w.lower() for w in a.levels]
    cur = dict(index_version=catalog.INDEX_VERSION, levels={}, zrbs={}, materials={})
    t0 = time.time()
    levels = [lv for lv in lib.index.levels if not want or any(w in lv['name'] for w in want)]
    for lv in levels:
        cur['levels'][lv['name']] = level_stats(lib, lv)
        print('level %-30s %s' % (lv['name'], {k: v for k, v in cur['levels'][lv['name']].items()
                                               if k not in ('foliage', 'terrain_lods')}))
    if not want:
        cur['materials'] = material_stats(lib)
        print('materials %s' % {k: v for k, v in cur['materials'].items() if k != 'techniques'})
    t1 = time.time()
    if not a.no_models:
        arch = {lv['archive'] for lv in levels} if want else None
        zis = [zi for zi, z in enumerate(lib.index.zrbs)
               if any(r[7] in MODEL_TYPES for r in z.records) and (arch is None or z.archive in arch)]

        def job(zi):
            res = zrb_models(lib, zi)
            lib._zrbs.pop(zi, None)  # noqa: SLF001 - one pass over the disc, don't keep them
            return zi, res
        with ThreadPoolExecutor(max_workers=8) as ex:
            for zi, res in ex.map(job, zis):
                z = lib.index.zrbs[zi]
                key = '%s:%s' % (lib.index.archives[z.archive][0].rsplit('/', 1)[-1], z.name)
                cur['zrbs'][key] = res
        n = sum(v['models'] for v in cur['zrbs'].values())
        f = sum(v['mesh_fails'] + v['parse_fails'] for v in cur['zrbs'].values())
        print('models: %d in %d ZRBs, %d failures (%.0fs)' % (n, len(cur['zrbs']), f, time.time() - t1))
    print('levels took %.0fs' % (t1 - t0))

    stored = {}
    if os.path.exists(a.file):
        with open(a.file) as fh:
            stored = json.load(fh)
    if a.write:
        stored['index_version'] = cur['index_version']
        stored.setdefault('levels', {}).update(cur['levels'])
        stored.setdefault('zrbs', {}).update(cur['zrbs'])
        if cur['materials']:
            stored['materials'] = cur['materials']
        os.makedirs(os.path.dirname(a.file), exist_ok=True)
        with open(a.file, 'w') as fh:
            json.dump(stored, fh, indent=1, sort_keys=True)
        print('baseline written: %s' % a.file)
        return 0
    if not stored:
        print('no baseline at %s (run with --write)' % a.file)
        return 1
    old_z = stored.get('zrbs', {})
    if a.no_models:
        old_z = {}
    elif want:  # only the ZRBs of the chosen levels were decoded
        old_z = {k: v for k, v in old_z.items() if k in cur['zrbs']}
    old_l = stored.get('levels', {})
    if want:
        old_l = {k: v for k, v in old_l.items() if k in cur['levels']}
    old_m = stored.get('materials', {}) if cur['materials'] else {}
    old = dict(levels=old_l, zrbs=old_z, materials=old_m)
    lines = diff(old, dict(levels=cur['levels'], zrbs=cur['zrbs'], materials=cur['materials']))
    if stored.get('index_version') != cur['index_version']:
        print('note: baseline index version %s, current %s' % (stored.get('index_version'), cur['index_version']))
    for l in lines[:200]:
        print('  CHANGED ' + l)
    if len(lines) > 200:
        print('  ... %d more' % (len(lines) - 200))
    print('baseline: %s' % ('no changes' if not lines else '%d difference(s)' % len(lines)))
    return 1 if lines else 0


if __name__ == '__main__':
    sys.exit(main())
