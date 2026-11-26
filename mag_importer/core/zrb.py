"""ZRB resource block container (Zipper Interactive, MAG retail v1.0).

Layout (all big-endian)::

    +0x00 u32 version (6)
    +0x04 u32 ?
    +0x08 u32 seg0 size        +0x0C u32 seg0 end  (= seg1 start)
    +0x10 u32 seg1 size        +0x14 u32 seg2 start
    +0x18 u32 seg2 size        +0x1C u32 seg3 start
    +0x20 u32 seg3 size        +0x24 u32 seg4 start
    +0x28 u32 seg4 size        +0x2C u32 type count
    +0x30 u16 dependency count, dependency records (crc32 path hashes + paths)
          type table: type count x (u32 type hash, u32 resource count)
          resource records

Segment 0 holds the serialized resource records, segment 1 CPU data (Havok packfiles, texture
objects), segment 3 RSX data (textures, vertex/index buffers), segment 4 Edge geometry
partitions. Each record::

    u32 6, u32 0x600DF00D, u32 end (relative to the first record), u32 kind,
    u32 seg1 offset, u32 seg2 offset, u32 seg3 offset, u32 seg4 offset,
    u32 type hash, u32 name hash, payload...
"""
import struct

from .errors import MagFormatError

REC_MAGIC = b'\x00\x00\x00\x06\x60\x0d\xf0\x0d'

# Resource type hashes
T_TEXTURE = 0x85CE0660
T_MATERIAL = 0x7CBE7595
T_MODEL = 0xA4F4D23C          # hierarchical static model (weapons, vehicles, props)
T_SKINNED_MODEL = 0x10B17150  # Edge skinned model (characters, gear)
T_LEVEL_MODEL = 0x80DC0132    # level piece (Edge static partitions)
T_FOLIAGE_MODEL = 0xB9F5747E  # instanced clutter model
T_HEAD_MODEL = 0xB2922512     # character heads: every head variant in one skinned model (mesh flags = head index)
T_FOLIAGE_BATCH = 0x03BE92A0  # instance batch of a foliage model
T_HAVOK = 0xFB93282D          # Havok 6.x packfile (animations, skeletons)
T_LIGHTING = 0x84DFB280        # baked per-vertex lighting of one placement (lighting.py)
T_TERRAIN = 0x284C2F78
T_PARTICLE_EFFECT = 0x98057459  # particle effect (placed as an emitter by InstanceProperties)
T_COLLISION = 0x2A8BDF5A       # collision mesh (Havok packfiles, physics.zrb)

MODEL_TYPES = (T_MODEL, T_SKINNED_MODEL, T_LEVEL_MODEL, T_FOLIAGE_MODEL, T_HEAD_MODEL)


class Record:
    __slots__ = ('off', 'end', 'kind', 'seg1', 'seg2', 'seg3', 'seg4', 'type', 'name')

    def to_tuple(self):
        return (self.off, self.end, self.kind, self.seg1, self.seg2, self.seg3, self.seg4, self.type, self.name)

    @classmethod
    def from_tuple(cls, t):
        r = cls()
        (r.off, r.end, r.kind, r.seg1, r.seg2, r.seg3, r.seg4, r.type, r.name) = t
        return r

    @property
    def payload_off(self):
        return self.off + 0x28


def header_span(head):
    """Bytes needed from the start of a ZRB to parse every record (end of segment 0)."""
    if len(head) < 0x30:
        raise MagFormatError('truncated ZRB header')
    ver, _, s0size, s0end = struct.unpack_from('>IIII', head, 0)
    if ver != 6:
        raise MagFormatError('unsupported ZRB version %d' % ver)
    return s0end


class Zrb:
    def __init__(self, data, full=True):
        """``data``: either the whole file, or at least ``header_span`` bytes when ``full`` is False."""
        self.data = data
        if len(data) < 0x30:
            raise MagFormatError('truncated ZRB')
        h = struct.unpack_from('>12I', data, 0)
        if h[0] != 6:
            raise MagFormatError('unsupported ZRB version %d' % h[0])
        self.hdr = h
        self.seg_start = (None, h[3], h[5], h[7], h[9])
        self.seg_size = (h[2], h[4], h[6], h[8], h[10])
        self.file_size = h[9] + h[10]
        ntypes = h[11]
        first = data.find(REC_MAGIC, 0x30, h[3])
        self.records = []
        self.types = []
        self.deps = self._deps(first)
        if first < 0:
            return
        self.types = [struct.unpack_from('>II', data, first - 8 * ntypes + 8 * k) for k in range(ntypes)]
        total = sum(c for _, c in self.types)
        r = first
        for _ in range(total):
            if data[r:r + 8] != REC_MAGIC:
                raise MagFormatError('bad ZRB record at %#x' % r)
            w = struct.unpack_from('>10I', data, r)
            rec = Record()
            rec.off = r
            rec.end = first + w[2]
            rec.kind = w[3]
            rec.seg1, rec.seg2, rec.seg3, rec.seg4 = w[4], w[5], w[6], w[7]
            rec.type, rec.name = w[8], w[9]
            self.records.append(rec)
            if rec.end <= r:
                raise MagFormatError('ZRB record does not advance at %#x' % r)
            r = rec.end

    def _deps(self, first):
        """Dependency ZRB paths (best effort: they are only used for display)."""
        d = self.data
        out = []
        end = first if first > 0 else min(len(d), 0x4000)
        i = 0x30
        while True:
            i = d.find(b'run/mag/', i, end)
            if i < 0:
                break
            j = d.find(b'\0', i, end)
            if j < 0:
                break
            s = d[i:j].decode('latin1')
            if s.endswith('.zrb'):
                out.append(s)
            i = j
        return out

    def payload(self, rec):
        return self.data[rec.payload_off:rec.end]

    def seg_offset(self, seg, off):
        return self.seg_start[seg] + off
