# Add-on internals

How the `mag_importer` add-on stores its cache, why it is fast, how its code is laid out, a short
summary of the formats it reads, and the scripts used to test it. The detailed format notes are
in [README.md](README.md).

## The cache survives restarts, upgrades and resets

The index (`mag_index.pkl`, about 14 MB) and the converted textures are stored in Blender's
per-user folder, next to the per-version folders rather than inside one, for example
`%APPDATA%\Blender Foundation\Blender\mag_importer\`. The ISO path is saved there too, in
`settings.json`. As a result:

* restarting Blender reloads the index automatically, in about 0.1 s, and opening a .blend
  refills the asset list;
* upgrading Blender (5.2 to 5.3...) keeps the cache: every version uses the same folder;
* the import options kept in the preferences (Lower LODs, Lightmaps, Collision, Pack Textures)
  are mirrored into `settings.json`, so they come back even if Blender's preferences are not
  saved;
* *Load Factory Settings* and reinstalling the add-on keep it;
* if the ISO changes (path, size or modification time), you are asked to rescan.

Textures are converted to DDS once and then reused. DXT data is stored unchanged, so Blender's
GPU decodes it. Preferences can move the cache to another folder.

Older versions of the add-on kept the cache in `...\Blender\<version>\config\mag_importer\`. The
first start moves the newest such cache to the shared folder. Opening a .blend whose textures
point at a cache location that no longer exists relinks them to the current cache.

## Speed

* Indexing reads only the record table (segment 0) of each ZRB: the PSARC reader inflates only
  the 64 KiB blocks it needs, and a thread pool spreads the work (zlib releases the GIL).
* All vertex decoding is vectorized with numpy: half floats, RSX CMP normals, Edge spherical
  normals and skin influences.
* Mesh data reaches Blender through `foreach_set` bulk calls.
* Havok spline curves are evaluated for all frames at once (vectorized B-spline basis).
  All 568 character animations decode in about 2 seconds.
* Maps prefetch and decompress their resource blocks in parallel, build each unique piece once,
  and place it with collection instances. A full map takes 2–8 seconds (Baku, the largest,
  about 7 s).
* The first import of a map converts its missing textures in parallel, and only the DXT blocks
  sampled for the alpha/normal check are decoded. With an empty texture cache, Baku takes about
  9 s instead of 17 s.
* Materials share node groups (*MAG Terrain*, *MAG Normal*) instead of rebuilding the same maths
  in every material.
* `tools/profile_import.py` profiles a character, animations, a vehicle and a map (cProfile, per
  stage, warm or cold texture cache, optional first EEVEE frame).
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
    materials.py   material header, texture bindings, shader constants
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
  `10B17150`, level piece `80DC0132`, foliage `B9F5747E`, heads `B2922512`, Havok `FB93282D`.
* **Models**: a byte-packed serialization. Each mesh entry is 149 bytes (LOD mask, vertex and
  index counts, vertex format, RSX size, Edge size, material hash), followed by a node table
  (local quaternion and translation, mesh ranges, parents). LODs are picked per node. Node names
  and property `util::Token` values are CRC32 of their text.
* **RSX static meshes**: `[vertex buffer] pad 0x80 [u16 triangle list] pad 0x80`.
  Format bits: `0x1` position half3, `0x2` CMP normal, `0x8` tangent half4, `0x80` colour,
  `0x200` / `0x400` UVs.
* **Edge partitions**: a 0x100-byte header of 24 u16 stream sizes, then config, secondary
  format, indices, positions, normals/tangents, influences and format tables. The skin palette
  is `skinMatricesByteOffsets[2]` at h10/h11 and `skinMatricesSizes[2]` at h12/h13, as 48-byte
  3×4 matrices.
* **Materials**: a 0x80-byte header (alpha test + reference, render group bits, technique hash,
  binding count, vertex attributes), 14-byte texture bindings, then `0xDEADBEEF`-tagged engine
  parameters, constant parameters with their float values, and pass records. Constants have no
  names; their meaning is per technique (62 on the disc).
* **Textures**: GCM format 5/6/7/8 = A8R8G8B8/DXT1/DXT3/DXT5. Pitch 0 means a swizzled (Morton)
  ARGB texture.
* **Animations**: Havok 6.5 `hkaSplineCompressedAnimation`. The block data is little-endian even
  on PS3, while the packfile itself is big-endian. The binding's blend hint (+0x18) marks 26
  additive clips, which are deltas layered on a base clip with Blender's Combine blend mode.
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

## Developer tools

```
python tools/scan_cli.py "MAG (USA).dec.iso" index.pkl        # scan outside Blender, print the catalog
python tools/validate_models.py store.pkl ...                 # decode every model, report statistics
blender -b --factory-startup -P tools/blender_e2e.py -- ISO OUT [map]     # import one of each category
blender -b --factory-startup -P tools/map_test.py -- ISO OUT baku         # import + render a map (flags: foliage fx eevee terrain macro lodN fade nomacrofade sky skydome nosky collision light nolight)
blender -b --factory-startup -P tools/anim_check.py -- ISO anim frame     # pose vs game-space skeleton
blender -b --factory-startup -P tools/assembly_test.py -- ISO OUT [CAT:label ...] [eevee] [showall] [frame=name] [pieces]  # vehicle LODs, weapon attachments, heads; pieces = level pieces
blender --factory-startup -P tools/viewport_test.py -- ISO OUT CAT:label [...] [frame=text] [view=x,y,z]  # windowed: captures the Solid / Material Preview / Rendered viewport
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
