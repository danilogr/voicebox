-- Full local schema. Every row here exists in the client's SQLite database.
-- `derive-remote.py` strips every line tagged `-- LOCAL` to produce
-- schema.remote.sql, the DDL that runs against D1. A column is local until
-- it is written down as not: the tag is what keeps content off the server,
-- not a comment convention.

CREATE TABLE installs (
  install_id      TEXT PRIMARY KEY,   -- uuid4, client-generated, never hardware-derived
  first_seen_at   TEXT NOT NULL,
  last_seen_at    TEXT NOT NULL,
  plugin_version  TEXT NOT NULL,
  disclosure_ver  INTEGER,            -- which first-run notice this install was shown

  -- where and when. `country` is written by the server from CF-IPCountry on
  -- ingest; the client never populates this column.
  country         TEXT,               -- ISO-3166 alpha-2
  language        TEXT,               -- BCP-47 from the OS locale, e.g. en-US
  timezone        TEXT,               -- IANA name, e.g. America/Sao_Paulo
  utc_offset_min  INTEGER,            -- the offset at the latest session; follows DST

  -- the machine. Static facts, probed once.
  os              TEXT NOT NULL,      -- darwin | linux | win32
  os_version      TEXT,               -- major.minor only, never the patch
  arch            TEXT NOT NULL,      -- arm64 | x86_64
  cpu_model       TEXT,               -- "Apple M5", "Intel Core i7-9750H"
  cpu_count       INTEGER,
  ram_gb          INTEGER,            -- bucketed: 8 16 32 64 128
  accel           TEXT,               -- metal | cuda | rocm | none | unknown
  accel_name      TEXT                -- "Apple M5", "NVIDIA GeForce RTX 4090"
);

CREATE TABLE sessions (
  session_id      TEXT PRIMARY KEY,   -- uuid4, one per MCP server process
  install_id      TEXT NOT NULL REFERENCES installs(install_id),
  started_at      TEXT NOT NULL,
  ended_at        TEXT,
  plugin_version  TEXT NOT NULL,
  tts_engine      TEXT,               -- say | nemo | command
  asr_engine      TEXT,               -- nemo, the only transcription engine
  listen_enabled  INTEGER NOT NULL,
  harness         TEXT NOT NULL,      -- claude-code|antigravity|gemini-cli|codex|other|unknown
  harness_version TEXT,               -- NULL when the host does not declare one

  shipped_at      TEXT                -- LOCAL: NULL = changed since it was last sent
);
CREATE INDEX sessions_unshipped ON sessions(shipped_at) WHERE shipped_at IS NULL; -- LOCAL: the column above does not exist remotely

CREATE TABLE utterances (
  utterance_id    TEXT PRIMARY KEY,   -- uuid4; doubles as the idempotency key
  session_id      TEXT NOT NULL REFERENCES sessions(session_id),
  install_id      TEXT NOT NULL,      -- denormalised; most queries group by it
  seq             INTEGER NOT NULL,   -- 1..n within the session, unique
  direction       TEXT NOT NULL,      -- 'out' = Claude spoke | 'in' = user spoke
  source          TEXT NOT NULL,      -- speak | listen | talk | transcribe
  occurred_at     TEXT NOT NULL,      -- when the event ended: playback, ASR, or job

  engine          TEXT,               -- the engine that ran, after any fallback
  model           TEXT,               -- magpie | nemotron-3.5 | NULL
  voice           TEXT,               -- out only; a known voice name or `custom`
  device_kind     TEXT,               -- in only: builtin|usb|bluetooth|virtual|unknown

  intent          TEXT,               -- out only
  words           INTEGER,
  chars           INTEGER,
  audio_ms        INTEGER,

  queue_wait_ms   INTEGER,            -- out: enqueued until synthesis started
  synth_ms        INTEGER,            -- out: say/command synthesise inside play_ms
  play_ms         INTEGER,            -- out: player process lifetime
  asr_ms          INTEGER,            -- in

  status          TEXT NOT NULL,      -- ok|failed|discarded|interrupted|heard_nothing|refused

  -- transcribe only: the shape of the file, never its name or its content
  source_bytes    INTEGER,
  audio_codec     TEXT,               -- ffprobe codec_name, e.g. flac, pcm_s16le
  converted       INTEGER,            -- 1 when ffmpeg ran before the model
  diarize         INTEGER,            -- 1 when speakers were labelled
  format          TEXT,               -- text | json | srt | vtt

  text            TEXT,               -- LOCAL
  text_sha256     TEXT,               -- LOCAL
  audio_path      TEXT,               -- LOCAL
  audio_sha256    TEXT,               -- LOCAL
  device_name     TEXT,               -- LOCAL

  corrected_text  TEXT,               -- LOCAL
  corrected_at    TEXT,               -- LOCAL
  correction_kind TEXT,               -- LOCAL

  shipped_at      TEXT,               -- LOCAL: NULL = still in the local backlog
  rejected_at     TEXT                -- LOCAL: the server refused it with a 400; never resent
);
CREATE INDEX utterances_session_seq ON utterances(session_id, seq);
CREATE INDEX utterances_unshipped   ON utterances(shipped_at) WHERE shipped_at IS NULL; -- LOCAL: the column above does not exist remotely
