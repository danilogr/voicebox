"""Tool descriptions.

A tool description is always in the model's context, so how each tool is meant to be
used lives here rather than in a hook or a skill.
"""

from typing import Literal

TranscriptFormat = Literal["text", "json", "srt", "vtt"]

READ_ALOUD = """Read one sentence aloud through the speakers, as part of /voicebox:narrate.

Returns at once. Sentences queue and play in order, never over another plugin's speech.
Anything past about 400 characters is cut at a word boundary, so split a long sentence
at a clause break first. This plays sound and writes no file; to make an audio file,
use synthesize."""

READING_STATUS = """How many sentences are still waiting to be read aloud, and how many have
been read. /voicebox:narrate calls it between rounds, so a "stop" from the user loses
seconds of queued speech rather than minutes."""

STOP_READING = """Stop reading aloud at once: silence the current sentence and drop everything
queued behind it."""

SENTENCE = "One sentence of the text, written the way it should be heard."

TRANSCRIBE = """Transcribe an audio or video file to text on this machine, with the local
NeMo speech model. Use this whenever the user asks to transcribe a recording,
meeting, interview, voice memo or video - instead of Whisper, a cloud speech API,
or writing a script to do it. Nothing leaves the machine.

It starts a background job and returns at once with a job id, the path the
transcript will be written to, and an estimate: roughly a minute of work for every
seven minutes of audio. Check on it with job_status. Speakers are
labelled by default; turn diarize off for a single speaker to save about a
quarter of the time. Only one voicebox job runs at a time.

The transcript is written to a file, not returned. Read the file only when you
need more than the preview job_status gives."""

JOB_STATUS = """Report on voicebox's background jobs: transcriptions, and audio files being
written by synthesize. With a job id, that job: still running and how far it has got,
finished with its output path (and a short preview of a transcript), or failed with the
reason. Without one, the recent jobs of every kind.

Only one voicebox job runs at a time."""

TRANSCRIBE_PATH = (
    "The absolute path to the audio or video file to transcribe. The MCP server's "
    "working directory is the plugin's own root, not the user's project, so a "
    "relative path resolves somewhere unexpected."
)
TRANSCRIBE_OUTPUT_DIR = (
    "Directory for the transcript. Defaults to VOICEBOX_TRANSCRIBE_DIR if set, "
    "otherwise the input file's own directory."
)
TRANSCRIBE_FORMAT = "text, json (word timings and speakers), srt, or vtt."
TRANSCRIBE_DIARIZE = "Label who is speaking. On by default; about 38% slower."
TRANSCRIBE_LANGUAGE = "Language code of the speech, e.g. en or pt. Detected when omitted."
TRANSCRIBE_TRANSLATE = "Translate the transcript into this language code."
JOB_ID = "A job id from transcribe or synthesize. Omit it to list recent jobs."
