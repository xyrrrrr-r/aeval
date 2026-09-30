import { attributionHeaders, LlmError } from '@deepseek-ai/dsh-llm';
import { buildUpstreamRequestBody, readUpstreamKey } from './upstream.js';
const DEFAULT_TIMEOUT_MS = 30_000;
const DEFAULT_MARGIN = 8;
const TIMER_RANGE_MS = 2_147_483_647;
// A count response is a handful of numbers; anything larger is not a count.
const MAX_COUNT_RESPONSE_BYTES = 64 * 1024;
function invalid(message) {
    throw new LlmError(`token count bound: ${message}`, 'INVALID_CONFIG');
}
function countEndpoint(options) {
    const endpoint = options.endpoint ?? `${options.baseUrl.replace(/\/+$/u, '')}/tokens/count`;
    let url;
    try {
        url = new URL(endpoint);
    }
    catch {
        return invalid('endpoint is not a valid URL');
    }
    const loopback = url.hostname === 'localhost' || url.hostname === '[::1]' || /^127(?:\.\d{1,3}){3}$/u.test(url.hostname);
    if (url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback))
        return invalid('endpoint requires HTTPS except for loopback HTTP');
    if (url.username || url.password || url.hash)
        return invalid('endpoint must not carry userinfo or a fragment');
    return endpoint;
}
function counted(raw) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
        throw new LlmError('token count endpoint returned a malformed payload', 'MALFORMED_RESPONSE');
    }
    const value = raw;
    for (const key of ['inputTokens', 'total_tokens']) {
        const count = value[key];
        if (count === undefined)
            continue;
        if (typeof count !== 'number' || !Number.isSafeInteger(count) || count < 0) {
            throw new LlmError(`token count endpoint returned an invalid ${key}`, 'MALFORMED_RESPONSE');
        }
        return count;
    }
    throw new LlmError('token count endpoint returned neither inputTokens nor total_tokens', 'MALFORMED_RESPONSE');
}
async function boundedText(response) {
    if (!response.body)
        throw new LlmError('token count endpoint returned no body', 'MALFORMED_RESPONSE');
    const reader = response.body.getReader();
    const chunks = [];
    let size = 0;
    try {
        for (;;) {
            const { done, value } = await reader.read();
            if (done)
                return new TextDecoder('utf-8', { fatal: true }).decode(Buffer.concat(chunks));
            size += value.byteLength;
            if (size > MAX_COUNT_RESPONSE_BYTES)
                throw new LlmError('token count response exceeds its bound', 'MALFORMED_RESPONSE');
            chunks.push(value);
        }
    }
    finally {
        await reader.cancel().catch(() => { });
        reader.releaseLock();
    }
}
/** Build a provider-backed input-token bound for one upstream route. */
export function createProviderCountBound(options) {
    if (typeof options.baseUrl !== 'string')
        invalid('baseUrl must be a string');
    // Defaulted here, not at the type, so the runtime check below stays the one
    // place an out-of-vocabulary protocol from a parsed config is refused.
    const protocol = options.protocol ?? 'chat_completions';
    if (protocol !== 'chat_completions' && protocol !== 'responses')
        invalid("protocol must be 'chat_completions' or 'responses'");
    const endpoint = countEndpoint(options);
    const margin = options.margin === undefined ? DEFAULT_MARGIN : options.margin;
    if (!Number.isSafeInteger(margin) || margin < 0)
        invalid('margin must be a non-negative safe integer');
    const timeoutMs = options.timeoutMs === undefined ? DEFAULT_TIMEOUT_MS : options.timeoutMs;
    if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > TIMER_RANGE_MS)
        invalid('timeoutMs must be a positive safe integer within timer range');
    const headers = Object.freeze({
        ...attributionHeaders(),
        'content-type': 'application/json',
        authorization: `Bearer ${readUpstreamKey(options.apiKeyEnv)}`,
    });
    return Object.freeze(async (request) => {
        // The deadline is per call: one shared timer would expire the meter for
        // every later request once the broker has been up for timeoutMs.
        const timeout = AbortSignal.timeout(timeoutMs);
        const signal = request.signal === undefined ? timeout : AbortSignal.any([request.signal, timeout]);
        // Count the exact wire body the dispatch adapter will send, so clamping
        // that changes the request is reflected in what gets counted.
        const body = JSON.stringify(buildUpstreamRequestBody(protocol, request.model, request));
        let response;
        try {
            response = await fetch(endpoint, { method: 'POST', headers, body, redirect: 'error', signal });
        }
        catch (error) {
            if (request.signal?.aborted)
                throw error;
            if (timeout.aborted)
                throw new LlmError(`token count endpoint timed out after ${timeoutMs}ms`, 'TIMEOUT');
            throw new LlmError(`token count endpoint failed: ${error instanceof Error ? error.message : String(error)}`, 'TRANSPORT', { cause: error });
        }
        try {
            if (!response.ok)
                throw new LlmError(`token count endpoint answered HTTP ${response.status}`, response.status >= 500 ? 'SERVER' : 'INVALID_REQUEST', { status: response.status });
            const text = await boundedText(response);
            let raw;
            try {
                raw = JSON.parse(text);
            }
            catch {
                throw new LlmError('token count endpoint returned invalid JSON', 'MALFORMED_RESPONSE');
            }
            const value = counted(raw);
            if (value > Number.MAX_SAFE_INTEGER - margin)
                throw new LlmError('token count exceeds the safe integer range', 'MALFORMED_RESPONSE');
            return value + margin;
        }
        finally {
            await response.body?.cancel().catch(() => { });
        }
    });
}
