# ZRB resource blocks

Code: `mag_importer/core/zrb.py`.

A ZRB is Zipper's resource container. Every asset on the disc lives in one: textures, materials,
models, Havok data, foliage, baked lighting and collision. All fields are big-endian.

## Header (0x30 bytes)

| Offset | Type | Field |
| --- | --- | --- |
| 0x00 | u32 | version, always **6** |
| 0x04 | u32 | unknown |
| 0x08 | u32 | segment 0 size |
| 0x0C | u32 | segment 0 end, which is also segment 1 start |
| 0x10 | u32 | segment 1 size |
| 0x14 | u32 | segment 2 start |
| 0x18 | u32 | segment 2 size |
| 0x1C | u32 | segment 3 start |
| 0x20 | u32 | segment 3 size |
| 0x24 | u32 | segment 4 start |
| 0x28 | u32 | segment 4 size |
| 0x2C | u32 | type count |

The file size is `seg4 start + seg4 size`.

### Segments

| # | Contents |
| --- | --- |
| 0 | Header, dependency list, type table and the serialized **resource records** |
| 1 | CPU data: Havok packfiles, plus CPU-side texture objects |
| 2 | Always empty on this disc |
| 3 | RSX (GPU) memory: texture pixels, RSX vertex and index buffers, Edge secondary streams, foliage instance colours |
| 4 | Edge geometry partitions (processed on the SPUs), foliage instance transforms |

Record offsets into segments are **relative to the segment start**, and buffer alignment is
computed on those relative offsets.

## Segment 0 layout

```
0x30  u16 dependency count
      dependency records: CRC32 path hash + NUL-terminated path ("run/mag/....zrb")
      type table: type count × (u32 type hash, u32 record count)
      records (immediately after the type table)
```

The exact framing of the dependency records has not been pinned down. The importer finds the
paths by scanning for `run/mag/` strings up to the first record. They are used only for display.

The start of the records is found by searching for the record magic. The type table is the
`8 × type count` bytes just before it. The total record count is the sum of the table's counts.

## Records

```
+0x00 u32  6
+0x04 u32  0x600DF00D            ("good food")
+0x08 u32  end offset, relative to the FIRST record
+0x0C u32  kind
+0x10 u32  segment 1 offset
+0x14 u32  segment 2 offset
+0x18 u32  segment 3 offset
+0x1C u32  segment 4 offset
+0x20 u32  type hash
+0x24 u32  name hash
+0x28      payload … up to end
```

The 8-byte magic `00000006 600DF00D` marks each record. Records follow each other directly, with
the next one starting at this one's `end`. The parser rejects a record whose end does not move
forward.

The payload is a byte-packed serialization specific to each type. The segment offsets tell you
where that resource's bulk data starts in each segment. Unused segments are 0.

`kind` is constant for each record type: 2 for Havok packfiles, 3 for sequence events
(`F27E3DFF`), 4 for foliage batches, 5 for textures, 6 for particle effects, and `0x3F` for
materials, every model type and the terrain tree. It may be a per-type version or loader class.
The importer does not need it.

## Type hashes

| Hash | Name used here | Status | Disc count |
| --- | --- | --- | --- |
| `85CE0660` | texture | Decoded | 69,474 |
| `7CBE7595` | material | Bindings decoded | 8,538 |
| `A4F4D23C` | static (hierarchical) model | Decoded | 502 |
| `10B17150` | skinned model (Edge) | Decoded | 30 |
| `80DC0132` | level piece (Edge static); also terrain tiles and the sky | Decoded | 19,705 |
| `B9F5747E` | foliage model | Decoded as a model | 113 |
| `03BE92A0` | foliage batch | Decoded (with its `.clv` transform entry) and imported (`core/foliage.py`) | 31,257 |
| `FB93282D` | Havok packfile | Decoded | 811 |
| `84DFB280` | baked per-vertex lighting of one placement (in `globaldata.zrb` and the `loadblock_*.zrb` files); see [properties-and-levels.md](properties-and-levels.md#baked-lighting-84dfb280) | Decoded and imported (`core/lighting.py`) | 22,547 |
| `284C2F78` | terrain LOD tree (1 per level globaldata) | Layout decoded; see [properties-and-levels.md](properties-and-levels.md#terrain-tree-record-284c2f78) | 15 |
| `2A8BDF5A` | collision mesh (in `physics.zrb`): Havok triangle meshes; see [havok.md](havok.md#collision-meshes-2a8bdf5a) | Decoded and imported (`core/collision.py`) | 4,780 |
| `F27E3DFF` | sequence events (112 in Baku's globaldata): embedded property objects such as `SoundEventDef`, `ParticleEventDef`, `PrintEventDef`, `AnimObjectCreationEventDef`, `CameraShakeEventDef` | Identified; not needed | — |
| `98057459` | particle effect (80 in `particle_effects.zrb`, 42 in `global_common.zrb`). Map placements of emitters point at these | Identified; not decoded | — |
| `D0A0F5E5` | per-language records: 161 in each of the 16 `globaldata_<language>.zrb` files of a level | Not examined | — |

Other types appear only in `globaldata.zrb`, one to three records each: `C90164D1`, `C641E7D7`,
`F61A2861`, `81FB91C1`, `24423B00`, `B4E53330`, `D19CECC6`, `992BAEA2`, `BAD70772`, `9E0E9DDA`,
`D1BBB22E`, `D802CE51`, `A9CB1E9E`, `B4D15096`, `4C01EBAB`. None of them have been examined yet.

Disc counts cover only the types the scanner keeps (`KEEP_TYPES` in `core/catalog.py`).
`tools/dump_record.py` lists and hex-dumps records of any type straight from the disc.

## `headers.zrb` (version 4)

Each level archive has a `resource_blocks/headers.zrb`, which the scanner skips. It is **not** a
version 6 ZRB: it starts with `u32 4`, and it is a directory of the level's other ZRBs. Each entry
holds a length-prefixed path (`run/mag/ps3/levels/<level>//resource_blocks/<name>.zrb`), followed
by a copy of that ZRB's header and record table. Baku's has 20,751 record magics and 4,573 path
strings. The game presumably reads it to learn every block's records without opening each file.
The exact entry framing has not been worked out. It holds nothing the per-file scan does not
already find.

## Name hashes and lookup

* A name hash identifies an asset across ZRBs. Materials reference textures by name hash, models
  reference materials, and level placements reference models.
* One name can appear in several ZRBs, for example the same texture in more than one level.
  Lookup prefers, in order: the same ZRB, the same archive, the MAIN archive, then any archive
  (`GameIndex.find` / `find_in_archive`).
* The asset name hash function is unknown. Readable names come from property tables and Havok
  source paths (see [properties-and-levels.md](properties-and-levels.md)). Property
  `util::Token` values and model node names, on the other hand, are plain CRC32 (zlib) of their
  text (see [Weapon, attachment and head tables](properties-and-levels.md#weapon-attachment-and-head-tables)).

## How the scanner uses ZRBs

1. Read the first 64 KiB of the ZRB. `header_span()` returns the end of segment 0. If that is past
   64 KiB, read up to it.
2. Parse the records and keep the tuples `(off, end, kind, seg1..4, type, name)` for the kept
   types.
3. For Havok records, read just the packfile out of segment 1 (its size is the u32 at payload
   +4) and summarise it.
4. Terrain ZRBs also keep each level piece's footprint
   (see [properties-and-levels.md](properties-and-levels.md#terrain)).

The resulting index is pickled to `mag_index.pkl` (about 14 MB). Bump `INDEX_VERSION` in
`core/catalog.py` whenever the index layout changes.
