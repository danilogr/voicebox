import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdir, mkdtemp, readdir, utimes } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { createSupervisor } from '../server/supervisor.mjs';

const stateDir = () => mkdtemp(join(tmpdir(), 'speak-state-'));

test('register records this process as a live client', async () => {
  const dir = await stateDir();
  const supervisor = createSupervisor({ stateDir: dir, pid: 4242, spawn: () => {} });

  await supervisor.register();

  assert.deepEqual(await readdir(join(dir, 'clients')), ['4242']);
});

function fakeSpawn() {
  const calls = [];
  const spawn = (command, args, options) => {
    calls.push({ command, args, options });
    return { pid: 9001, unref() {} };
  };
  return { spawn, calls };
}

test('ensureRunning spawns the speech server on the cpu backend', async () => {
  const dir = await stateDir();
  const { spawn, calls } = fakeSpawn();
  const supervisor = createSupervisor({
    stateDir: dir, pid: 1, port: 8082, spawn, probe: async () => true,
  });

  await supervisor.ensureRunning();

  assert.equal(calls.length, 1);
  assert.equal(calls[0].command, 'nemo-speech');
  assert.deepEqual(calls[0].args, [
    'serve', '--tts-model', 'magpie', '--port', '8082', '--device', 'cpu', '--no-ui',
  ]);
  assert.equal(calls[0].options.detached, true);
});

test('a session that finds a start already in progress does not spawn', async () => {
  const dir = await stateDir();
  const { spawn, calls } = fakeSpawn();
  await mkdir(join(dir, 'starting'), { recursive: true });
  const supervisor = createSupervisor({ stateDir: dir, pid: 2, spawn, probe: async () => true });

  const started = await supervisor.ensureRunning();

  assert.equal(started, false);
  assert.equal(calls.length, 0, 'the session already starting the server owns the spawn');
});

test('two sessions racing to start the server produce exactly one', async () => {
  const dir = await stateDir();
  const a = fakeSpawn();
  const b = fakeSpawn();
  const opts = { stateDir: dir, probe: async () => true };

  await Promise.all([
    createSupervisor({ ...opts, pid: 1, spawn: a.spawn }).ensureRunning(),
    createSupervisor({ ...opts, pid: 2, spawn: b.spawn }).ensureRunning(),
  ]);

  assert.equal(a.calls.length + b.calls.length, 1);
});

async function startedSupervisor(dir, { pid, alive = () => true } = {}) {
  const killed = [];
  const supervisor = createSupervisor({
    stateDir: dir, pid, spawn: fakeSpawn().spawn, probe: async () => true,
    isAlive: alive, kill: (p, signal) => killed.push({ p, signal }),
  });
  return { supervisor, killed };
}

test('release stops the server when the last session leaves', async () => {
  const dir = await stateDir();
  const { supervisor, killed } = await startedSupervisor(dir, { pid: 1 });
  await supervisor.register();
  await supervisor.ensureRunning();

  await supervisor.release();

  assert.deepEqual(await readdir(join(dir, 'clients')), []);
  assert.deepEqual(killed, [{ p: 9001, signal: 'SIGTERM' }]);
});

test('release leaves the server running while another session is alive', async () => {
  const dir = await stateDir();
  const a = await startedSupervisor(dir, { pid: 1 });
  const b = await startedSupervisor(dir, { pid: 2 });
  await a.supervisor.register();
  await b.supervisor.register();
  await a.supervisor.ensureRunning();

  await a.supervisor.release();

  assert.deepEqual(a.killed, [], 'session 2 is still using the server');
  assert.deepEqual(await readdir(join(dir, 'clients')), ['2']);
});

test('release prunes crashed sessions before deciding to stop the server', async () => {
  const dir = await stateDir();
  const ghost = await startedSupervisor(dir, { pid: 777 });
  await ghost.supervisor.register();

  const live = await startedSupervisor(dir, { pid: 1, alive: (p) => p === 1 });
  await live.supervisor.register();
  await live.supervisor.ensureRunning();

  await live.supervisor.release();

  assert.deepEqual(await readdir(join(dir, 'clients')), [], 'the crashed session left a stale entry');
  assert.deepEqual(live.killed, [{ p: 9001, signal: 'SIGTERM' }]);
});

test('a pinned seed is passed to the server so pacing stops drifting', async () => {
  const dir = await stateDir();
  const { spawn, calls } = fakeSpawn();
  const supervisor = createSupervisor({
    stateDir: dir, pid: 1, seed: 1234, spawn, probe: async () => true,
  });

  await supervisor.ensureRunning();

  assert.deepEqual(calls[0].args.slice(-2), ['--tts.seed', '1234']);
});

test('a start lock left behind by a crashed session is taken over', async () => {
  const dir = await stateDir();
  const { spawn, calls } = fakeSpawn();
  await mkdir(join(dir, 'starting'), { recursive: true });
  await utimes(join(dir, 'starting'), new Date(0), new Date(0));
  const supervisor = createSupervisor({
    stateDir: dir, pid: 1, spawn, probe: async () => true, staleLockMs: 60_000,
  });

  assert.equal(await supervisor.ensureRunning(), true);
  assert.equal(calls.length, 1);
});
