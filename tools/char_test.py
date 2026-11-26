"""Import a character, render rest pose and an animated frame.

blender --background --factory-startup --python tools/char_test.py -- ISO OUTDIR char_label anim_label frame [eevee]

``eevee`` renders with EEVEE (materials, alpha) and a sun instead of Workbench's texture colours.
"""
import math
import os
import sys

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index('--') + 1:]
ISO, OUT, CHAR, ANIM, FRAME = argv[0], argv[1], argv[2], argv[3], int(argv[4])
EEVEE = 'eevee' in argv[5:]
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import mag_importer  # noqa: E402
mag_importer.register()
from mag_importer.blender import state, ui  # noqa: E402

assert state.try_load_cached(ISO)
for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob)
wm = bpy.context.window_manager
scene = bpy.context.scene


def imp(cat, label):
    wm.mag.category = cat
    ui._refresh_list()
    i = next(i for i, it in enumerate(wm.mag.items) if it.label == label)
    wm.mag.active = i
    print('IMPORT', bpy.ops.mag.import_selected())


imp('CHARACTER', CHAR)
rig = next(o for o in scene.objects if o.type == 'ARMATURE')
bpy.context.view_layer.objects.active = rig
if EEVEE:
    scene.render.engine = 'BLENDER_EEVEE'
    sun = bpy.data.objects.new('sun', bpy.data.lights.new('sun', 'SUN'))
    sun.data.energy = 3.0
    sun.rotation_euler = (math.radians(50), 0.0, math.radians(30))
    scene.collection.objects.link(sun)
    if scene.world is None:
        scene.world = bpy.data.worlds.new('World')
    bg = scene.world.node_tree.nodes.get('Background')
    bg.inputs['Color'].default_value = (0.35, 0.37, 0.4, 1.0)
else:
    scene.render.engine = 'BLENDER_WORKBENCH'
    scene.display.shading.color_type = 'TEXTURE'
scene.render.resolution_x = scene.render.resolution_y = 512
cam = bpy.data.objects.new('cam', bpy.data.cameras.new('cam'))
scene.collection.objects.link(cam)
scene.camera = cam


def shot(name, front=True):
    deps = bpy.context.evaluated_depsgraph_get()
    pts = []
    for ob in scene.objects:
        if ob.type == 'MESH':
            ev = ob.evaluated_get(deps)
            m = ev.to_mesh()
            pts += [ev.matrix_world @ v.co for i, v in enumerate(m.vertices) if i % 7 == 0]
            ev.to_mesh_clear()
    mn = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    mx = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    c = (mn + mx) / 2
    r = (mx - mn).length / 2
    d = r / math.tan(cam.data.angle / 2) * 1.05
    dirv = Vector((0.3, -1.0, 0.15)).normalized() if front else Vector((1.0, 0.0, 0.1)).normalized()
    cam.location = c + dirv * d
    cam.rotation_euler = (c - cam.location).to_track_quat('-Z', 'Y').to_euler()
    scene.render.filepath = os.path.join(OUT, name)
    bpy.ops.render.render(write_still=True)
    print('SHOT', name, 'bbox', tuple(round(x, 2) for x in mn), tuple(round(x, 2) for x in mx))


shot('rest_front.png')
shot('rest_side.png', False)
imp('ANIMATION', ANIM)
scene.frame_set(FRAME)
shot('anim_front.png')

