"""Helpers for scripts that read the disc outside Blender, using the importer's cached index.

The index is the one the add-on writes (``%APPDATA%/Blender Foundation/Blender/mag_importer/
mag_index.pkl``); pass ``--index`` to a script to use another one.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

from mag_importer.core import catalog, library  # noqa: E402
from mag_importer.core.zrb import Zrb  # noqa: E402

_BLENDER_USER = os.path.expandvars(r'%APPDATA%\Blender Foundation\Blender')
DEFAULT_CACHE = os.path.join(_BLENDER_USER, 'mag_importer')  # shared by all Blender versions
if not os.path.exists(os.path.join(DEFAULT_CACHE, 'mag_index.pkl')):  # not migrated yet
    DEFAULT_CACHE = os.path.join(_BLENDER_USER, '5.2', 'config', 'mag_importer')
DEFAULT_ISO = r'F:\Recomp_GameSource\MAG (USA)\MAG (USA).dec.iso'


def open_library(index_path=None, iso=None, cache=None):
    cache = cache or DEFAULT_CACHE
    idx = catalog.GameIndex.load(index_path or os.path.join(cache, 'mag_index.pkl'))
    return library.Library(idx, iso or idx.iso_path or DEFAULT_ISO, cache)


def archive_index(lib, level):
    """Archive index for a level name fragment ('baku') or 'main'."""
    want = level.upper()
    for ai, a in enumerate(lib.index.archives):
        stem = a[0].rsplit('/', 1)[-1]
        if want == 'MAIN' and stem == 'MAIN.PSARC':
            return ai
        if want != 'MAIN' and want in stem:
            return ai
    raise SystemExit('no archive matches %r' % level)


def level_entry(lib, level):
    ai = archive_index(lib, level)
    for lv in lib.index.levels:
        if lv['archive'] == ai:
            return lv
    raise SystemExit('no level layout in archive %d' % ai)


def full_zrb(lib, ai, name):
    """Parse a ZRB straight from its archive (every record, not just the indexed types)."""
    return Zrb(lib.archive(ai).read(name))


def zrb_names(lib, ai, suffix='.zrb'):
    return [e.name for e in lib.archive(ai).entries if e.name.endswith(suffix)]
