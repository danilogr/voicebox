"""The measured limits stay inside what the chunker and the worker assume."""

from voicebox.synthesize import limits


def test_a_chunk_holds_at_least_a_sentence_and_at_most_what_was_measured():
    assert 100 <= limits.CHUNK_CHARS <= 1500


def test_a_request_gets_at_least_a_minute():
    assert limits.REQUEST_TIMEOUT_S >= 60


def test_the_estimate_has_a_positive_rate_and_a_cold_start():
    assert limits.SYNTH_CHARS_PER_S > 0
    assert limits.COLD_START_S > 0
