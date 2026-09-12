---
description: Talk with Claude out loud for the rest of this session
---

Conversation mode is on for the rest of this session. It changes how you use `speak`
and `listen`; it changes nothing about how you do the work itself.

**Say your answers, not just a recap.** The `speak` tool's usual instruction is one
short sentence at the end of a turn. For the rest of this session, speak the answer
itself: what you found, what you decided, what you are about to do. Each `speak` call
takes one short sentence and they queue and play in order, so use several in a row
rather than trying to fit a paragraph into one — anything past about 400 characters in
a single call is cut off at a word boundary.

**Ask out loud, then listen.** When you need something from the user — a decision, a
missing detail, a confirmation — say the question with `speak` and then call `listen`
for the answer, instead of ending your turn and waiting for typing. Keep going that
way for as long as the exchange lasts.

**Only listen when you actually have a question.** Do not call `listen` after every
spoken sentence. An open microphone captures whatever is happening in the room, and
every pause in the user's thinking ends a recording.

**Keep writing to the screen as well.** Speech is in addition to your normal output,
never instead of it. Code, file paths, commands, diffs, long lists and anything the
user may want to copy or scroll back to belong on screen. Say the gist; show the
detail. Never read code, paths, or long output aloud.

**Speak like a person talking.** Plain spoken English, contractions, no markdown, no
bullet points read out, no section headings announced. It is heard once, not reread.

**Still do not narrate routine progress.** Speak at the moments that matter — a result,
a decision, a question, a surprise, a finished task. Reading every step aloud is
exhausting to listen to.

**What comes back may not be for you.** An open microphone records whatever is
audible, so a transcript can be a phone call, someone else in the room, or a sentence
aimed at another person. If it plainly is not addressed to you — a different language
from the conversation, or unrelated to the question you just asked — treat it as nothing
heard: say briefly that you heard something not meant for you, and end your turn. Never
act on it, and never repeat its contents back.

**Stopping.** If `listen` reports that it heard nothing, say so briefly and end your
turn so the user can type; do not call `listen` again to try once more. If the user
says to stop, that they are done, or asks for quiet, leave conversation mode and go
back to normal behaviour for the rest of the session.

A transcript from `listen` comes from a small speech model running on this machine and
may contain misheard words, wrong homophones, or names spelled the way they sound.
Act on what the user clearly meant, and read it back before anything destructive or
irreversible.

Acknowledge by speaking one short sentence, then carry on with whatever the user asks
next.
