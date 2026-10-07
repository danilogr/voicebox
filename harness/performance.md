# <img src="../assets/voicebox-detailed.svg" width="28" height="28" alt="" valign="middle" /> Performance

> Measured figures and the hardware they came from. Update when a measurement is
> retaken or a default changes because of one. Decisions that rest on these numbers
> live in `architecture.md`; the README states behaviour, not figures.

All numbers below were taken on an **Apple M5** with the NeMo-Speech.cpp server warm
unless stated otherwise. The speech-latency table, the resident-memory table and
"Metal against CPU" are NeMo-Speech.cpp 0.2.0 on Metal, the backend the plugin runs;
the prose figures under them are 0.1.0 on the CPU backend and have not been retaken.
They are a guide to orders of
magnitude, not a guarantee: a different machine, a busier one, or a different model
will move them.

## Speech latency

| | |
|---|---|
| `nemo` cold start, to first `/health` answer | ~7 s |
| `nemo` synthesis, per sentence once warm | ~0.5x the audio duration |
| `say`, per sentence | ~0.7 s |

The cold start is why the first sentence after a server start is spoken by `say`
while the server warms: speech is never delayed waiting for a model to load, and
never silently dropped.

Synthesis scales with output length rather than being a fixed cost, and it is sensitive
to what else the machine is doing. On an idle machine a 20-word sentence took 3.6 s to
synthesise for 7.0 s of audio, about half realtime. With the test suite running
alongside, a 25-word sentence took 8.9 s for 9.9 s of audio — near parity. Both are
sub-realtime; treat the ratio as a range, not a constant.

Playback adds about 0.9 s per utterance for the player process to start and exit.

The queue prepares the next utterance while the current one plays, so synthesis overlaps
audio instead of adding to it. Over four sentences on an idle machine, depth-1 prefetch
measured 41.9 s serial against 32.4 s pipelined — 1.30x — and the gain grows as synthesis
slows, since more of it hides behind playback. A six-sentence answer moves from about
110 s serial to about 58 s pipelined. Any deadline that waits on the queue has to allow
for the pipelined sum, not the serial one.

Concurrent synthesis does not help: the speech server handles one request at a time, so
three requests fired together finished at 3.4 s, 7.3 s and 10.6 s for a wall clock of
10.6 s against 11.1 s serially — a 1.05x difference, and a loss of ordering.

## Synthesis chunk size

`synthesize` sends a long text to the speech server in chunks, because one request has
to finish inside its timeout and a long input may read worse. Three passages of plain
prose were synthesized at each request size with MagpieTTS (voice `John`) on the Apple M5
on Metal, by a private NeMo-Speech.cpp 0.2.0 server started with the plugin's flags and
warmed first, then transcribed back with the same server's Nemotron 3.5 and scored
against the input. Each reading is the longest run of whole sentences from the start of
its passage that fits the size, so a passage's longer readings begin with its shorter
ones:

| Request size (characters) | Synthesis (median / slowest) | Audio | Realtime factor | Word error rate (worst) |
|---|---|---|---|---|
| 47–82 (one sentence) | 1.4 s / 2.1 s | 2.7 s | 0.50x | 12.5% |
| 282–492 | 12.8 s / 16.5 s | 24.3 s | 0.52x | 9.7% |
| 941–999 | 33.4 s / 34.7 s | 63.9 s | 0.52x | 8.1% |
| 1385–1456 | 46.9 s / 49.7 s | 89.5 s | 0.52x | 8.4% |

The word error rate includes the recogniser's own mistakes, so the one-sentence rows are
the baseline. The rule: the largest size whose worst passage stays within 5 points of the
baseline's worst, and whose audio length per character stays within ±25 % of the
baseline (a skipped or repeated passage moves both). That is 1500 characters per
request. Four times the slowest request at that size, at least a minute, is 199 s: the
least any request waits (below). The estimate `synthesize` returns uses the median
throughput at that size, 29.5 characters a second, plus 10 s when the server has to start
first.

The output is 22,050 Hz 16-bit mono, 44,100 bytes a second, so one WAV file's 4 GiB
holds about 27 hours. `synthesize` refuses a text before any work starts when its
estimate passes that: 80 ms of audio a character, rounded up from the most any reading
here made (79 ms, the 282-character reading below), plus 700 ms for each paragraph
break. That is about 1.2 million characters of prose.

"Reads cleanly" here means no measurable regression against a noisy baseline, judged
from transcripts alone; nobody listened to the audio. The baseline's worst is one
spelling difference in an eight-word sentence (`harbour` heard as `harbor`), which puts
the word-error bar at 17.5 %, and no size came near it: the bar excluded nothing, and
the pace test was the only one that discriminated. It excluded the 282–492 row on one
passage, whose 282-character reading runs 41 % longer per character than the baseline
because a 2.1 s pause near its start, which every longer reading of that passage also
holds, weighs more in a short reading. 1500 is the top of the sweep, not a measured
limit: no larger size was tried.

The transcripts show defects that single sentences do not. Every size from 282–492 up
shares the ones in the passages' opening sentences, because the readings share those
prefixes: false starts and stray syllables at sentence joins (`peda` before `people`,
`than a dad` before `the first lesson`, `nodindo`, `adelana`), and the last word or
words of a sentence heard twice (`reasons reasons`). That echo also lands on the end of
a request in four of the nine readings above one sentence (`worse worse`, `reasons
reasons`, `music music`, `that is fine that is fine`) and in none of the three single
sentences. The 941–999 and 1385–1456 readings carry further defects in the text only
they contain: `a faint` missing (a possible two-word skip, or a recogniser miss),
`grease` heard as `pedigrees a greece`, stray `uh` and `but`, and, in the 1385–1456
readings only, `yet` missing. The error rate does not grow
with length: these defects sit where the text sits, so a smaller chunk would not remove
them, and it would add request ends for the echo to land on.

The speech server finishes a request its client has given up on. On the M5 against the
running server, a short request took 0.73 s alone and 19.9 s when sent right after
aborting a request of about 770 characters: it waited behind the abandoned one. Retrying
after a timeout, whole or in smaller pieces, therefore queues behind work nobody will
use and holds up every other session using the server.

So a request's timeout follows the machine instead (`request_timeout_s` in
`synthesize/limits.py`). The 199 s figure assumes Metal. On the CPU backend, at about
0.07x realtime ("Metal against CPU" below), a 1500-character request of about 90 s of
audio takes around 21 minutes. A request may take four times what the slowest pace this
job has shown would need, counting only requests of at least 200 characters: a short
request is mostly fixed cost (a 6-character heading runs at about 8 characters a
second) or, sent while another session holds the server, mostly waiting. Before such a
request has finished, the pace is the CPU backend's, about 1.2 characters a second. No
request waits longer than four times what the CPU backend's pace would need, or 45
minutes, and none less than 199 s. A request that times out fails the job with reason
`timeout`, and is not retried. The time left that `job_status` reports is the characters
still to speak at the job's overall throughput so far.

## Transcription

A transcription job runs `nemo-speech transcribe`, which loads the model per job, so its
time includes the load. On the M5, cold: about 6.9x realtime with speakers labelled and
9.5x without (`jobs/store.py`'s estimate uses these).

## Resident memory

| Server | Once `/health` answers |
|---|---|
| Voice only (`magpie`) | 830 MB |
| Voice plus transcription (the default) | 1.75 GB |

Both grow in use, and not by the same amount:

- One spoken sentence added **120 MB** to the voice-only server.
- The same sentence added only **45 MB** to the server carrying both models.
- A single transcription took the two-model server to **2.4 GB** at peak.

This is the cost behind sharing one server across every Claude Code window rather
than starting one per session, and behind stopping it when the last session leaves.

## Disk

The two models together are **1.4 GB**: MagpieTTS v2607 at 640 MB with its codec and
the `nemotron-3.5-asr-streaming-0.6b` q8_0 transcription model at 707 MB. `/voicebox:setup`
pulls both, because the same server serves speech and transcription. The model cache
is keyed by revision, so an install upgraded from 0.1.0 also keeps the v2602 MagpieTTS
file (447 MB) until it is deleted by hand.

## Metal against CPU

On NeMo-Speech.cpp 0.2.0 with the plugin's own server flags (voice `John`,
`--tts.seed 6285`, both models loaded), three warm sentences of 1.7–3.3 s of audio:

| Backend | Synthesis | Speed |
|---|---|---|
| Metal | 0.85–1.68 s | **~2.0x realtime** |
| CPU | 21–45 s | **~0.07x realtime** |

Transcription of a 4 s clip took 0.06 s warm on Metal. Hence `--device auto`, which
is Metal on Apple silicon; see the decision record in `architecture.md`. Below 0.2.0,
Metal synthesis on an M5 is silence, which is why `install-nemo.sh` requires 0.2.0.

A machine with neither Metal nor CUDA falls back to CPU under `auto`. Only the M5's
CPU has been measured, but at that speed most sentences outrun `DEFAULT_TIMEOUT`
(15 s) and are spoken by `say` instead.
