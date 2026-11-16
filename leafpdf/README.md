# LeafPDF

A small PDF reader and editor written in plain Python. It uses only the
standard library and the language features of **Python 3.4**, the last
Python release that runs on **Windows XP**. The GUI is Tkinter, which ships
with Python.

| Requirement | How LeafPDF covers it |
|---|---|
| 1. Bookmarks, page/topic selector | **Topics** panel shows the PDF's own outline, and you can add, rename, delete, reorder and indent topics; they are saved into the PDF. A **Topic** box in the toolbar always shows the topic you are reading and jumps to any topic. The status bar shows the breadcrumb (`Chapter 2 › 2.1 Data`). **Bookmarks** panel holds your personal bookmarks per file (Ctrl+B), which are never written into the PDF. |
| 2. Reopen what was left open | Every open file, its page, its scroll position and zoom, and even **unsaved edits**, come back the next time the app starts, including after a crash. The state is saved on every page change. Each file also remembers its last page after you close it. |
| 3. Edit text on many pages with a few commands | There is a command bar and a script dialog, for example `replace "2023" "2024" pages all`, `footer "{file} – page {n}"` or `delete pages 5-7`. Each command (or each whole script) is one undo step. |
| 4. Converters | Pictures → PDF and text → PDF are built in. Word/Excel/PowerPoint → PDF uses Microsoft Office or LibreOffice. PDF → DOCX, RTF, XLSX, PPTX, TXT and HTML are built in. PDF → PNG/JPG/TIFF/BMP uses MuPDF or Ghostscript, and PDF → ODT/DOC uses LibreOffice. |
| 5. Lightweight, runs back to Windows XP | About 6,000 lines of Python with no third-party packages. Idle memory use is about 30 MB. |
| 6. Large files | Objects are read on demand and no file is loaded into memory whole. Only the page on screen is drawn. Edits are appended as PDF incremental updates. A 553 MB, 3,000-page file opens in 0.2 s, draws any page in about 50 ms, numbers all 3,000 pages in about 1.5 s, and undoes in 15 ms. Peak memory use is 35 MB. Files over 2 GB work on 32-bit systems because no memory mapping is used. |

## Running

```
pythonw run_leafpdf.pyw            (Windows: or double-click run_leafpdf.pyw / run_leafpdf.bat)
python -m leafpdf [file.pdf ...]   (any OS)
```

You can open PDFs, pictures and Office documents. Pictures and Office
documents are converted to PDF when you open them.

### Helper programs (optional, but you need one to *see* pages)

LeafPDF reads, edits and converts PDFs by itself. To draw pages it calls one of these:

* **MuPDF** `mutool` (recommended: small and fast). Put `mutool.exe` in `tools\mupdf\`, next to `run_leafpdf.pyw`.
* **Ghostscript**. Put it in `tools\gs\bin\gswin32c.exe`, or install it normally.

For Office → PDF it uses **Microsoft Office 2007 or later** (through Windows
Script Host, so no Python add-ons are needed) or **LibreOffice**. You can see
what LeafPDF found, or point it to a program, under *Help → Helper programs*.

## Windows XP setup

1. Install **Python 3.4.4 (x86)** from python.org. This is the last version that supports XP, and it includes Tk 8.6.
2. Copy this `leafpdf` folder anywhere, for example to a USB stick.
3. Put a page renderer in `tools\`. Current MuPDF and Ghostscript builds no longer support XP, so use an older 32-bit release from the projects' download archives (MuPDF 1.x Windows zip, or Ghostscript 9.x `gs9xxw32`). To check one, run `mutool -v` or `gswin32c -v` in a command prompt.
4. Optional, for Office → PDF: Office 2010, or Office 2007 with Microsoft's "Save as PDF" add-in. Alternatively, use an XP-compatible LibreOffice 5.x.
5. Optional: to get a single `.exe`, py2exe 0.9.2.x supports Python 3.4.

> Note: I tested LeafPDF on Linux with Python 3.11–3.13. Static analysis
> (`vermin`) reports Python 3.3 as the minimum version. It has not yet been
> run on a real Windows XP machine, and the Microsoft Office conversion path
> has not been run on Windows at all.

## Edit commands

Type a command in the **Command** bar and press Enter. To run several at
once, open **Script…** (one command per line) or separate them with `;`.

```
replace "old" "new" [pages SPEC] [-i]     replace text (-i = ignore case)
find "text" [pages SPEC]                  list pages containing text
stamp "text" [pages SPEC] [pos P | at X Y] [size N] [color C] [opacity 0-1] [angle DEG]
header "text" / footer "text"             stamp at top / bottom center
watermark "text"                          big diagonal semi-transparent text
number ["Page {n} of {total}"] [pos P] [start N]
whiteout X Y W H [pages SPEC]             cover an area (points from top-left)
rotate DEG [pages SPEC]                   90 / 180 / 270
delete pages SPEC
move pages SPEC to N
reverse
blank [after N] [count K]
merge "other.pdf" [after N] [pages SPEC]
extract pages SPEC to "part.pdf"
compact to "smaller.pdf"                  rewrite without old revisions
```

`SPEC` can be `all`, `odd`, `even`, `last`, `3`, `2-5`, `7-`, `1,3,9-12` or `last-2`.
`P` can be `top-left`, `top`, `top-right`, `center`, `bottom-left`, `bottom` or `bottom-right`.
Text can contain `{n}`, `{total}`, `{date}` and `{file}`. As you move the
mouse over a page, the status bar shows its position in points, which you can
use with `at X Y` and `whiteout`.

**About `replace`:** if the font embedded in the PDF has the new letters,
LeafPDF edits the text in place. Most PDFs embed only the letters they use
(a *subset* font). In that case LeafPDF removes the old letters, keeping the
layout, and draws the new text in Helvetica at the same position, size and
color. If a phrase is split into separately positioned pieces in the file,
it cannot be matched, and the result message tells you how many such places
it found.

### Saving and undo

The first edit makes a private working copy of the file. Each command
appends one incremental revision to that copy, so undo and redo are instant
even for very large files. **Save** (Ctrl+S) writes the working copy over
your file. **Save as** writes it somewhere else. If you quit with unsaved
edits, they are kept and come back next time. The tab shows `*` while there
are unsaved edits.

## Command line

```
python -m leafpdf edit in.pdf 'replace "Draft" "Final"; number' -o out.pdf
python -m leafpdf convert report.pdf report.docx       # by extension
python -m leafpdf convert slides.pptx slides.pdf
python -m leafpdf images album.pdf a.jpg b.png --page-size a4
python -m leafpdf info | outline | text  file.pdf
python -m leafpdf tools                                # detected helpers
```

## Keys

PgUp / PgDn move between pages, and the mouse wheel turns the page at the
top or bottom edge. Ctrl+G goes to a page, Ctrl+F finds text (F3 finds the
next match), Ctrl+B adds a bookmark, and Ctrl+L jumps to the command line.
Ctrl+Z / Ctrl+Y undo and redo, Ctrl + wheel or Ctrl +/- zoom, Ctrl+0 fits
the width, Ctrl+Tab switches files, and Ctrl+W closes the current file.

## Code layout

```
leafpdf/pdf/      PDF engine: lexer, filters, lazy document, incremental writer,
                  fonts/encodings, content streams, outline, editing operations
leafpdf/commands.py  command language + undoable EditSession
leafpdf/render.py    MuPDF / Ghostscript rendering
leafpdf/convert.py   all conversions (OOXML / RTF writers built in)
leafpdf/session.py   reopen-on-start state and personal bookmarks
leafpdf/gui.py       Tkinter window
tests/               python -m unittest discover tests
```

## Limitations

* Encrypted PDFs can be viewed (through MuPDF or Ghostscript) but not edited.
* `replace` works on text drawn on pages and in form XObjects. It does not
  touch annotations or form fields.
* PDF → Word/Excel rebuilds text and simple tables. It does not reproduce
  the exact layout. PDF → PowerPoint places one picture of each page per slide.
