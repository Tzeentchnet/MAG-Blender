"""Turn decoded MAG data into Blender data (meshes, materials, armatures, actions, maps).

Coordinates: the game is Y-up; everything is converted to Blender's Z-up with
(x, y, z) -> (x, -z, y), a proper rotation, so winding and handedness are preserved.
"""
import collections
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor

import bpy
import numpy as np
from mathutils import Matrix, Quaternion

from ..core import foliage as fol
from ..core import level as lvl
from ..core import lighting as lit
from ..core.materials import VERTEX_LIT, WATER_COLOR, WIRE_COLOR
from ..core.library import skeleton_world, fit_score
from ..core.zrb import T_MODEL, T_SKINNED_MODEL, T_LEVEL_MODEL, T_FOLIAGE_MODEL, T_FOLIAGE_BATCH

MODEL_TYPES = (T_MODEL, T_SKINNED_MODEL, T_LEVEL_MODEL, T_FOLIAGE_MODEL)
C3 = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])
C4 = np.eye(4)
C4[:3, :3] = C3
C4_INV = C4.T.copy()


def to_blender_vec(v):
    """(n,3) game vectors -> Blender (x, -z, y)."""
    out = np.empty_like(v)
    out[:, 0] = v[:, 0]
    out[:, 1] = -v[:, 2]
    out[:, 2] = v[:, 1]
    return out


def to_blender_matrix(m):
    return C4 @ m @ C4_INV


def qmat(q):
    x, y, z, w = (float(v) for v in q)
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def _safe(name, maxlen=60):
    name = re.sub(r'[^\w\-. ]+', '_', name).strip() or 'mag'
    return name[:maxlen]


class ImportOptions:
    def __init__(self, **kw):
        self.lod = 0
        self.materials = True
        self.flip_green = False
        self.skeleton = 'AUTO'
        self.set_fps = True
        self.instance_maps = True
        self.include_terrain = True
        self.include_foliage = False
        self.foliage_density = 1.0
        self.foliage_min_distance = 0.0
        self.place_emitters = False
        self.terrain_detail = 'FULL'   # 'FULL' (blended detail layers) or 'MACRO' (tile colour only)
        self.terrain_lod = 0           # levels above the finest in the terrain LOD tree (clamped)
        self.terrain_fade = 250.0      # camera distance where terrain starts fading to macro (0 = off)
        self.foliage_fade = False      # shrink foliage with distance from the scene camera, as the game does
        self.sky = 'WORLD'             # 'WORLD' (scene world), 'DOME' (mesh) or 'NONE'
        self.sky_strength = 1.0
        self.collision = False         # import the map's collision meshes (physics.zrb)
        self.baked_lighting = True     # apply the map's baked per-vertex lighting (84DFB280)
        self.lower_lods = False        # also import the coarser LODs (hidden sub-collections)
        self.pack_textures = False     # pack the cached DDS files into the .blend
        self.__dict__.update(kw)


def _select_meshes(model, geoms, lod):
    """Meshes to import for a LOD; falls back to the lowest LOD present."""
    chosen = [i for i, me in enumerate(model.meshes) if geoms[i] is not None and (me.lod_mask & (1 << lod) or me.lod_mask == 0)]
    if not chosen:
        for alt in range(8):
            chosen = [i for i, me in enumerate(model.meshes) if geoms[i] is not None and me.lod_mask & (1 << alt)]
            if chosen:
                break
    return set(chosen)


def _extra_lods(model, geoms, keep, lod):
    """[(lod, mesh index set)] for every LOD coarser than ``lod`` whose meshes differ from ``keep``."""
    bits = 0
    for i, me in enumerate(model.meshes):
        if geoms[i] is not None:
            bits |= me.lod_mask
    out = []
    for k in range(lod + 1, 8):
        if not bits >> k & 1:
            continue
        ids = {i for i, me in enumerate(model.meshes)
               if geoms[i] is not None and (me.lod_mask & (1 << k) or me.lod_mask == 0)}
        if ids and ids != keep:
            out.append((k, ids))
    return out


def _find_layer_collection(lc, coll):
    if lc.collection == coll:
        return lc
    for child in lc.children:
        hit = _find_layer_collection(child, coll)
        if hit is not None:
            return hit
    return None


class _LodCollections:
    """One child collection per extra LOD (``<parent>_LOD<n>``), hidden in viewport and render."""

    def __init__(self, parent):
        self.parent = parent
        self.colls = {}

    def get(self, k):
        c = self.colls.get(k)
        if c is None:
            c = bpy.data.collections.new('%s_LOD%d' % (self.parent.name, k))
            self.parent.children.link(c)
            c.hide_render = True
            lc = _find_layer_collection(bpy.context.view_layer.layer_collection, c)
            if lc is not None:  # the eye toggle, so one click shows the whole LOD
                lc.hide_viewport = True
            else:  # parent not in this view layer: fall back to the global toggle
                c.hide_viewport = True
            self.colls[k] = c
        return c


# -- meshes --------------------------------------------------------------------------------------
def make_mesh(name, parts, materials=()):
    """parts: list of (Geometry, material slot index). Returns a bpy Mesh."""
    me = bpy.data.meshes.new(name)
    parts = [(g, mi) for g, mi in parts if g is not None and len(g.positions)]
    if not parts:
        return me
    pos = np.concatenate([g.positions for g, _ in parts]).astype(np.float32)
    offs = np.cumsum([0] + [len(g.positions) for g, _ in parts[:-1]])
    tris = np.concatenate([g.indices + o for (g, _), o in zip(parts, offs)]).astype(np.int32)
    mat_idx = np.concatenate([np.full(len(g.indices), mi, np.int32) for g, mi in parts])
    nv, nt = len(pos), len(tris)
    me.vertices.add(nv)
    me.vertices.foreach_set('co', to_blender_vec(pos).ravel())
    me.loops.add(nt * 3)
    me.loops.foreach_set('vertex_index', tris.ravel())
    me.polygons.add(nt)
    me.polygons.foreach_set('loop_start', np.arange(0, nt * 3, 3, dtype=np.int32))
    me.polygons.foreach_set('loop_total', np.full(nt, 3, np.int32))
    me.polygons.foreach_set('material_index', mat_idx)
    # UV layers (V flipped for Blender)
    nuv = max(len(g.uvs) for g, _ in parts)
    for k in range(nuv):
        uv = np.concatenate([g.uvs[k] if k < len(g.uvs) else np.zeros((len(g.positions), 2), np.float32) for g, _ in parts])
        uv = uv.astype(np.float32).copy()
        uv[:, 1] = 1.0 - uv[:, 1]
        layer = me.uv_layers.new(name='UVMap' if k == 0 else 'UV%d' % k)
        layer.data.foreach_set('uv', uv[tris.ravel()].ravel())
    if any(g.colors is not None for g, _ in parts):
        col = np.concatenate([g.colors if g.colors is not None else np.ones((len(g.positions), 4), np.float32) for g, _ in parts])
        ca = me.color_attributes.new('Col', 'FLOAT_COLOR', 'POINT')
        ca.data.foreach_set('color', np.clip(col, 0, 1).astype(np.float32).ravel())
    for m in materials:
        me.materials.append(m)
    me.update()
    me.validate(clean_customdata=False)
    if all(g.normals is not None for g, _ in parts):
        nrm = np.concatenate([g.normals for g, _ in parts]).astype(np.float64)
        nrm = to_blender_vec(nrm)
        ln = np.linalg.norm(nrm, axis=1, keepdims=True)
        nrm = nrm / np.where(ln > 1e-8, ln, 1.0)
        me.shade_smooth()
        if len(me.vertices) == nv:
            me.normals_split_custom_set_from_vertices(nrm.astype(np.float32))
    return me


def set_light(me, values):
    """Store (sky, sun) per vertex (floats 0-1) as the ``mag_light`` colour attribute."""
    ca = me.color_attributes.get(LIGHT_ATTR) or me.color_attributes.new(LIGHT_ATTR, 'FLOAT_COLOR', 'POINT')
    col = np.zeros((len(values), 4), np.float32)
    col[:, :2] = values
    col[:, 3] = 1.0
    ca.data.foreach_set('color', col.ravel())


# -- materials -----------------------------------------------------------------------------------
def _preview_image(col):
    """1x1 generated image of a (linear) colour, shared per colour."""
    r, g, b = (float(c) for c in col[:3])
    name = 'MAG_preview_%02x%02x%02x' % tuple(int(round(min(1.0, max(0.0, c)) * 255)) for c in (r, g, b))
    img = bpy.data.images.get(name)
    if img is None:
        img = bpy.data.images.new(name, 1, 1, float_buffer=True)
        img.generated_color = (r, g, b, 1.0)
    return img


# Bump when the material build changes: a .blend's materials built by an older importer are then
# rebuilt instead of reused.
IMAGE_SLOTS = (3, 4, 5, 15, 16, 17, 255)  # binding slots _build loads images for
MATERIAL_VERSION = 4  # 3: normal maps through the shared MAG Normal group; 4: base layer = lowest
#                       binding offset, colour images Channel Packed unless alpha is opacity, wire colour


class MaterialCache:
    def __init__(self, lib, opts):
        self.lib = lib
        self.opts = opts
        self.mats = {}
        self.images = {}
        self._flags = {}
        self._parsed = {}  # material hash -> lib.material() result

    def _material(self, h, near):
        if h not in self._parsed:
            self._parsed[h] = self.lib.material(h, near) if self.lib else None
        return self._parsed[h]

    def prefetch(self, pairs):
        """Convert the textures of materials ``pairs`` [(hash, near_zrb)] into the cache in
        parallel ahead of building, instead of one by one as materials are made."""
        if not self.lib or not self.opts.materials:
            return 0
        items = []
        for h, near in dict.fromkeys(pairs):
            out = self._material(h, near)
            if not out:
                continue
            info, mzrb = out
            if info.terrain is not None and self.opts.terrain_detail == 'FULL':
                t = info.terrain
                hs = [t.weights, t.macro] + list(t.colors) + list(t.normals)
            else:
                hs = [th for slot, th in info.bindings.items() if slot in IMAGE_SLOTS]
            items.extend((th, mzrb) for th in hs if th)
        return self.lib.prefetch_textures(items)

    def flags(self, h, near=None):
        """Flag word of a material (``MaterialInfo.flags``), None if it is not on the disc."""
        if h not in self._flags:
            out = self._material(h, near)
            self._flags[h] = out[0].flags if out else None
        return self._flags[h]

    def vertex_lit(self, h, near=None):
        f = self.flags(h, near)
        return f is not None and bool(f & VERTEX_LIT)

    def image(self, h, near, non_color=False, opacity=False):
        """-> (bpy Image or None, metadata dict). Colour, colour-with-opacity and data uses get
        separate datablocks. Only ``opacity`` images (a base map whose alpha the material tests
        or blends with) keep Straight alpha; the others are Channel Packed, because Blender
        premultiplies straight images and so loses the colour wherever alpha is 0 (backdrop
        rocks, roads and specular maps use alpha for something else and drew black)."""
        key = (h, non_color, opacity)
        if key in self.images:
            return self.images[key]
        path, meta = self.lib.texture_file(h, near)
        img = None
        if path:
            nm = '%08x%s' % (h, '_data' if non_color else '_alpha' if opacity else '')
            img = bpy.data.images.get(nm)
            if img is None:
                img = bpy.data.images.load(path, check_existing=False)
                img.name = nm
                if non_color:
                    img.colorspace_settings.name = 'Non-Color'
            img.alpha_mode = 'STRAIGHT' if opacity else 'CHANNEL_PACKED'
            if getattr(self.opts, 'pack_textures', False) and img.packed_file is None:
                try:
                    img.pack()
                except RuntimeError as e:
                    print('MAG: could not pack %s: %s' % (nm, e))
        self.images[key] = (img, meta)
        return img, meta

    def get(self, h, near):
        if h in self.mats:
            return self.mats[h]
        name = 'MAG_%08x' % h
        mode = self.opts.terrain_detail
        baked = bool(self.opts.baked_lighting)
        mat = next((m for m in bpy.data.materials if m.get('mag_hash') == '%08x' % h
                    and m.get('mag_terrain', mode) == mode and m.get('mag_baked', baked) == baked
                    and m.get('mag_v', 0) == MATERIAL_VERSION), None)
        if mat is None:
            mat = self._build(h, near, name)
        self.mats[h] = mat
        return mat

    def _build(self, h, near, name):
        mat = bpy.data.materials.new(name)
        mat['mag_hash'] = '%08x' % h
        mat.use_nodes = True
        nt = mat.node_tree
        bsdf = next(n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED')
        bsdf.location = (300, 300)
        out = self._material(h, near)
        if not out or not self.opts.materials:
            return mat
        info, mzrb = out
        self._flags[h] = info.flags
        mat['mag_v'] = MATERIAL_VERSION
        if info.parsed:
            mat['mag_technique'] = '%08x' % info.technique
        if info.flags & VERTEX_LIT:
            mat['mag_baked'] = bool(self.opts.baked_lighting)
        if info.terrain is not None:
            mat['mag_terrain'] = self.opts.terrain_detail
            if self.opts.terrain_detail == 'FULL':
                self._build_terrain(mat, bsdf, info.terrain, mzrb)
                return mat
        b = info.bindings
        links = nt.links
        y = 600
        base_node = None
        # the game's UV transform of the base maps: uv' = uv * s + o (V flipped in Blender)
        uvt = info.uv_transform()
        base_uv = None
        if uvt:
            su, sv, ou, ov = uvt
            uvn = nt.nodes.new('ShaderNodeUVMap')
            uvn.uv_map, uvn.location = 'UVMap', (-1700, 300)
            mp = nt.nodes.new('ShaderNodeMapping')
            mp.location, mp.label = (-1500, 300), 'Material UV transform'
            mp.inputs['Scale'].default_value = (su, sv, 1.0)
            mp.inputs['Location'].default_value = (ou, 1.0 - sv - ov, 0.0)
            links.new(uvn.outputs['UV'], mp.inputs['Vector'])
            base_uv = mp.outputs['Vector']
            mat['mag_uv_transform'] = list(uvt)
        if 3 in b:
            has_alpha = bool(self.lib.texture_file(b[3], mzrb)[1].get('alpha'))
            opacity = info.alpha_test or ((info.translucent or not info.parsed) and has_alpha)
            img, meta = self.image(b[3], mzrb, opacity=opacity)
            if img:
                n = nt.nodes.new('ShaderNodeTexImage')
                n.image, n.label, n.location = img, 'Base Color', (-500, y)
                if base_uv is not None:
                    links.new(base_uv, n.inputs['Vector'])
                color = n.outputs['Color']
                if info.alpha_test or (not info.parsed and meta.get('alpha')):
                    # alpha test: drawn where alpha >= reference (header +0x09); the fallback
                    # (unparsed header) keeps the old texture-alpha guess with a 0.5 threshold
                    ref = info.alpha_ref if info.alpha_test else 128
                    cut = nt.nodes.new('ShaderNodeMath')
                    cut.operation, cut.location, cut.label = 'GREATER_THAN', (-200, y - 200), 'Alpha test'
                    links.new(n.outputs['Alpha'], cut.inputs[0])
                    cut.inputs[1].default_value = (ref - 0.5) / 255.0
                    links.new(cut.outputs[0], bsdf.inputs['Alpha'])
                    mat.surface_render_method = 'DITHERED'
                    mat['mag_alpha_ref'] = ref
                elif info.translucent and meta.get('alpha'):
                    links.new(n.outputs['Alpha'], bsdf.inputs['Alpha'])
                    mat.surface_render_method = 'BLENDED'
                color = self._detail_color(mat, info, mzrb, color, y)
                links.new(color, bsdf.inputs['Base Color'])
                base_node = n
            y -= 300
        if info.translucent:
            mat['mag_translucent'] = True
        if 5 in b:
            img, _ = self.image(b[5], mzrb, non_color=False)
            if img:
                n = nt.nodes.new('ShaderNodeTexImage')
                n.image, n.label, n.location = img, 'Specular (gloss in A)', (-500, y)
                if base_uv is not None:
                    links.new(base_uv, n.inputs['Vector'])
                bw = nt.nodes.new('ShaderNodeRGBToBW')
                bw.location = (-200, y)
                links.new(n.outputs['Color'], bw.inputs[0])
                links.new(bw.outputs[0], bsdf.inputs['Specular IOR Level'])
                inv = nt.nodes.new('ShaderNodeMath')
                inv.operation, inv.location = 'SUBTRACT', (-200, y - 120)
                inv.inputs[0].default_value = 1.0
                links.new(n.outputs['Alpha'], inv.inputs[1])
                links.new(inv.outputs[0], bsdf.inputs['Roughness'])
            y -= 300
        normals = []  # (x, y) sockets in -1..1, summed (the detail normal adds to the base one)
        if 4 in b:
            img, meta = self.image(b[4], mzrb, non_color=True)
            if img:
                normals.append(self._normal_xy(nt, img, meta.get('dxt5nm', True), 'Normal', base_uv, y))
            y -= 400
        detail_normal = 16 in b and info.detail_scale(16)
        if detail_normal:
            img, meta = self.image(b[16], mzrb, non_color=True)
            if img:
                vec = self._detail_uv(nt, info.detail_scale(16), 'Detail normal', y)
                normals.append(self._normal_xy(nt, img, meta.get('dxt5nm', True), 'Detail normal', vec, y))
                mat['mag_detail_normal'] = list(info.detail_scale(16))
            y -= 400
        if normals:
            self._normal_out(nt, bsdf, normals, y)
        for slot, label in ((15, 'Emissive/Mask'), (16, 'Detail Normal'), (17, 'Detail Color'), (255, 'Mask')):
            if slot in b and not (slot == 16 and detail_normal) and not (slot == 17 and 'mag_detail_color' in mat):
                img, _ = self.image(b[slot], mzrb, non_color=slot != 17)
                if img:
                    n = nt.nodes.new('ShaderNodeTexImage')
                    n.image, n.label, n.location = img, label + ' (unconnected)', (-900, y)
                    y -= 300
        if info.flags & VERTEX_LIT and self.opts.baked_lighting:
            _apply_baked_light(nt, bsdf)
        if info.glass:
            # glass technique (normal + environment map, no colour texture): a clear blended surface
            bsdf.inputs['Base Color'].default_value = (0.8, 0.85, 0.9, 1.0)
            bsdf.inputs['Roughness'].default_value = 0.05
            bsdf.inputs['Alpha'].default_value = 0.2
            mat.surface_render_method = 'BLENDED'
            mat.diffuse_color = (0.6, 0.7, 0.8, 0.3)
            mat['mag_glass'] = True
        elif info.water or (not info.parsed and 3 not in b and 4 in b):
            # water technique; the fallback (unparsed header) keeps the old "normal map but no
            # colour" rule. The technique's colour parameter looks like the deep-water colour.
            wc = info.constant(WATER_COLOR) if info.water else None
            rgb = tuple(wc[:3]) if wc and len(wc) == 4 else (0.015, 0.035, 0.04)
            bsdf.inputs['Base Color'].default_value = rgb + (1.0,)
            bsdf.inputs['Roughness'].default_value = 0.05
            mat.diffuse_color = (0.04, 0.08, 0.1, 1.0)
            mat['mag_water'] = True
        elif info.wire:
            # power cables: no textures, a single linear colour parameter
            wc = info.constant(WIRE_COLOR)
            rgb = tuple(wc[:3]) if wc and len(wc) >= 3 else (0.002, 0.002, 0.002)
            bsdf.inputs['Base Color'].default_value = rgb + (1.0,)
            bsdf.inputs['Roughness'].default_value = 0.6
            mat.diffuse_color = rgb + (1.0,)
            mat['mag_wire'] = True
        # the viewport's Texture colour mode and texture painting show the active image node
        if base_node is not None:
            nt.nodes.active = base_node
        else:
            nt.nodes.active = bsdf
            # Workbench's Texture mode draws an image node regardless (the normal or specular
            # map), so give it an unconnected 1x1 image of the base colour to draw instead
            if any(n.type == 'TEX_IMAGE' for n in nt.nodes):
                col = mat.diffuse_color if (mat.get('mag_water') or mat.get('mag_glass')) \
                    else bsdf.inputs['Base Color'].default_value
                n = nt.nodes.new('ShaderNodeTexImage')
                n.image = _preview_image(col)
                n.label, n.location = 'Viewport colour (unconnected)', (-500, 900)
                nt.nodes.active = n
                mat['mag_preview'] = True
        return mat

    def _build_terrain(self, mat, bsdf, t, mzrb):
        """Four tiled detail layers blended by the tile's weight map (see TerrainLayers)."""
        nt = mat.node_tree
        links = nt.links
        grp = nt.nodes.new('ShaderNodeGroup')
        grp.node_tree = terrain_group(self.opts.flip_green)
        set_terrain_fade(grp.node_tree, self.opts.terrain_fade)
        grp.location = (0, 300)
        links.new(grp.outputs['Color'], bsdf.inputs['Base Color'])
        nm = nt.nodes.new('ShaderNodeNormalMap')
        nm.location, nm.uv_map = (150, -100), 'UVMap'
        links.new(grp.outputs['Normal'], nm.inputs['Color'])
        links.new(nm.outputs[0], bsdf.inputs['Normal'])
        bsdf.inputs['Roughness'].default_value = 0.9
        bsdf.inputs['Specular IOR Level'].default_value = 0.2
        uv = nt.nodes.new('ShaderNodeUVMap')
        uv.uv_map, uv.location = 'UVMap', (-1300, 300)

        def tex(h, non_color, loc, label, extend=False):
            img, _ = self.image(h, mzrb, non_color=non_color)
            n = nt.nodes.new('ShaderNodeTexImage')
            n.image, n.label, n.location = img, label, loc
            if extend:
                n.extension = 'EXTEND'
            return n
        w = tex(t.weights, True, (-500, 700), 'Layer weights (RGBA)', extend=True)
        links.new(uv.outputs['UV'], w.inputs['Vector'])
        links.new(w.outputs['Color'], grp.inputs['Weights'])
        links.new(w.outputs['Alpha'], grp.inputs['Weights Alpha'])
        for k in range(4):
            s, c, d = t.uv[k]
            y = 400 - 520 * k
            mp = nt.nodes.new('ShaderNodeMapping')
            mp.location, mp.label = (-1000, y), 'Layer %d UV (x%g)' % (k, s)
            # game: s*uv + (c, d); Blender's V is flipped (1 - v) for the mesh and the image
            mp.inputs['Scale'].default_value = (s, s, 1.0)
            mp.inputs['Location'].default_value = (c % 1.0, (1.0 - s - d) % 1.0, 0.0)
            links.new(uv.outputs['UV'], mp.inputs['Vector'])
            cn = tex(t.colors[k], False, (-500, y), 'Layer %d colour' % k)
            nn = tex(t.normals[k], True, (-500, y - 260), 'Layer %d normal (DXT5nm)' % k)
            links.new(mp.outputs['Vector'], cn.inputs['Vector'])
            links.new(mp.outputs['Vector'], nn.inputs['Vector'])
            links.new(cn.outputs['Color'], grp.inputs['Color %d' % k])
            links.new(nn.outputs['Color'], grp.inputs['Normal %d' % k])
            links.new(nn.outputs['Alpha'], grp.inputs['Normal %d Alpha' % k])
        # Workbench's Texture mode draws the active image node: show the tile's blended colour
        mac = tex(t.macro, False, (-500, 1000), 'Macro colour (distance fade, viewport)', extend=True)
        links.new(uv.outputs['UV'], mac.inputs['Vector'])
        links.new(mac.outputs['Color'], grp.inputs['Macro'])
        nt.nodes.active = mac
        mat['mag_layers'] = ['%08x' % h for h in t.colors]

    def _normal_xy(self, nt, img, dxt5nm, label, vector, y):
        """Image node of a normal map; returns (node, dxt5nm) for _normal_out."""
        tex = nt.nodes.new('ShaderNodeTexImage')
        tex.image, tex.location = img, (-500, y)
        tex.label = '%s (%s)' % (label, 'DXT5nm' if dxt5nm else 'RGB')
        if vector is not None:
            nt.links.new(vector, tex.inputs['Vector'])
        return tex, dxt5nm

    def _normal_out(self, nt, bsdf, normals, y):
        """Feed the normal map(s) through the shared *MAG Normal* group (decode, detail sum, Z
        rebuild) into a Normal Map node."""
        assert 1 <= len(normals) <= 2
        links = nt.links
        grp = nt.nodes.new('ShaderNodeGroup')
        grp.node_tree = normal_group(self.opts.flip_green)
        grp.location, grp.label = (-100, y), 'MAG Normal'
        for (tex, dxt5nm), pre in zip(normals, ('', 'Detail ')):
            links.new(tex.outputs['Color'], grp.inputs[pre + 'Color'])
            links.new(tex.outputs['Alpha'], grp.inputs[pre + 'Alpha'])
            grp.inputs[pre + 'DXT5nm'].default_value = 1.0 if dxt5nm else 0.0
        grp.inputs['Detail'].default_value = 1.0 if len(normals) > 1 else 0.0
        nm = nt.nodes.new('ShaderNodeNormalMap')
        nm.location = (100, y)
        nm.uv_map = 'UVMap'
        links.new(grp.outputs['Normal'], nm.inputs['Color'])
        links.new(nm.outputs[0], bsdf.inputs['Normal'])

    def _detail_uv(self, nt, scale, label, y):
        """UV0 scaled by a detail map's scale (the same V flip as the material UV transform)."""
        uvn = nt.nodes.new('ShaderNodeUVMap')
        uvn.uv_map, uvn.location = 'UVMap', (-1700, y)
        mp = nt.nodes.new('ShaderNodeMapping')
        mp.location, mp.label = (-1500, y), '%s UV (x%g, x%g)' % (label, scale[0], scale[1])
        mp.inputs['Scale'].default_value = (scale[0], scale[1], 1.0)
        mp.inputs['Location'].default_value = (0.0, (1.0 - scale[1]) % 1.0, 0.0)
        nt.links.new(uvn.outputs['UV'], mp.inputs['Vector'])
        return mp.outputs['Vector']

    def _detail_color(self, mat, info, mzrb, color, y):
        """Modulate the base colour by the detail colour (slot 17), 2x around mid-grey. The game
        presumably does this on gamma-space values, so the factor is raised to 2.2 here."""
        scale = info.detail_scale(17)
        if not scale:
            return color
        img, _ = self.image(info.bindings[17], mzrb, non_color=True)
        if not img:
            return color
        nt = mat.node_tree
        tex = nt.nodes.new('ShaderNodeTexImage')
        tex.image, tex.label, tex.location = img, 'Detail colour', (-500, y + 300)
        nt.links.new(self._detail_uv(nt, scale, 'Detail colour', y + 300), tex.inputs['Vector'])
        two = nt.nodes.new('ShaderNodeVectorMath')
        two.operation, two.location = 'SCALE', (-250, y + 300)
        two.inputs['Scale'].default_value = 2.0
        nt.links.new(tex.outputs['Color'], two.inputs[0])
        gam = nt.nodes.new('ShaderNodeGamma')
        gam.location = (-100, y + 300)
        gam.inputs['Gamma'].default_value = 2.2
        nt.links.new(two.outputs[0], gam.inputs['Color'])
        mix = nt.nodes.new('ShaderNodeMix')
        mix.data_type, mix.blend_type, mix.location = 'RGBA', 'MULTIPLY', (50, y + 150)
        mix.inputs['Factor'].default_value = 1.0
        nt.links.new(color, mix.inputs['A'])
        nt.links.new(gam.outputs[0], mix.inputs['B'])
        mat['mag_detail_color'] = list(scale)
        return mix.outputs['Result']


LIGHT_ATTR = 'mag_light'   # per-vertex (sky, sun, 0, 1) of the placement's baked lighting
# The game's lighting formula is not decoded. These weights put the median sunlit vertex of the
# disc (sky 0.81, sun 0.38) at 1.0; the median shadowed one (sky 0.36, sun 0) comes out at 0.27.
LIGHT_SKY_WEIGHT = 0.75
LIGHT_SUN_WEIGHT = 1.0


def baked_light_group():
    """Shared 'MAG Baked Light' group: Light colour (the ``mag_light`` attribute: sky in R, sun in
    G) -> Factor = Sky Weight * sky + Sun Weight * sun. The weights are value nodes inside the
    group, so one edit changes every lit material of the file."""
    ng = _current_group('MAG Baked Light', 'Light')
    if ng is not None:
        return ng
    ng = bpy.data.node_groups.new('MAG Baked Light', 'ShaderNodeTree')
    ng.interface.new_socket('Light', in_out='INPUT', socket_type='NodeSocketColor')
    ng.interface.new_socket('Factor', in_out='OUTPUT', socket_type='NodeSocketFloat')
    nodes, links = ng.nodes, ng.links
    gi = nodes.new('NodeGroupInput')
    gi.location = (-600, 0)
    go = nodes.new('NodeGroupOutput')
    go.location = (400, 0)
    sep = nodes.new('ShaderNodeSeparateColor')
    sep.location = (-400, 0)
    links.new(gi.outputs['Light'], sep.inputs[0])
    terms = []
    for k, (label, w) in enumerate((('Sky Weight', LIGHT_SKY_WEIGHT), ('Sun Weight', LIGHT_SUN_WEIGHT))):
        v = nodes.new('ShaderNodeValue')
        v.name = v.label = label
        v.outputs[0].default_value = w
        v.location = (-400, -200 - 120 * k)
        m = nodes.new('ShaderNodeMath')
        m.operation = 'MULTIPLY'
        m.location = (-150, -100 * k)
        links.new(sep.outputs[k], m.inputs[0])
        links.new(v.outputs[0], m.inputs[1])
        terms.append(m)
    add = nodes.new('ShaderNodeMath')
    add.operation = 'ADD'
    add.location = (150, 0)
    links.new(terms[0].outputs[0], add.inputs[0])
    links.new(terms[1].outputs[0], add.inputs[1])
    links.new(add.outputs[0], go.inputs['Factor'])
    return ng


def _apply_baked_light(nt, bsdf):
    """Multiply the base colour by the *MAG Baked Light* factor of the ``mag_light`` attribute.
    Meshes of vertex-lit materials always carry the attribute (1 where nothing is baked)."""
    inp = bsdf.inputs['Base Color']
    attr = nt.nodes.new('ShaderNodeAttribute')
    attr.attribute_type = 'GEOMETRY'
    attr.attribute_name = LIGHT_ATTR
    attr.label = 'Baked Light'
    attr.location = (-500, 1100)
    grp = nt.nodes.new('ShaderNodeGroup')
    grp.node_tree = baked_light_group()
    grp.location = (-250, 1100)
    nt.links.new(attr.outputs['Color'], grp.inputs['Light'])
    mix = nt.nodes.new('ShaderNodeMix')
    mix.data_type = 'RGBA'
    mix.blend_type = 'MULTIPLY'
    mix.label = 'Baked Light'
    mix.location = (0, 1000)
    mix.inputs['Factor'].default_value = 1.0
    if inp.is_linked:
        nt.links.new(inp.links[0].from_socket, mix.inputs['A'])
    else:
        mix.inputs['A'].default_value = inp.default_value
    nt.links.new(grp.outputs['Factor'], mix.inputs['B'])
    nt.links.new(mix.outputs['Result'], inp)


def _current_group(name, socket):
    """The node group ``name`` if it has the input ``socket``. A group saved by an older version
    of the importer lacks it: rename it aside (materials using it keep working) and return None."""
    ng = bpy.data.node_groups.get(name)
    if ng is None:
        return None
    if any(getattr(i, 'in_out', '') == 'INPUT' and i.name == socket for i in ng.interface.items_tree):
        return ng
    ng.name = name + ' (old)'
    return None


def normal_group(flip_green=False):
    """Shared 'MAG Normal' node group: decodes a normal map (DXT5nm: X in alpha, Y in green; RGB:
    X in red) to tangent X/Y in -1..1, adds a detail normal map's X/Y when *Detail* is 1 (as in
    UDN blending), rebuilds Z from the base map and outputs the colour for a Normal Map node
    (which normalises). One group instead of ~20 nodes per material keeps imports fast."""
    name = 'MAG Normal' + (' (flip G)' if flip_green else '')
    ng = _current_group(name, 'Detail DXT5nm')
    if ng is not None:
        return ng
    ng = bpy.data.node_groups.new(name, 'ShaderNodeTree')
    io = ng.interface
    for pre in ('', 'Detail '):
        io.new_socket(pre + 'Color', in_out='INPUT', socket_type='NodeSocketColor')
        io.new_socket(pre + 'Alpha', in_out='INPUT', socket_type='NodeSocketFloat')
        s = io.new_socket(pre + 'DXT5nm', in_out='INPUT', socket_type='NodeSocketFloat')
        s.default_value, s.min_value, s.max_value = 1.0, 0.0, 1.0
    s = io.new_socket('Detail', in_out='INPUT', socket_type='NodeSocketFloat')
    s.default_value, s.min_value, s.max_value = 0.0, 0.0, 1.0
    io.new_socket('Normal', in_out='OUTPUT', socket_type='NodeSocketColor')
    nodes, links = ng.nodes, ng.links
    gi = nodes.new('NodeGroupInput')
    gi.location = (-1000, 0)
    go = nodes.new('NodeGroupOutput')
    go.location = (900, 0)
    col = [0]

    def math(op, a, b=None, c=None):
        n = nodes.new('ShaderNodeMath')
        n.operation = op
        col[0] += 1
        n.location = (-500 + 130 * (col[0] % 10), 300 - 160 * (col[0] // 10))
        for i, v in enumerate((a, b, c)):
            if v is None:
                continue
            if isinstance(v, (int, float)):
                n.inputs[i].default_value = v
            else:
                links.new(v, n.inputs[i])
        return n.outputs[0]

    def xy(pre, y):
        sep = nodes.new('ShaderNodeSeparateColor')
        sep.location = (-750, y)
        links.new(gi.outputs[pre + 'Color'], sep.inputs[0])
        mix = nodes.new('ShaderNodeMix')  # float mix: exactly red at 0, alpha at 1
        mix.data_type, mix.location = 'FLOAT', (-600, y)
        links.new(gi.outputs[pre + 'DXT5nm'], mix.inputs['Factor'])
        links.new(sep.outputs['Red'], mix.inputs['A'])
        links.new(gi.outputs[pre + 'Alpha'], mix.inputs['B'])
        x = math('MULTIPLY_ADD', mix.outputs['Result'], 2.0, -1.0)
        g = sep.outputs['Green']
        if flip_green:
            g = math('SUBTRACT', 1.0, g)
        return x, math('MULTIPLY_ADD', g, 2.0, -1.0)

    x, yy = xy('', 200)
    dx, dy = xy('Detail ', -300)
    z = math('SQRT', math('MAXIMUM', math('SUBTRACT', 1.0, math('ADD', math('MULTIPLY', x, x),
                                                                  math('MULTIPLY', yy, yy))), 0.0))
    x = math('MULTIPLY_ADD', dx, gi.outputs['Detail'], x)
    yy = math('MULTIPLY_ADD', dy, gi.outputs['Detail'], yy)
    comb = nodes.new('ShaderNodeCombineColor')
    comb.location = (700, 0)
    links.new(math('MULTIPLY_ADD', x, 0.5, 0.5), comb.inputs[0])
    links.new(math('MULTIPLY_ADD', yy, 0.5, 0.5), comb.inputs[1])
    links.new(math('MULTIPLY_ADD', z, 0.5, 0.5), comb.inputs[2])
    links.new(comb.outputs[0], go.inputs['Normal'])
    return ng


TERRAIN_FADE_RATIO = 2.0  # the macro fade ends at this multiple of its start distance


def set_terrain_fade(ng, start):
    """Set the camera distances of the layers -> macro fade in a *MAG Terrain* group
    (``start <= 0`` turns it off)."""
    s, e = (start, start * TERRAIN_FADE_RATIO) if start > 0 else (1e9, 2e9)
    # Only write changes: every write re-tags all materials using the shared group, which made
    # setting it once per terrain tile cost ~1 s per map.
    for node, v in (('Macro Fade Start', s), ('Macro Fade End', e)):
        sock = ng.nodes[node].outputs[0]
        if abs(sock.default_value - v) > 1e-4 * max(1.0, abs(v)):  # float32 socket
            sock.default_value = v


def terrain_group(flip_green=False):
    """Shared 'MAG Terrain' node group: normalised RGBA weights blend four colour layers and four
    DXT5nm normal layers (X in alpha, Y in green; XY blended, Z rebuilt). Outputs a colour and a
    tangent-space normal colour for a Normal Map node.

    With distance from the camera (*Camera Data* View Distance) the result fades to the tile's
    macro colour and a flat normal, between the *Macro Fade Start* and *End* value nodes inside
    the group, as the game draws distant terrain with the macro colour."""
    name = 'MAG Terrain' + (' (flip G)' if flip_green else '')
    ng = _current_group(name, 'Macro')
    if ng is not None:
        return ng
    ng = bpy.data.node_groups.new(name, 'ShaderNodeTree')
    io = ng.interface
    io.new_socket('Weights', in_out='INPUT', socket_type='NodeSocketColor')
    io.new_socket('Weights Alpha', in_out='INPUT', socket_type='NodeSocketFloat')
    io.new_socket('Macro', in_out='INPUT', socket_type='NodeSocketColor')
    for k in range(4):
        io.new_socket('Color %d' % k, in_out='INPUT', socket_type='NodeSocketColor')
    for k in range(4):
        io.new_socket('Normal %d' % k, in_out='INPUT', socket_type='NodeSocketColor')
        io.new_socket('Normal %d Alpha' % k, in_out='INPUT', socket_type='NodeSocketFloat')
    io.new_socket('Color', in_out='OUTPUT', socket_type='NodeSocketColor')
    io.new_socket('Normal', in_out='OUTPUT', socket_type='NodeSocketColor')
    nodes, links = ng.nodes, ng.links
    gi = nodes.new('NodeGroupInput')
    gi.location = (-1600, 0)
    go = nodes.new('NodeGroupOutput')
    go.location = (1200, 0)
    count = [0]

    def node(kind, **kw):
        n = nodes.new(kind)
        count[0] += 1
        n.location = (-1400 + 160 * (count[0] % 16), 600 - 220 * (count[0] // 16))
        for k, v in kw.items():
            setattr(n, k, v)
        return n

    def wire(n, i, v):
        if isinstance(v, (int, float, tuple)):
            n.inputs[i].default_value = v
        else:
            links.new(v, n.inputs[i])

    def math(op, a, b=None, c=None):
        n = node('ShaderNodeMath', operation=op)
        for i, v in enumerate((a, b, c)):
            if v is not None:
                wire(n, i, v)
        return n.outputs[0]

    def vmath(op, a, b=None, scale=None, c=None):
        n = node('ShaderNodeVectorMath', operation=op)
        for i, v in enumerate((a, b, c)):
            if v is not None:
                wire(n, i, v)
        if scale is not None:
            wire(n, 'Scale', scale)
        return n.outputs['Vector']

    sw = node('ShaderNodeSeparateColor')
    links.new(gi.outputs['Weights'], sw.inputs[0])
    chans = [sw.outputs['Red'], sw.outputs['Green'], sw.outputs['Blue'], gi.outputs['Weights Alpha']]
    total = math('ADD', math('ADD', chans[0], chans[1]), math('ADD', chans[2], chans[3]))
    inv = math('DIVIDE', 1.0, math('MAXIMUM', total, 1e-3))
    w = [math('MULTIPLY', ch, inv) for ch in chans]
    colour = nrm = None
    for k in range(4):
        c = vmath('SCALE', gi.outputs['Color %d' % k], scale=w[k])
        colour = c if colour is None else vmath('ADD', colour, c)
        sn = node('ShaderNodeSeparateColor')
        links.new(gi.outputs['Normal %d' % k], sn.inputs[0])
        g = sn.outputs['Green']
        if flip_green:
            g = math('SUBTRACT', 1.0, g)
        xy = node('ShaderNodeCombineXYZ')
        wire(xy, 0, math('MULTIPLY_ADD', gi.outputs['Normal %d Alpha' % k], 2.0, -1.0))
        wire(xy, 1, math('MULTIPLY_ADD', g, 2.0, -1.0))
        v = vmath('SCALE', xy.outputs[0], scale=w[k])
        nrm = v if nrm is None else vmath('ADD', nrm, v)
    sx = node('ShaderNodeSeparateXYZ')
    links.new(nrm, sx.inputs[0])
    x, y = sx.outputs['X'], sx.outputs['Y']
    z = math('SQRT', math('MAXIMUM', math('SUBTRACT', 1.0, math('ADD', math('MULTIPLY', x, x),
                                                                     math('MULTIPLY', y, y))), 0.0))
    cz = node('ShaderNodeCombineXYZ')
    links.new(x, cz.inputs[0])
    links.new(y, cz.inputs[1])
    links.new(z, cz.inputs[2])
    enc = vmath('MULTIPLY_ADD', cz.outputs[0], (0.5, 0.5, 0.5), c=(0.5, 0.5, 0.5))
    # distance fade: 0 near (layers), 1 far (macro colour, flat normal)
    cam = node('ShaderNodeCameraData')
    fs = node('ShaderNodeValue', name='Macro Fade Start', label='Macro Fade Start (m)')
    fe = node('ShaderNodeValue', name='Macro Fade End', label='Macro Fade End (m)')
    mr = node('ShaderNodeMapRange', clamp=True)
    links.new(cam.outputs['View Distance'], mr.inputs['Value'])
    links.new(fs.outputs[0], mr.inputs['From Min'])
    links.new(fe.outputs[0], mr.inputs['From Max'])
    mr.inputs['To Min'].default_value, mr.inputs['To Max'].default_value = 0.0, 1.0
    fade = mr.outputs['Result']
    mc = node('ShaderNodeMix', data_type='RGBA')
    links.new(fade, mc.inputs['Factor'])
    links.new(colour, mc.inputs['A'])
    links.new(gi.outputs['Macro'], mc.inputs['B'])
    mn = node('ShaderNodeMix', data_type='RGBA')
    links.new(fade, mn.inputs['Factor'])
    links.new(enc, mn.inputs['A'])
    mn.inputs['B'].default_value = (0.5, 0.5, 1.0, 1.0)
    links.new(mc.outputs['Result'], go.inputs['Color'])
    links.new(mn.outputs['Result'], go.inputs['Normal'])
    set_terrain_fade(ng, 250.0)
    return ng


# -- static / level models ---------------------------------------------------------------------------
def _mesh_parts(model, geoms, mesh_ids, mcache, zi):
    """(parts, materials) for make_mesh: one material slot per distinct material hash."""
    slot_of, mats, parts = {}, [], []
    for i in mesh_ids:
        mh = model.meshes[i].material
        if mh not in slot_of:
            slot_of[mh] = len(mats)
            mats.append(mcache.get(mh, zi))
        parts.append((geoms[i], slot_of[mh]))
    return parts, mats


def _lod_name(name, k):
    return '%s_LOD%d' % (_safe(name, 54), k)


def build_model_objects(lib, zi, ri, label, opts, mcache, collection, lower_lods=False):
    """Create objects for a (non-skinned or skinned-as-static) model. Returns the root object.

    ``lower_lods``: also build the coarser LODs, in one hidden ``<collection>_LOD<n>`` child
    collection each, parented to their node's object."""
    model, geoms = lib.model(zi, ri)
    keep = _select_meshes(model, geoms, opts.lod)
    extra = _extra_lods(model, geoms, keep, opts.lod) if lower_lods else []
    lod_colls = _LodCollections(collection) if extra else None
    objs = {}
    root = None
    single = len(model.nodes) == 1
    for node in model.nodes:
        span = range(node.mesh_start, node.mesh_start + node.mesh_count)
        mesh_ids = [i for i in span if i in keep]
        name = label if single else '%s.%s' % (label, lib.index.label(node.name, 'node%d' % node.index))
        if mesh_ids:
            parts, mats = _mesh_parts(model, geoms, mesh_ids, mcache, zi)
            me = make_mesh(_safe(name), parts, mats)
            if opts.baked_lighting and any(mcache.vertex_lit(model.meshes[i].material, zi) for i in mesh_ids):
                set_light(me, np.ones((len(me.vertices), 2), np.float32))
            ob = bpy.data.objects.new(_safe(name), me)
            # the model's mesh entries behind this object's vertices, in order (baked lighting)
            ob['mag_meshes'] = [i for i in mesh_ids if geoms[i] is not None and len(geoms[i].positions)]
        else:
            ob = bpy.data.objects.new(_safe(name), None)
            ob.empty_display_size = 0.05
        L = np.eye(4)
        L[:3, :3] = qmat(node.rotation)
        L[:3, 3] = node.translation
        ob.matrix_parent_inverse = Matrix.Identity(4)
        ob.matrix_basis = Matrix(to_blender_matrix(L).tolist())
        collection.objects.link(ob)
        objs[node.index] = ob
        if node.parent >= 0 and node.parent in objs:
            ob.parent = objs[node.parent]
        elif root is None:
            root = ob
        for k, ids in extra:
            lod_ids = [i for i in span if i in ids]
            if not lod_ids or lod_ids == mesh_ids:
                continue
            parts, mats = _mesh_parts(model, geoms, lod_ids, mcache, zi)
            lob = bpy.data.objects.new(_lod_name(name, k), make_mesh(_lod_name(name, k), parts, mats))
            lob['mag_lod'] = k
            lob.parent = ob
            lob.matrix_parent_inverse = Matrix.Identity(4)
            lod_colls.get(k).objects.link(lob)
    # prune empty leaf nodes (attachment helpers) to keep the outliner tidy
    for idx in sorted(objs, reverse=True):
        ob = objs[idx]
        if ob.type == 'EMPTY' and not ob.children and ob is not root:
            bpy.data.objects.remove(ob)
    if root is None:
        root = next(iter(objs.values()))
    root['mag_model'] = '%08x' % model.name
    return root


# -- armatures --------------------------------------------------------------------------------------
def build_armature(skel, collection, name=None):
    arm = bpy.data.armatures.new(name or skel.name)
    ob = bpy.data.objects.new(name or skel.name, arm)
    ob['mag_skeleton'] = skel.name
    collection.objects.link(ob)
    W = skeleton_world(skel)
    prev_active = bpy.context.view_layer.objects.active
    bpy.context.view_layer.objects.active = ob
    bpy.ops.object.mode_set(mode='EDIT')
    ebs = []
    kids = {}
    for i, p in enumerate(skel.parents):
        kids.setdefault(p, []).append(i)
    names = []
    for i, bname in enumerate(skel.bones):
        nm = bname or 'bone%d' % i
        while nm in names:
            nm += '_'
        names.append(nm)
        eb = arm.edit_bones.new(nm)
        P = W[i][:3, 3]
        ch = kids.get(i, [])
        L = max([np.linalg.norm(W[c][:3, 3] - P) for c in ch] + [0.0])
        L = min(max(L, 0.03), 0.3) if ch else 0.05
        eb.head = (0, 0, 0)
        eb.tail = (0, L, 0)
        M = to_blender_matrix(W[i])
        M[:3, :3] /= np.maximum(np.linalg.norm(M[:3, :3], axis=0, keepdims=True), 1e-9)
        eb.matrix = Matrix(M.tolist())
        ebs.append(eb)
    for i, p in enumerate(skel.parents):
        if p >= 0:
            ebs[i].parent = ebs[p]
    bpy.ops.object.mode_set(mode='OBJECT')
    bpy.context.view_layer.objects.active = prev_active
    arm.display_type = 'OCTAHEDRAL'
    ob.show_in_front = True
    ob['mag_bones'] = names
    return ob


def choose_skeleton(lib, geoms, wanted='AUTO'):
    if wanted and wanted != 'AUTO':
        return lib.skeleton(wanted)
    best, best_s = None, 1e18
    for nm in lib.skeleton_names():
        sk = lib.skeleton(nm)
        if sk is None:
            continue
        s = fit_score(sk, geoms)
        if s < best_s:
            best, best_s = sk, s
    return best


def _skin(ob, parts, arm_ob):
    """Vertex groups from the parts' joints/weights and an Armature modifier; parents ob to arm_ob."""
    names = list(arm_ob['mag_bones'])
    # vertex groups: sum weights per (vertex, bone), then add per distinct weight value
    J = np.concatenate([g.joints for g, _ in parts if g.joints is not None]) if all(g.joints is not None for g, _ in parts) else None
    if J is not None:
        Wt = np.concatenate([g.weights for g, _ in parts])
        nb = len(names)
        dense = np.zeros((len(J), nb), np.float32)
        valid = (J < nb) & (J >= 0)
        rows = np.repeat(np.arange(len(J)), 4).reshape(-1, 4)
        np.add.at(dense, (rows[valid], J[valid]), Wt[valid])
        s = dense.sum(1, keepdims=True)
        dense = np.divide(dense, s, out=dense, where=s > 0)
        for b in np.nonzero(dense.any(0))[0]:
            vg = ob.vertex_groups.new(name=names[b])
            col = dense[:, b]
            nz = np.nonzero(col > 0)[0]
            q = np.round(col[nz] * 255).astype(np.int32)
            for w in np.unique(q):
                sel = nz[q == w]
                vg.add(sel.tolist(), float(w) / 255.0, 'REPLACE')
    ob.parent = arm_ob
    mod = ob.modifiers.new('Armature', 'ARMATURE')
    mod.object = arm_ob


def build_skinned(lib, zi, ri, label, opts, mcache, collection, lower_lods=False):
    model, geoms = lib.model(zi, ri)
    keep = _select_meshes(model, geoms, opts.lod)
    parts, mats = _mesh_parts(model, geoms, sorted(keep), mcache, zi)
    me = make_mesh(_safe(label), parts, mats)
    ob = bpy.data.objects.new(_safe(label), me)
    collection.objects.link(ob)
    skel = choose_skeleton(lib, [g for g, _ in parts], opts.skeleton)
    if skel is None:
        return ob
    arm_ob = None
    act = bpy.context.view_layer.objects.active
    if act is not None and act.type == 'ARMATURE' and act.get('mag_skeleton') == skel.name:
        arm_ob = act
    if arm_ob is None:
        arm_ob = build_armature(skel, collection, '%s_rig' % _safe(label, 50))
    _skin(ob, parts, arm_ob)
    ob['mag_model'] = '%08x' % model.name
    extra = _extra_lods(model, geoms, keep, opts.lod) if lower_lods else []
    lod_colls = _LodCollections(collection) if extra else None
    for k, ids in extra:
        lparts, lmats = _mesh_parts(model, geoms, sorted(ids), mcache, zi)
        lob = bpy.data.objects.new(_lod_name(label, k), make_mesh(_lod_name(label, k), lparts, lmats))
        lob['mag_lod'] = k
        lod_colls.get(k).objects.link(lob)
        _skin(lob, lparts, arm_ob)
    return arm_ob


# -- animation ---------------------------------------------------------------------------------------
def _qmul(a, b):
    """Hamilton product for (...,4) xyzw quaternions."""
    ax, ay, az, aw = np.moveaxis(a, -1, 0)
    bx, by, bz, bw = np.moveaxis(b, -1, 0)
    return np.stack([aw * bx + ax * bw + ay * bz - az * by,
                     aw * by - ax * bz + ay * bw + az * bx,
                     aw * bz + ax * by - ay * bx + az * bw,
                     aw * bw - ax * bx - ay * by - az * bz], -1)


def _qconj(q):
    return q * np.array([-1, -1, -1, 1], q.dtype)


def _qrot(q, v):
    qv = np.concatenate([v, np.zeros(v.shape[:-1] + (1,), v.dtype)], -1)
    return _qmul(_qmul(q, qv), _qconj(q))[..., :3]


_LINEAR = None


def build_action(anim, skel, arm_ob, scene):
    """Bake a decoded Havok animation onto an armature as a (slotted) action."""
    global _LINEAR
    if _LINEAR is None:
        _LINEAR = bpy.types.Keyframe.bl_rna.properties['interpolation'].enum_items['LINEAR'].value
    names = list(arm_ob['mag_bones'])
    nm = re.split(r'[\\/]', anim.name)[-1].rsplit('.', 1)[0] if anim.name else 'mag_anim'
    action = bpy.data.actions.new(_safe(nm))
    action['mag_skeleton'] = skel.name
    ad = arm_ob.animation_data or arm_ob.animation_data_create()
    ad.action = action
    fps = scene.render.fps / scene.render.fps_base
    step = anim.frame_duration * fps
    if abs(step - round(step)) < 1e-3:
        step = float(round(step))  # 1/30 s at 30 fps: whole frames, no float drift
    frames = 1.0 + np.arange(anim.num_frames) * step
    rest_q = skel.rotations.astype(np.float64)
    rest_t = skel.translations.astype(np.float64)
    rest_s = skel.scales.astype(np.float64)
    for ti, b in enumerate(anim.track_to_bone):
        if b < 0 or b >= len(names) or ti >= len(anim.translations):
            continue
        pb = arm_ob.pose.bones.get(names[b])
        if pb is None:
            continue
        pb.rotation_mode = 'QUATERNION'
        qa = anim.rotations[ti].astype(np.float64)
        ta = anim.translations[ti].astype(np.float64)
        sa = anim.scales[ti].astype(np.float64)
        # basis in game space: inv(rest_local) @ anim_local
        qi = _qconj(rest_q[b])[None, :]
        qb = _qmul(np.broadcast_to(qi, qa.shape), qa)
        tb = _qrot(np.broadcast_to(qi, (len(ta), 4)), ta - rest_t[b][None, :])
        sb = sa / np.where(np.abs(rest_s[b]) > 1e-8, rest_s[b], 1.0)[None, :]
        # bone matrices are C @ M @ C^T (see build_armature), so the basis is conjugated too:
        # vector parts go through the same (x, y, z) -> (x, -z, y) map
        qb = np.concatenate([to_blender_vec(qb[:, :3]), qb[:, 3:]], 1)
        tb = to_blender_vec(tb)
        sb = np.abs(to_blender_vec(sb))
        # keep the quaternion path continuous (q and -q are the same rotation)
        for f in range(1, len(qb)):
            if np.dot(qb[f], qb[f - 1]) < 0:
                qb[f] = -qb[f]
        path = 'pose.bones["%s"]' % names[b]
        chans = [('rotation_quaternion', 0, qb[:, 3]), ('rotation_quaternion', 1, qb[:, 0]),
                 ('rotation_quaternion', 2, qb[:, 1]), ('rotation_quaternion', 3, qb[:, 2])]
        if np.abs(tb).max() > 1e-5:
            chans += [('location', i, tb[:, i]) for i in range(3)]
        if np.abs(sb - 1.0).max() > 1e-4:
            chans += [('scale', i, sb[:, i]) for i in range(3)]
        for prop, idx, vals in chans:
            fc = action.fcurve_ensure_for_datablock(arm_ob, '%s.%s' % (path, prop), index=idx, group_name=names[b])
            kp = fc.keyframe_points
            kp.add(len(vals))
            co = np.empty(2 * len(vals), np.float32)
            co[0::2] = frames
            co[1::2] = vals
            kp.foreach_set('co', co)
            kp.foreach_set('interpolation', np.full(len(vals), _LINEAR, np.int32))
            fc.update()
    action.use_frame_range = True
    action.frame_start = 1
    action.frame_end = max(1.0, float(frames[-1]))
    return action


# -- maps ----------------------------------------------------------------------------------------------
def _hidden_assets_collection(scene):
    name = 'MAG Assets'
    coll = bpy.data.collections.get(name)
    if coll is None:
        coll = bpy.data.collections.new(name)
        scene.collection.children.link(coll)
    # exclude from the view layer: instanced, never drawn directly
    lc = _find_layer_collection(bpy.context.view_layer.layer_collection, coll)
    if lc is not None:
        lc.exclude = True
    return coll


def _chain_matrix(ob):
    """World matrix from the parent chain (objects in excluded collections are not evaluated)."""
    m = ob.matrix_basis.copy()
    p = ob.parent
    while p is not None:
        m = p.matrix_basis @ ob.matrix_parent_inverse @ m
        ob, p = p, p.parent
    return m


def _find_layer_collection(lc, coll):
    if lc.collection == coll:
        return lc
    for ch in lc.children:
        r = _find_layer_collection(ch, coll)
        if r is not None:
            return r
    return None


def terrain_tiles(lv, lod=0):
    """(zi, ri) terrain tiles of a level at a level of detail of its terrain tree: 0 is the
    finest, each step up halves the tile count; clamped to the coarsest level."""
    lods = lv.get('terrain_lods') or []
    if not lods:
        return lv.get('terrain', [])
    return lods[max(0, min(int(lod), len(lods) - 1))]


def _terrain_square(lib, lv):
    """The ground the level's terrain covers (game x/z), from its coarsest tiles' boxes."""
    lods = lv.get('terrain_lods') or [lv.get('terrain') or []]
    try:
        boxes = [lib.model_header(zi, ri).bounds for zi, ri in lods[-1]]
    except Exception as e:
        print('MAG: terrain bounds failed: %s' % e)
        return None
    return lvl.terrain_square(boxes) if boxes else None


def _piece_centre(lib, zi, ri):
    """Centre of a piece's box (model space); None for models without meshes (no box)."""
    try:
        m = lib.model_header(zi, ri)
    except Exception:
        return None
    return np.array(m.bounds[:3], float) if m.meshes else None


def build_map(lib, level_index, opts, mcache, scene, progress=None):
    idx = lib.index
    lv = idx.levels[level_index]
    arch = lv['archive']
    name = lv['name'].replace('mag_', '').replace('_final', '')
    map_coll = bpy.data.collections.new('MAG_' + name)
    scene.collection.children.link(map_coll)
    mesh_hashes = lv['mesh']
    uniq = np.unique(mesh_hashes)
    hits = {}
    for h in uniq.tolist():
        hit = idx.find_in_archive(h, MODEL_TYPES, arch)
        if hit is not None:
            hits[h] = hit
    terrain = terrain_tiles(lv, opts.terrain_lod) if opts.include_terrain else []
    lib.prefetch([hz for _, hz, _ in hits.values()] + [zi for zi, _ in terrain])
    if opts.materials:  # convert missing textures in parallel (first import of a map)
        pairs = []
        for zi, ri in [(hz, hr) for _, hz, hr in hits.values()] + list(terrain):
            try:
                pairs.extend((me.material, zi) for me in lib.model_header(zi, ri).meshes)
            except Exception as e:
                print('MAG: texture prefetch skipped %d/%d: %s' % (zi, ri, e))
        n = mcache.prefetch(pairs)
        if n:
            print('MAG: converted %d textures in parallel' % n)
    if terrain:
        tcoll = bpy.data.collections.new('MAG_%s_terrain' % name)
        map_coll.children.link(tcoll)
        for k, (zi, ri) in enumerate(terrain):
            try:
                build_model_objects(lib, zi, ri, '%s_terrain_%04d' % (name, k), opts, mcache, tcoll)
            except Exception as e:
                print('MAG: terrain tile %d failed: %s' % (k, e))
    assets = _hidden_assets_collection(scene)
    lib_coll = bpy.data.collections.new('MAG_%s_pieces' % name)
    assets.children.link(lib_coll)
    piece_coll = {}
    total = len(hits) + len(mesh_hashes)
    done = 0
    for h, (rt, zi, ri) in hits.items():
        c = bpy.data.collections.new('%08x' % h)
        lib_coll.children.link(c)
        try:
            label = idx.label(h, '%s_%08x' % (name, h))
            build_model_objects(lib, zi, ri, label, opts, mcache, c)
            piece_coll[h] = c
        except Exception as e:  # keep going: one bad piece must not stop the map
            print('MAG: piece %08x failed: %s' % (h, e))
        done += 1
        if progress and done % 50 == 0:
            progress(done / total)
    # scenery outside the playable terrain (mountains, treeline cards) goes to its own collection
    square = _terrain_square(lib, lv)
    centre = {h: _piece_centre(lib, *hits[h][1:]) for h in piece_coll}
    # placed pieces get their own child collection: loose objects of map_coll are listed in the
    # Outliner after its last child collection (the sky), as if they were part of it
    placed = bpy.data.collections.new('MAG_%s_objects' % name)
    map_coll.children.link(placed)
    backdrop = bpy.data.collections.new('MAG_%s_backdrop' % name)
    map_coll.children.link(backdrop)
    trs = lv['trs']
    skipped = []  # placement indices without a model
    light = _MapLighting(lib, lv, hits, mcache) if opts.baked_lighting else None
    for i, h in enumerate(mesh_hashes.tolist()):
        c = piece_coll.get(h)
        if c is None:
            skipped.append(i)
            continue
        G = lvl.trs_matrix(trs[i])
        M = Matrix(to_blender_matrix(G).tolist())
        target = placed
        if square is not None and centre.get(h) is not None:
            w = G[:3, :3] @ centre[h] + G[:3, 3]
            if lvl.outside_distance(w[0], w[2], square) > lvl.BACKDROP_MARGIN:
                target = backdrop
        if light is not None and light.place(i, h, c, M, target):
            pass  # own mesh copies carrying this placement's baked lighting
        elif opts.instance_maps:
            ob = bpy.data.objects.new(c.name, None)
            ob.instance_type = 'COLLECTION'
            ob.instance_collection = c
            ob.empty_display_size = 0.5
            ob.matrix_world = M
            target.objects.link(ob)
        else:  # linked duplicates (shared mesh data) of every mesh object of the piece
            for src in c.objects:
                if src.type != 'MESH':
                    continue
                ob = bpy.data.objects.new(src.name, src.data)
                ob.matrix_world = M @ _chain_matrix(src)
                target.objects.link(ob)
        done += 1
        if progress and done % 500 == 0:
            progress(done / total)
    stats = _report_skipped(idx, name, mesh_hashes, trs, skipped, opts, map_coll)
    stats['placed'] = len(mesh_hashes) - len(skipped)
    stats['lit'] = light.placed if light else 0
    if light is not None:
        print('MAG: %s baked lighting: %d placements lit (%d with a fitted layout), %d records '
              'with an unknown layout skipped' % (name, light.placed, light.fit, light.unmatched))
    stats['foliage'] = 0
    if opts.include_foliage:
        try:
            stats['foliage'] = build_foliage(lib, lv, name, opts, mcache, map_coll, lib_coll)
        except Exception as e:
            print('MAG: foliage failed: %s' % e)
    stats['sky'] = ''
    if opts.sky in ('WORLD', 'DOME'):
        try:
            stats['sky'] = build_sky(lib, lv, name, opts, mcache, scene, map_coll)
        except Exception as e:
            print('MAG: sky failed: %s' % e)
    stats['collision'] = 0
    if opts.collision:
        try:
            stats['collision'] = build_collision(lib, lv, name, map_coll)
        except Exception as e:
            print('MAG: collision failed: %s' % e)
    map_coll['mag_level'] = lv['name']
    return map_coll, stats


# -- baked lighting -------------------------------------------------------------------------------
class _MapLighting:
    """Places lit placements as objects with their own mesh copies carrying ``mag_light``."""

    def __init__(self, lib, lv, hits, mcache):
        from ..core.zrb import T_LIGHTING
        idx = lib.index
        ai = lv['archive']
        self.lib, self.hits, self.mcache = lib, hits, mcache
        self.recs = {r[8]: (zi, ri) for zi, z in enumerate(idx.zrbs) if z.archive == ai
                     for ri, r in enumerate(z.records) if r[7] == T_LIGHTING}
        per = lit.placement_lighting(lib.archive(ai).read(lv['clv']), set(self.recs)) if self.recs else []
        if len(per) != len(lv['mesh']):
            if self.recs:
                print('MAG: baked lighting placements %d != %d, ignored' % (len(per), len(lv['mesh'])))
            per = [0] * len(lv['mesh'])
        self.per = per
        self.mesh = lv['mesh']
        self.layout = {}  # model hash -> ({mesh index: offset}, total, [V per mesh]) or None
        self.fitted = {}  # (model hash, record length) -> ({mesh index: offset}, [V per mesh]) or None
        self.placed = self.unmatched = self.fit = 0

    def _layout(self, h):
        if h not in self.layout:
            _, zi, ri = self.hits[h]
            m = self.lib.model_header(zi, ri)
            offs, n = lit.lit_offsets(m, lambda mh: self.mcache.flags(mh, zi))
            self.layout[h] = (offs, n, [me.V for me in m.meshes]) if offs else None
        return self.layout[h]

    def _fit(self, h, n):
        """Layout of records of model ``h`` whose length ``n`` does not match its lit meshes,
        fitted from all of this level's records of that model and length (``lighting.fit_layout``)."""
        key = (h, n)
        if key not in self.fitted:
            self.fitted[key] = None
            _, zi, ri = self.hits[h]
            offs = self._layout(h)[0]
            m, geoms = self.lib.model(zi, ri)
            meshes = [(mi, geoms[mi].positions) for mi in sorted(offs) if geoms[mi] is not None]
            records = []
            for mh, rh in zip(self.mesh.tolist(), self.per):
                if rh and mh == h and len(records) < lit.FIT_RECORDS:
                    d = lit.read_lighting(*self.lib.record(*self.recs[rh]))
                    if len(d) == n:
                        records.append(d)
            if len(meshes) == len(offs) and records:
                fit, costs = lit.fit_layout(meshes, records)
                if fit is not None:
                    vs = [len(g.positions) if g is not None else me.V for me, g in zip(m.meshes, geoms)]
                    self.fitted[key] = (fit, vs)
                print('MAG: baked lighting %08x (%d entries, %d records): fitted %s, cost %s'
                      % (h, n, len(records), fit, {k: round(v, 2) for k, v in costs.items()}))
        return self.fitted[key]

    def place(self, i, h, coll, M, target):
        rh = self.per[i]
        if not rh or h not in self.hits:
            return False
        lay = self._layout(h)
        if lay is None:
            return False
        offs, n, vs = lay
        zi, ri = self.recs[rh]
        z, r = self.lib.record(zi, ri)
        data = lit.read_lighting(z, r)
        if len(data) != n:
            fit = self._fit(h, len(data))
            if fit is None:
                self.unmatched += 1
                return False
            offs, vs = fit
            self.fit += 1
        values = data.astype(np.float32) / 255.0
        made = False
        for src in coll.all_objects:
            if src.type != 'MESH':
                continue
            parts = []
            for mi in src.get('mag_meshes', ()):
                o = offs.get(mi)
                parts.append(values[o:o + vs[mi]] if o is not None else np.ones((vs[mi], 2), np.float32))
            me = src.data
            if parts and any(mi in offs for mi in src.get('mag_meshes', ())):
                v = np.concatenate(parts)
                if len(v) == len(me.vertices):
                    me = me.copy()
                    set_light(me, v)
            ob = bpy.data.objects.new(src.name, me)
            ob.matrix_world = M @ _chain_matrix(src)
            ob['mag_lighting'] = '%08x' % rh
            target.objects.link(ob)
            made = True
        self.placed += made
        return made


# -- collision ------------------------------------------------------------------------------------
COLLISION_COLOUR = (1.0, 0.45, 0.1, 1.0)


def _collision_mesh(name, cm):
    """bpy Mesh of a (welded) CollisionMesh, with the source packfile per face as 'mag_part'."""
    me = bpy.data.meshes.new(name)
    nv, nt = len(cm.positions), len(cm.triangles)
    me.vertices.add(nv)
    me.vertices.foreach_set('co', to_blender_vec(cm.positions).ravel())
    me.loops.add(nt * 3)
    me.loops.foreach_set('vertex_index', cm.triangles.ravel())
    me.polygons.add(nt)
    me.polygons.foreach_set('loop_start', np.arange(0, nt * 3, 3, dtype=np.int32))
    me.polygons.foreach_set('loop_total', np.full(nt, 3, np.int32))
    if nt and cm.part.max() > 0:
        me.attributes.new('mag_part', 'INT', 'FACE').data.foreach_set('value', cm.part)
    me.update()
    me.validate(clean_customdata=False)
    me['mag_collision'] = '%08x' % cm.name
    return me


def build_collision(lib, lv, name, map_coll):
    """The level's collision meshes (physics.zrb) in MAG_<map>_collision: one shared mesh per
    collision record, a linked duplicate per placement that names it, and the terrain collision
    (world space). Hidden from renders, drawn as wireframe. Returns the object count."""
    from ..core import collision as col
    from ..core.zrb import Zrb, T_COLLISION
    arc = lib.archive(lv['archive'])
    z = Zrb(arc.read(col.physics_zrb_name(lv['clv'])))
    recs = {r.name: r for r in z.records if r.type == T_COLLISION}
    per = col.placement_collisions(arc.read(lv['clv']), set(recs))
    if len(per) != len(lv['mesh']):
        raise ValueError('collision placements %d != %d' % (len(per), len(lv['mesh'])))
    used = set(per) - {0}
    lods = lv.get('terrain_lods') or [lv.get('terrain') or []]
    square = None
    if lods[-1]:
        square = lvl.terrain_square([lib.model_header(zi, ri).bounds for zi, ri in lods[-1]])
    decoded = {}

    def get(h):
        if h not in decoded:
            decoded[h] = col.weld(col.read_collision(z, recs[h]))
        return decoded[h]
    terrain = None
    if square is not None:
        unplaced = {h: col.read_collision(z, recs[h]) for h in recs if h not in used}
        terrain = col.terrain_record(unplaced, set(), square)
    coll = bpy.data.collections.new('MAG_%s_collision' % name)
    map_coll.children.link(coll)
    coll.hide_render = True
    meshes = {}
    n = 0

    def add(h, M, label=None):
        nonlocal n
        me = meshes.get(h)
        if me is None:
            me = meshes[h] = _collision_mesh('col_%08x' % h, get(h))
        ob = bpy.data.objects.new(label or 'col_%08x' % h, me)
        ob.display_type = 'WIRE'
        ob.color = COLLISION_COLOUR
        ob.hide_render = True
        ob.visible_shadow = False
        if M is not None:
            ob.matrix_world = M
        coll.objects.link(ob)
        n += 1
    if terrain is not None:
        add(terrain, None, 'col_terrain')
    trs = lv['trs']
    for i, h in enumerate(per):
        if h:
            add(h, Matrix(to_blender_matrix(lvl.trs_matrix(trs[i])).tolist()))
    print('MAG: %s collision: %d records, %d objects (%d unplaced records skipped)'
          % (name, len(recs), n, len(recs) - len(used) - (terrain is not None)))
    return n


def _report_skipped(idx, name, mesh_hashes, trs, skipped, opts, map_coll):
    """Classify placements that have no model: particle emitters (the hash names a particle
    effect record) or pieces missing from the disc. Optionally place an empty for each."""
    emit = collections.Counter()
    miss = collections.Counter()
    for i in skipped:
        h = int(mesh_hashes[i])
        (emit if h in idx.fx else miss)[h] += 1
    for kind, cnt in (('particle emitters', emit), ('missing pieces', miss)):
        if cnt:
            print('MAG: %s %s: %s' % (name, kind, ', '.join('%08x x%d' % kv for kv in sorted(cnt.items()))))
    if opts.place_emitters and skipped:
        fx = bpy.data.collections.new('MAG_%s_fx' % name)
        map_coll.children.link(fx)
        for i in skipped:
            h = int(mesh_hashes[i])
            ob = bpy.data.objects.new(('fx_%08x' if h in idx.fx else 'missing_%08x') % h, None)
            ob.empty_display_type = 'SPHERE'
            ob.empty_display_size = 0.5
            ob.matrix_world = Matrix(to_blender_matrix(lvl.trs_matrix(trs[i])).tolist())
            ob['mag_hash'] = '%08x' % h
            fx.objects.link(ob)
    return {'emitters': sum(emit.values()), 'missing': sum(miss.values())}


# -- foliage ---------------------------------------------------------------------------------------------
TINT_MIX = 0.5


FOLIAGE_INPUTS = ('Collection', 'Distance Fade', 'Fade Start', 'Fade End', 'Fade Scale')


def _foliage_node_group():
    """Shared Geometry Nodes group: instance a collection on every point, with the point's
    ``rotation`` (quaternion) and ``scale`` attributes. Point attributes (``tint``) carry over to
    the instances, where materials read them with an *Instancer* Attribute node.

    With *Distance Fade* on, each instance is scaled by ``1 - clamp((d - start) / (end - start))``
    of its distance ``d`` to the scene camera, the game's foliage fade (start and end times *Fade
    Scale*); instances past the end are removed before instancing."""
    ng = _current_group('MAG Foliage', 'Distance Fade')
    if ng is not None:
        return ng
    ng = bpy.data.node_groups.new('MAG Foliage', 'GeometryNodeTree')
    io = ng.interface
    io.new_socket('Geometry', in_out='INPUT', socket_type='NodeSocketGeometry')
    io.new_socket('Collection', in_out='INPUT', socket_type='NodeSocketCollection')
    io.new_socket('Distance Fade', in_out='INPUT', socket_type='NodeSocketBool')
    for nm, v in (('Fade Start', 50.0), ('Fade End', 60.0), ('Fade Scale', 1.0)):
        sk = io.new_socket(nm, in_out='INPUT', socket_type='NodeSocketFloat')
        sk.default_value, sk.min_value = v, 0.0
    io.new_socket('Geometry', in_out='OUTPUT', socket_type='NodeSocketGeometry')
    N, L = ng.nodes, ng.links
    gin = N.new('NodeGroupInput')
    gin.location = (-1400, 0)
    gout = N.new('NodeGroupOutput')
    gout.location = (600, 0)

    def math(op, a, b, loc):
        n = N.new('ShaderNodeMath')
        n.operation, n.location = op, loc
        for i, v in enumerate((a, b)):
            if isinstance(v, (int, float)):
                n.inputs[i].default_value = v
            else:
                L.new(v, n.inputs[i])
        return n.outputs[0]
    # distance to the scene camera, in the points object's space
    cam = N.new('GeometryNodeInputActiveCamera')
    cam.location = (-1200, -300)
    oi = N.new('GeometryNodeObjectInfo')
    oi.transform_space = 'RELATIVE'
    oi.location = (-1000, -300)
    L.new(cam.outputs[0], oi.inputs['Object'])
    pos = N.new('GeometryNodeInputPosition')
    pos.location = (-1000, -150)
    dist = N.new('ShaderNodeVectorMath')
    dist.operation, dist.location = 'DISTANCE', (-800, -200)
    L.new(pos.outputs[0], dist.inputs[0])
    L.new(oi.outputs['Location'], dist.inputs[1])
    mr = N.new('ShaderNodeMapRange')  # 1 at start, 0 at end (clamped)
    mr.location, mr.clamp = (-600, -200), True
    L.new(dist.outputs['Value'], mr.inputs['Value'])
    L.new(math('MULTIPLY', gin.outputs['Fade Start'], gin.outputs['Fade Scale'], (-800, -400)),
          mr.inputs['From Min'])
    L.new(math('MULTIPLY', gin.outputs['Fade End'], gin.outputs['Fade Scale'], (-800, -560)),
          mr.inputs['From Max'])
    mr.inputs['To Min'].default_value, mr.inputs['To Max'].default_value = 1.0, 0.0
    # off: factor 1; on: the fade
    keep = math('SUBTRACT', 1.0, math('MULTIPLY', gin.outputs['Distance Fade'],
                                      math('SUBTRACT', 1.0, mr.outputs['Result'], (-400, -300)),
                                      (-250, -300)), (-100, -300))
    dele = N.new('GeometryNodeDeleteGeometry')
    dele.domain, dele.location = 'POINT', (-100, 0)
    L.new(gin.outputs[0], dele.inputs['Geometry'])
    L.new(math('LESS_THAN', keep, 1e-4, (-100, -150)), dele.inputs['Selection'])
    ci = N.new('GeometryNodeCollectionInfo')
    ci.location = (-350, 250)
    ci.transform_space = 'ORIGINAL'
    ci.inputs['Separate Children'].default_value = False
    ci.inputs['Reset Children'].default_value = False
    rot = N.new('GeometryNodeInputNamedAttribute')
    rot.data_type = 'QUATERNION'
    rot.inputs['Name'].default_value = 'rotation'
    rot.location = (100, -250)
    sc = N.new('GeometryNodeInputNamedAttribute')
    sc.data_type = 'FLOAT'
    sc.inputs['Name'].default_value = 'scale'
    sc.location = (100, -400)
    iop = N.new('GeometryNodeInstanceOnPoints')
    iop.location = (350, 0)
    L.new(dele.outputs[0], iop.inputs['Points'])
    L.new(gin.outputs['Collection'], ci.inputs['Collection'])
    L.new(ci.outputs[0], iop.inputs['Instance'])
    L.new(rot.outputs['Attribute'], iop.inputs['Rotation'])
    L.new(math('MULTIPLY', sc.outputs['Attribute'], keep, (300, -400)), iop.inputs['Scale'])
    L.new(iop.outputs['Instances'], gout.inputs[0])
    return ng


def _set_modifier_input(mod, identifier, value):
    """Blender 5.x exposes Geometry Nodes modifier inputs as ``properties.inputs.<id>.value``;
    older versions store them as ID properties on the modifier. Setting them from Python does not
    tag the object for re-evaluation, so tag it here."""
    props = getattr(mod, 'properties', None)
    if props is not None and hasattr(props, 'inputs'):
        getattr(props.inputs, identifier).value = value
    else:
        mod[identifier] = value
    mod.id_data.update_tag()


def _tinted(mat):
    """Copy of a foliage material with the per-instance ``tint`` colour multiplied into the base
    colour (mix factor TINT_MIX). The colour looks like baked lighting; its exact use in the
    game's vertex program is unconfirmed, hence the adjustable factor."""
    name = mat.name + '_foliage'
    m = bpy.data.materials.get(name)
    if m is not None:
        return m
    m = mat.copy()
    m.name = name
    nt = m.node_tree
    bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
    if bsdf is None:
        return m
    inp = bsdf.inputs['Base Color']
    attr = nt.nodes.new('ShaderNodeAttribute')
    attr.attribute_type = 'INSTANCER'
    attr.attribute_name = 'tint'
    attr.label = 'Foliage Tint'
    attr.location = (-500, 900)
    mix = nt.nodes.new('ShaderNodeMix')
    mix.data_type = 'RGBA'
    mix.blend_type = 'MULTIPLY'
    mix.label = 'Foliage Tint'
    mix.location = (0, 800)
    mix.inputs['Factor'].default_value = TINT_MIX
    if inp.is_linked:
        nt.links.new(inp.links[0].from_socket, mix.inputs['A'])
    else:
        mix.inputs['A'].default_value = inp.default_value
    nt.links.new(attr.outputs['Color'], mix.inputs['B'])
    nt.links.new(mix.outputs['Result'], inp)
    return m


def build_foliage(lib, lv, name, opts, mcache, map_coll, lib_coll):
    """Place a level's foliage batches: one point mesh per foliage model, instanced with the
    shared ``MAG Foliage`` Geometry Nodes group. Returns the number of instances placed."""
    idx = lib.index
    arch = lv['archive']
    recs = [(zi, ri) for zi, z in enumerate(idx.zrbs) if z.archive == arch
            for ri, r in enumerate(z.records) if r[7] == T_FOLIAGE_BATCH]
    if not recs:
        return 0
    lib.prefetch([zi for zi, _ in recs])
    batches = [fol.read_batch(*lib.record(zi, ri)) for zi, ri in recs]
    clv = lib.archive(arch).read(lv['clv'])
    table = fol.batch_table(clv, batches[0].name)
    placed, lost = fol.level_foliage(batches, table)
    if lost:
        print('MAG: %d foliage batches have no transform entry' % lost)
    fcoll = bpy.data.collections.new('MAG_%s_foliage' % name)
    map_coll.children.link(fcoll)
    protos = bpy.data.collections.new('MAG_%s_foliage_models' % name)
    lib_coll.children.link(protos)
    ng = _foliage_node_group()
    sock = {i.name: i.identifier for i in ng.interface.items_tree
            if getattr(i, 'in_out', '') == 'INPUT' and i.name in FOLIAGE_INPUTS}
    total = 0
    for model, pl in sorted(placed.items(), key=lambda kv: -len(kv[1].scales)):
        if pl.fade_end < opts.foliage_min_distance:
            continue
        hit = idx.find_in_archive(model, (T_FOLIAGE_MODEL,), arch)
        if hit is None:
            print('MAG: foliage model %08x not found' % model)
            continue
        keep = np.ones(len(pl.scales), bool)
        if opts.foliage_density < 1.0:
            keep = np.random.default_rng(model).random(len(keep)) < opts.foliage_density
        n = int(keep.sum())
        if n == 0:
            continue
        label = _safe(idx.label(model, 'foliage_%08x' % model))
        c = bpy.data.collections.new(label)
        protos.children.link(c)
        try:
            build_model_objects(lib, hit[1], hit[2], label, opts, mcache, c)
        except Exception as e:
            print('MAG: foliage model %08x failed: %s' % (model, e))
            continue
        for o in c.objects:
            if o.type == 'MESH':
                for k, m in enumerate(o.data.materials):
                    if m is not None:
                        o.data.materials[k] = _tinted(m)
        q = pl.quats[keep]
        qb = np.empty_like(q)  # game xyzw -> Blender wxyz; the vector part turns like positions
        qb[:, 0], qb[:, 1], qb[:, 2], qb[:, 3] = q[:, 3], q[:, 0], -q[:, 2], q[:, 1]
        qb /= np.linalg.norm(qb, axis=1, keepdims=True)
        me = bpy.data.meshes.new(label + '_points')
        me.vertices.add(n)
        me.vertices.foreach_set('co', to_blender_vec(pl.positions[keep]).astype(np.float32).ravel())
        me.attributes.new('rotation', 'QUATERNION', 'POINT').data.foreach_set('value', qb.ravel())
        me.attributes.new('scale', 'FLOAT', 'POINT').data.foreach_set('value', pl.scales[keep])
        me.attributes.new('tint', 'BYTE_COLOR', 'POINT').data.foreach_set(
            'color_srgb', (pl.colours[keep].astype(np.float32) / 255.0).ravel())
        me.update()
        ob = bpy.data.objects.new(label + '_foliage', me)
        ob['mag_foliage_model'] = '%08x' % model
        ob['mag_fade'] = (pl.fade_start, pl.fade_end)
        mod = ob.modifiers.new('MAG Foliage', 'NODES')
        mod.node_group = ng
        _set_modifier_input(mod, sock['Collection'], c)
        _set_modifier_input(mod, sock['Distance Fade'], bool(opts.foliage_fade))
        _set_modifier_input(mod, sock['Fade Start'], float(pl.fade_start))
        _set_modifier_input(mod, sock['Fade End'], float(max(pl.fade_end, pl.fade_start + 0.01)))
        fcoll.objects.link(ob)
        total += n
    return total


# -- sky ---------------------------------------------------------------------------------------------
SKY_YMAX = 1.1           # profile values are stored /SKY_YMAX in the Float Curve (it clips to 0..1)
SKY_DOME_RADIUS = 2500.0  # metres; the game draws its ~5 m dome around the camera


def _sky_image_mean(img):
    """Mean colour of an image (for the World viewport colour of Workbench / Solid)."""
    try:
        px = np.empty(len(img.pixels), np.float32)
        img.pixels.foreach_get(px)
        return tuple(float(v) for v in px.reshape(-1, 4)[:, :3].mean(0))
    except Exception:
        return (0.05, 0.05, 0.05)


def sky_world(name, img, profile, strength=1.0):
    """World that maps the view direction onto the top-down sky texture: the direction's
    elevation gives the texture radius through the dome's profile (*Sky Profile* Float Curve,
    x = (elevation + 90) / 180, y = radius / 1.1), its azimuth the angle. Blender's
    (x, y) are the game's (x, -z), and the image V is flipped as for every UV set, so the lookup
    is ``uv = 0.5 + 0.5 * s * normalize(x, y)``. *Sky Rotation* (a Mapping node) turns or mirrors
    the sky."""
    w = bpy.data.worlds.new(name)
    w.use_nodes = True
    nt = w.node_tree
    N, L = nt.nodes, nt.links
    N.clear()

    def node(kind, loc, **kw):
        n = N.new(kind)
        n.location = loc
        for k, v in kw.items():
            setattr(n, k, v)
        return n

    def fmath(op, a, b, loc):
        n = node('ShaderNodeMath', loc, operation=op)
        for i, v in enumerate((a, b)):
            if isinstance(v, (int, float)):
                n.inputs[i].default_value = v
            else:
                L.new(v, n.inputs[i])
        return n.outputs[0]

    tc = node('ShaderNodeTexCoord', (-1600, 0))
    mp = node('ShaderNodeMapping', (-1400, 0), vector_type='VECTOR', label='Sky Rotation', name='Sky Rotation')
    L.new(tc.outputs['Generated'], mp.inputs['Vector'])
    sep = node('ShaderNodeSeparateXYZ', (-1200, 0))
    L.new(mp.outputs[0], sep.inputs[0])
    flat = node('ShaderNodeCombineXYZ', (-1000, 100))
    L.new(sep.outputs['X'], flat.inputs['X'])
    L.new(sep.outputs['Y'], flat.inputs['Y'])
    hlen = node('ShaderNodeVectorMath', (-800, -100), operation='LENGTH')
    L.new(flat.outputs[0], hlen.inputs[0])
    el = fmath('ARCTAN2', sep.outputs['Z'], hlen.outputs['Value'], (-600, -100))  # radians
    x = fmath('MULTIPLY_ADD', el, 1.0 / math.pi, (-400, -100))
    x.node.inputs[2].default_value = 0.5
    fc = node('ShaderNodeFloatCurve', (-200, -100), label='Sky Profile', name='Sky Profile')
    L.new(x, fc.inputs['Value'])
    _set_curve(fc.mapping, [((e + 90.0) / 180.0, s / SKY_YMAX) for e, s in profile])
    half_s = fmath('MULTIPLY', fc.outputs[0], 0.5 * SKY_YMAX, (100, -100))
    nrm = node('ShaderNodeVectorMath', (-600, 150), operation='NORMALIZE')
    L.new(flat.outputs[0], nrm.inputs[0])
    uv = node('ShaderNodeVectorMath', (300, 100), operation='MULTIPLY_ADD')
    L.new(nrm.outputs[0], uv.inputs[0])
    L.new(half_s, uv.inputs[1])
    uv.inputs[2].default_value = (0.5, 0.5, 0.0)
    tex = node('ShaderNodeTexImage', (500, 100), image=img, extension='EXTEND', label='Sky')
    L.new(uv.outputs[0], tex.inputs['Vector'])
    bg = node('ShaderNodeBackground', (800, 100))
    bg.inputs['Strength'].default_value = strength
    L.new(tex.outputs['Color'], bg.inputs['Color'])
    out = node('ShaderNodeOutputWorld', (1000, 100))
    L.new(bg.outputs[0], out.inputs['Surface'])
    w.color = _sky_image_mean(img)
    return w


def _set_curve(mapping, pts):
    """Make a CurveMapping's first curve the polyline ``pts`` (x ascending, both in 0..1)."""
    c = mapping.curves[0]
    while len(c.points) > 2:
        c.points.remove(c.points[-1])
    c.points[0].location = pts[0]
    c.points[1].location = pts[-1]
    for p in pts[1:-1]:
        c.points.new(*p)
    for p in c.points:
        p.handle_type = 'VECTOR'
    mapping.extend = 'HORIZONTAL'
    mapping.update()


def _sky_material(name, img):
    """Unlit sky material: the texture straight into an Emission shader."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    tex = nt.nodes.new('ShaderNodeTexImage')
    tex.image, tex.extension, tex.location, tex.label = img, 'EXTEND', (-400, 0), 'Sky'
    em = nt.nodes.new('ShaderNodeEmission')
    em.location = (-100, 0)
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    out.location = (150, 0)
    nt.links.new(tex.outputs['Color'], em.inputs['Color'])
    nt.links.new(em.outputs[0], out.inputs['Surface'])
    nt.nodes.active = tex
    mat.use_backface_culling = False
    return mat


def build_sky(lib, lv, name, opts, mcache, scene, map_coll):
    """The level's sky (``sky.zrb``) as the scene World (``opts.sky == 'WORLD'``) or as the dome
    mesh scaled around the map origin (``'DOME'``). Returns a short description or ''."""
    from ..core import sky
    hit = sky.sky_piece(lib.index, lv['archive'])
    if hit is None:
        return ''
    zi, ri = hit
    model, geoms = lib.model(zi, ri)
    keep = sorted(_select_meshes(model, geoms, 0))
    if not keep:
        return ''
    pos = np.concatenate([geoms[i].positions for i in keep]).astype(np.float64)
    radius = sky.dome_radius(pos)
    got = lib.material(model.meshes[keep[0]].material, zi)
    tex_hash = got[0].bindings.get(3) if got else None
    if tex_hash is None:
        return ''
    img, _ = mcache.image(tex_hash, got[1])
    if img is None:
        return ''
    dome = lib.index.zrbs[zi].records[ri][8]
    if opts.sky == 'WORLD':
        w = sky_world('MAG_%s_sky' % name, img, sky.elevation_profile(pos, radius), opts.sky_strength)
        w['mag_sky'] = '%08x' % dome
        scene.world = w
        return 'world %s' % w.name
    me = make_mesh('MAG_%s_sky' % name, [(geoms[i], 0) for i in keep],
                   [_sky_material('MAG_%s_sky' % name, img)])
    tris = np.empty(len(me.loops), np.int32)
    me.loops.foreach_get('vertex_index', tris)
    uv = sky.dome_uv(pos, radius)
    uv[:, 1] = 1.0 - uv[:, 1]
    me.uv_layers.new(name='UVMap').data.foreach_set('uv', uv[tris].ravel())
    ob = bpy.data.objects.new('MAG_%s_sky' % name, me)
    k = SKY_DOME_RADIUS / radius
    ob.scale = (k, k, k)
    ob.visible_shadow = False
    ob['mag_sky'] = '%08x' % dome
    coll = bpy.data.collections.new('MAG_%s_sky' % name)
    map_coll.children.link(coll)
    coll.objects.link(ob)
    return 'dome %s' % ob.name
