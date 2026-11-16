"""Document outline (the PDF's own bookmarks / table of contents)."""

from .objects import Name, Ref, PdfString, decode_text_string, encode_text_string


class OutlineItem(object):
    def __init__(self, title, page=None, top=None, children=None, is_open=False):
        self.title = title
        self.page = page          # 0-based page index or None
        self.top = top            # y coordinate (PDF user space) or None
        self.children = children if children is not None else []
        self.is_open = is_open
        self.frac = 0.0           # 0 = top of page, 1 = bottom

    def to_json(self):
        return {"title": self.title, "page": self.page, "top": self.top,
                "open": self.is_open,
                "children": [c.to_json() for c in self.children]}

    @classmethod
    def from_json(cls, d):
        item = cls(d.get("title", ""), d.get("page"), d.get("top"),
                   [cls.from_json(c) for c in d.get("children", [])],
                   d.get("open", False))
        return item

    def __repr__(self):
        return "OutlineItem(%r, page=%r)" % (self.title, self.page)


def _name_tree_lookup(doc, root, key):
    node = doc.resolve(root)
    depth = 0
    while isinstance(node, dict) and depth < 64:
        depth += 1
        names = doc.resolve(node.get("Names"))
        if isinstance(names, list):
            for i in range(0, len(names) - 1, 2):
                if bytes(doc.resolve(names[i]) or b"") == key:
                    return doc.resolve(names[i + 1])
        kids = doc.resolve(node.get("Kids"))
        if not isinstance(kids, list):
            return None
        nxt = None
        for kid in kids:
            k = doc.resolve(kid)
            if not isinstance(k, dict):
                continue
            limits = doc.resolve(k.get("Limits"))
            if isinstance(limits, list) and len(limits) == 2:
                lo, hi = bytes(doc.resolve(limits[0])), bytes(doc.resolve(limits[1]))
                if lo <= key <= hi:
                    nxt = k
                    break
            else:
                found = _name_tree_lookup(doc, k, key)
                if found is not None:
                    return found
        node = nxt
    return None


def resolve_dest(doc, dest):
    """Return (page_index, top) for an explicit or named destination."""
    dest = doc.resolve(dest)
    for _ in range(4):
        if isinstance(dest, (PdfString, bytes, Name)):
            cat = doc.catalog()
            found = None
            if isinstance(dest, Name):
                dests = doc.resolve(cat.get("Dests"))
                if isinstance(dests, dict):
                    found = doc.resolve(dests.get(dest))
            else:
                names = doc.resolve(cat.get("Names"))
                if isinstance(names, dict) and names.get("Dests") is not None:
                    found = _name_tree_lookup(doc, names.get("Dests"), bytes(dest))
                if found is None:
                    dests = doc.resolve(cat.get("Dests"))
                    if isinstance(dests, dict):
                        found = doc.resolve(dests.get(Name(bytes(dest).decode("latin-1"))))
            if isinstance(found, dict):
                found = doc.resolve(found.get("D"))
            dest = found
        else:
            break
    if not isinstance(dest, list) or not dest:
        return None, None
    target = dest[0]
    page = None
    if isinstance(target, Ref):
        page = doc.page_number_of(target)
    elif isinstance(target, int):
        page = target if 0 <= target < doc.page_count() else None
    top = None
    kind = doc.resolve(dest[1]) if len(dest) > 1 else None
    if kind == "XYZ" and len(dest) > 3:
        top = doc.resolve(dest[3])
    elif kind in ("FitH", "FitBH") and len(dest) > 2:
        top = doc.resolve(dest[2])
    elif kind == "FitR" and len(dest) > 5:
        top = doc.resolve(dest[5])
    if not isinstance(top, (int, float)) or isinstance(top, bool):
        top = None
    return page, top


def _item_dest(doc, d):
    if "Dest" in d:
        return resolve_dest(doc, d["Dest"])
    action = doc.resolve(d.get("A"))
    if isinstance(action, dict) and action.get("S") == "GoTo":
        return resolve_dest(doc, action.get("D"))
    return None, None


def read_outline(doc):
    """Return the outline as a list of OutlineItem trees."""
    try:
        root = doc.resolve(doc.catalog().get("Outlines"))
    except Exception:
        return []
    if not isinstance(root, dict):
        return []
    result = []
    seen = set()
    # stack of (first_child_ref, target_list)
    stack = [(root.get("First"), result)]
    count = 0
    while stack:
        ref, target = stack.pop()
        while isinstance(ref, Ref) and ref.num not in seen and count < 100000:
            seen.add(ref.num)
            count += 1
            d = doc.resolve(ref)
            if not isinstance(d, dict):
                break
            title = decode_text_string(doc.resolve(d.get("Title")))
            try:
                page, top = _item_dest(doc, d)
            except Exception:
                page, top = None, None
            c = doc.resolve(d.get("Count"))
            item = OutlineItem(title.replace("\r", " ").replace("\n", " "), page, top,
                               is_open=isinstance(c, int) and c > 0)
            if page is not None and top is not None:
                try:
                    x0, y0, x1, y1, rot = doc.page_box(page)
                    if rot == 0 and y1 > y0:
                        item.frac = min(1.0, max(0.0, (y1 - top) / (y1 - y0)))
                except Exception:
                    pass
            target.append(item)
            if isinstance(d.get("First"), Ref):
                # process children after siblings: push siblings continuation
                stack.append((d.get("Next"), target))
                stack.append((d.get("First"), item.children))
                break
            ref = d.get("Next")
    return result


def flatten(items, depth=0, out=None):
    if out is None:
        out = []
    for it in items:
        out.append((depth, it))
        flatten(it.children, depth + 1, out)
    return out


def current_item(items, page, frac):
    """The outline item the reader is 'in' at page/frac (or None)."""
    best = None
    for depth, it in flatten(items):
        if it.page is None:
            continue
        if it.page < page or (it.page == page and it.frac <= frac + 0.02):
            if best is None or (it.page, it.frac) >= (best.page, best.frac):
                best = it
    return best


def write_outline(doc, items):
    """Replace the document outline with ``items`` (pending until commit)."""
    doc.check_editable()
    pages = doc.page_refs()
    cat_ref = doc.trailer["Root"]
    catalog = doc.editable(cat_ref)
    if not items:
        catalog.pop("Outlines", None)
        return
    root_ref = doc.add(None)

    def visible(its):
        n = 0
        for it in its:
            n += 1
            if it.is_open:
                n += visible(it.children)
        return n

    def build(its, parent_ref):
        refs = [doc.add(None) for _ in its]
        for i, it in enumerate(its):
            d = {Name("Title"): encode_text_string(it.title or "Untitled"),
                 Name("Parent"): parent_ref}
            if i > 0:
                d[Name("Prev")] = refs[i - 1]
            if i + 1 < len(refs):
                d[Name("Next")] = refs[i + 1]
            if it.page is not None and 0 <= it.page < len(pages):
                top = it.top
                d[Name("Dest")] = [pages[it.page], Name("XYZ"), None,
                                   top if top is not None else None, None]
            if it.children:
                crefs = build(it.children, refs[i])
                d[Name("First")] = crefs[0]
                d[Name("Last")] = crefs[-1]
                n = visible(it.children)
                d[Name("Count")] = n if it.is_open else -len(it.children)
            doc.update(refs[i], d)
        return refs

    top_refs = build(items, root_ref)
    doc.update(root_ref, {Name("Type"): Name("Outlines"), Name("First"): top_refs[0],
                          Name("Last"): top_refs[-1], Name("Count"): visible(items)})
    catalog[Name("Outlines")] = root_ref
