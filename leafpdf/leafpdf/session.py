"""Remembering what the user had open (and where), plus personal bookmarks.

Everything lives in one small JSON file:
  Windows: %APPDATA%\\LeafPDF\\state.json    others: ~/.leafpdf/state.json
It is rewritten (atomically) whenever the page changes, so even after a
crash or power cut the files, pages and unsaved edits come back.
"""

import json
import os
import time


def app_data_dir():
    base = os.environ.get("LEAFPDF_HOME")
    if not base:
        if os.name == "nt":
            base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "LeafPDF")
        else:
            base = os.path.join(os.path.expanduser("~"), ".leafpdf")
    if not os.path.isdir(base):
        os.makedirs(base)
    return base


def file_key(path):
    return os.path.normcase(os.path.abspath(path))


class State(object):
    MAX_RECENT = 20
    MAX_FILES = 500

    def __init__(self, path=None):
        self.dir = app_data_dir()
        self.path = path or os.path.join(self.dir, "state.json")
        self.data = {"version": 1, "open": [], "active": 0, "recent": [], "files": {},
                     "window": None, "config": {}}
        self.load()

    @property
    def work_dir(self):
        d = os.path.join(self.dir, "work")
        if not os.path.isdir(d):
            os.makedirs(d)
        return d

    @property
    def config(self):
        return self.data.setdefault("config", {})

    def load(self):
        for p in (self.path, self.path + ".bak"):
            try:
                with open(p, "rb") as f:
                    d = json.loads(f.read().decode("utf-8"))
                if isinstance(d, dict):
                    self.data.update(d)
                    return
            except (IOError, OSError, ValueError):
                continue

    def save(self):
        tmp = self.path + ".tmp"
        data = json.dumps(self.data, indent=1, sort_keys=True).encode("utf-8")
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        if os.path.exists(self.path):
            if os.path.exists(self.path + ".bak"):
                os.remove(self.path + ".bak")
            os.rename(self.path, self.path + ".bak")
        os.rename(tmp, self.path)

    # ----------------------------------------------------------- per file

    def file_info(self, path):
        files = self.data.setdefault("files", {})
        k = file_key(path)
        if k not in files:
            files[k] = {"page": 0, "zoom": None, "bookmarks": []}
            if len(files) > self.MAX_FILES:
                oldest = sorted(files.items(), key=lambda kv: kv[1].get("seen", 0))
                for key, _ in oldest[:len(files) - self.MAX_FILES]:
                    files.pop(key, None)
        files[k]["seen"] = time.time()
        return files[k]

    def bookmarks(self, path):
        return self.file_info(path).setdefault("bookmarks", [])

    def add_recent(self, path):
        path = os.path.abspath(path)
        rec = [p for p in self.data.setdefault("recent", []) if file_key(p) != file_key(path)]
        rec.insert(0, path)
        self.data["recent"] = rec[:self.MAX_RECENT]

    def recent(self):
        return [p for p in self.data.get("recent", []) if os.path.exists(p)]

    # ---------------------------------------------------------- open tabs

    def set_open(self, tabs, active):
        """tabs: list of dicts with path/page/zoom/scroll/working_copy/history."""
        self.data["open"] = tabs
        self.data["active"] = active
        for t in tabs:
            info = self.file_info(t["path"])
            info["page"] = t.get("page", 0)
            info["zoom"] = t.get("zoom")
            info["scroll"] = t.get("scroll", 0)

    def open_tabs(self):
        """Tabs to restore (files that still exist)."""
        out = []
        for t in self.data.get("open", []):
            if isinstance(t, dict) and t.get("path") and os.path.exists(t["path"]):
                out.append(t)
        return out, int(self.data.get("active", 0) or 0)
