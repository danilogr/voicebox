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

`synthesize` sends one sentence per request, and cuts a sentence longer than 250
characters at its clauses (`CHUNK_CHARS`, `chunk_text`). MagpieTTS repeats and garbles
words when one request holds several sentences, and the errors are audible.

Two paragraphs of plain prose (961 and 4,229 characters, no code or markup) were
synthesized with MagpieTTS (voice `John`) on the Apple M5 on Metal, by the running
NeMo-Speech.cpp server with its fixed seed, cut into requests four ways, joined and
transcribed back with the same server's Nemotron 3.5. "Extra words" counts words heard
that the text does not hold. The rest of the differences are mostly the recogniser's
spelling (`harbour` as `harbor`, `stationmaster` as `station master`):

| Requests | 961-char paragraph: extra words | 4,229-char paragraph: extra words | 4,229: synthesis time |
|---|---|---|---|
| Sentences packed up to 1,500 characters (1 and 3 requests) | 10 | 30 | 132 s |
| Sentences packed up to 250 characters (21 requests) | | 11 | 118 s |
| One sentence each (6 and 36 requests, longest 330 and 261) | 4 | 0 | 110 s |
| One sentence each, cut at clauses past 250 (37 requests) | | 1 | 110 s |
| One sentence each, cut at clauses past 160 (9 and 44 requests) | 0 | 1 | |

Packed requests repeat words (`get get done`, `size size`, `machine machine`), say a
sentence's last words again after it (`again. Again.`, `button button`), add syllables
between sentences (`Nanda`, `Ishe`, `them the barked`) and garble some (`darker` as
`dark urk or be`, `Nobody remembers` as `and do you know everybody remembers`). A
listener heard `get get` and the repeated `again` in the 1,500-character reading. One
sentence a request removes nearly all of it; the 330-character sentence still said its
last four words twice, which is why a sentence past 250 characters is cut. Shorter
requests are also faster: the packed readings spend time on the extra speech.

The estimate `synthesize` returns uses the one-sentence throughput, 38 characters a
second (4,229 characters in 110 s), plus 10 s when the server has to start first.

A sweep of three passages at four request sizes, each reading the longest run of whole
sentences from the start of its passage that fits the size, sets the least any request waits (199 s, four times the slowest
1,385–1,456-character request) and the audio-per-character bound below:

| Request size (characters) | Synthesis (median / slowest) | Audio | Realtime factor |
|---|---|---|---|
| 47–82 (one sentence) | 1.4 s / 2.1 s | 2.7 s | 0.50x |
| 282–492 | 12.8 s / 16.5 s | 24.3 s | 0.52x |
| 941–999 | 33.4 s / 34.7 s | 63.9 s | 0.52x |
| 1385–1456 | 46.9 s / 49.7 s | 89.5 s | 0.52x |

The output is 22,050 Hz 16-bit mono, 44,100 bytes a second, so one WAV file's 4 GiB
holds about 27 hours. `synthesize` refuses a text before any work starts when its
estimate passes that: 80 ms of audio a character, rounded up from the most any reading
here made (79 ms, a 282-character reading holding a 2.1 s pause), plus 700 ms for each
paragraph break. That is about 1.2 million characters of prose.

The speech server finishes a request its client has given up on. On the M5 against the
running server, a short request took 0.73 s alone and 19.9 s when sent right after
aborting a request of about 770 characters: it waited behind the abandoned one. Retrying
after a timeout, whole or in smaller pieces, therefore queues behind work nobody will
use and holds up every other session using the server.

So a request's timeout follows the machine instead (`request_timeout_s` in
`synthesize/limits.py`). The 199 s figure assumes Metal. On the CPU backend, at about
0.07x realtime ("Metal against CPU" below), a 250-character request takes around 3.5
minutes. A request may take four times what the slowest pace this job has shown would
need, counting only requests of at least 100 characters: a shorter request is mostly
fixed cost (a 6-character heading runs at about 8 characters a second) or, sent while
another session holds the server, mostly waiting. Before such a request has finished,
the pace is the CPU backend's, about 1.2 characters a second. No request waits longer
than four times what the CPU backend's pace would need, or 45 minutes, and none less
than 199 s. A request that times out fails the job with reason `timeout`, and is not
retried. The time left that `job_status` reports is the characters still to speak at the
job's overall throughput so far.

## Transcription

A transcription job runs `nemo-speech transcribe`, which loads the model per job, so its
time includes the load. On the M5 with Metal, NeMo-Speech.cpp 0.3.0, a fresh process per
run: 10.3x realtime with speakers labelled and 13.0x without, the medians of six pairs of
runs on five recordings of 21 to 33 minutes (`jobs/store.py`'s estimate uses these).
Across those runs the range is 10.1–10.5x and 12.6–13.3x, and labelling speakers adds 24–28%,
27% at the median. Load weighs more on a short file but its speed is still higher: a
3-minute clip ran at 16x and 21x, so the estimate is cautious there. Converting a 33-minute
m4a to WAV took about a second, and the estimate leaves it out.

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

The text-normalization grammars the server reads numbers, prices, times and
abbreviations with are a **9 MB** download, **137 MB** unpacked (all twelve languages;
English alone is 9 MB), kept in `<cache>/speak-core/tn_configs`.

With them, a sentence without numbers synthesized in the same time as without them
(1.17–1.30 s for three warm sentences, against 1.17–1.31 s, on Metal). A sentence with
numbers takes longer because there is more to say: `It costs $12.50, about 15% more than
last year.` took 2.12 s against 1.85 s, and a time and date 2.22 s against 1.67 s.

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
