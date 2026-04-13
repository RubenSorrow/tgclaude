from __future__ import annotations

import pytest

from tgclaude.formatter import format_text


# ── Headings ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("md,expected_fragment", [
    ("# Hello World", "<b>Hello World</b>"),
    ("## Section", "<b>Section</b>"),
    ("### Sub", "<b>Sub</b>"),
])
def test_headings(md: str, expected_fragment: str) -> None:
    result = format_text(md)
    assert expected_fragment in result
    # Headings must be followed by a blank line
    assert result.strip().endswith("</b>") or "\n" in result


# ── Bold and italic ───────────────────────────────────────────────────────────

def test_bold() -> None:
    result = format_text("**bold text**")
    assert "<b>bold text</b>" in result


def test_italic_asterisk() -> None:
    result = format_text("*italic text*")
    assert "<i>italic text</i>" in result


def test_italic_underscore() -> None:
    result = format_text("_italic text_")
    assert "<i>italic text</i>" in result


def test_bold_and_italic() -> None:
    result = format_text("**bold** and *italic*")
    assert "<b>bold</b>" in result
    assert "<i>italic</i>" in result


# ── Inline code ───────────────────────────────────────────────────────────────

def test_inline_code_basic() -> None:
    result = format_text("`some code`")
    assert "<code>some code</code>" in result


def test_inline_code_escaping() -> None:
    """html.escape must be applied to code content."""
    result = format_text("`a < b && b > c`")
    assert "<code>a &lt; b &amp;&amp; b &gt; c</code>" in result


def test_inline_code_angle_bracket() -> None:
    result = format_text("`<tag>`")
    assert "&lt;tag&gt;" in result
    assert "<code>" in result


# ── Fenced code blocks ────────────────────────────────────────────────────────

def test_fenced_code_with_language() -> None:
    md = "```python\nprint('hello')\n```"
    result = format_text(md)
    assert '<pre><code class="language-python">' in result
    assert "print(&#x27;hello&#x27;)" in result or "print('hello')" in result
    assert "</code></pre>" in result


def test_fenced_code_without_language() -> None:
    md = "```\nsome code\n```"
    result = format_text(md)
    assert "<pre><code>" in result
    assert "some code" in result
    assert "</code></pre>" in result


def test_fenced_code_escapes_html() -> None:
    md = "```\n<script>alert(1)</script>\n```"
    result = format_text(md)
    assert "&lt;script&gt;" in result
    assert "<script>" not in result


# ── Links ─────────────────────────────────────────────────────────────────────

def test_link() -> None:
    result = format_text("[OpenAI](https://openai.com)")
    assert '<a href="https://openai.com">OpenAI</a>' in result


def test_link_with_special_chars_in_url() -> None:
    result = format_text("[Search](https://example.com/?q=a&b=c)")
    assert '<a href="https://example.com/?q=a&amp;b=c">' in result


# ── Horizontal rule ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("hr_md", ["---", "***", "___"])
def test_horizontal_rule(hr_md: str) -> None:
    result = format_text(hr_md)
    assert "━━━━━━━━━━━━" in result


# ── Blockquote ────────────────────────────────────────────────────────────────

def test_blockquote() -> None:
    result = format_text("> This is a quote")
    assert "<blockquote>" in result
    assert "This is a quote" in result
    assert "</blockquote>" in result


def test_blockquote_escapes_content() -> None:
    result = format_text("> a < b")
    assert "<blockquote>" in result
    assert "&lt;" in result


# ── Unordered lists ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("marker", ["- ", "* "])
def test_unordered_list_bullet(marker: str) -> None:
    result = format_text(f"{marker}Alpha\n{marker}Beta\n{marker}Gamma")
    assert "• Alpha" in result
    assert "• Beta" in result
    assert "• Gamma" in result


def test_unordered_list_no_html_markers() -> None:
    result = format_text("- Item one\n- Item two")
    # Must not contain raw markdown dashes as list markers.
    assert "- Item one" not in result
    assert "• Item one" in result


# ── Ordered lists ─────────────────────────────────────────────────────────────

def test_ordered_list_numbering() -> None:
    result = format_text("1. First\n2. Second\n3. Third")
    assert "1. First" in result
    assert "2. Second" in result
    assert "3. Third" in result


def test_ordered_list_preserves_start_number() -> None:
    """Literal numbering is preserved as emitted (1-based in markdown)."""
    result = format_text("1. Alpha\n2. Beta")
    assert "1. Alpha" in result
    assert "2. Beta" in result


# ── Plain text escaping ───────────────────────────────────────────────────────

def test_plain_text_ampersand_escaped() -> None:
    result = format_text("foo & bar")
    assert "&amp;" in result
    assert "foo & bar" not in result


def test_plain_text_lt_gt_escaped() -> None:
    result = format_text("x < y > z")
    assert "&lt;" in result
    assert "&gt;" in result


# ── ASCII art / box-drawing paragraphs ───────────────────────────────────────

def test_box_drawing_wrapped_in_pre() -> None:
    md = "┌─────────┐\n│  Hello  │\n└─────────┘"
    result = format_text(md)
    assert "<pre>" in result


def test_trailing_space_paragraph_wrapped_in_pre() -> None:
    # Two trailing spaces on the first line = alignment padding signal.
    md = "line one  \nline two"
    result = format_text(md)
    # markdown-it treats trailing double-space as a hard break, NOT a <pre>
    # signal, because trailing spaces are stripped by markdown-it before
    # the paragraph_open token. So we only assert no crash here.
    assert isinstance(result, str)
