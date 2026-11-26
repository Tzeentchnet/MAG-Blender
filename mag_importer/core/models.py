"""Model records: static hierarchical models, Edge skinned models and level pieces.

Serialized layout of a model payload (record payload, big-endian, byte packed)::

    +0x00 8 zero bytes
    +0x08 f32[4] bounding sphere (centre, radius)
    +0x18 f32[6] bounding box: centre xyz, half extents xyz
    +0x30 u8, u8 lod count, u8 0xFF
    +0x33 u32 mesh count
    +0x37 u32 attachment count
    +0x3B attachments, 76 bytes each: u32 name hash, f32[12] 3x4 matrix, 24 bytes
          (some level pieces have variable-size attachment records; the parser then searches
          for the first mesh entry)
    then mesh entries, 149 bytes each (docs/formats/models.md#mesh-entry-149-bytes):
        +0   u32 0
        +4   f32 max LOD distance
        +8   u16 flags, u16, u32
        +16  u8 0, u8 attribute mask (0x1D)
        +18  u32
        +22  u8 Edge partition count (0 = plain RSX vertex/index buffers)
        +23  u32 RSX bytes (vertex buffer, or Edge secondary streams)
        +27  u32 Edge partition bytes
        +31  unknown, including bounds (about 52 bytes)
        +91  u32 draw flags (low byte = LOD mask, 0x04000000 = skinned), u16
        +97  u32 V, u32 K, u32 3 (triangle list), u32 vertex format
        +113 32 bytes unknown
        +145 u32 material name hash
    then (static models) the node table.
"""
import struct

import numpy as np

from . import vertex as vx
from .errors import MagFormatError
from .zrb import T_MODEL, T_SKINNED_MODEL, T_LEVEL_MODEL, T_FOLIAGE_MODEL

MESH_ENTRY = 149
FIRST_MESH = 0x3B


class MeshEntry:
    __slots__ = ('index', 'lod_distance', 'flags', 'parts', 'rsx_size', 'edge_size', 'draw_flags',
                 'V', 'K', 'vformat', 'material')

    @property
    def lod_mask(self):
        return self.draw_flags & 0xFF

    @property
    def skinned(self):
        return bool(self.draw_flags & 0x04000000)


class Node:
    __slots__ = ('index', 'name', 'mesh_start', 'mesh_count', 'parent', 'rotation', 'translation')


class Model:
    def __init__(self):
        self.type = 0
        self.name = 0
        self.sphere = (0.0, 0.0, 0.0, 0.0)
        self.bounds = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)  # box centre xyz, half extents xyz
        self.meshes = []
        self.nodes = []
        self.attachments = []


def parse_model(payload, rtype=T_MODEL, name=0):
    p = payload
    if len(p) < FIRST_MESH + 4:
        raise MagFormatError('model record too short')
    m = Model()
    m.type = rtype
    m.name = name
    m.sphere = struct.unpack_from('>4f', p, 8)
    m.bounds = struct.unpack_from('>6f', p, 0x18)
    count, n_attach = struct.unpack_from('>II', p, 0x33)
    if count > 4096 or n_attach > 256:
        raise MagFormatError('implausible mesh/attachment count')
    # attachment points: name hash + 3x4 matrix + 24 bytes
    m.attachments = []
    for k in range(n_attach):
        a = FIRST_MESH + 76 * k
        h = struct.unpack_from('>I', p, a)[0]
        mtx = struct.unpack_from('>12f', p, a + 4)
        m.attachments.append((h, mtx))
    e = FIRST_MESH + 76 * n_attach
    if count and not (e + 91 + 58 <= len(p) and struct.unpack_from('>I', p, e + 91 + 14)[0] == 3):
        # variable-size attachment records (some level pieces): find the first mesh entry
        e = next((x for x in range(FIRST_MESH, min(len(p) - 150, 0x4000)) if _entry_ok(p, x)), -1)
        if e < 0:
            raise MagFormatError('mesh list not found')
    for i in range(count):
        c = e + 4
        mk = c + 87
        if mk + 58 > len(p):
            raise MagFormatError('mesh entry %d truncated' % i)
        V, K, three, vf = struct.unpack_from('>IIII', p, mk + 6)
        if three != 3:
            raise MagFormatError('mesh entry %d has unexpected layout' % i)
        me = MeshEntry()
        me.index = i
        me.lod_distance = struct.unpack_from('>f', p, c)[0]
        me.flags = struct.unpack_from('>H', p, c + 4)[0]
        me.parts = p[c + 18]
        me.rsx_size, me.edge_size = struct.unpack_from('>II', p, c + 19)
        me.draw_flags = struct.unpack_from('>I', p, mk)[0]
        me.V, me.K, me.vformat = V, K, vf
        me.material = struct.unpack_from('>I', p, mk + 54)[0]
        m.meshes.append(me)
        e = mk + 58
    if rtype == T_MODEL:
        _parse_nodes(m, p, e)
    if not m.nodes:
        n = Node()
        n.index, n.name, n.mesh_start, n.mesh_count, n.parent = 0, name, 0, len(m.meshes), -1
        n.rotation, n.translation = (0.0, 0.0, 0.0, 1.0), (0.0, 0.0, 0.0)
        m.nodes.append(n)
    return m


def _entry_ok(p, e):
    c = e + 4
    mk = c + 87
    return (mk + 58 <= len(p) and p[c + 13] == 0x1D and p[c + 12] == 0
            and struct.unpack_from('>I', p, mk + 14)[0] == 3 and struct.unpack_from('>I', p, e)[0] == 0)


def _parse_nodes(m, p, e):
    if e + 14 > len(p):
        return
    nn = struct.unpack_from('>H', p, e)[0]
    if nn == 0 or nn > 1024:
        return
    xf = e + 14
    nodes_at = xf + 32 * nn
    if nodes_at + 48 * nn > len(p):
        return
    for i in range(nn):
        q = struct.unpack_from('>4f', p, xf + 32 * i)
        t = struct.unpack_from('>3f', p, xf + 32 * i + 16)
        rec = nodes_at + 48 * i  # bbox[24], name hash, 10 x u16
        h = struct.unpack_from('>I', p, rec + 24)[0]
        f = struct.unpack_from('>10H', p, rec + 28)
        n = Node()
        n.index = i
        n.name = h
        n.mesh_start = f[1] if f[1] != 0xFFFF else 0
        n.mesh_count = f[2] if f[1] != 0xFFFF else 0
        n.parent = f[9] if f[9] != 0xFFFF else -1
        n.rotation = q
        n.translation = t
        m.nodes.append(n)
    # sanity: parents must precede children and mesh ranges must be inside the mesh list
    for n in m.nodes:
        if n.parent >= len(m.nodes) or n.mesh_start + n.mesh_count > len(m.meshes):
            m.nodes = []
            return


# -- geometry ----------------------------------------------------------------------------------
class Geometry:
    """Decoded geometry of one mesh entry."""

    def __init__(self):
        self.positions = None   # (V,3) float32
        self.normals = None     # (V,3) float32 or None
        self.uvs = []           # list of (V,2) float32
        self.colors = None      # (V,4) float32 or None
        self.indices = None     # (T,3) int32
        self.joints = None      # (V,4) int32 (skeleton bone indices) or None
        self.weights = None     # (V,4) float32 or None


RSX_ATTRS = (  # (format bit, name, bytes)
    (0x001, 'pos', 6),
    (0x002, 'nrm', 4),
    (0x004, 'unk4', 4),
    (0x008, 'tan', 8),
    (0x080, 'col', 4),
    (0x200, 'uv0', 4),
    (0x400, 'uv1', 4),
)


def rsx_layout(vformat, stride):
    """Byte offsets of each attribute of an RSX static vertex, or None if unknown."""
    offs = {}
    o = 0
    float_pos = False
    expect = sum(n for bit, _, n in RSX_ATTRS if vformat & bit)
    if vformat & 1 and stride == expect + 6:
        float_pos = True
        expect += 6
    if stride != expect:
        return None
    for bit, nm, n in RSX_ATTRS:
        if vformat & bit:
            if nm == 'pos' and float_pos:
                n = 12
            offs[nm] = (o, n)
            o += n
    return offs


def _tris(idx, V):
    idx = idx.astype(np.int32)
    t = len(idx) // 3
    tri = idx[:t * 3].reshape(t, 3)
    ok = (tri < V).all(1) & (tri[:, 0] != tri[:, 1]) & (tri[:, 1] != tri[:, 2]) & (tri[:, 0] != tri[:, 2])
    return tri[ok]


def decode_rsx(data, vb, ib, mesh):
    V, K = mesh.V, mesh.K
    if V == 0:
        return None
    stride = mesh.rsx_size // V if V else 0
    lay = rsx_layout(mesh.vformat, stride)
    if lay is None or vb + V * stride > len(data) or ib + 2 * K > len(data):
        return None
    g = Geometry()
    off, n = lay['pos']
    cols = vx.gather(data, vb + off, V, stride, n)
    g.positions = np.ascontiguousarray(cols).view('>f4').astype(np.float32) if n == 12 else vx.ieee_half(cols)
    if 'nrm' in lay:
        off, n = lay['nrm']
        g.normals = vx.rsx_cmp(vx.gather(data, vb + off, V, stride, 4))
    if 'col' in lay:
        off, n = lay['col']
        g.colors = vx.gather(data, vb + off, V, stride, 4).astype(np.float32) / 255.0
    for key in ('uv0', 'uv1'):
        if key in lay:
            off, n = lay[key]
            g.uvs.append(vx.ieee_half(vx.gather(data, vb + off, V, stride, 4)))
    idx = np.frombuffer(data, dtype='>u2', count=K, offset=ib)
    g.indices = _tris(idx, V)
    return g


# Edge partitions -------------------------------------------------------------------------------
def _table(data, o):
    n, stride = data[o], data[o + 1]
    decls = []
    for k in range(n):
        b = data[o + 8 + 8 * k:o + 16 + 8 * k]
        decls.append((b[0], b[1], b[2], b[3]))  # offset, codec, components, semantic
    return stride, decls


def partition_size(h):
    return 0x110 + h[19] + sum(h[0:9]) + h[14] + h[16] + h[17] + h[18]


class Partition:
    __slots__ = ('V', 'K', 'positions', 'normals', 'uvs', 'colors', 'indices', 'joints', 'weights', 'size', 'sec_size')


def decode_partition(data, at, sec_data, sec_at):
    h = struct.unpack_from('>24H', data, at)
    cfg = at + 0x100
    pos_codec = data[cfg + 2]
    V, K = struct.unpack_from('>HH', data, cfg + 8)
    pt = Partition()
    pt.V, pt.K = V, K
    pt.size = partition_size(h)
    pt.sec_size = h[9]
    pt.normals = None
    pt.uvs = []
    pt.colors = None
    pt.joints = None
    pt.weights = None
    s = cfg + 0x10 + h[19]           # streams start
    o_idx = s
    o_pos = o_idx + h[0] + h[1]
    o_nrm = o_pos + h[2] + h[3] + h[4]
    o_inf = o_nrm + h[5] + h[6] + h[7] + h[8]
    o_tab = o_inf + h[14]
    if o_tab + h[16] + h[17] > len(data):
        raise MagFormatError('partition exceeds data')
    # indices
    idx = np.frombuffer(data, dtype='>u2', count=K, offset=o_idx)
    pt.indices = _tris(idx, V)
    # positions
    if pos_codec == 0:
        pt.positions = np.frombuffer(data, dtype='>f4', count=3 * V, offset=o_pos).reshape(V, 3).astype(np.float32)
    elif pos_codec == 6:
        pt.positions = vx.rebased_half(vx.gather(data, o_pos, V, 6, 6))
    else:
        if not h[16]:
            raise MagFormatError('descriptor-driven positions without a format table')
        stride, decls = _table(data, o_tab)
        pos = None
        for off, codec, comps, sem in decls:
            if sem == 1:
                pos = vx.decode_edge(data, o_pos, V, stride, off, codec, comps)
                break
        if pos is None:
            raise MagFormatError('no position declaration')
        pt.positions = pos[:, :3].copy()
    # normals / tangents (primary attribute stream)
    if h[5] and h[17]:
        stride, decls = _table(data, o_tab + h[16])
        for off, codec, comps, sem in decls:
            if sem == 2:
                v = vx.decode_edge(data, o_nrm, V, stride, off, codec, comps)
                if v is not None:
                    pt.normals = v[:, :3].copy()
                break
    # skinning influences: 4 x (weight u8, bone u8)
    if h[14] >= 8 * V and V:
        inf = np.frombuffer(data, dtype=np.uint8, count=8 * V, offset=o_inf).reshape(V, 4, 2)
        pt.weights = inf[:, :, 0].astype(np.float32) / 255.0
        # Edge skinning palette: two contiguous ranges of the skeleton's 3x4 (48-byte) matrix
        # array. h10/h11 = byte offsets of segments 0/1, h12/h13 = their byte sizes. Local index
        # i < n0 -> bone o0 + i, else bone o1 + (i - n0). Verified against bind poses.
        local = inf[:, :, 1].astype(np.int32)
        if h[10] != 0xFFFF:
            o0, o1, n0 = h[10] // 48, h[11] // 48, h[12] // 48
            pt.joints = np.where(local < n0, o0 + local, o1 + (local - n0)).astype(np.int32)
        else:
            pt.joints = local
    # secondary (RSX) stream: UVs, colours
    if h[19] and h[9] and sec_data is not None and sec_at + h[9] <= len(sec_data):
        stride, decls = _table(data, cfg + 0x10)
        for off, codec, comps, sem in decls:
            v = vx.decode_rsx_decl(sec_data, sec_at, V, stride, off, codec, comps)
            if v is None:
                continue
            if sem in (5, 6, 7, 8):
                pt.uvs.append(v[:, :2].copy())
            elif sem == 9 and pt.colors is None:
                pt.colors = v.copy()
            elif sem == 2 and pt.normals is None:
                pt.normals = v[:, :3].copy()
    return pt


def decode_edge_mesh(data, seg4_at, seg3_at, mesh):
    parts = []
    cur4, cur3 = seg4_at, seg3_at
    for _ in range(mesh.parts):
        pt = decode_partition(data, cur4, data, cur3)
        parts.append(pt)
        cur4 += pt.size
        cur3 += align(pt.sec_size, 0x10)
    if not parts:
        return None
    g = Geometry()
    g.positions = np.concatenate([p.positions for p in parts])
    base = np.cumsum([0] + [p.V for p in parts[:-1]])
    g.indices = np.concatenate([p.indices + b for p, b in zip(parts, base)]).astype(np.int32)
    if all(p.normals is not None for p in parts):
        g.normals = np.concatenate([p.normals for p in parts])
    nuv = min(len(p.uvs) for p in parts)
    g.uvs = [np.concatenate([p.uvs[k] for p in parts]) for k in range(nuv)]
    if all(p.colors is not None for p in parts):
        g.colors = np.concatenate([p.colors for p in parts])
    if all(p.joints is not None for p in parts):
        g.joints = np.concatenate([p.joints for p in parts])
        g.weights = np.concatenate([p.weights for p in parts])
    return g


def align(v, a):
    return (v + a - 1) & ~(a - 1)


def decode_model_geometry(zrb, rec, model):
    """Decode every mesh of a model. Returns a list aligned with model.meshes (None = failed)."""
    data = zrb.data
    b3 = zrb.seg_start[3]
    b4 = zrb.seg_start[4]
    seg3 = rec.seg3  # offsets relative to their segment (alignment is segment-relative)
    seg4 = rec.seg4
    out = []
    for me in model.meshes:
        g = None
        try:
            if me.parts:
                seg3 = align(seg3, 0x80)
                g = decode_edge_mesh(data, b4 + seg4, b3 + seg3, me)
                seg4 = align(seg4 + me.edge_size, 0x80)
                seg3 = seg3 + me.rsx_size
            elif me.V and me.K:
                # [vertex buffer] pad to 0x80 [u16 index list] pad to 0x80
                vb = align(seg3, 0x80)
                ib = align(vb + me.rsx_size, 0x80)
                g = decode_rsx(data, b3 + vb, b3 + ib, me)
                seg3 = ib + 2 * me.K
        except (MagFormatError, ValueError, struct.error):
            g = None
        out.append(g)
    return out
