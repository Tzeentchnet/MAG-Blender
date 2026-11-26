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

The serialized header's size depends on the shader technique, and its layout has not been
decoded. Texture bindings are instead found by **scanning** the payload for any u32 that is a
known texture name hash, then checking the 14-byte binding entry that starts there:

```
+0   u32  texture name hash
+4   u32  pass / technique mask    (must be 0 < mask < 0x10000)
+8   u8   sampler slot
+9   u8   count                    (must be 1–4)
+10  u32  offset (meaning unknown)
```

If a slot is bound more than once, the binding with the lowest pass mask wins. The scan is
reliable because texture hashes almost never occur by chance and are checked against the global
index.

**Flag word.** The u32 at +0x28 (`MaterialInfo.flags`). Its low bits look like the vertex
attributes the technique reads (`0x20a`, `0x28a`, `0x60a`, `0x20b` seen). Bit **`0x10000000`**
marks materials drawn with the placement's baked per-vertex lighting (see
[properties-and-levels.md](properties-and-levels.md#baked-lighting-84dfb280)): a lighting
record's length equals the vertex count of exactly these meshes. The rest of the header is still
not decoded.

### Sampler slots

These meanings were inferred from which texture formats get bound to each slot across the whole
game, and from how the results render:

| Slot | Meaning | Typical format | Importer |
| --- | --- | --- | --- |
| 3 | Base colour | DXT1, or DXT5 with alpha | Base Color; Alpha when it is a cut-out (dithered) |
| 4 | Normal map | DXT5nm (X = A, Y = G), sometimes RGB | Normal Map node; Z rebuilt as √(1−x²−y²) |
| 5 | Specular colour, gloss in alpha | DXT5 | Luminance → Specular IOR Level; 1 − A → Roughness |
| 15 | Emissive or mask | — | Unconnected image node |
| 16 | Detail normal | DXT5nm | Unconnected |
| 17 | Detail colour | — | Unconnected |
| 18 | Environment (3 entries) | — | Not added |
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

**Values block.** The material's `u16 +0x46` is 0x320. The values are 2-byte aligned at **+0x322**:

```
+0x322  4 rows  (s, s, s·x0, s·z0)   layer k detail UV: uv_k = s · uv0 + (s·x0, s·z0)
+0x362  4 rows  (r, g, b, p)         layer k specular colour and power, e.g. (0.005, 0.005, 0.005, 3),
                                     (0.05, 0.05, 0.05, 5), (0, 0, 0, 1)
```

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

Materials that bind a normal map (slot 4) but no base colour are water or liquid shaders. They
get a dark, glossy Principled base (0.015, 0.035, 0.04, roughness 0.05) and the custom property
`mag_water`. The rule also catches a few small props (for example stacked cases on Canal).
Workbench's *Texture* colour mode draws the active image node whatever it is connected to, so
every material without a colour texture gets an unconnected 1×1 `MAG_preview_<rgb>` image of its
base colour as the active node (flag `mag_preview`). Rendered output does not change.

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
