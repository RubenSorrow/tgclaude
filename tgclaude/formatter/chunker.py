from __future__ import annotations

import re
from dataclasses import dataclass

TARGET_LENGTH = 4000  # leave slack for part suffix and Telegram's 4096 limit

# Tags whose open/close pairs must be balanced across chunk boundaries.
# Order matters: we track them in a stack (LIFO).
_BALANCEABLE_TAG_NAMES: frozenset[str] = frozenset(
    {"b", "i", "code", "pre", "blockquote", "a"}
)

# Matches any HTML open or close tag we care about.
# Group 1: "/" if close tag; group 2: tag name; group 3: rest of attributes (for <a>)
_TAG_RE = re.compile(
    r"<(/?)(\w+)(\s[^>]*)?>",
    re.IGNORECASE,
)


@dataclass(slots=True)
class _OpenTag:
    """Represents an open tag that is on the balance stack."""

    name: str
    # Full opening string, e.g. '<a href="https://…">' — used to re-open.
    open_str: str


def _parse_open_tags_in_slice(text: str) -> list[_OpenTag]:
    """Return the stack of open balanceable tags after processing *text*.

    Scans left-to-right; pushes on open tags, pops on close tags.
    Only considers tags in _BALANCEABLE_TAG_NAMES.
    """
    stack: list[_OpenTag] = []
    for m in _TAG_RE.finditer(text):
        is_close = bool(m.group(1))
        name = m.group(2).lower()
        if name not in _BALANCEABLE_TAG_NAMES:
            continue
        if is_close:
            # Pop the most recent matching open tag.
            for idx in range(len(stack) - 1, -1, -1):
                if stack[idx].name == name:
                    stack.pop(idx)
                    break
        else:
            attrs = m.group(3) or ""
            stack.append(_OpenTag(name=name, open_str=f"<{name}{attrs}>"))
    return stack


def _close_tags(stack: list[_OpenTag]) -> str:
    """Emit closing tags for all open tags, innermost first."""
    return "".join(f"</{tag.name}>" for tag in reversed(stack))


def _reopen_tags(stack: list[_OpenTag]) -> str:
    """Emit opening tags to restore context, outermost first."""
    return "".join(tag.open_str for tag in stack)


def _find_pre_end(html: str, start: int) -> int:
    """Return the index just after the </pre> that closes the <pre> at *start*.

    *start* points to the '<' of '<pre'.  Returns -1 if not found.
    """
    depth = 0
    pos = start
    while pos < len(html):
        m = re.search(r"<(/?)pre\b", html[pos:], re.IGNORECASE)
        if m is None:
            return -1
        if m.group(1):  # closing
            depth -= 1
            if depth <= 0:
                # Return position just after the '>'
                close_start = pos + m.start()
                close_end = html.index(">", close_start) + 1
                return close_end
        else:  # opening
            depth += 1
        pos += m.end()
    return -1


def _trim_to_outside_tag(html: str, limit: int) -> str:
    """Return html[:limit], but walk back if we'd be splitting inside a tag."""
    pos = min(limit, len(html))
    # Check if we're mid-tag: find the last '<' before pos and last '>' before pos.
    last_lt = html.rfind("<", 0, pos)
    last_gt = html.rfind(">", 0, pos)
    if last_lt > last_gt:
        # We're inside a tag — back up to just before the '<'
        pos = last_lt
    return html[:pos]


def candidate_len(html: str, limit: int) -> int:
    """Return the safe split length, never inside a tag."""
    return len(_trim_to_outside_tag(html, limit))


def chunk_message(html: str) -> list[str]:
    """Split HTML into Telegram-safe chunks <= TARGET_LENGTH chars.

    Rules (in priority order):
    1. Prefer splitting at \\n\\n paragraph boundaries
    2. Fall back to \\n line boundaries
    3. Last resort: split mid-content (outside any open tag)
    4. Maintain a stack of open HTML tags. On each split:
       - Close all open tags at end of chunk N (innermost first)
       - Re-open them at start of chunk N+1 (outermost first)
    5. Never split inside a tag itself (i.e. between < and >)
    6. Tags that need balancing: <b>, <i>, <code>, <pre>, <blockquote>, <a href="...">

    Returns raw chunks without (X/Y) suffixes — caller adds suffixes if needed.
    """
    if not html:
        return []

    if len(html) <= TARGET_LENGTH:
        return [html]

    return _collect_chunks(html)


def _collect_chunks(html: str) -> list[str]:  # noqa: C901
    """Core splitting logic — returns raw chunks without (X/Y) suffixes."""
    chunks: list[str] = []
    remainder = html
    # Running tag stack — updated as we consume each chunk.
    tag_stack: list[_OpenTag] = []

    while remainder:
        reopen_prefix = _reopen_tags(tag_stack)
        # Check if the remainder (with reopen prefix but no close suffix needed)
        # fits within TARGET_LENGTH.
        if len(reopen_prefix) + len(remainder) <= TARGET_LENGTH:
            chunks.append(reopen_prefix + remainder)
            break

        # Budget: TARGET_LENGTH minus reopen prefix minus close suffix overhead.
        reopen_str = reopen_prefix  # already computed above
        close_overhead = len(_close_tags(tag_stack))
        reopen_len = len(reopen_str)
        available_for_content = TARGET_LENGTH - reopen_len - close_overhead

        if available_for_content <= 0:
            # Pathological: tags themselves exceed the budget — emit remainder as-is.
            chunks.append(reopen_str + remainder)
            break

        content_slice = remainder

        # --- Guard for <pre> blocks starting at the head of remainder ----------
        pre_match = re.match(r"(\s*<pre\b[^>]*>)", content_slice, re.IGNORECASE)
        if pre_match or (tag_stack and tag_stack[-1].name == "pre"):
            # We are inside (or entering) a <pre> block. Never split it.
            if tag_stack and tag_stack[-1].name == "pre":
                # We're already inside <pre>; find its close.
                # The full pre started before this chunk; we need to find </pre>.
                close_m = re.search(r"</pre\s*>", content_slice, re.IGNORECASE)
                if close_m:
                    pre_content = content_slice[: close_m.end()]
                    chunk_text = reopen_str + pre_content
                    chunks.append(chunk_text)
                    remainder = content_slice[close_m.end():]
                    # Update tag stack: pre is now closed.
                    tag_stack = _parse_open_tags_in_slice(chunk_text)
                    continue
                else:
                    # No closing </pre> found — emit all as oversized.
                    chunks.append(reopen_str + content_slice)
                    break
            else:
                # <pre> starts here.
                pre_start = pre_match.start()
                pre_end = _find_pre_end(content_slice, pre_start)
                pre_block = content_slice[pre_start: pre_end if pre_end != -1 else len(content_slice)]
                before_pre = content_slice[:pre_start]

                if before_pre:
                    # Emit content before <pre> as normal chunk.
                    chunk_text, new_remainder = _split_content(
                        before_pre, available_for_content
                    )
                    close_str = _close_tags(tag_stack)
                    full_chunk = reopen_str + chunk_text + close_str
                    chunks.append(full_chunk)
                    tag_stack = _parse_open_tags_in_slice(reopen_str + chunk_text)
                    remainder = new_remainder + content_slice[len(before_pre):]
                else:
                    # <pre> is at head — emit entire pre block as one chunk.
                    chunks.append(reopen_str + pre_block)
                    tag_stack = []  # <pre> is self-contained
                    remainder = content_slice[len(pre_block):]
                continue

        # Normal split
        chunk_text, new_remainder = _split_content(content_slice, available_for_content)
        close_str = _close_tags(tag_stack)
        full_chunk = reopen_str + chunk_text + close_str
        chunks.append(full_chunk)
        tag_stack = _parse_open_tags_in_slice(reopen_str + chunk_text)

        if new_remainder == remainder:
            # Safety valve: no progress made — emit the rest as an oversized chunk
            # to avoid an infinite loop (can happen if tags fill the entire budget).
            chunks.append(reopen_str + new_remainder)
            break

        remainder = new_remainder

    return chunks


def _split_content(text: str, limit: int) -> tuple[str, str]:
    """Split *text* at a safe boundary within *limit* chars.

    Returns (chunk, remainder).  Never splits inside a tag.
    Priority: \\n\\n > \\n > space > tag boundary.
    """
    if len(text) <= limit:
        return text, ""

    # Never split inside a tag.
    safe_limit = candidate_len(text, limit)
    if safe_limit == 0:
        # Edge case: a single tag longer than limit — we have no choice.
        # Find the end of the tag.
        gt = text.find(">")
        safe_limit = gt + 1 if gt != -1 else limit

    candidate = text[:safe_limit]

    # Strategy 1: last paragraph break
    pos = candidate.rfind("\n\n")
    if pos > 0:
        return text[:pos + 2], text[pos + 2:]

    # Strategy 2: last newline
    pos = candidate.rfind("\n")
    if pos > 0:
        return text[:pos + 1], text[pos + 1:]

    # Strategy 3: last space (avoid splitting mid-word)
    pos = candidate.rfind(" ")
    if pos > 0:
        return text[:pos + 1], text[pos + 1:]

    # Strategy 4: hard split at tag boundary
    return text[:safe_limit], text[safe_limit:]
