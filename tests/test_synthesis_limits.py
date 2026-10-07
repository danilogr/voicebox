"""The measured limits stay inside what the chunker and the worker assume."""

import pytest

from voicebox.synthesize import limits


def test_a_chunk_holds_at_least_a_sentence_and_at_most_what_was_measured():
    assert 100 <= limits.CHUNK_CHARS <= 1500


def test_a_request_gets_at_least_a_minute():
    assert limits.REQUEST_TIMEOUT_S >= 60


def test_the_estimate_has_a_positive_rate_and_a_cold_start():
    assert limits.SYNTH_CHARS_PER_S > 0
    assert limits.COLD_START_S > 0


# On the CPU backend (~0.07x realtime) a full chunk, about 90 s of audio, takes about
# 21 minutes (performance.md): 0.84 s a character.
CPU_SECONDS_PER_CHAR = 21 * 60 / 1500


@pytest.mark.parametrize("chars", [100, 300, 750, limits.CHUNK_CHARS])
def test_a_first_request_waits_long_enough_for_a_cpu_only_machine(chars):
    timeout = limits.request_timeout_s(chars, None)
    assert chars * CPU_SECONDS_PER_CHAR < timeout <= limits.FIRST_REQUEST_CEILING_S


def test_the_first_request_is_bounded_and_never_under_the_measured_timeout():
    assert limits.FIRST_REQUEST_CEILING_S == 2700
    assert limits.request_timeout_s(10_000, None) == limits.FIRST_REQUEST_CEILING_S
    assert limits.request_timeout_s(10, None) == limits.REQUEST_TIMEOUT_S


def test_later_requests_wait_four_times_the_slowest_observed_pace():
    assert limits.request_timeout_s(1500, 1.0) == 6000
    assert limits.request_timeout_s(1500, 0.5) == 12000  # no ceiling once measured
    assert limits.request_timeout_s(100, 29.5) == limits.REQUEST_TIMEOUT_S
