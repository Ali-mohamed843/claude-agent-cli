"""Session recording: write everything the agent does to a JSONL file, one event per line.

The file is the raw material for a "receipt": a replayable record of what the agent read, ran and answered,
and where the human stepped in. Events are appended and flushed as they happen, so a crash or Ctrl+C
still leaves a valid file behind.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

FORMAT_VERSION = 1
RECEIPTS_DIR = ".agent-receipts"


def default_record_path(root: Path, name: str | None = None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    slug = slugify(name) if name else ""
    return root / RECEIPTS_DIR / (f"session-{stamp}-{slug}.jsonl" if slug else f"session-{stamp}.jsonl")


def slugify(name: str) -> str:
    """A filename-safe version of a session name. Keeps letters in any script (Arabic, etc.)."""
    return re.sub(r"[^\w]+", "-", name.lower()).strip("-_")[:50]


def load_events(path: Path) -> list[dict[str, Any]]:
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue  # a crash mid-write can leave a partial last line
        if isinstance(event, dict) and "type" in event:
            events.append(event)
    return events


def recordings(root: Path) -> list[Path]:
    """Recordings under the project root, newest first."""
    return sorted((root / RECEIPTS_DIR).glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)


def latest_recording(root: Path) -> Path | None:
    files = recordings(root)
    return files[0] if files else None


def session_name(events: list[dict[str, Any]]) -> str | None:
    """The session's latest name: from /name, else --name, else None."""
    for event in reversed(events):
        if event["type"] in {"rename", "session_start"} and event.get("name"):
            return str(event["name"])
    return None


def display_name(events: list[dict[str, Any]]) -> str:
    """What `receipts list` and the receipt page call a session: its name, else its first message."""
    first_message = next((str(e.get("text", "")) for e in events if e["type"] == "user_message"), "")
    return session_name(events) or first_message[:80] or "Untitled session"


def find_recording(root: Path, query: str) -> Path | None:
    """A recording by path, or the newest one whose file name or display name contains `query`.

    An empty query means the newest recording.
    """
    if query and Path(query).is_file():
        return Path(query)
    files = recordings(root)
    if not query:
        return files[0] if files else None
    wanted = slugify(query)
    for file in files:
        if wanted in file.stem or wanted in slugify(display_name(load_events(file))):
            return file
    return None


class Recorder:
    def __init__(self, path: Path, elapsed_offset_ms: int = 0) -> None:
        """`elapsed_offset_ms` continues a resumed recording's clock where it stopped."""
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.name == RECEIPTS_DIR:
            # Recordings can contain file contents and command output: keep them out of git by default.
            gitignore = path.parent / ".gitignore"
            if not gitignore.exists():
                gitignore.write_text("*\n", encoding="utf-8")
        self._file: TextIO = path.open("a", encoding="utf-8")
        self._start = time.monotonic() - elapsed_offset_ms / 1000

    def event(self, type: str, **fields: Any) -> None:
        record = {
            "type": type,
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "elapsed_ms": round((time.monotonic() - self._start) * 1000),
            **fields,
        }
        self._file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        self._file.flush()

    def close(self) -> None:
        if not self._file.closed:
            self._file.close()

    def wrap_confirm(self, confirm: Callable[[str], bool]) -> Callable[[str], bool]:
        """Record the user's answer every time they approve or decline a command."""

        def recorded_confirm(command: str) -> bool:
            approved = confirm(command)
            self.event("approval", command=command, approved=approved)
            return approved

        return recorded_confirm


def content_summary(block: Any) -> dict[str, Any]:
    """A JSON-friendly view of one response content block."""
    if block.type == "text":
        return {"type": "text", "text": block.text}
    if block.type == "thinking":
        return {"type": "thinking", "thinking": getattr(block, "thinking", "")}
    if block.type == "tool_use":
        return {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
    if block.type == "fallback":
        return {"type": "fallback", "from": block.from_.model, "to": block.to.model}
    return {"type": block.type}
