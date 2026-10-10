"""`receipts`: turn a recorded session (JSONL) into a self-contained HTML replay page, and verify its seal.

Secrets and local paths are redacted here, before anything is rendered: the output is meant to be shared.
The page itself is built by `receipt_page`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import proof
from .proof import Entry, Report, Signer, bundle, entries_from_bundle, line_hash, split_lines, verify
from .receipt_page import Seal, fmt_cost, plural, render, summarize
from .recorder import RECEIPTS_DIR, display_name, latest_recording, load_events, recordings
from .style import paint
from .ui import notice

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


def redact_recording(lines: list[str], root: str | None = None) -> tuple[list[dict[str, Any]], list[Entry], int]:
    """Redact a recording for sharing. Returns the events to show, the proof entries, and how much was redacted.

    A line that redaction changed goes into the proof as its hash only, so the page never carries the original
    text, yet the seals still cover it. Seal lines hold no secrets and always stay in full, so they can be checked.
    """
    parsed: list[dict[str, Any] | None] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            event = None
        parsed.append(event if isinstance(event, dict) and "type" in event else None)
    start = next((e for e in parsed if e and e["type"] == "session_start"), {})
    redactor = Redactor(root=start.get("root") or root)  # older recordings stored the root path

    shown: list[dict[str, Any]] = []
    entries: list[Entry] = []
    for line, event in zip(lines, parsed, strict=True):
        if event is None:
            entries.append(Entry(line_hash(line)))  # a damaged line: keep it in the chain, but don't show it
            continue
        if event["type"] == "seal":
            entries.append(Entry.of(line))
            continue
        clean = redactor.value(event)
        shown.append(clean)
        entries.append(Entry.of(line) if clean == event else Entry(line_hash(line)))
    return shown, entries, redactor.count


def share(path: Path, output: Path | None, open_browser: bool) -> int:
    lines = split_lines(path.read_text(encoding="utf-8"))
    root = str(path.resolve().parent.parent) if path.resolve().parent.name == RECEIPTS_DIR else None
    events, entries, redactions = redact_recording(lines, root)
    if not events:
        print(f"{path} has no recorded events.", file=sys.stderr)
        return 1
    output = output or path.with_suffix(".html")
    seal = Seal(report=verify(entries), bundle=bundle(entries), filename=output.name)
    output.write_text(render(events, redacted=redactions, seal=seal), encoding="utf-8")

    print(notice(f"Receipt written to {output}", "ok"))
    print(notice(f"Redacted {plural(redactions, 'secret')} or e-mail address(es); local paths hidden."))
    for line in describe(seal.report, mine=_my_fingerprint()):
        print(line)
    print(notice(paint("Redaction is best-effort: skim the page before you send it to anyone.", "gray")))
    if open_browser:
        webbrowser.open(output.resolve().as_uri())
    return 0


# ---------------------------------------------------------------------------------------------- verify and key


PROOF_IN_PAGE = re.compile(r'<script type="application/json" id="receipt-proof">(.*?)</script>', re.DOTALL)


def load_entries(path: Path) -> list[Entry]:
    """Proof entries from a recording (.jsonl) or a shared receipt page (.html)."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".html", ".htm"}:
        match = PROOF_IN_PAGE.search(text)
        if match is None:
            raise ValueError("This page has no proof in it (it was made before sealing existed, or edited).")
        return entries_from_bundle(json.loads(match.group(1).replace("<\\/", "</")))
    return [Entry.of(line) for line in split_lines(text)]


def describe(report: Report, mine: str | None) -> list[str]:
    """The verdict in plain words, one line per fact."""
    lines = []
    if report.problems:
        lines.append(notice(paint("Not trustworthy: this was edited after it was sealed.", "red", "bold"), "error"))
        lines += [notice(problem) for problem in report.problems]
        if report.sealed_through >= 0:
            lines.append(notice(f"Lines 1–{report.sealed_through + 1} are still intact."))
        return lines
    if report.seals == 0:
        return [notice("Not sealed: there's no proof either way (recorded without a signing key).", "warn")]
    lines.append(notice(paint("Verified: nothing was added, removed or changed after sealing.", "green", "bold"), "ok"))
    lines.append(notice(f"{plural(report.events, 'line')} · {plural(report.seals, 'seal')}"))
    for key in report.keys:
        whose = paint("  ← your key", "accent") if key == mine else ""
        lines.append(notice(f"Signed by key {paint(key, 'bold')}{whose}"))
    if report.redacted:
        lines.append(notice(f"{plural(report.redacted, 'line')} redacted in this copy: hidden, but still covered."))
    if report.unsealed_tail:
        lines.append(
            notice(
                f"The last {plural(report.unsealed_tail, 'line')} came after the final seal, so they aren't covered.",
                "warn",
            )
        )
    return lines


def verify_file(path: Path) -> int:
    try:
        entries = load_entries(path)
    except (OSError, ValueError) as e:  # json.JSONDecodeError is a ValueError
        print(notice(f"Can't read the proof in {path}: {e}", "error"))
        return 1
    report = verify(entries)
    print(notice(f"Checking {path.name}"))
    for line in describe(report, mine=_my_fingerprint()):
        print(line)
    if report.ok:
        # The page's HTML is only a view; these facts come from the sealed data itself.
        s = summarize([e for e in (_parse(entry.line) for entry in entries if entry.line) if e])
        facts = f"{plural(s.turns, 'turn')} · {plural(s.tool_calls, 'tool call')} · {fmt_cost(s.cost)}"
        print(notice(f"The sealed record: “{s.name}” · {s.model or 'unknown model'} · {facts}"))
    return 0 if report.ok else 1


def _parse(line: str) -> dict[str, Any] | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    return event if isinstance(event, dict) and "type" in event else None


def show_key() -> int:
    created = Signer.load() is None
    signer = Signer.load_or_create()
    if created:
        print(notice("Created your signing key.", "ok"))
    print(notice(f"Fingerprint  {paint(signer.fingerprint, 'accent', 'bold')}"))
    print(notice(f"Public key   {signer.public_key}"))
    print(notice(f"Stored in    {proof.KEY_FILE}"))
    print(
        notice(paint("Publish the fingerprint (e.g. in your GitHub profile) so people can tell your receipts", "gray"))
    )
    print(notice(paint("are yours. Never share the key file itself.", "gray")))
    return 0


def _my_fingerprint() -> str | None:
    try:
        signer = Signer.load()
    except (OSError, ValueError):
        return None
    return signer.fingerprint if signer else None


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
    verify_cmd = commands.add_parser(
        "verify", help="Check that a recording or receipt page wasn't edited after sealing"
    )
    verify_cmd.add_argument("file", type=Path, help="A recording (.jsonl) or a receipt page (.html)")
    commands.add_parser("key", help="Show your signing key's fingerprint (creates the key if needed)")
    args = parser.parse_args(argv)

    if args.command == "verify":
        return verify_file(args.file)
    if args.command == "key":
        return show_key()
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
