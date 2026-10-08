"""Tests for session recording."""

from __future__ import annotations

import json
from pathlib import Path

from claude_agent_cli.cli import AUTO_RECORD_PATH, parse_args
from claude_agent_cli.recorder import RECEIPTS_DIR, Recorder, default_record_path


def test_events_are_one_json_object_per_line(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "s.jsonl")
    recorder.event("user_message", text="مرحبا")
    recorder.event("reset")
    recorder.close()

    lines = (tmp_path / "s.jsonl").read_text(encoding="utf-8").splitlines()
    first, second = (json.loads(line) for line in lines)
    assert first["type"] == "user_message"
    assert first["text"] == "مرحبا"
    assert {"ts", "elapsed_ms"} <= first.keys()
    assert second["type"] == "reset"


def test_events_are_flushed_immediately(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "s.jsonl")
    recorder.event("user_message", text="hi")
    # Not closed yet: a crash here must still leave the event on disk.
    assert "user_message" in (tmp_path / "s.jsonl").read_text(encoding="utf-8")
    recorder.close()


def test_default_path_is_gitignored(tmp_path: Path) -> None:
    path = default_record_path(tmp_path)
    Recorder(path).close()

    assert path.parent == tmp_path / RECEIPTS_DIR
    assert (path.parent / ".gitignore").read_text(encoding="utf-8") == "*\n"


def test_custom_path_gets_no_gitignore(tmp_path: Path) -> None:
    Recorder(tmp_path / "logs" / "s.jsonl").close()
    assert not (tmp_path / "logs" / ".gitignore").exists()


def test_wrap_confirm_records_the_decision(tmp_path: Path) -> None:
    recorder = Recorder(tmp_path / "s.jsonl")
    confirm = recorder.wrap_confirm(lambda command: command == "ls")

    assert confirm("ls") is True
    assert confirm("rm -rf /") is False
    recorder.close()

    events = [json.loads(line) for line in (tmp_path / "s.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [(e["command"], e["approved"]) for e in events] == [("ls", True), ("rm -rf /", False)]


def test_record_flag() -> None:
    assert parse_args([]).record is None
    assert parse_args(["--record"]).record == AUTO_RECORD_PATH
    assert parse_args(["--record", "out.jsonl"]).record == Path("out.jsonl")


def test_name_goes_into_the_filename(tmp_path: Path) -> None:
    path = default_record_path(tmp_path, "IBM research: part 2!")
    assert path.name.endswith("-ibm-research-part-2.jsonl")
    assert default_record_path(tmp_path, "شركة IBM").name.endswith("-شركة-ibm.jsonl")


def test_name_flag_turns_recording_on() -> None:
    args = parse_args(["--name", "demo"])
    assert args.name == "demo"
