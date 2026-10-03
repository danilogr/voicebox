# Performance

> Measured figures and the hardware they came from. Update when a measurement is
> retaken or a default changes because of one. Decisions that rest on these numbers
> live in `architecture.md`; the README states behaviour, not figures.

All numbers below were taken on an **Apple M5** with the NeMo-Speech.cpp server warm
unless stated otherwise. The speech-latency table, the resident-memory table and
"Metal against CPU" are NeMo-Speech.cpp 0.2.0 on Metal, the backend the plugin runs;
the prose figures under them were taken on the CPU backend of 0.1.0, which
synthesised at roughly half the speed Metal does now. They are a guide to orders of
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

The two models together are **1.2 GB**: MagpieTTS at 523 MB and the
`nemotron-3.5-asr-streaming-0.6b` q8_0 transcription model at 707 MB. `/speak:setup`
pulls both, because the same server serves speech and transcription.

## Metal against CPU

On NeMo-Speech.cpp 0.2.0 with the plugin's own server flags (voice `John`,
`--tts.seed 6285`, both models loaded), three warm sentences of 1.7–3.3 s of audio:

| Backend | Synthesis | Speed |
|---|---|---|
| Metal | 0.85–1.68 s | **~2.0x realtime** |
| CPU | 21–45 s | **~0.07x realtime** |

Transcription of a 4 s clip took 0.06 s warm on Metal. Hence `--device auto`, which
is Metal on Apple silicon; see the decision record in `architecture.md`. Below 0.2.0
the ranking was the reverse, and Metal synthesis on an M5 was silence.

## Microphone warmup

CoreAudio delivers exact zeros while an input primes. A Bluetooth earbud produced
its first non-zero sample **630 ms** after the stream opened; a muted or disabled
input never produces one at all. The deafness window is 1.5 s,
which is a little over twice the slowest start measured — short enough that stepping
past a dead default is not felt, long enough that a slow device is not mistaken for
a dead one.

Speech levels on the same microphone peaked around **0.21–0.31** RMS against a gate
that calibrated to the **0.01** floor, so the working margin is an order of
magnitude. The gate ceiling is **0.05**, which only binds in a room noisy enough
that ambient x 3 exceeds it; at that point a quiet speaker would have little headroom,
and no measurement has been taken in such a room.
