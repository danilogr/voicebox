import test from 'node:test';
import assert from 'node:assert/strict';
import { normalize } from '../server/text.mjs';

test('collapses newlines and runs of whitespace into single spaces', () => {
  assert.equal(normalize('hello\n\n  world\t\tagain'), 'hello world again');
});

test('trims surrounding whitespace', () => {
  assert.equal(normalize('   spoken   '), 'spoken');
});

test('leaves text at exactly the cap untouched', () => {
  assert.equal(normalize('12345', { maxChars: 5 }), '12345');
});

test('truncates at a word boundary when over the cap', () => {
  assert.equal(normalize('one two three four five', { maxChars: 12 }), 'one two…');
});

test('hard-truncates a single word longer than the cap', () => {
  assert.equal(normalize('supercalifragilistic', { maxChars: 8 }), 'supercal…');
});

test('throws when the text is empty after normalising', () => {
  assert.throws(() => normalize('   \n  '), /empty/i);
});
