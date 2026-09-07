import pytest

from speak.text import DEFAULT_MAX_CHARS, normalize


def test_collapses_all_whitespace_to_single_spaces():
    assert normalize("  hello\n\tthere   world  ") == "hello there world"


def test_rejects_text_that_is_only_whitespace():
    with pytest.raises(ValueError, match="nothing to speak"):
        normalize("   \n\t ")


def test_rejects_none():
    with pytest.raises(ValueError, match="nothing to speak"):
        normalize(None)


def test_keeps_text_at_the_cap_untouched():
    assert normalize("abcde", max_chars=5) == "abcde"


def test_truncates_at_a_word_boundary_with_an_ellipsis():
    assert normalize("one two three four", max_chars=11) == "one two…"


def test_truncates_mid_word_when_a_single_word_exceeds_the_cap():
    assert normalize("supercalifragilistic", max_chars=5) == "super…"


def test_default_cap_is_four_hundred():
    assert DEFAULT_MAX_CHARS == 400
    assert len(normalize("word " * 200)) <= DEFAULT_MAX_CHARS + 1
