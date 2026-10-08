"""Tools the agent can call: their JSON schemas (sent to Claude) and implementations (run locally)."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from anthropic.types.beta import BetaToolParam

MAX_FILE_BYTES = 200_000
MAX_OUTPUT_CHARS = 20_000
MAX_SEARCH_MATCHES = 100
COMMAND_TIMEOUT_SECONDS = 60
SKIPPED_DIRS = {".agent-receipts", ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache"}

TOOL_DEFINITIONS: list[BetaToolParam] = [
    {
        "name": "read_file",
        "description": (
            "Read a UTF-8 text file inside the project and return its contents with line numbers. "
            "Use this before answering questions about a file's code."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Path relative to the project root."},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        "eager_input_streaming": True,
    },
    {
        "name": "search_files",
        "description": (
            "Search file contents in the project with a regular expression. Returns up to "
            f"{MAX_SEARCH_MATCHES} matches as 'path:line: text'. Use it to find where something is defined or used."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regular expression to search for."},
                "glob": {
                    "type": "string",
                    "description": (
                        "Optional glob to limit the search, matched against the path from the project root "
                        "or the file name, e.g. '*.py' or 'src/**/*.py'. Defaults to all files."
                    ),
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        "eager_input_streaming": True,
    },
    {
        "name": "run_command",
        "description": (
            "Run a shell command in the project root and return its exit code and output. "
            "The user must approve every command, so prefer read_file and search_files for reading code."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run."},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "eager_input_streaming": True,
    },
]


class ToolError(Exception):
    """A tool failed in a way Claude should hear about (sent back as an is_error tool_result)."""


@dataclass
class ToolBox:
    """Runs tools against one project root. `confirm` asks the user before a command runs."""

    root: Path
    confirm: Callable[[str], bool]

    def __post_init__(self) -> None:
        self.root = self.root.resolve()

    def run(self, name: str, args: object) -> str:
        validate_args(name, args)
        assert isinstance(args, dict)
        if name == "read_file":
            return self.read_file(args["path"])
        if name == "search_files":
            return self.search_files(args["pattern"], args.get("glob"))
        if name == "run_command":
            return self.run_command(args["command"])
        raise ToolError(f"Unknown tool: {name}")

    def resolve(self, path: str) -> Path:
        """Resolve a model-supplied path and refuse anything outside the project root."""
        target = (self.root / path).resolve()
        if not target.is_relative_to(self.root):
            raise ToolError(f"Path '{path}' is outside the project root.")
        return target

    def read_file(self, path: str) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise ToolError(f"File not found: {path}")
        if target.stat().st_size > MAX_FILE_BYTES:
            raise ToolError(f"File is larger than {MAX_FILE_BYTES} bytes; search it instead.")
        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError as e:
            raise ToolError(f"{path} is not a UTF-8 text file.") from e
        return "\n".join(f"{i:>5}  {line}" for i, line in enumerate(text.splitlines(), start=1))

    def search_files(self, pattern: str, glob: str | None = None) -> str:
        try:
            regex = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"Invalid regular expression: {e}") from e

        matches: list[str] = []
        for file in self._iter_files(glob or "*"):
            try:
                lines = file.read_text(encoding="utf-8").splitlines()
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable file
            for number, line in enumerate(lines, start=1):
                if regex.search(line):
                    relative = file.relative_to(self.root).as_posix()
                    matches.append(f"{relative}:{number}: {line.strip()}")
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        return "\n".join(matches) + f"\n[stopped at {MAX_SEARCH_MATCHES} matches]"
        return "\n".join(matches) if matches else "No matches."

    def run_command(self, command: str) -> str:
        if not self.confirm(command):
            raise ToolError("The user declined to run this command.")
        try:
            result = subprocess.run(
                command,
                shell=True,
                cwd=self.root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as e:
            raise ToolError(f"Command timed out after {COMMAND_TIMEOUT_SECONDS}s.") from e
        output = truncate(f"{result.stdout}{result.stderr}")
        return f"exit code: {result.returncode}\n{output}"

    def _iter_files(self, glob: str) -> list[Path]:
        files: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIPPED_DIRS)
            for name in sorted(filenames):
                file = Path(dirpath) / name
                if glob_matches(file.relative_to(self.root).as_posix(), glob):
                    files.append(file)
        return files


def glob_matches(relative_path: str, glob: str) -> bool:
    """Match a glob against a file's project-relative path or its bare name.

    Models write globs both ways ('*.py', 'src/**/*.py'), so accept either. '**/' may match
    zero folders, so also try the pattern with it removed ('src/**/*.py' matches 'src/a.py').
    """
    name = relative_path.rsplit("/", 1)[-1]
    patterns = {glob, glob.replace("**/", "")}
    return any(fnmatch(relative_path, p) or fnmatch(name, p) for p in patterns)


def validate_args(name: str, args: object) -> None:
    """Check tool input against its schema.

    With eager input streaming the API no longer validates tool input, so a truncated or
    malformed input can reach us. We check it ourselves before running anything.
    """
    definition = next((t for t in TOOL_DEFINITIONS if t["name"] == name), None)
    if definition is None:
        raise ToolError(f"Unknown tool: {name}")
    if not isinstance(args, dict):
        raise ToolError("Tool input must be a JSON object.")
    schema: dict[str, Any] = dict(definition["input_schema"])
    properties: dict[str, Any] = schema["properties"]
    for key in schema["required"]:
        if key not in args:
            raise ToolError(f"Missing required argument '{key}'.")
    for key, value in args.items():
        if key not in properties:
            raise ToolError(f"Unexpected argument '{key}'.")
        if not isinstance(value, str):
            raise ToolError(f"Argument '{key}' must be a string.")


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[output truncated: {len(text) - limit} more characters]"
