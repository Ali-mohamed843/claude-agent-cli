"""The interactive chat interface: prompt, `/` command menu, `@` file picker, status bar, welcome card and help.

Built on prompt_toolkit. When stdin or stdout is not a terminal (piped input, tests), `ChatUI` falls back to
plain `input()` so the agent still works in scripts.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.application import get_app
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import has_completions
from prompt_toolkit.formatted_text import FormattedText, StyleAndTextTuples
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.shortcuts import clear as clear_screen
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth

from .commands import BUDGET_CHOICES, COMMANDS, EFFORT_LEVELS, GROUPS, SHORTCUTS, find_command
from .style import ACCENT_HEX, BRANCH, BULLET, MARK, middle_truncate, paint, term_width
from .tools import SKIPPED_DIRS

MAX_FILES = 20_000  # stop indexing huge trees; the picker is for finding files, not listing them all
MAX_FILE_MATCHES = 40

STYLE = Style.from_dict(
    {
        "prompt": f"{ACCENT_HEX} bold",
        "continuation": "#6c6c6c",
        "auto-suggestion": "#6c6c6c",
        "bottom-toolbar": "noreverse #8a8a8a",
        "bottom-toolbar.key": "#d0d0d0 bold",
        "bottom-toolbar.rec": "#e06c6c bold",
        "bottom-toolbar.accent": f"{ACCENT_HEX}",
        "completion-menu": "bg:#262626 #d0d0d0",
        "completion-menu.completion": "bg:#262626 #d0d0d0",
        "completion-menu.completion.current": f"bg:{ACCENT_HEX} #1c1c1c bold",
        "completion-menu.meta.completion": "bg:#262626 #8a8a8a",
        "completion-menu.meta.completion.current": "bg:#5f3b2c #f0e0d8",
        "completion-menu.multi-column-meta": "bg:#262626 #8a8a8a",
        "arg": "#8a8a8a italic",
        "scrollbar.background": "bg:#303030",
        "scrollbar.button": "bg:#808080",
        "confirm": "#d0d0d0",
        "confirm.key": f"{ACCENT_HEX} bold",
    }
)


@dataclass
class Status:
    """What the status bar shows. The chat updates it; the bar reads it on every redraw."""

    model: str = ""
    recording: str | None = None  # the session's title while recording
    tokens: str = ""
    cost: str = ""


KEY_HINTS = [("/", "commands"), ("@", "files"), ("?", "help"), ("Alt+Enter", "new line")]


def toolbar(status: Status, width: int) -> FormattedText:
    """Key hints on the left, session status on the right. On narrow terminals it drops the least useful parts
    first: the token count, then key hints, then the session title, so "/ commands" and the model always stay.
    """

    def keys(count: int) -> StyleAndTextTuples:
        out: StyleAndTextTuples = []
        for key, label in KEY_HINTS[:count]:
            out += [("class:bottom-toolbar.key", f" {key}"), ("", f" {label} ")]
        return out

    def right(tokens: bool, title: bool) -> StyleAndTextTuples:
        out: StyleAndTextTuples = []
        if status.recording is not None:
            out.append(("class:bottom-toolbar.rec", "● rec "))
            if title:
                out.append(("", f"{middle_truncate(status.recording, 24)} · "))
        out.append(("class:bottom-toolbar.accent", middle_truncate(status.model, 34)))
        if tokens and status.tokens:
            out.append(("", f" · {status.tokens}"))
        if status.cost:
            out.append(("", f" · {status.cost}"))
        out.append(("", " "))
        return out

    layouts = [(4, True, True), (3, True, True), (3, False, True), (1, True, True), (1, False, True), (1, False, False)]
    for count, tokens, title in layouts:
        left, status_part = keys(count), right(tokens, title)
        used = sum(get_cwidth(fragment[1]) for fragment in left + status_part)
        if used < width:
            break
    return FormattedText([*left, ("", " " * max(1, width - used)), *status_part])


# ---------------------------------------------------------------------------------------------- completion


class ChatCompleter(Completer):
    """`/` opens the command menu, `/command ` suggests values, `@` picks a project file."""

    def __init__(
        self,
        root: Path,
        sessions: Callable[[], list[tuple[str, str]]],
        models: Callable[[], list[tuple[str, str]]],
    ) -> None:
        self.root = root
        self.sessions = sessions
        self.models = models
        self._files: list[str] | None = None

    def get_completions(self, document: Document, complete_event: CompleteEvent) -> Iterable[Completion]:
        text = document.text_before_cursor
        if text.startswith("/") and "\n" not in text:
            if " " not in text:
                yield from self._commands(text)
            else:
                yield from self._arguments(text)
            return
        word = document.get_word_before_cursor(WORD=True)
        if word.startswith("@"):
            yield from self._mentions(word)

    def _commands(self, text: str) -> Iterable[Completion]:
        typed = text[1:].lower()
        starts = [c for c in COMMANDS if c.name.startswith(typed)]
        contains = [c for c in COMMANDS if typed in c.name and c not in starts]
        for command in starts + contains:
            display: StyleAndTextTuples = [("", f"/{command.name}")]
            if command.arg:
                display.append(("class:arg", f" {command.arg}"))
            yield Completion(
                f"/{command.name}",
                start_position=-len(text),
                display=display,
                display_meta=command.description,
            )

    def _arguments(self, text: str) -> Iterable[Completion]:
        word, _, typed = text.partition(" ")
        command = find_command(word)
        if command is None:
            return
        options: list[tuple[str, str]] = []
        if command.name == "resume":
            options = self.sessions()
        elif command.name == "model":
            options = self.models()
        elif command.name == "effort":
            options = [(level, "") for level in EFFORT_LEVELS]
        elif command.name == "budget":
            options = BUDGET_CHOICES
        needle = typed.lower()
        for value, meta in options:
            if needle in value.lower():
                yield Completion(value, start_position=-len(typed), display_meta=meta)

    def _mentions(self, word: str) -> Iterable[Completion]:
        query = word[1:].lower().replace("\\", "/")
        files = self.files()
        name_starts = [f for f in files if f.rsplit("/", 1)[-1].lower().startswith(query)]
        path_starts = [f for f in files if f.lower().startswith(query) and f not in name_starts]
        contains = [f for f in files if query in f.lower() and f not in name_starts and f not in path_starts]
        for path in (name_starts + path_starts + contains)[:MAX_FILE_MATCHES]:
            folder, _, name = path.rpartition("/")
            yield Completion(f"@{path}", start_position=-len(word), display=name, display_meta=folder or ".")

    def files(self) -> list[str]:
        """Project files, relative and with forward slashes. Indexed once, on the first `@`."""
        if self._files is None:
            found: list[str] = []
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = sorted(d for d in dirnames if d not in SKIPPED_DIRS and not d.startswith("."))
                base = Path(dirpath).relative_to(self.root).as_posix()
                found.extend(name if base == "." else f"{base}/{name}" for name in sorted(filenames))
                if len(found) >= MAX_FILES:
                    break
            self._files = found
        return self._files


def chat_key_bindings() -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("enter", filter=has_completions)
    def _pick(event: KeyPressEvent) -> None:
        """Enter in a menu picks the item; it only sends when the message is complete."""
        buffer = event.current_buffer
        state = buffer.complete_state
        if state is None:
            buffer.validate_and_handle()
            return
        completion = state.current_completion or (state.completions[0] if state.completions else None)
        if completion is not None:
            buffer.apply_completion(completion)
        text = buffer.text
        if completion is not None and completion.text.startswith("@"):
            buffer.insert_text(" ")  # a file was picked: keep writing the message
            return
        command = find_command(text.split(" ", 1)[0]) if text.startswith("/") else None
        if command and command.arg_required and text.strip() == f"/{command.name}":
            buffer.insert_text(" ")  # the command needs a value: show its suggestions
            buffer.start_completion()
            return
        buffer.validate_and_handle()

    @bindings.add("escape", "enter")
    def _newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    return bindings


# ---------------------------------------------------------------------------------------------- the prompt


class ChatUI:
    def __init__(
        self,
        root: Path,
        status: Status,
        sessions: Callable[[], list[tuple[str, str]]],
        models: Callable[[], list[tuple[str, str]]],
        history_file: Path | None = None,
    ) -> None:
        self.status = status
        self.interactive = sys.stdin.isatty() and sys.stdout.isatty()
        self.completer = ChatCompleter(root, sessions, models)
        self._session: PromptSession[str] | None = None
        if self.interactive:
            try:
                self._session = self._make_session(history_file)
            except Exception:  # e.g. Git Bash's mintty on Windows, which prompt_toolkit can't drive
                self.interactive = False

    def _make_session(self, history_file: Path | None) -> PromptSession[str]:
        return PromptSession(
            message=[("class:prompt", "❯ ")],
            prompt_continuation=lambda width, line, wrap: [("class:continuation", "· ".rjust(width))],
            completer=self.completer,
            complete_while_typing=True,
            auto_suggest=AutoSuggestFromHistory(),
            history=_history(history_file),
            key_bindings=chat_key_bindings(),
            bottom_toolbar=self._toolbar,
            style=STYLE,
            reserve_space_for_menu=len(COMMANDS) + 1,  # room for every command at once
            enable_suspend=False,
        )

    def read(self) -> str:
        """The user's next message. Raises EOFError on Ctrl+D and KeyboardInterrupt on Ctrl+C."""
        if self._session is None:
            return input("\n> ")
        print()
        return self._session.prompt()

    def confirm(self, question: str) -> bool:
        """A one-key yes/no question. Enter or Esc means no, so nothing runs by accident."""
        if not self.interactive:
            return input(f"  {BRANCH} {question} [y/N] ").strip().lower() in {"y", "yes"}
        bindings = KeyBindings()

        def answer(value: bool) -> Callable[[KeyPressEvent], None]:
            def handler(event: KeyPressEvent) -> None:
                event.current_buffer.text = "yes" if value else "no"
                event.app.exit(result=value)

            return handler

        for key in ("y", "Y"):
            bindings.add(key)(answer(True))
        for key in ("n", "N", "enter", "escape"):
            bindings.add(key, eager=True)(answer(False))

        @bindings.add("c-c")
        def _interrupt(event: KeyPressEvent) -> None:
            event.app.exit(exception=KeyboardInterrupt())

        @bindings.add("<any>")
        def _ignore(event: KeyPressEvent) -> None:
            pass

        message: StyleAndTextTuples = [
            ("class:continuation", f"  {BRANCH} "),
            ("class:confirm", f"{question} "),
            ("class:confirm.key", "y"),
            ("class:confirm", "es / "),
            ("class:confirm.key", "n"),
            ("class:confirm", "o  "),
        ]
        session: PromptSession[bool] = PromptSession(message=message, key_bindings=bindings, style=STYLE)
        return bool(session.prompt())

    def _toolbar(self) -> FormattedText:
        return toolbar(self.status, get_app().output.get_size().columns)

    def clear(self) -> None:
        if self.interactive:
            clear_screen()


def _history(path: Path | None) -> History:
    if path is None:
        return InMemoryHistory()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return FileHistory(str(path))
    except OSError:
        return InMemoryHistory()


# ---------------------------------------------------------------------------------------------- screens


def welcome_card(rows: list[tuple[str, str]], width: int | None = None) -> str:
    """The boxed card shown at start-up. `rows` are (label, value) pairs; values may contain ANSI colors."""
    width = min((width or term_width()) - 2, 78)
    inner = width - 6  # two spaces of padding on each side inside the border
    title = f" {MARK} claude-agent "
    top = paint("╭─", "accent") + paint(title, "accent", "bold") + paint("─" * (width - 3 - len(title)) + "╮", "accent")
    lines = [top, _boxed("", inner)]
    label_width = max(len(label) for label, _ in rows) + 2
    for label, value in rows:
        lines.append(_boxed(paint(label.ljust(label_width), "gray") + value, inner, label_width + _visible(value)))
    lines.append(_boxed("", inner))
    lines.append(paint("╰" + "─" * (width - 2) + "╯", "accent"))
    return "\n".join(lines)


def _visible(text: str) -> int:
    return get_cwidth(re.sub(r"\033\[[0-9;]*m", "", text))


def _boxed(content: str, inner: int, visible: int | None = None) -> str:
    visible = _visible(content) if visible is None else visible
    side = paint("│", "accent")
    return f"{side}  {content}{' ' * max(0, inner - visible)}  {side}"


def hint_line() -> str:
    parts = [("/", "for commands"), ("@", "to mention a file"), ("?", "for help")]
    joined = paint(" · ", "gray").join(f"{paint(key, 'accent', 'bold')} {paint(text, 'gray')}" for key, text in parts)
    return f"  {joined}"


def help_screen() -> str:
    lines = ["", paint(f"{MARK} Commands", "accent", "bold")]
    usage_width = max(len(c.usage) for c in COMMANDS) + 3
    for group in GROUPS:
        lines.append("")
        lines.append(paint(f"  {group.upper()}", "gray", "bold"))
        for command in (c for c in COMMANDS if c.group == group):
            name = paint(f"/{command.name}", "accent")
            arg = paint(f" {command.arg}", "gray", "italic") if command.arg else ""
            pad = " " * (usage_width - len(command.usage))
            lines.append(f"    {name}{arg}{pad}{command.description}")
    lines += ["", paint("  KEYS", "gray", "bold")]
    key_width = usage_width  # one column for commands and keys, so the descriptions line up
    lines += [f"    {paint(key.ljust(key_width), 'bold')}{text}" for key, text in SHORTCUTS]
    lines += ["", paint("  Tip: type / and the menu shows everything, so there is nothing to memorise.", "gray")]
    return "\n".join(lines)


def notice(text: str, kind: str = "info") -> str:
    """A one-line message from the app (not from Claude)."""
    symbol, color = {
        "info": (BRANCH, "gray"),
        "ok": ("✓", "green"),
        "warn": ("!", "yellow"),
        "error": ("✗", "red"),
    }[kind]
    return f"  {paint(symbol, color)} {text}"


def bullet(text: str) -> str:
    return f"{paint(BULLET, 'accent')} {text}"
