import { attributionHeaders, LlmAdapter, LlmError, normalizeApiKey, ReasoningEffortId, ToolCallId } from '@deepseek-ai/dsh-llm';
import { ResponsesAdapter, buildResponsesBody } from './upstream_responses.js';
// One SSE data line carries one completion chunk; a line growing past the
// broker's wire bound can never yield a legal chunk, so refuse it early.
const MAX_SSE_LINE_BYTES = 8 * 1024 * 1024;
const TIMER_RANGE_MS = 2_147_483_647;
function invalid(message) {
    throw new LlmError(`upstream adapter: ${message}`, 'INVALID_CONFIG');
}
function objectOf(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) {
        throw new LlmError('chat-completions upstream sent a malformed payload', 'MALFORMED_RESPONSE');
    }
    return value;
}
function nonNegativeInt(value, what) {
    if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
        throw new LlmError(`chat-completions usage has an invalid ${what}`, 'MALFORMED_RESPONSE');
    }
    return value;
}
/** Read and validate the API key; the value itself never enters the message. */
export function readUpstreamKey(apiKeyEnv) {
    const raw = process.env[apiKeyEnv];
    if (raw === undefined) {
        throw new LlmError(`upstream API key is not set; export ${apiKeyEnv} in the broker environment`, 'MISSING_CREDENTIAL');
    }
    const check = normalizeApiKey(raw);
    if (!check.ok) {
        throw new LlmError(`upstream API key in ${apiKeyEnv} is not usable (${check.reason})`, 'INVALID_CREDENTIAL');
    }
    return check.value;
}
// The provider credential must never cross a plaintext hop: the same
// loopback-or-TLS rule the broker applies to its own listener.
function httpBase(value) {
    let url;
    try {
        url = new URL(value);
    }
    catch {
        return invalid('baseUrl is not a valid URL');
    }
    const loopback = url.hostname === 'localhost' || url.hostname === '[::1]' || /^127(?:\.\d{1,3}){3}$/u.test(url.hostname);
    if (url.protocol !== 'https:' && !(url.protocol === 'http:' && loopback))
        return invalid('baseUrl requires HTTPS except for loopback HTTP');
    if (url.username || url.password || url.search || url.hash)
        return invalid('baseUrl must not carry userinfo, query, or fragment');
    return value.replace(/\/+$/u, '');
}
function httpFailure(status) {
    const code = status === 401 || status === 403 ? 'AUTH' : status === 429 ? 'RATE_LIMIT' : status >= 500 ? 'SERVER' : 'INVALID_REQUEST';
    return new LlmError(`chat-completions upstream answered HTTP ${status}`, code, { status });
}
function unsupported(role, block) {
    throw new LlmError(`chat-completions cannot represent a ${block.type} block in ${role} history`, 'UNSUPPORTED_CONTENT');
}
function textOf(message, role) {
    const parts = [];
    for (const block of message.content) {
        if (block.type !== 'text')
            unsupported(role, block);
        parts.push(block.text);
    }
    return parts.join('\n');
}
function wireMessage(message) {
    switch (message.role) {
        case 'system':
        case 'developer':
        case 'user':
            return { role: message.role, content: textOf(message, message.role) };
        case 'assistant': {
            let content;
            const calls = [];
            for (const block of message.content) {
                if (block.type === 'text') {
                    content = content === undefined ? block.text : `${content}\n${block.text}`;
                }
                // The chat-completions wire has no input slot for prior-turn reasoning
                // and DeepSeek documents a 400 for reasoning_content in input, so
                // historical reasoning stays off the wire; the durable session log
                // keeps it and the model reasons afresh each turn.
                else if (block.type === 'reasoning')
                    continue;
                else if (block.type === 'tool-call') {
                    calls.push({ id: block.id, type: 'function', function: { name: block.name, arguments: block.arguments } });
                }
                else
                    unsupported('assistant', block);
            }
            return { role: 'assistant', ...(content !== undefined ? { content } : {}), ...(calls.length > 0 ? { tool_calls: calls } : {}) };
        }
        case 'tool':
            return { role: 'tool', tool_call_id: message.toolCallId, content: textOf(message, 'tool') };
    }
}
/**
 * Serialize one provider-neutral request to the exact chat-completions wire
 * body. The meter must count this same body, so the mapping lives here once.
 */
export function buildChatCompletionsBody(model, options) {
    const messages = [];
    if (options.system !== undefined)
        messages.push({ role: 'system', content: options.system });
    for (const message of options.messages)
        messages.push(wireMessage(message));
    return {
        model,
        messages,
        stream: true,
        // The broker clamps this before dispatch; carrying it on the wire is what
        // makes provider compliance with the output cap observable.
        ...(options.maxTokens !== undefined ? { max_tokens: options.maxTokens } : {}),
        ...(options.temperature !== undefined ? { temperature: options.temperature } : {}),
        ...(options.stop !== undefined ? { stop: options.stop } : {}),
        ...(options.tools !== undefined ? {
            tools: options.tools.map((tool) => ({ type: 'function', function: { name: tool.name, description: tool.description, parameters: tool.parameters } })),
        } : {}),
    };
}
function mapUsage(raw) {
    const usage = objectOf(raw);
    const prompt = nonNegativeInt(usage['prompt_tokens'], 'prompt_tokens');
    const completion = nonNegativeInt(usage['completion_tokens'], 'completion_tokens');
    const promptDetails = usage['prompt_tokens_details'] === undefined ? undefined : objectOf(usage['prompt_tokens_details']);
    // OpenAI-compatible gateways fold cache hits into prompt_tokens; the SDK
    // usage contract keeps cached input separate, so subtract it back out.
    const cached = promptDetails === undefined
        ? nonNegativeInt(usage['prompt_cache_hit_tokens'] ?? 0, 'prompt_cache_hit_tokens')
        : nonNegativeInt(promptDetails['cached_tokens'] ?? 0, 'cached_tokens');
    if (cached > prompt)
        throw new LlmError('chat-completions usage reports more cached than prompt tokens', 'MALFORMED_RESPONSE');
    const completionDetails = usage['completion_tokens_details'] === undefined ? undefined : objectOf(usage['completion_tokens_details']);
    const reasoning = completionDetails === undefined || completionDetails['reasoning_tokens'] === undefined
        ? undefined : nonNegativeInt(completionDetails['reasoning_tokens'], 'reasoning_tokens');
    const total = usage['total_tokens'] === undefined ? undefined : nonNegativeInt(usage['total_tokens'], 'total_tokens');
    return {
        inputTokens: prompt - cached,
        outputTokens: completion,
        // Keep the provider total only when it agrees with the disjoint counters.
        ...(total !== undefined && total === prompt + completion ? { totalTokens: total } : {}),
        ...(cached > 0 ? { cacheReadTokens: cached } : {}),
        ...(reasoning !== undefined && reasoning <= completion ? { reasoningTokens: reasoning } : {}),
    };
}
function mapFinish(reason) {
    switch (reason) {
        case 'stop': return { kind: 'stop' };
        case 'tool_calls':
        case 'function_call': return { kind: 'tool-calls' };
        case 'length': return { kind: 'max-tokens' };
        case 'content_filter': return { kind: 'error', failure: { code: 'CONTENT_FILTER', message: 'chat-completions upstream stopped the response with a content filter' } };
        default: return { kind: 'error', failure: { code: 'UNSUPPORTED_FINISH', message: `chat-completions upstream finished with ${reason}` } };
    }
}
class ChatCompletionsAdapter extends LlmAdapter {
    #model;
    #url;
    #headers;
    // The bearer credential lives only here and in the request header built
    // from it; no log line or error message ever renders this value.
    #key;
    #timeoutMs;
    #efforts;
    #contextWindow;
    constructor(model, url, headers, key, timeoutMs, efforts, contextWindow) {
        super();
        this.#model = model;
        this.#url = url;
        this.#headers = Object.freeze({ ...headers });
        this.#key = key;
        this.#timeoutMs = timeoutMs;
        this.#efforts = Object.freeze(efforts.map((id) => Object.freeze({ id: ReasoningEffortId(id), name: id })));
        this.#contextWindow = contextWindow;
    }
    providerInfo(provider) {
        return { id: provider, name: provider };
    }
    // Model resolution is a pure identity echo: the chat-completions wire offers
    // no discovery endpoint, so this never touches the network. The owner-declared
    // context capacity is echoed alongside the identity so the harness seals it
    // into the request/context event.
    async resolveModel(provider, model) {
        return {
            provider,
            id: model,
            name: model,
            ...(this.#efforts.length > 0 ? { reasoning: { efforts: this.#efforts } } : {}),
            ...(this.#contextWindow !== undefined ? { context: { contextWindow: this.#contextWindow } } : {}),
        };
    }
    async *stream(options) {
        const callerSignal = options.signal;
        const controller = new AbortController();
        const timeout = this.#timeoutMs === undefined ? undefined : AbortSignal.timeout(this.#timeoutMs);
        const signal = timeout === undefined
            ? (callerSignal ?? controller.signal)
            : AbortSignal.any(callerSignal === undefined ? [timeout, controller.signal] : [callerSignal, timeout, controller.signal]);
        let response;
        try {
            const body = buildChatCompletionsBody(this.#model, options);
            response = await fetch(this.#url, {
                method: 'POST',
                headers: { ...this.#headers, ...attributionHeaders(), 'content-type': 'application/json', authorization: `Bearer ${this.#key}` },
                body: JSON.stringify(body),
                redirect: 'error',
                signal,
            });
            if (!response.ok) {
                await response.body?.cancel();
                throw httpFailure(response.status);
            }
            if (!response.body)
                throw new LlmError('chat-completions upstream returned no body', 'SERVER');
            yield* readChatCompletionsSse(response.body, signal);
        }
        catch (error) {
            if (error instanceof LlmError)
                throw error;
            if (callerSignal?.aborted)
                throw error;
            if (timeout?.aborted)
                throw new LlmError(`chat-completions upstream timed out after ${this.#timeoutMs}ms`, 'TIMEOUT');
            throw new LlmError(`chat-completions upstream failed: ${error instanceof Error ? error.message : String(error)}`, 'TRANSPORT', { cause: error });
        }
        finally {
            controller.abort();
            const body = response?.body;
            if (body) {
                try {
                    void body.cancel().catch(() => { });
                }
                catch { /* The reader already released the stream. */ }
            }
        }
    }
}
async function* readChatCompletionsSse(body, signal) {
    const reader = body.getReader();
    const decoder = new TextDecoder('utf-8', { fatal: true });
    const channels = new Map();
    const order = [];
    let nextIndex = 0;
    let finishReason;
    let usage;
    let responseId;
    let sawDone = false;
    const channel = (key, kind) => {
        let block = channels.get(key);
        if (block === undefined) {
            block = { index: nextIndex++, kind, text: '', toolArguments: '' };
            channels.set(key, block);
            order.push(block);
            return { block, fresh: true };
        }
        return { block, fresh: false };
    };
    try {
        let buffer = '';
        for (;;) {
            const { done, value } = await reader.read();
            if (done) {
                buffer += decoder.decode();
                break;
            }
            buffer += decoder.decode(value, { stream: true });
            let boundary;
            while ((boundary = buffer.indexOf('\n')) !== -1) {
                const line = buffer.slice(0, boundary).replace(/\r$/u, '');
                buffer = buffer.slice(boundary + 1);
                if (line === '' || line.startsWith(':'))
                    continue;
                if (Buffer.byteLength(line) > MAX_SSE_LINE_BYTES)
                    throw new LlmError('chat-completions SSE line exceeds the wire bound', 'MALFORMED_RESPONSE');
                if (!line.startsWith('data:'))
                    throw new LlmError('chat-completions SSE line carries no data field', 'MALFORMED_RESPONSE');
                const payload = line.slice(5).startsWith(' ') ? line.slice(6) : line.slice(5);
                if (payload === '[DONE]') {
                    sawDone = true;
                    break;
                }
                let chunk;
                try {
                    chunk = JSON.parse(payload);
                }
                catch {
                    throw new LlmError('chat-completions SSE data line is not valid JSON', 'MALFORMED_RESPONSE');
                }
                const event = objectOf(chunk);
                if (typeof event['id'] === 'string' && responseId === undefined)
                    responseId = event['id'];
                if (event['usage'] !== undefined && event['usage'] !== null)
                    usage = event['usage'];
                const choices = event['choices'];
                if (choices === undefined)
                    continue;
                if (!Array.isArray(choices))
                    throw new LlmError('chat-completions chunk has invalid choices', 'MALFORMED_RESPONSE');
                const choice = choices[0];
                if (choice === undefined)
                    continue;
                const selected = objectOf(choice);
                const delta = selected['delta'] === undefined ? {} : objectOf(selected['delta']);
                if (typeof delta['content'] === 'string' && delta['content'] !== '') {
                    const { block, fresh } = channel('text', 'text');
                    if (fresh)
                        yield { type: 'block-start', index: block.index, blockType: 'text' };
                    block.text += delta['content'];
                    yield { type: 'text-delta', index: block.index, text: delta['content'] };
                }
                if (typeof delta['reasoning_content'] === 'string' && delta['reasoning_content'] !== '') {
                    const { block, fresh } = channel('reasoning', 'reasoning');
                    if (fresh)
                        yield { type: 'block-start', index: block.index, blockType: 'reasoning' };
                    block.text += delta['reasoning_content'];
                    yield { type: 'reasoning-delta', index: block.index, text: delta['reasoning_content'] };
                }
                if (Array.isArray(delta['tool_calls'])) {
                    for (const entry of delta['tool_calls']) {
                        const call = objectOf(entry);
                        const slot = call['index'] === undefined ? 0 : nonNegativeInt(call['index'], 'tool_calls index');
                        const { block, fresh } = channel(`tool:${slot}`, 'tool-call');
                        if (typeof call['id'] === 'string')
                            block.toolId = call['id'];
                        const fn = call['function'] === undefined ? {} : objectOf(call['function']);
                        if (typeof fn['name'] === 'string')
                            block.toolName = fn['name'];
                        const argumentsDelta = typeof fn['arguments'] === 'string' ? fn['arguments'] : '';
                        if (block.toolId === undefined)
                            throw new LlmError('chat-completions tool call delta carries no id', 'MALFORMED_RESPONSE');
                        block.toolArguments += argumentsDelta;
                        if (fresh)
                            yield { type: 'block-start', index: block.index, blockType: 'tool-call' };
                        yield {
                            type: 'tool-call-delta',
                            index: block.index,
                            id: ToolCallId(block.toolId),
                            ...(typeof fn['name'] === 'string' ? { name: fn['name'] } : {}),
                            argumentsDelta,
                        };
                    }
                }
                if (selected['finish_reason'] !== undefined && selected['finish_reason'] !== null) {
                    if (typeof selected['finish_reason'] !== 'string')
                        throw new LlmError('chat-completions chunk has an invalid finish_reason', 'MALFORMED_RESPONSE');
                    finishReason ??= selected['finish_reason'];
                }
            }
            signal.throwIfAborted();
            if (sawDone)
                break;
            if (Buffer.byteLength(buffer) > MAX_SSE_LINE_BYTES)
                throw new LlmError('chat-completions SSE line exceeds the wire bound', 'MALFORMED_RESPONSE');
        }
        signal.throwIfAborted();
        if (!sawDone)
            throw new LlmError('chat-completions stream ended without [DONE]', 'STREAM_CLOSED');
        for (const block of order) {
            if (block.kind === 'tool-call' && (block.toolId === undefined || block.toolName === undefined)) {
                throw new LlmError('chat-completions tool call never received an id or name', 'MALFORMED_RESPONSE');
            }
            const assembled = block.kind === 'text' ? { type: 'text', text: block.text }
                : block.kind === 'reasoning' ? { type: 'reasoning', text: block.text }
                    : { type: 'tool-call', id: ToolCallId(block.toolId), name: block.toolName, arguments: block.toolArguments };
            yield { type: 'block-end', index: block.index, block: assembled };
        }
        if (usage === undefined)
            throw new LlmError('chat-completions stream ended without usage', 'MALFORMED_RESPONSE');
        yield { type: 'usage', usage: mapUsage(usage) };
        if (finishReason === undefined)
            throw new LlmError('chat-completions stream ended without finish_reason', 'MALFORMED_RESPONSE');
        yield { type: 'finish', reason: mapFinish(finishReason), ...(responseId !== undefined ? { replayState: { response: { id: responseId } } } : {}) };
    }
    finally {
        try {
            await reader.cancel().catch(() => { });
        }
        catch { /* A finished stream needs no cancellation. */ }
        reader.releaseLock();
    }
}
/** Build the exact request body a dispatch over `protocol` would send. */
export function buildUpstreamRequestBody(protocol, model, options) {
    return protocol === 'responses' ? buildResponsesBody(model, options) : buildChatCompletionsBody(model, options);
}
/** Build the production upstream adapter for one provider route. */
export function createUpstreamAdapter(options) {
    if (typeof options.provider !== 'string' || options.provider.trim() === '' || /\s/u.test(options.provider))
        invalid('provider must be a non-empty identifier without whitespace');
    if (typeof options.model !== 'string' || options.model.trim() === '' || /\s/u.test(options.model))
        invalid('model must be a non-empty identifier without whitespace');
    if (typeof options.apiKeyEnv !== 'string' || !/^[A-Za-z_][A-Za-z0-9_]*$/u.test(options.apiKeyEnv))
        invalid('apiKeyEnv must be an environment variable name');
    if (typeof options.baseUrl !== 'string')
        invalid('baseUrl must be a string');
    // The protocol defaults to chat_completions: a spec written before the
    // responses mode existed must keep producing the same adapter, URL, and
    // bytes it always did (zero drift for sealed evidence).
    const protocol = options.protocol ?? 'chat_completions';
    if (protocol !== 'chat_completions' && protocol !== 'responses')
        invalid("protocol must be 'chat_completions' or 'responses'");
    if (options.timeoutMs !== undefined && (!Number.isSafeInteger(options.timeoutMs) || options.timeoutMs < 1 || options.timeoutMs > TIMER_RANGE_MS))
        invalid('timeoutMs must be a positive safe integer within timer range');
    // The context capacity is descriptive metadata, not a request bound: it only
    // has to be a positive token count. Refuse a non-integer or non-positive value
    // rather than seal a nonsense window into evidence.
    if (options.contextWindow !== undefined && (!Number.isSafeInteger(options.contextWindow) || options.contextWindow < 1))
        invalid('contextWindow must be a positive safe integer');
    if (options.headers !== undefined && (typeof options.headers !== 'object' || options.headers === null || Array.isArray(options.headers)
        || Object.entries(options.headers).some(([name, value]) => typeof name !== 'string' || typeof value !== 'string' || /[\r\n]/u.test(name + value))))
        invalid('headers must map header names to values');
    let efforts = [];
    if (options.reasoningEfforts !== undefined) {
        if (!Array.isArray(options.reasoningEfforts))
            invalid('reasoningEfforts must be an array of effort ids');
        const seen = new Set();
        for (const id of options.reasoningEfforts) {
            if (typeof id !== 'string' || id.trim() === '' || /\s/u.test(id))
                invalid('reasoningEfforts must contain non-empty identifiers without whitespace');
            if (seen.has(id))
                invalid('reasoningEfforts must not contain duplicates');
            seen.add(id);
        }
        efforts = options.reasoningEfforts;
    }
    const baseUrl = httpBase(options.baseUrl);
    const key = readUpstreamKey(options.apiKeyEnv);
    if (protocol === 'responses') {
        return new ResponsesAdapter(options.model, `${baseUrl}/responses`, options.headers ?? {}, key, options.timeoutMs, efforts, options.contextWindow);
    }
    return new ChatCompletionsAdapter(options.model, `${baseUrl}/chat/completions`, options.headers ?? {}, key, options.timeoutMs, efforts, options.contextWindow);
}
