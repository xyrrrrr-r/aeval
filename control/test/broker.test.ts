import assert from 'node:assert/strict';
import { once } from 'node:events';
import { ServerResponse } from 'node:http';
import { createConnection } from 'node:net';
import { existsSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { setImmediate as turn } from 'node:timers/promises';
import test from 'node:test';
import { BlockAssembler, LlmAdapter, ReasoningEffortId, ToolCallId } from '@deepseek-ai/dsh-llm';
import type { GenerateOptions, LlmResolvedModelInfo, StreamChunk, TokenUsage } from '@deepseek-ai/dsh-llm';
import { SessionId } from '@deepseek-ai/dsh-session';
import { cleanupJobToken, GatewayLease, isLoopbackHost, startHostBroker, writeJobToken } from '../src/host_broker.js';
import type { HostBroker, HostBrokerOptions } from '../src/host_broker.js';
import { BrokerAdapter, GATEWAY_PROTOCOL, GatewayError, MAX_WIRE_BYTES, parseBrokerRequest, resolveAuxiliaryPolicy, usageTotals } from '../src/gateway_lease.js';
import type { GatewayDispatchRecord, GatewayRejectionRecord } from '../src/gateway_lease.js';
import { EvalControlConfigError, type EvalControlConfig, type RunBinding } from '../src/config.js';

const run: RunBinding = Object.freeze({ run_id: 'run', job_config_hash: 'b'.repeat(64), config_file_sha256: 'c'.repeat(64), runtime_lock_digest: 'd'.repeat(64) });
const model: LlmResolvedModelInfo = { provider: 'test', id: 'model', name: 'Offline model', reasoning: { efforts: [{ id: ReasoningEffortId('high'), name: 'High' }] } };
const usage: TokenUsage = { inputTokens: 5, cacheReadTokens: 3, cacheWriteTokens: 2, outputTokens: 4, reasoningTokens: 2, totalTokens: 14 };
const request = (): GenerateOptions => ({ provider: 'test', model: 'model', sessionId: SessionId('session'), messages: [{ role: 'user', content: [{ type: 'text', text: 'hello' }] }] });
const json = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T;
const never = <T>(): Promise<T> => new Promise(() => {});
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
async function promptly<T>(promise: Promise<T>): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    return await Promise.race([promise, new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error('operation did not settle')), 1500); })]);
  } finally { clearTimeout(timer); }
}
async function collect(stream: AsyncIterable<StreamChunk>): Promise<StreamChunk[]> {
  const result: StreamChunk[] = [];
  for await (const chunk of stream) result.push(chunk);
  return result;
}
async function* success(): AsyncIterable<StreamChunk> {
  yield { type: 'text-delta', index: 0, text: 'answer' };
  yield { type: 'usage', usage };
  yield { type: 'finish', reason: { kind: 'stop' } };
}
class OfflineAdapter extends LlmAdapter {
  readonly requests: GenerateOptions[] = [];
  constructor(readonly produce: (options: GenerateOptions) => AsyncIterable<StreamChunk> = success) { super(); }
  override async resolveModel() { return model; }
  override stream(options: GenerateOptions) { this.requests.push(options); return this.produce(options); }
}
function policy(upstream: LlmAdapter, overrides: Partial<HostBrokerOptions> = {}): HostBrokerOptions {
  return { run, trialId: 'trial', sessionId: 'session', configDigest: 'a'.repeat(64), identity: { provider: 'test', model: 'model' }, limits: { maxSteps: 10, maxTokens: 100 }, maxOutputTokens: 20, upstream, inputTokenUpperBound: () => 10, signal: new AbortController().signal, ...overrides };
}
function client(
  broker?: HostBroker, overrides: Partial<EvalControlConfig> = {},
  onRejection?: (record: GatewayRejectionRecord) => void,
  onDispatch?: (record: GatewayDispatchRecord) => void,
) {
  return new BrokerAdapter({ run, trialId: 'trial', sessionId: 'session', sessionRoot: '.', configDigest: 'a'.repeat(64), provider: 'test', model: 'model', maxSteps: 10, maxTokens: 100, bundlePath: 'unused', gatewayUrl: broker?.url ?? 'http://127.0.0.1:1', jobTokenFile: 'unused', refuseAuxiliaryCalls: true, ...overrides }, broker?.token ?? 'a'.repeat(64), onRejection, onDispatch);
}
const rejected = (code: string, reason?: string) => (error: unknown) => error instanceof GatewayError && error.code === code && (reason === undefined || error.stopReason === reason);
const ndjson = (chunks: unknown[]) => chunks.map((chunk) => JSON.stringify(chunk)).join('\n') + '\n';

async function* replayStream(): AsyncIterable<StreamChunk> {
  // First-seen ordering, delta-only assembly, first-close-wins, and truncation
  // pruning are deliberately different from naive block-end collection.
  yield { type: 'reasoning-delta', index: 7, text: 'thought' };
  yield { type: 'tool-call-delta', index: 2, id: ToolCallId('call'), name: 'tool', argumentsDelta: '{' };
  yield { type: 'block-end', index: 9, block: { type: 'text', text: 'answer' } };
  yield { type: 'text-delta', index: 9, text: 'ignored' };
  yield { type: 'usage', usage };
  yield { type: 'finish', reason: { kind: 'max-tokens' }, replayState: { response: { nativeId: 'response-1' }, blocks: [{ signature: 'thought' }, { nativeCall: 'call' }, { nativeText: 'answer' }] } };
}

test('host startup and direct lease construction reject absent or invalid run bindings', async (t) => {
  const upstream = new OfflineAdapter();
  const resolved = t.mock.method(upstream, 'resolveModel');
  const invalid: unknown[] = [undefined, null, [], 'run', Object.create(run), { ...run, extra: true }, { ...run, run_id: 'bad id' }];
  for (const key of Object.keys(run)) {
    const missing: Record<string, unknown> = { ...run };
    delete missing[key];
    invalid.push(missing, { ...run, [key]: undefined });
    if (key !== 'run_id') invalid.push({ ...run, [key]: 'A'.repeat(64) }, { ...run, [key]: `${'a'.repeat(64)}\n` });
  }
  for (const value of invalid) {
    const options = policy(upstream, { run: value as RunBinding });
    assert.throws(() => new GatewayLease(options, model), EvalControlConfigError);
    await assert.rejects(startHostBroker(options), EvalControlConfigError);
  }
  for (const change of [{ trialId: '' }, { trialId: 'bad id' }, { sessionId: 'bad\nvalue' }, { configDigest: 'A'.repeat(64) }]) {
    const options = policy(upstream, change);
    assert.throws(() => new GatewayLease(options, model), EvalControlConfigError);
    await assert.rejects(startHostBroker(options), EvalControlConfigError);
  }
  assert.equal(resolved.mock.callCount(), 0);
});

test('host binding copies are frozen at construction and before asynchronous startup', async (t) => {
  const mutable = { ...run };
  const upstream = new OfflineAdapter();
  const options = { ...policy(upstream), run: mutable };
  const lease = new GatewayLease(options, model);
  assert.notEqual(lease.snapshot().run, mutable);
  assert.equal(Object.isFrozen(mutable), false);
  assert.equal(Object.isFrozen(lease.snapshot().run), true);
  assert.equal(Object.isFrozen(lease.snapshot()), true);
  assert.throws(() => Object.assign(lease.snapshot().run, { run_id: 'other' }), TypeError);
  const pending = deferred<LlmResolvedModelInfo>();
  t.mock.method(upstream, 'resolveModel', () => pending.promise);
  const starting = startHostBroker(options);
  for (const key of Object.keys(mutable) as (keyof RunBinding)[]) mutable[key] = 'changed';
  options.run = { ...run, run_id: 'replacement' };
  options.trialId = 'changed-trial';
  options.sessionId = 'changed-session';
  options.configDigest = 'e'.repeat(64);
  pending.resolve(model);
  const broker = await starting;
  try {
    assert.deepEqual(lease.snapshot().run, run);
    const info = await client(broker).info();
    assert.equal(info.protocol, 'aeval-model-broker/3');
    assert.deepEqual(info.run, run);
    assert.equal(Object.isFrozen(info.run), true);
    assert.equal(broker.lease.snapshot().trialId, 'trial');
    assert.equal(broker.lease.snapshot().sessionId, 'session');
    assert.equal(broker.lease.snapshot().configDigest, 'a'.repeat(64));
  } finally { await broker.close(); }
});

test('info rejects old protocol, missing run and every independently mismatched binding field', async (t) => {
  const info = new GatewayLease(policy(new OfflineAdapter()), model).snapshot();
  assert.equal(GATEWAY_PROTOCOL, 'aeval-model-broker/3');
  const variants: Record<string, unknown>[] = [
    { ...info, protocol: 'aeval-model-broker/1' }, { ...info, run: undefined },
    { ...info, run: null }, { ...info, run: { ...run, extra: true } },
    { ...info, trialId: 'other-trial' }, { ...info, sessionId: 'other-session' },
    { ...info, configDigest: 'e'.repeat(64) },
  ];
  for (const key of Object.keys(run)) {
    const missing: Record<string, unknown> = { ...run };
    delete missing[key];
    variants.push({ ...info, run: missing });
    variants.push({ ...info, run: { ...run, [key]: key === 'run_id' ? 'other-run' : 'e'.repeat(64) } });
    if (key !== 'run_id') variants.push({ ...info, run: { ...run, [key]: 'A'.repeat(64) } });
  }
  const digests = ['job_config_hash', 'config_file_sha256', 'runtime_lock_digest'] as const;
  for (const left of digests) {
    // Config digest has a distinct meaning too; even a paired swap cannot authorize it.
    variants.push({ ...info, configDigest: run[left], run: { ...run, [left]: info.configDigest } });
    for (const right of digests) {
      if (left < right) variants.push({ ...info, run: { ...run, [left]: run[right], [right]: run[left] } });
    }
  }
  for (const variant of variants) {
    const mocked = t.mock.method(globalThis, 'fetch', async () => Response.json(variant));
    await assert.rejects(client().info(), rejected('AEVAL_LEASE_MISMATCH'));
    mocked.mock.restore();
  }
  t.mock.method(globalThis, 'fetch', async () => Response.json(info));
  const mutable = { ...run };
  const adapter = client(undefined, { run: mutable });
  mutable.run_id = 'changed';
  // Matching fields succeed despite every digest representing different bytes.
  assert.deepEqual((await adapter.info()).run, run);
});

test('two trials sharing a run retain distinct lease identities and counters', { timeout: 5000 }, async () => {
  const upstreamA = new OfflineAdapter();
  const upstreamB = new OfflineAdapter();
  const first = await startHostBroker(policy(upstreamA));
  let second: HostBroker | undefined;
  try {
    const identityB = { trialId: 'trial-b', sessionId: 'session-b', configDigest: 'e'.repeat(64) };
    second = await startHostBroker(policy(upstreamB, identityB));
    const infoA = await client(first).info();
    const infoB = await client(second, identityB).info();
    assert.deepEqual(infoA.run, infoB.run);
    assert.notEqual(infoA.trialId, infoB.trialId);
    assert.notEqual(infoA.sessionId, infoB.sessionId);
    assert.notEqual(infoA.configDigest, infoB.configDigest);
    await assert.rejects(client(second).info(), rejected('AEVAL_LEASE_MISMATCH'));
    await assert.rejects(client(first, identityB).info(), rejected('AEVAL_LEASE_MISMATCH'));
    await collect(client(first).stream(request()));
    assert.equal(first.lease.snapshot().usedSteps, 1);
    assert.equal(second.lease.snapshot().usedSteps, 0);
    await assert.rejects(collect(second.lease.stream(request(), new AbortController().signal)), rejected('AEVAL_SESSION_MISMATCH'));
    assert.equal(upstreamB.requests.length, 0);
    await collect(client(second, identityB).stream({ ...request(), sessionId: SessionId(identityB.sessionId) }));
    assert.equal(second.lease.snapshot().usedSteps, 1);
    assert.equal(first.lease.snapshot().usedSteps, 1);
  } finally { await first.close(); await second?.close(); }
});

test('two-turn HTTP replay roundtrips through the real canonical DSH assembler', { timeout: 5000 }, async () => {
  const upstream = new OfflineAdapter(replayStream);
  const broker = await startHostBroker(policy(upstream));
  try {
    const adapter = client(broker);
    const assembler = new BlockAssembler();
    for (const chunk of await collect(adapter.stream(request()))) assembler.push(chunk);
    assert.deepEqual(assembler.blocks(), [{ type: 'reasoning', text: 'thought' }, { type: 'text', text: 'answer' }]);
    assert.deepEqual(assembler.replayState?.blocks, [{ signature: 'thought' }, { nativeText: 'answer' }]);
    const assistant = assembler.message({ provider: 'test', model: 'model', replayState: assembler.replayState });
    await collect(adapter.stream({ ...request(), messages: [...request().messages, assistant] }));
    const sent = upstream.requests[1]!.messages[1]!;
    assert.deepEqual(sent.content, assistant.content);
    assert.deepEqual(sent.source, assistant.source);
    assert.notEqual(sent.source, assistant.source);
    assert.equal(broker.lease.snapshot().usedTokens, 28);
  } finally { await broker.close(); }
});

test('replay rejects unknown, altered, misattributed, cross-lease and content-mismatched state', async () => {
  const upstream = new OfflineAdapter(replayStream);
  const lease = new GatewayLease(policy(upstream, { identity: { provider: 'test', model: 'model', reasoningEffort: 'high' } }), model);
  const assembler = new BlockAssembler();
  for (const chunk of await collect(lease.stream(request(), new AbortController().signal))) assembler.push(chunk);
  const assistant = assembler.message({ provider: 'test', model: 'model', replayState: assembler.replayState });
  const variants = [
    { ...assistant, source: { ...assistant.source, replayState: { response: { nativeId: 'forged' } } } },
    { ...assistant, source: { ...assistant.source, model: 'other' } },
    { ...assistant, source: { ...assistant.source, provider: 'other' } },
    { ...assistant, source: { ...assistant.source, extra: { path: '/host/secret' } } },
    { ...assistant, content: [{ type: 'text', text: 'replacement' }] },
    { ...assistant, source: { ...assistant.source, replayState: { ...assembler.replayState, blocks: [] } } },
  ];
  for (const variant of variants) await assert.rejects(collect(lease.stream({ ...request(), messages: [json(variant)] }, new AbortController().signal)), rejected('AEVAL_UNTRUSTED_REPLAY'));
  await assert.rejects(collect(lease.stream({ ...request(), reasoningEffort: 'low', messages: [assistant] }, new AbortController().signal)), rejected('AEVAL_UNTRUSTED_REPLAY'));
  const other = new GatewayLease(policy(upstream), model);
  await assert.rejects(collect(other.stream({ ...request(), messages: [assistant] }, new AbortController().signal)), rejected('AEVAL_UNTRUSTED_REPLAY'));
  assert.equal(upstream.requests.length, 1);
  await collect(lease.stream({ ...request(), messages: [json(assistant)] }, new AbortController().signal));
  assert.equal(upstream.requests.length, 2);
});

test('assembler-discarded replay envelopes are not authorized', async () => {
  const malformed = new OfflineAdapter(async function* () {
    yield { type: 'text-delta', index: 0, text: 'answer' };
    yield { type: 'usage', usage };
    yield { type: 'finish', reason: { kind: 'stop' }, replayState: { response: 'native', blocks: [] } };
  });
  const issued = new GatewayLease(policy(malformed), model);
  const assembler = new BlockAssembler();
  for (const chunk of await collect(issued.stream(request(), new AbortController().signal))) assembler.push(chunk);
  assert.equal(assembler.replayState, undefined);
  const assistant = assembler.message({ provider: 'test', model: 'model', replayState: { response: 'native', blocks: [] } });
  await assert.rejects(collect(issued.stream({ ...request(), messages: [assistant] }, new AbortController().signal)), rejected('AEVAL_UNTRUSTED_REPLAY'));
});

test('serialized requests continue to reject host attachment references', () => {
  for (const content of [[{ type: 'file', attachment: { path: '/host/secret' } }], [{ type: 'image', attachment: { path: '/host/secret' } }], [{ type: 'text', text: 'safe?', attachment: { path: '/host/secret' } }]]) {
    assert.throws(() => parseBrokerRequest({ ...request(), messages: [{ role: 'user', content }] }), rejected('AEVAL_UNSUPPORTED_CONTENT'));
  }
  assert.throws(() => parseBrokerRequest({ ...request(), messages: [{ role: 'user', content: [], source: { kind: 'user', replayState: {} } }] }), rejected('AEVAL_UNSUPPORTED_REPLAY'));
});

test('meter certifies the exact immutable final dispatch cap and cached usage', async () => {
  const upstream = new OfflineAdapter();
  const measured: Readonly<GenerateOptions>[] = [];
  const lease = new GatewayLease(policy(upstream, { maxOutputTokens: 80, inputTokenUpperBound: (value) => { measured.push(value); return value.maxTokens === 80 ? 30 : 25; } }), model);
  await collect(lease.stream({ ...request(), maxTokens: 90 }, new AbortController().signal));
  assert.deepEqual(measured.map((value) => value.maxTokens), [80, 70]);
  assert.equal(upstream.requests[0], measured[1]);
  assert.ok(Object.isFrozen(measured[1]));
  assert.ok(Object.isFrozen(measured[1]!.messages));
  assert.ok(measured[1]!.signal instanceof AbortSignal);
  assert.deepEqual(usageTotals(usage), { input: 10, total: 14 });
  assert.equal(lease.snapshot().usedTokens, 14);
  assert.equal(lease.snapshot().reservedTokens, 0);
  assert.equal(lease.snapshot().usedSteps, 1);
});

test('a cap-dependent bound that no longer fits refuses after exactly two measurements', async () => {
  const upstream = new OfflineAdapter();
  let measured = 0;
  const lease = new GatewayLease(policy(upstream, { maxOutputTokens: 80, inputTokenUpperBound: () => ++measured === 1 ? 30 : 31 }), model);
  await assert.rejects(collect(lease.stream(request(), new AbortController().signal)), rejected('AEVAL_BUDGET_EXHAUSTED', 'budget_exhausted'));
  assert.equal(measured, 2);
  assert.equal(upstream.requests.length, 0);
  assert.equal(lease.snapshot().usedTokens, 0);
  assert.equal(lease.snapshot().usedSteps, 0);
});

for (const mode of ['owner', 'client', 'close'] as const) test(`${mode} cancellation interrupts a hung meter and contains late rejection`, { timeout: 5000 }, async () => {
  const entered = deferred<AbortSignal>();
  const pending = deferred<number>();
  const owner = new AbortController();
  const caller = new AbortController();
  const upstream = new OfflineAdapter();
  const stops: string[] = [];
  const broker = await startHostBroker(policy(upstream, { signal: owner.signal, inputTokenUpperBound: (value) => { entered.resolve(value.signal!); return pending.promise; }, onStop: (reason) => { stops.push(reason); throw new Error('observer failed'); } }));
  try {
    const running = collect(broker.lease.stream(request(), caller.signal));
    const failure = assert.rejects(running, (error: unknown) => error instanceof GatewayError);
    const signal = await promptly(entered.promise);
    if (mode === 'owner') owner.abort();
    else if (mode === 'client') caller.abort();
    else await promptly(broker.close('timeout_killed'));
    await promptly(failure);
    assert.equal(signal.aborted, true);
    assert.equal(upstream.requests.length, 0);
    assert.equal(broker.lease.snapshot().usedTokens, 0);
    // A caller that cancels before anything was dispatched abandons no
    // work: its request fails, but the lease must survive so the run can
    // continue. Owner stops and broker closes still end the lease.
    assert.equal(stops.length, mode === 'client' ? 0 : 1);
    if (mode === 'client') assert.equal(broker.lease.snapshot().stopReason, undefined);
    pending.reject(new Error('late meter failure'));
    await turn();
  } finally { await promptly(broker.close()); }
});

test('a pre-dispatch client cancellation leaves the lease usable', { timeout: 5000 }, async () => {
  // Regression for the amplification that cost two real runs: a client
  // cancellation before dispatch closed the lease, and every later model
  // call then failed with AEVAL_LEASE_CLOSED.
  const entered = deferred<void>();
  const pending = deferred<number>();
  const caller = new AbortController();
  const upstream = new OfflineAdapter();
  let meters = 0;
  const broker = await startHostBroker(policy(upstream, {
    inputTokenUpperBound: () => { meters += 1; if (meters === 1) { entered.resolve(); return pending.promise; } return Promise.resolve(10); },
  }));
  try {
    const failure = assert.rejects(collect(broker.lease.stream(request(), caller.signal)), (error: unknown) => error instanceof GatewayError);
    await promptly(entered.promise);
    caller.abort();
    await promptly(failure);
    assert.equal(broker.lease.snapshot().stopReason, undefined);
    // The very next call must still reach the provider.
    const chunks = await promptly(collect(broker.lease.stream(request(), new AbortController().signal)));
    assert.equal(chunks.at(-1)?.type, 'finish');
    assert.equal(upstream.requests.length, 1);
  } finally { await promptly(broker.close()); }
});

test('a caller that aborts while a provider call is in flight still stops the lease', { timeout: 5000 }, async () => {
  // Fail-closed is preserved where it matters: an abandoned in-flight
  // provider call has unknown usage and stays unaccountable.
  const entered = deferred<void>();
  const upstream = new OfflineAdapter(() => (async function* () { entered.resolve(); await never(); })());
  const caller = new AbortController();
  const broker = await startHostBroker(policy(upstream));
  try {
    const failure = assert.rejects(collect(broker.lease.stream(request(), caller.signal)), (error: unknown) => error instanceof GatewayError);
    await promptly(entered.promise);
    caller.abort();
    await promptly(failure);
    assert.equal(broker.lease.snapshot().stopReason, 'infra_error');
    assert.equal(upstream.requests.length, 1);
  } finally { await promptly(broker.close()); }
});

test('close resolves an active HTTP handler whose meter ignores cancellation', { timeout: 5000 }, async () => {
  const entered = deferred<void>();
  const broker = await startHostBroker(policy(new OfflineAdapter(), { inputTokenUpperBound: () => { entered.resolve(); return never<number>(); } }));
  const running = collect(client(broker).stream(request()));
  const failure = assert.rejects(running);
  await promptly(entered.promise);
  await promptly(broker.close('timeout_killed'));
  await promptly(failure);
  assert.equal(broker.lease.snapshot().stopReason, 'timeout_killed');
});

test('owner timeout aborts hung EOF cleanup, withholds finish and charges the reservation', { timeout: 5000 }, async () => {
  const entered = deferred<void>();
  const cleanup = deferred<void>();
  const upstream = new OfflineAdapter(async function* () {
    try { yield* success(); }
    finally { entered.resolve(); await cleanup.promise; }
  });
  const lease = new GatewayLease(policy(upstream), model);
  const seen: StreamChunk[] = [];
  const running = (async () => { for await (const chunk of lease.stream(request(), new AbortController().signal)) seen.push(chunk); })();
  const failure = assert.rejects(running, (error: unknown) => error instanceof GatewayError && error.stopReason === 'timeout_killed');
  await promptly(entered.promise);
  lease.stop('timeout_killed');
  await promptly(failure);
  assert.equal(seen.some((chunk) => chunk.type === 'finish'), false);
  assert.equal(lease.snapshot().usedTokens, 30);
  assert.equal(lease.snapshot().reservedTokens, 0);
  assert.equal(upstream.requests[0]!.signal!.aborted, true);
  cleanup.reject(new Error('late iterator cleanup failure'));
  await turn();
});

test('aborted EOF cannot produce a successful terminal', async () => {
  const caller = new AbortController();
  const upstream = new OfflineAdapter(async function* () { yield* success(); caller.abort(); });
  const lease = new GatewayLease(policy(upstream), model);
  const seen: StreamChunk[] = [];
  await assert.rejects((async () => { for await (const chunk of lease.stream(request(), caller.signal)) seen.push(chunk); })());
  assert.equal(seen.some((chunk) => chunk.type === 'finish'), false);
  assert.equal(lease.snapshot().usedTokens, 30);
});

test('hung next and hung or rejecting return do not delay cancellation', async () => {
  for (const rejectReturn of [false, true]) {
    const entered = deferred<void>();
    const pending = deferred<IteratorResult<StreamChunk>>();
    let returned = 0;
    const upstream = new OfflineAdapter(() => ({ [Symbol.asyncIterator]: () => ({ next: () => { entered.resolve(); return pending.promise; }, return: () => { returned++; return rejectReturn ? Promise.reject(new Error('return failed')) : never<IteratorResult<StreamChunk>>(); } }) }));
    const lease = new GatewayLease(policy(upstream), model);
    const running = collect(lease.stream(request(), new AbortController().signal));
    const failure = assert.rejects(running);
    await promptly(entered.promise);
    lease.stop('timeout_killed');
    await promptly(failure);
    assert.equal(returned, 1);
    assert.equal(lease.snapshot().usedTokens, 30);
    pending.reject(new Error('late next failure'));
    await turn();
  }
});

test('busy requests neither dispatch nor stop the active lease', async () => {
  const entered = deferred<void>();
  const proceed = deferred<void>();
  const upstream = new OfflineAdapter(async function* () { entered.resolve(); await proceed.promise; yield* success(); });
  const lease = new GatewayLease(policy(upstream), model);
  const running = collect(lease.stream(request(), new AbortController().signal));
  await promptly(entered.promise);
  assert.equal(lease.snapshot().reservedTokens, 30);
  await assert.rejects(collect(lease.stream(request(), new AbortController().signal)), rejected('AEVAL_LEASE_BUSY'));
  assert.equal(lease.signal.aborted, false);
  assert.equal(upstream.requests.length, 1);
  proceed.resolve();
  await promptly(running);
  assert.equal(lease.snapshot().usedSteps, 1);
});

test('cached input exceeding the certified bound charges conservatively and stops', async () => {
  const upstream = new OfflineAdapter(async function* () { yield { type: 'usage', usage: { inputTokens: 1, cacheReadTokens: 31, outputTokens: 1, totalTokens: 33 } }; });
  const lease = new GatewayLease(policy(upstream), model);
  await assert.rejects(collect(lease.stream(request(), new AbortController().signal)), rejected('AEVAL_TOKEN_BOUND_VIOLATED'));
  assert.equal(lease.snapshot().usedTokens, 33);
  assert.equal(lease.snapshot().stopReason, 'infra_error');
});

for (const reason of ['budget_exhausted', 'timeout_killed'] as const) test(`client notifies ${reason} exactly once and contains throwing listeners`, async (t) => {
  t.mock.method(globalThis, 'fetch', async () => new Response(null, { status: 422, headers: { 'aeval-stop-reason': reason, 'aeval-error-code': 'AEVAL_BUDGET_EXHAUSTED' } }));
  const adapter = client();
  const seen: string[] = [];
  adapter.observeTerminal(() => { throw new Error('listener failed'); });
  adapter.observeTerminal(async () => { throw new Error('async listener failed'); });
  adapter.observeTerminal((value) => seen.push(value));
  await assert.rejects(collect(adapter.stream(request())), rejected('AEVAL_BUDGET_EXHAUSTED', reason));
  assert.deepEqual(seen, [reason]);
  await turn();
});

test('client preserves in-band budget failure without a second generic notification', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => new Response(ndjson([{ type: 'gateway-error', stopReason: 'budget_exhausted' }])));
  const adapter = client();
  const seen: string[] = [];
  adapter.observeTerminal((reason) => seen.push(reason));
  await assert.rejects(collect(adapter.stream(request())), rejected('AEVAL_GATEWAY_FAILED', 'budget_exhausted'));
  assert.deepEqual(seen, ['budget_exhausted']);
});

test('client enforces cancellation before a buffered terminal and before EOF success', async (t) => {
  for (const atEof of [false, true]) {
    const caller = new AbortController();
    let reads = 0;
    const body = new ReadableStream<Uint8Array>({ pull(controller) {
      if (reads++ === 0) controller.enqueue(Buffer.from(ndjson([{ type: 'usage', usage }, { type: 'finish', reason: { kind: 'stop' } }])));
      else { caller.abort(new GatewayError('AEVAL_LEASE_CLOSED', 'timeout_killed')); controller.close(); }
    } }, { highWaterMark: 0 });
    const mocked = t.mock.method(globalThis, 'fetch', async () => new Response(body));
    const adapter = client();
    const seen: string[] = [];
    adapter.observeTerminal((reason) => seen.push(reason));
    const stream = adapter.stream({ ...request(), signal: caller.signal })[Symbol.asyncIterator]();
    assert.equal((await stream.next()).value?.type, 'usage');
    if (!atEof) caller.abort(new GatewayError('AEVAL_LEASE_CLOSED', 'timeout_killed'));
    await assert.rejects(stream.next(), rejected('AEVAL_LEASE_CLOSED', 'timeout_killed'));
    assert.deepEqual(seen, ['timeout_killed']);
    mocked.mock.restore();
  }
});

test('client cancellation and early return do not wait for hung reader cleanup', async (t) => {
  const cancelled = deferred<void>();
  const body = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(Buffer.from(ndjson([{ type: 'text-delta', index: 0, text: 'hello' }]))); }, cancel() { cancelled.resolve(); return never<void>(); } });
  t.mock.method(globalThis, 'fetch', async () => new Response(body));
  const adapter = client();
  const seen: string[] = [];
  adapter.observeTerminal((reason) => seen.push(reason));
  const stream = adapter.stream(request())[Symbol.asyncIterator]();
  await stream.next();
  await promptly(stream.return!());
  await promptly(cancelled.promise);
  assert.deepEqual(seen, ['infra_error']);
});

test('consumer return after timeout retains that reason instead of infra_error', async (t) => {
  const body = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(Buffer.from(ndjson([{ type: 'text-delta', index: 0, text: 'hello' }]))); } });
  t.mock.method(globalThis, 'fetch', async () => new Response(body));
  const caller = new AbortController();
  const adapter = client();
  const seen: string[] = [];
  adapter.observeTerminal((reason) => seen.push(reason));
  const stream = adapter.stream({ ...request(), signal: caller.signal })[Symbol.asyncIterator]();
  await stream.next();
  caller.abort(new GatewayError('AEVAL_LEASE_CLOSED', 'timeout_killed'));
  await promptly(stream.return!());
  assert.deepEqual(seen, ['timeout_killed']);
});

test('a caller cancelling its own request reports no trusted failure', async (t) => {
  // The runtime aborts model calls for ordinary control-flow reasons. A
  // bare abort carries no gateway failure, so it must not be reported as
  // a trusted infrastructure terminal: doing so recorded infra_error on a
  // healthy run and permanently downgraded the trial (real-chain finding).
  const body = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(Buffer.from(ndjson([{ type: 'text-delta', index: 0, text: 'hello' }]))); } });
  t.mock.method(globalThis, 'fetch', async () => new Response(body));
  const caller = new AbortController();
  const adapter = client();
  const seen: string[] = [];
  adapter.observeTerminal((reason) => seen.push(reason));
  const stream = adapter.stream({ ...request(), signal: caller.signal })[Symbol.asyncIterator]();
  await stream.next();
  caller.abort(new Error('cancelled by the runtime'));
  await promptly(stream.return!());
  assert.deepEqual(seen, []);
});

test('a real gateway failure still reports a trusted terminal with a live caller signal', async (t) => {
  // Guards the opposite direction: the cancellation exemption must not
  // swallow genuine gateway failures.
  t.mock.method(globalThis, 'fetch', async () => new Response(ndjson([{ type: 'usage', usage }, { type: 'finish', reason: { kind: 'error', failure: { code: 'FAILED', message: 'failure' } } }])));
  const adapter = client();
  const seen: string[] = [];
  adapter.observeTerminal((reason) => seen.push(reason));
  await assert.rejects(collect(adapter.stream({ ...request(), signal: new AbortController().signal })), rejected('AEVAL_UPSTREAM_FAILED'));
  assert.deepEqual(seen, ['infra_error']);
});

test('a busy-lease conflict is retryable, not a trusted failure', async (t) => {
  // The runtime issues advisory calls (session title) next to the real one;
  // the broker answers 409 for the loser and deliberately keeps the lease.
  // Reporting that as a trusted terminal recorded infra_error on a healthy
  // run and blocked the owner's finalization (real-chain finding).
  t.mock.method(globalThis, 'fetch', async () => new Response('', { status: 409, headers: { 'aeval-error-code': 'AEVAL_LEASE_BUSY' } }));
  const adapter = client();
  const seen: string[] = [];
  adapter.observeTerminal((reason) => seen.push(reason));
  await assert.rejects(collect(adapter.stream(request())), rejected('AEVAL_LEASE_BUSY'));
  assert.deepEqual(seen, []);
});

test('coalesced valid NDJSON lines may exceed the per-line limit together', async (t) => {
  const text = 'x'.repeat(MAX_WIRE_BYTES / 2);
  const payload = ndjson([{ type: 'text-delta', index: 0, text }, { type: 'text-delta', index: 0, text }, { type: 'usage', usage }, { type: 'finish', reason: { kind: 'stop' } }]);
  assert.ok(Buffer.byteLength(payload) > MAX_WIRE_BYTES);
  t.mock.method(globalThis, 'fetch', async () => new Response(payload));
  const seen: string[] = [];
  const adapter = client();
  adapter.observeTerminal((reason) => seen.push(reason));
  assert.equal((await collect(adapter.stream(request()))).length, 4);
  assert.deepEqual(seen, []);
});

test('malformed, oversized, invalid UTF-8 and truncated NDJSON fail once', async (t) => {
  const goodUsage = { type: 'usage', usage };
  const finish = { type: 'finish', reason: { kind: 'stop' } };
  const invalid: (string | Uint8Array)[] = [
    '\n', '{broken}\n', ndjson([goodUsage]), ndjson([finish]), JSON.stringify(goodUsage),
    ndjson([goodUsage, goodUsage, finish]), ndjson([goodUsage, finish, finish]),
    ndjson([goodUsage, { type: 'text-delta', index: 0, text: 'late' }, finish]),
    ndjson([{ type: 'text-delta', index: 0, text: 'x'.repeat(MAX_WIRE_BYTES) }]),
    'x'.repeat(MAX_WIRE_BYTES), new Uint8Array([0xff, 10]),
    ndjson([goodUsage, { type: 'finish', reason: { kind: 'error', failure: { code: 'FAILED', message: 'failure' } } }]),
  ];
  for (const payload of invalid) {
    const mocked = t.mock.method(globalThis, 'fetch', async () => new Response(typeof payload === 'string' ? payload : Buffer.from(payload)));
    const adapter = client();
    const seen: string[] = [];
    adapter.observeTerminal((reason) => seen.push(reason));
    await assert.rejects(collect(adapter.stream(request())), (error: unknown) => error instanceof GatewayError);
    assert.deepEqual(seen, ['infra_error']);
    mocked.mock.restore();
  }
});

test('HTTP budget errors retain the authoritative stop reason', { timeout: 5000 }, async () => {
  const broker = await startHostBroker(policy(new OfflineAdapter(), { limits: { maxTokens: 5 } }));
  try {
    const adapter = client(broker);
    const seen: string[] = [];
    adapter.observeTerminal((reason) => seen.push(reason));
    await assert.rejects(collect(adapter.stream(request())), (error: unknown) => error instanceof GatewayError && error.stopReason === 'budget_exhausted');
    assert.deepEqual(seen, ['budget_exhausted']);
  } finally { await promptly(broker.close()); }
});

test('HTTP timeout interrupts a hung upstream and reports timeout once', { timeout: 5000 }, async () => {
  const entered = deferred<void>();
  const broker = await startHostBroker(policy(new OfflineAdapter(async function* () { entered.resolve(); await never<void>(); }), { timeoutMs: 50 }));
  try {
    const adapter = client(broker);
    const seen: string[] = [];
    adapter.observeTerminal((reason) => seen.push(reason));
    const running = collect(adapter.stream(request()));
    const failure = assert.rejects(running, (error: unknown) => error instanceof GatewayError && error.stopReason === 'timeout_killed');
    await promptly(entered.promise);
    await promptly(failure);
    assert.deepEqual(seen, ['timeout_killed']);
    assert.equal(broker.lease.snapshot().usedTokens, 30);
  } finally { await promptly(broker.close()); }
});

test('close cancels an incomplete HTTP request body', { timeout: 5000 }, async () => {
  const broker = await startHostBroker(policy(new OfflineAdapter()));
  const socket = createConnection(Number(new URL(broker.url).port), '127.0.0.1');
  socket.on('error', () => {});
  try {
    await once(socket, 'connect');
    socket.write(`POST /stream HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer ${broker.token}\r\nContent-Type: application/json\r\nContent-Length: 10000\r\n\r\n{`);
    await turn();
    await promptly(broker.close('timeout_killed'));
  } finally { socket.destroy(); await broker.close(); }
});

test('lease stop cancels a real paused-client HTTP drain and destroys its response', { timeout: 5000 }, async (t) => {
  const blocked = deferred<ServerResponse>();
  const original = ServerResponse.prototype.write;
  t.mock.method(ServerResponse.prototype, 'write', function (this: ServerResponse, ...args: unknown[]) {
    const result = Reflect.apply(original, this, args) as boolean;
    if (!result) blocked.resolve(this);
    return result;
  });
  const upstream = new OfflineAdapter(async function* () {
    yield { type: 'text-delta', index: 0, text: 'x'.repeat(6 * 1024 * 1024) };
    await never<void>();
  });
  const broker = await startHostBroker(policy(upstream));
  const socket = createConnection(Number(new URL(broker.url).port), '127.0.0.1');
  socket.on('error', () => {});
  socket.pause();
  try {
    await once(socket, 'connect');
    const body = JSON.stringify(request());
    socket.write(`POST /stream HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer ${broker.token}\r\nContent-Type: application/json\r\nContent-Length: ${Buffer.byteLength(body)}\r\n\r\n${body}`);
    const response = await promptly(blocked.promise);
    assert.equal(response.writableNeedDrain, true);
    broker.lease.stop('timeout_killed');
    assert.equal(response.destroyed, true);
    await promptly(broker.close());
    assert.equal(broker.lease.snapshot().usedTokens, 30);
    assert.equal(upstream.requests[0]!.signal!.aborted, true);
  } finally { socket.destroy(); await broker.close(); }
});

test('the default listener is loopback HTTP on an ephemeral port', { timeout: 5000 }, async () => {
  const broker = await startHostBroker(policy(new OfflineAdapter(), { listen: { host: '127.0.0.1' } }));
  try {
    assert.match(broker.url, /^http:\/\/127\.0\.0\.1:[1-9]\d*$/);
    const reply = await promptly(fetch(`${broker.url}/info`, { headers: { authorization: `Bearer ${broker.token}` } }));
    assert.equal(reply.status, 200);
  } finally { await promptly(broker.close()); }
});

test('only literal loopback addresses may serve plaintext', () => {
  for (const host of ['127.0.0.1', '127.9.9.9', '::1']) assert.equal(isLoopbackHost(host), true, host);
  // A name that usually resolves to loopback is not a literal: it cannot
  // authorize plaintext on its own.
  for (const host of ['localhost', '0.0.0.0', '::', '169.254.1.1', '127.0.0.1.attacker.test']) {
    assert.equal(isLoopbackHost(host), false, host);
  }
});

test('a nonloopback listener without TLS is refused before the socket opens', async () => {
  for (const host of ['0.0.0.0', 'localhost', '::']) {
    await assert.rejects(startHostBroker(policy(new OfflineAdapter(), { listen: { host } })),
      /Nonloopback broker listeners require TLS/);
  }
});

test('TLS listeners are validated by material, not by intent', async () => {
  for (const tls of [{ key: '', cert: 'cert' }, { key: 'key', cert: '' }, { key: 7 as never, cert: 'cert' }]) {
    await assert.rejects(startHostBroker(policy(new OfflineAdapter(), { listen: { host: '::1', tls } })),
      /Invalid broker TLS material/);
  }
});

test('malformed listen targets never reach the resolver', async () => {
  for (const host of ['', ' ', 'local host', '127.0.0.1:9', '../x']) {
    await assert.rejects(startHostBroker(policy(new OfflineAdapter(), { listen: { host } })),
      /Invalid broker listen host/);
  }
  for (const port of [-1, 65_536, 1.5, Number.NaN]) {
    await assert.rejects(startHostBroker(policy(new OfflineAdapter(), { listen: { host: '127.0.0.1', port } })),
      /Invalid broker listen port/);
  }
});

test('a lifetime abort records how the lease stopped', async () => {
  // Real-chain D43: the broker's lifetime signal aborts on owner shutdown,
  // which can arrive after the descriptor was settled. The diagnostic must
  // name that path instead of leaving the cause unspecified (where it reads
  // as an unexplained mid-run failure in the trial's broker_diagnostics).
  const lines: string[] = [];
  const original = process.stderr.write.bind(process.stderr);
  process.env['AEVAL_BROKER_DIAG'] = '1';
  (process.stderr as { write: (chunk: string) => boolean }).write = (chunk: string) => {
    lines.push(String(chunk));
    return true;
  };
  let broker: Awaited<ReturnType<typeof startHostBroker>> | undefined;
  try {
    const controller = new AbortController();
    broker = await startHostBroker(policy(new OfflineAdapter(), { signal: controller.signal }));
    controller.abort();
    assert.equal(broker.lease.snapshot().stopReason, 'infra_error');
    assert.ok(lines.some((line) => line.includes('reason=infra_error cause=lifetime_abort')),
      `expected the lifetime-abort cause, saw: ${lines.join('')}`);
  } finally {
    // The listener holds the event loop open; a leaked broker kept the whole
    // test process alive after the last assertion.
    await broker?.close().catch(() => undefined);
    (process.stderr as { write: typeof original }).write = original;
    delete process.env['AEVAL_BROKER_DIAG'];
  }
});

test('a signal aborted during bind cancels the listener without an uncaught error', async () => {
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(startHostBroker(policy(new OfflineAdapter(), { signal: controller.signal })),
    (error: unknown) => (error as Error).name === 'AbortError');
});

test('a correct token past its TTL fails 401 with AEVAL_TOKEN_EXPIRED while a wrong token stays plain 401', { timeout: 5000 }, async () => {
  const broker = await startHostBroker(policy(new OfflineAdapter(), { tokenTtlMs: 800 }));
  try {
    const call = (token: string) => fetch(`${broker.url}/info`, { headers: { authorization: `Bearer ${token}` } });
    assert.equal((await promptly(call(broker.token))).status, 200);
    const wrong = await promptly(call('f'.repeat(64)));
    assert.equal(wrong.status, 401);
    assert.equal(wrong.headers.get('aeval-error-code'), null);
    await new Promise((resolve) => { setTimeout(resolve, 1000); });
    const expired = await promptly(call(broker.token));
    assert.equal(expired.status, 401);
    assert.equal(expired.headers.get('aeval-error-code'), 'AEVAL_TOKEN_EXPIRED');
    // Expiry kills the credential, not the in-process lease inspection the
    // trusted owner still relies on for finalization.
    assert.equal(broker.lease.snapshot().stopReason, undefined);
  } finally { await promptly(broker.close()); }
});

test('token TTL options are validated like the other broker limits', async () => {
  await assert.rejects(startHostBroker(policy(new OfflineAdapter(), { tokenTtlMs: 0 })), /Broker limits must be positive safe integers/);
  await assert.rejects(startHostBroker(policy(new OfflineAdapter(), { tokenTtlMs: 2_147_483_648 })), /Broker token TTL exceeds timer range/);
  for (const tokenTtlMs of [-1, 1.5, Number.NaN]) {
    await assert.rejects(startHostBroker(policy(new OfflineAdapter(), { tokenTtlMs })),
      (error: unknown) => error instanceof GatewayError && error.code === 'AEVAL_INVALID_USAGE');
  }
});

test('cleanupJobToken removes only an existing owned 0600 regular file', () => {
  const root = mkdtempSync(join(realpathSync(tmpdir()), 'aeval-cleanup-'));
  try {
    // A path that never existed is already clean.
    cleanupJobToken(join(root, 'absent'));
    const mine = join(root, 'mine');
    writeJobToken(mine, 'a'.repeat(64));
    cleanupJobToken(mine);
    assert.equal(existsSync(mine), false);
    const loose = join(root, 'loose');
    writeFileSync(loose, 'a'.repeat(64), { mode: 0o644 });
    assert.throws(() => cleanupJobToken(loose), /0600 regular file/);
    assert.equal(existsSync(loose), true);
    const target = join(root, 'target');
    writeJobToken(target, 'a'.repeat(64));
    const link = join(root, 'link');
    symlinkSync(target, link);
    assert.throws(() => cleanupJobToken(link), /0600 regular file/);
    assert.equal(existsSync(target), true);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test('a refused advisory call neither stops the lease nor consumes budget (D45)', async () => {
  const upstream = new OfflineAdapter();
  const broker = await startHostBroker(policy(upstream));
  try {
    const rejections: GatewayRejectionRecord[] = [];
    const adapter = client(broker, {}, (record) => rejections.push(record));
    const terminals: string[] = [];
    const stopObserving = adapter.observeTerminal((reason) => terminals.push(reason));
    const advisory = (): GenerateOptions => ({ ...request(), purpose: 'session-title' }) as GenerateOptions;
    // The runtime issues this advisory call while the real turn is pending;
    // in the real sandbox it landed BEFORE the first model call.
    await assert.rejects(promptly(collect(adapter.stream(advisory()))), rejected('AEVAL_AUXILIARY_REFUSED'));
    const info = await adapter.info();
    assert.equal(info.stopReason, undefined, 'a refused advisory call must not close the lease');
    assert.equal(info.usedSteps, 0, 'the refusal happens before dispatch');
    // A second advisory call is refused again instead of finding a dead lease.
    await assert.rejects(promptly(collect(adapter.stream(advisory()))), rejected('AEVAL_AUXILIARY_REFUSED'));
    // The real call still runs, dispatches exactly once, and the refusal is
    // never reported as a trusted terminal failure.
    assert.ok((await promptly(collect(adapter.stream(request())))).length > 0);
    assert.equal(upstream.requests.length, 1);
    assert.deepEqual(terminals, []);
    assert.deepEqual(rejections, [
      { code: 'AEVAL_AUXILIARY_REFUSED', purpose: 'session-title' },
      { code: 'AEVAL_AUXILIARY_REFUSED', purpose: 'session-title' },
    ]);
    stopObserving();
  } finally { await broker.close(); }
});

test('resolveAuxiliaryPolicy: explicit per-purpose entries win over the blanket flag (D47)', () => {
  assert.deepEqual(resolveAuxiliaryPolicy(undefined, undefined), { compaction: 'refuse', 'session-title': 'refuse' });
  assert.deepEqual(resolveAuxiliaryPolicy(undefined, true), { compaction: 'refuse', 'session-title': 'refuse' });
  assert.deepEqual(resolveAuxiliaryPolicy(undefined, false), { compaction: 'allow', 'session-title': 'allow' });
  assert.deepEqual(resolveAuxiliaryPolicy({ compaction: 'allow' }, true), { compaction: 'allow', 'session-title': 'refuse' });
  assert.deepEqual(resolveAuxiliaryPolicy({ 'session-title': 'allow' }, true), { compaction: 'refuse', 'session-title': 'allow' });
});

test('an allowed compaction call dispatches, is metered, and is ledgered with its usage (D47)', async () => {
  const upstream = new OfflineAdapter();
  const broker = await startHostBroker(policy(upstream, { auxiliaryPolicy: { compaction: 'allow' } }));
  try {
    const dispatches: GatewayDispatchRecord[] = [];
    const adapter = client(broker, { auxiliaryPolicy: { compaction: 'allow' } }, undefined, (record) => dispatches.push(record));
    const chunks = await promptly(collect(adapter.stream({ ...request(), purpose: 'compaction' } as GenerateOptions)));
    assert.ok(chunks.length > 0, 'an allowed auxiliary call completes like any dispatch');
    assert.equal(upstream.requests.length, 1);
    const info = await adapter.info();
    assert.equal(info.auxiliaryPolicy['compaction'], 'allow');
    assert.equal(info.auxiliaryPolicy['session-title'], 'refuse');
    assert.equal(info.stopReason, undefined, 'an allowed auxiliary call must not close the lease');
    assert.equal(info.usedSteps, 1, 'an allowed auxiliary call consumes a step like any dispatch');
    assert.deepEqual(dispatches, [{ code: 'AEVAL_AUXILIARY_DISPATCHED', purpose: 'compaction', usage }]);
    // The same lease still refuses the other purpose before dispatch.
    await assert.rejects(
      promptly(collect(adapter.stream({ ...request(), purpose: 'session-title' } as GenerateOptions))),
      rejected('AEVAL_AUXILIARY_REFUSED'),
    );
    assert.equal(upstream.requests.length, 1, 'a refused advisory call never reaches the upstream');
    assert.equal((await adapter.info()).usedSteps, 1, 'the refusal happens before dispatch');
  } finally { await broker.close(); }
});

test('a control config that disagrees with the served auxiliary policy fails closed at /info (D47)', async () => {
  const upstream = new OfflineAdapter();
  const broker = await startHostBroker(policy(upstream, { auxiliaryPolicy: { compaction: 'allow' } }));
  try {
    // The control config still refuses everything while the lease allows
    // compaction: the identity chain is broken, so installation must fail.
    const adapter = client(broker);
    await assert.rejects(adapter.info(), rejected('AEVAL_LEASE_MISMATCH'));
  } finally { await broker.close(); }
});

test('ordinary model calls never produce dispatch ledger entries (D47)', async () => {
  const upstream = new OfflineAdapter();
  const broker = await startHostBroker(policy(upstream));
  try {
    const dispatches: GatewayDispatchRecord[] = [];
    const adapter = client(broker, {}, undefined, (record) => dispatches.push(record));
    await promptly(collect(adapter.stream(request())));
    assert.equal(upstream.requests.length, 1);
    assert.deepEqual(dispatches, [], 'only purpose-tagged calls belong in the dispatch ledger');
  } finally { await broker.close(); }
});
