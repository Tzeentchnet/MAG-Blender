"""Resource access on top of a GameIndex: ZRB loading (LRU cached), decoded models, textures,
skeletons and animations."""
import collections
import json
import os
import re
import struct
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import havok, materials, models, textures
from .catalog import skeleton_sources
from .errors import MagFormatError
from .iso9660 import IsoFile
from .psarc import Psarc
from .zrb import Zrb, T_TEXTURE, T_MATERIAL, T_HAVOK


class Library:
    def __init__(self, index, iso_path, cache_dir, cache_bytes=768 << 20):
        self.index = index
        self.iso = IsoFile(iso_path)
        self.cache_dir = cache_dir
        self.tex_dir = os.path.join(cache_dir, 'textures')
        os.makedirs(self.tex_dir, exist_ok=True)
        self._archives = {}
        self._zrbs = collections.OrderedDict()
        self._zrb_bytes = 0
        self._cache_bytes = cache_bytes
        self._lock = threading.RLock()
        self._tex_hashes = None
        self._skeletons = {}
        self._skel_sources = None

    def close(self):
        self.iso.close()

    # -- containers -----------------------------------------------------------------------------
    def archive(self, ai):
        with self._lock:
            a = self._archives.get(ai)
            if a is None:
                path, off, size, toc = self.index.archives[ai]
                a = Psarc(self.iso, off, size, toc)
                self._archives[ai] = a
            return a

    def zrb(self, zi):
        with self._lock:
            z = self._zrbs.get(zi)
            if z is not None:
                self._zrbs.move_to_end(zi)
                return z
        ent = self.index.zrbs[zi]
        data = self.archive(ent.archive).read(ent.name)
        z = Zrb(data)
        with self._lock:
            self._zrbs[zi] = z
            self._zrb_bytes += len(data)
            while self._zrb_bytes > self._cache_bytes and len(self._zrbs) > 1:
                _, old = self._zrbs.popitem(last=False)
                self._zrb_bytes -= len(old.data)
        return z

    def prefetch(self, zis, workers=8):
        """Read and decompress many ZRBs in parallel (zlib releases the GIL)."""
        todo = [zi for zi in dict.fromkeys(zis) if zi not in self._zrbs]
        if not todo:
            return
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(self.zrb, todo))

    def record(self, zi, ri):
        z = self.zrb(zi)
        rt = self.index.zrbs[zi].records[ri]
        for r in z.records:
            if r.off == rt[0]:
                return z, r
        raise MagFormatError('record not found')

    # -- models ---------------------------------------------------------------------------------
    def model_header(self, zi, ri):
        """The model record without its geometry (bounds, meshes, nodes)."""
        z, r = self.record(zi, ri)
        return models.parse_model(z.payload(r), r.type, r.name)

    def model(self, zi, ri):
        z, r = self.record(zi, ri)
        m = models.parse_model(z.payload(r), r.type, r.name)
        geoms = models.decode_model_geometry(z, r, m)
        return m, geoms

    # -- materials / textures ----------------------------------------------------------------------
    def texture_hashes(self):
        if self._tex_hashes is None:
            self._tex_hashes = self.index.texture_hashes()
        return self._tex_hashes

    def material(self, name, near_zrb=None):
        hit = self.index.find(name, (T_MATERIAL,), near_zrb)
        if hit is None:
            return None
        _, zi, ri = hit
        z, r = self.record(zi, ri)
        return materials.parse_material(z.payload(r), name, self.texture_hashes()), zi

    def texture_info(self, name, near_zrb=None):
        hit = self.index.find(name, (T_TEXTURE,), near_zrb)
        if hit is None:
            return None
        _, zi, ri = hit
        z, r = self.record(zi, ri)
        t = textures.parse_texture(z.payload(r), name)
        t.data_at = z.seg_start[3] + r.seg3
        return t, z

    def texture_file(self, name, near_zrb=None):
        """DDS file for the texture (written once into the persistent cache) and a small metadata
        dict: ``alpha`` (has cut-out alpha), ``dxt5nm`` (normal stored as X in alpha, Y in green)."""
        path = os.path.join(self.tex_dir, '%08x.dds' % name)
        meta_path = path + '.json'
        if os.path.exists(path) and os.path.exists(meta_path):
            try:
                with open(meta_path, 'r') as f:
                    return path, json.load(f)
            except (OSError, ValueError):
                pass
        got = self.texture_info(name, near_zrb)
        if got is None:
            return None, {}
        t, z = got
        raw = z.data[t.data_at:t.data_at + t.size]
        try:
            dds = textures.dds_bytes(t, raw)
        except MagFormatError:
            return None, {}
        meta = {'alpha': False, 'dxt5nm': False, 'fmt': t.label, 'size': [t.width, t.height]}
        try:
            s = textures.sample_rgba(t, raw, 64).reshape(-1, 4).astype(np.float32) / 255.0
            meta['alpha'] = bool((s[:, 3] < 0.5).mean() > 0.01)
            mean = s.mean(0)
            meta['dxt5nm'] = bool(mean[0] < 0.05 and mean[2] < 0.05)
        except Exception:
            pass
        tmp = path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(dds)
        os.replace(tmp, path)
        with open(meta_path, 'w') as f:
            json.dump(meta, f)
        return path, meta

    def texture_cached(self, name):
        path = os.path.join(self.tex_dir, '%08x.dds' % name)
        return os.path.exists(path) and os.path.exists(path + '.json')

    def prefetch_textures(self, items, workers=8):
        """Convert many textures into the cache in parallel (decompression, numpy and file I/O
        release the GIL). ``items``: (texture name, near_zrb). Returns the number converted."""
        todo = {}
        for name, near in items:
            if name not in todo and not self.texture_cached(name):
                todo[name] = near
        if not todo:
            return 0
        self.index.by_name()  # build the lookup tables once, before the threads use them
        self.texture_hashes()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(lambda kv: self.texture_file(*kv), todo.items()))
        return len(todo)

    # -- havok -----------------------------------------------------------------------------------
    def packfile(self, zi, ri):
        z, r = self.record(zi, ri)
        size = struct.unpack_from('>I', z.data, r.payload_off + 4)[0]
        s = z.seg_start[1] + r.seg1
        return havok.Packfile(z.data[s:s + size])

    def skeleton(self, name):
        if name in self._skeletons:
            return self._skeletons[name]
        if self._skel_sources is None:
            self._skel_sources = skeleton_sources(self.index)
        src = self._skel_sources.get(name)
        if src is None:
            return None
        pf = self.packfile(*src)
        for o in pf.find('hkaSkeleton'):
            s = havok.read_skeleton(pf, o)
            self._skeletons[s.name] = s
        return self._skeletons.get(name)

    def skeleton_names(self):
        if self._skel_sources is None:
            self._skel_sources = skeleton_sources(self.index)
        return sorted(self._skel_sources)

    def animations(self, zi, ri):
        return havok.read_animations(self.packfile(zi, ri))


# -- skeleton fitting ------------------------------------------------------------------------------
def _qmat(q):
    x, y, z, w = (float(v) for v in q)
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def skeleton_world(sk):
    """Bind pose world matrices (n,4,4) of a Havok skeleton (game space)."""
    n = len(sk.bones)
    out = np.zeros((n, 4, 4))
    for i in range(n):
        L = np.eye(4)
        L[:3, :3] = _qmat(sk.rotations[i]) @ np.diag(sk.scales[i])
        L[:3, 3] = sk.translations[i]
        p = sk.parents[i]
        out[i] = L if p < 0 else out[p] @ L
    return out


def fit_score(sk, geoms):
    """How well skinned geometry matches a skeleton's bind pose (lower is better)."""
    W = skeleton_world(sk)
    P = W[:, :3, 3]
    kids = collections.defaultdict(list)
    for i, p in enumerate(sk.parents):
        kids[p].append(i)
    errs = []
    for g in geoms:
        if g is None or g.joints is None:
            continue
        dom = g.weights[:, 0] > 0.8
        j = g.joints[dom, 0]
        if len(j) == 0:
            continue
        if j.max() >= len(P):
            return 1e9
        for b in np.unique(j):
            c = g.positions[dom][j == b].mean(0)
            best = np.linalg.norm(c - P[b])
            for ch in kids.get(b, ()):
                a, e = P[b], P[ch]
                v = e - a
                t = np.clip(np.dot(c - a, v) / max(np.dot(v, v), 1e-9), 0, 1)
                best = min(best, np.linalg.norm(c - (a + t * v)))
            errs.append(best)
    return float(np.median(errs)) if errs else 1e9
