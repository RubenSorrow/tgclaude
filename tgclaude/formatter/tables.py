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

    # Compute column widths using raw (unescaped) text lengths so that the
    # visual alignment matches what the user sees in Telegram (html.escape only
    # adds a handful of extra chars for <, >, & and those are rare in table
    # cells; using raw length is the simpler, more predictable choice).
    col_widths: list[int] = [len(h) for h in header_cells]
    for row in normalised_rows:
        for i, cell in enumerate(row):
            if i < col_count:
                col_widths[i] = max(col_widths[i], len(cell))

    def _pad(text: str, width: int) -> str:
        return text + " " * (width - len(text))

    def _render_row(cells: list[str]) -> str:
        escaped = [html.escape(_pad(c, col_widths[i])) for i, c in enumerate(cells)]
        return " │ ".join(escaped)

    # Separator line: '─' * width joined with '─┼─'
    separator_parts = ["─" * w for w in col_widths]
    separator = "─┼─".join(separator_parts)

    lines: list[str] = [
        _render_row(header_cells),
        separator,
        *(_render_row(row) for row in normalised_rows),
    ]

    inner = "\n".join(lines)
    return f"<pre>{inner}</pre>"
