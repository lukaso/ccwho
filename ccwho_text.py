"""Text as the screen draws it: cells, not characters, and cuts that say so.

Shared by the engine (rows) and ccwho_usage (the usage line), which cannot import
each other: the engine imports ccwho_usage. Pure; stdlib apart from Rich's own
cell counter, used when it is there so the list and its tests agree with Rich.
"""
from __future__ import annotations

import re

ANSI = re.compile(r"\033(?:\][^\007\033]*(?:\007|\033\\)|\[[0-9;]*[A-Za-z])")

try:        # the live list is drawn by Rich: count cells the way it does
    from rich.cells import cell_len as _rich_cell_len
except ImportError:                 # the command line has no Rich, and no need
    _rich_cell_len = None


def cells(text):
    """Screen cells, not characters: a CJK character or an emoji takes two, a
    combining mark none. What a row must fit, where a name can be anything."""
    text = ANSI.sub("", text or "")
    if _rich_cell_len is not None:
        return _rich_cell_len(text)
    import unicodedata
    return sum(0 if unicodedata.combining(c) else
               2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def cut(text, width):
    """`text` in at most `width` screen cells, `…` marking a cut."""
    text = text or ""
    if cells(text) <= width:
        return text
    out = ""
    for c in text:
        # the whole prefix, not char by char: a ZWJ family is one glyph
        if cells(out + c) > width - 1:
            break
        out += c
    return out + "…" if width > 0 else ""
