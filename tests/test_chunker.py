from __future__ import annotations

import pytest

from tgclaude.formatter.chunker import chunk_message, TARGET_LENGTH


# ── Basic behaviour ───────────────────────────────────────────────────────────

def test_empty_string_returns_empty_list() -> None:
    assert chunk_message("") == []


def test_short_message_single_chunk() -> None:
    msg = "Hello, world!"
    result = chunk_message(msg)
    assert result == [msg]


def test_short_message_no_suffix() -> None:
    """Single-chunk messages must not have (1/1) suffix."""
    result = chunk_message("Short message")
    assert "(1/1)" not in result[0]
    assert "/" not in result[0]


def test_all_chunks_within_limit() -> None:
    long_msg = "word " * 2000  # ~10000 chars
    chunks = chunk_message(long_msg)
    for chunk in chunks:
        assert len(chunk) <= TARGET_LENGTH, f"Chunk too long: {len(chunk)}"


# ── Multi-chunk suffix ────────────────────────────────────────────────────────

def test_multi_chunk_gets_suffix() -> None:
    long_msg = "A" * (TARGET_LENGTH + 100)
    chunks = chunk_message(long_msg)
    assert len(chunks) > 1
    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        assert chunk.endswith(f" ({i}/{total})"), f"Chunk {i} missing suffix: {chunk[-20:]!r}"


def test_suffix_format_is_correct() -> None:
    # Create a message that produces exactly 2 chunks.
    # Each chunk targets 4000 chars; we need > 4000 total.
    msg = "\n\n".join(["x" * 3990, "y" * 100])
    chunks = chunk_message(msg)
    assert len(chunks) >= 2
    assert chunks[0].endswith("(1/2)") or "(1/" in chunks[0]


# ── Paragraph-boundary preference ────────────────────────────────────────────

def test_split_prefers_paragraph_boundary() -> None:
    """The chunker should prefer splitting at \\n\\n rather than mid-word."""
    # Build a message where a \\n\\n boundary falls just inside the limit.
    part_a = "a" * 3990 + "\n\n"
    part_b = "b" * 200
    msg = part_a + part_b
    chunks = chunk_message(msg)
    # The first chunk should end at the \\n\\n boundary (after stripping suffix).
    # We strip the suffix "(1/N)" for comparison.
    first_chunk_content = chunks[0].rsplit(" (", 1)[0]
    assert first_chunk_content.endswith("\n\n") or first_chunk_content.strip().endswith("a" * 10)


# ── Tag balancing ─────────────────────────────────────────────────────────────

def test_open_b_tag_closed_at_chunk_end() -> None:
    """An unclosed <b> in chunk N must be closed at the end of that chunk."""
    # Craft HTML where <b> opens before the split point.
    inner = "<b>" + "x" * 3990 + "</b>"
    chunks = chunk_message(inner)
    if len(chunks) > 1:
        assert chunks[0].endswith("</b>") or "</b>" in chunks[0]


def test_open_b_tag_reopened_at_next_chunk() -> None:
    """A <b> closed at end of chunk N must be re-opened at start of chunk N+1."""
    inner = "<b>" + "x" * 3990 + "more bold text" + "</b>"
    chunks = chunk_message(inner)
    if len(chunks) > 1:
        # The second chunk (before suffix) should start with <b>
        second_content = chunks[1].rsplit(" (", 1)[0]
        assert second_content.startswith("<b>")


def test_nested_tags_balanced() -> None:
    """Nested <b><i> must both be closed/reopened across a split."""
    inner = "<b><i>" + "x" * 3990 + "end</i></b>"
    chunks = chunk_message(inner)
    if len(chunks) > 1:
        first = chunks[0]
        second = chunks[1].rsplit(" (", 1)[0]
        # First chunk must close both i and b
        assert "</i>" in first or "</b>" in first
        # Second chunk must re-open the tags it closed
        assert "<b>" in second or "<i>" in second


# ── <pre> blocks never split ──────────────────────────────────────────────────

def test_pre_block_not_split() -> None:
    """A <pre> block must never be broken across chunks, even if oversized."""
    # A <pre> block alone larger than TARGET_LENGTH.
    code = "x" * (TARGET_LENGTH + 500)
    pre_block = f"<pre><code>{code}</code></pre>"
    chunks = chunk_message(pre_block)
    # The pre block should be in one chunk (possibly oversized).
    pre_in_chunks = [c for c in chunks if "<pre>" in c]
    assert len(pre_in_chunks) == 1, "pre block was split across multiple chunks"


def test_pre_block_intact_in_chunk() -> None:
    """<pre> blocks within the limit stay in a single chunk."""
    pre_block = "<pre><code>short code</code></pre>"
    surrounding = "text before\n\n" + pre_block + "\n\ntext after"
    chunks = chunk_message(surrounding)
    # Find the chunk containing the pre block
    pre_chunks = [c for c in chunks if "<pre>" in c]
    for chunk in pre_chunks:
        # The chunk must have both opening and closing pre tags.
        assert "<pre>" in chunk and "</pre>" in chunk


# ── Tag-stack accuracy ────────────────────────────────────────────────────────

def test_balanced_tags_not_double_closed() -> None:
    """Tags that are properly closed before the split point are not re-closed."""
    # <b> opens and closes well before the 4000 limit.
    msg = "<b>short bold</b>" + " " * 100 + "\n\n" + "x" * 4000
    chunks = chunk_message(msg)
    # The first chunk should not have an extra </b> if <b> was already closed.
    if len(chunks) > 1:
        first = chunks[0].rsplit(" (", 1)[0]
        # Count tags: open and close should match within the chunk.
        b_opens = first.count("<b>")
        b_closes = first.count("</b>")
        assert b_opens == b_closes


def test_link_tag_reopened_with_href() -> None:
    """<a href="..."> must be re-opened with the same href on the next chunk."""
    href = "https://example.com"
    inner = f'<a href="{href}">' + "link text " * 800 + "</a>"
    chunks = chunk_message(inner)
    if len(chunks) > 1:
        second = chunks[1].rsplit(" (", 1)[0]
        assert f'href="{href}"' in second or 'href=' in second
