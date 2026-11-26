"""Havok 6.x binary packfiles (PS3: big-endian, 4-byte pointers) - skeletons and animations.

MAG stores every animation and rig as a ``hkRootLevelContainer`` packfile inside a ZRB
(segment 1). Object layouts below are the Havok 6.5 ones with 4-byte pointers; the skeleton and
binding use "simple arrays" (pointer + count) while the spline animation uses full hkArrays.
"""
import math
import struct

import numpy as np

from .errors import MagFormatError

MAGIC = (0x57E0E057, 0x10C0C010)


class Packfile:
    def __init__(self, data):
        d = bytes(data)
        self.d = d
        if len(d) < 0x40:
            raise MagFormatError('truncated Havok packfile')
        m0, m1 = struct.unpack_from('>II', d, 0)
        if (m0, m1) != MAGIC:
            raise MagFormatError('not a Havok packfile')
        self.ptr_size = d[16]
        self.e = '>' if d[17] == 0 else '<'
        e = self.e
        nsec = struct.unpack_from(e + 'i', d, 20)[0]
        self.version = d[40:56].split(b'\0')[0].decode('latin1')
        self.ptrs = {}
        self.objects = {}
        secs = []
        for i in range(nsec):
            o = 0x40 + 0x30 * i
            secs.append(struct.unpack_from(e + '7I', d, o + 20))
        n = len(d)
        for start, local, glob, virt, exp, imp, end in secs:
            for o in range(start + local, min(start + glob, n) - 7, 8):
                src, dst = struct.unpack_from(e + 'ii', d, o)
                if src >= 0:
                    self.ptrs[start + src] = start + dst
            for o in range(start + glob, min(start + virt, n) - 11, 12):
                src, dsec, dst = struct.unpack_from(e + 'iii', d, o)
                if src >= 0 and 0 <= dsec < nsec:
                    self.ptrs[start + src] = secs[dsec][0] + dst
            for o in range(start + virt, min(start + exp, n) - 11, 12):
                src, csec, coff = struct.unpack_from(e + 'iii', d, o)
                if src >= 0 and 0 <= csec < nsec:
                    ca = secs[csec][0] + coff
                    z = d.find(b'\0', ca)
                    self.objects[start + src] = d[ca:z].decode('latin1')

    def u32(self, o):
        return struct.unpack_from(self.e + 'I', self.d, o)[0]

    def i32(self, o):
        return struct.unpack_from(self.e + 'i', self.d, o)[0]

    def f32(self, o):
        return struct.unpack_from(self.e + 'f', self.d, o)[0]

    def cstr(self, o):
        p = self.ptrs.get(o)
        if p is None:
            return ''
        z = self.d.find(b'\0', p)
        return self.d[p:z].decode('latin1')

    def find(self, cls):
        return [o for o, c in sorted(self.objects.items()) if c == cls]


# -- skeleton ------------------------------------------------------------------------------------
class Skeleton:
    def __init__(self):
        self.name = ''
        self.bones = []        # names
        self.parents = []      # ints (-1 root)
        self.translations = None  # (n,3) local
        self.rotations = None     # (n,4) xyzw local
        self.scales = None        # (n,3)


def read_skeleton(pf, o):
    s = Skeleton()
    s.name = pf.cstr(o)
    pp, pn = pf.ptrs.get(o + 4), pf.i32(o + 8)
    bp, bn = pf.ptrs.get(o + 12), pf.i32(o + 16)
    rp, rn = pf.ptrs.get(o + 20), pf.i32(o + 24)
    if pp is None or bp is None or not (0 < bn < 2048):
        raise MagFormatError('bad hkaSkeleton')
    s.parents = list(struct.unpack_from(pf.e + '%dh' % pn, pf.d, pp))
    for i in range(bn):
        slot = bp + 4 * i
        if slot in pf.ptrs:            # array of hkaBone*
            s.bones.append(pf.cstr(pf.ptrs[slot]))
        else:                          # inline hkaBone (char*, bool)
            s.bones.append(pf.cstr(bp + 8 * i))
    if rp is not None and rn == bn:
        a = np.frombuffer(pf.d, dtype=pf.e + 'f4', count=12 * rn, offset=rp).reshape(rn, 3, 4)
        s.translations = a[:, 0, :3].astype(np.float32)
        s.rotations = a[:, 1, :].astype(np.float32)
        s.scales = a[:, 2, :3].astype(np.float32)
    else:
        s.translations = np.zeros((bn, 3), np.float32)
        s.rotations = np.tile(np.array([0, 0, 0, 1], np.float32), (bn, 1))
        s.scales = np.ones((bn, 3), np.float32)
    return s


# -- animation -----------------------------------------------------------------------------------
class Animation:
    def __init__(self):
        self.name = ''
        self.skeleton_name = ''
        self.duration = 0.0
        self.frame_duration = 1.0 / 30.0
        self.num_frames = 0
        self.track_to_bone = []
        self.translations = None  # (tracks, frames, 3)
        self.rotations = None     # (tracks, frames, 4) xyzw
        self.scales = None        # (tracks, frames, 3)
        self.additive = False     # binding blend hint ADDITIVE: tracks are deltas on a base pose


def _align(o, a):
    return (o + a - 1) & ~(a - 1)


_ROT_ALIGN = {0: 4, 1: 1, 2: 2, 3: 1, 4: 2, 5: 4}
_ROT_SIZE = {0: 4, 1: 5, 2: 6, 3: 3, 4: 8, 5: 16}


def _place(vals, shift, neg):
    x, y, z = vals
    w = np.sqrt(np.clip(1.0 - (x * x + y * y + z * z), 0.0, None))
    w = np.where(neg, -w, w)
    comps = [x, y, z]
    out = np.empty((len(x), 4), np.float64)
    for lane in range(4):
        # lane == shift receives w; other lanes take the remaining components in order
        k = lane - (lane > shift).astype(np.int64)
        src = np.stack(comps, 1)[np.arange(len(x)), np.clip(k, 0, 2)]
        out[:, lane] = np.where(lane == shift, w, src)
    return out


def _quats(raw, qtype, e):
    """Decode a (n, size) uint8 array of quantized quaternions -> (n,4) xyzw."""
    n = len(raw)
    if qtype == 2:  # THREECOMP48
        v = raw.copy().view(e + 'u2').reshape(n, 3).astype(np.int64)
        x, y, z = v[:, 0], v[:, 1], v[:, 2]
        shift = ((y >> 14) & 2) | ((x >> 15) & 1)
        neg = (z >> 15) != 0
        m = (1 << 15) - 1
        f = 0.000043161
        vals = [((x & m) - (m >> 1)) * f, ((y & m) - (m >> 1)) * f, ((z & m) - (m >> 1)) * f]
        return _place(vals, shift, neg)
    if qtype in (1, 3):  # THREECOMP40 / THREECOMP24 (packed little-endian bit fields)
        nb = 5 if qtype == 1 else 3
        v = np.zeros(n, np.int64)
        for k in range(nb):
            v |= raw[:, k].astype(np.int64) << (8 * k)
        bits, f = (12, 0.000345436) if qtype == 1 else (7, 0.0055248)
        m = (1 << bits) - 1
        vals = [((v & m) - (m >> 1)) * f, (((v >> bits) & m) - (m >> 1)) * f, (((v >> 2 * bits) & m) - (m >> 1)) * f]
        shift = (v >> 3 * bits) & 3
        neg = ((v >> (3 * bits + 2)) & 1) != 0
        return _place(vals, shift, neg)
    if qtype == 0:  # POLAR32
        c = raw.copy().view(e + 'u4').reshape(n).astype(np.int64)
        rmask = (1 << 10) - 1
        R = ((c >> 18) & rmask) / rmask
        R = 1.0 - R * R
        pt = (c & 0x3FFFF).astype(np.float64)
        phi = np.floor(np.sqrt(pt))
        safe = np.where(phi > 0, phi, 1.0)
        theta = np.where(phi > 0, (math.pi / 4) * (pt - phi * phi) / safe, 0.0)
        phi = np.where(phi > 0, (math.pi / 2 / 511.0) * phi, 0.0)
        mag = np.sqrt(np.clip(1.0 - R * R, 0.0, None))
        out = np.stack([np.sin(phi) * np.cos(theta) * mag, np.sin(phi) * np.sin(theta) * mag, np.cos(phi) * mag, R], 1)
        for lane, bit in enumerate((0x10000000, 0x20000000, 0x40000000, 0x80000000)):
            out[:, lane] = np.where(c & bit, -out[:, lane], out[:, lane])
        return out
    if qtype == 4:  # STRAIGHT16
        v = raw.copy().view(e + 'u2').reshape(n, 4).astype(np.float64)
        return (v - 32767.0) / 32767.0
    v = raw.copy().view(e + 'f4').reshape(n, 4)  # UNCOMPRESSED
    return v.astype(np.float64)


class _Cursor:
    __slots__ = ('d', 'o', 'e')

    def __init__(self, d, o, e):
        self.d, self.o, self.e = d, o, e

    def u8(self):
        v = self.d[self.o]
        self.o += 1
        return v

    def u16(self):
        v = struct.unpack_from(self.e + 'H', self.d, self.o)[0]
        self.o += 2
        return v

    def f32(self):
        v = struct.unpack_from(self.e + 'f', self.d, self.o)[0]
        self.o += 4
        return v

    def align(self, a):
        self.o = _align(self.o, a)


def _read_vec(c, flags, qtype, default):
    spline = [(flags >> (4 + i)) & 1 for i in range(3)]
    static = [(flags >> i) & 1 for i in range(3)]
    base = np.array(default, np.float64)
    if any(spline):
        n = c.u16()
        p = c.u8()
        knots = np.frombuffer(c.d, np.uint8, count=n + p + 2, offset=c.o).astype(np.float64)
        c.o += n + p + 2
        c.align(4)
        mins = np.zeros(3)
        maxs = np.zeros(3)
        for i in range(3):
            if spline[i]:
                mins[i] = c.f32()
                maxs[i] = c.f32()
            elif static[i]:
                base[i] = c.f32()
        axes = [i for i in range(3) if spline[i]]
        size = 1 if qtype == 0 else 2
        cnt = (n + 1) * len(axes)
        if size == 1:
            q = np.frombuffer(c.d, np.uint8, count=cnt, offset=c.o).astype(np.float64) / 255.0
        else:
            q = np.frombuffer(c.d, c.e + 'u2', count=cnt, offset=c.o).astype(np.float64) / 65535.0
        c.o += cnt * size
        q = q.reshape(n + 1, len(axes))
        ctrl = np.tile(base, (n + 1, 1))
        for k, i in enumerate(axes):
            ctrl[:, i] = mins[i] + (maxs[i] - mins[i]) * q[:, k]
        c.align(4)
        return ('s', n, p, knots, ctrl)
    for i in range(3):
        if static[i]:
            base[i] = c.f32()
    c.align(4)
    return ('c', base)


def _read_rot(c, flags, qtype):
    size = _ROT_SIZE.get(qtype)
    if size is None:
        raise MagFormatError('unknown rotation quantization %d' % qtype)
    al = _ROT_ALIGN[qtype]
    if flags & 0xF0:
        n = c.u16()
        p = c.u8()
        knots = np.frombuffer(c.d, np.uint8, count=n + p + 2, offset=c.o).astype(np.float64)
        c.o += n + p + 2
        c.align(al)
        raw = np.frombuffer(c.d, np.uint8, count=(n + 1) * size, offset=c.o).reshape(n + 1, size)
        c.o += (n + 1) * size
        c.align(4)
        return ('s', n, p, knots, _quats(raw, qtype, c.e))
    if flags & 0x0F:
        c.align(al)
        raw = np.frombuffer(c.d, np.uint8, count=size, offset=c.o).reshape(1, size)
        c.o += size
        c.align(4)
        return ('c', _quats(raw, qtype, c.e)[0])
    c.align(4)
    return ('c', np.array([0.0, 0.0, 0.0, 1.0]))


def _eval(track, u):
    """Evaluate a decoded track at local frame times ``u`` (float array) -> (len(u), dim)."""
    if track[0] == 'c':
        return np.tile(track[1], (len(u), 1))
    _, n, p, U, P = track
    # span: largest k in [p, n] with U[k] <= u
    span = np.clip(np.searchsorted(U, u, side='right') - 1, p, n)
    N = np.zeros((len(u), p + 1))
    N[:, 0] = 1.0
    left = np.zeros((len(u), p + 1))
    right = np.zeros((len(u), p + 1))
    for j in range(1, p + 1):
        left[:, j] = u - U[span + 1 - j]
        right[:, j] = U[span + j] - u
        saved = np.zeros(len(u))
        for r in range(j):
            den = right[:, r + 1] + left[:, j - r]
            tmp = np.divide(N[:, r], den, out=np.zeros(len(u)), where=den != 0)
            N[:, r] = saved + right[:, r + 1] * tmp
            saved = left[:, j - r] * tmp
        N[:, j] = saved
    idx = span[:, None] - p + np.arange(p + 1)[None, :]
    return np.einsum('fk,fkd->fd', N, P[idx])


def decode_spline_animation(pf, o):
    ntt = pf.i32(o + 0x10)
    nft = pf.i32(o + 0x14)
    nframes = pf.i32(o + 0x24)
    nblocks = pf.i32(o + 0x28)
    mfpb = pf.i32(o + 0x2C)
    fdur = pf.f32(o + 0x3C)
    bo = pf.ptrs.get(o + 0x40)
    data_at = pf.ptrs.get(o + 0x70)
    endian = pf.i32(o + 0x7C)
    if bo is None or data_at is None or not (0 < ntt < 1024) or nframes <= 0:
        raise MagFormatError('bad hkaSplineCompressedAnimation')
    e = '<' if endian == 0 else '>'
    offs = [pf.u32(bo + 4 * i) for i in range(nblocks)]
    T = np.zeros((ntt, nframes, 3))
    R = np.zeros((ntt, nframes, 4))
    R[..., 3] = 1.0
    S = np.ones((ntt, nframes, 3))
    for b in range(nblocks):
        c = _Cursor(pf.d, data_at + offs[b], e)
        masks = np.frombuffer(pf.d, np.uint8, count=4 * ntt, offset=c.o).reshape(ntt, 4)
        c.o += 4 * ntt + nft
        c.align(4)
        f0 = b * (mfpb - 1)
        f1 = min(nframes, f0 + mfpb)
        u = np.arange(f1 - f0, dtype=np.float64)
        for ti in range(ntt):
            qt, pt, rt, st = (int(x) for x in masks[ti])
            tp = _read_vec(c, pt, qt & 3, (0.0, 0.0, 0.0))
            tr = _read_rot(c, rt, (qt >> 2) & 0xF)
            ts = _read_vec(c, st, (qt >> 6) & 3, (1.0, 1.0, 1.0))
            T[ti, f0:f1] = _eval(tp, u)
            R[ti, f0:f1] = _eval(tr, u)
            S[ti, f0:f1] = _eval(ts, u)
    R /= np.maximum(np.linalg.norm(R, axis=2, keepdims=True), 1e-12)
    return nframes, fdur, T.astype(np.float32), R.astype(np.float32), S.astype(np.float32)


def decode_interleaved_animation(pf, o):
    ntt = pf.i32(o + 0x10)
    dur = pf.f32(o + 0x0C)
    tp = pf.ptrs.get(o + 0x24)  # hkaAnimation base is 0x24 bytes; then a simple array
    tn = pf.i32(o + 0x28)
    if tp is None or ntt <= 0 or tn % ntt:
        raise MagFormatError('bad hkaInterleavedUncompressedAnimation')
    nframes = tn // ntt
    a = np.frombuffer(pf.d, dtype=pf.e + 'f4', count=12 * tn, offset=tp).reshape(nframes, ntt, 3, 4)
    a = a.transpose(1, 0, 2, 3)
    fd = dur / max(1, nframes - 1)
    return nframes, fd, a[:, :, 0, :3].astype(np.float32), a[:, :, 1, :].astype(np.float32), a[:, :, 2, :3].astype(np.float32)


def scene_asset(pf):
    for o in pf.find('hkxScene'):
        a = pf.cstr(o + 4)
        if a:
            return a
    return ''


def summarize(pf):
    """Cheap description for the index: skeleton names, binding skeleton names, frame counts."""
    out = {'skeletons': [], 'animations': [], 'asset': scene_asset(pf)}
    for o in pf.find('hkaSkeleton'):
        out['skeletons'].append(pf.cstr(o))
    for o in pf.find('hkaAnimationBinding'):
        a = pf.ptrs.get(o + 4)
        nf = 0
        dur = 0.0
        if a is not None and pf.objects.get(a) == 'hkaSplineCompressedAnimation':
            nf = pf.i32(a + 0x24)
            dur = pf.f32(a + 0x0C)
        elif a is not None:
            dur = pf.f32(a + 0x0C)
        out['animations'].append((pf.cstr(o), nf, dur))
    return out


BLEND_HINT_ADDITIVE = 1  # hkaAnimationBinding +0x18 (i8 blendHint; 0 = normal)


def read_animations(pf):
    """Decode every bound animation in the packfile."""
    anims = []
    asset = scene_asset(pf)
    for o in pf.find('hkaAnimationBinding'):
        a = pf.ptrs.get(o + 4)
        if a is None:
            continue
        cls = pf.objects.get(a)
        if cls == 'hkaSplineCompressedAnimation':
            res = decode_spline_animation(pf, a)
        elif cls == 'hkaInterleavedUncompressedAnimation':
            res = decode_interleaved_animation(pf, a)
        else:
            continue
        an = Animation()
        an.name = asset
        an.skeleton_name = pf.cstr(o)
        an.duration = pf.f32(a + 0x0C)
        an.num_frames, an.frame_duration, an.translations, an.rotations, an.scales = res
        an.additive = pf.d[o + 0x18] == BLEND_HINT_ADDITIVE
        tp, tn = pf.ptrs.get(o + 8), pf.i32(o + 12)
        if tp is not None and tn > 0:
            an.track_to_bone = list(struct.unpack_from(pf.e + '%dh' % tn, pf.d, tp))
        else:
            an.track_to_bone = list(range(len(an.translations)))
        anims.append(an)
    return anims
