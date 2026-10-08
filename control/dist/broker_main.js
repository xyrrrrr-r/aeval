#!/usr/bin/env node
import { readFileSync, realpathSync } from 'node:fs';
import { isIP } from 'node:net';
import { pathToFileURL } from 'node:url';
import { LlmError } from '@deepseek-ai/dsh-llm';
import { EvalControlConfigError, validateIdentifier, validateRunBinding, validateSha256Digest } from './config.js';
import { GATEWAY_PROTOCOL } from './gateway_lease.js';
import { cleanupJobToken, isLoopbackHost, startHostBroker, writeJobToken } from './host_broker.js';
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
class BrokerConfigError extends Error {
    exitCode;
    name = 'BrokerConfigError';
    constructor(message, exitCode = 2) {
        super(message);
        this.exitCode = exitCode;
    }
}
function fail(field, problem) {
    throw new BrokerConfigError(`aeval-model-broker: config ${field} ${problem}`);
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
function positiveInt(value, field) {
    if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 1 || value > TIMER_RANGE_MS) {
        fail(field, 'must be a positive safe integer within timer range');
    }
    return value;
}
function optionalPositiveInt(value, field) {
    return value === undefined ? undefined : positiveInt(value, field);
}
function nonNegativeInt(value, field) {
    if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
        fail(field, 'must be a non-negative safe integer');
    }
    return value;
}
function filePath(value, field) {
    const path = nonEmptyString(value, field);
    const withoutDrive = path.replace(/^[a-z]:[\\/]/iu, '');
    if (/[<>"|?*:]/u.test(withoutDrive) || /[\\/]$/u.test(path))
        fail(field, 'must be a file path, not a URL or directory');
    return path;
}
function effortList(value, field) {
    if (!Array.isArray(value))
        fail(field, 'must be an array of reasoning effort ids');
    const seen = new Set();
    for (const id of value) {
        const effort = validateIdentifier(id, field);
        if (seen.has(effort))
            fail(field, 'must not contain duplicate efforts');
        seen.add(effort);
    }
    return Object.freeze(value);
}
function parseListen(raw) {
    const input = record(raw, 'listen', ['host', 'port', 'tls']);
    const host = nonEmptyString(input['host'], 'listen.host');
    if (!isIP(host) && !/^[a-zA-Z0-9.-]+$/u.test(host))
        fail('listen.host', 'is not a valid host');
    const port = input['port'] === undefined ? undefined
        : (typeof input['port'] !== 'number' || !Number.isSafeInteger(input['port']) || input['port'] < 0 || input['port'] > 65535
            ? fail('listen.port', 'must be a port number between 0 and 65535') : input['port']);
    let tls;
    if (input['tls'] !== undefined) {
        const material = record(input['tls'], 'listen.tls', ['key', 'cert']);
        // File paths, not PEM text: node's TLS server parses what it is given
        // as PEM, so a path needs reading first and inline PEM is rejected by
        // the control-character check. A nonloopback listener could otherwise
        // never start (found during environment verification).
        tls = Object.freeze({ key: filePath(material['key'], 'listen.tls.key'), cert: filePath(material['cert'], 'listen.tls.cert') });
    }
    if (!isLoopbackHost(host) && tls === undefined)
        fail('listen', 'nonloopback listeners require TLS material');
    return Object.freeze({ host, ...(port !== undefined ? { port } : {}), ...(tls !== undefined ? { tls } : {}) });
}
export function parseBrokerMainConfig(raw) {
    const input = record(raw, '', ['run', 'trialId', 'sessionId', 'configDigest', 'identity', 'limits', 'maxOutputTokens', 'timeoutMs', 'tokenTtlMs', 'listen', 'tokenOut', 'upstream', 'tokenCount', 'auxiliaryPolicy']);
    const identityRaw = record(input['identity'], 'identity', ['provider', 'model', 'reasoningEffort']);
    const reasoningEffort = identityRaw['reasoningEffort'] === undefined ? undefined : validateIdentifier(identityRaw['reasoningEffort'], 'identity.reasoningEffort');
    const identity = Object.freeze({
        provider: validateIdentifier(identityRaw['provider'], 'identity.provider'),
        model: validateIdentifier(identityRaw['model'], 'identity.model'),
        ...(reasoningEffort !== undefined ? { reasoningEffort } : {}),
    });
    const limitsRaw = record(input['limits'], 'limits', ['maxSteps', 'maxTokens']);
    const limits = Object.freeze({
        ...(limitsRaw['maxSteps'] !== undefined ? { maxSteps: positiveInt(limitsRaw['maxSteps'], 'limits.maxSteps') } : {}),
        ...(limitsRaw['maxTokens'] !== undefined ? { maxTokens: positiveInt(limitsRaw['maxTokens'], 'limits.maxTokens') } : {}),
    });
    const upstreamRaw = record(input['upstream'], 'upstream', ['provider', 'baseUrl', 'apiKeyEnv', 'model', 'protocol', 'timeoutMs', 'reasoningEfforts', 'contextWindow']);
    // Optional and closed-vocabulary: absent = chat_completions, anything else
    // is a config error, never a guess.
    const protocol = upstreamRaw['protocol'] === undefined
        ? undefined
        : upstreamRaw['protocol'] === 'chat_completions' || upstreamRaw['protocol'] === 'responses'
            ? upstreamRaw['protocol']
            : fail('upstream.protocol', "must be 'chat_completions' or 'responses'");
    const reasoningEfforts = upstreamRaw['reasoningEfforts'] === undefined ? undefined : effortList(upstreamRaw['reasoningEfforts'], 'upstream.reasoningEfforts');
    const upstream = Object.freeze({
        provider: validateIdentifier(upstreamRaw['provider'], 'upstream.provider'),
        baseUrl: nonEmptyString(upstreamRaw['baseUrl'], 'upstream.baseUrl'),
        apiKeyEnv: validateIdentifier(upstreamRaw['apiKeyEnv'], 'upstream.apiKeyEnv'),
        model: validateIdentifier(upstreamRaw['model'], 'upstream.model'),
        ...(protocol !== undefined ? { protocol } : {}),
        ...(upstreamRaw['timeoutMs'] !== undefined ? { timeoutMs: positiveInt(upstreamRaw['timeoutMs'], 'upstream.timeoutMs') } : {}),
        ...(reasoningEfforts !== undefined ? { reasoningEfforts } : {}),
        ...(upstreamRaw['contextWindow'] !== undefined ? { contextWindow: positiveInt(upstreamRaw['contextWindow'], 'upstream.contextWindow') } : {}),
    });
    // The lease pins identity.model on every dispatch and the meter counts that
    // same wire model, so the upstream route must be the pinned identity itself.
    if (upstream.provider !== identity.provider || upstream.model !== identity.model) {
        fail('upstream', 'provider and model must match the lease identity');
    }
    if (reasoningEffort !== undefined && !(upstream.reasoningEfforts ?? []).includes(reasoningEffort)) {
        fail('upstream.reasoningEfforts', `must declare the pinned reasoning effort ${reasoningEffort}`);
    }
    let tokenCount;
    if (input['tokenCount'] !== undefined) {
        const raw = record(input['tokenCount'], 'tokenCount', ['endpoint', 'margin', 'timeoutMs']);
        tokenCount = Object.freeze({
            ...(raw['endpoint'] !== undefined ? { endpoint: nonEmptyString(raw['endpoint'], 'tokenCount.endpoint') } : {}),
            ...(raw['margin'] !== undefined ? { margin: nonNegativeInt(raw['margin'], 'tokenCount.margin') } : {}),
            ...(raw['timeoutMs'] !== undefined ? { timeoutMs: positiveInt(raw['timeoutMs'], 'tokenCount.timeoutMs') } : {}),
        });
    }
    // Per-purpose decisions for advisory model calls. Only the two known
    // purposes may be configured, and only with an explicit decision; the
    // resolved policy (against refuseAuxiliaryCalls, default refuse) is what
    // the lease serves and /info reports.
    let auxiliaryPolicy;
    if (input['auxiliaryPolicy'] !== undefined) {
        const raw = record(input['auxiliaryPolicy'], 'auxiliaryPolicy', ['compaction', 'session-title']);
        for (const purpose of ['compaction', 'session-title']) {
            const decision = raw[purpose];
            if (decision === undefined)
                continue;
            if (decision !== 'refuse' && decision !== 'allow')
                fail('auxiliaryPolicy', `${purpose} must be 'refuse' or 'allow'`);
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
function loadConfig(argv) {
    const [path, ...extra] = argv;
    if (path === undefined || extra.length > 0) {
        throw new BrokerConfigError('aeval-model-broker: usage: aeval-model-broker <config.json>');
    }
    let text;
    try {
        text = readFileSync(path, 'utf8');
    }
    catch (error) {
        throw new BrokerConfigError(`aeval-model-broker: config file cannot be read: ${firstLine(error)}`);
    }
    let raw;
    try {
        raw = JSON.parse(text);
    }
    catch {
        throw new BrokerConfigError('aeval-model-broker: config file is not valid JSON');
    }
    return parseBrokerMainConfig(raw);
}
function firstLine(error) {
    const message = error instanceof Error ? `${error.name}: ${error.message}` : String(error);
    const line = message.split(/\r?\n/u)[0] ?? '';
    return line === '' ? 'unknown failure' : line;
}
export async function main(argv = process.argv.slice(2)) {
    let broker;
    let tokenPath;
    let controller;
    let resolveShutdown = () => { };
    const shutdown = new Promise((resolve) => { resolveShutdown = resolve; });
    let signals = 0;
    const onSignal = () => {
        // A second signal during shutdown means the operator wants out now.
        if (++signals > 1)
            process.exit(1);
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
            // The bound must count the exact dispatch body, so it speaks the same
            // wire the upstream adapter speaks.
            ...(config.upstream.protocol !== undefined ? { protocol: config.upstream.protocol } : {}),
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
            }
            catch (error) {
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
    }
    catch (error) {
        // A shutdown signal that interrupted startup is a normal close, not a failure.
        if (signals > 0 && error instanceof Error && error.name === 'AbortError')
            return 0;
        process.stderr.write(`aeval-model-broker: ${firstLine(error)}\n`);
        if (error instanceof BrokerConfigError)
            return error.exitCode;
        if (error instanceof EvalControlConfigError)
            return 2;
        if (error instanceof LlmError && ['MISSING_CREDENTIAL', 'INVALID_CREDENTIAL', 'INVALID_CONFIG'].includes(error.code))
            return 2;
        return 1;
    }
    finally {
        controller?.abort();
        if (broker !== undefined)
            await broker.close().catch(() => { });
        if (tokenPath !== undefined) {
            try {
                cleanupJobToken(tokenPath);
            }
            catch (error) {
                // Cleanup failure must not turn a clean shutdown into a failure exit.
                process.stderr.write(`aeval-model-broker: token cleanup failed: ${firstLine(error)}\n`);
            }
        }
    }
}
function isMain() {
    try {
        return process.argv[1] !== undefined && import.meta.url === pathToFileURL(realpathSync(process.argv[1])).href;
    }
    catch {
        return false;
    }
}
if (isMain())
    process.exit(await main());
