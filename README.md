# MAG Importer for Blender

Imports **characters, materials, animations, weapons, props, vehicles and complete maps** from
*MAG* (PS3, Zipper Interactive, 2010) directly out of the decrypted disc image. Nothing has to be
extracted by hand.

Tested with Blender 5.2.2 LTS and `MAG (USA).dec.iso` (retail v1.0, BCUS98110). It needs Blender
4.4 or newer.

## Install

```
python tools/build_extension.py        # -> dist/mag_importer-1.0.0.zip
```

In Blender: *Edit → Preferences → Get Extensions → (▾) Install from Disk…* and pick the zip.
You can also use *Add-ons → Install…* for a legacy install. Either way the panel appears in
**3D Viewport → Sidebar (N) → MAG**.

## Use

1. Click **Select ISO** and pick the decrypted disc image. The first scan indexes the whole
   15 GB disc in about 20 seconds. The scan runs in the background, shows a progress bar, and
   can be cancelled with Esc.
2. Choose a category, pick an item in the list (the list has a search filter), then click **Import**.

| Category | What you get |
| --- | --- |
| Characters | Skinned soldier and armor models, plus an armature fitted automatically (`root_main` body, `root_arms` first-person arms, `chute_root` parachute) |
| Weapons, Attachments, Gear, Vehicles, Props | The node hierarchy as objects, with meshes and materials |
| Maps | Every placed piece as a collection instance, the terrain, the scenery outside the terrain (mountains, treeline cards) in a separate backdrop collection, the sky as the scene World, the baked vertex lighting of trees and bushes, and optionally the collision meshes |
| Animations | A Blender action on the matching armature (the active one if it fits, otherwise a new one) |

### Options (Import Options sub-panel)

* **LOD**: the level of detail to import, from 0 (highest) to 3.
* **Materials**: Principled BSDF materials with base colour, normal maps and specular/gloss.
  Normal maps can be DXT5nm or RGB; the add-on detects which.
* **Flip Normal Green**: inverts the Y channel of the normal maps.
* **Skeleton**: *Automatic* scores each skeleton's bind pose against the mesh, or you can force one.
* **Set Scene to 30 fps**: MAG animations are authored at 30 fps.
* **Terrain**: imports the terrain tiles of one level of the terrain's LOD tree.
  * **LOD**: 0 (default) is the finest level. Each step up halves the tile count with coarser
    geometry, down to 2 tiles (clamped per map). Road decal meshes exist only at level 0; coarser
    tiles have the roads in their textures.
  * **Detail**: *Full Blend* (default) blends the four tiled ground layers, colour and normal, by
    each tile's weight map (shared *MAG Terrain* node group). *Macro Only* uses the tile's
    low-resolution blended colour, which is lighter and is what the game shows far away.
  * **Macro Fade** (default 250 m): with *Full Blend*, the ground fades from the layers to the
    macro colour between this camera distance and twice it, which removes the layers' repeat
    pattern from far away. 0 turns it off. It is shared by all terrain: change it later in the
    *Macro Fade Start* / *End* value nodes inside the *MAG Terrain* node group.
* **Collection Instances**: turn this off to get linked mesh duplicates instead of instances.
* **Baked Lighting** (on by default): applies the lighting the game baked into the vertices of
  some placements (mostly trees and bushes, a few hundred to 3,500 per map). Each lit placement
  becomes plain objects with its own mesh copies (`mag_lighting` custom property) carrying a
  `mag_light` colour attribute: sun visibility in G (0 in shadow), a sky/ambient term in R. Their
  materials multiply the base colour by `0.75 × sky + 1.0 × sun` (the *Sky Weight* / *Sun Weight*
  value nodes in the shared *MAG Baked Light* node group).
* **Sky**: *World* (default) makes the map's sky the scene World (`MAG_<map>_sky`), which also
  lights EEVEE and Cycles renders. It replaces the scene's current World. *Dome* imports the
  game's sky dome mesh instead, scaled to 2.5 km around the map origin with an unlit material
  (in `MAG_<map>_sky`). *None* skips it. **Strength** sets the World's background strength. The
  World's *Sky Rotation* (a Mapping node) turns or mirrors the sky.
* **Collision** (off by default): imports the map's collision meshes into `MAG_<map>_collision`:
  `col_terrain` and a `col_<hash>` object per placed piece that has collision (linked duplicates
  of one mesh per shape). They are drawn as orange wireframe and hidden from renders; the
  collision includes the invisible boundary walls around the playable area.

## The cache survives restarts and resets

The index (`mag_index.pkl`, about 14 MB) and the converted textures are stored in Blender's
per-user config folder, for example
`%APPDATA%\Blender Foundation\Blender\5.2\config\mag_importer\`. The ISO path is saved there too,
in `settings.json`. As a result:

* restarting Blender reloads the index automatically, in about 0.1 s;
* *Load Factory Settings* and reinstalling the add-on keep it;
* if the ISO changes (path, size or modification time), you are asked to rescan.

Textures are converted to DDS once and then reused. DXT data is stored unchanged, so Blender's
GPU decodes it. Preferences can move the cache to another folder.

## Speed

* Indexing reads only the record table (segment 0) of each ZRB: the PSARC reader inflates only
  the 64 KiB blocks it needs, and a thread pool spreads the work (zlib releases the GIL).
* All vertex decoding is vectorized with numpy: half floats, RSX CMP normals, Edge spherical
  normals and skin influences.
* Mesh data reaches Blender through `foreach_set` bulk calls.
* Havok spline curves are evaluated for all frames at once (vectorized B-spline basis).
  All 568 character animations decode in about 2 seconds.
* Maps prefetch and decompress their resource blocks in parallel, build each unique piece once,
  and place it with collection instances. A full map takes 2–9 seconds.
* Decompressed blocks are kept in an LRU cache (768 MB).

## Layout

```
mag_importer/
  core/            pure Python + numpy readers (no bpy; testable outside Blender)
    iso9660.py     ISO9660 + Joliet
    psarc.py       PSARC 1.3 with partial block extraction
    zrb.py         ZRB resource-block container
    models.py      model records, RSX vertex buffers, Edge partitions, skinning palettes
    vertex.py      vectorised attribute codecs
    textures.py    GCM textures -> DDS, DXT/ARGB decoders, un-swizzle
    materials.py   texture bindings
    havok.py       Havok 6.x packfiles, skeletons, spline-compressed animations
    props.py       reflected property objects (.zpr tables, .clv)
    level.py       map instance placements
    catalog.py     scanning + persistent index + asset catalog
    library.py     resource access, LRU cache, texture cache, skeleton fitting
  blender/         Blender side (UI, operators, mesh/material/armature/action builders)
tools/             validation and test scripts (see below)
```

## Format notes (reverse engineered for this add-on)

* **ZRB**: a 0x30-byte header with five segments: records, CPU data, (unused), RSX data and
  Edge geometry. Records are `6, 0x600DF00D, end, kind, seg1..seg4 offsets, type hash,
  name hash, payload`.
* **Types**: texture `85CE0660`, material `7CBE7595`, model `A4F4D23C`, skinned model
  `10B17150`, level piece `80DC0132`, foliage `B9F5747E`, Havok `FB93282D`.
* **Models**: a byte-packed serialization. Each mesh entry is 149 bytes (LOD mask, vertex and
  index counts, vertex format, RSX size, Edge size, material hash), followed by a node table
  (local quaternion and translation, mesh ranges, parents).
* **RSX static meshes**: `[vertex buffer] pad 0x80 [u16 triangle list] pad 0x80`.
  Format bits: `0x1` position half3, `0x2` CMP normal, `0x8` tangent half4, `0x80` colour,
  `0x200` / `0x400` UVs.
* **Edge partitions**: a 0x100-byte header of 24 u16 stream sizes, then config, secondary
  format, indices, positions, normals/tangents, influences and format tables. The skin palette
  is `skinMatricesByteOffsets[2]` at h10/h11 and `skinMatricesSizes[2]` at h12/h13, as 48-byte
  3×4 matrices.
* **Textures**: GCM format 5/6/7/8 = A8R8G8B8/DXT1/DXT3/DXT5. Pitch 0 means a swizzled (Morton)
  ARGB texture.
* **Animations**: Havok 6.5 `hkaSplineCompressedAnimation`. The block data is little-endian even
  on PS3, while the packfile itself is big-endian.
* **Maps**: `InstanceProperties` objects in `<level>.clv` hold the Mesh token plus T/R(deg)/S.
  The rotation order is `Rz·Ry·Rx`. Terrain tiles are world-space level pieces arranged in a
  binary LOD tree of right triangles (the `284C2F78` record in `globaldata.zrb`); the import
  keeps one level of it (the leaves by default).
  Foliage batches (`03BE92A0`) hold 16-byte instance rows (offset, scale, xyzw quaternion as
  rebased halves) and RGBA8 colours; the batch origin is an 81-byte entry in the `.clv`.
  Baked lighting (`84DFB280`) is one record per lit placement, named at +166 after its
  `InstanceProperties` object: 4 bytes `(0, sky, 0, sun)` per vertex of the meshes whose material
  has flag `0x10000000` (u32 at +0x28). For 15 models the meshes sit at other offsets with extra
  entries between them; the importer finds those offsets from the data.
* Asset names come from the game's own property tables (`model_name`, `Model`,
  `ModelFileName`…) and animation names from the Maya source paths stored in the Havok files.

## Limitations

* Foliage (grass, bushes, clutter) is off by default: turn on **Foliage** in the import options.
  It is placed with Geometry Nodes, one point cloud per foliage model. **Distance Fade** (off by
  default) applies the game's fade: plants shrink away between each model's fade start and end
  distance (often 50–60 m) from the **scene camera** (not the viewport view). Each foliage
  object's *MAG Foliage* modifier has the *Distance Fade*, *Fade Start*, *Fade End* and *Fade
  Scale* inputs; raise *Fade Scale* to draw further than the game. The per-instance colour is
  multiplied into the base colour at 50% (the *Foliage Tint* node in each `_foliage` material).
* Particle effects are not imported. Their emitter placements (and the 5 placements of one piece
  cut from the disc) are skipped and counted in the import report. Turn on **Emitters as
  Empties** to get a sphere empty (`fx_<hash>`, `missing_<hash>`) at each one, in `MAG_<map>_fx`.
* The terrain's macro fade distance is a guess (250–500 m): the game's switch distance has not
  been found. The layers' alpha masks and specular values are not used.
* Baked lighting: the weights that turn the sky and sun terms into a brightness are a calibration
  (the median sunlit vertex comes out at 1.0), not the game's formula. About 6 % of lit placements
  (16 models, such as Baku's most common tree) store extra entries between their meshes; the
  importer fits each mesh's offset from the lighting itself (see the console), which is reliable
  for models placed many times and a best guess for the two Kennicott models placed once.
* The sky's orientation is unconfirmed: the dome has no UVs, and the importer maps its top-down
  texture with a planar projection that may be mirrored or turned compared with the game. Fix
  it in the World's *Sky Rotation* node if a sun or landmark disagrees. The sky's fog and tint
  values are not used.
* Materials map the main texture slots to a Principled BSDF. The other slots (detail, masks,
  environment) are added as unconnected image nodes so you can wire them in.
* Materials with a normal map but no colour texture are treated as water and get a dark, glossy
  base. This also catches a few small props, which then render dark. Workbench's *Texture* mode
  draws them from an unconnected 1×1 *Viewport colour* image.

## Developer tools

```
python tools/scan_cli.py "MAG (USA).dec.iso" index.pkl        # scan outside Blender, print the catalog
python tools/validate_models.py store.pkl ...                 # decode every model, report statistics
blender -b --factory-startup -P tools/blender_e2e.py -- ISO OUT [map]     # import one of each category
blender -b --factory-startup -P tools/map_test.py -- ISO OUT baku         # import + render a map (flags: foliage fx eevee terrain macro lodN fade nomacrofade sky skydome nosky collision light nolight)
blender -b --factory-startup -P tools/anim_check.py -- ISO anim frame     # pose vs game-space skeleton
python tools/dump_record.py baku globaldata.zrb --list        # list / hex-dump records of any type
python tools/missing_pieces.py [level ...]                    # explain unresolved map placements
python tools/terrain_check.py baku canal                      # terrain layer order / UV continuity
python tools/sky_check.py                                     # sky dome mapping, every level
python tools/collision_check.py [level ...]                   # collision decode + placement, every level
python tools/lighting_check.py [level ...]                    # baked lighting references, layout, seams
python tools/baseline.py [--write] [level ...]                # regression baselines (counts, model digests)
blender -b --factory-startup -P tools/sky_test.py -- ISO OUT baku kennicott  # sky World vs Dome renders
```

`dump_record.py` and `missing_pieces.py` read the disc through the add-on's cached index.

`anim_check.py` compares Blender's evaluated pose with the skeleton composed in game space. The
current build matches within 0.1 mm.
