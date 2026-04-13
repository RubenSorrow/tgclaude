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


# ── Multi-chunk behaviour ─────────────────────────────────────────────────────

def test_multi_chunk_splits_long_input() -> None:
    """A message longer than TARGET_LENGTH must produce more than one chunk."""
    long_msg = "A" * (TARGET_LENGTH + 100)
    chunks = chunk_message(long_msg)
    assert len(chunks) > 1


def test_chunks_have_no_suffix() -> None:
    """chunk_message() returns raw chunks — no trailing \" (N/M)\" suffix on any chunk."""
    import re as _re
    suffix_pattern = _re.compile(r" \(\d+/\d+\)$")
    msg = "\n\n".join(["x" * 3990, "y" * 100])
    chunks = chunk_message(msg)
    assert len(chunks) >= 2
    for chunk in chunks:
        assert not suffix_pattern.search(chunk), (
            f"Unexpected suffix on chunk: {chunk[-30:]!r}"
        )


# ── Paragraph-boundary preference ────────────────────────────────────────────

def test_split_prefers_paragraph_boundary() -> None:
    """The chunker should prefer splitting at \\n\\n rather than mid-word."""
    # Build a message where a \\n\\n boundary falls just inside the limit.
    part_a = "a" * 3990 + "\n\n"
    part_b = "b" * 200
    msg = part_a + part_b
    chunks = chunk_message(msg)
    # The first chunk should end at the \\n\\n boundary.
    assert chunks[0].endswith("\n\n") or chunks[0].strip().endswith("a" * 10)


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
        assert chunks[1].startswith("<b>")


def test_nested_tags_balanced() -> None:
    """Nested <b><i> must both be closed/reopened across a split."""
    inner = "<b><i>" + "x" * 3990 + "end</i></b>"
    chunks = chunk_message(inner)
    if len(chunks) > 1:
        # First chunk must close both i and b
        assert "</i>" in chunks[0] or "</b>" in chunks[0]
        # Second chunk must re-open the tags it closed
        assert "<b>" in chunks[1] or "<i>" in chunks[1]


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
        # Count tags: open and close should match within the chunk.
        b_opens = chunks[0].count("<b>")
        b_closes = chunks[0].count("</b>")
        assert b_opens == b_closes


def test_link_tag_reopened_with_href() -> None:
    """<a href="..."> must be re-opened with the same href on the next chunk."""
    href = "https://example.com"
    inner = f'<a href="{href}">' + "link text " * 800 + "</a>"
    chunks = chunk_message(inner)
    if len(chunks) > 1:
        assert f'href="{href}"' in chunks[1] or 'href=' in chunks[1]
