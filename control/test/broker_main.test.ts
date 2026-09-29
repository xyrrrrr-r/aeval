import assert from 'node:assert/strict';
import { spawn, type ChildProcess } from 'node:child_process';
import fs from 'node:fs';
import { createServer, type ServerResponse } from 'node:http';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { test, type TestContext } from 'node:test';
import { fileURLToPath } from 'node:url';
import { attributionHeaders, LlmError, MessageId, ToolCallId } from '@deepseek-ai/dsh-llm';
import type { ContentBlock, GenerateOptions, RequestMessage, StreamChunk } from '@deepseek-ai/dsh-llm';
import { createProviderCountBound } from '../src/token_bound.js';
import { createUpstreamAdapter } from '../src/upstream.js';

/**
 * P0-3 offline acceptance: the broker host entry (dist/broker_main.js), the
 * production chat-completions upstream adapter, and the provider token-count
 * bound, all exercised against real loopback HTTP only. No external provider
 * is ever contacted: the fake upstream is the loopback server the production
 * configuration shape points at.
 */

const binPath = fileURLToPath(new URL('../../dist/broker_main.js', import.meta.url));
const KEY_ENV = 'AEVAL_UPSTREAM_TEST_KEY';
const KEY = 'offline-test-key-0123456789abcdef';
const PROVIDER = 'offline-openai';
const MODEL = 'test-model';

interface Recorded {
  readonly method: string;
  readonly url: string;
  readonly headers: Record<string, string | undefined>;
  readonly body: unknown;
}

interface UpstreamScript {
  chat?: (request: Recorded, response: ServerResponse) => void;
  count?: (request: Recorded, response: ServerResponse) => void;
}

function sse(response: ServerResponse, chunks: readonly unknown[]): void {
  response.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-store' });
  for (const chunk of chunks) response.write(`data: ${JSON.stringify(chunk)}\n\n`);
  response.write('data: [DONE]\n\n');
  response.end();
}

function defaultChat(_request: Recorded, response: ServerResponse): void {
  sse(response, [
    { id: 'chatcmpl-test', choices: [{ index: 0, delta: { role: 'assistant', content: 'Hello' }, finish_reason: null }] },
    { id: 'chatcmpl-test', choices: [{ index: 0, delta: { content: ' world' }, finish_reason: null }] },
    { id: 'chatcmpl-test', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] },
    { id: 'chatcmpl-test', choices: [], usage: { prompt_tokens: 7, completion_tokens: 2, total_tokens: 9 } },
  ]);
}

function defaultCount(_request: Recorded, response: ServerResponse): void {
  response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ inputTokens: 12 }));
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
      const record: Recorded = {
        method: req.method ?? '',
        url: req.url ?? '',
        headers: req.headers as Record<string, string | undefined>,
        body,
      };
      requests.push(record);
      if (record.url === '/chat/completions') (script.chat ?? defaultChat)(record, res);
      else if (record.url === '/tokens/count') (script.count ?? defaultCount)(record, res);
      else res.writeHead(404).end();
    });
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  t.after(() => { server.closeAllConnections(); server.close(); });
  const address = server.address();
  if (address === null || typeof address === 'string') throw new Error('fake upstream did not bind');
  return { url: `http://127.0.0.1:${address.port}`, requests };
}

function directory(t: TestContext): string {
  const path = fs.mkdtempSync(join(fs.realpathSync(tmpdir()), 'aeval-broker-main-'));
  t.after(() => fs.rmSync(path, { recursive: true, force: true }));
  return path;
}

function binConfig(upstreamUrl: string, tokenOut: string, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    run: { run_id: 'run-main', job_config_hash: 'b'.repeat(64), config_file_sha256: 'c'.repeat(64), runtime_lock_digest: 'd'.repeat(64) },
    trialId: 'trial-main',
    sessionId: 'session-main',
    configDigest: 'a'.repeat(64),
    identity: { provider: PROVIDER, model: MODEL },
    limits: { maxSteps: 5 },
    maxOutputTokens: 64,
    listen: { host: '127.0.0.1' },
    tokenOut,
    upstream: { provider: PROVIDER, baseUrl: upstreamUrl, apiKeyEnv: KEY_ENV, model: MODEL },
    ...overrides,
  };
}

interface Readiness {
  readonly url: string;
  readonly tokenPath: string;
  readonly token: string;
}

interface Exit {
  readonly code: number | null;
  readonly signal: NodeJS.Signals | null;
  readonly stdout: string;
  readonly stderr: string;
}

function launch(t: TestContext, config: unknown, env: Record<string, string> = {}): {
  readonly child: ChildProcess;
  readonly readiness: Promise<Readiness>;
  readonly exited: Promise<Exit>;
} {
  const root = directory(t);
  // A string is an already-written config file path (for corrupt-input cases);
  // anything else is the config value itself, written as JSON.
  const configPath = typeof config === 'string' ? config : join(root, 'config.json');
  if (typeof config !== 'string') fs.writeFileSync(configPath, JSON.stringify(config));
  const child = spawn(process.execPath, [binPath, configPath], {
    stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, ...env },
    windowsHide: true,
  });
  t.after(() => { if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL'); });
  let stdout = '';
  let stderr = '';
  let closed = false;
  let settled = false;
  const exited = new Promise<Exit>((resolve) => {
    child.once('close', (code, signal) => {
      closed = true;
      resolve({ code, signal, stdout, stderr });
    });
  });
  const readiness = new Promise<Readiness>((resolve, reject) => {
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error(`broker did not report readiness; stdout: ${stdout}; stderr: ${stderr}`));
    }, 10_000);
    timer.unref();
    const attempt = () => {
      if (settled) return;
      const newline = stdout.indexOf('\n');
      if (newline !== -1) {
        settled = true;
        clearTimeout(timer);
        try {
          const line = JSON.parse(stdout.slice(0, newline)) as { ready?: unknown; url?: string; tokenPath?: string; protocol?: string };
          if (line.ready !== true || typeof line.url !== 'string' || typeof line.tokenPath !== 'string' || line.protocol !== 'aeval-model-broker/3') {
            throw new Error(`unexpected readiness line: ${stdout.slice(0, newline)}`);
          }
          resolve({ url: line.url, tokenPath: line.tokenPath, token: fs.readFileSync(line.tokenPath, 'utf8').trim() });
        } catch (error) {
          reject(error instanceof Error ? error : new Error(String(error)));
        }
      } else if (closed) {
        settled = true;
        clearTimeout(timer);
        reject(new Error(`broker exited before readiness; stdout: ${stdout}; stderr: ${stderr}`));
      }
    };
    child.stdout!.on('data', (chunk: Buffer) => { stdout += chunk.toString('utf8'); attempt(); });
    child.stderr!.on('data', (chunk: Buffer) => { stderr += chunk.toString('utf8'); });
    child.once('close', () => attempt());
    attempt();
  });
  // Failure tests never await readiness; keep an early exit from becoming an
  // unhandled rejection while successful tests still observe the real value.
  readiness.catch(() => {});
  return { child, readiness, exited };
}

async function waitFor(predicate: () => boolean, what: string): Promise<void> {
  const deadline = Date.now() + 5000;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await sleep(10);
  }
}

async function collect(stream: AsyncIterable<StreamChunk>): Promise<StreamChunk[]> {
  const result: StreamChunk[] = [];
  for await (const chunk of stream) result.push(chunk);
  return result;
}

function streamRequest(token: string, url: string, body: Record<string, unknown>): Promise<Response> {
  return fetch(`${url}/stream`, {
    method: 'POST',
    headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

async function info(token: string, url: string): Promise<Record<string, unknown>> {
  const response = await fetch(`${url}/info`, { headers: { authorization: `Bearer ${token}` } });
  assert.equal(response.status, 200);
  return await response.json() as Record<string, unknown>;
}

function simpleRequest(): GenerateOptions {
  return { provider: PROVIDER, model: MODEL, messages: [{ role: 'user', content: [{ type: 'text', text: 'hi' }] }] };
}

// ---------------------------------------------------------------- lifecycle

test('the bin serves its lease, reports readiness once, and cleans the token on SIGTERM', { timeout: 15000 }, async (t) => {
  const upstream = await fakeUpstream(t);
  const root = directory(t);
  const tokenOut = join(root, 'job-token');
  const run = launch(t, binConfig(upstream.url, tokenOut, { tokenTtlMs: 300_000 }), { [KEY_ENV]: KEY });
  const ready = await run.readiness;
  assert.equal(ready.tokenPath, tokenOut);
  assert.match(ready.url, /^http:\/\/127\.0\.0\.1:[1-9]\d*$/);
  assert.match(ready.token, /^[a-f0-9]{64}$/);
  assert.equal(fs.statSync(tokenOut).mode & 0o777, 0o600);
  const snapshot = await info(ready.token, ready.url);
  assert.equal(snapshot['protocol'], 'aeval-model-broker/3');
  assert.equal(snapshot['trialId'], 'trial-main');
  assert.equal(snapshot['sessionId'], 'session-main');
  assert.deepEqual(snapshot['identity'], { provider: PROVIDER, model: MODEL });
  assert.equal((snapshot['run'] as Record<string, unknown>)['run_id'], 'run-main');
  run.child.kill('SIGTERM');
  const exit = await run.exited;
  assert.equal(exit.code, 0);
  assert.equal(exit.signal, null);
  assert.equal(fs.existsSync(tokenOut), false);
  assert.equal(exit.stderr, '');
  assert.equal(exit.stdout.trim().split('\n').length, 1);
});

// ------------------------------------------------------- adapter wire form

test('a streamed call traverses the adapter wire form and returns usage before finish', { timeout: 15000 }, async (t) => {
  const upstream = await fakeUpstream(t);
  const run = launch(t, binConfig(upstream.url, join(directory(t), 'job-token')), { [KEY_ENV]: KEY });
  const ready = await run.readiness;
  const response = await streamRequest(ready.token, ready.url, {
    sessionId: 'session-main',
    messages: [{ role: 'user', content: [{ type: 'text', text: 'hi' }] }],
    maxTokens: 999,
  });
  assert.equal(response.status, 200);
  const chunks = (await response.text()).trim().split('\n').map((line) => JSON.parse(line) as Record<string, unknown>);
  assert.deepEqual(chunks.filter((chunk) => chunk['type'] === 'text-delta').map((chunk) => chunk['text']), ['Hello', ' world']);
  assert.deepEqual(chunks.find((chunk) => chunk['type'] === 'block-end'), { type: 'block-end', index: 0, block: { type: 'text', text: 'Hello world' } });
  assert.deepEqual(chunks.find((chunk) => chunk['type'] === 'usage'), { type: 'usage', usage: { inputTokens: 7, outputTokens: 2, totalTokens: 9 } });
  assert.deepEqual(chunks.find((chunk) => chunk['type'] === 'finish'),
    { type: 'finish', reason: { kind: 'stop' }, replayState: { response: { id: 'chatcmpl-test' } } });
  const usageAt = chunks.findIndex((chunk) => chunk['type'] === 'usage');
  const finishAt = chunks.findIndex((chunk) => chunk['type'] === 'finish');
  assert.ok(usageAt !== -1 && finishAt !== -1 && usageAt < finishAt, 'usage must arrive before finish');
  const sent = upstream.requests.find((request) => request.url === '/chat/completions')!;
  assert.equal(sent.method, 'POST');
  assert.equal(sent.headers['authorization'], `Bearer ${KEY}`);
  assert.equal(sent.headers['user-agent'], attributionHeaders()['user-agent']);
  assert.equal(sent.headers['content-type'], 'application/json');
  const body = sent.body as Record<string, unknown>;
  assert.equal(body['model'], MODEL);
  assert.equal(body['stream'], true);
  // The lease clamped 999 to the broker's maxOutputTokens before dispatch.
  assert.equal(body['max_tokens'], 64);
  assert.deepEqual(body['messages'], [{ role: 'user', content: 'hi' }]);
  run.child.kill('SIGTERM');
  assert.equal((await run.exited).code, 0);
});

// ----------------------------------------------------------- trusted meter

test('a hard budget reserves the provider count plus margin over the exact dispatch body', { timeout: 15000 }, async (t) => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const upstream = await fakeUpstream(t, {
    chat: (_request, response) => {
      response.writeHead(200, { 'content-type': 'text/event-stream' });
      response.write(`data: ${JSON.stringify({ choices: [{ index: 0, delta: { content: 'answer' }, finish_reason: null }] })}\n\n`);
      void gate.then(() => {
        response.write(`data: ${JSON.stringify({ choices: [{ index: 0, delta: {}, finish_reason: 'stop' }] })}\n\n`);
        response.write(`data: ${JSON.stringify({ choices: [], usage: { prompt_tokens: 12, completion_tokens: 4, total_tokens: 16 } })}\n\n`);
        response.write('data: [DONE]\n\n');
        response.end();
      });
    },
  });
  const run = launch(t, binConfig(upstream.url, join(directory(t), 'job-token'), {
    limits: { maxSteps: 5, maxTokens: 50 },
    tokenCount: { endpoint: `${upstream.url}/tokens/count`, margin: 8 },
  }), { [KEY_ENV]: KEY });
  const ready = await run.readiness;
  const streaming = streamRequest(ready.token, ready.url, {
    sessionId: 'session-main',
    messages: [{ role: 'user', content: [{ type: 'text', text: 'measure me' }] }],
  });
  await waitFor(() => upstream.requests.some((request) => request.url === '/tokens/count')
    && upstream.requests.some((request) => request.url === '/chat/completions'), 'the count and dispatch requests');
  const during = await info(ready.token, ready.url);
  assert.equal(during['usedSteps'], 1);
  assert.equal(during['usedTokens'], 0);
  // The count endpoint returned 12; with margin 8 the input bound is 20, so
  // the 50-token budget leaves 30 output tokens and reserves exactly 50.
  assert.equal(during['reservedTokens'], 50);
  const counts = upstream.requests.filter((request) => request.url === '/tokens/count');
  assert.equal(counts.length, 2);
  assert.equal((counts[0]!.body as Record<string, unknown>)['max_tokens'], 64);
  assert.equal((counts[1]!.body as Record<string, unknown>)['max_tokens'], 30);
  assert.equal(counts[1]!.headers['authorization'], `Bearer ${KEY}`);
  assert.equal(counts[1]!.headers['user-agent'], attributionHeaders()['user-agent']);
  // The meter counted the exact wire body the dispatch sent.
  const chat = upstream.requests.find((request) => request.url === '/chat/completions')!;
  assert.deepEqual(counts[1]!.body, chat.body);
  release();
  const response = await streaming;
  assert.equal(response.status, 200);
  const chunks = (await response.text()).trim().split('\n').map((line) => JSON.parse(line) as Record<string, unknown>);
  assert.equal(chunks.find((chunk) => chunk['type'] === 'usage') !== undefined, true);
  const after = await info(ready.token, ready.url);
  assert.equal(after['usedTokens'], 16);
  assert.equal(after['reservedTokens'], 0);
  run.child.kill('SIGTERM');
  assert.equal((await run.exited).code, 0);
});

// ----------------------------------------------------------- startup gates

test('a missing or invalid upstream key refuses startup with exit code 2 and no readiness', { timeout: 15000 }, async (t) => {
  const upstream = await fakeUpstream(t);
  const root = directory(t);
  const tokenOut = join(root, 'job-token');
  const missing = launch(t, binConfig(upstream.url, tokenOut));
  const missingExit = await missing.exited;
  assert.equal(missingExit.code, 2);
  assert.match(missingExit.stderr, new RegExp(KEY_ENV));
  assert.equal(missingExit.stdout, '');
  assert.equal(fs.existsSync(tokenOut), false);

  const invalid = launch(t, binConfig(upstream.url, tokenOut), { [KEY_ENV]: 'not a valid key' });
  const invalidExit = await invalid.exited;
  assert.equal(invalidExit.code, 2);
  assert.match(invalidExit.stderr, /API key/);
  assert.equal(invalidExit.stdout, '');
  // The refused value never appears anywhere in the diagnostics.
  assert.equal(invalidExit.stderr.includes('not a valid key'), false);
  assert.equal(fs.existsSync(tokenOut), false);
  assert.equal(upstream.requests.length, 0);
});

test('a hard token budget without a counting source refuses with exit code 3 before listening', { timeout: 15000 }, async (t) => {
  const upstream = await fakeUpstream(t);
  const tokenOut = join(directory(t), 'job-token');
  const run = launch(t, binConfig(upstream.url, tokenOut, { limits: { maxSteps: 5, maxTokens: 100 } }), { [KEY_ENV]: KEY });
  const exit = await run.exited;
  assert.equal(exit.code, 3);
  assert.match(exit.stderr, /tokenCount/);
  assert.equal(exit.stdout, '');
  assert.equal(fs.existsSync(tokenOut), false);
  assert.equal(upstream.requests.length, 0);
});

test('a corrupt or incomplete config refuses startup with exit code 2', { timeout: 15000 }, async (t) => {
  const upstream = await fakeUpstream(t);
  const root = directory(t);
  const tokenOut = join(root, 'job-token');
  const broken = join(root, 'broken.json');
  fs.writeFileSync(broken, '{not valid json');
  const brokenRun = launch(t, broken);
  const brokenExit = await brokenRun.exited;
  assert.equal(brokenExit.code, 2);
  assert.match(brokenExit.stderr, /valid JSON|cannot be read/);
  assert.equal(brokenExit.stdout, '');

  const raw = JSON.parse(JSON.stringify(binConfig(upstream.url, tokenOut))) as { run: Record<string, unknown> };
  delete raw.run['run_id'];
  const incomplete = launch(t, raw);
  const incompleteExit = await incomplete.exited;
  assert.equal(incompleteExit.code, 2);
  assert.match(incompleteExit.stderr, /run\.run_id/);
  assert.equal(incompleteExit.stdout, '');
  assert.equal(fs.existsSync(tokenOut), false);
});

test('an existing token output path refuses startup without overwriting it', { timeout: 15000 }, async (t) => {
  const upstream = await fakeUpstream(t);
  const root = directory(t);
  const tokenOut = join(root, 'job-token');
  fs.writeFileSync(tokenOut, 'sentinel-token-content');
  const run = launch(t, binConfig(upstream.url, tokenOut), { [KEY_ENV]: KEY });
  const exit = await run.exited;
  assert.equal(exit.code, 2);
  assert.match(exit.stderr, /token file cannot be written/);
  assert.equal(exit.stdout, '');
  assert.equal(fs.readFileSync(tokenOut, 'utf8'), 'sentinel-token-content');
});

// ------------------------------------------------------------ TTL behavior

test('token TTL expiry stops the lease, cleans the token file, and exits the process on its own', { timeout: 15000 }, async (t) => {
  const upstream = await fakeUpstream(t);
  const run = launch(t, binConfig(upstream.url, join(directory(t), 'job-token'), { tokenTtlMs: 150 }), { [KEY_ENV]: KEY });
  const ready = await run.readiness;
  const exit = await run.exited;
  assert.equal(exit.code, 0);
  assert.equal(exit.signal, null);
  assert.equal(fs.existsSync(ready.tokenPath), false);
  assert.equal(exit.stderr, '');
});

// ---------------------------------------------------- upstream failure modes

test('a usage over the output cap is refused as AEVAL_TOKEN_BOUND_VIOLATED and terminalizes the lease', { timeout: 15000 }, async (t) => {
  const upstream = await fakeUpstream(t, {
    chat: (_request, response) => {
      sse(response, [{ choices: [], usage: { prompt_tokens: 5, completion_tokens: 999, total_tokens: 1004 } }]);
    },
  });
  const run = launch(t, binConfig(upstream.url, join(directory(t), 'job-token')), { [KEY_ENV]: KEY });
  const ready = await run.readiness;
  const response = await streamRequest(ready.token, ready.url, {
    sessionId: 'session-main',
    messages: [{ role: 'user', content: [{ type: 'text', text: 'overflow' }] }],
  });
  assert.equal(response.status, 422);
  assert.equal(response.headers.get('aeval-error-code'), 'AEVAL_TOKEN_BOUND_VIOLATED');
  assert.equal(response.headers.get('aeval-stop-reason'), 'infra_error');
  assert.equal((await response.text()), '');
  const snapshot = await info(ready.token, ready.url);
  assert.equal(snapshot['stopReason'], 'infra_error');
  run.child.kill('SIGTERM');
  assert.equal((await run.exited).code, 0);
});

test('a hung upstream fails within the adapter timeout and leaves no hang or phantom usage', { timeout: 15000 }, async (t) => {
  const upstream = await fakeUpstream(t, {
    chat: (_request, _response) => { /* receive the request, never answer */ },
  });
  const run = launch(t, binConfig(upstream.url, join(directory(t), 'job-token'), {
    upstream: { provider: PROVIDER, baseUrl: upstream.url, apiKeyEnv: KEY_ENV, model: MODEL, timeoutMs: 200 },
  }), { [KEY_ENV]: KEY });
  const ready = await run.readiness;
  const response = await streamRequest(ready.token, ready.url, {
    sessionId: 'session-main',
    messages: [{ role: 'user', content: [{ type: 'text', text: 'hang' }] }],
  });
  assert.equal(response.status, 422);
  assert.equal(response.headers.get('aeval-error-code'), 'AEVAL_UPSTREAM_FAILED');
  assert.equal(response.headers.get('aeval-stop-reason'), 'infra_error');
  const snapshot = await info(ready.token, ready.url);
  assert.equal(snapshot['stopReason'], 'infra_error');
  // No provider usage ever arrived, so nothing was charged.
  assert.equal(snapshot['usedTokens'], 0);
  assert.equal(upstream.requests.filter((request) => request.url === '/chat/completions').length, 1);
  run.child.kill('SIGTERM');
  assert.equal((await run.exited).code, 0);
});

test('upstream HTTP failures surface as structured infra failures without usage', { timeout: 20000 }, async (t) => {
  for (const status of [401, 500]) {
    const upstream = await fakeUpstream(t, {
      chat: (_request, response) => { response.writeHead(status).end('upstream refused'); },
    });
    const run = launch(t, binConfig(upstream.url, join(directory(t), 'job-token')), { [KEY_ENV]: KEY });
    const ready = await run.readiness;
    const response = await streamRequest(ready.token, ready.url, {
      sessionId: 'session-main',
      messages: [{ role: 'user', content: [{ type: 'text', text: 'fail' }] }],
    });
    assert.equal(response.status, 422);
    assert.equal(response.headers.get('aeval-error-code'), 'AEVAL_UPSTREAM_FAILED');
    assert.equal(response.headers.get('aeval-stop-reason'), 'infra_error');
    const snapshot = await info(ready.token, ready.url);
    assert.equal(snapshot['stopReason'], 'infra_error');
    assert.equal(snapshot['usedTokens'], 0);
    run.child.kill('SIGTERM');
    assert.equal((await run.exited).code, 0);
  }
});

// -------------------------------------------------- adapter unit behaviour

test('the adapter construction refuses unusable credentials without echoing them', async (t) => {
  const base = { provider: PROVIDER, baseUrl: 'http://127.0.0.1:9', model: MODEL };
  const saved = process.env[KEY_ENV];
  t.after(() => {
    if (saved === undefined) delete process.env[KEY_ENV];
    else process.env[KEY_ENV] = saved;
  });
  delete process.env[KEY_ENV];
  assert.throws(() => createUpstreamAdapter({ ...base, apiKeyEnv: KEY_ENV }),
    (error: unknown) => error instanceof LlmError && error.code === 'MISSING_CREDENTIAL' && error.message.includes(KEY_ENV));
  process.env[KEY_ENV] = 'leaky value with spaces';
  assert.throws(() => createUpstreamAdapter({ ...base, apiKeyEnv: KEY_ENV }),
    (error: unknown) => error instanceof LlmError && error.code === 'INVALID_CREDENTIAL' && !error.message.includes('leaky'));
  assert.throws(() => createUpstreamAdapter({ ...base, apiKeyEnv: KEY_ENV, baseUrl: 'ftp://127.0.0.1/' }),
    (error: unknown) => error instanceof LlmError && error.code === 'INVALID_CONFIG');
});

test('resolveModel is an offline identity echo and providerInfo matches it', async (t) => {
  process.env[KEY_ENV] = KEY;
  t.after(() => { delete process.env[KEY_ENV]; });
  const adapter = createUpstreamAdapter({ provider: PROVIDER, baseUrl: 'http://127.0.0.1:9', apiKeyEnv: KEY_ENV, model: MODEL });
  assert.deepEqual(await adapter.resolveModel(PROVIDER, MODEL), { provider: PROVIDER, id: MODEL, name: MODEL });
  assert.deepEqual(adapter.providerInfo(PROVIDER), { id: PROVIDER, name: PROVIDER });
  const reasoning = createUpstreamAdapter({ provider: PROVIDER, baseUrl: 'http://127.0.0.1:9', apiKeyEnv: KEY_ENV, model: MODEL, reasoningEfforts: ['high'] });
  assert.deepEqual((await reasoning.resolveModel(PROVIDER, MODEL)).reasoning, { efforts: [{ id: 'high', name: 'high' }] });
});

test('unrepresentable content blocks fail before any upstream dispatch', async (t) => {
  const upstream = await fakeUpstream(t);
  process.env[KEY_ENV] = KEY;
  t.after(() => { delete process.env[KEY_ENV]; });
  const adapter = createUpstreamAdapter({ provider: PROVIDER, baseUrl: upstream.url, apiKeyEnv: KEY_ENV, model: MODEL });
  const attachment = { role: 'user', content: [{ type: 'image', attachment: { path: '/host/secret' } } as unknown as ContentBlock] } as unknown as RequestMessage;
  await assert.rejects(collect(adapter.stream({ ...simpleRequest(), messages: [attachment] })),
    (error: unknown) => error instanceof LlmError && error.code === 'UNSUPPORTED_CONTENT');
  assert.equal(upstream.requests.length, 0);
});

test('history maps onto the wire: text, tool calls and tool results, prior reasoning omitted', async (t) => {
  const upstream = await fakeUpstream(t);
  process.env[KEY_ENV] = KEY;
  t.after(() => { delete process.env[KEY_ENV]; });
  const adapter = createUpstreamAdapter({ provider: PROVIDER, baseUrl: upstream.url, apiKeyEnv: KEY_ENV, model: MODEL });
  const history: RequestMessage[] = [
    { role: 'user', content: [{ type: 'text', text: 'use the tool' }] },
    {
      id: MessageId('m1'),
      role: 'assistant',
      source: { kind: 'model', provider: PROVIDER, model: MODEL },
      content: [
        { type: 'reasoning', text: 'thoughts' },
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
  await collect(adapter.stream({ ...simpleRequest(), messages: history, maxTokens: 32, system: 'be brief', temperature: 0.2 }));
  const sent = upstream.requests.find((request) => request.url === '/chat/completions')!;
  const body = sent.body as Record<string, unknown>;
  assert.deepEqual(body['messages'], [
    { role: 'system', content: 'be brief' },
    { role: 'user', content: 'use the tool' },
    { role: 'assistant', tool_calls: [{ id: 'call_1', type: 'function', function: { name: 'get_weather', arguments: '{"city":"SF"}' } }] },
    { role: 'tool', tool_call_id: 'call_1', content: 'sunny' },
  ]);
  assert.equal(body['max_tokens'], 32);
  assert.equal(body['temperature'], 0.2);
});

test('tool-call and reasoning deltas stream through as assembled blocks', async (t) => {
  const upstream = await fakeUpstream(t, {
    chat: (_request, response) => {
      sse(response, [
        { id: 'chatcmpl-tools', choices: [{ index: 0, delta: { reasoning_content: 'thinking' }, finish_reason: null }] },
        { id: 'chatcmpl-tools', choices: [{ index: 0, delta: { tool_calls: [{ index: 0, id: 'call_9', type: 'function', function: { name: 'search', arguments: '{"q":"' } }] }, finish_reason: null }] },
        { id: 'chatcmpl-tools', choices: [{ index: 0, delta: { tool_calls: [{ index: 0, function: { arguments: 'x"}' } }] }, finish_reason: null }] },
        { id: 'chatcmpl-tools', choices: [{ index: 0, delta: {}, finish_reason: 'tool_calls' }] },
        { id: 'chatcmpl-tools', choices: [], usage: { prompt_tokens: 6, completion_tokens: 3, completion_tokens_details: { reasoning_tokens: 2 } } },
      ]);
    },
  });
  process.env[KEY_ENV] = KEY;
  t.after(() => { delete process.env[KEY_ENV]; });
  const adapter = createUpstreamAdapter({ provider: PROVIDER, baseUrl: upstream.url, apiKeyEnv: KEY_ENV, model: MODEL });
  const chunks = await collect(adapter.stream(simpleRequest()));
  assert.deepEqual(chunks.filter((chunk) => chunk.type === 'block-start').map((chunk) => chunk.type === 'block-start' ? chunk.blockType : ''),
    ['reasoning', 'tool-call']);
  assert.deepEqual(chunks.filter((chunk) => chunk.type === 'block-end').map((chunk) => chunk.type === 'block-end' ? chunk.block : null), [
    { type: 'reasoning', text: 'thinking' },
    { type: 'tool-call', id: ToolCallId('call_9'), name: 'search', arguments: '{"q":"x"}' },
  ]);
  assert.deepEqual(chunks.find((chunk) => chunk.type === 'usage')?.type === 'usage' ? chunks.find((chunk) => chunk.type === 'usage')!.usage : null,
    { inputTokens: 6, outputTokens: 3, reasoningTokens: 2 });
  assert.deepEqual(chunks.at(-1), { type: 'finish', reason: { kind: 'tool-calls' }, replayState: { response: { id: 'chatcmpl-tools' } } });
});

test('cached prompt tokens are split out of the disjoint input counter', async (t) => {
  const upstream = await fakeUpstream(t, {
    chat: (_request, response) => {
      sse(response, [
        { id: 'chatcmpl-cache', choices: [{ index: 0, delta: { content: 'hi' }, finish_reason: 'stop' }] },
        { id: 'chatcmpl-cache', choices: [], usage: { prompt_tokens: 10, prompt_tokens_details: { cached_tokens: 4 }, completion_tokens: 3, total_tokens: 13 } },
      ]);
    },
  });
  process.env[KEY_ENV] = KEY;
  t.after(() => { delete process.env[KEY_ENV]; });
  const adapter = createUpstreamAdapter({ provider: PROVIDER, baseUrl: upstream.url, apiKeyEnv: KEY_ENV, model: MODEL });
  const chunks = await collect(adapter.stream(simpleRequest()));
  const usage = chunks.find((chunk) => chunk.type === 'usage');
  assert.deepEqual(usage?.type === 'usage' ? usage.usage : null, { inputTokens: 6, outputTokens: 3, cacheReadTokens: 4, totalTokens: 13 });
});

test('the adapter classifies upstream HTTP status codes', async (t) => {
  for (const [status, code] of [[401, 'AUTH'], [429, 'RATE_LIMIT'], [500, 'SERVER'], [400, 'INVALID_REQUEST']] as const) {
    const upstream = await fakeUpstream(t, {
      chat: (_request, response) => { response.writeHead(status).end(); },
    });
    process.env[KEY_ENV] = KEY;
    t.after(() => { delete process.env[KEY_ENV]; });
    const adapter = createUpstreamAdapter({ provider: PROVIDER, baseUrl: upstream.url, apiKeyEnv: KEY_ENV, model: MODEL });
    await assert.rejects(collect(adapter.stream(simpleRequest())),
      (error: unknown) => error instanceof LlmError && error.code === code, `status ${status}`);
    assert.equal(upstream.requests.length, 1);
  }
});

test('a malformed SSE stream fails closed instead of inventing a terminal', async (t) => {
  const cases: { chat: (request: Recorded, response: ServerResponse) => void; code: string }[] = [
    { chat: (_r, res) => { res.writeHead(200, { 'content-type': 'text/event-stream' }).end('data: [DONE]\n\n'); }, code: 'MALFORMED_RESPONSE' },
    { chat: (_r, res) => { res.writeHead(200, { 'content-type': 'text/event-stream' }).end('data: {"choices":[{"delta":{}}]}\n\ndata: [DONE]\n\n'); }, code: 'MALFORMED_RESPONSE' },
    { chat: (_r, res) => { res.writeHead(200, { 'content-type': 'text/event-stream' }).end('data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'); }, code: 'STREAM_CLOSED' },
  ];
  for (const item of cases) {
    const upstream = await fakeUpstream(t, { chat: item.chat });
    process.env[KEY_ENV] = KEY;
    t.after(() => { delete process.env[KEY_ENV]; });
    const adapter = createUpstreamAdapter({ provider: PROVIDER, baseUrl: upstream.url, apiKeyEnv: KEY_ENV, model: MODEL });
    await assert.rejects(collect(adapter.stream(simpleRequest())),
      (error: unknown) => error instanceof LlmError && error.code === item.code);
  }
});

// -------------------------------------------------------- meter unit tests

test('the meter counts the exact wire body and adds the configured margin', async (t) => {
  const upstream = await fakeUpstream(t);
  process.env[KEY_ENV] = KEY;
  t.after(() => { delete process.env[KEY_ENV]; });
  const bound = createProviderCountBound({ baseUrl: upstream.url, apiKeyEnv: KEY_ENV });
  assert.equal(await bound(simpleRequest()), 20);
  const zero = createProviderCountBound({ baseUrl: upstream.url, apiKeyEnv: KEY_ENV, margin: 0 });
  assert.equal(await zero(simpleRequest()), 12);
  const counted = upstream.requests.find((request) => request.url === '/tokens/count')!;
  assert.equal(counted.headers['authorization'], `Bearer ${KEY}`);
  assert.equal(counted.headers['content-type'], 'application/json');
  const body = counted.body as Record<string, unknown>;
  assert.equal(body['model'], MODEL);
  assert.deepEqual(body['messages'], [{ role: 'user', content: 'hi' }]);

  const totals = await fakeUpstream(t, {
    count: (_request, response) => { response.writeHead(200, { 'content-type': 'application/json' }).end(JSON.stringify({ total_tokens: 15 })); },
  });
  const fallback = createProviderCountBound({ baseUrl: totals.url, apiKeyEnv: KEY_ENV, endpoint: `${totals.url}/tokens/count` });
  assert.equal(await fallback(simpleRequest()), 23);
});

test('the meter fails closed on endpoint failures and malformed counts', async (t) => {
  const failing = await fakeUpstream(t, {
    count: (_request, response) => { response.writeHead(500).end('nope'); },
  });
  const garbage = await fakeUpstream(t, {
    count: (_request, response) => { response.writeHead(200, { 'content-type': 'application/json' }).end('{"inputTokens":"many"}'); },
  });
  const empty = await fakeUpstream(t, {
    count: (_request, response) => { response.writeHead(200, { 'content-type': 'application/json' }).end('{}'); },
  });
  process.env[KEY_ENV] = KEY;
  t.after(() => { delete process.env[KEY_ENV]; });
  for (const upstream of [failing, garbage, empty]) {
    const bound = createProviderCountBound({ baseUrl: upstream.url, apiKeyEnv: KEY_ENV });
    await assert.rejects(bound(simpleRequest()), (error: unknown) => error instanceof LlmError);
  }
  assert.throws(() => createProviderCountBound({ baseUrl: failing.url, apiKeyEnv: 'AEVAL_NEVER_SET_KEY' }),
    (error: unknown) => error instanceof LlmError && error.code === 'MISSING_CREDENTIAL');
});
