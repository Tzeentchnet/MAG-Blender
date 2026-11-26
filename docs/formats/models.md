# Models and geometry

Code: `mag_importer/core/models.py` (records, buffers, partitions) and `mag_importer/core/vertex.py`
(attribute codecs).

Four record types share one model serialization:

| Type | Use | Geometry path |
| --- | --- | --- |
| `A4F4D23C` | Static hierarchical models: weapons, vehicles, props | Mostly RSX vertex and index buffers, plus a node table |
| `10B17150` | Skinned characters and gear | Edge partitions with skinning |
| `80DC0132` | Level pieces, terrain tiles, sky | Edge static partitions |
| `B9F5747E` | Foliage models | Edge or RSX |

A mesh uses Edge when its partition count is non-zero. Otherwise it is a plain RSX buffer pair.

## Model payload

The payload is big-endian and **byte-packed**: fields are not aligned, so u32s sit at odd
offsets.

```
+0x00  8 bytes   zero
+0x08  f32[4]    bounding sphere (centre xyz, radius)
+0x18  f32[6]    bounds (centre xyz, half-extents xyz; matches the decoded vertices exactly;
                 used for terrain footprints and the backdrop rule; Model.bounds)
+0x30  u8        ?
+0x31  u8        LOD count
+0x32  u8        0xFF
+0x33  u32       mesh count          (sanity limit 4096)
+0x37  u32       attachment count    (sanity limit 256)
+0x3B            attachments, then mesh entries, then (A4F4D23C only) the node table
```

### Attachments

Normally each attachment is 76 bytes:

```
+0   u32      name hash (socket name, e.g. a muzzle or sight point)
+4   f32[12]  3×4 matrix
+52  24 bytes unknown
```

Some level pieces have variable-size attachment records. When the entry after
`0x3B + 76 × count` does not validate as a mesh entry, the parser searches forward from 0x3B for
the first offset that does. The check: u32 0 at the entry start, attribute mask byte 0x1D, the
byte before it 0, and the constant 3 in the draw block.

### Mesh entry (149 bytes)

Offsets are relative to the start of the entry. These come from the parser code (the `models.py`
docstring matches them).

| Offset | Type | Field |
| --- | --- | --- |
| +0 | u32 | 0 |
| +4 | f32 | maximum LOD distance |
| +8 | u16 | flags |
| +10 | … | u16, u32 (unknown) |
| +16 | u8 | 0 |
| +17 | u8 | attribute mask (0x1D seen on every mesh) |
| +18 | u32 | unknown |
| +22 | u8 | **Edge partition count** (0 means plain RSX buffers) |
| +23 | u32 | **RSX bytes**: the vertex buffer, or Edge secondary streams |
| +27 | u32 | **Edge partition bytes** |
| +31 | … | unknown, including bounds (about 52 bytes) |
| +91 | u32 | **draw flags**: low byte is the LOD mask, bit `0x04000000` means skinned |
| +95 | u16 | unknown |
| +97 | u32 | **V**, the vertex count |
| +101 | u32 | **K**, the index count |
| +105 | u32 | 3 (triangle list; used as a layout check) |
| +109 | u32 | **vertex format** bits (RSX meshes) |
| +113 | … | unknown (32 bytes) |
| +145 | u32 | **material name hash** |

**LODs.** A mesh belongs to LOD *n* when `lod_mask & (1 << n)` is set. A mask of 0 means all
LODs. If a model has nothing for the requested LOD, the importer falls back to the lowest LOD
that is present.

### Node table (static models `A4F4D23C` only)

The node table starts right after the last mesh entry:

```
+0     u16      node count (≤ 1024)
+2     12 bytes unknown
+14    node count × 32-byte local transforms:
           f32[4] rotation quaternion (x, y, z, w)
           f32[3] translation
           4 bytes pad
then   node count × 48-byte node records:
           f32[6] bounds
           u32    node name hash
           u16[10]: [1] first mesh, [2] mesh count  (0xFFFF in [1] = no meshes)
                    [9] parent index                 (0xFFFF = root)
```

Parents always come before their children. The table is thrown away when any parent or mesh range
is out of bounds. Models without a node table get a single node holding every mesh.

Leaf nodes with no meshes are attachment helpers. The importer deletes them to keep the scene
tidy.

## Segment walking

Mesh data is laid out in order across the segments, starting at the record's segment offsets.
All alignment is **relative to the segment**.

**RSX mesh** (partition count 0):

```
seg3: align(cursor, 0x80) → vertex buffer (rsx_size bytes)
      align(…, 0x80)      → u16 index list (K entries)
      cursor = index start + 2K           (no trailing alignment)
```

**Edge mesh**:

```
seg3 cursor = align(seg3, 0x80)
for each partition:
    partition header + streams at seg4 cursor (size from header, see below)
    secondary stream at seg3 cursor; seg3 cursor += align(secondary size, 0x10)
after the mesh:
    seg4 = align(seg4 + edge_size, 0x80)
    seg3 = mesh seg3 start + rsx_size
```

## RSX vertex buffers

The stride is `rsx_size / V`. Attributes are packed in this order:

| Bit | Attribute | Bytes | Encoding |
| --- | --- | --- | --- |
| `0x001` | position | 6 (or 12) | IEEE half ×3, or f32 ×3 |
| `0x002` | normal | 4 | RSX CMP (see below) |
| `0x004` | unknown | 4 | skipped |
| `0x008` | tangent | 8 | half ×4 (not imported) |
| `0x080` | colour | 4 | u8 ×4, /255 |
| `0x200` | UV0 | 4 | half ×2 |
| `0x400` | UV1 | 4 | half ×2 |

Positions are float32 when the stride is 6 bytes larger than the half-float layout. Any other
stride mismatch means the format is not understood, and the mesh is skipped.

The indices are a big-endian u16 **triangle list**. Triangles with an out-of-range or repeated
index are dropped.

## Edge partitions

Each partition starts with a 0x100-byte header. Its first 48 bytes are **24 u16 values (`h`)**
giving stream sizes and offsets:

| h | Meaning |
| --- | --- |
| h0, h1 | index stream bytes |
| h2, h3, h4 | position stream bytes |
| h5–h8 | primary attribute stream bytes (normals and tangents) |
| h9 | secondary (RSX, segment 3) stream bytes for this partition |
| h10, h11 | skin palette: byte offsets of the two matrix ranges (`skinMatricesByteOffsets[2]`); 0xFFFF means no palette |
| h12, h13 | skin palette: byte sizes of the two ranges (`skinMatricesSizes[2]`) |
| h14 | skinning influence stream bytes |
| h16 | position format table bytes |
| h17 | primary attribute format table bytes |
| h18 | trailing data bytes (skipped) |
| h19 | secondary format table bytes |

Partition size: `0x110 + h19 + Σh0..h8 + h14 + h16 + h17 + h18`.

The config block sits at header +0x100:

```
cfg+2   u8   position codec: 0 = f32×3, 6 = rebased half×3 (stride 6), other = use the format table
cfg+8   u16  V (vertices in this partition)
cfg+10  u16  K (indices in this partition)
cfg+0x10     secondary format table (h19 bytes)
```

The streams follow `cfg + 0x10 + h19` in this order:

```
indices    (h0 + h1)   u16 triangle list, partition-local
positions  (h2+h3+h4)
attributes (h5..h8)    normals / tangents
influences (h14)       4 × (u8 weight, u8 local bone) per vertex
format tables          position table (h16), then attribute table (h17)
```

### Format tables

```
+0  u8   declaration count
+1  u8   vertex stride
+2  6 bytes pad
+8  declarations, 8 bytes each: u8 offset, u8 codec, u8 components, u8 semantic, 4 bytes pad
```

Semantics: **1** position, **2** normal, **5–8** UV sets, **9** colour.

### Codecs

Edge (SPU-decompressed) streams and RSX-fetched streams share the codec numbers, but a few of
them decode differently:

| Codec | Edge stream (`decode_edge`) | RSX stream (`decode_rsx_decl`) |
| --- | --- | --- |
| 1 | i16 normalised, `(2v + 1) / 65535` | i16 / 32767 |
| 2 | f32 | f32 |
| 3 | **rebased half** | IEEE half |
| 4 | u8 / 255 | u8 / 255 |
| 5 | i16 integer | i16 integer |
| 6 | packed 11/11/10 with `(2s + 1)` scaling | RSX CMP 11/11/10 |
| 7 | u8 integer | u8 integer |
| 9 | 3-byte spherical normal | — |

**Rebased half:** like binary16, but exponent 31 is an ordinary exponent (there is no inf or NaN)
and there are no subnormals. Every non-zero code is `±2^(E−15) × (1 + m/1024)`.

**RSX CMP normal:** a big-endian u32. Bits 0–10 hold x (s11), bits 11–21 hold y (s11) and bits
22–31 hold z (s10). Decode as `x/1023, y/1023, z/511`.

**Edge packed (codec 6):** the same bit fields, decoded as `(2s+1)/2047` for x and y and
`(2s+1)/1023` for z.

**Spherical normal (codec 9):** a 24-bit big-endian value `n`.

```
u   = (n >> 14) & 1023
v   = (n >> 4)  & 1023
sel = n & 15
a   = −1/√2 + u·√2/1023
b   = −1/√2 + v·√2/1023
c   = √max(0, 1 − a² − b²), negative unless sel bit 0
axis = min(sel >> 2, 2)          lane that receives c; a and b fill the others in order
w    = +1 if sel bit 1 else −1   (tangent handedness)
```

The codec definitions come from the recomp's `geometry-streams.md` and `geometry-normals.md`.

### Where attributes come from

* **Positions:** from the position stream using the position codec, or through the position
  table's semantic-1 declaration.
* **Normals:** from the primary attribute stream (semantic 2) when h5 and h17 are non-zero.
  Otherwise from the secondary stream.
* **UVs and colours:** from the secondary stream in segment 3, decoded with RSX semantics.
* A mesh keeps an attribute only if every one of its partitions has it. Mesh-level UVs are
  truncated to the smallest UV count across its partitions.

### Skinning

The influence stream has 8 bytes per vertex: four `(u8 weight, u8 local bone)` pairs. Weights
are /255.

A local bone index points into the partition's **skinning palette**. The palette is made of two
contiguous ranges of the skeleton's 3×4 matrix array, with 48 bytes per matrix:

```
o0 = h10 / 48, o1 = h11 / 48, n0 = h12 / 48
bone = o0 + local            if local < n0
     = o1 + (local − n0)     otherwise
```

When h10 is 0xFFFF there is no palette, and the local index is the skeleton bone index. This
mapping has been checked against the bind poses of every character.

Skinned models do not name their skeleton. The importer scores each Havok skeleton by how well
its bind pose matches the mesh: for each bone, it measures the distance from the centroid of the
vertices dominated by that bone to the bone or to its child segments. The skeleton with the
lowest median distance wins. Results include `root_main` for bodies, `root_arms` for
first-person arms and `chute_root` for parachutes.
