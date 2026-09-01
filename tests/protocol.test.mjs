import test from 'node:test';
import assert from 'node:assert/strict';
import { createDispatcher, LATEST_PROTOCOL_VERSION } from '../server/protocol.mjs';

const fixture = (overrides = {}) => createDispatcher({
  serverInfo: { name: 'speak', version: '1.0.0' },
  tools: [{ name: 'speak', description: 'say it', inputSchema: { type: 'object' } }],
  callTool: async (name, args) => `called ${name} with ${args.text}`,
  ...overrides,
});

test('initialize echoes back a protocol version the client asked for', async () => {
  const res = await fixture().handle({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '2024-11-05' } });
  assert.equal(res.result.protocolVersion, '2024-11-05');
});

test('initialize answers with the newest supported version when the client asks for an unknown one', async () => {
  const res = await fixture().handle({ jsonrpc: '2.0', id: 1, method: 'initialize', params: { protocolVersion: '1999-01-01' } });
  assert.equal(res.result.protocolVersion, LATEST_PROTOCOL_VERSION);
});

test('initialize advertises the tools capability and the server identity', async () => {
  const res = await fixture().handle({ jsonrpc: '2.0', id: 1, method: 'initialize', params: {} });
  assert.deepEqual(res.result.capabilities.tools, {});
  assert.equal(res.result.serverInfo.name, 'speak');
});

test('notifications get no reply at all', async () => {
  assert.equal(await fixture().handle({ jsonrpc: '2.0', method: 'notifications/initialized' }), null);
});

test('tools/list returns the configured tools', async () => {
  const res = await fixture().handle({ jsonrpc: '2.0', id: 2, method: 'tools/list' });
  assert.equal(res.result.tools.length, 1);
  assert.equal(res.result.tools[0].name, 'speak');
});

test('tools/call routes to the handler and wraps the result as text content', async () => {
  const res = await fixture().handle({
    jsonrpc: '2.0', id: 3, method: 'tools/call',
    params: { name: 'speak', arguments: { text: 'hello' } },
  });
  assert.deepEqual(res.result.content, [{ type: 'text', text: 'called speak with hello' }]);
  assert.ok(!res.result.isError);
});

test('a handler that throws becomes an isError result, not a transport error', async () => {
  const d = fixture({ callTool: async () => { throw new Error('speech engine exploded'); } });
  const res = await d.handle({ jsonrpc: '2.0', id: 4, method: 'tools/call', params: { name: 'speak', arguments: {} } });
  assert.equal(res.result.isError, true);
  assert.match(res.result.content[0].text, /speech engine exploded/);
  assert.equal(res.error, undefined);
});

test('an unknown method returns JSON-RPC method-not-found', async () => {
  const res = await fixture().handle({ jsonrpc: '2.0', id: 5, method: 'resources/list' });
  assert.equal(res.error.code, -32601);
});

test('every reply carries the id it was asked with', async () => {
  const res = await fixture().handle({ jsonrpc: '2.0', id: 99, method: 'tools/list' });
  assert.equal(res.id, 99);
  assert.equal(res.jsonrpc, '2.0');
});
