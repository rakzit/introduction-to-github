"""Lazy PDF document reader + incremental / compacting writer.

Design goals:
  * Large files: nothing is read up front except the cross-reference
    table.  Objects are parsed on demand from small file windows (no mmap,
    so files bigger than the 2 GB address space of 32-bit Windows XP work).
  * Edits are saved as *incremental updates* appended to the file, which is
    fast even for huge files and makes undo a simple truncate.
"""

import os
import re
import zlib
from collections import OrderedDict

from .objects import (Name, Ref, Stream, PdfError, serialize,
                      decode_text_string)
from .lexer import Lexer, Keyword, EndOfData
from .filters import decode_stream

_startxref_re = re.compile(rb"startxref\s+(\d+)")
_obj_header_re = re.compile(rb"\s*(\d+)\s+(\d+)\s+obj\b")
_scan_obj_re = re.compile(rb"(?<![0-9])(\d{1,10})[\x00\t\n\x0c\r ]+(\d{1,5})[\x00\t\n\x0c\r ]+obj\b")
_xref_entry_re = re.compile(rb"(\d{10})[ ](\d{5})[ ]([nf])")
_subsection_re = re.compile(rb"\s*(\d+)\s+(\d+)\s*")

CACHE_LIMIT = 20000


class EncryptedPdfError(PdfError):
    pass


def clone(obj):
    """Deep-copy a PDF object (stream data itself is shared, not copied)."""
    if isinstance(obj, dict):
        return dict((k, clone(v)) for k, v in obj.items())
    if isinstance(obj, list):
        return [clone(v) for v in obj]
    if isinstance(obj, Stream):
        return Stream(clone(obj.dict), obj._raw, obj._loader)
    return obj


def map_refs(obj, func):
    """Return a copy of ``obj`` with every Ref replaced by func(ref)."""
    if isinstance(obj, Ref):
        return func(obj)
    if isinstance(obj, dict):
        return dict((k, map_refs(v, func)) for k, v in obj.items())
    if isinstance(obj, list):
        return [map_refs(v, func) for v in obj]
    if isinstance(obj, Stream):
        return Stream(map_refs(obj.dict, func), obj._raw, obj._loader)
    return obj


class PdfDocument(object):
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self._f = open(self.path, "rb")
        self.changes = OrderedDict()
        self._next_num = None
        self.repaired = False
        self._load()

    # ---------------------------------------------------------------- basics

    def close(self):
        if self._f:
            self._f.close()
            self._f = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _load(self):
        self._f.seek(0, 2)
        self.file_size = self._f.tell()
        self.cache = {}
        self._objstm_cache = OrderedDict()
        self._pages = None
        self._page_index = None
        self.xref = {}
        self.trailer = {}
        self.xref_is_stream = False
        self.startxref = None
        try:
            self._load_xref_chain()
            if "Root" not in self.trailer:
                raise PdfError("trailer has no /Root")
            self.catalog()  # make sure the root resolves
        except Exception:
            self._rebuild_xref()
            self.repaired = True
        self.encrypted = "Encrypt" in self.trailer

    def reload(self):
        """Re-read the file after it was changed on disk."""
        self._f.close()
        self._f = open(self.path, "rb")
        self.changes = OrderedDict()
        self._next_num = None
        self._load()

    def read(self, offset, size):
        self._f.seek(offset)
        return self._f.read(size)

    # ------------------------------------------------------------ xref

    def _find_startxref(self):
        size = min(self.file_size, 4096)
        tail = self.read(self.file_size - size, size)
        matches = list(_startxref_re.finditer(tail))
        if not matches:
            raise PdfError("startxref not found")
        return int(matches[-1].group(1))

    def _load_xref_chain(self):
        offset = self._find_startxref()
        self.startxref = offset
        visited = set()
        first = True
        pending = [offset]
        while pending:
            off = pending.pop(0)
            if off in visited:
                continue
            visited.add(off)
            trailer, is_stream = self._read_xref_section(off)
            if first:
                self.xref_is_stream = is_stream
                first = False
            for k, v in trailer.items():
                if k not in self.trailer:
                    self.trailer[k] = v
            if "XRefStm" in trailer and isinstance(trailer["XRefStm"], int):
                pending.insert(0, trailer["XRefStm"])
            prev = trailer.get("Prev")
            if isinstance(prev, (int, float)):
                pending.append(int(prev))
        for k in ("Prev", "XRefStm", "Type", "W", "Index", "Filter",
                  "DecodeParms", "Length"):
            self.trailer.pop(k, None)

    def _read_xref_section(self, offset):
        head = self.read(offset, 32)
        if head.lstrip().startswith(b"xref"):
            return self._read_xref_table(offset), False
        return self._read_xref_stream(offset), True

    def _read_xref_table(self, offset):
        window = 1 << 16
        pos = offset + self.read(offset, 64).index(b"xref") + 4
        while True:
            chunk = self.read(pos, 128)
            m = _subsection_re.match(chunk)
            stripped = chunk.lstrip()
            if stripped.startswith(b"trailer"):
                pos += chunk.index(b"trailer") + 7
                break
            if not m:
                raise PdfError("bad xref table at %d" % pos)
            start, count = int(m.group(1)), int(m.group(2))
            pos += m.end()
            body = self.read(pos, count * 21 + 64)
            n = 0
            last_end = 0
            for em in _xref_entry_re.finditer(body):
                if n >= count:
                    break
                num = start + n
                n += 1
                last_end = em.end()
                if num in self.xref:
                    continue
                if em.group(3) == b"n":
                    off = int(em.group(1))
                    if off == 0:
                        continue
                    self.xref[num] = ("n", off, int(em.group(2)))
                else:
                    self.xref[num] = ("f", 0, int(em.group(2)))
            pos += last_end
        # parse trailer dictionary
        data = self.read(pos, window)
        while True:
            try:
                trailer = Lexer(data).parse_object()
                break
            except EndOfData:
                if len(data) < window:
                    raise
                window *= 4
                data = self.read(pos, window)
        if not isinstance(trailer, dict):
            raise PdfError("bad trailer")
        return trailer

    def _read_xref_stream(self, offset):
        num, gen, obj = self._parse_indirect_at(offset)
        if not isinstance(obj, Stream):
            raise PdfError("xref stream expected at %d" % offset)
        d = obj.dict
        data = decode_stream(d, obj.raw, self.resolve)
        w = [int(x) for x in d.get("W", [1, 2, 1])]
        size = int(d.get("Size", 0))
        index = d.get("Index") or [0, size]
        rowlen = sum(w)
        p = 0
        for i in range(0, len(index) - 1, 2):
            start, count = int(index[i]), int(index[i + 1])
            for j in range(count):
                if p + rowlen > len(data):
                    break
                fields = []
                q = p
                for width in w:
                    val = 0
                    for _ in range(width):
                        val = (val << 8) | data[q]
                        q += 1
                    fields.append(val)
                p += rowlen
                t = fields[0] if w[0] else 1
                objnum = start + j
                if objnum in self.xref:
                    continue
                if t == 1:
                    if fields[1]:
                        self.xref[objnum] = ("n", fields[1], fields[2] if len(fields) > 2 else 0)
                elif t == 2:
                    self.xref[objnum] = ("c", fields[1], fields[2])
                elif t == 0:
                    self.xref[objnum] = ("f", 0, 0)
        return dict(d)

    def _rebuild_xref(self):
        """Scan the whole file for 'N G obj' when the xref is damaged."""
        self.xref = {}
        self.trailer = {}
        self.cache = {}
        chunk = 1 << 20
        overlap = 64
        pos = 0
        trailer_positions = []
        while pos < self.file_size:
            data = self.read(pos, chunk + overlap)
            for m in _scan_obj_re.finditer(data):
                if m.start() >= chunk and pos + chunk < self.file_size:
                    break
                num, gen = int(m.group(1)), int(m.group(2))
                self.xref[num] = ("n", pos + m.start(), gen)
            idx = data.find(b"trailer")
            while 0 <= idx < chunk:
                trailer_positions.append(pos + idx)
                idx = data.find(b"trailer", idx + 1)
            pos += chunk
        for tp in reversed(trailer_positions):
            try:
                t = Lexer(self.read(tp + 7, 1 << 16)).parse_object()
                if isinstance(t, dict) and "Root" in t:
                    self.trailer = t
                    break
            except Exception:
                continue
        # objects inside object streams
        for num in list(self.xref):
            try:
                o = self.get(num)
            except Exception:
                continue
            if isinstance(o, Stream) and o.get("Type") == "ObjStm":
                try:
                    for i, onum in enumerate(self._objstm(num)[0]):
                        if onum not in self.xref:
                            self.xref[onum] = ("c", num, i)
                except Exception:
                    pass
            elif "Root" not in self.trailer and isinstance(o, dict) and o.get("Type") == "Catalog":
                self.trailer["Root"] = Ref(num, self.xref[num][2])
        if "Root" not in self.trailer:
            raise PdfError("not a PDF file, or damaged beyond repair")
        self.trailer["Size"] = max(self.xref) + 1 if self.xref else 1
        self.cache = {}

    # ------------------------------------------------------------ objects

    def _parse_indirect_at(self, offset):
        window = 1 << 16
        while True:
            data = self.read(offset, window)
            try:
                m = _obj_header_re.match(data)
                if not m:
                    raise PdfError("no object at offset %d" % offset)
                lx = Lexer(data, m.end())
                obj = lx.parse_object()
                num, gen = int(m.group(1)), int(m.group(2))
                if isinstance(obj, dict):
                    save = lx.pos
                    tok = lx.next_token()
                    if isinstance(tok, Keyword) and tok == "stream":
                        start = lx.pos
                        if data[start:start + 2] == b"\r\n":
                            start += 2
                        elif data[start:start + 1] in (b"\n", b"\r"):
                            start += 1
                        return num, gen, self._make_stream(obj, offset + start)
                    lx.pos = save
                return num, gen, obj
            except EndOfData:
                if len(data) < window:
                    raise
                window *= 4
                if window > (1 << 28):
                    raise PdfError("object at %d too large" % offset)

    def _make_stream(self, sdict, data_offset):
        length = sdict.get("Length")
        if isinstance(length, Ref):
            try:
                length = self.get(length)
            except Exception:
                length = None
        if isinstance(length, int) and length >= 0:
            check = self.read(data_offset + length, 32)
            if b"endstream" not in check:
                length = None
        if not isinstance(length, int):
            length = self._find_endstream(data_offset) - data_offset
        f = self

        def loader(off=data_offset, n=length):
            return f.read(off, n)

        if length < 4096:
            return Stream(sdict, self.read(data_offset, length))
        return Stream(sdict, None, loader)

    def _find_endstream(self, start):
        pos = start
        chunk = 1 << 20
        while pos < self.file_size:
            data = self.read(pos, chunk + 16)
            i = data.find(b"endstream")
            if i >= 0:
                end = pos + i
                tail = self.read(max(start, end - 2), end - max(start, end - 2))
                if tail.endswith(b"\r\n"):
                    end -= 2
                elif tail.endswith(b"\n") or tail.endswith(b"\r"):
                    end -= 1
                return end
            pos += chunk
        return self.file_size

    def _objstm(self, num):
        if num in self._objstm_cache:
            return self._objstm_cache[num]
        stm = self.get(num)
        if not isinstance(stm, Stream):
            raise PdfError("object stream %d missing" % num)
        data = decode_stream(stm.dict, stm.raw, self.resolve)
        n = int(stm.get("N", 0))
        first = int(stm.get("First", 0))
        lx = Lexer(data)
        nums, offs = [], []
        for _ in range(n):
            nums.append(lx.next_token())
            offs.append(lx.next_token())
        entry = (nums, offs, first, data)
        self._objstm_cache[num] = entry
        if len(self._objstm_cache) > 32:
            self._objstm_cache.popitem(last=False)
        return entry

    def get(self, ref):
        """Return the object for ``ref`` (Ref or object number)."""
        num = ref.num if isinstance(ref, Ref) else ref
        if num in self.changes:
            return self.changes[num]
        if num in self.cache:
            return self.cache[num]
        entry = self.xref.get(num)
        if entry is None or entry[0] == "f":
            return None
        if entry[0] == "n":
            onum, gen, obj = self._parse_indirect_at(entry[1])
        else:
            nums, offs, first, data = self._objstm(entry[1])
            idx = entry[2]
            if idx >= len(offs) or nums[idx] != num:
                try:
                    idx = nums.index(num)
                except ValueError:
                    return None
            obj = Lexer(data, first + offs[idx]).parse_object()
        if len(self.cache) > CACHE_LIMIT:
            self.cache.clear()
        self.cache[num] = obj
        return obj

    def resolve(self, obj):
        depth = 0
        while isinstance(obj, Ref) and depth < 32:
            obj = self.get(obj)
            depth += 1
        return obj

    def decode(self, stream):
        stream = self.resolve(stream)
        if not isinstance(stream, Stream):
            return b""
        return decode_stream(stream.dict, stream.raw, self.resolve)

    # ------------------------------------------------------------ editing

    def _ensure_next_num(self):
        if self._next_num is None:
            biggest = max(self.xref) if self.xref else 0
            size = self.trailer.get("Size", 0) or 0
            self._next_num = max(biggest + 1, int(size), 1)

    def add(self, obj):
        """Add a new indirect object; returns its Ref."""
        self._ensure_next_num()
        num = self._next_num
        self._next_num += 1
        self.changes[num] = obj
        return Ref(num, 0)

    def update(self, ref, obj):
        num = ref.num if isinstance(ref, Ref) else ref
        self.changes[num] = obj
        self.cache.pop(num, None)

    def editable(self, ref):
        """Return a private, modifiable copy of the object at ``ref``."""
        num = ref.num if isinstance(ref, Ref) else ref
        if num in self.changes:
            return self.changes[num]
        obj = clone(self.get(num))
        self.changes[num] = obj
        return obj

    def check_editable(self):
        if self.encrypted:
            raise EncryptedPdfError(
                "This PDF is encrypted; editing is not supported (viewing is).")

    def _gen_of(self, num):
        e = self.xref.get(num)
        if e and e[0] in ("n", "f"):
            return e[2] if e[0] == "n" else 0
        return 0

    def commit(self):
        """Append pending changes to the file as an incremental update."""
        if not self.changes:
            return False
        self.check_editable()
        self._f.close()
        self._f = None
        try:
            with open(self.path, "r+b") as out:
                out.seek(0, 2)
                pos = out.tell()
                buf = [b"\n"]
                pos += 1
                offsets = {}
                for num in sorted(self.changes):
                    obj = self.changes[num]
                    gen = self._gen_of(num)
                    body = (("%d %d obj\n" % (num, gen)).encode("ascii") +
                            serialize(obj) + b"\nendobj\n")
                    offsets[num] = (pos, gen)
                    buf.append(body)
                    pos += len(body)
                    if sum(len(b) for b in buf) > (1 << 20):
                        out.write(b"".join(buf))
                        buf = []
                out.write(b"".join(buf))
                size = max(int(self.trailer.get("Size", 0) or 0),
                           max(offsets) + 1)
                trailer = {}
                for key in ("Root", "Info", "ID"):
                    if key in self.trailer:
                        trailer[Name(key)] = self.trailer[key]
                if self.startxref is not None:
                    trailer[Name("Prev")] = self.startxref
                if self.xref_is_stream:
                    xnum = size
                    size += 1
                    offsets[xnum] = (pos, 0)
                    trailer[Name("Size")] = size
                    out.write(_xref_stream_bytes(xnum, offsets, trailer, pos))
                else:
                    trailer[Name("Size")] = size
                    out.write(_xref_table_bytes(offsets) + b"trailer\n" +
                              serialize(trailer) + b"\n")
                out.write(("startxref\n%d\n%%%%EOF\n" % pos).encode("ascii"))
        finally:
            self._f = open(self.path, "rb")
        self.changes = OrderedDict()
        self._next_num = None
        self._load()
        return True

    # ------------------------------------------------------------ structure

    def catalog(self):
        cat = self.resolve(self.trailer.get("Root"))
        if not isinstance(cat, dict):
            raise PdfError("document catalog missing")
        return cat

    def info(self):
        info = self.resolve(self.trailer.get("Info"))
        out = {}
        if isinstance(info, dict):
            for k, v in info.items():
                v = self.resolve(v)
                if isinstance(v, bytes):
                    out[str(k)] = decode_text_string(v)
        return out

    def page_refs(self):
        if self._pages is not None:
            return self._pages
        pages = []
        root = self.catalog().get("Pages")
        stack = [root]
        seen = set()
        while stack:
            node_ref = stack.pop()
            if not isinstance(node_ref, Ref) or node_ref.num in seen:
                continue
            seen.add(node_ref.num)
            node = self.get(node_ref)
            if not isinstance(node, dict):
                continue
            kids = self.resolve(node.get("Kids"))
            if node.get("Type") == "Pages" or (isinstance(kids, list) and node.get("Type") != "Page"):
                if isinstance(kids, list):
                    stack.extend(reversed(kids))
            else:
                pages.append(node_ref)
        self._pages = pages
        return pages

    def page_count(self):
        return len(self.page_refs())

    def page_number_of(self, ref):
        """0-based page index for a page Ref, or None."""
        if self._page_index is None:
            self._page_index = dict((r.num, i) for i, r in enumerate(self.page_refs()))
        if isinstance(ref, Ref):
            return self._page_index.get(ref.num)
        return None

    def inherited(self, page_ref, key):
        node = self.get(page_ref)
        depth = 0
        while isinstance(node, dict) and depth < 64:
            if key in node:
                return self.resolve(node[key])
            node = self.resolve(node.get("Parent"))
            depth += 1
        return None

    def page_box(self, index):
        """(x0, y0, x1, y1, rotate) of the visible box of page ``index``."""
        ref = self.page_refs()[index]
        box = self.inherited(ref, "CropBox") or self.inherited(ref, "MediaBox")
        if not isinstance(box, list) or len(box) < 4:
            box = [0, 0, 612, 792]
        box = [float(self.resolve(v) or 0) for v in box[:4]]
        x0, x1 = min(box[0], box[2]), max(box[0], box[2])
        y0, y1 = min(box[1], box[3]), max(box[1], box[3])
        rot = self.inherited(ref, "Rotate") or 0
        try:
            rot = int(rot) % 360
        except (TypeError, ValueError):
            rot = 0
        return x0, y0, x1, y1, rot

    def page_size(self, index):
        """Displayed (width, height) in points, honoring /Rotate."""
        x0, y0, x1, y1, rot = self.page_box(index)
        w, h = x1 - x0, y1 - y0
        if rot in (90, 270):
            return h, w
        return w, h

    def invalidate_pages(self):
        self._pages = None
        self._page_index = None

    # ------------------------------------------------------- full rewrite

    def write_compact(self, out_path, page_indices=None, drop_outline=False):
        """Write a fresh, compacted file (optionally only some pages).

        Objects are streamed one by one, so memory stays low for big files.
        """
        self.check_editable()
        all_pages = self.page_refs()
        overrides = {}
        excluded = set()
        catalog_ref = self.trailer["Root"]
        catalog = clone(self.catalog())
        if page_indices is not None:
            # collect page tree nodes so they are never dragged along
            stack = [self.catalog().get("Pages")]
            while stack:
                r = stack.pop()
                if not isinstance(r, Ref) or r.num in excluded:
                    continue
                excluded.add(r.num)
                node = self.get(r)
                if isinstance(node, dict):
                    kids = self.resolve(node.get("Kids"))
                    if isinstance(kids, list):
                        stack.extend(kids)
            self._ensure_next_num()
            root_num = self._next_num + 1
            keep = [all_pages[i] for i in page_indices]
            for r in keep:
                excluded.discard(r.num)
                p = clone(self.get(r))
                for key in ("Resources", "MediaBox", "CropBox", "Rotate"):
                    if key not in p:
                        v = self.inherited(r, key)
                        if v is not None:
                            p[Name(key)] = clone(v)
                p[Name("Parent")] = Ref(root_num, 0)
                overrides[r.num] = p
            # a page selected twice would need a copy; keep first occurrence
            uniq = []
            seen = set()
            for r in keep:
                if r.num not in seen:
                    seen.add(r.num)
                    uniq.append(r)
            overrides[root_num] = {Name("Type"): Name("Pages"), Name("Kids"): uniq,
                                   Name("Count"): len(uniq)}
            catalog[Name("Pages")] = Ref(root_num, 0)
            for key in ("StructTreeRoot", "MarkInfo", "PageLabels"):
                catalog.pop(key, None)
            drop_outline = True
        if drop_outline:
            catalog.pop("Outlines", None)
            names = self.resolve(catalog.get("Names"))
            if isinstance(names, dict) and "Dests" in names:
                names = clone(names)
                names.pop("Dests", None)
                catalog[Name("Names")] = names
            catalog.pop("Dests", None)
            if catalog.get("PageMode") == "UseOutlines":
                catalog.pop("PageMode", None)
        overrides[catalog_ref.num] = catalog

        newnum = {}
        order = []

        def mapper(ref):
            if ref.num in excluded:
                return None
            if ref.num not in newnum:
                if ref.num not in overrides and self.get(ref) is None:
                    return None
                newnum[ref.num] = len(newnum) + 1
                order.append(ref.num)
            return Ref(newnum[ref.num], 0)

        root_new = mapper(catalog_ref)
        info_new = None
        if isinstance(self.trailer.get("Info"), Ref):
            info_new = mapper(self.trailer["Info"])
        tmp = out_path + ".part"
        offsets = {}
        with open(tmp, "wb") as out:
            out.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
            i = 0
            while i < len(order):
                old = order[i]
                i += 1
                obj = overrides[old] if old in overrides else self.get(old)
                obj = map_refs(obj, mapper)
                if isinstance(obj, Stream) and obj.get("Type") == "XRef":
                    obj = None
                num = newnum[old]
                offsets[num] = (out.tell(), 0)
                out.write(("%d 0 obj\n" % num).encode("ascii"))
                out.write(serialize(obj))
                out.write(b"\nendobj\n")
                cached = self.cache.get(old)
                if isinstance(cached, Stream) and cached._loader is not None:
                    cached._raw = None  # let big stream data be freed
            xpos = out.tell()
            trailer = {Name("Size"): len(newnum) + 1, Name("Root"): root_new}
            if info_new is not None:
                trailer[Name("Info")] = info_new
            if "ID" in self.trailer:
                trailer[Name("ID")] = self.trailer["ID"]
            out.write(_xref_table_bytes(offsets, full=True) + b"trailer\n" +
                      serialize(trailer) + b"\n")
            out.write(("startxref\n%d\n%%%%EOF\n" % xpos).encode("ascii"))
        _replace(tmp, out_path)


def _replace(src, dst):
    if os.path.exists(dst):
        os.remove(dst)
    os.rename(src, dst)


def _runs(nums):
    nums = sorted(nums)
    runs = []
    for n in nums:
        if runs and runs[-1][0] + runs[-1][1] == n:
            runs[-1][1] += 1
        else:
            runs.append([n, 1])
    return runs


def _xref_table_bytes(offsets, full=False):
    out = [b"xref\n"]
    if full:
        n = max(offsets) + 1 if offsets else 1
        out.append(("0 %d\n" % n).encode("ascii"))
        out.append(b"0000000000 65535 f\r\n")
        for num in range(1, n):
            if num in offsets:
                out.append(("%010d %05d n\r\n" % offsets[num]).encode("ascii"))
            else:
                out.append(b"0000000000 00001 f\r\n")
        return b"".join(out)
    for start, count in _runs(offsets):
        out.append(("%d %d\n" % (start, count)).encode("ascii"))
        for num in range(start, start + count):
            out.append(("%010d %05d n\r\n" % offsets[num]).encode("ascii"))
    return b"".join(out)


def _xref_stream_bytes(xnum, offsets, trailer, xpos):
    width = 4 if xpos < (1 << 32) else 8
    rows = []
    index = []
    for start, count in _runs(offsets):
        index.extend([start, count])
        for num in range(start, start + count):
            off, gen = offsets[num]
            rows.append(b"\x01" + off.to_bytes(width, "big") + (gen & 0xffff).to_bytes(2, "big"))
    data = zlib.compress(b"".join(rows))
    d = dict(trailer)
    d[Name("Type")] = Name("XRef")
    d[Name("W")] = [1, width, 2]
    d[Name("Index")] = index
    d[Name("Filter")] = Name("FlateDecode")
    d[Name("Length")] = len(data)
    return (("%d 0 obj\n" % xnum).encode("ascii") + serialize(d) +
            b"\nstream\n" + data + b"\nendstream\nendobj\n")
