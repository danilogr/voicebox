import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { mkdtemp, writeFile, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const SERVER = resolve(fileURLToPath(new URL('../server/index.mjs', import.meta.url)));

async function startServer() {
  const dir = await mkdtemp(join(tmpdir(), 'speak-e2e-'));
  const log = join(dir, 'spoken.log');
  const recorder = join(dir, 'recorder.mjs');
  await writeFile(recorder, `
    import { appendFileSync } from 'node:fs';
    appendFileSync(${JSON.stringify(log)}, process.argv[2] + '\\n');
  `);

  const child = spawn(process.execPath, [SERVER], {
    env: { ...process.env, SPEAK_ENGINE: 'command', SPEAK_COMMAND: `${process.execPath} ${recorder}` },
    stdio: ['pipe', 'pipe', 'pipe'],
  });

  let buffer = '';
  const pending = new Map();
  child.stdout.on('data', (chunk) => {
    buffer += chunk;
    let nl;
    while ((nl = buffer.indexOf('\n')) !== -1) {
      const line = buffer.slice(0, nl).trim();
      buffer = buffer.slice(nl + 1);
      if (!line) continue;
      const msg = JSON.parse(line);
      pending.get(msg.id)?.(msg);
      pending.delete(msg.id);
    }
  });

  return {
    log,
    request(msg) {
      const p = new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error(`no reply to ${msg.method}`)), 5000);
        pending.set(msg.id, (m) => { clearTimeout(timer); resolve(m); });
      });
      child.stdin.write(JSON.stringify(msg) + '\n');
      return p;
    },
    notify(msg) { child.stdin.write(JSON.stringify(msg) + '\n'); },
    raw(line) { child.stdin.write(line + '\n'); },
    stop() { child.kill(); },
  };
}

async function waitForLog(path, expectedLines, timeoutMs = 4000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const text = await readFile(path, 'utf8').catch(() => '');
    const lines = text.split('\n').filter(Boolean);
    if (lines.length >= expectedLines) return lines;
    await new Promise((r) => setTimeout(r, 25));
  }
  throw new Error(`timed out waiting for ${expectedLines} spoken line(s)`);
}

test('the server completes an MCP handshake over stdio', async () => {
  const s = await startServer();
  const res = await s.request({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2025-06-18' } });
  s.stop();
  assert.equal(res.result.serverInfo.name, 'speak');
  assert.deepEqual(res.result.capabilities.tools, {});
});

test('the server lists its four tools', async () => {
  const s = await startServer();
  await s.request({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} });
  const res = await s.request({ jsonrpc: '2.0', id: 2, method: 'tools/list' });
  s.stop();
  assert.deepEqual(
    res.result.tools.map((t) => t.name).sort(),
    ['list_voices', 'set_voice', 'speak', 'stop_speaking'],
  );
});

test('speaking reaches the configured engine with the normalised text', async () => {
  const s = await startServer();
  await s.request({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} });
  const res = await s.request({
    jsonrpc: '2.0', id: 2, method: 'tools/call',
    params: { name: 'speak', arguments: { text: '  the build\n\n  is green  ' } },
  });
  const lines = await waitForLog(s.log, 1);
  s.stop();

  assert.ok(!res.result.isError, JSON.stringify(res.result));
  assert.equal(lines[0], 'the build is green');
});

test('queued utterances reach the engine in the order they were requested', async () => {
  const s = await startServer();
  await s.request({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} });
  await s.request({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'speak', arguments: { text: 'first' } } });
  await s.request({ jsonrpc: '2.0', id: 3, method: 'tools/call', params: { name: 'speak', arguments: { text: 'second' } } });
  await s.request({ jsonrpc: '2.0', id: 4, method: 'tools/call', params: { name: 'speak', arguments: { text: 'third' } } });
  const lines = await waitForLog(s.log, 3);
  s.stop();
  assert.deepEqual(lines, ['first', 'second', 'third']);
});

test('empty text is reported as a tool error rather than spoken', async () => {
  const s = await startServer();
  await s.request({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} });
  const res = await s.request({
    jsonrpc: '2.0', id: 2, method: 'tools/call',
    params: { name: 'speak', arguments: { text: '   ' } },
  });
  s.stop();
  assert.equal(res.result.isError, true);
  assert.match(res.result.content[0].text, /empty/i);
});

test('stop_speaking answers with how many utterances it dropped', async () => {
  const s = await startServer();
  await s.request({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} });
  const res = await s.request({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'stop_speaking', arguments: {} } });
  s.stop();
  assert.match(res.result.content[0].text, /0/);
});

test('a malformed line does not kill the server', async () => {
  const s = await startServer();
  await s.request({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} });
  s.raw('this is not json at all');
  const res = await s.request({ jsonrpc: '2.0', id: 2, method: 'tools/list' });
  s.stop();
  assert.ok(res.result.tools.some((t) => t.name === 'speak'), 'server still serving after junk');
});

const call = (id, name, args = {}) => ({
  jsonrpc: '2.0', id, method: 'tools/call', params: { name, arguments: args },
});

test('set_voice holds for the rest of the connection', async () => {
  const s = await startServer();
  await s.request({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} });

  await s.request(call(2, 'set_voice', { voice: 'Sofia' }));
  const listed = await s.request(call(3, 'list_voices'));
  s.stop();

  assert.match(listed.result.content[0].text, /Sofia/);
});

test('one connection cannot change another connection voice', async () => {
  const a = await startServer();
  const b = await startServer();
  await a.request({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} });
  await b.request({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} });

  await a.request(call(2, 'set_voice', { voice: 'Sofia' }));
  const other = await b.request(call(2, 'list_voices'));
  a.stop();
  b.stop();

  assert.doesNotMatch(other.result.content[0].text, /Sofia/);
});

test('set_voice rejects a name the engine does not have', async () => {
  const s = await startServer();
  await s.request({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} });

  const res = await s.request(call(2, 'set_voice', { voice: '' }));
  s.stop();

  assert.equal(res.result.isError, true);
});
