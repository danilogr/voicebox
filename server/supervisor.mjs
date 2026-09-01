import { mkdir, writeFile, readFile, readdir, rm, stat } from 'node:fs/promises';
import { join } from 'node:path';

const noop = () => {};

const defaultIsAlive = (pid) => {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
};

/**
 * Lifecycle for the one NeMo-Speech.cpp server shared by every session.
 *
 * The server costs about a gigabyte of RSS, so sessions share a single
 * instance rather than each starting their own. State lives on disk because
 * the sessions are separate processes with no other way to see each other.
 */
export function createSupervisor({
  stateDir,
  port = 8082,
  bin = 'nemo-speech',
  model = 'magpie',
  seed,
  spawn,
  probe,
  pid = process.pid,
  staleLockMs = 60_000,
  isAlive = defaultIsAlive,
  kill = process.kill.bind(process),
  log = noop,
} = {}) {
  const clientsDir = join(stateDir, 'clients');
  const lockDir = join(stateDir, 'starting');
  const serverFile = join(stateDir, 'server.json');

  function serverArgs() {
    const args = ['serve', '--tts-model', model, '--port', String(port), '--device', 'cpu', '--no-ui'];
    if (seed !== undefined) args.push('--tts.seed', String(seed));
    return args;
  }

  /** mkdir is atomic, so exactly one session wins the right to spawn. */
  async function takeLock() {
    try {
      await mkdir(lockDir, { recursive: false });
      return true;
    } catch (err) {
      if (err.code !== 'EEXIST') throw err;
      if (Date.now() - (await stat(lockDir)).mtimeMs < staleLockMs) return false;
      log('taking over a start lock left by a session that never finished');
      await rm(lockDir, { recursive: true, force: true });
      return takeLock();
    }
  }

  return {
    async register() {
      await mkdir(clientsDir, { recursive: true });
      await writeFile(join(clientsDir, String(pid)), '');
    },

    async ensureRunning() {
      await mkdir(stateDir, { recursive: true });
      if (!(await takeLock())) return false;
      try {
        const child = spawn(bin, serverArgs(), { detached: true, stdio: 'ignore' });
        child.unref?.();
        await writeFile(
          serverFile,
          JSON.stringify({ pid: child.pid, port, startedAt: Date.now() }),
        );
        await probe();
        return true;
      } finally {
        await rm(lockDir, { recursive: true, force: true });
      }
    },

    /**
     * Leave, and stop the server if nobody else is using it. Sessions that
     * crashed without releasing are pruned here, so one lost session cannot
     * pin a gigabyte of memory forever.
     */
    async release() {
      await rm(join(clientsDir, String(pid)), { force: true });

      let entries;
      try {
        entries = await readdir(clientsDir);
      } catch {
        return false;
      }
      for (const entry of entries) {
        const other = Number(entry);
        if (!Number.isInteger(other) || !isAlive(other)) {
          await rm(join(clientsDir, entry), { force: true });
        }
      }
      if ((await readdir(clientsDir)).length > 0) return false;

      let info;
      try {
        info = JSON.parse(await readFile(serverFile, 'utf8'));
      } catch {
        return false;
      }
      log(`last session out, stopping speech server ${info.pid}`);
      try {
        kill(info.pid, 'SIGTERM');
      } catch (err) {
        log('speech server was already gone:', err?.message ?? err);
      }
      await rm(serverFile, { force: true });
      return true;
    },
  };
}
