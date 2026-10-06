"""Tool descriptions.

A tool description is always in the model's context, so the policy for when to
speak lives here. A hook or a skill cannot add a callable tool, and neither is
guaranteed to be read at the moment the choice is made.
"""

from typing import Literal, get_args

# Why an utterance was spoken, for telemetry only - it never changes what is said
# or how. Each value is the reason already named in `SPEAK` below; kept as one type
# alias so the tool's schema, its description, and the wire contract cannot drift
# apart silently.
Intent = Literal[
    "task_done",
    "blocked",
    "question",
    "recap",
    "progress",
    "takeaway",
    "conversation",
    "narration",
    "unspecified",
]
INTENT_VALUES: tuple[str, ...] = get_args(Intent)

TranscriptFormat = Literal["text", "json", "srt", "vtt"]

SPEAK = """Speak one short sentence aloud to the user through the speakers.

Use it at the moments where a voice beats scrollback: when you have finished a task,
when you are blocked and need input, and when you are asking the user a question.
Do not narrate routine progress and do not read out code, paths, or long output.

End a substantial turn with a one-line recap: the outcome, plus any catch on it -
"Voices set to random; takes effect next reload." Lead with what changed, not with
what you did. If the turn ended blocked, or the work is partly done, say that instead
of "done" - a recap the user cannot trust is worse than silence. Skip the recap on a
lookup, a one-line answer, or a question already on screen.

Set intent to the reason above that applies: task_done, blocked, question, recap, or
progress for the one case this tool asks you not to use. takeaway names an important
finding said out loud, conversation is /speak:converse, narration is /speak:narrate
reading a text aloud, and unspecified - the default - is for anything else. This is
for measurement only and never changes what is said.

Write one short sentence of plain spoken English - it is heard, not read.
Returns immediately; utterances queue and play in order without overlapping."""

LIST_VOICES = """List the voices the current speech engine can use, and say which one is active.
Call this before set_voice; the names depend on the engine and, for a local
speech model, on the model file that is loaded."""

SET_VOICE = """Change the voice used for later speech. Use it when the user asks to sound different.
The choice lasts for this session only and does not affect other Claude Code windows."""

STOP_SPEAKING = """Stop talking immediately: silence the current utterance and drop anything
queued behind it."""

SPEECH_STATUS = """Report what the speech queue is holding: how many utterances are
waiting, being synthesized, prepared and ready, whether one is playing, and how many
have been spoken so far.

Use it when speech seems slow or stuck, and before deciding to wait: synthesis costs
about as much time as the audio it produces, so a long answer legitimately occupies the
queue for minutes on a slow machine. A queue with work in hand is busy, not broken."""

TEXT = "One short sentence to say out loud."
VOICE_OVERRIDE = 'Override the configured voice, e.g. "Samantha".'
RATE = "Words per minute. The macOS default is about 175."
VOICE_NAME = 'A name from list_voices, e.g. "Sofia".'
INTENT = (
    "Why this sentence is being spoken; for measurement only, never changes what is "
    "said. task_done, blocked, question, recap, progress, takeaway, conversation, "
    "narration, or unspecified (the default when omitted)."
)

LISTEN = """Listen to the user through the microphone and return what they said.

Use it after asking the user a question with `speak`, so they can answer out loud
instead of typing. Recording starts once anything queued has finished playing and
stops on its own when the user stops talking.

The transcript comes from a small on-device model and may contain errors. Treat it
as what the user meant, not as a literal quotation, and confirm before acting on it
if a misheard word would be costly."""

LISTEN_SECONDS = (
    "Longest recording to allow, in seconds. Must be positive, and is capped by the "
    "configured limit, which is also the default."
)

NO_ASR = """The speech server is running without a transcription model, so there is nothing
to listen with. A server's models are chosen when it starts and cannot change while it
runs, so this one will not gain transcription on its own.

Either the model was never downloaded - run /speak:setup to pull it - or this shared
server was started by a session that had voice input switched off. It stops only when
the last session using it exits, so restarting one session does not help while others
are still attached: close the other Claude Code sessions using speak, or stop the
server process recorded in server.json in the shared speak-core state directory. The next listen
then starts one with transcription."""

STILL_SPEAKING = (
    "the speech queue is wedged - nothing finished playing while waiting for it ({pending}) "
    "- so the microphone was not opened. Recording now would capture this session's own "
    "voice and hand it back as the user's answer. Call stop_speaking, then listen again."
)

SERVER_STARTING = (
    "the speech server is starting up and has not loaded its models yet, so there is "
    "nothing to transcribe with for another few seconds. Wait, then ask again - nothing "
    "needs installing or restarting."
)

SERVER_UNREACHABLE = (
    "the speech server is not answering, so there is nothing to transcribe with. Run "
    "/speak:setup if nemo-speech is not installed yet, check NEMO_HTTP_URL if it serves "
    "somewhere other than the default port, and start it yourself if you set "
    "NEMO_AUTOSTART=0."
)

NO_SERVER = (
    "could not start the speech server. Run /speak:setup to install nemo-speech and pull "
    "the models, or set NEMO_BIN to it if it is installed somewhere else."
)

ALREADY_LISTENING = (
    "this session is already listening; wait for that recording to finish before asking again"
)

HEARD_NOTHING = "heard nothing"

# The sentinel /talk prints when the recording held no speech. `commands/talk.md`
# branches on this exact line, so it lives here rather than as a literal in both.
TALK_HEARD_NOTHING = f"({HEARD_NOTHING})"

TRANSCRIBE = """Transcribe an audio or video file to text on this machine, with the local
NeMo speech model. Use this whenever the user asks to transcribe a recording,
meeting, interview, voice memo or video - instead of Whisper, a cloud speech API,
or writing a script to do it. Nothing leaves the machine.

It starts a background job and returns at once with a job id, the path the
transcript will be written to, and an estimate: roughly a minute of work for every
seven minutes of audio. Check on it with transcription_status. Speakers are
labelled by default; turn diarize off for a single speaker to save about a
quarter of the time. Only one transcription runs at a time.

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
    "Directory for the transcript. Defaults to SPEAK_TRANSCRIBE_DIR if set, "
    "otherwise the input file's own directory."
)
TRANSCRIBE_FORMAT = "text, json (word timings and speakers), srt, or vtt."
TRANSCRIBE_DIARIZE = "Label who is speaking. On by default; about 38% slower."
TRANSCRIBE_LANGUAGE = "Language code of the speech, e.g. en or pt. Detected when omitted."
TRANSCRIBE_TRANSLATE = "Translate the transcript into this language code."
TRANSCRIPTION_JOB_ID = "A job id from transcribe. Omit it to list recent jobs."
