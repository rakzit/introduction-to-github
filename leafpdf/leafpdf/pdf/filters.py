"""Stream filters (decoding only; encoding is always FlateDecode)."""

import binascii
import zlib

from .objects import PdfError


class UnsupportedFilter(PdfError):
    pass


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def flate_decode(data):
    try:
        return zlib.decompress(data)
    except zlib.error:
        # Tolerate truncated / slightly corrupt streams.
        d = zlib.decompressobj()
        out = []
        try:
            out.append(d.decompress(data))
        except zlib.error:
            try:
                d = zlib.decompressobj(-15)
                out = [d.decompress(data[2:])]
            except zlib.error:
                pass
        return b"".join(out)


def apply_predictor(data, parms):
    if not parms:
        return data
    predictor = parms.get("Predictor", 1)
    if predictor in (None, 1):
        return data
    colors = parms.get("Colors", 1) or 1
    bpc = parms.get("BitsPerComponent", 8) or 8
    columns = parms.get("Columns", 1) or 1
    bpp = max(1, (colors * bpc + 7) // 8)
    rowlen = (colors * bpc * columns + 7) // 8
    if predictor == 2:  # TIFF, 8-bit only
        out = bytearray(data)
        for start in range(0, len(out), rowlen):
            for i in range(start + bpp, min(start + rowlen, len(out))):
                out[i] = (out[i] + out[i - bpp]) & 0xff
        return bytes(out)
    # PNG predictors (10..15): each row prefixed with a filter type byte.
    out = bytearray()
    prev = bytearray(rowlen)
    stride = rowlen + 1
    for start in range(0, len(data), stride):
        ftype = data[start]
        row = bytearray(data[start + 1:start + stride])
        if len(row) < rowlen:
            row += bytearray(rowlen - len(row))
        if ftype == 1:
            for i in range(bpp, rowlen):
                row[i] = (row[i] + row[i - bpp]) & 0xff
        elif ftype == 2:
            for i in range(rowlen):
                row[i] = (row[i] + prev[i]) & 0xff
        elif ftype == 3:
            for i in range(rowlen):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xff
        elif ftype == 4:
            for i in range(rowlen):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                if pa <= pb and pa <= pc:
                    pr = a
                elif pb <= pc:
                    pr = b
                else:
                    pr = c
                row[i] = (row[i] + pr) & 0xff
        out += row
        prev = row
    return bytes(out)


def ascii_hex_decode(data):
    data = data.split(b">")[0]
    data = bytes(c for c in data if c not in b" \t\r\n\x0c\x00")
    if len(data) % 2:
        data += b"0"
    return binascii.unhexlify(data)


def ascii85_decode(data):
    data = data.strip()
    if data.startswith(b"<~"):
        data = data[2:]
    end = data.find(b"~>")
    if end >= 0:
        data = data[:end]
    data = bytes(c for c in data if c not in b" \t\r\n\x0c\x00")
    out = bytearray()
    group = []
    for c in data:
        if c == ord("z") and not group:
            out += b"\0\0\0\0"
            continue
        group.append(c - 33)
        if len(group) == 5:
            val = 0
            for g in group:
                val = val * 85 + g
            out += (val & 0xffffffff).to_bytes(4, "big")
            group = []
    if group:
        n = len(group)
        group += [84] * (5 - n)
        val = 0
        for g in group:
            val = val * 85 + g
        out += (val & 0xffffffff).to_bytes(4, "big")[:n - 1]
    return bytes(out)


def lzw_decode(data, early=1):
    out = bytearray()
    table = [bytes([i]) for i in range(256)] + [None, None]
    bitpos = 0
    nbits = len(data) * 8
    width = 9
    prev = None
    while bitpos + width <= nbits:
        # read `width` bits
        code = 0
        for _ in range(width):
            byte = data[bitpos >> 3]
            code = (code << 1) | ((byte >> (7 - (bitpos & 7))) & 1)
            bitpos += 1
        if code == 256:
            table = table[:258]
            width = 9
            prev = None
            continue
        if code == 257:
            break
        if prev is None:
            entry = table[code]
            out += entry
            prev = entry
            continue
        if code < len(table):
            entry = table[code]
            table.append(prev + entry[:1])
        else:
            entry = prev + prev[:1]
            table.append(entry)
        out += entry
        prev = entry
        size = len(table) + early
        if size >= 4096:
            width = 12
        elif size >= 2048:
            width = 12
        elif size >= 1024:
            width = 11
        elif size >= 512:
            width = 10
    return bytes(out)


def run_length_decode(data):
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        length = data[i]
        if length == 128:
            break
        if length < 128:
            out += data[i + 1:i + 2 + length]
            i += 2 + length
        else:
            if i + 1 < n:
                out += bytes([data[i + 1]]) * (257 - length)
            i += 2
    return bytes(out)


IMAGE_FILTERS = ("DCTDecode", "DCT", "JPXDecode", "CCITTFaxDecode", "CCF",
                 "JBIG2Decode")


def decode_stream(sdict, raw, resolve=None):
    filters = _as_list(sdict.get("Filter"))
    parms = _as_list(sdict.get("DecodeParms") or sdict.get("DP"))
    if resolve is not None:
        filters = [resolve(f) for f in filters]
        parms = [resolve(p) for p in parms]
    data = raw
    for i, f in enumerate(filters):
        p = parms[i] if i < len(parms) else None
        if f in ("FlateDecode", "Fl"):
            data = apply_predictor(flate_decode(data), p)
        elif f in ("LZWDecode", "LZW"):
            early = 1 if not p else p.get("EarlyChange", 1)
            data = apply_predictor(lzw_decode(data, early), p)
        elif f in ("ASCIIHexDecode", "AHx"):
            data = ascii_hex_decode(data)
        elif f in ("ASCII85Decode", "A85"):
            data = ascii85_decode(data)
        elif f in ("RunLengthDecode", "RL"):
            data = run_length_decode(data)
        elif f == "Crypt":
            continue
        else:
            raise UnsupportedFilter("cannot decode filter %s" % f)
    return data
