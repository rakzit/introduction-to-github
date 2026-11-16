"""Content stream tokenizer and writer."""

import re

from .lexer import Lexer, Keyword, EndOfData
from .objects import serialize

_ei_re = re.compile(rb"[\x00\t\n\x0c\r ]EI(?=[\x00\t\n\x0c\r /<\[(]|$)")


class InlineImage(object):
    def __init__(self, params, data):
        self.params = params
        self.data = data


def parse_content(data):
    """Parse content bytes into a list of (operands, operator) tuples."""
    ops = []
    lx = Lexer(data)
    operands = []
    while True:
        try:
            tok = lx.next_token()
        except EndOfData:
            break
        if tok is None:
            break
        if isinstance(tok, Keyword):
            if tok in ("true", "false", "null"):
                operands.append({"true": True, "false": False, "null": None}[str(tok)])
                continue
            if tok == "BI":
                params = {}
                while True:
                    k = lx.next_token()
                    if k is None or (isinstance(k, Keyword) and k == "ID"):
                        break
                    v = lx._build(lx.next_token(), False)
                    params[k] = v
                start = lx.pos + 1
                m = _ei_re.search(data, start)
                end = m.start() if m else len(data)
                ops.append(([InlineImage(params, data[start:end])], "BI"))
                lx.pos = m.end() if m else len(data)
                operands = []
                continue
            ops.append((operands, str(tok)))
            operands = []
        else:
            try:
                operands.append(lx._build(tok, False))
            except EndOfData:
                break
    return ops


def serialize_content(ops):
    out = []
    for operands, op in ops:
        if op == "BI":
            img = operands[0]
            parts = [b"BI"]
            for k, v in img.params.items():
                parts.append(serialize(k) + b" " + serialize(v))
            out.append(b" ".join(parts) + b" ID " + img.data + b"\nEI")
            continue
        if operands:
            out.append(b" ".join(serialize(o) for o in operands) + b" " + op.encode("latin-1"))
        else:
            out.append(op.encode("latin-1"))
    return b"\n".join(out) + b"\n"
