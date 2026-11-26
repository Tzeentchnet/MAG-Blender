"""Decode every foliage batch of a level offline and report counts, timing and table matches.

python tools/foliage_check.py baku [canal ...]
"""
import sys
import time

import numpy as np

from _offline import open_library, level_entry
from mag_importer.core import foliage
from mag_importer.core.zrb import T_FOLIAGE_BATCH

lib = open_library()
for name in sys.argv[1:] or ['baku']:
    t0 = time.time()
    lv = level_entry(lib, name)
    ai = lv['archive']
    recs = [(zi, ri) for zi, z in enumerate(lib.index.zrbs) if z.archive == ai
            for ri, r in enumerate(z.records) if r[7] == T_FOLIAGE_BATCH]
    lib.prefetch([zi for zi, _ in recs])
    t1 = time.time()
    batches = [foliage.read_batch(*lib.record(zi, ri)) for zi, ri in recs]
    t2 = time.time()
    clv = lib.archive(ai).read(lv['clv'])
    table = foliage.batch_table(clv, batches[0].name) if batches else {}
    t3 = time.time()
    bad = sum(1 for b in batches if b.name in table and table[b.name][4] != b.count)
    placed, lost = foliage.level_foliage(batches, table)
    t4 = time.time()
    n = sum(len(p.scales) for p in placed.values())
    q = np.concatenate([p.quats for p in placed.values()]) if placed else np.zeros((0, 4))
    print('%s: %d batches, table %d, count mismatches %d, unplaced %d, %d models, %d instances'
          % (lv['name'], len(batches), len(table), bad, lost, len(placed), n))
    print('  |q| range %.4f..%.4f  read %.1fs decode %.2fs table %.2fs group %.2fs'
          % (np.linalg.norm(q, axis=1).min(), np.linalg.norm(q, axis=1).max(), t1 - t0, t2 - t1, t3 - t2, t4 - t3))
    for m, p in sorted(placed.items(), key=lambda kv: -len(kv[1].scales)):
        print('   %08x %-40s n=%6d fade %.0f-%.0f scale %.2f..%.2f' % (
            m, lib.index.label(m)[:40], len(p.scales), p.fade_start, p.fade_end, p.scales.min(), p.scales.max()))
