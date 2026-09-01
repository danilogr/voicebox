import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { readFileSync, existsSync } from 'node:fs';
import { sayEngine, createNemoEngine, createCommandEngine } from '../server/engines.mjs';

function stubServer(handler) {
  return new Promise((resolve) => {
    const s = createServer(handler);
    s.listen(0, '127.0.0.1', () => resolve({ s, url: `http://127.0.0.1:${s.address().port}` }));
  });
}

test('say engine passes the text as one argv entry, never a shell string', async () => {
  const { command, args } = await sayEngine.prepare('hello; rm -rf /');
  assert.equal(command, 'say');
  assert.deepEqual(args, ['hello; rm -rf /']);
});

test('say engine adds voice and rate flags when configured', async () => {
  const { args } = await sayEngine.prepare('hi', { voice: 'Samantha', rate: 190 });
  assert.deepEqual(args, ['-v', 'Samantha', '-r', '190', 'hi']);
});

test('nemo engine posts the text to /v1/audio/speech with the api key', async () => {
  let seen = null;
  const { s, url } = await stubServer(async (req, res) => {
    let body = '';
    for await (const c of req) body += c;
    seen = { path: req.url, auth: req.headers.authorization, body: JSON.parse(body) };
    res.writeHead(200, { 'content-type': 'audio/wav' });
    res.end('FAKEWAV');
  });
  const engine = createNemoEngine({ baseUrl: url, apiKey: 'devkey' });
  const prepared = await engine.prepare('hi there', { voice: 'nova' });
  await prepared.cleanup();
  s.close();

  assert.equal(seen.path, '/v1/audio/speech');
  assert.equal(seen.auth, 'Bearer devkey');
  assert.equal(seen.body.input, 'hi there');
  assert.equal(seen.body.voice, 'nova');
});

test('nemo engine writes the audio to a temp file that cleanup removes', async () => {
  const { s, url } = await stubServer((req, res) => res.end('FAKEWAV'));
  const engine = createNemoEngine({ baseUrl: url, apiKey: 'k' });
  const { command, args, cleanup } = await engine.prepare('hi');
  s.close();

  assert.equal(command, 'afplay');
  assert.match(args[0], /\.wav$/);
  assert.equal(readFileSync(args[0], 'utf8'), 'FAKEWAV');
  await cleanup();
  assert.equal(existsSync(args[0]), false);
});

test('nemo engine falls back to say when the server is unreachable', async () => {
  const engine = createNemoEngine({ baseUrl: 'http://127.0.0.1:1', apiKey: 'k' });
  const { command, args } = await engine.prepare('hi');
  assert.equal(command, 'say');
  assert.deepEqual(args, ['hi']);
});

test('nemo engine falls back to say when the server returns an error status', async () => {
  const { s, url } = await stubServer((req, res) => { res.writeHead(500); res.end('nope'); });
  const engine = createNemoEngine({ baseUrl: url, apiKey: 'k' });
  const { command } = await engine.prepare('hi');
  s.close();
  assert.equal(command, 'say');
});

test('command engine splits the configured command and appends the text as one argv entry', async () => {
  const { command, args } = await createCommandEngine({ command: '/bin/echo -n' }).prepare('hi there');
  assert.equal(command, '/bin/echo');
  assert.deepEqual(args, ['-n', 'hi there']);
});

test('command engine handles a bare command with no arguments', async () => {
  const { command, args } = await createCommandEngine({ command: '/usr/bin/say' }).prepare('hi');
  assert.equal(command, '/usr/bin/say');
  assert.deepEqual(args, ['hi']);
});

test('command engine rejects a blank command instead of spawning nothing', () => {
  assert.throws(() => createCommandEngine({ command: '   ' }), /SPEAK_COMMAND/);
});

test('nemo engine asks for a server start when the request cannot be made', async () => {
  const asked = [];
  const engine = createNemoEngine({
    baseUrl: 'http://127.0.0.1:1', onUnavailable: () => asked.push('start'),
  });

  await engine.prepare('hi');

  assert.deepEqual(asked, ['start']);
});

test('nemo engine does not ask for a server start when synthesis succeeds', async () => {
  const asked = [];
  const { s, url } = await stubServer((req, res) => res.end('FAKEWAV'));
  const engine = createNemoEngine({ baseUrl: url, onUnavailable: () => asked.push('start') });

  try {
    const prepared = await engine.prepare('hi');
    await prepared.cleanup();
  } finally {
    s.close();
  }

  assert.deepEqual(asked, []);
});

test('nemo engine lists the loaded speech model voices', async () => {
  const { s, url } = await stubServer((req, res) => {
    res.writeHead(200, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ data: [
      { id: 'asr', capability: 'transcription' },
      { id: 'magpietts', capability: 'speech', voices: ['John', 'Sofia', 'Aria'] },
    ] }));
  });
  const engine = createNemoEngine({ baseUrl: url });

  let voices;
  try {
    voices = await engine.listVoices();
  } finally {
    s.close();
  }

  assert.deepEqual(voices, ['John', 'Sofia', 'Aria']);
});

test('nemo engine reports no voices rather than throwing when the server is down', async () => {
  const engine = createNemoEngine({ baseUrl: 'http://127.0.0.1:1' });
  assert.deepEqual(await engine.listVoices(), []);
});

test('say engine lists voice names for the current language only', async () => {
  const run = async () => [
    'Albert              en_US    # Hello! My name is Albert.',
    'Alice               it_IT    # Ciao! Mi chiamo Alice.',
    'Eddy (English (UK)) en_GB    # Hello! My name is Eddy.',
    'Samantha            en_US    # Hello! My name is Samantha.',
  ].join('\n');

  const voices = await sayEngine.listVoices({ run, language: 'en' });

  assert.deepEqual(voices, ['Albert', 'Eddy (English (UK))', 'Samantha']);
});

test('nemo engine reports the voice it will use when none is chosen', async () => {
  const engine = createNemoEngine({ baseUrl: 'http://127.0.0.1:1', defaultVoice: 'John' });
  assert.equal(engine.defaultVoice, 'John');
});
