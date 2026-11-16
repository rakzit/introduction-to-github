"""The LeafPDF edit command language and the undoable edit session.

Commands (separate several with ';' or new lines; they run as ONE undo step):

  replace "old" "new" [pages SPEC] [-i]     replace text (-i = ignore case)
  find "text" [pages SPEC]                  list pages containing text
  stamp "text" [pages SPEC] [pos P | at X Y] [size N] [color C]
                [opacity 0-1] [angle DEG]   write text on pages
  header "text" [...]  / footer "text" [...] stamp at top / bottom center
  watermark "text" [...]                    big diagonal semi-transparent text
  number ["Page {n} of {total}"] [pages SPEC] [pos P] [start N] [size N]
  whiteout X Y W H [pages SPEC]             cover an area (points, top-left origin)
  rotate DEG [pages SPEC]                   rotate by 90/180/270
  delete pages SPEC                         remove pages
  move pages SPEC to N                      move pages to position N
  reverse                                   reverse page order
  blank [after N] [count K]                 insert blank pages
  merge "file.pdf" [after N] [pages SPEC]   insert pages of another PDF
  extract pages SPEC to "out.pdf"           save some pages as a new file
  compact to "out.pdf"                      save a rebuilt, smaller copy

SPEC: all | odd | even | last | 3 | 2-5 | 7- | 1,3,9-12 | last-2
P:    top-left | top | top-right | center | bottom-left | bottom | bottom-right
Text tokens: {n} page number, {total} page count, {date}, {file}
"""

import os
import re
import shutil
import tempfile

from .pdf.document import PdfDocument
from .pdf.objects import PdfError
from .pdf import editor as ed

HELP = __doc__

_token_re = re.compile(r'"((?:[^"\\]|\\.)*)"|\'([^\']*)\'|(;)|([^\s;]+)')


class CommandError(PdfError):
    pass


def tokenize(text):
    """Split a script into a list of commands (each a list of tokens).

    Quoted tokens are returned as ('q', value) so they are never taken for
    keywords; bare words are plain strings.
    """
    commands = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        cur = []
        for m in _token_re.finditer(line):
            if m.group(3):
                if cur:
                    commands.append(cur)
                cur = []
            elif m.group(1) is not None:
                cur.append(("q", m.group(1).replace('\\"', '"')))
            elif m.group(2) is not None:
                cur.append(("q", m.group(2)))
            else:
                cur.append(m.group(4))
        if cur:
            commands.append(cur)
    return commands


def _val(tok):
    return tok[1] if isinstance(tok, tuple) else tok


class _Args(object):
    """Parse 'keyword value' style options after the main arguments."""

    def __init__(self, tokens):
        self.tokens = list(tokens)
        self.pos = 0

    def done(self):
        return self.pos >= len(self.tokens)

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self, what="value"):
        if self.pos >= len(self.tokens):
            raise CommandError("missing %s" % what)
        t = self.tokens[self.pos]
        self.pos += 1
        return _val(t)

    def number(self, what):
        v = self.take(what)
        try:
            return float(v) if "." in v else int(v)
        except ValueError:
            raise CommandError("%s must be a number, got '%s'" % (what, v))

    def options(self, allowed):
        opts = {}
        while not self.done():
            key = self.take()
            k = key.lower().lstrip("-")
            if k not in allowed:
                raise CommandError("unknown option '%s' (expected: %s)" %
                                   (key, ", ".join(sorted(allowed))))
            kind = allowed[k]
            if kind == "flag":
                opts[k] = True
            elif kind == "num":
                opts[k] = self.number(k)
            elif kind == "xy":
                opts[k] = (self.number("x"), self.number("y"))
            else:
                opts[k] = self.take(k)
        return opts


STAMP_OPTS = {"pages": "str", "pos": "str", "at": "xy", "size": "num",
              "color": "str", "opacity": "num", "angle": "num", "start": "num",
              "margin": "num"}


def execute(doc, command, base_dir=None):
    """Execute one tokenized command on ``doc``.

    Returns (message, needs_commit, extra) where extra may hold documents
    that must stay open until commit.
    """
    if not command:
        return "", False, None
    name = _val(command[0]).lower()
    a = _Args(command[1:])
    total = doc.page_count()

    def pages(opts, key="pages"):
        return ed.parse_pages(opts.get(key), total)

    def path_of(p):
        if base_dir and not os.path.isabs(p):
            return os.path.join(base_dir, p)
        return p

    if name == "replace":
        old = a.take("text to find")
        new = a.take("replacement text")
        o = a.options({"pages": "str", "i": "flag", "ignore-case": "flag"})
        rep = ed.replace_text(doc, pages(o), old, new, o.get("i") or o.get("ignore-case"))
        msg = "Replaced %d occurrence(s) on %d page(s)." % (rep["replaced"], len(rep["pages"]))
        if rep["missed"]:
            msg += ("\n  %d occurrence(s) could not be replaced because the text is split "
                    "into separate pieces (pages %s)." %
                    (sum(c for _, c in rep["missed"]),
                     ", ".join(str(p + 1) for p, _ in rep["missed"][:15])))
        if rep["fallback_fonts"]:
            msg += ("\n  Note: font(s) %s lack some of the new characters, so the new text "
                    "was drawn in Helvetica at the same spot." %
                    ", ".join(sorted(rep["fallback_fonts"])))
        if rep["skipped"]:
            msg += "\n  %d occurrence(s) skipped (unusable font state)." % rep["skipped"]
        return msg, rep["replaced"] > 0, None
    if name == "find":
        needle = a.take("text to find")
        o = a.options({"pages": "str", "case": "flag"})
        hits = ed.find_text(doc, pages(o), needle, not o.get("case"))
        if not hits:
            return "'%s' not found." % needle, False, hits
        return ("Found '%s' on %d page(s): %s" %
                (needle, len(hits), ", ".join("%d(x%d)" % (p + 1, c) for p, c in hits[:50]))), False, hits
    if name in ("stamp", "header", "footer", "watermark"):
        text = a.take("text")
        o = a.options(STAMP_OPTS)
        defaults = {"stamp": ("top-right", 12, None, 0),
                    "header": ("top", 10, None, 0),
                    "footer": ("bottom", 10, None, 0),
                    "watermark": ("center", 60, 0.25, 45)}[name]
        n = ed.stamp_text(doc, pages(o), text, pos=o.get("pos", defaults[0]), at=o.get("at"),
                          size=o.get("size", defaults[1]),
                          color=o.get("color", "gray" if name == "watermark" else None),
                          opacity=o.get("opacity", defaults[2]),
                          angle=o.get("angle", defaults[3]), start=int(o.get("start", 1)),
                          margin=o.get("margin", 36))
        return "Added %s to %d page(s)." % (name, n), True, None
    if name == "number":
        fmt = "Page {n} of {total}"
        if not a.done() and isinstance(a.peek(), tuple):
            fmt = a.take()
        o = a.options(STAMP_OPTS)
        n = ed.stamp_text(doc, pages(o), fmt, pos=o.get("pos", "bottom"),
                          at=o.get("at"), size=o.get("size", 10), color=o.get("color"),
                          start=int(o.get("start", 1)), margin=o.get("margin", 36))
        return "Numbered %d page(s)." % n, True, None
    if name == "whiteout":
        x, y, w, h = [a.number(k) for k in ("x", "y", "width", "height")]
        o = a.options({"pages": "str", "color": "str"})
        n = ed.whiteout(doc, pages(o), x, y, w, h, o.get("color", "white"))
        return "Covered an area on %d page(s)." % n, True, None
    if name == "rotate":
        deg = a.number("degrees")
        o = a.options({"pages": "str"})
        n = ed.rotate_pages(doc, pages(o), int(deg))
        return "Rotated %d page(s)." % n, True, None
    if name == "delete":
        if not a.done() and str(_val(a.peek())).lower() != "pages":
            spec = a.take()
            o = {"pages": spec}
        else:
            o = a.options({"pages": "str"})
        if "pages" not in o:
            raise CommandError("say which pages, e.g. delete pages 3-5")
        n = ed.delete_pages(doc, pages(o))
        return "Deleted %d page(s)." % n, True, None
    if name == "move":
        o = a.options({"pages": "str", "to": "num"})
        if "pages" not in o or "to" not in o:
            raise CommandError("usage: move pages 3-5 to 1")
        n = ed.move_pages(doc, pages(o), int(o["to"]))
        return "Moved %d page(s)." % n, True, None
    if name == "reverse":
        n = ed.reverse_pages(doc)
        return "Reversed %d pages." % n, True, None
    if name == "blank":
        o = a.options({"after": "num", "count": "num"})
        n = ed.insert_blank(doc, int(o.get("after", total)), int(o.get("count", 1)))
        return "Inserted %d blank page(s)." % n, True, None
    if name == "merge":
        p = path_of(a.take("file name"))
        o = a.options({"after": "num", "pages": "str"})
        other, n = ed.merge_file(doc, p, o.get("after"), o.get("pages"))
        return "Merged %d page(s) from %s." % (n, os.path.basename(p)), True, other
    if name == "extract":
        o = a.options({"pages": "str", "to": "str"})
        if "to" not in o:
            raise CommandError('usage: extract pages 1-3 to "part.pdf"')
        out = path_of(o["to"])
        idx = pages(o)
        doc.write_compact(out, idx)
        return "Wrote %d page(s) to %s." % (len(idx), out), False, None
    if name == "compact":
        o = a.options({"to": "str"})
        if "to" not in o:
            raise CommandError('usage: compact to "smaller.pdf"')
        out = path_of(o["to"])
        doc.write_compact(out)
        return "Wrote compacted copy to %s (%d KB)." % (out, os.path.getsize(out) // 1024), False, None
    if name in ("help", "?"):
        return HELP, False, None
    raise CommandError("unknown command '%s' (type 'help')" % name)


class EditSession(object):
    """Owns a document being edited.

    The first edit copies the original to a private working file; every
    command appends one incremental revision to it.  Undo truncates the
    working file back to the previous revision, so it is instant even for
    very large PDFs.  Save copies the working file over the target.
    """

    def __init__(self, path, workdir=None, working_copy=None, history=None,
                 saved_at=None):
        self.original = os.path.abspath(path)
        self.workdir = workdir or tempfile.gettempdir()
        self.work = working_copy if working_copy and os.path.exists(working_copy) else None
        self.history = list(history or [])   # [(size_before, label)]
        self.redo_stack = []                 # [(appended_bytes, label)]
        self.saved_at = saved_at if saved_at is not None else 0
        self.doc = PdfDocument(self.work or self.original)

    @property
    def current_path(self):
        return self.work or self.original

    @property
    def dirty(self):
        return bool(self.work) and len(self.history) != self.saved_at

    def close(self):
        if self.doc:
            self.doc.close()
            self.doc = None

    def discard(self):
        """Drop all unsaved edits."""
        self.close()
        if self.work and os.path.exists(self.work):
            os.remove(self.work)
        self.work = None
        self.history = []
        self.redo_stack = []
        self.saved_at = 0
        self.doc = PdfDocument(self.original)

    def _ensure_work(self):
        if self.work:
            return
        if not os.path.isdir(self.workdir):
            os.makedirs(self.workdir)
        fd, path = tempfile.mkstemp(suffix=".pdf", prefix="edit-", dir=self.workdir)
        os.close(fd)
        self.doc.close()
        shutil.copyfile(self.original, path)
        self.work = path
        self.doc = PdfDocument(path)

    def run(self, script):
        """Run a script; returns a list of messages.  One undo step."""
        commands = tokenize(script)
        if not commands:
            return []
        self.doc.check_editable()
        needs = any(_val(c[0]).lower() not in ("find", "extract", "compact", "help", "?")
                    for c in commands)
        if needs:
            self._ensure_work()
        doc = self.doc
        messages = []
        keep_open = []
        changed = False
        size_before = os.path.getsize(doc.path)
        base = os.path.dirname(self.original)
        try:
            for cmd in commands:
                msg, needs_commit, extra = execute(doc, cmd, base)
                if isinstance(extra, PdfDocument):
                    keep_open.append(extra)
                messages.append(msg)
                changed = changed or needs_commit
            if changed and doc.changes:
                doc.commit()
                label = "; ".join(" ".join(str(_val(t)) for t in c) for c in commands)
                self.history.append((size_before, label[:80]))
                self.redo_stack = []
            else:
                doc.changes.clear()
                doc.invalidate_pages()
        except Exception:
            doc.changes.clear()
            doc.invalidate_pages()
            raise
        finally:
            for o in keep_open:
                o.close()
        return messages

    def apply(self, func, label):
        """Run ``func(doc)`` as one undoable change (used by the GUI)."""
        self.doc.check_editable()
        self._ensure_work()
        doc = self.doc
        size_before = os.path.getsize(doc.path)
        try:
            result = func(doc)
            if doc.changes:
                doc.commit()
                self.history.append((size_before, label[:80]))
                self.redo_stack = []
        except Exception:
            doc.changes.clear()
            doc.invalidate_pages()
            raise
        return result

    def undo(self):
        if not self.history:
            return None
        size, label = self.history.pop()
        path = self.doc.path
        self.doc.close()
        with open(path, "r+b") as f:
            f.seek(size)
            tail = f.read()
            f.seek(size)
            f.truncate()
        self.redo_stack.append((tail, label))
        self.doc = PdfDocument(path)
        return label

    def redo(self):
        if not self.redo_stack:
            return None
        tail, label = self.redo_stack.pop()
        path = self.doc.path
        size = os.path.getsize(path)
        self.doc.close()
        with open(path, "ab") as f:
            f.write(tail)
        self.history.append((size, label))
        self.doc = PdfDocument(path)
        return label

    def save(self, target=None):
        """Write the edited document to ``target`` (default: the original)."""
        target = os.path.abspath(target or self.original)
        if not self.work:
            if target != self.original:
                shutil.copyfile(self.original, target)
            return target
        self.doc.close()
        tmp = target + ".leafpdf-tmp"
        shutil.copyfile(self.work, tmp)
        if os.path.exists(target):
            os.remove(target)
        os.rename(tmp, target)
        self.doc = PdfDocument(self.work)
        if target == self.original:
            self.saved_at = len(self.history)
        return target
