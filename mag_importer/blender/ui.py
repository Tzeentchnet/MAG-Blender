import os
import time
import traceback

import bpy
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty,
                       PointerProperty, StringProperty)
from bpy_extras.io_utils import ImportHelper

from . import build, state
from .state import S

CATEGORY_ITEMS = [
    ('CHARACTER', 'Characters', 'Skinned soldier models (with automatically fitted skeleton)', 'ARMATURE_DATA', 0),
    ('WEAPON', 'Weapons', 'Weapon models', 'MOD_PHYSICS', 1),
    ('ATTACHMENT', 'Attachments', 'Scopes, grips, launchers...', 'LINKED', 2),
    ('EQUIPMENT', 'Gear', 'Equipment, gear and armor pieces', 'TOOL_SETTINGS', 3),
    ('VEHICLE', 'Vehicles', 'Vehicles and aircraft', 'AUTO', 4),
    ('PROP', 'Props', 'Other models', 'MESH_CUBE', 5),
    ('MAP', 'Maps', 'Complete levels (placed instances)', 'WORLD', 6),
    ('ANIMATION', 'Animations', 'Havok animations', 'ACTION', 7),
]


# -- preferences ------------------------------------------------------------------------------------
class MAG_AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__.rsplit('.', 1)[0]

    iso_path: StringProperty(name='MAG ISO', subtype='FILE_PATH',
                             description='Decrypted MAG (USA) disc image')
    cache_dir: StringProperty(name='Cache Folder', subtype='DIR_PATH',
                              description='Where the scan index and converted textures are stored. '
                                          'Empty = Blender user config folder (survives restarts and resets)')

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, 'iso_path')
        col.prop(self, 'cache_dir')
        col.label(text='Cache: ' + state.cache_dir())


# -- properties -----------------------------------------------------------------------------------------
class MAG_AssetItem(bpy.types.PropertyGroup):
    label: StringProperty()
    detail: StringProperty()
    key: StringProperty()


def _refresh_list(self=None, context=None):
    wm = bpy.context.window_manager
    p = wm.mag
    p.items.clear()
    if S.index is None:
        return
    cat = p.category
    for a in S.index.assets:
        if a.category != cat:
            continue
        if cat == 'PROP' and not p.show_level_pieces and 'level piece' in a.detail:
            continue
        it = p.items.add()
        it.label = a.label
        it.detail = a.detail
        it.key = a.key
    p.active = min(p.active, max(0, len(p.items) - 1))


def _skeleton_items(self, context):
    items = [('AUTO', 'Automatic', 'Pick the skeleton whose bind pose fits the mesh best')]
    if S.library is not None:
        for nm in S.library.skeleton_names():
            items.append((nm, nm, ''))
    return items


class MAG_WMProps(bpy.types.PropertyGroup):
    category: EnumProperty(items=CATEGORY_ITEMS, name='Category', update=_refresh_list)
    items: CollectionProperty(type=MAG_AssetItem)
    active: IntProperty(name='Active', default=0)
    show_level_pieces: BoolProperty(name='Level Pieces', default=False, update=_refresh_list,
                                    description='Also list every individual level piece (thousands)')


class MAG_SceneProps(bpy.types.PropertyGroup):
    lod: IntProperty(name='LOD', default=0, min=0, max=3, description='Level of detail to import (0 = highest)')
    materials: BoolProperty(name='Materials', default=True, description='Build materials and textures')
    flip_green: BoolProperty(name='Flip Normal Green', default=False,
                             description='Invert the Y channel of normal maps')
    skeleton: EnumProperty(name='Skeleton', items=_skeleton_items,
                           description='Skeleton used for skinned characters')
    set_fps: BoolProperty(name='Set Scene to 30 fps', default=True,
                          description='MAG animations are authored at 30 frames per second')
    include_terrain: BoolProperty(name='Terrain', default=True,
                                  description='Import the map terrain (finest level of its LOD tree)')
    terrain_detail: EnumProperty(
        name='Terrain Detail', default='FULL',
        items=(('FULL', 'Full Blend', 'Blend the four tiled ground layers (colour and normal) by the '
                'tile weight map, as the game draws close terrain'),
               ('MACRO', 'Macro Only', "Only the tile's low-resolution blended colour (lighter; the game "
                'uses it far away)')),
        description='How terrain materials are built')
    terrain_lod: IntProperty(name='Terrain LOD', default=0, min=0, max=9,
                             description='Level of the terrain LOD tree: 0 is the finest tiles, each '
                                         'step halves the tile count with coarser geometry (clamped '
                                         'to the coarsest level of the map)')
    terrain_fade: FloatProperty(name='Macro Fade', default=250.0, min=0.0, max=5000.0,
                                subtype='DISTANCE', unit='LENGTH',
                                description='Camera distance where Full Blend terrain starts fading to '
                                            "the tile's macro colour (done at twice this; 0 = off). "
                                            'Shared by all terrain: edit the values in the MAG Terrain '
                                            'node group to change it later')
    instance_maps: BoolProperty(name='Collection Instances', default=True,
                                description='Place map pieces as collection instances (fast, light). '
                                            'Off: linked mesh duplicates')
    include_foliage: BoolProperty(name='Foliage', default=False,
                                  description='Scatter the map grass, bushes and clutter (Geometry Nodes '
                                              'instances; large maps have over 150,000)')
    foliage_density: FloatProperty(name='Density', default=1.0, min=0.0, max=1.0, subtype='FACTOR',
                                   description='Share of foliage instances to keep (random, repeatable)')
    place_emitters: BoolProperty(name='Emitters as Empties', default=False,
                                 description='Place an empty (fx_<hash>) where the map plays a particle '
                                             'effect, and missing_<hash> for pieces not on the disc')
    foliage_min_distance: FloatProperty(name='Min Draw Distance', default=0.0, min=0.0, max=250.0,
                                        subtype='DISTANCE', unit='LENGTH',
                                        description='Skip foliage models the game stops drawing closer than '
                                                    'this (small grass fades out at 40-60 m)')
    foliage_fade: BoolProperty(name='Distance Fade', default=False,
                               description="Shrink foliage away with distance from the scene camera "
                                           "between each model's fade start and end, as the game does "
                                           '(a modifier input, with a Fade Scale to draw further)')
    sky: EnumProperty(
        name='Sky', default='WORLD',
        items=(('WORLD', 'World', "Make the map's sky the scene World (lights EEVEE and Cycles; "
                'replaces the current World)'),
               ('DOME', 'Dome', 'Import the sky dome mesh, scaled to 2.5 km around the map origin, '
                'with an unlit material'),
               ('NONE', 'None', 'Do not import the sky')),
        description="How the map's sky (sky.zrb) is imported")
    collision: BoolProperty(name='Collision', default=False,
                            description="Import the map's collision meshes (physics.zrb) as wireframe "
                                        'objects hidden from renders: terrain collision and one per '
                                        'placed piece that has one')
    baked_lighting: BoolProperty(name='Baked Lighting', default=True,
                                 description="Apply the map's baked per-vertex lighting (sun shadow and "
                                             'sky terms, mostly on trees): lit placements get their own '
                                             'mesh copies with a mag_light attribute that darkens the '
                                             'base colour (weights in the MAG Baked Light node group)')
    sky_strength: FloatProperty(name='Sky Strength', default=1.0, min=0.0, max=100.0, soft_max=10.0,
                                description='Strength of the sky World background')


# -- operators ------------------------------------------------------------------------------------------
class MAG_OT_select_iso(bpy.types.Operator, ImportHelper):
    """Pick the MAG disc image; scans it (or reloads the cached index) automatically"""
    bl_idname = 'mag.select_iso'
    bl_label = 'Select MAG ISO'
    filename_ext = '.iso'
    filter_glob: StringProperty(default='*.iso', options={'HIDDEN'})

    def execute(self, context):
        path = self.filepath
        if not os.path.isfile(path):
            self.report({'ERROR'}, 'Not a file: %s' % path)
            return {'CANCELLED'}
        prefs = state.addon_prefs()
        if prefs is not None:
            prefs.iso_path = path
        state.save_settings(iso_path=path)
        if state.try_load_cached(path):
            _refresh_list()
            self.report({'INFO'}, S.status)
            return {'FINISHED'}
        return bpy.ops.mag.scan('INVOKE_DEFAULT')


class MAG_OT_scan(bpy.types.Operator):
    """Scan the selected ISO and build the persistent asset index"""
    bl_idname = 'mag.scan'
    bl_label = 'Scan ISO'

    _timer = None

    def invoke(self, context, event):
        iso = state.iso_path()
        if not iso or not os.path.isfile(iso):
            self.report({'ERROR'}, 'Select the MAG ISO first')
            return {'CANCELLED'}
        if not state.start_scan(iso):
            self.report({'WARNING'}, 'A scan is already running')
            return {'CANCELLED'}
        self._iso = iso
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.25, window=context.window)
        wm.modal_handler_add(self)
        wm.progress_begin(0, 100)
        return {'RUNNING_MODAL'}

    def modal(self, context, event):
        if event.type == 'ESC' and S.scan_progress is not None:
            S.scan_progress.cancel = True
        if event.type != 'TIMER':
            return {'PASS_THROUGH'}
        wm = context.window_manager
        if S.scan_progress is not None:
            wm.progress_update(int(S.scan_progress.fraction * 100))
        for area in context.screen.areas if context.screen else ():
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        if state.scanning():
            return {'PASS_THROUGH'}
        wm.event_timer_remove(self._timer)
        wm.progress_end()
        if S.scan_error or S.scan_result is None:
            self.report({'ERROR'}, 'Scan failed: %s' % (S.scan_error or 'unknown error'))
            return {'CANCELLED'}
        state.set_index(S.scan_result, self._iso)
        _refresh_list()
        self.report({'INFO'}, S.status)
        return {'FINISHED'}


class MAG_OT_load_index(bpy.types.Operator):
    """Load the cached index for the selected ISO"""
    bl_idname = 'mag.load_index'
    bl_label = 'Load Cached Index'

    def execute(self, context):
        if state.try_load_cached():
            _refresh_list()
            self.report({'INFO'}, S.status)
            return {'FINISHED'}
        self.report({'WARNING'}, S.status or 'No cached index for this ISO; scan it first')
        return {'CANCELLED'}


class MAG_OT_clear_texture_cache(bpy.types.Operator):
    """Delete converted DDS textures (they are recreated on demand)"""
    bl_idname = 'mag.clear_texture_cache'
    bl_label = 'Clear Texture Cache'

    def execute(self, context):
        d = os.path.join(state.cache_dir(), 'textures')
        n = 0
        if os.path.isdir(d):
            for f in os.listdir(d):
                try:
                    os.remove(os.path.join(d, f))
                    n += 1
                except OSError:
                    pass
        self.report({'INFO'}, 'Removed %d cached texture files' % n)
        return {'FINISHED'}


def _options(context):
    sp = context.scene.mag
    return build.ImportOptions(lod=sp.lod, materials=sp.materials, flip_green=sp.flip_green,
                               skeleton=sp.skeleton, set_fps=sp.set_fps, instance_maps=sp.instance_maps,
                               include_terrain=sp.include_terrain, include_foliage=sp.include_foliage,
                               foliage_density=sp.foliage_density,
                               foliage_min_distance=sp.foliage_min_distance,
                               place_emitters=sp.place_emitters, terrain_detail=sp.terrain_detail,
                               terrain_lod=sp.terrain_lod, terrain_fade=sp.terrain_fade,
                               foliage_fade=sp.foliage_fade, sky=sp.sky, sky_strength=sp.sky_strength,
                               collision=sp.collision, baked_lighting=sp.baked_lighting)


def _target_collection(context, name):
    coll = bpy.data.collections.new(name)
    context.scene.collection.children.link(coll)
    return coll


class MAG_OT_import(bpy.types.Operator):
    """Import the selected asset"""
    bl_idname = 'mag.import_selected'
    bl_label = 'Import'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        p = context.window_manager.mag
        return S.library is not None and 0 <= p.active < len(p.items)

    def execute(self, context):
        p = context.window_manager.mag
        it = p.items[p.active]
        asset = next((a for a in S.index.assets if a.key == it.key), None)
        if asset is None:
            self.report({'ERROR'}, 'Asset not found in index; rescan')
            return {'CANCELLED'}
        opts = _options(context)
        lib = S.library
        mcache = build.MaterialCache(lib, opts)
        wm = context.window_manager
        t0 = time.time()
        if bpy.context.object and bpy.context.object.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        try:
            if asset.category == 'MAP':
                wm.progress_begin(0, 100)
                coll, st = build.build_map(lib, asset.rec, opts, mcache, context.scene,
                                                        lambda f: wm.progress_update(int(f * 100)))
                wm.progress_end()
                msg = 'Map %s: %d instances placed' % (asset.label, st['placed'])
                if st['foliage']:
                    msg += ', %d foliage instances' % st['foliage']
                if st.get('sky'):
                    msg += ', sky as %s' % st['sky']
                if st.get('collision'):
                    msg += ', %d collision objects' % st['collision']
                if st.get('lit'):
                    msg += ', %d with baked lighting' % st['lit']
                skipped = []
                if st['emitters']:
                    skipped.append('%d particle emitters' % st['emitters'])
                if st['missing']:
                    skipped.append('%d pieces missing from the disc' % st['missing'])
                if skipped:
                    msg += ' (%s %s)' % (' and '.join(skipped),
                                         'placed as empties' if opts.place_emitters else 'skipped')
            elif asset.category == 'ANIMATION':
                msg = self._import_animation(context, lib, asset, opts)
            else:
                rt = S.index.zrbs[asset.zrb].records[asset.rec][7]
                coll = _target_collection(context, build._safe(asset.label))
                if rt == build.T_SKINNED_MODEL:
                    root = build.build_skinned(lib, asset.zrb, asset.rec, asset.label, opts, mcache, coll)
                else:
                    root = build.build_model_objects(lib, asset.zrb, asset.rec, asset.label, opts, mcache, coll)
                root.location = context.scene.cursor.location
                for ob in context.selected_objects:
                    ob.select_set(False)
                root.select_set(True)
                context.view_layer.objects.active = root
                msg = 'Imported %s' % asset.label
        except Exception as e:
            traceback.print_exc()
            self.report({'ERROR'}, 'Import failed: %s' % e)
            return {'CANCELLED'}
        self.report({'INFO'}, '%s (%.1fs)' % (msg, time.time() - t0))
        return {'FINISHED'}

    def _import_animation(self, context, lib, asset, opts):
        anims = lib.animations(asset.zrb, asset.rec)
        k = asset.extra or 0
        if k >= len(anims):
            raise RuntimeError('animation not decodable')
        an = anims[k]
        skel = lib.skeleton(an.skeleton_name)
        if skel is None:
            raise RuntimeError('skeleton %r not found on the disc' % an.skeleton_name)
        arm = context.active_object
        if not (arm and arm.type == 'ARMATURE' and arm.get('mag_skeleton') == skel.name):
            arm = next((o for o in context.scene.objects if o.type == 'ARMATURE' and o.get('mag_skeleton') == skel.name), None)
        if arm is None:
            coll = _target_collection(context, skel.name)
            arm = build.build_armature(skel, coll, skel.name)
        if opts.set_fps:
            context.scene.render.fps = 30
            context.scene.render.fps_base = 1.0
        act = build.build_action(an, skel, arm, context.scene)
        context.scene.frame_start = 1
        context.scene.frame_end = int(act.frame_end)
        context.view_layer.objects.active = arm
        return 'Animation %s on %s (%d frames)' % (act.name, arm.name, an.num_frames)


# -- UI --------------------------------------------------------------------------------------------------
class MAG_UL_assets(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.label(text=item.label)
        sub = row.row()
        sub.alignment = 'RIGHT'
        sub.label(text=item.detail.split('  ')[0][:28])

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        flt = [self.bitflag_filter_item] * len(items)
        if self.filter_name:
            needle = self.filter_name.lower()
            flt = [self.bitflag_filter_item if needle in (it.label + ' ' + it.detail).lower() else 0 for it in items]
        order = []
        if self.use_filter_sort_alpha:
            order = bpy.types.UI_UL_list.sort_items_by_name(items, 'label')
        return flt, order


class MAG_PT_main(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'MAG'
    bl_label = 'MAG Importer'

    def draw(self, context):
        layout = self.layout
        iso = state.iso_path()
        box = layout.box()
        row = box.row()
        row.label(text=os.path.basename(iso) if iso else 'No ISO selected', icon='DISC')
        row = box.row(align=True)
        row.operator('mag.select_iso', text='Select ISO', icon='FILEBROWSER')
        row.operator('mag.scan', text='Rescan', icon='FILE_REFRESH')
        if state.scanning() and S.scan_progress is not None:
            box.progress(factor=S.scan_progress.fraction, type='BAR',
                         text='%s %d%%' % (S.scan_progress.stage, S.scan_progress.fraction * 100))
        elif S.index is None:
            if iso:
                box.operator('mag.load_index', icon='IMPORT')
            if S.status:
                box.label(text=S.status, icon='INFO')
            return
        else:
            box.label(text='%d assets indexed' % len(S.index.assets), icon='CHECKMARK')
        if S.index is None:
            return
        p = context.window_manager.mag
        layout.prop(p, 'category', text='')
        if p.category == 'PROP':
            layout.prop(p, 'show_level_pieces')
        layout.template_list('MAG_UL_assets', '', p, 'items', p, 'active', rows=12)
        if 0 <= p.active < len(p.items):
            it = p.items[p.active]
            col = layout.column(align=True)
            col.label(text=it.label, icon='OBJECT_DATA')
            col.label(text=it.detail)
        layout.operator('mag.import_selected', icon='IMPORT')


class MAG_PT_options(bpy.types.Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'MAG'
    bl_label = 'Import Options'
    bl_parent_id = 'MAG_PT_main'
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        sp = context.scene.mag
        col = self.layout.column()
        col.prop(sp, 'lod')
        col.prop(sp, 'materials')
        col.prop(sp, 'flip_green')
        col.prop(sp, 'skeleton')
        col.prop(sp, 'set_fps')
        col.prop(sp, 'include_terrain')
        sub = col.column()
        sub.active = sp.include_terrain and sp.materials
        sub.prop(sp, 'terrain_detail', text='Detail')
        sub = col.column()
        sub.active = sp.include_terrain
        sub.prop(sp, 'terrain_lod', text='LOD')
        sub = col.column()
        sub.active = sp.include_terrain and sp.materials and sp.terrain_detail == 'FULL'
        sub.prop(sp, 'terrain_fade')
        col.prop(sp, 'instance_maps')
        sub = col.column()
        sub.active = sp.materials
        sub.prop(sp, 'baked_lighting')
        col.prop(sp, 'place_emitters')
        col.prop(sp, 'include_foliage')
        sub = col.column()
        sub.active = sp.include_foliage
        sub.prop(sp, 'foliage_density')
        sub.prop(sp, 'foliage_min_distance')
        sub.prop(sp, 'foliage_fade')
        col.prop(sp, 'sky')
        sub = col.column()
        sub.active = sp.sky == 'WORLD'
        sub.prop(sp, 'sky_strength', text='Strength')
        col.prop(sp, 'collision')
        col.separator()
        col.operator('mag.clear_texture_cache', icon='TRASH')


CLASSES = (MAG_AddonPreferences, MAG_AssetItem, MAG_WMProps, MAG_SceneProps, MAG_OT_select_iso, MAG_OT_scan,
           MAG_OT_load_index, MAG_OT_clear_texture_cache, MAG_OT_import, MAG_UL_assets, MAG_PT_main, MAG_PT_options)


def _startup_load():
    try:
        if S.index is None and state.try_load_cached():
            _refresh_list()
    except Exception:
        traceback.print_exc()
    return None


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.WindowManager.mag = PointerProperty(type=MAG_WMProps)
    bpy.types.Scene.mag = PointerProperty(type=MAG_SceneProps)
    bpy.app.timers.register(_startup_load, first_interval=0.5)


def unregister():
    if bpy.app.timers.is_registered(_startup_load):
        bpy.app.timers.unregister(_startup_load)
    del bpy.types.Scene.mag
    del bpy.types.WindowManager.mag
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
    if S.library is not None:
        S.library.close()
    S.index = None
    S.library = None
