"""RSX (GCM) texture records -> DDS files, plus numpy decoders.

Texture record payload (0x28 bytes, big-endian)::

    +0x04 u8 dimension (1 = 2D)     +0x05 u8 mip count
    +0x06 u32 data size             +0x0A u32 width     +0x0E u32 height
    +0x12 u8 GCM format (low bits): 5 A8R8G8B8, 6 DXT1, 7 DXT3, 8 DXT5
    +0x14 u16 GCM remap             +0x16 u32 pitch (0 = swizzled / block linear)

The pixel data lives in segment 3 at the record's segment-3 offset, mip levels consecutive.
DXT data is written to DDS unchanged so Blender (GPU) decodes it natively.
"""
import struct

import numpy as np

from .errors import MagFormatError

FMT_ARGB8 = 5
FMT_DXT1 = 6
FMT_DXT3 = 7
FMT_DXT5 = 8
FOURCC = {FMT_DXT1: b'DXT1', FMT_DXT3: b'DXT3', FMT_DXT5: b'DXT5'}
BLOCK = {FMT_DXT1: 8, FMT_DXT3: 16, FMT_DXT5: 16}


class TextureInfo:
    __slots__ = ('name', 'fmt', 'width', 'height', 'mips', 'size', 'pitch', 'remap', 'data_at')

    @property
    def label(self):
        return {5: 'ARGB8', 6: 'DXT1', 7: 'DXT3', 8: 'DXT5'}.get(self.fmt, 'fmt%d' % self.fmt)


def parse_texture(payload, name=0):
    p = payload
    if len(p) < 0x1a:
        raise MagFormatError('texture record too short')
    t = TextureInfo()
    t.name = name
    t.mips = max(1, p[5])
    t.size, t.width, t.height = struct.unpack_from('>III', p, 6)
    t.fmt = p[0x12] & 0x1F
    t.remap = struct.unpack_from('>H', p, 0x14)[0]
    t.pitch = struct.unpack_from('>I', p, 0x16)[0]
    t.data_at = 0
    if not (0 < t.width <= 8192 and 0 < t.height <= 8192):
        raise MagFormatError('bad texture size')
    return t


def _level_dims(t, level):
    return max(1, t.width >> level), max(1, t.height >> level)


def _dxt_level_size(fmt, w, h):
    return max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * BLOCK[fmt]


# -- swizzle (Morton order) for power-of-two ARGB textures ------------------------------------
def _morton_index(w, h):
    """Linear index into swizzled data for each (y, x) of a w x h power-of-two texture."""
    def spread(v, bits):
        out = np.zeros_like(v)
        for b in range(bits):
            out |= ((v >> b) & 1) << (2 * b)
        return out
    x = np.arange(w, dtype=np.int64)
    y = np.arange(h, dtype=np.int64)
    bx = (w - 1).bit_length()
    by = (h - 1).bit_length()
    m = min(bx, by)
    # interleave the low m bits, then append the remaining bits of the larger axis
    lx = spread(x & ((1 << m) - 1), m)
    ly = spread(y & ((1 << m) - 1), m) << 1
    hx = (x >> m) << (2 * m)
    hy = (y >> m) << (2 * m)
    return ly[:, None] + hy[:, None] + lx[None, :] + hx[None, :]


def _is_pow2(v):
    return v & (v - 1) == 0


def argb_to_rgba(t, raw):
    """A8R8G8B8 level 0 -> (h, w, 4) uint8 RGBA."""
    w, h = t.width, t.height
    if t.pitch:
        rows = np.frombuffer(raw, np.uint8, count=t.pitch * h).reshape(h, t.pitch)
        px = rows[:, :w * 4].reshape(h, w, 4)
    else:
        px = np.frombuffer(raw, np.uint8, count=w * h * 4).reshape(-1, 4)
        if _is_pow2(w) and _is_pow2(h):
            px = px[_morton_index(w, h)]
        else:
            px = px.reshape(h, w, 4)
    # memory bytes b0..b3 are A R G B; remap 0xE4 is identity, 0x1B selects them reversed
    if (t.remap & 0xFF) == 0x1B:
        return px[..., [2, 1, 0, 3]]
    return px[..., [1, 2, 3, 0]]


def dds_bytes(t, data):
    """Build a DDS file for the texture. ``data``: the full pixel data (t.size bytes)."""
    w, h = t.width, t.height
    if t.fmt in FOURCC:
        levels = []
        off = 0
        rowlinear = t.pitch and t.mips == 1
        for lv in range(t.mips):
            lw, lh = _level_dims(t, lv)
            n = _dxt_level_size(t.fmt, lw, lh)
            if rowlinear:
                bw = max(1, (lw + 3) // 4) * BLOCK[t.fmt]
                bh = max(1, (lh + 3) // 4)
                if t.pitch != bw:
                    rows = np.frombuffer(data, np.uint8, count=t.pitch * bh).reshape(bh, t.pitch)[:, :bw]
                    levels.append(rows.tobytes())
                    break
            if off + n > len(data):
                break
            levels.append(data[off:off + n])
            off += n
        mips = len(levels)
        if mips == 0:
            raise MagFormatError('texture data truncated')
        flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000 | (0x20000 if mips > 1 else 0)
        pf = struct.pack('<II4sIIIII', 32, 0x4, FOURCC[t.fmt], 0, 0, 0, 0, 0)
        linsize = len(levels[0])
        caps = 0x1000 | (0x400008 if mips > 1 else 0)
        hdr = struct.pack('<4sIIIIIII', b'DDS ', 124, flags, h, w, linsize, 0, mips) + b'\0' * 44 + pf + \
            struct.pack('<IIIII', caps, 0, 0, 0, 0)
        return hdr + b''.join(levels)
    if t.fmt == FMT_ARGB8:
        rgba = argb_to_rgba(t, data)
        bgra = np.ascontiguousarray(rgba[..., [2, 1, 0, 3]])
        flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x8
        pf = struct.pack('<IIIIIIII', 32, 0x41, 0, 32, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
        hdr = struct.pack('<4sIIIIIII', b'DDS ', 124, flags, h, w, w * 4, 0, 1) + b'\0' * 44 + pf + \
            struct.pack('<IIIII', 0x1000, 0, 0, 0, 0)
        return hdr + bgra.tobytes()
    raise MagFormatError('unsupported texture format %d' % t.fmt)


# -- numpy DXT decode (previews / alpha analysis) -------------------------------------------------
def _rgb565(c):
    r = ((c >> 11) & 31) * 255 // 31
    g = ((c >> 5) & 63) * 255 // 63
    b = (c & 31) * 255 // 31
    return np.stack([r, g, b], -1).astype(np.int32)


def decode_dxt(fmt, data, w, h):
    """Decode DXT1/3/5 level 0 -> (h, w, 4) uint8."""
    bw, bh = max(1, (w + 3) // 4), max(1, (h + 3) // 4)
    bs = BLOCK[fmt]
    blk = np.frombuffer(data, np.uint8, count=bw * bh * bs).reshape(bh * bw, bs)
    cb = blk[:, bs - 8:]
    c0 = cb[:, 0].astype(np.int32) | (cb[:, 1].astype(np.int32) << 8)
    c1 = cb[:, 2].astype(np.int32) | (cb[:, 3].astype(np.int32) << 8)
    bits = (cb[:, 4].astype(np.uint32) | (cb[:, 5].astype(np.uint32) << 8) |
            (cb[:, 6].astype(np.uint32) << 16) | (cb[:, 7].astype(np.uint32) << 24))
    p0, p1 = _rgb565(c0), _rgb565(c1)
    four = (c0 > c1) | (fmt != FMT_DXT1)
    p2 = np.where(four[:, None], (2 * p0 + p1) // 3, (p0 + p1) // 2)
    p3 = np.where(four[:, None], (p0 + 2 * p1) // 3, 0)
    pal = np.stack([p0, p1, p2, p3], 1)  # (n,4,3)
    sel = (bits[:, None] >> (2 * np.arange(16, dtype=np.uint32))) & 3  # (n,16)
    rgb = np.take_along_axis(pal, sel[..., None].astype(np.int64).repeat(3, -1), 1)
    a = np.full((len(blk), 16), 255, np.int32)
    if fmt == FMT_DXT1:
        a = np.where((~four[:, None]) & (sel == 3), 0, 255)
    elif fmt == FMT_DXT3:
        ab = blk[:, :8]
        nib = np.stack([ab & 15, ab >> 4], -1).reshape(len(blk), 16)
        a = nib.astype(np.int32) * 17
    else:
        a0 = blk[:, 0].astype(np.int32)
        a1 = blk[:, 1].astype(np.int32)
        ab = np.zeros(len(blk), np.uint64)
        for k in range(6):
            ab |= blk[:, 2 + k].astype(np.uint64) << np.uint64(8 * k)
        asel = ((ab[:, None] >> (np.uint64(3) * np.arange(16, dtype=np.uint64))) & np.uint64(7)).astype(np.int64)
        lv = [a0, a1]
        for k in range(1, 7):
            lv.append(np.where(a0 > a1, ((7 - k) * a0 + k * a1) // 7, 0))
        alt = [a0, a1] + [((5 - k) * a0 + k * a1) // 5 for k in range(1, 5)] + [np.zeros_like(a0), np.full_like(a0, 255)]
        tab8 = np.stack([((7 - k) * a0 + k * a1) // 7 if k not in (0, 1) else (a0 if k == 0 else a1) for k in range(8)], 1)
        tab6 = np.stack(alt, 1)
        tab = np.where((a0 > a1)[:, None], tab8, tab6)
        # palette index order: 0->a0, 1->a1, 2..7 interpolated
        a = np.take_along_axis(tab, asel, 1)
    px = np.concatenate([rgb, a[..., None]], -1).astype(np.uint8)  # (n,16,4)
    px = px.reshape(bh, bw, 4, 4, 4).transpose(0, 2, 1, 3, 4).reshape(bh * 4, bw * 4, 4)
    return px[:h, :w]


def decode_rgba(t, data):
    if t.fmt in FOURCC:
        return decode_dxt(t.fmt, data, t.width, t.height)
    if t.fmt == FMT_ARGB8:
        return argb_to_rgba(t, data)
    raise MagFormatError('unsupported texture format %d' % t.fmt)
