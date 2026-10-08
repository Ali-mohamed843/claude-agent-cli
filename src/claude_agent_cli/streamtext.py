"""Format Claude's text for the terminal while it streams in.

Text arrives in small pieces, so this works incrementally: it wraps words at the terminal width (keeping the
indent under the "●"), colors `inline code`, makes **bold** bold, and shows ``` code blocks dimmed and unwrapped.
The Markdown markers themselves are not printed.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from prompt_toolkit.utils import get_cwidth

from . import style

TOKEN = re.compile(r"(```|`|\*\*|\n|[ \t]+|[^\s`*]+|\*)")


class StreamText:
    def __init__(
        self, write: Callable[[str], object], width: int, indent: str = "  ", base: tuple[str, ...] = ()
    ) -> None:
        self.write = write
        self.width = max(width, 20)
        self.indent = indent
        self.base = base  # styles for all text, e.g. ("gray", "italic") for thinking
        self.col = len(indent)
        self.pending = ""  # the end of the last chunk, held back in case it is half a word or marker
        self.code = False  # inside `inline code`
        self.bold = False
        self.fence = False  # inside a ``` block
        self.fence_header = False  # on the ``` line itself, before the code starts
        self.at_line_start = True
        self._apply_style()

    def feed(self, text: str) -> None:
        text = self.pending + text
        self.pending = ""
        # Hold back a trailing partial token: a word may continue, and "`" may become "```".
        match = re.search(r"([^\s`*]+|`{1,2}|\*)$", text)
        if match:
            self.pending = match.group(0)
            text = text[: match.start()]
        for token in TOKEN.findall(text):
            self._token(token)

    def finish(self) -> None:
        if self.pending:
            for token in TOKEN.findall(self.pending):
                self._token(token)
            self.pending = ""
        self.write(style.end())

    # ------------------------------------------------------------------------------------------ internals

    def _token(self, token: str) -> None:
        if token == "```":
            self.fence = not self.fence
            self.fence_header = self.fence  # skip the language name ("```bash") on the opening line
            self.code = False
            self._apply_style()
            return
        if self.fence_header:
            if token != "\n":
                return
            self.fence_header = False
        if token == "\n":
            self.write(style.end() + "\n" + self.indent + ("  " if self.fence else ""))
            self.col = len(self.indent)
            self.at_line_start = True
            self._apply_style()
            return
        if self.fence:  # code blocks: no wrapping, no Markdown
            self.write(token)
            return
        if token == "`":
            self.code = not self.code
            self._apply_style()
            return
        if token == "**":
            self.bold = not self.bold
            self._apply_style()
            return
        if token.isspace():
            if self.at_line_start or self.col + len(token) < self.width:  # leading spaces indent nested lists
                self.write(token)
                self.col += len(token)
            return
        width = get_cwidth(token)
        if self.col + width > self.width and self.col > len(self.indent):
            self.write(style.end() + "\n" + self.indent)
            self.col = len(self.indent)
            self._apply_style()
        self.write(token)
        self.col += width
        self.at_line_start = False

    def _apply_style(self) -> None:
        styles = list(self.base)
        if self.fence:
            styles = ["gray"]
        elif self.code:
            styles.append("blue")
        if self.bold and not self.fence:
            styles.append("bold")
        self.write(style.end() + (style.start(*styles) if styles else ""))
