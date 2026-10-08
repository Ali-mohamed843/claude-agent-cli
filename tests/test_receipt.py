"""Tests for the receipt page: redaction, rendering and the `receipts` command."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from claude_agent_cli.receipt import Redactor, main
from claude_agent_cli.receipt_page import render, summarize
from claude_agent_cli.recorder import load_events

ROOT = r"C:\Users\ali\projects\demo"


def session(*events: dict[str, Any]) -> list[dict[str, Any]]:
    start = {"type": "session_start", "ts": "2026-10-08T10:00:00.000+00:00", "elapsed_ms": 0, "root": ROOT}
    return [start, *events]


def turn(text: str, **end: Any) -> list[dict[str, Any]]:
    usage = {"input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 0, "cache_write_tokens": 0}
    return [
        {"type": "user_message", "text": text, "elapsed_ms": 1000},
        {"type": "model_response", "content": [{"type": "text", "text": "Answer"}], "elapsed_ms": 2000},
        {"type": "turn_end", "status": "ok", "usage": usage, "cost_usd": 0.001, "elapsed_ms": 2000, **end},
    ]


def test_redacts_api_keys_and_env_lines() -> None:
    redactor = Redactor()
    text = redactor.text(
        "key sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123\n"
        "    3  ANTHROPIC_API_KEY=sk-ant-whatever\n"
        "    4  OPENROUTER_TOKEN = abc123\n"
        'config = {"api_key": "hunter2hunter2"}\n'
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
    )
    assert "sk-ant" not in text
    assert "abc123" not in text
    assert "hunter2" not in text
    assert "abcdefghijklmnopqrstuvwxyz" not in text
    assert "ANTHROPIC_API_KEY=[redacted]" in text  # the variable name stays readable
    assert redactor.count == 5


def test_a_secret_is_counted_once() -> None:
    redactor = Redactor()
    assert redactor.text("API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz") == "API_KEY=[redacted]"
    assert redactor.count == 1


def test_keeps_ordinary_code() -> None:
    code = "    1  def tokenize(text):\n    2      return text.split()\n    3  MAX_TOKENS = 64_000"
    assert Redactor().text("token count is 5") == "token count is 5"
    assert "def tokenize" in Redactor().text(code)


def test_hides_emails_and_local_paths() -> None:
    redactor = Redactor(root=ROOT)
    text = redactor.text(ROOT + r"\src\app.py and C:/Users/ali/projects/demo/README.md by ali@example.com")
    assert text == r"<project>\src\app.py and <project>/README.md by [email]"


def test_redacts_nested_event_values() -> None:
    event = {"type": "tool_result", "content": "token: ghp_abcdefghijklmnopqrstuvwxyz123456", "id": "tu_1"}
    assert "ghp_" not in json.dumps(Redactor().value(event))


def test_name_comes_from_rename_then_start_then_first_message() -> None:
    assert summarize(session(*turn("what is IBM?"))).name == "what is IBM?"
    named = session(*turn("hi"))
    named[0]["name"] = "IBM research"
    assert summarize(named).name == "IBM research"
    named.append({"type": "rename", "name": "IBM deep dive", "elapsed_ms": 3000})
    assert summarize(named).name == "IBM deep dive"


def test_summary_totals() -> None:
    events = session(
        *turn("one"),
        *turn("two", status="failed", error="TurnFailed"),
        {"type": "approval", "command": "ls", "approved": True},
        {"type": "approval", "command": "rm x", "approved": False},
    )
    s = summarize(events)
    assert (s.turns, s.failed_turns, s.approved, s.declined, s.tokens) == (2, 1, 1, 1, 300)
    assert s.cost is not None and abs(s.cost - 0.002) < 1e-9


def test_unknown_price_makes_total_unknown() -> None:
    assert summarize(session(*turn("hi", cost_usd=None))).cost is None


def test_render_escapes_html() -> None:
    page = render(session(*turn("<script>alert(1)</script>")))
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page


def test_render_tool_call_with_approval_and_output() -> None:
    events = session(
        {"type": "user_message", "text": "run tests", "elapsed_ms": 0},
        {
            "type": "model_response",
            "content": [{"type": "tool_use", "id": "tu_1", "name": "run_command", "input": {"command": "pytest"}}],
        },
        {"type": "approval", "command": "pytest", "approved": False},
        {
            "type": "tool_result",
            "id": "tu_1",
            "name": "run_command",
            "is_error": True,
            "content": "The user declined to run this command.",
            "duration_ms": 3,
        },
    )
    page = render(events)
    assert "You declined" in page
    assert "Wanted to run" in page
    assert '<code class="target cmd">pytest</code>' in page
    assert "<details>" not in page.split('id="turn-1"')[1]  # nothing to expand for a declined command


def test_render_command_exit_code_and_output() -> None:
    events = session(
        {"type": "user_message", "text": "run tests", "elapsed_ms": 0},
        {
            "type": "model_response",
            "content": [{"type": "tool_use", "id": "tu_1", "name": "run_command", "input": {"command": "pytest"}}],
        },
        {"type": "approval", "command": "pytest", "approved": True},
        {
            "type": "tool_result",
            "id": "tu_1",
            "name": "run_command",
            "is_error": False,
            "content": "exit code: 1\n1 failed",
            "duration_ms": 3300,
        },
    )
    page = render(events)
    assert "Approved" in page
    assert '<span class="chip bad">exit 1</span>' in page
    assert "<pre>1 failed</pre>" in page  # the exit-code line becomes the badge
    assert "3.3 s" in page
    assert "Unfinished" in page  # no turn_end was recorded


def test_render_final_answer_as_markdown() -> None:
    events = session(
        {"type": "user_message", "text": "what is IBM?", "elapsed_ms": 0},
        {"type": "model_response", "content": [{"type": "text", "text": "## IBM\n\n- **Founded** 1911"}]},
        {"type": "turn_end", "status": "ok", "usage": {"output_tokens": 10}, "cost_usd": 0.0, "elapsed_ms": 9600},
    )
    page = render(events)
    assert '<div class="msg answer">' in page
    assert "<h4>IBM</h4>" in page
    assert "<li><strong>Founded</strong> 1911</li>" in page
    assert "took 9.6 s" in page


def test_render_failed_turn_banner_and_turn_list() -> None:
    events = session(
        *turn("first question"),
        {"type": "user_message", "text": "second question", "elapsed_ms": 3000},
        {"type": "turn_end", "status": "failed", "error": "RateLimitError", "message": "429", "elapsed_ms": 3100},
    )
    page = render(events, redacted=2)
    assert "RateLimitError: 429" in page
    assert '<li class="failed"><a href="#turn-2">' in page
    assert 'id="turn-1"' in page and 'id="turn-2"' in page
    assert "2 secrets redacted" in page


def test_load_events_skips_a_partial_last_line(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_text('{"type": "user_message", "text": "hi"}\n{"type": "model_resp', encoding="utf-8")
    assert [e["type"] for e in load_events(path)] == ["user_message"]


def test_share_writes_a_redacted_page(tmp_path: Path, monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / ".agent-receipts"
    folder.mkdir()
    events = session(*turn("my key is sk-ant-api03-abcdefghijklmnopqrstuvwxyz"))
    (folder / "session-1.jsonl").write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")

    assert main(["share", "--no-open"]) == 0  # no file given: uses the newest recording
    page = (folder / "session-1.html").read_text(encoding="utf-8")
    assert "sk-ant" not in page
    assert "Redacted 1 secret" in capsys.readouterr().out


def test_share_without_recordings(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["share", "--no-open"]) == 1


def test_render_marks_where_a_session_was_resumed() -> None:
    events = session(*turn("one"), {"type": "session_resume", "model": "claude-sonnet-5-5"}, *turn("two"))
    assert "Session resumed on claude-sonnet-5-5" in render(events)
