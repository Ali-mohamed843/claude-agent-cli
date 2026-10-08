"""Tests for the safe Markdown renderer used for Claude's answers."""

from __future__ import annotations

from claude_agent_cli.minimarkdown import inline, render_markdown


def test_raw_html_is_escaped() -> None:
    html = render_markdown('<img src=x onerror="alert(1)"> **<b>bold</b>**')
    assert "<img" not in html and "<b>" not in html
    assert "&lt;img" in html
    assert "<strong>&lt;b&gt;bold&lt;/b&gt;</strong>" in html


def test_only_safe_links() -> None:
    assert '<a href="https://anthropic.com"' in inline("[Anthropic](https://anthropic.com)")
    assert "<a" not in inline("[click](javascript:alert(1))")
    assert "<a" not in inline("[click](data:text/html,hi)")


def test_link_cannot_break_out_of_the_attribute() -> None:
    html = inline('[x](https://a.com/"onmouseover="alert(1))')
    assert 'href="https://a.com/&quot;onmouseover=&quot;alert(1"' in html


def test_bare_urls_become_links_without_trailing_punctuation() -> None:
    html = inline("See https://www.anthropic.com/careers.")
    assert '<a href="https://www.anthropic.com/careers" rel="nofollow noopener noreferrer">' in html
    assert html.endswith("</a>.")


def test_code_spans_are_not_formatted() -> None:
    assert inline("`**not bold**` and **bold**") == "<code>**not bold**</code> and <strong>bold</strong>"


def test_inline_styles() -> None:
    assert inline("*a* _b_ ~~c~~ __d__") == "<em>a</em> <em>b</em> <del>c</del> <strong>d</strong>"
    assert inline("snake_case_name and 2 * 3 * 4") == "snake_case_name and 2 * 3 * 4"


def test_headings_shift_below_the_page_headings() -> None:
    assert render_markdown("# Title") == "<h3>Title</h3>"
    assert render_markdown("### Small") == "<h5>Small</h5>"


def test_fenced_code_keeps_its_content() -> None:
    html = render_markdown("```python\nif a < b:\n    print('**hi**')\n```")
    assert '<span class="lang">python</span>' in html
    assert "if a &lt; b:\n    print(&#x27;**hi**&#x27;)" in html
    assert "<strong>" not in html


def test_unclosed_fence_runs_to_the_end() -> None:
    assert "still code" in render_markdown("```\nstill code")


def test_lists_with_nesting_and_numbers() -> None:
    html = render_markdown("- one\n  - nested\n- two\n\n1. first\n2. second")
    assert html.startswith("<ul><li>one<ul><li>nested</li></ul></li><li>two</li></ul>")
    assert "<ol><li>first</li><li>second</li></ol>" in html


def test_table_with_alignment() -> None:
    html = render_markdown("| Name | Year |\n|:-----|-----:|\n| IBM | 1911 |\n| Anthropic | 2021 |")
    assert "<th>Name</th>" in html and '<th class="right">Year</th>' in html
    assert '<td>Anthropic</td><td class="right">2021</td>' in html


def test_quotes_rules_and_paragraph_breaks() -> None:
    html = render_markdown("> quoted\n\n---\n\nline one\nline two")
    assert "<blockquote><p>quoted</p></blockquote>" in html
    assert "<hr>" in html
    assert "<p>line one<br>line two</p>" in html


def test_arabic_text_passes_through() -> None:
    assert render_markdown("**مرحبا** بالعالم") == "<p><strong>مرحبا</strong> بالعالم</p>"
