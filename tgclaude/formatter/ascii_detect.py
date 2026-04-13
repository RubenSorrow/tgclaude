from __future__ import annotations

# Box-drawing characters that signal ASCII art / diagram content
BOX_CHARS: frozenset[str] = frozenset("┌┐└┘│─━┃═║╔╗╚╝◆▶▲▼")


def is_ascii_art(paragraph_text: str) -> bool:
    """Return True if the paragraph should be wrapped in <pre>.

    Criteria (either is sufficient):
    1. Contains any character from BOX_CHARS
    2. Any line ends with 2+ trailing spaces (alignment padding)
    """
    if any(ch in BOX_CHARS for ch in paragraph_text):
        return True

    for line in paragraph_text.splitlines():
        if line.endswith("  "):
            return True

    return False
