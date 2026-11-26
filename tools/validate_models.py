"""Decode every model in pickled {name: bytes} stores and report statistics.

usage: python tools/validate_models.py store.pkl [store2.pkl ...]
"""
import collections
import os
import pickle
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from mag_importer.core.zrb import Zrb, MODEL_TYPES  # noqa: E402
from mag_importer.core import models  # noqa: E402

stats = collections.Counter()
nlen = []
uvr = []
fails = []
for path in sys.argv[1:]:
    st = pickle.load(open(path, 'rb'))
    for name, d in st.items():
        if not name.endswith('.zrb') or name.endswith('headers.zrb'):
            continue
        z = Zrb(d)
        for r in z.records:
            if r.type not in MODEL_TYPES:
                continue
            try:
                m = models.parse_model(z.payload(r), r.type, r.name)
            except Exception as e:
                stats['parse_fail'] += 1
                continue
            stats['models'] += 1
            geoms = models.decode_model_geometry(z, r, m)
            for me, g in zip(m.meshes, geoms):
                kind = 'edge' if me.parts else 'rsx'
                if g is None:
                    stats[kind + '_fail'] += 1
                    if len(fails) < 10:
                        fails.append((name.split('/')[-1], hex(r.name), me.index, kind, me.V, me.K, hex(me.vformat)))
                    continue
                stats[kind + '_ok'] += 1
                if not np.isfinite(g.positions).all():
                    stats[kind + '_nonfinite'] += 1
                if len(g.indices) == 0:
                    stats[kind + '_noidx'] += 1
                if g.normals is not None:
                    nlen.append(np.abs(np.linalg.norm(g.normals, axis=1) - 1).mean())
                for uv in g.uvs[:1]:
                    uvr.append((float(uv.min()), float(uv.max())))
                if g.joints is not None:
                    stats['skinned'] += 1
                    stats['maxjoint_%d' % (int(g.joints.max()) // 10 * 10)] += 1
print(sorted(stats.items()))
print('normal |n|-1 mean over meshes: %.4f  worst %.4f' % (np.mean(nlen), np.max(nlen)))
u = np.array(uvr)
print('uv min range', np.percentile(u[:, 0], [1, 50, 99]), 'uv max', np.percentile(u[:, 1], [1, 50, 99]))
print('fails', fails)
