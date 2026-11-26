# Havok packfiles: skeletons, animations and collision

Code: `mag_importer/core/havok.py`, and `build_action` in `mag_importer/blender/build.py`.
Collision: `mag_importer/core/collision.py` and `build_collision` (see
[Collision meshes](#collision-meshes-2a8bdf5a)).

Every rig and animation is a Havok **6.5** binary packfile (`hkRootLevelContainer`), stored in
segment 1 of a ZRB record of type `FB93282D`. The packfile size is the u32 at record payload
+4. There are 811 packfiles on the disc. They hold 809 animations and every skeleton.

## Packfile header

The PS3 packfiles use **4-byte pointers and big-endian** layout.

| Offset | Field |
| --- | --- |
| 0x00 | magic `57E0E057 10C0C010` |
| 0x10 | u8 pointer size (4) |
| 0x11 | u8 little-endian flag (0 = big-endian) |
| 0x14 | i32 section count |
| 0x28 | contents version string (16 bytes, NUL padded) |
| 0x40 | section headers, 0x30 bytes each |

Section header: a 20-byte NUL-padded tag, then at +20 seven u32 values: **start**, then local
fixups, global fixups, virtual fixups, exports, imports and end. All but start are relative to
the section start.

### Fixups

| Table | Entry | Meaning |
| --- | --- | --- |
| Local | `i32 src, i32 dst` (8 bytes) | A pointer at `start+src` points to `start+dst` |
| Global | `i32 src, i32 dst_section, i32 dst` (12 bytes) | A cross-section pointer |
| Virtual | `i32 src, i32 class_section, i32 class_name_off` (12 bytes) | The object at `start+src` has the class named at that string |

`src = −1` (0xFFFFFFFF) marks padding entries. The reader turns these tables into a pointer map
(`location → target`) and an object map (`location → class name`). It never patches the buffer,
so struct reads go through `ptrs.get(addr)`.

## hkaSkeleton

| Offset | Field |
| --- | --- |
| +0 | `char* name` |
| +4 / +8 | `i16* parentIndices`, count |
| +12 / +16 | `hkaBone* bones` or `hkaBone** bones`, count |
| +20 / +24 | `hkQsTransform* referencePose`, count |

Bones come in two forms. If the slot `bones + 4i` has a pointer fixup, it is an array of
pointers. Otherwise the bones are inline 8-byte `{char* name, bool lockTranslation}` records.

The reference pose is 48 bytes per bone: translation `f32[4]`, rotation quaternion `f32[4]`
(x, y, z, w), and scale `f32[4]`. Translations, rotations and scales are local to the parent.

Known skeletons include `root_main` (soldier body), `root_arms` (first-person arms) and
`chute_root` (parachute).

## hkaAnimationBinding

| Offset | Field |
| --- | --- |
| +0 | `char* originalSkeletonName` |
| +4 | `hkaAnimation* animation` |
| +8 / +12 | `i16* transformTrackToBoneIndices`, count |
| +0x10 / +0x14 | `i16* floatTrackToFloatSlotIndices`, count |
| +0x18 | `i8 blendHint`: 0 normal, 1 **additive** |

If there is no track map, track *i* drives bone *i*.

Additive clips (26 on the disc, all `root_main` and all named `a_*`: aim sweeps, recoils,
flinches) hold deltas, not poses: translations are about zero and rotations near identity. The
game layers them on the playing clip with Havok's additive blend (local rotation
`base * delta`, translation `base + delta`, scale `base * delta`). Read as full poses, every
bone collapses onto its parent. The first-person `a_fps_*recoil*` clips are normal (hint 0) full
poses. The importer bakes an additive clip relative to the bind pose. On a rig that is already
playing a normal clip, that clip moves to the `MAG base` NLA track and the additive clip plays
on top with Combine blending.

The packfile's `hkxScene` object (`+4`, a `char*`) holds the **Maya source path** of the
animation. The importer uses the file stem as the animation's name.

## hkaSplineCompressedAnimation

| Offset | Field |
| --- | --- |
| +0x0C | f32 duration (s) |
| +0x10 | i32 transform track count |
| +0x14 | i32 float track count |
| +0x24 | i32 frame count |
| +0x28 | i32 block count |
| +0x2C | i32 max frames per block |
| +0x3C | f32 frame duration (1/30 s) |
| +0x40 | `hkArray<u32>` block offsets (pointer at +0x40) |
| +0x70 | `hkArray<u8>` data (pointer at +0x70) |
| +0x7C | i32 endianness of the data: **0 = little-endian** |

On this disc the **block data is little-endian** even though the packfile around it is
big-endian. Reading it with the packfile's byte order produces garbage.

### Block layout

Block *b* starts at `data + blockOffsets[b]` and covers frames `b·(maxFramesPerBlock − 1)` to
`+maxFramesPerBlock`. Blocks share their boundary frame.

```
transform track masks: 4 bytes per track
    [0] quantization: bits 0–1 position, bits 2–5 rotation, bits 6–7 scale
    [1] position flags   [2] rotation flags   [3] scale flags
float track masks: 1 byte per float track
align 4
then, for each transform track: position, rotation, scale
```

Flag bits for position and scale: bits 0–2 mean *static* per axis (x, y, z), and bits 4–6 mean
*spline* per axis. Rotations use any of bits 4–7 for spline and any of bits 0–3 for static.

**Vector track (position or scale)**

```
if any spline axis:
    u16 n (control points − 1), u8 degree p
    u8 knots[n + p + 2]
    align 4
    per axis x, y, z: spline → f32 min, f32 max; static → f32 value
    control points: (n+1) × (number of spline axes), u8 (/255) or u16 (/65535) by quantization 0/1
    value = min + (max − min) · q
    align 4
else:
    per axis: static → f32 value (others keep the default: 0 for position, 1 for scale)
    align 4
```

**Rotation track**

```
spline:  u16 n, u8 p, u8 knots[n+p+2], align(type), (n+1) quantized quaternions, align 4
static:  align(type), one quantized quaternion, align 4
neither: identity
```

| Type | Name | Size | Align | Decode |
| --- | --- | --- | --- | --- |
| 0 | POLAR32 | 4 | 4 | 10-bit radius term, 18-bit φ/θ, 4 sign bits |
| 1 | THREECOMP40 | 5 | 1 | Three 12-bit components (×0.000345436), 2-bit drop index, sign of w |
| 2 | THREECOMP48 | 6 | 2 | Three 15-bit components (×0.000043161); drop index from the top bits of x and y; sign of w from z |
| 3 | THREECOMP24 | 3 | 1 | Three 7-bit components (×0.0055248), 2-bit drop index, sign of w |
| 4 | STRAIGHT16 | 8 | 2 | `(v − 32767) / 32767` per component |
| 5 | UNCOMPRESSED | 16 | 4 | f32 × 4 |

For the three-component types, the dropped component is rebuilt as `√(1 − x² − y² − z²)`. It goes
into the lane named by the drop index, and the stored components fill the other lanes in order.
THREECOMP40 and THREECOMP24 are packed little-endian bit fields.

**Evaluation.** Each spline is a clamped B-spline over the u8 knot vector, sampled at the local
frame numbers `0 … framesInBlock − 1`. The importer evaluates all frames at once with Cox–de Boor
and normalises quaternions afterwards.

## hkaInterleavedUncompressedAnimation

| Offset | Field |
| --- | --- |
| +0x0C | f32 duration |
| +0x10 | i32 transform track count |
| +0x24 / +0x28 | `hkQsTransform*` transforms, count (= frames × tracks) |

Transforms are stored frame-major. The frame duration is `duration / (frames − 1)`.

## Conversion to Blender

* The rest pose comes from `skeleton_world()` (parent-chained local TRS). It is converted with
  `C · M · Cᵀ`, where `C` maps `(x, y, z) → (x, −z, y)`. Bone matrices are orthonormalised.
* Each animation's local TRS becomes a pose-bone basis: `q_basis = q_rest⁻¹ · q_anim`,
  `t_basis = q_rest⁻¹ · (t_anim − t_rest)`, `s_basis = s_anim / s_rest`. The vector parts of all
  three then go through the same axis map.
* Quaternion signs are made continuous frame to frame. Keys are linear, one per frame, at 30 fps.
* `tools/anim_check.py` compares Blender's evaluated pose with the game-space composition. The
  current build matches within 0.1 mm.

All 568 character animations decode in about 2 seconds.

## Collision meshes (`2A8BDF5A`)

Each level's `resource_blocks/physics.zrb` holds only records of type `2A8BDF5A` (181–495 per
level, 4,780 on the disc). Every one is a static triangle mesh made of one to seven Havok 6.5
packfiles.

**Record payload** (big-endian u32 unless noted):

```
+0x00  u32 0, u32 0
+0x08  u32 n            packfile count (1-7; 1 in 81 % of records)
+0x0C  f32 0.5
+0x10  u32 size[n]      byte size of each packfile
then   per-packfile values not needed for import (n = 1: 1, 0, 9, 0, 0)
```

Segment 1 at the record's offset holds the `n` packfiles back to back, in that order.

**Packfile contents.** Always the same four classes: `hkpMoppBvTreeShape` → `hkpMoppCode`
(MOPP bounding-volume code, ignored) and `hkpStorageExtendedMeshShape`, whose triangle subparts
point at `hkpStorageExtendedMeshShapeMeshSubpartStorage` objects. With 4-byte pointers:

| Object | Offset | Field |
| --- | --- | --- |
| `hkpStorageExtendedMeshShape` | +0x80 | f32[3] scale (1, 1, 1) |
| | +0x90 / +0xA0 | AABB half extents / centre (f32[3] each) |
| | +0xB4 | trianglesSubparts: pointer, u32 count |
| | +0xE0 | meshstorage: pointer, u32 count (same count; storage *i* backs subpart *i*) |
| TrianglesSubpart (0x60 bytes) | +0x14 | u32 triangle count |
| | +0x1C | u32 vertex striding (16) |
| | +0x20 | u32 vertex count |
| | +0x44 | u32 index striding (8) |
| | +0x48 | u8 index type: 1 = u8, 2 = u16, 3 = u32 (always 2 on the disc) |
| MeshSubpartStorage | +0x08 | hkArray of hkVector4 vertices (pointer, count, capacity) |
| | +0x14 / +0x20 / +0x2C | hkArrays of u8 / u16 / u32 indices |
| | +0x38 … | material arrays (empty on the disc) |

Indices are four per triangle; the fourth is padding (0). The subparts have no transform of their
own. Vertices are repeated per triangle strip; the importer welds exact duplicates.

**Which shape goes where.** The record names are not model hashes. The level compiler names them
in the compiled block that follows each `InstanceProperties` in the `.clv`: the u32 at block +4 is
the collision record name (the u32 at +0 varies). On every map, each placement names at most one
record and a mesh always names the same one (75–92 % of placements have collision; the rest are
small props, decals and emitters). These shapes are in the piece's **model space**, so the
placement transform places them: collision vertices lie on the model's vertices (median distance
under 0.1 % of the piece's size; simplified hulls such as rocks and tree trunks differ more).

One record no placement names covers the whole terrain square (512 or 1,024 m): the **terrain
collision**, in world space, 43,000–254,000 triangles. Its vertices lie exactly on the leaf
terrain tiles (height difference 0.000 m on all 15 maps). The remaining 1–31 unnamed records per
level are small objects placed at run time (vehicles, destructibles and the like); the importer
skips them.

`tools/collision_check.py` checks all of this on every level.
