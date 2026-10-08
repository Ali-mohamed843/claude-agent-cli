"""A small, safe Markdown renderer for Claude's answers on the receipt page.

All text is HTML-escaped before any Markdown is turned into tags, so no raw HTML from a recording can reach
the page. Links are only kept for http(s) and mailto URLs.

Supported: fenced code, headings, paragraphs, bullet and numbered lists (one level of nesting), block quotes,
horizontal rules, pipe tables, inline code, bold, italic, strikethrough, links and bare URLs.
"""

from __future__ import annotations

import re
from html import escape, unescape

FENCE = re.compile(r"^\s*(```|~~~)\s*([\w+#.-]*)\s*$")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
LIST_ITEM = re.compile(r"^(\s*)(?:([-*+])|(\d{1,9})[.)])\s+(.*)$")
RULE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
QUOTE = re.compile(r"^\s*>\s?(.*)$")
TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
SAFE_URL = re.compile(r"(?i)^(?:https?://|mailto:)")
MAX_QUOTE_DEPTH = 3


def render_markdown(text: str, _depth: int = 0) -> str:
    lines = text.replace("\x00", "").replace("\r\n", "\n").split("\n")
    out: list[str] = []
    paragraph: list[str] = []
    i = 0

    def flush_paragraph() -> None:
        if paragraph:
            out.append(f"<p>{'<br>'.join(inline(line) for line in paragraph)}</p>")
            paragraph.clear()

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            flush_paragraph()
            i += 1
        elif fence := FENCE.match(line):
            flush_paragraph()
            marker, language = fence.group(1), fence.group(2)
            code: list[str] = []
            i += 1
            while i < len(lines) and lines[i].strip() != marker:
                code.append(lines[i])
                i += 1
            i += 1  # closing fence (or end of text)
            label = f'<span class="lang">{escape(language)}</span>' if language else ""
            out.append(f'<div class="codeblock">{label}<pre><code>{escape(chr(10).join(code))}</code></pre></div>')
        elif heading := HEADING.match(line):
            flush_paragraph()
            level = min(len(heading.group(1)) + 2, 6)  # answers sit under the page's h1 and the turn's h2
            out.append(f"<h{level}>{inline(heading.group(2))}</h{level}>")
            i += 1
        elif RULE.match(line):
            flush_paragraph()
            out.append("<hr>")
            i += 1
        elif QUOTE.match(line):
            flush_paragraph()
            quoted = []
            while i < len(lines) and (match := QUOTE.match(lines[i])):
                quoted.append(match.group(1))
                i += 1
            inner = (
                render_markdown("\n".join(quoted), _depth + 1)
                if _depth < MAX_QUOTE_DEPTH
                else f"<p>{inline(' '.join(quoted))}</p>"
            )
            out.append(f"<blockquote>{inner}</blockquote>")
        elif "|" in line and i + 1 < len(lines) and TABLE_SEPARATOR.match(lines[i + 1]):
            flush_paragraph()
            rows = [line]
            alignments = column_alignments(lines[i + 1])
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(lines[i])
                i += 1
            out.append(render_table(rows, alignments))
        elif LIST_ITEM.match(line):
            flush_paragraph()
            items: list[tuple[int, bool, list[str]]] = []  # (nesting level, ordered, lines)
            while i < len(lines):
                match = LIST_ITEM.match(lines[i])
                if match:
                    level = 1 if len(match.group(1).expandtabs(4)) >= 2 and items else 0
                    items.append((level, match.group(3) is not None, [match.group(4)]))
                elif lines[i].startswith(("  ", "\t")) and lines[i].strip() and items:
                    items[-1][2].append(lines[i].strip())  # continuation of the previous item
                else:
                    break
                i += 1
            out.append(render_list(items))
        else:
            paragraph.append(line.strip())
            i += 1
    flush_paragraph()
    return "\n".join(out)


def render_list(items: list[tuple[int, bool, list[str]]]) -> str:
    out: list[str] = []
    stack: list[str] = []  # open list tags, outermost first
    for level, ordered, item_lines in items:
        tag = "ol" if ordered else "ul"
        while len(stack) > level + 1:
            out.append(f"</li></{stack.pop()}>")
        if len(stack) == level + 1:
            if stack[-1] != tag:  # a different kind of list starts at this level
                out.append(f"</li></{stack.pop()}><{tag}>")
                stack.append(tag)
            else:
                out.append("</li>")
        else:
            out.append(f"<{tag}>")
            stack.append(tag)
        out.append(f"<li>{'<br>'.join(inline(text) for text in item_lines)}")
    while stack:
        out.append(f"</li></{stack.pop()}>")
    return "".join(out)


def split_row(row: str) -> list[str]:
    row = row.strip()
    row = row[1:] if row.startswith("|") else row
    row = row[:-1] if row.endswith("|") and not row.endswith("\\|") else row
    return [cell.strip().replace("\\|", "|") for cell in re.split(r"(?<!\\)\|", row)]


def column_alignments(separator: str) -> list[str]:
    alignments = []
    for cell in split_row(separator):
        left, right = cell.startswith(":"), cell.endswith(":")
        alignments.append("center" if left and right else "right" if right else "left")
    return alignments


def render_table(rows: list[str], alignments: list[str]) -> str:
    header, *body = (split_row(row) for row in rows)
    width = len(header)

    def cells(row: list[str], tag: str) -> str:
        row = (row + [""] * width)[:width]
        aligned = [
            f' class="{alignments[i]}"' if i < len(alignments) and alignments[i] != "left" else "" for i in range(width)
        ]
        return "".join(f"<{tag}{aligned[i]}>{inline(cell)}</{tag}>" for i, cell in enumerate(row))

    head = f"<thead><tr>{cells(header, 'th')}</tr></thead>"
    rows_html = "".join(f"<tr>{cells(row, 'td')}</tr>" for row in body)
    return f'<div class="table"><table>{head}<tbody>{rows_html}</tbody></table></div>'


def inline(text: str) -> str:
    """Inline Markdown on one line of text. Escapes first, then adds a fixed set of tags."""
    spans: list[str] = []

    def keep(html: str) -> str:
        spans.append(html)
        return f"\x00{len(spans) - 1}\x00"

    # Code spans first: nothing inside them is Markdown.
    text = re.sub(r"`([^`\n]+)`", lambda m: keep(f"<code>{escape(m.group(1))}</code>"), text)
    text = escape(text)

    def link(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        if not SAFE_URL.match(unescape(url)):
            return match.group(0)
        return keep(f'<a href="{url}" rel="nofollow noopener noreferrer">{label}</a>')

    text = re.sub(r"\[([^\]\n]+)\]\((\S+?)\)", link, text)

    def bare_url(match: re.Match[str]) -> str:
        url = match.group(0)
        trailing = re.search(r"[.,;:!?)\]]+$", url)
        tail = trailing.group(0) if trailing else ""
        url = url[: len(url) - len(tail)]
        return keep(f'<a href="{url}" rel="nofollow noopener noreferrer">{url}</a>') + tail

    text = re.sub(r"\bhttps?://[^\s<>\x00]+", bare_url, text)
    text = re.sub(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![*\w])", r"<em>\1</em>", text)
    text = re.sub(r"(?<!\w)_(?=[^\s_])(.+?)(?<=[^\s_])_(?!\w)", r"<em>\1</em>", text)
    text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"<del>\1</del>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: spans[int(m.group(1))], text)
