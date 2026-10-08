import { createHash } from 'node:crypto';
import z from '@deepseek-ai/schemastery';
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
    name = 'EvalControlConfigError';
}
function fail(field, problem) {
    throw new EvalControlConfigError(`aeval-control: config ${field} ${problem}`);
}
function record(value, field, keys) {
    if (typeof value !== 'object' || value === null || Array.isArray(value)
        || (Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null)) {
        fail(field, 'must be a plain object');
    }
    for (const key of Reflect.ownKeys(value)) {
        if (typeof key !== 'string' || !keys.includes(key))
            fail(field, 'contains an unknown key');
    }
    return value;
}
function nonEmptyString(value, field) {
    if (typeof value !== 'string' || value.length === 0 || value.trim() !== value
        || /[\u0000-\u001f\u007f-\u009f\u2028\u2029]/u.test(value)) {
        fail(field, 'must be a non-empty string without control characters or surrounding whitespace');
    }
    return value;
}
export function validateIdentifier(value, field) {
    const result = nonEmptyString(value, field);
    if (/\s/u.test(result))
        fail(field, 'must not contain whitespace');
    return result;
}
export function validateSha256Digest(value, field) {
    if (typeof value !== 'string' || value.length !== 64 || !/^[0-9a-f]{64}$/u.test(value)) {
        fail(field, 'must be a lowercase hex sha256');
    }
    return value;
}
export function validateRunBinding(raw) {
    const keys = ['run_id', 'job_config_hash', 'config_file_sha256', 'runtime_lock_digest'];
    const input = record(raw, 'run', keys);
    for (const key of keys) {
        if (!Object.hasOwn(input, key))
            fail(`run.${key}`, 'is required');
    }
    return Object.freeze({
        run_id: validateIdentifier(input['run_id'], 'run.run_id'),
        job_config_hash: validateSha256Digest(input['job_config_hash'], 'run.job_config_hash'),
        config_file_sha256: validateSha256Digest(input['config_file_sha256'], 'run.config_file_sha256'),
        runtime_lock_digest: validateSha256Digest(input['runtime_lock_digest'], 'run.runtime_lock_digest'),
    });
}
function optionalIdentifier(value, field) {
    return value === undefined ? undefined : validateIdentifier(value, field);
}
export function validateSessionRoot(value) {
    const path = nonEmptyString(value, 'sessionRoot').replaceAll('\\', '/');
    if (path.startsWith('/') || path.includes(':'))
        fail('sessionRoot', 'must be relative without a drive or stream');
    const parts = path.split('/');
    for (const part of parts) {
        if (part === '..')
            fail('sessionRoot', 'must not escape its descriptor directory');
        if (part === '.' || part === '')
            continue;
        if (/[.\s]$/u.test(part) || /^[\s]/u.test(part) || /[<>"|?*]/u.test(part)
            || /^(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)/iu.test(part)) {
            fail('sessionRoot', 'contains an unsafe Windows path component');
        }
    }
    return parts.filter((part) => part !== '' && part !== '.').join('/') || '.';
}
function filePath(value, field) {
    const path = nonEmptyString(value, field);
    const withoutDrive = path.replace(/^[a-z]:[\\/]/iu, '');
    if (/[<>"|?*:]/u.test(withoutDrive) || /[\\/]$/u.test(path)) {
        fail(field, 'must be a file path, not a URL or directory');
    }
    return path;
}
function safeInteger(value, field, minimum) {
    if (value === undefined)
        return undefined;
    if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < minimum) {
        fail(field, `must be a ${minimum === 0 ? 'non-negative' : 'positive'} safe integer`);
    }
    return value;
}
function toolNames(value, field) {
    if (value === undefined)
        return undefined;
    if (!Array.isArray(value))
        fail(field, 'must be an array of tool names');
    const names = [];
    const seen = new Set();
    for (const item of value) {
        const name = validateIdentifier(item, field);
        if (!/^[A-Za-z0-9_-]{1,64}$/u.test(name))
            fail(field, 'contains an invalid tool name');
        if (seen.has(name))
            fail(field, 'must not contain duplicate tool names');
        seen.add(name);
        names.push(name);
    }
    return Object.freeze(names);
}
function gateway(value) {
    const raw = nonEmptyString(value, 'gatewayUrl');
    if (!/^https?:\/\//iu.test(raw) || /[\s\\?#]/u.test(raw)
        || /%[01][0-9a-f]|%7f/iu.test(raw)) {
        fail('gatewayUrl', 'must be an HTTP(S) URL without whitespace, query, or fragment');
    }
    let url;
    try {
        url = new URL(raw);
    }
    catch {
        fail('gatewayUrl', 'must be a valid HTTP(S) URL');
    }
    const authority = raw.slice(raw.indexOf('://') + 3).split('/')[0];
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
export function resolveEvalControlConfig(raw) {
    const input = record(raw, '', Object.keys(EvalControlConfigFields));
    const run = validateRunBinding(input['run']);
    const trialId = validateIdentifier(input['trialId'], 'trialId');
    const sessionId = validateIdentifier(input['sessionId'], 'sessionId');
    const sessionRoot = validateSessionRoot(input['sessionRoot']);
    // Structural validation only: the owner must compute and bind this digest.
    const configDigest = validateSha256Digest(input['configDigest'], 'configDigest');
    const provider = validateIdentifier(input['provider'], 'provider');
    const model = validateIdentifier(input['model'], 'model');
    const reasoningEffort = optionalIdentifier(input['reasoningEffort'], 'reasoningEffort');
    const maxSteps = safeInteger(input['maxSteps'], 'maxSteps', 1);
    const maxTokens = safeInteger(input['maxTokens'], 'maxTokens', 1);
    let tools;
    if (input['tools'] !== undefined) {
        const t = record(input['tools'], 'tools', ['allow', 'deny']);
        const allow = toolNames(t['allow'], 'tools.allow');
        const deny = toolNames(t['deny'], 'tools.deny');
        tools = Object.freeze({
            ...(allow !== undefined ? { allow } : {}),
            ...(deny !== undefined ? { deny } : {}),
        });
    }
    let lineage;
    if (input['lineage'] !== undefined) {
        const l = record(input['lineage'], 'lineage', ['parentSessionId', 'parentTrialId', 'forkStep']);
        const parentSessionId = optionalIdentifier(l['parentSessionId'], 'lineage.parentSessionId');
        const parentTrialId = optionalIdentifier(l['parentTrialId'], 'lineage.parentTrialId');
        const forkStep = safeInteger(l['forkStep'], 'lineage.forkStep', 0);
        if (parentSessionId === sessionId)
            fail('lineage.parentSessionId', 'must differ from sessionId');
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
    if (typeof refuseAuxiliaryCalls !== 'boolean')
        fail('refuseAuxiliaryCalls', 'must be a boolean');
    // Per-purpose decisions, as authored. Validation here is the last
    // line before the digest is pinned: only the two known purposes, only
    // explicit decisions. The complete map is resolved where it is compared
    // (see resolveAuxiliaryPolicy) so this config hashes exactly what the
    // harness composed.
    let auxiliaryPolicy;
    if (input['auxiliaryPolicy'] !== undefined) {
        const raw = input['auxiliaryPolicy'];
        if (raw === null || typeof raw !== 'object' || Array.isArray(raw))
            fail('auxiliaryPolicy', 'must be an object');
        const authored = raw;
        for (const key of Object.keys(authored)) {
            if (key !== 'compaction' && key !== 'session-title')
                fail('auxiliaryPolicy', `has an unknown purpose: ${key}`);
            const decision = authored[key];
            if (decision !== 'refuse' && decision !== 'allow')
                fail('auxiliaryPolicy', `${key} must be 'refuse' or 'allow'`);
        }
        auxiliaryPolicy = Object.freeze({ ...authored });
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
function canonicalJson(value) {
    if (Array.isArray(value))
        return `[${value.map(canonicalJson).join(',')}]`;
    if (value !== null && typeof value === 'object') {
        const object = value;
        return `{${Object.keys(object).sort().filter((key) => object[key] !== undefined)
            .map((key) => `${JSON.stringify(key)}:${canonicalJson(object[key])}`).join(',')}}`;
    }
    return JSON.stringify(value);
}
/** Hash the complete resolved config except configDigest; this does not establish owner trust. */
export function digestEvalControlConfig(config) {
    const { configDigest: _configDigest, ...resolved } = config;
    return createHash('sha256').update(canonicalJson(resolved), 'utf8').digest('hex');
}
export const EvalControlConfigSchema = Object.freeze({
    '~standard': Object.freeze({
        version: 1,
        vendor: 'aeval-control',
        validate(value) {
            try {
                return { value: resolveEvalControlConfig(value) };
            }
            catch (error) {
                return { issues: [{ message: error instanceof EvalControlConfigError
                                ? error.message : 'aeval-control: invalid config' }] };
            }
        },
    }),
});
