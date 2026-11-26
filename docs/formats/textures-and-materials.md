# Textures and materials

Code: `mag_importer/core/textures.py`, `mag_importer/core/materials.py`, and the material builder
in `mag_importer/blender/build.py`.

## Texture records (`85CE0660`)

The payload is 0x28 bytes, big-endian. Its fields mirror a GCM `CellGcmTexture`.

| Offset | Type | Field |
| --- | --- | --- |
| +0x04 | u8 | dimension (1 = 2D) |
| +0x05 | u8 | mip count |
| +0x06 | u32 | pixel data size (all mips) |
| +0x0A | u32 | width |
| +0x0E | u32 | height |
| +0x12 | u8 | GCM format; the low 5 bits select the format (high bits are the linear/normalisation flags) |
| +0x14 | u16 | GCM remap (channel swizzle) |
| +0x16 | u32 | pitch; **0 means swizzled** |

The pixel data is in **segment 3** at the record's segment 3 offset. Mip levels follow one after
another, with no padding between them.

### Formats

| GCM low bits | Format | Handling |
| --- | --- | --- |
| 5 | A8R8G8B8 | Converted to BGRA8 DDS (level 0 only) |
| 6 | DXT1 | Copied unchanged into a DXT1 DDS |
| 7 | DXT3 | Copied unchanged into a DXT3 DDS |
| 8 | DXT5 | Copied unchanged into a DXT5 DDS |

No other formats have been seen in imported assets.

DXT block data is byte-for-byte the same as PC DXT, so it is written to DDS unchanged and
Blender decodes it on the GPU.

**Pitched DXT.** When pitch ≠ 0 and the texture has a single mip, rows are `pitch` bytes apart.
If `pitch` differs from the packed row size, the rows are repacked.

### ARGB8 layout

* **Pitch ≠ 0:** linear rows of `pitch` bytes.
* **Pitch = 0 and both sides a power of two:** Morton (Z-order) swizzle. Bits from x and y
  alternate, x first, for the low `min(log2 w, log2 h)` bits. The remaining high bits of the
  longer axis follow.
* **Pitch = 0, not a power of two:** treated as linear.

Bytes in memory are `A R G B`. A remap low byte of `0xE4` is the identity. `0x1B` means the
bytes are stored reversed (`B G R A`).

### Cache

The importer writes each converted texture to `<cache>/textures/<namehash>.dds`, with a
`.dds.json` sidecar beside it:

```json
{"alpha": false, "dxt5nm": true, "fmt": "DXT5", "size": [512, 512]}
```

* `alpha`: more than 1% of sampled texels have alpha < 0.5, so the texture is a cut-out.
* `dxt5nm`: mean red < 0.05 and mean blue < 0.05, so this is a DXT5nm normal map (X in alpha,
  Y in green). Otherwise a normal-slot texture is treated as an ordinary RGB normal map.

Both checks sample a 64×64 grid of level 0, decoded by the numpy DXT decoder in
`textures.decode_dxt`.

## Material records (`7CBE7595`)

Code: `materials.parse_material`. Check: `tools/material_check.py`.

The payload is a byte-packed serialization of the runtime material object. Pointers are written
as `0xDEADBEEF`, and each variable-size section is a run of records that start with that marker
and a tag. Every one of the 3,878 materials on the disc has this layout:

```
+0x00   0x80-byte header
+0x80   texture bindings, 14 bytes each (count = u8 +0x1D)
        engine parameters, 20 bytes each (tag 2)
        constant parameters, 23 bytes each (tag 4), then their values
        pass records, 28 bytes each (tag 7); the last one is followed by a trailer
```

The record is not the runtime layout. Its header offsets (`u16 +0x44 … +0x4C`) point into the
expanded object the loader builds (a 0x60-byte header, 32-byte binding records, 16-byte
parameter records), which matches the recomp's `material-sources.md` and `material-values.md`.
None of them can be used to index the payload.

### Header

| Offset | Type | Field |
| --- | --- | --- |
| +0x00 | u32 | 0 |
| +0x04 | u8 | 1, 2 or 3 (unknown; not the pass record count) |
| +0x06 | u8 | **render bits**: `0x80` = alpha test. `0x40`, `0x04` and `0x02` also occur (unknown) |
| +0x07 | u8 | unknown (`0x02` on most; `0x82` on terrain) |
| +0x08 | u8 | **render group bits** (below) |
| +0x09 | u8 | **alpha-test reference** (0–255). Nonzero exactly when render bit `0x80` is set (317 materials; 32 is typical, up to 200) |
| +0x0C | u16 | unknown (0x00D0–0x0810) |
| +0x12 | u32 | **technique hash**: the shader. 62 distinct on the disc |
| +0x16, +0x18 | u16 | unknown, always equal |
| +0x1D | u8 | **binding count** |
| +0x1E | 6 bytes | a copy of +0x04…+0x09 |
| +0x24 | u32 | 3 |
| +0x28 | u32 | **vertex attributes** the technique reads (`MaterialInfo.flags`), plus `0x10000000` = baked vertex lighting |
| +0x44 | u16 × 5 | runtime offsets (parameter records, values, technique map, technique table, descriptors) |
| +0x4E | u16 | 3 |
| +0x50 | 48 bytes | `0xFF` (14 materials have other bytes here) |

**Render group bits** (+0x08). Checked on every material against the models that use it
(`material_check.py --models`):

| Bit | Meaning | Evidence |
| --- | --- | --- |
| `0x01` | Wind / foliage | All foliage-model materials, plus 88 materials of plants placed as level pieces |
| `0x04` | Translucent | Scope lenses, vehicle windows, glass, blended level decals (85 materials) |
| `0x10` | Static level geometry | Only on materials of level pieces (2,711 of 3,107) |
| `0x20` | Skinned | Set exactly on the 56 materials of skinned meshes |
| `0x40` | Baked vertex lighting | Set exactly when flag `0x10000000` is |
| none | Dynamic models | Weapons, vehicles, props |

They probably select the renderer's draw groups (in the recomp's `main-group-transitions.md`,
group 0 has its own deformation and groups 8 and 9 are blended).

**Vertex attributes** (+0x28, low 16 bits). Every mesh's vertex format ([models.md](models.md))
is a subset of its material's attributes, and 93 % of meshes match exactly. Level pieces drop
bit `0x001` (`0x20b` → `0x20a`); vertex-lit level pieces drop `0x082` or `0x08b` too.
Foliage-model materials have 0.

### Texture bindings

```
+0   u32  texture name hash
+4   u32  pass mask (1, 2, 4, 8)
+8   u8   sampler slot
+9   u8   count (1-4; 3 for the environment slot 18)
+10  u32  runtime offset of the texture descriptor (0x18 apart)
```

If a slot is bound more than once, the binding with the lowest pass mask wins, and within a pass
the one with the lowest runtime offset (terrain materials are the exception: see below). A slot
bound twice in one pass is a layer list ordered by offset, as on terrain: the lowest offset is the
base layer. 12 techniques do this (about 290 materials): the road meshes on terrain tiles
(`46ac25f9`, `b22aaa42`, 44 materials) list their road texture and normal map first and then the
16×16 white placeholder `336759d8` (alpha 0) and the flat normal `75cb8229` as an unused second
layer; others add a white tint (`fb20fae7`), a 64×4 ramp, a second blend layer, or a second slot
15. Until `MATERIAL_VERSION` 4 the importer took the highest offset, so Baku's roads came out as the
blank placeholder. The table also holds textures that are not on the disc: slot 18's
environment cube `123456a6` (546 bindings) and a few slot 3 and 255 entries, presumably made at
run time.

Earlier versions of the importer found bindings by scanning the payload for known texture
hashes. The parser keeps that scan as a fallback (`MaterialInfo.parsed` is then False). On the
disc the scan finds exactly the table's entries for textures that exist.

### Parameters

A **parameter** sets shader constants. It has no name: it is identified by its technique and its
*destination*, a byte offset inside the technique's program chunk (the chunk is not in the
material, so the vertex or fragment register is unknown). The same parameter has a different
destination in each pass.

**Engine parameters** (tag 2) are values the renderer supplies:

```
DEADBEEF, u32 2, u32 semantic id, u32 pass mask, u32 destination
```

Semantic ids 1, 3, 4, 5, 7, 8 and 9 occur in nearly every technique (transforms and similar);
2, 14–18, 22, 27, 29 and 30 in some. 31 and 32 occur only in the water technique; they may be the
two water-constant rows the recomp's `scene-plan.md` writes for group 12.

**Constant parameters** (tag 4) carry the material's own values:

```
DEADBEEF, u32 4, u32 pass mask, u16 value index, 3 bytes 0, u8 value type, u16 destination, 3 bytes
```

After the last constant come the values: `f32` words, indexed by the value index. Types follow
the recomp's `material-values.md`: 2 = integer (one word), 4–7 = `f32` × 1–4 (8–10, matrices, do
not occur). Pass mask 0 marks a handful of constants that no pass reads.

**Known constants.** Meanings come from the values across a technique's materials
(`materials.TECHNIQUES`):

| Parameter | Value | Techniques | Evidence |
| --- | --- | --- | --- |
| UV transform | `(su, sv, ou, ov)`: `uv' = uv · s + o` | 31 | `(1, 1, 0, 0)` on 95 % of materials; the others fit their textures (`(32, 32, 0, 0)` with 64×64 base textures, `(0.2, 0.2, 0, 0)` with a 512×512 one) |
| Detail scale | `(d, d)`: detail UV = `uv0 · d` | 25 | Present when slot 16 or 17 is bound (except 5 materials that bind slot 16 with no parameter for it); 1–256, mostly 8–32 |
| Terrain layer UVs | `(s, s, s·x0, s·z0)` × 4 | terrain | Destinations 0x208–0x250; see [Terrain materials](#terrain-materials) |
| Terrain layer specular | `(r, g, b, power)` × 4 | terrain | Destinations 0x448–0x490 |
| Water colour | `(r, g, b, a)`, dark, e.g. `(0.007, 0.009, 0.015, 0.7)` | water | Destination 0x430; `a` is 0.05–0.9, unknown |
| Wire colour | `(r, g, b)`, linear, `(0.0014, 0.0014, 0.0014)` | wire | Destination 0xB8; the material's only constant |

Many techniques also have a `(r, g, b, power)` row that looks like a specular colour and exponent
(for example `(0.047, 0.047, 0.047, 50)` at 0x2F8 in `7bda1e1f`), colour multipliers, and
scalars. They are not used.

When a technique has two detail parameters (`53126094` binds slots 16 and 17), the importer gives
them to the bound detail slots in slot order. That order is a guess.

### Pass records (tag 7)

20 bytes each. Byte 1 counts up (0, 1, 2, …) and probably selects the technique table entry. The
last 8 bytes vary with the technique (`00 00 03 b2 cc 00 fc 00` on terrain) and may be the
recomp's 64-bit technique flags. The trailer after the last record holds 32-bit pairs that look
like the technique table's (chunk offset, length) entries. Not decoded further.

### Techniques

The largest groups and the special ones:

| Technique | Materials | Group bits | Slots | Use |
| --- | --- | --- | --- | --- |
| `f9f3bda5` | 1,440 | `0x10` | 0, 2, 3, 8, 12 | Terrain tiles |
| `7bda1e1f` | 765 | `0x10` | 0, 2, 3, 4, 16 | Level pieces (151 alpha-tested) |
| `9b4bb646` | 195 | `0x10` | 0, 2, 3, 4, 16 | Level pieces |
| `154af603` | 183 | `0x00`, `0x01` | 3, 4, 16 | Props |
| `53126094` | 141 | `0x00` | 3, 4, 5, 16, 17, 18 | Weapons and gear (detail colour) |
| `1d64212b` | 86 | `0x20` | 3, 4, 5, 15, 255 | Characters |
| `26e8d269` | 41 | `0x41`, `0x40` | 3 | Vertex-lit plants (all alpha-tested) |
| `c1f9aedc` | 39 | `0x20` | 3, 4, 5, 15 | Characters (the colour's alpha is not opacity) |
| `f436de9a` | 25 | `0x04` | 4, 18, 255 | **Glass**: windows, display cases |
| `ff0cafee` | 19 | `0x01` | 3 | Foliage models |
| `b18b8189` | 17 | `0x00` | 4, 18, 255 | **Water**: flat planes of 10 m to 1 km |
| `b9f18d99` | 5 | `0x00` | 3, 255 | Sky domes |
| `bc705d52` | 1 | `0x00` | none | **Wire**: Baku's power cables (`172d080c`, 428 models; positions only, no UVs or normals) |

`tools/material_check.py --techniques` prints all 62.

### What the importer builds

* **Alpha.** An alpha-tested material clips the base colour's alpha at the header's reference
  (`alpha ≥ ref`: a *Greater Than* node at `(ref − 0.5)/255`, render method Dithered). A
  translucent material (group bit `0x04`) whose colour texture has alpha is Blended. Otherwise
  the alpha channel is not opacity and stays unconnected. Before this decode, any colour texture
  with some transparent texels became a cut-out, which put holes in the Raven armour
  (`c1f9aedc`) and in other materials that store something else in alpha.
  Only a base map whose alpha is opacity keeps Blender's *Straight* alpha (image `<hash>_alpha`).
  Every other colour image is *Channel Packed* (`<hash>`; data images stay `<hash>_data`):
  Blender premultiplies straight images, so their colour is lost wherever alpha is 0, and opaque
  materials whose texture stores something else in alpha drew black (Baku's backdrop rocks
  `623ce871` have alpha 0 everywhere). Workbench still needs *Straight* to show cut-outs.
* **UV transform.** When it is not the identity, a *Material UV transform* Mapping node feeds the
  base colour, normal and specular maps (scale `(su, sv)`, location `(ou, 1 − sv − ov)` for
  Blender's flipped V). This affects 39 materials on the disc.
* **Detail normal (16).** Sampled at `uv0 · d` and added to the base normal's X and Y (UDN
  blending) before the Normal Map node.
* **Normal decoding** happens in one shared node group, *MAG Normal* (*MAG Normal (flip G)* with
  Flip Normal Green): it takes the base and the detail map's colour and alpha, a *DXT5nm* flag for
  each (X from alpha, else from red), and *Detail* (0/1). It sums the X/Y, rebuilds Z from the
  base map, and outputs the colour for the material's Normal Map node. Before `MATERIAL_VERSION`
  3, each material built these ~20 maths nodes itself, which made material building about twice
  as slow.
* **Detail colour (17).** Sampled at `uv0 · d`. The base colour is multiplied by
  `(2 · detail)^2.2`: 2× around mid-grey (the detail textures average 0.5), with the exponent
  because the game presumably blends gamma-space values.
* **Glass** (`f436de9a`): a clear, glossy, Blended surface (alpha 0.2), flag `mag_glass`.
* **Water** (`b18b8189`): the water colour parameter as base colour, roughness 0.05, flag
  `mag_water`.
* **Wire** (`bc705d52`): the wire colour parameter as base and viewport colour, flag `mag_wire`.
  Before `MATERIAL_VERSION` 4 the cables were the default white.
* Every material records its technique as `mag_technique`. `build.MATERIAL_VERSION` is stored as
  `mag_v`, so the materials an older importer left in a `.blend` are rebuilt, not reused.

### Sampler slots

These meanings were inferred from which texture formats get bound to each slot across the whole
game, and from how the results render:

| Slot | Meaning | Typical format | Importer |
| --- | --- | --- | --- |
| 3 | Base colour | DXT1, or DXT5 with alpha | Base Color; Alpha only for alpha-tested or translucent materials (see above) |
| 4 | Normal map | DXT5nm (X = A, Y = G), sometimes RGB | Normal Map node; Z rebuilt as √(1−x²−y²) |
| 5 | Specular colour, gloss in alpha | DXT5 | Luminance → Specular IOR Level; 1 − A → Roughness |
| 15 | Emissive or mask | — | Unconnected image node |
| 16 | Detail normal | DXT5nm, often a flat 16×16 | Added to the normal map's XY at the detail scale |
| 17 | Detail colour | DXT1, mid-grey centred | Multiplies the base colour at the detail scale |
| 18 | Environment (3 entries) | Mostly a cube that is not on the disc | Not added |
| 255 | Auxiliary mask | — | Unconnected |
| 0, 2, 13 | Unknown | — | Not added |

The optional **Flip Normal Green** setting inverts Y for any asset whose normal map uses the
other convention.

### Terrain materials

Code: `materials.TerrainLayers`, `build.MaterialCache._build_terrain` and `build.terrain_group`.
Check: `tools/terrain_check.py`.

Every terrain leaf has its own material, and on all 15 maps it has the same layout: a 0x418-byte
payload whose first-pass bindings are

| Slot | Bindings | Format | Size | Meaning |
| --- | --- | --- | --- | --- |
| 3 | **4** | DXT1, or DXT5 | 512×512 | **Detail layer colours**, tiled. Shared across the map (Baku's 256 tiles use 6 distinct textures). Some layers carry a mask in alpha (unused) |
| 4 | **4** | DXT5nm | 512×512 | **Detail layer normals**, paired with the colours |
| 8 | 1 | DXT5 | 256×256 | **Layer weight map**, unique per tile: R, G, B, A weight layers 0–3. The channels sum to about 255 |
| 12 | 1 | DXT1 | 64×64 | **Macro colour**, unique per tile: the layers pre-blended at low resolution. Later passes (masks 2 and 4) bind only this slot and slot 2, so it is the distant-LOD colour |
| 0 | 1 | DXT1 | 16×16 | Same texture on every tile (purple 75, 36, 75); unknown |
| 2 | 1 (+1 in pass 2) | DXT1 | 4×4 | Same texture on every tile (128, 64, 128); unknown |

**Layer order.** The binding entry's last u32 is the byte offset of its texture descriptor in the
material, 0x18 bytes apart: weights at +0x328, macro +0x340, colours +0x358…+0x3A0, normals
+0x3B8…+0x400, then slots 0 and 2. Sorting each slot's bindings by this offset gives layer 0–3.
The colours happen to be listed in that order, but the normals are not (on Baku the first-listed
normal is layer 1). Pairing by offset matches the images: dirt, pebbles, rock and mud colour
layers line up with the dirt, pebble, rock and flat normals.

**Weight channels.** R, G, B, A select layers 0, 1, 2, 3. Two tests confirm this:

* the 64×64 macro colour correlates with `Σ weight · mean layer colour` best for this order; and
* across tile borders, the weight maps are continuous in *layer* space (a channel is credited to
  the texture it selects, so neighbouring tiles with different layer sets can be compared). On six
  maps the edge mismatch is 0.007–0.014 for this order, 0.009–0.056 for the other orders (when
  they differ at all), and 0.17–0.27 with V and U flipped.

**Constants.** Technique `f9f3bda5` has eight pass-1 constants (see [Parameters](#parameters)):

```
0x208, 0x220, 0x238, 0x250   (s, s, s·x0, s·z0)   layer k detail UV: uv_k = s · uv0 + (s·x0, s·z0)
0x448, 0x460, 0x478, 0x490   (r, g, b, p)         layer k specular colour and power, e.g.
                                                  (0.005, 0.005, 0.005, 3), (0.05, 0.05, 0.05, 5), (0, 0, 0, 1)
```

In the 0x418-byte terrain payload their values sit at +0x322 and +0x362, which is what the
importer read before the record layout was decoded (and still reads if the structure does not
parse). The material's `u16 +0x46` (0x320) is a runtime offset, and only coincidentally close.

The scale `s` is per layer (10.5–36; Baku uses 12, 16, 17 and 21). `(x0, z0)` is the tile's origin in
tiles, so the offset cancels UV 0's own per-tile offset: `uv_k = s · world_xz / L` with
`L` = the leaf size (64 m on 1,024-leaf maps, 128 m otherwise, V along −Z). The detail UVs are
therefore continuous across the whole map, to within half-float precision.

**UVs.** UV 0 maps the leaf to 0–1 (`u = x / L − x0`, `v = −z / L + z0`, exact on every tile). UV 1
is identical to UV 0 on the sampled Baku tiles. The weight map and the macro colour are sampled
with UV 0, and their edge texels match the neighbouring tiles' in this orientation.

**What the importer builds** (*Terrain Detail: Full Blend*, the default): each tile material
samples the weight map and the macro colour with `UVMap` (clamped at the edges), and each layer
through a Mapping node (scale `s`, location `(s·x0 mod 1, (1 − s − s·z0) mod 1)`, which is the
game's UV with Blender's flipped V). A shared **MAG Terrain** node group normalises the weights,
blends the four colours, blends the four normals' decoded XY (DXT5nm: X in alpha, Y in green),
rebuilds Z, and returns a colour and a tangent-space normal colour for a Normal Map node. With
camera distance (*Camera Data* View Distance) the group fades to the macro colour and a flat normal
between its *Macro Fade Start* and *End* value nodes (import option *Macro Fade*, default 250 and
500 m; the game's own switch distance is unknown). Roughness
is a fixed 0.9 and Specular IOR Level 0.2 (the per-layer specular rows are very small). The macro
image is the material's active node, so Workbench's *Texture* mode shows the tile colour.
*Macro Only* binds just the macro colour as Base Color.

Before this was decoded, the importer showed the first slot-3 binding (layer 0, stretched over the
tile) as a "macro colour map", and took slot 4's first binding as a macro normal map.

**Not used yet:** the colour layers' alpha masks (`c0f868b1`, the Baku pebble layer, has one; it
may drive height-based blending), the specular rows, slots 0 and 2, UV 1, and the terrain tree's
per-region textures (hash C: DXT5 256×256 with data only in G and A, see
[properties-and-levels.md](properties-and-levels.md#terrain-tree-record-284c2f78)).

### Materials with no colour texture

Two techniques bind a normal map (slot 4) but no base colour: water (`b18b8189`) and glass
(`f436de9a`). Before the header was decoded, the importer treated all of them as water, so the
glass of Canal's stacked display cases rendered dark and glossy. Both are now told apart by
technique (see [What the importer builds](#what-the-importer-builds)); if a material's structure
does not parse, the old rule still applies.
Workbench's *Texture* colour mode draws the active image node whatever it is connected to, so
every material without a colour texture gets an unconnected 1×1 `MAG_preview_<rgb>` image of its
base colour (or the water and glass viewport colour) as the active node (flag `mag_preview`).
Rendered output does not change.

## Sky (`sky.zrb`)

Every level archive has a `resource_blocks/sky.zrb` with four records: two textures, one
material and one level piece (`80DC0132`). The 15 levels share 5 domes and 5 sky textures:

| Dome | Material | Colour texture (slot 3) | Levels |
| --- | --- | --- | --- |
| `cdea8d73` | `7d33b316` | `747a10da` (DXT1) | Alaska Subpen, Kennicott, Training Valor, Unalaska, Valour Sup |
| `dd2b01e9` | `966d361e` | `3a6e7f3f` (DXT1) | Baku, Cosmodrome, Sver Sup, Training Sver |
| `3cd18f67` | `9ae05b48` | `e5c30440` (DXT5, 1 mip) | Canal, Panama, Raven Sup, Training Raven |
| `72a29ddd` | `5d43a81e` | `4534efc7` (DXT1) | Remote Comms |
| `494c3cd8` | `18c48da5` | `0b3b3ac0` (DXT1) | Voz |

**Dome mesh.** One node, one mesh (LOD distance 99999), Edge streams with a position (half ×3)
and a normal only: **no UV set**. The dome is small, about 5.1 m in horizontal radius (5.6 m for
`dd2b01e9`) and 1–2 m high: a flattened upper shell plus a skirt down to about −20° of
elevation, closed by a fan at the bottom. Faces and normals point inward. It is rotationally
symmetric to within about 3 % (the 64-sided rim ring). The game draws it around the camera.

**Texture.** The colour texture is 1024×1024 and shows the sky from below as a disc inscribed in
the square: the centre is the zenith and the rim is the horizon (Kennicott's rim is a mountain
silhouette). Slot 255 binds `42f406fd`, a 64×64 DXT5 noise shared by every sky (probably
dither); it is not used.

**Mapping (inferred).** With no UVs, the shader must derive the lookup. The importer uses a
planar projection of the dome's own positions, which puts the rim exactly on the disc edge and
the apex at the centre:

```
uv = 0.5 + 0.5 * (x, z) / R        R = max |x|, |z| of the dome (game axes, Y up)
```

Not confirmed: the mirror orientation (sign of z, or a turn), and whether R comes from the
material instead of the mesh.

**Material values.** `u16 +0x46` is 0x130 and the values start at +0x1F1 (unaligned): `(1, 1, 0,
0)`, `(60, 60, 20000 or 5000, 200 or 400)`, then colour triples such as `(0.974, 0.908, 0.609,
2.0)` and `(1, 0.992, 0.914, 0.7)`, and `(200, 0.01, 0.001)`. They vary per sky and look like
fog, haze and tint parameters. None is a texture transform. They are not used.

**Import.** `core/sky.py` finds the piece (`sky_piece`) and computes the planar UVs (`dome_uv`)
and the dome's **elevation profile** (`elevation_profile`): the texture radius `s = |(x, z)| / R`
averaged per degree of elevation. Because the dome is symmetric, `s` depends on elevation
alone, so a World shader can apply the same mapping to a view direction `d`:
`uv = 0.5 + 0.5 · s(elevation(d)) · normalize(d.x, d.y)` (Blender axes; Blender's `(x, y)` is
the game's `(x, −z)`, and V is flipped as for every UV set, so the signs cancel). The profile is a
Float Curve node (*Sky Profile*, x = (elevation + 90°) / 180°, y = s / 1.1, vector handles).
`tools/sky_check.py` finds the World mapping within 9 texels (median 1–1.4) of the planar UVs at
every dome vertex above the horizon, on all five domes. `tools/sky_test.py` renders the World
and the Dome from the same camera on all five: mean difference per channel ≤ 0.002 looking up,
≤ 0.015 at 30°, and ≤ 0.04 at the horizon, where the rim's 3 % asymmetry shows.
