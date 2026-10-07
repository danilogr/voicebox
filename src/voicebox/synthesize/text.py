"""What of a text is spoken, and in which pieces it is sent to the speech server.

Pure: no I/O. Markdown loses only its markup - heading markers, emphasis characters,
link targets, fences and the code they hold - and every word of the prose is kept.
Chunks follow paragraphs, then sentences, then clauses, then words, and a word longer
than a chunk is cut rather than dropped: nothing a reader wrote is ever lost.
"""

import re
from dataclasses import dataclass

MARKDOWN_SUFFIXES = frozenset({".md", ".markdown", ".mdown", ".mkd", ".mdx"})

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+")
_HEADING_TAIL = re.compile(r"\s+#+\s*$")
_SETEXT = re.compile(r"^\s{0,3}(=+|-+)\s*$")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_QUOTE = re.compile(r"^\s{0,3}>\s?")
_LIST = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])\s+")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*\S+")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_TAG = re.compile(r"</?[A-Za-z][^>\n]*>")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_REFERENCE_LINK = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_AUTOLINK = re.compile(r"<(?:https?|mailto):[^>]+>")
_BARE_URL = re.compile(r"https?://\S+")
_CODE = re.compile(r"(`+)([^`]+?)\1")
_STAR = re.compile(r"(\*{1,3})(?=\S)(.+?)(?<=\S)\1")
_UNDERSCORE = re.compile(r"(?<!\w)(_{1,3})(?=\S)(.+?)(?<=\S)\1(?!\w)")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")

# Sentence ends (with any closing quote or bracket) and clause breaks, followed by the
# whitespace a chunk may be cut at. Typographic characters are written as escapes.
_SENTENCE_END = re.compile(r"[.!?\u2026][\"'\u201d\u2019)\]]*(\s+)")
_CLAUSE_END = re.compile(r"[,;:\u2014\u2013](\s+)")
_FINAL = re.compile(r"[.!?\u2026][\"'\u201d\u2019)\]]*$")
_TRAILING_CLAUSE = re.compile(r"[,;:\u2014\u2013]+$")


def looks_like_markdown(text: str) -> bool:
    """Whether pasted text is markdown: a heading, a fence, a link or image, or `**`."""
    if "**" in text or _LINK.search(text) or _IMAGE.search(text):
        return True
    return any(_HEADING.match(line) or _FENCE.match(line) for line in text.splitlines())


def strip_markdown(text: str) -> str:
    """The prose of a markdown text, with paragraphs still separated by blank lines."""
    lines: list[str] = []
    fence: str | None = None
    for line in _COMMENT.sub("", text).splitlines():
        opened = _FENCE.match(line)
        if fence is not None:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence):
                fence = None
            continue
        if opened:
            fence = opened.group(1)
            lines.append("")
            continue
        if _SETEXT.match(line) or _RULE.match(line) or _TABLE_RULE.match(line):
            lines.append("")
            continue
        if _REFERENCE.match(line):
            continue
        heading = _HEADING.match(line) is not None
        line = _LIST.sub("", _QUOTE.sub("", line))
        if heading:
            line = _HEADING_TAIL.sub("", _HEADING.sub("", line))
        if line.strip().startswith("|"):
            line = ", ".join(cell.strip() for cell in line.strip().strip("|").split("|"))
        line = _inline(line)
        lines += ["", line, ""] if heading else [line]
    return "\n".join(lines)


def _inline(line: str) -> str:
    line = _TAG.sub("", line)
    line = _IMAGE.sub(r"\1", line)
    line = _LINK.sub(r"\1", line)
    line = _REFERENCE_LINK.sub(r"\1", line)
    line = _AUTOLINK.sub("", line)
    line = _BARE_URL.sub("", line)
    line = _CODE.sub(r"\2", line)
    for _ in range(3):  # nested emphasis, `**bold _and_ more**`, peels one layer a pass
        before = line
        line = _STRIKE.sub(r"\1", _UNDERSCORE.sub(r"\2", _STAR.sub(r"\2", line)))
        if line == before:
            break
    return line


def paragraphs(text: str) -> list[str]:
    """Blank-line-separated blocks, each collapsed to one line, empty ones dropped."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = (" ".join(block.split()) for block in re.split(r"\n\s*\n", text))
    return [block for block in blocks if block]


@dataclass(frozen=True, slots=True)
class Chunk:
    text: str
    # The last chunk of its paragraph: a longer pause follows it.
    paragraph_end: bool


def chunk_text(text: str, *, max_chars: int) -> list[Chunk]:
    """`text`'s paragraphs, each packed into pieces of at most `max_chars` characters.

    Every chunk ends in sentence-final punctuation so the speech model closes the
    phrase: a chunk that does not gets a full stop (replacing a trailing clause mark).
    That stop counts against `max_chars`.

    Raises:
        ValueError: `max_chars` is under 2, too small for a character and its stop.
    """
    if max_chars < 2:
        raise ValueError(f"max_chars must be at least 2, not {max_chars}")
    budget = max_chars - 1
    chunks: list[Chunk] = []
    for paragraph in paragraphs(text):
        pieces = _pack(_units(paragraph, budget), budget)
        chunks += [Chunk(_close(piece), i == len(pieces) - 1) for i, piece in enumerate(pieces)]
    return chunks


def _close(piece: str) -> str:
    if _FINAL.search(piece):
        return piece
    return _TRAILING_CLAUSE.sub("", piece) + "."


def _split_after(pattern: re.Pattern[str], text: str) -> list[str]:
    """`text` cut at each whitespace run `pattern` marks; the punctuation stays left."""
    pieces, start = [], 0
    for match in pattern.finditer(text):
        pieces.append(text[start : match.start(1)])
        start = match.end(1)
    pieces.append(text[start:])
    return [piece for piece in pieces if piece]


def _units(paragraph: str, max_chars: int) -> list[str]:
    """Sentences, or for a sentence over the limit its clauses, or their words: every
    unit at most `max_chars` long."""
    units: list[str] = []
    for sentence in _split_after(_SENTENCE_END, paragraph):
        if len(sentence) <= max_chars:
            units.append(sentence)
            continue
        for clause in _split_after(_CLAUSE_END, sentence):
            units += [clause] if len(clause) <= max_chars else _words(clause, max_chars)
    return units


def _words(text: str, max_chars: int) -> list[str]:
    pieces: list[str] = []
    for word in text.split():
        pieces += [word[i : i + max_chars] for i in range(0, len(word), max_chars)]
    return _pack(pieces, max_chars)


def _pack(units: list[str], max_chars: int) -> list[str]:
    packed: list[str] = []
    for unit in units:
        if packed and len(packed[-1]) + 1 + len(unit) <= max_chars:
            packed[-1] = f"{packed[-1]} {unit}"
        else:
            packed.append(unit)
    return packed
