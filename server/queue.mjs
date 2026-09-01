/**
 * Serial speech queue.
 *
 * enqueue() returns immediately with the number of utterances ahead, so the
 * agent never blocks on playback. Exactly one child process runs at a time,
 * so two quick calls do not talk over each other.
 */
export function createQueue({ prepare, spawn, onError } = {}) {
  const waiting = [];
  let current = null; // { child, cleanup }
  let starting = false;

  async function pump() {
    if (starting || current || waiting.length === 0) return;
    starting = true;

    const item = waiting.shift();
    let prepared;
    try {
      prepared = await prepare(item.text, item.opts);
    } catch (err) {
      onError?.(err);
      starting = false;
      return pump(); // a failed utterance must not stall the ones behind it
    }

    const child = spawn(prepared.command, prepared.args);
    current = { child, cleanup: prepared.cleanup };

    const finish = (err) => {
      if (current?.child !== child) return; // already torn down by stop()
      if (err) onError?.(err);
      current = null;
      Promise.resolve(prepared.cleanup?.()).catch(() => {}).then(pump);
    };
    child.once('exit', () => finish());
    child.once('error', (err) => finish(err));

    starting = false;
  }

  return {
    /** Queue one utterance. Returns how many are ahead of it. */
    enqueue(text, opts = {}) {
      const ahead = waiting.length + (current || starting ? 1 : 0);
      waiting.push({ text, opts });
      pump();
      return ahead;
    },

    /** Silence everything. Returns how many utterances were dropped. */
    stop() {
      const dropped = waiting.length + (current ? 1 : 0);
      waiting.length = 0;
      const playing = current;
      current = null;
      if (playing) {
        playing.child.kill();
        Promise.resolve(playing.cleanup?.()).catch(() => {});
      }
      return dropped;
    },

    get size() {
      return waiting.length + (current ? 1 : 0);
    },
  };
}
