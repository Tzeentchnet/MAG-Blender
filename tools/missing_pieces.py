"""Find out why some map placements point at pieces the index does not have.

usage: python tools/missing_pieces.py [level ...] [--no-search]

For each level, lists every `Mesh` hash that resolves to no indexed model record, with its
placement count, sample transforms and the other fields of one of its `InstanceProperties`
objects. Then reads the record table of every ZRB on the disc (headers.zrb included, every record
type) and reports where each missing hash appears as a record name, and which segment 0 blobs
mention it anywhere as a big-endian u32.
"""
import collections
import os
import struct
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _offline  # noqa: E402
from mag_importer.core import props  # noqa: E402
from mag_importer.core.catalog import MODEL_TYPES  # noqa: E402
from mag_importer.core.zrb import Zrb, header_span  # noqa: E402


def missing_for_level(lib, lv):
    found = {}
    out = collections.OrderedDict()
    for i, h in enumerate(lv['mesh']):
        h = int(h)
        if h not in found:
            found[h] = lib.index.find(h, MODEL_TYPES) is not None
        if not found[h]:
            out.setdefault(h, []).append(i)
    return out


def describe_objects(lib, lv, hashes):
    """One InstanceProperties object per missing hash, with all its fields."""
    data = lib.archive(lv['archive']).read(lv['clv'])
    seen = {}
    for obj in props.iter_objects(data, type_name='InstanceProperties'):
        h = obj.token('Mesh')
        if h in hashes and h not in seen:
            seen[h] = obj
    return seen


def fmt_field(typ, val):
    if typ == 'float' and len(val) == 4:
        return '%g' % struct.unpack('>f', val)[0]
    if typ == 'util::Token' and len(val) >= 4:
        return '%08x' % struct.unpack('>I', val[:4])[0]
    if typ == 'ztl::string' and len(val) >= 4:
        return repr(val[4:].split(b'\0')[0].decode('latin1'))
    return val[:24].hex() + ('…' if len(val) > 24 else '')


def search_disc(lib, hashes):
    """Every record named by one of ``hashes`` and every segment 0 that mentions one."""
    needles = {h: struct.pack('>I', h) for h in hashes}
    jobs = [(ai, e.name, e.length) for ai in range(len(lib.index.archives))
            for e in lib.archive(ai).entries if e.name.endswith('.zrb')]

    def scan(job):
        ai, name, length = job
        arc = lib.archive(ai)
        try:
            head = arc.read(name, 0, min(length, 0x10000))
            span = header_span(head)
            data = head if span <= len(head) else arc.read(name, 0, span)
            z = Zrb(data, full=False)
        except Exception as e:  # noqa: BLE001 - diagnostic: report and go on
            return [('error', ai, name, str(e))]
        hits = []
        for r in z.records:
            if r.name in needles:
                hits.append(('record', ai, name, r.name, r.type, r.kind))
        for h, nd in needles.items():
            o = data.find(nd)
            while o >= 0:
                hits.append(('bytes', ai, name, h, o))
                o = data.find(nd, o + 1)
        return hits

    out = []
    with ThreadPoolExecutor(max_workers=16) as ex:
        for hs in ex.map(scan, jobs):
            out.extend(hs)
    return out, len(jobs)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    lib = _offline.open_library()
    levels = [l for l in lib.index.levels if not args or any(a in l['name'] for a in args)]
    all_missing = {}
    for lv in levels:
        miss = missing_for_level(lib, lv)
        objs = describe_objects(lib, lv, set(miss))
        print('== %s: %d placements, %d missing (%d unique)' % (
            lv['name'], len(lv['mesh']), sum(len(v) for v in miss.values()), len(miss)))
        for h, ids in miss.items():
            all_missing.setdefault(h, []).append((lv['name'], len(ids)))
            label = lib.index.labels.get(h, '')
            print('  %08x  x%-3d %s' % (h, len(ids), label))
            for i in ids[:3]:
                print('      trs', ' '.join('%.2f' % v for v in lv['trs'][i]))
            o = objs.get(h)
            if o is not None:
                print('      fields', ', '.join('%s=%s' % (k, fmt_field(t, v)) for k, (t, v) in o.fields.items()
                                                if not k.startswith(('Translate', 'Rotate', 'Scale'))))
    shared = {h: v for h, v in all_missing.items() if len(v) > 1}
    print('\n%d unique missing hashes; %d appear in more than one level' % (len(all_missing), len(shared)))
    for h, v in shared.items():
        print('  %08x %s' % (h, v))
    if '--no-search' in sys.argv:
        return
    hits, n = search_disc(lib, set(all_missing))
    print('\nsearched the record tables of %d ZRBs' % n)
    names = lambda ai: lib.index.archives[ai][0].rsplit('/', 1)[-1]  # noqa: E731
    by_hash = collections.defaultdict(list)
    for h in hits:
        if h[0] == 'error':
            print('  read error', names(h[1]), h[2], h[3])
        else:
            by_hash[h[3]].append(h)
    for h in all_missing:
        hs = by_hash.get(h, [])
        recs = [x for x in hs if x[0] == 'record']
        refs = [x for x in hs if x[0] == 'bytes']
        print('  %08x  records: %s' % (h, ', '.join('%08x in %s:%s' % (x[4], names(x[1]), x[2].rsplit('/', 1)[-1])
                                                       for x in recs) or 'none'))
        zs = collections.Counter('%s:%s' % (names(x[1]), x[2].rsplit('/', 1)[-1]) for x in refs)
        if zs:
            print('            mentioned in %d ZRBs: %s' % (len(zs), ', '.join('%s(%d)' % kv for kv in zs.most_common(6))))


if __name__ == '__main__':
    main()
