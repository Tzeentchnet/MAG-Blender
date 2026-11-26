"""Import assets in a windowed Blender and capture the 3D viewport (what the user sees), in Solid
(texture colour), Material Preview and Rendered (EEVEE) shading. Background renders (F12) do not
always match the viewport: decal problems showed only there.

blender --factory-startup --python tools/viewport_test.py -- ISO OUTDIR CAT:label [CAT:label ...]
        [view=<x,y,z>] [frame=<text>]

Opens a Blender window, imports every asset (level pieces allowed), frames the view on the
objects whose name contains ``frame`` (default: everything) from direction ``view``, writes
<OUTDIR>/<first label>_<shading>.png for each shading and quits. In a map, ``frame=<hash>``
frames the placed pieces (collection instances) of that model.
"""
import math
import os
import sys

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index('--') + 1:]
ISO, OUT = argv[0], argv[1]
ASSETS = [a.split(':', 1) for a in argv[2:] if ':' in a and '=' not in a.split(':', 1)[0]]
OPTS = dict(a.split('=', 1) for a in argv[2:] if '=' in a)
VIEW = Vector([float(v) for v in OPTS.get('view', '0.8,-1.0,0.35').split(',')]).normalized()
FRAME = OPTS.get('frame', '')
os.makedirs(OUT, exist_ok=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))


def run():
    import mag_importer
    mag_importer.register()
    from mag_importer.blender import state, ui
    assert state.try_load_cached(ISO)
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob)
    wm = bpy.context.window_manager
    wm.mag.show_level_pieces = True
    x = 0.0
    for cat, label in ASSETS:
        wm.mag.category = cat
        ui._refresh_list()
        i = next((i for i, it in enumerate(wm.mag.items) if it.label == label), None)
        if i is None:
            print('MISSING', cat, label)
            continue
        wm.mag.active = i
        before = set(bpy.context.scene.objects)
        print('IMPORT', cat, label, bpy.ops.mag.import_selected())
        new = [o for o in bpy.context.scene.objects if o not in before and o.parent is None]
        for o in new:  # side by side
            o.location.x += x
        x += 30.0
    bpy.app.timers.register(shoot, first_interval=1.0)


def shoot():
    deps = bpy.context.evaluated_depsgraph_get()
    pts = []
    for ob in bpy.context.scene.objects:
        if not ob.visible_get() or FRAME not in ob.name:
            continue
        if ob.type == 'MESH':
            ev = ob.evaluated_get(deps)
            m = ev.to_mesh()
            pts += [ev.matrix_world @ v.co for v in m.vertices]
            ev.to_mesh_clear()
        elif ob.instance_type == 'COLLECTION' and ob.instance_collection:  # placed map piece
            for o in ob.instance_collection.all_objects:
                pts += [ob.matrix_world @ o.matrix_world @ Vector(c) for c in o.bound_box]
    mn = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    mx = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    c, r = (mn + mx) / 2, max((mx - mn).length / 2, 1.0)
    win = bpy.context.window_manager.windows[0]
    area = max(win.screen.areas, key=lambda a: a.width * a.height)
    area.type = 'VIEW_3D'
    space = area.spaces.active
    region = next(rg for rg in area.regions if rg.type == 'WINDOW')
    rv = space.region_3d
    rv.view_perspective = 'PERSP'
    rv.view_location = c
    rv.view_rotation = (-VIEW).to_track_quat('-Z', 'Y')
    rv.view_distance = r / math.tan(math.radians(25)) * 1.1
    space.clip_start, space.clip_end = 0.05, rv.view_distance * 10
    space.overlay.show_overlays = False
    bpy.context.scene.render.resolution_x, bpy.context.scene.render.resolution_y = 1200, 800
    name = ASSETS[0][1].replace(' ', '_') if ASSETS else 'view'
    for shading, setup in (('solid', lambda s: (setattr(s, 'type', 'SOLID'), setattr(s, 'color_type', 'TEXTURE'))),
                           ('material', lambda s: setattr(s, 'type', 'MATERIAL')),
                           ('rendered', lambda s: setattr(s, 'type', 'RENDERED'))):
        setup(space.shading)
        bpy.context.scene.render.engine = 'BLENDER_EEVEE'
        with bpy.context.temp_override(window=win, area=area, region=region):
            bpy.ops.render.opengl(write_still=False, view_context=True)
        img = bpy.data.images['Render Result']
        path = os.path.join(OUT, '%s_%s.png' % (name, shading))
        img.save_render(path)
        print('SHOT', path)
    bpy.ops.wm.quit_blender()
    return None


bpy.app.timers.register(run, first_interval=0.5)
