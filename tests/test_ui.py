"""Tests for the chat interface: commands, menus, tool descriptions, model switching and the Chat controller."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import anthropic
import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from test_agent import FakeClient, message, text_block, tool_use_block

from claude_agent_cli import ui
from claude_agent_cli.agent import describe_result, describe_tool
from claude_agent_cli.cli import Chat
from claude_agent_cli.commands import COMMANDS, find_command, parse
from claude_agent_cli.recorder import load_events, recordings
from claude_agent_cli.streamtext import StreamText
from claude_agent_cli.tools import ToolBox

ANSI = re.compile(r"\033\[[0-9;]*m")


def plain(text: str) -> str:
    return ANSI.sub("", text)


# ---------------------------------------------------------------------------------------------- commands


def test_parse_commands_and_messages() -> None:
    assert parse("hello") is None
    assert parse("/usr/bin/env is a path") is None
    assert parse("//not a command") is None
    command, arg = parse("/name  My session ") or (None, "")
    assert command is not None and command.name == "name" and arg == "My session"
    assert parse("?") == (find_command("help"), "")
    assert parse("/quit") == (find_command("exit"), "")
    assert parse("/nope") == (None, "/nope")


def test_every_command_is_in_help() -> None:
    screen = plain(ui.help_screen())
    for command in COMMANDS:
        assert f"/{command.name}" in screen


# ---------------------------------------------------------------------------------------------- menus


def completer(tmp_path: Path) -> ui.ChatCompleter:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("", encoding="utf-8")
    (tmp_path / "README.md").write_text("", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "app.py").write_text("", encoding="utf-8")
    return ui.ChatCompleter(
        tmp_path,
        sessions=lambda: [("IBM research", "2 h ago"), ("Anthropic", "just now")],
        models=lambda: [("claude-opus-5-5", ""), ("claude-haiku-5-5", "")],
    )


def complete(c: ui.ChatCompleter, text: str) -> list[str]:
    return [item.text for item in c.get_completions(Document(text), CompleteEvent())]


def test_slash_opens_the_full_menu(tmp_path: Path) -> None:
    assert complete(completer(tmp_path), "/") == [f"/{c.name}" for c in COMMANDS]


def test_menu_filters_as_you_type(tmp_path: Path) -> None:
    c = completer(tmp_path)
    assert complete(c, "/sh") == ["/share"]
    assert complete(c, "/re") == ["/record", "/resume", "/reset", "/share"]  # prefix matches first, then the rest


def test_command_values_are_suggested(tmp_path: Path) -> None:
    c = completer(tmp_path)
    assert complete(c, "/effort ") == ["low", "medium", "high", "xhigh", "max"]
    assert complete(c, "/resume ib") == ["IBM research"]
    assert complete(c, "/model haiku") == ["claude-haiku-5-5"]
    assert complete(c, "/cost ") == []


def test_at_picks_project_files(tmp_path: Path) -> None:
    c = completer(tmp_path)
    assert complete(c, "explain @app") == ["@src/app.py"]  # .venv is skipped
    assert complete(c, "@READ") == ["@README.md"]
    assert complete(c, "no mention here") == []


# ---------------------------------------------------------------------------------------------- tool lines


def test_tools_read_as_actions() -> None:
    assert describe_tool("read_file", {"path": "src/app.py"}) == ("Read", "src/app.py")
    assert describe_tool("run_command", {"command": "pytest"}) == ("Run", "pytest")
    assert plain(describe_tool("search_files", {"pattern": "TODO", "glob": "*.py"})[1]) == "“TODO” in *.py"


def test_results_are_summarised() -> None:
    def result(content: str, error: bool = False) -> Any:
        return {"type": "tool_result", "tool_use_id": "t", "content": content, "is_error": error}

    assert describe_result("read_file", result("    1  a\n    2  b")) == ("2 lines", True)
    assert describe_result("search_files", result("No matches.")) == ("no matches", True)
    assert describe_result("search_files", result("a:1: x\nb:2: y")) == ("2 matches", True)
    assert describe_result("run_command", result("exit code: 0\nok")) == ("exit 0 · 1 line of output", True)
    assert describe_result("run_command", result("exit code: 2\n")) == ("exit 2 · no output", False)
    assert describe_result("run_command", result("The user declined to run this command.", True)) == (
        "Not run: you declined",
        False,
    )
    assert describe_result("read_file", result("File not found: x", True)) == ("✗ File not found: x", False)


def test_welcome_card_lines_line_up() -> None:
    card = plain(ui.welcome_card([("Model", "claude-opus-5-5"), ("Session", "● recording “شركة IBM”")], width=60))
    widths = {len(line) for line in card.splitlines()}
    assert widths == {58}


# ---------------------------------------------------------------------------------------------- the chat


def make_chat(tmp_path: Path, responses: list[SimpleNamespace]) -> tuple[Chat, FakeClient]:
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    client = FakeClient(responses)
    chat = Chat(
        cast(anthropic.Anthropic, client), ToolBox(root=tmp_path, confirm=lambda _c: False), "claude-opus-5-5", "high"
    )
    chat.agent.write = lambda _text: None
    return chat, client


def test_name_starts_a_recording(tmp_path: Path, capsys: Any) -> None:
    chat, _ = make_chat(tmp_path, [message([text_block("Hi")], "end_turn")])
    chat.handle(find_command("name"), "IBM research")  # type: ignore[arg-type]
    assert chat.recorder is not None and chat.status.recording == "IBM research"
    assert chat.recorder.path.name.endswith("-ibm-research.jsonl")
    chat.send("hi")
    chat.goodbye()
    events = load_events(recordings(tmp_path)[0])
    assert [e["type"] for e in events][0] == "session_start" and events[-1]["type"] == "session_end"
    assert 'claude-agent --resume "IBM research"' in plain(capsys.readouterr().out)


def test_resume_switches_sessions(tmp_path: Path) -> None:
    chat, _ = make_chat(tmp_path, [message([text_block("One")], "end_turn"), message([text_block("Two")], "end_turn")])
    chat.handle(find_command("record"), "first")  # type: ignore[arg-type]
    chat.send("question one")
    first = chat.recorder.path  # type: ignore[union-attr]
    chat.stop_recording()
    chat.start_recording("second")
    chat.send("question two")

    chat.handle(find_command("resume"), "first")  # type: ignore[arg-type]
    assert chat.recorder is not None and chat.recorder.path == first
    assert [m["content"] for m in chat.agent.messages if m["role"] == "user"] == ["question one"]
    assert chat.status.recording == "first"


def test_model_switch_drops_thinking_and_is_recorded(tmp_path: Path) -> None:
    thinking = SimpleNamespace(type="thinking", thinking="hmm", signature="sig")
    chat, client = make_chat(
        tmp_path,
        [
            message([thinking, tool_use_block("tu_1", "read_file", {"path": "app.py"})], "tool_use"),
            message([text_block("It prints hi.")], "end_turn"),
            message([text_block("Still here.")], "end_turn"),
        ],
    )
    for block in client.responses[0].content:  # give the fake blocks the SDK's model_dump()
        block.model_dump = lambda b=block: dict(vars(b))
    client.responses[1].content[0].model_dump = lambda: {"type": "text", "text": "It prints hi."}
    chat.start_recording("demo")
    chat.send("what does app.py do?")

    chat.handle(find_command("model"), "nvidia/nemotron-3-super-120b-a12b:free")  # type: ignore[arg-type]
    chat.send("thanks")

    sent = client.requests[-1]
    assert sent["model"] == "nvidia/nemotron-3-super-120b-a12b:free"
    assert "thinking" not in sent  # other providers get only the core parameters
    blocks = [b for m in sent["messages"] if isinstance(m["content"], list) for b in m["content"]]
    assert all(b["type"] in {"text", "tool_use", "tool_result"} for b in blocks)
    assert any(e["type"] == "model_change" for e in load_events(chat.recorder.path))  # type: ignore[union-attr]


def test_commands_needing_a_value_explain_themselves(tmp_path: Path, capsys: Any) -> None:
    chat, _ = make_chat(tmp_path, [])
    assert chat.handle(find_command("effort"), "") is True  # type: ignore[arg-type]
    assert "Usage: /effort <level>" in plain(capsys.readouterr().out)
    chat.handle(find_command("effort"), "low")  # type: ignore[arg-type]
    assert chat.agent.effort == "low"
    assert chat.handle(find_command("exit"), "") is False  # type: ignore[arg-type]


def test_share_without_recording_says_how(tmp_path: Path, capsys: Any) -> None:
    chat, _ = make_chat(tmp_path, [])
    chat.handle(find_command("share"), "")  # type: ignore[arg-type]
    assert "/record" in plain(capsys.readouterr().out)


@pytest.mark.parametrize("error_class", [anthropic.RateLimitError])
def test_rate_limit_suggests_switching_model(tmp_path: Path, capsys: Any, error_class: type) -> None:
    chat, client = make_chat(tmp_path, [])

    def rate_limited(**_params: Any) -> Any:
        raise error_class("429", response=SimpleNamespace(request=None, status_code=429, headers={}), body=None)

    client.beta.messages.stream = rate_limited
    assert chat.send("hi") is True
    assert "/model" in plain(capsys.readouterr().out)


# ---------------------------------------------------------------------------------------------- streamed text


def stream(chunks: list[str], width: int = 30) -> str:
    out: list[str] = []
    text = StreamText(out.append, width, "  ")
    for chunk in chunks:
        text.feed(chunk)
    text.finish()
    return plain("".join(out))


def test_stream_wraps_words_and_keeps_the_indent() -> None:
    result = stream(["The quick brown fox jumps over the lazy dog again and again"])
    lines = result.split("\n")
    assert len(lines) > 1
    assert all(len(line) <= 30 for line in lines)
    assert all(line.startswith("  ") for line in lines[1:])


def test_stream_never_splits_a_word_across_chunks() -> None:
    assert stream(["hel", "lo wor", "ld"], width=80) == "hello world"


def test_stream_hides_markdown_markers() -> None:
    assert stream(["Use `path", "lib` and **never** guess"], width=80) == "Use pathlib and never guess"


def test_stream_code_blocks_keep_their_lines() -> None:
    result = stream(["Run:\n```bash\nuv run pytest -q --very-long-flag-name\n```\nDone."], width=24)
    assert "uv run pytest -q --very-long-flag-name" in result  # code is never wrapped
    assert "```" not in result
    assert "bash" not in result  # the language label isn't printed
    assert result.endswith("Done.")


def test_stream_newlines_are_indented() -> None:
    assert stream(["one\ntwo"], width=80) == "one\n  two"


# ---------------------------------------------------------------------------------------------- status bar


def bar(width: int, **status: Any) -> str:
    return "".join(fragment[1] for fragment in ui.toolbar(ui.Status(**status), width))


def test_toolbar_shows_everything_when_wide() -> None:
    text = bar(160, model="claude-opus-5-5", recording="IBM", tokens="12k tokens", cost="$0.04")
    parts = ["/ commands", "@ files", "? help", "Alt+Enter", "● rec", "IBM", "claude-opus-5-5", "12k tokens", "$0.04"]
    for part in parts:
        assert part in text
    assert len(text) == 160


def test_toolbar_drops_details_before_the_essentials() -> None:
    text = bar(60, model="claude-opus-5-5", recording="IBM research", tokens="12k tokens", cost="$0.04")
    assert "/ commands" in text and "claude-opus-5-5" in text and "$0.04" in text
    assert "12k tokens" not in text
    assert len(text) <= 60


def test_cost_survives_a_model_switch(tmp_path: Path) -> None:
    chat, _ = make_chat(tmp_path, [message([text_block("Hi")], "end_turn")])
    chat.send("hi")
    paid = chat.agent.session_cost
    assert paid
    chat.handle(find_command("model"), "nvidia/nemotron-3-super-120b-a12b:free")  # type: ignore[arg-type]
    assert chat.agent.session_cost == paid  # switching to a free model doesn't make earlier turns free
    assert chat.status.cost == f"${paid:.4f}"
