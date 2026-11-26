"""Session state: the loaded game index, resource library and background scan.

The cache lives in Blender's per-user CONFIG directory (``.../Blender/<ver>/config/mag_importer``),
which survives Blender restarts, "Load Factory Settings" and re-installing the add-on. The ISO path
is kept there too (settings.json) so the index reloads automatically on startup.
"""
import json
import os
import threading
import time
import traceback

import bpy

from ..core import catalog
from ..core.library import Library

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


def cache_dir():
    prefs = addon_prefs()
    custom = getattr(prefs, 'cache_dir', '') if prefs else ''
    if custom:
        path = bpy.path.abspath(custom)
    else:
        path = bpy.utils.user_resource('CONFIG', path='mag_importer', create=True)
    os.makedirs(path, exist_ok=True)
    return path


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
