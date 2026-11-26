"""Verify Blender pose = game-space animated skeleton (converted Y-up -> Z-up).

blender --background --factory-startup --python tools/anim_check.py -- ISO anim_label frame
"""
import os
import sys

import bpy
import numpy as np

argv = sys.argv[sys.argv.index('--') + 1:]
ISO, ANIM, FRAME = argv[0], argv[1], int(argv[2])
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import mag_importer  # noqa: E402
mag_importer.register()
from mag_importer.blender import state, ui, build  # noqa: E402
from mag_importer.blender.state import S  # noqa: E402

assert state.try_load_cached(ISO)
for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob)
wm = bpy.context.window_manager
wm.mag.category = 'ANIMATION'
ui._refresh_list()
i = next(i for i, it in enumerate(wm.mag.items) if it.label.startswith(ANIM))
wm.mag.active = i
print('IMPORT', bpy.ops.mag.import_selected())
asset = next(a for a in S.index.assets if a.key == wm.mag.items[i].key)
an = S.library.animations(asset.zrb, asset.rec)[asset.extra or 0]
sk = S.library.skeleton(an.skeleton_name)
arm = next(o for o in bpy.context.scene.objects if o.type == 'ARMATURE')
bpy.context.scene.frame_set(1 + FRAME)
f = FRAME
T = sk.translations.astype(np.float64).copy()
R = sk.rotations.astype(np.float64).copy()
for t, b in enumerate(an.track_to_bone):
    T[b] = an.translations[t, f]
    R[b] = an.rotations[t, f]
n = len(sk.bones)
M = [None] * n
P = np.zeros((n, 3))
for k in range(n):
    p = sk.parents[k]
    r = build.qmat(R[k])
    if p < 0:
        M[k], P[k] = r, T[k]
    else:
        M[k], P[k] = M[p] @ r, P[p] + M[p] @ T[k]
Pb = build.to_blender_vec(P)
names = list(arm['mag_bones'])
err = []
for k, nm in enumerate(names):
    pb = arm.pose.bones[nm]
    h = np.array(arm.matrix_world @ pb.head)
    err.append(np.linalg.norm(h - Pb[k]))
err = np.array(err)
print('POSECHECK max err %.5f mean %.5f (frame %d, %d bones)' % (err.max(), err.mean(), FRAME, n))
for k in ('head', 'l_wrist', 'r_wrist', 'l_ankle'):
    if k in names:
        j = names.index(k)
        print('POSECHECK', k, 'game', P[j].round(3), 'blender', np.array(arm.pose.bones[k].head).round(3))
