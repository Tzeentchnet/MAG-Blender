# Importer roadmap

This page lists what the importer does not do yet, what is already known about each gap, and how
to close it. The four items at the top are the known gaps in map import, in priority order.
All four are done. The format details they build on are in [formats/](formats/).

| # | Gap | Effect | Status | Remaining size |
| --- | --- | --- | --- | --- |
| 1 | ~~Foliage and clutter are not placed~~ | — | **Done:** optional Geometry Nodes import, verified on Baku and Canal | Follow-ups: ~~distance fade~~ (done), tint meaning |
| 2 | ~~Terrain uses only the macro colour map~~ | — | **Done:** four detail layers blended by the weight map (*MAG Terrain* node group), verified on 6 maps | Follow-ups: ~~distance fade to macro~~ (done), layer alpha masks |
| 3 | ~~Water shows its normal map in Workbench *Texture* mode~~ | — | **Done:** 1×1 viewport-colour image | Follow-up: ~~props misread as water~~ (done: glass, by technique) |
| 4 | ~~Placements that point to pieces that aren't on the disc~~ | — | **Done:** classified in the import report, optional empties | — |

Fixed while working on this list: the importer missed **12 terrain tiles on Canal**. Leaf
selection now reads the level's terrain tree (see [§2](#2-terrain-multi-layer-ground-blending)).
It also **squared every non-unit placement scale** (29,000 placements across the 15 maps). That is
fixed in INDEX_VERSION 13 and checked against the game's compiled transforms (see
[Placement scale](#placement-scale-and-backdrop--done)).

---

## 1. Foliage and clutter placement — done

**Now:** the **Foliage** import option (off by default) places every foliage batch of the map.
The format is in [properties-and-levels.md](formats/properties-and-levels.md#foliage).

### How it works

* `core/foliage.py` decodes a batch (`read_batch`: model hash, offsets, scales, xyzw quaternions,
  RGBA8 colours, using the rebased-half codec) and the level's 81-byte `.clv` origin table
  (`batch_table`, located from one batch hash and walked in both directions). `level_foliage`
  groups the instances by model in world (game) space.
* Decoding happens on import, not in the index, so `INDEX_VERSION` is unchanged. It takes about
  0.1 s for Baku.
* `build.build_foliage` creates one point mesh per foliage model (`<model>_foliage` in
  `MAG_<map>_foliage`) with `rotation` (quaternion), `scale` and `tint` (byte colour) point
  attributes, and a *MAG Foliage* Geometry Nodes modifier: *Collection Info* → *Instance on
  Points*. The prototypes go to the hidden `MAG Assets` collection.
* Each prototype gets copies of its materials (`<material>_foliage`) with a *Foliage Tint* mix
  node: an *Instancer* Attribute node reading `tint`, multiplied into the base colour at 0.5.
* Options: **Density** (a repeatable random share of the points, seeded by the model hash) and
  **Min Draw Distance** (skips models whose fade end is below it).

### Results

| Map | Batches | Models | Instances | Import (whole map) |
| --- | --- | --- | --- | --- |
| Baku | 5,847 | 8 | 172,873 | 6.5 s |
| Canal | 3,914 | 11 | 115,104 | about 6 s |
| Kennicott | 1,016 | 8 | 76,713 | (decode only) |

Every batch has a `.clv` entry, and every instance count matches it. `tools/map_test.py … foliage` compares 200 evaluated instance matrices per model with the decoded transform (error
0). EEVEE ground-level renders show grass and bushes standing on the terrain.

### Follow-ups

* ~~**Distance fade.**~~ **Done** (see below).
* **Tint meaning.** The colour is likely baked lighting (alpha 31 = shaded). Confirm it from the
  foliage vertex program and set the mix factor or blend accordingly.
  The earlier "not the ground colour" test sampled a stretched detail layer; repeat it against the
  tile's real macro colour (slot 12, §2).
* **Wind** (`group-zero-deformation.md`) is not reproduced.
* Consider turning **Foliage** on by default. The fade is in, but it follows the scene camera, so
  it stays off by default (see below).

### Distance fade — done

**Foliage → Distance Fade** (off by default) applies the game's fade, `σ·(1 − clamp((d − φs)·φm, 0,
1))`, with `d` the distance to the **scene camera**.

* The *MAG Foliage* group has new inputs *Distance Fade*, *Fade Start*, *Fade End* and *Fade
  Scale* (multiplies both distances). `build_foliage` sets start and end per model from the `.clv`
  table (as before, also stored as `mag_fade`). Points faded to zero are deleted before *Instance
  on Points*, so a far camera also makes the scene lighter.
* The camera comes from *Active Camera* → *Object Info* (Relative). Moving the camera re-evaluates
  the modifier; the viewport view does not, which is why the option is off by default (with no
  scene camera, every plant away from the origin would vanish).
* Setting a Geometry Nodes modifier input from Python (`mod.properties.inputs.<id>.value`) does
  not tag the object for re-evaluation in Blender 5.2: `_set_modifier_input` now calls
  `update_tag()`.
* A `.blend` holding the older *MAG Foliage* (or *MAG Terrain*) group keeps it, renamed
  `… (old)`; new imports build the new group.
* `tools/map_test.py … foliage fade` puts the camera at the ground shot and compares every
  evaluated instance's scale with the formula: Kennicott draws 9,017 of 76,713 instances, all
  expected ones, scale error 5·10⁻⁷.

---

## 2. Terrain multi-layer ground blending — done

**Now:** with **Terrain Detail: Full Blend** (the default), each leaf tile's material blends four
tiled detail layers (colour and normal) by the tile's weight map, through a shared *MAG Terrain*
node group. **Macro Only** uses the tile's 64×64 pre-blended colour instead. The format is in
[textures-and-materials.md](formats/textures-and-materials.md#terrain-materials).

### What was wrong

The layer textures were in the tile material all along. Slots 3 and 4 are each bound **four
times**, and the scanner kept only the first binding of each slot. That first colour binding
(layer 0, stretched over the tile) was what the importer showed as the "macro colour map". The
real macro colour is slot 12 (64×64), which the game uses for distant LODs.

### How it works

* `materials.parse_material` sets `MaterialInfo.terrain` (`TerrainLayers`) for the 0x418-byte terrain
  material layout: colours and normals sorted by the binding's descriptor offset, weights (slot 8),
  macro (slot 12), the per-layer UV rows `(s, s, s·x0, s·z0)` at +0x322 and specular rows at +0x362.
  For terrain materials, `bindings[3]` is now the macro colour and slot 4 is dropped, so any
  code that reads plain bindings shows the right thing.
* `build.MaterialCache._build_terrain` builds the tile material. `build.terrain_group` builds the
  shared node group (one per Flip Normal Green setting). The materials are tagged
  `mag_terrain = FULL | MACRO`, so re-importing in the other mode builds new ones.
* Decoding happens on import: `INDEX_VERSION` is unchanged.

### Verification

* `tools/terrain_check.py` (offline): for every pair of neighbouring tiles, the weight-map edges
  agree in layer space for RGBA → layers 0–3 and disagree for the other orders and for flipped V/U.
  The detail UVs are world-continuous to within 0.009 of a repeat. It passes on Baku, Kennicott,
  Panama, Canal, Unalaska and Cosmodrome.
* `tools/map_test.py … eevee terrain` renders a point where four tiles meet with everything but the
  terrain hidden. Baku, Panama and Kennicott show the layers with no seam at the tile corner. All
  layered tiles get the blend (256/256 on Baku; the 2–4 other terrain materials are ordinary
  decal-style materials). `macro` renders the *Macro Only* mode.
* Import time is unchanged (Baku about 7–10 s).

### Follow-ups

* ~~**Distance fade to macro.**~~ **Done:** *MAG Terrain* gets a *Macro* input (the tile's
  slot-12 image, already the Workbench preview node) and mixes colour → macro and normal → flat
  by *Camera Data* View Distance, between two value nodes in the group, *Macro Fade Start* and
  *End*. Import option **Terrain → Macro Fade** (default 250 m, end at twice that, 0 = off) sets
  them; they are shared, so the latest import's value applies to the whole file. The game's real
  switch distance is unknown (the leaf LOD distances are 32–64 m, and coarse tiles still use the
  layered material), so 250 m is chosen by eye. From 640 m on Panama (`map_test.py … eevee terrain
  fade`, `terrain_far` shot, against `nomacrofade`), the layers' repeat grid and moiré are gone.
  Finding the switch distance in the terrain shader constants would make it exact.
* **Layer alpha masks.** Some colour layers carry a mask in alpha (Baku's pebbles `c0f868b1`, and
  `d530bad6`). It may drive a height-based blend; it is unused.
* **Specular rows** (per layer `(r, g, b, power)`) are not used; roughness is a fixed 0.9.
* **Slots 0 and 2** (the same tiny purple textures on every tile) and **UV 1** are still unexplained,
  as are the terrain tree's per-region hashes A, B and C.
* ~~**Terrain LOD choice**~~ **Done** (see below).

### Terrain LOD option — done

**Terrain → LOD** (0–9, default 0) imports one level of the terrain tree: 0 is the leaves, and each
step up halves the tile count, down to the root's two children. Values past a map's coarsest
level are clamped.

* `level.terrain_tree_levels` groups the tree's piece hashes by height above the leaves. The
  scanner stores every level that resolves to terrain tiles as `terrain_lods` (finest first;
  `INDEX_VERSION` 12). `terrain` is still the leaves. `build.terrain_tiles(lv, lod)` picks the
  level.
* The tree turned out to be a **triangle bintree**: every tile is a right triangle filling half its
  bounding box. All 15 trees are complete, every level covers the same ground, and coarse tiles use
  the same terrain material layout. Road decal meshes exist only at level 0. See
  [properties-and-levels.md](formats/properties-and-levels.md#terrain-tree-record-284c2f78).
* `tools/map_test.py … lodN` checks the tile count against the tree and renders a terrain-only
  overview. It passes on Kennicott (levels 0, 2 and 5: 64, 16 and 2 tiles) and Baku (levels 0
  and 3: 1,024 and 128 tiles). EEVEE close-ups at level 3 show the full layer blend.

---

## 3. Water in Workbench Texture mode — done

Every material without a colour texture (slot 3) that has some image node gets an unconnected
1×1 generated image of its base colour (`MAG_preview_<rgb>`, shared per colour) as its active
node. Workbench *Texture* mode draws that, and EEVEE and Cycles output is unchanged.
`tools/map_test.py` renders the widest flat water surface top-down in Workbench and checks its
mean colour is dark and not the normal map. This passes on Baku and Canal.

~~**Follow-up:** the "normal map but no colour = water" rule also matches a few small props.~~
**Done:** the material header gives the technique, and the two no-colour techniques are water
(`b18b8189`) and glass (`f436de9a`, the stacked display cases on Canal among them). See
[Material header](#material-header--done).

---

## 4. Placements that point to missing pieces — done

The scanner records the name of every particle effect record (`98057459`) in `GameIndex.fx`
(123 names, `INDEX_VERSION` 11). On import, each placement without a model is classified as a
**particle emitter** (its hash is in `fx`) or a **missing piece**. The report says, for example,
`18 particle emitters and 5 pieces missing from the disc skipped`, and the console lists the
hashes with their counts.

The **Emitters as Empties** option places a sphere empty for each one (`fx_<hash>` or
`missing_<hash>`, custom property `mag_hash`) in `MAG_<map>_fx`, with the placement transform.

Across all 15 maps, every skipped placement is an emitter except the 5 placements of
`4ffe859e` on Kennicott (cut content), which agrees with `tools/missing_pieces.py`.

---

## Sky — done

**Sky** import option, default *World*. The format is in
[textures-and-materials.md](formats/textures-and-materials.md#sky-skyzrb).

* Each level's `sky.zrb` has one dome piece with **no UVs** and a 1024×1024 top-down sky
  texture (a disc; centre = zenith, rim = horizon). The importer maps it with a planar
  projection of the dome positions, `uv = 0.5 + 0.5·(x, z)/R`.
* *World*: `build.sky_world` makes `MAG_<map>_sky` and sets it as the scene World (replacing the
  current one). It turns the view direction into the same UV through the dome's elevation
  profile (*Sky Profile* Float Curve), so it renders at any distance and lights the scene. Its
  viewport colour is the texture's mean colour. A *Sky Rotation* Mapping node turns or mirrors it.
* *Dome*: the mesh with the planar UVs and an emission material, scaled to 2.5 km around the
  map origin, no shadows, in `MAG_<map>_sky`.
* Decoding happens on import (`INDEX_VERSION` unchanged); it takes under 0.15 s.
* `tools/sky_check.py` (offline, all levels) and `tools/sky_test.py` (EEVEE, World against Dome on
  the five domes) pass; `map_test.py … eevee sky` adds a shot of the sky from above the map.

### Follow-ups

* **Orientation.** The mirror and turn of the mapping are a guess. A sun direction (the level's
  light setup) or a game screenshot of a landmark against the sky would settle it.
* **Fog and tint values** in the sky material (`+0x1F1`) are unused. Decoding them could give the
  map's fog colour and distances (a World *Mist* or volume).
* The World ignores the dome's 3 % azimuth asymmetry at the rim; the horizon can differ slightly
  from the Dome option there.

---

## Placement scale and backdrop — done

**The bug.** `InstanceProperties` has `Scale` and `ScaleX/Y/Z`, and the importer multiplied them.
The editor writes the same value into all four, so every non-unit scale was squared. The level
compiler stores the runtime transform after each placement (quaternion, translation, one scale),
and that scale is always `Scale`. `level.read_instances` now uses `Scale` alone (`INDEX_VERSION`
13). The format is in [properties-and-levels.md](formats/properties-and-levels.md#map-placements-levelclv).

Effects of the fix: Kennicott's treeline cards are 110–260 m wide instead of 0.4–1.2 km and no
longer hide the sky; Baku's "kilometre skylines" (scales 964–1807) are 230–300 m mountains at
31–43×; every scaled rock, cliff and building piece has its real size.

**Backdrop rule.** With real scales, the old rule (radius × scale > 1,500 m) caught almost nothing.
A placement now goes to `_backdrop` when its world box centre is more than 50 m outside the terrain
square (the coarsest terrain tiles' model boxes). That is 13 placements on Kennicott and 9 on Baku
(mountain ranges, distant hills, edge treeline cards; 0–13 per map). Models without meshes are
skipped: their header box is meaningless (249 of them on Baku).

**Checks.**

* `tools/placement_check.py` (offline, all maps) compares every importer matrix with the compiled
  transform: about 92,000 placements, worst error 5·10⁻⁷, and it fails on the old squared scale.
  This also confirms the rotation order `Rz · Ry · Rx`.
* `tools/map_test.py … eevee sky` on Kennicott and Baku: backdrop counts match the offline survey,
  and the Kennicott sky shot is clear.

**Follow-ups.** Treeline cards within 50 m of the terrain edge stay in the map collection (5 of 14
on Kennicott). Labelling the card piece hashes would let them move too, if that turns out to be
wanted.

---

## Collision — done

**Collision** import option (off by default). The format is in
[havok.md](formats/havok.md#collision-meshes-2a8bdf5a).

* Every `physics.zrb` record is 1–7 Havok 6.5 packfiles, each one `hkpStorageExtendedMeshShape`
  (behind a MOPP tree) with triangle subparts: `hkVector4` vertices, u16 indices four per
  triangle. `core/collision.py` decodes them (`read_collision`, all 4,780 records in about 1 s).
* The `.clv`'s compiled block after each `InstanceProperties` names the placement's collision
  record at +4. The shape is in the piece's model space. One unnamed record per level is the
  world-space terrain collision (`terrain_record`: it covers the terrain square); the other
  1–31 unnamed records (run-time objects) are skipped.
* `build.build_collision` makes `MAG_<map>_collision` (render-hidden): `col_terrain`, and one
  linked duplicate per placement of a welded mesh per record (`col_<hash>`, custom property
  `mag_collision`; a `mag_part` face attribute when the record has several packfiles). Objects are
  drawn as orange wireframe. Decoding happens on import (`INDEX_VERSION` unchanged); Kennicott
  adds 2,587 objects, Baku 9,918, and the import takes a few seconds longer.
* Checks: `tools/collision_check.py` (offline, every level: decode, unambiguous placement, piece
  collision on its model at a median 0.02–0.07 % of the piece size, terrain collision on the leaf
  tiles at 0.000 m) and `tools/map_test.py … collision` (every placed collision object matches a
  map instance's matrix exactly, the terrain collision's box equals the tiles', and two shots).
  Both pass on Kennicott and Baku in Blender and on all 15 levels offline.

### Follow-ups

* **Surface materials.** The per-packfile words after the size table (and the empty material
  arrays) may carry a physics surface type (metal, dirt, …); records with several packfiles may
  split a piece by surface. Not decoded.
* **Run-time objects.** The 1–31 unnamed records per level could be matched to vehicles or
  destructibles through their property objects.

---

## Baked lighting — done

**Baked Lighting** import option, on by default. The format is in
[properties-and-levels.md](formats/properties-and-levels.md#baked-lighting-84dfb280).

* The `84DFB280` records ("lightmaps" in the earlier notes) are not textures: each is the baked
  per-vertex lighting of **one placement**, named by the u32 at +166 after its
  `InstanceProperties` object. Every record on the 15 maps is named by exactly one placement
  (22,547 records).
* An entry `(0, sky, 0, sun)` per vertex of the model's vertex-lit meshes (material flag
  `0x10000000` at +0x28), in mesh order, all LODs. Renders of the raw channels show `sun` is the
  sun's visibility (trees inside a building's shadow are 0) and `sky` a soft ambient term.
* The records are now indexed (`INDEX_VERSION` 14). `core/lighting.py` decodes them;
  `build._MapLighting` places each lit placement as plain objects with **mesh copies** carrying the
  `mag_light` attribute (R sky, G sun), instead of a collection instance. Meshes of vertex-lit
  materials always get the attribute (1 where nothing is baked), and their materials multiply the
  base colour by the shared *MAG Baked Light* group: `0.75 × sky + 1.0 × sun`, weights chosen so
  the median sunlit vertex is 1.0.
* Baku imports 656 lit placements and takes about the same time (9 s).
* Checks: `tools/lighting_check.py` (offline, every level: unique references, layout match,
  value agreement at mesh seams against random pairs) and `tools/map_test.py … eevee light`
  (every lit object has the attribute, seam agreement in Blender, a close shot; `nolight` for
  comparison). Both pass on all levels offline and on Baku, Unalaska and Kennicott in Blender
  (seams 0.75–1.00 against 0.07–0.15 random). The regression baseline now counts records,
  referencing placements and layout matches per level.

### Follow-ups

* ~~**Unknown layouts.**~~ **Done** (see below).
* **The game's formula.** How `sky` and `sun` combine with the sun colour, the ambient colour and
  the texture is not decoded (the vertex/fragment programs of the vertex-lit technique would tell).
  The weights are a calibration.
* **Bytes 0 and 2** of each entry are almost always 0 (equal to each other when not).
* **Sun direction.** `sun` does not follow N·L for one direction, so it does not give the sun
  direction for the sky orientation follow-up directly; the shadow directions might.

### Other lighting layouts — done

The 6 % of records longer than their model's vertex-lit meshes are now imported, so every lit
placement on the disc gets its lighting (Baku's most common tree `5552a9be` included).

* One record (`94727d6f`, Cosmodrome) only looked longer: its Edge partitions decode to 1,327
  vertices against 1,324 in the mesh header, and N follows the decoded count.
* The other 15 models keep their lit meshes in mesh order with extra entries between them, a
  layout shared by every placement and by model families across maps (table in
  [properties-and-levels.md](formats/properties-and-levels.md#baked-lighting-84dfb280)). The extra
  entries look like bakes of an earlier export; they are not used.
* `lighting.fit_layout` finds each lit mesh's offset from the data: at the right offset each
  vertex's value is close to its 3 nearest vertices' (equal across seams). It picks offsets in
  mesh order without overlap by dynamic programming over the cost relative to each mesh's median,
  and accepts the fit only when every mesh is at most 0.75 of its median. Leaf cards without seams
  locate this way too, which the seam test alone could not do.
* `build._MapLighting` fits once per model and record length on import, from up to 64 of the
  level's records, and prints the fit to the console. `tools/lighting_check.py` fits the same way
  and seam-tests the fitted records: 100 % of records are covered on all 15 levels and every level
  passes. `map_test.py … light` passes on Baku (1,052 lit, 396 fitted), Kennicott (1,022 / 137) and
  Cosmodrome (1,820 / 59), with no record skipped.
* Control: inserting random gaps into the records of models that do match recovers the true
  offsets for 213 of 270 models, rejects 55 and is off by one vertex for 2 (both close to the
  threshold). The two Kennicott models placed once (`bd126b61`, `e9a5bbb1`) rest on a single
  record; `e9a5bbb1`'s offset agrees with the seam test.

---

## Material header — done

The material record is now decoded structurally instead of scanned for texture hashes. The format
is in [textures-and-materials.md](formats/textures-and-materials.md#material-records-7cbe7595).

* The payload is a packed serialization of the runtime object the recomp's `material-*.md`
  describe: a 0x80-byte header, the binding table (count at +0x1D), then `0xDEADBEEF`-tagged
  engine parameters, constant parameters with their values, and pass records. The header offsets
  at +0x44 are runtime offsets, not payload offsets (the terrain's "values at +0x322" was a
  coincidence of the 0x418-byte layout).
* Header: alpha test (+0x06 bit `0x80`) with its reference (+0x09), render group bits (+0x08:
  wind, translucent, level, skinned, vertex-lit), the technique hash (+0x12, 62 on the disc) and
  the vertex attributes the technique reads (+0x28, a superset of every mesh's vertex format).
* Constants are named per technique (`materials.TECHNIQUES`): the UV transform of the base maps
  and the detail-map scale for 31 techniques, the terrain rows, the water colour.
* Builder (`MATERIAL_VERSION` 2): alpha test at the game's reference instead of "texture has
  transparent texels"; Blended translucent materials; the UV transform; detail normal (UDN) and
  detail colour (2× modulate); water and glass by technique.
* The texture-alpha guess had been wrong for every material whose alpha is not opacity: the
  Raven armour (`heavy_armor_black` and the other Raven classes) lost its sleeves and trousers.
  It now renders whole (`char_test.py … eevee`).
* Checks: `tools/material_check.py` (offline, every material: structure, bindings against the old
  scan, header consistency, terrain constants against the fixed offsets, technique parameters;
  `--models` the skinned bit and vertex formats against all 19,859 models) passes;
  `tools/baseline.py` now stores a material summary and digest; `map_test.py … eevee materials`
  checks the node wiring and renders glass, alpha-tested and detail shots (Canal, Baku).

### Follow-ups

* **Unnamed constants.** Most techniques have a `(r, g, b, power)` row that looks like specular
  colour and exponent, plus tints and scalars. Naming them needs the technique's programs (not in
  the material: a separate shader resource, `+0x38` at run time) or more correlation with the
  textures.
* **Glass and water shading.** Both are plain Principled approximations. Water ripples are
  tiled in world space from two guessed tile-size scalars (`0x4D8`, `0x4F0`). The other glass and
  water constants (`0x478` 50–400, perhaps a specular power; `0x508`; the water colour's alpha), the
  slot-255 mask and the environment cube (slot 18, not on the disc) are not used.
* **Detail scale second value.** 14 materials have detail-colour scales such as `(100, 1)`; the
  importer makes them uniform (`DETAIL_MAX_RATIO`), because what `sv` means there is unknown.
* **Pass records** and the remaining header bytes (+0x04, +0x07, +0x0C, +0x16) are not decoded.
* **Detail colour order.** `53126094` has two detail scales for slots 16 and 17; which is which is
  a guess (slot order).

---

## Assembled assets: vehicle LODs, heads, weapon attachments — done

Reported: the Hind had no chin turret, characters had no head, and the M4 had no stock and looked
unfinished around the barrel.

* **Per-node LODs.** The Hind's turret nodes only have LOD 1+ meshes, and the importer chose LODs
  per model, so LOD 0 dropped them. The same happened to the wheels of seven ground vehicles and
  the turrets of the Maultier, NH90 and Pave Hawk. LODs are now chosen per node
  (`build._node_selection`, see [models.md](formats/models.md#mesh-entry-149-bytes)).
* **Heads.** Heads are a separate record type, `B2922512` (15 heads × 3 LODs), now indexed
  (INDEX_VERSION 15). Third-person bodies get a *Heads* collection on their rig, head 0 shown;
  the record is also listed as *character_heads*.
* **Weapons.** `util::Token` values turned out to be CRC32 of their text, which resolves the
  equipment tables (see
  [properties-and-levels.md](formats/properties-and-levels.md#weapon-attachment-and-head-tables)).
  The *WEAPON* list now has the third-person models (with stock) next to the first-person view
  models, which are labelled *(1st person)* and have no stock by design. Importing a weapon mounts
  its required sight and adds every compatible attachment, hidden, on the right socket. The M4's
  "incomplete" look was the missing rear sight.
* **Vehicle weapons** (INDEX_VERSION 16): the turret machine guns are mounted on their
  `turret_pitch` from the vehicle properties records (HMMWV/Cougar M2HB, Panhard/Kangaroo Kord,
  Fennek/Maultier XM312, the deployable turrets' miniguns). Vehicles whose turret already models
  the gun (NH90, Hind) get nothing. The Panhard/Kangaroo pintle was only a LOD 1 mesh behind a
  tiny LOD 0 detail; the per-node rule now adds such a base, except where it is a stand-in for a
  mounted gun (the Fennek's LOD 1 housing) (see
  [models.md](formats/models.md#mesh-entry-149-bytes)).
* Checked with `tools/assembly_test.py` (renders in `E:/tests/mag/assembly`); baseline rewritten
  (one new model record).

### Follow-ups

* Skin tones: the body's `ArmorItemData.replacement_skin_mats` swaps the arm skin to match the
  head's `skin_tone`; the importer keeps the body's default skin.
* `CharacterClassInv` loadouts (default weapon, sight, head and face apparel per class).
* Names of the three `skin_tone` values.

---

## Further work

These are smaller or lower-priority items found while documenting the formats.

### Materials

* ~~Wire the detail normal (16) and detail colour (17) slots.~~ **Done** with the detail scale
  from the material constants (see [Material header](#material-header--done)).
* ~~Decode the material header properly instead of scanning for texture hashes.~~ **Done** (see
  [Material header](#material-header--done)); its follow-ups are listed there.
* Emissive (15) and environment (18) slots.

### Maps

* ~~**Lightmaps**~~ **Done:** they are baked per-vertex lighting; see
  [Baked lighting](#baked-lighting--done) above.
* ~~**Sky**~~ **Done** (see [Sky](#sky--done) above).
* ~~**Treeline cards inside the map.**~~ **Done:** the cards were only huge because of the
  squared scale; see [Placement scale](#placement-scale-and-backdrop--done).
* ~~**Collision**~~ **Done:** see [Collision](#collision--done) above.
* ~~**Terrain LOD choice**~~ **Done:** see §2.
* **Particle effects** (`98057459`): the records hold offset tables and hashes, but no readable
  name. Decoding them is a separate project. Emitter positions are covered by §4.

### Formats

* Finish the ZRB header: the unknown u32 at +0x04, the dependency record framing, and the record
  `kind` field (now known to be constant per record type).
* ~~Fix the mesh-entry offsets in the `core/models.py` docstring.~~ **Done:** it now matches the
  parser and [models.md](formats/models.md#mesh-entry-149-bytes), attachment table included.
* Find out what the unknown vertex format bit `0x004` means, and import tangents (bit `0x008`)
  when present.
* Find the meaning of the terrain tree's node hashes A and B, and of its header u16 at +0x30
  (8 on every map; it is not the tree depth, which is 6, 8 or 10).

### Tooling

* ~~A `tools/dump_record.py`~~ **Done.** It lists or hex-dumps any record straight from the disc
  (every type, not only indexed ones), with its segment data.
* ~~A missing-pieces diagnostic~~ **Done:** `tools/missing_pieces.py`.
* ~~Regression baselines~~ **Done:** `tools/baseline.py` (offline, about 30 s for the whole disc)
  stores `tools/baselines/baseline.json` and lists every difference from it (exit code 1 on
  change). Per level: placements, pieces, emitters, missing pieces, terrain leaf and per-LOD tile
  counts, backdrop count (the importer's rule), foliage batches/table/instances per model, sky
  piece. Per ZRB: model, mesh and failure counts and a digest of every decoded model (geometry
  rounded to 1e-4). `--write` stores the current results; level names limit the run; `--no-models`
  skips the digests. Baseline at INDEX_VERSION 16: 15 levels, 20,351 models in 7,578 ZRBs, no
  decode failures, plus per-level baked lighting counts (records, referencing placements, layout
  matches). Run it before and after any change to decoding or level code, and rewrite it
  when a change is intended.
