# MAG reverse-engineering notes

These notes cover what has been worked out about the data on the *MAG* (PS3, Zipper Interactive,
2010) disc. They describe the formats the `mag_importer` Blender extension reads. Every layout here
was worked out from the retail data (`MAG (USA).dec.iso`, v1.0, BCUS98110) and checked by decoding
the whole disc. The [recomp project's](#references) render-frontend specs were used for the Edge
vertex codecs.

Where a detail is inferred rather than proven, the document says so. The code in
`mag_importer/core/` is the reference implementation. If a document and the code disagree, the
code wins and the document needs fixing.

## Documents

| Document | Contents |
| --- | --- |
| [formats/disc-and-archives.md](formats/disc-and-archives.md) | The ISO9660/Joliet image, disc layout, PSARC 1.3 archives and partial block extraction |
| [formats/zrb.md](formats/zrb.md) | The ZRB resource-block container: header, segments, dependencies, records, type hashes |
| [formats/models.md](formats/models.md) | Model records, mesh entries, node tables, RSX vertex buffers, Edge partitions, vertex codecs, skinning palettes |
| [formats/textures-and-materials.md](formats/textures-and-materials.md) | GCM texture records, swizzling, DDS conversion, material texture bindings and sampler slots |
| [formats/havok.md](formats/havok.md) | Havok 6.5 packfiles, skeletons, animation bindings, spline-compressed animation |
| [formats/properties-and-levels.md](formats/properties-and-levels.md) | Reflected property objects (`.zpr`, `.clv`), map placements, terrain tiles, foliage batches, asset naming |
| [roadmap.md](roadmap.md) | Known gaps in the importer and the planned work on them |

## Summary

### How the data is packaged

```
MAG (USA).dec.iso                    ISO9660 + Joliet
└─ PS3_GAME/USRDIR/RUN/MAG/PS3/
   ├─ MAIN.PSARC                     shared assets: characters, weapons, vehicles, animations
   └─ LEVELS/MAG_<MAP>_FINAL.PSARC   one per map (15 maps)
      └─ *.zrb, <level>.clv, *.zpr   resource blocks, compiled level, property tables
```

* **PSARC 1.3** archives with zlib-compressed 64 KiB blocks. Reading any byte range only needs
  the blocks that cover it.
* **ZRB** ("resource block") files hold every asset. A ZRB has a record table (segment 0) and
  four data segments: CPU data, unused, RSX (GPU) data, and Edge geometry. Each record has a
  32-bit **type hash** and a 32-bit **name hash**. Cross-references between assets always use
  name hashes.
* **Property objects** (magic `DEADBEEF`) are self-describing, reflected C++ objects with named,
  typed fields. Level layouts (`.clv`) and game tables (`.zpr`) use them. They hold the only
  readable asset names in the game.

### What each asset type is

| Asset | Record type | Main data | Notes |
| --- | --- | --- | --- |
| Texture | `85CE0660` | Segment 3 | GCM ARGB8 / DXT1 / DXT3 / DXT5, linear or Morton-swizzled |
| Material | `7CBE7595` | Payload | A table of (texture hash, pass mask, sampler slot) bindings |
| Static model | `A4F4D23C` | Segment 3 | Hierarchical: node table, RSX vertex and index buffers |
| Skinned model | `10B17150` | Segments 3 and 4 | Edge partitions with a per-partition skinning palette |
| Level piece | `80DC0132` | Segments 3 and 4 | Edge static partitions; terrain tiles are level pieces too |
| Foliage model | `B9F5747E` | Segments 3 and 4 | Scattered clutter mesh |
| Foliage batch | `03BE92A0` | Segments 3 and 4 | Instances of one foliage model; the origin is in the level's `.clv` (imported as Geometry Nodes instances) |
| Terrain tree | `284C2F78` | Payload | The level's terrain LOD tree: one node per tile (decoded) |
| Particle effect | `98057459` | Payload | Emitter definitions; map placements of emitters point at these |
| Havok packfile | `FB93282D` | Segment 1 | Skeletons and animations (Havok 6.5, big-endian) |
| Collision mesh | `2A8BDF5A` | Segment 1 | Havok 6.5 triangle meshes in `physics.zrb`, named by the placements' compiled blocks; one is the terrain's |

### Disc census (retail v1.0)

| Item | Count |
| --- | --- |
| PSARC archives | 16 (MAIN plus 15 levels) |
| ZRB files | 33,668 |
| Texture records | 69,474 |
| Foliage batch records | 31,257 (1.2 million instances) |
| Level piece records | 19,705 |
| Material records | 8,538 |
| Havok packfiles | 811 (809 animations) |
| Static models | 502 |
| Foliage model records | 113 (24 distinct names) |
| Skinned models | 30 |
| Readable labels mined from property tables | 353 |

### Conventions used throughout

* **Byte order.** The game data is big-endian. The one exception is the Havok spline block data,
  which is little-endian (see [havok.md](formats/havok.md)).
* **Hashes.** Type and name hashes are opaque 32-bit values. Dependency paths in ZRB headers use
  CRC32 path hashes. Name hashes are *not* a known function of the asset name, so readable names
  come only from property tables and Havok source paths.
* **Coordinates.** The game is Y-up, right-handed, in metres. The importer maps
  `(x, y, z) → (x, −z, y)`, which is a proper rotation. Winding and handedness are preserved, and
  no mirroring is needed.
* **Alignment.** Within a segment, buffers are aligned relative to the *segment* start, not to
  the file start. Model buffers use 0x80 alignment and Edge secondary streams use 0x10.

### Import status

| Area | Status |
| --- | --- |
| Static models (weapons, vehicles, props) | Done: hierarchy, LODs, materials |
| Skinned characters and gear | Done: Edge decode, skin weights, automatic skeleton fitting |
| Animations | Done: spline and interleaved, matching game space within 0.1 mm |
| Textures | Done: DXT passed through unchanged as DDS, ARGB un-swizzled |
| Materials | Partial: base, normal (DXT5nm or RGB) and specular are wired; other slots are left unconnected |
| Maps: placed pieces | Done. Unresolved placements are reported as particle emitters (all maps) or missing pieces (one cut piece on Kennicott), optionally placed as empties |
| Maps: terrain | Done: any level of the terrain tree (finest by default, *Terrain LOD* option) with the four detail layers blended by the weight map, fading to the macro colour with camera distance |
| Maps: foliage | Done (optional): Geometry Nodes instances with rotation, scale and tint; optional camera-distance fade as in the game |
| Water | Done: dark glossy base in EEVEE and Cycles, flat colour in Workbench *Texture* mode |
| Maps: sky | Done: the scene World (default) or the dome mesh; orientation unconfirmed |
| Maps: collision | Done (optional): terrain and every placed piece's collision mesh, as render-hidden wireframe objects |
| Maps: baked lighting | Done for 94 % of lit placements: per-vertex sun and sky terms as a `mag_light` attribute that darkens the base colour; the game's exact lighting formula is unknown |

See [roadmap.md](roadmap.md) for the plan on each gap.

## Verification tools

| Script | What it checks |
| --- | --- |
| `tools/scan_cli.py` | Scans the ISO outside Blender and prints the catalog |
| `tools/validate_models.py` | Decodes every model and reports statistics and failures |
| `tools/blender_e2e.py` | Imports one item of every category inside Blender |
| `tools/map_test.py` | Imports a map and renders overview shots; `foliage` also checks every foliage model's evaluated instance transforms and adds ground-level shots, `eevee` renders with EEVEE, `terrain` renders a four-tile corner with the terrain alone and counts layered materials, `macro` imports *Macro Only* terrain, `lodN` imports terrain LOD N, checks its tile count against the tree and adds a terrain-only overview, `fade` turns on the foliage Distance Fade and checks every evaluated instance scale against the game's formula (and adds a far terrain shot with `terrain`), `nomacrofade` turns the terrain macro fade off, `sky` adds a shot of the sky from above the map (`skydome` imports the sky as the Dome, `nosky` not at all), `collision` imports the collision, checks each placed collision object against its map instance and the terrain collision against the tiles, and renders it, `light` checks the baked lighting attribute on every lit object and adds a close shot of lit trees (`nolight` imports without it) |
| `tools/anim_check.py` | Compares Blender's evaluated pose with the skeleton composed in game space (passes at under 0.1 mm) |
| `tools/char_test.py`, `tools/render_previews.py` | Character and asset preview renders |
| `tools/dump_record.py` | Lists or hex-dumps any ZRB record (every type) with its segment data, straight from the disc |
| `tools/foliage_check.py` | Decodes every foliage batch of a level and checks it against the `.clv` transform table |
| `tools/terrain_check.py` | Terrain detail layers: weight-map continuity across tile borders for the decoded channel order (against the other orders and flipped UVs), and world-continuous detail UVs |
| `tools/sky_check.py` | Every level's sky dome: the planar UVs fill the texture disc, and the World's direction mapping lands within 12 texels of them |
| `tools/sky_test.py` | Builds a map's sky as World and as Dome and renders the same four views of each with EEVEE; they must agree |
| `tools/collision_check.py` | Every level's collision: all records decode, placements name them unambiguously, piece collision lies on its model and terrain collision on the leaf tiles |
| `tools/lighting_check.py` | Every level's baked lighting: each record named by one placement, record length against the model's vertex-lit meshes, and value agreement at mesh seams |
| `tools/baseline.py` | Regression baselines: per-level counts and per-model decode digests against `tools/baselines/baseline.json` (`--write` to store) |
| `tools/missing_pieces.py` | Explains placements whose `Mesh` hash resolves to no model (searches every record table on the disc) |

`baseline.py`, `collision_check.py`, `dump_record.py`, `foliage_check.py`, `lighting_check.py`, `missing_pieces.py`, `placement_check.py`, `sky_check.py` and `terrain_check.py` run outside Blender on the add-on's cached index
(`tools/_offline.py`).

Use Workbench renders for geometry checks and EEVEE for materials. Workbench shows only an image
node, not the shaded result.

## References

* The recomp project's render-frontend specs,
  `MAG-Windows-Recomp/mag-recomp/docs/render-frontend/behavior/`. `geometry-streams.md` and
  `geometry-normals.md` define the Edge descriptor codecs. `material-*.md` and
  `texture-unit-controls.md` are the best starting point for the unmapped material slots.
* Havok 6.5 SDK class layouts (4-byte pointers), for `hkaSkeleton`, `hkaAnimationBinding` and
  `hkaSplineCompressedAnimation`.
* Sony Edge Geometry documentation, for the partition and stream concepts.
