import { randomBytes, createHash, timingSafeEqual } from 'node:crypto';
import { createServer } from 'node:http';
import { createServer as createTlsServer } from 'node:https';
import { isIP } from 'node:net';
import { once } from 'node:events';
import { lstatSync, readFileSync, unlinkSync, writeFileSync } from 'node:fs';
import { isDeepStrictEqual } from 'node:util';
import { BlockAssembler, ReasoningEffortId } from '@deepseek-ai/dsh-llm';
import { SessionId } from '@deepseek-ai/dsh-session';
import { GATEWAY_PROTOCOL, MAX_WIRE_BYTES, GatewayError, abortable, detachedCleanup, isAuxiliaryPurpose, parseBrokerRequest, parseStreamChunk, resolveAuxiliaryPolicy, tokenCount, usageTotals } from './gateway_lease.js';
import { isStopReason } from './stop_reason.js';
import { validateIdentifier, validateRunBinding, validateSha256Digest } from './config.js';
// Only literal loopback addresses qualify: a DNS name must not authorize plaintext.
export function isLoopbackHost(host) {
    return (isIP(host) === 4 && host.startsWith('127.')) || host === '::1';
}
function freeze(value) {
    if (value && typeof value === 'object') {
        Object.freeze(value);
        Object.values(value).forEach(freeze);
    }
    return value;
}
async function* abortableIteration(source, signal) {
    signal.throwIfAborted();
    const iterator = source[Symbol.asyncIterator]();
    let ended = false;
    try {
        for (;;) {
            const next = await abortable(() => iterator.next(), signal);
            if (next.done) {
                ended = true;
                return;
            }
            yield next.value;
        }
    }
    finally {
        if (!ended)
            detachedCleanup(() => iterator.return?.());
    }
}
export class GatewayLease {
    #policy;
    #lifetime = new AbortController();
    #model;
    // Model sources do not carry effort in DSH; the immutable lease identity binds it.
    #replay = [];
    get signal() { return this.#lifetime.signal; }
    #usedSteps = 0;
    #usedTokens = 0;
    #reservedTokens = 0;
    #busy = false;
    #stopReason;
    // The complete per-purpose decision this lease serves: explicit
    // entries from the operator's spec, falling back to the blanket flag.
    #auxiliary;
    constructor(options, model) {
        this.#policy = Object.freeze({
            ...options,
            run: validateRunBinding(options.run),
            trialId: validateIdentifier(options.trialId, 'trialId'),
            sessionId: validateIdentifier(options.sessionId, 'sessionId'),
            configDigest: validateSha256Digest(options.configDigest, 'configDigest'),
            identity: freeze(structuredClone(options.identity)),
            limits: freeze(structuredClone(options.limits)),
        });
        this.#auxiliary = resolveAuxiliaryPolicy(this.#policy.auxiliaryPolicy, this.#policy.refuseAuxiliaryCalls ?? true);
        this.#model = freeze(structuredClone(model));
    }
    stop(reason, cause = 'unspecified') {
        if (!isStopReason(reason))
            throw new Error('Invalid stop reason');
        if (this.#stopReason)
            return;
        this.#stopReason = reason;
        // Diagnostics: the trial captures this process's stderr, and a lease
        // that closes for an unexplained reason makes every later model call
        // fail with AEVAL_LEASE_CLOSED. WHO closed it, and on which path, is
        // otherwise unrecorded (observed in practice: an unexplained closed
        // lease cost two full runs before the cause could be attributed).
        if (process.env['AEVAL_BROKER_DIAG'] === '1') {
            process.stderr.write(`[aeval-broker] lease stop at=${new Date().toISOString()} reason=${reason} cause=${cause}\n${new Error('lease stop').stack ?? ''}\n`);
        }
        this.#lifetime.abort(new GatewayError('AEVAL_LEASE_CLOSED', reason));
        detachedCleanup(() => this.#policy.onStop?.(reason));
    }
    snapshot() {
        return freeze({
            protocol: GATEWAY_PROTOCOL,
            run: this.#policy.run,
            trialId: this.#policy.trialId,
            sessionId: this.#policy.sessionId,
            configDigest: this.#policy.configDigest,
            identity: this.#policy.identity,
            limits: this.#policy.limits,
            auxiliaryPolicy: this.#auxiliary,
            usedSteps: this.#usedSteps,
            usedTokens: this.#usedTokens,
            reservedTokens: this.#reservedTokens,
            ...(this.#stopReason ? { stopReason: this.#stopReason } : {}),
            model: this.#model,
        });
    }
    refuse(code, reason) {
        this.stop(reason);
        throw new GatewayError(code, reason);
    }
    async *stream(raw, callerSignal) {
        if (this.#stopReason)
            throw new GatewayError('AEVAL_LEASE_CLOSED', this.#stopReason);
        if (this.#busy)
            throw new GatewayError('AEVAL_LEASE_BUSY');
        const input = parseBrokerRequest(structuredClone(raw));
        if (input.sessionId !== this.#policy.sessionId)
            throw new GatewayError('AEVAL_SESSION_MISMATCH');
        input.messages = input.messages.map((message) => {
            if (message.source?.kind !== 'model' || message.source.replayState === undefined)
                return message;
            const issued = this.#replay.find((entry) => isDeepStrictEqual(entry.identity, this.#policy.identity)
                && isDeepStrictEqual(entry.content, message.content) && isDeepStrictEqual(entry.source, message.source));
            if (!issued || message.role !== 'assistant'
                || (input.reasoningEffort !== undefined && input.reasoningEffort !== this.#policy.identity.reasoningEffort)) {
                throw new GatewayError('AEVAL_UNTRUSTED_REPLAY');
            }
            // Never forward candidate-owned native state, even after equality checks.
            return { ...message, content: structuredClone(issued.content), source: structuredClone(issued.source) };
        });
        if (input.purpose) {
            // The decision is per-purpose. A refused advisory call is still
            // request-scoped: it provably consumes no tokens and must not
            // end a healthy lease — the same non-terminal shape as AEVAL_LEASE_BUSY.
            const decision = isAuxiliaryPurpose(input.purpose)
                ? this.#auxiliary[input.purpose]
                : 'refuse';
            if (decision === 'refuse')
                throw new GatewayError('AEVAL_AUXILIARY_REFUSED');
        }
        this.#busy = true;
        let dispatched = false;
        let complete = false;
        let reservation = 0;
        let actual;
        const operation = new AbortController();
        const signal = AbortSignal.any([callerSignal, this.#lifetime.signal, operation.signal]);
        try {
            signal.throwIfAborted();
            const limits = this.#policy.limits;
            // Retries and auxiliary requests also consume a step, so clients cannot forge step identities.
            if (limits.maxSteps !== undefined && this.#usedSteps >= limits.maxSteps)
                this.refuse('AEVAL_BUDGET_EXHAUSTED', 'budget_exhausted');
            const { provider: _provider, model: _model, reasoningEffort: _effort, ...content } = input;
            let maxTokens = Math.min(input.maxTokens ?? this.#policy.maxOutputTokens, this.#policy.maxOutputTokens);
            let request = Object.freeze({ ...freeze({
                    ...content,
                    provider: this.#policy.identity.provider,
                    model: this.#policy.identity.model,
                    sessionId: SessionId(this.#policy.sessionId),
                    ...(this.#policy.identity.reasoningEffort ? { reasoningEffort: ReasoningEffortId(this.#policy.identity.reasoningEffort) } : {}),
                    maxTokens,
                }), signal });
            let inputBound;
            if (limits.maxTokens !== undefined) {
                const measure = async () => tokenCount(await abortable(() => this.#policy.inputTokenUpperBound(request), signal));
                inputBound = await measure();
                const remaining = limits.maxTokens - this.#usedTokens;
                const availableOutput = remaining - inputBound;
                if (availableOutput < 1)
                    this.refuse('AEVAL_BUDGET_EXHAUSTED', 'budget_exhausted');
                if (maxTokens > availableOutput) {
                    maxTokens = availableOutput;
                    request = Object.freeze({ ...request, maxTokens });
                    inputBound = await measure();
                }
                // A cap-dependent meter must certify the exact dispatch. Do not converge
                // indefinitely if clamping changes its bound: conservatively refuse.
                if (inputBound > remaining - maxTokens)
                    this.refuse('AEVAL_BUDGET_EXHAUSTED', 'budget_exhausted');
                reservation = tokenCount(inputBound + maxTokens);
            }
            signal.throwIfAborted();
            this.#reservedTokens = reservation;
            this.#usedSteps++;
            dispatched = true;
            let usage;
            let terminal;
            const assembler = new BlockAssembler();
            for await (const rawChunk of abortableIteration(this.#policy.upstream.stream(request), signal)) {
                signal.throwIfAborted();
                if (terminal)
                    throw new GatewayError('AEVAL_INVALID_WIRE');
                // Assemble the exact detached JSON that the client receives, not mutable
                // adapter objects or a second, approximate transcript representation.
                const wire = JSON.stringify(rawChunk);
                if (Buffer.byteLength(wire) + 1 > MAX_WIRE_BYTES)
                    throw new GatewayError('AEVAL_INVALID_WIRE');
                const chunk = freeze(parseStreamChunk(JSON.parse(wire)));
                assembler.push(chunk);
                if (chunk.type === 'usage') {
                    if (usage)
                        throw new GatewayError('AEVAL_INVALID_USAGE');
                    usage = chunk.usage;
                    const totals = usageTotals(usage);
                    actual = totals.total;
                    if (usage.outputTokens > maxTokens || (inputBound !== undefined && totals.input > inputBound))
                        throw new GatewayError('AEVAL_TOKEN_BOUND_VIOLATED');
                }
                else if (usage && chunk.type !== 'finish')
                    throw new GatewayError('AEVAL_INVALID_WIRE');
                if (chunk.type === 'finish') {
                    if (chunk.reason.kind === 'error' || chunk.reason.kind === 'aborted')
                        throw new GatewayError('AEVAL_UPSTREAM_FAILED');
                    terminal = chunk;
                }
                else
                    yield chunk;
            }
            signal.throwIfAborted();
            if (!terminal || !usage)
                throw new GatewayError('AEVAL_TRUNCATED_STREAM');
            const replayState = assembler.replayState;
            if (replayState !== undefined) {
                this.#replay.push(freeze(structuredClone({
                    identity: this.#policy.identity,
                    content: assembler.blocks(),
                    source: { kind: 'model', provider: request.provider, model: request.model, replayState },
                })));
            }
            this.#usedTokens = tokenCount(this.#usedTokens + actual);
            complete = true;
            this.#reservedTokens = 0;
            yield terminal;
        }
        catch (error) {
            // Only an abandoned IN-FLIGHT provider call is unaccountable, and
            // only that must fail closed. A caller that cancels before anything
            // was dispatched spends no tokens and leaves no unknown usage, so
            // closing the lease there converts one transient cancellation into
            // a permanently dead trial: every later call answers
            // AEVAL_LEASE_CLOSED and the run cannot recover (found on the real
            // chain, where two runs were lost to exactly this amplification).
            // The caller's own cancellation still fails its request.
            const abandonedInFlight = signal.aborted && dispatched;
            if (!this.#stopReason && (abandonedInFlight || !signal.aborted)) {
                this.stop('infra_error', signal.aborted ? 'client_abort_in_flight' : 'upstream_error');
            }
            throw new GatewayError(error instanceof GatewayError ? error.code : 'AEVAL_UPSTREAM_FAILED', this.#stopReason ?? 'infra_error');
        }
        finally {
            operation.abort();
            if (dispatched && !complete) {
                this.#usedTokens = tokenCount(this.#usedTokens + Math.max(reservation, actual ?? 0));
                this.stop('infra_error', 'dispatch_incomplete');
            }
            this.#reservedTokens = 0;
            this.#busy = false;
        }
    }
}
export async function startHostBroker(options) {
    // Pin owner-selected binding before asynchronous model resolution can yield.
    options = Object.freeze({
        ...options,
        run: validateRunBinding(options.run),
        trialId: validateIdentifier(options.trialId, 'trialId'),
        sessionId: validateIdentifier(options.sessionId, 'sessionId'),
        configDigest: validateSha256Digest(options.configDigest, 'configDigest'),
    });
    const { host, port = 0, tls } = options.listen ?? { host: '127.0.0.1' };
    if (typeof host !== 'string' || !host || (!isIP(host) && !/^[a-zA-Z0-9.-]+$/.test(host)))
        throw new Error('Invalid broker listen host');
    if (!Number.isSafeInteger(port) || port < 0 || port > 65535)
        throw new Error('Invalid broker listen port');
    if (!isLoopbackHost(host) && !tls)
        throw new Error('Nonloopback broker listeners require TLS');
    if (tls && (typeof tls.key !== 'string' || !tls.key || typeof tls.cert !== 'string' || !tls.cert))
        throw new Error('Invalid broker TLS material');
    for (const value of [options.maxOutputTokens, options.limits.maxSteps, options.limits.maxTokens, options.timeoutMs, options.tokenTtlMs]) {
        if (value !== undefined && tokenCount(value) === 0)
            throw new Error('Broker limits must be positive safe integers');
    }
    if (options.timeoutMs !== undefined && options.timeoutMs > 2_147_483_647)
        throw new Error('Broker timeout exceeds timer range');
    if (options.tokenTtlMs !== undefined && options.tokenTtlMs > 2_147_483_647)
        throw new Error('Broker token TTL exceeds timer range');
    if (options.limits.maxTokens !== undefined && !options.inputTokenUpperBound)
        throw new Error('Hard token budgets require a trusted provider input-token upper bound');
    options.signal.throwIfAborted();
    const model = await abortable(() => options.upstream.resolveModel(options.identity.provider, options.identity.model, options.signal), options.signal);
    options.signal.throwIfAborted();
    if (model.provider !== options.identity.provider || model.id !== options.identity.model)
        throw new Error('Upstream resolved a different model');
    if (options.identity.reasoningEffort && !model.reasoning?.efforts.some((effort) => effort.id === options.identity.reasoningEffort))
        throw new Error('Upstream does not declare the requested reasoning effort');
    if (!options.identity.reasoningEffort && model.reasoning?.defaultEffort)
        throw new Error('Pin the upstream default reasoning effort explicitly');
    const lease = new GatewayLease(options, { ...model, defaultMaxTokens: options.maxOutputTokens });
    const token = randomBytes(32).toString('hex');
    const expectedAuth = createHash('sha256').update(`Bearer ${token}`).digest();
    // The TTL is anchored at token issuance, before the listener exists, so the
    // credential can never outlive its lifetime while the host entry is starting up.
    const expiresAt = options.tokenTtlMs === undefined ? undefined : Date.now() + options.tokenTtlMs;
    const active = new Set();
    const sockets = new Set();
    const handler = (req, res) => {
        const work = handle(req, res).catch(() => { res.destroy(); }).finally(() => active.delete(work));
        active.add(work);
    };
    const serverOptions = { maxHeaderSize: 16_384, requestTimeout: 30_000 };
    // ``tls.key``/``tls.cert`` are file paths: node's TLS server parses the
    // strings it receives as PEM, so the material is read here and an
    // unreadable pair fails loudly instead of starting a broken listener.
    const server = tls
        ? createTlsServer({ ...serverOptions, key: readFileSync(tls.key), cert: readFileSync(tls.cert) }, handler)
        : createServer(serverOptions, handler);
    server.on('connection', (socket) => {
        sockets.add(socket);
        socket.once('close', () => sockets.delete(socket));
    });
    async function handle(req, res) {
        const supplied = createHash('sha256').update(req.headers.authorization ?? '').digest();
        if (!timingSafeEqual(supplied, expectedAuth)) {
            res.writeHead(401).end();
            return;
        }
        // A correct token past its lifetime is still dead: the lease identity the
        // candidate received must not outlive the host's enforced TTL.
        if (expiresAt !== undefined && Date.now() >= expiresAt) {
            res.writeHead(401, { 'aeval-error-code': 'AEVAL_TOKEN_EXPIRED' }).end();
            return;
        }
        const controller = new AbortController();
        const signal = AbortSignal.any([controller.signal, lease.signal]);
        const disconnected = () => { if (!res.writableFinished)
            controller.abort(); };
        const stopped = () => {
            // A blocked peer cannot be relied upon to read even an error frame.
            if (controller.signal.aborted || !req.complete || res.writableNeedDrain)
                res.destroy();
        };
        res.on('close', disconnected);
        req.on('aborted', disconnected);
        signal.addEventListener('abort', stopped, { once: true });
        try {
            if (req.method === 'GET' && req.url === '/info') {
                res.writeHead(200, { 'content-type': 'application/json', 'cache-control': 'no-store' }).end(JSON.stringify(lease.snapshot()));
                return;
            }
            if (req.method !== 'POST' || req.url !== '/stream') {
                res.writeHead(404).end();
                return;
            }
            if (req.headers['content-type'] !== 'application/json')
                throw new GatewayError('AEVAL_INVALID_REQUEST');
            let size = 0;
            const chunks = [];
            for await (const value of abortableIteration(req, signal)) {
                const chunk = Buffer.from(value);
                size += chunk.length;
                if (size > MAX_WIRE_BYTES)
                    throw new GatewayError('AEVAL_REQUEST_TOO_LARGE');
                chunks.push(chunk);
            }
            const raw = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks)));
            for await (const chunk of lease.stream(raw, signal)) {
                signal.throwIfAborted();
                if (!res.headersSent)
                    res.writeHead(200, { 'content-type': 'application/x-ndjson', 'cache-control': 'no-store' });
                const line = `${JSON.stringify(chunk)}\n`;
                if (Buffer.byteLength(line) > MAX_WIRE_BYTES)
                    throw new GatewayError('AEVAL_INVALID_WIRE');
                if (!res.write(line))
                    await once(res, 'drain', { signal });
            }
            signal.throwIfAborted();
            res.end();
        }
        catch (error) {
            const cause = lease.signal.aborted ? lease.signal.reason : error;
            const reason = cause instanceof GatewayError ? cause.stopReason : 'infra_error';
            const code = error instanceof GatewayError ? error.code : cause instanceof GatewayError ? cause.code : 'AEVAL_INVALID_REQUEST';
            if (res.destroyed)
                return;
            if (controller.signal.aborted || res.writableNeedDrain) {
                res.destroy();
                return;
            }
            if (!res.headersSent)
                res.writeHead(code === 'AEVAL_LEASE_BUSY' ? 409 : 422, { 'aeval-stop-reason': reason, 'aeval-error-code': code }).end();
            else
                res.end(`${JSON.stringify({ type: 'gateway-error', stopReason: reason })}\n`);
        }
        finally {
            signal.removeEventListener('abort', stopped);
            req.removeListener('aborted', disconnected);
            res.removeListener('close', disconnected);
        }
    }
    // The broker's lifetime signal aborts on process shutdown (SIGTERM/SIGINT
    // from the owner), which may legitimately arrive AFTER the trial's
    // descriptor was already settled. The reason stays the fail-closed
    // ``infra_error`` (the broker cannot know whether the run had finished),
    // but the cause names the path so the diagnostic is not misread as a
    // mid-run provider failure (observed in practice).
    const stop = () => lease.stop('infra_error', 'lifetime_abort');
    options.signal.addEventListener('abort', stop, { once: true });
    const timer = options.timeoutMs === undefined ? undefined : setTimeout(() => lease.stop('timeout_killed'), options.timeoutMs);
    timer?.unref();
    try {
        server.listen(port, host);
        await once(server, 'listening', { signal: options.signal });
        options.signal.throwIfAborted();
    }
    catch (error) {
        options.signal.removeEventListener('abort', stop);
        clearTimeout(timer);
        server.closeAllConnections();
        // A listener cancelled before it finished binding has nothing to close;
        // without this callback node reports that as an uncaught 'error' event.
        server.close(() => { });
        throw error;
    }
    const address = server.address();
    if (!address || typeof address === 'string')
        throw new Error('Broker did not bind a TCP port');
    let closing;
    return {
        url: `${tls ? 'https' : 'http'}://${address.family === 'IPv6' ? `[${address.address}]` : address.address}:${address.port}`,
        token,
        lease,
        close(reason = 'infra_error') {
            if (closing)
                return closing;
            lease.stop(reason);
            clearTimeout(timer);
            options.signal.removeEventListener('abort', stop);
            closing = (async () => {
                const closed = new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
                server.closeAllConnections();
                for (const socket of sockets)
                    socket.destroy();
                await Promise.all([...active]);
                await closed;
            })();
            return closing;
        },
    };
}
export function writeJobToken(path, token) {
    if (process.platform === 'win32')
        throw new Error('Job-token files require a POSIX 0600 runtime');
    if (!/^[a-f0-9]{64}$/.test(token))
        throw new Error('Invalid job token');
    writeFileSync(path, token, { mode: 0o600, flag: 'wx' });
}
/**
 * Remove a job-token file this host wrote. Only an existing, owned, 0600
 * regular file that is not a symlink is deleted — anything else at the path
 * refuses removal — and every failure surfaces to the caller, because this
 * runs on the credentialed host side where silent cleanup gaps accumulate.
 */
export function cleanupJobToken(path) {
    if (process.platform === 'win32')
        throw new Error('Job-token files require a POSIX 0600 runtime');
    let stat;
    try {
        stat = lstatSync(path);
    }
    catch (error) {
        if (error.code === 'ENOENT')
            return;
        throw error;
    }
    if (stat.isSymbolicLink() || !stat.isFile() || stat.uid !== process.getuid?.() || (stat.mode & 0o777) !== 0o600) {
        throw new Error('Refusing to remove a job-token file that is not an owned 0600 regular file');
    }
    unlinkSync(path);
}
