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

N counts decoded vertices (an Edge mesh can hold a few more than its header V). For a few models
(about 6 % of lit placements) N is larger than that: the meshes sit at other offsets, with extra
entries between them (stale bakes of an older mesh, for example each lit mesh stored twice).
``fit_layout`` finds those offsets from the data itself; docs/formats/properties-and-levels.md
lists them.
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


FIT_ACCEPT = 0.75   # largest accepted cost of a fitted mesh, relative to its median over offsets
FIT_RECORDS = 64    # records of one model used for a fit


FIT_NEIGHBOURS = 3  # nearest vertices compared with each vertex


def neighbours(positions, k=FIT_NEIGHBOURS):
    """(V, k) indices of each vertex's nearest other vertices (coincident ones count: seams)."""
    p = np.asarray(positions, np.float64)
    k = max(1, min(k, len(p) - 1))
    out = np.empty((len(p), k), np.int64)
    for a in range(0, len(p), 512):
        d = ((p[a:a + 512, None, :] - p[None, :, :]) ** 2).sum(2)
        d[np.arange(len(d)), np.arange(a, a + len(d))] = np.inf
        out[a:a + 512] = np.argsort(d, 1)[:, :k]
    return out


def fit_layout(meshes, records):
    """Offsets of the lit meshes in records whose length does not match them.

    ``meshes``: [(mesh index, positions)] of the vertex-lit meshes in mesh order (decoded
    geometry); ``records``: list of (N, 2) arrays (one model's records, all of length N).
    Lighting is smooth over a mesh, so at the right offset each vertex's value is close to that of
    its nearest vertex (equal across seams). For every mesh and start offset the cost is the mean
    absolute difference to the nearest vertex over the records; offsets are chosen in mesh order
    without overlap to minimise the summed cost relative to each mesh's median cost. Returns
    ({mesh index: offset}, {mesh index: relative cost}) or (None, costs) when the fit is not clear.
    """
    data = np.stack(records[:FIT_RECORDS]).astype(np.float32)
    N = data.shape[1]
    vs = [len(p) for _, p in meshes]
    if not meshes or sum(vs) > N:
        return None, {}
    if sum(vs) == N:  # contiguous (the header vertex count differed from the decoded one)
        offs = np.cumsum([0] + vs[:-1]).tolist()
        return {mi: o for (mi, _), o in zip(meshes, offs)}, {mi: 0.0 for mi, _ in meshes}
    rel = []
    for (mi, pos), V in zip(meshes, vs):
        j = neighbours(pos)
        cost = np.array([np.abs(data[:, s:s + V, None, :] - data[:, s:s + V][:, j]).mean()
                         for s in range(N - V + 1)])
        rel.append(cost / max(float(np.median(cost)), 1e-6))
    # dynamic programme: best[k][s] = rel cost of meshes 0..k with mesh k at s
    INF = np.inf
    best, arg = [], []
    for k, (r, V) in enumerate(zip(rel, vs)):
        full = np.full(N + 1, INF)
        full[:len(r)] = r
        a = np.full(N + 1, -1, np.int64)
        if k:
            prev, pv = best[-1], vs[k - 1]
            # prefix minimum of prev over t <= s - pv
            pm = np.minimum.accumulate(prev)
            pa = np.zeros(len(prev), np.int64)
            cur = 0
            for t in range(len(prev)):
                if prev[t] < prev[cur]:
                    cur = t
                pa[t] = cur
            shifted = np.full(N + 1, INF)
            shifted[pv:] = pm[:N + 1 - pv]
            a[pv:] = pa[:N + 1 - pv]
            full = full + shifted
        best.append(full)
        arg.append(a)
    s = int(np.argmin(best[-1]))
    if not np.isfinite(best[-1][s]):
        return None, {}
    starts = [0] * len(meshes)
    for k in range(len(meshes) - 1, -1, -1):
        starts[k] = s
        s = int(arg[k][s])
    costs = {mi: float(rel[k][starts[k]]) for k, (mi, _) in enumerate(meshes)}
    if max(costs.values()) > FIT_ACCEPT:
        return None, costs
    return {mi: st for (mi, _), st in zip(meshes, starts)}, costs
