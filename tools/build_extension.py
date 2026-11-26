"""Build an installable zip of the add-on (works as a Blender 4.2+ extension and as a legacy add-on).

usage: python tools/build_extension.py [out_dir]
"""
import os
import sys
import zipfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
PKG = os.path.join(ROOT, 'mag_importer')
out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'dist')
os.makedirs(out_dir, exist_ok=True)
version = '1.0.0'
for line in open(os.path.join(PKG, 'blender_manifest.toml'), encoding='utf-8'):
    if line.startswith('version'):
        version = line.split('=')[1].strip().strip('"')
out = os.path.join(out_dir, 'mag_importer-%s.zip' % version)
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:
    for base, dirs, files in os.walk(PKG):
        dirs[:] = [d for d in dirs if d != '__pycache__']
        for f in files:
            if f.endswith(('.pyc', '.pyo')):
                continue
            full = os.path.join(base, f)
            # extension layout: manifest at the zip root
            zf.write(full, os.path.relpath(full, PKG))
print(out)
