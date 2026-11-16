"""Command line entry point.

  leafpdf [FILE...]                         open the window
  leafpdf edit IN.pdf "COMMANDS" [-o OUT]   run edit commands (see 'help')
  leafpdf convert SRC DST [--page-size a4]  convert by file extension
  leafpdf images OUT.pdf PIC... [--page-size a4]
  leafpdf info FILE.pdf                     page count, size, metadata
  leafpdf outline FILE.pdf                  print the topics (bookmarks)
  leafpdf text FILE.pdf [PAGES]             print the text of pages
  leafpdf tools                             show detected helper programs
  leafpdf help                              edit command reference
"""

import os
import sys

USAGE = __doc__


def _opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            v = args[i + 1]
            del args[i:i + 2]
            return v
    return default


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or (args[0] not in ("edit", "convert", "images", "info", "outline", "text",
                                    "tools", "help", "-h", "--help", "--version")):
        from .gui import main as gui_main
        return gui_main(args)
    cmd = args.pop(0)
    out = sys.stdout
    try:
        if cmd in ("-h", "--help"):
            out.write(USAGE)
            return 0
        if cmd == "--version":
            from . import __version__
            out.write(__version__ + "\n")
            return 0
        if cmd == "help":
            from .commands import HELP
            out.write(HELP)
            return 0
        if cmd == "tools":
            from .tools import Tools
            out.write(Tools().summary() + "\n")
            return 0
        if cmd == "edit":
            from .commands import EditSession
            target = _opt(args, "-o")
            if len(args) < 2:
                raise SystemExit("usage: leafpdf edit IN.pdf \"COMMANDS\" [-o OUT.pdf]")
            src, script = args[0], " ; ".join(args[1:])
            if os.path.isfile(script):
                with open(script) as f:
                    script = f.read()
            import tempfile
            work = tempfile.mkdtemp(prefix="leafpdf-cli-")
            s = EditSession(src, work)
            try:
                for m in s.run(script.replace(" ; ", "\n")):
                    if m:
                        out.write(m + "\n")
                if s.dirty:
                    dest = s.save(target or src)
                    out.write("Saved %s\n" % dest)
            finally:
                s.close()
                import shutil
                shutil.rmtree(work, ignore_errors=True)
            return 0
        if cmd in ("convert", "images"):
            from .tools import Tools
            from .render import Renderer
            from . import convert as cv
            tools = Tools()
            size = _opt(args, "--page-size", "auto")
            if cmd == "images":
                cv.images_to_pdf(args[1:], args[0], size, tools=tools,
                                 margin=18 if size != "auto" else 0)
                out.write("Wrote %s\n" % args[0])
                return 0
            files = cv.convert_any(args[0], args[1], tools=tools, renderer=Renderer(tools),
                                   page_size=size)
            for f in files:
                out.write("Wrote %s\n" % f)
            return 0
        from .pdf.document import PdfDocument
        doc = PdfDocument(args[0])
        try:
            if cmd == "info":
                w, h = doc.page_size(0)
                out.write("Pages: %d\nFirst page: %.0f x %.0f pt\nEncrypted: %s\n"
                          "Incremental revisions: %s\n" %
                          (doc.page_count(), w, h, doc.encrypted,
                           "xref stream" if doc.xref_is_stream else "xref table"))
                for k, v in sorted(doc.info().items()):
                    out.write("%s: %s\n" % (k, v))
            elif cmd == "outline":
                from .pdf.outline import read_outline, flatten
                for depth, it in flatten(read_outline(doc)):
                    page = "-" if it.page is None else str(it.page + 1)
                    out.write("%s%s  [p.%s]\n" % ("  " * depth, it.title, page))
            elif cmd == "text":
                from .pdf.editor import parse_pages, page_text
                for p in parse_pages(args[1] if len(args) > 1 else None, doc.page_count()):
                    out.write("--- page %d ---\n%s\n" % (p + 1, page_text(doc, p)))
        finally:
            doc.close()
        return 0
    except Exception as e:
        sys.stderr.write("Error: %s\n" % e)
        return 1
