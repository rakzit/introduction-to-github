"""Locating and running the optional helper programs.

LeafPDF itself is pure Python.  For drawing pages it uses one of:
  * MuPDF's ``mutool`` (preferred: tiny, fast, opens huge files instantly)
  * Ghostscript (``gswin32c`` / ``gs``)
For Office conversions it uses Microsoft Office (through Windows Script
Host, no extra Python modules) or LibreOffice.

Portable layout: put the programs next to the app in ``tools\\``:
    tools\\mupdf\\mutool.exe      tools\\gs\\bin\\gswin32c.exe
"""

import glob
import os
import subprocess
import sys

IS_WINDOWS = os.name == "nt"
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(os.path.abspath(sys.executable))

CREATE_NO_WINDOW = 0x08000000


class ToolError(Exception):
    pass


def _which(name):
    exts = [""]
    if IS_WINDOWS:
        exts = os.environ.get("PATHEXT", ".EXE;.BAT;.CMD").lower().split(";") + [""]
    for d in os.environ.get("PATH", "").split(os.pathsep):
        d = d.strip('"')
        for ext in exts:
            p = os.path.join(d, name + ext)
            if os.path.isfile(p):
                return p
    return None


def _first(paths):
    for pattern in paths:
        hits = sorted(glob.glob(pattern), reverse=True)
        for h in hits:
            if os.path.isfile(h):
                return h
    return None


def _program_dirs():
    dirs = []
    for var in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        v = os.environ.get(var)
        if v and v not in dirs:
            dirs.append(v)
    if not dirs and IS_WINDOWS:
        dirs = ["C:\\Program Files"]
    return dirs


def find_mutool(config=None):
    if config and config.get("mutool") and os.path.isfile(config["mutool"]):
        return config["mutool"]
    local = [os.path.join(APP_DIR, "tools", "mupdf", "mutool*"),
             os.path.join(APP_DIR, "tools", "mutool*")]
    hit = _first(local)
    if hit:
        return hit
    hit = _which("mutool")
    if hit:
        return hit
    if IS_WINDOWS:
        return _first([os.path.join(d, "MuPDF", "mutool.exe") for d in _program_dirs()] +
                      [os.path.join(d, "SumatraPDF", "mutool.exe") for d in _program_dirs()])
    return None


def find_ghostscript(config=None):
    if config and config.get("ghostscript") and os.path.isfile(config["ghostscript"]):
        return config["ghostscript"]
    hit = _first([os.path.join(APP_DIR, "tools", "gs", "bin", "gswin32c.exe"),
                  os.path.join(APP_DIR, "tools", "gs", "bin", "gswin64c.exe"),
                  os.path.join(APP_DIR, "tools", "gs", "gswin32c.exe")])
    if hit:
        return hit
    for name in ("gswin32c", "gswin64c", "gs"):
        hit = _which(name)
        if hit:
            return hit
    if IS_WINDOWS:
        pats = []
        for d in _program_dirs():
            pats.append(os.path.join(d, "gs", "gs*", "bin", "gswin32c.exe"))
            pats.append(os.path.join(d, "gs", "gs*", "bin", "gswin64c.exe"))
        return _first(pats)
    return None


def find_soffice(config=None):
    if config and config.get("soffice") and os.path.isfile(config["soffice"]):
        return config["soffice"]
    hit = _first([os.path.join(APP_DIR, "tools", "LibreOfficePortable", "App",
                               "libreoffice", "program", "soffice.exe")])
    if hit:
        return hit
    for name in ("soffice", "libreoffice"):
        hit = _which(name)
        if hit:
            return hit
    if IS_WINDOWS:
        return _first([os.path.join(d, "LibreOffice*", "program", "soffice.exe")
                       for d in _program_dirs()])
    if sys.platform == "darwin":
        return _first(["/Applications/LibreOffice.app/Contents/MacOS/soffice"])
    return None


def find_cscript():
    if not IS_WINDOWS:
        return None
    root = os.environ.get("SystemRoot", "C:\\WINDOWS")
    p = os.path.join(root, "System32", "cscript.exe")
    return p if os.path.isfile(p) else _which("cscript")


def run(args, timeout=600, cwd=None):
    """Run a program without a console window; returns (code, stdout, stderr)."""
    kwargs = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
              "stderr": subprocess.PIPE, "cwd": cwd}
    if IS_WINDOWS:
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        proc = subprocess.Popen(args, **kwargs)
    except OSError as e:
        raise ToolError("cannot start %s: %s" % (args[0], e))
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise ToolError("%s timed out" % os.path.basename(args[0]))
    return proc.returncode, out, err


class Tools(object):
    """Detected helper programs (cached)."""

    def __init__(self, config=None):
        self.config = config or {}
        self.refresh()

    def refresh(self):
        self.mutool = find_mutool(self.config)
        self.gs = find_ghostscript(self.config)
        self.soffice = find_soffice(self.config)
        self.cscript = find_cscript()

    def summary(self):
        def show(p):
            return p if p else "not found"
        return ("MuPDF (mutool): %s\nGhostscript: %s\nLibreOffice: %s\n"
                "Microsoft Office via Windows Script Host: %s" %
                (show(self.mutool), show(self.gs), show(self.soffice),
                 "available" if self.cscript else "n/a"))
