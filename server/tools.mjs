/**
 * Tool descriptions are always in the model's context, so the "when to speak"
 * policy lives here rather than in a hook or a skill.
 */
export const toolDefinitions = [
  {
    name: 'speak',
    description: [
      'Speak one short sentence aloud to the user through the speakers.',
      '',
      'Use it at the moments where a voice beats scrollback: when you have finished a task,',
      'when you are blocked and need input, and when you are asking the user a question.',
      'Do not narrate routine progress and do not read out code, paths, or long output.',
      '',
      'Write one short sentence of plain spoken English — it is heard, not read.',
      'Returns immediately; utterances queue and play in order without overlapping.',
    ].join('\n'),
    inputSchema: {
      type: 'object',
      properties: {
        text: { type: 'string', description: 'One short sentence to say out loud.' },
        voice: { type: 'string', description: 'Override the configured voice, e.g. "Samantha".' },
        rate: { type: 'number', description: 'Words per minute. macOS default is about 175.' },
      },
      required: ['text'],
    },
  },
  {
    name: 'list_voices',
    description: [
      'List the voices the current speech engine can use, and say which one is active.',
      'Call this before set_voice; the names depend on the engine and, for a local',
      'speech model, on the model file that is loaded.',
    ].join('\n'),
    inputSchema: { type: 'object', properties: {} },
  },
  {
    name: 'set_voice',
    description: [
      'Change the voice used for later speech. Use it when the user asks to sound different.',
      'The choice lasts for this session only and does not affect other Claude Code windows.',
    ].join('\n'),
    inputSchema: {
      type: 'object',
      properties: {
        voice: { type: 'string', description: 'A name from list_voices, e.g. "Sofia".' },
      },
      required: ['voice'],
    },
  },
  {
    name: 'stop_speaking',
    description: 'Stop talking immediately: silence the current utterance and drop anything queued behind it.',
    inputSchema: { type: 'object', properties: {} },
  },
];
