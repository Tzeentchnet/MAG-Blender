"""Scan the MAG ISO outside Blender and print catalog statistics.

usage: python tools/scan_cli.py "path/to/MAG (USA).dec.iso" [index_out.pkl]
"""
import collections
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from mag_importer.core import catalog  # noqa: E402

iso = sys.argv[1]
out = sys.argv[2] if len(sys.argv) > 2 else None
prog = catalog.ScanProgress()
res = {}


def run():
    res['idx'] = catalog.scan_iso(iso, prog)


t = threading.Thread(target=run)
t0 = time.time()
t.start()
while t.is_alive():
    t.join(2.0)
    print('  %-28s %5.1f%%  (%.0fs)' % (prog.stage, prog.fraction * 100, time.time() - t0), flush=True)
idx = res['idx']
print('scan %.1fs: %d archives, %d zrbs, %d records, %d labels, %d havok, %d levels' % (
    idx.scan_seconds, len(idx.archives), len(idx.zrbs), sum(len(z.records) for z in idx.zrbs),
    len(idx.labels), len(idx.havok), len(idx.levels)))
print(collections.Counter(a.category for a in idx.assets))
for cat in catalog.CATEGORIES:
    xs = [a for a in idx.assets if a.category == cat]
    print(cat, [(a.label, a.detail) for a in xs[:4]])
if out:
    t1 = time.time()
    idx.save(out)
    print('saved %s (%.1f MB) in %.2fs' % (out, os.path.getsize(out) / 1e6, time.time() - t1))
    t1 = time.time()
    idx2 = catalog.GameIndex.load(out)
    print('reloaded in %.2fs, %d assets' % (time.time() - t1, len(idx2.assets)))
