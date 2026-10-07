"""Request limits for synthesize, set by measurement (harness/performance.md,
"Synthesis chunk size"). Change them only with a new measurement."""

# Characters per request: the top of the measured sweep (47 to 1456 characters on the
# Apple M5, Metal). Its readings, transcribed back with no listening check, showed no
# measurable regression against a noisy one-sentence baseline; larger was not tried.
CHUNK_CHARS = 1500

# The least any request waits: four times the slowest request measured at that size,
# and never under a minute. Not the 15 s that suits a spoken status line. It is the
# Metal figure; `request_timeout_s` raises it for a slower machine.
REQUEST_TIMEOUT_S = 199.0

# Characters synthesized per second at that size: the estimate synthesize returns.
SYNTH_CHARS_PER_S = 29.5

# Added to the estimate when the speech server has to start first (performance.md).
COLD_START_S = 10.0

# The CPU backend's pace, derived rather than measured at chunk size: about 0.07x
# realtime, so a full chunk (about 90 s of audio) takes about 21 minutes.
CPU_CHARS_PER_S = 1.2

# The most a job's first request waits, before any pace has been seen: 45 minutes, about
# twice what a full chunk takes on the CPU backend.
FIRST_REQUEST_CEILING_S = 2700.0


def request_timeout_s(chars: int, slowest_chars_per_s: float | None) -> float:
    """How long one request of `chars` characters may take.

    Four times what the slowest pace this job's server has shown would need, and never
    under `REQUEST_TIMEOUT_S`. Before any request has finished nothing is known about the
    machine, so the first request is given four times the CPU backend's pace, capped at
    `FIRST_REQUEST_CEILING_S`: a CPU-only machine finishes it, and a server that never
    answers fails the job within 45 minutes. The speech server works through requests
    one at a time and finishes one its client gave up on, so a timed-out request is
    never retried: the job fails.
    """
    if slowest_chars_per_s is None:
        first = min(FIRST_REQUEST_CEILING_S, 4 * chars / CPU_CHARS_PER_S)
        return max(REQUEST_TIMEOUT_S, first)
    return max(REQUEST_TIMEOUT_S, 4 * chars / slowest_chars_per_s)
