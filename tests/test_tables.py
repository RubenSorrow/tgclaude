from __future__ import annotations

import pytest

from tgclaude.formatter.tables import render_table


def test_output_wrapped_in_pre() -> None:
    result = render_table(["Name", "Age"], [["Alice", "30"]])
    assert result.startswith("<pre>")
    assert result.endswith("</pre>")


def test_column_padding() -> None:
    """All columns are padded to their max width."""
    result = render_table(["Col"], [["short"], ["a much longer value"]])
    # "a much longer value" is 19 chars; "Col" header padded to 19.
    assert "Col                " in result  # 16 spaces = 19 - 3
    assert "short              " in result  # 14 spaces = 19 - 5


def test_separator_under_header() -> None:
    result = render_table(["A", "B"], [["1", "2"]])
    lines = result.removeprefix("<pre>").removesuffix("</pre>").splitlines()
    # line 0: header, line 1: separator, line 2+: body
    assert "─" in lines[1]
    assert "┼" in lines[1]


def test_separator_column_count_matches_header() -> None:
    result = render_table(["X", "Y", "Z"], [["1", "2", "3"]])
    lines = result.removeprefix("<pre>").removesuffix("</pre>").splitlines()
    sep_line = lines[1]
    # The separator should contain exactly 2 '┼' for 3 columns.
    assert sep_line.count("┼") == 2


def test_html_escape_in_cells() -> None:
    result = render_table(["Tag"], [["<b>bold</b>"]])
    assert "&lt;b&gt;bold&lt;/b&gt;" in result
    # Raw tags must NOT appear.
    assert "<b>" not in result.replace("<pre>", "").replace("</pre>", "")


def test_html_escape_in_header() -> None:
    result = render_table(["A & B"], [["value"]])
    assert "&amp;" in result


def test_rows_joined_with_pipe() -> None:
    result = render_table(["Col1", "Col2"], [["foo", "bar"]])
    inner = result.removeprefix("<pre>").removesuffix("</pre>")
    # At least one row line should contain ' │ '
    assert " │ " in inner


def test_empty_rows() -> None:
    """Table with no body rows is still valid."""
    result = render_table(["H1", "H2"], [])
    assert "<pre>" in result
    assert "H1" in result


def test_empty_header_returns_empty_string() -> None:
    result = render_table([], [])
    assert result == ""


def test_short_rows_normalised() -> None:
    """Rows with fewer cells than header are padded with empty strings."""
    result = render_table(["A", "B", "C"], [["1"]])
    assert "<pre>" in result


@pytest.mark.parametrize("header,rows,expected_col_count", [
    (["X"], [["val"]], 1),
    (["A", "B"], [["1", "2"], ["3", "4"]], 2),
    (["P", "Q", "R"], [["a", "b", "c"]], 3),
])
def test_correct_column_count(
    header: list[str],
    rows: list[list[str]],
    expected_col_count: int,
) -> None:
    result = render_table(header, rows)
    lines = result.removeprefix("<pre>").removesuffix("</pre>").splitlines()
    sep_line = lines[1]
    assert sep_line.count("┼") == expected_col_count - 1
