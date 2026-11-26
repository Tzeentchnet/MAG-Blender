"""Shared helpers for the Blender-side test scripts."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))


def ensure_index(iso):
    """Register the add-on and make sure an index is loaded (scanning if the cache is stale)."""
    import mag_importer
    mag_importer.register()
    from mag_importer.blender import state
    from mag_importer.blender.state import S
    state.save_settings(iso_path=iso)
    if not state.try_load_cached(iso):
        print('TEST: scanning (%s)' % S.status)
        state.start_scan(iso)
        S.scan_thread.join()
        assert S.scan_result is not None, S.scan_error
        state.set_index(S.scan_result, iso)
    return S
