"""PDF object model and serializer.

Plain Python types are used wherever possible:
    null -> None, boolean -> bool, numbers -> int/float, array -> list,
    dictionary -> dict (keys are Name), string -> PdfString, name -> Name.

Written for Python 3.4+ (Windows XP compatible), stdlib only.
"""

import binascii
import re


class PdfError(Exception):
    """Any problem with reading or writing a PDF."""


class Name(str):
    """A PDF name such as /Type (stored without the slash)."""

    def __repr__(self):
        return "/" + str(self)


class PdfString(bytes):
    """A PDF string. ``hexform`` remembers how it was written."""

    hexform = False

    def __new__(cls, value, hexform=False):
        obj = bytes.__new__(cls, value)
        obj.hexform = hexform
        return obj


class Ref(object):
    """An indirect reference ``num gen R``."""

    __slots__ = ("num", "gen")

    def __init__(self, num, gen=0):
        self.num = num
        self.gen = gen

    def __eq__(self, other):
        return isinstance(other, Ref) and other.num == self.num and other.gen == self.gen

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash((self.num, self.gen))

    def __repr__(self):
        return "Ref(%d, %d)" % (self.num, self.gen)


class Stream(object):
    """A stream object. ``raw`` is the still-encoded data.

    Large streams are loaded lazily: ``loader`` is a callable that returns
    the raw bytes when they are first needed.
    """

    def __init__(self, dict_=None, raw=None, loader=None):
        self.dict = dict_ if dict_ is not None else {}
        self._raw = raw
        self._loader = loader

    @property
    def raw(self):
        if self._raw is None and self._loader is not None:
            self._raw = self._loader()
        return self._raw if self._raw is not None else b""

    @raw.setter
    def raw(self, value):
        self._raw = value
        self._loader = None

    def get(self, key, default=None):
        return self.dict.get(key, default)

    def __getitem__(self, key):
        return self.dict[key]

    def __setitem__(self, key, value):
        self.dict[key] = value

    def __contains__(self, key):
        return key in self.dict

    def decoded(self):
        from .filters import decode_stream
        return decode_stream(self.dict, self.raw)

    def set_data(self, data, compress=True):
        """Replace the stream contents with ``data`` (unencoded bytes)."""
        import zlib
        for key in ("Filter", "DecodeParms", "DL", "F", "FFilter", "FDecodeParms"):
            self.dict.pop(key, None)
        if compress and len(data) > 64:
            self.raw = zlib.compress(data, 6)
            self.dict[Name("Filter")] = Name("FlateDecode")
        else:
            self.raw = data
        self.dict[Name("Length")] = len(self.raw)

    def copy(self):
        return Stream(dict(self.dict), self._raw, self._loader)


def N(s):
    """Shortcut for creating a Name."""
    return Name(s)


# ---------------------------------------------------------------- serializer

_NAME_SAFE = re.compile(rb"^[!-~]+$")
_NAME_SPECIAL = set(b"()<>[]{}/%#")


def _ser_name(name):
    raw = str(name).encode("latin-1", "replace")
    if _NAME_SAFE.match(raw) and not any(c in _NAME_SPECIAL for c in raw):
        return b"/" + raw
    out = bytearray(b"/")
    for c in raw:
        if c < 0x21 or c > 0x7e or c in _NAME_SPECIAL:
            out += ("#%02X" % c).encode("ascii")
        else:
            out.append(c)
    return bytes(out)


def _ser_string(s):
    if getattr(s, "hexform", False):
        return b"<" + binascii.hexlify(bytes(s)).upper() + b">"
    out = bytearray(b"(")
    for c in bytes(s):
        if c in (0x28, 0x29, 0x5c):  # ( ) \
            out.append(0x5c)
            out.append(c)
        elif c == 0x0d:
            out += b"\\r"
        elif c == 0x0a:
            out += b"\\n"
        else:
            out.append(c)
    out.append(0x29)
    return bytes(out)


def _ser_number(n):
    if isinstance(n, bool):
        return b"true" if n else b"false"
    if isinstance(n, int):
        return str(n).encode("ascii")
    if n == int(n) and abs(n) < 1e15:
        return str(int(n)).encode("ascii")
    text = ("%.5f" % n).rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    return text.encode("ascii")


def serialize(obj):
    """Serialize a PDF object (not including stream data) to bytes."""
    if obj is None:
        return b"null"
    if obj is True:
        return b"true"
    if obj is False:
        return b"false"
    if isinstance(obj, Name):
        return _ser_name(obj)
    if isinstance(obj, Ref):
        return ("%d %d R" % (obj.num, obj.gen)).encode("ascii")
    if isinstance(obj, (int, float)):
        return _ser_number(obj)
    if isinstance(obj, PdfString):
        return _ser_string(obj)
    if isinstance(obj, bytes):
        return _ser_string(obj)
    if isinstance(obj, str):
        return _ser_string(encode_text_string(obj))
    if isinstance(obj, (list, tuple)):
        return b"[" + b" ".join(serialize(x) for x in obj) + b"]"
    if isinstance(obj, dict):
        parts = [b"<<"]
        for key in sorted(obj.keys()):
            parts.append(_ser_name(Name(key)) + b" " + serialize(obj[key]))
        parts.append(b">>")
        return b"\n".join(parts) if len(parts) > 6 else b" ".join(parts)
    if isinstance(obj, Stream):
        d = dict(obj.dict)
        d[Name("Length")] = len(obj.raw)
        return serialize(d) + b"\nstream\n" + obj.raw + b"\nendstream"
    raise PdfError("cannot serialize %r" % (obj,))


# ------------------------------------------------------------- text strings

def decode_text_string(value):
    """Decode a PDF 'text string' (UTF-16BE with BOM, UTF-8 BOM, or PDFDoc)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    data = bytes(value)
    if data[:2] == b"\xfe\xff":
        return data[2:].decode("utf-16-be", "replace")
    if data[:2] == b"\xff\xfe":
        return data[2:].decode("utf-16-le", "replace")
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace")
    # PDFDocEncoding is close enough to latin-1 / cp1252 for display.
    try:
        return data.decode("cp1252")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def encode_text_string(text):
    """Encode a Python string as a PDF text string."""
    try:
        raw = text.encode("latin-1")
        if all(0x20 <= c < 0x7f or c in (9, 10, 13) or c >= 0xa1 for c in raw):
            return PdfString(raw)
    except UnicodeEncodeError:
        pass
    return PdfString(b"\xfe\xff" + text.encode("utf-16-be"))
