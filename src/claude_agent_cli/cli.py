"""Command-line entry point: the interactive chat in the terminal."""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from . import ui
from .agent import DEFAULT_MODEL, Agent, TurnFailed
from .commands import EFFORT_LEVELS, Command, parse
from .cost import PRICES
from .proof import Signer
from .recorder import (
    FORMAT_VERSION,
    RECEIPTS_DIR,
    Recorder,
    default_record_path,
    display_name,
    find_recording,
    load_events,
    recordings,
)
from .resume import Restored, restore
from .style import BULLET, fmt_cost, fmt_seconds, fmt_tokens, middle_truncate, paint
from .tools import ToolBox

AUTO_RECORD_PATH = Path("<auto>")  # `--record` with no path: pick a name under the project root
HISTORY_FILE = Path.home() / ".claude-agent" / "history"

# Free OpenRouter models that support tool use (checked October 2026; the list changes, any model ID works).
FREE_MODELS = [
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "google/gemma-4-31b-it:free",
    "cohere/north-mini-code:free",
    "thinkingmachines/inkling:free",
    "poolside/laguna-s-2.1:free",
    "dots-studio/dots-3-note-preview:free",
]


def error_type(error: anthropic.APIStatusError) -> str | None:
    """The `error.type` field of an API error body, e.g. "overloaded_error".

    Errors that arrive mid-stream come with HTTP status 200, so the status code alone can't identify them.
    """
    body = error.body
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        value = body["error"].get("type")
        return value if isinstance(value, str) else None
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="claude-agent",
        description="A small coding agent for your terminal. Inside the chat, type / to see every command.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("AGENT_MODEL", DEFAULT_MODEL),
        help=f"Model ID (default: AGENT_MODEL from .env, else {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--effort",
        default="high",
        choices=EFFORT_LEVELS,
        help="How hard Claude thinks before answering (default: high)",
    )
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Project folder the tools can access")
    parser.add_argument(
        "--record",
        nargs="?",
        const=AUTO_RECORD_PATH,
        type=Path,
        metavar="PATH",
        help=f"Record the session to a JSONL file (default: <root>/{RECEIPTS_DIR}/session-<time>-<name>.jsonl)",
    )
    parser.add_argument("--name", help="Name of the recorded session, shown as the receipt's title")
    parser.add_argument(
        "--budget",
        type=parse_budget,
        metavar="USD",
        help="Spending limit for the session, e.g. 0.50; Claude is told what is left and stops to ask at the limit",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="",
        metavar="NAME_OR_PATH",
        help="Continue a recorded session: by name, by file, or the newest one when left empty",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page; make sure Arabic, emoji etc. print.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    # override=True: the project's .env wins over variables already in the shell. Some tools
    # (e.g. Claude Code) export ANTHROPIC_BASE_URL, which would silently send requests to the wrong host.
    load_dotenv(override=True)
    args = parse_args(argv)

    if args.resume is not None and args.record is not None:
        print("--resume keeps writing to the session's own file; leave out --record.", file=sys.stderr)
        return 1

    client = anthropic.Anthropic()
    if not (client.api_key or client.auth_token or client.credentials):
        print(
            "No API key found. Copy .env.example to .env and set ANTHROPIC_API_KEY "
            "(or the OpenRouter settings described there).",
            file=sys.stderr,
        )
        return 1

    chat = Chat(client, ToolBox(root=args.root, confirm=lambda _command: False), args.model, args.effort)
    restored = None
    if args.resume is not None:
        path = find_recording(chat.root, args.resume)
        if path is None:
            wanted = f" matching “{args.resume}”" if args.resume else ""
            print(
                f"No recorded session{wanted} in {chat.root / RECEIPTS_DIR}. `receipts list` shows them.",
                file=sys.stderr,
            )
            return 1
        restored = chat.resume(path, args.name)
    elif args.record is not None or args.name:
        chat.start_recording(args.name, None if args.record in (None, AUTO_RECORD_PATH) else args.record)

    if args.budget is not None:
        chat.agent.set_budget(args.budget)
        chat.update_cost()
    print(chat.welcome())
    if restored:
        print(chat.resumed_summary(restored))
    return chat.run()


class Chat:
    """One interactive session: the agent, its recording, and the commands that act on them."""

    def __init__(self, client: anthropic.Anthropic, toolbox: ToolBox, model: str, effort: str) -> None:
        self.toolbox = toolbox
        self.root = toolbox.root
        self.status = ui.Status(model=model)
        self.ui = ui.ChatUI(self.root, self.status, self.session_choices, self.model_choices, HISTORY_FILE)
        toolbox.confirm = self.confirm_command
        self.agent = Agent(client, toolbox, model=model, effort=effort)
        self.agent.on_budget = self.confirm_over_budget
        self.title: str | None = None
        self.signer = load_signer()

    # ------------------------------------------------------------------------------------------ recording

    @property
    def recorder(self) -> Recorder | None:
        return self.agent.recorder

    def start_recording(self, title: str | None, path: Path | None = None) -> Recorder:
        recorder = Recorder(path or default_record_path(self.root, title), signer=self.signer)
        recorder.event(
            "session_start",
            format_version=FORMAT_VERSION,
            agent="claude-agent-cli",
            name=title,
            model=self.agent.model,
            effort=self.agent.effort,
            # No absolute root path: it would always be redacted, leaving this line unverifiable in receipts.
            project=self.root.name,
        )
        self.agent.recorder = recorder
        self.title = title or "Untitled session"
        self.status.recording = self.title
        return recorder

    def stop_recording(self) -> None:
        recorder = self.recorder
        if recorder is None:
            return
        usage = self.agent.session_usage
        recorder.event("session_end", usage=asdict(usage), cost_usd=self.agent.session_cost)
        recorder.close()
        self.agent.recorder = None
        self.status.recording = None

    def resume(self, path: Path, title: str | None = None) -> Restored:
        self.stop_recording()
        restored = restore(load_events(path))
        recorder = Recorder(path, elapsed_offset_ms=restored.elapsed_ms, signer=self.signer)
        recorder.event(
            "session_resume", model=self.agent.model, effort=self.agent.effort, turns_restored=restored.turns
        )
        if title:
            recorder.event("rename", name=title)
        self.agent.recorder = recorder
        self.agent.messages = restored.messages
        self.agent.session_usage = restored.usage
        self.agent.session_cost = restored.cost
        self.agent.budget = restored.budget  # the budget carries over; --budget or /budget changes it
        self.title = title or restored.name
        self.status.recording = self.title
        self.update_cost()
        return restored

    def confirm_command(self, command: str) -> bool:
        approved = self.ui.confirm("Run this command?")
        if self.recorder:
            self.recorder.event("approval", command=command, approved=approved)
        return approved

    # ------------------------------------------------------------------------------------------ screens

    def welcome(self) -> str:
        model = paint(self.agent.model, "accent")
        if self.agent.is_claude:
            model += paint(f" · effort {self.agent.effort}", "gray")
        elif self.agent.model.endswith(":free"):
            model += paint(" · free", "gray")
        if self.recorder:
            session = paint("● recording", "red") + f" “{self.title}”"
            if self.signer:
                session += paint(" · sealed", "green")
        else:
            session = paint("○ not recorded", "gray") + paint(" · /record to start", "gray")
        rows = [("Model", model), ("Folder", middle_truncate(str(self.root), 52)), ("Session", session)]
        if self.agent.budget is not None:
            spent = fmt_cost(self.agent.session_cost)
            rows.append(("Budget", f"${self.agent.budget:.2f}" + paint(f" · {spent} spent", "gray")))
        return "\n" + ui.welcome_card(rows) + "\n" + ui.hint_line()

    def resumed_summary(self, restored: Restored) -> str:
        turns = f"{restored.turns} turn{'s' if restored.turns != 1 else ''}"
        lines = ["", ui.notice(f"Resumed “{self.title}” · {turns} restored", "ok")]
        if restored.last_question:
            lines.append(ui.notice(paint(f"You:    {shorten(restored.last_question, 90)}", "gray")))
            lines.append(ui.notice(paint(f"Claude: {shorten(restored.last_answer or '', 90)}", "gray")))
        if restored.skipped_turns:
            lines.append(ui.notice(f"{restored.skipped_turns} unanswered turn(s) left out", "warn"))
        if restored.unanswered:
            lines.append(
                ui.notice(f"Never answered: “{shorten(restored.unanswered, 80)}”. Send it again to retry.", "warn")
            )
        if restored.model and restored.model != self.agent.model:
            lines.append(ui.notice(f"Last time this used {restored.model}. Switch back with /model {restored.model}"))
        return "\n".join(lines)

    # ------------------------------------------------------------------------------------------ choices for menus

    def session_choices(self) -> list[tuple[str, str]]:
        choices = []
        for path in recordings(self.root)[:30]:
            events = load_events(path)
            turns = sum(e["type"] == "turn_end" and e.get("status") == "ok" for e in events)
            choices.append(
                (display_name(events), f"{ago(path.stat().st_mtime)} · {turns} turn{'s' if turns != 1 else ''}")
            )
        return choices

    def model_choices(self) -> list[tuple[str, str]]:
        if "openrouter" in os.environ.get("ANTHROPIC_BASE_URL", ""):
            return [(model, "free · OpenRouter") for model in FREE_MODELS]
        return [(model, f"${prices[0]:g} / ${prices[1]:g} per M tokens") for model, prices in PRICES.items()]

    def session_tokens(self) -> int:
        usage = self.agent.session_usage
        return usage.input_tokens + usage.output_tokens + usage.cache_read_tokens + usage.cache_write_tokens

    def update_cost(self) -> None:
        tokens = self.session_tokens()
        self.status.model = self.agent.model
        self.status.tokens = f"{fmt_tokens(tokens)} tokens" if tokens else ""
        cost = fmt_cost(self.agent.session_cost) if tokens else ""
        if self.agent.budget is not None:
            cost = f"{fmt_cost(self.agent.session_cost or 0.0)} / ${self.agent.budget:.2f}"
        self.status.cost = cost

    # ------------------------------------------------------------------------------------------ the loop

    def run(self) -> int:
        try:
            return self.loop()
        finally:
            self.goodbye()

    def loop(self) -> int:
        while True:
            try:
                text = self.ui.read()
            except KeyboardInterrupt:
                print(ui.notice("Ctrl+D or /exit quits."))
                continue
            except EOFError:
                return 0
            if not text.strip():
                continue
            parsed = parse(text)
            if parsed is not None:
                command, arg = parsed
                if command is None:
                    print(ui.notice(f"{arg} isn't a command. Type / to see them all.", "warn"))
                    continue
                if not self.handle(command, arg):
                    return 0
                continue
            if not self.send(text.strip()):
                return 1

    def send(self, text: str) -> bool:
        """Run one turn. Returns False only for errors that make continuing pointless."""
        started = time.monotonic()
        try:
            usage = self.agent.send(text)
        except KeyboardInterrupt:
            print("\n" + ui.notice("Stopped. Your last message was dropped from the conversation.", "warn"))
            return True
        except TurnFailed as e:
            print("\n" + ui.notice(f"{e} Your last message was dropped.", "error"))
            return True
        except anthropic.AuthenticationError:
            print("\n" + ui.notice("Invalid API key. Check the key in your .env file.", "error"))
            return False
        except anthropic.NotFoundError:
            print("\n" + ui.notice(f"Model “{self.agent.model}” was not found. Pick another with /model.", "error"))
            return True
        except anthropic.RateLimitError:
            print("\n" + ui.notice("Rate limited. Wait a minute and send it again, or switch with /model.", "error"))
            return True
        except anthropic.APIStatusError as e:
            if error_type(e) == "overloaded_error":
                print("\n" + ui.notice("The model is overloaded. Wait a moment, or switch with /model.", "error"))
            else:
                print("\n" + ui.notice(f"API error {e.status_code}: {e.message}", "error"))
            return True
        except anthropic.APIConnectionError:
            print("\n" + ui.notice("Network error. Check your internet connection.", "error"))
            return True
        finally:
            self.update_cost()

        tokens = usage.input_tokens + usage.output_tokens + usage.cache_read_tokens + usage.cache_write_tokens
        parts = [
            fmt_seconds(time.monotonic() - started),
            f"{fmt_tokens(tokens)} tokens",
            fmt_cost(usage.cost(self.agent.model)),
        ]
        print(paint(f"\n  ✓ {' · '.join(parts)}", "gray"))
        return True

    # ------------------------------------------------------------------------------------------ commands

    def handle(self, command: Command, arg: str) -> bool:
        """Run a slash command. Returns False when the chat should end."""
        name = command.name
        if command.arg_required and not arg:
            print(ui.notice(f"Usage: {command.usage}  ({command.description.lower()})", "warn"))
            return True
        if name == "exit":
            return False
        if name == "help":
            print(ui.help_screen())
        elif name == "clear":
            self.ui.clear()
        elif name == "cost":
            u = self.agent.session_usage
            print(ui.notice(f"This session: {fmt_cost(self.agent.session_cost)}"))
            if self.agent.budget is not None:
                left = max(self.agent.budget - (self.agent.session_cost or 0.0), 0)
                print(ui.notice(f"Budget: ${self.agent.budget:.2f} · ${left:.4f} left"))
            print(
                ui.notice(
                    paint(f"input {u.input_tokens:,} · output {u.output_tokens:,} · ", "gray")
                    + paint(f"cache read {u.cache_read_tokens:,} · cache write {u.cache_write_tokens:,}", "gray")
                )
            )
        elif name == "reset":
            self.agent.reset()
            print(ui.notice("New conversation. Claude has forgotten the earlier messages.", "ok"))
        elif name == "record":
            self.cmd_record(arg)
        elif name == "name":
            self.cmd_name(arg)
        elif name == "share":
            self.cmd_share()
        elif name == "sessions":
            self.cmd_sessions()
        elif name == "resume":
            self.cmd_resume(arg)
        elif name == "model":
            self.cmd_model(arg)
        elif name == "budget":
            self.cmd_budget(arg)
        elif name == "effort":
            self.cmd_effort(arg)
        return True

    def cmd_record(self, title: str) -> None:
        if self.recorder:
            print(ui.notice(f"Already recording to {self.recorder.path}"))
            return
        recorder = self.start_recording(title or None)
        print(ui.notice(f"Recording to {recorder.path}", "ok"))
        if self.agent.messages:
            print(ui.notice("Earlier messages in this chat aren't in the recording; everything from now on is."))

    def cmd_name(self, title: str) -> None:
        if self.recorder is None:
            self.start_recording(title)
            print(ui.notice(f"Recording started as “{title}”", "ok"))
            return
        self.recorder.event("rename", name=title)
        self.title = self.status.recording = title
        print(ui.notice(f"Session named “{title}”", "ok"))

    def cmd_share(self) -> None:
        if self.recorder is None:
            print(
                ui.notice("This session isn't recorded yet. Start with /record, then /share after a message.", "warn")
            )
            return
        from .receipt import share

        share(self.recorder.path, None, open_browser=True)

    def cmd_sessions(self) -> None:
        files = recordings(self.root)
        if not files:
            print(ui.notice("No recorded sessions yet. /record starts one."))
            return
        current = self.recorder.path.resolve() if self.recorder else None
        print()
        for path in files[:15]:
            events = load_events(path)
            turns = sum(e["type"] == "turn_end" and e.get("status") == "ok" for e in events)
            marker = paint(BULLET, "red") if path.resolve() == current else " "
            name = shorten(display_name(events), 50)
            meta = paint(f"{ago(path.stat().st_mtime)} · {turns} turn{'s' if turns != 1 else ''}", "gray")
            print(f"  {marker} {name}  {meta}")
        if len(files) > 15:
            print(ui.notice(f"and {len(files) - 15} older"))
        print(ui.notice("Switch with /resume <name>"))

    def cmd_resume(self, query: str) -> None:
        path = find_recording(self.root, query)
        if path is None:
            print(ui.notice(f"No session matching “{query}”. /sessions lists them.", "warn"))
            return
        if self.recorder and path.resolve() == self.recorder.path.resolve():
            print(ui.notice("That's the session you're in."))
            return
        print(self.resumed_summary(self.resume(path)))

    def cmd_model(self, model: str) -> None:
        if model == self.agent.model:
            print(ui.notice(f"Already using {model}"))
            return
        self.agent.switch_model(model)
        self.update_cost()
        print(ui.notice(f"Now using {paint(model, 'accent')}", "ok"))
        if not self.agent.is_claude:
            print(ui.notice(paint("Thinking, effort and prompt caching only work with Claude models.", "gray")))

    def cmd_budget(self, value: str) -> None:
        try:
            amount = parse_budget(value)
        except argparse.ArgumentTypeError as e:
            print(ui.notice(str(e), "warn"))
            return
        self.agent.set_budget(amount)
        self.update_cost()
        if amount is None:
            print(ui.notice("No budget: Claude can spend without a limit.", "ok"))
            return
        print(
            ui.notice(
                f"Budget set to ${amount:.2f} for this session ({fmt_cost(self.agent.session_cost)} spent).", "ok"
            )
        )
        if self.agent.session_cost is None:
            print(ui.notice(f"There's no known price for {self.agent.model}, so the budget can't be enforced.", "warn"))
        elif self.agent.model.endswith(":free"):
            print(ui.notice("You're on a free model, so this budget won't be reached.", "info"))

    def confirm_over_budget(self, spent: float, budget: float) -> bool:
        print("\n" + ui.notice(f"Your ${budget:.2f} budget is used up ({fmt_cost(spent)} spent).", "warn"))
        return self.ui.confirm("Let Claude finish this turn anyway?")

    def cmd_effort(self, level: str) -> None:
        if level not in EFFORT_LEVELS:
            print(ui.notice(f"Effort must be one of: {', '.join(EFFORT_LEVELS)}", "warn"))
            return
        self.agent.effort = level
        note = "" if self.agent.is_claude else f" (only used by Claude models; you're on {self.agent.model})"
        print(ui.notice(f"Effort set to {level}{note}", "ok"))

    # ------------------------------------------------------------------------------------------ the end

    def goodbye(self) -> None:
        """The last thing on screen: what the session cost, where it's saved, and how to get back to it."""
        print()
        tokens = self.session_tokens()
        if tokens:
            print(ui.notice(f"{fmt_tokens(tokens)} tokens · {fmt_cost(self.agent.session_cost)} this session"))
        if self.recorder:
            path = self.recorder.path
            title = self.title or ""
            self.stop_recording()
            try:
                shown = str(path.relative_to(self.root))
            except ValueError:
                shown = str(path)
            print(ui.notice(f"Saved {paint(shown, 'gray')}", "ok"))
            if self.signer:
                print(f"    {paint('Sealed', 'gray')}    with your key {paint(self.signer.fingerprint, 'green')}")
            named = title and title != "Untitled session"
            resume = f'claude-agent --resume "{title}"' if named else "claude-agent --resume"
            print(f"    {paint('Continue', 'gray')}  {paint(resume, 'accent')}")
            print(f"    {paint('Share', 'gray')}     {paint('receipts share', 'accent')}")
        print(paint(f"\n  {BULLET} See you soon!", "accent"))


def parse_budget(value: str) -> float | None:
    """'0.50', '$2' or '1.5' as dollars; 'off', 'none' or '0' for no budget."""
    text = value.strip().lower().removeprefix("$")
    if text in {"off", "none", "no", "0", ""}:
        return None
    try:
        amount = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"“{value}” isn't an amount. Try /budget 0.50, or /budget off.") from None
    if amount <= 0 or amount != amount or amount == float("inf"):
        raise argparse.ArgumentTypeError("The budget must be a positive amount of dollars, like 0.50.")
    return amount


def load_signer() -> Signer | None:
    """The user's signing key for sealing recordings, created on first use. None if it can't be read."""
    try:
        return Signer.load_or_create()
    except (OSError, ValueError) as e:
        print(ui.notice(f"Recordings won't be sealed: couldn't use your signing key ({e}).", "warn"))
        return None


def shorten(text: str, limit: int = 100) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def ago(timestamp: float) -> str:
    seconds = max(0, time.time() - timestamp)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    if seconds < 7 * 86400:
        return f"{int(seconds // 86400)} d ago"
    return datetime.fromtimestamp(timestamp).strftime("%d %b")


if __name__ == "__main__":
    sys.exit(main())
