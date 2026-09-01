import test from 'node:test';
import assert from 'node:assert/strict';
import { toolDefinitions } from '../server/tools.mjs';

const byName = Object.fromEntries(toolDefinitions.map((t) => [t.name, t]));

test('the package ships the speak, stop, and voice tools', () => {
  assert.deepEqual(
    toolDefinitions.map((t) => t.name).sort(),
    ['list_voices', 'set_voice', 'speak', 'stop_speaking'],
  );
});

test('set_voice requires the voice name', () => {
  assert.deepEqual(byName.set_voice.inputSchema.required, ['voice']);
  assert.equal(byName.set_voice.inputSchema.properties.voice.type, 'string');
});

test('list_voices takes no required arguments', () => {
  assert.equal(byName.list_voices.inputSchema.required, undefined);
});

test('set_voice says the choice lasts for the session, not forever', () => {
  assert.match(byName.set_voice.description, /session/i);
});

test('speak requires text and accepts an optional voice and rate', () => {
  const s = byName.speak.inputSchema;
  assert.deepEqual(s.required, ['text']);
  assert.equal(s.properties.text.type, 'string');
  assert.equal(s.properties.voice.type, 'string');
  assert.equal(s.properties.rate.type, 'number');
});

test('the speak description tells the agent which moments deserve speech', () => {
  const d = byName.speak.description.toLowerCase();
  for (const moment of ['finished', 'blocked', 'question']) {
    assert.ok(d.includes(moment), `description should mention "${moment}"`);
  }
});

test('the speak description warns that output is heard, not read', () => {
  assert.match(byName.speak.description, /one short sentence/i);
});

test('stop_speaking takes no required arguments', () => {
  assert.equal(byName.stop_speaking.inputSchema.required, undefined);
});
