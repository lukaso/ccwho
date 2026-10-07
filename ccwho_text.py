"""Text as the screen draws it: cells, not characters, and cuts that say so.

Shared by the engine (rows), ccwho_usage (the usage line) and ccwho_index (what
was said, as a row shows it), which cannot import each other: the engine imports
the other two. Pure; stdlib apart from Rich's own cell counter, used when it is
there so the list and its tests agree with Rich.
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


# Every escape a session might have printed, as a terminal reads it: an OSC up
# to what ends it (BEL, ST, a CAN or SUB that aborts it, the next ESC - or, one
# never ended, its line: a terminal would eat the rest, but the rest of a recap
# is worth more than that), any CSI - the private ones too (\x1b[?25l hides the
# cursor) - and the short ones, ESC and a final (tput sgr0 prints \x1b(B). Not
# the C1 forms: in a str those are code points, and in mojibake they are text.
ESCAPES = re.compile(r"\x1b\][^\x07\x1b\x18\x1a\n]*(?:\x07|\x1b\\|[\x18\x1a]|(?=[\x1b\n])|$)"
                     r"|\x1b\[[0-?]*[ -/]*[@-~]"
                     r"|\x1b[ -/]*[0-~]")


def plain_text(text):
    """What a terminal would show of `text`, without its escapes: pasted coloured
    output is common in a prompt, and neither the pane nor a paste wants it."""
    # and the controls the pane drops (BEL, backspace, VT, FF): a paste drops them too
    return re.sub(r"[\x1b\x07\x08\x0b\x0c]", "", ESCAPES.sub("", text))
