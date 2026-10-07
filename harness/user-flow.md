# <img src="../assets/voicebox-detailed.svg" width="28" height="28" alt="" valign="middle" /> User flow

> What happens on each path through voicebox, the states a job moves through, and every
> branch that ends one early. The *why* lives in `architecture.md`; measured figures in
> `performance.md`.

## Transcribing a file

1. `transcribe(path, …)` checks, in order, and each refusal is one `refused` row: the file
   exists (`input_missing`); ffprobe is installed (`ffmpeg_missing`) and can read it and
   finds audio (`input_unsupported`); the destination is writable (`output_unwritable`);
   no other voicebox job runs (`busy`, naming the running job). An unknown format is
   answered and not recorded.
2. It claims the job lock, writes the record (`stage`: `convert` or `asr`) and spawns
   `python -m voicebox.transcribe.worker`, then answers with the output path and an
   estimate.
3. The worker takes the lock over, converts to 16 kHz mono WAV if needed, runs
   `nemo-speech transcribe`, and writes the record's final state: `done`, `empty` (no
   speech), or `failed` with a reason (`ffmpeg_missing`, `conversion_failed`, `timeout`,
   `nemo_missing`, `model_failed`, `other`). It records the row and releases the lock.

## Writing a voice-over

1. `synthesize` checks, in order: exactly one of `text` and `path`, and a known format
   (answered, not recorded); the file is absolute, exists and is UTF-8 text
   (`input_missing`, `input_unsupported`); something is left once markup is removed
   (`input_unsupported`); the output is absolute, does not exist, matches the format and
   its folder is writable (`output_unwritable`; pasted text must name one); ffmpeg for
   m4a and mp3 (`ffmpeg_missing`); NeMo — the server answers, or its binary resolves
   (`nemo_missing`) and `NEMO_AUTOSTART` allows starting it (`server_unavailable`); the
   voice, when the server can list them (`input_unsupported`); no other job (`busy`).
2. It stages the spoken text in `jobs/<id>.txt`, writes the record and spawns
   `python -m voicebox.synthesize.worker`.
3. The worker, registered with the supervisor throughout:

| Stage | What happens | Ends early with |
|---|---|---|
| `split` | reads the staged text, cuts it into chunks | `other` (text gone) |
| `synthesize` | starts the server if needed and waits for it; one request per chunk, in order | `server_unavailable`, `nemo_missing`, `timeout`, `model_failed` — naming the chunk |
| `encode` | joins the WAVs with 250 ms pauses (700 ms between paragraphs); ffmpeg for m4a/mp3 | `format_mismatch`, `model_failed`, `ffmpeg_missing`, `conversion_failed`, `timeout` |
| `write` | copies to a hidden partial file beside the output, renames it into place | `output_unwritable` |

4. Whatever ended it, the staged text and chunk WAVs are deleted, the row recorded, the
   registration and the lock released.

A chunk's request is never retried: one that times out ends the job, because the speech
server would go on working on it and queue the retry behind it.

## Asking about jobs

`job_status(job_id)` reads the record. A running job whose worker is gone — its pid dead,
or no pid anywhere a minute after starting — becomes `crashed`, keeps the stage it had
reached (for a voice-over, "after chunk i of n"), loses its artifacts and is recorded.
Without an id it lists recent jobs of every kind. A job far past its estimate is
reported as "it may be wedged"; that is a report, and the job keeps running. Every
session also reconciles running jobs when it starts.

## Reading aloud

`/voicebox:narrate` splits the text into sentences and sends at most ten `read_aloud`
calls, then one `reading_status`, and starts the next round when two or fewer are left.

1. `read_aloud` normalises the sentence (one line, capped at `VOICEBOX_MAX_CHARS`, 400)
   and queues it; empty text is refused.
2. The queue prepares the next sentence while one plays, waits for the speaker lock
   (another process may be speaking), plays it, and reports how it ended.
3. `stop_reading` clears the queue and stops the sentence playing, including one waiting
   for the speakers.

A **reading-aloud run** is one telemetry row: it opens with the first sentence after the
queue was idle and closes after 30 s with nothing queued (`ok`, or `failed` if nothing
played and something failed), on `stop_reading`, or at session end (`ok` when everything
queued played, else `stopped`).

## Telemetry

At session start:

1. `DO_NOT_TRACK` set → nothing collected, nothing sent, no files.
2. Notice not yet shown → nothing collected or sent this session; the first tool reply
   carries the notice, and showing it is recorded in `notice.json`.
3. Otherwise the install count is due when `ping.json` does not name this version, and,
   with telemetry `on`, the backlog ships in the background:

| Answer | Rows in that chunk | The run |
|---|---|---|
| `202`, all accepted | shipped | continues |
| `202`, fewer accepted | shipped (the daily quota is spent) | stops |
| `400`, `413` | set aside, never resent | continues |
| `429`, `5xx`, unreachable | stay in the backlog | stops after retries (at least `Retry-After`) |

When the session ends its `ended_at` is written; the next session ships it.

The setting is read once, at session start, and a job is recorded for the session that
accepted it: `/voicebox:telemetry off` applies from the next session.

## The shared speech server

One per machine, reference-counted under `<cache>/speak-core` and shared by every plugin
on speak-core: started by the first process that needs it (reading aloud through `nemo`,
or a synthesis worker), stopped when the last registered process leaves. Its arguments
come from the shared `NEMO_*` settings alone.
