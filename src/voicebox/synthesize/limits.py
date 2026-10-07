"""Request limits for synthesize, set by measurement (harness/performance.md,
"Synthesis chunk size"). Change them only with a new measurement."""

# Characters per request: the top of the measured sweep (47 to 1456 characters on the
# Apple M5, Metal). Its readings, transcribed back with no listening check, showed no
# measurable regression against a noisy one-sentence baseline; larger was not tried.
CHUNK_CHARS = 1500

# One request's limit: four times the slowest request measured at that size, and never
# under a minute. Not the 15 s that suits a spoken status line. It assumes Metal: on the
# CPU backend a full chunk exceeds it, and the worker splits a timed-out chunk in half
# and retries.
REQUEST_TIMEOUT_S = 199.0

# Characters synthesized per second at that size: the estimate synthesize returns.
SYNTH_CHARS_PER_S = 29.5

# Added to the estimate when the speech server has to start first (performance.md).
COLD_START_S = 10.0
