from pathlib import Path

import pytest

from claude_agent_cli.tools import MAX_SEARCH_MATCHES, ToolBox, ToolError, truncate, validate_args


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("hello from markdown\n", encoding="utf-8")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "hidden.py").write_text("def hello(): pass\n", encoding="utf-8")
    return tmp_path


def make_toolbox(root: Path, approve: bool = True) -> ToolBox:
    return ToolBox(root=root, confirm=lambda _command: approve)


def test_read_file_adds_line_numbers(project: Path) -> None:
    output = make_toolbox(project).run("read_file", {"path": "app.py"})
    assert "    1  def hello():" in output
    assert "    2      return 'hi'" in output


def test_read_file_rejects_paths_outside_root(project: Path) -> None:
    with pytest.raises(ToolError, match="outside the project root"):
        make_toolbox(project).run("read_file", {"path": "../secret.txt"})


def test_read_file_missing(project: Path) -> None:
    with pytest.raises(ToolError, match="not found"):
        make_toolbox(project).run("read_file", {"path": "nope.py"})


def test_search_finds_matches_and_skips_venv(project: Path) -> None:
    output = make_toolbox(project).run("search_files", {"pattern": "hello"})
    assert "app.py:1: def hello():" in output
    assert "notes.md:1: hello from markdown" in output
    assert ".venv" not in output


def test_search_with_glob(project: Path) -> None:
    output = make_toolbox(project).run("search_files", {"pattern": "hello", "glob": "*.md"})
    assert output == "notes.md:1: hello from markdown"


def test_search_stops_at_limit(tmp_path: Path) -> None:
    (tmp_path / "big.txt").write_text("x\n" * (MAX_SEARCH_MATCHES + 50), encoding="utf-8")
    output = make_toolbox(tmp_path).run("search_files", {"pattern": "x"})
    assert output.count("big.txt:") == MAX_SEARCH_MATCHES


def test_search_bad_regex(project: Path) -> None:
    with pytest.raises(ToolError, match="Invalid regular expression"):
        make_toolbox(project).run("search_files", {"pattern": "("})


def test_run_command_returns_exit_code_and_output(project: Path) -> None:
    output = make_toolbox(project).run("run_command", {"command": "echo hi"})
    assert output.startswith("exit code: 0")
    assert "hi" in output


def test_run_command_needs_approval(project: Path) -> None:
    with pytest.raises(ToolError, match="declined"):
        make_toolbox(project, approve=False).run("run_command", {"command": "echo hi"})


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ("not a dict", "JSON object"),
        ({}, "Missing required argument 'path'"),
        ({"path": 3}, "must be a string"),
        ({"path": "a", "extra": "b"}, "Unexpected argument"),
    ],
)
def test_validate_args_rejects_bad_input(args: object, message: str) -> None:
    with pytest.raises(ToolError, match=message):
        validate_args("read_file", args)


def test_unknown_tool() -> None:
    with pytest.raises(ToolError, match="Unknown tool"):
        validate_args("delete_everything", {})


def test_truncate() -> None:
    assert truncate("short", limit=10) == "short"
    assert truncate("a" * 15, limit=10) == "a" * 10 + "\n[output truncated: 5 more characters]"
