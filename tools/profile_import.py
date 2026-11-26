"""Profile imports inside Blender: a character plus animations, a vehicle and a map.

blender --background --factory-startup --python tools/profile_import.py -- ISO OUTDIR [map_label] [cold] [foliage]

Each stage runs under cProfile; OUTDIR gets <stage>.prof (open with snakeviz / pstats) and
<stage>.txt (top functions by cumulative and own time), plus summary.txt with wall times.
``cold`` points the texture cache at an empty folder first, so DDS conversion is included.
``foliage`` also imports the map's foliage.
"""
import cProfile
import io
import os
import pstats
import shutil
import sys
import tempfile
import time

import bpy

argv = sys.argv[sys.argv.index('--') + 1:]
ISO, OUT = argv[0], argv[1]
MAP = next((a for a in argv[2:] if a not in ('cold', 'foliage')), 'baku')
COLD = 'cold' in argv[2:]
FOLIAGE = 'foliage' in argv[2:]
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

summary = []


def profiled(stage, fn):
    pr = cProfile.Profile()
    t = time.perf_counter()
    pr.enable()
    try:
        res = fn()
    finally:
        pr.disable()
    dt = time.perf_counter() - t
    pr.dump_stats(os.path.join(OUT, stage + '.prof'))
    s = io.StringIO()
    st = pstats.Stats(pr, stream=s)
    s.write('%s: %.2fs wall\n\n=== cumulative ===\n' % (stage, dt))
    st.sort_stats('cumulative').print_stats(45)
    s.write('\n=== own time ===\n')
    st.sort_stats('tottime').print_stats(45)
    with open(os.path.join(OUT, stage + '.txt'), 'w', encoding='utf-8') as f:
        f.write(s.getvalue())
    line = '%-28s %8.2fs  %s' % (stage, dt, res if res is not None else '')
    print('PROFILE ' + line)
    summary.append(line)
    return res


from _testutil import ensure_index  # noqa: E402
import mag_importer  # noqa: E402,F401

# index load (what Blender startup does)
profiled('00_index_load', lambda: '%d assets' % len(ensure_index(ISO).index.assets))
from mag_importer.blender import ui  # noqa: E402
from mag_importer.blender.state import S  # noqa: E402

tmp_tex = None
if COLD:  # empty texture cache: include DDS conversion
    tmp_tex = tempfile.mkdtemp(prefix='mag_tex_')
    S.library.tex_dir = tmp_tex

wm = bpy.context.window_manager
for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob)


def select(cat, pred):
    wm.mag.category = cat
    ui._refresh_list()
    wm.mag.active = next(i for i, it in enumerate(wm.mag.items) if pred(it))
    return wm.mag.items[wm.mag.active].label


def run_import():
    res = bpy.ops.mag.import_selected()
    return '%s objects=%d meshes=%d materials=%d images=%d' % (
        res, len(bpy.data.objects), len(bpy.data.meshes), len(bpy.data.materials), len(bpy.data.images))


select('CHARACTER', lambda it: it.label == 'heavy_armor_green')
profiled('10_character', run_import)
rig = next(o for o in bpy.context.scene.objects if o.type == 'ARMATURE')
anims = ['stand_l_aim_walk', 'fps_reload_m4a1', 'stand_l_idle']
for k, a in enumerate(anims):
    bpy.context.view_layer.objects.active = rig
    try:
        label = select('ANIMATION', lambda it, a=a: it.label.startswith(a))
    except StopIteration:
        print('PROFILE no animation %s' % a)
        continue
    profiled('2%d_anim_%s' % (k, label[:30]), run_import)

select('VEHICLE', lambda it: 'hind' in it.detail)
profiled('30_vehicle_hind', run_import)

for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob)
scene = bpy.context.scene
scene.mag.include_foliage = FOLIAGE
select('MAP', lambda it: it.label == MAP)
profiled('40_map_%s%s' % (MAP, '_foliage' if FOLIAGE else ''), run_import)

# depsgraph evaluation of the imported map (first viewport/render frame)
profiled('50_map_depsgraph', lambda: bpy.context.evaluated_depsgraph_get().update())


def first_render():
    """Small EEVEE frame: shader compilation of every material plus texture upload, which is
    what the first Material Preview / render of the map costs."""
    sc = bpy.context.scene
    sc.render.engine = 'BLENDER_EEVEE'
    sc.render.resolution_x, sc.render.resolution_y = 320, 180
    sc.eevee.taa_render_samples = 1
    if sc.camera is None:
        cam = bpy.data.objects.new('ProfileCam', bpy.data.cameras.new('ProfileCam'))
        sc.collection.objects.link(cam)
        cam.location, cam.rotation_euler = (0, -300, 200), (1.0, 0, 0)
        sc.camera = cam
    sc.render.filepath = os.path.join(OUT, 'first_render.png')
    bpy.ops.render.render(write_still=True)


profiled('55_first_eevee_render', first_render)
profiled('60_save_blend', lambda: bpy.ops.wm.save_as_mainfile(filepath=os.path.join(OUT, 'profile.blend')))

with open(os.path.join(OUT, 'summary.txt'), 'w', encoding='utf-8') as f:
    f.write('ISO %s\nmap %s cold=%s foliage=%s\n\n' % (ISO, MAP, COLD, FOLIAGE))
    f.write('\n'.join(summary) + '\n')
if tmp_tex:
    shutil.rmtree(tmp_tex, ignore_errors=True)
print('PROFILE done ->', OUT)
