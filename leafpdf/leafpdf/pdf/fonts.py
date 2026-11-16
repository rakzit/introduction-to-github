"""Font text codecs: map string bytes in content streams <-> Unicode.

Used by find/replace.  Supports ToUnicode CMaps (incl. Identity-H CID
fonts), WinAnsi / MacRoman / Standard encodings and /Differences.
"""

import binascii

from .lexer import Lexer, Keyword, EndOfData
from .objects import Name, Stream

_GLYPHS = {
    "space": " ", "exclam": "!", "quotedbl": '"', "numbersign": "#",
    "dollar": "$", "percent": "%", "ampersand": "&", "quotesingle": "'",
    "quoteright": "’", "quoteleft": "‘", "parenleft": "(",
    "parenright": ")", "asterisk": "*", "plus": "+", "comma": ",",
    "hyphen": "-", "minus": "−", "period": ".", "slash": "/",
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
    "colon": ":", "semicolon": ";", "less": "<", "equal": "=",
    "greater": ">", "question": "?", "at": "@", "bracketleft": "[",
    "backslash": "\\", "bracketright": "]", "asciicircum": "^",
    "underscore": "_", "grave": "`", "braceleft": "{", "bar": "|",
    "braceright": "}", "asciitilde": "~", "bullet": "•",
    "endash": "–", "emdash": "—", "quotedblleft": "“",
    "quotedblright": "”", "quotesinglbase": "‚",
    "quotedblbase": "„", "ellipsis": "…", "fi": "fi", "fl": "fl",
    "ff": "ff", "ffi": "ffi", "ffl": "ffl", "copyright": "©",
    "registered": "®", "trademark": "™", "degree": "°",
    "section": "§", "paragraph": "¶", "dagger": "†",
    "daggerdbl": "‡", "nbspace": " ", "Euro": "€",
    "periodcentered": "·", "multiply": "×", "divide": "÷",
}

def glyph_to_unicode(name):
    name = str(name).split(".")[0].split("_")[0]
    if name in _GLYPHS:
        return _GLYPHS[name]
    if len(name) == 1:
        return name
    if name.startswith("uni") and len(name) >= 7:
        try:
            return "".join(chr(int(name[i:i + 4], 16)) for i in range(3, len(name) - 3, 4))
        except ValueError:
            pass
    if name.startswith("u") and 5 <= len(name) <= 7:
        try:
            return chr(int(name[1:], 16))
        except ValueError:
            pass
    try:
        import unicodedata
        return unicodedata.lookup(_accent_name(name))
    except (KeyError, ImportError, ValueError):
        return None


def _accent_name(name):
    # e.g. "eacute" -> "LATIN SMALL LETTER E WITH ACUTE"
    accents = {"acute": "ACUTE", "grave": "GRAVE", "circumflex": "CIRCUMFLEX",
               "dieresis": "DIAERESIS", "tilde": "TILDE", "ring": "RING ABOVE",
               "cedilla": "CEDILLA", "caron": "CARON"}
    for suffix, uname in accents.items():
        if name.endswith(suffix) and len(name) == len(suffix) + 1:
            base = name[0]
            case = "CAPITAL" if base.isupper() else "SMALL"
            return "LATIN %s LETTER %s WITH %s" % (case, base.upper(), uname)
    raise KeyError(name)


def _base_table(encoding_name):
    table = {}
    codec = {"WinAnsiEncoding": "cp1252", "MacRomanEncoding": "mac_roman",
             "PDFDocEncoding": "latin-1"}.get(encoding_name, "cp1252")
    for code in range(32, 256):
        try:
            table[code] = bytes([code]).decode(codec)
        except UnicodeDecodeError:
            pass
    if encoding_name == "StandardEncoding":
        table[0x27] = "’"
        table[0x60] = "‘"
    return table


def parse_cmap(data):
    """Return (code_lengths, {code_bytes: unicode})."""
    lx = Lexer(data)
    mapping = {}
    lengths = set()
    tokens = []
    while True:
        try:
            t = lx.next_token()
        except EndOfData:
            break
        if t is None:
            break
        if isinstance(t, Keyword):
            if t == "endcodespacerange":
                for i in range(0, len(tokens) - 1, 2):
                    if isinstance(tokens[i], bytes):
                        lengths.add(len(tokens[i]))
            elif t == "endbfchar":
                for i in range(0, len(tokens) - 1, 2):
                    src, dst = tokens[i], tokens[i + 1]
                    if isinstance(src, bytes) and isinstance(dst, bytes):
                        mapping[bytes(src)] = _utf16(dst)
            elif t == "endbfrange":
                i = 0
                while i + 2 < len(tokens):
                    lo, hi, dst = tokens[i], tokens[i + 1], tokens[i + 2]
                    i += 3
                    if not (isinstance(lo, bytes) and isinstance(hi, bytes)):
                        continue
                    n = len(lo)
                    a = int(binascii.hexlify(lo) or b"0", 16)
                    b = int(binascii.hexlify(hi) or b"0", 16)
                    if b - a > 65535:
                        continue
                    for k, code in enumerate(range(a, b + 1)):
                        key = code.to_bytes(n, "big")
                        if isinstance(dst, list):
                            if k < len(dst) and isinstance(dst[k], bytes):
                                mapping[key] = _utf16(dst[k])
                        elif isinstance(dst, bytes) and dst:
                            base = bytearray(dst)
                            val = int(binascii.hexlify(bytes(base[-2:])), 16) + k
                            out = bytes(base[:-2]) + (val & 0xffff).to_bytes(2, "big")
                            mapping[key] = _utf16(out)
            tokens = []
            continue
        if t == "[":
            arr = []
            while True:
                x = lx.next_token()
                if x is None or x == "]":
                    break
                arr.append(x)
            tokens.append(arr)
            continue
        tokens.append(t)
    if not lengths:
        lengths = set(len(k) for k in mapping) or {1}
    return lengths, mapping


def _utf16(b):
    b = bytes(b)
    if len(b) % 2:
        return b.decode("latin-1")
    return b.decode("utf-16-be", "replace")


class FontCodec(object):
    def __init__(self, doc, font):
        self.doc = doc
        font = doc.resolve(font) or {}
        self.font = font
        self.name = str(font.get("BaseFont", ""))
        self.subtype = font.get("Subtype")
        self.composite = self.subtype == "Type0"
        self.subset = len(self.name) > 7 and self.name[6] == "+"
        self.code_lengths = {2} if self.composite else {1}
        self.to_uni = {}
        self.widths = None
        self.default_width = 500
        self._load()
        self.from_uni = {}
        for code, text in sorted(self.to_uni.items(), key=lambda kv: (len(kv[0]), kv[0]), reverse=True):
            if len(text) == 1 and self.has_glyph(code):
                self.from_uni[text] = code
        self.code_lengths = sorted(self.code_lengths)

    def _load(self):
        doc = self.doc
        font = self.font
        if not self.composite:
            enc = doc.resolve(font.get("Encoding"))
            base_name = "StandardEncoding"
            if self.name.split("+")[-1] in ("Symbol", "ZapfDingbats"):
                base_name = None
            elif self.subtype == "TrueType" or enc is not None:
                base_name = "WinAnsiEncoding"
            diffs = None
            if isinstance(enc, Name):
                base_name = str(enc)
            elif isinstance(enc, dict):
                if enc.get("BaseEncoding"):
                    base_name = str(enc.get("BaseEncoding"))
                diffs = doc.resolve(enc.get("Differences"))
            if base_name:
                for code, ch in _base_table(base_name).items():
                    self.to_uni[bytes([code])] = ch
            if isinstance(diffs, list):
                code = 0
                for item in diffs:
                    item = doc.resolve(item)
                    if isinstance(item, (int, float)):
                        code = int(item)
                    elif isinstance(item, Name):
                        u = glyph_to_unicode(item)
                        if u is not None and 0 <= code < 256:
                            self.to_uni[bytes([code])] = u
                        code += 1
            fd = doc.resolve(font.get("FontDescriptor"))
            if isinstance(fd, dict):
                mw = doc.resolve(fd.get("MissingWidth"))
                if isinstance(mw, (int, float)) and mw:
                    self.default_width = mw
            if self.name in ("Helvetica", "Arial", "Helvetica-Bold", "Times-Roman", "Courier"):
                self.default_width = 600 if self.name == "Courier" else 556
            first = doc.resolve(font.get("FirstChar"))
            widths = doc.resolve(font.get("Widths"))
            if isinstance(widths, list) and isinstance(first, int):
                self.widths = {}
                for i, w in enumerate(widths):
                    w = doc.resolve(w)
                    if isinstance(w, (int, float)):
                        self.widths[bytes([first + i])] = w
        else:
            desc = doc.resolve(font.get("DescendantFonts"))
            if isinstance(desc, list) and desc:
                d0 = doc.resolve(desc[0]) or {}
                dw = doc.resolve(d0.get("DW"))
                self.default_width = dw if isinstance(dw, (int, float)) else 1000
                warr = doc.resolve(d0.get("W"))
                if isinstance(warr, list):
                    self.widths = {}
                    i = 0
                    while i < len(warr):
                        a = doc.resolve(warr[i])
                        b = doc.resolve(warr[i + 1]) if i + 1 < len(warr) else None
                        if isinstance(b, list):
                            for k, w in enumerate(b):
                                self.widths[(a + k).to_bytes(2, "big")] = doc.resolve(w)
                            i += 2
                        else:
                            c = doc.resolve(warr[i + 2]) if i + 2 < len(warr) else 0
                            if isinstance(a, int) and isinstance(b, int) and b - a < 70000:
                                for cid in range(a, b + 1):
                                    self.widths[cid.to_bytes(2, "big")] = c
                            i += 3
        tu = doc.resolve(font.get("ToUnicode"))
        if isinstance(tu, Stream):
            try:
                lengths, mapping = parse_cmap(doc.decode(tu))
                if self.composite:
                    self.code_lengths = lengths
                self.to_uni.update(mapping)
            except Exception:
                pass

    def width(self, code):
        if self.widths is not None and code in self.widths:
            w = self.widths[code]
            if isinstance(w, (int, float)):
                return w
        return self.default_width

    def has_glyph(self, code):
        if self.widths is None:
            return not (self.subset and self.composite)
        if code in self.widths:
            return bool(self.widths[code]) or self.to_uni.get(code) == " "
        return False

    def split(self, data):
        """Split string bytes into a list of (code_bytes, text)."""
        out = []
        i = 0
        n = len(data)
        lengths = self.code_lengths
        while i < n:
            code = None
            for ln in lengths:
                cand = bytes(data[i:i + ln])
                if cand in self.to_uni:
                    code = cand
                    break
            if code is None:
                ln = lengths[-1] if self.composite else 1
                code = bytes(data[i:i + ln])
                text = "�"
            else:
                text = self.to_uni[code]
            out.append((code, text))
            i += len(code)
        return out

    def decode(self, data):
        return "".join(t for _, t in self.split(data))

    def encode_char(self, ch):
        return self.from_uni.get(ch)


class FontCache(object):
    def __init__(self, doc):
        self.doc = doc
        self._by_num = {}

    def codec(self, font_obj):
        from .objects import Ref
        key = font_obj.num if isinstance(font_obj, Ref) else id(font_obj)
        c = self._by_num.get(key)
        if c is None:
            c = FontCodec(self.doc, font_obj)
            self._by_num[key] = c
        return c
