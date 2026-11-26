"""Render a preview PNG per top-level collection of the open .blend (Workbench, textured).

blender --background file.blend --python tools/render_previews.py -- OUTDIR [frame]
"""
import math
import os
import sys

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index('--') + 1:]
OUT = argv[0]
FRAME = int(argv[1]) if len(argv) > 1 else 1
os.makedirs(OUT, exist_ok=True)
scene = bpy.context.scene
scene.render.engine = 'BLENDER_WORKBENCH'
scene.display.shading.light = 'STUDIO'
scene.display.shading.color_type = 'TEXTURE'
scene.render.resolution_x = 640
scene.render.resolution_y = 640
scene.render.film_transparent = False
scene.frame_set(FRAME)
cam_data = bpy.data.cameras.new('cam')
cam = bpy.data.objects.new('cam', cam_data)
scene.collection.objects.link(cam)
scene.camera = cam
cam_data.clip_end = 10000


def coll_bbox(coll):
    deps = bpy.context.evaluated_depsgraph_get()
    pts = []
    for ob in coll.all_objects:
        if ob.type != 'MESH':
            continue
        ev = ob.evaluated_get(deps)
        pts += [ev.matrix_world @ Vector(c) for c in ev.bound_box]
    if not pts:
        return None
    mn = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    mx = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return mn, mx


tops = [c for c in scene.collection.children if c.name != 'MAG Assets']
for c in tops:
    for o in tops:
        lc = bpy.context.view_layer.layer_collection.children.get(o.name)
        if lc:
            lc.hide_viewport = (o != c)
            o.hide_render = (o != c)
    bb = coll_bbox(c)
    if bb is None:
        continue
    mn, mx = bb
    ctr = (mn + mx) / 2
    rad = max((mx - mn).length / 2, 0.05)
    d = rad / math.tan(cam_data.angle / 2) * 1.1
    direction = Vector((1.0, -1.4, 0.7)).normalized()
    cam.location = ctr + direction * d
    cam.rotation_euler = (ctr - cam.location).to_track_quat('-Z', 'Y').to_euler()
    scene.render.filepath = os.path.join(OUT, 'prev_%s.png' % bpy.path.clean_name(c.name))
    bpy.ops.render.render(write_still=True)
    print('PREVIEW', c.name, scene.render.filepath)
