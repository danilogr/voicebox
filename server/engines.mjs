import { execFile } from 'node:child_process';
import { writeFile, unlink } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';

const noop = async () => {};

const runSayVoices = () =>
  new Promise((resolve, reject) =>
    execFile('say', ['-v', '?'], (err, stdout) => (err ? reject(err) : resolve(stdout))),
  );

/**
 * `say -v '?'` prints "Name  locale  # sample sentence". Names contain spaces
 * and parentheses ("Eddy (English (UK))"), so the locale is found from the
 * right rather than by splitting on whitespace from the left.
 */
export function parseSayVoices(stdout, language) {
  const voices = [];
  for (const line of String(stdout).split('\n')) {
    const head = line.split('#')[0].trim();
    if (!head) continue;
    const split = head.lastIndexOf(' ');
    if (split < 0) continue;
    const locale = head.slice(split + 1);
    const name = head.slice(0, split).trim();
    if (!language || locale.toLowerCase().startsWith(language.toLowerCase())) voices.push(name);
  }
  return voices;
}

/** macOS built-in speech. Text is an argv entry, so it is never shell-interpreted. */
export const sayEngine = {
  name: 'say',
  async prepare(text, { voice, rate } = {}) {
    const args = [];
    if (voice) args.push('-v', voice);
    if (rate) args.push('-r', String(rate));
    args.push(text);
    return { command: 'say', args, cleanup: noop };
  },
  async listVoices({ run = runSayVoices, language = defaultLanguage() } = {}) {
    try {
      return parseSayVoices(await run(), language);
    } catch {
      return [];
    }
  },
};

const defaultLanguage = () => (process.env.LANG || 'en_US').slice(0, 2);

/**
 * NeMo-Speech.cpp (or any OpenAI-compatible /v1/audio/speech server).
 * Falls back to `say` whenever the server cannot answer, so speech never
 * silently disappears because a sidecar is down.
 */
export function createNemoEngine({
  baseUrl,
  apiKey,
  model = 'magpie',
  defaultVoice = 'nova',
  timeoutMs = 15_000,
  onFallback,
  onUnavailable,
} = {}) {
  const root = () => String(baseUrl).replace(/\/$/, '');
  const headers = () => (apiKey ? { authorization: `Bearer ${apiKey}` } : {});

  return {
    name: 'nemo',
    defaultVoice,

    /** Real speaker names come from the loaded model, not from any static list. */
    async listVoices() {
      try {
        const res = await fetch(`${root()}/v1/models`, {
          headers: headers(),
          signal: AbortSignal.timeout(timeoutMs),
        });
        if (!res.ok) return [];
        const body = await res.json();
        const speech = (body.data ?? []).find((m) => m.capability === 'speech');
        return speech?.voices ?? [];
      } catch {
        return [];
      }
    },

    async prepare(text, opts = {}) {
      try {
        const res = await fetch(`${root()}/v1/audio/speech`, {
          method: 'POST',
          headers: { 'content-type': 'application/json', ...headers() },
          body: JSON.stringify({
            model,
            input: text,
            voice: opts.voice ?? defaultVoice,
            response_format: 'wav',
          }),
          signal: AbortSignal.timeout(timeoutMs),
        });
        if (!res.ok) throw new Error(`speech server responded ${res.status}`);

        const file = join(tmpdir(), `speak-${randomUUID()}.wav`);
        await writeFile(file, Buffer.from(await res.arrayBuffer()));
        return { command: 'afplay', args: [file], cleanup: () => unlink(file).catch(noop) };
      } catch (err) {
        onFallback?.(err);
        // Not awaited: the sentence goes out through `say` now, and the
        // server is warm for the next one.
        onUnavailable?.();
        return sayEngine.prepare(text, opts);
      }
    },
  };
}

/**
 * Hand speech to any command: piper, espeak, a shell script, a remote speaker.
 * The text is appended as a single argv entry, never interpolated into a shell.
 */
export function createCommandEngine({ command } = {}) {
  const parts = String(command ?? '').trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) {
    throw new Error('SPEAK_COMMAND must be set when SPEAK_ENGINE=command');
  }
  const [bin, ...prefix] = parts;
  return {
    name: 'command',
    async prepare(text) {
      return { command: bin, args: [...prefix, text], cleanup: noop };
    },
  };
}
