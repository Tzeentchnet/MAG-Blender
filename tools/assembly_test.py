"""Import assembled assets one at a time and render each: vehicles (per-node LODs), weapons
(mounted attachments) and characters (head collection).

blender --background --factory-startup --python tools/assembly_test.py -- ISO OUTDIR [CAT:label ...] [eevee] [lodN]

Without assets it imports VEHICLE:vehicle_hind, VEHICLE:vehicle_hmmwv, WEAPON:m4a1,
"WEAPON:m4a1 (1st person)" and CHARACTER:heavy_armor_green. Each import prints its object tree
(hidden objects marked) and renders <label>.png framed on the visible meshes. ``showall`` also
renders every hidden attachment / head made visible (<label>_all.png); ``frame=<text>`` frames
the shot on the objects whose name contains <text> (with some surroundings); ``pieces`` lists
level pieces too (PROP:<loadblock>_<hash>, for map pieces on their own).
"""
import math
import os
import sys

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index('--') + 1:]
ISO, OUT = argv[0], argv[1]
FLAGS = [a for a in argv[2:] if ':' not in a]
FRAME = next((f[6:] for f in FLAGS if f.startswith('frame=')), '')  # frame on objects named *FRAME*
ASSETS = [a.split(':', 1) for a in argv[2:] if ':' in a] or [
    ('VEHICLE', 'vehicle_hind'), ('VEHICLE', 'vehicle_hmmwv'), ('WEAPON', 'm4a1'),
    ('WEAPON', 'm4a1 (1st person)'), ('CHARACTER', 'heavy_armor_green')]
EEVEE = 'eevee' in FLAGS
LOD = next((int(f[3:]) for f in FLAGS if f.startswith('lod')), 0)
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import mag_importer  # noqa: E402
mag_importer.register()
from mag_importer.blender import state, ui  # noqa: E402

assert state.try_load_cached(ISO)
wm = bpy.context.window_manager
scene = bpy.context.scene
scene.mag.lod = LOD


def clear():
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob)
    for c in list(bpy.data.collections):
        bpy.data.collections.remove(c)


def setup():
    if EEVEE:
        scene.render.engine = 'BLENDER_EEVEE'
        sun = bpy.data.objects.new('sun', bpy.data.lights.new('sun', 'SUN'))
        sun.data.energy = 3.0
        sun.rotation_euler = (math.radians(50), 0.0, math.radians(30))
        scene.collection.objects.link(sun)
        if scene.world is None:
            scene.world = bpy.data.worlds.new('World')
        scene.world.node_tree.nodes.get('Background').inputs['Color'].default_value = (0.35, 0.37, 0.4, 1.0)
    else:
        scene.render.engine = 'BLENDER_WORKBENCH'
        scene.display.shading.color_type = 'TEXTURE'
    scene.render.resolution_x, scene.render.resolution_y = 900, 600
    cam = bpy.data.objects.new('cam', bpy.data.cameras.new('cam'))
    scene.collection.objects.link(cam)
    scene.camera = cam
    return cam


def tree(ob, depth=0):
    hid = ' [hidden]' if ob.hide_get() or ob.hide_render else ''
    extra = ''.join(' %s=%s' % (k, ob[k]) for k in ('mag_node', 'mag_attachment', 'mag_head') if k in ob)
    nv = len(ob.data.vertices) if ob.type == 'MESH' else 0
    print('  ' * depth + '- %s (%s%s)%s%s' % (ob.name, ob.type, ', %d verts' % nv if nv else '', extra, hid))
    for ch in sorted(ob.children, key=lambda o: o.name):
        tree(ch, depth + 1)


def shot(cam, name):
    deps = bpy.context.evaluated_depsgraph_get()
    pts = []
    for ob in scene.objects:
        if ob.type == 'MESH' and ob.visible_get() and not ob.hide_render and FRAME in ob.name:
            ev = ob.evaluated_get(deps)
            m = ev.to_mesh()
            pts += [ev.matrix_world @ v.co for v in m.vertices]
            ev.to_mesh_clear()
    mn = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    mx = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    c = (mn + mx) / 2
    r = max((mx - mn).length / 2, 1.0 if FRAME else 0.0)
    d = r / math.tan(cam.data.angle / 2) * 1.1
    dirv = Vector((0.8, -1.0, 0.35)).normalized()
    cam.location = c + dirv * d
    cam.data.clip_start = d / 1000
    cam.data.clip_end = d * 10
    cam.rotation_euler = (c - cam.location).to_track_quat('-Z', 'Y').to_euler()
    scene.render.filepath = os.path.join(OUT, name)
    bpy.ops.render.render(write_still=True)
    print('SHOT', name)


for cat, label in ASSETS:
    clear()
    cam = setup()
    wm.mag.show_level_pieces = 'pieces' in FLAGS
    wm.mag.category = cat
    ui._refresh_list()
    i = next((i for i, it in enumerate(wm.mag.items) if it.label == label), None)
    if i is None:
        print('MISSING', cat, label)
        continue
    wm.mag.active = i
    print('IMPORT', cat, label, bpy.ops.mag.import_selected())
    for ob in scene.objects:
        if ob.parent is None and ob.type != 'CAMERA' and ob.type != 'LIGHT':
            tree(ob)
    for c in bpy.data.collections:
        print('COLLECTION', c.name, len(c.objects), 'objects')
    safe = label.replace(' ', '_').replace('(', '').replace(')', '')
    shot(cam, safe + '.png')
    if 'showall' in FLAGS:
        for ob in scene.objects:
            if ob.type == 'MESH' and 'mag_head' not in ob:
                ob.hide_set(False)
                ob.hide_render = False
        shot(cam, safe + '_all.png')
