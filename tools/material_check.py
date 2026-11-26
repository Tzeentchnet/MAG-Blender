"""Check the material record decode on every material of the disc (offline, no Blender).

usage: python tools/material_check.py [--models] [--techniques]

* every material parses structurally (``MaterialInfo.parsed``), and its binding table, without
  textures that are not on the disc (slot 18's environment cube, a few others), equals what the
  old hash scan finds;
* the header's binding count (+0x1D) equals the table length, the alpha-test bit equals a
  nonzero alpha reference, and group bit 0x40 equals the VERTEX_LIT flag;
* terrain materials give the same layers, UV rows and specular rows as the fixed-offset reading;
* water materials bind a normal map and no colour; glass materials are translucent;
* every technique in ``materials.TECHNIQUES`` has its UV parameter (four floats, mostly identity)
  on every one of its materials, and a detail parameter of two floats wherever it has one (a few
  materials bind a detail map but have no parameter for it; the importer leaves those unused);
* ``--models`` also reads every model header: group bit 0x20 must be set exactly on materials of
  skinned meshes, and a mesh's vertex format must be a subset of its material's vertex attributes
  (+0x28; level pieces drop bit 0x001, vertex-lit ones more; foliage materials have 0). Takes a
  few minutes.
* ``--techniques`` prints a table of the techniques.

Exit code 1 if any check fails.
"""
import collections
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _offline  # noqa: E402
from mag_importer.core import materials as mt  # noqa: E402
from mag_importer.core.zrb import T_MATERIAL, T_MODEL, T_SKINNED_MODEL, T_LEVEL_MODEL, T_FOLIAGE_MODEL  # noqa: E402

MODEL_KINDS = {T_MODEL: 'model', T_SKINNED_MODEL: 'skinned', T_LEVEL_MODEL: 'level', T_FOLIAGE_MODEL: 'foliage'}


def load_materials(lib):
    out = {}
    for zi, z in enumerate(lib.index.zrbs):
        for ri, r in enumerate(z.records):
            if r[7] == T_MATERIAL and r[8] not in out:
                zz, rr = lib.record(zi, ri)
                out[r[8]] = bytes(zz.payload(rr))
    return out


def main():
    lib = _offline.open_library()
    tex = lib.texture_hashes()
    payloads = load_materials(lib)
    fails = collections.Counter()
    info = {}
    for h, p in payloads.items():
        m = mt.parse_material(p, h, tex)
        info[h] = m
        if not m.parsed:
            fails['structure does not parse'] += 1
            continue
        if sorted(b for b in m.binding_table if b[0] in tex) != sorted(mt._scan_bindings(p, tex)):
            fails['binding table differs from the scan'] += 1
        if p[0x1D] != len(m.binding_table):
            fails['binding count'] += 1
        if m.alpha_test != (m.alpha_ref != 0):
            fails['alpha test bit vs reference'] += 1
        if bool(m.group & mt.GROUP_VERTEX_LIT) != bool(m.flags & mt.VERTEX_LIT):
            fails['group 0x40 vs VERTEX_LIT'] += 1
        if m.technique == mt.TERRAIN:
            old = mt.MaterialInfo()
            old.parsed, old.technique = False, 0
            every = {}
            for th, ps, slot, cnt, off in m.binding_table:
                if ps == 1:
                    every.setdefault(slot, []).append((off, th))
            t0, t1 = mt._terrain_layers(old, p, every), m.terrain
            if t0 is None or t1 is None or (t0.colors, t0.normals, t0.weights, t0.macro, t0.uv, t0.spec) != \
                    (t1.colors, t1.normals, t1.weights, t1.macro, t1.uv, t1.spec):
                fails['terrain layers differ from the fixed-offset reading'] += 1
        if m.water and (3 in m.bindings or 4 not in m.bindings):
            fails['water binds colour or lacks normal'] += 1
        if m.glass and not m.translucent:
            fails['glass not translucent'] += 1
    by_tech = collections.defaultdict(list)
    for h, m in info.items():
        by_tech[m.technique].append(m)
    unused_detail = 0
    for t, spec in mt.TECHNIQUES.items():
        ms = by_tech.get(t, [])
        if not ms:
            fails['technique %08x not on the disc' % t] += 1
            continue
        uv = [m.constant(spec['uv']) for m in ms]
        if any(v is None or len(v) != 4 for v in uv):
            fails['technique %08x: UV parameter missing' % t] += 1
        elif len(uv) >= 5 and sum(v == (1.0, 1.0, 0.0, 0.0) for v in uv) < 0.6 * len(uv):
            fails['technique %08x: UV parameter mostly not identity' % t] += 1
        for m in ms:
            bound = [s for s in (16, 17) if s in m.bindings]
            for s, d in zip(bound, spec.get('detail', ())):
                v = m.constant(d)
                if v is None:
                    unused_detail += 1  # bound, but the shader variant has no detail parameter
                elif len(v) != 2:
                    fails['technique %08x: detail parameter 0x%x is not two floats' % (t, d)] += 1
    n_uv = sum(1 for m in info.values() if m.uv_transform())
    n_det = sum(1 for m in info.values() for s in (16, 17) if m.detail_scale(s))
    print('%d materials, %d techniques; %d alpha-tested, %d translucent, %d water, %d glass, '
          '%d terrain; %d non-identity UV transforms, %d detail scales (%d detail bindings without one)' % (
              len(info), len(by_tech), sum(m.alpha_test for m in info.values()),
              sum(m.translucent for m in info.values()), sum(m.water for m in info.values()),
              sum(m.glass for m in info.values()), sum(m.terrain is not None for m in info.values()),
              n_uv, n_det, unused_detail))

    if '--models' in sys.argv:
        check_models(lib, info, fails)

    if '--techniques' in sys.argv:
        print('technique  count  group  alpha-tested  slots')
        for t, ms in sorted(by_tech.items(), key=lambda kv: -len(kv[1])):
            groups = collections.Counter('%02x' % m.group for m in ms)
            slots = sorted(set().union(*[m.bindings.keys() for m in ms]))
            print('%08x  %5d  %-12s %4d  %s' % (t, len(ms), ','.join(groups), sum(m.alpha_test for m in ms), slots))

    for k, v in sorted(fails.items()):
        print('FAIL %s: %d' % (k, v))
    print('material_check: %s' % ('FAIL' if fails else 'PASS'))
    return 1 if fails else 0


def check_models(lib, info, fails):
    seen = set()
    exact = collections.Counter()
    skinned_mats, users = set(), collections.defaultdict(set)
    for zi, z in enumerate(lib.index.zrbs):
        for ri, r in enumerate(z.records):
            if r[7] not in MODEL_KINDS or r[8] in seen:
                continue
            seen.add(r[8])
            try:
                mh = lib.model_header(zi, ri)
            except Exception:
                continue
            for me in mh.meshes:
                m = info.get(me.material)
                if m is None:
                    continue
                users[me.material].add(MODEL_KINDS[r[7]])
                if me.skinned:
                    skinned_mats.add(me.material)
                want = m.flags & 0xFFFF
                got = me.vformat & 0xFFFF
                if r[7] != T_FOLIAGE_MODEL and got & ~want:
                    fails['mesh vertex format has bits the material lacks'] += 1
                exact[got == want] += 1
    for h, m in info.items():
        if h in users and bool(m.group & mt.GROUP_SKINNED) != (h in skinned_mats):
            fails['group 0x20 vs skinned meshes'] += 1
    print('%d models checked; %d materials used, %d by skinned meshes; %d of %d meshes have exactly '
          'the material vertex attributes' % (len(seen), len(users), len(skinned_mats), exact[True],
                                                  exact[True] + exact[False]))


if __name__ == '__main__':
    sys.exit(main())
