"""Tool descriptions.

A tool description is always in the model's context, so the policy for when to
speak lives here. A hook or a skill cannot add a callable tool, and neither is
guaranteed to be read at the moment the choice is made.
"""

SPEAK = """Speak one short sentence aloud to the user through the speakers.

Use it at the moments where a voice beats scrollback: when you have finished a task,
when you are blocked and need input, and when you are asking the user a question.
Do not narrate routine progress and do not read out code, paths, or long output.

Write one short sentence of plain spoken English - it is heard, not read.
Returns immediately; utterances queue and play in order without overlapping."""

LIST_VOICES = """List the voices the current speech engine can use, and say which one is active.
Call this before set_voice; the names depend on the engine and, for a local
speech model, on the model file that is loaded."""

SET_VOICE = """Change the voice used for later speech. Use it when the user asks to sound different.
The choice lasts for this session only and does not affect other Claude Code windows."""

STOP_SPEAKING = """Stop talking immediately: silence the current utterance and drop anything
queued behind it."""

TEXT = "One short sentence to say out loud."
VOICE_OVERRIDE = 'Override the configured voice, e.g. "Samantha".'
RATE = "Words per minute. The macOS default is about 175."
VOICE_NAME = 'A name from list_voices, e.g. "Sofia".'

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
server process recorded in server.json in the speak state directory. The next listen
then starts one with transcription."""

SERVER_STARTING = (
    "the speech server is starting up and has not loaded its models yet, so there is "
    "nothing to transcribe with for another few seconds. Wait, then ask again - nothing "
    "needs installing or restarting."
)

SERVER_UNREACHABLE = (
    "the speech server is not answering, so there is nothing to transcribe with. Run "
    "/speak:setup if nemo-speech is not installed yet, check NEMO_HTTP_URL if it serves "
    "somewhere other than the default port, and start it yourself if you set "
    "SPEAK_NEMO_AUTOSTART=0."
)

LISTEN_OFF = "Voice input is off. Set SPEAK_LISTEN=1 in the environment and restart the session."

NO_SERVER = (
    "could not start the speech server. Run /speak:setup to install nemo-speech and pull "
    "the models, or set SPEAK_NEMO_BIN to it if it is installed somewhere else."
)

ALREADY_LISTENING = (
    "this session is already listening; wait for that recording to finish before asking again"
)

HEARD_NOTHING = "heard nothing"

# The sentinel /talk prints when the recording held no speech. `commands/talk.md`
# branches on this exact line, so it lives here rather than as a literal in both.
TALK_HEARD_NOTHING = f"({HEARD_NOTHING})"
