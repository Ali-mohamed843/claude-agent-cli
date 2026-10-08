"""Terminal colors, symbols and small formatting helpers shared by the agent output and the chat UI.

Colors switch off when stdout is not a terminal (piped output, tests) or when NO_COLOR is set.
"""

from __future__ import annotations

import os
import shutil
import sys

COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ

# 256-color codes work in Windows Terminal, the VS Code terminal, macOS Terminal, iTerm and most Linux terminals.
_CODES = {
    "reset": "0",
    "bold": "1",
    "dim": "2",
    "italic": "3",
    "accent": "38;5;173",  # Claude's clay orange
    "green": "38;5;71",
    "red": "38;5;167",
    "yellow": "38;5;179",
    "blue": "38;5;110",
    "gray": "38;5;245",
}

ACCENT_HEX = "#d7875f"  # the same orange, for prompt_toolkit styles
MARK = "✻"  # the brand mark
BULLET = "●"
BRANCH = "└"


def paint(text: str, *styles: str) -> str:
    if not COLOR or not styles:
        return text
    codes = ";".join(_CODES[s] for s in styles)
    return f"\033[{codes}m{text}\033[0m"


def start(*styles: str) -> str:
    """Open a style without closing it (for streamed text); close it with `end()`."""
    return f"\033[{';'.join(_CODES[s] for s in styles)}m" if COLOR else ""


def end() -> str:
    return "\033[0m" if COLOR else ""


def term_width(default: int = 80) -> int:
    return shutil.get_terminal_size((default, 24)).columns


def fmt_tokens(n: int) -> str:
    if n < 1000:
        return f"{n}"
    if n < 1_000_000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return f"{n / 1_000_000:.1f}M"


def fmt_seconds(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds * 1000:.0f} ms"
    if seconds < 60:
        return f"{seconds:.1f} s"
    return f"{int(seconds // 60)} min {int(seconds % 60)} s"


def fmt_cost(cost: float | None) -> str:
    if cost is None:
        return "unknown price"
    if cost == 0:
        return "free"
    return f"${cost:.4f}" if cost < 1 else f"${cost:.2f}"


def middle_truncate(text: str, width: int) -> str:
    """Shorten long paths from the middle, where the least useful part usually is."""
    if len(text) <= width:
        return text
    keep = max(width - 1, 2)
    return text[: keep // 2] + "…" + text[len(text) - (keep - keep // 2) :]
