"""Request limits for synthesize, set by measurement (harness/performance.md,
"Synthesis chunk size"). Change them only with a new measurement."""

# The most characters one request carries. Each request is one sentence (`chunk_text`);
# a sentence longer than this is cut at its clauses. MagpieTTS repeats and garbles words
# when a request holds several sentences or one past about 300 characters, and read
# 250-character pieces back cleanly (performance.md, "Synthesis chunk size").
CHUNK_CHARS = 250

# The least any request waits: four times the slowest 1,500-character request measured
# on Metal, so far more than a request of `CHUNK_CHARS` needs there. Not the 15 s that
# suits a spoken status line; `request_timeout_s` raises it for a slower machine.
REQUEST_TIMEOUT_S = 199.0

# Characters synthesized per second, one sentence a request, on the Apple M5 (Metal):
# the estimate synthesize returns.
SYNTH_CHARS_PER_S = 38.0

# Added to the estimate when the speech server has to start first (performance.md).
COLD_START_S = 10.0

# What the speech server answers with: MagpieTTS writes 22,050 Hz, 16-bit, mono.
AUDIO_BYTES_PER_S = 22_050 * 2

# Seconds of speech per character, rounded up from the most any measured reading took
# (79 ms, a 282-character reading holding a 2.1 s pause), so a size estimate errs large.
# The 250 ms between chunks, about 2 ms a character for sentences of ordinary length, is
# inside the rounding.
AUDIO_S_PER_CHAR = 0.08

# The CPU backend's pace, derived rather than measured: about 0.07x realtime, so a
# 1,500-character request (about 90 s of audio) takes about 21 minutes.
CPU_CHARS_PER_S = 1.2

# The most any request waits: 45 minutes, about twice what a 1,500-character request
# takes on the CPU backend.
FIRST_REQUEST_CEILING_S = 2700.0

# Requests shorter than this never set the pace later timeouts follow: a heading's request
# is mostly fixed cost, and a short one sent while another session holds the server is
# mostly waiting, so either would read as a machine many times slower than it is. Single
# sentences of 47 to 82 characters already ran at the pace of longer requests.
PACE_MIN_CHARS = 100

# How long the worker waits for a speech server it started to answer /health. A cold
# start takes about 7 s (performance.md); this bounds one that never finishes.
WARMUP_TIMEOUT_S = 60.0


def request_timeout_s(chars: int, slowest_chars_per_s: float | None) -> float:
    """How long one request of `chars` characters may take.

    Four times what the slowest pace this job has shown on a request of at least
    `PACE_MIN_CHARS` would need, and never under `REQUEST_TIMEOUT_S`. Before such a
    request has finished the pace is the CPU backend's, so a CPU-only machine finishes its
    first full chunk. The CPU backend's pace also bounds every timeout - a pace slower
    than it is a server busy with someone else's work, not this machine's speed - and so
    does `FIRST_REQUEST_CEILING_S`, so a server that never answers fails the job within
    45 minutes. The speech server works through requests one at a time and finishes one
    its client gave up on, so a timed-out request is never retried: the job fails.
    """
    on_cpu = 4 * chars / CPU_CHARS_PER_S
    shown = on_cpu if slowest_chars_per_s is None else 4 * chars / slowest_chars_per_s
    return max(REQUEST_TIMEOUT_S, min(FIRST_REQUEST_CEILING_S, on_cpu, shown))
