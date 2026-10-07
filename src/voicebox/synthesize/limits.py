"""Request limits for synthesize, set by measurement (harness/performance.md,
"Synthesis chunk size"). Change them only with a new measurement."""

# Characters per request: the largest size measured whose readings came back as
# cleanly as single sentences did.
CHUNK_CHARS = 1500

# One request's limit: four times the slowest request measured at that size, and never
# under a minute. Not the 15 s that suits a spoken status line.
REQUEST_TIMEOUT_S = 199.0

# Characters synthesized per second at that size: the estimate synthesize returns.
SYNTH_CHARS_PER_S = 29.5

# Added to the estimate when the speech server has to start first (performance.md).
COLD_START_S = 10.0
