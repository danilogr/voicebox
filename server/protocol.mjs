/**
 * The slice of MCP stdio a speech server needs: initialize, tools/list,
 * tools/call. Hand-rolled so the plugin has no node_modules to install.
 */
export const SUPPORTED_PROTOCOL_VERSIONS = ['2025-06-18', '2025-03-26', '2024-11-05'];
export const LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0];

const reply = (id, result) => ({ jsonrpc: '2.0', id, result });
const fail = (id, code, message) => ({ jsonrpc: '2.0', id, error: { code, message } });

export function createDispatcher({ serverInfo, tools, callTool }) {
  return {
    async handle(msg) {
      if (msg?.id === undefined) return null; // notification: acknowledge by silence

      switch (msg.method) {
        case 'initialize': {
          const asked = msg.params?.protocolVersion;
          return reply(msg.id, {
            protocolVersion: SUPPORTED_PROTOCOL_VERSIONS.includes(asked) ? asked : LATEST_PROTOCOL_VERSION,
            capabilities: { tools: {} },
            serverInfo,
          });
        }

        case 'tools/list':
          return reply(msg.id, { tools });

        case 'tools/call': {
          const { name, arguments: args = {} } = msg.params ?? {};
          if (!tools.some((t) => t.name === name)) {
            return reply(msg.id, { content: [{ type: 'text', text: `unknown tool: ${name}` }], isError: true });
          }
          try {
            return reply(msg.id, { content: [{ type: 'text', text: await callTool(name, args) }] });
          } catch (err) {
            return reply(msg.id, { content: [{ type: 'text', text: String(err?.message ?? err) }], isError: true });
          }
        }

        case 'ping':
          return reply(msg.id, {});

        default:
          return fail(msg.id, -32601, `method not found: ${msg.method}`);
      }
    },
  };
}
