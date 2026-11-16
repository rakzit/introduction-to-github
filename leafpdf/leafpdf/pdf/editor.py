"""Editing operations on a PdfDocument.

Every function only records pending changes on the document; the caller
commits them (as one incremental update) when the whole command is done.
"""

import math
import time

from .objects import PdfString, Ref, Stream, PdfError, N
from .document import clone, map_refs, PdfDocument
from .content import parse_content, serialize_content
from .fonts import FontCache

# Helvetica advance widths for characters 32..126 (from the standard AFM).
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333,
         278, 278, 556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278,
         584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278,
         500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944,
         667, 667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556,
         278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500,
         278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
assert len(_HELV) == 95

FONT_KEY = "LPHelv"
GS_KEY = "LPGS"


class EditError(PdfError):
    pass


# ------------------------------------------------------------ page specs

def parse_pages(spec, total):
    """Parse '1-3,7,10-', 'all', 'odd', 'even', 'last', 'last-2' -> 0-based list."""
    if spec is None:
        return list(range(total))
    spec = str(spec).strip().lower().replace(" ", "")
    if spec in ("", "all", "*"):
        return list(range(total))
    if spec == "odd":
        return list(range(0, total, 2))
    if spec == "even":
        return list(range(1, total, 2))
    result = []

    def num(s):
        if s == "last" or s == "end":
            return total
        if s.startswith("last-"):
            return total - int(s[5:])
        return int(s)

    for part in spec.split(","):
        if not part:
            continue
        if "-" in part and not part.startswith("last-"):
            a, b = part.split("-", 1)
            a = num(a) if a else 1
            b = num(b) if b else total
        else:
            a = b = num(part)
        if a < 1 or b > total or a > b:
            raise EditError("page range '%s' is outside 1-%d" % (part, total))
        for p in range(a, b + 1):
            if p - 1 not in result:
                result.append(p - 1)
    return result


# -------------------------------------------------------------- helpers

def _contents_list(doc, page):
    c = page.get("Contents")
    r = doc.resolve(c)
    if isinstance(r, list):
        return list(r)
    if c is None:
        return []
    return [c]


def _page_content_bytes(doc, page):
    parts = []
    for c in _contents_list(doc, page):
        try:
            parts.append(doc.decode(c))
        except Exception:
            pass
    return b"\n".join(parts)


def _new_stream(doc, data):
    s = Stream({})
    s.set_data(data)
    return doc.add(s)


def _escape_text(text):
    raw = text.encode("cp1252", "replace")
    return PdfString(raw)


def text_width(text, size):
    w = 0
    for ch in text:
        c = ord(ch)
        w += _HELV[c - 32] if 32 <= c <= 126 else 556
    return w * size / 1000.0


def _matmul(m, n):
    a, b, c, d, e, f = m
    A, B, C, D, E, F = n
    return [a * A + b * C, a * B + b * D, c * A + d * C, c * B + d * D,
            e * A + f * C + E, e * B + f * D + F]


def display_matrix(doc, index):
    """Matrix mapping displayed page coords (origin bottom-left) to user space,
    plus displayed width and height."""
    x0, y0, x1, y1, rot = doc.page_box(index)
    W, H = x1 - x0, y1 - y0
    if rot == 90:
        return [0, 1, -1, 0, x0 + W, y0], H, W
    if rot == 180:
        return [-1, 0, 0, -1, x0 + W, y0 + H], W, H
    if rot == 270:
        return [0, -1, 1, 0, x0, y0 + H], H, W
    return [1, 0, 0, 1, x0, y0], W, H


def _fmt(m):
    return " ".join(("%.4f" % v).rstrip("0").rstrip(".") for v in m)


class Overlay(object):
    """Collects drawing appended on top of pages within one command."""

    def __init__(self, doc):
        self.doc = doc
        self._font_ref = None
        self._q_ref = None
        self._gs_refs = {}

    def font_ref(self):
        if self._font_ref is None:
            self._font_ref = self.doc.add({N("Type"): N("Font"), N("Subtype"): N("Type1"),
                                           N("BaseFont"): N("Helvetica"),
                                           N("Encoding"): N("WinAnsiEncoding")})
        return self._font_ref

    def gs_ref(self, opacity):
        key = round(opacity, 3)
        if key not in self._gs_refs:
            self._gs_refs[key] = self.doc.add({N("Type"): N("ExtGState"),
                                               N("ca"): key, N("CA"): key})
        return self._gs_refs[key]

    def add(self, index, ops, need_font=False, opacity=None):
        doc = self.doc
        ref = doc.page_refs()[index]
        page = doc.editable(ref)
        res = doc.inherited(ref, "Resources")
        res = clone(res) if isinstance(res, dict) else {}
        if need_font:
            fonts = doc.resolve(res.get("Font"))
            fonts = clone(fonts) if isinstance(fonts, dict) else {}
            fonts[N(FONT_KEY)] = self.font_ref()
            res[N("Font")] = fonts
        gs_name = None
        if opacity is not None and opacity < 1:
            gss = doc.resolve(res.get("ExtGState"))
            gss = clone(gss) if isinstance(gss, dict) else {}
            gs_name = "%s%d" % (GS_KEY, int(round(opacity * 100)))
            gss[N(gs_name)] = self.gs_ref(opacity)
            res[N("ExtGState")] = gss
        page[N("Resources")] = res
        if self._q_ref is None:
            self._q_ref = _new_stream(doc, b"q\n")
        body = b"Q\nq\n"
        if gs_name:
            body += ("/%s gs\n" % gs_name).encode("ascii")
        body += ops + b"\nQ\n"
        contents = _contents_list(doc, page)
        page[N("Contents")] = [self._q_ref] + contents + [_new_stream(doc, body)]


def _color_ops(color):
    r, g, b = color
    return ("%.3f %.3f %.3f rg %.3f %.3f %.3f RG" % (r, g, b, r, g, b)).encode("ascii")


def parse_color(value):
    names = {"black": (0, 0, 0), "red": (0.85, 0, 0), "blue": (0, 0, 0.8),
             "green": (0, 0.55, 0), "gray": (0.5, 0.5, 0.5), "grey": (0.5, 0.5, 0.5),
             "white": (1, 1, 1), "orange": (1, 0.55, 0), "yellow": (1, 0.9, 0)}
    if value is None:
        return (0, 0, 0)
    v = str(value).lower()
    if v in names:
        return names[v]
    v = v.lstrip("#")
    if len(v) == 6:
        try:
            return tuple(int(v[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
        except ValueError:
            pass
    raise EditError("unknown color '%s' (use a name or #RRGGBB)" % value)


# --------------------------------------------------------------- stamping

POSITIONS = ("top-left", "top", "top-right", "center", "bottom-left", "bottom",
             "bottom-right")


def stamp_text(doc, pages, text, pos="top-right", at=None, size=12, color=None,
               opacity=None, angle=0, margin=36, overlay=None, start=1):
    """Write text on pages. ``text`` may contain {n}, {total}, {date}, {file}.

    ``at`` = (x, y) in points measured from the displayed top-left corner.
    """
    doc.check_editable()
    overlay = overlay or Overlay(doc)
    total = doc.page_count()
    color = parse_color(color)
    import os
    for index in pages:
        s = text.replace("{n}", str(index + start)).replace("{total}", str(total + start - 1))
        s = s.replace("{date}", time.strftime("%Y-%m-%d"))
        s = s.replace("{file}", os.path.basename(doc.path))
        A, Wd, Hd = display_matrix(doc, index)
        tw = text_width(s, size)
        if at is not None:
            u, v = at[0], Hd - at[1] - size
        else:
            p = pos or "center"
            if p not in POSITIONS:
                raise EditError("position must be one of: " + ", ".join(POSITIONS))
            if p.endswith("left"):
                u = margin
            elif p.endswith("right"):
                u = Wd - margin - tw
            else:
                u = (Wd - tw) / 2.0
            if p.startswith("top"):
                v = Hd - margin * 0.66 - size
            elif p.startswith("bottom"):
                v = margin * 0.66
            else:
                v = (Hd - size * 0.7) / 2.0
        if angle:
            rad = math.radians(angle)
            cs, sn = math.cos(rad), math.sin(rad)
            if at is None and (pos or "center") == "center":
                cx, cy = Wd / 2.0, Hd / 2.0
                dx, dy = -tw / 2.0, -size * 0.35
                u = cx + dx * cs - dy * sn
                v = cy + dx * sn + dy * cs
            m = [cs, sn, -sn, cs, u, v]
        else:
            m = [1, 0, 0, 1, u, v]
        tm = _matmul(m, A)
        from .objects import serialize
        ops = (_color_ops(color) + b"\nBT\n/" + FONT_KEY.encode("ascii") +
               (" %s Tf\n" % _fmt([size])).encode("ascii") +
               _fmt(tm).encode("ascii") + b" Tm\n" + serialize(_escape_text(s)) +
               b" Tj\nET")
        overlay.add(index, ops, need_font=True, opacity=opacity)
    return len(pages)


def whiteout(doc, pages, x, y, w, h, color="white"):
    """Cover a rectangle (top-left origin, points) on each page."""
    doc.check_editable()
    overlay = Overlay(doc)
    col = parse_color(color)
    for index in pages:
        A, Wd, Hd = display_matrix(doc, index)
        ops = (_color_ops(col) + b"\n" + _fmt(A).encode("ascii") + b" cm\n" +
               _fmt([x, Hd - y - h, w, h]).encode("ascii") + b" re f")
        overlay.add(index, ops)
    return len(pages)


# ---------------------------------------------------------- find/replace

_IDENT = [1, 0, 0, 1, 0, 0]


class _State(object):
    """The parts of the graphics/text state that matter for text layout."""

    def __init__(self):
        self.ctm = list(_IDENT)
        self.codec = None
        self.size = 0.0
        self.tc = 0.0
        self.tw = 0.0
        self.th = 1.0
        self.tl = 0.0
        self.rise = 0.0
        self.fill = (0, 0, 0)

    def copy(self):
        s = _State()
        s.__dict__.update(self.__dict__)
        s.ctm = list(self.ctm)
        return s


def _nums(operands, n):
    vals = [float(x) for x in operands[-n:]]
    if len(vals) != n:
        raise ValueError("operand count")
    return vals


def _unit_advance(codec, code, st):
    a = codec.width(code) / 1000.0 * st.size + st.tc
    if code == b" ":
        a += st.tw
    return a * st.th


def _walk(ops, fonts, cache):
    """Yield (index, state, text_matrix) for text-showing ops and 'Do'.

    The text matrix is the one in effect when the op starts.
    """
    st = _State()
    stack = []
    tm = list(_IDENT)
    tlm = list(_IDENT)
    for i, (opr, op) in enumerate(ops):
        try:
            if op == "q":
                stack.append(st.copy())
            elif op == "Q":
                if stack:
                    st = stack.pop()
            elif op == "cm":
                st.ctm = _matmul(_nums(opr, 6), st.ctm)
            elif op == "BT":
                tm, tlm = list(_IDENT), list(_IDENT)
            elif op == "Tf" and len(opr) >= 2:
                fobj = fonts.get(opr[0]) if isinstance(fonts, dict) else None
                st.codec = cache.codec(fobj) if fobj is not None else None
                st.size = float(opr[1])
            elif op == "Tc":
                st.tc = _nums(opr, 1)[0]
            elif op == "Tw":
                st.tw = _nums(opr, 1)[0]
            elif op == "Tz":
                st.th = _nums(opr, 1)[0] / 100.0
            elif op == "TL":
                st.tl = _nums(opr, 1)[0]
            elif op == "Ts":
                st.rise = _nums(opr, 1)[0]
            elif op in ("Td", "TD"):
                tx, ty = _nums(opr, 2)
                if op == "TD":
                    st.tl = -ty
                tlm = _matmul([1, 0, 0, 1, tx, ty], tlm)
                tm = list(tlm)
            elif op == "Tm":
                tlm = _nums(opr, 6)
                tm = list(tlm)
            elif op == "T*":
                tlm = _matmul([1, 0, 0, 1, 0, -st.tl], tlm)
                tm = list(tlm)
            elif op == "g":
                v = _nums(opr, 1)[0]
                st.fill = (v, v, v)
            elif op == "rg":
                st.fill = tuple(_nums(opr, 3))
            elif op == "k":
                c, m, y, k = _nums(opr, 4)
                st.fill = ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
            elif op in ("sc", "scn"):
                vals = [float(x) for x in opr if isinstance(x, (int, float))]
                if len(vals) == 1:
                    st.fill = (vals[0],) * 3
                elif len(vals) == 3:
                    st.fill = tuple(vals)
                elif len(vals) == 4:
                    c, m, y, k = vals
                    st.fill = ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
            elif op == "Do":
                yield i, st, tm
            elif op in ("Tj", "TJ", "'", '"'):
                if op in ("'", '"'):
                    if op == '"':
                        st.tw, st.tc = float(opr[0]), float(opr[1])
                    tlm = _matmul([1, 0, 0, 1, 0, -st.tl], tlm)
                    tm = list(tlm)
                if st.codec is None:
                    continue
                yield i, st, tm
                items = _items_of(opr, op)
                adv = 0.0
                for it in items:
                    if isinstance(it, bytes):
                        for code, _ in st.codec.split(it):
                            adv += _unit_advance(st.codec, code, st)
                    elif isinstance(it, (int, float)):
                        adv -= it / 1000.0 * st.size * st.th
                tm = _matmul([1, 0, 0, 1, adv, 0], tm)
        except (TypeError, ValueError, IndexError, KeyError):
            continue


def _items_of(operands, op):
    if op == "TJ":
        return operands[0] if operands and isinstance(operands[0], list) else []
    if operands and isinstance(operands[-1], bytes):
        return [operands[-1]]
    return []


def _replace_items(items, st, old, new, ignore_case, report):
    """Replace inside one text-showing op.

    Returns (new_items, count, overlays) where overlays is a list of
    (x_offset, text) to be drawn with a fallback font because the
    original font cannot show the new characters.
    """
    codec = st.codec
    units = []   # (item_index, code, text, start_advance, advance)
    adv = 0.0
    for idx, it in enumerate(items):
        if isinstance(it, bytes):
            for code, t in codec.split(it):
                a = _unit_advance(codec, code, st)
                units.append((idx, code, t, adv, a))
                adv += a
        elif isinstance(it, (int, float)):
            adv -= it / 1000.0 * st.size * st.th
    if not units:
        return None, 0, []
    owner = []
    for ui, u in enumerate(units):
        owner.extend([ui] * len(u[2]))
    joined = "".join(u[2] for u in units)
    hay = joined.lower() if ignore_case else joined
    needle = old.lower() if ignore_case else old
    if not needle or needle not in hay or len(hay) != len(owner):
        return None, 0, []
    matches = []
    pos = hay.find(needle)
    while pos >= 0:
        matches.append((owner[pos], owner[pos + len(needle) - 1]))
        pos = hay.find(needle, pos + len(needle))
    # try to encode the replacement with the document's own font
    encoded = []
    for ch in new:
        code = codec.encode_char(ch)
        if code is None and ch == " ":
            code = -250
        if code is None:
            encoded = None
            report["fallback_fonts"].add(codec.name or "?")
            break
        encoded.append(code)
    if encoded is None and (not st.size or not st.th):
        report["skipped"] += len(matches)
        return None, 0, []
    replace_at = {}
    overlays = []
    skip = set()
    drop_items = set()
    for a, b in matches:
        if encoded is not None:
            replace_at[a] = encoded
        else:
            width = units[b][3] + units[b][4] - units[a][3]
            replace_at[a] = [-width / (st.size * st.th) * 1000.0]
            overlays.append((units[a][3], new, width))
        for k in range(a + 1, b + 1):
            skip.add(k)
        ia, ib = units[a][0], units[b][0]
        for k in range(ia + 1, ib):
            if not isinstance(items[k], bytes):
                drop_items.add(k)
    hexform = any(getattr(it, "hexform", False) for it in items if isinstance(it, bytes))
    out = []
    cur = bytearray()
    ui = 0

    def flush():
        if cur:
            out.append(PdfString(bytes(cur), hexform))
            del cur[:]

    for idx, it in enumerate(items):
        if isinstance(it, bytes):
            while ui < len(units) and units[ui][0] == idx:
                if ui in replace_at:
                    for e in replace_at[ui]:
                        if isinstance(e, (int, float)):
                            flush()
                            out.append(round(e, 3) if isinstance(e, float) else e)
                        else:
                            cur += e
                elif ui not in skip:
                    cur += units[ui][1]
                ui += 1
        else:
            if idx in drop_items:
                continue
            flush()
            out.append(it)
    flush()
    return out, len(matches), overlays


def _with_font(doc, res, font_ref):
    res = clone(res) if isinstance(res, dict) else {}
    fonts = doc.resolve(res.get("Font"))
    fonts = clone(fonts) if isinstance(fonts, dict) else {}
    fonts[N(FONT_KEY)] = font_ref
    res[N("Font")] = fonts
    return res


def _process_content(doc, data, resources, cache, old, new, ignore_case, ctx):
    """Find (new is None) or replace text in one content stream.

    Returns (new_data or None, needs_fallback_font, replaced, text).
    """
    from .objects import serialize
    ops = parse_content(data)
    fonts = doc.resolve(resources.get("Font")) if isinstance(resources, dict) else None
    xobjs = doc.resolve(resources.get("XObject")) if isinstance(resources, dict) else None
    total = 0
    parts = []
    edits = {}
    draws = []
    for i, st, tm in _walk(ops, fonts or {}, cache):
        operands, op = ops[i]
        if op == "Do":
            name = operands[0] if operands else None
            xref = xobjs.get(name) if isinstance(xobjs, dict) else None
            if not isinstance(xref, Ref) or xref.num in ctx["forms"]:
                continue
            ctx["forms"].add(xref.num)
            x = doc.resolve(xref)
            if not isinstance(x, Stream) or x.get("Subtype") != "Form":
                continue
            xres = doc.resolve(x.get("Resources"))
            own = isinstance(xres, dict)
            if not own:
                xres = resources
            try:
                xdata = doc.decode(x)
            except Exception:
                continue
            nd, need_font, n, t = _process_content(doc, xdata, xres, cache, old, new,
                                                   ignore_case, ctx)
            parts.append(t)
            if nd is not None:
                form = doc.editable(xref)
                if need_font:
                    form[N("Resources")] = _with_font(doc, xres, ctx["font"]())
                elif not own and isinstance(xres, dict):
                    form[N("Resources")] = clone(xres)
                form.set_data(nd)
                total += n
            continue
        items = _items_of(operands, op)
        parts.append("".join(st.codec.decode(x) for x in items if isinstance(x, bytes)))
        if new is None:
            continue
        res, n, overlays = _replace_items(items, st, old, new, ignore_case, ctx["report"])
        if not n:
            continue
        total += n
        for x_off, text, room in overlays:
            need = text_width(text, st.size) * st.th
            squeeze = room / need if need > room > 0 else 1.0
            m = _matmul([st.size * st.th * max(squeeze, 0.5), 0, 0, st.size, x_off, st.rise],
                        _matmul(tm, st.ctm))
            draws.append((m, text, st.fill))
        if op == "TJ" or (op == "Tj" and not (len(res) == 1 and isinstance(res[0], bytes))):
            edits[i] = [([res], "TJ")]
        elif op == "Tj":
            edits[i] = [([res[0]], "Tj")]
        elif op == "'":
            edits[i] = [([], "T*"), ([res], "TJ")]
        else:
            edits[i] = [([operands[0]], "Tw"), ([operands[1]], "Tc"), ([], "T*"),
                        ([res], "TJ")]
    if not edits:
        return None, False, total, "".join(parts)
    rebuilt = []
    for i, o in enumerate(ops):
        rebuilt.extend(edits.get(i, [o]))
    data = serialize_content(rebuilt)
    if draws:
        out = [b"q\n", data, b"Q\n"]
        for m, text, fill in draws:
            out.append(b"q " + _color_ops(fill) + b" BT /" + FONT_KEY.encode("ascii") +
                       b" 1 Tf " + _fmt(m).encode("ascii") + b" Tm " +
                       serialize(_escape_text(text)) + b" Tj ET Q\n")
        data = b"".join(out)
    return data, bool(draws), total, "".join(parts)


def _new_report():
    return {"replaced": 0, "pages": [], "missed": [], "fallback_fonts": set(),
            "skipped": 0}


def replace_text(doc, pages, old, new, ignore_case=False):
    """Replace text on the given pages.  Returns a report dict.

    Text is replaced inside the document's own font when it has the needed
    characters; otherwise the old glyphs are removed (layout preserved) and
    the new text is drawn in Helvetica at the same position, size and color.
    """
    doc.check_editable()
    cache = FontCache(doc)
    refs = doc.page_refs()
    overlay = Overlay(doc)
    report = _new_report()
    ctx = {"forms": set(), "report": report, "font": overlay.font_ref}
    for index in pages:
        ref = refs[index]
        page = doc.get(ref)
        res = doc.inherited(ref, "Resources") or {}
        data = _page_content_bytes(doc, page)
        nd, need_font, n, text = _process_content(doc, data, res, cache, old, new,
                                                  ignore_case, ctx)
        if nd is not None:
            edp = doc.editable(ref)
            edp[N("Contents")] = _new_stream(doc, nd)
            if need_font:
                edp[N("Resources")] = _with_font(doc, res, overlay.font_ref())
        hay = text.lower() if ignore_case else text
        found = hay.count(old.lower() if ignore_case else old)
        if n:
            report["replaced"] += n
            report["pages"].append(index)
        if found > n:
            report["missed"].append((index, found - n))
    return report


def page_text(doc, index, cache=None):
    cache = cache or FontCache(doc)
    ref = doc.page_refs()[index]
    res = doc.inherited(ref, "Resources") or {}
    data = _page_content_bytes(doc, doc.get(ref))
    ctx = {"forms": set(), "report": _new_report(), "font": None}
    return _process_content(doc, data, res, cache, "", None, False, ctx)[3]


def find_text(doc, pages, needle, ignore_case=True):
    """Return [(page_index, count)] for pages containing ``needle``."""
    cache = FontCache(doc)
    out = []
    n2 = needle.lower() if ignore_case else needle
    for index in pages:
        try:
            text = page_text(doc, index, cache)
        except Exception:
            continue
        hay = text.lower() if ignore_case else text
        c = hay.count(n2)
        if c:
            out.append((index, c))
    return out


# ------------------------------------------------------------- page ops

def rotate_pages(doc, pages, degrees):
    doc.check_editable()
    if degrees % 90:
        raise EditError("rotation must be a multiple of 90")
    refs = doc.page_refs()
    for index in pages:
        cur = doc.inherited(refs[index], "Rotate") or 0
        page = doc.editable(refs[index])
        page[N("Rotate")] = (int(cur) + degrees) % 360
    return len(pages)


def delete_pages(doc, pages):
    doc.check_editable()
    refs = doc.page_refs()
    if len(set(pages)) >= len(refs):
        raise EditError("cannot delete every page")
    for index in sorted(set(pages), reverse=True):
        ref = refs[index]
        page = doc.get(ref)
        parent_ref = page.get("Parent")
        if not isinstance(parent_ref, Ref):
            raise EditError("damaged page tree")
        parent = doc.editable(parent_ref)
        kids = list(doc.resolve(parent.get("Kids")) or [])
        kids = [k for k in kids if not (isinstance(k, Ref) and k.num == ref.num)]
        parent[N("Kids")] = kids
        node_ref = parent_ref
        depth = 0
        while isinstance(node_ref, Ref) and depth < 64:
            node = doc.editable(node_ref)
            node[N("Count")] = max(0, int(doc.resolve(node.get("Count")) or 1) - 1)
            node_ref = node.get("Parent")
            depth += 1
    doc.invalidate_pages()
    return len(set(pages))


def _set_page_order(doc, new_refs):
    root_ref = doc.catalog().get("Pages")
    for r in new_refs:
        page = doc.get(r)
        parent = page.get("Parent") if isinstance(page, dict) else None
        if isinstance(parent, Ref) and parent.num == root_ref.num:
            continue
        ed = doc.editable(r)
        for key in ("Resources", "MediaBox", "CropBox", "Rotate"):
            if key not in ed and parent is not None:
                v = doc.inherited(r, key)
                if v is not None:
                    ed[N(key)] = clone(v)
        ed[N("Parent")] = root_ref
    root = doc.editable(root_ref)
    root[N("Kids")] = list(new_refs)
    root[N("Count")] = len(new_refs)
    if "MediaBox" not in root:
        root[N("MediaBox")] = [0, 0, 612, 792]
    doc.invalidate_pages()


def move_pages(doc, pages, to):
    """Move pages so the first moved page becomes page number ``to`` (1-based)."""
    doc.check_editable()
    refs = list(doc.page_refs())
    moving = [refs[i] for i in pages]
    rest = [r for i, r in enumerate(refs) if i not in set(pages)]
    to = max(1, min(int(to), len(rest) + 1))
    new = rest[:to - 1] + moving + rest[to - 1:]
    _set_page_order(doc, new)
    return len(moving)


def reverse_pages(doc):
    doc.check_editable()
    _set_page_order(doc, list(reversed(doc.page_refs())))
    return doc.page_count()


def insert_blank(doc, after, count=1):
    doc.check_editable()
    refs = list(doc.page_refs())
    ref_index = max(0, min(after - 1, len(refs) - 1)) if refs else 0
    x0, y0, x1, y1, rot = doc.page_box(ref_index) if refs else (0, 0, 612, 792, 0)
    root_ref = doc.catalog().get("Pages")
    new = []
    for _ in range(count):
        new.append(doc.add({N("Type"): N("Page"), N("Parent"): root_ref,
                            N("MediaBox"): [x0, y0, x1, y1], N("Rotate"): rot,
                            N("Resources"): {}}))
    after = max(0, min(after, len(refs)))
    _set_page_order(doc, refs[:after] + new + refs[after:])
    return count


def import_pages(doc, other, pages=None):
    """Copy pages (0-based indices) of ``other`` into ``doc``; returns new Refs.

    ``other`` must stay open until ``doc`` is committed (stream data is read
    lazily from it).
    """
    doc.check_editable()
    other.check_editable()
    opages = other.page_refs()
    if pages is None:
        pages = list(range(len(opages)))
    tree = set()
    stack = [other.catalog().get("Pages")]
    while stack:
        r = stack.pop()
        if not isinstance(r, Ref) or r.num in tree:
            continue
        tree.add(r.num)
        node = other.get(r)
        if isinstance(node, dict):
            kids = other.resolve(node.get("Kids"))
            if isinstance(kids, list):
                stack.extend(kids)
    memo = {}
    queue = []
    for i in pages:
        r = opages[i]
        if r.num not in memo:
            memo[r.num] = doc.add(None)
            queue.append(r)
    page_nums = set(opages[i].num for i in pages)
    root_ref = doc.catalog().get("Pages")

    def mapper(ref):
        if ref.num in memo:
            return memo[ref.num]
        if ref.num in tree and ref.num not in page_nums:
            return None
        if other.get(ref) is None:
            return None
        memo[ref.num] = doc.add(None)
        queue.append(ref)
        return memo[ref.num]

    while queue:
        r = queue.pop()
        obj = other.get(r)
        if r.num in page_nums:
            obj = clone(obj)
            for key in ("Resources", "MediaBox", "CropBox", "Rotate"):
                if key not in obj:
                    v = other.inherited(r, key)
                    if v is not None:
                        obj[N(key)] = clone(v)
            obj.pop("Parent", None)
            obj.pop("StructParents", None)
            obj = map_refs(obj, mapper)
            obj[N("Parent")] = root_ref
        else:
            obj = map_refs(obj, mapper)
        doc.update(memo[r.num], obj)
    return [memo[opages[i].num] for i in pages]


def merge_file(doc, path, after=None, pages=None):
    other = PdfDocument(path)
    if pages is not None:
        pages = parse_pages(pages, other.page_count())
    new = import_pages(doc, other, pages)
    refs = list(doc.page_refs())
    after = len(refs) if after is None else max(0, min(int(after), len(refs)))
    _set_page_order(doc, refs[:after] + new + refs[after:])
    return other, len(new)
