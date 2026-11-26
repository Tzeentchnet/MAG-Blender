"""Material records (7CBE7595): header, texture bindings and shader constants.

The payload is a byte-packed serialization (big-endian; pointers are written as 0xDEADBEEF)::

    +0x00  0x80-byte header (fields below)
    +0x80  binding table: count = u8 +0x1D, 14 bytes each:
               u32 texture name hash, u32 pass mask, u8 sampler slot, u8 count,
               u32 runtime offset of the texture descriptor (0x18 apart)
    then   engine parameters, 20 bytes each (values the renderer supplies: transforms, ...):
               DEADBEEF, u32 2, u32 semantic id, u32 pass mask, u32 destination
    then   constant parameters, 23 bytes each, followed by their f32 values:
               DEADBEEF, u32 4, u32 pass mask, u16 value index, 3 bytes 0, u8 value type,
               u16 destination, 3 bytes
    then   pass records: DEADBEEF, u32 7, 20 bytes (the last one is followed by more data)

Header fields that are known::

    +0x06  u8  render bits: 0x80 = alpha test
    +0x08  u8  render group bits (GROUP_*): 0x01 wind/foliage, 0x04 translucent,
               0x10 static level geometry, 0x20 skinned, 0x40 baked vertex lighting
    +0x09  u8  alpha test reference (0-255), nonzero exactly when 0x80 of +0x06 is set
    +0x12  u32 technique (shader) hash: 62 distinct on the disc
    +0x1D  u8  binding count
    +0x28  u32 vertex attributes the technique reads (the meshes' vertex format), plus
               VERTEX_LIT (0x10000000)
    +0x44  u16 x5  runtime offsets (parameter records, values, technique map, table, ...)

A parameter is identified by its technique and its *destination* (a byte offset inside the
technique's program chunk, which is not in the material). Parameters carry no names, so their
meanings come from the values seen across all materials of a technique (``TECHNIQUES``).

Sampler slots (by the formats bound to them across the game)::

    3   base colour (DXT1, DXT5 when it carries alpha)
    4   normal map, DXT5nm (X in alpha, Y in green)
    5   specular colour (gloss in alpha)
    15  emissive / mask
    16  detail normal (DXT5nm)
    17  detail colour (mid-grey centred)
    18  environment (3 entries)
    255 auxiliary mask

Terrain tile materials (technique ``TERRAIN``) bind four tiled detail layers instead: slot 3
four times (colour) and slot 4 four times (DXT5nm normal), ordered by the binding's descriptor
offset. Slot 8 is the per-tile RGBA layer weight map (R, G, B, A -> layers 0-3), slot 12 a 64x64
pre-blended colour of the tile (used for distant LODs). See ``TerrainLayers``.

If the structure does not parse, ``parse_material`` falls back to scanning the payload for known
texture hashes (``MaterialInfo.parsed`` is then False).
"""
import struct

SLOT_NAMES = {
    3: 'base_color', 4: 'normal', 5: 'specular', 15: 'emissive', 16: 'detail_normal',
    17: 'detail_color', 18: 'environment', 255: 'mask', 0: 'slot0', 2: 'slot2', 13: 'slot13',
}

HEADER = 0x80
BINDING = 14
POINTER = b'\xde\xad\xbe\xef'
TAG_ENGINE, TAG_CONSTANT, TAG_PASS = 2, 4, 7

FLAGS_AT = 0x28
VERTEX_LIT = 0x10000000     # flag: lit by the placement's baked vertex lighting (84DFB280)

RENDER_ALPHA_TEST = 0x80    # header +0x06

GROUP_WIND = 0x01           # header +0x08: foliage and wind-animated plants
GROUP_TRANSLUCENT = 0x04    # glass, lenses, windows, blended decals
GROUP_LEVEL = 0x10          # static level geometry
GROUP_SKINNED = 0x20        # skinned characters and gear
GROUP_VERTEX_LIT = 0x40     # same materials as VERTEX_LIT

# words per constant value type (the recomp's material-values.md: 1-2 int, 4-7 f32 x1-4, 8-10
# matrices). Only types 2 and 4-7 occur on the disc.
VALUE_WORDS = {1: 1, 2: 1, 3: 0, 4: 1, 5: 2, 6: 3, 7: 4, 8: 8, 9: 12, 10: 16}

TERRAIN = 0xF9F3BDA5        # terrain tile technique
WATER = 0xB18B8189          # water planes (the only technique with engine parameters 31 and 32)
GLASS = 0xF436DE9A          # translucent glass: normal + environment map, no colour texture
WIRE = 0xBC705D52           # power cables (Baku): no textures, no UVs or normals, one colour

# Pass-1 constant destinations with a known meaning, per technique:
#   uv      (su, sv, ou, ov): uv' = uv * s + o for the base colour, normal and specular maps
#           (identity on 95 % of materials)
#   detail  (d, d): UV0 scale of the detail maps, in slot order (16, then 17)
TECHNIQUES = {
    0x7BDA1E1F: dict(uv=0x208, detail=(0x220,)),
    0x9B4BB646: dict(uv=0x208, detail=(0x220,)),
    0x154AF603: dict(uv=0x220, detail=(0x238,)),
    0x53126094: dict(uv=0x238, detail=(0x250, 0x268)),
    0xEE546AAE: dict(uv=0x220, detail=(0x238,)),
    0xA06FEF2E: dict(uv=0x208, detail=(0x220,)),
    0x1D64212B: dict(uv=0x268),
    0x1948A87F: dict(uv=0x268),
    0x5BCF2C4C: dict(uv=0x238, detail=(0x250,)),
    0x17CEAD41: dict(uv=0x268, detail=(0x280,)),
    0xC1F9AEDC: dict(uv=0x250),
    0xD79CFC92: dict(uv=0x238),
    0x92F95536: dict(uv=0x268, detail=(0x250,)),
    0x18397D5E: dict(uv=0x238, detail=(0x250,)),
    0x0599D5B9: dict(uv=0x250, detail=(0x268,)),
    0xF2F1399F: dict(uv=0x238, detail=(0x250,)),
    0x0DA1D4BC: dict(uv=0x238),
    0x32FB8679: dict(uv=0x238),
    0xE936F659: dict(uv=0x1F0, detail=(0x208,)),
    0x4C1C9376: dict(uv=0x238),
    0x635DAAEF: dict(uv=0x250, detail=(0x268,)),
    0x48D4635B: dict(uv=0x220, detail=(0x238,)),
    0x72C59D94: dict(uv=0x220, detail=(0x250,)),
    0xE265090F: dict(uv=0x208, detail=(0x220,)),
    0x7CB882E8: dict(uv=0x220, detail=(0x238,)),
    0xF792D9CF: dict(uv=0x238, detail=(0x250,)),
    0xE02975D2: dict(uv=0x238, detail=(0x268,)),
    0x023AC66F: dict(uv=0x208, detail=(0x220,)),
    0x670B51D2: dict(uv=0x250, detail=(0x268,)),
    0xC923C40A: dict(uv=0x250, detail=(0x268,)),
    0x96261A09: dict(uv=0x250),
}
WATER_COLOR = 0x430         # water: (r, g, b, a), a dark linear colour (a 0.05-0.9, unknown)
WIRE_COLOR = 0xB8           # wire: (r, g, b), linear (0.0014 grey: near-black cables)

TERRAIN_UV = (0x208, 0x220, 0x238, 0x250)    # four rows (s, s, s*x0, s*z0): s * uv0 + (c, d)
TERRAIN_SPEC = (0x448, 0x460, 0x478, 0x490)  # four rows (spec r, g, b, power)


class Constant:
    __slots__ = ('mask', 'index', 'type', 'dest', 'values')

    def __repr__(self):
        return 'Constant(mask=%d, dest=0x%x, type=%d, %r)' % (self.mask, self.dest, self.type, self.values)


class TerrainLayers:
    """Detail layers of a terrain tile material."""
    __slots__ = ('colors', 'normals', 'weights', 'macro', 'uv', 'spec')


class MaterialInfo:
    __slots__ = ('name', 'bindings', 'flags', 'terrain', 'parsed', 'technique', 'render', 'group',
                 'alpha_ref', 'binding_table', 'engine', 'constants', 'passes')

    def slot(self, s):
        return self.bindings.get(s)

    @property
    def alpha_test(self):
        return bool(self.render & RENDER_ALPHA_TEST)

    @property
    def translucent(self):
        return bool(self.group & GROUP_TRANSLUCENT)

    @property
    def water(self):
        return self.technique == WATER

    @property
    def glass(self):
        return self.technique == GLASS

    @property
    def wire(self):
        return self.technique == WIRE

    def constant(self, dest, mask=1):
        """Values of the first constant with this destination whose pass mask shares a bit with
        ``mask``, or None."""
        for c in self.constants:
            if c.dest == dest and c.mask & mask:
                return c.values
        return None

    def uv_transform(self):
        """(su, sv, ou, ov) of the base maps, or None when unknown or identity."""
        t = TECHNIQUES.get(self.technique)
        v = t and self.constant(t['uv'])
        if not v or len(v) != 4 or v == (1.0, 1.0, 0.0, 0.0) or v[0] == 0.0 or v[1] == 0.0:
            return None
        return v

    def detail_scale(self, slot):
        """UV0 scale (su, sv) of detail slot 16 or 17, or None."""
        t = TECHNIQUES.get(self.technique)
        dests = t.get('detail', ()) if t else ()
        bound = [s for s in (16, 17) if s in self.bindings]
        if slot not in bound or len(dests) < len(bound):
            return None
        v = self.constant(dests[bound.index(slot)])
        return v if v and len(v) == 2 and v[0] > 0.0 and v[1] > 0.0 else None


def parse_material(payload, name, texture_hashes):
    """Decode a material record. ``texture_hashes`` (known texture name hashes from the global
    index) is used only by the fallback scan."""
    p = payload
    m = MaterialInfo()
    m.name = name
    m.flags = struct.unpack_from('>I', p, FLAGS_AT)[0] if len(p) >= FLAGS_AT + 4 else 0
    m.technique = m.render = m.group = m.alpha_ref = 0
    m.binding_table, m.engine, m.constants, m.passes = [], [], [], []
    m.parsed = _parse_structure(m, p)
    if not m.parsed:
        m.binding_table = _scan_bindings(p, texture_hashes)
    # A slot bound more than once in a pass (roads, blended and tinted techniques) lists its
    # layers by binding offset: the lowest offset is the base layer, the others are a second blend
    # layer, a tint or ramp, or the 16x16 white placeholder 336759d8 / flat normal 75cb8229.
    found = {}  # slot -> (pass, offset, hash)
    every = {}  # slot -> [(offset, hash)] of the first pass
    for h, ps, slot, cnt, off in m.binding_table:
        if ps == 1:
            every.setdefault(slot, []).append((off, h))
        prev = found.get(slot)
        if prev is None or (ps, off) < prev[:2]:
            found[slot] = (ps, off, h)
    m.bindings = {s: v[2] for s, v in found.items()}
    m.terrain = _terrain_layers(m, p, every)
    if m.terrain is not None:
        # the first colour/normal layer is not a tile texture: fall back to the tile's macro colour
        m.bindings[3] = m.terrain.macro
        m.bindings.pop(4, None)
    return m


def _parse_structure(m, p):
    if len(p) < HEADER or p[0:4] != b'\0\0\0\0':
        return False
    nb = p[0x1D]
    end = HEADER + BINDING * nb
    if end > len(p):
        return False
    m.binding_table = [struct.unpack_from('>IIBBI', p, HEADER + BINDING * i) for i in range(nb)]
    m.render, m.group, m.alpha_ref = p[6], p[8], p[9]
    m.technique = struct.unpack_from('>I', p, 0x12)[0]
    i = end
    try:
        while p[i:i + 4] == POINTER:
            tag = struct.unpack_from('>I', p, i + 4)[0]
            if tag == TAG_ENGINE:
                m.engine.append(struct.unpack_from('>III', p, i + 8))  # (semantic, mask, dest)
                i += 20
            elif tag == TAG_CONSTANT:
                c = Constant()
                c.mask, c.index, c.type, c.dest = struct.unpack_from('>IH3xBH', p, i + 8)
                if c.type not in VALUE_WORDS:
                    return False
                m.constants.append(c)
                i += 23
            else:
                break
        if m.constants:
            words = max(c.index + VALUE_WORDS[c.type] for c in m.constants)
            vals = struct.unpack_from('>%df' % words, p, i)
            for c in m.constants:
                c.values = vals[c.index:c.index + VALUE_WORDS[c.type]]
            i += 4 * words
        while p[i:i + 4] == POINTER and struct.unpack_from('>I', p, i + 4)[0] == TAG_PASS:
            m.passes.append(bytes(p[i + 8:i + 28]))
            i += 28
            j = p.find(POINTER, i)
            if j < 0:
                break
            i = j
    except struct.error:
        return False
    return bool(m.passes)


def _scan_bindings(p, texture_hashes):
    """Fallback: find 14-byte bindings by scanning for known texture hashes."""
    out = []
    n = len(p) - 14
    i = 0
    while i <= n:
        h = struct.unpack_from('>I', p, i)[0]
        if h in texture_hashes:
            ps, slot, cnt, off = struct.unpack_from('>IBBI', p, i + 4)
            if 0 < ps < 0x10000 and 0 < cnt <= 4:
                out.append((h, ps, slot, cnt, off))
                i += 14
                continue
        i += 1
    return out


def _terrain_layers(m, p, every):
    cols, nrms = sorted(every.get(3, ())), sorted(every.get(4, ()))
    if len(cols) != 4 or len(nrms) != 4 or 8 not in every or 12 not in every:
        return None
    if m.parsed:
        if m.technique != TERRAIN:
            return None
        rows = [m.constant(d) for d in TERRAIN_UV]
        spec = [m.constant(d) for d in TERRAIN_SPEC]
    else:  # fallback: the values sit at fixed offsets in the 0x418-byte layout
        if len(p) != 0x418:
            return None
        rows = [struct.unpack_from('>4f', p, 0x322 + 16 * k) for k in range(4)]
        spec = [struct.unpack_from('>4f', p, 0x362 + 16 * k) for k in range(4)]
    t = TerrainLayers()
    t.colors = [h for _, h in cols]
    t.normals = [h for _, h in nrms]
    t.weights = every[8][0][1]
    t.macro = every[12][0][1]
    t.uv, t.spec = [], []
    for r, sp in zip(rows, spec):
        if r is None or len(r) != 4:
            return None
        s, s2, c, d = r
        if not (0.0 < s < 1024.0 and s == s2):
            return None
        t.uv.append((s, c, d))
        t.spec.append(tuple(sp) if sp else (0.0, 0.0, 0.0, 1.0))
    return t
