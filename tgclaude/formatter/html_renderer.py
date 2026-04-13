from __future__ import annotations

import html

from markdown_it import MarkdownIt
from markdown_it.token import Token

from .tables import render_table
from .ascii_detect import is_ascii_art

# Horizontal rule replacement (12 heavy box-drawing dashes)
HR_REPLACEMENT = "━━━━━━━━━━━━\n"

# Maps diagram language tags to their canonical render URL hint.
# Stable policy: adding a new diagram tool = adding one entry here (OCP).
_DIAGRAM_RENDER_HINTS: dict[str, str] = {
    "mermaid": "mermaid.live",
    "graphviz": "dreampuf.github.io/GraphvizOnline",
    "dot": "dreampuf.github.io/GraphvizOnline",
    "plantuml": "www.plantuml.com/plantuml",
}


def _collect_inline_tokens(tokens: list[Token]) -> str:
    """Walk a flat list of inline tokens and emit Telegram HTML."""
    parts: list[str] = []
    for tok in tokens:
        match tok.type:
            case "text":
                parts.append(html.escape(tok.content))
            case "softbreak":
                parts.append("\n")
            case "hardbreak":
                parts.append("\n")
            case "code_inline":
                parts.append(f"<code>{html.escape(tok.content)}</code>")
            case "strong_open":
                parts.append("<b>")
            case "strong_close":
                parts.append("</b>")
            case "em_open":
                parts.append("<i>")
            case "em_close":
                parts.append("</i>")
            case "link_open":
                href = ""
                for attr_name, attr_val in (tok.attrs or {}).items():
                    if attr_name == "href":
                        href = html.escape(str(attr_val), quote=True)
                parts.append(f'<a href="{href}">')
            case "link_close":
                parts.append("</a>")
            case "html_inline":
                # Escape as literal text; Telegram rejects unsupported tags.
                parts.append(html.escape(tok.content))
            case _:
                # image, etc. — render children if any
                if tok.children:
                    parts.append(_collect_inline_tokens(tok.children))
    return "".join(parts)


def _render_inline(token: Token) -> str:
    """Render an 'inline' container token."""
    if token.children is None:
        return html.escape(token.content)
    return _collect_inline_tokens(token.children)


def format_text(text: str) -> str:
    """Convert Claude markdown to Telegram HTML.

    Uses markdown-it-py AST walker. All plain text runs through html.escape().
    Returns HTML string safe for parse_mode=HTML in Telegram.
    """
    md = MarkdownIt()
    tokens = md.parse(text)
    return _render_tokens(tokens)


def _render_tokens(tokens: list[Token]) -> str:  # noqa: C901  (complex by necessity)
    """Walk the top-level token list and emit Telegram HTML."""
    output: list[str] = []
    i = 0
    # We use an index-based loop so we can skip ahead when consuming
    # multi-token constructs (e.g. table, list).
    while i < len(tokens):
        tok = tokens[i]

        # ── Headings ────────────────────────────────────────────────────────
        if tok.type == "heading_open":
            # Next token is the inline content, then heading_close.
            inline_tok = tokens[i + 1] if i + 1 < len(tokens) else None
            inner = _render_inline(inline_tok) if inline_tok and inline_tok.type == "inline" else ""
            output.append(f"<b>{inner}</b>\n\n")
            # Skip inline + heading_close
            i += 3
            continue

        # ── Paragraphs ──────────────────────────────────────────────────────
        if tok.type == "paragraph_open":
            inline_tok = tokens[i + 1] if i + 1 < len(tokens) else None
            if inline_tok and inline_tok.type == "inline":
                raw_text = inline_tok.content
                if is_ascii_art(raw_text):
                    output.append(f"<pre>{html.escape(raw_text)}</pre>\n\n")
                else:
                    rendered = _render_inline(inline_tok)
                    output.append(f"{rendered}\n\n")
            i += 3  # paragraph_open, inline, paragraph_close
            continue

        # ── Fenced / indented code blocks ───────────────────────────────────
        if tok.type == "fence":
            lang = (tok.info or "").strip()
            escaped_code = html.escape(tok.content)
            lang_lower = lang.lower()
            render_hint = _DIAGRAM_RENDER_HINTS.get(lang_lower)
            if render_hint:
                output.append(
                    f'<pre><code class="language-{html.escape(lang)}">{escaped_code}</code></pre>'
                    f'\n<i>↑ render at {html.escape(render_hint)}</i>\n\n'
                )
                i += 1
                continue
            if lang:
                safe_lang = html.escape(lang)
                output.append(
                    f'<pre><code class="language-{safe_lang}">{escaped_code}</code></pre>\n\n'
                )
            else:
                output.append(f"<pre><code>{escaped_code}</code></pre>\n\n")
            i += 1
            continue

        if tok.type == "code_block":
            escaped_code = html.escape(tok.content)
            output.append(f"<pre><code>{escaped_code}</code></pre>\n\n")
            i += 1
            continue

        # ── Horizontal rule ─────────────────────────────────────────────────
        if tok.type == "hr":
            output.append(HR_REPLACEMENT + "\n")
            i += 1
            continue

        # ── Blockquote ──────────────────────────────────────────────────────
        if tok.type == "blockquote_open":
            # Collect all tokens until blockquote_close (may be nested).
            depth = 1
            j = i + 1
            inner_tokens: list[Token] = []
            while j < len(tokens) and depth > 0:
                if tokens[j].type == "blockquote_open":
                    depth += 1
                elif tokens[j].type == "blockquote_close":
                    depth -= 1
                    if depth == 0:
                        break
                inner_tokens.append(tokens[j])
                j += 1
            inner_html = _render_tokens(inner_tokens).strip()
            output.append(f"<blockquote>{inner_html}</blockquote>\n\n")
            i = j + 1  # skip past blockquote_close
            continue

        # ── Bullet / ordered lists ──────────────────────────────────────────
        if tok.type in ("bullet_list_open", "ordered_list_open"):
            ordered = tok.type == "ordered_list_open"
            # Determine starting number for ordered lists
            start_num = 1
            if ordered:
                start_attr = (tok.attrs or {}).get("start")
                if start_attr is not None:
                    try:
                        start_num = int(start_attr)
                    except (ValueError, TypeError):
                        start_num = 1

            # Collect until matching close, tracking nesting.
            list_close_type = "bullet_list_close" if not ordered else "ordered_list_close"
            depth = 1
            j = i + 1
            list_inner: list[Token] = []
            while j < len(tokens) and depth > 0:
                if tokens[j].type == tok.type:
                    depth += 1
                elif tokens[j].type == list_close_type:
                    depth -= 1
                    if depth == 0:
                        break
                list_inner.append(tokens[j])
                j += 1

            rendered_list = _render_list_tokens(list_inner, ordered=ordered, start=start_num, level=0)
            output.append(rendered_list)
            i = j + 1
            continue

        # ── Tables ──────────────────────────────────────────────────────────
        if tok.type == "table_open":
            depth = 1
            j = i + 1
            table_tokens: list[Token] = []
            while j < len(tokens) and depth > 0:
                if tokens[j].type == "table_open":
                    depth += 1
                elif tokens[j].type == "table_close":
                    depth -= 1
                    if depth == 0:
                        break
                table_tokens.append(tokens[j])
                j += 1
            table_html = _render_table_tokens(table_tokens)
            output.append(table_html + "\n\n")
            i = j + 1
            continue

        # ── HTML block (pass-through) ────────────────────────────────────────
        if tok.type == "html_block":
            output.append(html.escape(tok.content))
            i += 1
            continue

        # ── Everything else — skip silently ─────────────────────────────────
        i += 1

    return "".join(output)


def _render_list_tokens(
    tokens: list[Token],
    ordered: bool,
    start: int,
    level: int,
) -> str:
    """Render list_item tokens for one list level."""
    output: list[str] = []
    indent = "  " * level
    item_number = start
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "list_item_open":
            # Collect tokens until matching list_item_close
            depth = 1
            j = i + 1
            item_tokens: list[Token] = []
            while j < len(tokens) and depth > 0:
                if tokens[j].type == "list_item_open":
                    depth += 1
                elif tokens[j].type == "list_item_close":
                    depth -= 1
                    if depth == 0:
                        break
                item_tokens.append(tokens[j])
                j += 1

            # item_tokens may contain: paragraph_open/inline/paragraph_close,
            # nested bullet_list_open, etc.
            item_parts: list[str] = []
            nested_list_html = ""
            k = 0
            while k < len(item_tokens):
                it = item_tokens[k]
                if it.type == "paragraph_open":
                    inline = item_tokens[k + 1] if k + 1 < len(item_tokens) else None
                    if inline and inline.type == "inline":
                        item_parts.append(_render_inline(inline))
                    k += 3
                    continue
                if it.type == "inline":
                    item_parts.append(_render_inline(it))
                    k += 1
                    continue
                if it.type in ("bullet_list_open", "ordered_list_open"):
                    nested_ordered = it.type == "ordered_list_open"
                    nested_close = "bullet_list_close" if not nested_ordered else "ordered_list_close"
                    nested_start = 1
                    if nested_ordered:
                        ns = (it.attrs or {}).get("start")
                        if ns is not None:
                            try:
                                nested_start = int(ns)
                            except (ValueError, TypeError):
                                nested_start = 1
                    depth2 = 1
                    m = k + 1
                    nested_inner: list[Token] = []
                    while m < len(item_tokens) and depth2 > 0:
                        if item_tokens[m].type == it.type:
                            depth2 += 1
                        elif item_tokens[m].type == nested_close:
                            depth2 -= 1
                            if depth2 == 0:
                                break
                        nested_inner.append(item_tokens[m])
                        m += 1
                    nested_list_html = _render_list_tokens(
                        nested_inner, ordered=nested_ordered, start=nested_start, level=level + 1
                    )
                    k = m + 1
                    continue
                k += 1

            item_text = "".join(item_parts)
            if ordered:
                prefix = f"{indent}{item_number}. "
                item_number += 1
            else:
                prefix = f"{indent}• "

            output.append(f"{prefix}{item_text}\n")
            if nested_list_html:
                output.append(nested_list_html)

            i = j + 1
            continue

        i += 1

    return "".join(output)


def _render_table_tokens(tokens: list[Token]) -> str:
    """Extract header and body rows from table token stream and delegate to render_table."""
    header_cells: list[str] = []
    body_rows: list[list[str]] = []

    in_thead = False
    in_tbody = False
    in_tr = False
    current_row: list[str] = []

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        match tok.type:
            case "thead_open":
                in_thead = True
            case "thead_close":
                in_thead = False
            case "tbody_open":
                in_tbody = True
            case "tbody_close":
                in_tbody = False
            case "tr_open":
                in_tr = True
                current_row = []
            case "tr_close":
                in_tr = False
                if in_thead:
                    header_cells = current_row
                elif in_tbody:
                    body_rows.append(current_row)
            case "th_open" | "td_open":
                pass
            case "th_close" | "td_close":
                pass
            case "inline":
                if in_tr:
                    # Render the cell as plain text (strip tags for width calc)
                    current_row.append(tok.content)
        i += 1

    return render_table(header_cells, body_rows)
