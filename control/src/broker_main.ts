#!/usr/bin/env node
import { readFileSync, realpathSync } from 'node:fs';
import { isIP } from 'node:net';
import { pathToFileURL } from 'node:url';
import { LlmError } from '@deepseek-ai/dsh-llm';
import { EvalControlConfigError, validateIdentifier, validateRunBinding, validateSha256Digest, type RunBinding } from './config.js';
import { GATEWAY_PROTOCOL } from './gateway_lease.js';
import type { AuxiliaryDecision, AuxiliaryPurpose, LeaseIdentity, LeaseLimits } from './gateway_lease.js';
import { cleanupJobToken, isLoopbackHost, startHostBroker, writeJobToken } from './host_broker.js';
import type { HostBroker } from './host_broker.js';
import { createProviderCountBound } from './token_bound.js';
import { createUpstreamAdapter } from './upstream.js';

/**
 * Broker host entry for aeval: a trusted-owner subprocess that starts the
 * model broker against a real provider upstream, publishes one readiness line,
 * and owns the job token's lifetime (TTL auto-stop and cleanup on shutdown).
 *
 * Exit codes: 0 normal shutdown (signal or token TTL), 1 runtime failure,
 * 2 configuration or credential failure, 3 hard token budget without a
 * trusted counting source.
 */

const TIMER_RANGE_MS = 2_147_483_647;

export interface BrokerMainConfig {
  readonly run: RunBinding;
  readonly trialId: string;
  readonly sessionId: string;
  readonly configDigest: string;
  readonly identity: LeaseIdentity;
  readonly limits: LeaseLimits;
  readonly maxOutputTokens: number;
  readonly timeoutMs?: number;
  readonly tokenTtlMs?: number;
  readonly auxiliaryPolicy?: Readonly<Partial<Record<AuxiliaryPurpose, AuxiliaryDecision>>>;
  readonly listen: { readonly host: string; readonly port?: number; readonly tls?: { readonly key: string; readonly cert: string } };
  readonly tokenOut: string;
  readonly upstream: {
    readonly provider: string;
    readonly baseUrl: string;
    readonly apiKeyEnv: string;
    readonly model: string;
    readonly timeoutMs?: number;
    readonly reasoningEfforts?: readonly string[];
  };
  readonly tokenCount?: { readonly endpoint?: string; readonly margin?: number; readonly timeoutMs?: number };
}

class BrokerConfigError extends Error {
  override readonly name = 'BrokerConfigError';
  constructor(message: string, readonly exitCode: number = 2) {
    super(message);
  }
}

function fail(field: string, problem: string): never {
  throw new BrokerConfigError(`aeval-model-broker: config ${field} ${problem}`);
}

function record(value: unknown, field: string, keys: readonly string[]): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)
    || (Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null)) {
    fail(field, 'must be a plain object');
  }
  for (const key of Reflect.ownKeys(value)) {
    if (typeof key !== 'string' || !keys.includes(key)) fail(field, 'contains an unknown key');
  }
  return value as Record<string, unknown>;
}

function nonEmptyString(value: unknown, field: string): string {
  if (typeof value !== 'string' || value.length === 0 || value.trim() !== value
    || /[\u0000-\u001f\u007f-\u009f\u2028\u2029]/u.test(value)) {
    fail(field, 'must be a non-empty string without control characters or surrounding whitespace');
  }
  return value;
}

function positiveInt(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1 || value > TIMER_RANGE_MS) {
    fail(field, 'must be a positive safe integer within timer range');
  }
  return value;
}

function optionalPositiveInt(value: unknown, field: string): number | undefined {
  return value === undefined ? undefined : positiveInt(value, field);
}

function nonNegativeInt(value: unknown, field: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
    fail(field, 'must be a non-negative safe integer');
  }
  return value;
}

function filePath(value: unknown, field: string): string {
  const path = nonEmptyString(value, field);
  const withoutDrive = path.replace(/^[a-z]:[\\/]/iu, '');
  if (/[<>"|?*:]/u.test(withoutDrive) || /[\\/]$/u.test(path)) fail(field, 'must be a file path, not a URL or directory');
  return path;
}

function effortList(value: unknown, field: string): readonly string[] {
  if (!Array.isArray(value)) fail(field, 'must be an array of reasoning effort ids');
  const seen = new Set<string>();
  for (const id of value) {
    const effort = validateIdentifier(id, field);
    if (seen.has(effort)) fail(field, 'must not contain duplicate efforts');
    seen.add(effort);
  }
  return Object.freeze(value as readonly string[]);
}

function parseListen(raw: unknown): BrokerMainConfig['listen'] {
  const input = record(raw, 'listen', ['host', 'port', 'tls']);
  const host = nonEmptyString(input['host'], 'listen.host');
  if (!isIP(host) && !/^[a-zA-Z0-9.-]+$/u.test(host)) fail('listen.host', 'is not a valid host');
  const port = input['port'] === undefined ? undefined
    : (typeof input['port'] !== 'number' || !Number.isSafeInteger(input['port']) || input['port'] < 0 || input['port'] > 65535
      ? fail('listen.port', 'must be a port number between 0 and 65535') : input['port']);
  let tls: { readonly key: string; readonly cert: string } | undefined;
  if (input['tls'] !== undefined) {
    const material = record(input['tls'], 'listen.tls', ['key', 'cert']);
    // File paths, not PEM text: node's TLS server parses what it is given
    // as PEM, so a path needs reading first and inline PEM is rejected by
    // the control-character check. A nonloopback listener could otherwise
    // never start (found during environment verification).
    tls = Object.freeze({ key: filePath(material['key'], 'listen.tls.key'), cert: filePath(material['cert'], 'listen.tls.cert') });
  }
  if (!isLoopbackHost(host) && tls === undefined) fail('listen', 'nonloopback listeners require TLS material');
  return Object.freeze({ host, ...(port !== undefined ? { port } : {}), ...(tls !== undefined ? { tls } : {}) });
}

export function parseBrokerMainConfig(raw: unknown): BrokerMainConfig {
  const input = record(raw, '', ['run', 'trialId', 'sessionId', 'configDigest', 'identity', 'limits', 'maxOutputTokens', 'timeoutMs', 'tokenTtlMs', 'listen', 'tokenOut', 'upstream', 'tokenCount', 'auxiliaryPolicy']);
  const identityRaw = record(input['identity'], 'identity', ['provider', 'model', 'reasoningEffort']);
  const reasoningEffort = identityRaw['reasoningEffort'] === undefined ? undefined : validateIdentifier(identityRaw['reasoningEffort'], 'identity.reasoningEffort');
  const identity: LeaseIdentity = Object.freeze({
    provider: validateIdentifier(identityRaw['provider'], 'identity.provider'),
    model: validateIdentifier(identityRaw['model'], 'identity.model'),
    ...(reasoningEffort !== undefined ? { reasoningEffort } : {}),
  });
  const limitsRaw = record(input['limits'], 'limits', ['maxSteps', 'maxTokens']);
  const limits: LeaseLimits = Object.freeze({
    ...(limitsRaw['maxSteps'] !== undefined ? { maxSteps: positiveInt(limitsRaw['maxSteps'], 'limits.maxSteps') } : {}),
    ...(limitsRaw['maxTokens'] !== undefined ? { maxTokens: positiveInt(limitsRaw['maxTokens'], 'limits.maxTokens') } : {}),
  });
  const upstreamRaw = record(input['upstream'], 'upstream', ['provider', 'baseUrl', 'apiKeyEnv', 'model', 'timeoutMs', 'reasoningEfforts']);
  const reasoningEfforts = upstreamRaw['reasoningEfforts'] === undefined ? undefined : effortList(upstreamRaw['reasoningEfforts'], 'upstream.reasoningEfforts');
  const upstream = Object.freeze({
    provider: validateIdentifier(upstreamRaw['provider'], 'upstream.provider'),
    baseUrl: nonEmptyString(upstreamRaw['baseUrl'], 'upstream.baseUrl'),
    apiKeyEnv: validateIdentifier(upstreamRaw['apiKeyEnv'], 'upstream.apiKeyEnv'),
    model: validateIdentifier(upstreamRaw['model'], 'upstream.model'),
    ...(upstreamRaw['timeoutMs'] !== undefined ? { timeoutMs: positiveInt(upstreamRaw['timeoutMs'], 'upstream.timeoutMs') } : {}),
    ...(reasoningEfforts !== undefined ? { reasoningEfforts } : {}),
  });
  // The lease pins identity.model on every dispatch and the meter counts that
  // same wire model, so the upstream route must be the pinned identity itself.
  if (upstream.provider !== identity.provider || upstream.model !== identity.model) {
    fail('upstream', 'provider and model must match the lease identity');
  }
  if (reasoningEffort !== undefined && !(upstream.reasoningEfforts ?? []).includes(reasoningEffort)) {
    fail('upstream.reasoningEfforts', `must declare the pinned reasoning effort ${reasoningEffort}`);
  }
  let tokenCount: BrokerMainConfig['tokenCount'];
  if (input['tokenCount'] !== undefined) {
    const raw = record(input['tokenCount'], 'tokenCount', ['endpoint', 'margin', 'timeoutMs']);
    tokenCount = Object.freeze({
      ...(raw['endpoint'] !== undefined ? { endpoint: nonEmptyString(raw['endpoint'], 'tokenCount.endpoint') } : {}),
      ...(raw['margin'] !== undefined ? { margin: nonNegativeInt(raw['margin'], 'tokenCount.margin') } : {}),
      ...(raw['timeoutMs'] !== undefined ? { timeoutMs: positiveInt(raw['timeoutMs'], 'tokenCount.timeoutMs') } : {}),
    });
  }
  // D47: per-purpose decisions for advisory model calls. Only the two known
  // purposes may be configured, and only with an explicit decision; the
  // resolved policy (against refuseAuxiliaryCalls, default refuse) is what
  // the lease serves and /info reports.
  let auxiliaryPolicy: BrokerMainConfig['auxiliaryPolicy'];
  if (input['auxiliaryPolicy'] !== undefined) {
    const raw = record(input['auxiliaryPolicy'], 'auxiliaryPolicy', ['compaction', 'session-title']);
    for (const purpose of ['compaction', 'session-title'] as const) {
      const decision = raw[purpose];
      if (decision === undefined) continue;
      if (decision !== 'refuse' && decision !== 'allow') fail('auxiliaryPolicy', `${purpose} must be 'refuse' or 'allow'`);
      auxiliaryPolicy = Object.freeze({ ...auxiliaryPolicy, [purpose]: decision });
    }
  }
  return Object.freeze({
    run: validateRunBinding(input['run']),
    trialId: validateIdentifier(input['trialId'], 'trialId'),
    sessionId: validateIdentifier(input['sessionId'], 'sessionId'),
    configDigest: validateSha256Digest(input['configDigest'], 'configDigest'),
    identity,
    limits,
    maxOutputTokens: positiveInt(input['maxOutputTokens'], 'maxOutputTokens'),
    ...(input['timeoutMs'] !== undefined ? { timeoutMs: positiveInt(input['timeoutMs'], 'timeoutMs') } : {}),
    ...(input['tokenTtlMs'] !== undefined ? { tokenTtlMs: positiveInt(input['tokenTtlMs'], 'tokenTtlMs') } : {}),
    listen: parseListen(input['listen']),
    tokenOut: filePath(input['tokenOut'], 'tokenOut'),
    upstream,
    ...(tokenCount !== undefined ? { tokenCount } : {}),
    ...(auxiliaryPolicy !== undefined ? { auxiliaryPolicy } : {}),
  });
}

function loadConfig(argv: readonly string[]): BrokerMainConfig {
  const [path, ...extra] = argv;
  if (path === undefined || extra.length > 0) {
    throw new BrokerConfigError('aeval-model-broker: usage: aeval-model-broker <config.json>');
  }
  let text: string;
  try {
    text = readFileSync(path, 'utf8');
  } catch (error) {
    throw new BrokerConfigError(`aeval-model-broker: config file cannot be read: ${firstLine(error)}`);
  }
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    throw new BrokerConfigError('aeval-model-broker: config file is not valid JSON');
  }
  return parseBrokerMainConfig(raw);
}

function firstLine(error: unknown): string {
  const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
  const line = message.split(/\r?\n/u)[0] ?? '';
  return line === '' ? 'unknown failure' : line;
}

export async function main(argv: readonly string[] = process.argv.slice(2)): Promise<number> {
  let broker: HostBroker | undefined;
  let tokenPath: string | undefined;
  let controller: AbortController | undefined;
  let resolveShutdown: (cause: 'signal' | 'ttl') => void = () => {};
  const shutdown = new Promise<'signal' | 'ttl'>((resolve) => { resolveShutdown = resolve; });
  let signals = 0;
  const onSignal = () => {
    // A second signal during shutdown means the operator wants out now.
    if (++signals > 1) process.exit(1);
    controller?.abort();
    resolveShutdown('signal');
  };
  try {
    const config = loadConfig(argv);
    // Reading the key and building the adapter both fail closed here, before
    // any listener or file exists.
    const upstream = createUpstreamAdapter(config.upstream);
    if (config.limits.maxTokens !== undefined && config.tokenCount === undefined) {
      throw new BrokerConfigError('aeval-model-broker: limits.maxTokens requires a trusted tokenCount source', 3);
    }
    const meter = config.limits.maxTokens === undefined ? undefined : createProviderCountBound({
      baseUrl: config.upstream.baseUrl,
      apiKeyEnv: config.upstream.apiKeyEnv,
      ...config.tokenCount,
    });
    controller = new AbortController();
    process.on('SIGINT', onSignal);
    process.on('SIGTERM', onSignal);
    broker = await startHostBroker({
      run: config.run,
      trialId: config.trialId,
      sessionId: config.sessionId,
      configDigest: config.configDigest,
      identity: config.identity,
      limits: config.limits,
      maxOutputTokens: config.maxOutputTokens,
      upstream,
      ...(config.auxiliaryPolicy !== undefined ? { auxiliaryPolicy: config.auxiliaryPolicy } : {}),
      ...(meter !== undefined ? { inputTokenUpperBound: meter } : {}),
      signal: controller.signal,
      ...(config.timeoutMs !== undefined ? { timeoutMs: config.timeoutMs } : {}),
      ...(config.tokenTtlMs !== undefined ? { tokenTtlMs: config.tokenTtlMs } : {}),
      listen: config.listen,
    });
    if (signals === 0) {
      try {
        writeJobToken(config.tokenOut, broker.token);
      } catch (error) {
        // The token output path is configuration: refusing to overwrite an
        // existing file (wx semantics) is a startup refusal, not a runtime fault.
        throw new BrokerConfigError(`aeval-model-broker: token file cannot be written: ${firstLine(error)}`);
      }
      tokenPath = config.tokenOut;
      // Exactly one stdout line, ever: the trusted owner polls this for readiness.
      process.stdout.write(`${JSON.stringify({ ready: true, url: broker.url, tokenPath: config.tokenOut, protocol: GATEWAY_PROTOCOL })}\n`);
    }
    // The TTL timer is unref'd and starts after readiness: at expiry the lease
    // is stopped, the token file cleaned, and the process exits on its own.
    const ttlTimer = config.tokenTtlMs === undefined ? undefined : setTimeout(() => resolveShutdown('ttl'), config.tokenTtlMs);
    ttlTimer?.unref();
    const cause = await shutdown;
    clearTimeout(ttlTimer);
    await broker.close(cause === 'ttl' ? 'timeout_killed' : undefined);
    return 0;
  } catch (error) {
    // A shutdown signal that interrupted startup is a normal close, not a failure.
    if (signals > 0 && error instanceof Error && error.name === 'AbortError') return 0;
    process.stderr.write(`aeval-model-broker: ${firstLine(error)}\n`);
    if (error instanceof BrokerConfigError) return error.exitCode;
    if (error instanceof EvalControlConfigError) return 2;
    if (error instanceof LlmError && ['MISSING_CREDENTIAL', 'INVALID_CREDENTIAL', 'INVALID_CONFIG'].includes(error.code)) return 2;
    return 1;
  } finally {
    controller?.abort();
    if (broker !== undefined) await broker.close().catch(() => {});
    if (tokenPath !== undefined) {
      try {
        cleanupJobToken(tokenPath);
      } catch (error) {
        // Cleanup failure must not turn a clean shutdown into a failure exit.
        process.stderr.write(`aeval-model-broker: token cleanup failed: ${firstLine(error)}\n`);
      }
    }
  }
}

function isMain(): boolean {
  try {
    return process.argv[1] !== undefined && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href;
  } catch {
    return false;
  }
}

if (isMain()) process.exit(await main());
