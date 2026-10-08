"""Render a (redacted) recording as the receipt page: a receipt-style summary beside a step-by-step replay.

Everything is rendered here with every recorded value HTML-escaped (Claude's answers go through the safe
Markdown renderer), so the page needs no JavaScript and loads nothing from the network.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from importlib.resources import files
from string import Template
from typing import Any

from .minimarkdown import render_markdown
from .recorder import display_name

# ---------------------------------------------------------------------------------------------- formatting


def fmt_cost(cost: float | None) -> str:
    if cost is None:
        return "unknown"
    return f"${cost:.4f}" if cost < 1 else f"${cost:.2f}"


def fmt_clock(ms: int) -> str:
    seconds = ms // 1000
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def fmt_duration(ms: int) -> str:
    if ms < 1000:
        return f"{ms} ms"
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f} s"
    if seconds < 3600:
        return f"{int(seconds // 60)} min {int(seconds % 60)} s"
    return f"{int(seconds // 3600)} h {int(seconds // 60 % 60)} min"


def plural(n: int, word: str, words: str | None = None) -> str:
    return f"{n:,} {word if n == 1 else (words or word + 's')}"


def total_tokens(usage: dict[str, int]) -> int:
    return sum(usage.get(k, 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))


def shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


ICONS = {
    "read_file": (
        '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h4"/>'
    ),
    "search_files": '<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2"/>',
    "run_command": '<rect x="3" y="4.5" width="18" height="15" rx="2"/><path d="m7 9.5 3 2.5-3 2.5M12.5 15H17"/>',
    "tool": '<path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L4 17l3 3 5.3-5.3a4 4 0 0 0 5.4-5.4l-2.5 2.5-2.4-.6-.6-2.4z"/>',
    "thinking": (
        '<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.5 10.9c.6.5 1 1.2 1 2.1h5c0-.9.4-1.6 1-2.1A6 6 0 0 0 12 3z"/>'
    ),
    "note": '<circle cx="12" cy="12" r="9"/><path d="M12 8v5M12 16.5v.5"/>',
    "user": '<circle cx="12" cy="8" r="4"/><path d="M4.5 20.5a7.5 7.5 0 0 1 15 0"/>',
    "lock": '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
}
CLAUDE_MARK = (
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 2.5c.5 0 .9.4 1 .9l.9 5.3 '
    "4.3-3.2a1 1 0 0 1 1.4 1.4l-3.2 4.3 5.3.9a1 1 0 0 1 0 2l-5.3.9 3.2 4.3a1 1 0 0 1-1.4 1.4l-4.3-3.2-.9 5.3a1 1 0 "
    "0 1-2 0l-.9-5.3-4.3 3.2a1 1 0 0 1-1.4-1.4l3.2-4.3-5.3-.9a1 1 0 0 1 0-2l5.3-.9-3.2-4.3a1 1 0 0 1 1.4-1.4l4.3 "
    '3.2.9-5.3c.1-.5.5-.9 1-.9z"/></svg>'
)


def icon(name: str) -> str:
    return (
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
        f'stroke-linejoin="round" aria-hidden="true">{ICONS.get(name, ICONS["tool"])}</svg>'
    )


# ---------------------------------------------------------------------------------------------- structure


@dataclass
class Turn:
    number: int
    question: str
    start_ms: int
    events: list[dict[str, Any]] = field(default_factory=list)
    end: dict[str, Any] | None = None

    @property
    def status(self) -> str:
        if self.end is None:
            return "open"
        if self.end.get("status") == "ok":
            return "ok"
        return "interrupted" if self.end.get("error") == "KeyboardInterrupt" else "failed"

    @property
    def duration_ms(self) -> int | None:
        return None if self.end is None else max(0, self.end.get("elapsed_ms", 0) - self.start_ms)

    @property
    def tool_calls(self) -> int:
        return sum(e["type"] == "tool_result" for e in self.events)


@dataclass
class Marker:
    """A line between turns: reset, resume, rename."""

    kind: str
    text: str


def group(events: list[dict[str, Any]]) -> list[Turn | Marker]:
    items: list[Turn | Marker] = []
    current: Turn | None = None
    number = 0
    for e in events:
        kind = e["type"]
        if kind == "user_message":
            number += 1
            current = Turn(number, str(e.get("text", "")), e.get("elapsed_ms", 0))
            items.append(current)
        elif kind in {"model_response", "tool_result", "approval"} and current is not None:
            current.events.append(e)
        elif kind == "turn_end" and current is not None:
            current.end = e
            current = None
        elif kind == "reset":
            current = None
            items.append(Marker("reset", "Conversation reset — Claude starts fresh from here"))
        elif kind == "session_resume":
            current = None
            model = f" on {e['model']}" if e.get("model") else ""
            items.append(Marker("resume", f"Session resumed{model}"))
        elif kind == "rename":
            items.append(Marker("rename", f"Renamed to “{e.get('name', '')}”"))
    return items


@dataclass
class Summary:
    name: str
    agent: str = "claude-agent-cli"
    model: str = ""
    started: str = ""
    duration_ms: int = 0
    turns: int = 0
    failed_turns: int = 0
    tool_calls: int = 0
    files_read: int = 0
    searches: int = 0
    approved: int = 0
    declined: int = 0
    tokens: int = 0
    cost: float | None = 0.0


def summarize(events: list[dict[str, Any]]) -> Summary:
    start = next((e for e in events if e["type"] == "session_start"), {})
    s = Summary(name=display_name(events), agent=start.get("agent", "claude-agent-cli"), model=start.get("model", ""))
    s.started = start.get("ts", events[0].get("ts", "") if events else "")
    s.duration_ms = max((e.get("elapsed_ms", 0) for e in events), default=0)
    for e in events:
        if e["type"] == "turn_end":
            s.turns += 1
            s.failed_turns += e.get("status") != "ok"
            s.tokens += total_tokens(e.get("usage", {}))
            cost = e.get("cost_usd")
            s.cost = None if cost is None or s.cost is None else s.cost + cost
        elif e["type"] == "tool_result":
            s.tool_calls += 1
            name = e.get("name")
            s.files_read += name == "read_file"
            s.searches += name == "search_files"
        elif e["type"] == "approval":
            s.approved += bool(e.get("approved"))
            s.declined += not e.get("approved")
    return s


# ---------------------------------------------------------------------------------------------- rendering


def render_turn(turn: Turn) -> str:
    results = {e["id"]: e for e in turn.events if e["type"] == "tool_result" and "id" in e}
    approvals = [e for e in turn.events if e["type"] == "approval"]
    responses = [e for e in turn.events if e["type"] == "model_response"]
    final = responses[-1] if responses and turn.status == "ok" else None

    steps: list[str] = []
    answer: list[str] = []
    for response in responses:
        for block in response.get("content", []):
            kind = block.get("type")
            if kind == "text" and response is final:
                answer.append(block.get("text", ""))
            elif kind == "text" and block.get("text"):
                steps.append(
                    f'<li class="step narration"><span class="node">{CLAUDE_MARK}</span>'
                    f'<div class="md" dir="auto">{render_markdown(block["text"])}</div></li>'
                )
            elif kind == "thinking" and block.get("thinking"):
                steps.append(
                    f'<li class="step thinking"><span class="node">{icon("thinking")}</span>'
                    '<details><summary><span class="step-title">Thinking</span>'
                    f'<span class="chev" aria-hidden="true"></span></summary>'
                    f'<div class="thought" dir="auto">{escape(block["thinking"])}</div></details></li>'
                )
            elif kind == "tool_use":
                steps.append(render_tool(block, results, approvals))
            elif kind == "fallback":
                steps.append(
                    f'<li class="step note"><span class="node">{icon("note")}</span><p>'
                    f"{escape(block.get('from', ''))} declined this request; it continued on "
                    f"{escape(block.get('to', ''))}.</p></li>"
                )

    meta = [f'<span title="Time since the session started">at {fmt_clock(turn.start_ms)}</span>']
    if turn.duration_ms is not None:
        meta.append(f"<span>took {fmt_duration(turn.duration_ms)}</span>")
    if turn.tool_calls:
        meta.append(f"<span>{plural(turn.tool_calls, 'tool call')}</span>")
    chip = {
        "ok": "",
        "failed": '<span class="chip bad">Failed</span>',
        "interrupted": '<span class="chip warn">Interrupted</span>',
        "open": '<span class="chip warn">Unfinished</span>',
    }[turn.status]

    parts = [
        f'<section class="turn" id="turn-{turn.number}" aria-labelledby="turn-{turn.number}-title">',
        '<header class="turn-head">',
        f'<h2 id="turn-{turn.number}-title"><a href="#turn-{turn.number}">Turn {turn.number}</a></h2>',
        f'<div class="turn-meta">{"".join(meta)}</div>{chip}</header>',
        f'<div class="msg user"><span class="avatar">{icon("user")}</span><div class="msg-body">',
        f'<div class="who">You</div><div class="text" dir="auto">{escape(turn.question)}</div></div></div>',
    ]
    if steps:
        parts.append(f'<ol class="steps">{"".join(steps)}</ol>')
    if answer:
        parts.append(
            f'<div class="msg answer"><span class="avatar claude">{CLAUDE_MARK}</span><div class="msg-body">'
            f'<div class="who">Claude</div><div class="md" dir="auto">{render_markdown(chr(10).join(answer))}</div>'
            "</div></div>"
        )
    parts.append(render_turn_end(turn))
    parts.append("</section>")
    return "".join(parts)


def render_turn_end(turn: Turn) -> str:
    if turn.end is None:
        return (
            '<div class="banner warn">This turn has no ending in the recording: the agent was closed '
            "or is still working on it.</div>"
        )
    usage = total_tokens(turn.end.get("usage", {}))
    cost = f"{plural(usage, 'token')} · {fmt_cost(turn.end.get('cost_usd'))}"
    if turn.status == "ok":
        return f'<footer class="turn-foot">{cost}</footer>'
    if turn.status == "interrupted":
        reason = "You stopped this turn with Ctrl+C."
    else:
        message = str(turn.end.get("message", ""))
        error = str(turn.end.get("error", "Error"))
        reason = f"{error}: {shorten(message, 300)}" if message else error
    return (
        f'<div class="banner {"warn" if turn.status == "interrupted" else "bad"}"><strong>No answer.</strong> '
        f"{escape(reason)} The agent rolled this turn back, so Claude never saw it again.</div>"
        f'<footer class="turn-foot">{cost}</footer>'
    )


def render_tool(block: dict[str, Any], results: dict[str, dict[str, Any]], approvals: list[dict[str, Any]]) -> str:
    name = str(block.get("name", ""))
    raw_input = block.get("input")
    args: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
    result = results.get(str(block.get("id", "")))
    output = result.get("content") if result else None
    output = output if isinstance(output, str) else None
    error = bool(result and result.get("is_error"))

    badges: list[str] = []
    title = {"read_file": "Read", "search_files": "Searched", "run_command": "Ran"}.get(name, name)
    if name == "read_file":
        target = f'<code class="target">{escape(str(args.get("path", "")))}</code>'
    elif name == "search_files":
        where = (
            f' <span class="dim">in</span> <code class="target">{escape(args["glob"])}</code>'
            if args.get("glob")
            else ""
        )
        target = f'<code class="target">{escape(str(args.get("pattern", "")))}</code>{where}'
    elif name == "run_command":
        target = f'<code class="target cmd">{escape(str(args.get("command", "")))}</code>'
        approval = next((a for a in approvals if a.get("command") == args.get("command")), None)
        if approval:
            approvals.remove(approval)  # each approval belongs to exactly one call
            if approval.get("approved"):
                badges.append('<span class="chip ok">Approved</span>')
            else:
                title = "Wanted to run"
                badges.append('<span class="chip bad">You declined</span>')
        if output and (exit_line := re.match(r"exit code: (-?\d+)\n?", output)):
            code = int(exit_line.group(1))
            badges.append(f'<span class="chip {"ok" if code == 0 else "bad"}">exit {code}</span>')
            output = output[exit_line.end() :]
    else:
        target = f'<code class="target">{escape(json.dumps(args, ensure_ascii=False))}</code>'

    if result is None:
        # The turn ended (Ctrl+C, crash) before the tool finished, or before it started.
        title = {"read_file": "Was reading", "search_files": "Was searching", "run_command": "Asked to run"}.get(
            name, name
        )
        badges.append('<span class="chip warn">No result</span>')
    elif error and not any("declined" in b for b in badges):
        badges.append('<span class="chip bad">Error</span>')
    if result and "duration_ms" in result:
        badges.append(f'<span class="dim">{fmt_duration(result["duration_ms"])}</span>')

    head = f'<span class="step-title">{escape(title)}</span>{target}<span class="step-meta">{"".join(badges)}</span>'
    css = f"step tool{' is-error' if error else ''}"
    node = f'<span class="node">{icon(name)}</span>'
    if output is None or (error and output == "The user declined to run this command."):
        return f'<li class="{css}">{node}<div class="tool-head">{head}</div></li>'
    lines = output.count("\n") + 1 if output.strip() else 0
    body = f"<pre>{escape(output)}</pre>" if lines else '<p class="empty">No output</p>'
    size = plural(lines, "line") if lines else "empty"
    return (
        f'<li class="{css}">{node}<details><summary class="tool-head">{head}'
        f'<span class="size">{size}</span><span class="chev" aria-hidden="true"></span></summary>'
        f"{body}</details></li>"
    )


def render_marker(marker: Marker) -> str:
    if marker.kind == "rename":
        return f'<p class="marker-note">{escape(marker.text)}</p>'
    return f'<div class="marker {marker.kind}"><span>{escape(marker.text)}</span></div>'


def render_receipt(s: Summary, turns: list[Turn], redacted: int | None) -> str:
    rows = [("Turns", f"{s.turns:,}" + (f" ({s.failed_turns} unanswered)" if s.failed_turns else ""))]
    if s.files_read:
        rows.append(("Files read", f"{s.files_read:,}"))
    if s.searches:
        rows.append(("Searches", f"{s.searches:,}"))
    if s.approved or s.declined:
        rows.append(("Commands approved", f"{s.approved:,}"))
        rows.append(("Commands declined", f"{s.declined:,}"))
    rows += [("Tokens", f"{s.tokens:,}"), ("Duration", fmt_duration(s.duration_ms))]
    lines = "".join(f"<div><dt>{escape(k)}</dt><dd>{escape(v)}</dd></div>" for k, v in rows)

    toc = ""
    if turns:
        entries = "".join(
            f'<li class="{t.status}"><a href="#turn-{t.number}"><span class="n">{t.number}</span>'
            f'<span class="q" dir="auto">{escape(shorten(t.question, 70))}</span></a></li>'
            for t in turns
        )
        toc = f'<nav class="toc" aria-label="Turns"><h2>Turns</h2><ol>{entries}</ol></nav>'

    redaction = "Secrets and local paths redacted"
    if redacted:
        redaction = f"{plural(redacted, 'secret')} redacted · local paths hidden"
    return (
        f'<dl class="lines">{lines}</dl>'
        f'<div class="total"><span>Total</span><span>{escape(fmt_cost(s.cost))}</span></div>'
        f'{toc}<p class="sealed">{icon("lock")}<span>{escape(redaction)}</span></p>'
    )


PAGE = Template(files(__package__).joinpath("receipt_template.html").read_text(encoding="utf-8"))


def render(events: list[dict[str, Any]], redacted: int | None = None) -> str:
    s = summarize(events)
    try:
        started = datetime.fromisoformat(s.started).astimezone().strftime("%d %b %Y · %H:%M")
    except ValueError:
        started = ""
    items = group(events)
    turns = [item for item in items if isinstance(item, Turn)]
    timeline = "\n".join(render_turn(i) if isinstance(i, Turn) else render_marker(i) for i in items)
    description = (
        f"{plural(s.turns, 'turn')} · {plural(s.tool_calls, 'tool call')} · {fmt_cost(s.cost)} — "
        f"an agent session recorded with {s.agent}"
    )
    return PAGE.substitute(
        title=escape(s.name),
        description=escape(description),
        agent=escape(s.agent),
        model=escape(s.model or "unknown model"),
        started=escape(started),
        receipt=render_receipt(s, turns, redacted),
        timeline=timeline or '<p class="empty-session">This recording has no messages yet.</p>',
        generated=escape(datetime.now().astimezone().strftime("%d %b %Y")),
    )
