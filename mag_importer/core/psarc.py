"""PSARC 1.3 (zlib) archive reader over an ISO byte range.

Supports partial extraction: only the 64 KiB blocks covering a requested byte range are read
and inflated, which keeps indexing fast (a ZRB's record table lives in its first blocks).
"""
import struct
import zlib

from .errors import MagFormatError


class PsarcEntry:
    __slots__ = ('name', 'block', 'length', 'offset')

    def __init__(self, name, block, length, offset):
        self.name = name
        self.block = block
        self.length = length
        self.offset = offset

    def to_tuple(self):
        return (self.name, self.block, self.length, self.offset)


class Psarc:
    def __init__(self, iso, base, size, toc=None):
        """``iso``: object with read(offset, size). ``base``: byte offset of the archive in the ISO.
        ``toc``: cached (block_size, sizes, entries) to skip re-reading the table of contents."""
        self.iso = iso
        self.base = base
        self.size = size
        if toc is not None:
            self.block_size, self.sizes, ents = toc
            self.entries = [PsarcEntry(*e) for e in ents]
        else:
            self._read_toc()
        self.by_name = {e.name: e for e in self.entries}

    def toc(self):
        return (self.block_size, self.sizes, [e.to_tuple() for e in self.entries])

    def _read_toc(self):
        hdr = self.iso.read(self.base, 32)
        magic, ver, comp, toc_len, esz, count, bs, flags = struct.unpack('>4sI4sIIIII', hdr)
        if magic != b'PSAR' or comp != b'zlib':
            raise MagFormatError('not a zlib PSARC archive')
        if esz != 30:
            raise MagFormatError('unsupported PSARC entry size %d' % esz)
        d = self.iso.read(self.base, toc_len)
        width = ((bs - 1).bit_length() + 7) // 8
        start = 32 + esz * count
        self.block_size = bs
        self.sizes = [int.from_bytes(d[x:x + width], 'big') for x in range(start, toc_len - width + 1, width)]
        raw = []
        for i in range(count):
            e = d[32 + esz * i:32 + esz * (i + 1)]
            raw.append((int.from_bytes(e[16:20], 'big'), int.from_bytes(e[20:25], 'big'),
                        int.from_bytes(e[25:30], 'big')))
        self.entries = []
        names_entry = PsarcEntry('', *raw[0])
        names = self._extract(names_entry).decode('utf-8', 'replace').splitlines()
        for name, (blk, ln, off) in zip(names, raw[1:]):
            self.entries.append(PsarcEntry(name.lower(), blk, ln, off))

    def _block_iter(self, entry, first_block, last_block):
        """Yield decompressed blocks first_block..last_block (inclusive, relative to the entry)."""
        bs = self.block_size
        off = entry.offset
        for b in range(first_block):
            off += self.sizes[entry.block + b] or bs
        # read the compressed span in one go
        span = 0
        for b in range(first_block, last_block + 1):
            span += self.sizes[entry.block + b] or bs
        raw = self.iso.read(self.base + off, span)
        pos = 0
        for b in range(first_block, last_block + 1):
            n = self.sizes[entry.block + b] or bs
            chunk = raw[pos:pos + n]
            pos += n
            expect = min(bs, entry.length - b * bs)
            if n == expect:
                out = chunk  # stored uncompressed
            else:
                try:
                    out = zlib.decompress(chunk)
                except zlib.error:
                    out = chunk
            yield out

    def _extract(self, entry, start=0, length=None):
        if length is None:
            length = entry.length - start
        length = max(0, min(length, entry.length - start))
        if length == 0:
            return b''
        bs = self.block_size
        fb = start // bs
        lb = (start + length - 1) // bs
        data = b''.join(self._block_iter(entry, fb, lb))
        s = start - fb * bs
        return data[s:s + length]

    def read(self, name, start=0, length=None):
        e = self.by_name.get(name.lower())
        if e is None:
            raise KeyError(name)
        return self._extract(e, start, length)

    def names(self):
        return [e.name for e in self.entries]
