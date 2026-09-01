export const DEFAULT_MAX_CHARS = 400;

/** Normalise a sentence for speech: one line, bounded length, never empty. */
export function normalize(text, { maxChars = DEFAULT_MAX_CHARS } = {}) {
  const collapsed = String(text ?? '').replace(/\s+/g, ' ').trim();
  if (!collapsed) throw new Error('nothing to speak: text is empty');
  if (collapsed.length <= maxChars) return collapsed;

  const cut = collapsed.slice(0, maxChars);
  const lastSpace = cut.lastIndexOf(' ');
  return (lastSpace > 0 ? cut.slice(0, lastSpace) : cut) + '…';
}
