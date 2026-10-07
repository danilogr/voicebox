# Telemetry

`speak` reports anonymous usage data so its author can tell where to spend effort next.
It is on by default. Turning it off takes one command and no account.

```text
/speak:telemetry off     # collect nothing, send nothing but the install ping
/speak:telemetry local   # collect on this machine, send nothing but the install ping
/speak:telemetry on      # the default
/speak:telemetry         # show the current mode
/speak:stats             # see everything this machine has collected
```

From a terminal, `uv run` against the plugin's directory runs the same commands:

```bash
uv run --directory <plugin directory> speak-telemetry off
uv run --directory <plugin directory> speak-stats
```

`SPEAK_TELEMETRY` set to `off` or `local` in your environment lowers the mode for this
tool alone, while it is set. It never raises it: `SPEAK_TELEMETRY=on` does not undo an
`off` you chose with the command. `DO_NOT_TRACK=1` goes further: nothing is collected and
**nothing at all is sent**, not even the install ping, with no configuration needed.

## What it is for

Three questions, and nothing beyond them:

- **Is the neural voice worth its gigabyte on the machines people actually run?** Or is
  the built-in `say` carrying most sessions?
- **Which host is this running under?** Claude Code is not the only thing that speaks MCP,
  and effort should follow the users who exist rather than the ones assumed.
- **Do people keep using voice input, or try it once?** And when they interrupt, what kind
  of sentence were they cutting off?

## The install ping

One request when the plugin first runs on a machine, and one when a new version first
runs. It is sent whether telemetry is `on`, `local` or `off`, so that installs can be
counted at all. It carries exactly three fields:

| Field | Example | Why |
|---|---|---|
| `plugin_version` | `1.7.0` | which releases are installed |
| `os` | `darwin` | which platforms to support |
| `event` | `install` or `upgrade` | new installs apart from upgrades |

There is no install id or any other identifier in it, so two pings cannot be linked to
each other or to the rest of this page's data. It says the same thing in every mode, so
it does not reveal whether you turned telemetry off. The server adds one to a daily
count per version, OS, event and country, and keeps nothing per request.

The only thing kept on your machine is `ping.json` beside the telemetry config, holding
the last version pinged. Under `DO_NOT_TRACK` the ping is never sent and that file is
never written.

## What is sent

With telemetry `on`, and only then: counts, timings and fixed vocabularies. Every field, exhaustively — this table is the whole
of it, and the machine-checked version is `contract/v1/schema.remote.sql` in the
[speak-telemetry](https://github.com/danilogr/speak-telemetry) repo, which is the only
schema the server will accept.

**Once per install**

| Field | Example | Why |
|---|---|---|
| `install_id` | a random UUID | tells a heavy user from many light ones |
| `plugin_version` | `1.3.0` | which releases are in use |
| `country` | `US` | derived at the network edge, never sent by your machine |
| `language`, `timezone`, `utc_offset_min` | `en-US`, `America/New_York`, `-240` | which languages to support; the offset is the current one, so it follows daylight saving |
| `os`, `os_version`, `arch` | `darwin`, `25.6`, `arm64` | major/minor only, never the patch |
| `cpu_model`, `cpu_count`, `ram_gb` | `Apple M5`, `10`, `32` | RAM is bucketed, never exact |
| `accel`, `accel_name` | `metal` | whether local models could use acceleration |

**Once per session**

| Field | Why |
|---|---|
| `session_id`, `started_at`, `ended_at` | how long sessions run |
| `tts_engine`, `asr_engine` | which engine is configured; `asr_engine` is always `nemo` (`listen_enabled` is retained but always true) |
| `harness`, `harness_version` | which host is driving the plugin |

A session is sent when it starts, even if nothing is said in it, and sent again with its
`ended_at` by the next session.

**Once per sentence spoken or heard**

| Field | Why |
|---|---|
| `seq`, `direction`, `occurred_at` | reconstructs the back-and-forth of a conversation; `occurred_at` is when the sentence finished playing, or its transcription finished |
| `engine`, `model`, `voice`, `device_kind` | what produced it: the engine that actually spoke, so a sentence the built-in voice spoke because the neural server was down says `say`; `voice` is a voice name the plugin ships, or `custom` for any other; `device_kind` is a category, never a device name |
| `intent` | what kind of sentence it was — a question, a recap, a finished task |
| `words`, `chars`, `audio_ms` | how much was said, never *what* |
| `queue_wait_ms`, `synth_ms`, `play_ms`, `asr_ms` | where the time goes: `queue_wait_ms` is the wait behind other sentences before synthesis started; `synth_ms` is synthesis, which `say` and custom commands do inside `play_ms` instead |
| `status` | `ok`, `interrupted` (cut off while playing), `discarded` (dropped before it played), `failed`, `refused`, or `heard_nothing` |
| `source_bytes`, `audio_codec`, `converted`, `diarize`, `format` | the shape of a transcribed file — its size, codec, whether it was converted, whether speakers were labelled, and the output format. Only on `transcribe` rows. Never the file's name, its path, or what was said |

## What is never sent

**Not what you said. Not what Claude said. Not any audio.**

Also never: file paths, repository or project names, environment variables, hostnames,
usernames, your IP address, the name of your microphone, or any free text of any kind.

This is not a promise, it is a property of the design. The server's schema has no column
for any of it. The client keeps a richer local database — including transcripts, if you
turn on recording — and what uploads is that table *with the content columns removed*. A
build step derives the server schema from the local one by stripping every column tagged
`-- LOCAL`, and a test fails if the two drift apart. An untagged new column breaks the
build rather than quietly shipping.

Run `SPEAK_TELEMETRY_DEBUG=1` and the plugin prints exactly what it would send, the ping
included, instead of sending it.

## Keeping your own recordings

Separate, and off by default:

```text
/speak:telemetry record on
```

This keeps the audio of what **you** say on your own computer, so you can later improve
speech recognition for your own voice. **Recordings never leave your machine** under any
setting, and they use real disk space. `/speak:telemetry record off` stops it.

## Where it goes, and for how long

To `speak-telemetry.actionableintelligence.app`, a Cloudflare Worker, into a database
Danilo Gasques controls. Not shared, not sold, not fed to advertising.

Your IP address reaches Cloudflare, as it does for any website. It is used once to
rate-limit abuse and discarded in the same request; only the two-letter country is kept.

## Deleting your data

There is deliberately **no delete API** — an erasure endpoint on a public server is a
standing invitation to erase other people's data.

Email **danilo@gasqu.es** with your install id, which `/speak:telemetry --id` prints, and it
will be deleted.

That id is also the only thing that connects these rows to you. Nothing here records who
you are, so without it, this data cannot be traced to a person — which is why the request
has to carry it. Under GDPR Article 11 a controller who cannot identify you is not
required to start collecting information in order to, and your rights attach when you
supply the identifier yourself.

## Changing your mind

Nothing is retroactive in either direction. Turning telemetry off stops future sends
other than the install ping; it does not delete what has already arrived — email for that. Turning it back on does not
send a backlog older than what is still in the local database.
