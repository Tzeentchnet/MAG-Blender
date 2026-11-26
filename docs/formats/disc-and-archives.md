# Disc image and PSARC archives

Code: `mag_importer/core/iso9660.py`, `mag_importer/core/psarc.py`.

## Disc image

The decrypted disc (`MAG (USA).dec.iso`, about 15 GB) is a plain ISO9660 volume with a Joliet
supplementary descriptor.

* Volume descriptors start at sector 16 (2048-byte sectors). Type 1 is the primary descriptor and
  type 2 with escape sequence `%/@`, `%/C` or `%/E` is Joliet. Type 255 ends the list.
* **Prefer Joliet.** The level 1/2 names in the primary tree are truncated, for example
  `MAG_PANAMA_MILITARY_BASE_FINAL.`, while Joliet has the full UTF-16BE names.
* Directory records are standard: the extent LBA is at +2 (LE u32), the size at +10 (LE u32),
  flags at +25 (bit 1 = directory), the name length at +32 and the name at +33. Records never
  cross a sector boundary. A zero length byte means "skip to the next sector".
* The reader keeps a map of `upper-case path → (byte offset, size)` and opens one file handle per
  thread, so the scanner's thread pool can read without locking.

### Layout

```
/PS3_GAME/USRDIR/RUN/MAG/PS3/MAIN.PSARC                      162 MiB,  543 entries
/PS3_GAME/USRDIR/RUN/MAG/PS3/LEVELS/MAG_<NAME>_FINAL.PSARC   98–289 MiB, 943–4,590 entries
```

| Level archive | Size (MiB) | Entries |
| --- | --- | --- |
| MAG_ALASKA_SUBPEN_FINAL | 195 | 2,332 |
| MAG_BAKU_FINAL | 231 | 4,590 |
| MAG_CANAL_FINAL | 237 | 3,379 |
| MAG_COSMODROME_FINAL | 100 | 1,016 |
| MAG_KENNICOTT_FINAL | 107 | 1,177 |
| MAG_PANAMA_MILITARY_BASE_FINAL | 201 | 3,786 |
| MAG_RAVEN_SUP_FINAL | 109 | 1,018 |
| MAG_REMOTE_COMMS_FINAL | 108 | 1,175 |
| MAG_SVER_SUP_FINAL | 98 | 943 |
| MAG_TRAINING_RAVEN_FINAL | 115 | 998 |
| MAG_TRAINING_SVER_FINAL | 265 | 2,998 |
| MAG_TRAINING_VALOR_FINAL | 170 | 1,674 |
| MAG_UNALASKA_FINAL | 262 | 3,494 |
| MAG_VALOUR_SUP_FINAL | 118 | 1,054 |
| MAG_VOZ_FINAL | 289 | 3,521 |

The scanner processes `MAIN.PSARC` first, then the levels in path order, so that archive index 0
is always MAIN. Lookups that fall back to shared assets depend on this order.

## PSARC 1.3

Everything is big-endian.

### Header (32 bytes)

| Offset | Type | Field |
| --- | --- | --- |
| 0x00 | char[4] | `PSAR` |
| 0x04 | u32 | version (1.3 → `0x00010003`) |
| 0x08 | char[4] | compression, always `zlib` on this disc |
| 0x0C | u32 | TOC length (bytes, from the archive start, header included) |
| 0x10 | u32 | TOC entry size (30) |
| 0x14 | u32 | entry count (including the name-list entry) |
| 0x18 | u32 | block size (65536) |
| 0x1C | u32 | flags (path handling; not needed for reading) |

### TOC entries (30 bytes each, starting at 0x20)

| Offset | Size | Field |
| --- | --- | --- |
| 0 | 16 | MD5 of the path (not used) |
| 16 | 4 | index of the entry's first block in the block-size table |
| 20 | 5 | uncompressed length (u40) |
| 25 | 5 | byte offset of the first block from the archive start (u40) |

Entry 0 is the **manifest**: newline-separated paths for entries 1…N, in order. The importer
lower-cases these paths, for example `/run/mag/ps3/levels/mag_baku_final/resource_blocks/...`.

### Block-size table

The block-size table follows the entries and runs to the end of the TOC. Each value is a
big-endian integer `ceil(log256(block_size))` bytes wide, which is **2 bytes** for 64 KiB blocks.
Each value is the compressed size of one block.

* A value of **0** means a full, uncompressed block (`block_size` bytes).
* A value equal to the expected uncompressed length means the block is stored raw.
* Otherwise the block is a zlib stream. If decompression fails, the reader falls back to the raw
  bytes.

An entry's blocks are contiguous. Block *k* of an entry starts at `offset + Σ sizes[first..first+k-1]`.

### Partial extraction

`Psarc.read(name, start, length)` inflates only the blocks that cover `[start, start+length)` and
reads their compressed bytes in a single read. The scanner relies on this to stay fast. A ZRB's
record table (segment 0) sits at the front of the file, so indexing reads the first 64 KiB and
then only the extra blocks that `header_span()` asks for. A full scan of all 16 archives takes
about 20 seconds.

The cached index stores each archive's TOC as `(block_size, sizes, entries)`, so later sessions
skip re-reading the manifest.

## Archive contents

| Pattern | Meaning |
| --- | --- |
| `*.zrb` | Resource blocks ([zrb.md](zrb.md)) |
| `*/headers.zrb` | Excluded by the scanner (its contents have not been examined; see the roadmap) |
| `<level>.clv` | Compiled level: a stream of property objects ([properties-and-levels.md](properties-and-levels.md)) |
| `*.zpr` | Property tables (game data) |

Level ZRBs are named after their role and their streaming position:

| Name | Contents |
| --- | --- |
| `globaldata.zrb` | Shared level assets: textures, materials, pieces, foliage models, baked lighting, Havok, and several types not yet identified |
| `loadblock_<x>_<z>_radius_<r>.zrb` | Streamed region: level pieces, foliage batches |
| `terrain_<x>_<z>_radius_<r>.zrb` | Terrain tiles (level pieces) for one node of the terrain LOD tree |
| `environment_high_<x>_<z>_radius_<r>.zrb` | Textures only (1–6 each); probably high-resolution streamed texture data |
| `sky.zrb` | Sky dome: 1 level piece, 1 material, 2 textures (see [textures-and-materials.md](textures-and-materials.md#sky-skyzrb)) |
| `physics.zrb` | 455 records of type `2A8BDF5A` (collision meshes; see [havok.md](havok.md#collision-meshes-2a8bdf5a)) |

`<x>`, `<z>` and `<r>` appear to be the block's centre and radius in metres, in map space.
