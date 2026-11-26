"""Check every map placement against the transform the level compiler stores.

usage: python tools/placement_check.py [level ...]

After each ``InstanceProperties`` object of a placed model, the ``.clv`` holds the compiled runtime
transform: an xyzw quaternion, the translation and one uniform scale (f32, big-endian). This script
finds that block (by the translation) and compares the full world matrix built from the indexed
TRS (``level.trs_matrix``, what the importer uses) with the one built from the compiled block.
Placements without a model (particle emitters) compile to a 4x4 matrix instead and are skipped.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _offline  # noqa: E402
from mag_importer.core import level as lvl, props  # noqa: E402


def quat_matrix(x, y, z, w):
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def check_level(lib, lv):
    data = lib.archive(lv['archive']).read(lv['clv'])
    objs = [o for o in props.iter_objects(data, type_name='InstanceProperties') if o.token('Mesh')]
    if len(objs) != len(lv['mesh']):
        return 'placement count %d != index %d' % (len(objs), len(lv['mesh'])), False
    worst_rel, worst_t, checked, nofind, scaled = 0.0, 0.0, 0, 0, 0
    for o, trs in zip(objs, lv['trs']):
        t = b''.join(o.fields['Translate' + a][1][:4] for a in 'XYZ')
        j = data.find(t, o.end, o.end + 4000)
        if j < 0:
            nofind += 1
            continue
        q = np.frombuffer(data[j - 16:j], '>f4').astype(float)
        s = float(np.frombuffer(data[j + 12:j + 16], '>f4')[0])
        if abs(np.linalg.norm(q) - 1) > 1e-3:
            nofind += 1
            continue
        ref = quat_matrix(*q) * s
        got = lvl.trs_matrix(trs)
        worst_rel = max(worst_rel, float(np.abs(got[:3, :3] - ref).max()) / max(1.0, s))
        worst_t = max(worst_t, float(np.abs(got[:3, 3] - np.frombuffer(t, '>f4')).max()))
        checked += 1
        scaled += abs(s - 1) > 1e-4
    ok = worst_rel < 2e-3 and worst_t < 1e-3
    return ('%5d checked (%4d scaled), %3d without compiled block; worst rotation*scale error %.1e '
            '(relative), translation %.1e m' % (checked, scaled, nofind, worst_rel, worst_t)), ok


def main():
    lib = _offline.open_library()
    want = [a.lower() for a in sys.argv[1:]]
    bad = 0
    for lv in lib.index.levels:
        if want and not any(w in lv['name'] for w in want):
            continue
        msg, ok = check_level(lib, lv)
        bad += not ok
        print('%-32s %s  %s' % (lv['name'], 'ok  ' if ok else 'FAIL', msg))
    print('placement_check: %s' % ('all ok' if not bad else '%d level(s) failed' % bad))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
