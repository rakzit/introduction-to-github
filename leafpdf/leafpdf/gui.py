"""LeafPDF desktop window (Tkinter, works with the Tk that ships with
Python 3.4 on Windows XP)."""

import os
import sys
import threading
import time
import traceback

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog
except ImportError:  # Python built without Tk
    tk = None

import queue

from . import __version__
from .commands import EditSession, HELP, CommandError
from .convert import images_to_pdf, office_to_pdf, pdf_to, IMAGE_EXTS, OFFICE_EXTS
from .pdf.outline import read_outline, write_outline, flatten, current_item, OutlineItem
from .pdf.objects import PdfError
from .render import Renderer
from .session import State, file_key
from .tools import Tools

SCREEN_DPI = 96.0
ZOOM_STEPS = [0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5,
              3.0, 4.0]

EXPORT_TYPES = [("Word document", "*.docx"), ("Rich Text (WordPad)", "*.rtf"),
                ("Excel workbook", "*.xlsx"), ("PowerPoint (page pictures)", "*.pptx"),
                ("Plain text", "*.txt"), ("Web page", "*.html"),
                ("OpenDocument text", "*.odt"), ("Word 97-2003", "*.doc"),
                ("PNG pictures", "*.png"), ("JPEG pictures", "*.jpg"),
                ("TIFF pictures", "*.tiff"), ("BMP pictures", "*.bmp")]


def _short(text, n=60):
    return text if len(text) <= n else text[:n - 1] + "\u2026"


class RenderWorker(object):
    """One background thread that renders the most recent request first."""

    def __init__(self, renderer):
        self.renderer = renderer
        self.requests = []
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.results = queue.Queue()
        t = threading.Thread(target=self._loop)
        t.daemon = True
        t.start()

    def submit(self, token, path, page, dpi, prefetch=False):
        with self.lock:
            if not prefetch:
                self.requests = [r for r in self.requests if r[4]]
            self.requests.insert(0 if not prefetch else len(self.requests),
                                 (token, path, page, dpi, prefetch))
        self.event.set()

    def _loop(self):
        while True:
            self.event.wait()
            with self.lock:
                if not self.requests:
                    self.event.clear()
                    continue
                token, path, page, dpi, prefetch = self.requests.pop(0)
            try:
                out = self.renderer.render(path, page, dpi)
                if not prefetch:
                    self.results.put((token, out, None))
            except Exception as e:
                if not prefetch:
                    self.results.put((token, None, e))


class DocTab(object):
    def __init__(self, app, path, restore=None):
        self.app = app
        restore = restore or {}
        hist = [tuple(h) for h in restore.get("history", [])]
        self.session = EditSession(path, app.state.work_dir, restore.get("working_copy"),
                                   hist, restore.get("saved_at"))
        self.display_name = restore.get("display_name") or os.path.basename(path)
        info = app.state.file_info(path)
        self.page = int(restore.get("page", info.get("page", 0)) or 0)
        self.zoom = restore.get("zoom", info.get("zoom")) or "fit-width"
        self.scroll = float(restore.get("scroll", info.get("scroll", 0)) or 0)
        self.page = max(0, min(self.page, self.page_count() - 1))
        self.outline = []
        self.load_outline()
        self.image = None
        self.token = 0
        self.hits = []
        self.frame = ttk.Frame(app.notebook)
        self.canvas = tk.Canvas(self.frame, background="#808080", highlightthickness=0,
                                xscrollincrement=20, yscrollincrement=20)
        self.vbar = ttk.Scrollbar(self.frame, orient="vertical", command=self._yview)
        self.hbar = ttk.Scrollbar(self.frame, orient="horizontal", command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vbar.set, xscrollcommand=self.hbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.vbar.grid(row=0, column=1, sticky="ns")
        self.hbar.grid(row=1, column=0, sticky="ew")
        self.frame.rowconfigure(0, weight=1)
        self.frame.columnconfigure(0, weight=1)
        app.bind_canvas(self)

    # -------------------------------------------------------- properties

    @property
    def path(self):
        return self.session.original

    @property
    def doc(self):
        return self.session.doc

    def page_count(self):
        try:
            return max(1, self.doc.page_count())
        except Exception:
            return 1

    def page_size(self, index=None):
        try:
            return self.doc.page_size(self.page if index is None else index)
        except Exception:
            return 612.0, 792.0

    def title(self):
        return ("*" if self.session.dirty else "") + _short(self.display_name, 28)

    def load_outline(self):
        try:
            self.outline = [] if self.doc.encrypted else read_outline(self.doc)
        except Exception:
            self.outline = []

    def _yview(self, *args):
        self.canvas.yview(*args)
        self.app.on_scrolled(self)

    def dpi(self):
        w, h = self.page_size()
        cw = max(self.canvas.winfo_width(), 200)
        ch = max(self.canvas.winfo_height(), 200)
        if self.zoom == "fit-width":
            z = (cw - 24) / (w * SCREEN_DPI / 72.0)
        elif self.zoom == "fit-page":
            z = min((cw - 24) / (w * SCREEN_DPI / 72.0), (ch - 24) / (h * SCREEN_DPI / 72.0))
        else:
            z = float(self.zoom)
        z = max(0.1, min(z, 6.0))
        return int(round(SCREEN_DPI * z))

    def zoom_factor(self):
        return self.dpi() / SCREEN_DPI

    def to_json(self):
        s = self.session
        return {"path": s.original, "page": self.page, "zoom": self.zoom,
                "scroll": round(self.scroll, 4), "working_copy": s.work,
                "history": [list(h) for h in s.history], "saved_at": s.saved_at,
                "display_name": self.display_name}


class App(object):
    def __init__(self, root, files=None, state=None):
        self.root = root
        self.state = state or State()
        self.tools = Tools(self.state.config)
        self.renderer = Renderer(self.tools, os.path.join(self.state.dir, "cache"))
        self.renderer.trim_cache()
        self.worker = RenderWorker(self.renderer)
        self.tabs = []
        self._persist_job = None
        self._topic_sync = False
        self.cmd_history = []
        self.cmd_pos = 0
        root.title("LeafPDF")
        root.report_callback_exception = self.on_error
        geo = self.state.data.get("window")
        root.geometry(geo or "1100x760")
        self.build_ui()
        self.restore(files or [])
        root.protocol("WM_DELETE_WINDOW", self.exit)
        root.after(50, self.poll)

    # =============================================================== UI

    def build_ui(self):
        root = self.root
        self.build_menu()
        bar = ttk.Frame(root, padding=(4, 3))
        bar.pack(side="top", fill="x")
        b = lambda text, cmd, w=None: ttk.Button(bar, text=text, command=cmd, width=w)
        b("Open", self.open_dialog).pack(side="left")
        b("Save", self.save).pack(side="left", padx=(2, 8))
        b("\u25c0", self.prev_page, 3).pack(side="left")
        self.page_var = tk.StringVar()
        pe = ttk.Entry(bar, textvariable=self.page_var, width=6, justify="center")
        pe.pack(side="left", padx=2)
        pe.bind("<Return>", lambda e: self.goto_page_text(self.page_var.get()))
        self.total_label = ttk.Label(bar, text="/ 0")
        self.total_label.pack(side="left")
        self.search_var = tk.StringVar()
        se = ttk.Entry(bar, textvariable=self.search_var, width=16)
        se.pack(side="right")
        se.bind("<Return>", lambda e: self.search())
        ttk.Label(bar, text="Find:").pack(side="right", padx=(8, 2))
        self.search_entry = se
        b("\u25b6", self.next_page, 3).pack(side="left", padx=(2, 8))
        b("\u2212", lambda: self.zoom_step(-1), 3).pack(side="left")
        self.zoom_var = tk.StringVar()
        zc = ttk.Combobox(bar, textvariable=self.zoom_var, width=9,
                          values=["Fit width", "Fit page"] + ["%d%%" % (z * 100) for z in ZOOM_STEPS])
        zc.pack(side="left", padx=2)
        zc.bind("<<ComboboxSelected>>", lambda e: self.set_zoom_text(self.zoom_var.get()))
        zc.bind("<Return>", lambda e: self.set_zoom_text(self.zoom_var.get()))
        b("+", lambda: self.zoom_step(1), 3).pack(side="left", padx=(0, 8))
        ttk.Label(bar, text="Topic:").pack(side="left")
        self.topic_var = tk.StringVar()
        self.topic_box = ttk.Combobox(bar, textvariable=self.topic_var, width=26, state="readonly")
        self.topic_box.pack(side="left", padx=2)
        self.topic_box.bind("<<ComboboxSelected>>", self.on_topic_combo)
        b("\u2605 Bookmark", self.add_bookmark).pack(side="left", padx=(8, 0))

        self.paned = ttk.PanedWindow(root, orient="horizontal")
        self.paned.pack(side="top", fill="both", expand=True)
        side = ttk.Notebook(self.paned, width=250)
        self.side = side
        # --- topics (PDF outline)
        tf = ttk.Frame(side)
        self.tree = ttk.Treeview(tf, show="tree", selectmode="browse")
        tsb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tsb.grid(row=0, column=1, sticky="ns")
        tf.rowconfigure(0, weight=1)
        tf.columnconfigure(0, weight=1)
        tb = ttk.Frame(tf)
        tb.grid(row=1, column=0, columnspan=2, sticky="ew")
        for col, (text, cmd) in enumerate((("Add", self.topic_add), ("Rename", self.topic_rename),
                                           ("Delete", self.topic_delete))):
            ttk.Button(tb, text=text, command=cmd).grid(row=0, column=col * 4, columnspan=4,
                                                        sticky="ew")
        for col, (text, cmd) in enumerate((("\u2191 Up", lambda: self.topic_move(-1)),
                                           ("\u2193 Down", lambda: self.topic_move(1)),
                                           ("\u2192 In", self.topic_indent),
                                           ("\u2190 Out", self.topic_outdent))):
            ttk.Button(tb, text=text, command=cmd).grid(row=1, column=col * 3, columnspan=3,
                                                        sticky="ew")
        for col in range(12):
            tb.columnconfigure(col, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self.on_tree_select)
        self.tree.bind("<Double-1>", lambda e: self.topic_rename())
        side.add(tf, text="Topics")
        # --- personal bookmarks
        bf = ttk.Frame(side)
        self.bm_list = tk.Listbox(bf, activestyle="none", exportselection=False)
        bsb = ttk.Scrollbar(bf, orient="vertical", command=self.bm_list.yview)
        self.bm_list.configure(yscrollcommand=bsb.set)
        self.bm_list.grid(row=0, column=0, sticky="nsew")
        bsb.grid(row=0, column=1, sticky="ns")
        bf.rowconfigure(0, weight=1)
        bf.columnconfigure(0, weight=1)
        bb = ttk.Frame(bf)
        bb.grid(row=1, column=0, columnspan=2, sticky="ew")
        for i, (text, cmd) in enumerate((("Add", self.add_bookmark), ("Rename", self.bm_rename),
                                         ("Delete", self.bm_delete),
                                         ("\u2191 Up", lambda: self.bm_move(-1)),
                                         ("\u2193 Down", lambda: self.bm_move(1)),
                                         ("Go", self.bm_go))):
            ttk.Button(bb, text=text, command=cmd).grid(row=i // 3, column=i % 3, sticky="ew")
        for col in range(3):
            bb.columnconfigure(col, weight=1)
        self.bm_list.bind("<Double-1>", lambda e: self.bm_go())
        self.bm_list.bind("<Return>", lambda e: self.bm_go())
        side.add(bf, text="Bookmarks")
        self.paned.add(side, weight=0)

        right = ttk.Frame(self.paned)
        self.notebook = ttk.Notebook(right)
        self.notebook.pack(side="top", fill="both", expand=True)
        self.notebook.bind("<<NotebookTabChanged>>", lambda e: self.on_tab_changed())
        self.empty = ttk.Label(right, text="Open a PDF (Ctrl+O), a picture or an Office "
                               "document.\nFiles you leave open come back next time.",
                               anchor="center", justify="center")
        # --- command bar
        cmd = ttk.Frame(right, padding=(0, 3, 0, 0))
        cmd.pack(side="bottom", fill="x")
        ttk.Label(cmd, text="Command:").pack(side="left")
        self.cmd_var = tk.StringVar()
        ce = ttk.Entry(cmd, textvariable=self.cmd_var)
        ce.pack(side="left", fill="x", expand=True, padx=3)
        ce.bind("<Return>", lambda e: self.run_command_line())
        ce.bind("<Up>", lambda e: self.cmd_hist(-1))
        ce.bind("<Down>", lambda e: self.cmd_hist(1))
        self.cmd_entry = ce
        ttk.Button(cmd, text="Run", command=self.run_command_line).pack(side="left")
        ttk.Button(cmd, text="Script\u2026", command=self.script_dialog).pack(side="left")
        ttk.Button(cmd, text="Undo", command=self.undo).pack(side="left", padx=(6, 0))
        ttk.Button(cmd, text="Redo", command=self.redo).pack(side="left")
        ttk.Button(cmd, text="?", width=3, command=self.show_help).pack(side="left")
        self.log = tk.Text(right, height=4, wrap="word", state="disabled",
                           background="#f6f6f0", relief="flat")
        self.log.pack(side="bottom", fill="x")
        self.paned.add(right, weight=1)

        self.status_var = tk.StringVar(value="Ready")
        self.pos_var = tk.StringVar()
        sb = ttk.Frame(root)
        sb.pack(side="bottom", fill="x")
        ttk.Label(sb, textvariable=self.status_var, anchor="w").pack(side="left", fill="x",
                                                                     expand=True, padx=4)
        ttk.Label(sb, textvariable=self.pos_var, anchor="e", width=34).pack(side="right", padx=4)

        keys = {"<Control-o>": self.open_dialog, "<Control-s>": self.save,
                "<Control-w>": self.close_current, "<Control-z>": self.undo,
                "<Control-y>": self.redo, "<Control-b>": self.add_bookmark,
                "<Control-f>": lambda: self.search_entry.focus_set(),
                "<Control-g>": self.goto_dialog, "<Control-l>": lambda: self.cmd_entry.focus_set(),
                "<F1>": self.show_help, "<Control-Tab>": lambda: self.cycle_tab(1),
                "<Control-plus>": lambda: self.zoom_step(1),
                "<Control-equal>": lambda: self.zoom_step(1),
                "<Control-minus>": lambda: self.zoom_step(-1),
                "<Control-0>": lambda: self.set_zoom("fit-width"), "<F3>": self.search}
        for k, f in keys.items():
            root.bind_all(k, lambda e, f=f: (f(), "break")[1])
        for k, f in {"<Next>": self.next_page, "<Prior>": self.prev_page,
                     "<Home>": lambda: self.goto_page(0),
                     "<End>": lambda: self.goto_page(10 ** 9)}.items():
            root.bind(k, lambda e, f=f: self._nav_key(e, f))

    def _nav_key(self, event, func):
        if isinstance(event.widget, (tk.Entry, ttk.Entry, tk.Text, ttk.Combobox)):
            return None
        func()
        return "break"

    def build_menu(self):
        m = tk.Menu(self.root)
        f = tk.Menu(m, tearoff=0)
        f.add_command(label="Open\u2026", accelerator="Ctrl+O", command=self.open_dialog)
        self.recent_menu = tk.Menu(f, tearoff=0, postcommand=self.fill_recent)
        f.add_cascade(label="Open recent", menu=self.recent_menu)
        f.add_separator()
        f.add_command(label="Save", accelerator="Ctrl+S", command=self.save)
        f.add_command(label="Save as\u2026", command=self.save_as)
        f.add_command(label="Discard unsaved changes", command=self.revert)
        f.add_separator()
        f.add_command(label="Close tab", accelerator="Ctrl+W", command=self.close_current)
        f.add_command(label="Exit", command=self.exit)
        m.add_cascade(label="File", menu=f)
        e = tk.Menu(m, tearoff=0)
        e.add_command(label="Undo", accelerator="Ctrl+Z", command=self.undo)
        e.add_command(label="Redo", accelerator="Ctrl+Y", command=self.redo)
        e.add_separator()
        e.add_command(label="Command script\u2026", command=self.script_dialog)
        templates = [
            ("Replace text on all pages\u2026", 'replace "old text" "new text" pages all'),
            ("Add header\u2026", 'header "Company name" pages all'),
            ("Add footer\u2026", 'footer "Confidential - {date}" pages all'),
            ("Add page numbers\u2026", 'number "Page {n} of {total}" pos bottom'),
            ("Add watermark\u2026", 'watermark "DRAFT" pages all'),
            ("Cover (white-out) an area\u2026", "whiteout 50 50 200 30 pages 1"),
            ("Rotate pages\u2026", "rotate 90 pages {p}"),
            ("Delete pages\u2026", "delete pages {p}"),
            ("Move pages\u2026", "move pages {p} to 1"),
            ("Insert blank page\u2026", "blank after {p}"),
            ("Merge another PDF\u2026", None),
            ("Extract pages to new file\u2026", 'extract pages {p} to "pages.pdf"'),
            ("Save compact copy\u2026", 'compact to "smaller.pdf"'),
        ]
        e.add_separator()
        for label, tpl in templates:
            if tpl is None:
                e.add_command(label=label, command=self.merge_dialog)
            else:
                e.add_command(label=label, command=lambda t=tpl: self.prefill(t))
        m.add_cascade(label="Edit", menu=e)
        v = tk.Menu(m, tearoff=0)
        v.add_command(label="Next page", accelerator="PgDn", command=self.next_page)
        v.add_command(label="Previous page", accelerator="PgUp", command=self.prev_page)
        v.add_command(label="Go to page\u2026", accelerator="Ctrl+G", command=self.goto_dialog)
        v.add_separator()
        v.add_command(label="Fit width", accelerator="Ctrl+0", command=lambda: self.set_zoom("fit-width"))
        v.add_command(label="Fit page", command=lambda: self.set_zoom("fit-page"))
        v.add_command(label="Actual size", command=lambda: self.set_zoom(1.0))
        v.add_command(label="Zoom in", accelerator="Ctrl++", command=lambda: self.zoom_step(1))
        v.add_command(label="Zoom out", accelerator="Ctrl+-", command=lambda: self.zoom_step(-1))
        v.add_separator()
        v.add_command(label="Document info", command=self.doc_info)
        m.add_cascade(label="View", menu=v)
        bm = tk.Menu(m, tearoff=0)
        bm.add_command(label="Add bookmark here", accelerator="Ctrl+B", command=self.add_bookmark)
        bm.add_command(label="Add topic (PDF outline) here", command=self.topic_add)
        bm.add_command(label="Topics from personal bookmarks", command=self.bookmarks_to_topics)
        m.add_cascade(label="Bookmarks", menu=bm)
        c = tk.Menu(m, tearoff=0)
        c.add_command(label="Pictures \u2192 PDF\u2026", command=self.convert_images)
        c.add_command(label="Office / text document \u2192 PDF\u2026", command=self.convert_office)
        c.add_separator()
        c.add_command(label="This PDF \u2192 Word / Excel / PowerPoint / pictures\u2026",
                      command=self.export_dialog)
        c.add_command(label="Any PDF \u2192 other format\u2026", command=self.export_other)
        m.add_cascade(label="Convert", menu=c)
        h = tk.Menu(m, tearoff=0)
        h.add_command(label="Command reference", accelerator="F1", command=self.show_help)
        h.add_command(label="Helper programs\u2026", command=self.setup_dialog)
        h.add_command(label="About LeafPDF", command=self.about)
        m.add_cascade(label="Help", menu=h)
        self.root.configure(menu=m)

    def bind_canvas(self, tab):
        c = tab.canvas
        c.bind("<Configure>", lambda e, t=tab: self.on_resize(t))
        c.bind("<MouseWheel>", lambda e, t=tab: self.on_wheel(t, -1 if e.delta > 0 else 1, e))
        c.bind("<Button-4>", lambda e, t=tab: self.on_wheel(t, -1, e))
        c.bind("<Button-5>", lambda e, t=tab: self.on_wheel(t, 1, e))
        c.bind("<ButtonPress-1>", lambda e: (c.focus_set(), c.scan_mark(e.x, e.y)))
        c.bind("<B1-Motion>", lambda e, t=tab: (c.scan_dragto(e.x, e.y, gain=1),
                                                self.on_scrolled(t)))
        c.bind("<Motion>", lambda e, t=tab: self.on_motion(t, e))

    # ============================================================ helpers

    def current(self):
        if not self.tabs:
            return None
        try:
            idx = self.notebook.index(self.notebook.select())
        except tk.TclError:
            return None
        return self.tabs[idx] if 0 <= idx < len(self.tabs) else None

    def status(self, text):
        self.status_var.set(text)

    def write_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def on_error(self, exc, val, tb):
        traceback.print_exception(exc, val, tb)
        messagebox.showerror("LeafPDF", "%s: %s" % (exc.__name__, val))

    def busy(self, on=True):
        try:
            self.root.configure(cursor="watch" if on else "")
            self.root.update_idletasks()
        except tk.TclError:
            pass

    def schedule_persist(self):
        if self._persist_job is not None:
            self.root.after_cancel(self._persist_job)
        self._persist_job = self.root.after(600, self.persist)

    def persist(self):
        self._persist_job = None
        tabs = [t.to_json() for t in self.tabs]
        cur = self.current()
        active = self.tabs.index(cur) if cur in self.tabs else 0
        self.state.set_open(tabs, active)
        try:
            if self.root.state() == "normal":
                self.state.data["window"] = self.root.geometry()
        except tk.TclError:
            pass
        try:
            self.state.save()
        except (IOError, OSError) as e:
            self.status("Could not save session: %s" % e)

    # ========================================================= open/close

    def restore(self, files):
        tabs, active = self.state.open_tabs()
        for t in tabs:
            try:
                self.add_tab(t["path"], t, select=False)
            except Exception as e:
                self.write_log("Could not reopen %s: %s" % (t["path"], e))
        if self.tabs:
            self.notebook.select(self.tabs[max(0, min(active, len(self.tabs) - 1))].frame)
            self.write_log("Restored %d file(s) from your last session." % len(self.tabs))
        for f in files:
            self.open_path(f)
        self.update_empty()
        if not self.renderer.available():
            self.write_log("No page renderer found. Pages cannot be drawn until MuPDF "
                           "(mutool) or Ghostscript is installed - see Help > Helper programs.")

    def update_empty(self):
        if self.tabs:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.45, anchor="center")
            self.root.title("LeafPDF")
            self.total_label.configure(text="/ 0")
            self.page_var.set("")
            self.tree.delete(*self.tree.get_children())
            self.topic_box.configure(values=[])
            self.topic_var.set("")
            self.bm_list.delete(0, "end")

    def add_tab(self, path, restore=None, select=True):
        for t in self.tabs:
            if file_key(t.path) == file_key(path):
                self.notebook.select(t.frame)
                return t
        tab = DocTab(self, path, restore)
        self.tabs.append(tab)
        self.notebook.add(tab.frame, text=tab.title())
        self.state.add_recent(path)
        if select:
            self.notebook.select(tab.frame)
        self.update_empty()
        self.schedule_persist()
        return tab

    def open_dialog(self):
        types = [("Supported files", " ".join("*" + e for e in (".pdf",) + IMAGE_EXTS + OFFICE_EXTS)),
                 ("PDF", "*.pdf"), ("Pictures", " ".join("*" + e for e in IMAGE_EXTS)),
                 ("Office documents", " ".join("*" + e for e in OFFICE_EXTS)), ("All files", "*.*")]
        paths = filedialog.askopenfilenames(parent=self.root, filetypes=types)
        for p in self.root.tk.splitlist(paths) if paths else []:
            self.open_path(p)

    def open_path(self, path):
        path = os.path.abspath(path)
        ext = os.path.splitext(path)[1].lower()
        if ext == ".pdf" or ext == "":
            try:
                self.busy()
                self.add_tab(path)
            except (PdfError, IOError, OSError) as e:
                messagebox.showerror("LeafPDF", "Cannot open %s:\n%s" % (path, e))
            finally:
                self.busy(False)
            return
        out = os.path.join(self.state.work_dir, "converted-%d-%s.pdf" %
                           (int(time.time()), os.path.splitext(os.path.basename(path))[0]))

        def job():
            if ext in IMAGE_EXTS:
                return images_to_pdf([path], out, "auto", tools=self.tools)
            return office_to_pdf(path, out, self.tools)

        def done(result):
            self.add_tab(result, {"display_name": os.path.basename(path) + " (as PDF)"})
            self.write_log("Converted %s to PDF. Use File > Save as to keep it." %
                           os.path.basename(path))
        self.background("Converting %s\u2026" % os.path.basename(path), job, done)

    def fill_recent(self):
        self.recent_menu.delete(0, "end")
        rec = self.state.recent()
        if not rec:
            self.recent_menu.add_command(label="(empty)", state="disabled")
        for p in rec:
            self.recent_menu.add_command(label=_short(p, 80), command=lambda p=p: self.open_path(p))

    def close_tab(self, tab, ask=True):
        if ask and tab.session.dirty:
            r = messagebox.askyesnocancel("LeafPDF", "Save changes to %s?" % tab.display_name)
            if r is None:
                return False
            if r and not self.save(tab):
                return False
            if not r:
                tab.session.discard()
        self.notebook.forget(tab.frame)
        self.tabs.remove(tab)
        tab.session.close()
        if not tab.session.dirty and tab.session.work and os.path.exists(tab.session.work):
            try:
                os.remove(tab.session.work)
            except OSError:
                pass
        self.update_empty()
        self.schedule_persist()
        return True

    def close_current(self):
        t = self.current()
        if t:
            self.close_tab(t)

    def exit(self):
        # Unsaved edits are kept in working copies and restored next time.
        self.persist()
        for t in self.tabs:
            t.session.close()
        self.root.destroy()

    def cycle_tab(self, step):
        if self.tabs:
            cur = self.current()
            i = (self.tabs.index(cur) + step) % len(self.tabs)
            self.notebook.select(self.tabs[i].frame)

    # ========================================================== saving

    def save(self, tab=None):
        tab = tab or self.current()
        if not tab:
            return False
        if tab.display_name.endswith("(as PDF)"):
            return self.save_as(tab)
        try:
            self.busy()
            tab.session.save()
            self.status("Saved %s" % tab.path)
        except (IOError, OSError) as e:
            messagebox.showerror("LeafPDF", "Could not save:\n%s" % e)
            return False
        finally:
            self.busy(False)
        self.refresh_tab_title(tab)
        self.schedule_persist()
        return True

    def save_as(self, tab=None):
        tab = tab or self.current()
        if not tab:
            return False
        base = os.path.splitext(tab.display_name.replace(" (as PDF)", ""))[0]
        p = filedialog.asksaveasfilename(parent=self.root, defaultextension=".pdf",
                                         initialfile=base + ".pdf", filetypes=[("PDF", "*.pdf")])
        if not p:
            return False
        try:
            self.busy()
            tab.session.save(p)
        finally:
            self.busy(False)
        self.status("Saved as %s" % p)
        self.close_tab(tab, ask=False)
        self.open_path(p)
        return True

    def revert(self):
        tab = self.current()
        if tab and tab.session.work and messagebox.askyesno(
                "LeafPDF", "Throw away all unsaved changes to %s?" % tab.display_name):
            tab.session.discard()
            self.after_edit(tab)

    def refresh_tab_title(self, tab):
        try:
            self.notebook.tab(tab.frame, text=tab.title())
        except tk.TclError:
            pass
        if tab is self.current():
            self.root.title("%s - LeafPDF" % tab.title())

    # ======================================================== navigation

    def on_tab_changed(self):
        tab = self.current()
        if not tab:
            return
        self.root.title("%s - LeafPDF" % tab.title())
        self.fill_topics(tab)
        self.fill_bookmarks(tab)
        self.show_page(tab, keep_scroll=True)
        self.schedule_persist()

    def goto_page(self, index, frac=0.0, tab=None):
        tab = tab or self.current()
        if not tab:
            return
        index = max(0, min(int(index), tab.page_count() - 1))
        tab.page = index
        tab.scroll = frac
        self.show_page(tab, keep_scroll=True)
        self.schedule_persist()

    def goto_page_text(self, text):
        try:
            self.goto_page(int(text.split("/")[0].strip()) - 1)
        except ValueError:
            self.bell()

    def goto_dialog(self):
        tab = self.current()
        if tab:
            n = simpledialog.askinteger("Go to page", "Page (1-%d):" % tab.page_count(),
                                        parent=self.root, minvalue=1, maxvalue=tab.page_count())
            if n:
                self.goto_page(n - 1)

    def next_page(self):
        tab = self.current()
        if tab and tab.page < tab.page_count() - 1:
            self.goto_page(tab.page + 1)

    def prev_page(self):
        tab = self.current()
        if tab and tab.page > 0:
            self.goto_page(tab.page - 1)

    def bell(self):
        self.root.bell()

    def set_zoom(self, z):
        tab = self.current()
        if tab:
            tab.zoom = z
            self.show_page(tab, keep_scroll=True)
            self.schedule_persist()

    def set_zoom_text(self, text):
        t = text.strip().lower()
        if t.startswith("fit w"):
            return self.set_zoom("fit-width")
        if t.startswith("fit p"):
            return self.set_zoom("fit-page")
        try:
            self.set_zoom(max(0.1, min(float(t.rstrip("%")) / 100.0, 6.0)))
        except ValueError:
            self.bell()

    def zoom_step(self, d):
        tab = self.current()
        if not tab:
            return
        z = tab.zoom_factor()
        if d > 0:
            nxt = [s for s in ZOOM_STEPS if s > z + 0.01]
            self.set_zoom(nxt[0] if nxt else ZOOM_STEPS[-1])
        else:
            prv = [s for s in ZOOM_STEPS if s < z - 0.01]
            self.set_zoom(prv[-1] if prv else ZOOM_STEPS[0])

    def on_resize(self, tab):
        if tab is self.current() and tab.zoom in ("fit-width", "fit-page"):
            if getattr(tab, "_resize_job", None):
                self.root.after_cancel(tab._resize_job)
            tab._resize_job = self.root.after(250, lambda: self.show_page(tab, keep_scroll=True))
        elif tab.image is not None:
            self.place_image(tab)

    def on_wheel(self, tab, direction, event):
        if event.state & 0x4:  # Ctrl+wheel = zoom
            self.zoom_step(-direction)
            return "break"
        top, bottom = tab.canvas.yview()
        if direction > 0 and bottom >= 0.999:
            if tab.page < tab.page_count() - 1:
                self.goto_page(tab.page + 1, 0.0)
            return "break"
        if direction < 0 and top <= 0.001:
            if tab.page > 0:
                self.goto_page(tab.page - 1, 1.0)
            return "break"
        tab.canvas.yview_scroll(direction * 3, "units")
        self.on_scrolled(tab)
        return "break"

    def on_scrolled(self, tab):
        top, bottom = tab.canvas.yview()
        span = bottom - top
        tab.scroll = top / (1 - span) if span < 0.999 else 0.0
        self.sync_topic(tab)
        self.schedule_persist()

    def on_motion(self, tab, e):
        if tab.image is None:
            return
        x = tab.canvas.canvasx(e.x) - tab.img_x
        y = tab.canvas.canvasy(e.y) - tab.img_y
        z = tab.dpi() / 72.0
        w, h = tab.page_size()
        px, py = x / z, y / z
        if 0 <= px <= w and 0 <= py <= h:
            self.pos_var.set("x %d  y %d pt (from top-left)  |  %.0f%%" % (px, py, tab.zoom_factor() * 100))
        else:
            self.pos_var.set("%.0f%%" % (tab.zoom_factor() * 100))

    # =========================================================== render

    def show_page(self, tab, keep_scroll=False):
        if tab is not self.current():
            return
        total = tab.page_count()
        tab.page = max(0, min(tab.page, total - 1))
        self.page_var.set(str(tab.page + 1))
        self.total_label.configure(text="/ %d" % total)
        z = tab.zoom
        self.zoom_var.set("Fit width" if z == "fit-width" else "Fit page" if z == "fit-page"
                          else "%d%%" % round(float(z) * 100))
        self.sync_topic(tab)
        if not self.renderer.available():
            self.draw_message(tab, "Install MuPDF (mutool) or Ghostscript to see pages.\n"
                                   "Help > Helper programs explains how.")
            return
        tab.token += 1
        dpi = tab.dpi()
        path = tab.session.current_path
        self.worker.submit((id(tab), tab.token, tab.page, keep_scroll), path, tab.page, dpi)
        for nxt in (tab.page + 1, tab.page - 1):
            if 0 <= nxt < total:
                self.worker.submit(None, path, nxt, dpi, prefetch=True)
        self.status("Page %d of %d" % (tab.page + 1, total))

    def draw_message(self, tab, text):
        c = tab.canvas
        c.delete("all")
        tab.image = None
        c.create_text(max(c.winfo_width(), 400) // 2, 120, text=text, fill="white",
                      justify="center", font=("Arial", 11))

    def poll(self):
        try:
            while True:
                token, out, err = self.worker.results.get_nowait()
                tab_id, tok, page, keep = token
                tab = next((t for t in self.tabs if id(t) == tab_id), None)
                if tab is None or tok != tab.token:
                    continue
                if err is not None:
                    self.draw_message(tab, "Could not draw page %d:\n%s" % (page + 1, err))
                    continue
                try:
                    tab.image = tk.PhotoImage(file=out)
                except tk.TclError as e:
                    self.draw_message(tab, "Could not load page image: %s" % e)
                    continue
                self.place_image(tab)
                span = tab.canvas.yview()
                visible = span[1] - span[0]
                tab.canvas.yview_moveto(tab.scroll * (1 - visible) if keep else 0)
                tab.canvas.xview_moveto(0)
                self.sync_topic(tab)
        except queue.Empty:
            pass
        self.root.after(40, self.poll)

    def place_image(self, tab):
        c = tab.canvas
        c.delete("all")
        iw, ih = tab.image.width(), tab.image.height()
        cw = max(c.winfo_width(), 1)
        tab.img_x = max(12, (cw - iw) // 2)
        tab.img_y = 12
        c.create_rectangle(tab.img_x + 3, tab.img_y + 3, tab.img_x + iw + 3, tab.img_y + ih + 3,
                           fill="#555555", outline="")
        c.create_image(tab.img_x, tab.img_y, image=tab.image, anchor="nw")
        c.configure(scrollregion=(0, 0, max(cw, iw + 24), ih + 24))

    # =========================================================== topics

    def fill_topics(self, tab):
        self.tree.delete(*self.tree.get_children())
        self.topic_items = {}
        self.topic_iids = {}
        values = []

        def add(parent, items, depth):
            for it in items:
                label = it.title if it.page is None else "%s   \u00b7 p.%d" % (it.title, it.page + 1)
                iid = self.tree.insert(parent, "end", text=label, open=it.is_open or depth == 0 and len(items) < 15)
                self.topic_items[iid] = it
                self.topic_iids[id(it)] = iid
                values.append(("    " * depth) + it.title)
                add(iid, it.children, depth + 1)
        add("", tab.outline, 0)
        self.topic_list = [it for _, it in flatten(tab.outline)]
        self.topic_box.configure(values=values)
        if not values:
            self.topic_var.set("(no topics - add some in the Topics panel)")
        self.sync_topic(tab)

    def sync_topic(self, tab):
        """Highlight the topic the reader is currently in."""
        if tab is not self.current() or not getattr(self, "topic_list", None):
            return
        it = current_item(tab.outline, tab.page, tab.scroll)
        if it is None:
            self.topic_var.set("(before first topic)")
            return
        idx = self.topic_list.index(it) if it in self.topic_list else -1
        if idx >= 0:
            self.topic_box.current(idx)
        iid = self.topic_iids.get(id(it))
        if iid and self.tree.selection() != (iid,):
            self._topic_sync = True
            p = self.tree.parent(iid)
            while p:
                self.tree.item(p, open=True)
                p = self.tree.parent(p)
            self.tree.selection_set(iid)
            self.tree.see(iid)
            self.root.after(10, self._end_sync)
        path = []
        cur = iid
        while cur:
            path.insert(0, self.topic_items[cur].title)
            cur = self.tree.parent(cur)
        self.status("Page %d of %d   \u2502   %s" % (tab.page + 1, tab.page_count(),
                                                      _short(" \u203a ".join(path), 90)))

    def _end_sync(self):
        self._topic_sync = False

    def on_tree_select(self, event=None):
        if self._topic_sync:
            return
        sel = self.tree.selection()
        if sel:
            self.jump_to_topic(self.topic_items.get(sel[0]))

    def on_topic_combo(self, event=None):
        i = self.topic_box.current()
        if 0 <= i < len(self.topic_list):
            self.jump_to_topic(self.topic_list[i])

    def jump_to_topic(self, it):
        if it is not None and it.page is not None:
            self.goto_page(it.page, it.frac)

    def selected_topic(self):
        sel = self.tree.selection()
        return self.topic_items.get(sel[0]) if sel else None

    def _siblings(self, tab, it):
        def find(items):
            if it in items:
                return items
            for x in items:
                r = find(x.children)
                if r is not None:
                    return r
            return None
        return find(tab.outline)

    def save_topics(self, tab, label):
        items = tab.outline
        try:
            self.busy()
            tab.session.apply(lambda doc: write_outline(doc, items), label)
        except PdfError as e:
            messagebox.showerror("LeafPDF", str(e))
        finally:
            self.busy(False)
        self.after_edit(tab)

    def _top_for(self, tab):
        try:
            x0, y0, x1, y1, rot = tab.doc.page_box(tab.page)
            if rot == 0:
                return round(y1 - tab.scroll * 0.85 * (y1 - y0), 1)
        except Exception:
            pass
        return None

    def topic_add(self):
        tab = self.current()
        if not tab:
            return
        title = simpledialog.askstring("New topic", "Topic title for page %d:" % (tab.page + 1),
                                       parent=self.root)
        if not title:
            return
        new = OutlineItem(title, tab.page, self._top_for(tab))
        cur = self.selected_topic() or current_item(tab.outline, tab.page, tab.scroll)
        if cur is None:
            pos = [i for i, x in enumerate(tab.outline)
                   if x.page is not None and x.page <= tab.page]
            tab.outline.insert(pos[-1] + 1 if pos else 0, new)
        else:
            sib = self._siblings(tab, cur)
            sib.insert(sib.index(cur) + 1, new)
        self.save_topics(tab, "add topic %s" % title)

    def topic_rename(self):
        tab, it = self.current(), self.selected_topic()
        if tab and it:
            title = simpledialog.askstring("Rename topic", "Title:", initialvalue=it.title,
                                           parent=self.root)
            if title:
                it.title = title
                self.save_topics(tab, "rename topic")

    def topic_delete(self):
        tab, it = self.current(), self.selected_topic()
        if tab and it and messagebox.askyesno("Delete topic", "Delete '%s'%s?" % (
                it.title, " and its %d sub-topics" % len(it.children) if it.children else "")):
            self._siblings(tab, it).remove(it)
            self.save_topics(tab, "delete topic")

    def topic_move(self, d):
        tab, it = self.current(), self.selected_topic()
        if tab and it:
            sib = self._siblings(tab, it)
            i = sib.index(it)
            j = i + d
            if 0 <= j < len(sib):
                sib[i], sib[j] = sib[j], sib[i]
                self.save_topics(tab, "move topic")

    def topic_indent(self):
        tab, it = self.current(), self.selected_topic()
        if tab and it:
            sib = self._siblings(tab, it)
            i = sib.index(it)
            if i > 0:
                sib.pop(i)
                sib[i - 1].children.append(it)
                sib[i - 1].is_open = True
                self.save_topics(tab, "indent topic")

    def topic_outdent(self):
        tab, it = self.current(), self.selected_topic()
        if not (tab and it):
            return
        sib = self._siblings(tab, it)
        if sib is tab.outline:
            return

        def parent_of(items, target_list):
            for x in items:
                if x.children is target_list:
                    return x, items
                r = parent_of(x.children, target_list)
                if r:
                    return r
            return None
        parent, psib = parent_of(tab.outline, sib)
        sib.remove(it)
        psib.insert(psib.index(parent) + 1, it)
        self.save_topics(tab, "outdent topic")

    def bookmarks_to_topics(self):
        tab = self.current()
        if not tab:
            return
        bms = self.state.bookmarks(tab.path)
        if not bms:
            messagebox.showinfo("LeafPDF", "This file has no personal bookmarks yet.")
            return
        for b in bms:
            tab.outline.append(OutlineItem(b["name"], b["page"], None))
        tab.outline.sort(key=lambda x: (x.page if x.page is not None else 10 ** 9))
        self.save_topics(tab, "bookmarks to topics")

    # ======================================================== bookmarks

    def fill_bookmarks(self, tab):
        self.bm_list.delete(0, "end")
        for b in self.state.bookmarks(tab.path):
            self.bm_list.insert("end", "p.%-4d %s" % (b["page"] + 1, b["name"]))

    def add_bookmark(self):
        tab = self.current()
        if not tab:
            return
        it = current_item(tab.outline, tab.page, tab.scroll)
        default = ("%s (p.%d)" % (it.title, tab.page + 1)) if it else "Page %d" % (tab.page + 1)
        name = simpledialog.askstring("Bookmark", "Name for this place:", initialvalue=default,
                                      parent=self.root)
        if not name:
            return
        bms = self.state.bookmarks(tab.path)
        bms.append({"name": name, "page": tab.page, "frac": round(tab.scroll, 4),
                    "created": time.strftime("%Y-%m-%d %H:%M")})
        self.fill_bookmarks(tab)
        self.side.select(1)
        self.schedule_persist()

    def _bm_index(self):
        sel = self.bm_list.curselection()
        return int(sel[0]) if sel else None

    def bm_go(self):
        tab, i = self.current(), self._bm_index()
        if tab and i is not None:
            b = self.state.bookmarks(tab.path)[i]
            self.goto_page(b["page"], b.get("frac", 0))

    def bm_rename(self):
        tab, i = self.current(), self._bm_index()
        if tab and i is not None:
            b = self.state.bookmarks(tab.path)[i]
            name = simpledialog.askstring("Rename bookmark", "Name:", initialvalue=b["name"],
                                          parent=self.root)
            if name:
                b["name"] = name
                self.fill_bookmarks(tab)
                self.schedule_persist()

    def bm_delete(self):
        tab, i = self.current(), self._bm_index()
        if tab and i is not None:
            del self.state.bookmarks(tab.path)[i]
            self.fill_bookmarks(tab)
            self.schedule_persist()

    def bm_move(self, d):
        tab, i = self.current(), self._bm_index()
        if tab and i is not None:
            bms = self.state.bookmarks(tab.path)
            j = i + d
            if 0 <= j < len(bms):
                bms[i], bms[j] = bms[j], bms[i]
                self.fill_bookmarks(tab)
                self.bm_list.selection_set(j)
                self.schedule_persist()

    # ========================================================= editing

    def prefill(self, template):
        tab = self.current()
        p = str(tab.page + 1) if tab else "1"
        self.cmd_var.set(template.replace("{p}", p))
        self.cmd_entry.focus_set()
        self.cmd_entry.icursor("end")

    def cmd_hist(self, d):
        if not self.cmd_history:
            return "break"
        self.cmd_pos = max(0, min(self.cmd_pos + d, len(self.cmd_history)))
        self.cmd_var.set(self.cmd_history[self.cmd_pos] if self.cmd_pos < len(self.cmd_history) else "")
        self.cmd_entry.icursor("end")
        return "break"

    def run_command_line(self):
        text = self.cmd_var.get().strip()
        if not text:
            return
        if not self.cmd_history or self.cmd_history[-1] != text:
            self.cmd_history.append(text)
        self.cmd_pos = len(self.cmd_history)
        if self.run_script(text):
            self.cmd_var.set("")

    def run_script(self, text):
        tab = self.current()
        if not tab:
            messagebox.showinfo("LeafPDF", "Open a PDF first.")
            return False
        self.write_log("> " + text.replace("\n", "; "))
        try:
            self.busy()
            msgs = tab.session.run(text)
        except (PdfError, CommandError, ValueError, IOError, OSError) as e:
            self.write_log("  Error: %s" % e)
            return False
        finally:
            self.busy(False)
        for m in msgs:
            if m:
                self.write_log("  " + m.replace("\n", "\n  "))
        self.after_edit(tab)
        return True

    def after_edit(self, tab):
        tab.load_outline()
        tab.page = min(tab.page, tab.page_count() - 1)
        self.refresh_tab_title(tab)
        if tab is self.current():
            self.fill_topics(tab)
            self.show_page(tab, keep_scroll=True)
        self.schedule_persist()

    def undo(self):
        tab = self.current()
        if tab:
            label = tab.session.undo()
            self.write_log("Undid: %s" % label if label else "Nothing to undo.")
            self.after_edit(tab)

    def redo(self):
        tab = self.current()
        if tab:
            label = tab.session.redo()
            self.write_log("Redid: %s" % label if label else "Nothing to redo.")
            self.after_edit(tab)

    def script_dialog(self):
        win = tk.Toplevel(self.root)
        win.title("Command script")
        win.transient(self.root)
        ttk.Label(win, text="One command per line. All lines run together and can be undone "
                            "in one step.").pack(anchor="w", padx=8, pady=(8, 2))
        txt = tk.Text(win, width=80, height=10, wrap="none")
        txt.pack(fill="both", expand=True, padx=8)
        txt.insert("1.0", "# example\nreplace \"2023\" \"2024\" pages all\n"
                          "footer \"{file} - page {n}\"\n")
        ref = tk.Text(win, width=80, height=14, wrap="none", background="#f4f4f4")
        ref.insert("1.0", HELP.strip())
        ref.configure(state="disabled")
        ref.pack(fill="both", expand=True, padx=8, pady=6)
        bf = ttk.Frame(win)
        bf.pack(fill="x", padx=8, pady=(0, 8))

        def go():
            if self.run_script(txt.get("1.0", "end")):
                win.destroy()
        ttk.Button(bf, text="Run", command=go).pack(side="right")
        ttk.Button(bf, text="Cancel", command=win.destroy).pack(side="right", padx=4)
        txt.focus_set()

    def merge_dialog(self):
        tab = self.current()
        if not tab:
            return
        p = filedialog.askopenfilename(parent=self.root, filetypes=[("PDF", "*.pdf")])
        if p:
            self.run_script('merge "%s" after %d' % (p, tab.page + 1))

    def search(self):
        tab = self.current()
        needle = self.search_var.get().strip()
        if not tab or not needle:
            return
        if getattr(tab, "hit_needle", None) != needle:
            from .pdf.editor import find_text
            self.busy()
            try:
                tab.hits = [p for p, _ in find_text(tab.doc, range(tab.page_count()), needle)]
            finally:
                self.busy(False)
            tab.hit_needle = needle
            self.write_log("'%s' found on %d page(s)%s" % (
                needle, len(tab.hits), (": " + ", ".join(str(p + 1) for p in tab.hits[:40]))
                if tab.hits else ""))
        if not tab.hits:
            self.bell()
            return
        later = [p for p in tab.hits if p > tab.page]
        self.goto_page(later[0] if later else tab.hits[0])

    # ======================================================== conversion

    def background(self, label, job, done):
        self.status(label)
        self.busy()
        result = {}

        def run():
            try:
                result["ok"] = job()
            except Exception as e:
                result["err"] = e

        th = threading.Thread(target=run)
        th.daemon = True
        th.start()

        def check():
            if th.is_alive():
                self.root.after(150, check)
                return
            self.busy(False)
            if "err" in result:
                self.status("Failed")
                messagebox.showerror("LeafPDF", str(result["err"]))
            else:
                self.status("Done")
                done(result["ok"])
        self.root.after(150, check)

    def convert_images(self):
        paths = filedialog.askopenfilenames(parent=self.root, title="Pictures to put in one PDF",
                                            filetypes=[("Pictures", " ".join("*" + e for e in IMAGE_EXTS))])
        paths = list(self.root.tk.splitlist(paths)) if paths else []
        if not paths:
            return
        size = self.ask_choice("Page size", "Page size for the pictures:",
                               ["Same as picture", "A4", "Letter"])
        if size is None:
            return
        key = {"A4": "a4", "Letter": "letter"}.get(size, "auto")
        out = filedialog.asksaveasfilename(parent=self.root, defaultextension=".pdf",
                                           initialfile="pictures.pdf", filetypes=[("PDF", "*.pdf")])
        if not out:
            return
        self.background("Making PDF from %d picture(s)\u2026" % len(paths),
                        lambda: images_to_pdf(paths, out, key, margin=18 if key != "auto" else 0,
                                              tools=self.tools),
                        lambda r: self.open_path(r))

    def convert_office(self):
        src = filedialog.askopenfilename(parent=self.root, filetypes=[
            ("Office / text", " ".join("*" + e for e in OFFICE_EXTS)), ("All files", "*.*")])
        if not src:
            return
        out = filedialog.asksaveasfilename(parent=self.root, defaultextension=".pdf",
                                           initialfile=os.path.splitext(os.path.basename(src))[0] + ".pdf",
                                           filetypes=[("PDF", "*.pdf")])
        if out:
            self.background("Converting %s\u2026" % os.path.basename(src),
                            lambda: office_to_pdf(src, out, self.tools),
                            lambda r: self.open_path(r))

    def export_dialog(self, src=None, name=None):
        tab = self.current()
        if src is None:
            if not tab:
                return
            src = tab.session.current_path
            name = tab.display_name
        base = os.path.splitext(name or os.path.basename(src))[0].replace(" (as PDF)", "")
        out = filedialog.asksaveasfilename(parent=self.root, initialfile=base + ".docx",
                                           filetypes=EXPORT_TYPES, defaultextension=".docx")
        if not out:
            return
        fmt = os.path.splitext(out)[1].lstrip(".").lower() or "docx"

        def job():
            return pdf_to(src, out, fmt, renderer=self.renderer, tools=self.tools)

        def done(files):
            self.write_log("Saved %d file(s): %s" % (len(files), ", ".join(files[:5])))
            if files and messagebox.askyesno("LeafPDF", "Conversion finished. Open the folder?"):
                self.open_folder(os.path.dirname(files[0]))
        self.background("Converting to %s\u2026" % fmt.upper(), job, done)

    def export_other(self):
        src = filedialog.askopenfilename(parent=self.root, filetypes=[("PDF", "*.pdf")])
        if src:
            self.export_dialog(src, os.path.basename(src))

    def open_folder(self, path):
        try:
            if os.name == "nt":
                os.startfile(path)
            elif sys.platform == "darwin":
                os.system('open "%s"' % path)
            else:
                os.system('xdg-open "%s" >/dev/null 2>&1 &' % path)
        except OSError:
            pass

    def ask_choice(self, title, prompt, options):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text=prompt).pack(anchor="w", padx=10, pady=(10, 4))
        var = tk.StringVar(value=options[0])
        for o in options:
            ttk.Radiobutton(win, text=o, value=o, variable=var).pack(anchor="w", padx=16)
        res = {}
        bf = ttk.Frame(win)
        bf.pack(fill="x", padx=10, pady=10)
        ttk.Button(bf, text="OK", command=lambda: (res.update(v=var.get()), win.destroy())).pack(side="right")
        ttk.Button(bf, text="Cancel", command=win.destroy).pack(side="right", padx=4)
        self.root.wait_window(win)
        return res.get("v")

    # ============================================================= info

    def doc_info(self):
        tab = self.current()
        if not tab:
            return
        d = tab.doc
        info = d.info()
        w, h = tab.page_size()
        lines = ["File: %s" % tab.path, "Size: %.1f MB" % (os.path.getsize(tab.path) / 1048576.0),
                 "Pages: %d" % tab.page_count(), "This page: %.0f x %.0f pt (%.0f x %.0f mm)" %
                 (w, h, w / 72 * 25.4, h / 72 * 25.4),
                 "Topics: %d" % len(flatten(tab.outline)),
                 "Encrypted: %s" % ("yes (view only)" if d.encrypted else "no"),
                 "Repaired on load: %s" % ("yes" if d.repaired else "no")]
        for k in ("Title", "Author", "Subject", "Creator", "Producer"):
            if info.get(k):
                lines.append("%s: %s" % (k, info[k]))
        if tab.session.history:
            lines.append("Unsaved edits: %d" % (len(tab.session.history) - tab.session.saved_at))
        messagebox.showinfo("Document info", "\n".join(lines))

    def show_help(self):
        win = tk.Toplevel(self.root)
        win.title("LeafPDF commands")
        t = tk.Text(win, width=92, height=34, wrap="none")
        t.insert("1.0", HELP.strip() + "\n\nExamples:\n"
                 '  replace "Acme Ltd" "Acme Inc" pages all\n'
                 '  replace "draft" "final" -i; footer "{file}"; number start 1\n'
                 '  stamp "APPROVED" pages 1 pos top-right color red size 18\n'
                 '  whiteout 72 700 200 20 pages 2-4\n'
                 '  delete pages 5-7; rotate 90 pages odd\n'
                 '  merge "appendix.pdf" after 10\n'
                 "\nTip: the status bar shows the pointer position in points (for 'at X Y').\n"
                 "Keys: PgUp/PgDn pages, Ctrl+G go to page, Ctrl+F find, Ctrl+B bookmark,\n"
                 "Ctrl+L command line, Ctrl+Z / Ctrl+Y undo/redo, Ctrl+wheel zoom.")
        t.configure(state="disabled")
        t.pack(fill="both", expand=True)

    def setup_dialog(self):
        msg = (self.tools.summary() + "\n\nTo pick a program yourself, choose Yes.\n\n"
               "Portable setup: put mutool.exe in tools\\mupdf\\ next to LeafPDF, or "
               "Ghostscript in tools\\gs\\. See README for versions that run on Windows XP.")
        if messagebox.askyesno("Helper programs", msg):
            which = self.ask_choice("Helper", "Which program do you want to set?",
                                    ["MuPDF mutool", "Ghostscript", "LibreOffice soffice"])
            if not which:
                return
            p = filedialog.askopenfilename(parent=self.root, title="Locate " + which)
            if p:
                key = {"MuPDF mutool": "mutool", "Ghostscript": "ghostscript",
                       "LibreOffice soffice": "soffice"}[which]
                self.state.config[key] = p
                self.tools.refresh()
                self.persist()
                cur = self.current()
                if cur:
                    self.show_page(cur, keep_scroll=True)

    def about(self):
        messagebox.showinfo("About LeafPDF", "LeafPDF %s\nA small PDF reader and editor.\n"
                            "Python %s, Tk %s\n\n%s" % (__version__, sys.version.split()[0],
                                                       tk.TkVersion, self.tools.summary()))


def main(argv=None):
    if tk is None:
        sys.stderr.write("Tkinter is not available in this Python.\n")
        return 1
    argv = sys.argv[1:] if argv is None else argv
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" not in style.theme_names() and "xpnative" in style.theme_names():
            style.theme_use("xpnative")
    except tk.TclError:
        pass
    App(root, [a for a in argv if not a.startswith("-")])
    root.mainloop()
    return 0
