"""Level placement data.

The compiled level (``<level>.clv`` inside the level PSARC) is a stream of reflected property
objects. Static geometry is placed by ``InstanceProperties`` objects::

    Name (util::Token), Mesh (util::Token = model record name hash), ObjectID,
    TranslateX/Y/Z, RotateX/Y/Z (degrees), Scale, ScaleX/Y/Z

World matrix = T * Rz * Ry * Rx * S in the game's Y-up space. The level compiler stores the
runtime transform after each instance (xyzw quaternion, translation, one scale); on every map that
scale is ``Scale``. The editor mirrors ``Scale`` into ``ScaleX/Y/Z``, so the fields must not be
multiplied (that squares the scale).
"""
import math
import struct

import numpy as np

from . import props


class Instance:
    __slots__ = ('mesh', 'name', 'translation', 'rotation', 'scale')


def read_instances(clv):
    out = []
    for obj in props.iter_objects(clv, type_name='InstanceProperties'):
        mesh = obj.token('Mesh')
        if not mesh:
            continue
        i = Instance()
        i.mesh = mesh
        i.name = obj.token('Name') or 0
        i.translation = (obj.float('TranslateX'), obj.float('TranslateY'), obj.float('TranslateZ'))
        i.rotation = (obj.float('RotateX'), obj.float('RotateY'), obj.float('RotateZ'))
        if obj.get('Scale') is not None:  # what the compiled transform holds
            s = obj.float('Scale', 1.0)
            i.scale = (s, s, s)
        else:
            i.scale = (obj.float('ScaleX', 1.0), obj.float('ScaleY', 1.0), obj.float('ScaleZ', 1.0))
        out.append(i)
    return out


def instances_to_arrays(insts):
    """Compact numpy form for caching: (N,) uint32 mesh hashes and (N, 9) float32 TRS."""
    mesh = np.array([i.mesh for i in insts], np.uint32)
    trs = np.array([i.translation + i.rotation + i.scale for i in insts], np.float32).reshape(-1, 9)
    return mesh, trs


TREE_HEADER = 0x3F
TREE_NODE = 161


def terrain_tree_levels(payload):
    """Levels of detail of a level's terrain LOD tree (``284C2F78`` record in globaldata.zrb),
    finest first: ``[[piece name hash, ...], ...]`` where level k holds the nodes k steps above
    the leaves. Level 0 is the leaves; the last level is the root's two children.

    The record is a 0x3F-byte header (u16 node count at +0x3D) followed by 161-byte binary-tree
    nodes; node 0 is the root and has no piece. In each node, +0x79 holds two u16 child indices
    (FFFF in a leaf) and +0x9D the u32 name hash of the tile's level piece. Every tree on the
    disc is complete and balanced, so a level is a seamless cover of the terrain; a node's
    height is taken from its subtree all the same. Returns [] if the payload does not look like
    a terrain tree."""
    if len(payload) < TREE_HEADER:
        return []
    count = struct.unpack_from('>H', payload, 0x3D)[0]
    if count < 2 or TREE_HEADER + TREE_NODE * count > len(payload):
        return []
    kids, piece = [], []
    for k in range(count):
        o = TREE_HEADER + TREE_NODE * k
        if k and payload[o + 1:o + 5] != b'\xde\xad\xbe\xef':
            return []
        a, b = struct.unpack_from('>HH', payload, o + 0x79)
        kids.append([c for c in (a, b) if c < count])
        piece.append(struct.unpack_from('>I', payload, o + 0x9D)[0])
    order, seen = [0], {0}
    for k in order:  # breadth first from the root; ignore anything unreachable or repeated
        for c in kids[k]:
            if c not in seen:
                seen.add(c)
                order.append(c)
    height = [0] * count
    for k in reversed(order):
        height[k] = 1 + max((height[c] for c in kids[k] if c in seen), default=-1)
    levels = [[] for _ in range(height[0])]
    for k in order[1:]:
        levels[height[k]].append(piece[k])
    return [lv for lv in levels if lv]


def terrain_tree_leaves(payload):
    """Level-piece name hashes of the leaves of a level's terrain LOD tree; see
    :func:`terrain_tree_levels`."""
    levels = terrain_tree_levels(payload)
    return levels[0] if levels else []


BACKDROP_MARGIN = 50.0  # metres outside the terrain square


def terrain_square(bounds):
    """(x0, z0, x1, z1) covered by terrain tiles, from their model boxes (centre, half extents)."""
    b = np.asarray(bounds, float).reshape(-1, 6)
    return ((b[:, 0] - b[:, 3]).min(), (b[:, 2] - b[:, 5]).min(),
            (b[:, 0] + b[:, 3]).max(), (b[:, 2] + b[:, 5]).max())


def outside_distance(x, z, square):
    """Horizontal distance of the points (x, z) outside the terrain square (0 inside)."""
    x0, z0, x1, z1 = square
    dx = np.maximum(np.maximum(x0 - np.asarray(x, float), np.asarray(x, float) - x1), 0.0)
    dz = np.maximum(np.maximum(z0 - np.asarray(z, float), np.asarray(z, float) - z1), 0.0)
    return np.hypot(dx, dz)


def trs_matrix(trs):
    """(9,) TRS (degrees) -> 4x4 world matrix in game space (column vectors)."""
    tx, ty, tz, rx, ry, rz, sx, sy, sz = (float(v) for v in trs)
    cx, sxn = math.cos(math.radians(rx)), math.sin(math.radians(rx))
    cy, syn = math.cos(math.radians(ry)), math.sin(math.radians(ry))
    cz, szn = math.cos(math.radians(rz)), math.sin(math.radians(rz))
    Rx = np.array([[1, 0, 0], [0, cx, -sxn], [0, sxn, cx]])
    Ry = np.array([[cy, 0, syn], [0, 1, 0], [-syn, 0, cy]])
    Rz = np.array([[cz, -szn, 0], [szn, cz, 0], [0, 0, 1]])
    m = np.eye(4)
    m[:3, :3] = Rz @ Ry @ Rx @ np.diag([sx, sy, sz])
    m[:3, 3] = (tx, ty, tz)
    return m
