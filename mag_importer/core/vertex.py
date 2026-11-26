"""Vectorised vertex attribute codecs (numpy).

Two families exist in MAG:

* RSX vertex arrays (static weapon/vehicle meshes): IEEE half floats, the RSX CMP normal
  (11/11/10 signed, big-endian), u8 colours.
* Edge partition streams: the descriptor codecs documented by the recomp project
  (geometry-streams.md / geometry-normals.md) - rebased halves, packed spherical normals, ...
"""
import numpy as np

# -- lookup tables (built once) ---------------------------------------------------------------
_u = np.arange(65536, dtype=np.uint32)
_sign = np.where(_u & 0x8000, -1.0, 1.0)
_exp = ((_u >> 10) & 31).astype(np.float64)
_man = (_u & 1023).astype(np.float64)
#: "rebased half": like binary16 but exponent 31 is a normal number (no inf/NaN) and there are
#: no subnormals (every nonzero code is 2^(E-15) * (1 + m/1024)).
REBASED_HALF = np.where((_u & 0x7FFF) == 0, 0.0, _sign * np.exp2(_exp - 15) * (1 + _man / 1024)).astype(np.float32)
del _u, _sign, _exp, _man


def be_u16(buf, offset, count, stride):
    """Strided big-endian u16 gather: returns (count,) uint16 from buf[offset + i*stride]."""
    a = np.frombuffer(buf, dtype=np.uint8, count=stride * (count - 1) + 2, offset=offset) if count else np.zeros(0, np.uint8)
    if count == 0:
        return np.zeros(0, np.uint16)
    idx = np.arange(count) * stride
    return (a[idx].astype(np.uint16) << 8) | a[idx + 1]


def gather(buf, offset, count, stride, nbytes):
    """Return a (count, nbytes) uint8 view of a strided attribute."""
    if count == 0:
        return np.zeros((0, nbytes), np.uint8)
    need = stride * (count - 1) + nbytes
    a = np.frombuffer(buf, dtype=np.uint8, count=need, offset=offset)
    if stride == nbytes:
        return a.reshape(count, nbytes)
    return np.lib.stride_tricks.as_strided(a, shape=(count, nbytes), strides=(stride, 1))


def _u16(cols):
    return (cols[:, 0::2].astype(np.uint16) << 8) | cols[:, 1::2]


def ieee_half(cols):
    """(n, 2k) big-endian half bytes -> (n, k) float32 (RSX vertex fetch format)."""
    return np.ascontiguousarray(cols).view('>f2').astype(np.float32)


def rebased_half(cols):
    return REBASED_HALF[_u16(cols)]


def rsx_cmp(cols):
    """RSX CMP normal: big-endian u32, x/y signed 11 bit, z signed 10 bit."""
    w = np.ascontiguousarray(cols).view('>u4')[:, 0].astype(np.int64)
    x = w & 0x7FF
    y = (w >> 11) & 0x7FF
    z = (w >> 22) & 0x3FF
    x = np.where(x >= 1024, x - 2048, x) / 1023.0
    y = np.where(y >= 1024, y - 2048, y) / 1023.0
    z = np.where(z >= 512, z - 1024, z) / 511.0
    return np.stack([x, y, z], 1).astype(np.float32)


def edge_packed(cols):
    """Edge descriptor codec 6 (x/y 11 bit, z 10 bit, (2s+1) scaling)."""
    w = np.ascontiguousarray(cols).view('>u4')[:, 0].astype(np.int64)
    x = w & 0x7FF
    y = (w >> 11) & 0x7FF
    z = (w >> 22) & 0x3FF
    x = (2 * np.where(x >= 1024, x - 2048, x) + 1) / 2047.0
    y = (2 * np.where(y >= 1024, y - 2048, y) + 1) / 2047.0
    z = (2 * np.where(z >= 512, z - 1024, z) + 1) / 1023.0
    return np.stack([x, y, z, np.ones_like(x)], 1).astype(np.float32)


_SQ = np.sqrt(2.0)


def spherical_normal(cols):
    """Edge descriptor codec 9: 3 bytes -> (n, 4) unit vector + handedness."""
    n = (cols[:, 0].astype(np.uint32) << 16) | (cols[:, 1].astype(np.uint32) << 8) | cols[:, 2]
    u = (n >> 14) & 1023
    v = (n >> 4) & 1023
    sel = n & 15
    a = -1.0 / _SQ + u * (_SQ / 1023.0)
    b = -1.0 / _SQ + v * (_SQ / 1023.0)
    r = np.clip(1.0 - (a * a + b * b), 0.0, None)
    c = np.sqrt(r) * np.where(sel & 1, 1.0, -1.0)
    axis = np.minimum(sel >> 2, 2)
    out = np.empty((len(n), 4), np.float32)
    # lane order: axis gets c, the other two get a then b in ascending lane order
    out[:, 0] = np.where(axis == 0, c, a)
    out[:, 1] = np.where(axis == 0, a, np.where(axis == 1, c, b))
    out[:, 2] = np.where(axis == 2, c, b)
    out[:, 3] = np.where(sel & 2, 1.0, -1.0)
    return out


def decode_rsx_decl(buf, base, count, stride, offset, codec, comps):
    """Decode a declaration of a stream the RSX fetches directly (Edge secondary/auxiliary
    streams). Same codec numbers, RSX vertex-fetch semantics."""
    out = np.zeros((count, 4), np.float32)
    o = base + offset
    if codec == 1:
        v = _u16(gather(buf, o, count, stride, 2 * comps)).astype(np.int32)
        out[:, :comps] = np.where(v >= 32768, v - 65536, v) / 32767.0
    elif codec == 2:
        out[:, :comps] = np.ascontiguousarray(gather(buf, o, count, stride, 4 * comps)).view('>f4')
    elif codec == 3:
        out[:, :comps] = ieee_half(gather(buf, o, count, stride, 2 * comps))
    elif codec == 4:
        out[:, :comps] = gather(buf, o, count, stride, comps) / 255.0
    elif codec == 5:
        v = _u16(gather(buf, o, count, stride, 2 * comps)).astype(np.int32)
        out[:, :comps] = np.where(v >= 32768, v - 65536, v)
    elif codec == 6:
        out[:, :3] = rsx_cmp(gather(buf, o, count, stride, 4))
        out[:, 3] = 1.0
    elif codec == 7:
        out[:, :comps] = gather(buf, o, count, stride, comps)
    else:
        return None
    return out


def decode_edge(buf, base, count, stride, offset, codec, comps):
    """Decode one Edge descriptor declaration -> (count, 4) float32 (missing lanes zero)."""
    out = np.zeros((count, 4), np.float32)
    o = base + offset
    if codec == 1:
        v = _u16(gather(buf, o, count, stride, 2 * comps)).astype(np.int32)
        v = np.where(v >= 32768, v - 65536, v)
        out[:, :comps] = (2 * v + 1) / 65535.0
    elif codec == 2:
        out[:, :comps] = np.ascontiguousarray(gather(buf, o, count, stride, 4 * comps)).view('>f4')
    elif codec == 3:
        out[:, :comps] = rebased_half(gather(buf, o, count, stride, 2 * comps))
    elif codec == 4:
        out[:, :comps] = gather(buf, o, count, stride, comps) / 255.0
    elif codec == 5:
        v = _u16(gather(buf, o, count, stride, 2 * comps)).astype(np.int32)
        out[:, :comps] = np.where(v >= 32768, v - 65536, v)
    elif codec == 6:
        out[:] = edge_packed(gather(buf, o, count, stride, 4))
    elif codec == 7:
        out[:, :comps] = gather(buf, o, count, stride, comps)
    elif codec == 9:
        out[:] = spherical_normal(gather(buf, o, count, stride, 3))
    else:
        return None
    return out
