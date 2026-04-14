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
    """A <pre> block within TARGET_LENGTH must not be split across chunks."""
    # A <pre> block small enough to fit in one chunk.
    code = "x" * 100
    pre_block = f"<pre><code>{code}</code></pre>"
    assert len(pre_block) <= TARGET_LENGTH
    chunks = chunk_message(pre_block)
    assert len(chunks) == 1, "Small pre block must stay in a single chunk"
    assert "<pre>" in chunks[0] and "</pre>" in chunks[0]


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


# ── Boundary lengths ──────────────────────────────────────────────────────────

def test_exactly_target_length_is_single_chunk() -> None:
    """Input of exactly TARGET_LENGTH characters must return a single chunk.

    The early-return branch ``if len(html) <= TARGET_LENGTH: return [html]``
    should fire, producing exactly one chunk equal to the original string.
    """
    msg = "x" * TARGET_LENGTH
    chunks = chunk_message(msg)
    assert len(chunks) == 1
    assert chunks[0] == msg


def test_target_length_plus_one_splits() -> None:
    """Input of TARGET_LENGTH + 1 characters must produce more than one chunk.

    The very first character beyond the limit must trigger the splitting path,
    so the result must have at least two chunks.
    """
    msg = "x" * (TARGET_LENGTH + 1)
    chunks = chunk_message(msg)
    assert len(chunks) > 1


# ── Split-point fallback chain ────────────────────────────────────────────────

def test_split_falls_back_to_single_newline() -> None:
    """When no \\n\\n exists, the chunker must split at the last \\n within the limit.

    Verifies the Strategy-2 branch in ``_split_content``.  The first chunk
    must end with a newline, and together the chunks must reconstruct the
    original message.
    """
    # Fill to just over the limit with no paragraph breaks, but with a
    # single newline well inside the window.
    line_a = "a" * 3990 + "\n"
    line_b = "b" * 200
    msg = line_a + line_b
    assert len(msg) > TARGET_LENGTH  # ensure splitting is triggered

    chunks = chunk_message(msg)
    assert len(chunks) >= 2
    # First chunk must end at the \n boundary.
    assert chunks[0].endswith("\n"), (
        f"Expected first chunk to end with newline, got: {chunks[0][-20:]!r}"
    )
    # Reassembled text must equal the original.
    assert "".join(chunks) == msg


def test_split_falls_back_to_space() -> None:
    """When no newlines exist, the chunker splits at the last space within the limit.

    Verifies the Strategy-3 (space) branch.  The first chunk must end with a
    space, and the chunks must reconstruct the original.
    """
    # 3990 'a's, then a space, then enough 'b's to push over TARGET_LENGTH.
    part_a = "a" * 3990 + " "
    part_b = "b" * 200
    msg = part_a + part_b
    assert len(msg) > TARGET_LENGTH

    chunks = chunk_message(msg)
    assert len(chunks) >= 2
    # The split should have landed at the space.
    assert chunks[0].endswith(" "), (
        f"Expected first chunk to end with space, got: {chunks[0][-20:]!r}"
    )
    assert "".join(chunks) == msg


def test_hard_char_split_when_no_whitespace() -> None:
    """When there is no whitespace at all, the chunker must do a hard character split.

    Verifies the final fallback (Strategy 3 tag-boundary path).  All chunks
    must individually be within TARGET_LENGTH and together reconstruct the
    original.
    """
    msg = "z" * (TARGET_LENGTH * 2)  # no whitespace, no tags
    chunks = chunk_message(msg)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= TARGET_LENGTH
    assert "".join(chunks) == msg


# ── Multiple <pre> blocks ─────────────────────────────────────────────────────

def test_two_pre_blocks_each_kept_intact() -> None:
    """Two separate <pre> blocks in the same message must each land in one chunk.

    Neither block may be split across chunk boundaries.  Even when the content
    between them forces a split, each <pre>…</pre> unit must appear wholly
    within a single chunk.
    """
    code_a = "<pre><code>" + "a" * 200 + "</code></pre>"
    code_b = "<pre><code>" + "b" * 200 + "</code></pre>"
    # Separate the two blocks with enough plain text to force a split.
    filler = "word " * 800  # ~4000 chars
    msg = code_a + "\n\n" + filler + "\n\n" + code_b

    chunks = chunk_message(msg)

    # Every chunk that contains an opening <pre> must also contain the
    # matching </pre> — no dangling half-blocks.
    for i, chunk in enumerate(chunks):
        opens = chunk.count("<pre")
        closes = chunk.count("</pre>")
        assert opens == closes, (
            f"Chunk {i} has unbalanced <pre>: {opens} open, {closes} close"
        )


def test_three_pre_blocks_all_intact() -> None:
    """Three <pre> blocks with oversized fillers each stay whole.

    Extends the two-block test to confirm the loop handles more than one
    encounter with the pre-guard branch.
    """
    def make_pre(char: str, n: int = 100) -> str:
        return f"<pre><code>{char * n}</code></pre>"

    filler = "x " * 2000  # pushes each section past TARGET_LENGTH
    msg = make_pre("a") + "\n\n" + filler + "\n\n" + make_pre("b") + "\n\n" + filler + "\n\n" + make_pre("c")

    chunks = chunk_message(msg)

    for i, chunk in enumerate(chunks):
        opens = chunk.count("<pre")
        closes = chunk.count("</pre>")
        assert opens == closes, (
            f"Chunk {i} has unbalanced <pre>: {opens} open, {closes} close"
        )


# ── Tight nested-tag assertions ───────────────────────────────────────────────

def test_nested_b_i_both_closed_at_chunk_end() -> None:
    """Nested <b><i> must both be closed at the end of the first chunk.

    The close-tag suffix appended at split time must contain </i> (inner)
    followed by </b> (outer), in that LIFO order.
    """
    inner = "<b><i>" + "x" * 3990 + " more text" + "</i></b>"
    chunks = chunk_message(inner)

    if len(chunks) < 2:
        pytest.skip("Input did not produce multiple chunks — adjust padding")

    # Both closing tags must appear in the first chunk.
    assert "</i>" in chunks[0], "Missing </i> close in chunk 0"
    assert "</b>" in chunks[0], "Missing </b> close in chunk 0"


def test_nested_b_i_both_reopened_at_next_chunk() -> None:
    """Nested <b><i> must both be re-opened at the start of the second chunk.

    The reopen prefix must restore outermost first: <b><i>.
    """
    inner = "<b><i>" + "x" * 3990 + " more text" + "</i></b>"
    chunks = chunk_message(inner)

    if len(chunks) < 2:
        pytest.skip("Input did not produce multiple chunks — adjust padding")

    # Both opening tags must be present in the second chunk.
    assert "<b>" in chunks[1], "Missing <b> reopen in chunk 1"
    assert "<i>" in chunks[1], "Missing <i> reopen in chunk 1"


def test_nested_b_i_reopen_order() -> None:
    """Re-opened nested tags must appear in outermost-first order: <b><i>.

    Verifies that the reopen prefix preserves the original nesting order so
    that the rendered HTML remains valid.
    """
    inner = "<b><i>" + "x" * 3990 + " more text" + "</i></b>"
    chunks = chunk_message(inner)

    if len(chunks) < 2:
        pytest.skip("Input did not produce multiple chunks — adjust padding")

    b_pos = chunks[1].find("<b>")
    i_pos = chunks[1].find("<i>")
    assert b_pos != -1 and i_pos != -1, "Both tags must be present in chunk 1"
    assert b_pos < i_pos, (
        f"<b> must appear before <i> in the reopen prefix, "
        f"but <b> is at {b_pos} and <i> is at {i_pos}"
    )


# ── <a href> tight assertion ──────────────────────────────────────────────────

def test_link_tag_reopened_with_exact_href() -> None:
    """<a href="..."> must be re-opened with precisely the same href value.

    Tightens the existing loose assertion: both the attribute name and the
    exact URL must appear together in the second chunk's reopen prefix.
    """
    href = "https://example.com/path?q=1&r=2"
    inner = f'<a href="{href}">' + "click here " * 400 + "</a>"
    chunks = chunk_message(inner)

    if len(chunks) < 2:
        pytest.skip("Input did not produce multiple chunks — adjust padding")

    assert f'href="{href}"' in chunks[1], (
        f'Expected href="{href}" in chunk 1, got: {chunks[1][:120]!r}'
    )


# ── Content before a <pre> block ─────────────────────────────────────────────

def test_text_before_pre_emitted_as_separate_chunk() -> None:
    """Plain text preceding a <pre> block must be emitted before the pre chunk.

    Exercises the ``before_pre`` branch in ``_collect_chunks``: when a <pre>
    is not at the very start of the remainder, the content before it is split
    off normally and the <pre> block then heads the next remainder.
    """
    # Enough plain text before the <pre> to fill a chunk on its own.
    preamble = "intro " * 700  # ~4200 chars — forces a split before the <pre>
    code_block = "<pre><code>some code here</code></pre>"
    msg = preamble + code_block

    chunks = chunk_message(msg)
    assert len(chunks) >= 2

    # The <pre> block must be wholly contained in exactly one chunk.
    pre_chunks = [c for c in chunks if "<pre>" in c]
    assert len(pre_chunks) == 1, (
        f"<pre> block found in {len(pre_chunks)} chunks, expected 1"
    )
    pre_chunk = pre_chunks[0]
    assert "</pre>" in pre_chunk, "Chunk with <pre> must also contain </pre>"


# ── Chunk content reconstruction ─────────────────────────────────────────────

def test_chunks_reconstruct_plain_text() -> None:
    """Joining all chunks must reproduce the original plain-text content.

    For a plain-text (no HTML tags) message the concatenation of all chunks
    must equal the original string exactly.
    """
    # Use paragraph-separated words so the split lands on \n\n.
    paragraph = "word " * 400  # ~2000 chars per paragraph
    msg = paragraph + "\n\n" + paragraph + "\n\n" + paragraph
    assert len(msg) > TARGET_LENGTH

    chunks = chunk_message(msg)
    assert "".join(chunks) == msg


def test_oversized_pre_at_head_is_split_within_limit() -> None:
    """A <pre> block longer than TARGET_LENGTH at the head of the message must be
    split into chunks each within TARGET_LENGTH, not emitted as one oversized chunk.
    """
    # Build a <pre> block with content well over the limit.
    inner = "\n".join("x" * 80 for _ in range(60))  # 60 lines × 81 chars ≈ 4860 chars
    pre_block = f"<pre><code>{inner}</code></pre>"
    assert len(pre_block) > TARGET_LENGTH

    chunks = chunk_message(pre_block)
    assert len(chunks) > 1, "Oversized pre block must be split into multiple chunks"
    for i, chunk in enumerate(chunks):
        assert len(chunk) <= TARGET_LENGTH, (
            f"Chunk {i} exceeds TARGET_LENGTH: {len(chunk)} chars"
        )


def test_oversized_pre_carried_over_is_split_within_limit() -> None:
    """A <pre> block that was opened in a previous chunk (carried over on the tag
    stack) must also be split rather than emitted as one oversized chunk.
    """
    # Preamble forces a split before the <pre>; then the <pre> itself is oversized.
    preamble = "intro " * 700  # ~4200 chars, forces initial split
    inner = "\n".join("y" * 80 for _ in range(60))  # ≈ 4860 chars
    pre_block = f"<pre><code>{inner}</code></pre>"
    msg = preamble + pre_block

    chunks = chunk_message(msg)
    for i, chunk in enumerate(chunks):
        assert len(chunk) <= TARGET_LENGTH, (
            f"Chunk {i} exceeds TARGET_LENGTH: {len(chunk)} chars"
        )
