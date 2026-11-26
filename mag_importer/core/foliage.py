"""Foliage batches (``03BE92A0``) and their per-batch transform table in the level ``.clv``.

See docs/formats/properties-and-levels.md#foliage. In short:

* batch record payload: ``u32 0, u32 count, u32 foliage model hash, 12 bytes 0``;
* segment 4: 16 bytes per instance, eight rebased halves ``ox oy oz scale qx qy qz qw``;
* segment 3: 4 bytes per instance, RGBA8 (baked lighting colour);
* ``.clv`` table: 81-byte big-endian entries ``u32 batch hash, f32[16] row-major matrix,
  f32 1.0, u8 count, f16 fade end, f16 fade multiplier, f16 fade start, f16 radius``.

A model vertex P of instance i lands at ``T + o_i + s_i * R(q_i) * P`` (game space).
"""
import struct

import numpy as np

from .vertex import gather, rebased_half

ENTRY = 81
_ONE = struct.pack('>f', 1.0)
_ZERO = b'\0\0\0\0'


class Batch:
    __slots__ = ('name', 'model', 'count', 'offsets', 'scales', 'quats', 'colours')


def read_batch(zrb, rec):
    """Decode one batch record: model hash, offsets (N,3), scales (N,), quats (N,4) xyzw and
    colours (N,4) uint8, all in game space relative to the batch origin."""
    p = zrb.payload(rec)
    _, count, model = struct.unpack_from('>III', p, 0)
    b = Batch()
    b.name, b.model, b.count = rec.name, model, count
    rows = rebased_half(gather(zrb.data, zrb.seg_offset(4, rec.seg4), count, 16, 16))
    b.offsets = rows[:, 0:3]
    b.scales = rows[:, 3]
    b.quats = rows[:, 4:8]
    b.colours = gather(zrb.data, zrb.seg_offset(3, rec.seg3), count, 4, 4).copy()
    return b


def _is_entry(clv, o):
    """Cheap structural check of an 81-byte table entry at ``o``."""
    if o < 0 or o + ENTRY > len(clv):
        return False
    m = clv[o + 4:o + 0x44]
    return (m[0:4] == _ONE and m[20:24] == _ONE and m[40:44] == _ONE and m[60:64] == _ONE
            and m[48:52] == _ZERO and m[52:56] == _ZERO and m[56:60] == _ZERO
            and clv[o + 0x44:o + 0x48] == _ONE)


def batch_table(clv, any_hash):
    """``batch hash -> (translation (3,) game space, fade end, fade start, radius, count)`` for
    the whole table, located from one batch hash known to be in it. Empty if not found."""
    needle = struct.pack('>I', any_hash)
    o = clv.find(needle)
    while o >= 0 and not _is_entry(clv, o):
        o = clv.find(needle, o + 1)
    if o < 0:
        return {}
    start = o
    while _is_entry(clv, start - ENTRY):
        start -= ENTRY
    end = o
    while _is_entry(clv, end + ENTRY):
        end += ENTRY
    n = (end - start) // ENTRY + 1
    raw = np.frombuffer(clv, np.uint8, count=n * ENTRY, offset=start).reshape(n, ENTRY)
    hashes = raw[:, 0:4].copy().view('>u4')[:, 0]
    mat = raw[:, 4:0x44].copy().view('>f4').reshape(n, 4, 4).astype(np.float32)
    count = raw[:, 0x48]
    halves = raw[:, 0x49:0x51].copy().view('>f2').astype(np.float32)  # end, mult, start, radius
    out = {}
    for k in range(n):
        out[int(hashes[k])] = (mat[k, :3, 3].copy(), float(halves[k, 0]), float(halves[k, 2]),
                               float(halves[k, 3]), int(count[k]))
    return out


class Placement:
    """All instances of one foliage model in a level (game space)."""
    __slots__ = ('model', 'positions', 'scales', 'quats', 'colours', 'fade_end', 'fade_start')

    def __init__(self, model):
        self.model = model
        self.positions, self.scales, self.quats, self.colours = [], [], [], []
        self.fade_end = 0.0
        self.fade_start = 0.0

    def finish(self):
        self.positions = np.concatenate(self.positions).astype(np.float32)
        self.scales = np.concatenate(self.scales).astype(np.float32)
        self.quats = np.concatenate(self.quats).astype(np.float32)
        self.colours = np.concatenate(self.colours)
        return self


def level_foliage(batches, table):
    """Group decoded batches by model. ``batches``: iterable of ``Batch``; ``table``: the result
    of ``batch_table``. Returns ``(model hash -> Placement, unplaced batch count)``."""
    out = {}
    lost = 0
    for b in batches:
        t = table.get(b.name)
        if t is None or b.count == 0:
            lost += 1
            continue
        pl = out.get(b.model)
        if pl is None:
            pl = out[b.model] = Placement(b.model)
        pl.positions.append(b.offsets + t[0][None, :])
        pl.scales.append(b.scales)
        pl.quats.append(b.quats)
        pl.colours.append(b.colours)
        pl.fade_end = max(pl.fade_end, t[1])
        pl.fade_start = max(pl.fade_start, t[2])
    return {m: p.finish() for m, p in out.items()}, lost
