"""Terminal styling — muted truecolor palette, zero dependencies."""

import os
import re
import sys
import unicodedata


def _enable_windows_vt():
    """Enable VT100 escape processing on Windows consoles."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


_enable_windows_vt()

_is_tty = sys.stdout.isatty()
_use_color = _is_tty and not os.environ.get("NO_COLOR")
_use_unicode = _is_tty and not os.environ.get("NO_COLOR")


def _fg(r, g, b):
    """Return a 24-bit foreground color escape or empty string."""
    return f"\033[38;2;{r};{g};{b}m" if _use_color else ""


# --- Palette (muted, btop-inspired) ---
_RESET = "\033[0m" if _use_color else ""
# Combine bold with an explicit 24-bit white foreground in a single SGR
# sequence.  Bare \033[1m or \033[1;97m often renders only as "bright"
# without switching to the bold font face on Windows Terminal; a 24-bit
# FG paired with the bold parameter triggers the bold-font path reliably.
_BOLD  = "\033[38;2;130;160;210;1m" if _use_color else ""
_DIM_SGR = "\033[2m" if _use_color else ""

_ERROR   = _fg(215,  95,  95)   # soft red
_WARN    = _fg(220, 180,  90)   # soft amber
_SUCCESS = _fg(115, 190, 120)   # soft green
_INFO    = _fg(130, 160, 210)   # soft blue
_DIM     = _fg(110, 110, 120)   # dim text
# Dim slate paired with bold weight — used for muted brand labels where the
# glyphs should still carry the bold font face.
_DIM_BOLD = "\033[38;2;110;110;120;1m" if _use_color else ""
_BORDER  = _fg(80,   85,  95)   # dark gray borders
_HDR     = _fg(220, 140, 160)   # soft rose
_SDR     = _fg(120, 190, 200)   # soft cyan
_COMPARE = _fg(200, 180, 140)   # soft yellow


# --- Semantic helpers ---

def error(text):
    return f"{_ERROR}{text}{_RESET}"

def warn(text):
    return f"{_WARN}{text}{_RESET}"

def success(text):
    return f"{_SUCCESS}{text}{_RESET}"

def info(text):
    return f"{_INFO}{text}{_RESET}"

def dim(text):
    return f"{_DIM}{text}{_RESET}"

def border(text):
    return f"{_BORDER}{text}{_RESET}"

def bold(text):
    return f"{_BOLD}{text}{_RESET}"

def dim_bold(text):
    return f"{_DIM_BOLD}{text}{_RESET}"

_WHITE = "\033[38;2;255;255;255m" if _use_color else ""

def badge_hdr(text):
    return f"{_WHITE}{text}{_RESET}"

def badge_sdr(text):
    return f"{_WHITE}{text}{_RESET}"

def badge_compare(text):
    return f"{_COMPARE}{text}{_RESET}"

def badge_unknown(text):
    return f"{_DIM}{text}{_RESET}"

def badge_mismatch(text):
    return f"{_WARN}{text}{_RESET}"


# --- Cursor control (always active on TTY, silent when piped) ---
CURSOR_UP_CLEAR = "\033[A\033[K" if _is_tty else ""
CLEAR_LINE      = "\033[K"       if _is_tty else ""


# ---------------------------------------------------------------------------
# Box drawing and status glyphs
# ---------------------------------------------------------------------------

# Pretty (UTF-8) and ASCII fallback glyph tables.  Selected by _use_unicode,
# which is gated the same way as color: TTY + no NO_COLOR.
_GLYPH_UNICODE = {
    "tl": "╭", "tr": "╮", "bl": "╰", "br": "╯",
    "h":  "─", "v":  "│",
    "ok": "✓", "err": "✗",
    "dot": "•",
    "rule": "·",
    "hdr": "◆",   # solid diamond — high dynamic range
    "sdr": "◇",   # hollow diamond — standard dynamic range
    "warn": "⚠",  # warning sign — mismatch / caution
}
_GLYPH_ASCII = {
    "tl": "+", "tr": "+", "bl": "+", "br": "+",
    "h":  "-", "v":  "|",
    "ok": "[ok]", "err": "[x]",
    "dot": "*",
    "rule": ".",
    "hdr": "*",
    "sdr": "-",
    "warn": "!",
}
_G = _GLYPH_UNICODE if _use_unicode else _GLYPH_ASCII


def glyph(name: str) -> str:
    """Return a glyph by name, respecting the Unicode/ASCII fallback.

    Known names: ``dot`` (bullet separator), ``ok``, ``err``, and the
    box-drawing primitives ``tl tr bl br h v``.
    """
    return _G[name]


# Box width used for all framed sections.  Sized so track rows can carry
# language + title + sub count + two badges without wrapping.
BOX_WIDTH = 72


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _visible_len(s: str) -> int:
    """Return the column width of *s* on a terminal.

    Strips ANSI SGR escapes, then sums East Asian width: wide/full-width
    characters count as 2 columns, everything else as 1.  Handles CJK
    titles without pulling in the ``wcwidth`` dependency.
    """
    plain = _ANSI_RE.sub("", s)
    width = 0
    for ch in plain:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            width += 2
        else:
            width += 1
    return width


def box_top(title: str = "", width: int = BOX_WIDTH) -> str:
    """Return the top border of a box, with an optional centered title.

    The title is rendered via ``bold()``; the border
    uses the dim slate color so the frame stays quiet.
    """
    inner = width - 2  # chars between corners
    if title:
        label = f" {title} "
        label_len = len(label)
        if label_len >= inner:
            return (
                border(_G['tl'])
                + bold(label[:inner])
                + border(_G['tr'])
            )
        left = (inner - label_len) // 2
        right = inner - label_len - left
        return (
            border(_G['tl'] + _G['h'] * left)
            + bold(label)
            + border(_G['h'] * right + _G['tr'])
        )
    return border(_G['tl'] + _G['h'] * inner + _G['tr'])


def box_bottom(width: int = BOX_WIDTH) -> str:
    """Return the bottom border of a box."""
    return border(_G['bl'] + _G['h'] * (width - 2) + _G['br'])


def box_row(content: str = "", width: int = BOX_WIDTH) -> str:
    """Return a single box row: ``│ content<pad> │``.

    Padding is computed from the *visible* width of *content* (ANSI
    escapes and zero-width/wide characters handled), so rows stay aligned
    even when they contain colored badges or CJK track titles.  Content
    that overflows the interior is truncated at the visible-length level.
    """
    inner = width - 4  # subtract "│ " + " │"
    vlen = _visible_len(content)
    if vlen > inner:
        # Truncate to inner width.  Walk characters, counting visible
        # columns; preserve ANSI escapes and append a reset at the end.
        plain_pos = 0
        out = []
        i = 0
        s = content
        # Flush any trailing escapes, but stop emitting printable chars
        # once we hit the limit.
        while i < len(s) and plain_pos < inner:
            m = _ANSI_RE.match(s, i)
            if m:
                out.append(m.group(0))
                i = m.end()
                continue
            ch = s[i]
            w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            if plain_pos + w > inner:
                break
            out.append(ch)
            plain_pos += w
            i += 1
        content = "".join(out) + _RESET
        vlen = plain_pos
    pad = " " * (inner - vlen)
    bar = border(_G['v'])
    return f"{bar} {content}{pad} {bar}"


def box_blank(width: int = BOX_WIDTH) -> str:
    """Return an empty interior row — used for breathing room inside a box."""
    return box_row("", width)


def box_sep(width: int = BOX_WIDTH) -> str:
    """Return a dotted divider row: ``│ ········ │``.

    Used between groups of content inside a box (e.g. between tracks in
    the listing) when a blank row is too quiet to read as a separator.
    """
    inner = width - 4  # subtract "│ " + " │"
    bar = border(_G['v'])
    rule = border(_G['rule'] * inner)
    return f"{bar} {rule} {bar}"


# --- Status helpers (reserved for terminal outcomes only) ---

def status_ok(text: str) -> str:
    """Final success line: ``  ✓ text`` in green.

    Reserved for terminal success messages — the thing the user reads
    right before the program exits cleanly.
    """
    return f"  {success(_G['ok'])} {success(text)}"


def status_err(text: str) -> str:
    """Fatal error line: ``  ✗ text`` in red.

    Reserved for fatal errors where the program is about to abort.
    Non-fatal errors should use ``error()`` alone (color only, no glyph).
    """
    return f"  {error(_G['err'])} {error(text)}"
