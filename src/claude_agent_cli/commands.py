"""The slash commands available inside the chat.

This list is the single source for the `/` menu, `/help` and the chat loop's dispatcher, so they can't drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    arg: str = ""  # placeholder shown in help, e.g. "<title>"; empty when the command takes no argument
    arg_required: bool = False
    group: str = "Session"
    aliases: tuple[str, ...] = ()

    @property
    def usage(self) -> str:
        return f"/{self.name} {self.arg}".rstrip()


COMMANDS: list[Command] = [
    Command("help", "Show all commands and keyboard shortcuts", group="Basics", aliases=("?",)),
    Command("share", "Turn this session into a receipt page and open it", group="Receipts"),
    Command("record", "Start recording this session", "[title]", group="Receipts"),
    Command("name", "Give this session a title", "<title>", arg_required=True, group="Receipts"),
    Command("sessions", "List your recorded sessions", group="Receipts"),
    Command("resume", "Switch to a recorded session", "<session>", arg_required=True, group="Receipts"),
    Command("model", "Switch model (handy when you are rate limited)", "<model>", arg_required=True, group="Model"),
    Command("effort", "How hard Claude thinks: low, medium, high, xhigh, max", "<level>", True, group="Model"),
    Command("budget", "Set a spending limit for this session: /budget 0.50 (or off)", "<amount>", True, group="Model"),
    Command("cost", "Show tokens and cost so far", group="Model"),
    Command("reset", "Start a new conversation (Claude forgets this one)", group="Basics"),
    Command("clear", "Clear the screen", group="Basics"),
    Command("exit", "Quit (Ctrl+D also works)", group="Basics", aliases=("quit",)),
]

GROUPS = ["Basics", "Receipts", "Model"]
EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]
BUDGET_CHOICES = [("0.10", "ten cents"), ("0.50", ""), ("1", "one dollar"), ("5", ""), ("off", "no limit")]

SHORTCUTS: list[tuple[str, str]] = [
    ("/", "open the command menu"),
    ("@", "mention a file in your project"),
    ("Enter", "send · picks the highlighted menu item"),
    ("Alt+Enter", "new line (or paste several lines)"),
    ("↑ ↓", "previous messages · move in a menu"),
    ("Ctrl+R", "search your message history"),
    ("Ctrl+C", "stop Claude · clear what you typed"),
    ("Ctrl+L", "clear the screen"),
    ("Ctrl+D", "quit"),
]


def find_command(word: str) -> Command | None:
    """The command for '/name', 'name' or an alias like '?'."""
    word = word.removeprefix("/").lower()
    return next((c for c in COMMANDS if word == c.name or word in c.aliases), None)


def parse(text: str) -> tuple[Command | None, str] | None:
    """Split '/name My title' into (command, 'My title'). None when the text is a normal message.

    Unknown commands come back as (None, '/word') so the chat can say so instead of sending them to Claude.
    """
    stripped = text.strip()
    if stripped == "?":
        return find_command("help"), ""
    if not stripped.startswith("/") or stripped.startswith("//"):
        return None
    word, _, arg = stripped.partition(" ")
    if "/" in word[1:]:  # a path like /usr/bin/env is a message, not a command
        return None
    command = find_command(word)
    return (command, arg.strip()) if command else (None, word)
