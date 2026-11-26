# Property objects, maps, terrain and foliage

Code: `mag_importer/core/props.py`, `mag_importer/core/level.py`, terrain selection in
`mag_importer/core/catalog.py`, and `build_map` in `mag_importer/blender/build.py`.

## Reflected property objects

Zipper serialises game objects in a self-describing format. It appears in `.zpr` property
tables, in the compiled level (`.clv`), and inside ZRB record payloads. Everything is big-endian.

```
u32   0xDEADBEEF
u32   version (1–15)
u32   type-name length (including the NUL)
char  type name, NUL terminated      e.g. "InstanceProperties"
u8    more
while more:
    u32  field-name length,  char field name (NUL terminated)
    u32  type-name length,   char type name  (NUL terminated)
    u32  size                (includes these 4 bytes)
    u8   value[size − 4]
    u8   more
```

Value types seen:

| Type | Encoding |
| --- | --- |
| `float` | f32 |
| `util::Token` | u32 hash, the same hash space as ZRB name hashes |
| `ztl::string` | u32 length + NUL-terminated text |
| `ztl::vector`, `object`, … | Nested blobs, not parsed (kept as raw bytes) |

Objects are found with a regex on the magic, the version and a plausible type-name length.
Parsing gives up on a field name or type name longer than 256 bytes, or a size that runs past the
buffer.

### Asset names

Name hashes cannot be reversed, so readable names are mined from property objects
(`props.token_labels`):

1. Find the object's label: the first non-empty string among `id`, `Name`, `name`, `Class`,
   `m_name`, `ItemName`, `VehicleName`, or else the first string field.
2. Every `util::Token` field whose name contains `model`, `mesh`, `geometry` or `hulk` maps its
   hash to that label.
3. Fields that name *alternative* models (`alternate`, `zoom`, `detonator`, `remote`, `hulk`,
   `invalid`, `highlight`, `godray`, `attacker`, `defender`) get the field name added as a
   suffix, e.g. `M4_ZoomModel`.

This yields 353 labels. Assets without one are shown as `<zrb stem>_<hash>`.

## Map placements (`<level>.clv`)

Each level PSARC has one `.clv`: a stream of property objects. Static geometry is placed by
**`InstanceProperties`** objects:

| Field | Type | Use |
| --- | --- | --- |
| `Mesh` | Token | Name hash of a model record (usually a level piece) |
| `Name` | Token | Instance name hash |
| `ObjectID` | — | Not used |
| `TranslateX/Y/Z` | float | Position (metres, Y-up) |
| `RotateX/Y/Z` | float | Rotation in **degrees** |
| `Scale` | float | Uniform scale (default 1). This is the scale the game uses |
| `ScaleX/Y/Z` | float | A copy of `Scale` on every placement. **Do not multiply** them by `Scale` |

**World matrix** (column vectors, game space):

```
M = T · Rz · Ry · Rx · S
```

with `S = Scale` on all three axes. Blender gets `C · M · Cᵀ`.

**Compiled transform.** After each placement of a model, the level compiler stores the runtime
transform: an xyzw quaternion (4 × f32), the translation (3 × f32) and **one** scale (f32), found
by searching for the three translation floats after the object. On all 15 maps (about 90,000
placements, 29,000 of them scaled) that scale equals `Scale`, and `ScaleX/Y/Z` always equal
`Scale` (three placements differ in the 6th digit). Placements without a model (particle emitters)
store a 4×4 matrix instead. `tools/placement_check.py` compares every importer matrix with the
compiled one (worst error 5·10⁻⁷). The u32 at +4 of the same compiled block names the piece's
collision mesh, if it has one (see [havok.md](havok.md#collision-meshes-2a8bdf5a)), and the u32
at +166 names its baked lighting record, if it has one (see [Baked lighting](#baked-lighting-84dfb280)).
Both offsets count from the end of the `InstanceProperties` object.

Before INDEX_VERSION 13 the importer multiplied `ScaleX/Y/Z` by `Scale`, which squared every
non-unit scale: a 0.75 rock became 0.56, and Kennicott's 3.5–8.3× treeline cards became
12.6–69×, kilometre-wide walls that hid the sky.

### Resolving a placement

The mesh hash is looked up among the model types: first in the level's own archive, then in
MAIN, then anywhere. Each unique piece is built once, in a hidden `MAG Assets` collection, and
placed with collection instances.

**Backdrop.** A placement goes to the separate `_backdrop` collection when the centre of its
world bounding box lies more than 50 m (`level.BACKDROP_MARGIN`) outside the terrain square. The
square is the union of the coarsest terrain tiles' model boxes (0–512 m or 0–1024 m); the piece's
box is the model header's (+0x18). Models without meshes are never moved. This catches 0–13
placements per map: mountain ranges (`ffc00233`/`da6c3e90`/`3cea7fab`, 2.3 km wide), distant hills
and rocks, off-map building chunks, and treeline cards (a 31 × 14 m alpha card, `8fd68536`,
`4c388684`, `7153c295`, `c3d4c14a`, placed along the map edge at 3–8×). Cards within 50 m of the
edge stay in the map collection.

### Level statistics

| Level | Placements | Unique pieces | Terrain leaves | Missing pieces (unique / placements) |
| --- | --- | --- | --- | --- |
| alaska_subpen | 8,074 | 528 | 256 | 3 / 15 |
| baku | 11,308 | 956 | 1,024 | 3 / 13 |
| canal | 10,703 | 770 | 256 | 1 / 1 |
| cosmodrome | 4,370 | 337 | 64 | 2 / 12 |
| kennicott | 3,335 | 473 | 64 | 5 / 23 |
| panama_military_base | 9,417 | 750 | 1,024 | 5 / 22 |
| raven_sup | 3,037 | 384 | 64 | 4 / 33 |
| remote_comms | 5,609 | 245 | 64 | 2 / 31 |
| sver_sup | 2,074 | 302 | 64 | 4 / 16 |
| training_raven | 2,964 | 392 | 64 | 3 / 22 |
| training_sver | 5,002 | 655 | 1,024 | 5 / 34 |
| training_valor | 4,597 | 412 | 256 | 4 / 26 |
| unalaska | 10,893 | 749 | 256 | 1 / 7 |
| valour_sup | 2,195 | 252 | 256 | 6 / 45 |
| voz | 8,099 | 685 | 1,024 | 2 / 18 |

"Missing" means the `Mesh` hash matches no indexed model record anywhere on the disc. Those
placements are skipped today.

### What the missing placements are

`tools/missing_pieces.py` searched the record table of every ZRB on the disc (all record types,
not only the indexed ones). There are **17 distinct missing hashes**:

* **16 are particle effects.** They are records of type `98057459` (only their names are
  indexed, in `GameIndex.fx`, 123 on the disc), 14 in
  `MAIN.PSARC:global_resources/particle_effects.zrb` and 2 (`5f7619cc`, `fe266bab`) in
  `global_common.zrb`. `InstanceProperties` is used for emitters as well as meshes. Nine of these
  hashes are placed in more than one level. For example, `f070979b` is placed in nine levels.
* **1 is nowhere on the disc:** `4ffe859e`, 5 placements in Kennicott only. Its hash occurs only
  in Kennicott's `.clv` (the object itself and the compiled transform after it), so it is cut
  content.

Emitter placements look like any other placement: `SnapToTerrain_M`, `IsVisible_M`,
`IsFrozen_M`, `Name`, `Mesh`, `ObjectID = ffffffff` and a TRS, usually unrotated and unscaled.
The compiled bytes after each object do not encode the target type either. A `Mesh` token can
only be classified by looking up which record type it names.

A full map imports in 2–9 seconds.

## Terrain

* Terrain lives in `terrain_<x>_<z>_radius_<r>.zrb` files as ordinary **level piece** records
  (`80DC0132`), already in **world space**. They need no placement.
* The tiles form a **binary LOD tree of right isosceles triangles** (a triangle bintree): each
  tile is split along its hypotenuse into two children. A tile's bounds are therefore a 1:1 or
  1:2 box that the tile fills only half of: the projected triangle area of every level is
  0.50 ± 0.02 of its boxes' area, and every level covers the same ground (checked on Baku,
  Canal and Kennicott; the leaves overlap their neighbours by about 2%).
  The scanner records each tile's footprint from its model bounds (payload +0x18: centre xyz,
  half-extents xyz; the X/Z parts are used).
* **Level selection** reads the level's terrain tree (`level.terrain_tree_levels`; see
  [below](#terrain-tree-record-284c2f78)). Each map has 64, 256 or 1,024 leaves (6, 8 or 10
  levels below the root), and the index stores every level (`terrain_lods`, finest first); the
  *Terrain LOD* option picks one. Coarse tiles use the same 0x418-byte terrain material layout
  as the leaves (sampled on Baku, Canal and Kennicott at every level). The leaves also carry the
  road and decal meshes with ordinary materials; coarser tiles have none.
* The fallback for a level without a tree is `terrain_leaves`. It keeps every tile whose
  footprint contains no tile with less than 75% of its area, testing containment on the centre
  with a 0.1% tolerance. It agrees with the tree on 14 maps. On Canal it misses 12 leaves whose
  Z extent is trimmed along the canal (half-depth 50–55 m instead of 64) and finds only 244.
* Each tile has its own material: four tiled detail layers (colour and normal) blended by a
  per-tile RGBA weight map, plus a 64×64 macro colour for distant LODs; see
  [textures-and-materials.md](textures-and-materials.md#terrain-materials).
* `globaldata.zrb` holds one `284C2F78` record per level. It is the **terrain LOD tree** itself
  (below), not a layer palette.

### Terrain tree record (`284C2F78`)

The record is a flat array of binary-tree nodes. In Baku it is 329,640 bytes: a 0x3F-byte header,
2,047 nodes of 161 bytes, and 10 trailing bytes. All values are big-endian, and most fields are
unaligned because of the leading flag byte of each node.

Header:

```
+0x00  24 bytes zero
+0x18  f32[3]  terrain centre          (512, 45.8, 512 in Baku)
+0x24  f32[3]  terrain half extents    (512, 25.6, 512)
+0x30  u16     8                       (meaning unknown; 8 on every map, whatever the depth)
+0x3D  u16     node count              (0x7FF = 2047)
+0x3F  nodes
```

Node (161 bytes):

| Offset | Type | Field |
| --- | --- | --- |
| +0x00 | u8 | 1 |
| +0x01 | u32, u32 | `DEADBEEF`, 6 (it looks like a property object, but it is not one) |
| +0x09 | u32 ×3 | hashes A, B, C. **C is a texture** (DXT5, 256×256, stored in the terrain ZRBs). A and B are not the name of any record on the disc. All three are shared by the nodes of one quadtree region: each has 341 distinct values (1 + 4 + 16 + 64 + 256) |
| +0x15 | u16 ×2 | `01FF 01FF` (`000F 000F` in the root) |
| +0x19 | 68 bytes | constant: three times (16 zero bytes, `000F 000F`), then 8 zero bytes |
| +0x5D | f32 ×3 | centre |
| +0x69 | f32 | distance. It is larger than the bounding-sphere radius (174 for a 128 m node), so it is probably the LOD switch distance |
| +0x6D | f32 | 0 in leaves and positive in inner nodes (3.8, 12.0): probably the geometric error of the LOD |
| +0x71 | u32 ×2 | `0000FFFF`, 0 |
| +0x79 | u16 ×2 | indices of the two children (`FFFF FFFF` in a leaf) |
| +0x7D | f32 ×4 | box centre, w = `FFFFFF7F` |
| +0x8D | f32 ×4 | box half extents, w = `FFFFFF7F` |
| +0x9D | u32 | name hash of the tile's level piece (`80DC0132`) |

Node 0 is a root with no hashes, children 1 and 1024, and bounds of ±1e38. The other 2,046 nodes
name every terrain tile in the level once. Their 1,024 leaves (children `FFFF`) are **exactly the
leaf set `terrain_leaves` selects from footprints** (except on Canal, see above).

All 15 trees on the disc are complete and balanced (127, 511 or 2,047 nodes), and every node's
piece is on the disc. The tiles are triangles, so a node's box (+0x7D/+0x8D) alternates between
square and 1:2. In Baku the leaves are triangles with a 64 m hypotenuse (32×64 m boxes, 1,024 m² each). The node distance
(+0x69) and error (+0x6D) grow steadily with the height above the leaves:

| Height | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Baku nodes | 1,024 | 512 | 256 | 128 | 64 | 32 | 16 | 8 | 4 | 2 |
| Baku distance (m) | 32 | 64 | 109 | 173 | 264 | 392 | 573 | 829 | 1,192 | 1,705 |
| Baku error (max) | 0 | 0.1 | 0.4 | 0.5 | 3.4 | 5.6 | 8.4 | 12 | 12 | 12 |

On maps with 64 or 256 leaves the distances run 64 m (leaf), 128 m, 219 m and so on. The
exceptions are Valour (256 leaves, the same distances as Baku) and Canal, whose leaf distances
(55–58 m) follow its trimmed tiles.
`level.terrain_tree_levels` returns the piece hashes grouped by height; the index keeps every
level that fully resolves to terrain tiles.

## Baked lighting (`84DFB280`)

The level compiler bakes lighting into the vertices of some placements: mostly trees and bushes
(16 models on Baku), plus a few props. Each lit placement has its **own record**, so two
placements of the same tree carry different lighting. These are the "lightmaps" of earlier notes;
there are no lightmap textures.

* **Where.** Records sit in the level's `globaldata.zrb` and `loadblock_*.zrb` files (Baku: 35 in
  globaldata, 1,017 in 86 loadblocks). The u32 at +166 after a placement's `InstanceProperties`
  object names its record. On all 15 maps every record is named by exactly one placement
  (22,547 records).
* **Payload** (24 bytes): u32 0, u32 **N**, then 16 zero bytes.
* **Segment 3**: N entries of 4 bytes, `(0, sky, 0, sun)`. On Baku, bytes 0 and 2 are zero in
  99.96 % of entries (when not, they are equal to each other); their meaning is unknown.
* **Which vertices.** One entry per vertex of the model's **vertex-lit meshes**, in mesh order, all
  LODs included. A mesh is vertex-lit when its material has flag `0x10000000` in the u32 at +0x28
  (see [textures-and-materials.md](textures-and-materials.md#material-records-7cbe7595)). Many of
  them are leaf cards whose vertex format has no normal.
* **Channels** (from renders and statistics):
  * `sun` is the sun's visibility. It is 0 in shadow, including shadows cast by other pieces
    (whole trees sit in a building's shadow), and it does not follow N·L for any single
    direction. Sunlit values are mostly 0.27–0.76 (median 0.38); 59 % of entries are 0.
  * `sky` is a soft ambient or sky term: median 0.81 where the sun reaches, 0.36 in shadow.
* **Count.** N counts the **decoded** vertices: for `94727d6f` (two Edge partitions) the mesh
  header says 1,324 vertices, the partitions hold 1,327, and N is 1,327.
* **Other layouts.** For 15 models N is larger than the vertex-lit vertex count: 1,322 records
  (6 %). The lit meshes are still stored in mesh order, but with extra entries before, between or
  after them. The layout is the same for a model on every map and for every model of one family:

  | Models | Lit meshes (V) | N | Mesh offsets | Extra entries |
  | --- | --- | --- | --- | --- |
  | `5552a9be` (Baku), `70445cc6`, `ba2ddee4`, `b261cb23`, `b1881408` | 140, 340, 52 | 544 | 0, 152, 492 | 12 after the first mesh |
  | `8134366c`, `86c25738` (Unalaska), `36b09204` | 96, 54, 27 | 354 | 0, 273, 327 | 177 after the first mesh: a second copy of all three meshes (the copy of mesh 2 at 192), vertices reordered |
  | `ad331ef6`, `93cb46b7`, `21100978`, `99d35d7b`, `4233accd` | 51 | 405 | 0 | the same 51 values again at 67, 220, 271 and 322, other data between |
  | `bd126b61` (Kennicott, 1 placement) | 8 | 264 | 42 | |
  | `e9a5bbb1` (Kennicott, 1 placement) | 77 | 5,998 | 4,913 | |

  These models exist once on the disc, so the extra entries are not from another version of the
  model on the disc; they look like bakes of an earlier export (duplicated or extra vertices).
  The offsets were found from the data (`lighting.fit_layout`): lighting is smooth over a mesh,
  so at the right offset each vertex's value is close to that of its nearest vertices and equal
  across seams. For each lit mesh and start offset the cost is the mean absolute difference to the
  3 nearest vertices over up to 64 records of the model; the offsets are chosen in mesh order
  without overlap to minimise the summed cost relative to each mesh's median cost, and a fit is
  accepted when every mesh is at most 0.75 of its median (the fits above are 0.4–0.73). Fitted
  records pass the seam test at the same rate as the others. As a control, inserting random gaps
  into the records of models that do match gives the true offsets for 213 of 270 models, rejects
  55 and is off by one vertex for 2.

`tools/lighting_check.py` checks every level: unique references, the layout match (94 % of
records overall; 100 % on Alaska, Raven and Valor maps) or fit (the rest; every record on the
disc is covered), and that vertices of a lit mesh at the
same position carry the same value far more often than random vertex pairs do (0.58–1.00 against
0.02–0.15). Agreement is lower on boxy maps because hard edges are lit per normal.

## Foliage

Foliage is stored as **foliage models** (`B9F5747E`, 24 distinct names, kept in each level's
`globaldata.zrb`) and **foliage batches** (`03BE92A0`, 31,257 records, in the `loadblock_*`
ZRBs). The importer places the batches when the **Foliage** option is on (`core/foliage.py`,
`build.build_foliage`), and can also import foliage models on their own as props.

A foliage instance is fully described by three pieces of data: the batch record, its per-batch
entry in the level's `.clv`, and the instance rows and colours in the batch's segments. All three
are decoded below. The layout was checked against every one of the 31,257 batches on the disc,
and placed positions were checked against the terrain surface. The recomp's
`group-zero-deformation.md` (foliage is material group 0) describes the same data as the
renderer consumes it, and agrees.

### Batch record (`03BE92A0`)

The payload is 24 bytes:

```
+0x00 u32  0
+0x04 u32  instance count            (1–217; matches the .clv entry on every batch)
+0x08 u32  foliage model name hash   (resolves to a B9F5747E record in globaldata.zrb)
+0x0C      12 bytes zero
```

The record's `kind` is 4 for all 31,257 batches. Every batch is in a `loadblock_*` ZRB. Training Raven has none.

### Batch transform table (in the `.clv`)

The batch record has no position. The origin of each batch is in a contiguous table near the end
of the level's `.clv` (Baku: 5,847 entries from offset `0xCCE982`). Entries are **81 bytes**,
big-endian and unaligned:

```
+0x00 u32     batch record name hash
+0x04 f32[16] 4x4 matrix, row-major, translation in the 4th column
              (the rotation part is the identity in every batch; the last row is 0 0 0 1)
+0x44 f32     1.0 (object scale)
+0x48 u8      instance count
+0x49 f16     fade end distance            e.g. 60
+0x4B f16     fade multiplier φm = 1/(end − start)   e.g. 0.1
+0x4D f16     fade start φs                e.g. 50
+0x4F f16     bounding-sphere radius of the batch (a little more than the instance spread)
```

Find an entry by searching the `.clv` for the batch's name hash. A hash occurs exactly once in all
but eight batches. For those eight, take the occurrence inside the table.

The fade values match the renderer's per-instance fade, `φ = clamp((d − φs)·φm, 0, 1)` with the
effective scale `σ·(1 − φ)`, which shrinks plants to nothing between the start and end distances.
Common settings are (60, 0.1, 50), (50, 0.1, 40), (60, 0.2, 55), (40, 1/7, 33), and up to
(210, 0.1, 200) for large clutter. The importer reproduces this fade in its *MAG Foliage* Geometry
Nodes group (option *Distance Fade*), from the scene camera.

### Instance rows (segment 4)

**16 bytes per instance**, padded to 0x80 per batch: eight 16-bit **rebased halves** (the Edge
codec 3 half; see [models.md](models.md#codecs). It differs from IEEE binary16 only at exponents 0
and 31, which never occur in this data):

```
h0 h1 h2  instance offset o (x, y, z) from the batch origin, game space, metres
h3        instance scale σ
h4..h7    quaternion (x, y, z, w), unit length
```

Offsets are centred on the batch: min = −max on every axis in every batch.

**World position = batch translation + o** (no rotation or scale between them). On Baku, Canal
and Kennicott, 100% of the sampled instances that fall on a terrain tile sit on the terrain
surface to within 1 mm. That is 79,000 instances, and the few that do not fall on a tile lie
outside the terrain mesh. A vertex of the model is placed at `T + o + σ·R(q)·P`.

The quaternion is in **xyzw** order. It is mostly a yaw, and it also tilts the plant toward the
slope: the rotated up axis is a median 2.6° from the terrain normal, against 5.8° for an upright
plant. Reading it as wxyz gives nonsense (173°).

### Instance colours (segment 3)

**4 bytes per instance** (RGBA8), padded to 0x80. The renderer feeds them to vertex attribute
slot 12 (the vertex colour). They are **not the ground colour**: the correlation with the terrain
colour under each instance is 0.06. (That test sampled the first slot-3 binding, which turned out to
be a stretched detail layer, not the tile's macro colour in slot 12; repeating it against slot 12
is a cheap follow-up.) They look like **baked lighting**:

| Level | Alpha > 31 (share) | Mean RGB, alpha > 31 | Mean RGB, alpha = 31 |
| --- | --- | --- | --- |
| Baku (desert) | 52% | 255, 155, 84 (sunlit orange) | 78, 61, 54 |
| Kennicott (snow) | 43% | 255, 237, 216 | 104, 101, 121 (blue shade) |
| Canal | 29% | 242, 207, 254 | 98, 92, 126 |

Alpha is 31 (`0x1F`) or more, and it correlates with brightness (r ≈ 0.65). It is most likely a
sun-visibility or intensity term, with RGB the lit colour. The exact use is in the foliage vertex
program and has not been confirmed. Multiply RGB into the base colour as a first approximation.
