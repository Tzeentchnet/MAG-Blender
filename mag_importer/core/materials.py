"""Material records: texture bindings.

A material payload contains a binding table of 14-byte entries::

    u32 texture name hash, u32 pass/technique mask, u8 sampler slot, u8 count, u32 offset

Observed sampler slots (by the formats bound to them across the game):

    3   base colour (DXT1, DXT5 when it carries alpha)
    4   normal map, DXT5nm (X in alpha, Y in green)
    5   specular colour (gloss in alpha)
    15  emissive / mask
    16  detail normal (DXT5nm)
    17  detail colour
    18  environment (3 entries)
    255 auxiliary mask

Terrain tile materials (0x418-byte payload) bind four tiled detail layers instead: slot 3 four
times (colour) and slot 4 four times (DXT5nm normal), ordered by the binding's offset field.
Slot 8 is the per-tile RGBA layer weight map (R, G, B, A -> layers 0-3), slot 12 a 64x64
pre-blended colour of the tile (used for distant LODs). See ``TerrainLayers``.

The u32 at +0x28 of the payload is a flag word (``MaterialInfo.flags``). Its low bits look like
the vertex attributes the technique reads; ``VERTEX_LIT`` marks materials drawn with baked
per-vertex lighting (``lighting.py``).
"""
import struct

SLOT_NAMES = {
    3: 'base_color', 4: 'normal', 5: 'specular', 15: 'emissive', 16: 'detail_normal',
    17: 'detail_color', 18: 'environment', 255: 'mask', 0: 'slot0', 2: 'slot2', 13: 'slot13',
}


FLAGS_AT = 0x28
VERTEX_LIT = 0x10000000     # flag: lit by the placement's baked vertex lighting (84DFB280)

TERRAIN_PAYLOAD = 0x418
TERRAIN_UV_ROWS = 0x322     # four rows (s, s, s*x0, s*z0): detail UV = s * uv0 + (c, d)
TERRAIN_SPEC_ROWS = 0x362   # four rows (spec r, g, b, power)


class TerrainLayers:
    """Detail layers of a terrain tile material."""
    __slots__ = ('colors', 'normals', 'weights', 'macro', 'uv', 'spec')


class MaterialInfo:
    __slots__ = ('name', 'bindings', 'flags', 'terrain')

    def slot(self, s):
        return self.bindings.get(s)


def parse_material(payload, name, texture_hashes):
    """Find texture bindings. ``texture_hashes``: set of known texture name hashes (from the
    global index) - scanning for them is more robust than following the serialized layout,
    whose header size varies with the shader technique."""
    p = payload
    m = MaterialInfo()
    m.name = name
    m.flags = struct.unpack_from('>I', p, FLAGS_AT)[0] if len(p) >= FLAGS_AT + 4 else 0
    found = {}  # slot -> (pass, hash)
    every = {}  # slot -> [(offset, hash)] of the first pass
    n = len(p) - 14
    i = 0
    while i <= n:
        h = struct.unpack_from('>I', p, i)[0]
        if h in texture_hashes:
            ps, slot, cnt, off = struct.unpack_from('>IBBI', p, i + 4)
            if 0 < ps < 0x10000 and 0 < cnt <= 4:
                if ps == 1:
                    every.setdefault(slot, []).append((off, h))
                prev = found.get(slot)
                if prev is None or ps < prev[0]:
                    found[slot] = (ps, h)
                i += 14
                continue
        i += 1
    m.bindings = {s: h for s, (ps, h) in found.items()}
    m.terrain = _terrain_layers(p, every)
    if m.terrain is not None:
        # the first colour/normal layer is not a tile texture: fall back to the tile's macro colour
        m.bindings[3] = m.terrain.macro
        m.bindings.pop(4, None)
    return m


def _terrain_layers(p, every):
    cols, nrms = sorted(every.get(3, ())), sorted(every.get(4, ()))
    if (len(p) != TERRAIN_PAYLOAD or len(cols) != 4 or len(nrms) != 4
            or 8 not in every or 12 not in every):
        return None
    t = TerrainLayers()
    t.colors = [h for _, h in cols]
    t.normals = [h for _, h in nrms]
    t.weights = every[8][0][1]
    t.macro = every[12][0][1]
    t.uv, t.spec = [], []
    for k in range(4):
        s, s2, c, d = struct.unpack_from('>4f', p, TERRAIN_UV_ROWS + 16 * k)
        if not (0.0 < s < 1024.0 and s == s2):
            return None
        t.uv.append((s, c, d))
        t.spec.append(struct.unpack_from('>4f', p, TERRAIN_SPEC_ROWS + 16 * k))
    return t
