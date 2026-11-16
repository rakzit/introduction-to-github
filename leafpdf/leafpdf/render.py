"""Page rendering and text extraction through MuPDF or Ghostscript.

Only the page being looked at is rendered (plus a small read-ahead in the
GUI), so a 5000-page file costs the same as a 5-page one.
"""

import hashlib
import os
import shutil
import tempfile

from .tools import run, ToolError


class Renderer(object):
    def __init__(self, tools, cache_dir=None):
        self.tools = tools
        self.cache_dir = cache_dir or os.path.join(tempfile.gettempdir(), "leafpdf-render")
        if not os.path.isdir(self.cache_dir):
            os.makedirs(self.cache_dir)

    @property
    def backend(self):
        if self.tools.mutool:
            return "mupdf"
        if self.tools.gs:
            return "ghostscript"
        return None

    def available(self):
        return self.backend is not None

    def _key(self, path, page, dpi, ext):
        st = os.stat(path)
        raw = ("%s|%d|%d|%d|%d" % (os.path.abspath(path), st.st_size, int(st.st_mtime),
                                     page, dpi)).encode("utf-8", "replace")
        return os.path.join(self.cache_dir, hashlib.md5(raw).hexdigest() + ext)

    def render(self, path, page, dpi=96):
        """Render 0-based ``page`` to a PPM file and return its path."""
        out = self._key(path, page, int(dpi), ".ppm")
        if os.path.exists(out) and os.path.getsize(out) > 0:
            return out
        tmp = out + ".tmp.ppm"
        if self.backend == "mupdf":
            args = [self.tools.mutool, "draw", "-o", tmp, "-r", str(int(dpi)), path,
                    str(page + 1)]
        elif self.backend == "ghostscript":
            args = [self.tools.gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-dUseCropBox",
                    "-sDEVICE=ppmraw", "-r%d" % int(dpi), "-dTextAlphaBits=4",
                    "-dGraphicsAlphaBits=4", "-dFirstPage=%d" % (page + 1),
                    "-dLastPage=%d" % (page + 1), "-sOutputFile=" + tmp, path]
        else:
            raise ToolError("No renderer found. Install MuPDF (mutool) or Ghostscript "
                            "(see Help > Setup).")
        code, _, err = run(args, timeout=300)
        if not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            raise ToolError("rendering failed: %s" % err.decode("utf-8", "replace")[-400:])
        if os.path.exists(out):
            os.remove(out)
        os.rename(tmp, out)
        return out

    def render_to_files(self, path, out_pattern, dpi=150, pages=None, fmt="png"):
        """Render pages to image files.  ``out_pattern`` contains %d (1-based)."""
        written = []
        pages = list(pages) if pages is not None else None
        if self.backend == "mupdf" and fmt == "png":
            for p in pages:
                out = out_pattern % (p + 1)
                code, _, err = run([self.tools.mutool, "draw", "-o", out, "-r", str(dpi),
                                    path, str(p + 1)], timeout=600)
                if os.path.exists(out):
                    written.append(out)
            return written
        if self.backend is not None and self.tools.gs:
            device = {"png": "png16m", "jpg": "jpeg", "jpeg": "jpeg", "tif": "tiff24nc",
                      "tiff": "tiff24nc", "bmp": "bmp16m"}.get(fmt)
            if device:
                for p in pages:
                    out = out_pattern % (p + 1)
                    run([self.tools.gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER",
                         "-dUseCropBox", "-sDEVICE=" + device, "-r%d" % dpi,
                         "-dTextAlphaBits=4", "-dGraphicsAlphaBits=4",
                         "-dJPEGQ=90", "-dFirstPage=%d" % (p + 1), "-dLastPage=%d" % (p + 1),
                         "-sOutputFile=" + out, path], timeout=600)
                    if os.path.exists(out):
                        written.append(out)
                return written
        # convert from PNG/PPM through Pillow if it happens to be installed
        try:
            from PIL import Image
        except ImportError:
            raise ToolError("Saving as %s needs Ghostscript (or Pillow). PNG works with "
                            "MuPDF alone." % fmt.upper())
        for p in pages:
            ppm = self.render(path, p, dpi)
            out = out_pattern % (p + 1)
            Image.open(ppm).convert("RGB").save(out)
            written.append(out)
        return written

    def stext(self, path, page):
        """MuPDF structured text (XML) of one page, or None."""
        if not self.tools.mutool:
            return None
        fd, tmp = tempfile.mkstemp(suffix=".xml")
        os.close(fd)
        try:
            run([self.tools.mutool, "draw", "-F", "stext", "-o", tmp, path, str(page + 1)],
                timeout=300)
            with open(tmp, "rb") as f:
                data = f.read()
            return data or None
        finally:
            os.remove(tmp)

    def text(self, path, pages):
        """Plain text of the given pages, separated by form feeds, or None."""
        fd, tmp = tempfile.mkstemp(suffix=".txt")
        os.close(fd)
        try:
            spec = ",".join(str(p + 1) for p in pages)
            if self.tools.mutool:
                run([self.tools.mutool, "draw", "-F", "txt", "-o", tmp, path, spec],
                    timeout=900)
            elif self.tools.gs:
                first, last = min(pages) + 1, max(pages) + 1
                run([self.tools.gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER",
                     "-sDEVICE=txtwrite", "-dFirstPage=%d" % first, "-dLastPage=%d" % last,
                     "-sOutputFile=" + tmp, path], timeout=900)
            else:
                return None
            with open(tmp, "rb") as f:
                return f.read().decode("utf-8", "replace")
        finally:
            os.remove(tmp)

    def clear_cache(self):
        shutil.rmtree(self.cache_dir, ignore_errors=True)
        os.makedirs(self.cache_dir)

    def trim_cache(self, max_files=400):
        try:
            files = [os.path.join(self.cache_dir, f) for f in os.listdir(self.cache_dir)]
            if len(files) <= max_files:
                return
            files.sort(key=lambda p: os.path.getmtime(p))
            for p in files[:len(files) - max_files]:
                os.remove(p)
        except OSError:
            pass
