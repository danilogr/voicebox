"""Tool descriptions.

A tool description is always in the model's context, so how each tool is meant to be
used lives here rather than in a hook or a skill.
"""

from typing import Literal

TranscriptFormat = Literal["text", "json", "srt", "vtt"]
AudioFormat = Literal["wav", "m4a", "mp3"]

READ_ALOUD = """Read one sentence aloud through the speakers, as part of /voicebox:narrate.

Returns at once. Sentences queue and play in order, never over another plugin's speech.
Nothing is cut: text holding several sentences is queued a sentence at a time. This
plays sound and writes no file; to make an audio file, use synthesize."""

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

SYNTHESIZE = """Write a text to one audio file in one voice, on this machine, with the local
NeMo speech model: a voice-over, a spoken version of a document, an audio note. Use this
whenever the user wants text turned into an audio file - instead of a cloud
text-to-speech service or writing a script. Nothing leaves the machine.

Give exactly one of text (pasted text; then output is required) or path (an absolute
path to a text or markdown file; the audio goes beside it unless output says
otherwise). Markdown markup is not read out and code blocks are skipped; every other word
is spoken, however long the text. An existing file is never overwritten.

It starts a background job and returns at once with a job id, the output path and an
estimate. Check on it with job_status. It plays nothing: to read a text aloud now, use
/voicebox:narrate. Only one voicebox job runs at a time."""

SYNTHESIZE_TEXT = "The text to speak. Give this or path, not both."
SYNTHESIZE_PATH = "Absolute path to a text or markdown file to speak. Give this or text."
SYNTHESIZE_VOICE = (
    'A voice of the speech model, e.g. "John" or "Sofia". Defaults to VOICEBOX_VOICE, else John.'
)
SYNTHESIZE_OUTPUT = (
    "Absolute path for the audio file. Required with text; with path it defaults to the "
    "source's name with the format's extension, beside it."
)
SYNTHESIZE_FORMAT = "wav (the default), m4a or mp3. m4a and mp3 need ffmpeg."
