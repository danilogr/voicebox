"""What of a text is spoken, and the pieces it is sent in: markup goes, words never do."""

import re

import pytest

from voicebox.synthesize.text import (
    Chunk,
    chunk_text,
    looks_like_markdown,
    paragraphs,
    strip_markdown,
)


def letters(text: str) -> str:
    """The text without whitespace or the marks chunking may change (a closing full
    stop, a trailing clause mark)."""
    return "".join(words(text))


def words(text: str) -> list[str]:
    return re.sub(r"[.,;:\u2014\u2013]", "", text).split()


@pytest.mark.parametrize(
    "text",
    [
        "# Title\n\nSome prose.",
        "Intro\n\n```python\nprint(1)\n```\n",
        "See [the docs](https://example.com) for more.",
        "This is **important**.",
        "![diagram](d.png)",
    ],
)
def test_markdown_is_recognised_by_its_markup(text):
    assert looks_like_markdown(text)


@pytest.mark.parametrize(
    "text",
    [
        "Plain prose with a snake_case_name and 2 * 3 * 4 in it.",
        "A path like ~/dev/voicebox/src is not markup.",
        "Hashtags #like this are not headings.",
    ],
)
def test_plain_text_is_not_mistaken_for_markdown(text):
    assert not looks_like_markdown(text)


def test_headings_emphasis_links_and_code_lose_only_their_markup():
    source = (
        "# The *lock* works\n"
        "\n"
        "It uses **an atomic** `mkdir` and [a pid file](https://x.y/z) ~~always~~.\n"
        "Some __strong__ and _soft_ words.\n"
    )
    assert " ".join(strip_markdown(source).split()) == (
        "The lock works It uses an atomic mkdir and a pid file always. Some strong and soft words."
    )


def test_a_fenced_code_block_is_skipped_whole_and_ends_the_paragraph():
    source = "Before the code.\n```sh\nrm -rf /\n```\nAfter the code.\n"
    assert paragraphs(strip_markdown(source)) == ["Before the code.", "After the code."]


def test_an_unclosed_fence_skips_to_the_end_rather_than_reading_code():
    assert paragraphs(strip_markdown("Prose.\n~~~\ncode\nmore code\n")) == ["Prose."]


def test_lists_quotes_tables_and_rules_keep_their_words():
    source = (
        "- first item\n"
        "2. second item\n"
        "> quoted line\n"
        "\n"
        "| name | value |\n"
        "|------|-------|\n"
        "| a | b |\n"
        "\n"
        "---\n"
        "Closing words.\n"
    )
    text = strip_markdown(source)
    kept = ("first item", "second item", "quoted line", "name, value", "a, b", "Closing words.")
    for words in kept:
        assert words in text
    assert "|" not in text and "---" not in text and ">" not in text


def test_images_keep_their_alt_text_and_urls_are_not_read():
    text = strip_markdown("![A diagram of the lock](d.png) see https://example.com/x now")
    assert text.split() == ["A", "diagram", "of", "the", "lock", "see", "now"]


def test_html_comments_tags_and_reference_definitions_are_not_read():
    source = "<!-- hidden\nnote -->Visible <b>bold</b> text.\n\n[1]: https://example.com\n"
    assert paragraphs(strip_markdown(source)) == ["Visible bold text."]


def test_plain_prose_with_markdown_lookalikes_survives_stripping():
    """Review Focus 2: identifiers and arithmetic are not emphasis."""
    source = "Call read_job_record with 2 * 3 * 4 items from ~/dev/voicebox, not a*b."
    stripped = strip_markdown(source)
    assert "read_job_record" in stripped
    assert "2 * 3 * 4" in stripped
    assert "~/dev/voicebox" in stripped


def test_paragraphs_are_blank_line_blocks_collapsed_to_one_line():
    assert paragraphs("one\ntwo\n\n\n  three  \n\n") == ["one two", "three"]


def test_short_paragraphs_are_one_chunk_each_and_end_their_paragraph():
    assert chunk_text("First one.\n\nSecond one.", max_chars=100) == [
        Chunk("First one.", True),
        Chunk("Second one.", True),
    ]


def test_every_sentence_is_a_chunk_of_its_own_however_much_room_is_left():
    text = "Aaa aaa. Bbb bbb! Ccc ccc? Ddd ddd."
    chunks = chunk_text(text, max_chars=1000)
    assert [c.text for c in chunks] == ["Aaa aaa.", "Bbb bbb!", "Ccc ccc?", "Ddd ddd."]
    assert [c.paragraph_end for c in chunks] == [False, False, False, True]


def test_a_long_sentence_is_split_at_clauses_before_words():
    text = "one two three, four five six; seven eight nine"
    assert [c.text for c in chunk_text(text, max_chars=20)] == [
        "one two three.",
        "four five six.",
        "seven eight nine.",
    ]


def test_a_long_sentences_clauses_are_packed_but_never_with_the_next_sentence():
    text = "one, two, three four five six seven. Eight."
    assert [c.text for c in chunk_text(text, max_chars=30)] == [
        "one, two.",
        "three four five six seven.",
        "Eight.",
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Prof. Smith arrived. He sat.", ["Prof. Smith arrived.", "He sat."]),
        ("It rained, e.g. here. Then it stopped.", ["It rained, e.g. here.", "Then it stopped."]),
        ("He left. Yes. Then she did.", ["He left.", "Yes. Then she did."]),
        ("He left. Yes.", ["He left.", "Yes."]),
    ],
)
def test_a_one_word_sentence_goes_with_the_sentence_after_it(text, expected):
    assert [c.text for c in chunk_text(text, max_chars=1000)] == expected


def test_a_one_word_sentence_stands_alone_when_the_next_would_not_fit_with_it():
    assert [c.text for c in chunk_text("Prof. Smith arrived late.", max_chars=20)] == [
        "Prof.",
        "Smith arrived late.",
    ]


SECTION = "## Section\n\n" + "A sentence of ordinary prose, with a clause. " * 40 + "\n\n"
LONG_MARKDOWN = SECTION * 120  # about 218 KB
NO_PUNCTUATION = "word " * 1000
ONE_HUGE_WORD = "x" * 3000 + " tail"


@pytest.mark.parametrize(
    "text",
    [LONG_MARKDOWN, NO_PUNCTUATION, ONE_HUGE_WORD, "Short."],
    ids=["218KB-markdown", "no-punctuation", "3000-char-word", "short"],
)
@pytest.mark.parametrize("limit", [120, 500, 1500])
def test_no_character_is_ever_dropped(text, limit):
    """Review Focus 1: whatever the text, every chunk fits the request and the chunks
    together hold every character of every paragraph."""
    spoken = strip_markdown(text)
    chunks = chunk_text(spoken, max_chars=limit)
    assert all(0 < len(c.text) <= limit for c in chunks)
    assert letters("".join(c.text for c in chunks)) == letters("".join(paragraphs(spoken)))


ONE_LONG_SENTENCE = "The quick brown fox jumps over the lazy dog and " * 120 + "stops."
UNICODE = ("Zażółć gęślą jaźń, naïve café \u2014 \u00fcber \u65e5\u672c\u8a9e text. " * 60).strip()
CRLF = "First line of one.\r\nSecond line, same paragraph.\r\n\r\nSecond paragraph.\r\n" * 30
LONE_CR = "Alpha beta.\r\rGamma delta.\r\rEpsilon."


@pytest.mark.parametrize(
    "text",
    [LONG_MARKDOWN, NO_PUNCTUATION, ONE_LONG_SENTENCE, UNICODE, CRLF, LONE_CR],
    ids=["markdown", "no-punctuation", "long-sentence", "unicode", "crlf", "lone-cr"],
)
@pytest.mark.parametrize("limit", [40, 120, 500, 1500])
def test_no_word_is_dropped_or_duplicated_and_order_is_kept(text, limit):
    """Chunks, read in order, hold the paragraphs' words exactly: the only difference
    is the full stop a chunk gains to end on one."""
    spoken = strip_markdown(text)
    chunks = chunk_text(spoken, max_chars=limit)
    assert all(0 < len(c.text) <= limit for c in chunks)
    assert words(" ".join(c.text for c in chunks)) == words(" ".join(paragraphs(spoken)))


def test_crlf_and_lone_cr_separate_paragraphs_like_lf():
    for text in ("one\r\n\r\ntwo", "one\r\rtwo", "one\n\ntwo"):
        assert [c.text for c in chunk_text(text, max_chars=100)] == ["one.", "two."]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("No full stop", "No full stop."),
        ("Ends with a comma,", "Ends with a comma."),
        ("A clause;", "A clause."),
        ("Done.", "Done."),
        ("Really?", "Really?"),
        ("Stop!", "Stop!"),
        ('He said "go."', 'He said "go."'),
        ("(see above.)", "(see above.)"),
        ("Trailing quote\u201d", "Trailing quote\u201d."),
    ],
)
def test_every_chunk_ends_in_sentence_final_punctuation(text, expected):
    assert [c.text for c in chunk_text(text, max_chars=100)] == [expected]


@pytest.mark.parametrize("limit", [2, 7, 30, 300])
def test_every_chunk_of_a_long_text_ends_in_final_punctuation_within_the_limit(limit):
    ending = re.compile(r"[.!?\u2026][\"'\u201d\u2019)\]]*$")
    chunks = chunk_text(NO_PUNCTUATION + ONE_HUGE_WORD, max_chars=limit)
    assert chunks
    assert all(ending.search(c.text) and len(c.text) <= limit for c in chunks)


def test_a_limit_too_small_for_a_character_and_its_full_stop_is_refused():
    with pytest.raises(ValueError, match="max_chars"):
        chunk_text("word", max_chars=1)


def test_an_empty_text_has_no_chunks():
    assert chunk_text(" \n\n ", max_chars=100) == []


@pytest.mark.parametrize(
    ("source", "spoken"),
    [
        ("[Warning]: do not run this on production.", "[Warning]: do not run this on production."),
        ('[1]: https://example.com "Title"\n\nAfter.', "After."),
        (
            "The `<Button>` component renders `<div>` tags.",
            "The <Button> component renders <div> tags.",
        ),
        (
            "Use `<!--` to open.\n\nMore prose here.\n\nAnd here -->tail.",
            "Use <!-- to open. More prose here. And here -->tail.",
        ),
        ("Born in\n1984. He later moved.", "Born in 1984. He later moved."),
        (
            "Call `__init__.py` or `*args, **kwargs` in `src/**/*.py`.",
            "Call __init__.py or *args, **kwargs in src/**/*.py.",
        ),
        ("Visit https://example.com. Then rest.", "Visit. Then rest."),
        ("See (https://x.y) for more.", "See for more."),
    ],
)
def test_visible_prose_survives_stripping(source, spoken):
    assert " ".join(paragraphs(strip_markdown(source))) == spoken


def test_a_reference_definition_is_dropped_but_a_prose_line_in_brackets_is_not():
    spoken = paragraphs(strip_markdown("[Warning]: do not run this.\n[1]: https://x.y 'T'\n"))
    assert spoken == ["[Warning]: do not run this."]


def test_tags_in_code_spans_are_read_and_tags_outside_are_not():
    spoken = paragraphs(strip_markdown("The `<Button>` and <b>bold</b>."))
    assert spoken == ["The <Button> and bold."]


def test_a_comment_marker_inside_code_does_not_swallow_the_prose_after_it():
    source = "Use `<!--` to open.\n\nMore prose here.\n\nAnd here -->tail."
    assert paragraphs(strip_markdown(source)) == [
        "Use <!-- to open.",
        "More prose here.",
        "And here -->tail.",
    ]


def test_a_real_comment_is_dropped_across_lines():
    assert paragraphs(strip_markdown("Before <!-- a\nb --> after.")) == ["Before after."]


def test_a_year_at_the_start_of_a_wrapped_line_is_not_a_list_marker():
    assert paragraphs(strip_markdown("Born in\n1984. He later moved.")) == [
        "Born in 1984. He later moved."
    ]
    assert paragraphs(strip_markdown("Steps:\n\n1. first\n2. second")) == ["Steps:", "first second"]


def test_a_url_keeps_the_punctuation_that_ended_the_sentence():
    assert paragraphs(strip_markdown("Visit https://example.com. Then rest.")) == [
        "Visit. Then rest."
    ]


def test_text_without_a_word_makes_no_chunk():
    for text in ("| | |", "\u2014", "(https://x.y)", "...", "- - -"):
        assert chunk_text(strip_markdown(text), max_chars=100) == []


def test_a_wordless_piece_between_words_is_not_sent_alone():
    chunks = chunk_text("Hello \u2014, world", max_chars=8)
    assert chunks
    assert all(re.search(r"\w", c.text) for c in chunks)


def test_the_stop_replaces_the_space_before_a_clause_mark():
    assert [c.text for c in chunk_text("end of the line ,", max_chars=100)] == ["end of the line."]


def test_a_stray_backtick_pairs_with_nothing_beyond_its_paragraph():
    source = (
        "Press the ` key.\n\nRun `make` then **build** it.\n\n"
        "Open [the page](https://x.y/z) and call `go`.\n\nDone `now`."
    )
    assert paragraphs(strip_markdown(source)) == [
        "Press the ` key.",
        "Run make then build it.",
        "Open the page and call go.",
        "Done now.",
    ]


def test_a_code_span_keeps_the_spaces_inside_its_markers():
    assert paragraphs(strip_markdown("Type `ls `and enter.")) == ["Type ls and enter."]
    assert paragraphs(strip_markdown("Run `a\n  b` now.")) == ["Run a b now."]


@pytest.mark.parametrize("text", ["Item 0 here", "Option <0> and <12> and 0", ""])
def test_text_that_looks_like_a_placeholder_is_left_alone(text):
    assert paragraphs(strip_markdown(f"{text} and `code`")) == [
        " ".join(f"{text} and code".split())
    ]


def test_a_placeholder_lookalike_with_no_code_does_not_raise():
    assert paragraphs(strip_markdown("7 alone")) == ["7 alone"]


def test_a_list_starting_at_one_may_interrupt_a_paragraph():
    assert paragraphs(strip_markdown("Steps:\n1. one\n2. two")) == ["Steps: one two"]


def test_a_continuation_line_does_not_end_the_list():
    assert paragraphs(strip_markdown("1. first\n   more\n2. second")) == ["first more second"]
    assert paragraphs(strip_markdown("1. first\n   more\n   3. nested")) == ["first more nested"]


def test_whitespace_only_lines_are_blank_lines():
    assert paragraphs(strip_markdown("one\n   \n2. two")) == ["one", "two"]
