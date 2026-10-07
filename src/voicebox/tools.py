"""Tool descriptions.

A tool description is always in the model's context, so how each tool is meant to be
used lives here rather than in a hook or a skill.
"""

from typing import Literal, get_args

# Why a sentence was spoken. Carried to the queue unchanged; voicebox reads aloud only
# for /voicebox:narrate, which passes "narration".
Intent = Literal["narration", "unspecified"]
INTENT_VALUES: tuple[str, ...] = get_args(Intent)

TranscriptFormat = Literal["text", "json", "srt", "vtt"]

SPEAK = """Say one sentence aloud through the speakers. /voicebox:narrate uses it to read a
long text in order; pass intent="narration" then.

Returns immediately; sentences queue and play in order without overlapping, and never
over another plugin's speech."""

STOP_SPEAKING = """Stop talking immediately: silence the current sentence and drop anything
queued behind it."""

SPEECH_STATUS = """Report what the speech queue is holding: how many sentences are waiting,
being synthesized, prepared and ready, whether one is playing, and how many have been
spoken so far."""

TEXT = "One sentence to say out loud."
VOICE_OVERRIDE = 'Override the configured voice, e.g. "Sofia".'
RATE = "Words per minute, for engines that take a rate."
INTENT = 'Why this sentence is spoken: "narration" for /voicebox:narrate, else omit it.'

TRANSCRIBE = """Transcribe an audio or video file to text on this machine, with the local
NeMo speech model. Use this whenever the user asks to transcribe a recording,
meeting, interview, voice memo or video - instead of Whisper, a cloud speech API,
or writing a script to do it. Nothing leaves the machine.

It starts a background job and returns at once with a job id, the path the
transcript will be written to, and an estimate: roughly a minute of work for every
seven minutes of audio. Check on it with transcription_status. Speakers are
labelled by default; turn diarize off for a single speaker to save about a
quarter of the time. Only one job runs at a time.

The transcript is written to a file, not returned. Read the file only when you
need more than the preview transcription_status gives."""

TRANSCRIPTION_STATUS = """Report on transcription jobs started with transcribe. With a job id,
that job: still running and how long it has left, finished with its output path
and a short preview, or failed with the reason. Without one, the recent jobs.

The preview is the first few hundred characters; read the output file for the
rest."""

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
TRANSCRIPTION_JOB_ID = "A job id from transcribe. Omit it to list recent jobs."
