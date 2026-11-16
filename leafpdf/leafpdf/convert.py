"""Conversions between PDF, pictures and office formats.

Built in (pure Python, nothing to install):
  pictures -> PDF : JPEG and PNG (others with MuPDF or Pillow)
  text     -> PDF : .txt
  PDF -> DOCX / RTF / XLSX / PPTX / TXT / HTML
Through helper programs:
  PDF -> PNG / JPG / TIFF / BMP pictures       (MuPDF or Ghostscript)
  DOC/DOCX/XLS/XLSX/PPT/PPTX/ODT/RTF -> PDF    (Microsoft Office or LibreOffice)
  PDF -> ODT / DOC                             (built-in DOCX + LibreOffice)
"""

import os
import re
import struct
import tempfile
import zipfile
import zlib
from xml.sax.saxutils import escape as xml_escape

from .pdf.objects import PdfString, serialize, N
from .pdf.filters import apply_predictor
from .tools import run, IS_WINDOWS

IMAGE_EXTS = (".jpg", ".jpeg", ".jpe", ".png", ".gif", ".bmp", ".tif", ".tiff")
OFFICE_EXTS = (".doc", ".docx", ".rtf", ".odt", ".txt", ".xls", ".xlsx", ".ods",
               ".csv", ".ppt", ".pptx", ".odp", ".wpd", ".wps", ".html", ".htm")
PDF_TARGETS = ("docx", "rtf", "txt", "html", "xlsx", "pptx", "odt", "doc", "png",
               "jpg", "tiff", "bmp")

PAGE_SIZES = {"a4": (595.28, 841.89), "letter": (612, 792), "legal": (612, 1008),
              "a5": (419.53, 595.28), "a3": (841.89, 1190.55)}


class ConvertError(Exception):
    pass


# =========================================================== small PDF writer

class PdfWriter(object):
    """Streams a brand-new PDF to disk object by object."""

    def __init__(self, path):
        self.path = path
        self.f = open(path, "wb")
        self.f.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        self.offsets = {}
        self.next = 1

    def reserve(self):
        n = self.next
        self.next += 1
        return n

    def write(self, num, obj):
        from .pdf.objects import Ref
        self.offsets[num] = self.f.tell()
        self.f.write(("%d 0 obj\n" % num).encode("ascii"))
        self.f.write(serialize(obj))
        self.f.write(b"\nendobj\n")
        return Ref(num)

    def add(self, obj):
        return self.write(self.reserve(), obj)

    def close(self, root_ref):
        xpos = self.f.tell()
        size = self.next
        lines = [b"xref\n", ("0 %d\n" % size).encode("ascii"), b"0000000000 65535 f\r\n"]
        for n in range(1, size):
            lines.append(("%010d 00000 n\r\n" % self.offsets.get(n, 0)).encode("ascii"))
        self.f.write(b"".join(lines))
        trailer = {N("Size"): size, N("Root"): root_ref}
        self.f.write(b"trailer\n" + serialize(trailer) + b"\n")
        self.f.write(("startxref\n%d\n%%%%EOF\n" % xpos).encode("ascii"))
        self.f.close()

def _stream(d, data):
    from .pdf.objects import Stream
    s = Stream(d, data)
    s.dict[N("Length")] = len(data)
    return s


# ============================================================ image parsing

def _jpeg_info(data):
    """(width, height, components, adobe, orientation, dpi)."""
    if data[:2] != b"\xff\xd8":
        raise ConvertError("not a JPEG file")
    pos = 2
    adobe = False
    orientation = 1
    dpi = None
    w = h = comps = None
    n = len(data)
    while pos < n - 4:
        if data[pos] != 0xff:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0xd8, 0x01) or 0xd0 <= marker <= 0xd7:
            pos += 2
            continue
        seglen = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        seg = data[pos + 4:pos + 2 + seglen]
        if marker == 0xe0 and seg[:5] == b"JFIF\0" and len(seg) >= 12:
            unit, xd = seg[7], struct.unpack(">H", seg[8:10])[0]
            if unit == 1 and xd > 1:
                dpi = xd
            elif unit == 2 and xd > 1:
                dpi = int(xd * 2.54)
        elif marker == 0xe1 and seg[:6] == b"Exif\0\0":
            orientation = _exif_orientation(seg[6:]) or 1
        elif marker == 0xee and seg[:5] == b"Adobe":
            adobe = True
        elif 0xc0 <= marker <= 0xcf and marker not in (0xc4, 0xc8, 0xcc):
            h, w = struct.unpack(">HH", seg[1:5])
            comps = seg[5]
            break
        elif marker == 0xda:
            break
        pos += 2 + seglen
    if not w:
        raise ConvertError("cannot read JPEG size")
    return w, h, comps, adobe, orientation, dpi


def _exif_orientation(tiff):
    try:
        endian = "<" if tiff[:2] == b"II" else ">"
        ifd = struct.unpack(endian + "I", tiff[4:8])[0]
        count = struct.unpack(endian + "H", tiff[ifd:ifd + 2])[0]
        for i in range(count):
            e = ifd + 2 + i * 12
            tag = struct.unpack(endian + "H", tiff[e:e + 2])[0]
            if tag == 0x0112:
                return struct.unpack(endian + "H", tiff[e + 8:e + 10])[0]
    except (struct.error, IndexError):
        pass
    return None


def _png_chunks(data):
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ConvertError("not a PNG file")
    pos = 8
    while pos + 8 <= len(data):
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        yield ctype, data[pos + 8:pos + 8 + length]
        pos += 12 + length


def _png_image(data):
    """Return (xobject_dict, stream_data, smask or None, width, height, dpi)."""
    ihdr = None
    idat = []
    palette = None
    dpi = None
    for ctype, body in _png_chunks(data):
        if ctype == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif ctype == b"IDAT":
            idat.append(body)
        elif ctype == b"PLTE":
            palette = body
        elif ctype == b"pHYs" and len(body) >= 9:
            px, py, unit = struct.unpack(">IIB", body)
            if unit == 1 and px > 0:
                dpi = int(round(px * 0.0254))
    if not ihdr:
        raise ConvertError("bad PNG")
    w, h, bits, ctype, comp, filt, interlace = ihdr
    if interlace:
        raise ConvertError("interlaced PNG")
    raw = b"".join(idat)
    if ctype in (0, 2, 3):
        colors = {0: 1, 2: 3, 3: 1}[ctype]
        cs = N("DeviceGray") if ctype == 0 else N("DeviceRGB")
        if ctype == 3:
            cs = [N("Indexed"), N("DeviceRGB"), len(palette) // 3 - 1, PdfString(palette)]
        d = {N("Type"): N("XObject"), N("Subtype"): N("Image"), N("Width"): w,
             N("Height"): h, N("ColorSpace"): cs, N("BitsPerComponent"): bits,
             N("Filter"): N("FlateDecode"),
             N("DecodeParms"): {N("Predictor"): 15, N("Colors"): colors,
                                N("BitsPerComponent"): bits, N("Columns"): w}}
        return d, raw, None, w, h, dpi
    # gray+alpha (4) or RGBA (6): split the alpha channel into an SMask
    colors = 2 if ctype == 4 else 4
    pixels = apply_predictor(zlib.decompress(raw), {"Predictor": 15, "Colors": colors,
                                                    "BitsPerComponent": bits, "Columns": w})
    bps = bits // 8
    stride = colors * bps
    color = bytearray()
    alpha = bytearray()
    for i in range(0, len(pixels), stride):
        color += pixels[i:i + stride - bps]
        alpha += pixels[i + stride - bps:i + stride]
    cs = N("DeviceGray") if ctype == 4 else N("DeviceRGB")
    smask = ({N("Type"): N("XObject"), N("Subtype"): N("Image"), N("Width"): w,
              N("Height"): h, N("ColorSpace"): N("DeviceGray"), N("BitsPerComponent"): bits,
              N("Filter"): N("FlateDecode")}, zlib.compress(bytes(alpha)))
    d = {N("Type"): N("XObject"), N("Subtype"): N("Image"), N("Width"): w, N("Height"): h,
         N("ColorSpace"): cs, N("BitsPerComponent"): bits, N("Filter"): N("FlateDecode")}
    return d, zlib.compress(bytes(color)), smask, w, h, dpi


def _to_png_or_jpeg(path, tools, workdir):
    """Convert an unsupported picture to PNG using Pillow or MuPDF."""
    out = os.path.join(workdir, "conv-%d.png" % (abs(hash(path)) % 100000))
    try:
        from PIL import Image
        im = Image.open(path)
        if im.mode not in ("RGB", "L", "RGBA", "LA", "P"):
            im = im.convert("RGB")
        im.save(out)
        return out
    except ImportError:
        pass
    if tools is not None and tools.mutool:
        run([tools.mutool, "draw", "-o", out, "-r", "72", path, "1"])
        if os.path.exists(out):
            return out
    raise ConvertError("%s: only JPEG and PNG are built in; other picture types need "
                       "MuPDF (mutool) or Pillow." % os.path.basename(path))


def images_to_pdf(paths, out_path, page_size="auto", margin=0, tools=None, progress=None):
    """Create a PDF with one picture per page.

    page_size: 'auto' (page = picture size), or 'a4', 'letter', ... (picture
    fitted and centered; landscape pages are used for wide pictures).
    """
    if not paths:
        raise ConvertError("no pictures given")
    workdir = tempfile.mkdtemp(prefix="leafpdf-img-")
    wr = PdfWriter(out_path + ".part")
    pages_num = wr.reserve()
    kids = []
    try:
        for i, path in enumerate(paths):
            if progress:
                progress(i, len(paths))
            ext = os.path.splitext(path)[1].lower()
            src = path
            if ext not in (".jpg", ".jpeg", ".jpe", ".png"):
                src = _to_png_or_jpeg(path, tools, workdir)
                ext = ".png"
            with open(src, "rb") as f:
                data = f.read()
            rotate = 0
            smask = None
            if ext == ".png":
                try:
                    d, sdata, smask, w, h, dpi = _png_image(data)
                except ConvertError:
                    src = _to_png_or_jpeg(path, tools, workdir)
                    with open(src, "rb") as f:
                        d, sdata, smask, w, h, dpi = _png_image(f.read())
            else:
                w, h, comps, adobe, orient, dpi = _jpeg_info(data)
                cs = {1: "DeviceGray", 3: "DeviceRGB", 4: "DeviceCMYK"}.get(comps, "DeviceRGB")
                d = {N("Type"): N("XObject"), N("Subtype"): N("Image"), N("Width"): w,
                     N("Height"): h, N("ColorSpace"): N(cs), N("BitsPerComponent"): 8,
                     N("Filter"): N("DCTDecode")}
                if comps == 4 and adobe:
                    d[N("Decode")] = [1, 0, 1, 0, 1, 0, 1, 0]
                sdata = data
                rotate = {3: 180, 6: 90, 8: 270}.get(orient, 0)
            if smask is not None:
                d[N("SMask")] = wr.add(_stream(smask[0], smask[1]))
            img_ref = wr.add(_stream(d, sdata))
            data = sdata = None
            dpi = dpi or 96
            iw, ih = w * 72.0 / dpi, h * 72.0 / dpi
            shown_w, shown_h = (ih, iw) if rotate in (90, 270) else (iw, ih)
            if page_size in PAGE_SIZES:
                pw, ph = PAGE_SIZES[page_size]
                if shown_w > shown_h:
                    pw, ph = ph, pw
                if rotate in (90, 270):
                    pw, ph = ph, pw   # page box is in unrotated space
                scale = min((pw - 2 * margin) / iw, (ph - 2 * margin) / ih)
                dw, dh = iw * scale, ih * scale
                x, y = (pw - dw) / 2.0, (ph - dh) / 2.0
            else:
                pw, ph = iw + 2 * margin, ih + 2 * margin
                dw, dh, x, y = iw, ih, margin, margin
            content = ("q %.3f 0 0 %.3f %.3f %.3f cm /Im0 Do Q" % (dw, dh, x, y)).encode("ascii")
            c_ref = wr.add(_stream({}, content))
            page = {N("Type"): N("Page"), N("Parent"): _ref(pages_num),
                    N("MediaBox"): [0, 0, round(pw, 3), round(ph, 3)],
                    N("Resources"): {N("XObject"): {N("Im0"): img_ref}},
                    N("Contents"): c_ref}
            if rotate:
                page[N("Rotate")] = rotate
            kids.append(wr.add(page))
        wr.write(pages_num, {N("Type"): N("Pages"), N("Kids"): kids, N("Count"): len(kids)})
        root = wr.add({N("Type"): N("Catalog"), N("Pages"): _ref(pages_num)})
        wr.close(root)
    except Exception:
        wr.f.close()
        os.remove(out_path + ".part")
        raise
    finally:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
    _move(out_path + ".part", out_path)
    return out_path


def _ref(n):
    from .pdf.objects import Ref
    return Ref(n)


def _move(src, dst):
    if os.path.exists(dst):
        os.remove(dst)
    os.rename(src, dst)


# ================================================================ text -> PDF

def text_to_pdf(text, out_path, title=None, font_size=10):
    """Simple monospaced text to PDF (A4), with wrapping."""
    pw, ph = PAGE_SIZES["a4"]
    margin = 50
    line_h = font_size * 1.25
    cols = int((pw - 2 * margin) / (font_size * 0.6))
    rows = int((ph - 2 * margin) / line_h)
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if "\f" in raw:
            parts = raw.split("\f")
            for k, part in enumerate(parts):
                lines.extend(_wrap(part, cols))
                if k < len(parts) - 1:
                    lines.append("\f")
            continue
        lines.extend(_wrap(raw.expandtabs(4), cols))
    pages = [[]]
    for ln in lines:
        if ln == "\f" or len(pages[-1]) >= rows:
            pages.append([])
            if ln == "\f":
                continue
        pages[-1].append(ln)
    wr = PdfWriter(out_path + ".part")
    pages_num = wr.reserve()
    font = wr.add({N("Type"): N("Font"), N("Subtype"): N("Type1"), N("BaseFont"): N("Courier"),
                   N("Encoding"): N("WinAnsiEncoding")})
    kids = []
    for pg in pages:
        ops = [b"BT", ("/F1 %d Tf %.2f TL %d %.2f Td" % (font_size, line_h, margin,
                                                         ph - margin - font_size)).encode("ascii")]
        for ln in pg:
            ops.append(serialize(PdfString(ln.encode("cp1252", "replace"))) + b" Tj T*")
        ops.append(b"ET")
        c = wr.add(_stream({N("Filter"): N("FlateDecode")}, zlib.compress(b"\n".join(ops))))
        kids.append(wr.add({N("Type"): N("Page"), N("Parent"): _ref(pages_num),
                            N("MediaBox"): [0, 0, pw, ph], N("Contents"): c,
                            N("Resources"): {N("Font"): {N("F1"): font}}}))
    wr.write(pages_num, {N("Type"): N("Pages"), N("Kids"): kids, N("Count"): len(kids)})
    wr.close(wr.add({N("Type"): N("Catalog"), N("Pages"): _ref(pages_num)}))
    _move(out_path + ".part", out_path)
    return out_path


def _wrap(line, cols):
    if len(line) <= cols:
        return [line]
    out = []
    while len(line) > cols:
        cut = line.rfind(" ", 0, cols)
        if cut <= cols // 3:
            cut = cols
        out.append(line[:cut])
        line = line[cut:].lstrip(" ")
    out.append(line)
    return out


# ============================================================ office -> PDF

_VBS = r'''
Dim a, src, dst, kind, app, d
Set a = WScript.Arguments
src = a(0): dst = a(1): kind = a(2)
On Error Resume Next
If kind = "word" Then
  Set app = CreateObject("Word.Application")
  app.Visible = False
  app.DisplayAlerts = 0
  Set d = app.Documents.Open(src, False, True, False)
  d.ExportAsFixedFormat dst, 17
  d.Close False
ElseIf kind = "word-from-pdf" Then
  Set app = CreateObject("Word.Application")
  app.Visible = False
  app.DisplayAlerts = 0
  Set d = app.Documents.Open(src, False, True, False)
  d.SaveAs dst, 16
  d.Close False
ElseIf kind = "excel" Then
  Set app = CreateObject("Excel.Application")
  app.Visible = False
  app.DisplayAlerts = False
  Set d = app.Workbooks.Open(src, 0, True)
  d.ExportAsFixedFormat 0, dst
  d.Close False
ElseIf kind = "powerpoint" Then
  Set app = CreateObject("PowerPoint.Application")
  Set d = app.Presentations.Open(src, True, False, False)
  d.SaveAs dst, 32
  d.Close
End If
If Err.Number <> 0 Then
  WScript.Echo "ERROR: " & Err.Description
  If IsObject(app) Then app.Quit
  WScript.Quit 1
End If
app.Quit
WScript.Quit 0
'''

_OFFICE_KIND = {".doc": "word", ".docx": "word", ".rtf": "word", ".odt": "word",
                ".txt": "word", ".wpd": "word", ".wps": "word", ".htm": "word",
                ".html": "word", ".xls": "excel", ".xlsx": "excel", ".ods": "excel",
                ".csv": "excel", ".ppt": "powerpoint", ".pptx": "powerpoint",
                ".odp": "powerpoint"}


def _ms_office(tools, src, dst, kind):
    if not (IS_WINDOWS and tools is not None and tools.cscript):
        return False
    fd, script = tempfile.mkstemp(suffix=".vbs")
    os.close(fd)
    with open(script, "w") as f:
        f.write(_VBS)
    try:
        code, out, err = run([tools.cscript, "//nologo", script, os.path.abspath(src), os.path.abspath(dst), kind],
                             timeout=600)
    finally:
        os.remove(script)
    return code == 0 and os.path.exists(dst)


def _libreoffice(tools, src, out_dir, target, infilter=None):
    if tools is None or not tools.soffice:
        return None
    profile = os.path.join(tempfile.gettempdir(), "leafpdf-lo-profile")
    url = "file:///" + profile.replace("\\", "/").lstrip("/")
    args = [tools.soffice, "-env:UserInstallation=" + url, "--headless", "--norestore"]
    if infilter:
        args.append("--infilter=" + infilter)
    args += ["--convert-to", target, "--outdir", out_dir, os.path.abspath(src)]
    run(args, timeout=900)
    base = os.path.splitext(os.path.basename(src))[0]
    out = os.path.join(out_dir, base + "." + target.split(":")[0])
    return out if os.path.exists(out) else None


def office_to_pdf(src, out_path, tools=None, engine="auto"):
    ext = os.path.splitext(src)[1].lower()
    tried = []
    if engine in ("auto", "office") and ext in _OFFICE_KIND:
        tried.append("Microsoft Office")
        if _ms_office(tools, src, out_path, _OFFICE_KIND[ext]):
            return out_path
    if engine in ("auto", "libreoffice"):
        tried.append("LibreOffice")
        tmpdir = tempfile.mkdtemp(prefix="leafpdf-lo-")
        try:
            out = _libreoffice(tools, src, tmpdir, "pdf")
            if out:
                _move_any(out, out_path)
                return out_path
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
    if ext in (".txt", ".csv", ".log", ".ini", ".md"):
        with open(src, "rb") as f:
            raw = f.read()
        for enc in ("utf-8", "cp1252", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        return text_to_pdf(text, out_path)
    raise ConvertError("Could not convert %s. Install Microsoft Office 2007+ or "
                       "LibreOffice (tried: %s)." % (os.path.basename(src),
                                                    ", ".join(tried) or "nothing"))


def _move_any(src, dst):
    import shutil
    if os.path.exists(dst):
        os.remove(dst)
    shutil.move(src, dst)


# ============================================================ PDF -> layout

class Line(object):
    def __init__(self, x0, y0, x1, y1, cells, size=0):
        self.x0, self.y0, self.x1, self.y1 = x0, y0, x1, y1
        self.cells = cells   # list of (x0, text)
        self.size = size

    @property
    def text(self):
        return " ".join(t for _, t in self.cells)


def _parse_stext(xml_bytes):
    """Parse MuPDF stext XML into blocks of Lines (tolerates many versions)."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    blocks = []
    for block in root.iter("block"):
        lines = []
        for line in block.iter("line"):
            chars = []
            size = 0.0
            for el in line.iter():
                if el.tag == "font":
                    try:
                        size = float(el.get("size", size))
                    except ValueError:
                        pass
                if el.tag != "char":
                    continue
                c = el.get("c")
                if c is None:
                    continue
                quad = el.get("quad") or el.get("bbox")
                if quad:
                    q = [float(v) for v in quad.split()]
                    x0, x1 = min(q[0::2]), max(q[0::2])
                elif el.get("x") is not None:
                    x0 = float(el.get("x"))
                    x1 = x0 + size * 0.5
                else:
                    continue
                chars.append((x0, x1, c))
            if not chars:
                continue
            bbox = [float(v) for v in (line.get("bbox") or "0 0 0 0").split()]
            cells = []
            cur = ""
            cur_x = chars[0][0]
            prev_x1 = chars[0][0]
            gap_limit = max(size, 6) * 1.5
            for x0, x1, c in chars:
                if cur and x0 - prev_x1 > gap_limit:
                    cells.append((cur_x, cur.strip()))
                    cur = ""
                    cur_x = x0
                cur += c
                prev_x1 = x1
            if cur.strip():
                cells.append((cur_x, cur.strip()))
            if len(bbox) < 4:
                bbox = [chars[0][0], 0, chars[-1][1], 0]
            lines.append(Line(bbox[0], bbox[1], bbox[2], bbox[3], cells, size))
        if lines:
            blocks.append(lines)
    return blocks


def page_layout(pdf_path, page, renderer=None, doc=None):
    """Return a list of blocks (paragraphs); each block is a list of Lines."""
    if renderer is not None and renderer.tools.mutool:
        xml = renderer.stext(pdf_path, page)
        if xml:
            blocks = _parse_stext(xml)
            if blocks:
                return blocks
    text = None
    if renderer is not None and renderer.available():
        text = renderer.text(pdf_path, [page])
    if text is None and doc is not None:
        from .pdf.editor import page_text
        text = page_text(doc, page)
    blocks = []
    cur = []
    for raw in (text or "").replace("\f", "").split("\n"):
        if not raw.strip():
            if cur:
                blocks.append(cur)
                cur = []
            continue
        cells = [(i, t) for i, t in enumerate(re.split(r"\s{2,}", raw.strip())) if t]
        cur.append(Line(0, 0, 0, 0, cells))
    if cur:
        blocks.append(cur)
    return blocks


# ================================================================ writers

def _zip(path, files):
    tmp = path + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files:
            if isinstance(data, str):
                data = data.encode("utf-8")
            z.writestr(name, data)
    _move(tmp, path)


def _x(text):
    # strip characters that are illegal in XML 1.0
    text = re.sub("[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return xml_escape(text)


def write_docx(path, pages_blocks, title="Document"):
    body = []
    for pi, blocks in enumerate(pages_blocks):
        for lines in blocks:
            text = " ".join(l.text for l in lines).strip()
            if not text:
                continue
            size = max([l.size for l in lines] + [0])
            rpr = ""
            if size >= 15:
                rpr = "<w:rPr><w:b/><w:sz w:val=\"%d\"/></w:rPr>" % int(min(size, 40) * 2)
            body.append('<w:p><w:r>%s<w:t xml:space="preserve">%s</w:t></w:r></w:p>' %
                        (rpr, _x(text)))
        if pi < len(pages_blocks) - 1:
            body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
           '<w:body>%s<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
           '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" '
           'w:header="709" w:footer="709" w:gutter="0"/></w:sectPr></w:body></w:document>'
           % "".join(body))
    files = [
        ("[Content_Types].xml",
         '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
         '<Default Extension="xml" ContentType="application/xml"/>'
         '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
         '</Types>'),
        ("_rels/.rels",
         '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
         '</Relationships>'),
        ("word/document.xml", doc),
    ]
    _zip(path, files)
    return path


def _rtf_escape(text):
    out = []
    for ch in text:
        o = ord(ch)
        if ch in "\\{}":
            out.append("\\" + ch)
        elif o < 128:
            out.append(ch)
        else:
            if o > 32767:
                o -= 65536
            out.append("\\u%d?" % o)
    return "".join(out)


def write_rtf(path, pages_blocks):
    """RTF opens in WordPad on every Windows version, including XP."""
    parts = ["{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0 Arial;}}\\fs22\n"]
    for pi, blocks in enumerate(pages_blocks):
        for lines in blocks:
            text = " ".join(l.text for l in lines).strip()
            if text:
                parts.append(_rtf_escape(text) + "\\par\\par\n")
        if pi < len(pages_blocks) - 1:
            parts.append("\\page\n")
    parts.append("}")
    with open(path, "w") as f:
        f.write("".join(parts))
    return path


def _col(n):
    s = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def write_xlsx(path, pages_blocks):
    """One worksheet per page (max 200); columns detected from text gaps."""
    sheets = []
    for pi, blocks in enumerate(pages_blocks[:200]):
        lines = [l for b in blocks for l in b]
        # column anchors: cluster cell x positions
        xs = sorted(set(round(x) for l in lines for x, _ in l.cells))
        anchors = []
        for x in xs:
            if not anchors or x - anchors[-1] > 12:
                anchors.append(x)
        rows = []
        for r, l in enumerate(lines):
            cells = []
            used = set()
            for x, t in l.cells:
                c = 0
                if anchors:
                    c = min(range(len(anchors)), key=lambda k: abs(anchors[k] - x))
                while c in used:
                    c += 1
                used.add(c)
                ref = "%s%d" % (_col(c), r + 1)
                num = t.replace(",", "")
                if re.match(r"^-?\d+(\.\d+)?$", num) and len(num) < 15:
                    cells.append('<c r="%s"><v>%s</v></c>' % (ref, num))
                else:
                    cells.append('<c r="%s" t="inlineStr"><is><t>%s</t></is></c>' % (ref, _x(t)))
            rows.append('<row r="%d">%s</row>' % (r + 1, "".join(cells)))
        sheets.append('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                      '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                      '<sheetData>%s</sheetData></worksheet>' % "".join(rows))
    if not sheets:
        sheets.append('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                      '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                      '<sheetData/></worksheet>')
    ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>']
    wb_sheets = []
    wb_rels = []
    files = []
    for i, s in enumerate(sheets):
        n = i + 1
        ct.append('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % n)
        wb_sheets.append('<sheet name="Page %d" sheetId="%d" r:id="rId%d"/>' % (n, n, n))
        wb_rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (n, n))
        files.append(("xl/worksheets/sheet%d.xml" % n, s))
    ct.append("</Types>")
    files = [
        ("[Content_Types].xml", "".join(ct)),
        ("_rels/.rels",
         '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
         '</Relationships>'),
        ("xl/workbook.xml",
         '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
         'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
         '<sheets>%s</sheets></workbook>' % "".join(wb_sheets)),
        ("xl/_rels/workbook.xml.rels",
         '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">%s'
         '</Relationships>' % "".join(wb_rels)),
    ] + files
    _zip(path, files)
    return path


_PPT_NS = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
           'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"')
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
_XMLH = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def _png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    return struct.unpack(">II", head[16:24])


def write_pptx(path, images):
    """One slide per page picture (PNG files)."""
    w0, h0 = _png_size(images[0])
    cx = 9144000
    cy = int(cx * h0 / float(w0))
    files = []
    ct = [_XMLH, '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/>'
          '<Default Extension="png" ContentType="image/png"/>'
          '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>'
          '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>'
          '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>'
          '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>']
    sld_ids = []
    pres_rels = ['<Relationship Id="rId1" Type="%sslideMaster" Target="slideMasters/slideMaster1.xml"/>' % _REL,
                 '<Relationship Id="rId2" Type="%stheme" Target="theme/theme1.xml"/>' % _REL]
    empty_tree = ('<p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/>'
                  '<p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>%s</p:spTree></p:cSld>')
    for i, img in enumerate(images):
        n = i + 1
        with open(img, "rb") as f:
            files.append(("ppt/media/image%d.png" % n, f.read()))
        iw, ih = _png_size(img)
        scale = min(cx / float(iw), cy / float(ih))
        w, h = int(iw * scale), int(ih * scale)
        x, y = (cx - w) // 2, (cy - h) // 2
        pic = ('<p:pic><p:nvPicPr><p:cNvPr id="2" name="Page %d"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
               '<p:blipFill><a:blip r:embed="rId2"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>'
               '<p:spPr><a:xfrm><a:off x="%d" y="%d"/><a:ext cx="%d" cy="%d"/></a:xfrm>'
               '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>' % (n, x, y, w, h))
        files.append(("ppt/slides/slide%d.xml" % n,
                      _XMLH + '<p:sld %s>%s<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sld>'
                      % (_PPT_NS, empty_tree % pic)))
        files.append(("ppt/slides/_rels/slide%d.xml.rels" % n,
                      _XMLH + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                      '<Relationship Id="rId1" Type="%sslideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
                      '<Relationship Id="rId2" Type="%simage" Target="../media/image%d.png"/>'
                      '</Relationships>' % (_REL, _REL, n)))
        ct.append('<Override PartName="/ppt/slides/slide%d.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>' % n)
        sld_ids.append('<p:sldId id="%d" r:id="rId%d"/>' % (255 + n, n + 2))
        pres_rels.append('<Relationship Id="rId%d" Type="%sslide" Target="slides/slide%d.xml"/>' % (n + 2, _REL, n))
    ct.append("</Types>")
    files += [
        ("[Content_Types].xml", "".join(ct)),
        ("_rels/.rels", _XMLH + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="%sofficeDocument" Target="ppt/presentation.xml"/></Relationships>' % _REL),
        ("ppt/presentation.xml", _XMLH + '<p:presentation %s><p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/>'
         '</p:sldMasterIdLst><p:sldIdLst>%s</p:sldIdLst><p:sldSz cx="%d" cy="%d"/>'
         '<p:notesSz cx="6858000" cy="9144000"/></p:presentation>' % (_PPT_NS, "".join(sld_ids), cx, cy)),
        ("ppt/_rels/presentation.xml.rels", _XMLH + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">%s</Relationships>' % "".join(pres_rels)),
        ("ppt/slideMasters/slideMaster1.xml", _XMLH + '<p:sldMaster %s>%s'
         '<p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" '
         'accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" '
         'folHlink="folHlink"/><p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/>'
         '</p:sldLayoutIdLst></p:sldMaster>' % (_PPT_NS, empty_tree % "")),
        ("ppt/slideMasters/_rels/slideMaster1.xml.rels", _XMLH + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="%sslideLayout" Target="../slideLayouts/slideLayout1.xml"/>'
         '<Relationship Id="rId2" Type="%stheme" Target="../theme/theme1.xml"/></Relationships>' % (_REL, _REL)),
        ("ppt/slideLayouts/slideLayout1.xml", _XMLH + '<p:sldLayout %s type="blank">%s'
         '<p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr></p:sldLayout>' % (_PPT_NS, empty_tree % "")),
        ("ppt/slideLayouts/_rels/slideLayout1.xml.rels", _XMLH + '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="%sslideMaster" Target="../slideMasters/slideMaster1.xml"/></Relationships>' % _REL),
        ("ppt/theme/theme1.xml", _theme_xml()),
    ]
    _zip(path, files)
    return path


def _theme_xml():
    colors = "".join('<a:%s><a:srgbClr val="%s"/></a:%s>' % (n, v, n) for n, v in [
        ("dk1", "000000"), ("lt1", "FFFFFF"), ("dk2", "1F497D"), ("lt2", "EEECE1"),
        ("accent1", "4F81BD"), ("accent2", "C0504D"), ("accent3", "9BBB59"),
        ("accent4", "8064A2"), ("accent5", "4BACC6"), ("accent6", "F79646"),
        ("hlink", "0000FF"), ("folHlink", "800080")])
    fill = '<a:solidFill><a:schemeClr val="phClr"/></a:solidFill>'
    line = '<a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>'
    return (_XMLH + '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="LeafPDF">'
            '<a:themeElements><a:clrScheme name="LeafPDF">%s</a:clrScheme>'
            '<a:fontScheme name="LeafPDF"><a:majorFont><a:latin typeface="Arial"/><a:ea typeface=""/>'
            '<a:cs typeface=""/></a:majorFont><a:minorFont><a:latin typeface="Arial"/><a:ea typeface=""/>'
            '<a:cs typeface=""/></a:minorFont></a:fontScheme>'
            '<a:fmtScheme name="LeafPDF"><a:fillStyleLst>%s%s%s</a:fillStyleLst>'
            '<a:lnStyleLst>%s%s%s</a:lnStyleLst>'
            '<a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle><a:effectStyle><a:effectLst/>'
            '</a:effectStyle><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst>'
            '<a:bgFillStyleLst>%s%s%s</a:bgFillStyleLst></a:fmtScheme></a:themeElements></a:theme>'
            % (colors, fill, fill, fill, line, line, line, fill, fill, fill))


def write_html(path, pages_blocks, title):
    out = ['<!DOCTYPE html><html><head><meta charset="utf-8"><title>%s</title>'
           '<style>body{font-family:Arial,sans-serif;max-width:50em;margin:2em auto;'
           'line-height:1.45}hr{margin:2em 0}</style></head><body>' % _x(title)]
    for pi, blocks in enumerate(pages_blocks):
        out.append('<div class="page" id="page%d">' % (pi + 1))
        for lines in blocks:
            text = " ".join(l.text for l in lines).strip()
            if text:
                size = max([l.size for l in lines] + [0])
                tag = "h2" if size >= 15 else "p"
                out.append("<%s>%s</%s>" % (tag, _x(text), tag))
        out.append("</div><hr>")
    out.append("</body></html>")
    with open(path, "wb") as f:
        f.write("\n".join(out).encode("utf-8"))
    return path


# ============================================================ PDF -> other

def pdf_to(src, out_path, fmt, renderer=None, doc=None, pages=None, dpi=150,
           tools=None, progress=None):
    """Convert ``src`` PDF to ``fmt``.  Returns a list of written files."""
    fmt = fmt.lower().lstrip(".")
    if fmt == "jpeg":
        fmt = "jpg"
    if fmt == "tif":
        fmt = "tiff"
    own_doc = False
    if doc is None:
        from .pdf.document import PdfDocument
        try:
            doc = PdfDocument(src)
            own_doc = True
        except Exception:
            doc = None
    try:
        total = doc.page_count() if doc is not None else None
        if pages is None:
            if total is None:
                raise ConvertError("cannot read page count")
            pages = list(range(total))
        if fmt in ("png", "jpg", "tiff", "bmp"):
            if renderer is None or not renderer.available():
                raise ConvertError("Converting to pictures needs MuPDF or Ghostscript.")
            base, ext = os.path.splitext(out_path)
            pattern = base.replace("%", "%%") + "-%03d." + fmt
            return renderer.render_to_files(src, pattern, dpi, pages, fmt)
        if fmt == "pptx":
            if renderer is None or not renderer.available():
                raise ConvertError("PDF to PowerPoint needs MuPDF or Ghostscript.")
            tmpdir = tempfile.mkdtemp(prefix="leafpdf-pptx-")
            try:
                imgs = []
                for k, p in enumerate(pages):
                    if progress:
                        progress(k, len(pages))
                    imgs += renderer.render_to_files(src, os.path.join(tmpdir, "p%05d.png"),
                                                     dpi, [p], "png")
                if not imgs:
                    raise ConvertError("rendering failed")
                return [write_pptx(out_path, imgs)]
            finally:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)
        layouts = []
        for k, p in enumerate(pages):
            if progress:
                progress(k, len(pages))
            layouts.append(page_layout(src, p, renderer, doc))
        title = os.path.splitext(os.path.basename(src))[0]
        if fmt == "txt":
            with open(out_path, "wb") as f:
                for k, blocks in enumerate(layouts):
                    for lines in blocks:
                        for l in lines:
                            f.write((l.text + "\r\n").encode("utf-8"))
                        f.write(b"\r\n")
                    if k < len(layouts) - 1:
                        f.write(b"\x0c")
            return [out_path]
        if fmt == "docx":
            return [write_docx(out_path, layouts, title)]
        if fmt == "rtf":
            return [write_rtf(out_path, layouts)]
        if fmt in ("html", "htm"):
            return [write_html(out_path, layouts, title)]
        if fmt == "xlsx":
            return [write_xlsx(out_path, layouts)]
        if fmt in ("odt", "doc"):
            tmpdir = tempfile.mkdtemp(prefix="leafpdf-lo-")
            try:
                d = write_docx(os.path.join(tmpdir, title + ".docx"), layouts, title)
                out = _libreoffice(tools, d, tmpdir, fmt)
                if not out:
                    raise ConvertError("Saving as .%s needs LibreOffice. DOCX and RTF work "
                                       "without it." % fmt)
                _move_any(out, out_path)
                return [out_path]
            finally:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)
        raise ConvertError("unknown target format '%s'" % fmt)
    finally:
        if own_doc and doc is not None:
            doc.close()


def convert_any(src, out_path, tools=None, renderer=None, page_size="auto", progress=None):
    """Convert by file extensions: picture/office -> PDF, or PDF -> anything."""
    sext = os.path.splitext(src)[1].lower()
    dext = os.path.splitext(out_path)[1].lower()
    if sext == ".pdf":
        return pdf_to(src, out_path, dext, renderer=renderer, tools=tools, progress=progress)
    if dext != ".pdf":
        raise ConvertError("can only convert to or from PDF")
    if sext in IMAGE_EXTS:
        return [images_to_pdf([src], out_path, page_size, tools=tools)]
    return [office_to_pdf(src, out_path, tools)]
