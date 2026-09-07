"""Tool descriptions.

A tool description is always in the model's context, so the policy for when to
speak lives here. A hook or a skill cannot add a callable tool, and neither is
guaranteed to be read at the moment the choice is made.
"""

SPEAK = """Speak one short sentence aloud to the user through the speakers.

Use it at the moments where a voice beats scrollback: when you have finished a task,
when you are blocked and need input, and when you are asking the user a question.
Do not narrate routine progress and do not read out code, paths, or long output.

End a substantial turn with a one-line recap: the outcome, plus any catch on it -
"Voices set to random; takes effect next reload." Lead with what changed, not with
what you did. If the turn ended blocked, or the work is partly done, say that instead
of "done" - a recap the user cannot trust is worse than silence. Skip the recap on a
lookup, a one-line answer, or a question already on screen.

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
