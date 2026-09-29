"""Terminal colour, with the escape sequences turned off when they would be noise.

Colour is disabled when output is piped, when NO_COLOR is set, or when the terminal
cannot handle VT sequences, so redirected output stays clean.
"""
import os
import sys

_ENABLED: bool | None = None


def use_utf8():
    """Windows consoles default to cp1252, which cannot encode box-drawing characters.

    Reconfiguring the stream is enough on Python 3.7+; errors="replace" means a console
    that still cannot cope degrades to '?' instead of raising mid-render.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


use_utf8()


def _enable_windows_vt() -> bool:
    """Turn on virtual-terminal processing so cmd.exe renders escapes instead of printing them."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def enabled() -> bool:
    global _ENABLED
    if _ENABLED is None:
        _ENABLED = bool(
            not os.environ.get("NO_COLOR")
            and sys.stdout.isatty()
            and _enable_windows_vt()
        )
    return _ENABLED


CODES = {
    "reset": "0", "bold": "1", "dim": "2", "italic": "3", "underline": "4",
    "red": "31", "green": "32", "yellow": "33", "blue": "34",
    "magenta": "35", "cyan": "36", "white": "37", "grey": "90",
    "bright_red": "91", "bright_green": "92", "bright_yellow": "93",
    "bright_blue": "94", "bright_magenta": "95", "bright_cyan": "96",
    "on_blue": "44", "on_grey": "100",
}


def c(text: str, *styles: str) -> str:
    """Wrap text in the named styles, or return it untouched when colour is off."""
    if not enabled() or not styles:
        return text
    codes = ";".join(CODES[s] for s in styles if s in CODES)
    return f"\033[{codes}m{text}\033[0m" if codes else text


def pad(text: str, width: int, align: str = "<") -> str:
    """Pad to a visible width, ignoring escape sequences when measuring."""
    visible = len(strip(text))
    fill = max(0, width - visible)
    if align == ">":
        return " " * fill + text
    if align == "^":
        left = fill // 2
        return " " * left + text + " " * (fill - left)
    return text + " " * fill


def strip(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        if text[i] == "\033":
            end = text.find("m", i)
            i = len(text) if end == -1 else end + 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def bar(fraction: float, width: int = 12, style: str = "green") -> str:
    """A small block-gauge, e.g. for a 0-100 score."""
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    return c("█" * filled, style) + c("░" * (width - filled), "grey")


def heat(score: float) -> str:
    """Colour a 0-100 score: strong green, mid yellow, weak grey."""
    if score >= 85:
        return "bright_green"
    if score >= 70:
        return "green"
    if score >= 50:
        return "yellow"
    return "grey"


def money_move(pct: float | None) -> str:
    if pct is None:
        return "grey"
    return "bright_green" if pct > 0 else "red" if pct < 0 else "grey"
