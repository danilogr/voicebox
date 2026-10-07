# Telemetry

voicebox reports anonymous usage data so its author can tell where to spend effort. It
is on by default, starts only after you have been shown a one-time notice, and turning it
off takes one command and no account.

    /voicebox:telemetry off      collect nothing; send only an anonymous install count
    /voicebox:telemetry local    keep the data on this machine; send only that count
    /voicebox:telemetry on       the default
    /voicebox:stats              see everything this machine has recorded

Outside Claude Code the same commands are `uv run --directory <plugin folder>
voicebox-telemetry off|local|on` and `… voicebox-stats`. `VOICEBOX_TELEMETRY=off` or
`local` in the environment lowers the setting for that process alone; it can never raise
it above what you chose. `DO_NOT_TRACK=1` goes further: nothing is collected and nothing
at all is sent, not even the install count.

## What it is for

Four questions, and nothing beyond them:

1. **Which features are used, and do people come back?** Transcription, voice-overs and
   reading aloud compete for the same effort.
2. **How fast is each kind of job on each kind of machine?** The estimates voicebox shows
   should be honest, and some machines may be too slow to support.
3. **What fails, where, and why?** In a fixed set of reasons, by OS and version.
4. **What shape are the inputs and outputs?** How long, how large, which codec, format,
   options and voice — never a name and never content.

## The notice

The first time voicebox could send anything, it shows this, once (and `/voicebox:setup`
prints it too). Until it has been shown, voicebox records nothing and sends nothing.
Telemetry from a session that never surfaces a tool reply or runs `/voicebox:setup` is
therefore lost, not deferred.

    voicebox collects anonymous usage data: which features you use, how long jobs
    take on your machine, why they fail, and the size and format of files (never
    their names or contents, and never the text you synthesize).

      /voicebox:telemetry off     collect nothing; send only an anonymous install count
      /voicebox:telemetry local   keep the data; send only an anonymous install count
      /voicebox:stats             see your own

    Details: https://github.com/danilogr/voicebox/blob/main/TELEMETRY.md

Under `DO_NOT_TRACK=1` no notice is owed and none is shown.

## The install count

One request when voicebox first runs on a machine and one when a new version first runs,
in every setting except `DO_NOT_TRACK`, so that installs can be counted at all. It carries
exactly `plugin_version` (`1.0.0`), `os` (`darwin`, `linux` or `win32`) and `event`
(`install` or `upgrade`): no identifier, so two of them cannot be linked to each other or
to anything else, and nothing that says whether you turned telemetry off. The server adds
one to a daily count per version, OS, event and country and keeps nothing per request.
Only `ping.json`, holding the last version counted, is kept on your machine.

## What is sent

With telemetry `on`, and only then. This is every field; the machine-checked version is
`contract/v1/schema.remote.sql` in the voicebox-telemetry repository, the only schema the
server accepts (a copy is in `tests/contract/v1/`).

**Once per install:** a random `install_id`; `first_seen_at`, `last_seen_at`;
`plugin_version`; `disclosure_ver` (which notice you were shown); `language`, `timezone`
and the current UTC offset (`utc_offset_min`); `os`, `os_version` (major and minor only)
and `arch`; `cpu_model`, `cpu_count`, `ram_gb` (bucketed: 8, 16, 32, 64 or 128); `accel`
and `accel_name` (Metal, CUDA, ROCm, none or unknown). `country` is added by the server from the
network edge; your machine never sends it.

**Once per session** (one per MCP server process): `session_id`, `install_id`,
`started_at`, `ended_at`; `plugin_version`; `core_version` (speak-core); `nemo_version`
(NeMo-Speech.cpp, when installed); `harness` and `harness_version` (Claude Code,
Antigravity, …); `ffmpeg_present`.

**Once per job** — a transcription, a voice-over, or one reading-aloud run:

| Field | What it says |
|---|---|
| `job_id`, `session_id`, `install_id`, `kind` | which job, in which session, of which kind |
| `started_at`, `ended_at`, `wall_ms` | when and how long |
| `status`, `reason`, `stage` | `ok`, `empty`, `stopped`, `refused`, `failed` or `crashed`; for a refusal or failure one of a fixed set of reasons (`busy`, `input_missing`, `ffmpeg_missing`, `timeout`, …), and where it stopped (`convert`, `asr`, `synthesize`, `encode`, …) |
| `engine`, `model` | what did the work: `nemo`, `say`, …; `magpie`, `nemotron-3.5` |
| `server_warm` | synthesis and narration: whether the speech server was already running |

Transcriptions add the file's `source_bytes`, `audio_ms`, `audio_codec`, `has_video`,
whether it was `converted` first and how long that took (`convert_ms`), model time
(`asr_ms`), the options asked for (`diarize`, `language` — requested, never detected —,
`translate`, `format`), and the output's `words` and `chars`. The schema has a `speakers`
column; voicebox 1.0.0 does not fill it.

Voice-overs add `input_kind` (text or file), `input_format` (plain or markdown), the
spoken text's `chars`, `words` and `paragraphs`, how many `chunks` it was sent in and
how many were done (`chunks_done`), the `voice` (a voice the model ships, or `custom` for
any other name), model time (`synth_ms`), joining and encoding time (`encode_ms`), the
output's `audio_ms`, `format` and `output_bytes`.

Reading-aloud runs add how many `sentences` were queued and played (`sentences_played`),
their `chars`, the `voice`, `synth_ms`, the `audio_ms` actually played, and
`fallback_sentences` (spoken by macOS `say` because the speech server was still starting).

## What is never sent

**Not what you transcribe, not what you synthesize, not what is read aloud, and no
audio.** Also never: file names or paths, repository or project names, environment
variables, hostnames, usernames, error messages, or free text of any kind. Your IP
address is not a field of anything above.

This is a property of the design, not a promise. voicebox keeps a richer local database
(`telemetry.db` in its state directory, `~/Library/Caches/voicebox/` on macOS): it also
holds each job's error message, its source file's name and its output path, so that
`/voicebox:stats` can show you your own failures. What uploads is that table **with those
columns removed**: the server's schema is derived from the local one by stripping every
column tagged `-- LOCAL`, and a test fails if the two drift apart.
`VOICEBOX_TELEMETRY_DEBUG=1` prints exactly what would be sent, the install count
included, instead of sending it.

## Where it goes

To `voicebox-telemetry.actionableintelligence.app`, a Cloudflare Worker, into a database
Danilo Gasques controls. Not shared, not sold, not used for advertising. Your IP address
reaches Cloudflare as it does for any website; the server never writes it to its
database: it is used for rate limiting and discarded, and only the two-letter country is
kept. Each install may store 100 rows a day (jobs plus sessions); past that the server
answers normally and drops them, and voicebox does not retry.

## Deleting your data

There is deliberately no delete API — an erasure endpoint on a public server invites
erasing other people's data. Email **danilo@gasqu.es** with your install id, which
`/voicebox:telemetry --id` prints, and it will be deleted. That id is the only thing
connecting these rows to you, which is why the request has to carry it.

## Changing your mind

Nothing is retroactive. Turning telemetry off stops future sends other than the install
count; it does not delete what has arrived — email for that. Turning it back on sends
only what is still in the local database. The setting is read when a session starts, and
a job is recorded for the session that accepted it, so a change applies from the next
session.
