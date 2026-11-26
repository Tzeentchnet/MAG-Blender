"""Import a map and render overview shots.

blender --background --factory-startup --python tools/map_test.py -- ISO OUTDIR map_label [foliage] [fx] [eevee]
                                                                         [terrain] [macro] [lodN] [materials]

``foliage`` turns on foliage import, checks the evaluated instance transforms against the decoded
data and adds a ground-level shot over the densest foliage. ``eevee`` renders with EEVEE (for
materials) instead of Workbench. ``fx`` places particle emitters and missing pieces as empties.
``terrain`` adds close shots of a point where four terrain tiles meet (detail layers and their
seams; use with ``eevee``) and checks every terrain material got its layer blend. ``macro`` imports
the terrain with *Macro Only* detail. ``lodN`` imports the terrain N levels above the finest
(Terrain LOD option), checks the tile count and that the tiles cover the same ground as the finest
level, and adds a terrain-only overview shot. ``fade`` (with ``foliage``) turns on the foliage
Distance Fade from the ground shot's camera and checks every evaluated instance scale against the
game's fade; with ``terrain`` it adds a high terrain shot past the macro fade (``nomacrofade``
turns the terrain fade off, for comparison). ``sky`` adds a ground-level shot toward the horizon
(the Sky import option is World by default; ``skydome`` imports it as the Dome mesh, ``nosky`` not
at all). ``collision`` imports the collision meshes, checks that every placed collision object
sits on a map instance's transform and that the terrain collision covers the terrain tiles, and
adds a shot of the collision alone (solid, object colour) and one of it over the map (Workbench
renders it solid and untextured, not as the viewport's wireframe). ``light`` checks the baked
lighting (on by default; ``nolight`` imports without it): every lit object carries ``mag_light``,
and vertices at the same position mostly share its value (far more often than random pairs), and
it adds a close shot of the densest group of lit placements (use with ``eevee``). ``materials``
counts the material kinds the header decode produces (alpha-tested, translucent, glass, water,
UV transform, detail maps), checks their node wiring, and adds close shots of the largest glass,
alpha-tested and detail-normal pieces and of the widest water plane (use with ``eevee``).
"""
import os
import sys
import time

import bpy
from mathutils import Vector

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _testutil import ensure_index  # noqa: E402

def _chain(ob):
    m = ob.matrix_basis.copy()
    while ob.parent is not None:
        m = ob.parent.matrix_basis @ ob.matrix_parent_inverse @ m
        ob = ob.parent
    return m


argv = sys.argv[sys.argv.index('--') + 1:]
ISO, OUT, MAP = argv[0], argv[1], argv[2]
FLAGS = set(argv[3:])
TLOD = next((int(f[3:]) for f in FLAGS if f.startswith('lod') and f[3:].isdigit()), None)
TLOD_SET = TLOD is not None
TLOD = TLOD or 0
TAG = ('_nolight' if 'nolight' in FLAGS else '') + ('_collision' if 'collision' in FLAGS else '') + ('_macro' if 'macro' in FLAGS else '') + ('_skydome' if 'skydome' in FLAGS else '') + ('_fade' if 'fade' in FLAGS else '') + ('_lod%d' % TLOD if TLOD_SET else '')
os.makedirs(OUT, exist_ok=True)
S = ensure_index(ISO)
from mag_importer.blender import ui  # noqa: E402

for ob in list(bpy.data.objects):
    bpy.data.objects.remove(ob)
wm = bpy.context.window_manager
scene = bpy.context.scene
wm.mag.category = 'MAP'
scene.mag.include_foliage = 'foliage' in FLAGS
scene.mag.place_emitters = 'fx' in FLAGS
scene.mag.terrain_detail = 'MACRO' if 'macro' in FLAGS else 'FULL'
scene.mag.terrain_lod = TLOD
scene.mag.sky = 'DOME' if 'skydome' in FLAGS else 'NONE' if 'nosky' in FLAGS else 'WORLD'
from mag_importer.blender import state  # noqa: E402
state.import_prefs().import_collision = 'collision' in FLAGS
state.import_prefs().import_lightmaps = 'nolight' not in FLAGS
if 'nomacrofade' in FLAGS:
    scene.mag.terrain_fade = 0.0
ui._refresh_list()
wm.mag.active = next(i for i, it in enumerate(wm.mag.items) if it.label == MAP)
print('MAP entry', wm.mag.items[wm.mag.active].detail)
t = time.time()
print('MAP import', bpy.ops.mag.import_selected(), '%.1fs' % (time.time() - t))
print('MAP stats objects=%d meshes=%d materials=%d images=%d' % (len(bpy.data.objects), len(bpy.data.meshes),
                                                                len(bpy.data.materials), len(bpy.data.images)))
for c in bpy.data.collections:
    if c.name.endswith('_fx'):
        print('MAP fx empties', len(c.objects), sorted({o.name.split('.')[0] for o in c.objects}))
    if c.name.endswith('_backdrop'):
        print('MAP backdrop instances', len(c.objects))
        c.hide_render = True

if 'foliage' in FLAGS:
    import numpy as np
    from mathutils import Matrix, Quaternion
    dg = bpy.context.evaluated_depsgraph_get()
    worst = 0.0
    count = 0
    for ob in [o for o in scene.objects if o.get('mag_foliage_model')]:
        me = ob.data
        n = len(me.vertices)
        co = np.empty(n * 3, np.float32)
        me.vertices.foreach_get('co', co)
        rot = np.empty(n * 4, np.float32)
        me.attributes['rotation'].data.foreach_get('value', rot)
        sc = np.empty(n, np.float32)
        me.attributes['scale'].data.foreach_get('value', sc)
        co, rot = co.reshape(-1, 3), rot.reshape(-1, 4)
        want = {}
        for i in range(n):
            want[tuple(np.round(co[i], 2))] = i
        seen = 0
        for inst in dg.object_instances:
            if not inst.is_instance or inst.parent is None or inst.parent.original != ob:
                continue
            if inst.object.original.parent is not None:
                continue  # only check root objects of the prototype
            m = inst.matrix_world
            i = want.get(tuple(np.round(np.array(m.translation, np.float32), 2)))
            if i is None:
                worst = max(worst, 1e9)
                continue
            proto = inst.object.original.matrix_world
            exp = (Matrix.Translation(co[i]) @ Quaternion(rot[i]).to_matrix().to_4x4()
                   @ Matrix.Diagonal((sc[i],) * 3 + (1,)) @ proto)
            worst = max(worst, max(abs(a - b) for ra, rb in zip(m, exp) for a, b in zip(ra, rb)))
            seen += 1
            if seen >= 200:
                break
        count += n
        print('MAP foliage %s points=%d checked=%d' % (ob.name, n, seen))
    print('MAP foliage total=%d worst matrix error %.2e' % (count, worst))
scene.render.engine = 'BLENDER_EEVEE' if 'eevee' in FLAGS else 'BLENDER_WORKBENCH'
scene.display.shading.color_type = 'TEXTURE'
if 'eevee' in FLAGS:
    sun = bpy.data.objects.new('sun', bpy.data.lights.new('sun', 'SUN'))
    sun.data.energy = 4.0
    sun.rotation_euler = (0.7, 0.2, 0.8)
    scene.collection.objects.link(sun)
    if scene.world is None or not scene.world.get('mag_sky'):
        scene.world = bpy.data.worlds.new('w')
        scene.world.color = (0.4, 0.45, 0.5)
scene.render.resolution_x, scene.render.resolution_y = 1280, 720
cam = bpy.data.objects.new('cam', bpy.data.cameras.new('cam'))
cam.data.clip_start = 0.5
cam.data.clip_end = 20000
scene.collection.objects.link(cam)
scene.camera = cam
FADE_SHOT = None
if 'fade' in FLAGS and 'foliage' in FLAGS:
    import numpy as np
    from mag_importer.blender.build import FOLIAGE_INPUTS, _set_modifier_input
    fol = [o for o in scene.objects if o.get('mag_foliage_model')]
    big = max(fol, key=lambda o: len(o.data.vertices))
    f = big.data.vertices[len(big.data.vertices) // 2].co.copy()
    cam.location = f + Vector((2.5, -3.5, 1.2))
    FADE_SHOT = cam.location.copy()
    worst, total, drawn, want_drawn = 0.0, 0, 0, 0
    for ob in fol:
        mod = ob.modifiers['MAG Foliage']
        ids = {i.name: i.identifier for i in mod.node_group.interface.items_tree
               if getattr(i, 'in_out', '') == 'INPUT' and i.name in FOLIAGE_INPUTS}
        _set_modifier_input(mod, ids['Distance Fade'], True)
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    for ob in fol:
        mod = ob.modifiers['MAG Foliage']
        ids = {i.name: i.identifier for i in mod.node_group.interface.items_tree
               if getattr(i, 'in_out', '') == 'INPUT' and i.name in FOLIAGE_INPUTS}
        fs = getattr(mod.properties.inputs, ids['Fade Start']).value
        fe = getattr(mod.properties.inputs, ids['Fade End']).value
        me = ob.data
        n = len(me.vertices)
        co = np.empty(n * 3, np.float32)
        me.vertices.foreach_get('co', co)
        co = co.reshape(-1, 3)
        sc = np.empty(n, np.float32)
        me.attributes['scale'].data.foreach_get('value', sc)
        d = np.linalg.norm(co - np.array(cam.location, np.float32), axis=1)
        k = 1.0 - np.clip((d - fs) / (fe - fs), 0.0, 1.0)
        want = {tuple(np.round(co[i], 2)): i for i in range(n)}
        seen = 0
        for inst in dg.object_instances:
            if not inst.is_instance or inst.parent is None or inst.parent.original != ob:
                continue
            if inst.object.original.parent is not None:
                continue
            m = inst.matrix_world
            i = want.get(tuple(np.round(np.array(m.translation, np.float32), 2)))
            if i is None:
                worst = max(worst, 1e9)
                continue
            p = inst.object.original.matrix_world
            s_got = m.to_3x3().col[0].length / max(p.to_3x3().col[0].length, 1e-9)
            worst = max(worst, abs(s_got - sc[i] * k[i]))
            seen += 1
        proto = getattr(mod.properties.inputs, ids['Collection']).value
        roots = sum(1 for o in proto.all_objects if o.parent is None)
        drawn += seen // max(roots, 1)
        want_drawn += int((k > 1e-4).sum())
        total += n
        print('MAP fade %s start=%.1f end=%.1f points=%d drawn=%d expected=%d' % (
            ob.name, fs, fe, n, seen // max(roots, 1), int((k > 1e-4).sum())))
    print('MAP fade total=%d drawn=%d expected=%d worst scale error %.2e -> %s' % (
        total, drawn, want_drawn, worst, 'OK' if worst < 1e-3 and drawn == want_drawn else 'FAIL'))
pts = [o.matrix_world.translation for o in scene.objects if o.instance_type == 'COLLECTION']
mn = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
mx = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
c = (mn + mx) / 2
size = max(mx.x - mn.x, mx.y - mn.y)
print('MAP extent', tuple(round(v) for v in mn), tuple(round(v) for v in mx))
shots = [('overview', c + Vector((0.0, -0.55, 0.75)) * size, c),
         ('ground', c + Vector((40, -60, 25)), c + Vector((0, 0, 5)))]
if 'sky' in FLAGS:
    shots.append(('sky', Vector((c.x, c.y, mx.z + 100)), Vector((c.x, c.y + 1000, mx.z + 400))))
water = None  # the flattest, widest placed piece with a water material
best = 0.0
for inst in [o for o in scene.objects if o.instance_type == 'COLLECTION' and o.instance_collection]:
    for src in inst.instance_collection.objects:
        if src.type == 'MESH' and any(m and m.get('mag_water') for m in src.data.materials):
            M = inst.matrix_world @ _chain(src)
            bb = [M @ Vector(v) for v in src.bound_box]
            dx = max(v.x for v in bb) - min(v.x for v in bb)
            dy = max(v.y for v in bb) - min(v.y for v in bb)
            dz = max(v.z for v in bb) - min(v.z for v in bb)
            score = min(dx, dy) / (dz + 0.5)
            if score > best and min(dx, dy) > 4:
                best, water = score, sum(bb, Vector()) / 8
tiles = []
if 'terrain' in FLAGS or TLOD_SET:
    from mag_importer.blender.build import terrain_tiles
    from mag_importer.blender.state import S as _S
    lv = next(l for l in _S.index.levels if l['name'].replace('mag_', '').replace('_final', '') == MAP)
    want = len(terrain_tiles(lv, TLOD))
    tcol = [c for c in bpy.data.collections if c.name.endswith('_terrain')]
    roots = [o for c in tcol for o in c.objects if o.parent is None]
    tiles = [o for c in tcol for o in c.all_objects if o.type == 'MESH']
    bbs = [[o.matrix_world @ Vector(v) for v in o.bound_box] for o in tiles]
    area = sum((max(v.x for v in b) - min(v.x for v in b)) * (max(v.y for v in b) - min(v.y for v in b))
               for b in bbs if b)
    ext = [min(v.x for b in bbs for v in b), min(v.y for b in bbs for v in b),
           max(v.x for b in bbs for v in b), max(v.y for b in bbs for v in b)]
    print('MAP terrain lod=%d tiles=%d (tree %d, %s) meshes=%d extent=%s bbox-area/extent=%.3f' % (
        TLOD, len(roots), want, 'OK' if len(roots) == want else 'MISMATCH', len(tiles),
        tuple(round(v) for v in ext), area / max((ext[2] - ext[0]) * (ext[3] - ext[1]), 1e-9)))
    if TLOD_SET:
        tc = Vector(((ext[0] + ext[2]) / 2, (ext[1] + ext[3]) / 2, c.z))
        shots.append(('terrain_overview', tc + Vector((0.0, -0.45, 0.6)) * max(ext[2] - ext[0], ext[3] - ext[1]), tc))
if 'terrain' in FLAGS:
    tiles = [o for c in bpy.data.collections if c.name.endswith('_terrain') for o in c.all_objects
             if o.type == 'MESH']
    tmats = {m for o in tiles for m in o.data.materials if m}
    full = [m for m in tmats if m.get('mag_terrain') == 'FULL']
    print('MAP terrain tiles=%d materials=%d layered=%d macro=%d other=%d' % (
        len(tiles), len(tmats), len(full), sum(m.get('mag_terrain') == 'MACRO' for m in tmats),
        sum('mag_terrain' not in m for m in tmats)))
    # the tile corner nearest the map centre where four tiles meet
    corners = {}
    for o in tiles:
        bb = [o.matrix_world @ Vector(v) for v in o.bound_box]
        for x in (min(v.x for v in bb), max(v.x for v in bb)):
            for y in (min(v.y for v in bb), max(v.y for v in bb)):
                k = (round(x), round(y))
                corners.setdefault(k, []).append(max(v.z for v in bb))
    k = min((k for k, v in corners.items() if len(v) >= 4), key=lambda k: (k[0] - c.x) ** 2 + (k[1] - c.y) ** 2)
    g, best = None, 1e18  # the terrain vertex nearest the corner (rays hit the backdrop)
    for o in tiles:
        for v in o.data.vertices:
            w = o.matrix_world @ v.co
            d = (w.x - k[0]) ** 2 + (w.y - k[1]) ** 2
            if d < best:
                g, best = w, d
    print('MAP terrain corner', k, 'ground z %.1f' % g.z)
    shots.append(('terrain_close', g + Vector((10, -14, 6)), g))
    shots.append(('terrain_top', g + Vector((0, -0.01, 60)), g))
    if 'fade' in FLAGS:
        shots.append(('terrain_far', g + Vector((0, -400, 500)), g))
COL = []
if 'collision' in FLAGS:
    COL = [o for c in bpy.data.collections if c.name.endswith('_collision') for o in c.objects]
    inst_t = {}  # several placements can share a position (stacked or rotated copies)
    for o in scene.objects:
        if o.instance_type == 'COLLECTION' or o.get('mag_lighting'):
            inst_t.setdefault(tuple(round(v, 2) for v in o.matrix_world.translation), []).append(o)
    placed = [o for o in COL if o.name != 'col_terrain']
    worst, missing = 0.0, 0
    for o in placed:
        cands = inst_t.get(tuple(round(v, 2) for v in o.matrix_world.translation))
        if not cands:
            missing += 1
            continue
        worst = max(worst, min(max(abs(a - b) for ra, rb in zip(o.matrix_world, i.matrix_world) for a, b in zip(ra, rb))
                               for i in cands))
    ter = next((o for o in COL if o.name == 'col_terrain'), None)
    tiles_all = [o for c in bpy.data.collections if c.name.endswith('_terrain') for o in c.all_objects if o.type == 'MESH']
    def _box(obs):
        bb = [o.matrix_world @ Vector(v) for o in obs for v in o.bound_box]
        return [min(v.x for v in bb), min(v.y for v in bb), max(v.x for v in bb), max(v.y for v in bb)]
    tb = _box([ter]) if ter else None
    rb = _box(tiles_all) if tiles_all else None
    terr = max(abs(a - b) for a, b in zip(tb, rb)) if tb and rb else float('nan')
    ok = missing == 0 and worst < 1e-3 and ter is not None and terr < 1.0
    print('MAP collision objects=%d placed=%d meshes=%d unmatched=%d worst matrix error %.1e terrain box %s vs tiles %s -> %s'
          % (len(COL), len(placed), len({o.data for o in COL}), missing, worst,
             [round(v) for v in tb] if tb else None, [round(v) for v in rb] if rb else None, 'OK' if ok else 'FAIL'))
    shots.append(('collision', c + Vector((0.0, -0.55, 0.75)) * size, c))
    shots.append(('collision_overlay', c + Vector((40, -60, 25)), c + Vector((0, 0, 5))))
if 'light' in FLAGS:
    import collections as _co
    import numpy as np
    lit_obs = [o for o in scene.objects if o.get('mag_lighting')]
    placements = {o['mag_lighting'] for o in lit_obs}
    rng = np.random.default_rng(0)
    eq = rnd = tot = 0
    missing = 0
    for o in lit_obs:
        ca = o.data.color_attributes.get('mag_light')
        if ca is None:
            missing += 1
            continue
        n = len(o.data.vertices)
        col = np.empty(n * 4, np.float32)
        ca.data.foreach_get('color', col)
        v = np.round(col.reshape(-1, 4)[:, :2] * 255).astype(int)
        co = np.empty(n * 3, np.float32)
        o.data.vertices.foreach_get('co', co)
        key = _co.defaultdict(list)
        for i, p in enumerate(np.round(co.reshape(-1, 3), 4).tolist()):
            key[tuple(p)].append(i)
        pr = np.array([(ix[0], j) for ix in key.values() for j in ix[1:]], np.int64).reshape(-1, 2)
        lit_v = ~(v == 255).all(1)  # skip vertices of meshes without baked lighting
        pr = pr[lit_v[pr[:, 0]]]
        if not len(pr):
            continue
        eq += int((v[pr[:, 0]] == v[pr[:, 1]]).all(1).sum())
        r = rng.integers(0, n, len(pr))
        rnd += int((v[r] == v[pr[:, 0]]).all(1).sum())
        tot += len(pr)
    seam, rate = eq / max(tot, 1), rnd / max(tot, 1)
    ok = (not lit_obs and 'nolight' in FLAGS) or (lit_obs and missing == 0 and seam - rate >= 0.4)
    print('MAP light objects=%d placements=%d without attribute=%d seams %.3f vs random %.3f (%d pairs) -> %s'
          % (len(lit_obs), len(placements), missing, seam, rate, tot, 'OK' if ok else 'FAIL'))
    if True:  # aim at the densest group of lit placements (from the records, so nolight matches)
        from mag_importer.blender.state import S as _S
        from mag_importer.core import lighting as _lit
        from mag_importer.core.zrb import T_LIGHTING
        lv = next(l for l in _S.index.levels if l['name'].replace('mag_', '').replace('_final', '') == MAP)
        names = {r[8] for z in _S.index.zrbs if z.archive == lv['archive'] for r in z.records if r[7] == T_LIGHTING}
        per = _lit.placement_lighting(_S.library.archive(lv['archive']).read(lv['clv']), names)
        from mag_importer.core import level as _lvl
        from mag_importer.blender.build import to_blender_matrix
        from mathutils import Matrix as _M
        P = [_M(to_blender_matrix(_lvl.trs_matrix(lv['trs'][i])).tolist()).translation for i, h in enumerate(per) if h]
    if P:
        cells = _co.Counter((int(p.x // 40), int(p.y // 40)) for p in P)
        (gx, gy), _ = cells.most_common(1)[0]
        inside = [p for p in P if int(p.x // 40) == gx and int(p.y // 40) == gy]
        lc = sum(inside, Vector()) / len(inside)
        shots.append(('light', lc + Vector((28, -34, 14)), lc + Vector((0, 0, 4))))
if 'materials' in FLAGS:
    import collections as _co
    mats = [m for m in bpy.data.materials if m.get('mag_hash') and m.node_tree]
    kinds = _co.Counter()
    bad = _co.Counter()
    for m in mats:
        nodes = m.node_tree.nodes
        bsdf = next((n for n in nodes if n.type == 'BSDF_PRINCIPLED'), None)
        alpha_in = bsdf.inputs['Alpha'].links[0].from_node if bsdf and bsdf.inputs['Alpha'].is_linked else None
        for k in ('mag_alpha_ref', 'mag_soft_alpha', 'mag_translucent', 'mag_glass', 'mag_water', 'mag_uv_transform',
                  'mag_detail_normal', 'mag_detail_color'):
            if k in m:
                kinds[k] += 1
        if 'mag_alpha_ref' in m:
            want = (m['mag_alpha_ref'] - 0.5) / 255.0
            if m.get('mag_soft_alpha'):  # soft decal: (alpha test) x alpha, Blended
                if m.surface_render_method != 'BLENDED':
                    bad['soft alpha not blended'] += 1
                alpha_in = next((l.from_node for l in alpha_in.inputs[0].links), None) \
                    if alpha_in is not None and alpha_in.type == 'MATH' and alpha_in.operation == 'MULTIPLY' else None
            if not (alpha_in and alpha_in.type == 'MATH' and alpha_in.operation == 'GREATER_THAN'
                    and abs(alpha_in.inputs[1].default_value - want) < 1e-6):
                bad['alpha test not wired'] += 1
        elif alpha_in is not None and m.surface_render_method != 'BLENDED':
            bad['alpha wired without alpha test or blending'] += 1
        if m.get('mag_glass') and m.surface_render_method != 'BLENDED':
            bad['glass not blended'] += 1
        if 'mag_uv_transform' in m and not any(n.type == 'MAPPING' and n.label == 'Material UV transform' for n in nodes):
            bad['uv transform node missing'] += 1
        if 'mag_detail_normal' in m and not any(n.type == 'TEX_IMAGE' and n.label.startswith('Detail normal')
                                                and n.outputs['Color'].is_linked for n in nodes):
            bad['detail normal not wired'] += 1
    print('MAP materials total=%d %s %s -> %s' % (len(mats), dict(kinds), dict(bad), 'FAIL' if bad else 'OK'))

    def _densest(pred, max_size=80.0):
        """World centre of the largest placed mesh (up to ``max_size`` m) with a material matching
        ``pred``."""
        best, where = 0.0, None
        for inst in [o for o in scene.objects if o.instance_type == 'COLLECTION' and o.instance_collection]:
            for src in inst.instance_collection.objects:
                if src.type == 'MESH' and any(mm and pred(mm) for mm in src.data.materials):
                    M = inst.matrix_world @ _chain(src)
                    bb = [M @ Vector(v) for v in src.bound_box]
                    size = (max(v.x for v in bb) - min(v.x for v in bb)) + (max(v.y for v in bb) - min(v.y for v in bb))
                    if best < size <= max_size:
                        best, where = size, (sum(bb, Vector()) / 8, size)
        return where
    for label, pred in (('glass', lambda mm: mm.get('mag_glass')),
                        ('alphatest', lambda mm: 'mag_alpha_ref' in mm),
                        ('detail', lambda mm: 'mag_detail_normal' in mm)):
        hit = _densest(pred)
        if hit:
            p, size = hit
            d = max(3.0, min(size, 40.0))
            shots.append(('mat_' + label, p + Vector((0.6, -0.9, 0.45)) * d, p))
            print('MAP materials shot %s at %s (size %.1f)' % (label, tuple(round(v, 1) for v in p), size))
if water is not None and 'eevee' not in FLAGS:
    shots.append(('water', water + Vector((0, -0.01, 12)), water))
elif water is not None and 'materials' in FLAGS:  # ripples and reflection, at a low angle
    shots.append(('mat_water', water + Vector((12, -20, 7)), water))
fol = [o for o in scene.objects if o.get('mag_foliage_model')]
if fol:
    big = max(fol, key=lambda o: len(o.data.vertices))
    vs = big.data.vertices
    f = vs[len(vs) // 2].co.copy()
    shots.append(('foliage', f + Vector((2.5, -3.5, 1.2)), f + Vector((0, 0, 0.2))))
    if FADE_SHOT is not None:
        shots.append(('foliage_fade', f + Vector((25, -35, 12)), f))
    shots.append(('foliage_top', f + Vector((0, -20, 45)), f))
shown = {o: o.hide_render for o in scene.objects}
for name, loc, tgt in shots:
    keep = set(tiles) | {cam} | {o for o in scene.objects if o.type == 'LIGHT'}
    for o, hidden in shown.items():  # terrain shots: terrain alone, so props do not hide the seams
        o.hide_render = hidden or (name.startswith('terrain') and o not in keep)
    if name.startswith('collision'):  # collision alone, or over the map
        for o in shown:
            o.hide_render = (o not in COL and name == 'collision') or (shown[o] and o not in COL)
        for cc in bpy.data.collections:
            if cc.name.endswith('_collision'):
                cc.hide_render = False
        for o in COL:
            o.hide_render = False
            o.display_type = 'SOLID' if name == 'collision' else 'WIRE'
        scene.display.shading.color_type = 'OBJECT' if name == 'collision' else 'TEXTURE'
    else:
        scene.display.shading.color_type = 'TEXTURE'
    cam.location = loc
    cam.rotation_euler = (tgt - loc).to_track_quat('-Z', 'Y').to_euler()
    scene.render.filepath = os.path.join(OUT, 'map_%s_%s%s.png' % (MAP, name, TAG))
    t = time.time()
    bpy.ops.render.render(write_still=True)
    print('MAP shot', name, '%.1fs' % (time.time() - t))
    if name == 'water':
        img = bpy.data.images.load(scene.render.filepath)
        px = list(img.pixels)
        w, h = img.size
        cx, cy = w // 2, h // 2
        acc = [0.0, 0.0, 0.0]
        k = 0
        for y in range(cy - 20, cy + 20, 4):
            for x in range(cx - 20, cx + 20, 4):
                o = (y * w + x) * 4
                for j in range(3):
                    acc[j] += px[o + j]
                k += 1
        mean = [a / k for a in acc]
        ok = max(mean) < 0.35 and mean[2] >= mean[0] and mean[1] >= mean[0]
        print('MAP water mean rgb %.3f %.3f %.3f -> %s' % (*mean, 'OK' if ok else 'FAIL'))
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(OUT, 'map_%s.blend' % MAP))
print('MAP saved')
