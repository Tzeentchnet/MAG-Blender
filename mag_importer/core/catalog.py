"""Game index: one scan of the ISO, persisted to disk, reused across Blender sessions.

The scan opens every PSARC on the disc, reads only the record table (segment 0) of every ZRB -
the leading 64 KiB blocks of each member - and keeps a compact list of the records the importer
cares about. Havok packfiles are summarised (skeleton / animation names), property tables are
mined for readable asset names, and each level's placement list is parsed once.
Work is spread over a thread pool (zlib and file reads release the GIL).
"""
import collections
import os
import pickle
import re
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import havok, level, props
from .errors import MagFormatError
from .iso9660 import IsoFile
from .psarc import Psarc
from .zrb import (Zrb, header_span, T_TEXTURE, T_MATERIAL, T_MODEL, T_SKINNED_MODEL, T_LEVEL_MODEL,
                  T_FOLIAGE_MODEL, T_HAVOK, T_FOLIAGE_BATCH, T_TERRAIN, T_PARTICLE_EFFECT, T_LIGHTING,
                  Record)

INDEX_VERSION = 14
KEEP_TYPES = {T_TEXTURE, T_MATERIAL, T_MODEL, T_SKINNED_MODEL, T_LEVEL_MODEL, T_FOLIAGE_MODEL, T_HAVOK,
              T_FOLIAGE_BATCH, T_LIGHTING}
MODEL_TYPES = (T_MODEL, T_SKINNED_MODEL, T_LEVEL_MODEL, T_FOLIAGE_MODEL)

CATEGORIES = ('CHARACTER', 'WEAPON', 'ATTACHMENT', 'EQUIPMENT', 'VEHICLE', 'PROP', 'MAP', 'ANIMATION')


class ZrbEntry:
    __slots__ = ('archive', 'name', 'length', 'seg_start', 'seg_size', 'records', 'terrain')


class AssetEntry:
    """One importable item shown in the UI."""
    __slots__ = ('key', 'category', 'label', 'detail', 'zrb', 'rec', 'extra')

    def __init__(self, key, category, label, detail, zrb, rec, extra=None):
        self.key = key
        self.category = category
        self.label = label
        self.detail = detail
        self.zrb = zrb
        self.rec = rec
        self.extra = extra


class GameIndex:
    def __init__(self):
        self.version = INDEX_VERSION
        self.iso_path = ''
        self.iso_size = 0
        self.iso_mtime = 0
        self.scan_seconds = 0.0
        self.archives = []    # (iso path, byte offset, size, toc)
        self.zrbs = []        # ZrbEntry
        self.labels = {}      # name hash -> readable label
        self.havok = {}       # (zrb, rec) -> summary dict
        self.levels = []      # dict(name, archive, clv, mesh=np.uint32[N], trs=np.float32[N,9])
        self.fx = set()       # name hashes of particle effects (placements of these are emitters)
        self.assets = []      # AssetEntry
        self._by_name = None

    # -- persistence (plain data only, so the cache survives reinstalling the add-on) --------
    def save(self, path):
        blob = dict(
            version=self.version, iso_path=self.iso_path, iso_size=self.iso_size, iso_mtime=self.iso_mtime,
            scan_seconds=self.scan_seconds, archives=self.archives,
            zrbs=[(z.archive, z.name, z.length, z.seg_start, z.seg_size, z.records, z.terrain) for z in self.zrbs],
            labels=self.labels, havok=self.havok, levels=self.levels, fx=sorted(self.fx))
        tmp = path + '.tmp'
        with open(tmp, 'wb') as f:
            pickle.dump(blob, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, path)

    @staticmethod
    def load(path):
        with open(path, 'rb') as f:
            blob = pickle.load(f)
        if not isinstance(blob, dict) or blob.get('version') != INDEX_VERSION:
            raise MagFormatError('index version changed; rescan required')
        idx = GameIndex()
        for k in ('iso_path', 'iso_size', 'iso_mtime', 'scan_seconds', 'archives', 'labels', 'havok', 'levels'):
            setattr(idx, k, blob[k])
        idx.fx = set(blob['fx'])
        for a, n, ln, ss, sz, recs, ter in blob['zrbs']:
            z = ZrbEntry()
            z.archive, z.name, z.length, z.seg_start, z.seg_size, z.records, z.terrain = a, n, ln, ss, sz, recs, ter
            idx.zrbs.append(z)
        build_catalog(idx)
        return idx

    def matches_iso(self, path):
        try:
            st = os.stat(path)
        except OSError:
            return False
        return (os.path.normcase(os.path.abspath(path)) == os.path.normcase(self.iso_path)
                and st.st_size == self.iso_size and int(st.st_mtime) == self.iso_mtime)

    # -- lookups -------------------------------------------------------------------------------
    def by_name(self):
        """name hash -> list of (type, zrb index, record index)."""
        if self._by_name is None:
            m = collections.defaultdict(list)
            for zi, z in enumerate(self.zrbs):
                for ri, r in enumerate(z.records):
                    m[r[8]].append((r[7], zi, ri))
            self._by_name = m
        return self._by_name

    def find(self, name, types, prefer_zrb=None):
        hits = [h for h in self.by_name().get(name, ()) if h[0] in types]
        if not hits:
            return None
        if prefer_zrb is not None:
            same = [h for h in hits if h[1] == prefer_zrb]
            if same:
                return same[0]
            arch = self.zrbs[prefer_zrb].archive
            same = [h for h in hits if self.zrbs[h[1]].archive == arch]
            if same:
                return same[0]
        return hits[0]

    def find_in_archive(self, name, types, archive):
        hits = [h for h in self.by_name().get(name, ()) if h[0] in types]
        if not hits:
            return None
        same = [h for h in hits if self.zrbs[h[1]].archive == archive]
        return (same or [h for h in hits if self.zrbs[h[1]].archive == 0] or hits)[0]

    def texture_hashes(self):
        return {r[8] for z in self.zrbs for r in z.records if r[7] == T_TEXTURE}

    def record(self, zi, ri):
        return Record.from_tuple(self.zrbs[zi].records[ri])

    def label(self, h, fallback=None):
        return self.labels.get(h) or fallback or '%08x' % h


# -- scanning ------------------------------------------------------------------------------------
class ScanProgress:
    def __init__(self):
        self.total = 1
        self.done = 0
        self.stage = 'Starting'
        self.lock = threading.Lock()
        self.cancel = False

    def step(self, n=1):
        with self.lock:
            self.done += n

    @property
    def fraction(self):
        return min(1.0, self.done / max(1, self.total))


def _stem(path):
    return path.rsplit('/', 1)[-1].rsplit('.', 1)[0]


def scan_iso(iso_path, progress=None, workers=None):
    t0 = time.time()
    progress = progress or ScanProgress()
    iso = IsoFile(iso_path)
    idx = GameIndex()
    idx.iso_path = os.path.normcase(iso.path)
    idx.iso_size = iso.size
    idx.iso_mtime = iso.mtime
    workers = workers or min(16, (os.cpu_count() or 4))

    psarcs = [p for p in iso.files if p.endswith('.PSARC')]
    if not psarcs:
        raise MagFormatError('no PSARC archives found on the image (is this the MAG disc?)')
    # MAIN first, levels after (stable ordering)
    psarcs.sort(key=lambda p: (not p.endswith('/MAIN.PSARC'), p))
    progress.stage = 'Reading archive tables'
    archives = []
    for p in psarcs:
        off, size = iso.files[p]
        arc = Psarc(iso, off, size)
        archives.append(arc)
        idx.archives.append((p, off, size, arc.toc()))

    jobs = []
    clv_jobs = []
    for ai, arc in enumerate(archives):
        for e in arc.entries:
            if e.name.endswith('.zrb') and not e.name.endswith('/headers.zrb'):
                jobs.append((ai, e.name, e.length))
            elif e.name.endswith('.clv'):
                clv_jobs.append((ai, e.name))
    progress.total = len(jobs) + len(clv_jobs) * 20
    progress.stage = 'Indexing resource blocks'

    def index_zrb(job):
        ai, name, length = job
        if progress.cancel:
            return None
        arc = archives[ai]
        try:
            head = arc.read(name, 0, min(length, 0x10000))
            span = header_span(head)
            data = head if span <= len(head) else arc.read(name, 0, span)
            z = Zrb(data, full=False)
        except (MagFormatError, struct.error, KeyError):
            progress.step()
            return None
        ent = ZrbEntry()
        ent.archive = ai
        ent.name = name
        ent.length = length
        ent.seg_start = z.seg_start
        ent.seg_size = z.seg_size
        ent.records = [r.to_tuple() for r in z.records if r.type in KEEP_TYPES]
        # terrain tiles are world-space level pieces organised as an LOD tree; remember each
        # piece's footprint (half extents X*Z) so the finest level can be selected per map
        ent.terrain = []
        if '/terrain_' in name:
            for ri, r in enumerate(ent.records):
                if r[7] == T_LEVEL_MODEL:
                    cx, cy, cz, ex, ey, ez = struct.unpack_from('>6f', data, r[0] + 0x28 + 0x18)
                    ent.terrain.append((ri, cx, cz, abs(ex), abs(ez)))
        # the level's terrain LOD tree names the tiles of every level of detail exactly
        tree = []
        if name.endswith('/globaldata.zrb'):
            for r in z.records:
                if r.type == T_TERRAIN:
                    tree = level.terrain_tree_levels(z.payload(r))
                    break
        labels = props.token_labels(data) if b'util::Token' in data else {}
        hk = {}
        for ri, r in enumerate(ent.records):
            if r[7] != T_HAVOK:
                continue
            try:
                rec = Record.from_tuple(r)
                size = struct.unpack_from('>I', data, rec.payload_off + 4)[0]
                blob = arc.read(name, z.seg_start[1] + rec.seg1, size)
                hk[ri] = havok.summarize(havok.Packfile(blob))
            except (MagFormatError, struct.error, KeyError, IndexError):
                pass
        progress.step()
        fx = {r.name for r in z.records if r.type == T_PARTICLE_EFFECT}
        return ent, labels, hk, tree, fx

    def index_clv(job):
        ai, name = job
        if progress.cancel:
            return None
        try:
            data = archives[ai].read(name)
            insts = level.read_instances(data)
            mesh, trs = level.instances_to_arrays(insts)
        except (MagFormatError, struct.error, KeyError):
            mesh, trs = np.zeros(0, np.uint32), np.zeros((0, 9), np.float32)
        progress.step(20)
        return ai, name, mesh, trs

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(index_zrb, jobs))
        progress.stage = 'Reading level layouts'
        clvs = list(ex.map(index_clv, clv_jobs))
    if progress.cancel:
        raise MagFormatError('scan cancelled')

    trees = {}  # archive -> per-LOD piece hashes from the terrain tree, finest first
    for res in results:
        if res is None:
            continue
        ent, labels, hk, tree, fx = res
        idx.fx |= fx
        if tree:
            trees[ent.archive] = tree
        zi = len(idx.zrbs)
        idx.zrbs.append(ent)
        for h, lab in labels.items():
            idx.labels.setdefault(h, lab)
        for ri, s in hk.items():
            idx.havok[(zi, ri)] = s
    tiles = collections.defaultdict(list)  # archive -> [(zi, ri, cx, cz, ex, ez)]
    for zi, z in enumerate(idx.zrbs):
        for ri, cx, cz, ex, ez in z.terrain:
            tiles[z.archive].append((zi, ri, cx, cz, ex, ez))
    for res in clvs:
        if res is None:
            continue
        ai, name, mesh, trs = res
        lods = tree_lods(idx, tiles.get(ai, []), trees.get(ai))
        # 'terrain' = the finest tiles; 'terrain_lods' = every level of the tree, finest first
        idx.levels.append(dict(name=_stem(name), archive=ai, clv=name, mesh=mesh, trs=trs,
                               terrain=lods[0] if lods else terrain_leaves(tiles.get(ai, [])),
                               terrain_lods=lods))
    idx.levels.sort(key=lambda l: l['name'])
    progress.stage = 'Building catalog'
    build_catalog(idx)
    idx.scan_seconds = time.time() - t0
    iso.close()
    return idx


def tree_lods(idx, tiles, levels):
    """The terrain tree's levels of detail as lists of (zi, ri) tiles, finest first. A level is
    kept only while every one of its pieces resolves to one of the level's terrain tiles, so
    each kept level covers the whole terrain; [] if not even the leaves resolve."""
    by_hash = {idx.zrbs[zi].records[ri][8]: (zi, ri) for zi, ri, *_ in tiles}
    out = []
    for hashes in levels or ():
        lv = [by_hash.get(h) for h in hashes]
        if not all(lv):
            break
        out.append(lv)
    return out


def terrain_leaves(tiles):
    """Finest level of the terrain LOD tree from tile footprints alone: tiles whose footprint
    contains no smaller tile. Fallback for a level without a terrain tree record; it misses
    tiles with trimmed bounds (12 on Canal)."""
    if not tiles:
        return []
    a = np.array([t[2:] for t in tiles], np.float64)  # cx, cz, ex, ez
    area = a[:, 2] * a[:, 3]
    parent = np.zeros(len(a), bool)
    for i in range(0, len(a), 512):  # chunked to bound memory
        blk = a[i:i + 512]
        inside = ((np.abs(a[None, :, 0] - blk[:, None, 0]) <= blk[:, None, 2] * 0.999) &
                  (np.abs(a[None, :, 1] - blk[:, None, 1]) <= blk[:, None, 3] * 0.999) &
                  (area[None, :] < area[i:i + 512, None] * 0.75))
        parent[i:i + 512] = inside.any(1)
    return [(t[0], t[1]) for t, p in zip(tiles, parent) if not p]


# -- catalog --------------------------------------------------------------------------------------
_PRETTY = re.compile(r'[_\s]+')


def _classify(zname, rtype):
    s = _stem(zname)
    if '/levels/' in zname:
        return 'PROP'
    if rtype == T_SKINNED_MODEL:
        return 'CHARACTER'
    if s.startswith('streamed_weapons'):
        return 'WEAPON'
    if s.startswith('streamed_attachments'):
        return 'ATTACHMENT'
    if s.startswith('vehicles'):
        return 'VEHICLE'
    if s.startswith(('streamed_gear', 'equipment', 'streamed_character_class', 'character_class')):
        return 'EQUIPMENT'
    return 'PROP'


def build_catalog(idx):
    idx.assets = []
    seen = set()
    for zi, z in enumerate(idx.zrbs):
        stem = _stem(z.name)
        area = z.name.split('/levels/')[1].split('/')[0] if '/levels/' in z.name else 'global'
        for ri, r in enumerate(z.records):
            rtype, name = r[7], r[8]
            if rtype in MODEL_TYPES:
                key = ('M', name, area)
                if key in seen:
                    continue
                seen.add(key)
                cat = _classify(z.name, rtype)
                lab = idx.labels.get(name)
                if not lab:
                    lab = stem.replace('streamed_', '').replace('vehicles_vehicle_', '') if cat in ('WEAPON', 'VEHICLE') else '%s_%08x' % (stem, name)
                    if cat in ('WEAPON', 'VEHICLE'):
                        lab = '%s_%08x' % (lab, name)
                detail = '%s  [%s]' % (stem, {T_MODEL: 'model', T_SKINNED_MODEL: 'skinned', T_LEVEL_MODEL: 'level piece',
                                              T_FOLIAGE_MODEL: 'foliage'}[rtype])
                if area != 'global':
                    detail = area + ' / ' + detail
                idx.assets.append(AssetEntry('%d:%d' % (zi, ri), cat, lab, detail, zi, ri))
            elif rtype == T_HAVOK:
                s = idx.havok.get((zi, ri))
                if not s:
                    continue
                for k, (skel, nf, dur) in enumerate(s['animations']):
                    asset = s.get('asset') or ''
                    nm = re.split(r'[\\/]', asset)[-1].rsplit('.', 1)[0] if asset else '%s_%08x' % (stem, name)
                    if len(s['animations']) > 1:
                        nm += '_%d' % k
                    idx.assets.append(AssetEntry('%d:%d:%d' % (zi, ri, k), 'ANIMATION', nm,
                                                 '%s  %d frames  %.2fs  (%s)' % (skel, nf, dur, stem), zi, ri, k))
    for li, lv in enumerate(idx.levels):
        n = len(lv['mesh'])
        idx.assets.append(AssetEntry('L%d' % li, 'MAP', lv['name'].replace('mag_', '').replace('_final', ''),
                                     '%d placed instances, %d terrain tiles' % (n, len(lv.get('terrain', ()))),
                                     -1, li))
    idx.assets.sort(key=lambda a: (a.category, a.label.lower()))


def skeleton_sources(idx):
    """skeleton name -> (zrb, rec) of a packfile that contains it."""
    out = {}
    for (zi, ri), s in idx.havok.items():
        for nm in s['skeletons']:
            out.setdefault(nm, (zi, ri))
    return out

