"""Zipper reflected property objects (.zpr tables, .clv level data).

Each object::

    u32 0xDEADBEEF, u32 version, u32 type-name length, type name (NUL terminated), u8 more
    while more:
        u32 len, field name, u32 len, type name, u32 size, value[size - 4], u8 more

Values are big-endian; ``util::Token`` is a 32-bit hash, ``ztl::string`` is u32 length +
NUL-terminated text, nested types (``ztl::vector``, ``object`` ...) are opaque blobs here.
"""
import re
import struct

MAGIC = b'\xde\xad\xbe\xef'
_OBJ = re.compile(rb'\xde\xad\xbe\xef\x00\x00\x00[\x01-\x0f]\x00\x00\x00[\x02-\x7f][A-Za-z_]')


class PropObject:
    __slots__ = ('offset', 'end', 'type', 'fields')

    def get(self, name, default=None):
        f = self.fields.get(name)
        return f if f is not None else default

    def token(self, name):
        f = self.fields.get(name)
        if f and f[0] == 'util::Token' and len(f[1]) >= 4:
            return struct.unpack('>I', f[1][:4])[0]
        return None

    def float(self, name, default=0.0):
        f = self.fields.get(name)
        if f and f[0] == 'float' and len(f[1]) >= 4:
            return struct.unpack('>f', f[1][:4])[0]
        return default

    def string(self, name):
        f = self.fields.get(name)
        if f and f[0] == 'ztl::string' and len(f[1]) >= 4:
            n = struct.unpack('>I', f[1][:4])[0]
            return f[1][4:4 + n].split(b'\0')[0].decode('latin1')
        return None


def parse_object(d, o):
    if d[o:o + 4] != MAGIC:
        return None
    try:
        ver, ln = struct.unpack_from('>II', d, o + 4)
        tname = d[o + 12:o + 12 + ln - 1].decode('latin1')
        p = o + 12 + ln
        fields = {}
        more = d[p]
        p += 1
        guard = 0
        while more and guard < 4096:
            guard += 1
            nl = struct.unpack_from('>I', d, p)[0]
            if nl > 256:
                return None
            name = d[p + 4:p + 4 + nl - 1].decode('latin1')
            p += 4 + nl
            tl = struct.unpack_from('>I', d, p)[0]
            if tl > 256:
                return None
            typ = d[p + 4:p + 4 + tl - 1].decode('latin1')
            p += 4 + tl
            sz = struct.unpack_from('>I', d, p)[0]
            if sz < 4 or p + sz > len(d):
                return None
            p += 4
            fields[name] = (typ, d[p:p + sz - 4])
            p += sz - 4
            more = d[p]
            p += 1
    except (struct.error, IndexError, UnicodeDecodeError):
        return None
    obj = PropObject()
    obj.offset, obj.end, obj.type, obj.fields = o, p, tname, fields
    return obj


def iter_objects(d, start=0, end=None, type_name=None):
    pat = _OBJ
    if type_name is not None:
        tn = type_name.encode('latin1') + b'\0'
        pat = re.compile(re.escape(MAGIC) + rb'\x00\x00\x00[\x01-\x0f]' + re.escape(struct.pack('>I', len(tn)) + tn))
    for m in pat.finditer(d, start, len(d) if end is None else end):
        obj = parse_object(d, m.start())
        if obj is not None:
            yield obj


_LABEL_FIELDS = ('id', 'Name', 'name', 'Class', 'm_name', 'ItemName', 'VehicleName')
# fields that name an alternative model of the item (zoomed view, destroyed hulk, ...)
_VARIANT_WORDS = ('alternate', 'zoom', 'detonator', 'remote', 'hulk', 'invalid', 'highlight', 'godray',
                  'attacker', 'defender')


def token_labels(d):
    """Map token hashes referenced by model-like fields to readable labels."""
    out = {}
    for obj in iter_objects(d):
        label = None
        for k in _LABEL_FIELDS:
            s = obj.string(k)
            if s:
                label = s
                break
        if label is None:
            for k, (typ, val) in obj.fields.items():
                if typ == 'ztl::string':
                    s = obj.string(k)
                    if s:
                        label = s
                        break
        if not label:
            continue
        for k, (typ, val) in obj.fields.items():
            if typ != 'util::Token' or len(val) < 4:
                continue
            lk = k.lower()
            if 'model' in lk or 'mesh' in lk or 'geometry' in lk or 'hulk' in lk:
                h = struct.unpack('>I', val[:4])[0]
                if h:
                    variant = any(w in lk for w in _VARIANT_WORDS)
                    out.setdefault(h, label + ('_' + k if variant else ''))
    return out
