import { attributionHeaders, LlmAdapter, LlmError, ReasoningEffortId, ToolCallId } from '@deepseek-ai/dsh-llm';
import type {
  ContentBlock,
  FinishReason,
  GenerateOptions,
  LlmProviderInfo,
  LlmResolvedModelInfo,
  RequestMessage,
  StreamChunk,
  TokenUsage,
} from '@deepseek-ai/dsh-llm';

/**
 * The Responses-API production upstream (AGENT-ABSTRACTION-2-PLAN.md §4.4).
 *
 * Some model APIs are served in OpenAI's Responses format rather than chat
 * completions — DeepSeek's is the deployed example: base_url
 * `https://api.deepseek.com`, endpoint `POST /responses` (api-docs.deepseek.com,
 * "Using the Responses API": the endpoint exists "to meet the demand for
 * Codex"). This adapter speaks that wire against the same neutral
 * {@link GenerateOptions} → {@link StreamChunk} contract the chat-completions
 * adapter serves, so the broker's metering, budget, and token-bound semantics
 * are identical in both modes.
 *
 * Wire facts this file encodes (all from the DeepSeek Responses API docs,
 * 2026-09-30):
 *
 * - requests carry `model`, `input` (a string or a list of items:
 *   `message` / `function_call` / `function_call_output` / `reasoning`),
 *   `instructions`, `reasoning.effort`, `max_output_tokens`, `stream`,
 *   `temperature`, `tools` (function only), `tool_choice`, `text.format`;
 *   there is **no `stop` parameter** — an option the wire cannot represent is
 *   refused here rather than silently dropped (dropping it would change what
 *   the meter counts).
 * - the API is stateless: no `previous_response_id`; each turn re-sends the
 *   full history, exactly like the chat wire.
 * - streaming is semantic SSE (`response.created`, `response.output_item.added`,
 *   `response.output_text.delta`, `response.reasoning_text.delta`,
 *   `response.function_call_arguments.delta`, `response.output_item.done`,
 *   …) and the stream ENDS with a terminal `response.completed` /
 *   `response.incomplete` / `response.failed` event — there is no
 *   `data: [DONE]` sentinel.
 * - usage is `input_tokens` (+`input_tokens_details.cached_tokens`),
 *   `output_tokens` (+`output_tokens_details.reasoning_tokens`),
 *   `total_tokens`.
 */

/** One input item exactly as it appears on the `/responses` wire. */
export type ResponsesInputItem =
  | { readonly type: 'message'; readonly role: 'user' | 'assistant' | 'system' | 'developer'; readonly content: string }
  | { readonly type: 'function_call'; readonly call_id: string; readonly name: string; readonly arguments: string }
  | { readonly type: 'function_call_output'; readonly call_id: string; readonly output: string };

/** The exact JSON body POSTed to `/responses` for one request. */
export interface ResponsesBody {
  readonly model: string;
  readonly input: readonly ResponsesInputItem[];
  readonly stream: true;
  readonly instructions?: string;
  readonly max_output_tokens?: number;
  readonly temperature?: number;
  readonly tools?: readonly { readonly type: 'function'; readonly name: string; readonly description: string; readonly parameters: Record<string, unknown> }[];
}

// One SSE line growing past the wire bound can never yield a legal event, so
// refuse it early (same bound as the chat-completions reader).
const MAX_SSE_LINE_BYTES = 8 * 1024 * 1024;

function malformed(message: string): never {
  throw new LlmError(`responses upstream sent a malformed payload: ${message}`, 'MALFORMED_RESPONSE');
}

function objectOf(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    malformed('expected a JSON object');
  }
  return value as Record<string, unknown>;
}

function nonNegativeInt(value: unknown, what: string): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) {
    malformed(`usage has an invalid ${what}`);
  }
  return value;
}

function unsupported(role: string, block: ContentBlock): never {
  throw new LlmError(`responses wire cannot represent a ${block.type} block in ${role} history`, 'UNSUPPORTED_CONTENT');
}

function textOf(message: RequestMessage, role: string): string {
  const parts: string[] = [];
  for (const block of message.content) {
    if (block.type !== 'text') unsupported(role, block);
    parts.push(block.text);
  }
  return parts.join('\n');
}

/**
 * Serialize one provider-neutral request to the exact responses wire body.
 * The meter must count this same body, so the mapping lives here once.
 */
export function buildResponsesBody(model: string, options: Readonly<GenerateOptions>): ResponsesBody {
  // The responses wire has no stop-sequence parameter (the DeepSeek docs list
  // none). Silently dropping the option would dispatch a request whose exact
  // bytes differ from what the meter counted, so it is refused instead — the
  // same fail-closed posture the chat adapter applies to unrepresentable
  // content blocks.
  if (options.stop !== undefined) {
    throw new LlmError('responses wire has no stop parameter; refusing rather than silently dropping it', 'UNSUPPORTED_CONTENT');
  }
  const input: ResponsesInputItem[] = [];
  for (const message of options.messages) {
    switch (message.role) {
      case 'system':
      case 'developer':
      case 'user':
        input.push({ type: 'message', role: message.role, content: textOf(message, message.role) });
        break;
      case 'assistant': {
        // Same honesty rule as the chat wire: historical reasoning stays off
        // the wire (the durable session log keeps it; the model reasons afresh
        // each turn), and one assistant turn folds into one message item plus
        // its function calls, which the server merges back together.
        const parts: string[] = [];
        const calls: { type: 'function_call'; call_id: string; name: string; arguments: string }[] = [];
        for (const block of message.content) {
          if (block.type === 'text') parts.push(block.text);
          else if (block.type === 'reasoning') continue;
          else if (block.type === 'tool-call') {
            calls.push({ type: 'function_call', call_id: block.id, name: block.name, arguments: block.arguments });
          } else unsupported('assistant', block);
        }
        if (parts.length > 0) input.push({ type: 'message', role: 'assistant', content: parts.join('\n') });
        input.push(...calls);
        break;
      }
      case 'tool':
        input.push({ type: 'function_call_output', call_id: message.toolCallId, output: textOf(message, 'tool') });
        break;
    }
  }
  return {
    model,
    input,
    stream: true,
    ...(options.system !== undefined ? { instructions: options.system } : {}),
    // The broker clamps this before dispatch; carrying it on the wire is what
    // makes provider compliance with the output cap observable.
    ...(options.maxTokens !== undefined ? { max_output_tokens: options.maxTokens } : {}),
    ...(options.temperature !== undefined ? { temperature: options.temperature } : {}),
    ...(options.tools !== undefined ? {
      tools: options.tools.map((tool) => ({ type: 'function' as const, name: tool.name, description: tool.description, parameters: tool.parameters })),
    } : {}),
  };
}

function mapUsage(raw: unknown): TokenUsage {
  const usage = objectOf(raw);
  const inputTokens = nonNegativeInt(usage['input_tokens'], 'input_tokens');
  const outputTokens = nonNegativeInt(usage['output_tokens'], 'output_tokens');
  // OpenAI-compatible gateways fold cache hits into input_tokens; the SDK
  // usage contract keeps cached input separate, so subtract it back out.
  const inputDetails = usage['input_tokens_details'] === undefined ? undefined : objectOf(usage['input_tokens_details']);
  const cached = inputDetails === undefined || inputDetails['cached_tokens'] === undefined
    ? 0 : nonNegativeInt(inputDetails['cached_tokens'], 'cached_tokens');
  if (cached > inputTokens) malformed('usage reports more cached than input tokens');
  const outputDetails = usage['output_tokens_details'] === undefined ? undefined : objectOf(usage['output_tokens_details']);
  const reasoning = outputDetails === undefined || outputDetails['reasoning_tokens'] === undefined
    ? undefined : nonNegativeInt(outputDetails['reasoning_tokens'], 'reasoning_tokens');
  const total = usage['total_tokens'] === undefined ? undefined : nonNegativeInt(usage['total_tokens'], 'total_tokens');
  return {
    inputTokens: inputTokens - cached,
    outputTokens,
    // Keep the provider total only when it agrees with the disjoint counters.
    ...(total !== undefined && total === inputTokens + outputTokens ? { totalTokens: total } : {}),
    ...(cached > 0 ? { cacheReadTokens: cached } : {}),
    ...(reasoning !== undefined && reasoning <= outputTokens ? { reasoningTokens: reasoning } : {}),
  };
}

interface OpenBlock {
  readonly index: number;
  readonly kind: 'text' | 'reasoning' | 'tool-call';
  text: string;
  toolId?: string;
  toolName?: string;
  toolArguments: string;
  ended: boolean;
}

/**
 * Read one `/responses` SSE stream into neutral chunks.
 *
 * Terminal semantics: `response.completed` (usage + finish; `tool-calls` when
 * any function-call item was emitted, else `stop`), `response.incomplete`
 * (`max_output_tokens` → max-tokens, `content_filter` → error), and
 * `response.failed` (a provider failure, thrown as `SERVER` — the same class
 * of failure as an upstream HTTP 5xx). A stream that ends without a terminal
 * event is `STREAM_CLOSED`, mirroring the chat reader's missing-`[DONE]`
 * refusal.
 */
async function* readResponsesSse(body: ReadableStream<Uint8Array>, signal: AbortSignal): AsyncIterable<StreamChunk> {
  const reader = body.getReader();
  const decoder = new TextDecoder('utf-8', { fatal: true });
  const byId = new Map<string, OpenBlock>();
  const byIndex = new Map<number, OpenBlock>();
  const order: OpenBlock[] = [];
  let nextIndex = 0;
  let usage: TokenUsage | undefined;
  let finish: FinishReason | undefined;
  let responseId: string | undefined;
  let sawTerminal = false;
  const resolve = (itemId: unknown, outputIndex: number | undefined): OpenBlock | undefined => {
    if (typeof itemId === 'string') {
      const block = byId.get(itemId);
      if (block !== undefined) return block;
    }
    if (outputIndex !== undefined) return byIndex.get(outputIndex);
    return undefined;
  };
  const open = (item: Record<string, unknown>, outputIndex: number | undefined): OpenBlock => {
    // Item kinds outside the neutral contract (e.g. custom_tool_call) are
    // ignored together with their deltas: the neutral wire could not carry
    // them, and inventing a lossy mapping would change what is metered.
    if (item['type'] === 'message' || item['type'] === 'reasoning') {
      const block: OpenBlock = {
        index: nextIndex++,
        kind: item['type'] === 'message' ? 'text' : 'reasoning',
        text: '',
        toolArguments: '',
        ended: false,
      };
      remember(item, outputIndex, block);
      return block;
    }
    if (item['type'] === 'function_call') {
      if (typeof item['call_id'] !== 'string') malformed('function_call item carries no call_id');
      const block: OpenBlock = {
        index: nextIndex++,
        kind: 'tool-call',
        text: '',
        toolId: item['call_id'],
        ...(typeof item['name'] === 'string' ? { toolName: item['name'] } : {}),
        toolArguments: '',
        ended: false,
      };
      remember(item, outputIndex, block);
      return block;
    }
    return blockless(item, outputIndex);
  };
  const remember = (item: Record<string, unknown>, outputIndex: number | undefined, block: OpenBlock): void => {
    const id = typeof item['id'] === 'string' ? item['id'] : `#${block.index}`;
    byId.set(id, block);
    if (outputIndex !== undefined) byIndex.set(outputIndex, block);
    order.push(block);
  };
  // An item we do not model still needs a slot so its later `done` event
  // resolves to something ignorable; a shared sentinel keeps the bookkeeping
  // honest without fabricating a block.
  const ignored: OpenBlock = { index: -1, kind: 'text', text: '', toolArguments: '', ended: true };
  const blockless = (item: Record<string, unknown>, outputIndex: number | undefined): OpenBlock => {
    const id = typeof item['id'] === 'string' ? item['id'] : `#i${outputIndex ?? -1}`;
    byId.set(id, ignored);
    if (outputIndex !== undefined) byIndex.set(outputIndex, ignored);
    return ignored;
  };
  try {
    let buffer = '';
    let sseEvent: string | undefined;
    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        buffer += decoder.decode();
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      let boundary: number;
      while ((boundary = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, boundary).replace(/\r$/u, '');
        buffer = buffer.slice(boundary + 1);
        if (line === '') {
          // An empty line closes the current SSE event block.
          sseEvent = undefined;
          continue;
        }
        if (line.startsWith(':')) continue;
        if (Buffer.byteLength(line) > MAX_SSE_LINE_BYTES) throw new LlmError('responses SSE line exceeds the wire bound', 'MALFORMED_RESPONSE');
        if (line.startsWith('event:')) {
          sseEvent = line.slice(6).trim();
          continue;
        }
        if (!line.startsWith('data:')) throw new LlmError('responses SSE line carries no data field', 'MALFORMED_RESPONSE');
        const payload = line.slice(5).startsWith(' ') ? line.slice(6) : line.slice(5);
        if (payload === '') continue;
        let chunk: unknown;
        try {
          chunk = JSON.parse(payload);
        } catch {
          throw new LlmError('responses SSE data line is not valid JSON', 'MALFORMED_RESPONSE');
        }
        const event = objectOf(chunk);
        // OpenAI frames the type on the SSE `event:` line; DeepSeek's docs
        // describe it carried by the payload. Accept both, payload first.
        const kind = typeof event['type'] === 'string' ? event['type'] : typeof event['event'] === 'string' ? event['event'] : sseEvent;
        if (kind === undefined) malformed('event carries no type');
        const outputIndex = event['output_index'] === undefined ? undefined : nonNegativeInt(event['output_index'], 'output_index');
        switch (kind) {
          case 'response.created':
          case 'response.in_progress':
          case 'response.queued': {
            const response = objectOf(event['response']);
            if (responseId === undefined && typeof response['id'] === 'string') responseId = response['id'];
            break;
          }
          case 'response.output_item.added': {
            const block = open(objectOf(event['item']), outputIndex);
            if (block !== ignored) yield { type: 'block-start', index: block.index, blockType: block.kind };
            break;
          }
          case 'response.output_item.done': {
            const item = objectOf(event['item']);
            const block = resolve(event['item_id'] ?? item['id'], outputIndex) ?? open(item, outputIndex);
            if (block === ignored) break;
            if (block.kind === 'tool-call') {
              if (typeof item['call_id'] === 'string') block.toolId ??= item['call_id'];
              if (typeof item['name'] === 'string') block.toolName ??= item['name'];
              if (block.toolId === undefined || block.toolName === undefined) {
                malformed('function_call item never received a call_id or name');
              }
            }
            const assembled: ContentBlock = block.kind === 'text' ? { type: 'text', text: block.text }
              : block.kind === 'reasoning' ? { type: 'reasoning', text: block.text }
                : { type: 'tool-call', id: ToolCallId(block.toolId!), name: block.toolName!, arguments: block.toolArguments };
            block.ended = true;
            yield { type: 'block-end', index: block.index, block: assembled };
            break;
          }
          case 'response.output_text.delta': {
            const text = deltaText(event);
            const block = resolve(event['item_id'], outputIndex);
            if (block === undefined || block === ignored || block.kind !== 'text') {
              malformed('output_text delta has no open message item');
            }
            block.text += text;
            yield { type: 'text-delta', index: block.index, text };
            break;
          }
          case 'response.reasoning_text.delta': {
            const text = deltaText(event);
            const block = resolve(event['item_id'], outputIndex);
            if (block === undefined || block === ignored || block.kind !== 'reasoning') {
              malformed('reasoning_text delta has no open reasoning item');
            }
            block.text += text;
            yield { type: 'reasoning-delta', index: block.index, text };
            break;
          }
          case 'response.function_call_arguments.delta': {
            const text = deltaText(event);
            const block = resolve(event['item_id'], outputIndex);
            if (block === undefined || block === ignored || block.kind !== 'tool-call') {
              malformed('function_call_arguments delta has no open function_call item');
            }
            block.toolArguments += text;
            yield {
              type: 'tool-call-delta',
              index: block.index,
              id: ToolCallId(block.toolId!),
              ...(block.toolName !== undefined ? { name: block.toolName } : {}),
              argumentsDelta: text,
            };
            break;
          }
          case 'response.completed': {
            const response = objectOf(event['response']);
            if (responseId === undefined && typeof response['id'] === 'string') responseId = response['id'];
            usage = mapUsage(response['usage']);
            finish = order.some((block) => block !== ignored && block.kind === 'tool-call')
              ? { kind: 'tool-calls' }
              : { kind: 'stop' };
            sawTerminal = true;
            break;
          }
          case 'response.incomplete': {
            const response = objectOf(event['response']);
            if (responseId === undefined && typeof response['id'] === 'string') responseId = response['id'];
            usage = mapUsage(response['usage']);
            const details = response['incomplete_details'] === undefined ? {} : objectOf(response['incomplete_details']);
            const reason = details['reason'];
            finish = reason === 'max_output_tokens' ? { kind: 'max-tokens' }
              : reason === 'content_filter'
                ? { kind: 'error', failure: { code: 'CONTENT_FILTER', message: 'responses upstream stopped the response with a content filter' } }
                : { kind: 'error', failure: { code: 'UNSUPPORTED_FINISH', message: `responses upstream ended incomplete with ${String(reason)}` } };
            sawTerminal = true;
            break;
          }
          case 'response.failed': {
            const response = objectOf(event['response']);
            const error = response['error'] === undefined ? {} : objectOf(response['error']);
            throw new LlmError(`responses upstream failed: ${typeof error['message'] === 'string' ? error['message'] : 'no message'}`, 'SERVER');
          }
          default:
            // A typed event this reader does not model (content_part.*,
            // *.done variants, custom_tool_call_input.*): forward-compatible
            // ignore — the neutral stream is already complete without them.
            break;
        }
        if (sawTerminal) break;
      }
      signal.throwIfAborted();
      if (sawTerminal) break;
      if (Buffer.byteLength(buffer) > MAX_SSE_LINE_BYTES) throw new LlmError('responses SSE line exceeds the wire bound', 'MALFORMED_RESPONSE');
    }
    signal.throwIfAborted();
    if (!sawTerminal) throw new LlmError('responses stream ended without a terminal event', 'STREAM_CLOSED');
    // Defensive close: a provider that skipped an item's `done` event still
    // owes the neutral stream its block-end.
    for (const block of order) {
      if (block === ignored || block.ended) continue;
      const assembled: ContentBlock = block.kind === 'text' ? { type: 'text', text: block.text }
        : block.kind === 'reasoning' ? { type: 'reasoning', text: block.text }
          : { type: 'tool-call', id: ToolCallId(block.toolId!), name: block.toolName!, arguments: block.toolArguments };
      block.ended = true;
      yield { type: 'block-end', index: block.index, block: assembled };
    }
    if (usage === undefined) throw new LlmError('responses stream ended without usage', 'MALFORMED_RESPONSE');
    yield { type: 'usage', usage };
    if (finish === undefined) throw new LlmError('responses stream ended without a finish reason', 'MALFORMED_RESPONSE');
    yield { type: 'finish', reason: finish, ...(responseId !== undefined ? { replayState: { response: { id: responseId } } } : {}) };
  } finally {
    try { await reader.cancel().catch(() => {}); } catch { /* A finished stream needs no cancellation. */ }
    reader.releaseLock();
  }
}

function deltaText(event: Record<string, unknown>): string {
  if (typeof event['delta'] !== 'string') malformed('delta event carries no text');
  return event['delta'];
}

/** The `/responses` counterpart of the chat-completions upstream adapter. */
export class ResponsesAdapter extends LlmAdapter {
  readonly #model: string;
  readonly #url: string;
  readonly #headers: Record<string, string>;
  // The bearer credential lives only here and in the request header built
  // from it; no log line or error message ever renders this value.
  readonly #key: string;
  readonly #timeoutMs: number | undefined;
  readonly #efforts: readonly { id: ReturnType<typeof ReasoningEffortId>; name: string }[];

  constructor(model: string, url: string, headers: Record<string, string>, key: string, timeoutMs: number | undefined, efforts: readonly string[]) {
    super();
    this.#model = model;
    this.#url = url;
    this.#headers = Object.freeze({ ...headers });
    this.#key = key;
    this.#timeoutMs = timeoutMs;
    this.#efforts = Object.freeze(efforts.map((id) => Object.freeze({ id: ReasoningEffortId(id), name: id })));
  }

  override providerInfo(provider: string): LlmProviderInfo {
    return { id: provider, name: provider };
  }

  // Model resolution is a pure identity echo: the responses wire offers no
  // discovery endpoint, so this never touches the network.
  override async resolveModel(provider: string, model: string): Promise<LlmResolvedModelInfo> {
    return {
      provider,
      id: model,
      name: model,
      ...(this.#efforts.length > 0 ? { reasoning: { efforts: this.#efforts } } : {}),
    };
  }

  override async *stream(options: GenerateOptions): AsyncIterable<StreamChunk> {
    const callerSignal = options.signal;
    const controller = new AbortController();
    const timeout = this.#timeoutMs === undefined ? undefined : AbortSignal.timeout(this.#timeoutMs);
    const signal = timeout === undefined
      ? (callerSignal ?? controller.signal)
      : AbortSignal.any(callerSignal === undefined ? [timeout, controller.signal] : [callerSignal, timeout, controller.signal]);
    let response: Response | undefined;
    try {
      const body = buildResponsesBody(this.#model, options);
      response = await fetch(this.#url, {
        method: 'POST',
        headers: { ...this.#headers, ...attributionHeaders(), 'content-type': 'application/json', authorization: `Bearer ${this.#key}` },
        body: JSON.stringify(body),
        redirect: 'error',
        signal,
      });
      if (!response.ok) {
        await response.body?.cancel();
        const code = response.status === 401 || response.status === 403 ? 'AUTH' : response.status === 429 ? 'RATE_LIMIT' : response.status >= 500 ? 'SERVER' : 'INVALID_REQUEST';
        throw new LlmError(`responses upstream answered HTTP ${response.status}`, code, { status: response.status });
      }
      if (!response.body) throw new LlmError('responses upstream returned no body', 'SERVER');
      yield* readResponsesSse(response.body, signal);
    } catch (error) {
      if (error instanceof LlmError) throw error;
      if (callerSignal?.aborted) throw error;
      if (timeout?.aborted) throw new LlmError(`responses upstream timed out after ${this.#timeoutMs}ms`, 'TIMEOUT');
      throw new LlmError(`responses upstream failed: ${error instanceof Error ? error.message : String(error)}`, 'TRANSPORT', { cause: error });
    } finally {
      controller.abort();
      const body = response?.body;
      if (body) { try { void body.cancel().catch(() => {}); } catch { /* The reader already released the stream. */ } }
    }
  }
}
