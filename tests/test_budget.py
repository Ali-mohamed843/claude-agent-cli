"""Tests for the budget guard: the note to Claude, the 80% warning, stopping at the limit, and resuming."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pytest
from test_agent import make_agent, message, text_block, tool_use_block
from test_ui import make_chat, plain

from claude_agent_cli.agent import BudgetReached
from claude_agent_cli.cli import parse_budget
from claude_agent_cli.commands import find_command
from claude_agent_cli.receipt_page import render
from claude_agent_cli.recorder import Recorder, load_events
from claude_agent_cli.resume import restore

RESPONSE_COST = 0.0014  # the fake responses: 100 input + 50 output tokens on claude-opus-5-5


def tool_then_answer() -> list[Any]:
    return [
        message([tool_use_block("tu_1", "read_file", {"path": "app.py"})], "tool_use"),
        message([text_block("It prints hi.")], "end_turn"),
    ]


def test_claude_is_told_what_is_left(tmp_path: Path) -> None:
    agent, client = make_agent(tmp_path, [message([text_block("Hi")], "end_turn")])
    agent.set_budget(0.50)
    agent.send("hi")
    content = client.requests[0]["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "hi"}
    assert "$0.50 budget; $0.50 is left" in content[1]["text"]


def test_no_budget_means_no_note(tmp_path: Path) -> None:
    agent, client = make_agent(tmp_path, [message([text_block("Hi")], "end_turn")])
    agent.send("hi")
    assert client.requests[0]["messages"][0]["content"] == "hi"


def test_stops_before_going_over(tmp_path: Path) -> None:
    agent, client = make_agent(tmp_path, tool_then_answer())
    agent.recorder = Recorder(tmp_path / "s.jsonl")
    agent.set_budget(0.001)  # the first response alone uses it up
    with pytest.raises(BudgetReached, match=r"\$0.00 budget"):
        agent.send("what does app.py do?")
    assert len(client.requests) == 1  # the second request was never sent
    assert agent.messages == []  # the turn was rolled back like any failed turn
    types = [e["type"] for e in load_events(tmp_path / "s.jsonl")]
    assert "budget_stop" in types and types[-1] == "turn_end"


def test_the_user_can_let_the_turn_finish(tmp_path: Path) -> None:
    agent, client = make_agent(tmp_path, tool_then_answer())
    asked: list[tuple[float, float]] = []

    def keep_going(spent: float, budget: float) -> bool:
        asked.append((spent, budget))
        return True

    agent.on_budget = keep_going
    agent.set_budget(0.001)
    agent.send("what does app.py do?")
    assert len(client.requests) == 2
    assert len(asked) == 1  # asked once, not before every request
    assert asked[0][0] == pytest.approx(RESPONSE_COST) and asked[0][1] == 0.001


def test_warns_once_at_80_percent(tmp_path: Path) -> None:
    agent, _ = make_agent(tmp_path, [*tool_then_answer(), message([text_block("Again")], "end_turn")])
    out: list[str] = []
    agent.write = out.append
    agent.set_budget(RESPONSE_COST / 0.85)  # 85% after the first response
    agent.on_budget = lambda spent, budget: True
    agent.send("first")
    agent.send("second")
    assert sum("of your $" in chunk for chunk in out) == 1


def test_parse_budget() -> None:
    assert parse_budget("0.50") == 0.5
    assert parse_budget("$2") == 2.0
    assert parse_budget("off") is None and parse_budget("0") is None
    for bad in ["cheap", "-1", "nan", "inf"]:
        with pytest.raises(argparse.ArgumentTypeError):
            parse_budget(bad)


def test_budget_command_updates_the_status_bar(tmp_path: Path, capsys: Any) -> None:
    chat, _ = make_chat(tmp_path, [message([text_block("Hi")], "end_turn")])
    chat.handle(find_command("budget"), "0.50")  # type: ignore[arg-type]
    chat.send("hi")
    assert chat.status.cost == f"${RESPONSE_COST:.4f} / $0.50"
    chat.handle(find_command("budget"), "off")  # type: ignore[arg-type]
    assert chat.agent.budget is None
    assert "No budget" in plain(capsys.readouterr().out)


def test_budget_survives_resume_and_shows_on_the_receipt(tmp_path: Path) -> None:
    chat, _ = make_chat(tmp_path, [message([text_block("Hi")], "end_turn")])
    chat.start_recording("budgeted")
    chat.handle(find_command("budget"), "0.25")  # type: ignore[arg-type]
    chat.send("hi")
    path = chat.recorder.path  # type: ignore[union-attr]
    chat.stop_recording()

    events = load_events(path)
    assert restore(events).budget == 0.25
    page = render(events)
    assert "<dt>Budget</dt><dd>$0.25 ✓</dd>" in page
    assert "Budget set to $0.25" in page
