"""Minimal ISO9660 reader with Joliet support.

The decrypted MAG disc image is a plain ISO9660 volume. Level 1/2 names are truncated
(e.g. ``MAG_PANAMA_MILITARY_BASE_FINAL.``), so the Joliet tree is preferred when present.
"""
import os
import struct
import threading

from .errors import MagFormatError

SECTOR = 2048


class IsoFile:
    """Thread-safe random access to an ISO image (one handle per thread)."""

    def __init__(self, path):
        self.path = os.path.abspath(path)
        st = os.stat(self.path)
        self.size = st.st_size
        self.mtime = int(st.st_mtime)
        self._local = threading.local()
        self.files = {}  # upper-case path ('/PS3_GAME/USRDIR/...') -> (byte offset, size)
        self._read_tree()

    def _fh(self):
        fh = getattr(self._local, 'fh', None)
        if fh is None:
            fh = open(self.path, 'rb', buffering=0)
            self._local.fh = fh
        return fh

    def read(self, offset, size):
        fh = self._fh()
        fh.seek(offset)
        data = fh.read(size)
        if len(data) != size:
            raise MagFormatError('short read at %#x (+%d)' % (offset, size))
        return data

    def close(self):
        fh = getattr(self._local, 'fh', None)
        if fh is not None:
            fh.close()
            self._local.fh = None

    # -- directory tree ---------------------------------------------------------------
    def _read_tree(self):
        pvd_root = None
        joliet_root = None
        for sector in range(16, 64):
            vd = self.read(sector * SECTOR, SECTOR)
            if vd[1:6] != b'CD001':
                raise MagFormatError('not an ISO9660 image')
            kind = vd[0]
            if kind == 1 and pvd_root is None:
                pvd_root = vd[156:156 + 34]
            elif kind == 2 and vd[88:91] in (b'%/@', b'%/C', b'%/E'):
                joliet_root = vd[156:156 + 34]
            elif kind == 255:
                break
        root, joliet = (joliet_root, True) if joliet_root else (pvd_root, False)
        if root is None:
            raise MagFormatError('ISO has no primary volume descriptor')
        lba, size = struct.unpack_from('<I', root, 2)[0], struct.unpack_from('<I', root, 10)[0]
        self._walk(lba, size, '', joliet, set())

    def _walk(self, lba, size, path, joliet, seen):
        if lba in seen:
            return
        seen.add(lba)
        data = self.read(lba * SECTOR, size)
        i = 0
        while i < len(data):
            ln = data[i]
            if ln == 0:
                i = (i // SECTOR + 1) * SECTOR
                continue
            rec = data[i:i + ln]
            i += ln
            elba = struct.unpack_from('<I', rec, 2)[0]
            esz = struct.unpack_from('<I', rec, 10)[0]
            flags = rec[25]
            nl = rec[32]
            raw = rec[33:33 + nl]
            if raw in (b'\x00', b'\x01'):
                continue
            name = raw.decode('utf-16-be', 'replace') if joliet else raw.decode('latin1')
            name = name.split(';')[0].rstrip('.') if not (flags & 2) else name
            child = path + '/' + name
            if flags & 2:
                self._walk(elba, esz, child, joliet, seen)
            else:
                self.files[child.upper()] = (elba * SECTOR, esz)

    def find(self, suffix):
        suffix = suffix.upper()
        return sorted(p for p in self.files if p.endswith(suffix))
