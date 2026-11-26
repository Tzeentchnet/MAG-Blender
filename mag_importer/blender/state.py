"""Session state: the loaded game index, resource library and background scan.

The cache lives in Blender's per-user folder but outside the per-version ones
(``.../Blender/mag_importer``), so it survives Blender restarts, upgrades, "Load Factory Settings"
and re-installing the add-on. The ISO path is kept there too (settings.json) so the index reloads
automatically on startup.
"""
import json
import os
import re
import shutil
import threading
import time
import traceback
import types

import bpy

from ..core import catalog
from ..core.library import Library

CACHE_NAME = 'mag_importer'
INDEX_FILE = 'mag_index.pkl'
SETTINGS_FILE = 'settings.json'


class _State:
    index = None
    library = None
    scan_thread = None
    scan_progress = None
    scan_error = ''
    scan_result = None
    status = ''


S = _State()


def addon_prefs():
    pkg = __package__.rsplit('.', 1)[0]
    addon = bpy.context.preferences.addons.get(pkg)
    return addon.preferences if addon else None


_VERSION_DIR = re.compile(r'\d+\.\d+$')
_migrated = False


def default_cache_dir():
    """``<Blender user folder>/mag_importer``, next to the per-version folders (``.../Blender/5.2``)
    rather than inside one, so a Blender upgrade keeps the index and textures. Falls back to the
    versioned config folder when the user folder is not laid out that way (portable installs)."""
    parent, ver = os.path.split(os.path.normpath(bpy.utils.resource_path('USER')))
    if _VERSION_DIR.match(ver) and parent:
        return os.path.join(parent, CACHE_NAME)
    return bpy.utils.user_resource('CONFIG', path=CACHE_NAME, create=True)


def _migrate_versioned_cache(dest):
    """Move the newest cache older add-on versions kept in ``.../Blender/<ver>/config/mag_importer``
    into ``dest`` (once, only while ``dest`` holds no index)."""
    if os.path.exists(os.path.join(dest, INDEX_FILE)):
        return
    parent = os.path.dirname(dest)
    best, best_t = None, -1.0
    try:
        versions = [v for v in os.listdir(parent) if _VERSION_DIR.match(v)]
    except OSError:
        return
    for v in versions:
        old = os.path.join(parent, v, 'config', CACHE_NAME)
        try:
            t = os.path.getmtime(os.path.join(old, INDEX_FILE))
        except OSError:
            continue
        if t > best_t:
            best, best_t = old, t
    if best is None:
        return
    moved = 0
    for name in os.listdir(best):
        src, dst = os.path.join(best, name), os.path.join(dest, name)
        if os.path.exists(dst):
            continue
        try:
            shutil.move(src, dst)
            moved += 1
        except OSError as e:
            print('MAG: could not move %s to the shared cache: %s' % (src, e))
    print('MAG: moved the cache from %s to %s (%d entries)' % (best, dest, moved))
    try:
        os.rmdir(best)  # only succeeds when everything moved
    except OSError:
        pass


def cache_dir():
    global _migrated
    prefs = addon_prefs()
    custom = getattr(prefs, 'cache_dir', '') if prefs else ''
    if custom:
        path = bpy.path.abspath(custom)
    else:
        path = default_cache_dir()
    os.makedirs(path, exist_ok=True)
    if not custom and not _migrated:
        _migrated = True
        try:
            _migrate_versioned_cache(path)
        except Exception:
            traceback.print_exc()
    return path


_TEXTURE_FILE = re.compile(r'[0-9a-f]{8}\.dds$')


def is_mag_texture(img):
    """A file image loaded from the add-on's texture cache (``<8 hex digits>.dds``)."""
    if img.source != 'FILE' or not img.filepath:
        return False
    return bool(_TEXTURE_FILE.match(os.path.basename(img.filepath.replace('\\', '/'))))


def relink_textures():
    """Point MAG textures whose file is gone (cache moved, e.g. by the move out of the versioned
    folder, or Cache Folder changed) at the current cache. Returns the number relinked."""
    tex = os.path.join(cache_dir(), 'textures')
    n = 0
    for img in bpy.data.images:
        if not is_mag_texture(img) or img.packed_file is not None:
            continue
        if os.path.exists(bpy.path.abspath(img.filepath)):
            continue
        base = os.path.basename(img.filepath.replace('\\', '/'))
        new = os.path.join(tex, base)
        if os.path.exists(new):
            img.filepath = new
            n += 1
    return n


def load_settings():
    try:
        with open(os.path.join(cache_dir(), SETTINGS_FILE), 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(**kw):
    s = load_settings()
    s.update(kw)
    path = os.path.join(cache_dir(), SETTINGS_FILE)
    with open(path + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(s, f, indent=1)
    os.replace(path + '.tmp', path)


# Import options kept in the add-on preferences. They are mirrored into settings.json so they
# survive even when Blender's preferences are not saved (auto-save off, factory reset, reinstall).
IMPORT_PREFS = {
    'import_collision': False,
    'import_lightmaps': True,
    'import_lower_lods': False,
    'pack_textures': False,
}
# stand-in when the add-on is not enabled as such (test scripts register it by hand)
_fallback_prefs = types.SimpleNamespace(**IMPORT_PREFS)


def import_prefs():
    """Object carrying the IMPORT_PREFS attributes: the add-on preferences, or a stand-in."""
    return addon_prefs() or _fallback_prefs


def save_import_prefs():
    prefs = addon_prefs()
    if prefs is not None:
        save_settings(**{k: bool(getattr(prefs, k)) for k in IMPORT_PREFS})


def restore_import_prefs():
    """Apply the import options saved in settings.json to the add-on preferences."""
    prefs = addon_prefs()
    if prefs is None:
        return
    saved = load_settings()
    for k in IMPORT_PREFS:
        if k in saved and bool(saved[k]) != bool(getattr(prefs, k)):
            setattr(prefs, k, bool(saved[k]))


def iso_path():
    prefs = addon_prefs()
    p = getattr(prefs, 'iso_path', '') if prefs else ''
    return bpy.path.abspath(p) if p else load_settings().get('iso_path', '')


def index_path():
    return os.path.join(cache_dir(), INDEX_FILE)


def set_index(idx, iso):
    if S.library is not None:
        S.library.close()
    S.index = idx
    S.library = Library(idx, iso, cache_dir()) if idx is not None else None


def try_load_cached(iso=None):
    """Load the persisted index if it matches the ISO. Returns True on success."""
    iso = iso or iso_path()
    p = index_path()
    if not iso or not os.path.exists(p) or not os.path.exists(iso):
        return False
    if S.index is not None and S.index.matches_iso(iso):
        return True  # already loaded
    if scanning():
        return False  # the running scan rewrites the index file
    try:
        idx = catalog.GameIndex.load(p)
    except Exception as e:  # stale/corrupt cache
        S.status = 'Cached index unusable (%s); rescan needed' % e
        return False
    if not idx.matches_iso(iso):
        S.status = 'Cached index belongs to a different ISO; rescan needed'
        return False
    set_index(idx, iso)
    S.status = 'Index loaded from cache (%d assets)' % len(idx.assets)
    return True


def start_scan(iso):
    if S.scan_thread is not None and S.scan_thread.is_alive():
        return False
    S.scan_progress = catalog.ScanProgress()
    S.scan_error = ''
    S.scan_result = None
    out = index_path()

    def run():
        try:
            t0 = time.time()
            idx = catalog.scan_iso(iso, S.scan_progress)
            idx.save(out)
            S.scan_result = idx
            S.status = 'Scanned %d resource blocks in %.1fs' % (len(idx.zrbs), time.time() - t0)
        except Exception as e:
            S.scan_error = '%s' % e
            traceback.print_exc()

    S.scan_thread = threading.Thread(target=run, name='MAG ISO scan', daemon=True)
    S.scan_thread.start()
    return True


def scanning():
    return S.scan_thread is not None and S.scan_thread.is_alive()
