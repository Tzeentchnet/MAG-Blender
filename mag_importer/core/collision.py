"""Collision meshes: ``physics.zrb`` records of type ``2A8BDF5A``.

See docs/formats/havok.md#collision-meshes-2a8bdf5a. In short:

* record payload (big-endian u32): ``0, 0, n, f32 0.5, size[n], ...`` - the record's segment 1
  data is ``n`` Havok 6.5 packfiles of those sizes, back to back;
* each packfile holds an ``hkpMoppBvTreeShape`` over one ``hkpStorageExtendedMeshShape``
  whose triangle subparts are stored in ``hkpStorageExtendedMeshShapeMeshSubpartStorage``
  objects: ``hkVector4`` vertices and 16-bit indices, four per triangle (the fourth is padding);
* placed pieces: the compiled block after each ``InstanceProperties`` in the level ``.clv``
  holds the collision record name at +4 (after a u32 that varies); the shape is in the piece's model space
  and is placed by the placement transform. A mesh always maps to the same record (or none);
* the terrain's collision is one record no placement names, in world space; its box covers the
  terrain square (``terrain_record``). The other unnamed records are not placed by the level
  (vehicles, destructibles and the like, placed at run time).
"""
import struct

import numpy as np

from . import havok
from .errors import MagFormatError
from .zrb import T_COLLISION

SUBPART = 0x60  # hkpExtendedMeshShape::TrianglesSubpart, 4-byte pointers
# INDICES_INT8, INDICES_INT16, INDICES_INT32 -> (storage array offset, numpy type)
_INDEX_ARRAYS = {1: (0x14, '>u1'), 2: (0x20, '>u2'), 3: (0x2C, '>u4')}


class CollisionMesh:
    """One collision record merged into a single triangle mesh (model or world space).
    ``part`` gives, per triangle, the packfile it came from (the record may hold several)."""
    __slots__ = ('name', 'positions', 'triangles', 'part')


def _array(pf, o):
    """(pointer target, count) of an hkArray / simple array at ``o``."""
    return pf.ptrs.get(o), pf.u32(o + 4)


def read_shape(pf):
    """[(positions (N,3) f32, triangles (M,3) i32)] for every triangle subpart of a packfile."""
    out = []
    for sh in pf.find('hkpStorageExtendedMeshShape'):
        subs, nsub = _array(pf, sh + 0xB4)
        stores, nstore = _array(pf, sh + 0xE0)
        if nstore != nsub or (nsub and (subs is None or stores is None)):
            raise MagFormatError('collision shape: %d subparts, %d storages' % (nsub, nstore))
        for i in range(nsub):
            sp = subs + SUBPART * i
            ntri, vstride, nvert = pf.u32(sp + 0x14), pf.u32(sp + 0x1C), pf.u32(sp + 0x20)
            istride, itype = pf.u32(sp + 0x44), pf.d[sp + 0x48]
            st = pf.ptrs.get(stores + 4 * i)
            if st is None or vstride != 16 or itype not in _INDEX_ARRAYS:
                raise MagFormatError('collision subpart %d: unsupported layout' % i)
            vp, vn = _array(pf, st + 0x08)
            if vn != nvert:
                raise MagFormatError('collision subpart %d: %d vertices, storage %d' % (i, nvert, vn))
            ao, dt = _INDEX_ARRAYS[itype]
            ip, n_idx = _array(pf, st + ao)
            per = istride // np.dtype(dt).itemsize
            if per < 3 or n_idx != per * ntri:
                raise MagFormatError('collision subpart %d: %d indices for %d triangles' % (i, n_idx, ntri))
            pos = np.frombuffer(pf.d, '>f4', vn * 4, vp).reshape(-1, 4)[:, :3].astype(np.float32)
            tri = np.frombuffer(pf.d, dt, n_idx, ip).reshape(-1, per)[:, :3].astype(np.int32)
            if ntri and tri.max() >= vn:
                raise MagFormatError('collision subpart %d: index out of range' % i)
            out.append((pos, tri))
    return out


def packfiles(zrb, rec):
    """The record's Havok packfiles (segment 1), sized by the payload's table."""
    p = zrb.payload(rec)
    n = struct.unpack_from('>I', p, 8)[0]
    if not 0 < n <= 64 or len(p) < 16 + 4 * n:
        raise MagFormatError('collision record %08x: bad packfile count %d' % (rec.name, n))
    sizes = struct.unpack_from('>%dI' % n, p, 16)
    base = zrb.seg_offset(1, rec.seg1)
    out = []
    for s in sizes:
        out.append(havok.Packfile(zrb.data[base:base + s]))
        base += s
    return out


def read_collision(zrb, rec):
    """Decode a collision record into one merged ``CollisionMesh``."""
    if rec.type != T_COLLISION:
        raise MagFormatError('record %08x is not a collision mesh' % rec.name)
    pos, tri, part = [], [], []
    nv = 0
    for k, pf in enumerate(packfiles(zrb, rec)):
        for p, t in read_shape(pf):
            pos.append(p)
            tri.append(t + nv)
            part.append(np.full(len(t), k, np.int32))
            nv += len(p)
    m = CollisionMesh()
    m.name = rec.name
    m.positions = np.concatenate(pos) if pos else np.zeros((0, 3), np.float32)
    m.triangles = np.concatenate(tri) if tri else np.zeros((0, 3), np.int32)
    m.part = np.concatenate(part) if part else np.zeros(0, np.int32)
    return m


def placement_collisions(clv, names):
    """Collision record name of every model placement in the ``.clv`` (in placement order, the
    same order as ``level.read_instances``), 0 where the placement has none. ``names`` is the
    set of collision record names of the level."""
    from . import props
    out = []
    for o in props.iter_objects(clv, type_name='InstanceProperties'):
        if not o.token('Mesh'):
            continue
        h = 0
        if o.end + 8 <= len(clv):
            cand = struct.unpack_from('>I', clv, o.end + 4)[0]
            if cand in names:
                h = cand
        out.append(h)
    return out


def terrain_record(meshes, used, square, cover=0.9):
    """Name of the world-space terrain collision: the record no placement uses whose box covers
    at least ``cover`` of the terrain square (x0, z0, x1, z1) in both directions; None if none."""
    x0, z0, x1, z1 = square
    best = None
    for h, m in meshes.items():
        if h in used or not len(m.positions):
            continue
        lo, hi = m.positions.min(0), m.positions.max(0)
        ox = min(hi[0], x1) - max(lo[0], x0)
        oz = min(hi[2], z1) - max(lo[2], z0)
        if ox >= cover * (x1 - x0) and oz >= cover * (z1 - z0):
            if best is None or len(m.triangles) > len(meshes[best].triangles):
                best = h
    return best


def weld(mesh):
    """Merge vertices at exactly the same position (Havok stores them per triangle strip) and
    drop triangles that collapse. Returns a new CollisionMesh."""
    out = CollisionMesh()
    out.name = mesh.name
    if not len(mesh.positions):
        out.positions, out.triangles, out.part = mesh.positions, mesh.triangles, mesh.part
        return out
    uniq, inv = np.unique(mesh.positions, axis=0, return_inverse=True)
    tri = inv.reshape(-1)[mesh.triangles]
    keep = (tri[:, 0] != tri[:, 1]) & (tri[:, 1] != tri[:, 2]) & (tri[:, 0] != tri[:, 2])
    out.positions = uniq.astype(np.float32)
    out.triangles = tri[keep].astype(np.int32)
    out.part = mesh.part[keep]
    return out


def physics_zrb_name(clv_name):
    """Archive path of a level's physics.zrb from its .clv path."""
    return clv_name.rsplit('/', 1)[0] + '/resource_blocks/physics.zrb'
