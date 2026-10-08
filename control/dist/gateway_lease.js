import { closeSync, constants, fstatSync, openSync, readFileSync } from 'node:fs';
import { isDeepStrictEqual } from 'node:util';
import { LlmAdapter, LlmError, attributionHeaders, resolveRetryPolicy } from '@deepseek-ai/dsh-llm';
import { validateRunBinding } from './config.js';
import { isStopReason } from './stop_reason.js';
export const GATEWAY_PROTOCOL = 'aeval-model-broker/3';
export const MAX_WIRE_BYTES = 8 * 1024 * 1024;
/**
 * Resolve the authored auxiliary policy against the legacy blanket flag.
 *
 * An explicit per-purpose decision always wins; purposes without one take
 * ``refuseAuxiliaryCalls`` (default refuse). Both the broker and the sandbox
 * adapter resolve with this exact function, so ``/info`` comparisons are
 * authoritative rather than field-by-field approximations.
 */
export function resolveAuxiliaryPolicy(partial, refuseAll) {
    const fallback = refuseAll === false ? 'allow' : 'refuse';
    const resolved = {
        compaction: fallback,
        'session-title': fallback,
    };
    if (partial !== undefined) {
        if (partial.compaction !== undefined)
            resolved.compaction = partial.compaction;
        if (partial['session-title'] !== undefined)
            resolved['session-title'] = partial['session-title'];
    }
    return Object.freeze(resolved);
}
export function isAuxiliaryPurpose(value) {
    return value === 'compaction' || value === 'session-title';
}
export class GatewayError extends LlmError {
    stopReason;
    constructor(code, stopReason = 'infra_error') {
        super(code, code);
        this.stopReason = stopReason;
    }
}
// Always observe late settlements, but never let an uncooperative operation own shutdown.
export async function abortable(operation, signal) {
    signal.throwIfAborted();
    let abort;
    try {
        const result = await new Promise((resolve, reject) => {
            abort = () => reject(signal.reason);
            signal.addEventListener('abort', abort, { once: true });
            try {
                Promise.resolve(operation()).then(resolve, reject);
            }
            catch (error) {
                reject(error);
            }
        });
        signal.throwIfAborted();
        return result;
    }
    finally {
        signal.removeEventListener('abort', abort);
    }
}
export function detachedCleanup(operation) {
    try {
        void Promise.resolve(operation()).catch(() => { });
    }
    catch { /* Cleanup cannot replace the original failure. */ }
}
export function objectOf(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value))
        throw new GatewayError('AEVAL_INVALID_WIRE');
    return value;
}
export function tokenCount(value) {
    if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
        throw new GatewayError('AEVAL_INVALID_USAGE');
    }
    return value;
}
export function usageTotals(usage) {
    const input = tokenCount(usage.inputTokens) + tokenCount(usage.cacheReadTokens ?? 0) + tokenCount(usage.cacheWriteTokens ?? 0);
    const total = tokenCount(input + tokenCount(usage.outputTokens));
    if (usage.totalTokens !== undefined && tokenCount(usage.totalTokens) !== total)
        throw new GatewayError('AEVAL_INVALID_USAGE');
    if (usage.reasoningTokens !== undefined && tokenCount(usage.reasoningTokens) > usage.outputTokens)
        throw new GatewayError('AEVAL_INVALID_USAGE');
    return { input, total };
}
function checkBlock(raw) {
    const block = objectOf(raw);
    if ((block['type'] === 'text' || block['type'] === 'reasoning') && typeof block['text'] === 'string'
        && Object.keys(block).every((key) => ['type', 'text'].includes(key)))
        return;
    if (block['type'] === 'tool-call' && ['id', 'name', 'arguments'].every((k) => typeof block[k] === 'string')
        && Object.keys(block).every((key) => ['type', 'id', 'name', 'arguments'].includes(key)))
        return;
    // Attachment references belong to the sandbox and must never resolve against host files.
    throw new GatewayError('AEVAL_UNSUPPORTED_CONTENT');
}
export function parseStreamChunk(raw) {
    const chunk = objectOf(raw);
    switch (chunk['type']) {
        case 'usage':
            usageTotals(objectOf(chunk['usage']));
            break;
        case 'finish': {
            const reason = objectOf(chunk['reason']);
            if (!['stop', 'tool-calls', 'max-tokens', 'error', 'aborted'].includes(String(reason['kind'])))
                throw new GatewayError('AEVAL_INVALID_WIRE');
            if (reason['kind'] === 'error' || reason['kind'] === 'aborted') {
                const failure = objectOf(reason['failure']);
                if (typeof failure['code'] !== 'string' || typeof failure['message'] !== 'string')
                    throw new GatewayError('AEVAL_INVALID_WIRE');
            }
            if (chunk['replayState'] !== undefined) {
                const replay = objectOf(chunk['replayState']);
                if (!Object.hasOwn(replay, 'response') || (replay['blocks'] !== undefined && !Array.isArray(replay['blocks'])))
                    throw new GatewayError('AEVAL_INVALID_WIRE');
            }
            break;
        }
        case 'block-start':
            tokenCount(chunk['index']);
            if (!['text', 'reasoning', 'tool-call'].includes(String(chunk['blockType'])))
                throw new GatewayError('AEVAL_UNSUPPORTED_CONTENT');
            break;
        case 'text-delta':
        case 'reasoning-delta':
            tokenCount(chunk['index']);
            if (typeof chunk['text'] !== 'string')
                throw new GatewayError('AEVAL_INVALID_WIRE');
            break;
        case 'tool-call-delta':
            tokenCount(chunk['index']);
            if (typeof chunk['id'] !== 'string' || typeof chunk['argumentsDelta'] !== 'string' || (chunk['name'] !== undefined && typeof chunk['name'] !== 'string'))
                throw new GatewayError('AEVAL_INVALID_WIRE');
            break;
        case 'block-end':
            tokenCount(chunk['index']);
            checkBlock(chunk['block']);
            break;
        default: throw new GatewayError('AEVAL_INVALID_WIRE');
    }
    return chunk;
}
/**
 * The exact key set the broker's trust boundary accepts. Kept next to
 * ``parseBrokerRequest`` so the two cannot drift apart silently.
 */
export const BROKER_WIRE_KEYS = Object.freeze([
    'provider', 'model', 'reasoningEffort', 'messages', 'system', 'tools',
    'temperature', 'maxTokens', 'stop', 'sessionId', 'purpose',
]);
/**
 * Project DSH's ``GenerateOptions`` onto the broker wire contract.
 *
 * DSH carries fields the wire does not define — ``toolHistory`` is
 * optional by contract ("omission sends complete declarations without
 * tool updates") and ``signal`` is transport-local — so forwarding the
 * object verbatim makes the broker answer ``AEVAL_INVALID_REQUEST``
 * (found by running a real sandbox against a real broker).
 */
export function wireBodyOf(options) {
    const source = options;
    const body = {};
    for (const key of BROKER_WIRE_KEYS) {
        if (source[key] !== undefined)
            body[key] = source[key];
    }
    return body;
}
export function parseBrokerRequest(raw) {
    const request = objectOf(raw);
    const keys = new Set(BROKER_WIRE_KEYS);
    if (Object.keys(request).some((key) => !keys.has(key)) || !Array.isArray(request['messages']))
        throw new GatewayError('AEVAL_INVALID_REQUEST');
    for (const rawMessage of request['messages']) {
        const message = objectOf(rawMessage);
        if (!['user', 'assistant', 'system', 'tool'].includes(String(message['role'])) || !Array.isArray(message['content']))
            throw new GatewayError('AEVAL_INVALID_REQUEST');
        message['content'].forEach(checkBlock);
        if (message['role'] === 'tool' && typeof message['toolCallId'] !== 'string')
            throw new GatewayError('AEVAL_INVALID_REQUEST');
        if (message['source'] !== undefined) {
            const source = objectOf(message['source']);
            // This is structural validation only. The host lease must authorize native
            // replay against its own emitted-message ledger before dispatch.
            if (source['replayState'] !== undefined && (message['role'] !== 'assistant'
                || source['kind'] !== 'model' || typeof source['provider'] !== 'string'
                || typeof source['model'] !== 'string'))
                throw new GatewayError('AEVAL_UNSUPPORTED_REPLAY');
        }
    }
    if (request['tools'] !== undefined) {
        if (!Array.isArray(request['tools']))
            throw new GatewayError('AEVAL_INVALID_REQUEST');
        for (const rawTool of request['tools']) {
            const tool = objectOf(rawTool);
            if (typeof tool['name'] !== 'string' || typeof tool['description'] !== 'string')
                throw new GatewayError('AEVAL_INVALID_REQUEST');
            objectOf(tool['parameters']);
        }
    }
    for (const field of ['provider', 'model', 'reasoningEffort', 'system', 'sessionId']) {
        if (request[field] !== undefined && typeof request[field] !== 'string')
            throw new GatewayError('AEVAL_INVALID_REQUEST');
    }
    if (request['maxTokens'] !== undefined && tokenCount(request['maxTokens']) === 0)
        throw new GatewayError('AEVAL_INVALID_REQUEST');
    if (request['temperature'] !== undefined && (typeof request['temperature'] !== 'number' || !Number.isFinite(request['temperature'])))
        throw new GatewayError('AEVAL_INVALID_REQUEST');
    if (request['purpose'] !== undefined && !['compaction', 'session-title'].includes(String(request['purpose'])))
        throw new GatewayError('AEVAL_INVALID_REQUEST');
    if (request['stop'] !== undefined && (!Array.isArray(request['stop']) || request['stop'].some((s) => typeof s !== 'string')))
        throw new GatewayError('AEVAL_INVALID_REQUEST');
    return request;
}
export function readJobToken(path) {
    if (process.platform === 'win32')
        throw new Error('Job-token files require a POSIX 0600 runtime; Windows ACL equivalence is not implemented');
    const fd = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
        const stat = fstatSync(fd);
        if (!stat.isFile() || stat.uid !== process.getuid?.() || (stat.mode & 0o777) !== 0o600 || stat.size > 256)
            throw new Error('Job-token file must be owned by the current user with mode 0600');
        const token = readFileSync(fd, 'utf8').trim();
        if (!/^[a-f0-9]{64}$/.test(token))
            throw new Error('Invalid job-token file');
        return token;
    }
    finally {
        closeSync(fd);
    }
}
/** Rejections that a lease returns BEFORE dispatching to any provider. */
const PRE_DISPATCH_REJECTION_CODES = new Set([
    'AEVAL_LEASE_CLOSED', 'AEVAL_LEASE_BUSY', 'AEVAL_AUXILIARY_REFUSED', 'AEVAL_BUDGET_EXHAUSTED',
]);
export class BrokerAdapter extends LlmAdapter {
    #token;
    #config;
    #listeners = new Set();
    #onRejection;
    #onDispatch;
    #model;
    constructor(config, jobToken, onRejection, onDispatch) {
        super();
        this.#config = Object.freeze({ ...config, run: validateRunBinding(config.run) });
        this.#token = jobToken;
        this.#onRejection = onRejection;
        this.#onDispatch = onDispatch;
    }
    observeTerminal(listener) {
        this.#listeners.add(listener);
        return () => { this.#listeners.delete(listener); };
    }
    async request(path, init = {}) {
        const response = await fetch(`${this.#config.gatewayUrl.replace(/\/$/, '')}${path}`, {
            ...init,
            redirect: 'error',
            signal: init.signal ?? AbortSignal.timeout(30_000),
            headers: { ...attributionHeaders(), authorization: `Bearer ${this.#token}`, 'content-type': 'application/json' },
        });
        if (!response.ok) {
            await response.body?.cancel();
            const reason = response.headers.get('aeval-stop-reason');
            const code = response.headers.get('aeval-error-code') ?? 'AEVAL_GATEWAY_FAILED';
            throw new GatewayError(/^AEVAL_[A-Z_]+$/.test(code) ? code : 'AEVAL_GATEWAY_FAILED', isStopReason(reason) ? reason : 'infra_error');
        }
        return response;
    }
    async info(signal) {
        const response = await this.request('/info', signal ? { signal } : {});
        const raw = objectOf(JSON.parse(await boundedText(response)));
        const identity = objectOf(raw['identity']);
        const limits = objectOf(raw['limits']);
        const c = this.#config;
        let run;
        try {
            run = validateRunBinding(raw['run']);
        }
        catch {
            throw new GatewayError('AEVAL_LEASE_MISMATCH');
        }
        if (raw['protocol'] !== GATEWAY_PROTOCOL || raw['trialId'] !== c.trialId || raw['sessionId'] !== c.sessionId || raw['configDigest'] !== c.configDigest
            || run.run_id !== c.run.run_id || run.job_config_hash !== c.run.job_config_hash
            || run.config_file_sha256 !== c.run.config_file_sha256 || run.runtime_lock_digest !== c.run.runtime_lock_digest
            || identity['provider'] !== c.provider || identity['model'] !== c.model || identity['reasoningEffort'] !== c.reasoningEffort
            || limits['maxSteps'] !== c.maxSteps || limits['maxTokens'] !== c.maxTokens
            || !isDeepStrictEqual(raw['auxiliaryPolicy'], resolveAuxiliaryPolicy(c.auxiliaryPolicy, c.refuseAuxiliaryCalls)))
            throw new GatewayError('AEVAL_LEASE_MISMATCH');
        tokenCount(raw['usedSteps']);
        tokenCount(raw['usedTokens']);
        tokenCount(raw['reservedTokens']);
        if (raw['stopReason'] !== undefined && !isStopReason(raw['stopReason']))
            throw new GatewayError('AEVAL_INVALID_WIRE');
        const model = objectOf(raw['model']);
        if (model['provider'] !== c.provider || model['id'] !== c.model || typeof model['name'] !== 'string')
            throw new GatewayError('AEVAL_LEASE_MISMATCH');
        this.#model = structuredClone(model);
        return { ...raw, run };
    }
    providerRetryPolicy() { return resolveRetryPolicy({ mode: 'normal', maxRetries: 0 }, 'gateway.retry'); }
    async resolveModel(provider, model, signal) {
        if (provider !== this.#config.provider || model !== this.#config.model)
            throw new GatewayError('AEVAL_IDENTITY_MISMATCH');
        if (!this.#model)
            await this.info(signal);
        return structuredClone(this.#model);
    }
    async *stream(options) {
        const controller = new AbortController();
        const signal = options.signal ? AbortSignal.any([options.signal, controller.signal]) : controller.signal;
        // Project onto the broker's wire contract instead of forwarding every
        // DSH field: the broker validates a strict key set (unknown keys are
        // AEVAL_INVALID_REQUEST), and DSH carries fields the wire does not
        // define — `toolHistory` is optional by contract ("omission sends
        // complete declarations without tool updates") and `signal` is
        // transport-local. Found by running the real sandbox against a real
        // broker: the request was rejected with AEVAL_INVALID_REQUEST.
        const body = wireBodyOf(options);
        let reader;
        let finished = false;
        let usageSeen = false;
        let usage;
        let failure;
        let gatewayFailure = false;
        let expectedRejection = false;
        try {
            const response = await abortable(() => this.request('/stream', { method: 'POST', body: JSON.stringify(body), signal }), signal);
            if (!response.body)
                throw new GatewayError('AEVAL_INVALID_WIRE');
            reader = response.body.getReader();
            const decoder = new TextDecoder('utf-8', { fatal: true });
            let buffer = '';
            let terminal;
            for (;;) {
                const { done, value } = await abortable(() => reader.read(), signal);
                buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
                let boundary;
                while ((boundary = buffer.indexOf('\n')) !== -1) {
                    signal.throwIfAborted();
                    const line = buffer.slice(0, boundary);
                    buffer = buffer.slice(boundary + 1);
                    if (!line || terminal || Buffer.byteLength(line) + 1 > MAX_WIRE_BYTES)
                        throw new GatewayError('AEVAL_INVALID_WIRE');
                    const raw = objectOf(JSON.parse(line));
                    if (raw['type'] === 'gateway-error')
                        throw new GatewayError('AEVAL_GATEWAY_FAILED', isStopReason(raw['stopReason']) ? raw['stopReason'] : 'infra_error');
                    const chunk = parseStreamChunk(raw);
                    if (chunk.type === 'usage') {
                        if (usageSeen)
                            throw new GatewayError('AEVAL_INVALID_USAGE');
                        usageSeen = true;
                        usage = chunk.usage;
                    }
                    else if (usageSeen && chunk.type !== 'finish')
                        throw new GatewayError('AEVAL_INVALID_WIRE');
                    if (chunk.type === 'finish') {
                        if (chunk.reason.kind === 'error' || chunk.reason.kind === 'aborted')
                            throw new GatewayError('AEVAL_UPSTREAM_FAILED');
                        terminal = chunk;
                    }
                    else
                        yield chunk;
                }
                // A transport read may contain many individually valid lines.
                if (Buffer.byteLength(buffer) >= MAX_WIRE_BYTES)
                    throw new GatewayError('AEVAL_INVALID_WIRE');
                if (done)
                    break;
            }
            signal.throwIfAborted();
            if (buffer || !terminal || !usageSeen)
                throw new GatewayError('AEVAL_TRUNCATED_STREAM');
            finished = true;
            // An auxiliary call the policy allowed and the broker dispatched
            // is model work the session will never settle as an assistant sample.
            // Record its metered usage beside the descriptor so the reducer can
            // merge it into the accounted totals. Recording must never disturb
            // the model path — same contract as the rejection recorder.
            const purpose = body.purpose;
            if (typeof purpose === 'string' && purpose !== '' && usage !== undefined) {
                try {
                    this.#onDispatch?.({ code: 'AEVAL_AUXILIARY_DISPATCHED', purpose, usage: structuredClone(usage) });
                }
                catch { /* the caller's recorder must not break the run */ }
            }
            yield terminal;
        }
        catch (error) {
            const cause = signal.aborted && signal.reason instanceof GatewayError ? signal.reason : error;
            if (cause instanceof GatewayError && PRE_DISPATCH_REJECTION_CODES.has(cause.code)) {
                // Evidence only: the broker already refused, so this cannot consume
                // tokens. Recording must never disturb the model path.
                const purpose = body.purpose;
                try {
                    this.#onRejection?.({
                        code: cause.code,
                        ...(typeof purpose === 'string' && purpose !== '' ? { purpose } : {}),
                    });
                }
                catch { /* the caller's recorder must not break the run */ }
            }
            gatewayFailure = cause instanceof GatewayError;
            // A concurrent request that finds the lease busy is an expected,
            // retryable conflict, not an infrastructure fault: the broker answers
            // 409 and deliberately neither dispatches nor stops the lease (see
            // "busy requests neither dispatch nor stop the active lease"). The
            // runtime issues advisory calls (a session-title request) alongside
            // the real one, so treating this as a trusted failure recorded
            // infra_error on a healthy run and blocked the owner's finalization
            // (observed in practice, tracked down through the plugin trace). A
            // policy-refused advisory call is the same shape: the owner decided
            // that request consumes nothing, so it cannot downgrade the trial.
            expectedRejection = cause instanceof GatewayError
                && (cause.code === 'AEVAL_LEASE_BUSY' || cause.code === 'AEVAL_AUXILIARY_REFUSED');
            failure = cause instanceof GatewayError ? cause.stopReason : 'infra_error';
            throw cause instanceof GatewayError ? cause : new GatewayError('AEVAL_GATEWAY_FAILED', failure);
        }
        finally {
            const brokerReported = signal.aborted && signal.reason instanceof GatewayError;
            const reason = failure ?? (brokerReported ? signal.reason.stopReason : 'infra_error');
            controller.abort();
            detachedCleanup(() => reader?.cancel());
            detachedCleanup(() => reader?.releaseLock());
            // A caller that cancels its OWN request observes no gateway failure:
            // the run's runtime aborts model calls for ordinary control-flow
            // reasons (step cancellation, shutdown). Reporting that as a trusted
            // infrastructure failure permanently downgraded the trial — the
            // descriptor recorded infra_error and the evidence gate treated a
            // healthy run as infra-invalid (observed in practice). The broker's
            // lease state stays authoritative: a lease that really stopped is
            // reported by its own error code here, and is re-read from /info
            // when the owner finalizes.
            const callerCancelled = !gatewayFailure && !brokerReported && options.signal?.aborted === true;
            if (!finished && !callerCancelled && !expectedRejection)
                for (const listener of [...this.#listeners]) {
                    detachedCleanup(() => listener(reason));
                }
        }
    }
}
async function boundedText(response) {
    if (!response.body)
        throw new GatewayError('AEVAL_INVALID_WIRE');
    const reader = response.body.getReader();
    const chunks = [];
    let size = 0;
    try {
        for (;;) {
            const { done, value } = await reader.read();
            if (done)
                return new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks));
            size += value.byteLength;
            if (size > MAX_WIRE_BYTES)
                throw new GatewayError('AEVAL_INVALID_WIRE');
            chunks.push(value);
        }
    }
    finally {
        await reader.cancel().catch(() => { });
        reader.releaseLock();
    }
}
