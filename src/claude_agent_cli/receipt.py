"""`receipts`: turn a recorded session (JSONL) into a self-contained HTML replay page.

Secrets and local paths are redacted here, before anything is rendered: the output is meant to be shared.
The page itself is built by `receipt_page`.
"""

from __future__ import annotations

import argparse
import re
import sys
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .receipt_page import render
from .recorder import RECEIPTS_DIR, display_name, latest_recording, load_events, recordings

# Best-effort secret patterns. Each match is replaced, keeping group 1 (e.g. "API_KEY=") when there is one.
SECRET_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),  # Anthropic, OpenAI, OpenRouter
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google API key
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),  # Slack
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
    re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/-]{16,}=*"),
    # KEY=value lines in .env files, also as read_file shows them ("   3  API_KEY=...").
    re.compile(
        r"(?im)^(\s*\d*\s*(?:export\s+)?[A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)[A-Z0-9_]*"
        r"\s*[=:]\s*)(?!\[redacted\]$)\S.*$"  # skip values an earlier pattern already redacted
    ),
    # "api_key": "value" in JSON, YAML or code.
    re.compile(r"""(?i)(["']?[\w-]*(?:api[_-]?key|token|secret|password)["']?\s*[:=]\s*["'])[^"'\s]{6,}(?=["'])"""),
]
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
REDACTED = "[redacted]"


@dataclass
class Redactor:
    """Removes secrets, e-mail addresses and local paths from everything that goes on the page."""

    root: str | None = None
    count: int = 0
    _paths: list[tuple[re.Pattern[str], str]] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Longest first, so the project root wins over the home folder that contains it.
        for path, label in sorted([(self.root, "<project>"), (str(Path.home()), "~")], key=lambda p: -len(p[0] or "")):
            if path:
                variants = {path, path.replace("\\", "/"), path.replace("\\", "\\\\")}
                for variant in variants:
                    self._paths.append((re.compile(re.escape(variant), re.IGNORECASE), label))

    def text(self, value: str) -> str:
        for pattern in SECRET_PATTERNS:
            value, n = pattern.subn(lambda m: (m.group(1) if m.lastindex else "") + REDACTED, value)
            self.count += n
        value, n = EMAIL.subn("[email]", value)
        self.count += n
        for pattern, label in self._paths:
            value = pattern.sub(label, value)
        return value

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(v) for v in value]
        if isinstance(value, dict):
            return {k: self.value(v) for k, v in value.items()}
        return value


# ---------------------------------------------------------------------------------------------- command line


def share(path: Path, output: Path | None, open_browser: bool) -> int:
    events = load_events(path)
    if not events:
        print(f"{path} has no recorded events.", file=sys.stderr)
        return 1
    start = next((e for e in events if e["type"] == "session_start"), {})
    redactor = Redactor(root=start.get("root"))
    redacted = [redactor.value(e) for e in events]
    page = render(redacted, redacted=redactor.count)

    output = output or path.with_suffix(".html")
    output.write_text(page, encoding="utf-8")
    print(f"Receipt written to {output}")
    print(
        f"Redacted {redactor.count} secret{'s' if redactor.count != 1 else ''} or e-mail address(es); "
        "local paths hidden."
    )
    print("Redaction is best-effort: skim the page before you send it to anyone.")
    if open_browser:
        webbrowser.open(output.resolve().as_uri())
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="receipts", description="Turn recorded agent sessions into shareable pages.")
    commands = parser.add_subparsers(dest="command", required=True)

    share_cmd = commands.add_parser("share", help="Render a recording as a redacted HTML replay page")
    share_cmd.add_argument("file", nargs="?", type=Path, help=f"Recording (default: newest in ./{RECEIPTS_DIR})")
    share_cmd.add_argument("-o", "--output", type=Path, help="Where to write the page (default: next to the recording)")
    share_cmd.add_argument("--no-open", action="store_true", help="Don't open the page in the browser")

    commands.add_parser("list", help=f"List recordings in ./{RECEIPTS_DIR}")
    args = parser.parse_args(argv)

    if args.command == "list":
        files = recordings(Path.cwd())
        if not files:
            print(f"No recordings in {Path.cwd() / RECEIPTS_DIR}. Record one with `claude-agent --record`.")
        for file in files:
            print(f"{display_name(load_events(file))[:60]:<62} {file}")
        return 0

    path = args.file or latest_recording(Path.cwd())
    if path is None:
        print(f"No recordings in {Path.cwd() / RECEIPTS_DIR}. Pass a file, or record one with `claude-agent --record`.")
        return 1
    return share(path, args.output, open_browser=not args.no_open)


if __name__ == "__main__":
    sys.exit(main())
