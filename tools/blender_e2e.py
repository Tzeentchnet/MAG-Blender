"""End-to-end test inside Blender (background).

blender --background --factory-startup --python tools/blender_e2e.py -- ISO OUTDIR [map_name]
"""
import os
import sys
import time

import bpy

argv = sys.argv[sys.argv.index('--') + 1:]
ISO, OUT = argv[0], argv[1]
MAP = argv[2] if len(argv) > 2 else ''
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

import mag_importer  # noqa: E402
mag_importer.register()
from mag_importer.blender import state, ui  # noqa: E402
from mag_importer.blender.state import S  # noqa: E402

state.save_settings(iso_path=ISO)
t0 = time.time()
if not state.try_load_cached(ISO):
    print('E2E: scanning...')
    state.start_scan(ISO)
    S.scan_thread.join()
    assert S.scan_result is not None, S.scan_error
    state.set_index(S.scan_result, ISO)
print('E2E: index ready in %.1fs, %d assets, cache %s' % (time.time() - t0, len(S.index.assets), state.cache_dir()))

# clean scene
for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob)

wm = bpy.context.window_manager


def import_by(category, pred, tag):
    wm.mag.category = category
    ui._refresh_list()
    idx = next((i for i, it in enumerate(wm.mag.items) if pred(it)), None)
    if idx is None:
        print('E2E: %s not found' % tag)
        return None
    wm.mag.active = idx
    t = time.time()
    res = bpy.ops.mag.import_selected()
    print('E2E: %-10s %-40s %s %.2fs  objects=%d' % (category, wm.mag.items[idx].label, res, time.time() - t, len(bpy.data.objects)))
    return wm.mag.items[idx].label


tests = [
    ('WEAPON', lambda it: it.label == 'm4a1' or it.label.startswith('m4a1'), 'm4a1'),
    ('CHARACTER', lambda it: 'heavy_armor_green' == it.label, 'heavy armor'),
    ('VEHICLE', lambda it: 'hind' in it.detail, 'hind'),
    ('ATTACHMENT', lambda it: 'scope' in it.label, 'scope'),
    ('EQUIPMENT', lambda it: 'medkit' in it.label, 'medkit'),
    ('ANIMATION', lambda it: it.label.startswith('stand_l_aim_walk'), 'walk anim'),
    ('ANIMATION', lambda it: it.label.startswith('fps_reload_m4a1'), 'fps anim'),
]
if MAP:
    tests.append(('MAP', lambda it: it.label == MAP, MAP))
for cat, pred, tag in tests:
    import_by(cat, pred, tag)

stats = dict(objects=len(bpy.data.objects), meshes=len(bpy.data.meshes), materials=len(bpy.data.materials),
             images=len(bpy.data.images), actions=len(bpy.data.actions), armatures=len(bpy.data.armatures))
print('E2E: totals', stats)
for a in bpy.data.actions:
    print('E2E: action', a.name, 'slots', len(a.slots), 'range', tuple(a.frame_range))
for ob in bpy.data.objects:
    if ob.type == 'MESH' and ob.vertex_groups:
        print('E2E: skinned', ob.name, 'verts', len(ob.data.vertices), 'groups', len(ob.vertex_groups), 'parent', ob.parent and ob.parent.name)
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(OUT, 'e2e.blend'))
print('E2E: saved')

