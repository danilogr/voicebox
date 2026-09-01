import test from 'node:test';
import assert from 'node:assert/strict';
import { execFile } from 'node:child_process';
import { mkdtemp, writeFile, chmod, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const SCRIPT = resolve(fileURLToPath(new URL('../scripts/install-nemo.sh', import.meta.url)));

const run = (script, env) =>
  new Promise((res) =>
    execFile('bash', [script], { env }, (error, stdout, stderr) =>
      res({ error, stdout, stderr })));

/** A PATH containing fakes that record how they were called. */
async function fakeTools(names) {
  const dir = await mkdtemp(join(tmpdir(), 'speak-setup-'));
  const log = join(dir, 'calls.log');
  for (const name of names) {
    const bin = join(dir, name);
    await writeFile(bin, `#!/bin/sh\necho "${name} $*" >> ${log}\n`);
    await chmod(bin, 0o755);
  }
  return { dir, log, calls: async () => (await readFile(log, 'utf8').catch(() => '')).trim().split('\n').filter(Boolean) };
}

test('setup pulls the speech model when the binary is already installed', async () => {
  const { dir, calls } = await fakeTools(['nemo-speech', 'curl']);

  const { error } = await run(SCRIPT, { PATH: `${dir}:/usr/bin:/bin`, HOME: dir });

  assert.equal(error, null);
  const seen = await calls();
  assert.ok(seen.some((c) => c.startsWith('nemo-speech pull magpie')), `saw ${JSON.stringify(seen)}`);
  assert.ok(!seen.some((c) => c.startsWith('curl')), 'must not re-download an installed binary');
});

test('setup downloads the installer when the binary is missing', async () => {
  const { dir, calls } = await fakeTools(['curl', 'sh']);

  await run(SCRIPT, { PATH: `${dir}:/usr/bin:/bin`, HOME: dir });

  const seen = await calls();
  assert.ok(seen.some((c) => c.includes('NVIDIA/NeMo-Speech.cpp')), `saw ${JSON.stringify(seen)}`);
});
