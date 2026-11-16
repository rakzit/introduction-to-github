"""LeafPDF tests (stdlib unittest; run: python -m unittest discover tests)."""

import json
import os
import shutil
import struct
import sys
import tempfile
import unittest
import zipfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from leafpdf.pdf.document import PdfDocument  # noqa: E402
from leafpdf.pdf.objects import N, Ref, serialize, Stream  # noqa: E402
from leafpdf.pdf.lexer import parse  # noqa: E402
from leafpdf.pdf.filters import ascii85_decode, apply_predictor  # noqa: E402
from leafpdf.pdf.outline import (OutlineItem, read_outline, write_outline,  # noqa: E402
                                 current_item, flatten)
from leafpdf.pdf.editor import parse_pages, page_text, EditError  # noqa: E402
from leafpdf.commands import EditSession, tokenize, CommandError  # noqa: E402
from leafpdf.convert import (text_to_pdf, images_to_pdf, PdfWriter, write_docx,  # noqa: E402
                             write_xlsx, write_rtf, pdf_to, Line)
from leafpdf.session import State  # noqa: E402
from leafpdf.tools import Tools  # noqa: E402
from leafpdf.render import Renderer  # noqa: E402

TOOLS = Tools()


def make_text_pdf(path, pages=5, text="Hello World page {n}"):
    body = "\f".join(text.replace("{n}", str(i + 1)) for i in range(pages))
    return text_to_pdf(body, path)


def make_nested_pdf(path, leaves=(3, 2, 4)):
    """A PDF whose page tree has intermediate /Pages nodes and inherited
    MediaBox/Resources, like big real-world files."""
    w = PdfWriter(path)
    root_num = w.reserve()
    font = w.add({N("Type"): N("Font"), N("Subtype"): N("Type1"), N("BaseFont"): N("Helvetica")})
    kids = []
    n = 0
    for count in leaves:
        mid = w.reserve()
        pages = []
        for _ in range(count):
            n += 1
            c = w.add(Stream({}, ("BT /F1 24 Tf 72 700 Td (Page %d of tree) Tj ET" % n).encode()))
            pages.append(w.add({N("Type"): N("Page"), N("Parent"): Ref(mid), N("Contents"): c}))
        w.write(mid, {N("Type"): N("Pages"), N("Parent"): Ref(root_num), N("Kids"): pages,
                      N("Count"): count})
        kids.append(Ref(mid))
    w.write(root_num, {N("Type"): N("Pages"), N("Kids"): kids, N("Count"): n,
                       N("MediaBox"): [0, 0, 612, 792],
                       N("Resources"): {N("Font"): {N("F1"): font}}})
    w.close(w.add({N("Type"): N("Catalog"), N("Pages"): Ref(root_num)}))
    return path


def make_png(path, w=4, h=3, color_type=6):
    rows = b""
    for y in range(h):
        row = b"\x00"
        for x in range(w):
            if color_type == 6:
                row += bytes([x * 60, y * 80, 200, 128])
            elif color_type == 2:
                row += bytes([x * 60, y * 80, 200])
            else:
                row += bytes([(x + y) % 2])
        rows += row

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
    if color_type == 3:
        data += chunk(b"PLTE", b"\xff\x00\x00\x00\x00\xff")
    data += chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    with open(path, "wb") as f:
        f.write(data)
    return path


def make_jpeg(path):
    # minimal baseline JPEG header + SOF0 (enough for header parsing and embedding test)
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 17, 8, 10, 20, 3) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    with open(path, "wb") as f:
        f.write(b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x01\x00\x48\x00\x48\x00\x00" +
                sof + b"\xff\xd9")
    return path


def mutool_ok(path):
    if not TOOLS.mutool:
        return True
    from leafpdf.tools import run
    code, out, err = run([TOOLS.mutool, "info", path])
    return code == 0 and b"error" not in err.lower()


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="leafpdf-test-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def p(self, name):
        return os.path.join(self.dir, name)


class TestObjects(Base):
    def test_lexer_roundtrip(self):
        src = b"<< /Type /Page /Kids [1 0 R 2 0 R] /S (a\\(b\\)c) /H <414243> /N 3.5 /B true /Z null /E /A#20B >>"
        obj = parse(src)
        self.assertEqual(obj["Type"], "Page")
        self.assertEqual(obj["Kids"], [Ref(1), Ref(2)])
        self.assertEqual(bytes(obj["S"]), b"a(b)c")
        self.assertEqual(bytes(obj["H"]), b"ABC")
        self.assertTrue(obj["B"])
        self.assertIsNone(obj["Z"])
        self.assertEqual(obj["E"], "A B")
        self.assertEqual(parse(serialize(obj)), obj)

    def test_filters(self):
        self.assertEqual(ascii85_decode(b"<~87cURD]i,\"Ebo80~>"), b"Hello World!")
        self.assertEqual(ascii85_decode(b"<~87cURD]i,\"Ebo7~>"), b"Hello World")
        data = b"\x02\x01\x01\x02\x02\x02"  # PNG 'Up' filter rows
        self.assertEqual(apply_predictor(data, {"Predictor": 12, "Columns": 2}), b"\x01\x01\x03\x03")

    def test_page_specs(self):
        self.assertEqual(parse_pages("1-3,7", 10), [0, 1, 2, 6])
        self.assertEqual(parse_pages("8-", 10), [7, 8, 9])
        self.assertEqual(parse_pages("odd", 5), [0, 2, 4])
        self.assertEqual(parse_pages("last", 5), [4])
        self.assertEqual(parse_pages("last-1", 5), [3])
        self.assertRaises(EditError, parse_pages, "11", 10)

    def test_tokenizer(self):
        cmds = tokenize('replace "a \\"b\\"" \'c\' pages 1-2; number\n# comment\nrotate 90')
        self.assertEqual(cmds[0], ["replace", ("q", 'a "b"'), ("q", "c"), "pages", "1-2"])
        self.assertEqual(cmds[1], ["number"])
        self.assertEqual(cmds[2], ["rotate", "90"])


class TestDocument(Base):
    def test_open_and_pages(self):
        path = make_text_pdf(self.p("a.pdf"), 4)
        with PdfDocument(path) as d:
            self.assertEqual(d.page_count(), 4)
            self.assertIn("Hello World page 3", page_text(d, 2))
            w, h = d.page_size(0)
            self.assertAlmostEqual(w, 595.28, 1)

    def test_nested_tree(self):
        path = make_nested_pdf(self.p("n.pdf"))
        with PdfDocument(path) as d:
            self.assertEqual(d.page_count(), 9)
            self.assertIn("Page 6 of tree", page_text(d, 5))
            self.assertEqual(d.page_size(8), (612.0, 792.0))

    def test_repair_damaged_xref(self):
        path = make_text_pdf(self.p("a.pdf"), 3)
        with open(path, "rb") as f:
            data = f.read()
        i = data.rindex(b"startxref")
        data = data[:i] + b"startxref\n999999\n%%EOF\n"
        with open(path, "wb") as f:
            f.write(data)
        with PdfDocument(path) as d:
            self.assertTrue(d.repaired)
            self.assertEqual(d.page_count(), 3)

    def test_compact_rewrite(self):
        path = make_nested_pdf(self.p("n.pdf"))
        with PdfDocument(path) as d:
            d.write_compact(self.p("part.pdf"), [8, 0])
        with PdfDocument(self.p("part.pdf")) as d:
            self.assertEqual(d.page_count(), 2)
            self.assertIn("Page 9", page_text(d, 0))
        self.assertTrue(mutool_ok(self.p("part.pdf")))


class TestEditing(Base):
    def session(self, path):
        return EditSession(path, self.p("work"))

    def test_replace_in_font_and_undo_redo(self):
        path = make_text_pdf(self.p("a.pdf"), 3)
        s = self.session(path)
        msgs = s.run('replace "World" "Earth" pages 1-2')
        self.assertIn("Replaced 2", msgs[0])
        self.assertIn("Hello Earth page 1", page_text(s.doc, 0))
        self.assertIn("Hello World page 3", page_text(s.doc, 2))
        self.assertTrue(s.dirty)
        s.undo()
        self.assertIn("Hello World page 1", page_text(s.doc, 0))
        s.redo()
        self.assertIn("Hello Earth page 1", page_text(s.doc, 0))
        s.save()
        self.assertFalse(s.dirty)
        s.close()
        with PdfDocument(path) as d:
            self.assertIn("Earth", page_text(d, 1))
        self.assertTrue(mutool_ok(path))

    def test_replace_ignore_case(self):
        path = make_text_pdf(self.p("a.pdf"), 1)
        s = self.session(path)
        s.run('replace "hello world" "Bye" -i')
        self.assertIn("Bye page 1", page_text(s.doc, 0))
        s.close()

    def test_replace_fallback_font(self):
        # A subset-like font with only a few glyphs forces the overlay fallback.
        w = PdfWriter(self.p("sub.pdf"))
        root = w.reserve()
        font = w.add({N("Type"): N("Font"), N("Subtype"): N("TrueType"),
                      N("BaseFont"): N("ABCDEF+Tiny"), N("FirstChar"): 72,
                      N("Widths"): [600 if chr(c) in "Helo" else 0 for c in range(72, 112)],
                      N("Encoding"): N("WinAnsiEncoding")})
        c = w.add(Stream({}, b"BT /F1 12 Tf 100 700 Td (Hello) Tj ET"))
        page = w.add({N("Type"): N("Page"), N("Parent"): Ref(root), N("Contents"): c,
                      N("MediaBox"): [0, 0, 612, 792],
                      N("Resources"): {N("Font"): {N("F1"): font}}})
        w.write(root, {N("Type"): N("Pages"), N("Kids"): [page], N("Count"): 1})
        w.close(w.add({N("Type"): N("Catalog"), N("Pages"): Ref(root)}))
        s = self.session(self.p("sub.pdf"))
        msgs = s.run('replace "Hello" "Zap!"')
        self.assertIn("Replaced 1", msgs[0])
        self.assertIn("Helvetica", msgs[0])
        text = page_text(s.doc, 0)
        self.assertNotIn("Hello", text)
        self.assertIn("Zap!", text)
        s.close()

    def test_stamps_and_numbers(self):
        path = make_text_pdf(self.p("a.pdf"), 3)
        s = self.session(path)
        s.run('number "P{n}/{total}"; header "Top" pages 1; watermark "DRAFT" pages 2; '
              'stamp "Here" at 10 20 pages 3 color #ff0000; whiteout 0 0 50 50 pages 1')
        self.assertIn("P2/3", page_text(s.doc, 1))
        self.assertIn("DRAFT", page_text(s.doc, 1))
        self.assertIn("Top", page_text(s.doc, 0))
        self.assertIn("Here", page_text(s.doc, 2))
        s.save()
        s.close()
        self.assertTrue(mutool_ok(path))

    def test_page_operations(self):
        path = make_nested_pdf(self.p("n.pdf"))
        s = self.session(path)
        s.run("delete pages 2-3")
        self.assertEqual(s.doc.page_count(), 7)
        self.assertIn("Page 4", page_text(s.doc, 1))
        s.run("rotate 90 pages 1")
        self.assertEqual(s.doc.page_size(0), (792.0, 612.0))
        s.run("move pages 7 to 1")
        self.assertIn("Page 9", page_text(s.doc, 0))
        s.run("blank after 1 count 2")
        self.assertEqual(s.doc.page_count(), 9)
        self.assertEqual(page_text(s.doc, 1).strip(), "")
        s.run("reverse")
        self.assertIn("Page 9", page_text(s.doc, 8))
        s.save()
        s.close()
        self.assertTrue(mutool_ok(path))
        with PdfDocument(path) as d:
            self.assertEqual(d.page_count(), 9)

    def test_merge_and_extract(self):
        a = make_text_pdf(self.p("a.pdf"), 2, "Alpha {n}")
        make_nested_pdf(self.p("b.pdf"), (2, 1))
        s = self.session(a)
        s.run('merge "b.pdf" after 1 pages 2-3')
        self.assertEqual(s.doc.page_count(), 4)
        self.assertIn("Page 2 of tree", page_text(s.doc, 1))
        self.assertIn("Alpha 2", page_text(s.doc, 3))
        s.run('extract pages 2-3 to "x.pdf"')
        s.save()
        s.close()
        with PdfDocument(self.p("x.pdf")) as d:
            self.assertEqual(d.page_count(), 2)
        self.assertTrue(mutool_ok(a))

    def test_errors_leave_document_clean(self):
        path = make_text_pdf(self.p("a.pdf"), 2)
        s = self.session(path)
        self.assertRaises(CommandError, s.run, "frobnicate")
        self.assertRaises(EditError, s.run, "delete pages 1-2")
        self.assertEqual(s.doc.page_count(), 2)
        self.assertFalse(s.dirty)
        s.close()

    def test_xref_stream_incremental(self):
        # build a file that uses an xref *stream*, then edit it
        path = make_text_pdf(self.p("a.pdf"), 2)
        with PdfDocument(path) as d:
            d.xref_is_stream = True
            d.update(d.page_refs()[0], dict(d.get(d.page_refs()[0]), **{N("Rotate"): 180}))
            d.commit()
        with PdfDocument(path) as d:
            self.assertTrue(d.xref_is_stream)
            self.assertEqual(d.page_box(0)[4], 180)
        self.assertTrue(mutool_ok(path))


class TestOutline(Base):
    def test_roundtrip_and_current_topic(self):
        path = make_text_pdf(self.p("a.pdf"), 6)
        s = EditSession(path, self.p("work"))
        items = [OutlineItem("One", 0, 800, [OutlineItem("One.a", 1, None)], True),
                 OutlineItem("Two é中", 3, 400)]
        s.apply(lambda d: write_outline(d, items), "outline")
        got = read_outline(s.doc)
        self.assertEqual([(d, i.title, i.page) for d, i in flatten(got)],
                         [(0, "One", 0), (1, "One.a", 1), (0, "Two é中", 3)])
        self.assertEqual(current_item(got, 2, 0.5).title, "One.a")
        self.assertEqual(current_item(got, 3, 0.0).title, "One.a")   # above 'Two' on page 4
        self.assertEqual(current_item(got, 3, 0.9).title, "Two é中")
        s.close()


class TestConvert(Base):
    def test_images_to_pdf(self):
        imgs = [make_png(self.p("rgba.png")), make_png(self.p("rgb.png"), color_type=2),
                make_png(self.p("pal.png"), color_type=3), make_jpeg(self.p("x.jpg"))]
        out = images_to_pdf(imgs, self.p("img.pdf"), "a4", margin=10)
        with PdfDocument(out) as d:
            self.assertEqual(d.page_count(), 4)
            self.assertEqual(d.page_size(0), (841.89, 595.28))  # wide picture -> landscape
        if TOOLS.mutool:
            r = Renderer(TOOLS, self.p("cache"))
            for i in range(3):
                self.assertTrue(os.path.getsize(r.render(out, i, 30)) > 0)

    def test_office_writers(self):
        blocks = [[[Line(0, 0, 0, 0, [(0, "Name"), (100, "Qty")], 16)],
                   [Line(0, 0, 0, 0, [(0, "Apple"), (100, "3")])]],
                  [[Line(0, 0, 0, 0, [(0, "Page two & <more>")])]]]
        for fn, name in ((write_docx, "o.docx"), (write_xlsx, "o.xlsx")):
            fn(self.p(name), blocks)
            with zipfile.ZipFile(self.p(name)) as z:
                self.assertIsNone(z.testzip())
        with zipfile.ZipFile(self.p("o.xlsx")) as z:
            self.assertIn("<v>3</v>", z.read("xl/worksheets/sheet1.xml").decode())
        write_rtf(self.p("o.rtf"), blocks)
        with open(self.p("o.rtf")) as f:
            self.assertIn("\\page", f.read())

    def test_pdf_to_text_formats(self):
        path = make_text_pdf(self.p("a.pdf"), 2)
        r = Renderer(TOOLS, self.p("cache"))
        out = pdf_to(path, self.p("a.txt"), "txt", renderer=r)
        with open(out[0], "rb") as f:
            self.assertIn(b"Hello World page 2", f.read())
        pdf_to(path, self.p("a.docx"), "docx", renderer=r)
        with zipfile.ZipFile(self.p("a.docx")) as z:
            self.assertIn("Hello World page 1", z.read("word/document.xml").decode())


class TestSession(Base):
    def test_state_persistence(self):
        os.environ["LEAFPDF_HOME"] = self.p("home")
        st = State()
        st.set_open([{"path": __file__, "page": 7, "zoom": 1.5}], 0)
        st.bookmarks(__file__).append({"name": "Here", "page": 3})
        st.save()
        st2 = State()
        tabs, active = st2.open_tabs()
        self.assertEqual(tabs[0]["page"], 7)
        self.assertEqual(st2.bookmarks(__file__)[0]["name"], "Here")
        with open(st2.path) as f:
            json.load(f)

    def test_working_copy_survives_restart(self):
        path = make_text_pdf(self.p("a.pdf"), 2)
        s = EditSession(path, self.p("work"))
        s.run('replace "World" "There"')
        work, hist, saved = s.work, s.history, s.saved_at
        s.close()
        s2 = EditSession(path, self.p("work"), work, hist, saved)
        self.assertTrue(s2.dirty)
        self.assertIn("There", page_text(s2.doc, 0))
        s2.undo()
        self.assertIn("World", page_text(s2.doc, 0))
        s2.close()


if __name__ == "__main__":
    unittest.main()
