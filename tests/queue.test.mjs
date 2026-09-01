import test from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { createQueue } from '../server/queue.mjs';

const settle = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };

function harness({ prepare } = {}) {
  const started = [];
  const spawn = (command, args) => {
    const child = new EventEmitter();
    child.kill = () => { child.killed = true; child.emit('exit', null, 'SIGTERM'); };
    started.push({ command, args, child });
    return child;
  };
  const cleaned = [];
  const queue = createQueue({
    spawn,
    prepare: prepare ?? (async (text) => ({
      command: 'fake-say',
      args: [text],
      cleanup: async () => { cleaned.push(text); },
    })),
  });
  return { queue, started, cleaned };
}

test('a second utterance does not start until the first process exits', async () => {
  const { queue, started } = harness();
  queue.enqueue('one');
  queue.enqueue('two');
  await settle();

  assert.equal(started.length, 1, 'only the first utterance should be playing');
  assert.deepEqual(started[0].args, ['one']);

  started[0].child.emit('exit', 0, null);
  await settle();

  assert.equal(started.length, 2);
  assert.deepEqual(started[1].args, ['two']);
});

test('enqueue reports how many utterances are already ahead of it', async () => {
  const { queue, started } = harness();
  assert.equal(queue.enqueue('one'), 0);
  await settle();
  assert.equal(queue.enqueue('two'), 1);
  assert.equal(queue.enqueue('three'), 2);
  assert.equal(started.length, 1);
});

test('cleanup runs after the utterance finishes playing, not before', async () => {
  const { queue, started, cleaned } = harness();
  queue.enqueue('one');
  await settle();
  assert.deepEqual(cleaned, [], 'temp audio must survive until playback ends');

  started[0].child.emit('exit', 0, null);
  await settle();
  assert.deepEqual(cleaned, ['one']);
});

test('stop kills what is playing and drops everything queued behind it', async () => {
  const { queue, started } = harness();
  queue.enqueue('one');
  queue.enqueue('two');
  queue.enqueue('three');
  await settle();

  const dropped = queue.stop();
  await settle();

  assert.equal(dropped, 3, 'reports the one playing plus the two waiting');
  assert.equal(started[0].child.killed, true);
  assert.equal(started.length, 1, 'nothing queued may start after stop');
  assert.equal(queue.size, 0);
});

test('a spawn error does not stall the utterances behind it', async () => {
  const { queue, started } = harness();
  queue.enqueue('one');
  queue.enqueue('two');
  await settle();

  started[0].child.emit('error', new Error('say not found'));
  await settle();

  assert.equal(started.length, 2, 'the queue must keep draining after a failure');
  assert.deepEqual(started[1].args, ['two']);
});

test('a prepare failure does not stall the utterances behind it', async () => {
  let calls = 0;
  const { queue, started } = harness({
    prepare: async (text) => {
      if (++calls === 1) throw new Error('synthesis failed');
      return { command: 'fake-say', args: [text], cleanup: async () => {} };
    },
  });
  queue.enqueue('one');
  queue.enqueue('two');
  await settle();

  assert.equal(started.length, 1);
  assert.deepEqual(started[0].args, ['two']);
});

test('size counts the utterance playing plus those waiting', async () => {
  const { queue, started } = harness();
  queue.enqueue('one');
  queue.enqueue('two');
  await settle();
  assert.equal(queue.size, 2);

  started[0].child.emit('exit', 0, null);
  await settle();
  assert.equal(queue.size, 1);
});
