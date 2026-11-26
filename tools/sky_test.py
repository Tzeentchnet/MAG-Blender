"""Build a map's sky both ways and check they agree.

blender --background --factory-startup --python tools/sky_test.py -- ISO OUTDIR map_label [map_label ...]

For each map: build the sky as the scene World and render four views from the map origin with
EEVEE (zenith, and the horizon looking north, east and up at 30 degrees), then build it as the
Dome mesh (unlit emission, World black) and render the same views. The World maps the view
direction through the dome's elevation profile, the Dome uses the per-vertex planar UVs; the
renders must agree where the dome covers the view (mean absolute difference per channel below
0.02, or 0.04 for the two horizon views). Also checks the sky
World's Float Curve against ``sky.elevation_profile``. Renders go to OUTDIR/sky_<map>_<view>_<mode>.png.
"""
import math
import os
import sys
import time

import bpy
import numpy as np
from mathutils import Euler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _testutil import ensure_index  # noqa: E402

argv = sys.argv[sys.argv.index('--') + 1:]
ISO, OUT, MAPS = argv[0], argv[1], argv[2:] or ['baku']
os.makedirs(OUT, exist_ok=True)
S = ensure_index(ISO)
from mag_importer.blender import build  # noqa: E402
from mag_importer.core import sky  # noqa: E402

scene = bpy.context.scene
for ob in list(bpy.data.objects):  # the factory cube would sit around the camera
    bpy.data.objects.remove(ob)
scene.render.engine = 'BLENDER_EEVEE'
scene.render.resolution_x, scene.render.resolution_y = 480, 270
scene.view_settings.view_transform = 'Standard'
cam = bpy.data.objects.new('cam', bpy.data.cameras.new('cam'))
cam.data.clip_start, cam.data.clip_end = 1.0, 10000.0
cam.data.lens = 18.0
scene.collection.objects.link(cam)
scene.camera = cam
VIEWS = (('zenith', (math.pi, 0.0, 0.0)), ('north', (math.radians(95), 0.0, 0.0)),
         ('east', (math.radians(95), 0.0, math.radians(-90))), ('up30', (math.radians(120), 0.0, math.radians(45))))


def render(path):
    scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    img = bpy.data.images.load(path, check_existing=False)
    px = np.empty(len(img.pixels), np.float32)
    img.pixels.foreach_get(px)
    bpy.data.images.remove(img)
    return px.reshape(-1, 4)[:, :3]


fails = 0
for label in MAPS:
    li = next(i for i, l in enumerate(S.index.levels) if l['name'].replace('mag_', '').replace('_final', '') == label)
    lv = S.index.levels[li]
    shots = {}
    for mode in ('WORLD', 'DOME'):
        for ob in [o for o in scene.objects if o.get('mag_sky')]:
            bpy.data.objects.remove(ob)
        scene.world = bpy.data.worlds.new('black')  # 5.x: always a node tree, 0.05 grey by default
        scene.world.use_nodes = True
        bg = next(n for n in scene.world.node_tree.nodes if n.type == 'BACKGROUND')
        bg.inputs['Color'].default_value = (0, 0, 0, 1)
        opts = build.ImportOptions(sky=mode)
        mcache = build.MaterialCache(S.library, opts)
        coll = bpy.data.collections.new('test_' + mode)
        scene.collection.children.link(coll)
        t = time.time()
        what = build.build_sky(S.library, lv, label, opts, mcache, scene, coll)
        print('SKY %s %s -> %s (%.2fs)' % (label, mode, what, time.time() - t))
        if mode == 'WORLD':
            zi, ri = sky.sky_piece(S.index, lv['archive'])
            m, g = S.library.model(zi, ri)
            pos = np.concatenate([x.positions for x in g if x is not None])
            prof = sky.elevation_profile(pos)
            fc = scene.world.node_tree.nodes['Sky Profile']
            # evaluate the curve at the profile points: must reproduce the profile
            fc.mapping.initialize()
            cv = fc.mapping.curves[0]
            err = max(abs(fc.mapping.evaluate(cv, (e + 90) / 180) * build.SKY_YMAX - s) for e, s in prof)
            print('SKY %s curve points %d (profile %d) max error %.4f' % (label, len(cv.points), len(prof), err))
            fails += err > 0.01
        for view, rot in VIEWS:
            cam.location = (0.0, 0.0, 0.0)
            cam.rotation_euler = Euler(rot)
            shots[(view, mode)] = render(os.path.join(OUT, 'sky_%s_%s_%s.png' % (label, view, mode.lower())))
    for view, _ in VIEWS:
        a, b = shots[(view, 'WORLD')], shots[(view, 'DOME')]
        cover = b.max(1) > 0.0  # below its skirt the dome shows the black World
        diff = np.abs(a[cover] - b[cover]).mean(0)
        # the horizon views see the dome's rim ring, which varies ~3 % in radius with azimuth (one
        # elevation profile cannot follow it; Kennicott's rim is a hard mountain silhouette)
        limit = 0.04 if view in ('north', 'east') else 0.02
        ok = diff.max() < limit and a.mean() > 0.02 and cover.mean() > 0.5
        fails += not ok
        print('SKY %s %-6s covered %3d%%  world mean %s dome mean %s  |diff| %s -> %s' % (
            label, view, round(100 * cover.mean()), a[cover].mean(0).round(3), b[cover].mean(0).round(3),
            diff.round(4), 'OK' if ok else 'FAIL'))
print('SKY result: %s' % ('FAIL (%d)' % fails if fails else 'OK'))
