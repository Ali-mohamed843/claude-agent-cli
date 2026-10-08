"""Command-line entry point: a chat loop in the terminal."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv

from .agent import DEFAULT_MODEL, Agent, TurnFailed
from .tools import ToolBox

HELP = "Commands: /cost (session usage), /reset (new conversation), /exit (quit)"


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page; make sure Arabic, emoji etc. print.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
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
    agent = Agent(client, toolbox, model=args.model, effort=args.effort)

    print(f"claude-agent · {args.model} · effort {args.effort} · {toolbox.root}")
    print(HELP)

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
            print(f"Session: {agent.session_usage.summary(args.model)}")
            continue
        if user_text == "/reset":
            agent.reset()
            print("Started a new conversation.")
            continue
        if user_text.startswith("/"):
            print(HELP)
            continue

        try:
            usage = agent.send(user_text)
            print(f"\033[2m[{usage.summary(args.model)}]\033[0m")
        except KeyboardInterrupt:
            print("\n[interrupted; your last message was dropped from the conversation]")
        except TurnFailed as e:
            print(f"\n[{e} Your last message was dropped from the conversation.]")
        except anthropic.AuthenticationError:
            print("\n[Invalid API key. Check the key in your .env file.]")
            return 1
        except anthropic.NotFoundError:
            print(f"\n[Model '{args.model}' was not found. Check the --model value.]")
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

    print(f"Session: {agent.session_usage.summary(args.model)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
