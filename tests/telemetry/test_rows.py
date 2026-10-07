"""`job_row`: every value inside the contract before it is stored, and a caller's bug
raised rather than shipped."""

import pytest

from voicebox.telemetry.rows import (
    CUSTOM_VOICE,
    NARRATION,
    SYNTHESIS,
    TRANSCRIPTION,
    JobFacts,
    iso,
    job_row,
    recorded_voice,
    wall_ms,
)

JOB = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SESSION = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
INSTALL = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def facts(kind=TRANSCRIPTION, status="ok", **overrides: object) -> JobFacts:
    fields = dict(
        job_id=JOB,
        session_id=SESSION,
        install_id=INSTALL,
        kind=kind,
        started_at=iso(1_000_000_000),
        ended_at=iso(1_000_000_060),
        wall_ms=60_000,
        status=status,
        engine="nemo",
    )
    return JobFacts(**{**fields, **overrides})


def test_a_reason_is_kept_only_on_failed_and_refused_rows():
    assert job_row(facts(status="failed", reason="timeout", stage="asr"))["reason"] == "timeout"
    assert job_row(facts(status="refused", reason="busy"))["reason"] == "busy"
    assert job_row(facts(status="ok", reason="busy"))["reason"] is None
    assert job_row(facts(status="crashed", reason="busy", stage="asr"))["reason"] is None


def test_a_missing_or_unknown_reason_becomes_other():
    assert job_row(facts(status="failed", stage="asr"))["reason"] == "other"
    assert job_row(facts(status="refused", reason="cosmic_rays"))["reason"] == "other"


def test_a_stage_is_kept_only_on_failed_and_crashed_rows_and_defaults_per_kind():
    assert job_row(facts(status="crashed", stage="convert"))["stage"] == "convert"
    assert job_row(facts(status="ok", stage="convert"))["stage"] is None
    assert job_row(facts(status="refused", reason="busy", stage="probe"))["stage"] is None
    assert job_row(facts(status="failed", reason="other"))["stage"] == "asr"
    assert job_row(facts(SYNTHESIS, "crashed", stage="elsewhere"))["stage"] == "synthesize"
    assert job_row(facts(NARRATION, "failed", reason="other"))["stage"] == "play"


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        (SYNTHESIS, "empty"),
        (TRANSCRIPTION, "stopped"),
        (NARRATION, "crashed"),
        (NARRATION, "empty"),
    ],
)
def test_a_status_the_kind_cannot_have_is_a_bug(kind, status):
    with pytest.raises(ValueError, match="cannot be"):
        job_row(facts(kind, status))


def test_a_field_from_another_kind_is_a_bug():
    with pytest.raises(ValueError, match="does not carry 'voice'"):
        job_row(facts(TRANSCRIPTION), {"voice": "John"})
    with pytest.raises(ValueError, match="does not carry 'source_bytes'"):
        job_row(facts(SYNTHESIS), {"source_bytes": 1})


def test_numbers_are_clamped_to_their_kinds_own_ceilings():
    transcription = job_row(facts(wall_ms=10**12), {"audio_ms": 10**12, "speakers": 99})
    synthesis = job_row(facts(SYNTHESIS), {"audio_ms": 10**12, "words": -5})
    assert transcription["wall_ms"] == 172_800_000
    assert (transcription["audio_ms"], transcription["speakers"]) == (86_400_000, 64)
    assert (synthesis["audio_ms"], synthesis["words"]) == (172_800_000, 0)


def test_strings_that_do_not_fit_their_pattern_are_dropped():
    row = job_row(
        facts(model="/models/magpie.gguf"),
        {"audio_codec": "mp4a/40", "language": "português", "format": "docx"},
    )
    assert (row["model"], row["audio_codec"], row["language"], row["format"]) == (
        None,
        None,
        None,
        None,
    )
    assert job_row(facts(), {"language": "pt-BR"})["language"] == "pt-BR"


def test_an_engine_that_does_not_fit_is_unknown_because_engine_is_required():
    assert job_row(facts(engine="my engine"))["engine"] == "unknown"


def test_booleans_are_booleans_and_none_stays_none():
    row = job_row(facts(), {"has_video": 1, "converted": None})
    assert row["has_video"] is True and row["converted"] is None


@pytest.mark.parametrize(
    ("engine", "voice", "recorded"),
    [
        ("nemo", "sofia", "Sofia"),
        ("nemo", "My Voice", CUSTOM_VOICE),
        ("say", "Samantha", "Samantha"),
        ("command", "Samantha", CUSTOM_VOICE),
        ("nemo", None, None),
    ],
)
def test_only_a_shipped_voice_name_is_recorded_as_itself(engine, voice, recorded):
    assert recorded_voice(engine, voice) == recorded


def test_wall_ms_uses_the_monotonic_clock_until_a_reboot_resets_it():
    assert wall_ms(1000.0, 50.0, now=1010.0, now_monotonic=52.5) == 2500
    assert wall_ms(1000.0, 50.0, now=1010.0, now_monotonic=3.0) == 10_000
    assert wall_ms(1000.0, 50.0, now=990.0, now_monotonic=3.0) == 0
    assert wall_ms(1000.0, None, now=1004.0, now_monotonic=99.0) == 4000


def test_a_string_with_a_trailing_newline_is_stripped_not_shipped_raw():
    row = job_row(facts(engine="nemo\n", model="magpie\n"), {"audio_codec": "aac\n"})
    assert (row["engine"], row["model"], row["audio_codec"]) == ("nemo", "magpie", "aac")


def test_a_timestamp_that_is_not_utc_is_a_bug():
    with pytest.raises(ValueError, match="UTC timestamp"):
        job_row(facts(started_at="2026-10-06T10:00:00+02:00"))


def test_timestamps_match_the_contracts_pattern():
    import re

    assert re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z",
        iso(1_000_000_000.25),
    )
