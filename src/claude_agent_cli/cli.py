"""Command-line entry point: a chat loop in the terminal."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import asdict
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from .agent import DEFAULT_MODEL, Agent, TurnFailed
from .recorder import FORMAT_VERSION, RECEIPTS_DIR, Recorder, default_record_path, find_recording, load_events
from .resume import restore
from .tools import ToolBox

HELP = "Commands: /cost (session usage), /reset (new conversation), /exit (quit)"
RECORDING_HELP = "Recording: /name <title> renames the session; `receipts share` turns it into a web page"
AUTO_RECORD_PATH = Path("<auto>")  # `--record` with no path: pick a name under the project root


def confirm_command(command: str) -> bool:
    answer = input(f"\n  Run `{command}`? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


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
    parser = argparse.ArgumentParser(prog="claude-agent", description="A small coding agent for your terminal.")
    parser.add_argument(
        "--model",
        default=os.environ.get("AGENT_MODEL", DEFAULT_MODEL),
        help=f"Model ID (default: AGENT_MODEL from .env, else {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--effort",
        default="high",
        choices=["low", "medium", "high", "xhigh", "max"],
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

    client = anthropic.Anthropic()
    if not (client.api_key or client.auth_token or client.credentials):
        print(
            "No API key found. Copy .env.example to .env and set ANTHROPIC_API_KEY "
            "(or the OpenRouter settings described there).",
            file=sys.stderr,
        )
        return 1

    toolbox = ToolBox(root=args.root, confirm=confirm_command)
    if args.resume is not None:
        return resume_session(client, toolbox, args)
    recorder = None
    if args.name and args.record is None:
        args.record = AUTO_RECORD_PATH  # naming a session only makes sense if it is recorded
    if args.record is not None:
        path = default_record_path(toolbox.root, args.name) if args.record == AUTO_RECORD_PATH else args.record
        recorder = Recorder(path)
        toolbox.confirm = recorder.wrap_confirm(confirm_command)
        recorder.event(
            "session_start",
            format_version=FORMAT_VERSION,
            agent="claude-agent-cli",
            name=args.name,
            model=args.model,
            effort=args.effort,
            root=str(toolbox.root),
        )
    agent = Agent(client, toolbox, model=args.model, effort=args.effort, recorder=recorder)

    print(f"claude-agent · {args.model} · effort {args.effort} · {toolbox.root}")
    print(HELP)
    if recorder:
        print(f"Recording to {recorder.path}")
        print(RECORDING_HELP)
    return run(agent)


def resume_session(client: anthropic.Anthropic, toolbox: ToolBox, args: argparse.Namespace) -> int:
    if args.record is not None:
        print("--resume keeps writing to the session's own file; leave out --record.", file=sys.stderr)
        return 1
    path = find_recording(toolbox.root, args.resume)
    if path is None:
        where = toolbox.root / RECEIPTS_DIR
        wanted = f" matching “{args.resume}”" if args.resume else ""
        print(f"No recorded session{wanted} in {where}. `receipts list` shows what is there.", file=sys.stderr)
        return 1

    restored = restore(load_events(path))
    recorder = Recorder(path, elapsed_offset_ms=restored.elapsed_ms)
    toolbox.confirm = recorder.wrap_confirm(confirm_command)
    recorder.event("session_resume", model=args.model, effort=args.effort, turns_restored=restored.turns)
    if args.name:
        recorder.event("rename", name=args.name)

    agent = Agent(client, toolbox, model=args.model, effort=args.effort, recorder=recorder)
    agent.messages = restored.messages
    agent.session_usage = restored.usage

    print(f"claude-agent · {args.model} · effort {args.effort} · {toolbox.root}")
    print(HELP)
    title = args.name or restored.name
    turns = f"{restored.turns} turn{'s' if restored.turns != 1 else ''}"
    print(f"\nResumed “{title}”: {turns} restored, recording to {recorder.path}")
    if restored.skipped_turns:
        print(f"  ({restored.skipped_turns} failed or unfinished turn(s) left out; they never got an answer)")
    if restored.model and restored.model != args.model:
        print(f"  (last time this session used {restored.model}; pass --model to switch back)")
    if restored.last_question:
        print(f"\033[2m  Last question: {shorten(restored.last_question)}")
        print(f"  Last answer:   {shorten(restored.last_answer or '')}\033[0m")
    if restored.unanswered:
        print(f"  Your last message never got an answer: “{shorten(restored.unanswered)}”. Send it again to retry.")
    print(RECORDING_HELP)
    return run(agent)


def shorten(text: str, limit: int = 100) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def run(agent: Agent) -> int:
    """Run the chat, and close the recording properly however the chat ends."""
    try:
        return chat_loop(agent, agent.model)
    finally:
        if agent.recorder:
            usage = agent.session_usage
            agent.recorder.event("session_end", usage=asdict(usage), cost_usd=usage.cost(agent.model))
            agent.recorder.close()
            print(f"Session recorded to {agent.recorder.path}")
            print("Continue it later with `claude-agent --resume`, or view it with `receipts share`.")


def rename(agent: Agent, name: str) -> None:
    if not agent.recorder:
        print("This session is not being recorded. Start with --record (or --name) to name it.")
    elif not name:
        print("Usage: /name <title>")
    else:
        agent.recorder.event("rename", name=name)
        print(f"Session named “{name}”.")


def chat_loop(agent: Agent, model: str) -> int:
    while True:
        try:
            user_text = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_text:
            continue
        if user_text in {"/exit", "/quit"}:
            break
        if user_text == "/cost":
            print(f"Session: {agent.session_usage.summary(model)}")
            continue
        if user_text == "/reset":
            agent.reset()
            print("Started a new conversation.")
            continue
        if user_text == "/name" or user_text.startswith("/name "):
            rename(agent, user_text.removeprefix("/name").strip())
            continue
        if user_text.startswith("/"):
            print(HELP)
            continue

        try:
            usage = agent.send(user_text)
            print(f"\033[2m[{usage.summary(model)}]\033[0m")
        except KeyboardInterrupt:
            print("\n[interrupted; your last message was dropped from the conversation]")
        except TurnFailed as e:
            print(f"\n[{e} Your last message was dropped from the conversation.]")
        except anthropic.AuthenticationError:
            print("\n[Invalid API key. Check the key in your .env file.]")
            return 1
        except anthropic.NotFoundError:
            print(f"\n[Model '{model}' was not found. Check the --model value.]")
            return 1
        except anthropic.RateLimitError:
            print("\n[Rate limited. Wait a moment and try again.]")
        except anthropic.APIStatusError as e:
            if error_type(e) == "overloaded_error":
                print("\n[The model is overloaded right now. Wait a moment and send your message again.]")
            else:
                print(f"\n[API error {e.status_code}: {e.message}]")
        except anthropic.APIConnectionError:
            print("\n[Network error. Check your internet connection.]")

    print(f"Session: {agent.session_usage.summary(model)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
