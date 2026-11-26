"""Baked per-vertex lighting (``84DFB280`` records) of map placements.

A level bakes lighting for the placements of a few models (mostly trees and bushes). Each such
placement has its own record, named by the u32 at ``LIGHT_REF`` (+166) in the compiled block
after its ``InstanceProperties`` object in the ``.clv``. The records live in the level's
``globaldata.zrb`` and ``loadblock_*.zrb`` files:

* payload (24 bytes): u32 0, u32 **N** (entry count), 16 bytes of zeros;
* segment 3: N × 4 bytes ``(0, sky, 0, sun)``, one entry per vertex of the model's
  **vertex-lit meshes** (meshes whose material has ``materials.VERTEX_LIT`` set), in mesh order,
  every LOD included. ``sun`` is the sun's visibility (0 in shadow, including shadows cast by
  other pieces); ``sky`` is a soft ambient/sky term. Bytes 0 and 2 are almost always 0.

For a few models (about 6 % of lit placements) N is larger than the vertex-lit vertex count (for
example each lit mesh stored twice); their layout is not known and they are skipped.
"""
import struct

import numpy as np

from .materials import VERTEX_LIT

LIGHT_REF = 166  # offset of the lighting record name after an InstanceProperties object


def placement_lighting(clv, names):
    """Lighting record name of every model placement in the ``.clv`` (placement order, the same
    as ``level.read_instances``), 0 where it has none. ``names``: the level's record names."""
    from . import props
    out = []
    for o in props.iter_objects(clv, type_name='InstanceProperties'):
        if not o.token('Mesh'):
            continue
        h = 0
        if o.end + LIGHT_REF + 4 <= len(clv):
            cand = struct.unpack_from('>I', clv, o.end + LIGHT_REF)[0]
            if cand in names:
                h = cand
        out.append(h)
    return out


def read_lighting(zrb, rec):
    """(N, 2) uint8 array of (sky, sun) per entry."""
    n = struct.unpack_from('>I', zrb.payload(rec), 4)[0]
    raw = np.frombuffer(zrb.data, np.uint8, n * 4, zrb.seg_start[3] + rec.seg3).reshape(n, 4)
    return raw[:, [1, 3]].copy()


def lit_offsets(model, material_flags):
    """Start of each vertex-lit mesh's entries: {mesh index: offset}, and the entry total.
    ``material_flags(hash)`` -> the material's flag word (``MaterialInfo.flags``) or None."""
    offs, n = {}, 0
    for i, me in enumerate(model.meshes):
        f = material_flags(me.material)
        if f is not None and f & VERTEX_LIT:
            offs[i] = n
            n += me.V
    return offs, n
