"""Tokenizer / parser for PDF objects and content streams."""

import re

from .objects import Name, PdfString, Ref, PdfError

WHITESPACE = b"\x00\t\n\x0c\r "
DELIMS = b"()<>[]{}/%"

_ws_re = re.compile(rb"(?:[\x00\t\n\x0c\r ]+|%[^\r\n]*)+")
_number_re = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")
_regular_re = re.compile(rb"[^\x00\t\n\x0c\r ()<>\[\]{}/%]+")
_int_re = re.compile(rb"^[+-]?\d+$")
_hex_re = re.compile(rb"[^0-9A-Fa-f]")
_name_escape_re = re.compile(rb"#([0-9A-Fa-f]{2})")

_ESCAPES = {
    ord("n"): b"\n", ord("r"): b"\r", ord("t"): b"\t", ord("b"): b"\b",
    ord("f"): b"\f", ord("("): b"(", ord(")"): b")", ord("\\"): b"\\",
}


class Keyword(str):
    """A bare keyword/operator such as ``obj``, ``R`` or ``Tj``."""

    def __repr__(self):
        return "Keyword(%s)" % str(self)


class EndOfData(PdfError):
    """Raised when the buffer ends in the middle of an object."""


class Lexer(object):
    def __init__(self, data, pos=0):
        self.data = data
        self.pos = pos
        self.length = len(data)

    def skip_ws(self):
        m = _ws_re.match(self.data, self.pos)
        if m:
            self.pos = m.end()

    def next_token(self):
        """Return the next token or None at end of data.

        Tokens: Keyword, int/float, Name, PdfString, or one of the
        structural markers '<<', '>>', '[', ']', '{', '}'.
        """
        self.skip_ws()
        if self.pos >= self.length:
            return None
        data = self.data
        c = data[self.pos]
        if c == 0x2f:  # /
            m = _regular_re.match(data, self.pos + 1)
            raw = m.group(0) if m else b""
            self.pos = self.pos + 1 + len(raw)
            if b"#" in raw:
                raw = _name_escape_re.sub(lambda mm: bytes([int(mm.group(1), 16)]), raw)
            return Name(raw.decode("latin-1"))
        if c == 0x28:  # (
            return self._literal_string()
        if c == 0x3c:  # <
            if data[self.pos + 1:self.pos + 2] == b"<":
                self.pos += 2
                return "<<"
            return self._hex_string()
        if c == 0x3e:  # >
            if data[self.pos + 1:self.pos + 2] == b">":
                self.pos += 2
                return ">>"
            self.pos += 1
            return Keyword(">")
        if c in (0x5b, 0x5d, 0x7b, 0x7d):  # [ ] { }
            self.pos += 1
            return chr(c)
        if c == 0x29:  # stray )
            self.pos += 1
            return Keyword(")")
        m = _regular_re.match(data, self.pos)
        if not m:
            self.pos += 1
            return Keyword(chr(c))
        raw = m.group(0)
        self.pos = m.end()
        nm = _number_re.match(raw)
        if nm and nm.end() == len(raw):
            if _int_re.match(raw):
                return int(raw)
            try:
                return float(raw)
            except ValueError:
                return 0.0
        return Keyword(raw.decode("latin-1"))

    def _literal_string(self):
        data = self.data
        pos = self.pos + 1
        depth = 1
        out = bytearray()
        n = self.length
        while pos < n:
            c = data[pos]
            if c == 0x5c:  # backslash
                pos += 1
                if pos >= n:
                    break
                c = data[pos]
                if c in _ESCAPES:
                    out += _ESCAPES[c]
                    pos += 1
                elif 0x30 <= c <= 0x37:
                    digits = 0
                    val = 0
                    while digits < 3 and pos < n and 0x30 <= data[pos] <= 0x37:
                        val = val * 8 + (data[pos] - 0x30)
                        pos += 1
                        digits += 1
                    out.append(val & 0xff)
                elif c == 0x0d:  # line continuation
                    pos += 1
                    if pos < n and data[pos] == 0x0a:
                        pos += 1
                elif c == 0x0a:
                    pos += 1
                else:
                    out.append(c)
                    pos += 1
                continue
            if c == 0x28:
                depth += 1
            elif c == 0x29:
                depth -= 1
                if depth == 0:
                    pos += 1
                    break
            out.append(c)
            pos += 1
        else:
            if depth:
                raise EndOfData("unterminated string")
        self.pos = pos
        return PdfString(bytes(out))

    def _hex_string(self):
        end = self.data.find(b">", self.pos + 1)
        if end < 0:
            raise EndOfData("unterminated hex string")
        raw = _hex_re.sub(b"", self.data[self.pos + 1:end])
        if len(raw) % 2:
            raw += b"0"
        self.pos = end + 1
        import binascii
        return PdfString(binascii.unhexlify(raw), hexform=True)

    # ------------------------------------------------------------ objects

    def parse_object(self, allow_refs=True):
        """Parse one complete object starting at the current position."""
        tok = self.next_token()
        if tok is None:
            raise EndOfData("unexpected end of data")
        return self._build(tok, allow_refs)

    def _build(self, tok, allow_refs):
        if isinstance(tok, int) and not isinstance(tok, bool) and allow_refs:
            save = self.pos
            t2 = self.next_token()
            if isinstance(t2, int) and not isinstance(t2, bool):
                t3 = self.next_token()
                if isinstance(t3, Keyword) and t3 == "R":
                    return Ref(tok, t2)
            self.pos = save
            return tok
        if isinstance(tok, Keyword):
            if tok == "true":
                return True
            if tok == "false":
                return False
            if tok == "null":
                return None
            return tok
        if tok == "[":
            arr = []
            while True:
                t = self.next_token()
                if t is None:
                    raise EndOfData("unterminated array")
                if t == "]" and not isinstance(t, (Name, PdfString, Keyword)):
                    return arr
                arr.append(self._build(t, allow_refs))
        if tok == "<<" and not isinstance(tok, (Name, PdfString, Keyword)):
            d = {}
            while True:
                t = self.next_token()
                if t is None:
                    raise EndOfData("unterminated dictionary")
                if t == ">>" and not isinstance(t, (Name, PdfString, Keyword)):
                    return d
                if not isinstance(t, Name):
                    # malformed key; skip it
                    continue
                v = self.next_token()
                if v is None:
                    raise EndOfData("unterminated dictionary")
                if v == ">>" and not isinstance(v, (Name, PdfString, Keyword)):
                    d[t] = None
                    return d
                d[t] = self._build(v, allow_refs)
        return tok


def parse(data, pos=0):
    lx = Lexer(data, pos)
    return lx.parse_object()
