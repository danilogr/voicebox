---
description: Read a long text aloud — a file, a page, or something in this conversation
argument-hint: "[file path, URL, or nothing to pick from the conversation]"
---

Read a long text out loud, in order, until it is finished or the user stops it. This is
the other direction from `transcribe`: that turns a recording into a file, this turns a
text into speech.

## What to read

`$ARGUMENTS` names the source. Resolve it in this order:

- **A file path** — read that file. Markdown, plain text, source code's prose, anything
  textual. An absolute path is safest; this server's working directory is the plugin's
  own, not the user's project.
- **A URL** — fetch it and keep the article, not the page. Drop navigation, cookie
  banners, subscription prompts, comment threads and "related posts".
- **Nothing** — the source is in this conversation: text the user pasted, or the last
  substantial thing you wrote. If both are plausible, or neither is, ask which in one
  short question and stop until they answer.

## Before you start

Say what it will cost, in one sentence: synthesis runs at about speaking speed, so 150
words is roughly a minute of listening. Over about 1,500 words — ten minutes — offer the
choice rather than assuming: the whole thing, or one section. Ask with `speak`, take the
answer with `listen`, then do what they chose. Under that, just begin.

## How to read it

**Prepare the text before the first call.**

- Split it into sentences. One `speak` call carries one sentence, and anything past about
  400 characters is cut off at a word boundary — so split a long sentence at its clause
  break rather than letting it truncate.
- Skip what cannot be heard: code blocks, tables, ASCII diagrams, reference lists,
  footnote markers, figure labels. Say "there is a code example here" and move on.
- Turn headings into transitions, not announcements. "Next, on how the lock works" — never
  "Heading level two, how the lock works".
- Say what is written for the eye the way it would be spoken: `~/dev/speak` is "the speak
  repo", `v1.5.0` is "version one five oh", a bare URL is "a link here".
- Keep the author's words. This is narration, not summary. Trim only what exists for a
  reader: "as shown below", "click here", "see Figure 3".

**Read it in rounds.** One round is:

1. At most ten `speak` calls, sent together in one message. They queue and play in order
   without overlapping. Pass `intent="narration"` — it is neither a recap nor an
   answer, and it keeps a read-aloud out of the conversation statistics.
2. Then one `speech_status` call, in the next message. Nothing else goes in it.
3. When that reports two or fewer utterances still waiting, start the next round.

Every round has all three parts, and no round begins before the previous round's
`speech_status` comes back. That is what keeps the queue short: a user who says "stop"
loses a few seconds of queued speech rather than twenty minutes of it.

**Say nothing of your own between batches.** No "continuing", no "next section", no
progress. The text is the only thing being read.

## Stopping

If the user interrupts, call `stop_speaking` and stop there. Tell them the sentence you
reached, so they can start again from it.

If a `speak` call fails, say why once and stop. Do not restart the text.

## When it is done

One short spoken line: that it finished. Then end the turn — do not summarize what was
just read, because they heard it.
