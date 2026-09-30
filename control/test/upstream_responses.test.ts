import assert from 'node:assert/strict';
import { createServer, type ServerResponse } from 'node:http';
import { test, type TestContext } from 'node:test';
import { LlmError, MessageId, ToolCallId } from '@deepseek-ai/dsh-llm';
import type { GenerateOptions, RequestMessage, StreamChunk } from '@deepseek-ai/dsh-llm';
import { buildResponsesBody, type ResponsesBody } from '../src/upstream_responses.js';
import { createProviderCountBound } from '../src/token_bound.js';
import { buildUpstreamRequestBody, createUpstreamAdapter, type UpstreamAdapterOptions, type UpstreamProtocol } from '../src/upstream.js';

/**
 * Offline acceptance for the responses-wire upstream adapter
 * (AGENT-ABSTRACTION-2-PLAN.md §4.4): the body mapping, the semantic-SSE
 * reader (payload-typed AND `event:`-line-typed framings), usage/finish
 * mapping, the fail-closed refusals (no `stop` on this wire, no terminal
 * event), and the protocol dispatch in createUpstreamAdapter + the provider
 * token-count bound. Everything runs against a loopback fake `/responses`
 * endpoint; no external provider is contacted.
 */

const KEY_ENV = 'AEVAL_UPSTREAM_RESPONSES_TEST_KEY';
const KEY = 'offline-test-key-0123456789abcdef';
const PROVIDER = 'offline-deepseek';
const MODEL = 'test-model';

interface Recorded {
  readonly method: string;
  readonly url: string;
  readonly headers: Record<string, string | undefined>;
  readonly body: unknown;
}

interface UpstreamScript {
  readonly responses?: (request: Recorded, response: ServerResponse) => void;
  readonly count?: (request: Recorded, response: ServerResponse) => void;
}

/**
 * Emit semantic SSE events the way DeepSeek documents them: the type carried
 * by the payload. `framed` switches to OpenAI's `event:`-line framing — both
 * must parse.
 */
function responsesSse(response: ServerResponse, events: readonly unknown[], framed = false): void {
  response.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-store' });
  for (const event of events) {
    const type = (event as Record<string, unknown>)['type'];
    const data = `data: ${JSON.stringify(framed ? stripType(event) : event)}\n\n`;
    response.write(framed ? `event: ${String(type)}\n${data}` : data);
  }
  // No `data: [DONE]` sentinel on this wire: the terminal event closes it.
  response.end();
}

function stripType(event: unknown): Record<string, unknown> {
  const { ...rest } = event as Record<string, unknown>;
  delete rest['type'];
  return rest;
}

function defaultResponses(_request: Recorded, response: ServerResponse): void {
  responsesSse(response, [
    { type: 'response.created', response: { id: 'resp_test', status: 'in_progress' } },
    { type: 'response.output_item.added', output_index: 0, item: { id: 'msg_1', type: 'message', role: 'assistant' } },
    { type: 'response.output_text.delta', item_id: 'msg_1', output_index: 0, delta: 'Hello' },
    { type: 'response.output_text.delta', item_id: 'msg_1', output_index: 0, delta: ' world' },
    { type: 'response.output_item.done', output_index: 0, item: { id: 'msg_1', type: 'message', role: 'assistant', content: [{ type: 'output_text', text: 'Hello world' }] } },
    { type: 'response.completed', response: { id: 'resp_test', status: 'completed', usage: { input_tokens: 7, output_tokens: 2, total_tokens: 9 } } },
  ]);
}

async function fakeUpstream(t: TestContext, script: UpstreamScript = {}): Promise<{ url: string; requests: Recorded[] }> {
  const requests: Recorded[] = [];
  const server = createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on('data', (chunk: Buffer) => chunks.push(chunk));
    req.on('end', () => {
      const raw = Buffer.concat(chunks).toString('utf8');
      let body: unknown = raw;
      try { body = JSON.parse(raw); } catch { /* keep the raw text for diagnostics */ }
      const record: Recorded = { method: req.method ?? '', url: req.url ?? '', headers: req.headers as Record<string, string | undefined>, body };
      requests.push(record);
      if (record.url === '/responses') (script.responses ?? defaultResponses)(record, res);
      else if (record.url === '/chat/completions') {
        // The chat wire is exercised here only to prove the protocol default;
        // its full acceptance lives in broker_main.test.ts.
        res.writeHead(200, { 'content-type': 'text/event-stream' });
        res.write('data: {"choices":[{"index":0,"delta":{"role":"assistant","content":"ok"},"finish_reason":null}]}\n\n');
        res.write('data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n');
        res.write('data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n');
        res.write('data: [DONE]\n\n');
        res.end();
      }
      else if (record.url === '/tokens/count') (script.count ?? ((_r, out) => { out.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ inputTokens: 12 })); }))(record, res);
      else res.writeHead(404).end();
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  const address = server.address();
  if (address === null || typeof address === 'string') throw new Error('fake upstream did not bind');
  return { url: `http://127.0.0.1:${address.port}`, requests };
}

function options(over: Partial<UpstreamAdapterOptions> = {}): UpstreamAdapterOptions {
  return { provider: PROVIDER, baseUrl: 'http://127.0.0.1:9', apiKeyEnv: KEY_ENV, model: MODEL, protocol: 'responses', ...over };
}

function simpleRequest(): GenerateOptions {
  return { provider: PROVIDER, model: MODEL, messages: [{ role: 'user', content: [{ type: 'text', text: 'hi' }] }] };
}

async function collect(stream: AsyncIterable<StreamChunk>): Promise<StreamChunk[]> {
  const result: StreamChunk[] = [];
  for await (const chunk of stream) result.push(chunk);
  return result;
}

function withKey(t: TestContext): void {
  process.env[KEY_ENV] = KEY;
  t.after(() => { delete process.env[KEY_ENV]; });
}

// ------------------------------------------------------------- body mapping

test('buildResponsesBody maps the neutral request onto the responses wire', () => {
  const history: RequestMessage[] = [
    { id: MessageId('s0'), role: 'system', source: { kind: 'system-prompt' }, content: [{ type: 'text', text: 'be brief' }] },
    { role: 'user', content: [{ type: 'text', text: 'use the tool' }] },
    {
      id: MessageId('m1'),
      role: 'assistant',
      source: { kind: 'model', provider: PROVIDER, model: MODEL },
      content: [
        { type: 'reasoning', text: 'thoughts' },
        { type: 'text', text: 'calling' },
        { type: 'tool-call', id: ToolCallId('call_1'), name: 'get_weather', arguments: '{"city":"SF"}' },
      ],
    },
    {
      id: MessageId('m2'),
      role: 'tool',
      source: { kind: 'tool', callId: ToolCallId('call_1') },
      toolCallId: ToolCallId('call_1'),
      content: [{ type: 'text', text: 'sunny' }],
    },
  ];
  const body = buildResponsesBody(MODEL, {
    ...simpleRequest(),
    messages: history,
    system: 'root instructions',
    maxTokens: 32,
    temperature: 0,
    tools: [{ name: 'get_weather', description: 'weather', parameters: { type: 'object' } }],
  });
  assert.deepEqual(body, {
    model: MODEL,
    input: [
      { type: 'message', role: 'system', content: 'be brief' },
      { type: 'message', role: 'user', content: 'use the tool' },
      // historical reasoning stays off the wire (same honesty rule as chat);
      // the assistant turn folds into one message plus its function call
      { type: 'message', role: 'assistant', content: 'calling' },
      { type: 'function_call', call_id: 'call_1', name: 'get_weather', arguments: '{"city":"SF"}' },
      { type: 'function_call_output', call_id: 'call_1', output: 'sunny' },
    ],
    stream: true,
    instructions: 'root instructions',
    max_output_tokens: 32,
    temperature: 0,
    tools: [{ type: 'function', name: 'get_weather', description: 'weather', parameters: { type: 'object' } }],
  } satisfies ResponsesBody);
});

test('a stop option is refused rather than silently dropped', () => {
  assert.throws(() => buildResponsesBody(MODEL, { ...simpleRequest(), stop: ['END'] }),
    (error: unknown) => error instanceof LlmError && error.code === 'UNSUPPORTED_CONTENT' && error.message.includes('stop'));
});

// ------------------------------------------------------------ stream reader

test('a streamed response traverses the semantic SSE wire and returns usage before finish', async (t) => {
  const upstream = await fakeUpstream(t);
  withKey(t);
  const adapter = createUpstreamAdapter(options({ baseUrl: upstream.url }));
  const chunks = await collect(adapter.stream(simpleRequest()));
  const kinds = chunks.map((chunk) => chunk.type);
  assert.deepEqual(kinds, ['block-start', 'text-delta', 'text-delta', 'block-end', 'usage', 'finish']);
  assert.equal(chunks[0]!.type === 'block-start' && chunks[0]!.blockType, 'text');
  assert.equal(chunks[1]!.type === 'text-delta' && chunks[1]!.text, 'Hello');
  assert.equal(chunks[2]!.type === 'text-delta' && chunks[2]!.text, ' world');
  assert.deepEqual(chunks[3]!.type === 'block-end' && chunks[3]!.block, { type: 'text', text: 'Hello world' });
  assert.deepEqual(chunks[4]!.type === 'usage' && chunks[4]!.usage, { inputTokens: 7, outputTokens: 2, totalTokens: 9 });
  const finish = chunks[5]!;
  assert.equal(finish.type === 'finish' && finish.reason.kind, 'stop');
  assert.deepEqual(finish.type === 'finish' && finish.replayState, { response: { id: 'resp_test' } });

  const recorded = upstream.requests[0]!;
  assert.equal(recorded.url, '/responses');
  assert.equal(recorded.headers['authorization'], `Bearer ${KEY}`);
  const body = recorded.body as Record<string, unknown>;
  assert.equal(body['model'], MODEL);
  assert.equal(body['stream'], true);
  assert.deepEqual(body['input'], [{ type: 'message', role: 'user', content: 'hi' }]);
});

test('reasoning, text and function-call items each map onto their neutral block', async (t) => {
  const upstream = await fakeUpstream(t, {
    responses: (_r, res) => {
      responsesSse(res, [
        { type: 'response.output_item.added', output_index: 0, item: { id: 'rs_1', type: 'reasoning' } },
        { type: 'response.reasoning_text.delta', item_id: 'rs_1', output_index: 0, delta: 'thinking' },
        { type: 'response.output_item.done', output_index: 0, item: { id: 'rs_1', type: 'reasoning' } },
        { type: 'response.output_item.added', output_index: 1, item: { id: 'fc_1', type: 'function_call', call_id: 'call_9', name: 'get_weather' } },
        { type: 'response.function_call_arguments.delta', item_id: 'fc_1', output_index: 1, delta: '{"city"' },
        { type: 'response.function_call_arguments.delta', item_id: 'fc_1', output_index: 1, delta: ':"SF"}' },
        { type: 'response.output_item.done', output_index: 1, item: { id: 'fc_1', type: 'function_call', call_id: 'call_9', name: 'get_weather', arguments: '{"city":"SF"}' } },
        { type: 'response.completed', response: { id: 'resp_tool', status: 'completed', usage: { input_tokens: 5, output_tokens: 4, total_tokens: 9 } } },
      ]);
    },
  });
  withKey(t);
  const adapter = createUpstreamAdapter(options({ baseUrl: upstream.url }));
  const chunks = await collect(adapter.stream(simpleRequest()));
  const kinds = chunks.map((chunk) => chunk.type);
  assert.deepEqual(kinds, [
    'block-start', 'reasoning-delta', 'block-end',
    'block-start', 'tool-call-delta', 'tool-call-delta', 'block-end',
    'usage', 'finish',
  ]);
  assert.deepEqual(chunks[5]!.type === 'tool-call-delta' && [chunks[5]!.id, chunks[5]!.name, chunks[5]!.argumentsDelta],
    ['call_9', 'get_weather', ':"SF"}']);
  assert.deepEqual(chunks[6]!.type === 'block-end' && chunks[6]!.block,
    { type: 'tool-call', id: 'call_9', name: 'get_weather', arguments: '{"city":"SF"}' });
  // a completed response carrying a function call finishes as tool-calls
  assert.equal(chunks[8]!.type === 'finish' && chunks[8]!.reason.kind, 'tool-calls');
});

test("OpenAI's event-line framing parses identically to payload typing", async (t) => {
  const upstream = await fakeUpstream(t, {
    responses: (_r, res) => {
      responsesSse(res, [
        { type: 'response.output_item.added', output_index: 0, item: { id: 'msg_1', type: 'message', role: 'assistant' } },
        { type: 'response.output_text.delta', item_id: 'msg_1', output_index: 0, delta: 'framed' },
        { type: 'response.output_item.done', output_index: 0, item: { id: 'msg_1', type: 'message', role: 'assistant' } },
        { type: 'response.completed', response: { id: 'resp_framed', status: 'completed', usage: { input_tokens: 3, output_tokens: 1, total_tokens: 4 } } },
      ], true);
    },
  });
  withKey(t);
  const adapter = createUpstreamAdapter(options({ baseUrl: upstream.url }));
  const chunks = await collect(adapter.stream(simpleRequest()));
  assert.equal(chunks[1]!.type === 'text-delta' && chunks[1]!.text, 'framed');
  assert.equal(chunks[3]!.type === 'usage' && chunks[3]!.usage.inputTokens, 3);
});

test('usage separates cached input and reasoning output the way the SDK contract expects', async (t) => {
  const upstream = await fakeUpstream(t, {
    responses: (_r, res) => {
      responsesSse(res, [
        { type: 'response.completed', response: { id: 'resp_u', status: 'completed', usage: {
          input_tokens: 10,
          input_tokens_details: { cached_tokens: 4 },
          output_tokens: 6,
          output_tokens_details: { reasoning_tokens: 5 },
          total_tokens: 16,
        } } },
      ]);
    },
  });
  withKey(t);
  const adapter = createUpstreamAdapter(options({ baseUrl: upstream.url }));
  const chunks = await collect(adapter.stream(simpleRequest()));
  assert.deepEqual(chunks.find((chunk) => chunk.type === 'usage'),
    { type: 'usage', usage: { inputTokens: 6, outputTokens: 6, totalTokens: 16, cacheReadTokens: 4, reasoningTokens: 5 } });
});

test('an incomplete response maps its reason: max_output_tokens and content_filter', async (t) => {
  const cases: { reason: string; kind: string; code?: string }[] = [
    { reason: 'max_output_tokens', kind: 'max-tokens' },
    { reason: 'content_filter', kind: 'error', code: 'CONTENT_FILTER' },
  ];
  for (const item of cases) {
    const upstream = await fakeUpstream(t, {
      responses: (_r, res) => {
        responsesSse(res, [
          { type: 'response.incomplete', response: { id: 'resp_i', status: 'incomplete', incomplete_details: { reason: item.reason }, usage: { input_tokens: 2, output_tokens: 1, total_tokens: 3 } } },
        ]);
      },
    });
    withKey(t);
    const adapter = createUpstreamAdapter(options({ baseUrl: upstream.url }));
    const chunks = await collect(adapter.stream(simpleRequest()));
    const finish = chunks.find((chunk) => chunk.type === 'finish');
    assert.ok(finish !== undefined, item.reason);
    if (finish.type !== 'finish') throw new Error('unreachable');
    assert.equal(finish.reason.kind, item.kind, item.reason);
    if (item.code !== undefined) {
      assert.equal((finish.reason as { failure?: { code?: string } }).failure?.code, item.code);
    }
  }
});

test('a failed response is a provider failure, not a finish', async (t) => {
  const upstream = await fakeUpstream(t, {
    responses: (_r, res) => {
      responsesSse(res, [
        { type: 'response.failed', response: { id: 'resp_f', status: 'failed', error: { code: 'server_error', message: 'boom' } } },
      ]);
    },
  });
  withKey(t);
  const adapter = createUpstreamAdapter(options({ baseUrl: upstream.url }));
  await assert.rejects(collect(adapter.stream(simpleRequest())),
    (error: unknown) => error instanceof LlmError && error.code === 'SERVER' && error.message.includes('boom'));
});

test('a stream without a terminal event fails closed', async (t) => {
  const upstream = await fakeUpstream(t, {
    responses: (_r, res) => {
      res.writeHead(200, { 'content-type': 'text/event-stream' }).end(
        'data: {"type":"response.created","response":{"id":"resp_x"}}\n\n',
      );
    },
  });
  withKey(t);
  const adapter = createUpstreamAdapter(options({ baseUrl: upstream.url }));
  await assert.rejects(collect(adapter.stream(simpleRequest())),
    (error: unknown) => error instanceof LlmError && error.code === 'STREAM_CLOSED');
});

test('unknown-but-typed events are ignored without failing the stream', async (t) => {
  const upstream = await fakeUpstream(t, {
    responses: (_r, res) => {
      responsesSse(res, [
        { type: 'response.created', response: { id: 'resp_n', status: 'in_progress' } },
        { type: 'response.in_progress', response: { id: 'resp_n', status: 'in_progress' } },
        { type: 'response.content_part.added', item_id: 'msg_1', output_index: 0, content_index: 0, part: { type: 'output_text', text: '' } },
        { type: 'response.output_item.added', output_index: 0, item: { id: 'msg_1', type: 'message', role: 'assistant' } },
        { type: 'response.output_text.delta', item_id: 'msg_1', output_index: 0, delta: 'ok' },
        { type: 'response.output_text.done', item_id: 'msg_1', output_index: 0, text: 'ok' },
        { type: 'response.content_part.done', item_id: 'msg_1', output_index: 0, content_index: 0, part: { type: 'output_text', text: 'ok' } },
        { type: 'response.output_item.done', output_index: 0, item: { id: 'msg_1', type: 'message', role: 'assistant' } },
        { type: 'response.completed', response: { id: 'resp_n', status: 'completed', usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2 } } },
      ]);
    },
  });
  withKey(t);
  const adapter = createUpstreamAdapter(options({ baseUrl: upstream.url }));
  const chunks = await collect(adapter.stream(simpleRequest()));
  assert.deepEqual(chunks.map((chunk) => chunk.type), ['block-start', 'text-delta', 'block-end', 'usage', 'finish']);
});

// ------------------------------------------------------- protocol dispatch

test('createUpstreamAdapter dispatches on protocol and refuses unknown values', async (t) => {
  withKey(t);
  const chat = await fakeUpstream(t, {});
  assert.throws(() => createUpstreamAdapter(options({ baseUrl: chat.url, protocol: 'gopher' as never })),
    (error: unknown) => error instanceof LlmError && error.code === 'INVALID_CONFIG');
  // absent protocol keeps the chat wire: a pre-responses spec is unchanged
  const chatOptions = options({ baseUrl: chat.url });
  delete (chatOptions as { protocol?: UpstreamProtocol }).protocol;
  await collect(createUpstreamAdapter(chatOptions).stream(simpleRequest()));
  assert.equal(chat.requests[0]!.url, '/chat/completions');
  const responses = await fakeUpstream(t, {});
  await collect(createUpstreamAdapter(options({ baseUrl: responses.url })).stream(simpleRequest()));
  assert.equal(responses.requests[0]!.url, '/responses');
});

test('the provider count bound counts the exact dispatch body for the protocol', async (t) => {
  const upstream = await fakeUpstream(t, {});
  withKey(t);
  const bound = createProviderCountBound({ baseUrl: upstream.url, apiKeyEnv: KEY_ENV, protocol: 'responses' });
  assert.equal(await bound(simpleRequest()), 20);
  const counted = upstream.requests.find((request) => request.url === '/tokens/count')!;
  const body = counted.body as Record<string, unknown>;
  assert.equal(body['model'], MODEL);
  // the responses serialization, not the chat one
  assert.deepEqual(body['input'], [{ type: 'message', role: 'user', content: 'hi' }]);
  assert.equal(body['messages'], undefined);
  // and buildUpstreamRequestBody agrees with the per-protocol builders
  assert.deepEqual(
    buildUpstreamRequestBody('responses', MODEL, simpleRequest()),
    buildResponsesBody(MODEL, simpleRequest()),
  );
  assert.equal(((buildUpstreamRequestBody('chat_completions', MODEL, simpleRequest()) as unknown) as Record<string, unknown>)['messages'] !== undefined, true);
});

test('upstream HTTP failures classify the same way as the chat wire', async (t) => {
  for (const [status, code] of [[401, 'AUTH'], [429, 'RATE_LIMIT'], [500, 'SERVER'], [400, 'INVALID_REQUEST']] as const) {
    const upstream = await fakeUpstream(t, {
      responses: (_r, res) => { res.writeHead(status).end(); },
    });
    withKey(t);
    const adapter = createUpstreamAdapter(options({ baseUrl: upstream.url }));
    await assert.rejects(collect(adapter.stream(simpleRequest())),
      (error: unknown) => error instanceof LlmError && error.code === code, `status ${status}`);
  }
});
