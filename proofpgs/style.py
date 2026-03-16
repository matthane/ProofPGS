"""Terminal styling — muted truecolor palette, zero dependencies."""

import os
import sys


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

_use_color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _fg(r, g, b):
    """Return a 24-bit foreground color escape or empty string."""
    return f"\033[38;2;{r};{g};{b}m" if _use_color else ""


# --- Palette (muted / One Dark inspired) ---
_RESET = "\033[0m" if _use_color else ""
_BOLD  = "\033[1m" if _use_color else ""
_DIM_SGR = "\033[2m" if _use_color else ""

_ERROR   = _fg(224, 108, 117)   # #E06C75  soft coral red
_WARN    = _fg(209, 154, 102)   # #D19A66  muted amber
_SUCCESS = _fg(152, 195, 121)   # #98C379  sage green
_INFO    = _fg(86,  182, 194)   # #56B6C2  muted teal
_HEADING = _fg(97,  175, 239)   # #61AFEF  soft blue
_DIM     = _fg(92,  99,  112)   # #5C6370  slate gray
_HDR     = _fg(198, 120, 221)   # #C678DD  soft lavender
_SDR     = _fg(152, 195, 121)   # #98C379  sage green
_COMPARE = _fg(229, 192, 123)   # #E5C07B  soft gold


# --- Semantic helpers ---

def error(text):
    return f"{_ERROR}{text}{_RESET}"

def warn(text):
    return f"{_WARN}{text}{_RESET}"

def success(text):
    return f"{_SUCCESS}{text}{_RESET}"

def info(text):
    return f"{_INFO}{text}{_RESET}"

def heading(text):
    return f"{_BOLD}{_HEADING}{text}{_RESET}"

def dim(text):
    return f"{_DIM}{text}{_RESET}"

def bold(text):
    return f"{_BOLD}{text}{_RESET}"

def badge_hdr(text):
    return f"{_HDR}{text}{_RESET}"

def badge_sdr(text):
    return f"{_SDR}{text}{_RESET}"

def badge_compare(text):
    return f"{_COMPARE}{text}{_RESET}"

def badge_unknown(text):
    return f"{_DIM}{text}{_RESET}"


# --- Cursor control (always active on TTY, silent when piped) ---
_is_tty = sys.stdout.isatty()

CURSOR_UP_CLEAR = "\033[A\033[K" if _is_tty else ""
CLEAR_LINE      = "\033[K"       if _is_tty else ""
