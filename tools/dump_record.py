"""Hex-dump ZRB records straight from the disc.

usage:
  python tools/dump_record.py <level|main> <zrb name fragment> [--list]
  python tools/dump_record.py <level|main> <zrb name fragment> [--type HASH] [--name HASH]
                              [--seg 1,3,4] [--bytes N]

Every record type is shown, not only the ones the scanner keeps. ``--list`` prints the record
table; otherwise each matching record's payload is dumped, followed by the first ``--bytes`` of
its data in each requested segment (the span runs to the next record's offset in that segment).

  python tools/dump_record.py baku globaldata --list
  python tools/dump_record.py baku globaldata --type 284C2F78 --seg 1,3,4
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _offline  # noqa: E402


def hexdump(b, base=0, width=16):
    out = []
    for i in range(0, len(b), width):
        chunk = b[i:i + width]
        hx = ' '.join('%02x' % c for c in chunk)
        asc = ''.join(chr(c) if 32 <= c < 127 else '.' for c in chunk)
        out.append('  %06x  %-*s  %s' % (base + i, width * 3 - 1, hx, asc))
    return '\n'.join(out)


def seg_spans(z, seg):
    """record -> (start, end) of its data in segment ``seg`` (end = next larger offset)."""
    attr = 'seg%d' % seg
    offs = sorted({getattr(r, attr) for r in z.records} | {z.seg_size[seg]})
    spans = {}
    for r in z.records:
        o = getattr(r, attr)
        nxt = next((x for x in offs if x > o), z.seg_size[seg])
        spans[id(r)] = (o, nxt)
    return spans


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('level')
    ap.add_argument('zrb')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('--type')
    ap.add_argument('--name')
    ap.add_argument('--seg', default='')
    ap.add_argument('--bytes', type=int, default=256)
    ap.add_argument('--index')
    a = ap.parse_args()
    lib = _offline.open_library(a.index)
    ai = _offline.archive_index(lib, a.level)
    names = [n for n in _offline.zrb_names(lib, ai) if a.zrb.lower() in n.lower()]
    if not names:
        raise SystemExit('no ZRB matches %r' % a.zrb)
    want_t = int(a.type, 16) if a.type else None
    want_n = int(a.name, 16) if a.name else None
    segs = [int(s) for s in a.seg.split(',') if s]
    for zn in names[:20]:
        z = _offline.full_zrb(lib, ai, zn)
        print('== %s  segs %s  sizes %s  types %d  records %d' % (
            zn, z.seg_start[1:], z.seg_size, len(z.types), len(z.records)))
        if a.list:
            for r in z.records:
                print('  %08x %08x kind %08x  payload %5d  seg1 %8x seg3 %8x seg4 %8x' % (
                    r.type, r.name, r.kind, r.end - r.payload_off, r.seg1, r.seg3, r.seg4))
            continue
        spans = {s: seg_spans(z, s) for s in segs}
        for r in z.records:
            if want_t is not None and r.type != want_t:
                continue
            if want_n is not None and r.name != want_n:
                continue
            p = z.payload(r)
            print('-- type %08x name %08x kind %08x payload %d bytes' % (r.type, r.name, r.kind, len(p)))
            print(hexdump(p[:max(a.bytes, 0x100)]))
            for s in segs:
                o, e = spans[s][id(r)]
                if e <= o:
                    continue
                print('   seg%d +%x .. +%x (%d bytes)' % (s, o, e, e - o))
                b = z.data[z.seg_start[s] + o:z.seg_start[s] + min(e, o + a.bytes)]
                print(hexdump(b, o))


if __name__ == '__main__':
    main()
