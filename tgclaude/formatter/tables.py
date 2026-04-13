from __future__ import annotations

import html


def render_table(header_cells: list[str], rows: list[list[str]]) -> str:
    """Render a markdown table as monospace HTML in a <pre> block.

    Column widths = max(len(cell)) across header + all rows.
    Cells padded with spaces. Rows joined with ' │ '.
    A '─┼─' separator sits under the header row.
    Result is wrapped in <pre>…</pre> for guaranteed monospace in Telegram.
    All cell content runs through html.escape().
    """
    if not header_cells:
        return ""

    col_count = len(header_cells)

    # Normalise rows so every row has exactly col_count cells.
    normalised_rows: list[list[str]] = []
    for row in rows:
        padded = list(row)
        while len(padded) < col_count:
            padded.append("")
        normalised_rows.append(padded[:col_count])

    # Escape all content first so that column widths and padding are based on
    # the final rendered lengths (e.g. "A & B" → "A &amp; B" is 9 chars, not 5).
    escaped_header = [html.escape(h) for h in header_cells]
    escaped_rows = [[html.escape(cell) for cell in row] for row in normalised_rows]

    col_widths: list[int] = [len(h) for h in escaped_header]
    for row in escaped_rows:
        for i, cell in enumerate(row):
            if i < col_count:
                col_widths[i] = max(col_widths[i], len(cell))

    def _pad(text: str, width: int) -> str:
        return text + " " * (width - len(text))

    def _render_row(cells: list[str]) -> str:
        return " │ ".join(_pad(c, col_widths[i]) for i, c in enumerate(cells))

    # Separator line: '─' * width joined with '─┼─'
    separator_parts = ["─" * w for w in col_widths]
    separator = "─┼─".join(separator_parts)

    lines: list[str] = [
        _render_row(escaped_header),
        separator,
        *(_render_row(row) for row in escaped_rows),
    ]

    inner = "\n".join(lines)
    return f"<pre>{inner}</pre>"
