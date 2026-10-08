"""What of a text is spoken, and in which pieces it is sent to the speech server.

Pure: no I/O. Markdown loses only its markup - heading markers, emphasis characters,
link targets, fences and the code they hold - and every word of the prose is kept.
Chunks follow paragraphs, then sentences, then clauses, then words, and a word longer
than a chunk is cut rather than dropped: nothing a reader wrote is ever lost.
"""

import re
from dataclasses import dataclass

from speak_core.text import close, pieces

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
# A code span never crosses a blank line, so a stray backtick pairs with nothing beyond
# its paragraph.
_SPAN = r"(`+)((?:[^`\n]|\n(?![ \t]*\n))+?)\1"
_CODE_OR_COMMENT = re.compile(_SPAN + r"|<!--.*?-->", re.DOTALL)
_ONE = re.compile(r"^\s*0*1[.)]\s+")
_TAG = re.compile(r"</?[A-Za-z][^>\n]*>")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_REFERENCE_LINK = re.compile(r"\[([^\]]+)\]\[[^\]]*\]")
_AUTOLINK = re.compile(r"<(?:https?|mailto):[^>]+>")
_BARE_URL = re.compile(r"([ \t]*)(\()?(https?://\S+)")
_CODE = re.compile(_SPAN)
_URL_TAIL = re.compile(r"[.,;:!?)\]'\"]*$")
_STAR = re.compile(r"(\*{1,3})(?=\S)(.+?)(?<=\S)\1")
_UNDERSCORE = re.compile(r"(?<!\w)(_{1,3})(?=\S)(.+?)(?<=\S)\1(?!\w)")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")

_WORD = re.compile(r"\w")


def unspeakable(text: str) -> str | None:
    """What in `text` no speech engine can take, or None.

    A NUL cannot travel in a child process's arguments, and an unpaired surrogate cannot
    be encoded as UTF-8 for them, for a file or for the speech server's request.
    """
    if "\x00" in text:
        return "it contains a NUL character"
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return "it contains an unpaired surrogate, which is not valid Unicode text"
    return None


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
    mark = _unused_mark(text)
    holder = re.compile(f"{mark}(\\d+){mark}")
    in_list = False
    list_may_start = True  # a numbered marker other than 1 only continues a list
    for line in _hold_code(_without_fences(text), codes, mark).splitlines():
        if not line.strip() or _SETEXT.match(line) or _RULE.match(line):
            lines.append("")
            in_list, list_may_start = False, True
            continue
        if _TABLE_RULE.match(line):
            lines.append("")
            in_list, list_may_start = False, True
            continue
        if _REFERENCE.match(line):
            continue
        heading = _HEADING.match(line) is not None
        line = _QUOTE.sub("", line)
        bullet = _BULLET.match(line) is not None
        numbered = _NUMBERED.match(line) is not None and (
            list_may_start or _ONE.match(line) is not None
        )
        indented = line[:1].isspace()
        line = _NUMBERED.sub("", _BULLET.sub("", line)) if numbered or bullet else line
        if heading:
            line = _HEADING_TAIL.sub("", _HEADING.sub("", line))
        if line.strip().startswith("|"):
            line = ", ".join(cell.strip() for cell in line.strip().strip("|").split("|"))
        line = _inline(line)
        lines += ["", line, ""] if heading else [line]
        if bullet or numbered:
            in_list = list_may_start = True
        elif heading:
            in_list, list_may_start = False, True
        else:  # an indented line continues its list item; the next marker still counts
            in_list = in_list and indented
            list_may_start = in_list
    return holder.sub(lambda m: codes[int(m.group(1))], "\n".join(lines))


def _unused_mark(text: str) -> str:
    """A private-use character absent from `text`, so a placeholder cannot collide."""
    return next(chr(c) for c in range(0xE000, 0xF900) if chr(c) not in text)


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


def _hold_code(text: str, codes: list[str], mark: str) -> str:
    """Replace each inline code span by a one-token placeholder, remembering its text."""

    def hold(match: re.Match[str]) -> str:
        codes.append(re.sub(r"\s+", " ", match.group(2)))
        return f"{mark}{len(codes) - 1}{mark}"

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
    """`text`'s paragraphs, each cut by speak-core's `pieces` into chunks of at most
    `max_chars` characters: one sentence a chunk, because MagpieTTS repeats and garbles
    words when one request holds several (harness/performance.md), a sentence over
    `max_chars` cut at its clauses, then its words, and a one-word sentence such as
    `Dr.` with the sentence after it.

    A piece holding no word (only punctuation) is not spoken.
    Every chunk ends in sentence-final punctuation (`close`) so the speech model closes
    the phrase: a chunk that does not gets a full stop (replacing a trailing clause
    mark). That stop counts against `max_chars`.

    Raises:
        ValueError: `max_chars` is under 2, too small for a character and its stop.
    """
    if max_chars < 2:
        raise ValueError(f"max_chars must be at least 2, not {max_chars}")
    budget = max_chars - 1
    chunks: list[Chunk] = []
    for paragraph in paragraphs(text):
        cut = [piece for piece in pieces(paragraph, max_chars=budget) if _WORD.search(piece)]
        chunks += [Chunk(close(piece), i == len(cut) - 1) for i, piece in enumerate(cut)]
    return chunks
