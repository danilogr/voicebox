# Telemetry

`speak` reports anonymous usage data so its author can tell where to spend effort next.
It is on by default. Turning it off takes one command and no account.

```
speak-telemetry off      collect nothing, send nothing
speak-telemetry local    collect on this machine, send nothing
speak-telemetry on       the default
speak-stats              see everything this machine has collected
```

`DO_NOT_TRACK=1` in your environment turns it off with no configuration at all, and
`SPEAK_TELEMETRY=off` does the same for this tool alone.

## What it is for

Three questions, and nothing beyond them:

- **Is the neural voice worth its gigabyte on the machines people actually run?** Or is
  the built-in `say` carrying most sessions?
- **Which host is this running under?** Claude Code is not the only thing that speaks MCP,
  and effort should follow the users who exist rather than the ones assumed.
- **Do people keep using voice input, or try it once?** And when they interrupt, what kind
  of sentence were they cutting off?

## What is sent

Counts, timings and fixed vocabularies. Every field, exhaustively — this table is the whole
of it, and the machine-checked version is `contract/v1/schema.remote.sql` in the
[speak-telemetry](https://github.com/danilogr/speak-telemetry) repo, which is the only
schema the server will accept.

**Once per install**

| Field | Example | Why |
|---|---|---|
| `install_id` | a random UUID | tells a heavy user from many light ones |
| `plugin_version` | `1.3.0` | which releases are in use |
| `country` | `US` | derived at the network edge, never sent by your machine |
| `language`, `timezone`, `utc_offset_min` | `en-US`, `America/New_York` | which languages to support |
| `os`, `os_version`, `arch` | `darwin`, `25.6`, `arm64` | major/minor only, never the patch |
| `cpu_model`, `cpu_count`, `ram_gb` | `Apple M5`, `10`, `32` | RAM is bucketed, never exact |
| `accel`, `accel_name` | `metal` | whether local models could use acceleration |

**Once per session**

| Field | Why |
|---|---|
| `session_id`, `started_at`, `ended_at` | how long sessions run |
| `tts_engine`, `asr_engine`, `listen_enabled` | which engines are actually used |
| `harness`, `harness_version` | which host is driving the plugin |

**Once per sentence spoken or heard**

| Field | Why |
|---|---|
| `seq`, `direction`, `occurred_at` | reconstructs the back-and-forth of a conversation |
| `engine`, `model`, `voice`, `device_kind` | what produced it; `device_kind` is a category, never a device name |
| `intent` | what kind of sentence it was — a question, a recap, a finished task |
| `words`, `chars`, `audio_ms` | how much was said, never *what* |
| `queue_wait_ms`, `synth_ms`, `play_ms`, `asr_ms` | where the time goes |
| `status` | whether it played, failed, or was interrupted |

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

Run `SPEAK_TELEMETRY_DEBUG=1` and the plugin prints exactly what it would send, instead of
sending it.

## Keeping your own recordings

Separate, and off by default:

```
speak-telemetry record on
```

This keeps the audio of what **you** say on your own computer, so you can later improve
speech recognition for your own voice. **Recordings never leave your machine** under any
setting, and they use real disk space. `speak-telemetry record off` stops it.

## Where it goes, and for how long

To `speak-telemetry.actionableintelligence.app`, a Cloudflare Worker, into a database
Danilo Gasques controls. Not shared, not sold, not fed to advertising.

Your IP address reaches Cloudflare, as it does for any website. It is used once to
rate-limit abuse and discarded in the same request; only the two-letter country is kept.

## Deleting your data

There is deliberately **no delete API** — an erasure endpoint on a public server is a
standing invitation to erase other people's data.

Email **danilo@gasqu.es** with your install id, which `speak-telemetry --id` prints, and it
will be deleted.

That id is also the only thing that connects these rows to you. Nothing here records who
you are, so without it, this data cannot be traced to a person — which is why the request
has to carry it. Under GDPR Article 11 a controller who cannot identify you is not
required to start collecting information in order to, and your rights attach when you
supply the identifier yourself.

## Changing your mind

Nothing is retroactive in either direction. Turning telemetry off stops future sends; it
does not delete what has already arrived — email for that. Turning it back on does not
send a backlog older than what is still in the local database.
