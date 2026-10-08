"""Tests for --resume: rebuilding a conversation from its recording."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_agent import make_agent, message, text_block, tool_use_block

from claude_agent_cli.agent import TurnFailed
from claude_agent_cli.cli import parse_args
from claude_agent_cli.recorder import RECEIPTS_DIR, Recorder, find_recording, load_events
from claude_agent_cli.resume import restore


def thinking_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="thinking", thinking=text)


def record_session(tmp_path: Path, responses: list[SimpleNamespace], prompts: list[str]) -> Path:
    """Run prompts through a fake agent with recording on and return the recording's path."""
    agent, _ = make_agent(tmp_path, responses)
    path = tmp_path / "session.jsonl"
    agent.recorder = Recorder(path)
    agent.recorder.event("session_start", name="demo", model="claude-opus-5-5")
    for prompt in prompts:
        with contextlib.suppress(TurnFailed):
            agent.send(prompt)
    agent.recorder.close()
    return path


def test_round_trip_matches_the_live_conversation(tmp_path: Path) -> None:
    path = record_session(
        tmp_path,
        [
            message(
                [thinking_block("look at app.py"), tool_use_block("tu_1", "read_file", {"path": "app.py"})], "tool_use"
            ),
            message([text_block("It prints hi.")], "end_turn"),
        ],
        ["what does app.py do?"],
    )
    restored = restore(load_events(path))

    assert restored.turns == 1
    assert restored.name == "demo"
    assert restored.messages == [
        {"role": "user", "content": "what does app.py do?"},
        # The thinking block is dropped: its signature was never recorded.
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "tu_1", "name": "read_file", "input": {"path": "app.py"}}],
        },
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "    1  print('hi')"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "It prints hi."}]},
    ]
    assert restored.usage.output_tokens == 100
    assert restored.last_question == "what does app.py do?"
    assert restored.last_answer == "It prints hi."


def test_failed_turns_are_left_out(tmp_path: Path) -> None:
    refused = message([], "refusal", stop_details=SimpleNamespace(category="cyber"))
    path = record_session(
        tmp_path,
        [refused, message([text_block("Hello")], "end_turn")],
        ["declined question", "hi"],
    )
    restored = restore(load_events(path))

    assert [m["content"] for m in restored.messages if m["role"] == "user"] == ["hi"]
    assert (restored.turns, restored.skipped_turns) == (1, 1)


def test_tool_errors_keep_their_flag(tmp_path: Path) -> None:
    path = record_session(
        tmp_path,
        [
            message([tool_use_block("tu_1", "read_file", {"path": "missing.py"})], "tool_use"),
            message([text_block("Not found.")], "end_turn"),
        ],
        ["read missing.py"],
    )
    results_message: Any = restore(load_events(path)).messages[2]
    result = results_message["content"][0]
    assert result["is_error"] is True


def test_reset_clears_earlier_turns() -> None:
    events: list[dict[str, Any]] = [
        {"type": "user_message", "text": "old"},
        {"type": "model_response", "content": [{"type": "text", "text": "a"}]},
        {"type": "turn_end", "status": "ok", "usage": {}},
        {"type": "reset"},
        {"type": "user_message", "text": "new"},
        {"type": "model_response", "content": [{"type": "text", "text": "b"}]},
        {"type": "turn_end", "status": "ok", "usage": {}},
    ]
    restored = restore(events)
    assert restored.messages[0]["content"] == "new"
    assert restored.turns == 1


def test_unfinished_turn_is_left_out() -> None:
    """The window was closed while Claude was working: no turn_end was written."""
    events: list[dict[str, Any]] = [
        {"type": "user_message", "text": "done"},
        {"type": "model_response", "content": [{"type": "text", "text": "a"}]},
        {"type": "turn_end", "status": "ok", "usage": {}},
        {"type": "user_message", "text": "cut off"},
        {"type": "model_response", "content": [{"type": "tool_use", "id": "t", "name": "read_file", "input": {}}]},
    ]
    restored = restore(events)
    assert len(restored.messages) == 2
    assert restored.skipped_turns == 1
    assert restored.unanswered == "cut off"


def test_failed_last_message_is_reported_as_unanswered(tmp_path: Path) -> None:
    rate_limited = message([], "refusal", stop_details=SimpleNamespace(category="cyber"))
    path = record_session(tmp_path, [message([text_block("Hi")], "end_turn"), rate_limited], ["hi", "what is IBM?"])
    assert restore(load_events(path)).unanswered == "what is IBM?"
    (tmp_path / "b").mkdir()
    path = record_session(tmp_path / "b", [message([text_block("Hi")], "end_turn")], ["hi"])
    assert restore(load_events(path)).unanswered is None


def test_resumed_agent_sends_the_old_history(tmp_path: Path) -> None:
    path = record_session(tmp_path, [message([text_block("One")], "end_turn")], ["first"])
    agent, client = make_agent(tmp_path, [message([text_block("Two")], "end_turn")])
    agent.messages = restore(load_events(path)).messages

    agent.send("second")
    sent = client.requests[0]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[0]["content"] == "first" and sent[2]["content"] == "second"


def test_resumed_recording_continues_the_clock(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    recorder = Recorder(path, elapsed_offset_ms=60_000)
    recorder.event("session_resume")
    recorder.close()
    assert load_events(path)[0]["elapsed_ms"] >= 60_000


def test_find_recording_by_name_path_or_newest(tmp_path: Path) -> None:
    folder = tmp_path / RECEIPTS_DIR
    folder.mkdir()
    old = folder / "session-20261008-101132.jsonl"
    old.write_text('{"type": "session_start", "name": "IBM research"}\n', encoding="utf-8")
    new = folder / "session-20261008-103222-anthropic.jsonl"
    new.write_text('{"type": "session_start", "name": "Anthropic"}\n', encoding="utf-8")
    newer_mtime = old.stat().st_mtime + 10

    os.utime(new, (newer_mtime, newer_mtime))

    assert find_recording(tmp_path, "") == new
    assert find_recording(tmp_path, "Anthropic") == new
    assert find_recording(tmp_path, "ibm research") == old  # matched by session name, not file name
    assert find_recording(tmp_path, str(old)) == old
    assert find_recording(tmp_path, "nothing like this") is None

    unnamed = folder / "session-20261008-090000.jsonl"
    unnamed.write_text('{"type": "user_message", "text": "what is ibm company"}\n', encoding="utf-8")
    os.utime(unnamed, (newer_mtime + 10, newer_mtime + 10))
    assert find_recording(tmp_path, "ibm company") == unnamed  # unnamed: matched by its first message


def test_resume_flag() -> None:
    assert parse_args([]).resume is None
    assert parse_args(["--resume"]).resume == ""
    assert parse_args(["--resume", "anthropic"]).resume == "anthropic"


@pytest.mark.parametrize("argv", [["--resume", "--record"], ["--resume", "x", "--record", "out.jsonl"]])
def test_resume_refuses_record(argv: list[str], tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    from claude_agent_cli import cli

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(cli, "load_dotenv", lambda **_kwargs: None)
    assert cli.main([*argv, "--root", str(tmp_path)]) == 1
    assert "leave out --record" in capsys.readouterr().err
