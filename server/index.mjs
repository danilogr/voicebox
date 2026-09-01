#!/usr/bin/env node
/**
 * speak — an MCP stdio server that says things out loud.
 * stdout is the protocol channel; everything human-readable goes to stderr.
 */
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { homedir } from 'node:os';
import { join } from 'node:path';

import { normalize, DEFAULT_MAX_CHARS } from './text.mjs';
import { sayEngine, createNemoEngine, createCommandEngine } from './engines.mjs';
import { createQueue } from './queue.mjs';
import { createSupervisor } from './supervisor.mjs';
import { createDispatcher } from './protocol.mjs';
import { toolDefinitions } from './tools.mjs';

const VERSION = '1.1.0';
const env = process.env;
const log = (...parts) => console.error('[speak]', ...parts);

const maxChars = Number(env.SPEAK_MAX_CHARS) || DEFAULT_MAX_CHARS;

const defaultStateDir = () =>
  process.platform === 'darwin'
    ? join(homedir(), 'Library', 'Caches', 'speak')
    : join(env.XDG_CACHE_HOME || join(homedir(), '.cache'), 'speak');

/** Poll until the speech server answers, so the start lock is held for the real warmup. */
function healthProbe(baseUrl, { timeoutMs = 30_000 } = {}) {
  return async () => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        const res = await fetch(`${baseUrl}/health`, { signal: AbortSignal.timeout(2000) });
        if (res.ok) return true;
      } catch {
        // not up yet
      }
      await new Promise((r) => setTimeout(r, 500));
    }
    return false;
  };
}

let supervisor = null;

function selectEngine() {
  const name = (env.SPEAK_ENGINE || 'say').toLowerCase();
  if (name === 'say') return sayEngine;
  if (name === 'command') return createCommandEngine({ command: env.SPEAK_COMMAND });
  if (name === 'nemo') {
    const baseUrl = env.NEMO_HTTP_URL || 'http://127.0.0.1:8082';
    if (env.SPEAK_NEMO_AUTOSTART !== '0') {
      supervisor = createSupervisor({
        stateDir: env.SPEAK_STATE_DIR || defaultStateDir(),
        port: Number(new URL(baseUrl).port) || 8082,
        bin: env.SPEAK_NEMO_BIN || 'nemo-speech',
        model: env.NEMO_TTS_MODEL || 'magpie',
        seed: env.SPEAK_SEED === undefined ? undefined : Number(env.SPEAK_SEED),
        spawn,
        probe: healthProbe(baseUrl),
        log,
      });
    }
    return createNemoEngine({
      baseUrl,
      apiKey: env.NEMO_API_KEY,
      model: env.NEMO_TTS_MODEL || 'magpie',
      defaultVoice: env.SPEAK_VOICE || 'John',
      onFallback: (err) => log('speech server unavailable, using say instead:', err?.message ?? err),
      onUnavailable: () => {
        supervisor?.ensureRunning().catch((err) => log('could not start speech server:', err?.message ?? err));
      },
    });
  }
  log(`unknown SPEAK_ENGINE "${name}", using say`);
  return sayEngine;
}

const engine = selectEngine();

/**
 * Per-connection state. Claude Code spawns one of these processes per session,
 * so the session and the process are the same thing and a plain variable is
 * already isolated from every other window.
 */
const session = {
  voice: env.SPEAK_VOICE || undefined,
  rate: env.SPEAK_RATE ? Number(env.SPEAK_RATE) : undefined,
  registered: false,
};

const queue = createQueue({
  prepare: (text, opts) => engine.prepare(text, opts),
  spawn: (command, args) => spawn(command, args, { stdio: 'ignore' }),
  onError: (err) => log('playback failed:', err?.message ?? err),
});

const plural = (n) => (n === 1 ? '' : 's');

const listVoices = () => engine.listVoices?.() ?? Promise.resolve([]);

async function callTool(name, args = {}) {
  if (name === 'speak') {
    const text = normalize(args.text, { maxChars });
    if (supervisor && !session.registered) {
      session.registered = true;
      supervisor.register().catch((err) => log('could not register session:', err?.message ?? err));
    }
    const ahead = queue.enqueue(text, {
      voice: args.voice ?? session.voice,
      rate: args.rate ?? session.rate,
    });
    return ahead === 0
      ? `speaking: "${text}"`
      : `queued behind ${ahead} utterance${plural(ahead)}: "${text}"`;
  }
  if (name === 'list_voices') {
    const voices = await listVoices();
    const active = session.voice ?? engine.defaultVoice ?? 'the engine default';
    if (voices.length === 0) {
      return `engine: ${engine.name}; active voice: ${active}. This engine does not report voice names.`;
    }
    return `engine: ${engine.name}; active voice: ${active}. Available: ${voices.join(', ')}.`;
  }
  if (name === 'set_voice') {
    const wanted = String(args.voice ?? '').trim();
    if (!wanted) throw new Error('set_voice needs a voice name');
    const voices = await listVoices();
    const match = voices.find((v) => v.toLowerCase() === wanted.toLowerCase());
    if (voices.length > 0 && !match) {
      throw new Error(`unknown voice "${wanted}"; available: ${voices.join(', ')}`);
    }
    session.voice = match ?? wanted;
    return `voice set to ${session.voice} for this session`;
  }
  if (name === 'stop_speaking') {
    const dropped = queue.stop();
    return `stopped; dropped ${dropped} utterance${plural(dropped)}`;
  }
  throw new Error(`unknown tool: ${name}`);
}

const dispatcher = createDispatcher({
  serverInfo: { name: 'speak', version: VERSION },
  tools: toolDefinitions,
  callTool,
});

const rl = createInterface({ input: process.stdin });

rl.on('line', async (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;

  let message;
  try {
    message = JSON.parse(trimmed);
  } catch {
    log('ignoring malformed line');
    return;
  }

  try {
    const response = await dispatcher.handle(message);
    if (response) process.stdout.write(JSON.stringify(response) + '\n');
  } catch (err) {
    log('dispatch failed:', err?.message ?? err);
  }
});

async function shutdown() {
  queue.stop();
  if (session.registered) await supervisor?.release().catch(() => {});
  process.exit(0);
}

rl.on('close', shutdown);
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
