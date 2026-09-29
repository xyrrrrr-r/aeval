import { createHash } from 'node:crypto';
import z from '@deepseek-ai/schemastery';
import type { AuxiliaryDecision, AuxiliaryPurpose } from './gateway_lease.js';

export interface RunBinding {
  readonly run_id: string;
  readonly job_config_hash: string;
  readonly config_file_sha256: string;
  readonly runtime_lock_digest: string;
}

export interface ToolFaceConfig {
  readonly allow?: readonly string[];
  readonly deny?: readonly string[];
}

export interface LineageConfig {
  readonly parentSessionId?: string;
  readonly parentTrialId?: string;
  readonly forkStep?: number;
}

export interface EvalControlConfig {
  readonly run: RunBinding;
  readonly trialId: string;
  readonly sessionId: string;
  readonly sessionRoot: string;
  readonly ownerFinalize?: boolean;
  readonly configDigest: string;
  readonly provider: string;
  readonly model: string;
  readonly reasoningEffort?: string;
  readonly maxSteps?: number;
  readonly maxTokens?: number;
  readonly tools?: ToolFaceConfig;
  readonly lineage?: LineageConfig;
  readonly bundlePath: string;
  readonly gatewayUrl: string;
  readonly jobTokenFile: string;
  readonly refuseAuxiliaryCalls: boolean;
  /**
   * Per-purpose decisions for advisory model calls, as authored (D47).
   * Explicit entries win over ``refuseAuxiliaryCalls``; missing entries take
   * that blanket flag (default refuse). Kept as-authored so the config
   * digest matches the harness-composed config field for field; the resolved
   * complete map is computed at the comparison sites via
   * ``resolveAuxiliaryPolicy``.
   */
  readonly auxiliaryPolicy?: Readonly<Partial<Record<AuxiliaryPurpose, AuxiliaryDecision>>>;
}

export const EvalControlConfigFields = Object.freeze({
  run: z.object({
    run_id: z.string().required(),
    job_config_hash: z.string().required(),
    config_file_sha256: z.string().required(),
    runtime_lock_digest: z.string().required(),
  }).required(),
  trialId: z.string().required(),
  sessionId: z.string().required(),
  sessionRoot: z.string().required(),
  // Opt-in: the harness process performs the owner-side finalize after a
  // completed turn (one-shot sandboxed runs). See apply() for the caveat.
  ownerFinalize: z.boolean(),
  configDigest: z.string().required(),
  provider: z.string().required(),
  model: z.string().required(),
  reasoningEffort: z.string(),
  maxSteps: z.number().step(1).min(1).max(Number.MAX_SAFE_INTEGER),
  maxTokens: z.number().step(1).min(1).max(Number.MAX_SAFE_INTEGER),
  tools: z.object({ allow: z.array(z.string()), deny: z.array(z.string()) }),
  lineage: z.object({
    parentSessionId: z.string(),
    parentTrialId: z.string(),
    forkStep: z.number().step(1).min(0).max(Number.MAX_SAFE_INTEGER),
  }),
  bundlePath: z.string(),
  gatewayUrl: z.string().required(),
  jobTokenFile: z.string().required(),
  refuseAuxiliaryCalls: z.boolean(),
  // Values are validated to 'refuse' | 'allow' during resolution; the
  // schema only declares the shape (schemastery has no enum literal).
  auxiliaryPolicy: z.object({
    compaction: z.string(),
    'session-title': z.string(),
  }),
});

export class EvalControlConfigError extends Error {
  override readonly name = 'EvalControlConfigError';
}

function fail(field: string, problem: string): never {
  throw new EvalControlConfigError(`dsh-eval-control: config ${field} ${problem}`);
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

export function validateIdentifier(value: unknown, field: string): string {
  const result = nonEmptyString(value, field);
  if (/\s/u.test(result)) fail(field, 'must not contain whitespace');
  return result;
}

export function validateSha256Digest(value: unknown, field: string): string {
  if (typeof value !== 'string' || value.length !== 64 || !/^[0-9a-f]{64}$/u.test(value)) {
    fail(field, 'must be a lowercase hex sha256');
  }
  return value;
}

export function validateRunBinding(raw: unknown): RunBinding {
  const keys = ['run_id', 'job_config_hash', 'config_file_sha256', 'runtime_lock_digest'] as const;
  const input = record(raw, 'run', keys);
  for (const key of keys) {
    if (!Object.hasOwn(input, key)) fail(`run.${key}`, 'is required');
  }
  return Object.freeze({
    run_id: validateIdentifier(input['run_id'], 'run.run_id'),
    job_config_hash: validateSha256Digest(input['job_config_hash'], 'run.job_config_hash'),
    config_file_sha256: validateSha256Digest(input['config_file_sha256'], 'run.config_file_sha256'),
    runtime_lock_digest: validateSha256Digest(input['runtime_lock_digest'], 'run.runtime_lock_digest'),
  });
}

function optionalIdentifier(value: unknown, field: string): string | undefined {
  return value === undefined ? undefined : validateIdentifier(value, field);
}

export function validateSessionRoot(value: string): string {
  const path = nonEmptyString(value, 'sessionRoot').replaceAll('\\', '/');
  if (path.startsWith('/') || path.includes(':')) fail('sessionRoot', 'must be relative without a drive or stream');
  const parts = path.split('/');
  for (const part of parts) {
    if (part === '..') fail('sessionRoot', 'must not escape its descriptor directory');
    if (part === '.' || part === '') continue;
    if (/[.\s]$/u.test(part) || /^[\s]/u.test(part) || /[<>"|?*]/u.test(part)
      || /^(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)/iu.test(part)) {
      fail('sessionRoot', 'contains an unsafe Windows path component');
    }
  }
  return parts.filter((part) => part !== '' && part !== '.').join('/') || '.';
}

function filePath(value: unknown, field: string): string {
  const path = nonEmptyString(value, field);
  const withoutDrive = path.replace(/^[a-z]:[\\/]/iu, '');
  if (/[<>"|?*:]/u.test(withoutDrive) || /[\\/]$/u.test(path)) {
    fail(field, 'must be a file path, not a URL or directory');
  }
  return path;
}

function safeInteger(value: unknown, field: string, minimum: number): number | undefined {
  if (value === undefined) return undefined;
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < minimum) {
    fail(field, `must be a ${minimum === 0 ? 'non-negative' : 'positive'} safe integer`);
  }
  return value;
}

function toolNames(value: unknown, field: string): readonly string[] | undefined {
  if (value === undefined) return undefined;
  if (!Array.isArray(value)) fail(field, 'must be an array of tool names');
  const names: string[] = [];
  const seen = new Set<string>();
  for (const item of value) {
    const name = validateIdentifier(item, field);
    if (!/^[A-Za-z0-9_-]{1,64}$/u.test(name)) fail(field, 'contains an invalid tool name');
    if (seen.has(name)) fail(field, 'must not contain duplicate tool names');
    seen.add(name);
    names.push(name);
  }
  return Object.freeze(names);
}

function gateway(value: unknown): string {
  const raw = nonEmptyString(value, 'gatewayUrl');
  if (!/^https?:\/\//iu.test(raw) || /[\s\\?#]/u.test(raw)
    || /%[01][0-9a-f]|%7f/iu.test(raw)) {
    fail('gatewayUrl', 'must be an HTTP(S) URL without whitespace, query, or fragment');
  }
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    fail('gatewayUrl', 'must be a valid HTTP(S) URL');
  }
  const authority = raw.slice(raw.indexOf('://') + 3).split('/')[0]!;
  if (authority.includes('@') || url.username || url.password || !url.hostname) {
    fail('gatewayUrl', 'must not contain userinfo and must have a hostname');
  }
  const loopback = url.hostname === 'localhost' || url.hostname === '[::1]'
    || /^127(?:\.\d{1,3}){3}$/u.test(url.hostname);
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback)) {
    fail('gatewayUrl', 'requires HTTPS except for loopback HTTP');
  }
  return raw;
}

export function resolveEvalControlConfig(raw: unknown): EvalControlConfig {
  const input = record(raw, '', Object.keys(EvalControlConfigFields));
  const run = validateRunBinding(input['run']);
  const trialId = validateIdentifier(input['trialId'], 'trialId');
  const sessionId = validateIdentifier(input['sessionId'], 'sessionId');
  const sessionRoot = validateSessionRoot(input['sessionRoot'] as string);
  // Structural validation only: the owner must compute and bind this digest.
  const configDigest = validateSha256Digest(input['configDigest'], 'configDigest');
  const provider = validateIdentifier(input['provider'], 'provider');
  const model = validateIdentifier(input['model'], 'model');
  const reasoningEffort = optionalIdentifier(input['reasoningEffort'], 'reasoningEffort');
  const maxSteps = safeInteger(input['maxSteps'], 'maxSteps', 1);
  const maxTokens = safeInteger(input['maxTokens'], 'maxTokens', 1);

  let tools: ToolFaceConfig | undefined;
  if (input['tools'] !== undefined) {
    const t = record(input['tools'], 'tools', ['allow', 'deny']);
    const allow = toolNames(t['allow'], 'tools.allow');
    const deny = toolNames(t['deny'], 'tools.deny');
    tools = Object.freeze({
      ...(allow !== undefined ? { allow } : {}),
      ...(deny !== undefined ? { deny } : {}),
    });
  }

  let lineage: LineageConfig | undefined;
  if (input['lineage'] !== undefined) {
    const l = record(input['lineage'], 'lineage', ['parentSessionId', 'parentTrialId', 'forkStep']);
    const parentSessionId = optionalIdentifier(l['parentSessionId'], 'lineage.parentSessionId');
    const parentTrialId = optionalIdentifier(l['parentTrialId'], 'lineage.parentTrialId');
    const forkStep = safeInteger(l['forkStep'], 'lineage.forkStep', 0);
    if (parentSessionId === sessionId) fail('lineage.parentSessionId', 'must differ from sessionId');
    lineage = Object.freeze({
      ...(parentSessionId !== undefined ? { parentSessionId } : {}),
      ...(parentTrialId !== undefined ? { parentTrialId } : {}),
      ...(forkStep !== undefined ? { forkStep } : {}),
    });
  }

  const bundlePath = input['bundlePath'] === undefined
    ? 'bundle_descriptor.json' : filePath(input['bundlePath'], 'bundlePath');
  const gatewayUrl = gateway(input['gatewayUrl']);
  const jobTokenFile = filePath(input['jobTokenFile'], 'jobTokenFile');
  const refuseAuxiliaryCalls = input['refuseAuxiliaryCalls'] === undefined ? true : input['refuseAuxiliaryCalls'];
  // opt-in owner-side finalize (one-shot sandboxed deployments only)
  const ownerFinalize = input['ownerFinalize'] === true;
  if (typeof refuseAuxiliaryCalls !== 'boolean') fail('refuseAuxiliaryCalls', 'must be a boolean');
  // D47: per-purpose decisions, as authored. Validation here is the last
  // line before the digest is pinned: only the two known purposes, only
  // explicit decisions. The complete map is resolved where it is compared
  // (see resolveAuxiliaryPolicy) so this config hashes exactly what the
  // harness composed.
  let auxiliaryPolicy: EvalControlConfig['auxiliaryPolicy'];
  if (input['auxiliaryPolicy'] !== undefined) {
    const raw = input['auxiliaryPolicy'];
    if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) fail('auxiliaryPolicy', 'must be an object');
    const authored = raw as Record<string, unknown>;
    for (const key of Object.keys(authored)) {
      if (key !== 'compaction' && key !== 'session-title') fail('auxiliaryPolicy', `has an unknown purpose: ${key}`);
      const decision = authored[key];
      if (decision !== 'refuse' && decision !== 'allow') fail('auxiliaryPolicy', `${key} must be 'refuse' or 'allow'`);
    }
    auxiliaryPolicy = Object.freeze({ ...authored } as Partial<Record<AuxiliaryPurpose, AuxiliaryDecision>>);
  }

  return Object.freeze({
    run, trialId, sessionId, sessionRoot, configDigest, provider, model,
    ...(reasoningEffort !== undefined ? { reasoningEffort } : {}),
    ...(maxSteps !== undefined ? { maxSteps } : {}),
    ...(maxTokens !== undefined ? { maxTokens } : {}),
    ...(tools !== undefined ? { tools } : {}),
    ...(lineage !== undefined ? { lineage } : {}),
    bundlePath, gatewayUrl, jobTokenFile, refuseAuxiliaryCalls,
    ...(auxiliaryPolicy !== undefined ? { auxiliaryPolicy } : {}),
    // included only when set, so the digest of an ordinary deployment is
    // unchanged by the flag's existence
    ...(ownerFinalize ? { ownerFinalize } : {}),
  });
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value !== null && typeof value === 'object') {
    const object = value as Record<string, unknown>;
    return `{${Object.keys(object).sort().filter((key) => object[key] !== undefined)
      .map((key) => `${JSON.stringify(key)}:${canonicalJson(object[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

/** Hash the complete resolved config except configDigest; this does not establish owner trust. */
export function digestEvalControlConfig(config: EvalControlConfig): string {
  const { configDigest: _configDigest, ...resolved } = config;
  return createHash('sha256').update(canonicalJson(resolved), 'utf8').digest('hex');
}

export interface ConfigStandardSchema {
  readonly '~standard': {
    readonly version: 1;
    readonly vendor: string;
    readonly types?: { readonly input: unknown; readonly output: EvalControlConfig };
    readonly validate: (value: unknown) =>
      | { readonly value: EvalControlConfig; readonly issues?: undefined }
      | { readonly issues: readonly { readonly message: string }[] };
  };
}

export const EvalControlConfigSchema: ConfigStandardSchema = Object.freeze({
  '~standard': Object.freeze({
    version: 1 as const,
    vendor: 'dsh-eval-control',
    validate(value: unknown) {
      try {
        return { value: resolveEvalControlConfig(value) };
      } catch (error) {
        return { issues: [{ message: error instanceof EvalControlConfigError
          ? error.message : 'dsh-eval-control: invalid config' }] };
      }
    },
  }),
});
