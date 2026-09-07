"""Turn arbitrary text into something worth saying out loud."""

import re

DEFAULT_MAX_CHARS = 400

_WHITESPACE = re.compile(r"\s+")


def normalize(text: str | None, *, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Collapse to one line and cap the length, truncating at a word boundary.

    Raises:
        ValueError: if nothing is left after collapsing.
    """
    collapsed = _WHITESPACE.sub(" ", text or "").strip()
    if not collapsed:
        raise ValueError("nothing to speak: text is empty")
    if len(collapsed) <= max_chars:
        return collapsed

    cut = collapsed[:max_chars]
    last_space = cut.rfind(" ")
    return (cut[:last_space] if last_space > 0 else cut) + "…"
