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
_BULLET = re.compile(r"^\s*[-*+]\s+")
_NUMBERED = re.compile(r"^\s*\d{1,9}[.)]\s+")
_TABLE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*\S+(\s+(\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*$")
_CODE_OR_COMMENT = re.compile(r"(`+)([^`]+?)\1|<!--.*?-->", re.DOTALL)
_TAG = re.compile(r"</?[A-Za-z][^>\n]*>")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_REFERENCE_LINK = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_AUTOLINK = re.compile(r"<(?:https?|mailto):[^>]+>")
_BARE_URL = re.compile(r"([ \t]*)(\()?(https?://\S+)")
_CODE = re.compile(r"(`+)([^`]+?)\1")
_HOLDER = re.compile("\ue000(\\d+)\ue001")
_URL_TAIL = re.compile(r"[.,;:!?)\]'\"]*$")
_STAR = re.compile(r"(\*{1,3})(?=\S)(.+?)(?<=\S)\1")
_UNDERSCORE = re.compile(r"(?<!\w)(_{1,3})(?=\S)(.+?)(?<=\S)\1(?!\w)")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")

# Sentence ends (with any closing quote or bracket) and clause breaks, followed by the
# whitespace a chunk may be cut at. Typographic characters are written as escapes.
_SENTENCE_END = re.compile(r"[.!?\u2026][\"'\u201d\u2019)\]]*(\s+)")
_CLAUSE_END = re.compile(r"[,;:\u2014\u2013](\s+)")
_WORD = re.compile(r"\w")
_FINAL = re.compile(r"[.!?\u2026][\"'\u201d\u2019)\]]*$")
_TRAILING_CLAUSE = re.compile(r"\s*[,;:\u2014\u2013]+$")


def looks_like_markdown(text: str) -> bool:
    """Whether pasted text is markdown: a heading, a fence, a link or image, or `**`."""
    if "**" in text or _LINK.search(text) or _IMAGE.search(text):
        return True
    return any(_HEADING.match(line) or _FENCE.match(line) for line in text.splitlines())


def strip_markdown(text: str) -> str:
    """The prose of a markdown text, with paragraphs still separated by blank lines.

    Inline code is kept as written (its text is spoken, its markers are not), and
    nothing in it is read as markup.
    """
    codes: list[str] = []
    lines: list[str] = []
    list_may_start = True  # a numbered marker only opens a list, never a wrapped line
    for line in _hold_code(_without_fences(text), codes).splitlines():
        if _SETEXT.match(line) or _RULE.match(line) or _TABLE_RULE.match(line) or not line:
            lines.append("")
            list_may_start = True
            continue
        if _REFERENCE.match(line):
            continue
        heading = _HEADING.match(line) is not None
        line = _QUOTE.sub("", line)
        bullet = _BULLET.match(line) is not None
        numbered = list_may_start and _NUMBERED.match(line) is not None
        line = _NUMBERED.sub("", _BULLET.sub("", line)) if numbered or bullet else line
        if heading:
            line = _HEADING_TAIL.sub("", _HEADING.sub("", line))
        if line.strip().startswith("|"):
            line = ", ".join(cell.strip() for cell in line.strip().strip("|").split("|"))
        line = _inline(line)
        lines += ["", line, ""] if heading else [line]
        list_may_start = heading or bullet or numbered
    return _HOLDER.sub(lambda m: codes[int(m.group(1))], "\n".join(lines))


def _without_fences(text: str) -> str:
    """`text` with each fenced block removed (an unclosed one runs to the end) and its
    place marked by a blank line, then with comments removed from what is left."""
    kept: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        opened = _FENCE.match(line)
        if fence is not None:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence):
                fence = None
            continue
        if opened:
            fence = opened.group(1)
            kept.append("")
            continue
        kept.append(line)
    return _CODE_OR_COMMENT.sub(lambda m: m.group(0) if m.group(1) else "", "\n".join(kept))


def _hold_code(text: str, codes: list[str]) -> str:
    """Replace each inline code span by a one-token placeholder, remembering its text."""

    def hold(match: re.Match[str]) -> str:
        codes.append(" ".join(match.group(2).split()))
        return f"\ue000{len(codes) - 1}\ue001"

    return _CODE.sub(hold, text)


def _drop_url(match: re.Match[str]) -> str:
    """Remove a URL (and the parenthesis it opened), keeping the punctuation after it."""
    space, opener, url = match.groups()
    tail = _URL_TAIL.search(url)
    kept = tail.group() if tail else ""
    if opener and kept.startswith(")"):
        kept = kept[1:]
    if kept and kept[0] in ".,;:!?":
        space = ""
    return space + kept


def _inline(line: str) -> str:
    line = _TAG.sub("", line)
    line = _IMAGE.sub(r"\1", line)
    line = _LINK.sub(r"\1", line)
    line = _REFERENCE_LINK.sub(r"\1", line)
    line = _AUTOLINK.sub("", line)
    line = _BARE_URL.sub(_drop_url, line)
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

    A piece holding no word (only punctuation) is not spoken.
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
        pieces = [p for p in _pack(_units(paragraph, budget), budget) if _WORD.search(p)]
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
